#!/bin/bash
# fc2s_shadow runner — measurement-ONLY forecast-error collector for fc2s tail
# recalibration. NEVER trades, never reads the order book; logs day-ahead forecast
# highs and their realized highs so the (forecast, realized) error distribution
# accrues (the evidence TRADE_ABOVE_STRIKES waits on). Forwards args to fc2s_shadow.py.
#
# Usage:  run_fc2s_shadow.sh collect | settle | report
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
echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) fc2s_shadow $* ===" >> "$PROJECT_ROOT/logs/fc2s_shadow.log"
exec python scripts/fc2s_shadow.py "$@" >> "$PROJECT_ROOT/logs/fc2s_shadow.log" 2>&1
