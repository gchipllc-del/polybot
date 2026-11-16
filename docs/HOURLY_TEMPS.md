# Kalshi HOURLY temperature markets - the 97-99c "both sides of the reading" method

Research only. Public endpoints, no credentials, no orders. Built 2026-11-16 overnight.

The method under test (seen in a trader's portfolio): in a city-hour market late in the
hour, buy 2-3 strikes on BOTH sides of the current temperature - e.g. Coastal LA 10pm
with the temperature near 68: YES ">= 66", YES ">= 67", NO ">= 69" - 10 contracts each at
97-99c, for $0.09-0.30 profit each. ~34 positions seen, all wins; many fills had no fee
(resting limit orders = maker).

**Bottom line (history, 300 most recent events per city = Nov 3-16, 2026):**
* The method as seen - any 97-99c strike, near the current reading, any time in the hour,
  several strikes per city-hour - **loses after fees**: 5,412 entries, 98.02% won vs a
  98.52% break-even, -0.35c per contract (section 4a).
* **Choosing the strike from the live reading does not fix it.** At every cell of the
  pre-declared grid the reading-chosen bet is at best break-even, and requiring a BIGGER
  margin makes it WORSE: a strike still offered at 97-99c despite sitting 3 degF on the
  safe side is one the market expects the temperature to cross (section 4b).
* One version did make money: **ONE bet per city-hour, the first strike quoted 97-99c**,
  which in practice means buying the market's own opening favorite within ~10 minutes of
  the open (median 6 minutes): 1,199 events, 4 losses, +0.96c per contract, exact p = 0.001 (trade-confirmed
  only: 462 events, 1 loss, p = 0.015). It survives changes of tie-break (+0.65 to +0.96c,
  p 0.001-0.024) and is positive in all four cities. It is two weeks of one season, quote
  data not fills - a NOMINATION for a forward paper rule (section 6), nothing more.
* Her 34-for-34 is no evidence either way: at 98c a break-even strategy wins 34 in a row
  54% of the time (0.982^34).

---

## 1. The series

The in-app title "Temperature in New York City today at 7pm EST?" is rendered from an
absolute-date title: `Temperature in New York City on Nov 15, 2026 at 7pm EST?`.

| series | city (title) | settled events | first .. last | notes |
|---|---|---|---|---|
| `KXTEMPNYCHS` | New York City | 1,547 | 2026-09-10 .. 11-16 | current |
| `KXTEMPCHIHS` | Chicago Metro Area | 1,575 | 2026-09-10 .. 11-16 | current |
| `KXTEMPMIAH` | Miami | 1,660 | 2026-09-06 .. 11-16 | current |
| `KXTEMPLAXHS` | Coastal Los Angeles | 1,572 | 2026-09-10 .. 11-16 | current |
| `KXTEMPNYCH`, `KXTEMPCHIH`, `KXTEMPLAXH`, `KXTEMPAUSH`, `KXTEMPDCH` | NYC, Chicago, LA, Austin, DC | - | last event 2026-09-10 10am | legacy: settled on The Weather Company until Sep 10, then replaced |
| `KXTEMPBOSH`, `KXTEMPLAHS`, `KXTEMPCHIMETROHS`, `KXHIGHNYD` | - | 0 | - | catalog entries with no events |

Found with `GET /trade-api/v2/series` (frequency `hourly`, category Climate and Weather)
and `GET /events?series_ticker=...`.

**Hours.** All 24 clock hours, every day (each series has 57-71 events at every UTC hour).
The hours seen in the portfolio (5pm-10pm EST) are just the ones that trader was awake for.

**Tickers.** Event `KXTEMPLAXHS-26NOV1522` = Nov 15, 2026, 22:00 Eastern (EST). Market
`KXTEMPLAXHS-26NOV1522-T66.99`.

**Strikes.** 10 markets per event (a few events have 13-16), all `strike_type: greater`,
1 degF apart: `floor_strike` 66.99 = `yes_sub_title` "67° or above"; YES iff value > 66.99.
The ladder is centred near the expected temperature.

**Market life: exactly 1 hour.** `open_time` = settlement hour - 1h, `close_time` = the
settlement hour, `expected_expiration_time` = close + 5 min. Example: the 10pm EST market
opens 02:00Z, closes 03:00Z, settles ~03:05Z. So "the last hour" IS the whole market.

**Fees.** `fee_type: quadratic`, `fee_multiplier: 1` - the standard taker fee
ceil(0.07 x C x P x (1-P)) per order: a 10-lot at 98c pays $0.02 (0.2c/contract).

## 2. Settlement rules (quoted from the API)

Series (all four current series): `settlement_sources` = **Synoptic Data**
(https://synopticdata.com/) for NYC, Miami, Coastal LA; **"Kalshi" (https://kalshi.com)**
for Chicago Metro. `contract_url`:
`https://assets.kalshi.com/regulatory/product-certifications/{NYCWINDEX,CHIMETROWINDEX,MIAWINDEX,LAWINDEX}.pdf`
(`contract_terms_url` `.../contract_terms/<same>.pdf`). The PDFs could not be fetched from
this sandbox (assets.kalshi.com is not on its network allowlist) - **read them on the
host**: they define the index.

`KXTEMPMIAH` `product_metadata.important_info`:
> **Important information:** The data shown is 5 minutes delayed. Resolution for KXTEMPMIAH
> is based on Synoptic Data and calculated via Kalshi Weather Index Methodology. Please see
> additional details for more information.

Legacy `KXTEMPNYCH` important_info:
> Effective Thursday, September 10th for the 11am - 12pm ET market, the resolution source
> for KXTEMPLAXH, KXTEMPCHIH, and KXTEMPNYCH will transition to Synoptic Data. Until then,
> these stations will continue to be resolved using the current TWC methodology.

`rules_primary` (KXTEMPLAXHS-26NOV1522-T66.99; the other cities read the same with their
city name - "New York City", "Chicago Metro Area", "Miami, FL"):
> If the temperature recorded at Coastal Los Angeles for Nov 15, 2026 at 10 PM EST as
> reported by Synoptic Data, is above 66.99°, then the market resolves to Yes.

`rules_secondary` (identical in all four):
> The official final value for this market is determined strictly by the temperature
> reported by Synoptic Data, calculated in accordance with the Kalshi Weather Index
> Methodology. Alternative sources—such as NWS Climatological Reports or Google Weather—may
> serve as references, but are not authoritative and will not be used for market
> resolution.

**What this means - it is an INDEX, not a station.**
* The settled value (`expiration_value`) is usually NOT an integer and not a single
  station's reading: NYC 44.65, Chicago 38.19, Miami 71.96 (only 41-72 of ~1,560 values
  per series are whole degrees). Units degF, two decimals, compared directly with the
  X.99 strike - no rounding.
* No single ASOS station matches it. For NYC/Chicago, the latest routine METAR at the hour
  from NYC, LGA, JFK, EWR, ORD or MDW is within 1 degF of the value only 26-53% of the
  time (exact match about 1% or less), and averages of those stations do no better (a ~1-1.5 degF cool
  offset remains).
* A web search summary of Kalshi's CFTC self-certification filing (Aug 12, 2026; the
  filing itself was not reachable from here) describes the Miami index as a
  minute-resolution weighted average of five high-frequency ASOS records - KMIA, KOPF,
  KFLL, KFXE, KPMP - delivered by Synoptic, with no carry-forward of missing data; a
  third-party site describes NYC as 8 stations with a per-station offset re-estimated
  weekly and a 7-of-8 quorum. Treat these as unverified until the PDFs are read.
