#!/bin/bash
# Weather-fade paper sleeve runner — called by the LaunchAgents below.
# Books day-ahead "fade overpriced YES" paper trades at the LIVE Kalshi book
# (NO real orders) and settles them. The scorecard (`weather_fade.py report`)
# is the real-fill test of the becker_edge calibration edge.
#
# Usage:  run_weather_fade.sh scan     (book new day-ahead fades)
#         run_weather_fade.sh settle   (resolve + P&L open paper trades)
#
# Self-locating: resolves the repo from this script's own path, so it works
# whether it lives in the live project OR the ~/polybot-backtest clone.
set -euo pipefail

export PATH="/Users/jesse/anaconda3/bin:$PATH"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT"

# Load auth/env if present (public book reads don't need it; settle may).
if [ -f "$PROJECT_ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$PROJECT_ROOT/.env"
  set +a
fi

MODE="${1:-scan}"
mkdir -p "$PROJECT_ROOT/logs"
echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) weather_fade $MODE ===" >> "$PROJECT_ROOT/logs/weather_fade.log"
exec python scripts/weather_fade.py "$MODE" >> "$PROJECT_ROOT/logs/weather_fade.log" 2>&1
