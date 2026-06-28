"""Feature assembly: join weather, fuels and calendar onto one hourly index."""
import numpy as np
import pandas as pd

try:
    import holidays as pyholidays
except Exception:  # pragma: no cover
    pyholidays = None

FEATURES = ["temp", "wind100", "radiation", "cloud", "gas", "co2",
            "hour_sin", "hour_cos", "dow", "month", "is_weekend", "is_holiday"]


def _map_daily(index, series):
    """Map a daily series onto an hourly index by UTC date, forward/back filled."""
    if series is None or len(series) == 0:
        return pd.Series(np.nan, index=index)
    dates = pd.Series(index.normalize(), index=index)
    out = pd.to_numeric(dates.map(series), errors="coerce")
    return out.ffill().bfill()


def assemble(weather, gas, co2, tz, country):
    """Return a feature DataFrame (FEATURES columns) on the weather hourly index."""
    if weather is None or weather.empty:
        return pd.DataFrame(columns=FEATURES)
    df = weather.copy()
    idx = df.index

    df["gas"] = _map_daily(idx, gas).fillna(0.0).values
    df["co2"] = _map_daily(idx, co2).fillna(0.0).values

    loc = idx.tz_convert(tz)
    df["hour_sin"] = np.sin(2 * np.pi * loc.hour / 24)
    df["hour_cos"] = np.cos(2 * np.pi * loc.hour / 24)
    df["dow"] = loc.dayofweek
    df["month"] = loc.month
    df["is_weekend"] = (loc.dayofweek >= 5).astype(int)

    if pyholidays is not None:
        try:
            hol = pyholidays.country_holidays(country)
            df["is_holiday"] = [1 if d.date() in hol else 0 for d in loc]
        except Exception:
            df["is_holiday"] = 0
    else:
        df["is_holiday"] = 0

    return df[FEATURES]
