# SHADOW REPLAY from Kalshi's public price history - NOT the official forward ledger on the owner's PC

The official forward record is `data/paper_sleeves.jsonl` on the Windows PC that runs the
paper trader. It could not be read while the owner was away, so this is an **independent
reconstruction** of the two pre-registered forward rules from Kalshi's public candles. It
is a cross-check, not the record. Where the two disagree, the ledger on the PC wins.

- Replay time: **2026-11-22 04:29Z** (backfill finished at that time; data fetched 2026-11-21 18:58Z - 2026-11-22 04:29Z)
- Source: public endpoints only (`/markets`, `/series/.../candlesticks`). No credentials, no orders.
- Rules replayed exactly as registered. Nothing was registered, retired or edited.
- Method: `scripts/sleeve_scan.py backfill` to get the candles, then `scan_many` ->
  `trades_from` -> `first_per_event` -> `summarize` from `scripts/strategy_check.py`. Band
  is 0.97-0.99, 10 contracts, fee `ceil(0.07*10*p*(1-p))` per order. p-values are exact
  (`sleeve_paper.forward_stats`, one bet per event). Kept only events that have **settled**
  and whose **entry time is after the rule's registration**.
- **Too few events to judge either rule.** Neither rule has the 100+ post-registration
  events plus false-discovery survival that EDGE requires. HOURLY_FAV has 477 events, but
  its result is not significant in either direction. F9799 has only 28.

## HOURLY_FAV (registered 2026-11-17T00:36Z)

Hourly temperature (KXTEMPNYCHS, KXTEMPCHIHS, KXTEMPMIAH, KXTEMPLAXHS). The entry is the
first 1-minute candle whose ask sits at 97-99c, inside the market's last hour, one bet per
city-hour.

| | events | wins | losses | win rate | break-even | net $ (10-lot) | won $ | lost $ | edge c/contract | exact p (makes money) | p (loses money) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| All entries (quote-based) | 477 | 468 | 9 | 98.11% | 98.60% | **-23.31** | +64.98 | -88.29 | **-0.49** | 0.864 | 0.227 |
| Trade-confirmed only | 209 | 203 | 6 | 97.13% | 98.51% | **-28.77** | +29.88 | -58.65 | **-1.38** | 0.961 | 0.095 |

Fees paid: $7.41. Average entry ask: 0.9845. Mix: 71 entries at 97c, 122 at 98c, 284 at 99c.
"Trade-confirmed" means a trade printed at or through the entry price in the entry candle.

Per city (all entries; trade-confirmed in the last column):

| series | events | W-L | win rate vs need | net $ | won $ / lost $ | edge c | exact p | trade-confirmed |
|---|---|---|---|---|---|---|---|---|
| KXTEMPNYCHS | 118 | 116-2 | 98.31% vs 98.69% | -4.52 | +15.12 / -19.64 | -0.38 | 0.798 | 63 bets, 1 lost, -$1.90 |
| KXTEMPCHIHS | 120 | 118-2 | 98.33% vs 98.65% | -3.80 | +15.84 / -19.64 | -0.32 | 0.779 | 46 bets, 1 lost, -$3.34 |
| KXTEMPMIAH  | 120 | 115-5 | 95.83% vs 98.36% | -30.29 | +18.72 / -49.01 | -2.52 | 0.985 (p_neg 0.048) | 56 bets, 4 lost, -$29.65 |
| KXTEMPLAXHS | 119 | 119-0 | 100.00% vs 98.71% | +15.30 | +15.30 / 0.00 | +1.29 | 0.214 | 44 bets, 0 lost, +$6.12 |

Every loss:

