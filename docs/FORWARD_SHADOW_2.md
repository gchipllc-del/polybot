# SHADOW REPLAY from Kalshi's public price history - NOT the official forward ledger on the owner's PC

The official forward record is `data/paper_sleeves.jsonl` on the Windows PC that runs the
paper trader. It could not be read while the owner was away, so this is an independent
reconstruction of the two pre-registered forward rules from Kalshi's public candles. It is
a cross-check, not the record. Where the two disagree, the ledger on the PC wins.

This is the second shadow replay. `docs/FORWARD_SHADOW.md` (replay time 2026-11-22 04:29Z)
is the first and is left untouched; this one was fetched from scratch and covers events
settled up to the replay times below.

- Source: public endpoints only (`/markets`, `/series/.../candlesticks`). No credentials,
  no orders. Nothing was registered, retired or edited.
- Method: `scripts/sleeve_scan.py backfill` for the candles, then `scan_many` ->
  `trades_from` -> `first_per_event` -> `summarize` from `scripts/strategy_check.py`.
  Band 0.97-0.99 inclusive, 10 contracts, fee `ceil(0.07*10*p*(1-p))` per order to the
  cent, held to settlement. The entry is the first candle in which a side's ask sits in
  the band on a formed book; one bet per event. p-values are exact
  (`sleeve_paper.forward_stats`). Kept only events that have settled and whose entry time
  is after the rule's registration.
- **Neither rule can be judged from this.** EDGE needs 100+ post-registration events and
  survival of the false-discovery correction, on the official ledger.

## F9799 (registered 2026-11-16T04:55Z)

Daily high temperature (KXHIGHNY, KXHIGHCHI, KXHIGHMIA, KXHIGHAUS, KXHIGHDEN, KXHIGHLAX,
KXHIGHPHIL). Favorite at 97-99c, any time, one bet per city-day.

- Replay time: **2026-11-24 21:10Z** (data fetched 2026-11-24 20:46Z - 21:10Z).
- Result: **49 settled city-days entered, 49 won, 0 lost, net +$6.84** at 10 contracts.
  With no edge at all, a clean 49-0 at these prices happens about half the time
  (exact p = 0.50), so this says nothing yet.

| | events | wins | losses | win rate | break-even | net $ (10-lot) | won $ | lost $ | edge c/contract | exact p (makes money) | p (loses money) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| All entries (quote-based) | 49 | 49 | 0 | 100.00% | 98.60% | +6.84 | +6.84 | 0.00 | +1.40 | 0.502 | 1.000 |
| Trade-confirmed only | 32 | 32 | 0 | 100.00% | 98.57% | +4.59 | +4.59 | 0.00 | +1.43 | 0.629 | 1.000 |

Fees paid: $0.76. Entry mix: 5 at 97c, 17 at 98c, 27 at 99c. Sides: 0 YES, 49 NO.

Per city (all entries, then the trade-confirmed subset):

