# Sports sleeves harness (launchd agents)

Runs the two paper sports sleeves + their shared scorer hands-free as macOS
LaunchAgents. Runners live here; the live `.plist` files in
`~/Library/LaunchAgents/` are not in git (regenerate with the installer).

## Install
```bash
# verify tickers + mappings FIRST (per league you'll run):
python scripts/kalshi_survey.py --drill "Sports"      # find the LIVE series tickers
python scripts/sports_lock.py  probe nba KXNBAGAMES   # ESPN ↔ Kalshi + 2nd-feed check
python scripts/devig_check.py  probe nba KXNBAGAMES   # OddsAPI ↔ Kalshi (needs ODDS_API_KEY)

# KXNBAGAMES = NBA per-game series (note the plural). NHL/NFL game-series tickers are
# season-dependent — confirm each in kalshi_survey, then add them as more pairs:
PAIRS="nba:KXNBAGAMES" bash scripts/launchd/install_sports_agents.sh
caffeinate -dimsu &     # launchd won't fire while asleep — stay awake during games
```

> **Season check:** NBA/NHL/NFL only have open game-markets in season. Off-season the
> survey shows `open=0 vol=0` and scans find nothing to do — that's expected, not a bug.
> Install when the league is actually playing.

## What runs (`com.jesse.polybot.sports.*`)
| Agent | Fires | Does |
|---|---|---|
| `<league>.lock`  | every 15 min | `sports_lock scan` — flag near-locked, mispriced moneylines (free ESPN). `--confirm` auto-added for **nba/nhl** (needs a 2nd independent feed to agree) |
| `<league>.devig` | every 2 h    | `devig_check scan` — Pinnacle-devig vs Kalshi YES. Infrequent on purpose: The Odds API free tier ≈ 16 req/day |
| `eval`           | hourly :40   | `sports_eval eval` — resolve settled games → per-day PSR/DSR + calibration across **both** logs |

Only **nba** and **nhl** are confirmable (an independent-origin 2nd feed exists —
NBA.com CDN / NHLE). Other leagues run the lock single-source (printed at install).

## Watch it
- Live log: `tail -f logs/sports.log`
- Verdict: `python scripts/sports_eval.py eval` (gross+net PSR/DSR/MinTRL + reliability table)
- Signals: `data/sports_lock.jsonl`, `data/devig_check.jsonl`; resolutions cached in `data/sports_eval_resolutions.json`

## Notes
- **Paper only** — places no orders. This is the collect-then-judge phase.
- `devig_check` needs `ODDS_API_KEY` in `.env` (free key from the-odds-api.com).
- The edge is real only once net **DSR > 95%** *and* calibration holds — collect a
  couple weeks of settled games before reading anything into the P&L.
- Uninstall: `bash scripts/launchd/install_sports_agents.sh --uninstall`
