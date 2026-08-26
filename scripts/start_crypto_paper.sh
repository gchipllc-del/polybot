#!/bin/bash
# Start the Kalshi 15-min crypto (BTC) PAPER loop on this host.
#
# One-shot bootstrap: smoke-test one cycle, install + (re)load the 60s
# launchd cron, then tail the log. Paper only — real orders stay gated
# behind live_migration_approved in config/settings.yaml.
#
# PREREQ: network policy must allow api.elections.kalshi.com + the price
# feed (api.binance.us / Coinbase). If the smoke test shows no markets,
# check those first.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

PLIST="com.jesse.polybot.kalshi_15min.plist"
PLIST_SRC="$PROJECT_ROOT/scripts/launchd/$PLIST"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
LOG="$PROJECT_ROOT/logs/launchd_kalshi_15min.log"
PY="$(command -v python || command -v python3)"

echo "→ Project: $PROJECT_ROOT"
echo "→ Python:  $PY"
if [ -f "$PROJECT_ROOT/.env" ]; then set -a; source "$PROJECT_ROOT/.env"; set +a; fi
mkdir -p "$PROJECT_ROOT/logs"

echo
echo "── Step 1/3: smoke-test one paper cycle ─────────────────────────────"
if ! "$PY" main.py kalshi-15min-monitor; then
  echo "✗ Smoke test failed. Fix the error above before installing the cron."
  exit 1
fi

echo
echo "── Step 2/3: install + load the 60s cron ────────────────────────────"
mkdir -p "$LAUNCH_AGENTS"
cp "$PLIST_SRC" "$LAUNCH_AGENTS/$PLIST"
launchctl unload "$LAUNCH_AGENTS/$PLIST" 2>/dev/null || true
launchctl load "$LAUNCH_AGENTS/$PLIST"
if launchctl list | grep -q "com.jesse.polybot.kalshi_15min$"; then
  echo "✓ Cron loaded: com.jesse.polybot.kalshi_15min (every 60s)"
else
  echo "✗ Cron did not register. Check: launchctl list | grep kalshi_15min"
  exit 1
fi

echo
echo "── Step 3/3: watch it run ───────────────────────────────────────────"
echo "  Paper P&L:   $PY main.py kalshi-15min-paper-report"
echo "  Dashboard:   http://localhost:5053  (python main.py kalshi-dashboard)"
echo
echo "Tailing the log (Ctrl-C to stop watching — the cron keeps running):"
echo
touch "$LOG"; tail -f "$LOG"
