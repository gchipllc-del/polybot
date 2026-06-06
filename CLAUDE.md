# CLAUDE.md — polybot operating guide

Prediction-market trading bot for **Kalshi** (CFTC-regulated, real money) and
**Polymarket** (paper). Read this before doing anything — parts of this system
trade **real money live**.

Primary dev branch: `research-pass-2026-05-24`.
Shared core lives in the sibling repo `../tradingcore` (`pip install -e ../tradingcore`).

---

## ⚠️ SAFETY — read first (real money is live)

- **Live trading is REAL** via `lib/kalshi_live_executor.py`. Live caps in
  `config/settings.yaml`:
  - `max_trade_bankroll_pct: 0.10` (per-trade ≤ 10% of cash) with `max_trade_usd: 50` as an absolute backstop
  - `max_concurrent: 4` total open; per-asset `live_asset_max_concurrent` (weather 3 / btc 1 / …)
  - `live_assets:` currently includes `weather`, `weather_daily` (and btc)
  - `live_migration_approved` / `mode` gate the real-money path
- **Do NOT** enable live, raise caps, add to `live_assets`, or edit the live
  executor / order path **without explicit user approval.** Surface the change
  and ask first.
- This is a **real-money machine** — when running the local CLI, use **per-command
  approval** (never `--dangerously-skip-permissions`).
- **Paper P&L is not truth.** Judge every sleeve by trades settled on the actual
  exchange result, not the headline. See "Analysis discipline" below.

---

## Sleeves (each: signal → paper recorder → settle; some wire the live executor)

| Sleeve | Signal / paper modules | Ledger | Notes |
|---|---|---|---|
| Kalshi 15-min crypto | `kalshi_15min_signal.py` / `kalshi_15min_paper.py` | `data/kalshi_15min_paper.jsonl` | **BTC-only** (ETH/SOL disabled — they were 25% WR). Settles on real Kalshi result. |
| Kalshi weather **hourly** | `weather_signal.py` / `weather_paper.py` | `data/weather_paper.jsonl` | NWS+Open-Meteo+ECMWF ensemble. Live-capable. NO-side is the historical winner. |
| Kalshi weather **daily** (high/low) | `weather_daily_signal.py` / `weather_daily_paper.py` | `data/weather_daily_paper.jsonl` | Multi-city KXHIGHT*/KXLOWT*. Live-capable. |
| Polymarket BTC arb | `btc_arb_signal.py` | `data/btc_arb_paper.jsonl` | **Losing (−$45, 35% WR) — being disabled.** |
| Polymarket BTC 5-min | `btc_5min_signal.py` | `data/btc_5min_paper.jsonl` | Was dead (Binance.US blocked); now on Coinbase feed. |

Live order routing for all live sleeves goes through `lib/kalshi_live_executor.py`
(daily-loss halt + kill-switch + concurrency caps).

---

## Data sources

- **Crypto price + 1-min klines:** Coinbase Exchange public API is **primary**
  (`fetch_binance_btc_price` / `fetch_binance_klines` in `btc_5min_signal.py` —
  names kept for back-compat; Binance.US is geo-blocked, kept only as fallback).
  Crypto is priced off a **60s trimmed-mean RTI settlement-shadow**
  (`fetch_coinbase_rti_proxy`) that matches how Kalshi settles. `whale_monitor`
  also uses Coinbase.
- **Weather:** NWS (`api.weather.gov`) + Open-Meteo (ECMWF/ICON/GFS ensemble) +
  per-city calibration. Settlement is the official station obs / daily extreme.

The up/down crypto call is a **composite**: RSI-14 (chart, on the klines) +
Black-Scholes theo-gap + market-agreement + whale flow — not a single indicator.

---

## Analysis discipline (this is where money was lost vs. imagined)

- **Weather paper massively overstates.** It often settles via NWS observation,
  which diverges from Kalshi ~17% of the time. Use `scripts/weather_report.py`
  and **read the `kalshi_result` line only** — that's the real-money proxy.
  (Real hourly weather was −$105/24% WR while paper showed +$3,215.)
- **Beware cheap-NO longshots** (fill < ~$0.15, ~19:1 payoff): they look great
  on one lucky hit but are ~46% WR and net-negative on real settlement. Big
  payouts there are variance, not edge.
- **Daily-high forecasts are ±2–3°F at day-ahead** — narrow buckets bet a day out
  are unwinnable by physics, not by a bad API. Edge (if any) comes from *when*
  and *which bucket*, not a sharper forecast.
- Size small. One oversized $25 trade lost more than a sleeve's entire net.

---

## Common commands (`python main.py …`)

```
kalshi-15min-monitor / -paper-settle / -paper-report     # BTC 15-min sleeve
kalshi-weather-* (if present) , weather monitors via launchd
kalshi-dashboard            # crypto dashboard :5053
```
Plus standalone scripts:
```
python scripts/weather_report.py [--log data/weather_daily_paper.jsonl]  # trust-aware P&L (read kalshi_result line)
python scripts/coinbase_rti_shadow.py                                    # live 60s settlement-shadow (manual watch)
```
Crons are launchd plists in `scripts/launchd/` (load/unload with `launchctl`).
Disable a sleeve = `launchctl unload ~/Library/LaunchAgents/<label>.plist`.

---

## Local data / state

`data/*.jsonl` ledgers are **gitignored, host-local** (force-add to share for
analysis). Never commit them as part of normal work. Calibration lives in
`data/weather_calibration.json` (separate from the ledgers).

---

## Work in flight (open PRs vs `research-pass-2026-05-24`, draft)

- `_price` cents/dollars fix (catastrophe-floor correctness)
- timing + margin entry gates (hourly + daily, config-flagged, OFF by default)
- `scripts/weather_report.py` (trust-aware P&L tool)
- Coinbase primary crypto feed + RTI shadow
> Several touch files with uncommitted local edits — `git diff` and reconcile
> before merging.
