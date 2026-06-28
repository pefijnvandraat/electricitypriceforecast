# electricitypriceforecast

Lean, fully open-source consumer **electricity price forecaster** for the Netherlands
(multi-zone ready), built on **public data only**. No server, no database: a daily
GitHub Action computes the forecast and publishes a static site to GitHub Pages.

## What it does

- Forecasts the **dynamic consumer electricity price** per hour, up to ~16 days ahead.
- Trains on **>= 1 year** of history of the underlying drivers.
- Web page lets the user set the **look-back window** and **forecast window**, and
  toggle between the **kale marktprijs** (wholesale) and the **all-in** price
  (incl. energy tax + VAT) for fully-priced zones.

## Data sources (public interfaces only)

| Variable | Source | Notes |
|---|---|---|
| Day-ahead price (target) | ENTSO-E Transparency Platform | free token, XML |
| Weather (wind/solar/temp/cloud) | Open-Meteo archive + forecast | no key, 16-day horizon |
| Gas (TTF) | Yahoo Finance `TTF=F` | EUR, daily |
| CO2 (EU-ETS proxy) | Yahoo Finance `KRBN` | daily |
| Taxes & levies | `zones.yaml` config | NL verified, Belastingdienst 2026 |

## Architecture

```
GitHub Actions (cron, daily)                Static site (GitHub Pages)
  ingest -> features -> train -> predict  ->  public/data/<zone>.json
  (ENTSOE_TOKEN as Actions secret)             index.html + app.js (ECharts)
```

The model is a scikit-learn `HistGradientBoostingRegressor` with quantile loss
(p10 / p50 / p90) so the chart shows an uncertainty band.

## Configuration

All zones live in [`zones.yaml`](./zones.yaml). Only zones listed under `enabled`
are fetched and published. The Netherlands (`NL`) is fully pre-filled with verified
2026 tax figures; additional zones can be added as wholesale-only (no tax block).

## Setup

1. Create a free ENTSO-E account at https://transparency.entsoe.eu and request an
   API token (Account Settings -> *Generate a new token*, or email the service desk).
2. Add it as a repository secret named `ENTSOE_TOKEN`
   (`gh secret set ENTSOE_TOKEN`).
3. Enable Pages with **Source = GitHub Actions**.
4. Run the **Refresh forecast & deploy** workflow (or wait for the daily schedule).

## License

Code: see [LICENSE](./LICENSE). Data attribution: ENTSO-E, Open-Meteo, Ember/Yahoo.
