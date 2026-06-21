#!/bin/bash
# asos bucket-lock runner — dispatches to asos_tracker / asos_dash. Reads the
# REALIZED daily high from the settlement ASOS station (Iowa Environmental Mesonet)
# and flags now-near-certain Kalshi buckets the overnight book still misprices.
# Paper/data only — places NO orders. Sources .env (Kalshi auth for the ladder).
#
# Usage:  run_asos.sh asos_tracker scan
#         run_asos.sh asos_tracker settle
#         run_asos.sh asos_dash render
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
mod="$1"; shift
echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) $mod $* ===" >> "$PROJECT_ROOT/logs/asos.log"
exec python "scripts/$mod.py" "$@" >> "$PROJECT_ROOT/logs/asos.log" 2>&1
