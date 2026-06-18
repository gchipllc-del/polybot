#!/bin/bash
# OPT-IN installer for forward price→outcome collection on candidate series.
# Separate from every other harness. Paper/data only — places NO orders.
#
# Default target: KXAAAGASD (daily gas) — the one survey candidate with both fast
# cadence and listed strikes. Collects every 3h (to catch any quote window, since
# the book is currently unquoted) and settles daily after the 03:59Z close.
#
#   bash scripts/launchd/install_series_collect_agents.sh            # install (gas)
#   bash scripts/launchd/install_series_collect_agents.sh --uninstall
#
# To collect a different/extra series, edit SERIES below. Idempotent.
set -euo pipefail

SERIES="${SERIES:-KXAAAGASD}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
RUN="$PROJECT_ROOT/scripts/launchd/run_series_collect.sh"
AGENTS="$HOME/Library/LaunchAgents"
P="com.jesse.polybot.seriescollect.$(echo "$SERIES" | tr '[:upper:]' '[:lower:]')"
mkdir -p "$AGENTS"
chmod +x "$RUN" 2>/dev/null || true

if [ "${1:-}" = "--uninstall" ]; then
  for suffix in collect settle; do
    plist="$AGENTS/$P.$suffix.plist"
    launchctl unload "$plist" 2>/dev/null || true
    rm -f "$plist"
    echo "  removed $P.$suffix"
  done
  exit 0
fi

write_agent() {
  local label="$1"; local cadence="$2"; shift 2
  local plist="$AGENTS/$label.plist"
  local argxml=""
  for a in "$@"; do argxml+="    <string>$a</string>
"; done
  local cadence_xml
  if [[ "$cadence" == cal:* ]]; then
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
    <string>$RUN</string>
$argxml  </array>
$cadence_xml
</dict></plist>
EOF
  launchctl unload "$plist" 2>/dev/null || true
  launchctl load "$plist" 2>/dev/null || true
  echo "  installed $label"
}

echo "Installing series-collect agents for $SERIES (paper/data only) ..."
write_agent "$P.collect" 10800 collect "$SERIES"   # every 3h: catch any quote window
write_agent "$P.settle"  cal:20 settle "$SERIES"    # hourly at :20 — resolves after close
echo ""
echo "Done. Check:  python scripts/series_collect.py status $SERIES"
echo "Eval later:   python scripts/series_collect.py eval $SERIES   (after ~2 weeks of days)"
echo "Uninstall:    bash scripts/launchd/install_series_collect_agents.sh --uninstall"
