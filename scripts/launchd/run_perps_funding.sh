#!/bin/bash
# Phase-0 PERPS FUNDING LOGGER — READ-ONLY measurement (no trading path exists in
# lib/perps_funding_logger.py). Self-throttles to ~hourly (funding updates every
# 8h), so it's safe to call from the 5-min weather cron. Accrues funding+basis to
# data/perps_funding_log.jsonl for the 1-2 week measure-first gate (perps_roadmap).
set -uo pipefail
cd /Users/jesse/Desktop/projects/polybot

/Users/jesse/anaconda3/bin/python main.py perps-funding-log \
    >> logs/perps_funding.log 2>> logs/perps_funding_err.log
