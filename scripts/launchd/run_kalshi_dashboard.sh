#!/bin/bash
# Polybot Kalshi 15-min trader dashboard runner — called by
# com.jesse.polybot.kalshi_dashboard LaunchAgent. Serves at
# localhost:5053. Long-running; KeepAlive auto-restarts on any exit
# since the dashboard isn't trade-sensitive (losing visibility is
# worse than auto-restart loops).
set -euo pipefail

export PATH="/Users/jesse/anaconda3/bin:$PATH"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT"

if [ -f "$PROJECT_ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$PROJECT_ROOT/.env"
  set +a
fi

mkdir -p "$PROJECT_ROOT/logs"
exec python main.py kalshi-dashboard
