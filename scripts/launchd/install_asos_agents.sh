#!/bin/bash
# OPT-IN installer for the ASOS BUCKET-LOCK sleeve — the observation edge: read the
# REALIZED daily high off the settlement ASOS station (Iowa Environmental Mesonet) and
# flag now-near-certain Kalshi buckets the overnight book still misprices. Paper/data
# only — places NO orders. Idempotent; --uninstall.
#
# Agents:
#   • asos_tracker scan     every SCAN_INTERVAL (default 30 min) — frequent so it catches
#       fresh evening locks across US time zones before the 11:59pm ET cutoff. is_locked
#       self-gates on local hour ≥19, so off-window scans are cheap no-ops.
#   • asos_tracker settle   hourly at :15 — resolve settled locks → paper P&L (idempotent)
#   • asos_dash render       every 5 min — re-render the HTML scorecard
#   • asos_dash serve        KeepAlive — live dashboard at http://127.0.0.1:5058
#
#   bash scripts/launchd/install_asos_agents.sh
#   SCAN_INTERVAL=900 bash scripts/launchd/install_asos_agents.sh
#   bash scripts/launchd/install_asos_agents.sh --uninstall
#
# Pre-flight (read-only) FIRST — verify a station is the one Kalshi names:
#   python scripts/asos_tracker.py probe KXHIGHNY
#   python scripts/asos_tracker.py selftest
set -euo pipefail

SCAN_INTERVAL="${SCAN_INTERVAL:-1800}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
RUN="$PROJECT_ROOT/scripts/launchd/run_asos.sh"
AGENTS="$HOME/Library/LaunchAgents"
BASE="com.jesse.polybot.asos"
mkdir -p "$AGENTS"
chmod +x "$RUN" 2>/dev/null || true

if [ "${1:-}" = "--uninstall" ]; then
  for plist in "$AGENTS/$BASE."*.plist; do
    [ -e "$plist" ] || continue
    launchctl unload "$plist" 2>/dev/null || true
    rm -f "$plist"
    echo "  removed $(basename "$plist" .plist)"
  done
  echo "asos bucket-lock sleeve uninstalled."
  exit 0
fi

# write_agent LABEL CADENCE RUNNER [args...]   (cadence: seconds | cal:MM | keepalive)
write_agent() {
  local label="$1"; local cadence="$2"; local runner="$3"; shift 3
  local plist="$AGENTS/$label.plist"; local argxml=""
  for a in "$@"; do argxml+="    <string>$a</string>
"; done
  local cadence_xml
  if [ "$cadence" = "keepalive" ]; then
    cadence_xml="  <key>KeepAlive</key><true/>
  <key>RunAtLoad</key><true/>"
  elif [[ "$cadence" == cal:* ]]; then
    cadence_xml="  <key>StartCalendarInterval</key>
  <dict><key>Minute</key><integer>${cadence#cal:}</integer></dict>"
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

echo "Installing ASOS bucket-lock sleeve (paper/data only) ..."
write_agent "$BASE.scan"      "$SCAN_INTERVAL" "$RUN" asos_tracker scan            # every 30 min
write_agent "$BASE.settle"    cal:15           "$RUN" asos_tracker settle          # hourly resolve
write_agent "$BASE.dashfile"  300              "$RUN" asos_dash render             # re-render every 5 min
write_agent "$BASE.dashserve" keepalive        "$RUN" asos_dash serve --port 5058 --host 127.0.0.1

echo ""
echo "Done. scan every ${SCAN_INTERVAL}s · settle hourly :15."
echo "Dashboard: http://127.0.0.1:5058   (http not https; file backup: data/asos_dash.html)"
echo "Tail:   tail -f logs/asos.log"
echo "Score:  python scripts/asos_tracker.py report"
echo "VERIFY FIRST: python scripts/asos_tracker.py probe KXHIGHNY  (station map verified 2026-06-21,"
echo "        but re-probe if Kalshi changes contract terms). Awake: caffeinate -dimsu & overnight."
echo "Uninstall: bash scripts/launchd/install_asos_agents.sh --uninstall"
