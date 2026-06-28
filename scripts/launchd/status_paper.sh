#!/bin/bash
# status_paper.sh — one-look health check for the PAPER sleeves (no live trading).
# Shows each paper agent's launchd status and how fresh its log is, and flags stale
# ones. Run it in the clone the paper agents use (e.g. ~/polybot-backtest).
#
#   bash scripts/launchd/status_paper.sh [stale_hours]   # default stale threshold: 3h
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
STALE_H="${1:-3}"
now=$(date +%s)

echo "=== launchd: paper sleeves loaded (PID / last-exit / label) ==="
# Paper-only prefixes; the live agents (kalshi_daily*, kalshi_hermes, weather*, trade,
# harvester, monitor) are intentionally absent after disarm.
launchctl list 2>/dev/null \
  | grep -E 'com\.jesse\.polybot\.(weatherfade|asos|sports|seriescollect|bucketarb)' \
  | sort -k3 || echo "  (none loaded)"

echo
echo "=== guard against re-arm: any LIVE agent that should NOT be here ==="
live=$(launchctl list 2>/dev/null \
  | grep -E 'com\.jesse\.polybot\.(kalshi_daily|kalshi_hermes|weather($|\.)|weather_hermes|weather_daily|trade|harvester|monitor)' || true)
if [ -n "$live" ]; then echo "⚠ LIVE AGENT PRESENT:"; echo "$live"; else echo "  none ✓ (disarmed)"; fi

echo
echo "=== paper log freshness (threshold: ${STALE_H}h) ==="
for log in fc2s ensemble fc2s_shadow asos sports series_collect; do
  f="$PROJECT_ROOT/logs/$log.log"
  if [ ! -f "$f" ]; then printf "  %-16s  MISSING\n" "$log"; continue; fi
  m=$(stat -f %m "$f" 2>/dev/null || stat -c %Y "$f" 2>/dev/null)
  age_h=$(( (now - m) / 3600 ))
  flag="ok"; [ "$age_h" -ge "$STALE_H" ] && flag="⚠ STALE"
  last=$(tail -n 1 "$f" 2>/dev/null | cut -c1-80)
  printf "  %-16s  %3dh ago  %-8s | %s\n" "$log" "$age_h" "$flag" "$last"
done
# Retired sleeves: listed for the record but NOT freshness-checked — their agents were
# removed during the disarm, so a stale log is expected, not an alert.
printf "  %-16s  %s\n" "weather_fade" "retired (scan/settle agents removed; not monitored)"

echo
echo "(For trade counts/P&L use each sleeve's own report, e.g. python scripts/weather_fade.py report)"
