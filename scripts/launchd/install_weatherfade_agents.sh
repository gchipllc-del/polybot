#!/bin/bash
# Reproduce the entire weather-fade launchd harness on this machine.
# Writes all the LaunchAgents and loads them, so the hands-free system
# (scan/probe/collect/settle + dashboard) survives a fresh machine / reinstall.
# Idempotent: re-running unloads-then-reloads each agent with current settings.
#
#   bash scripts/launchd/install_weatherfade_agents.sh
#
# Self-locating: derives PROJECT_ROOT from this script's path.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
RUN="$PROJECT_ROOT/scripts/launchd/run_weather_fade.sh"
RUN_DASH="$PROJECT_ROOT/scripts/launchd/run_weather_fade_dash.sh"
RUN_FC2S="$PROJECT_ROOT/scripts/launchd/run_fc2s.sh"
AGENTS="$HOME/Library/LaunchAgents"
mkdir -p "$AGENTS"
chmod +x "$RUN" "$RUN_DASH" "$RUN_FC2S" 2>/dev/null || true

# write_agent LABEL  INTERVAL_OR_KEEPALIVE  RUNNER  [args...]
#   INTERVAL: a number = StartInterval seconds; "keepalive" = KeepAlive long-run
write_agent() {
  local label="$1"; local cadence="$2"; local runner="$3"; shift 3
  local plist="$AGENTS/$label.plist"
  local argxml=""
  for a in "$@"; do argxml+="    <string>$a</string>
"; done
  local cadence_xml
  if [ "$cadence" = "keepalive" ]; then
    cadence_xml="  <key>KeepAlive</key><true/>
  <key>RunAtLoad</key><true/>"
  else
    cadence_xml="  <key>StartInterval</key><integer>$cadence</integer>
  <key>RunAtLoad</key><true/>"
  fi
  cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array>
    <string>$runner</string>
$argxml  </array>
$cadence_xml
</dict></plist>
EOF
  launchctl unload "$plist" 2>/dev/null || true
  launchctl load "$plist" 2>/dev/null || true
  echo "  installed $label"
}

P=com.jesse.polybot.weatherfade
echo "Installing weather-fade agents (PROJECT_ROOT=$PROJECT_ROOT) ..."
# data agents — hourly
write_agent "$P.scan"          3600 "$RUN" scan --thr 0.03
write_agent "$P.probe"         3600 "$RUN" probe
write_agent "$P.collect"       3600 "$RUN" collect
write_agent "$P.collectsettle" 3600 "$RUN" collect-settle
write_agent "$P.settle"        3600 "$RUN" settle
# fc2s — the forecast two-sided sleeve (parallel paper experiment; the live
# execution test of the forecast_skill_days backtest verdict)
write_agent "$P.fc2sscan"      3600 "$RUN_FC2S" scan --thr 0.05
write_agent "$P.fc2ssettle"    3600 "$RUN_FC2S" settle
# dashboard — file render every 5 min (the reliable view: open the HTML file,
# no server/localhost/https). The live Flask :5060 server is intentionally NOT
# installed (it flapped and the file view replaced it); start it manually with
# `python scripts/weather_fade_dash.py serve` only if you want the live URL.
write_agent "$P.dashfile"       300 "$RUN_DASH" render

echo ""
echo "Done. Roster:"
launchctl list 2>/dev/null | grep weatherfade || echo "  (none listed — check Console for load errors)"
echo ""
echo "Dashboard:  open $PROJECT_ROOT/data/weather_fade_dash.html"
echo "Scorecard:  python scripts/weather_fade.py report"
echo "NOTE: also keep the Mac awake during the US-evening liquid window (caffeinate -dimsu &)."
