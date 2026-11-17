# Repo sweep - decision record (2026-11-16)

**How it was done.** 21 repositories (GitHub trending and relevant, plus PyPI `pykalshi`)
were each read in full at a pinned commit - nothing installed or executed - with the
license verified from the actual LICENSE file, a safety scan (eval/exec, obfuscation, key
handling, network hosts, install hooks, CI) and a usefulness score (0-4) against five
needs: (1) an event calendar and news for the regime agent, (2) execution realism (order
books, fills, maker/taker), (3) historical data, (4) structural or cross-venue edges,
(5) settlement modeling. Every "adopt" verdict then faced an adversarial verifier.
**Result: none unsafe; three adoptions upheld.**

## Adopted

| repo | license | taken | where |
|---|---|---|---|
| jackson-cmd/risk-aware-trading-agents @3cf41fb | MIT | official FOMC decision dates (through 2027-12-08) and BLS CPI / jobs / PPI release dates (through 2026-12) | `config/news_calendar.yaml`, attribution in `NOTICE` |

Not taken from it: rule-estimated release dates (the reviewer's re-implementation of its
jobs-report rule disagreed with the official date in 18 of 203 months), the hand-set
event weights, and all code.

## Proposed next - each is real work, so each waits for a decision

1. **Synpath-ai/synpath** (MIT, upheld, usefulness 4). `engine/paper.py` `PaperVenue`:
   takers walk the book level by level, makers queue behind resting size, fills credited
   from trade prints. This is the missing piece for testing maker-style methods (resting
   limit orders, as the friend's fills suggest) honestly. Copy with attribution; do not
   `pip install` (it pulls in a live-trading stack).
2. **pykalshi** (MIT, upheld). `feed.py`: wire-accurate websocket message models,
   reconnect with backoff and auto-resubscribe. Pairs with #1 to keep live books.
3. **Jon-Becker/prediction-market-analysis** (MIT code; the data dump carries Kalshi's and
   Polymarket's data terms - internal research only). A 36 GiB Parquet archive of Kalshi
   trade prints with taker side, joined to final results: years of history for
   calibration and fill modeling. Required integrity gates before use: `no_price == 100 -
   yes_price`, unique `trade_id`, trade volume vs market volume (truncated tapes), and
   never treat the markets table's end-state `yes_bid/yes_ask` as decision-time quotes.
   Rewrite its price parser with Decimal (`int(float(v)*100)` turns 0.29 into 28).

## Ideas worth keeping (no code taken)

- Resample whole events in bootstrap intervals (OctagonAI/kalshi-trading-bot-cli) - the
  same independence unit our scorers use.
- Point-in-time data: store observed / published / collected times (marc-shade/
  world-intel-mcp); a headline belongs to the first decision time after it; a
  perturbation look-ahead test (randomize everything after a cut; decisions before it
  must not change) (risk-aware-trading-agents).
- Holm-Bonferroni and deflated-Sharpe helpers (bradca0/orderbook-sim, MIT).
- Kalshi's `/historical/*` tier for archived markets (oracle3, pmxt's vendored spec) -
  backfill completeness for older events.
- Fee-aware no-arbitrage checks across an event's related markets (oracle3).
- Closed-form pricing for markets that settle on an average (Polytutor; no license, so
  re-derive from the math) - relevant to the 15-minute BRTI average.
- An "outcome control" taxonomy to flag markets where insiders decide or learn early
  (Aji-Q/levelfield) - a screen, not a signal.

## Every verdict

| repo | verdict | use | license |
|---|---|---|---|
| Synpath-ai/synpath | adopt-code (upheld) | 4 | MIT |
| jackson-cmd/risk-aware-trading-agents | adopt-code (upheld) - **adopted** | 4 | MIT |
| pypi:pykalshi (ArshKA/pykalshi) | adopt-code (upheld) | 3 | MIT |
| Jon-Becker/prediction-market-analysis | adopt-data | 3 | MIT (data: venue terms) |
| ryanfrigo/kalshi-ai-trading-bot | adopt-code (not upheld) | 3 | MIT |
| YichengYang-Ethan/oracle3-prediction-market-agent | adopt-code (not upheld) | 3 | Apache-2.0 |
| agent-next/polymarket-paper-trader | adopt-code (not upheld) | 3 | MIT |
| tradermonty/claude-trading-skills | adopt-code (not upheld) | 3 | MIT |
| bradca0/orderbook-sim | adopt-code (not upheld) | 3 | MIT |
| kachence/polymm | adopt-idea | 3 | MIT |
| fizahkhalid/forex_factory_calendar_news_scraper | adopt-idea | 3 | MIT |
| aarora4/Awesome-Prediction-Market-Tools | adopt-idea | 3 | none (list only) |
| LLMQuant/awesome-trading-agents | adopt-idea | 3 | CC0-1.0 |
| pmxt-dev/pmxt | adopt-idea | 2 | MIT |
| alsk1992/CloddsBot | adopt-idea | 2 | MIT |
| Polytutor-AI-Labs/polymarket-black-scholes-hft-bot | adopt-idea | 2 | none |
| marc-shade/world-intel-mcp | adopt-idea | 2 | MIT |
| OctagonAI/kalshi-trading-bot-cli | adopt-idea | 1 | MIT |
| Jakeschincariol/day-trader-skill | adopt-idea | 1 | MIT |
| Aji-Q/levelfield | adopt-idea | 1 | MIT |
| suislanchez/polymarket-kalshi-weather-bot | skip | 1 | none |

A repo with no license can lend ideas only, never code.
