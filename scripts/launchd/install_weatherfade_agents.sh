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

# write_agent LABEL  CADENCE  RUNNER  [args...]
#   CADENCE: a number    = StartInterval seconds (fires at load too)
#            "cal:MM"    = hourly at minute MM (StartCalendarInterval, no
#                          RunAtLoad) — staggers the API agents so they don't
#                          all sweep Kalshi in the same second and trip 429s
#            "keepalive" = KeepAlive long-run
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
  elif [[ "$cadence" == cal:* ]]; then
    local minute="${cadence#cal:}"
    cadence_xml="  <key>StartCalendarInterval</key>
  <dict><key>Minute</key><integer>$minute</integer></dict>"
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
# Hourly API agents, STAGGERED across the hour (cal:MM = hourly at minute MM).
# The heavy market sweeps (scan/fc2sscan/probe/collect) are spaced 12-18 min
# apart; settles slot between them. All-at-once firing tripped Kalshi 429s.
write_agent "$P.scan"          cal:05 "$RUN" scan --thr 0.03
write_agent "$P.fc2ssettle"    cal:12 "$RUN_FC2S" settle
write_agent "$P.fc2sscan"      cal:20 "$RUN_FC2S" scan --thr 0.05
write_agent "$P.settle"        cal:32 "$RUN" settle
write_agent "$P.probe"         cal:38 "$RUN" probe
write_agent "$P.collectsettle" cal:44 "$RUN" collect-settle
write_agent "$P.collect"       cal:50 "$RUN" collect
# dashboard — file render every 5 min (the reliable view: open the HTML file,
# no server/localhost/https). The live Flask :5060 server is intentionally NOT
# installed (it flapped and the file view replaced it); start it manually with
# `python scripts/weather_fade_dash.py serve` only if you want the live URL.
write_agent "$P.dashfile"       300 "$RUN_DASH" render
# live auto-refreshing link: stdlib server (no Flask) on 127.0.0.1:5052,
# KeepAlive so it's always up. Open http://127.0.0.1:5052 (http, not https).
write_agent "$P.dashserve" keepalive "$RUN_DASH" serve --port 5052 --host 127.0.0.1

echo ""
echo "Done. Roster:"
launchctl list 2>/dev/null | grep weatherfade || echo "  (none listed — check Console for load errors)"
echo ""
echo "Dashboard (live link):  http://127.0.0.1:5052   (http, not https)"
echo "Dashboard (file backup): open $PROJECT_ROOT/data/weather_fade_dash.html"
echo "Scorecards: python scripts/weather_fade.py report   |   python scripts/fc_two_sided.py report"
echo "NOTE: staggered agents fire at their minute-of-hour (scan :05, fc2s :20, probe :38,"
echo "      collect :50) — first runs happen within the next hour, NOT at install."
echo "NOTE: also keep the Mac awake during the US-evening liquid window (caffeinate -dimsu &)."
