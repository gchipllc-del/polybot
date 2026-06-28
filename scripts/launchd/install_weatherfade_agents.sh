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
RUN_SHADOW="$PROJECT_ROOT/scripts/launchd/run_fc2s_shadow.sh"
RUN_ENS="$PROJECT_ROOT/scripts/launchd/run_ensemble.sh"
RUN_FILLPROBE="$PROJECT_ROOT/scripts/launchd/run_weather_no_fill_probe.sh"
AGENTS="$HOME/Library/LaunchAgents"
mkdir -p "$AGENTS"
chmod +x "$RUN" "$RUN_DASH" "$RUN_FC2S" "$RUN_SHADOW" "$RUN_ENS" "$RUN_FILLPROBE" 2>/dev/null || true

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
echo "Installing weather sleeve agents (PROJECT_ROOT=$PROJECT_ROOT) ..."
# NOTE: the price-fade weather-fade strategy is RETIRED (ruled out — see
# docs/FINDINGS.md: per-day PSR 0.24, failed OOS). Its scan/settle/collect/
# collectsettle/probe agents are intentionally NOT installed here anymore. What
# remains are the still-in-flight hypotheses (fc2s forecast, ensemble A/B) plus
# the dashboard file-render and the shared caffeinate keep-awake.
# Staggered cal:MM (hourly at minute MM) so they don't all hit Kalshi at once.
write_agent "$P.fc2ssettle"    cal:12 "$RUN_FC2S" settle
write_agent "$P.fc2sscan"      cal:20 "$RUN_FC2S" scan --thr 0.05
write_agent "$P.enscollect"    cal:26 "$RUN_ENS" collect
# fc2s_shadow: measurement-only tail-recalibration collector (NEVER trades). Logs
# day-ahead forecast highs (:34) and fills realized highs (:48) so the forecast-error
# distribution accrues even while above-strike trading stays vetoed.
write_agent "$P.fc2sshadowcollect" cal:34 "$RUN_SHADOW" collect
write_agent "$P.fc2sshadowsettle"  cal:48 "$RUN_SHADOW" settle
write_agent "$P.enssettle"     cal:56 "$RUN_ENS" settle
# weather-NO fill-realism probe — snapshots the live order-book NO depth every 5 min so we
# can verify the cheap-NO fills (the whole edge) are actually available in size BEFORE any
# real money. Read-only (no orders). Review: python scripts/weather_no_fill_probe.py report
write_agent "$P.fillprobe"      300 "$RUN_FILLPROBE"
# dashboard — file render every 5 min (the reliable view: open the HTML file).
# Now shows honest "retired/stale" banners; still renders the fc2s + ensemble panels.
write_agent "$P.dashfile"       300 "$RUN_DASH" render
# keep the Mac awake — MANAGED, shared infra for ALL sleeves (fc2s, ensemble,
# bucket-arb, series-collect). Without it the Mac sleeps and every scheduled agent
# stalls. Auto-starts at login, respawns if it dies. To let the Mac sleep again:
# launchctl unload ~/Library/LaunchAgents/$P.caffeinate.plist
write_agent "$P.caffeinate" keepalive /usr/bin/caffeinate -dims

echo ""
echo "Done. Roster:"
launchctl list 2>/dev/null | grep weatherfade || echo "  (none listed — check Console for load errors)"
echo ""
echo "Dashboard (file): open $PROJECT_ROOT/data/weather_fade_dash.html"
echo "Scorecards: python scripts/fc_two_sided.py report   |   python scripts/ensemble_collect.py status"
echo "Tail recal: python scripts/fc2s_shadow.py report   (measured bias/σ + exceedance calibration)"
echo "NOTE: staggered agents fire at their minute-of-hour (fc2ssettle :12, fc2sscan :20,"
echo "      enscollect :26, enssettle :56) — first runs happen within the next hour, NOT at install."
echo "NOTE: caffeinate is now a MANAGED KeepAlive agent (auto-starts at login, respawns if"
echo "      it dies) — no manual 'caffeinate &' needed. To let the Mac sleep again:"
echo "      launchctl unload ~/Library/LaunchAgents/$P.caffeinate.plist"
