# Weather — the Conditional Test (Decision Record, 2026-09-11)

## The mistake this corrects

The 7-city weather backfill (2,852 settled markets, 3.1M candles) was screened with
`edge_analysis` and `maker_replay` and produced **0 HELD cells**. Read carelessly, that
reads as "weather is dead too" — and it would have closed the one venue where this
project has ever measured a real edge.

It is not what the screen says. `edge_analysis` and `maker_replay` test for
**unconditional** mispricing:

> at price P, does this contract settle YES more often than P?

That is the right question for 15-minute crypto, where nobody at retail has better
information than the tape, so if there is an edge it must live in the price alone. It is
the **wrong question for weather**, because a weather market can be perfectly calibrated
on average — every 30c contract settling 30% of the time — and still be beatable every
day. The weather edge is **conditional**: it exists only on days when a forecast disagrees
with the price. Averaged across all days, the disagreements point both ways and cancel
exactly. The screener is blind to it *by construction*, not by accident.

So: 0 HELD in the unconditional screen is **not** evidence against the weather sleeve. It
is silence on the question. `scripts/forecast_replay.py` asks the question.

## What the tool does

For every city-day in the backfill: take what the forecast said, price every temperature
bucket from it, compare against the book that actually existed at that minute, bet only
where forecast and price disagree by more than a threshold, and settle on Kalshi's own
result.

Bucket caps are recovered from the neighbouring strike in the same event (the backfill
stored `floor_strike` but not `cap_strike`). The day's realized high is recovered from
Kalshi's own settlement — the one bucket that resolved YES brackets it — so no
observations API is needed and the settlement truth and the trading truth are the same
number.

## The three guards, and why each exists

**1. No lookahead.** The headline ("STRICT") replay prices the rest of a day from the
forecast run issued the *previous* day, plus the temperature already observed that day.
Both are genuinely available to a trader standing at that minute. The tempting shortcut —
using the archived best-match series for the whole day — contains analysis of hours that
had not happened yet at entry time.

This is not a theoretical concern. On a fixture where the book is priced *at* the
forecast's own fair value (nothing to find, by construction), the lookahead variant
reports **+$0.65 per city-day, 8 cells HELD, forecast Brier 0.029 vs price 0.139**. Every
cent of it is fabricated by knowing the answer. That variant is still computed and printed
as `DIAGNOSTIC`, so the size of the gap is visible, and it must never be quoted as a
result.

**2. The forecast has to beat the price, not merely have an opinion.** Before any P&L, the
report scores our forecast-implied probabilities and the market's own prices against the
same settled outcomes with the Brier score. If the price wins, no threshold can rescue it
and every positive cell below is selection. This is gate G4 of `lib/binary_justify`
applied to history instead of to a live quote. It is also the highest-powered test in the
report: it separates the two fixture worlds at under 100 city-days, long before the P&L
grid commits to anything.

**3. Walk-forward, per window, search-corrected.** Sigma and bias are *measured* on the
first half by date and frozen; the second half is scored with no further choices. A window
is a city-day, not a contract — the eight buckets of one city-day resolve on ONE
temperature, so they are one bet, not eight, and all P&L is per window. The grid is
frozen at 6 bands x 4 thresholds, and the out-of-sample interval is Bonferroni-corrected
over the cells actually searched. Both corrections were forced by fixture failures: the
uncorrected interval called one cell significant on data with nothing in it, and a cell
backed by 4 city-days produced a +$0.55/window "result". Cells now need 30 city-days on
each side of the split before they can be called anything.

## Measured discrimination

Two synthetic worlds, same shape as the real data — one where the book is priced at the
forecast's fair value, one where it is anchored to climatology while the forecast knows
the day:

| city-day windows | beatable world | efficient world |
|---|---|---|
| ~80  | gate: FORECAST WINS, 0 HELD (8 persist-thin) | gate: PRICE WINS, 0 HELD |
| ~160 | 5 HELD  | 0 HELD |
| ~320 | 15 HELD | 0 HELD |
| ~600 | 16 HELD | 0 HELD |
| ~800 | 16 HELD | 0 HELD |

Cheaper than the ~600 independent windows the crypto screen needed, for a structural
reason: one weather bet is a whole day's disagreement, while one crypto bet is a coin flip
with a spread on it. **Caveat:** the planted edge there is large. A one- or two-cent
conditional edge would need far more data than this table suggests.

## Running it (on the host — the sandbox cannot reach Open-Meteo)

```powershell
$env:BACKFILL_LOG="data\backfill_weather.jsonl"
py scripts\forecast_replay.py probe      # which archive is reachable, and does it
                                         # carry the previous-day run?
py scripts\forecast_replay.py fetch      # cache forecasts for the backfilled days
py scripts\forecast_replay.py replay     # the report
Remove-Item Env:BACKFILL_LOG
```

`probe` must be run first and its output read. The one thing no code review can verify
remotely is whether the archive carries `temperature_2m_previous_day1`. If no source does,
`replay` can only print the DIAGNOSTIC number, which cannot justify a trade — the report
says so in place rather than quietly degrading.

## How to read the verdict

- **Gate says PRICE WINS** → done. The book already contains the forecast; the sleeve is
  closed on the same evidentiary standard that closed crypto, for $0.
- **Gate says FORECAST WINS but 0 HELD** → the forecast is better than the price, yet not
  by enough to clear spread and fees. Worth a maker-side replay before anything else: the
  crypto work showed taker friction is what kills marginal edges.
- **Gate says FORECAST WINS and a cell HELD** → a real conditional edge, in a shape the
  unconditional screener cannot see. Next step is *not* live money. It is a pre-registered
  hypothesis in `shadow_book.RULES` with a `named_at` stamp, then forward-only paper,
  exactly as every other rule in this project has had to earn.

No result here moves real money. Real money stays off until a rule is positive
out-of-sample on the *forward* paper ledger, which is the only record that can earn it.
