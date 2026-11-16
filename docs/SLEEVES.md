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
across *all* paper rules at verdict time, so adding sleeves never lowers the bar. "All"
means every sleeve rule, RETIRED ones included, plus the crypto rules on their
fill-checked trades, and `sleeve_paper.py report` and the command center correct over
that same family. Retirement is decided after seeing results, so a retired rule is no
longer shown but its record stays in the correction: retiring rules can never raise
another rule's verdict.

## Testing an outside method (e.g. a friend's 97-99c favorites)

```
py scripts/sleeve_paper.py register-rule --name F9799 --series KXHIGHNY,KXHIGHCHI,... \
    --side favorite --lo 0.97 --hi 0.995 --band any [--contracts 10] \
    [--min-hours 6 --max-hours 12] --thesis "..."
```

Three choices in that command decide whether the test is fair.

**One hypothesis = one rule = one test.** A method run on seven cities is one rule over
seven series, not seven rules. Seven per-city tests would each wait ~100 days for 100
events (one bet per city-day) and widen the false-discovery family seven-fold; the pooled
rule reaches 100 events in about two weeks. `report` still prints a per-series line, as
description, never as a test.

**Pooled series are assumed independent.** Each series' event counts as its own bet, and
the exact p-value treats those bets as independent. That holds for series that settle on
different things (cities far apart are close to it), and fails for series on one
underlying: pool two S&P 500 series, or a BTC range series with a BTC above/below series,
and one bust day counts as two losses while calm days count twice - p comes out too
small. Never pool those; `register-rule` and `report` print this assumption whenever a
rule spans more than one series.

**Order size is part of the rule (default 10 contracts).** Kalshi rounds the fee per
ORDER up to the cent. A 1-lot at 99c pays a 1c fee on a 1c win: it cannot profit even
if it never loses. At 10 contracts the fee is about 0.1-0.2c per contract. Rules
registered before sizes were stored keep their 1-lot definition. A sized order is booked
entirely at the ask, so the fill gate needs all of its contracts offered AT the ask; size
one tick worse does not count (the gate's one-tick tolerance for quote/book skew applies
only to the legacy 1-lots).

**Exact odds, not a normal approximation.** Under the house rule (one bet per event, held
to settlement) every outcome is a win or a loss that differ by exactly $1 per contract,
so a rule's record is decided by how many trades lost. With no edge, a trade at ask A and
fee f loses with probability 1-(A+f); the p-value is the Poisson-binomial chance of a
record this good anyway, computed exactly (a dynamic program over the trades) for every
favorite or longshot rule up to thousands of events. Only past that work budget -
mid-price rules with thousands of events, whose loss count is nearly symmetric - is a
normal limit used, corrected for skewness and within 1% of exact at p >= 0.002. (A plain
normal is 15-45% off at p ~ 0.001 for favorites and longshots.) For near-certain
favorites this matters a great deal:

| forward record, 98c, 10-lot | chance with NO edge | verdict |
|---|---|---|
| 40 wins, 0 losses | 0.48 | positive, not significant |
| 100 wins, 0 losses | 0.16 | positive, not significant |
| 27 wins, 3 losses | 0.016 of doing this badly | NEGATIVE (significant) |

A 100% win rate on 97-99c favorites is what a no-edge method shows most of the time for
its first few dozen trades. The question is never the win rate - it is whether the win
rate beats ~98.2% (the break-even at 98c with fees) by enough, over enough events.

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
