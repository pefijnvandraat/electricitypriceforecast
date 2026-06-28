"""Orchestrate ingest -> features -> model -> static JSON, per enabled zone."""
import json
import os
import pathlib

import numpy as np
import pandas as pd
import yaml

from . import features, ingest, model, taxes

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "public" / "data"


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

    weather = ingest.fetch_weather(z["weather_points"], start.date(), now.date())
    if gas is None:
        gas = ingest.fetch_yahoo_daily(d["gas_symbol"])
    if co2 is None:
        co2 = ingest.fetch_yahoo_daily(d["co2_symbol"])

    feats = features.assemble(weather, gas, co2, z["timezone"], z["holidays"])
    priced = bool(z.get("priced")) and "taxes" in z

    result = {
        "zone": code, "name": z["name"], "priced": priced,
        "generated_at": now.isoformat(), "timezone": z["timezone"],
        "history_days": d["history_days_export"], "horizon_days": d["forecast_horizon_days"],
        "unit": "EUR/kWh", "price_source": "energy-charts",
        "entsoe_available": bool(token and len(entsoe.dropna()) > 0),
    }
    if priced:
        result["taxes"] = z["taxes"]

    if feats.empty:
        result.update(history=None, forecast=None, error="no_weather_data")
        _write(code, result)
        return result

    price = price.reindex(feats.index) if len(price) else pd.Series(np.nan, index=feats.index)
    entsoe = entsoe.reindex(feats.index) if len(entsoe) else pd.Series(np.nan, index=feats.index)

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
    cols = features.FEATURES
    valid = feats[cols].notna().all(axis=1)
    train_mask = valid & price.notna()
    horizon_end = now + pd.Timedelta(days=d["forecast_horizon_days"])
    fut_mask = valid & (feats.index > now) & (feats.index <= horizon_end)

    x_tr, y_tr = feats.loc[train_mask, cols], price.loc[train_mask]
    x_fut = feats.loc[fut_mask, cols]

    if len(x_tr) > 300 and len(x_fut) > 0:
        preds = model.train_predict(x_tr, y_tr, x_fut)
        fc = {"time": [t.isoformat() for t in x_fut.index]}
        for key, arr in preds.items():
            fc[f"{key}_wholesale_kwh"] = _round(taxes.wholesale_kwh(arr))
            if priced:
                fc[f"{key}_allin_kwh"] = _round(taxes.all_in_kwh(arr, z["taxes"]))
        result["forecast"] = fc
        mae = model.backtest_mae(x_tr, y_tr)
        result["mae_eur_mwh"] = round(mae, 2) if mae is not None else None
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
    cfg = load_config()
    enabled = cfg.get("enabled", [])
    d = cfg["defaults"]
    # Fetch the daily commodity proxies once and reuse across all zones.
    gas = ingest.fetch_yahoo_daily(d["gas_symbol"])
    co2 = ingest.fetch_yahoo_daily(d["co2_symbol"])
    meta = {"generated_at": pd.Timestamp.now(tz="UTC").isoformat(), "zones": []}
    for code in enabled:
        try:
            r = run_zone(code, cfg, gas=gas, co2=co2)
            meta["zones"].append({"code": code, "name": r.get("name", code),
                                  "priced": r.get("priced", False),
                                  "entsoe_available": r.get("entsoe_available", False)})
            print(f"[{code}] history={'y' if r.get('history') else 'n'} "
                  f"forecast={'y' if r.get('forecast') else 'n'} "
                  f"err={r.get('error', '-')}")
        except Exception as e:  # keep other zones going
            meta["zones"].append({"code": code, "name": code, "error": str(e)})
            print(f"[{code}] FAILED: {e}")
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / "meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, separators=(",", ":"))
    print("done ->", OUT)


if __name__ == "__main__":
    main()
