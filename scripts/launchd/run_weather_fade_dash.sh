#!/bin/bash
# weather-fade dashboard runner — long-running Flask server on :5060.
# Called by com.jesse.polybot.weatherfade.dash (KeepAlive + RunAtLoad), so it
# auto-starts and respawns like the main poly dashboard. Self-locating: resolves
# the repo from this script's own path (works from the backtest clone).
set -euo pipefail

export PATH="/Users/jesse/anaconda3/bin:$PATH"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT"

mkdir -p "$PROJECT_ROOT/logs"
exec python scripts/weather_fade_dash.py "$@" >> "$PROJECT_ROOT/logs/weather_fade_dash.log" 2>&1