| ticker | side | entry | entry time (UTC) | before close | settled | trade-confirmed | P&L |
|---|---|---|---|---|---|---|---|
| KXTEMPMIAH-26NOV1804-T71.99 | YES | 0.97 | 2026-11-18 08:09 | 0.85h | NO | yes | -9.73 |
| KXTEMPNYCHS-26NOV1906-T39.99 | NO | 0.98 | 2026-11-19 10:08 | 0.87h | YES | no | -9.82 |
| KXTEMPCHIHS-26NOV1906-T47.99 | NO | 0.99 | 2026-11-19 10:11 | 0.82h | YES | no | -9.91 |
| KXTEMPMIAH-26NOV1911-T77.99 | NO | 0.98 | 2026-11-19 15:04 | 0.93h | YES | yes | -9.82 |
| KXTEMPMIAH-26NOV1916-T78.99 | NO | 0.98 | 2026-11-19 20:08 | 0.87h | YES | yes | -9.82 |
| KXTEMPMIAH-26NOV2010-T73.99 | YES | 0.97 | 2026-11-20 14:31 | 0.48h | NO | yes | -9.73 |
| KXTEMPNYCHS-26NOV2015-T67.99 | YES | 0.98 | 2026-11-20 19:06 | 0.90h | NO | yes | -9.82 |
| KXTEMPCHIHS-26NOV2104-T51.99 | NO | 0.97 | 2026-11-21 08:03 | 0.95h | YES | yes | -9.73 |
| KXTEMPMIAH-26NOV2113-T77.99 | NO | 0.99 | 2026-11-21 17:09 | 0.85h | YES | no | -9.91 |

Every event is accounted for:
- **482 settled city-hours** close after registration.
- **477 were entered** after registration.
- **1 had no 97-99c favorite at all** in its window: KXTEMPNYCHS-26NOV2009.
- **4 were dropped** because their first in-band minute came before the 00:36Z
  registration (the 20:00 ET hour on Nov 16, first touches at 00:03-00:13Z). The live
  trader, started at registration, may still have taken them later in that hour if the
  favorite was still in band. That rule is not reproduced here.

Ties: 76 of the 477 entries had more than one strike in band in the same minute. In 5 of
them the tied strikes settled differently, so the tie-break decides the result:
- One loss above, CHI 26NOV1906-T47.99, would have been a win if the tie had gone to
  T48.99 NO @ 0.98.
- 4 wins would have been losses if the tie had gone the other way: MIA 26NOV1819,
  NYC 26NOV1903, LAX 26NOV1920, NYC 26NOV2005.

## F9799 (registered 2026-11-16T04:55Z)

Daily high temperature (KXHIGHNY, KXHIGHCHI, KXHIGHMIA, KXHIGHAUS, KXHIGHDEN, KXHIGHLAX,
KXHIGHPHIL). Favorite at 97-99c, any time, one bet per city-day.

| | events | wins | losses | win rate | break-even | net $ (10-lot) | won $ | lost $ | edge c/contract | exact p |
|---|---|---|---|---|---|---|---|---|---|---|
| All entries (quote-based) | 28 | 28 | 0 | 100.00% | 98.68% | **+3.69** | +3.69 | 0.00 | **+1.32** | 0.689 |
| Trade-confirmed only | 16 | 16 | 0 | 100.00% | 98.59% | **+2.25** | +2.25 | 0.00 | **+1.41** | 0.797 |

Fees paid: $0.41. Mix: 2 entries at 97c, 9 at 98c, 17 at 99c. **No losses.**

Per city: each series has 4 events (26NOV17-26NOV20), 4-0.

| series | net $ | edge c | exact p | trade-confirmed |
|---|---|---|---|---|
| KXHIGHNY | +0.63 | +1.57 | 0.938 | 3 bets, +$0.54 |
| KXHIGHCHI | +0.63 | +1.57 | 0.938 | 3 bets, +$0.54 |
| KXHIGHMIA | +0.45 | +1.12 | 0.956 | 0 bets |
| KXHIGHAUS | +0.45 | +1.12 | 0.956 | 2 bets, +$0.18 |
| KXHIGHDEN | +0.54 | +1.35 | 0.947 | 3 bets, +$0.45 |
| KXHIGHLAX | +0.54 | +1.35 | 0.947 | 2 bets, +$0.27 |
| KXHIGHPHIL | +0.45 | +1.12 | 0.956 | 3 bets, +$0.27 |

