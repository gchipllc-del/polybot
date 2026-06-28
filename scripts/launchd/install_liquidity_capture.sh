#!/bin/bash
# Install a 20-minute liquidity poll so each venue is sampled while it's ACTIVE — turns
# the inconclusive off-hours DEAD readings into a real time-series (which series carry a
# resting book, and when). Reads only the public /markets endpoint; places nothing.
#
#   bash scripts/launchd/install_liquidity_capture.sh
#   bash scripts/launchd/install_liquidity_capture.sh --uninstall
# Review by morning:
#   python -c "import json;[print(json.loads(l)) for l in open('data/liquidity_log.jsonl') if json.loads(l)['fillable']>0]"
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
RUN="$SCRIPT_DIR/run_liquidity_capture.sh"
AGENTS="$HOME/Library/LaunchAgents"
LABEL="com.jesse.polybot.liquiditycapture"
PLIST="$AGENTS/$LABEL.plist"
mkdir -p "$AGENTS"
chmod +x "$RUN" 2>/dev/null || true

if [ "${1:-}" = "--uninstall" ]; then
  launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "liquidity capture uninstalled."
  exit 0
fi

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$RUN</string></array>
  <key>StartInterval</key><integer>1200</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$PROJECT_ROOT/logs/liquidity_capture.log</string>
  <key>StandardErrorPath</key><string>$PROJECT_ROOT/logs/liquidity_capture.log</string>
</dict></plist>
EOF
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST" 2>/dev/null || true
echo "  installed $LABEL (every 20 min, runs at load)"
echo "Logs to: $PROJECT_ROOT/data/liquidity_log.jsonl"
echo "By morning, which series ever showed a resting book + when:"
echo "  python -c \"import json;[print(json.loads(l)) for l in open('data/liquidity_log.jsonl') if json.loads(l)['fillable']>0]\""
echo "Uninstall: bash scripts/launchd/install_liquidity_capture.sh --uninstall"