| series | events | wins | losses | win rate | break-even | net $ (10-lot) | won $ | lost $ | edge c/contract | exact p (makes money) | p (loses money) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| KXHIGHNY | 7 | 7 | 0 | 100.00% | 98.71% | +0.90 | +0.90 | 0.00 | +1.29 | 0.913 | 1.000 |
| KXHIGHNY (trade-confirmed) | 6 | 6 | 0 | 100.00% | 98.65% | +0.81 | +0.81 | 0.00 | +1.35 | 0.922 | 1.000 |
| KXHIGHCHI | 7 | 7 | 0 | 100.00% | 98.46% | +1.08 | +1.08 | 0.00 | +1.54 | 0.897 | 1.000 |
| KXHIGHCHI (trade-confirmed) | 4 | 4 | 0 | 100.00% | 98.42% | +0.63 | +0.63 | 0.00 | +1.57 | 0.938 | 1.000 |
| KXHIGHMIA | 7 | 7 | 0 | 100.00% | 98.71% | +0.90 | +0.90 | 0.00 | +1.29 | 0.913 | 1.000 |
| KXHIGHMIA (trade-confirmed) | 2 | 2 | 0 | 100.00% | 98.65% | +0.27 | +0.27 | 0.00 | +1.35 | 0.973 | 1.000 |
| KXHIGHAUS | 7 | 7 | 0 | 100.00% | 98.33% | +1.17 | +1.17 | 0.00 | +1.67 | 0.889 | 1.000 |
| KXHIGHAUS (trade-confirmed) | 5 | 5 | 0 | 100.00% | 98.20% | +0.90 | +0.90 | 0.00 | +1.80 | 0.913 | 1.000 |
| KXHIGHDEN | 7 | 7 | 0 | 100.00% | 98.71% | +0.90 | +0.90 | 0.00 | +1.29 | 0.913 | 1.000 |
| KXHIGHDEN (trade-confirmed) | 5 | 5 | 0 | 100.00% | 98.56% | +0.72 | +0.72 | 0.00 | +1.44 | 0.930 | 1.000 |
| KXHIGHLAX | 7 | 7 | 0 | 100.00% | 98.71% | +0.90 | +0.90 | 0.00 | +1.29 | 0.913 | 1.000 |
| KXHIGHLAX (trade-confirmed) | 5 | 5 | 0 | 100.00% | 98.74% | +0.63 | +0.63 | 0.00 | +1.26 | 0.939 | 1.000 |
| KXHIGHPHIL | 7 | 7 | 0 | 100.00% | 98.59% | +0.99 | +0.99 | 0.00 | +1.41 | 0.905 | 1.000 |
| KXHIGHPHIL (trade-confirmed) | 5 | 5 | 0 | 100.00% | 98.74% | +0.63 | +0.63 | 0.00 | +1.26 | 0.938 | 1.000 |

Every loss (0):

None.

Event accounting:

- 63 settled events on file close after registration (2026-11-16T04:55Z).
- 49 were entered after registration (the table above).
- 0 never had a 97-99c favorite in the window.
- 14 were dropped because their first in-band candle is at or before registration (KXHIGHAUS-26NOV15, KXHIGHAUS-26NOV16, KXHIGHCHI-26NOV15, KXHIGHCHI-26NOV16, KXHIGHDEN-26NOV15, KXHIGHDEN-26NOV16, KXHIGHLAX-26NOV15, KXHIGHLAX-26NOV16, KXHIGHMIA-26NOV15, KXHIGHMIA-26NOV16, KXHIGHNY-26NOV15, KXHIGHNY-26NOV16, KXHIGHPHIL-26NOV15, KXHIGHPHIL-26NOV16).
- Ties: 26 kept entries had more than one strike in band in the entry candle; in 0 the tied strikes settled differently:

Data coverage:

| series | settled events on file | first event | last event | first kept | last kept | markets | candles spacing |
|---|---|---|---|---|---|---|---|
| KXHIGHNY | 12 | KXHIGHNY-26NOV12 (closes 2026-11-13 06:00) | KXHIGHNY-26NOV23 (closes 2026-11-24 06:00) | KXHIGHNY-26NOV17 | KXHIGHNY-26NOV23 | 72 | hourly |
| KXHIGHCHI | 12 | KXHIGHCHI-26NOV12 (closes 2026-11-13 07:00) | KXHIGHCHI-26NOV23 (closes 2026-11-24 07:00) | KXHIGHCHI-26NOV17 | KXHIGHCHI-26NOV23 | 72 | hourly |
| KXHIGHMIA | 12 | KXHIGHMIA-26NOV12 (closes 2026-11-13 06:00) | KXHIGHMIA-26NOV23 (closes 2026-11-24 06:00) | KXHIGHMIA-26NOV17 | KXHIGHMIA-26NOV23 | 72 | hourly |
| KXHIGHAUS | 12 | KXHIGHAUS-26NOV12 (closes 2026-11-13 07:00) | KXHIGHAUS-26NOV23 (closes 2026-11-24 07:00) | KXHIGHAUS-26NOV17 | KXHIGHAUS-26NOV23 | 72 | hourly |
| KXHIGHDEN | 12 | KXHIGHDEN-26NOV12 (closes 2026-11-13 08:00) | KXHIGHDEN-26NOV23 (closes 2026-11-24 08:00) | KXHIGHDEN-26NOV17 | KXHIGHDEN-26NOV23 | 72 | hourly |
| KXHIGHLAX | 12 | KXHIGHLAX-26NOV12 (closes 2026-11-13 09:00) | KXHIGHLAX-26NOV23 (closes 2026-11-24 09:00) | KXHIGHLAX-26NOV17 | KXHIGHLAX-26NOV23 | 72 | hourly |
| KXHIGHPHIL | 12 | KXHIGHPHIL-26NOV12 (closes 2026-11-13 06:00) | KXHIGHPHIL-26NOV23 (closes 2026-11-24 06:00) | KXHIGHPHIL-26NOV17 | KXHIGHPHIL-26NOV23 | 72 | hourly |