* Consequence for this test: the exact settlement value cannot be rebuilt from public
  station data. But it does not need to be - see section 3.

**Edge-case rules worth knowing:** values are compared at two decimals against X.99, so
"67° or above" means index > 66.99. Missing-data handling (quorum) is in the PDFs; the
settlement-value consistency check found **0 events** (of 6,354) whose strike results
disagree with the reported value.

## 3. The reading a trader can know at entry - and the price-free odds

**Entry 60 min before settlement** (= market open): the index value for the PREVIOUS
clock hour is published as that hour's settlement value - it IS the index at the entry
moment, exactly. It is public ~5 min later, so the 60-minute test reads the quote at
settlement - 55 min.

**Entry 30 min before:** the previous hour's value, carried forward by the mean change of
1-minute ASOS readings (IEM archive) at proxy stations from the previous hour mark to
entry - 5 min (the app's stated delay). Proxy stations, chosen before any price result:
NYC: NYC, LGA, JFK, EWR, TEB, HPN, FRG (CDW has little 1-min data) - Chicago: ORD, MDW,
PWK, ARR, UGN (DPA, LOT, GYY have no 1-min data in IEM) - Miami: MIA, OPF, FLL, FXE, PMP -
Coastal LA: LAX, SMO, HHR, LGB (TOA none).

How well that works, measured over the FULL hour (degF error vs the settled value):

| series | method | n | MAE | RMSE | within 0.5 | within 1 | within 2 |
|---|---|---|---|---|---|---|---|
| NYC | persistence (value 1h earlier) | 1509 | 1.26 | 1.72 | 32% | 53% | 79% |
| NYC | prev value + proxy change | 1466 | 0.49 | 0.74 | 64% | 90% | 99% |
| Chicago | persistence | 1560 | 1.39 | 1.96 | 33% | 49% | 76% |
| Chicago | prev + proxy | 1482 | 0.49 | 0.64 | 61% | 90% | 99% |
| Miami | persistence | 1646 | 0.98 | 1.41 | 41% | 61% | 87% |
| Miami | prev + proxy | 1567 | 0.42 | 0.69 | 71% | 94% | 99% |
| Coastal LA | persistence | 1558 | 1.22 | 1.74 | 35% | 52% | 82% |
| Coastal LA | prev + proxy | 1510 | 0.46 | 0.65 | 67% | 91% | 99% |

So the index moves ~1-1.4 degF in a typical hour, and the proxy tracks it to ~0.5 degF.

**Price-free base rate** - for every strike of every settled event, the side the entry
reading favors and its MARGIN (degF between the reading and the strike's line: 68 vs
"67 or above" = YES by 1; 68 vs "69 or above" = NO by 1). How often did that side LOSE?
(per strike; strikes of one hour are correlated - one weather outcome.)

| series | entry | [0,1) | [1,2) | [2,3) | [3,4) | [4,6) | 6+ |
|---|---|---|---|---|---|---|---|
| NYC | 60m | 35.6% | 15.8% | 6.6% | 3.1% | 0.7% | 0.0% (n=309) |
| NYC | 30m | 30.6% | 9.5% | 2.5% | 0.6% | 0.2% | 0.5% (n=373) |
| Chicago | 60m | 35.4% | 17.5% | 8.3% | 4.3% | 1.5% | 0.3% |
| Chicago | 30m | 32.2% | 10.4% | 2.7% | 0.6% | 0.1% | 0.0% |
| Miami | 60m | 31.6% | 11.1% | 4.2% | 1.7% | 0.5% | 0.0% |
| Miami | 30m | 28.2% | 6.6% | 1.5% | 0.2% | 0.1% | 0.0% |
| Coastal LA | 60m | 34.3% | 14.5% | 6.8% | 3.3% | 1.5% | 0.7% |
| Coastal LA | 30m | 30.0% | 9.0% | 2.3% | 0.4% | 0.0% | 0.0% |

(~2,600-3,300 strikes per cell, ~1,500 events per series.) **Read this against the price:**
buying at 98c with a 10-lot taker fee breaks even at a 1.8% loss rate; at 97c, 2.7%; at
99c, 0.9%. At 60 minutes out, only margins of 4+ degF are clearly safer than a 98c price
implies; at 30 minutes out, margins of 3+ are. Margins of 1-2 degF lose 7-18% of the time
at 60 minutes - far more than any 97-99c price pays for. Whether the market actually
offers 97-99c on the safe strikes (rather than 99c+, i.e. no entry) is section 4.

## 4. History with prices

**Data.** `sleeve_scan.py backfill --series KXTEMPNYCHS,KXTEMPCHIHS,KXTEMPMIAH,KXTEMPLAXHS
--events 300 --final-hours 2`: 1-minute candles over each market's whole 1-hour life for
the 300 most recent settled events per city = 12,185 markets, 1,199 events (with prices),
2026-11-03 .. 11-16. (Run here through a retrying fetcher - Kalshi answered bursts with
429; 2 markets failed.) 300 events per city was not cut; it took ~2.5 hours.
Settlements for ALL ~6,350 events since Sep 6-10 come from `hourly_temps.py settlements`.

### 4a. Unconditional - the simple proxy (strategy_check, 10-lot taker fee)

`py scripts\strategy_check.py favorites --lo 0.97 --hi 0.99 --contracts 10 --max-hours 1
[--one-per-event] --file data\sleeves\final` (and `--max-hours 0.5`, `--min-hours 0.5
--max-hours 1`). Entry = the FIRST minute a side's ask sits in 97-99c on a formed book.

| window | bets | events | losses | win % | break-even | c / contract / event [95% CI] | p | trade-confirmed: events, losses, c/ct, p |
|---|---|---|---|---|---|---|---|---|
| 0-1h, one per event | 1,199 | 1,199 | 4 | 99.67 | 98.71 | **+0.96 [+0.63, +1.29]** | **0.001** | 462, 1, +1.11, 0.015 |
| 0-1h, every market | 5,412 | 1,199 | 107 | 98.02 | 98.52 | -0.35 [-0.71, +0.00] | 0.84 | 1,096, 64, -0.37, 0.83 |
| 0.5-1h before, one per event | 1,197 | 1,197 | 4 | 99.67 | 98.71 | +0.96 [+0.63, +1.28] | 0.001 | 461, 1, +1.11, 0.015 |
| 0.5-1h before, every market | 4,776 | 1,197 | 79 | 98.35 | 98.60 | -0.18 [-0.54, +0.18] | 0.70 | 1,020, 42, -0.13, 0.63 |
| 0-0.5h before, one per event | 1,185 | 1,185 | 21 | 98.23 | 98.53 | -0.30 [-1.05, +0.45] | 0.84 | 460, 7, +0.15, 0.50 |
| 0-0.5h before, every market | 2,967 | 1,185 | 72 | 97.57 | 98.40 | -0.42 [-0.91, +0.06] | 0.88 | 956, 47, -0.85, **0.975 (loses)** |

("every market" counts each strike as an order but judges per event; p there is the
null-variance test, the one-per-event p's are exact.)

What the winning row actually buys: the first in-band quote per event comes 1-10 minutes
after the open in 84% of events (median 6 minutes), later in 11%, and in 5% before the
listed open_time (events whose markets were listed early). In 1-in-3 events several strikes are in band in that first minute;
the reported row breaks ties by ticker - other tie-breaks: highest ticker +0.65c
(p 0.024), random +0.79c (0.005), cheapest ask +0.89c (0.002), dearest +0.65c (0.019).
It is NOT a far-away strike: vs the last published value its margin is 0-2 degF in 80%
of picks, and in 163 of 1,176 picks the market's favorite is the side the last value
does NOT favor - the market's opening ladder already prices the hour's trend (e.g. NYC
5pm value 56.98, the 6pm market opened with NO on ">= 56" at 98c; it settled 54.05).

| by city (one per event, 0-1h) | events | losses | c/contract | exact p |
|---|---|---|---|---|
| NYC | 298 | 2 | +0.64 | 0.25 |
| Chicago | 299 | 1 | +0.89 | 0.12 |
| Miami | 298 | 1 | +1.00 | 0.09 |
| Coastal LA | 298 | 0 | +1.29 | 0.02 |
| 5-10pm EST hours only | 308 | 1 | +0.98 | 0.09 |
| all other hours | 885 | 3 | +0.95 | 0.003 |

The four losses: NYC Nov 4 11pm YES ">= 59" @99 (last value 60.13, settled 58.40); Miami
Nov 5 midnight NO ">= 76" @98 (76.28 -> 76.64); Chicago Nov 8 7pm YES ">= 52" @98 (55.76 ->
51.80); NYC Nov 11 3am NO ">= 50" @98 (50.23 -> 50.23).

### 4b. Conditional - her selection, using the reading (pre-declared grid, every cell)

`py scripts\hourly_temps.py report`. Reading at entry: section 3 (60 min: the last
published value, exact; 30 min: that value + the proxy stations' change, ~0.5 degF
error). Bet: among strikes whose reading-favored side has margin >= D, the one quoted
97-99c with the LARGEST margin; one per event; 10-lot. Quote read at settlement - 55 min
(60m entry) or - 30 min. "priced >99c" = events where every strike meeting the margin was
already 99c+ (no entry possible); "no reading" = first event of a run, or 1-minute
station data not yet in the IEM archive (after Nov 15 ~17Z).

| entry | D | N | wins | win % | break-even | taker c/ct | p | maker c/ct | p | trade-confirmed (N, wins) | priced >99c | no reading |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 60m | 1 | 310 | 274 | 88.4 | 98.5 | -10.10 | 1.000 | -9.93 | 1.000 | 81, 79 | 867 | 17 |
| 60m | 2 | 197 | 182 | 92.4 | 98.6 | -6.18 | 1.000 | -6.03 | 1.000 | 64, 64 | 980 | 17 |
| 60m | 3 | 96 | 91 | 94.8 | 98.6 | -3.82 | 0.998 | -3.67 | 0.996 | 32, 32 | 1,081 | 17 |
| 30m | 1 | 532 | 526 | 98.9 | 98.7 | +0.17 | 0.46 | +0.32 | 0.35 | 302, 299 | 605 | 57 |
| 30m | 2 | 189 | 186 | 98.4 | 98.7 | -0.28 | 0.77 | -0.14 | 0.71 | 115, 113 | 948 | 57 |
| 30m | 3 | 40 | 38 | 95.0 | 98.7 | -3.67 | 0.98 | -3.52 | 0.98 | 24, 22 | 1,097 | 57 |

By city (taker, wins/N, $/contract): 60m D=1 NYC 70/76 -0.065, CHI 58/74 -0.201, MIA
78/80 -0.010, LA 68/80 -0.135 - 30m D=1 NYC 104/104 +0.013, CHI 131/134 -0.010, MIA
141/143 -0.001, LA 150/151 +0.006 - 30m D=2 NYC 16/16, CHI 57/57, MIA 41/43, LA 72/73 -
30m D=3 NYC 5/5, CHI 7/7, MIA 4/6, LA 22/22. Full output incl. every loss:
`data/hourly/results/conditional.txt` on a re-run.

**Reading it.** Most of the time the market has already priced the safe strikes at 99c+
(867-1,097 of ~1,180 events, depending on the cell). What is left at 97-99c with a big
reading margin is adversely selected: the market is pricing a move the last reading has
not shown yet (morning warm-up, evening cool-down, fronts). The price-free table in
section 3 says the same from the other side - at 60 minutes a 2-3 degF margin loses 4-8%
of the time, far above a 1.5-3% price. No cell is a candidate; per the pre-declaration
none is crowned, and the best (30m D=1, +0.17c, p 0.46) is indistinguishable from zero.

## 5. Caveats

* **Quotes are not fills.** Entries are candle CLOSE quotes. Trade-confirmed = a trade
  printed at or through our price in the following minute (fixed tonight: the API renamed
  `volume` to `volume_fp`, which had silently zeroed this check in strategy_check and
  sleeve_scan). Depth behind the quote is unknown; a 10-lot may not fill at the quote.
* **Maker vs taker.** Her fills often showed no fee - resting limit orders. A maker pays
  no fee but gets filled preferentially when the price is moving against her (a resting
  97c NO bid fills when someone wants to sell NO - often because the temperature is
  heading toward the line). The maker columns above assume the same fills without the
  fee; real maker fills are adversely selected, so treat them as an upper bound.
* **Correlated strikes.** Strikes of one city-hour resolve on ONE value: 2-3 strikes on
  both sides of the reading is one bet, and when it goes wrong two of them can lose
  together. Every p here is per event. Cities on the same night share weather systems too.
* **Small, one-season sample.** Prices cover 13 days of November. The one positive row
  rests on 4 losses; 3 more would erase its significance. Settlements (no prices) cover
  2+ months.
* **The index is not reconstructable** from public data (multi-station, weekly offsets);
  the 30-minute reading is a proxy with ~0.5 degF typical error. Her app shows the real
  index (5 min delayed), so her 30-minute picks are a bit better informed than ours. The
  60-minute reading is exact.
* **History nominates, forward decides.** The positive row was found among ~12 cells
  reported here (6 unconditional + 6 conditional); its p does not survive "it was the
  best of several" without the forward test.

## 6. Pre-register the forward rule (on the Windows host - not done here)

`sleeve_paper` can express the unconditional rule exactly as tested (first in-band entry,
one bet per event, 10-lot, real fee, depth-gated). It enters on its first cycle (every
2 minutes) at which a market's favorite ask is 97c-99c; where several strikes qualify it
takes the first in API order (history: tie-break moves the edge between +0.65c and
+0.96c, all positive). Run from the repo folder:

```
py scripts\sleeve_paper.py register-rule --name HOURLY_FAV --series KXTEMPNYCHS,KXTEMPCHIHS,KXTEMPMIAH,KXTEMPLAXHS --side favorite --lo 0.97 --hi 0.995 --band any --max-hours 1 --contracts 10 --thesis "Hourly city temperature, one bet per city-hour: buy the market's first 97-99c favorite (in practice its opening ladder, minutes after open), hold to settlement. History Nov 3-16 2026: 1,199 events, 4 losses, +0.96c/contract, exact p=0.001 (trade-confirmed 462 events, p=0.015). Any-strike/any-time and reading-margin versions lose (docs/HOURLY_TEMPS.md)."
py scripts\sleeve_paper.py rules
```

(`--hi` is exclusive in sleeve_paper, so 0.995 admits a 99c ask.) Verdict bar, per
CLAUDE.md: 100+ events after registration and surviving the false-discovery correction
across all forward rules - at 24 events per city per day that is ~1 day of running.
**Run it for at least 2-3 weeks anyway** - 100 events is one weather pattern.

The conditional rules (reading margin) are NOT recommended for registration: no cell
was positive beyond noise, and sleeve_paper cannot express a reading condition. If a
forward check is still wanted, it needs a runner that reads the live index (the previous
hour's settlement value + a live station feed) - not built.

## 7. Reproduce

```
py scripts\hourly_temps.py settlements                       # all settled events -> data\hourly\
py scripts\hourly_temps.py stations                          # IEM 1-minute ASOS proxy stations
py scripts\sleeve_scan.py backfill --series KXTEMPNYCHS,KXTEMPCHIHS,KXTEMPMIAH,KXTEMPLAXHS --events 300 --final-hours 2
py scripts\strategy_check.py favorites --lo 0.97 --hi 0.99 --contracts 10 --max-hours 1 --one-per-event --file data\sleeves\final
py scripts\hourly_temps.py report                            # calibration, flip rates, conditional grid
py scripts\hourly_temps.py selftest
```

If `sleeve_scan.py backfill` stops on `429 Too Many Requests`, re-run it (resumable).
