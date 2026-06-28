#!/bin/bash
# Install a STANDING hourly live-safety guard. preflight_live_check.py audits every
# data/*.jsonl for is_live=true (open real exposure) and whether the live switch is
# armed; the runner alarms (log banner + optional Telegram) on any non-zero result.
#
# IMPORTANT: install this in the clone the LIVE agents actually use (e.g.
# ~/Desktop/projects/polybot), not a backtest clone — it audits the checkout it runs in.
#
#   bash scripts/launchd/install_preflight_agent.sh
#   bash scripts/launchd/install_preflight_agent.sh --uninstall
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
RUN="$PROJECT_ROOT/scripts/launchd/run_preflight.sh"
AGENTS="$HOME/Library/LaunchAgents"
LABEL="com.jesse.polybot.safety.preflight"
PLIST="$AGENTS/$LABEL.plist"
mkdir -p "$AGENTS"
chmod +x "$RUN" 2>/dev/null || true

if [ "${1:-}" = "--uninstall" ]; then
  launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "preflight guard uninstalled."
  exit 0
fi

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$RUN</string>
  </array>
  <key>StartCalendarInterval</key><dict><key>Minute</key><integer>3</integer></dict>
  <key>RunAtLoad</key><true/>
</dict></plist>
EOF
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST" 2>/dev/null || true
echo "  installed $LABEL (hourly at :03, runs at load)"
echo "Audits: $PROJECT_ROOT/data + this clone's live switch."
echo "Alarms to: $PROJECT_ROOT/logs/preflight.log (+ Telegram if TELEGRAM_BOT_TOKEN/CHAT_ID set in .env)."
echo "Check now: python scripts/preflight_live_check.py; echo exit=\$?"
echo "Uninstall: bash scripts/launchd/install_preflight_agent.sh --uninstall"
