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
- **Neither rule shows an edge here.** F9799 is 49-0 but has only 49 events. HOURLY_FAV
  has 738 events and is break-even. EDGE needs 100+ post-registration events and survival
  of the false-discovery correction, and only the official ledger can award it.

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
- Ties: 26 kept entries had more than one strike in band in the entry candle; in 0 the tied strikes settled differently.

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

Hourly temperature (KXTEMPNYCHS, KXTEMPCHIHS, KXTEMPMIAH, KXTEMPLAXHS). Each market lives
one hour; the entry is the first 1-minute candle whose ask sits at 97-99c, one bet per
city-hour.

- Replay time: **2026-11-24 22:19Z** (data fetched 2026-11-24 21:11Z - 22:19Z).
- Result: **738 settled city-hours entered, 728 won, 10 lost, net +$4.76** at 10 contracts
  (+0.06c per contract). That is break-even: the win rate is 98.64% against 98.58%
  needed, and the exact p is 0.52 for "makes money" and 0.60 for "loses money".
- On the trade-confirmed subset it is slightly negative: 295 bets, 6 lost, net -$14.19.
- Miami carries the losses: 5 of the 10, net -$18.86. No city is significant either way.

| | events | wins | losses | win rate | break-even | net $ (10-lot) | won $ | lost $ | edge c/contract | exact p (makes money) | p (loses money) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| All entries (quote-based) | 738 | 728 | 10 | 98.64% | 98.58% | +4.76 | +102.96 | -98.20 | +0.06 | 0.523 | 0.602 |
| Trade-confirmed only | 295 | 289 | 6 | 97.97% | 98.45% | -14.19 | +44.46 | -58.65 | -0.48 | 0.822 | 0.310 |

Fees paid: $11.64. Entry mix: 103 at 97c, 220 at 98c, 415 at 99c. Sides: 305 YES, 433 NO.

Per city (all entries, then the trade-confirmed subset):

| series | events | wins | losses | win rate | break-even | net $ (10-lot) | won $ | lost $ | edge c/contract | exact p (makes money) | p (loses money) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| KXTEMPNYCHS | 183 | 181 | 2 | 98.91% | 98.68% | +4.21 | +23.85 | -19.64 | +0.23 | 0.563 | 0.699 |
| KXTEMPNYCHS (trade-confirmed) | 88 | 87 | 1 | 98.86% | 98.63% | +2.06 | +11.88 | -9.82 | +0.23 | 0.660 | 0.704 |
| KXTEMPCHIHS | 185 | 183 | 2 | 98.92% | 98.64% | +5.20 | +24.84 | -19.64 | +0.28 | 0.538 | 0.719 |
| KXTEMPCHIHS (trade-confirmed) | 66 | 65 | 1 | 98.48% | 98.54% | -0.37 | +9.36 | -9.73 | -0.06 | 0.750 | 0.621 |
| KXTEMPMIAH | 185 | 180 | 5 | 97.30% | 98.32% | -18.86 | +30.15 | -49.01 | -1.02 | 0.906 | 0.203 |
| KXTEMPMIAH (trade-confirmed) | 83 | 79 | 4 | 95.18% | 98.06% | -23.89 | +15.21 | -39.10 | -2.88 | 0.977 | 0.078 |
| KXTEMPLAXHS | 185 | 184 | 1 | 99.46% | 98.69% | +14.21 | +24.12 | -9.91 | +0.77 | 0.301 | 0.913 |
| KXTEMPLAXHS (trade-confirmed) | 58 | 58 | 0 | 100.00% | 98.62% | +8.01 | +8.01 | 0.00 | +1.38 | 0.446 | 1.000 |

Every loss (10):

| ticker | side | entry | entry time (UTC) | hours before close | settled | trade-confirmed | P&L $ |
|---|---|---|---|---|---|---|---|
| KXTEMPMIAH-26NOV1804-T71.99 | YES | 0.97 | 2026-11-18 08:09 | 0.85 | NO | yes | -9.73 |
| KXTEMPNYCHS-26NOV1906-T39.99 | NO | 0.98 | 2026-11-19 10:08 | 0.87 | YES | no | -9.82 |
| KXTEMPCHIHS-26NOV1906-T47.99 | NO | 0.99 | 2026-11-19 10:11 | 0.82 | YES | no | -9.91 |
| KXTEMPMIAH-26NOV1911-T77.99 | NO | 0.98 | 2026-11-19 15:04 | 0.93 | YES | yes | -9.82 |
| KXTEMPMIAH-26NOV1916-T78.99 | NO | 0.98 | 2026-11-19 20:08 | 0.87 | YES | yes | -9.82 |
| KXTEMPMIAH-26NOV2010-T73.99 | YES | 0.97 | 2026-11-20 14:31 | 0.48 | NO | yes | -9.73 |
| KXTEMPNYCHS-26NOV2015-T67.99 | YES | 0.98 | 2026-11-20 19:06 | 0.90 | NO | yes | -9.82 |
| KXTEMPCHIHS-26NOV2104-T51.99 | NO | 0.97 | 2026-11-21 08:03 | 0.95 | YES | yes | -9.73 |
| KXTEMPMIAH-26NOV2113-T77.99 | NO | 0.99 | 2026-11-21 17:09 | 0.85 | YES | no | -9.91 |
| KXTEMPLAXHS-26NOV2210-T47.99 | YES | 0.99 | 2026-11-22 14:10 | 0.83 | NO | no | -9.91 |

Event accounting:

