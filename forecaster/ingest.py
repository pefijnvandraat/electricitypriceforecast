"""Data ingest from public interfaces only.

Sources (all public, tested):
- Energy-Charts (Fraunhofer ISE) -> day-ahead price (PRIMARY target). No token.
- ENTSO-E Transparency Platform  -> day-ahead price (OPTIONAL validation). Needs a free token.
- Open-Meteo archive + forecast  -> weather features (no key, 16-day horizon).
- Yahoo Finance (unofficial)      -> TTF gas (EUR) and KRBN carbon proxy.

Rate-limit note (ENTSO-E): the public limit is 400 requests/minute per API token.
End users never hit ENTSO-E directly: the browser only loads pre-computed static
JSON from GitHub Pages. Only this CI job calls ENTSO-E (a handful of requests/day).
`_get` below adds polite throttling + retry/backoff on HTTP 429 so the job stays
well under the limit even if many zones are added later.
"""
import time
import xml.etree.ElementTree as ET

import numpy as np
import pandas as pd
import requests

ENERGY_CHARTS_PRICE = "https://api.energy-charts.info/price"
ENTSOE_BASE = "https://web-api.tp.entsoe.eu/api"
OM_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
OM_FORECAST = "https://api.open-meteo.com/v1/forecast"
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

_HOURLY_VARS = "temperature_2m,wind_speed_100m,shortwave_radiation,cloud_cover"
_OM_COLS = ["temperature_2m", "wind_speed_100m", "shortwave_radiation", "cloud_cover"]

# Throttle: stay far below 400 req/min (ENTSO-E limit). ~5 req/sec ceiling.
_MIN_INTERVAL_S = 0.20
_last_call = {"t": 0.0}


def _get(url, params=None, timeout=90, retries=6, throttle=True, min_interval=None):
    """HTTP GET with throttling and exponential backoff on 429 / 5xx.

    `min_interval` overrides the default spacing between calls. Energy-Charts is
    rate-sensitive, so its calls pass a larger interval to avoid 429s entirely
    (predictable pacing beats many exponential-backoff retries).
    """
    gap = _MIN_INTERVAL_S if min_interval is None else min_interval
    if throttle:
        wait = gap - (time.monotonic() - _last_call["t"])
        if wait > 0:
            time.sleep(wait)
    delay = 3.0
    last_exc = None
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=timeout,
                             headers={"User-Agent": "Mozilla/5.0 (electricitypriceforecast)"})
            _last_call["t"] = time.monotonic()
            if r.status_code == 429 or 500 <= r.status_code < 600:
                # honour Retry-After if present, else exponential backoff
                ra = r.headers.get("Retry-After")
                sleep_s = float(ra) if (ra and ra.isdigit()) else delay
                time.sleep(min(sleep_s, 60))
                delay = min(delay * 2, 60)
                continue
            return r
        except requests.RequestException as e:
            last_exc = e
            time.sleep(delay)
            delay = min(delay * 2, 60)
    if last_exc is not None:
        raise last_exc
    return r


# --------------------------------------------------------------------------- #
# Energy-Charts day-ahead prices (PRIMARY, no token)
# --------------------------------------------------------------------------- #
def fetch_energy_charts_prices(bzn, start_dt, end_dt):
    """Hourly (UTC) day-ahead price series from Energy-Charts for a bidding zone.

    Public, token-free (CC BY 4.0, Bundesnetzagentur/SMARD). Recent data may be
    quarter-hourly; we resample to hourly means for a consistent index.
    """
    empty = pd.DataFrame(columns=["price_eur_mwh"]).set_index(pd.DatetimeIndex([], tz="UTC"))
    frames = []
    cur = start_dt
    while cur < end_dt:
        chunk_end = min(cur + pd.Timedelta(days=370), end_dt)
        params = {"bzn": bzn,
                  "start": cur.strftime("%Y-%m-%d"),
                  "end": chunk_end.strftime("%Y-%m-%d")}
        try:
            r = _get(ENERGY_CHARTS_PRICE, params=params, timeout=90, min_interval=2.5)
            if r.status_code == 200:
                j = r.json()
                secs, prices = j.get("unix_seconds"), j.get("price")
                if secs and prices:
                    d = pd.DataFrame({
                        "ts": pd.to_datetime(secs, unit="s", utc=True),
                        "price_eur_mwh": pd.to_numeric(prices, errors="coerce"),
                    }).dropna()
                    frames.append(d)
        except (requests.RequestException, ValueError):
            pass
        cur = chunk_end
    if not frames:
        return empty
    df = pd.concat(frames).drop_duplicates("ts").set_index("ts").sort_index()
    return df.resample("1h").mean()


# --------------------------------------------------------------------------- #
# ENTSO-E day-ahead prices
# --------------------------------------------------------------------------- #
def _strip_ns(tag):
    return tag.split("}", 1)[-1]


def _res_minutes(res):
    return {"PT15M": 15, "PT30M": 30, "PT60M": 60, "P1D": 1440}.get(res, 60)