Entry time before close: min 38.00h, median 39.00h, max 41.00h. Orphan candles: 0. Late-data warnings: 0 markets.

Notes on F9799:

- Each city has the same 7 kept events, 26NOV17 to 26NOV23. The 26NOV24 city-days had not
  settled at replay time.
- The 14 dropped events (26NOV15 and 26NOV16, all 7 cities) opened before registration and
  were already in band on their first hourly bar (Nov 14 / Nov 15 16:00Z). In this replay
  all 14 of those first touches settled as wins. The live trader, started at registration,
  probably took many of them at its first cycle after 04:55Z Nov 16, so **the PC ledger
  may hold up to 14 more F9799 events than this replay**, possibly on a different strike.
- Every kept entry is the NO side of one strike, 38-41h before close: the first hourly bar
  after the market opened (16:00Z; one at 17:00Z). In practice F9799 here buys "not this
  bucket" on a far strike at the open.
- The daily backfill stores **hourly** candles (the repo's backfill picks the candle size
  from the market's life), so "first in-band minute" is the first in-band hourly bar here.

## HOURLY_FAV (registered 2026-11-17T00:36Z)

Not in this push. Its 1-minute backfill (4 series, about 190 city-hours each) takes several
hours against Kalshi's rate limit and is added in a second push.

## How this differs from the paper trader on the PC

1. **Entry timing.** Here the entry is the first in-band candle: the first 1-minute candle
   for HOURLY_FAV, the first hourly bar for F9799. The trader runs on a 2-minute cycle, so
   it sees a different instant and may enter later, at a different price, on a different
   strike, or not at all.
2. **No order-book depth check.** The paper trader refuses a book without 10 contracts at
   the ask. Candles carry no depth, so this replay assumes every in-band quote filled 10.
   The trade-confirmed subset (a trade printed at or through the entry price in the entry
   candle) is the closest proxy for a real fill.
3. **Ties between strikes.** When several strikes of one event are in band in the same
   candle, this replay takes the first ticker alphabetically; the trader takes the first
   in API order. The tie lists above show where that choice decides the result.
4. **Quote definition.** Here the quote is the candle's closing yes_ask (YES side) or
   1 - closing yes_bid (NO side), and a book counts only if bid >= 1c and the spread is
   <= 15c (`strategy_check.scan`). The trader reads the live order book.
5. **Events that straddle registration are dropped** rather than entered at the first
   in-band moment after registration, because the task's rule is "entry time after
   registration" and the entry is the first touch.
6. **Outages are not modelled.** The trader misses events whenever the PC, network or
   process is down; this replay never does.
7. **At most 15 markets per event** are fetched (the repo backfill's cap). The daily
   events have 6 markets each, so the cap does not bind for F9799.

Reproduce: helper scripts were kept outside the repo (`/tmp/replay/`): a thin wrapper
around `sleeve_scan.cmd_backfill` that waits and retries on Kalshi 429s, and a report
script that makes the `strategy_check` calls listed above. No repo code was changed.
