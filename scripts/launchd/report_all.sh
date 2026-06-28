#!/bin/bash
# report_all.sh — print every paper sleeve's scorecard in one shot (no dashboard needed).
# Reads each sleeve's ledger directly. Run in the clone the paper agents use (polybot-backtest).
#   bash scripts/launchd/report_all.sh
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT"
export PATH="/Users/jesse/anaconda3/bin:$PATH"

# label | script | args
run() {
  local label="$1"; shift
  echo
  echo "════════════════════════════════════════════════════════════"
  echo "  $label"
  echo "════════════════════════════════════════════════════════════"
  if [ ! -f "scripts/$1" ]; then echo "  (scripts/$1 not found)"; return; fi
  python "scripts/$@" 2>&1 || echo "  (report errored — see above)"
}

run "WEATHER FADE (fc one-sided)"      weather_fade.py report
run "FC TWO-SIDED (fc2s)"              fc_two_sided.py report
run "FC2S SHADOW"                      fc2s_shadow.py report
run "ENSEMBLE"                         ensemble_collect.py status
run "BUCKET ARB (paper bankroll)"      bucket_arb.py status
run "ASOS TRACKER"                     asos_tracker.py report
run "SPORTS (lock + devig)"            sports_eval.py eval

# series_collect is per-series (one ledger each) and needs the ticker as an arg —
# discover every series_collect_<TICKER>.jsonl and report each.
echo
echo "════════════════════════════════════════════════════════════"
echo "  SERIES COLLECT (sports calib)"
echo "════════════════════════════════════════════════════════════"
shopt -s nullglob
series_found=0
for led in data/series_collect_*.jsonl; do
  series="${led#data/series_collect_}"; series="${series%.jsonl}"
  echo "  --- $series ---"
  python scripts/series_collect.py status "$series" 2>&1 || echo "  (errored)"
  series_found=1
done
[ "$series_found" -eq 0 ] && echo "  (no series_collect_*.jsonl ledgers yet)"

echo
echo "Tip: dashboards (if up) — weather 5052, bucket 5053, series 5054."
