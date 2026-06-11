#!/bin/bash
# weather-fade dashboard runner. Forwards args to weather_fade_dash.py, so it
# drives either mode: `render` (dashfile agent, every 5 min → HTML file) or
# `serve --port 5052` (dashserve KeepAlive agent → live auto-refreshing link).
# Self-locating: resolves the repo from this script's own path.
set -euo pipefail

export PATH="/Users/jesse/anaconda3/bin:$PATH"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT"

mkdir -p "$PROJECT_ROOT/logs"
exec python scripts/weather_fade_dash.py "$@" >> "$PROJECT_ROOT/logs/weather_fade_dash.log" 2>&1
