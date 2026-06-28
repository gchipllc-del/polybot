#!/bin/bash
# series_collect runner — forward price→outcome collection for one Kalshi series.
# Paper/data only — places NO orders. Forwards all args to series_collect.py.
#
# Usage:  run_series_collect.sh collect KXAAAGASD  |  run_series_collect.sh settle KXAAAGASD
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
echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) series_collect $* ===" >> "$PROJECT_ROOT/logs/series_collect.log"
exec python scripts/series_collect.py "$@" >> "$PROJECT_ROOT/logs/series_collect.log" 2>&1
