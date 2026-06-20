#!/bin/bash
# OPT-IN installer for the SPORTS sleeves (sports_lock + devig_check) and their
# shared scorer (sports_eval). Paper/data only — places NO orders. Separate from
# every other harness, idempotent, with --uninstall.
#
# PAIRS is space-separated league:kalshiseries items. Each pair gets:
#   • a sports_lock scan every 15 min (free ESPN feed; --confirm added automatically
#     for confirmable leagues so a lock needs a 2nd independent feed to agree)
#   • a devig_check scan every 2 h (Pinnacle-devig vs Kalshi; INFREQUENT on purpose —
#     The Odds API free tier is ~500 req/month ≈ 16/day, one request per scan-league)
# Plus ONE shared sports_eval agent hourly at :40 (resolve settled games → PSR/DSR +
# calibration across BOTH logs).
#
#   bash scripts/launchd/install_sports_agents.sh                          # default: nba (KXNBAGAMES)
#   PAIRS="nba:KXNBAGAMES nhl:<series>" bash scripts/launchd/install_sports_agents.sh
#   bash scripts/launchd/install_sports_agents.sh --uninstall
#
# Series tickers are SEASON-DEPENDENT and must be confirmed — KXNBAGAMES is the NBA
# per-game series (note the plural 'S'); the NHL/NFL game series only list when in
# season. FIRST verify each pair's ticker + team mapping (and 2nd-feed availability):
#   python scripts/kalshi_survey.py --drill "Sports"      # find the live series tickers
#   python scripts/sports_lock.py probe nba KXNBAGAMES    # confirm ESPN↔Kalshi + 2nd feed
#   python scripts/devig_check.py probe nba KXNBAGAMES    # confirm OddsAPI↔Kalshi (needs ODDS_API_KEY)
set -euo pipefail

PAIRS="${PAIRS:-nba:KXNBAGAMES}"
CONFIRMABLE="nba nhl"            # leagues with an independent 2nd feed (see sports_lock SECONDARY)
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
  if [[ "$cadence" == cal:* ]]; then
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

echo "Installing sports sleeve for: $PAIRS (paper/data only) ..."
for pair in $PAIRS; do
  league="${pair%%:*}"; series="${pair##*:}"
  tag="$(echo "$league" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9')"
  # --confirm only where a 2nd independent feed exists (else it would suppress every lock)
  if [[ " $CONFIRMABLE " == *" $league "* ]]; then
    write_agent "$BASE.$tag.lock" 900 "$RUN" sports_lock scan "$league" "$series" --confirm
  else
    write_agent "$BASE.$tag.lock" 900 "$RUN" sports_lock scan "$league" "$series"
    echo "    note: $league has no 2nd feed — lock runs UNconfirmed (single-source)."
  fi
  write_agent "$BASE.$tag.devig" 7200 "$RUN" devig_check scan "$league" "$series"
done
write_agent "$BASE.eval" cal:40 "$RUN" sports_eval eval     # hourly resolve + score both logs

echo ""
echo "Done. Paper only — no orders. Tail: tail -f logs/sports.log"
echo "Score:  python scripts/sports_eval.py eval         (PSR/DSR + calibration, after settled games)"
echo "Reqs:   devig_check needs ODDS_API_KEY in .env (free key: the-odds-api.com; mind the ~500/mo quota)"
echo "Awake:  launchd timers don't fire while asleep — keep the Mac awake during game windows"
echo "        (caffeinate -dimsu &) or you'll miss live locks."
echo "Uninstall: bash scripts/launchd/install_sports_agents.sh --uninstall"
