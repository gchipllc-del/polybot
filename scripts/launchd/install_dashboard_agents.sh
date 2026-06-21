#!/bin/bash
# Install the two web dashboards as KeepAlive LaunchAgents pointing at THIS clone:
#   com.jesse.polybot.dashboard        → main Flask dashboard   (localhost:5050)
#   com.jesse.polybot.kalshi_dashboard → Kalshi 15-min dashboard (localhost:5053)
# Use this to move them off a TCC-blocked path (e.g. ~/Desktop) onto an accessible
# clone such as ~/polybot-backtest. Re-running overwrites any existing agent of the
# same label (so it cleanly repoints from the old path).
#
#   bash scripts/launchd/install_dashboard_agents.sh
#   bash scripts/launchd/install_dashboard_agents.sh --uninstall
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
UNINSTALL=0
[ "${1:-}" = "--uninstall" ] && UNINSTALL=1
mkdir -p "$AGENTS" "$PROJECT_ROOT/logs"

mk() {
  local label="$1" runner="$2"
  local plist="$AGENTS/$label.plist"
  chmod +x "$runner" 2>/dev/null || true
  launchctl unload "$plist" 2>/dev/null || true     # detach old (possibly Desktop-path) agent
  if [ "$UNINSTALL" = "1" ]; then
    rm -f "$plist"; echo "  removed $label"; return
  fi
  cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array><string>$runner</string></array>
  <key>KeepAlive</key><true/>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$PROJECT_ROOT/logs/$label.log</string>
  <key>StandardErrorPath</key><string>$PROJECT_ROOT/logs/$label.log</string>
</dict></plist>
EOF
  launchctl load "$plist" 2>/dev/null || true
  echo "  installed $label -> $runner"
}

mk "com.jesse.polybot.dashboard"        "$SCRIPT_DIR/run_dashboard.sh"
mk "com.jesse.polybot.kalshi_dashboard" "$SCRIPT_DIR/run_kalshi_dashboard.sh"
# Weather research view (fade + fc2s + ensemble). run_weather_fade_dash.sh with no
# args defaults to `serve --port 5052`; embedded inline in the main dashboard.
mk "com.jesse.polybot.weatherfade.dashserve" "$SCRIPT_DIR/run_weather_fade_dash.sh"

if [ "$UNINSTALL" = "1" ]; then
  echo "dashboards uninstalled."
else
  echo "Done — serving from $PROJECT_ROOT:"
  echo "  main:    http://localhost:5050"
  echo "  kalshi:  http://localhost:5053"
  echo "  weather: http://localhost:5052  (also embedded in the main dashboard)"
  echo "Logs: $PROJECT_ROOT/logs/com.jesse.polybot.dashboard.log (+ .kalshi_dashboard.log)"
  echo "      $PROJECT_ROOT/logs/weather_fade_dash.log"
  echo "Status: launchctl list | grep -E 'dashboard|dashserve'"
  # Self-verify: the unload/reload restarts each server and they take ~1-2s to bind,
  # so wait before health-checking (avoids a false "couldn't connect" right after load).
  if command -v curl >/dev/null 2>&1; then
    echo "Verifying (giving the servers a moment to bind)…"
    sleep 4
    for entry in "main 5050" "weather 5052" "kalshi 5053"; do
      name="${entry% *}"; port="${entry#* }"
      code=$(curl -s -o /dev/null -m 5 -w "%{http_code}" "http://localhost:$port/" 2>/dev/null || echo 000)
      if [ "$code" = "200" ]; then
        echo "  ✓ $name  :$port → HTTP 200"
      else
        echo "  ⚠ $name  :$port → HTTP $code (may still be binding; re-check: curl -I http://localhost:$port)"
      fi
    done
  fi
fi
