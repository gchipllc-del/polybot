#!/bin/bash
# OPT-IN installer for forward price→outcome collection sleeves + their dashboard.
# Separate from every other harness. Paper/data only — places NO orders.
#
# SERIES is a space-separated list. Each gets a collect (every 3h) + settle (hourly)
# agent; one shared dashboard (render every 5 min + live link on :5054) aggregates them.
#
#   bash scripts/launchd/install_series_collect_agents.sh                      # default: gas
#   SERIES="KXNFLGAME KXNBA" bash scripts/launchd/install_series_collect_agents.sh   # sports sleeve
#   bash scripts/launchd/install_series_collect_agents.sh --uninstall
#
# Idempotent. Find liquid sports tickers first with: python scripts/kalshi_survey.py --drill "Sports"
set -euo pipefail

SERIES="${SERIES:-KXAAAGASD}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
RUN="$PROJECT_ROOT/scripts/launchd/run_series_collect.sh"
RUN_DASH="$PROJECT_ROOT/scripts/launchd/run_series_collect_dash.sh"
AGENTS="$HOME/Library/LaunchAgents"
BASE="com.jesse.polybot.seriescollect"
mkdir -p "$AGENTS"
chmod +x "$RUN" "$RUN_DASH" 2>/dev/null || true

if [ "${1:-}" = "--uninstall" ]; then
  for plist in "$AGENTS/$BASE."*.plist; do
    [ -e "$plist" ] || continue
    launchctl unload "$plist" 2>/dev/null || true
    rm -f "$plist"
    echo "  removed $(basename "$plist" .plist)"
  done
  echo "series-collect sleeve uninstalled."
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

echo "Installing series-collect sleeve for: $SERIES (paper/data only) ..."
for s in $SERIES; do
  tag="$(echo "$s" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9')"
  write_agent "$BASE.$tag.collect" 10800 "$RUN" collect "$s"   # every 3h
  write_agent "$BASE.$tag.settle"  cal:20 "$RUN" settle "$s"    # hourly at :20
done
# dashboard scoped to THIS sleeve's series (so a sports sleeve's binary-game
# calibration isn't muddied by e.g. gas's price-ladder buckets) — file render + :5054
SERIES_CSV="$(echo "$SERIES" | tr ' ' ',')"
write_agent "$BASE.dash"  300       "$RUN_DASH" render --series "$SERIES_CSV"
write_agent "$BASE.serve" keepalive "$RUN_DASH" serve --series "$SERIES_CSV" --port 5054 --host 127.0.0.1
echo ""
echo "Done. Dashboard: http://127.0.0.1:5054   (own paper bankroll; http not https)"
echo "Status:   for s in $SERIES; do python scripts/series_collect.py status \$s; done"
echo "Eval:     python scripts/series_collect.py eval <SERIES>   (after ~2 weeks of days)"
echo "Uninstall: bash scripts/launchd/install_series_collect_agents.sh --uninstall"