def _parse_a44(xml_text):
    """Parse an A44 Publication_MarketDocument into an hourly price series."""
    root = ET.fromstring(xml_text)
    rows = []
    for ts in root.iter():
        if _strip_ns(ts.tag) != "TimeSeries":
            continue
        for period in ts.iter():
            if _strip_ns(period.tag) != "Period":
                continue
            start = None
            res = "PT60M"
            points = {}
            for el in period.iter():
                t = _strip_ns(el.tag)
                if t == "start" and start is None:
                    start = el.text
                elif t == "resolution":
                    res = el.text
                elif t == "Point":
                    pos = price = None
                    for c in el:
                        ct = _strip_ns(c.tag)
                        if ct == "position":
                            pos = int(c.text)
                        elif ct == "price.amount":
                            price = float(c.text)
                    if pos is not None and price is not None:
                        points[pos] = price
            if not start or not points:
                continue
            start_dt = pd.Timestamp(start)
            start_dt = start_dt.tz_localize("UTC") if start_dt.tzinfo is None else start_dt.tz_convert("UTC")
            step = _res_minutes(res)
            last = None
            for pos in range(1, max(points) + 1):
                if pos in points:
                    last = points[pos]
                if last is None:
                    continue
                rows.append((start_dt + pd.Timedelta(minutes=step * (pos - 1)), last))
    if not rows:
        return pd.DataFrame(columns=["price_eur_mwh"]).set_index(pd.DatetimeIndex([], tz="UTC"))
    df = pd.DataFrame(rows, columns=["ts", "price_eur_mwh"])
    df = df.drop_duplicates("ts").set_index("ts").sort_index()
    return df


def fetch_dayahead_prices(eic, start_dt, end_dt, token):
    """Return an hourly (UTC) day-ahead price series for the bidding zone."""
    frames = []
    cur = start_dt
    while cur < end_dt:
        chunk_end = min(cur + pd.Timedelta(days=360), end_dt)
        params = {
            "securityToken": token,
            "documentType": "A44",
            "in_Domain": eic,
            "out_Domain": eic,
            "periodStart": cur.strftime("%Y%m%d%H%M"),
            "periodEnd": chunk_end.strftime("%Y%m%d%H%M"),
        }
        try:
            r = _get(ENTSOE_BASE, params=params)
            if r.status_code == 200 and "Publication_MarketDocument" in r.text:
                frames.append(_parse_a44(r.text))
        except requests.RequestException:
            pass
        cur = chunk_end
    if frames:
        df = pd.concat(frames)
        df = df[~df.index.duplicated(keep="last")].sort_index()
    else:
        df = pd.DataFrame(columns=["price_eur_mwh"]).set_index(pd.DatetimeIndex([], tz="UTC"))
    return df.resample("1h").mean()


# --------------------------------------------------------------------------- #
# Open-Meteo weather (capacity-weighted across points)
# --------------------------------------------------------------------------- #
def _om_collect(base, points, extra):
    total_w = sum(p.get("weight", 1.0) for p in points) or 1.0
    agg = None
    for p in points:
        params = {"latitude": p["lat"], "longitude": p["lon"],
                  "hourly": _HOURLY_VARS, "timezone": "UTC"}
        params.update(extra)
        try:
            r = _get(base, params=params)
            r.raise_for_status()
            h = r.json().get("hourly", {})
        except (requests.RequestException, ValueError):
            continue
        if not h.get("time"):
            continue
        d = pd.DataFrame(h)
        d["time"] = pd.to_datetime(d["time"], utc=True)
        d = d.set_index("time")[_OM_COLS].astype(float)
        w = p.get("weight", 1.0) / total_w
        d = d * w
        agg = d if agg is None else agg.add(d, fill_value=0)
    if agg is None:
        return pd.DataFrame()
    agg.columns = ["temp", "wind100", "radiation", "cloud"]
    return agg


def fetch_weather(points, start_date, end_date):
    """Hourly (UTC) weather history + forecast, weighted across points.

    Archive (ERA5) covers history with a ~5-day lag; the forecast endpoint with
    past_days fills the recent gap and extends 16 days ahead.
    """
    arch = _om_collect(OM_ARCHIVE, points,
                       {"start_date": str(start_date), "end_date": str(end_date)})
    fc = _om_collect(OM_FORECAST, points, {"past_days": 7, "forecast_days": 16})
    parts = [x for x in (arch, fc) if x is not None and not x.empty]
    if not parts:
        return pd.DataFrame()
    df = pd.concat(parts)
    return df[~df.index.duplicated(keep="last")].sort_index()


# --------------------------------------------------------------------------- #
# Yahoo Finance daily commodity proxies
# --------------------------------------------------------------------------- #
def fetch_yahoo_daily(symbol, rng="2y"):
    """Return a daily (UTC-normalised date index) close series, or empty on failure."""
    try:
        r = _get(YAHOO.format(symbol=symbol),
                 params={"interval": "1d", "range": rng}, timeout=60, throttle=False)
        r.raise_for_status()
        res = r.json()["chart"]["result"][0]
        ts = res["timestamp"]
        closes = res["indicators"]["quote"][0]["close"]
    except (requests.RequestException, ValueError, KeyError, TypeError, IndexError):
        return pd.Series(dtype=float)
    d = pd.DataFrame({"date": pd.to_datetime(ts, unit="s", utc=True).normalize(),
                      "value": closes}).dropna()
    if d.empty:
        return pd.Series(dtype=float)
    return d.drop_duplicates("date").set_index("date")["value"]
