# Sports sleeves harness (launchd agents)

Runs the two paper sports sleeves + their shared scorer hands-free as macOS
LaunchAgents, as an **all-sports sweep**: every scan auto-discovers all live
per-game series on Kalshi — no per-league config, no hardcoded tickers. Runners
live here; the live `.plist` files in `~/Library/LaunchAgents/` are not in git.

## Install
```bash
# read-only pre-flight (lists every live per-game series + 2nd-feed status):
python scripts/sports_lock.py probe
python scripts/devig_check.py probe          # needs ODDS_API_KEY

bash scripts/launchd/install_sports_agents.sh
caffeinate -dimsu &     # launchd won't fire while asleep — stay awake during games
```
Tune cadences: `LOCK_INTERVAL=600 DEVIG_INTERVAL=14400 bash .../install_sports_agents.sh`

## What runs (`com.jesse.polybot.sports.*`)
| Agent | Fires | Does |
|---|---|---|
| `lock`  | every 15 min (`LOCK_INTERVAL`) | `sports_lock scan --confirm` — sweeps **all** live series; `--confirm` gates nba/nhl on an independent 2nd feed, others run single-source |
| `devig` | every 6 h (`DEVIG_INTERVAL`) | `devig_check scan` — sweeps **all** live series; Pinnacle-devig vs Kalshi YES |
| `eval`  | hourly :40 | `sports_eval eval` — resolve settled games → per-day PSR/DSR + calibration across **both** logs |

Leagues covered: discovery maps each Kalshi series to a league via `infer_league`.
`sports_lock` covers nba/wnba/ncaab/nfl/ncaaf/nhl (clock model); `devig_check`
additionally covers **mlb** (no clock needed). Series for sports neither models are
skipped automatically.

## Odds API quota (important)
`devig_check scan` (all) costs **~1 Odds API credit per live league per run**. The
free tier is ~500/month. At the 6h default that's ≈ 4 runs/day × (live leagues); raise
`DEVIG_INTERVAL` when many leagues are in season, or run `devig_check scan <league>
<series>` for just the ones you care about.

## Watch it
- Live log: `tail -f logs/sports.log`
- Verdict: `python scripts/sports_eval.py eval` (gross+net PSR/DSR/MinTRL + reliability table)
- Signals: `data/sports_lock.jsonl`, `data/devig_check.jsonl`; resolutions cached in `data/sports_eval_resolutions.json`

## Notes
- **Paper only** — places no orders. Collect-then-judge phase.
- **Season check:** off-season leagues simply have no live games — the sweep finds
  nothing for them, which is expected, not a bug.
- Edge is real only once net **DSR > 95%** *and* calibration holds — collect a couple
  weeks of settled games first.
- Uninstall: `bash scripts/launchd/install_sports_agents.sh --uninstall`
