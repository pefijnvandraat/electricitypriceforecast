"""Quantile forecasting model (scikit-learn, no native deps)."""
import numpy as np
from sklearn.ensemble import HistGradientBoostingRegressor

QUANTILES = {"p10": 0.1, "p50": 0.5, "p90": 0.9}


def _estimator(quantile):
    return HistGradientBoostingRegressor(
        loss="quantile", quantile=quantile,
        max_iter=400, learning_rate=0.05,
        l2_regularization=1.0, random_state=42,
    )


def train_predict(x_train, y_train, x_future):
    """Train one model per quantile and predict the future frame."""
    out = {}
    for name, q in QUANTILES.items():
        model = _estimator(q)
        model.fit(x_train, y_train)
        out[name] = model.predict(x_future)
    # guard against quantile crossing
    p10, p50, p90 = out["p10"], out["p50"], out["p90"]
    lo = np.minimum.reduce([p10, p50, p90])
    hi = np.maximum.reduce([p10, p50, p90])
    out["p10"], out["p90"] = lo, hi
    out["p50"] = np.clip(p50, lo, hi)
    return out


def backtest_mae(x, y, days=7):
    """Simple holdout MAE on the most recent `days` of the median model."""
    n = days * 24
    if len(x) <= n + 200:
        return None
    model = _estimator(0.5)
    model.fit(x.iloc[:-n], y.iloc[:-n])
    pred = model.predict(x.iloc[-n:])
    return float(np.mean(np.abs(pred - y.iloc[-n:].values)))
