#!/bin/bash
# OPT-IN installer for the bucket-arb sleeve ONLY — kept separate from the
# weather harness on purpose, so the running weather sleeve is never touched by
# bucket-arb changes (and vice-versa). Run this ONLY when you actually want
# bucket-arb scheduled; until then the scanner/dashboard work fine by hand:
#
#   python scripts/bucket_arb.py              # scan now
#   python scripts/bucket_arb.py status|eval  # scoreboard / near-miss distro
#   python scripts/bucket_arb_dash.py serve   # http://127.0.0.1:5053
#
# To schedule it (4 agents, own bankroll, own dashboard):
#   bash scripts/launchd/install_bucket_arb_agents.sh
# To remove later:
#   bash scripts/launchd/install_bucket_arb_agents.sh --uninstall
#
# Idempotent. Self-locating from this script's path.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
RUN_BARB="$PROJECT_ROOT/scripts/launchd/run_bucket_arb.sh"
RUN_BARB_DASH="$PROJECT_ROOT/scripts/launchd/run_bucket_arb_dash.sh"
AGENTS="$HOME/Library/LaunchAgents"
P=com.jesse.polybot.bucketarb
mkdir -p "$AGENTS"
chmod +x "$RUN_BARB" "$RUN_BARB_DASH" 2>/dev/null || true

if [ "${1:-}" = "--uninstall" ]; then
  for label in "$P.scan" "$P.settle" "$P.dash" "$P.serve"; do
    plist="$AGENTS/$label.plist"
    launchctl unload "$plist" 2>/dev/null || true
    rm -f "$plist"
    echo "  removed $label"
  done
  echo "bucket-arb agents uninstalled (weather harness untouched)."
  exit 0
fi

# write_agent LABEL CADENCE RUNNER [args...]
#   CADENCE: number = StartInterval seconds; "cal:MM" = hourly at minute MM;
#            "keepalive" = long-running.
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

echo "Installing bucket-arb agents (separate bankroll; weather harness untouched) ..."
# scan ladders: log every ladder's near-miss margin (--collect) AND book genuine
# locks to bucket-arb's OWN paper bankroll (--book, NO-sweeps only). Fires at :15,
# its own minute so it never collides with the weather agents' Kalshi sweeps.
write_agent "$P.scan"   cal:15 "$RUN_BARB" --collect --book
write_agent "$P.settle" cal:42 "$RUN_BARB" settle
# dashboard: file render every 5 min + a live link on :5055 (KeepAlive).
# (:5055, not :5053 — kalshi_dashboard owns :5053; this avoids a bind clash.)
write_agent "$P.dash"   300 "$RUN_BARB_DASH" render
write_agent "$P.serve"  keepalive "$RUN_BARB_DASH" serve --port 5055 --host 127.0.0.1

echo ""
echo "Done. bucket-arb roster:"
launchctl list 2>/dev/null | grep bucketarb || echo "  (none listed — check Console for load errors)"
echo ""
echo "Dashboard:  http://127.0.0.1:5055   (http, not https; separate \$100 bankroll)"
echo "File backup: open $PROJECT_ROOT/data/bucket_arb_dash.html"
echo "Scoreboard: python scripts/bucket_arb.py status   |   python scripts/bucket_arb.py eval"
echo "Uninstall:  bash scripts/launchd/install_bucket_arb_agents.sh --uninstall"