Every event is accounted for:
- **42 settled city-days** close after registration.
- **28 were entered** after registration (event dates NOV17-NOV20).
- **0 had no 97-99c favorite.**
- **14 were dropped** (event dates NOV15 and NOV16, all 7 cities). They opened before
  registration and were already in band on their first hourly bar (Nov 14 / Nov 15
  16:00Z). The live trader probably took many of them at its first cycle after 04:55Z
  Nov 16, so **the PC ledger should hold up to 14 more F9799 events than this replay.**
- NOV21 events had not settled at replay time.

Every kept entry came 38-41h before close, which is the first hourly bar after the market
opened. The first in-band side is usually a far-tail strike, so in practice F9799 buys a
far-tail strike at the open.

## Data coverage (events on file per series; the last event is the newest settled at fetch)

| series | first event on file | last event on file | first kept | last kept |
|---|---|---|---|---|
| KXTEMPNYCHS | 26NOV1600 | 26NOV2122 | 26NOV1621 | 26NOV2122 |
| KXTEMPCHIHS | 26NOV1601 | 26NOV2122 | 26NOV1621 | 26NOV2122 |
| KXTEMPMIAH | 26NOV1601 | 26NOV2122 | 26NOV1621 | 26NOV2122 |
| KXTEMPLAXHS | 26NOV1600 | 26NOV2121 | 26NOV1621 | 26NOV2121 |
| KXHIGHNY / CHI / MIA / AUS / DEN / LAX / PHIL | 26NOV13 | 26NOV20 | 26NOV17 | 26NOV20 |

Hourly event suffixes are ET date+hour. In-scope hourly closes run 2026-11-17 01:00Z to
2026-11-22 03:00Z (LAX to 02:00Z: its 03:00Z hour had not settled when it was fetched).

Data checks:
- Hourly series: 5,757 markets, 1-minute candles over each market's whole 1-hour life.
- Daily series: 336 markets, hourly candles over the whole life.
- No late-data warnings and no orphan candles.
- Some hourly markets have only ~8-10 one-minute candles, because Kalshi does not return
  a candle for every minute. A first touch inside a missing minute appears at the next
  candle.

## How this differs from the paper trader on the PC

1. **Entry timing.** Here, the entry is the first in-band candle: the first minute for
   HOURLY_FAV, the first **hour** bar for F9799, because the daily backfill uses hourly
   candles. The trader runs on a 2-minute cycle, so it sees a different instant and may
   enter later, at a different price, or not at all.
2. **No order-book depth check.** The paper trader refuses a book without 10 contracts at
   the ask. Candles have no depth, so this replay assumes every in-band quote filled 10.
   The trade-confirmed subset is the closest proxy for a real fill.
3. **Tie-breaking between strikes.** When several strikes of one event are in band in the
   same minute, this replay takes the first ticker alphabetically. The trader takes the
   first in API order. This decides the outcome of 5 HOURLY_FAV events (listed above).
4. **Quote definition.** Here the quote is the candle's closing yes_ask (YES) or
   1 - yes_bid (NO), and a book only counts if bid >= 1c and the spread is <= 15c
   (`strategy_check.scan`). The trader reads the live order book.
5. **Events that straddle registration are dropped** (4 HOURLY_FAV, 14 F9799) instead of
   being entered at the first in-band moment after registration.
6. **Outages are not modelled.** The trader misses events whenever the PC, network or
   process is down; this replay never does.

Reproduce: the helper scripts lived outside the repo (`/tmp/replay/`). They are a thin
wrapper around `sleeve_scan.cmd_backfill` that backs off on Kalshi 429s, plus the
`strategy_check` calls listed above.
