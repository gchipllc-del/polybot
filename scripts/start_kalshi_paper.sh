#!/bin/bash
# Start the KALSHI paper-trading sleeves on this host (and only those):
#   - kalshi_15min   (Kalshi 15-min BTC,   60s)
#   - kalshi_weather (Kalshi hourly temp, 300s)
#
# For each: run ONE cycle as a smoke test (paper only, never real orders),
# then install + (re)load its launchd cron. A sleeve whose smoke test fails
# (e.g. an API not allowlisted) is skipped with a warning; the other still
# comes up.
#
# Everything stays paper: real orders are gated behind
# live_migration_approved in config/settings.yaml.
#
# PREREQ allowlist: api.elections.kalshi.com (both), api.weather.gov +
# api.open-meteo.com (weather), and the BTC price feed (15-min).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
PY="$(command -v python || command -v python3)"

echo "→ Project: $PROJECT_ROOT"
echo "→ Python:  $PY"
if [ -f "$PROJECT_ROOT/.env" ]; then set -a; source "$PROJECT_ROOT/.env"; set +a; fi
mkdir -p "$PROJECT_ROOT/logs" "$LAUNCH_AGENTS"

# sleeve: "label|monitor-command"
SLEEVES=(
  "com.jesse.polybot.kalshi_15min|kalshi-15min-monitor"
  "com.jesse.polybot.kalshi_weather|kalshi-weather-monitor"
)

loaded=()
skipped=()
for entry in "${SLEEVES[@]}"; do
  label="${entry%%|*}"
  cmd="${entry##*|}"
  plist="$label.plist"
  src="$PROJECT_ROOT/scripts/launchd/$plist"
  echo
  echo "════════════════════════════════════════════════════════════════════"
  echo "  $label  ($cmd)"
  echo "════════════════════════════════════════════════════════════════════"
  if [ ! -f "$src" ]; then
    echo "  ✗ missing plist: $src — skipping"; skipped+=("$label"); continue
  fi
  echo "  → smoke-testing one cycle…"
  if "$PY" main.py "$cmd"; then
    cp "$src" "$LAUNCH_AGENTS/$plist"
    launchctl unload "$LAUNCH_AGENTS/$plist" 2>/dev/null || true
    if launchctl load "$LAUNCH_AGENTS/$plist" && launchctl list | grep -q "${label}\$"; then
      echo "  ✓ loaded $label"; loaded+=("$label")
    else
      echo "  ✗ failed to load $label"; skipped+=("$label")
    fi
  else
    echo "  ✗ smoke test failed (API blocked / no markets?) — cron NOT loaded"
    skipped+=("$label")
  fi
done

echo
echo "════════════════════════════════════════════════════════════════════"
echo "  SUMMARY"
echo "════════════════════════════════════════════════════════════════════"
echo "  loaded:  ${loaded[*]:-(none)}"
echo "  skipped: ${skipped[*]:-(none)}"
echo
echo "  Reports:"
echo "    $PY main.py kalshi-15min-paper-report"
echo "    $PY main.py kalshi-weather-paper-report"
echo "  Dashboards: :5053 crypto · :5054 weather"
echo
echo "  All paper. Real orders remain gated (live_migration_approved: false)."
