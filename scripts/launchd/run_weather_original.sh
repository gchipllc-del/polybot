#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────
# SHADOW hourly-weather sleeve — PAPER-ONLY A/B replica of the ORIGINAL ungated
# strategy. Runs the SAME code as the live sleeve, but with:
#   * WEATHER_STRATEGY_PATH -> config/weather_strategy_original.yaml  (ungated)
#   * WEATHER_PAPER_LOG     -> data/weather_paper_original.jsonl      (separate
#                              ledger; never pollutes the live sleeve's data)
#   * WEATHER_PAPER_ONLY=1  -> HARD-disables real Kalshi order placement for
#                              this process (defense in depth; the ungated
#                              profile must never touch real money).
# Invoked alongside the live sleeve from run_weather.sh (same 5-min cadence) so
# the A/B sees the same markets. Safe to run standalone for testing.
# ─────────────────────────────────────────────────────────────────────────
set -uo pipefail
cd /Users/jesse/Desktop/projects/polybot

export WEATHER_STRATEGY_PATH="/Users/jesse/Desktop/projects/polybot/config/weather_strategy_original.yaml"
export WEATHER_PAPER_LOG="/Users/jesse/Desktop/projects/polybot/data/weather_paper_original.jsonl"
export WEATHER_PAPER_ONLY="1"

/Users/jesse/anaconda3/bin/python main.py weather-paper-settle \
    >> logs/launchd_weather_original.log 2>> logs/launchd_weather_original_err.log

/Users/jesse/anaconda3/bin/python main.py weather-monitor \
    >> logs/launchd_weather_original.log 2>> logs/launchd_weather_original_err.log
