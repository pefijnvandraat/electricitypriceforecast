# electricitypriceforecast

Lean, fully open-source consumer **electricity price forecaster** for the Netherlands
(multi-zone ready), built on **public data only**. No server, no database: a daily
GitHub Action computes the forecast and publishes a static site to GitHub Pages.

## What it does

- Forecasts the **dynamic consumer electricity price** per hour, up to ~16 days ahead.
- Trains on **>= 1 year** of history of the underlying drivers.
- Web page lets the user set the **look-back window** and **forecast window**, toggle
  between the **wholesale** and **all-in** price (incl. energy tax + VAT) for
  fully-priced zones, switch the **UI language** (all 24 official EU languages), and
  flip between **dark / light** theme. An optional **ENTSO-E validation** overlay
  (off by default) appears once an ENTSO-E token is configured.

## Data sources (public interfaces only)

| Variable | Source | Notes |
|---|---|---|
| Day-ahead price (primary target) | Energy-Charts (Fraunhofer ISE) | **no token**, CC BY 4.0 |
| Day-ahead price (optional validation) | ENTSO-E Transparency Platform | free token, XML; off by default |
| Weather (wind/solar/temp/cloud) | Open-Meteo archive + forecast | no key, 16-day horizon |
| Gas (TTF) | Yahoo Finance `TTF=F` | EUR, daily |
| CO2 (EU-ETS proxy) | Yahoo Finance `KRBN` | daily |
| Taxes & levies | `zones.yaml` config | NL verified, Belastingdienst 2026 |

## Architecture

```
GitHub Actions (cron, daily)                Static site (GitHub Pages)
  ingest -> features -> train -> predict  ->  public/data/<zone>.json
  (Energy-Charts primary;                      index.html + app.js (ECharts)
   ENTSOE_TOKEN optional validation)           i18n.js (24 EU languages)
```

The model is a scikit-learn `HistGradientBoostingRegressor` with quantile loss
(p10 / p50 / p90) so the chart shows an uncertainty band. The primary price source
needs no token, so the site produces forecasts even before any ENTSO-E token exists.

## Configuration

All zones live in [`zones.yaml`](./zones.yaml). Only zones listed under `enabled`
are fetched and published. The Netherlands (`NL`) is fully pre-filled with verified
2026 tax figures; additional zones can be added as wholesale-only (no tax block).

## Setup

1. Enable Pages with **Source = GitHub Actions**.
2. Run the **Refresh forecast & deploy** workflow (or wait for the daily schedule).
   This already works with no token (Energy-Charts is the primary source).
3. *(Optional)* To enable the ENTSO-E validation overlay: create a free account at
   https://transparency.entsoe.eu, request an API token, and add it as a repository
   secret named `ENTSOE_TOKEN` (`gh secret set ENTSOE_TOKEN`). The toggle then appears
   on the page, defaulting to off.

## License

Code: see [LICENSE](./LICENSE). Data attribution: Energy-Charts (Fraunhofer ISE,
CC BY 4.0), ENTSO-E, Open-Meteo, Yahoo Finance.
