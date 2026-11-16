# Kalshi HOURLY temperature markets - the 97-99c "both sides of the reading" method

Research only. Public endpoints, no credentials, no orders. Built 2026-11-16 overnight.

The method under test (seen in a trader's portfolio): in a city-hour market late in the
hour, buy 2-3 strikes on BOTH sides of the current temperature - e.g. Coastal LA 10pm
with the temperature near 68: YES ">= 66", YES ">= 67", NO ">= 69" - 10 contracts each at
97-99c, for $0.09-0.30 profit each. ~34 positions seen, all wins; many fills had no fee
(resting limit orders = maker).

**Status of this file:** sections 1-3 are final. Section 4 (prices) is filled in when the
candle backfill finishes - see the bottom of the file.

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

(pending: candle backfill of the most recent events per series is running)
