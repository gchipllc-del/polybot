#!/bin/bash
# sports-calibration dashboard runner. Forwards args to series_collect_dash.py.
# Usage:  run_series_collect_dash.sh render  |  run_series_collect_dash.sh serve --port 5054
set -euo pipefail
export PATH="/Users/jesse/anaconda3/bin:$PATH"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT"
if [ -f "$PROJECT_ROOT/.env" ]; then set -a; source "$PROJECT_ROOT/.env"; set +a; fi
exec python scripts/series_collect_dash.py "$@"
