#!/bin/bash
# Start the Kalshi weather (hourly) PAPER loop on this host.
#
# One-shot bootstrap:
#   1. Runs ONE sampling cycle as a smoke test (proves network + series
#      discovery + forecast blending work) — this is paper only, never
#      places real orders.
#   2. Installs + (re)loads the launchd cron so it keeps running every 5 min.
#   3. Tails the log so you can watch the first scheduled cycle.
#
# Safe: the sleeve is Phase-2 paper. Real orders stay gated behind
# live_migration_approved in config/settings.yaml regardless.
#
# PREREQ: your network policy must allow api.weather.gov + api.open-meteo.com
# (and api.elections.kalshi.com). If the smoke test shows 0 markets / forecast
# errors, that's the first thing to check.
set -euo pipefail

# Resolve project root from this script's location (scripts/ -> repo root).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

PLIST="com.jesse.polybot.kalshi_weather.plist"
PLIST_SRC="$PROJECT_ROOT/scripts/launchd/$PLIST"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
LOG="$PROJECT_ROOT/logs/launchd_kalshi_weather.log"

PY="$(command -v python || command -v python3)"

echo "→ Project: $PROJECT_ROOT"
echo "→ Python:  $PY"

if [ -f "$PROJECT_ROOT/.env" ]; then
  set -a; # shellcheck disable=SC1091
  source "$PROJECT_ROOT/.env"; set +a
fi

mkdir -p "$PROJECT_ROOT/logs"

echo
echo "── Step 1/3: smoke-test one paper cycle ─────────────────────────────"
if ! "$PY" main.py kalshi-weather-monitor; then
  echo "✗ Smoke test failed. Fix the error above before installing the cron."
  echo "  (Common cause: api.weather.gov / api.open-meteo.com not allowlisted.)"
  exit 1
fi

echo
echo "── Step 2/3: install + load the 5-min cron ──────────────────────────"
mkdir -p "$LAUNCH_AGENTS"
cp "$PLIST_SRC" "$LAUNCH_AGENTS/$PLIST"
# Reload cleanly if it was already loaded.
launchctl unload "$LAUNCH_AGENTS/$PLIST" 2>/dev/null || true
launchctl load "$LAUNCH_AGENTS/$PLIST"
if launchctl list | grep -q "com.jesse.polybot.kalshi_weather$"; then
  echo "✓ Cron loaded: com.jesse.polybot.kalshi_weather (every 300s)"
else
  echo "✗ Cron did not register. Check: launchctl list | grep kalshi_weather"
  exit 1
fi

echo
echo "── Step 3/3: watch it run ───────────────────────────────────────────"
echo "  Live log:    tail -f $LOG"
echo "  Paper P&L:   $PY main.py kalshi-weather-paper-report"
echo "  Dashboard:   $PY main.py kalshi-weather-dashboard   # http://localhost:5054"
echo
echo "Tailing the log (Ctrl-C to stop watching — the cron keeps running):"
echo
touch "$LOG"
tail -f "$LOG"
