#!/bin/bash
# Liquidity-capture runner — samples resting-book liquidity for the candidate series and
# appends to data/liquidity_log.jsonl. Scheduled every ~20 min so it catches each venue
# while it's ACTIVE (equities 9:30-16:00 ET, sports during live games) — a single
# off-hours snapshot is meaningless for session-bound / event-driven markets.
set -uo pipefail
export PATH="/Users/jesse/anaconda3/bin:$PATH"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT"
mkdir -p "$PROJECT_ROOT/data" "$PROJECT_ROOT/logs"
# Financial (bucket_arb), sports (lock thesis), + weather control. Override via env.
SERIES="${LIQUIDITY_SERIES:-KXINX,KXBTCD,KXNDQ,KXETHD,KXWNBAGAME,KXNBAGAME,KXNHLGAME,KXMLBGAME,KXHIGHNY}"
exec python scripts/kalshi_liquidity.py --series "$SERIES" \
     --log "$PROJECT_ROOT/data/liquidity_log.jsonl" \
     >> "$PROJECT_ROOT/logs/liquidity_capture.log" 2>&1
