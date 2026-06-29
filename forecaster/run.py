"""Orchestrate ingest -> features -> model -> static JSON, per enabled zone.

Supports sharding so a CI matrix can build zones in parallel across runners
(each runner has its own IP, which sidesteps Energy-Charts per-IP rate limits).
"""
import argparse
import json
import os
import pathlib

import numpy as np
import pandas as pd
import yaml

from . import features, ingest, learn, model, taxes

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = pathlib.Path(os.environ.get("FORECAST_OUT") or (ROOT / "public" / "data"))
# Previously-committed state is always read from the canonical location, even when
# a sharded build writes its fresh output to a clean per-shard directory.
STATE_READ = ROOT / "public" / "data" / "state"


def load_config():
    with open(ROOT / "zones.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _round(arr, nd=4):
    out = []
    for v in np.asarray(arr, dtype=float):
        out.append(None if not np.isfinite(v) else round(float(v), nd))
    return out


def run_zone(code, cfg, gas=None, co2=None):
    z = cfg["zones"][code]
    d = cfg["defaults"]
    token = os.environ.get("ENTSOE_TOKEN", "").strip()

    now = pd.Timestamp.now(tz="UTC").floor("h")
    start = now - pd.Timedelta(days=d["history_days_export"])

    # PRIMARY price source: Energy-Charts (token-free)
    price = ingest.fetch_energy_charts_prices(z["entsoe_eic_bzn"], start,
                                              now + pd.Timedelta(days=2))["price_eur_mwh"]
    # OPTIONAL validation: ENTSO-E (only when a token is configured and the zone has an EIC)
    eic = z.get("entsoe_eic")
    entsoe = (ingest.fetch_dayahead_prices(eic, start,
                                           now + pd.Timedelta(days=2), token)["price_eur_mwh"]
              if (token and eic) else pd.Series(dtype=float))

    # Weather is only needed for the training window + forecast, not the full
    # 730-day price-history export. Fetch a shorter window to keep the run fast.
    train_days = d.get("history_days_train", 365)
    wx_start = now - pd.Timedelta(days=train_days + 10)
    weather = ingest.fetch_weather(z["weather_points"], wx_start.date(), now.date())
    if gas is None:
        gas = ingest.fetch_yahoo_daily(d["gas_symbol"])
    if co2 is None:
        co2 = ingest.fetch_yahoo_daily(d["co2_symbol"])

    feats = features.assemble(weather, gas, co2, z["timezone"], z["holidays"])
    priced = bool(z.get("priced")) and "taxes" in z

    # ---- exogenous residual-demand feature (single-zone countries only) ----
    # Residual load = demand minus wind & solar; it drives the evening peak.
    # It is only available historically, so we forecast it from weather+calendar
    # and feed it to the price model as a stacked feature.
    feat_cols = list(features.FEATURES)
    pp_country = z.get("public_power_country")
    if pp_country and not feats.empty:
        try:
            resid = ingest.fetch_residual_load(pp_country, wx_start.date(), now.date())
            resid = resid.reindex(feats.index) if len(resid) else pd.Series(dtype=float)
            known = feats.notna().all(axis=1) & resid.notna()
            if int(known.sum()) > 500:
                # Use the PREDICTED residual load everywhere (not the actual on
                # history) so the feature has the same quality at train and serve
                # time -- avoids stacking leakage / train-serve skew.
                pred_all = model.predict_feature(
                    feats.loc[known, feat_cols], resid.loc[known].values, feats[feat_cols])
                feats["resid_load"] = pred_all
                feat_cols.append("resid_load")
                result_resid = True
            else:
                result_resid = False
        except Exception:
            result_resid = False
    else:
        result_resid = False

    # ---- ENTSO-E day-ahead load + wind/solar forecast (residual-demand forecast) ----
    # The market's own next-day residual demand drives the price peak. Available
    # for D+1 from ENTSO-E; beyond that we fall back to a weather+calendar estimate
    # so the feature is defined across the whole horizon. Needs a token + EIC.
    eic_fc = z.get("entsoe_eic")
    if token and eic_fc and not feats.empty:
        try:
            efc, _ld, _gn = ingest.fetch_entsoe_residual_forecast(
                eic_fc, wx_start, now + pd.Timedelta(days=2), token)
            efc = efc.reindex(feats.index) if len(efc) else pd.Series(dtype=float)
            known = feats[list(features.FEATURES)].notna().all(axis=1) & efc.notna()
            if int(known.sum()) > 500:
                # smooth weather+calendar model of the ENTSO-E residual forecast,
                # used to fill hours beyond D+1 (and avoid train/serve skew)
                pred = model.predict_feature(
                    feats.loc[known, list(features.FEATURES)], efc.loc[known].values,
                    feats[list(features.FEATURES)])
                blended = efc.copy()
                blended[blended.isna()] = pred[blended.isna().values]
                feats["entso_resid_fc"] = blended.values
                feat_cols.append("entso_resid_fc")
                result_entso_fc = True
            else:
                result_entso_fc = False
        except Exception:
            result_entso_fc = False
    else:
        result_entso_fc = False

    result = {
        "zone": code, "name": z["name"], "priced": priced,
        "generated_at": now.isoformat(), "timezone": z["timezone"],
        "history_days": d["history_days_export"], "horizon_days": d["forecast_horizon_days"],
        "unit": "EUR/kWh", "price_source": "energy-charts",
        "entsoe_available": bool(token and len(entsoe.dropna()) > 0),
        "resid_demand": bool(result_resid),
    }
    if priced:
        result["taxes"] = z["taxes"]

    if feats.empty:
        result.update(history=None, forecast=None, error="no_weather_data")
        _write(code, result)
        return result

    # Keep the full price series for the (730-day) history display; build a
    # feature-aligned copy only for training/validation.
    price_feat = price.reindex(feats.index) if len(price) else pd.Series(np.nan, index=feats.index)
    entsoe = entsoe.reindex(feats.index) if len(entsoe) else pd.Series(np.nan, index=feats.index)

    # ---- price-lag features (cheap, ~15% MAE gain in backtest) ----
    # lag24 = same hour yesterday, lag168 = same hour last week, roll7 = 7-day
    # hourly average. For future hours without a real lag, fall back to roll7 so
    # the feature is always defined; per-zone auto-gating keeps them only if useful.
    roll7 = price_feat.shift(24).rolling(168, min_periods=24).mean().ffill().bfill()
    lag24 = price_feat.shift(24); lag24 = lag24.where(lag24.notna(), roll7)
    lag168 = price_feat.shift(168); lag168 = lag168.where(lag168.notna(), roll7)
    feats["lag24"] = lag24.values
    feats["lag168"] = lag168.values
    feats["roll7"] = roll7.values
    feat_cols = list(feat_cols) + ["lag24", "lag168", "roll7"]

    # ---- history (actual published day-ahead prices, primary = Energy-Charts) ----
    hist = price.dropna()
    if len(hist):
        h = {"time": [t.isoformat() for t in hist.index],
             "wholesale_kwh": _round(taxes.wholesale_kwh(hist.values))}
        if priced:
            h["allin_kwh"] = _round(taxes.all_in_kwh(hist.values, z["taxes"]))
        # ENTSO-E validation series aligned to the same timestamps (may contain nulls)
        if result["entsoe_available"]:
            ev = entsoe.reindex(hist.index)
            h["entsoe_wholesale_kwh"] = _round(taxes.wholesale_kwh(ev.values))
            if priced:
                h["entsoe_allin_kwh"] = _round(taxes.all_in_kwh(ev.values, z["taxes"]))
        result["history"] = h
    else:
        result["history"] = None

    # ---- forecast (model trained on the primary price series) ----
    base_cols = list(features.FEATURES)
    valid = feats[feat_cols].notna().all(axis=1)
    train_mask = valid & price_feat.notna()
    # Train on the most recent `history_days_train` days (full seasonality with
    # fewer rows keeps the 41-zone run fast). Export history stays longer.
    train_cutoff = now - pd.Timedelta(days=d.get("history_days_train", 365))
    train_mask = train_mask & (feats.index >= train_cutoff)
    horizon_end = now + pd.Timedelta(days=d["forecast_horizon_days"])
    fut_mask = valid & (feats.index > now) & (feats.index <= horizon_end)

    y_tr = price_feat.loc[train_mask]

    # Auto-gating (group-wise): evaluate each optional feature group on its own and
    # keep only the groups that lower this zone's holdout error, then combine them.
    # This prevents a harmful group from masking a helpful one when bundled.
    groups = {}
    if result_resid and "resid_load" in feat_cols:
        groups["resid"] = ["resid_load"]
    if "lag24" in feat_cols:
        groups["lags"] = ["lag24", "lag168", "roll7"]
    if result_entso_fc and "entso_resid_fc" in feat_cols:
        groups["entso"] = ["entso_resid_fc"]

    cols = base_cols
    kept = {"resid": False, "lags": False, "entso": False}
    if len(y_tr) > 700 and groups:
        mae_base = model.holdout_mae(feats.loc[train_mask, base_cols], y_tr, days=21)
        chosen = list(base_cols)
        for name, gcols in groups.items():
            mae_g = model.holdout_mae(feats.loc[train_mask, base_cols + gcols], y_tr, days=21)
            if mae_base is not None and mae_g is not None and mae_g < mae_base * 0.995:
                chosen += gcols
                kept[name] = True
        if len(chosen) > len(base_cols):
            # final safety check on the combined set
            mae_comb = model.holdout_mae(feats.loc[train_mask, chosen], y_tr, days=21)
            if mae_comb is not None and mae_base is not None and mae_comb <= mae_base:
                cols = chosen
            else:
                kept = {k: False for k in kept}
    result["resid_demand"] = bool(kept["resid"])
    result["price_lags"] = bool(kept["lags"])
    result["entso_forecast"] = bool(kept["entso"])

    x_tr = feats.loc[train_mask, cols]
    x_fut = feats.loc[fut_mask, cols]

    if len(x_tr) > 300 and len(x_fut) > 0:
        preds = model.train_predict(x_tr, y_tr, x_fut)
        preds.pop("p50_insample", None)

        # ---- self-learning: daily bias correction + uncertainty calibration ----
        # Genuine out-of-sample bias from a held-out tail (captures peak underestimation).
        ho_idx, ho_true, ho_pred = model.holdout_predict(x_tr, y_tr, days=21)
        bias = learn.oos_bias(ho_idx, ho_true, ho_pred, z["timezone"])
        state = learn.load_state(code, read_dir=STATE_READ)
        band_scale, learn_metrics = learn.calibrate(state, price, z["timezone"], now)
        # log the RAW predictions so future runs can measure true error
        learn.log_predictions(state, x_fut.index, preds["p10"], preds["p50"], preds["p90"])
        learn.save_state(code, state, write_dir=OUT / "state")
        p10c, p50c, p90c = learn.apply(x_fut.index, preds["p10"], preds["p50"],
                                       preds["p90"], bias, band_scale, z["timezone"])
        preds = {"p10": p10c, "p50": p50c, "p90": p90c}
        peak_corr = float(np.mean([bias[h] for h in learn._PEAK_HOURS]))
        learn_metrics["peak_correction"] = round(peak_corr, 1)
        if ho_true is not None:
            mae_ho = round(float(np.mean(np.abs(ho_true - ho_pred))), 2)
            learn_metrics["mae_holdout"] = mae_ho
            result["mae_eur_mwh"] = mae_ho
        result["learning"] = learn_metrics

        fc = {"time": [t.isoformat() for t in x_fut.index]}
        for key, arr in preds.items():
            fc[f"{key}_wholesale_kwh"] = _round(taxes.wholesale_kwh(arr))
            if priced:
                fc[f"{key}_allin_kwh"] = _round(taxes.all_in_kwh(arr, z["taxes"]))
        result["forecast"] = fc
        result["train_rows"] = int(len(x_tr))
    else:
        result["forecast"] = None
        result["error"] = "no_price_data" if not len(hist) else "insufficient_data"

    _write(code, result)
    return result


def _write(code, result):
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / f"{code}.json", "w", encoding="utf-8") as f:
        json.dump(result, f, separators=(",", ":"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shard", type=int, default=0, help="this shard index (0-based)")
    ap.add_argument("--shards", type=int, default=1, help="total number of shards")
    ap.add_argument("--meta-only", action="store_true",
                    help="rebuild meta.json from existing public/data/*.json and exit")
    args = ap.parse_args()

    cfg = load_config()
    enabled = cfg.get("enabled", [])

    if args.meta_only:
        build_meta(cfg)
        return

    # Select this shard's zones (round-robin keeps the heavy NL zone alone-ish).
    my_zones = [c for i, c in enumerate(enabled) if i % args.shards == args.shard]
    d = cfg["defaults"]
    gas = ingest.fetch_yahoo_daily(d["gas_symbol"])
    co2 = ingest.fetch_yahoo_daily(d["co2_symbol"])
    for code in my_zones:
        try:
            r = run_zone(code, cfg, gas=gas, co2=co2)
            print(f"[{code}] history={'y' if r.get('history') else 'n'} "
                  f"forecast={'y' if r.get('forecast') else 'n'} "
                  f"err={r.get('error', '-')}")
        except Exception as e:  # keep other zones going
            print(f"[{code}] FAILED: {e}")
    if args.shards == 1:
        build_meta(cfg)
    print(f"done shard {args.shard}/{args.shards} ->", OUT)


def build_meta(cfg):
    """Build meta.json from whatever zone JSONs are present in public/data."""
    enabled = cfg.get("enabled", [])
    zones = []
    for code in enabled:
        fp = OUT / f"{code}.json"
        if not fp.exists():
            continue
        try:
            z = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            continue
        zones.append({"code": code, "name": z.get("name", code),
                      "priced": z.get("priced", False),
                      "entsoe_available": z.get("entsoe_available", False)})
    OUT.mkdir(parents=True, exist_ok=True)
    meta = {"generated_at": pd.Timestamp.now(tz="UTC").isoformat(), "zones": zones}
    with open(OUT / "meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, separators=(",", ":"))
    print(f"meta.json -> {len(zones)} zones")


if __name__ == "__main__":
    main()
