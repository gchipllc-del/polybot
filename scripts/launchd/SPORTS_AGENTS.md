# Sports sleeves harness (launchd agents)

Runs the two paper sports sleeves + their shared scorer hands-free as macOS
LaunchAgents. Runners live here; the live `.plist` files in
`~/Library/LaunchAgents/` are not in git (regenerate with the installer).

## Install
```bash
# verify tickers + mappings FIRST (per league you'll run):
python scripts/kalshi_survey.py --drill "Sports"      # find liquid series tickers
python scripts/sports_lock.py  probe nba KXNBAGAME    # ESPN ↔ Kalshi + 2nd-feed check
python scripts/devig_check.py  probe nba KXNBAGAME    # OddsAPI ↔ Kalshi (needs ODDS_API_KEY)

PAIRS="nba:KXNBAGAME nhl:KXNHLGAME" bash scripts/launchd/install_sports_agents.sh
caffeinate -dimsu &     # launchd won't fire while asleep — stay awake during games
```

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
