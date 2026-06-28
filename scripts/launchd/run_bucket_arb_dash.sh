#!/bin/bash
# bucket-arb dashboard runner — renders the bucket-arb paper sleeve's own
# scoreboard (separate bankroll from weather). Forwards args to bucket_arb_dash.py.
#
# Usage:  run_bucket_arb_dash.sh render   |   run_bucket_arb_dash.sh serve --port 5053
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

exec python scripts/bucket_arb_dash.py "$@"
