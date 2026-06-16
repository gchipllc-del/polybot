#!/bin/bash
# bucket_arb runner — scans Kalshi mutually-exclusive ladders for structural
# (prediction-free) arbitrage and logs each ladder's sweep margin so a week of
# scans yields the near-miss distribution. Paper/data only — places NO orders.
# Forwards all args to bucket_arb.py.
#
# Usage:  run_bucket_arb.sh --collect   |   run_bucket_arb.sh --eval
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
echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) bucket_arb $* ===" >> "$PROJECT_ROOT/logs/bucket_arb.log"
exec python scripts/bucket_arb.py "$@" >> "$PROJECT_ROOT/logs/bucket_arb.log" 2>&1
