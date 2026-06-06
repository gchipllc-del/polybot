#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────
# SHADOW MISPRICING arm — PAPER-ONLY. The THIRD A/B arm: trades polybot's
# MEASURED mispricing edge (lib/mispricing_paper.py) — bets the underpriced NO
# side from the realized-WR gauge, IGNORING the forecast. Own ledger
# (data/weather_paper_mispricing.jsonl), settled by the same weather-paper-settle
# (via WEATHER_PAPER_LOG). There is NO live-execution path in this sleeve at all;
# WEATHER_PAPER_ONLY=1 is belt-and-suspenders. Runs on the same 5-min cadence.
# ─────────────────────────────────────────────────────────────────────────
set -uo pipefail
cd /Users/jesse/Desktop/projects/polybot

export WEATHER_PAPER_LOG="/Users/jesse/Desktop/projects/polybot/data/weather_paper_mispricing.jsonl"
# LIVE ARMED 2026-06-02 (explicit user sign-off). The mispricing edge now trades
# REAL money, hard-capped at $40 open exposure (MISPRICING_LIVE_BUDGET) on top of
# every executor rail (balance floor, kill-switch, daily-loss, dedup, per-asset
# budget). Routes under asset="weather" so it shares the weather pool + dedup.
# To return to paper: set MISPRICING_LIVE=0 (or delete these two lines).
export MISPRICING_LIVE="0"   # DISARMED 2026-06-03: paper arm went 0/5 (-$100) on
                             # its first settles — correlated NO clusters losing
                             # into a warm forecast. Reverted to paper pending a
                             # decision. Set to "1" to re-arm live ($40 cap).
export MISPRICING_LIVE_BUDGET="40"

# Settle the mispricing ledger (reads WEATHER_PAPER_LOG), then scan + record.
/Users/jesse/anaconda3/bin/python main.py weather-paper-settle \
    >> logs/launchd_weather_mispricing.log 2>> logs/launchd_weather_mispricing_err.log

/Users/jesse/anaconda3/bin/python main.py weather-mispricing-cycle \
    >> logs/launchd_weather_mispricing.log 2>> logs/launchd_weather_mispricing_err.log
