# Weather-fade harness (launchd agents)

The `weather_fade` sleeve runs hands-free as a set of macOS LaunchAgents. This
directory holds the runners + an installer so the whole setup is reproducible
(the live `.plist` files in `~/Library/LaunchAgents/` are not in git).

## Install / reproduce everything
```bash
bash scripts/launchd/install_weatherfade_agents.sh
caffeinate -dimsu &     # keep the Mac awake during the US-evening liquid window
```

## What runs

| Agent (`com.jesse.polybot.weatherfade.*`) | Cadence | Does |
|---|---|---|
| `scan`          | hourly        | book "fade overpriced YES" paper trades at the live book (`--thr 0.03`) |
| `probe`         | hourly        | log book liquidity by hour (maps the live window) |
| `collect`       | hourly        | forward-collect hourly-weather price→outcome data |
| `collectsettle` | hourly        | fill outcomes for collected hourly markets |
| `settle`        | hourly        | resolve booked fades → scorecard |
| `fc2sscan`      | hourly        | book **forecast two-sided** paper trades (`--thr 0.05`) — the live execution test of the `forecast_skill_days` rank-skill edge |
| `fc2ssettle`    | hourly        | resolve fc2s trades → scorecard |
| `dashfile`      | every 5 min   | re-render the dashboard to `data/weather_fade_dash.html` (open via `file://`) |

> The live Flask server (`:5060`) is **not** installed by default — the file
> render replaced it (localhost/https quirks made the server view unreliable).
> Run it manually only if you want the live URL: `python scripts/weather_fade_dash.py serve`.

## View it
- **Dashboard (reliable):** `open data/weather_fade_dash.html` — bookmark the `file://` URL.
- **Scorecard (price-only fade):** `python scripts/weather_fade.py report`
- **Scorecard (forecast two-sided):** `python scripts/fc_two_sided.py report`
- **Pattern analysis:** `python scripts/weather_fade.py analyze`

## Why two sleeves
`weather_fade` is the price-only fade — the backtest showed it's +EV but
directional (corr(net, YES-rate) ≈ −0.8: a short-heat bet). `fc2s` is the
forecast two-sided rule that the controls validated (rank-skill lift +0.15 at
fixed price; beats its own side-scrambled floor by +0.05/ct over 285 OOS days).
They run in parallel so the live scorecards answer the only question the
backtest can't: does the thin +0.04/ct survive real fills on flickering books?
fc2s carries a per-event-date risk cap (`DAY_RISK_CAP_USD`) because the
residual corr −0.37 means one hot day still hits several cities at once.

## Liquidity / sleep note
Weather books are only live ~14:00–03:00 UTC (US daytime/evening); 04:00–13:00 UTC
is dead (no markets). `launchd` interval timers don't fire while the Mac is
asleep, so keep it awake during the evening window or trades won't book.

## The verdict is per-DAY, not per-trade
A day's city-fades share one weather pattern → they're correlated. Judge the edge
by **distinct settlement dates** (the `report` per-day panel), not trade count.
~20–30 distinct days before anything is conclusive.
