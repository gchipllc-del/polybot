#!/bin/bash
# fc2s (forecast two-sided) paper sleeve runner — called by the LaunchAgents.
# Books two-sided paper trades (buy NO when the market's YES is rich vs the
# Open-Meteo forecast, buy YES when cheap) at the LIVE Kalshi book (NO real
# orders) and settles them. Runs NEXT TO weather_fade, not instead of it —
# the two sleeves accumulate live scorecards side by side.
#
# Usage:  run_fc2s.sh scan [--thr 0.05]
#         run_fc2s.sh settle
# Forwards ALL args to fc_two_sided.py.
#
# Self-locating: resolves the repo from this script's own path.
set -euo pipefail

export PATH="/Users/jesse/anaconda3/bin:$PATH"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT"

if [ -f "$PROJECT_ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$PROJECT_ROOT/.env"
  set +a
fi

mkdir -p "$PROJECT_ROOT/logs"
echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) fc2s $* ===" >> "$PROJECT_ROOT/logs/fc2s.log"
exec python scripts/fc_two_sided.py "$@" >> "$PROJECT_ROOT/logs/fc2s.log" 2>&1
