#!/bin/bash
# OPT-IN installer for the SPORTS sleeves — now an ALL-SPORTS SWEEP (no per-league
# config, no hardcoded tickers). Each scan auto-discovers every live per-game series
# on Kalshi and scans them. Paper/data only — places NO orders. Idempotent; --uninstall.
#
# Three agents:
#   • sports_lock scan --confirm   every LOCK_INTERVAL  (default 15 min; free ESPN feed;
#       --confirm gates confirmable leagues (nba/nhl) on a 2nd feed, others single-source)
#   • devig_check scan             every DEVIG_INTERVAL (default 6 h) — INFREQUENT because
#       The Odds API free tier is ~500 req/month and a sweep costs ~1 credit PER LIVE
#       LEAGUE per run (≈ 4 runs/day × N live leagues). Raise DEVIG_INTERVAL if near cap.
#   • sports_eval eval             hourly at :40 — resolve settled games → PSR/DSR + calibration
#
#   bash scripts/launchd/install_sports_agents.sh
#   LOCK_INTERVAL=600 DEVIG_INTERVAL=14400 bash scripts/launchd/install_sports_agents.sh
#   bash scripts/launchd/install_sports_agents.sh --uninstall
#
# Pre-flight (read-only) before relying on it:
#   python scripts/sports_lock.py probe          # list every live per-game series + 2nd-feed status
#   python scripts/devig_check.py probe          # same, for the odds side (needs ODDS_API_KEY)
set -euo pipefail

LOCK_INTERVAL="${LOCK_INTERVAL:-900}"
DEVIG_INTERVAL="${DEVIG_INTERVAL:-21600}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
RUN="$PROJECT_ROOT/scripts/launchd/run_sports.sh"
AGENTS="$HOME/Library/LaunchAgents"
BASE="com.jesse.polybot.sports"
mkdir -p "$AGENTS"
chmod +x "$RUN" 2>/dev/null || true

if [ "${1:-}" = "--uninstall" ]; then
  for plist in "$AGENTS/$BASE."*.plist; do
    [ -e "$plist" ] || continue
    launchctl unload "$plist" 2>/dev/null || true
    rm -f "$plist"
    echo "  removed $(basename "$plist" .plist)"
  done
  echo "sports sleeve uninstalled."
  exit 0
fi

# write_agent LABEL CADENCE RUNNER [args...]   (cadence: seconds | cal:MM)
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

echo "Installing ALL-SPORTS sweep (paper/data only) ..."
write_agent "$BASE.lock"  "$LOCK_INTERVAL"  "$RUN" sports_lock scan --confirm   # every 15 min, all live series
write_agent "$BASE.devig" "$DEVIG_INTERVAL" "$RUN" devig_check scan             # every 6 h, all live series
write_agent "$BASE.eval"  cal:40            "$RUN" sports_eval eval             # hourly resolve + score
write_agent "$BASE.dashfile"  300       "$RUN" sports_dash render                # re-render HTML every 5 min
write_agent "$BASE.dashserve" keepalive "$RUN" sports_dash serve --port 5056 --host 127.0.0.1

echo ""
echo "Done. Sweeps EVERY live per-game series automatically — no ticker config."
echo "  lock  every ${LOCK_INTERVAL}s   devig every ${DEVIG_INTERVAL}s   eval hourly :40"
echo "Dashboard: http://127.0.0.1:5056   (http not https; file backup: data/sports_dash.html)"
echo "Tail:   tail -f logs/sports.log"
echo "Score:  python scripts/sports_eval.py eval"
echo "Reqs:   devig needs ODDS_API_KEY in .env (the-odds-api.com); sweep costs ~1 credit/live-league/run"
echo "        — free tier ~500/mo, so raise DEVIG_INTERVAL if many leagues are in season."
echo "Awake:  launchd won't fire while asleep — caffeinate -dimsu & during game windows."
echo "Uninstall: bash scripts/launchd/install_sports_agents.sh --uninstall"
