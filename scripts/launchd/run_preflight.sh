#!/bin/bash
# preflight live-safety guard runner — runs preflight_live_check.py and, if it finds
# OPEN real exposure or an ARMED live switch (non-zero exit), writes an ALARM banner to
# the log and (best-effort) fires a Telegram alert if TELEGRAM_BOT_TOKEN/CHAT_ID are set.
# Read-only; places/cancels nothing. Install it IN THE CLONE THE LIVE AGENTS USE so it
# audits the authoritative config + data (this repo defaults to its own checkout).
#
# Usage:  run_preflight.sh                 # audit this clone's data/ + live switch
#         run_preflight.sh --data /path    # audit a specific data dir
set -uo pipefail   # NOTE: no -e — we want to capture the non-zero exit, not die on it

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
LOG="$PROJECT_ROOT/logs/preflight.log"
ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
out="$(python scripts/preflight_live_check.py "$@" 2>&1)"
code=$?
{
  echo "=== $ts preflight (exit $code) ==="
  echo "$out"
} >> "$LOG"

if [ "$code" -ne 0 ]; then
  echo "⚠⚠⚠ $ts PREFLIGHT ALARM (exit $code) — open live exposure or armed switch ⚠⚠⚠" >> "$LOG"
  if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
    curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
      --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
      --data-urlencode "text=⚠ polybot PREFLIGHT ALARM on $(hostname): open live exposure or the live switch is ARMED. Check logs/preflight.log." \
      >/dev/null 2>&1 || true
  fi
fi
exit "$code"
