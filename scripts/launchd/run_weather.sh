#!/bin/bash
# Kalshi WEATHER scanner. Fires every 5 minutes during likely-market-hours
# (NWS forecasts update ~hourly, but Kalshi prices shift faster as the
# close window approaches).
set -uo pipefail

cd /Users/jesse/Desktop/projects/polybot

/Users/jesse/anaconda3/bin/python main.py weather-paper-settle \
    >> logs/launchd_weather.log 2>> logs/launchd_weather_err.log

/Users/jesse/anaconda3/bin/python main.py weather-monitor \
    >> logs/launchd_weather.log 2>> logs/launchd_weather_err.log

# ── SHADOW A/B (2026-06-02): ungated "original" replica, PAPER-ONLY and
# hard-guarded (WEATHER_PAPER_ONLY=1 inside the script). Runs on the same
# 5-min cadence against the same markets, writing a separate ledger
# (data/weather_paper_original.jsonl). The `|| true` guarantees a shadow
# failure can NEVER abort or affect the live sleeve above. To disable the
# A/B, just delete these two lines.
# RETIRED 2026-06-03: original/ungated arm went 90 settled, 21% WR, -$510 paper —
# definitively refuted the "original loose params win" thesis. Stopped firing;
# ledger data/weather_paper_original.jsonl kept for the record.
# bash scripts/launchd/run_weather_original.sh || true

# ── SHADOW A/B control arm: edge-gated "current strategy" replica, also
# PAPER-ONLY + hard-guarded, own ledger (data/weather_paper_edge.jsonl). Same
# `|| true` isolation. Together the two shadows form the controlled A/B. ──
bash scripts/launchd/run_weather_edge.sh || true

# Phase-0 PERPS FUNDING LOGGER — read-only measurement, self-throttled to ~hourly.
# `|| true` so it can NEVER affect the weather sleeves. Accrues the 1-2 week
# funding/basis series for the perps measure-first gate (memory perps_roadmap).
bash scripts/launchd/run_perps_funding.sh || true

# ── SHADOW A/B arm 3: MEASURED-MISPRICING edge (the "real edge" — realized WR
# vs price, no forecast), PAPER-ONLY, own ledger. Same `|| true` isolation. ──
# RETIRED 2026-06-03: mispricing arm went 91 settled, 19% WR, -$1180 paper —
# confirmed gauge-as-sleeve failure (memory: mispricing_edge). Never went live
# (verified is_live=0). Stopped firing; ledger kept for the record.
# bash scripts/launchd/run_weather_mispricing.sh || true
