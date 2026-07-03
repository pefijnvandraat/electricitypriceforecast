"""Per-horizon walk-forward backtest harness -- the acceptance gate.

Rolls a forecast origin across recent history and, at each origin, produces a
genuine D+1..D+7 forecast using ONLY information available at that origin
(price lags are real at D+1 and degraded to the rolling mean beyond, mirroring
serving). The conformal, horizon+regime band is calibrated on a holdout ending
at the origin -- exactly like `run.py`. For every horizon it reports point
accuracy (MAE/RMSE), interval coverage of p10-p90, pinball loss, and mean band
width, plus per-regime coverage. This is the artefact that gates any change:
coverage must sit near 80% at EACH horizon and the band must widen with lead.

Run:  python -m forecaster.backtest [--zone NL] [--origins 30] [--train 180]
"""
import argparse
import os

import numpy as np
import pandas as pd

from . import ingest, features, model as M, learn


def _wp(nl_points):
    return nl_points


def build_matrix(cfg, code, days):
    """Assemble the price + feature matrix for one zone over `days` history."""
    z = cfg["zones"][code]
    token = os.environ.get("ENTSOE_TOKEN", "").strip()
    now = pd.Timestamp.now(tz="UTC").floor("h")
    start = now - pd.Timedelta(days=days + 10)

    ec = ingest.fetch_energy_charts_prices(z["entsoe_eic_bzn"], start, now)["price_eur_mwh"]
    price = ec
    if z.get("fallback_source") == "energyzero":
        try:
            price = ec.combine_first(ingest.fetch_energyzero_prices(start, now))
        except Exception:
            pass
    weather = ingest.fetch_weather(z["weather_points"], start.date(), now.date())
    gas = ingest.fetch_yahoo_daily(cfg["defaults"]["gas_symbol"])
    co2 = ingest.fetch_yahoo_daily(cfg["defaults"]["co2_symbol"])
    feats = features.assemble(weather, gas, co2, z["timezone"], z["holidays"])
    idx = feats.index
    pr = price.reindex(idx)

    roll7 = pr.shift(24).rolling(168, min_periods=24).mean().ffill().bfill()
    feats["lag24"] = pr.shift(24).where(pr.shift(24).notna(), roll7).values
    feats["lag168"] = pr.shift(168).where(pr.shift(168).notna(), roll7).values
    feats["roll7"] = roll7.values
    base = list(features.FEATURES)
    cols = base + ["lag24", "lag168", "roll7"]

    # regional fundamentals (neighbour ENTSO-E residual forecast)
    for nbkey in (z.get("neighbours") or []):
        nbeic = (cfg["zones"].get(nbkey) or {}).get("entsoe_eic")
        if not (token and nbeic):
            continue
        try:
            nefc, _l, _g = ingest.fetch_entsoe_residual_forecast(nbeic, start, now, token)
            nefc = nefc.reindex(idx)
            known = feats[base].notna().all(axis=1) & nefc.notna()
            if int(known.sum()) > 500:
                pred = M.predict_feature(feats.loc[known, base], nefc.loc[known].values, feats[base])
                bl = nefc.copy(); bl[bl.isna()] = pred[bl.isna().values]
                feats["nb_%s_resid" % nbkey] = bl.values
                feats["nb_%s_ramp3" % nbkey] = pd.Series(bl.values, index=idx).diff(3).fillna(0.0).values
                cols += ["nb_%s_resid" % nbkey, "nb_%s_ramp3" % nbkey]
        except Exception:
            continue
    return feats, pr, cols, z["timezone"]


def _pinball(y, q, tau):
    d = y - q
    return np.mean(np.maximum(tau * d, (tau - 1.0) * d))


def run(cfg, code, origins, train_days, horizons=(1, 2, 3, 5, 7)):
    feats, pr, cols, tz = build_matrix(cfg, code, train_days + origins + 12)
    idx = feats.index
    degrade = {"lag24": "roll7", "lag168": "roll7"}
    valid = feats[cols].notna().all(axis=1) & pr.notna()

    last = idx[valid][-1]
    # origins every 24h, ending a few days before the last known price so D+7 has actuals
    rec = {h: [] for h in horizons}
    made = 0
    for o in range(origins):
        origin = (last.normalize() - pd.Timedelta(days=origins - o))
        tr_mask = valid & (idx <= origin) & (idx > origin - pd.Timedelta(days=train_days))
        x_tr, y_tr = feats.loc[tr_mask, cols], pr.loc[tr_mask]
        if len(x_tr) < 24 * 60:
            continue
        m = M._estimator(0.5).fit(x_tr, y_tr.values)
        ho_idx, ho_true, near, far = M.holdout_predict_pair(x_tr, y_tr, degrade, days=28)
        if ho_idx is None:
            continue
        made += 1
        for h in horizons:
            day = origin + pd.Timedelta(days=h)
            fm = valid & (idx >= day) & (idx < day + pd.Timedelta(days=1))
            if int(fm.sum()) == 0:
                continue
            xf = feats.loc[fm, cols].copy()
            if h >= 2:                      # lags unknown beyond D+1 -> degrade
                for c, s in degrade.items():
                    xf[c] = xf["roll7"].to_numpy()
            p50 = m.predict(xf)
            lead = (idx[fm] - origin).total_seconds() / 3600.0
            cb = learn.conformal_band(ho_idx, ho_true, near, far, idx[fm], p50, tz, lead)
            if cb is None:
                continue
            p10, p50c, p90, _ = cb
            a = pr.loc[fm].values
            rec[h].append(np.column_stack([a, p10, p50c, p90]))

    print("\nzone=%s  origins used=%d  train_days=%d" % (code, made, train_days))
    print("%-4s %6s %6s %7s %7s %8s %8s" % ("D+", "MAE", "RMSE", "cov%", "pinball", "width", "n"))
    ok = True
    for h in horizons:
        if not rec[h]:
            continue
        A = np.vstack(rec[h]); a, p10, p50, p90 = A[:, 0], A[:, 1], A[:, 2], A[:, 3]
        mae = np.mean(np.abs(a - p50)); rmse = np.sqrt(np.mean((a - p50) ** 2))
        cov = np.mean((a >= p10) & (a <= p90)) * 100
        pb = (_pinball(a, p10, 0.1) + _pinball(a, p90, 0.9)) / 2
        width = np.mean(p90 - p10)
        print("%-4d %6.1f %6.1f %7.0f %7.1f %8.1f %8d" % (h, mae, rmse, cov, pb, width, len(a)))
        if not (72 <= cov <= 88):
            ok = False
    print("ACCEPTANCE per-horizon coverage in [72,88]:", "PASS" if ok else "REVIEW")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--zone", default="NL")
    ap.add_argument("--origins", type=int, default=30)
    ap.add_argument("--train", type=int, default=180)
    args = ap.parse_args()
    import yaml, pathlib
    cfg = yaml.safe_load(open(pathlib.Path(__file__).resolve().parents[1] / "zones.yaml", encoding="utf-8"))
    run(cfg, args.zone, args.origins, args.train)


if __name__ == "__main__":
    main()
