# Sleeves — Searching All of Kalshi for a Consistent Edge (Decision Record, 2026-11-16)

## Why more sleeves, and why carefully

Two sleeves have been tested one at a time and both closed: 15-min crypto (no
unconditional mispricing; the volatility model loses to the book) and daily-high weather
(the forecast measures a model grid cell, not the settling thermometer). Kalshi lists
hundreds of other series. Some may be priced worse, and the cheapest way to find out
is to test them all with one consistent method.

**The trap:** searching many sleeves manufactures fake edges. Test 40 sleeves × 80 cells
at 95% confidence and roughly 160 cells "win" by luck. So the whole search is treated as
**one family of hypotheses**, with false-discovery-rate control (Benjamini–Hochberg,
q = 0.10). What comes out is a **nomination**, never a verdict. A nomination earns
exactly one thing: a pre-registered rule tested on the forward paper ledger.

## The scanner — `scripts/sleeve_scan.py`

```powershell
py scripts\sleeve_scan.py probe          # what Kalshi returns from this machine
py scripts\sleeve_scan.py discover       # rank every series with settled events
py scripts\sleeve_scan.py backfill --top 20 --events 60     # resumable, hours
py scripts\sleeve_scan.py screen         # the one honest test
```

Guards, each paid for by an earlier mistake in this project:

1. **The event is the unit.** All strikes of one event resolve on one outcome: one bet,
   not thirteen.
2. **Time is a fraction of market life** (early / mid / late / final), so a 15-minute
   market and a 30-day market are compared on the same footing.
3. **Walk-forward.** A cell must be positive on the first half of dates and pass the
   corrected test on the second half.
4. **Quotes are not fills.** Every nominated cell is re-scored on *trade-confirmed*
   entries, where a trade printed at or through our price in that candle. An edge that
   lives only in untraded quotes is flagged "phantom risk". That is the pattern that
   produced the late-longshot paper "edge".
5. **"It never lost" is not proof.** A 94¢ favorite often shows zero losses in a small
   sample; its variance computes to zero and a naive test reports p = 0. Every cell is
   judged against the variance a *fair* price would produce, and never less. (The first
   version lacked this. The measured detection table caught it: a 3¢ edge was "found"
   at 80 events and missed at 640, which is impossible for an honest test.)

## What the search can and cannot see (measured on synthetic sleeves)

The planted edge is a favorite-longshot bias: longshots quoted *k* cents rich. One
biased sleeve is hidden among 9 efficient ones (a ~440-cell family), over two seeds:

| quote bias | 80 events | 160 | 320 | 640 | 1,280 |
|---|---|---|---|---|---|
| 3¢ | 0/2 | 0/2 | 0/2 | 0/2 | 0/2 |
| 5¢ | 0/2 | 0/2 | 0/2 | 0/2 | 0/2 |
| 8¢ | 0/2 | 0/2 | 0/2 | 0/2 | **2/2** |
| 12¢ | 0/2 | 0/2 | 0/2 | **2/2** | **2/2** |

Pure noise (30 efficient sleeves, 1,320 cells): **0 nominations.**

**Read this as a budget, not a disappointment.** Searching everything costs statistical
power: the more cells tested, the stronger any single winner must be. A few-cent edge,
which is what real markets tend to leave, cannot be found by brute-force grid search on
Kalshi's history. It can be found by a **small, theory-driven family** tested where the
data is deep. That is the design of the forward paper sleeves.

## Paper-trading the sleeves

Forward paper is the only scoreboard. Each sleeve gets a small family of
**pre-registered** rules (with a `named_at` stamp), never a grid:

- **The favorite-longshot probe** (two rules per sleeve). This is the most consistently
  documented pricing bias in betting markets: longshots overpriced, favorites
  underpriced. The probe buys the favorite at 80–95¢, and its mirror buys the longshot
  at 5–20¢, both in the mid-life band. Two rules per sleeve rather than ~80 cells means
  far more power for the same data.
- **Scanner nominations.** Clean ones only; phantom-risk nominations are not traded.

Every forward rule goes through the same fill-depth gate as the crypto paper trader, one
bet per event, and the news regime stamp. The forward family is corrected with BH
across *all* paper rules at verdict time, so adding sleeves never lowers the bar.

## The other route: conditional sleeves

An unconditional screen asks "is this price wrong on average?". The edges that survive in
efficient venues are usually *conditional*: a stronger independent price disagrees with
Kalshi's. The candidates, each a self-contained follow-up:

| Kalshi sleeve | independent price | data |
|---|---|---|
| index ranges (S&P, Nasdaq) | options-implied distribution | public option chains |
| crypto dailies / hourlies | Deribit options-implied distribution | Deribit public API |
| sports | de-vigged sharp sportsbook lines | odds API (key needed) |
| CPI / economics | official nowcasts (e.g. Cleveland Fed) | public |
| weather | the settling station itself (METAR / NWS obs) | public |

These have the structure that worked for the original weather idea: forecast vs price.
The difference is an estimator good enough to beat the book. The weather run showed why
that bar matters: our estimator measured the wrong thermometer.
