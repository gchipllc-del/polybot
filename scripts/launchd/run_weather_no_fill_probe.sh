#!/bin/bash
# weather-NO fill-realism probe runner. Discovers the current weather-NO candidate
# markets via the live signal and snapshots their Kalshi order books, logging the NO-side
# depth curve to data/weather_no_fill_probe.jsonl. This is the ONE check the paper ledger
# can't do — whether the cheap-NO fills are actually available in size. Read-only (no
# orders). Schedule every ~5 min during weather windows; review with:
#   python scripts/weather_no_fill_probe.py report
# Self-locating: resolves the repo from this script's own path. Sources .env for the
# Kalshi creds that get_orderbook needs.
set -euo pipefail

export PATH="/Users/jesse/anaconda3/bin:$PATH"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT"

# Kalshi credentials (order-book reads are authenticated).
if [ -f "$PROJECT_ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$PROJECT_ROOT/.env"
  set +a
fi

mkdir -p "$PROJECT_ROOT/logs"
exec python scripts/weather_no_fill_probe.py capture --from-signals \
  >> "$PROJECT_ROOT/logs/weather_no_fill_probe.log" 2>&1
