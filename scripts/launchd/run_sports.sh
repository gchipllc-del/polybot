#!/bin/bash
# sports sleeves runner — dispatches to sports_lock / devig_check / sports_eval.
# Paper/data only — places NO orders. Sources .env (Kalshi + ODDS_API_KEY).
#
# Usage:  run_sports.sh sports_lock scan nba KXNBAGAME --confirm
#         run_sports.sh devig_check scan nba KXNBAGAME
#         run_sports.sh sports_eval  eval
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
echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) $mod $* ===" >> "$PROJECT_ROOT/logs/sports.log"
exec python "scripts/$mod.py" "$@" >> "$PROJECT_ROOT/logs/sports.log" 2>&1