- 743 settled events on file close after registration (2026-11-17T00:36Z).
- 738 were entered after registration (the table above).
- 1 never had a 97-99c favorite in the window: KXTEMPNYCHS-26NOV2009.
- 4 were dropped because their first in-band candle is at or before registration (KXTEMPCHIHS-26NOV1620, KXTEMPLAXHS-26NOV1620, KXTEMPMIAH-26NOV1620, KXTEMPNYCHS-26NOV1620).
- Ties: 108 kept entries had more than one strike in band in the entry candle; in 5 the tied strikes settled differently:
  - KXTEMPMIAH-26NOV1819 at 2026-11-18 23:11: took KXTEMPMIAH-26NOV1819-T73.99 (win). Tied: KXTEMPMIAH-26NOV1819-T73.99 YES @ 0.97 -> win; KXTEMPMIAH-26NOV1819-T74.99 NO @ 0.98 -> LOSS
  - KXTEMPNYCHS-26NOV1903 at 2026-11-19 07:07: took KXTEMPNYCHS-26NOV1903-T37.99 (win). Tied: KXTEMPNYCHS-26NOV1903-T37.99 YES @ 0.98 -> win; KXTEMPNYCHS-26NOV1903-T39.99 NO @ 0.99 -> LOSS
  - KXTEMPCHIHS-26NOV1906 at 2026-11-19 10:11: took KXTEMPCHIHS-26NOV1906-T47.99 (LOSS). Tied: KXTEMPCHIHS-26NOV1906-T47.99 NO @ 0.99 -> LOSS; KXTEMPCHIHS-26NOV1906-T48.99 NO @ 0.98 -> win
  - KXTEMPLAXHS-26NOV1920 at 2026-11-20 00:11: took KXTEMPLAXHS-26NOV1920-T61.99 (win). Tied: KXTEMPLAXHS-26NOV1920-T61.99 YES @ 0.98 -> win; KXTEMPLAXHS-26NOV1920-T63.99 NO @ 0.97 -> LOSS
  - KXTEMPNYCHS-26NOV2005 at 2026-11-20 09:06: took KXTEMPNYCHS-26NOV2005-T39.99 (win). Tied: KXTEMPNYCHS-26NOV2005-T39.99 YES @ 0.98 -> win; KXTEMPNYCHS-26NOV2005-T40.99 YES @ 0.99 -> LOSS

Data coverage:

| series | settled events on file | first event | last event | first kept | last kept | markets | candles spacing |
|---|---|---|---|---|---|---|---|
| KXTEMPNYCHS | 200 | KXTEMPNYCHS-26NOV1605 (closes 2026-11-16 10:00) | KXTEMPNYCHS-26NOV2415 (closes 2026-11-24 20:00) | KXTEMPNYCHS-26NOV1621 | KXTEMPNYCHS-26NOV2415 | 2068 | 1-minute |
| KXTEMPCHIHS | 200 | KXTEMPCHIHS-26NOV1606 (closes 2026-11-16 11:00) | KXTEMPCHIHS-26NOV2415 (closes 2026-11-24 20:00) | KXTEMPCHIHS-26NOV1621 | KXTEMPCHIHS-26NOV2415 | 2059 | 1-minute |
| KXTEMPMIAH | 200 | KXTEMPMIAH-26NOV1606 (closes 2026-11-16 11:00) | KXTEMPMIAH-26NOV2415 (closes 2026-11-24 20:00) | KXTEMPMIAH-26NOV1621 | KXTEMPMIAH-26NOV2415 | 2009 | 1-minute |
| KXTEMPLAXHS | 200 | KXTEMPLAXHS-26NOV1606 (closes 2026-11-16 11:00) | KXTEMPLAXHS-26NOV2415 (closes 2026-11-24 20:00) | KXTEMPLAXHS-26NOV1621 | KXTEMPLAXHS-26NOV2415 | 2057 | 1-minute |

Entry time before close: min 0.27h, median 0.90h, max 0.98h. Orphan candles: 0. Late-data warnings: 0 markets.

Notes on HOURLY_FAV:

- Kept events run from the hour closing 2026-11-17 02:00Z (suffix 26NOV1621 is the ET
  date and hour the market opens) to the hour closing 2026-11-24 20:00Z. Later hours had
  not settled when the market list was fetched.
- 752 city-hours fall in that span (4 cities x 188 closes, 01:00Z Nov 17 to 20:00Z
  Nov 24). 743 are on file. The other 9 do not exist on Kalshi: the hours closing 09:00Z
  and 10:00Z on Nov 19 in all four cities, and NYC's hour closing 01:00Z on Nov 20
  (asking the API for those events returns no markets).
- The 4 dropped events are the 20:00 ET hour on Nov 16, first in band at 00:03-00:13Z,
  before the 00:36Z registration. All 4 first touches settled as wins. The live trader may
  still have taken them later in that hour.
- 9 of the 10 losses are the same 9 the first shadow replay listed; the new one is
  KXTEMPLAXHS-26NOV2210. There have been no losses in the 249 kept events since then.
- Three in-scope events have 16 strikes on Kalshi, one more than the backfill's 15-market
  cap (KXTEMPNYCHS-26NOV2010, KXTEMPCHIHS-26NOV1710, -26NOV1810, -26NOV2011). I fetched
  the four omitted strikes separately: none was ever in band, so no result changes.
- No failed fetches, no orphan candles, no late-data warnings. Kalshi does not return a
  candle for every minute, so a first touch inside a missing minute shows up at the next
  candle.

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
   events have 6 markets each. Three in-scope hourly events have 16; the omitted strikes
   were checked by hand and change nothing (see the HOURLY_FAV notes).

Reproduce: helper scripts were kept outside the repo (`/tmp/replay/`): a thin wrapper
around `sleeve_scan.cmd_backfill` that waits and retries on Kalshi 429s, and a report
script that makes the `strategy_check` calls listed above. No repo code was changed.
