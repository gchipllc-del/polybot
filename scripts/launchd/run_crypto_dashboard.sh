#!/bin/bash
# Kalshi crypto (15-min BTC) dashboard runner — called by the
# com.jesse.polybot.kalshi_crypto_dashboard LaunchAgent. Serves the Flask
# dashboard on port 5053 (localhost only). Long-running; KeepAlive
# auto-restarts on any exit since the dashboard isn't trade-sensitive —
# losing visibility is worse than auto-restart loops.
set -euo pipefail

export PATH="/Users/jesse/anaconda3/bin:$PATH"
PROJECT_ROOT="/Users/jesse/Desktop/projects/polybot"
cd "$PROJECT_ROOT"

if [ -f "$PROJECT_ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$PROJECT_ROOT/.env"
  set +a
fi

mkdir -p "$PROJECT_ROOT/logs"
exec python main.py kalshi-dashboard --port=5053
