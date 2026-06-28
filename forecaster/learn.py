"""Self-learning layer: daily bias correction + uncertainty calibration.

The forecaster underestimates sharp evening peaks because quantile regression
pulls rare spikes toward the mean. This module makes the system learn from its
own mistakes on a daily cycle (it fits the lean static-CI model, no online
training):

1. In-sample bias by hour-of-day
   Each run, compare the freshly fitted p50 against the training actuals and
   measure the systematic residual per local hour. Peaks show a positive
   residual (model too low) -> we add that residual back to the forecast.
   Recomputed every day, so it adapts as data grows.

2. Honest out-of-sample error log (`public/data/state/<zone>.json`)
   Every run logs its RAW future predictions keyed by target timestamp. On
   later runs, once the actual price for those timestamps is known (via
   Energy-Charts), we compute the true forecast error. These trailing
   out-of-sample residuals calibrate the uncertainty band so p10-p90 really
   covers ~80%, and provide honest accuracy metrics shown on the page.

State persists by being committed to the repo by the CI deploy job.
"""
import json
import pathlib

import numpy as np
import pandas as pd

STATE_DIR = pathlib.Path(__file__).resolve().parents[1] / "public" / "data" / "state"

_BIAS_CAP = 300.0          # EUR/MWh, guard against runaway corrections
_LOG_KEEP_DAYS = 50        # prune error-log entries older than this
_OOS_WINDOW_DAYS = 45      # trailing window for calibration
_PEAK_HOURS = range(17, 22)  # local hours treated as the evening peak


def _hod_local(index, tz):
    """Local hour-of-day for a tz-aware UTC DatetimeIndex."""
    return index.tz_convert(tz).hour.to_numpy()


# --------------------------------------------------------------------------- #
# State persistence
# --------------------------------------------------------------------------- #
def load_state(code, read_dir=None):
    fp = pathlib.Path(read_dir or STATE_DIR) / f"{code}.json"
    if fp.exists():
        try:
            return json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"pred_log": {}, "updated": None}


def save_state(code, state, write_dir=None):
    d = pathlib.Path(write_dir or STATE_DIR)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{code}.json").write_text(
        json.dumps(state, separators=(",", ":")), encoding="utf-8")


# --------------------------------------------------------------------------- #
# 1. Out-of-sample bias by hour-of-day (immediate peak correction)
# --------------------------------------------------------------------------- #
def oos_bias(holdout_index, y_true, y_pred, tz):
    """Median residual (actual - pred) per local hour on a held-out tail.

    Uses a small shrinkage by sample count so thin hours are not over-corrected.
    Returns a length-24 list (EUR/MWh).
    """
    if holdout_index is None or y_pred is None:
        return [0.0] * 24
    resid = np.asarray(y_true, dtype=float) - np.asarray(y_pred, dtype=float)
    hod = _hod_local(holdout_index, tz)
    bias = [0.0] * 24
    for h in range(24):
        r = resid[hod == h]
        if len(r) >= 3:
            shrink = min(1.0, len(r) / 14.0)   # full weight from ~14 samples
            bias[h] = float(np.clip(np.median(r) * shrink, -_BIAS_CAP, _BIAS_CAP))
    return bias


# --------------------------------------------------------------------------- #
# 2. Error log + out-of-sample calibration
# --------------------------------------------------------------------------- #
def log_predictions(state, fc_index, p10, p50, p90):
    """Store RAW future predictions keyed by ISO timestamp (latest wins)."""
    log = state.setdefault("pred_log", {})
    for i, ts in enumerate(fc_index):
        log[ts.isoformat()] = [round(float(p10[i]), 2),
                               round(float(p50[i]), 2),
                               round(float(p90[i]), 2)]


def calibrate(state, price_actual, tz, now):
    """Match logged predictions against known actuals; prune; return calibration.

    Returns dict with band_scale and honest metrics computed on out-of-sample
    forecasts (predictions whose target time has since become known).
    """
    log = state.setdefault("pred_log", {})
    horizon_cut = now - pd.Timedelta(days=_OOS_WINDOW_DAYS)
    keep_cut = now - pd.Timedelta(days=_LOG_KEEP_DAYS)

    ratios, abs_err, peak_err, covered, total = [], [], [], 0, 0
    pruned = {}
    for iso, (p10, p50, p90) in log.items():
        ts = pd.Timestamp(iso)
        if ts < keep_cut:
            continue                      # prune very old entries
        pruned[iso] = [p10, p50, p90]
        if ts > now or ts not in price_actual.index:
            continue                      # still in the future / no actual yet
        actual = price_actual.loc[ts]
        if not np.isfinite(actual):
            continue
        if ts >= horizon_cut:             # within trailing calibration window
            total += 1
            abs_err.append(abs(actual - p50))
            half = max((p90 - p10) / 2.0, 1e-6)
            ratios.append(abs(actual - p50) / half)
            if p10 <= actual <= p90:
                covered += 1
            if ts.tz_convert(tz).hour in _PEAK_HOURS:
                peak_err.append(actual - p50)
    state["pred_log"] = pruned

    band_scale = 1.0
    if len(ratios) >= 24:
        # scale so ~80% of actuals fall inside the band; widen only
        band_scale = float(np.clip(np.quantile(ratios, 0.8), 0.8, 4.0))
    metrics = {
        "n_samples": total,
        "mae_raw": round(float(np.mean(abs_err)), 2) if abs_err else None,
        "coverage": round(covered / total, 3) if total else None,
        "peak_bias": round(float(np.mean(peak_err)), 1) if peak_err else None,
        "band_scale": round(band_scale, 2),
    }
    return band_scale, metrics


# --------------------------------------------------------------------------- #
# 3. Apply corrections to a forecast
# --------------------------------------------------------------------------- #
def apply(fc_index, p10, p50, p90, bias_by_hour, band_scale, tz):
    """Return bias-corrected, calibrated (p10, p50, p90) arrays."""
    p10 = np.asarray(p10, dtype=float)
    p50 = np.asarray(p50, dtype=float)
    p90 = np.asarray(p90, dtype=float)
    hod = _hod_local(fc_index, tz)
    bias = np.array([bias_by_hour[h] for h in hod], dtype=float)

    p50c = p50 + bias
    half_lo = np.maximum(p50 - p10, 0.0) * band_scale
    half_hi = np.maximum(p90 - p50, 0.0) * band_scale
    p10c = p50c - half_lo
    p90c = p50c + half_hi
    # keep ordering sane
    p10c = np.minimum(p10c, p50c)
    p90c = np.maximum(p90c, p50c)
    return p10c, p50c, p90c
