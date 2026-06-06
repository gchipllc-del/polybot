#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────
# SHADOW "EDGE" arm — PAPER-ONLY clean mirror of the CURRENT edge-gated live
# strategy. It is the control arm for the A/B against run_weather_original.sh:
#   * WEATHER_STRATEGY_PATH -> config/weather_strategy.yaml  (the SAME config the
#       live sleeve uses, so this arm == "exactly what we trade now")
#   * WEATHER_PAPER_LOG     -> data/weather_paper_edge.jsonl  (own clean ledger,
#       starting today, so it's directly comparable to the original shadow)
#   * WEATHER_PAPER_ONLY=1  -> HARD-disables real order placement.
# Why a separate file when the live sleeve already records edge-gated paper to
# weather_paper.jsonl? That file has months of history under many past configs;
# this gives a clean, same-start, isolated arm for an apples-to-apples A/B.
# Runs alongside the live sleeve + the original shadow on the same 5-min cadence.
# ─────────────────────────────────────────────────────────────────────────
set -uo pipefail
cd /Users/jesse/Desktop/projects/polybot

export WEATHER_STRATEGY_PATH="/Users/jesse/Desktop/projects/polybot/config/weather_strategy.yaml"
export WEATHER_PAPER_LOG="/Users/jesse/Desktop/projects/polybot/data/weather_paper_edge.jsonl"
export WEATHER_PAPER_ONLY="1"

/Users/jesse/anaconda3/bin/python main.py weather-paper-settle \
    >> logs/launchd_weather_edge.log 2>> logs/launchd_weather_edge_err.log

/Users/jesse/anaconda3/bin/python main.py weather-monitor \
    >> logs/launchd_weather_edge.log 2>> logs/launchd_weather_edge_err.log
