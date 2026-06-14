#!/bin/bash
# ensemble_collect runner — forward A/B test (ensemble vs σ=3 forecast).
# Records day-ahead weather markets with both forecast probabilities + the
# market price, settles outcomes, for a later `eval`. Paper/data only — places
# NO orders. Forwards all args to ensemble_collect.py.
#
# Usage:  run_ensemble.sh collect   |   run_ensemble.sh settle
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
echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) ensemble $* ===" >> "$PROJECT_ROOT/logs/ensemble.log"
exec python scripts/ensemble_collect.py "$@" >> "$PROJECT_ROOT/logs/ensemble.log" 2>&1
