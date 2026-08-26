# 15-Minute Crypto Restart — Decision Record (2026-08-04)

The restart's foundation, per the operator's directive: **rebuild from "how we justify a
YES or NO"** — not from a signal. This file records what the math and the adversarial
council decided, so future sessions don't re-litigate it.

## What is settled (do not re-open without new evidence)

1. **15-min direction prediction is dead.** Killed twice: the original 7-vote confluence
   composite (33 settled paper trades, 48.5% WR, −$30.61, composite anti-predictive) and
   a fresh 196-bar tape check on 2026-08-04 (P(up|up)=0.43, P(up|dn)=0.52, AC(1)≤0).
   No indicator composite gates money. Ever.
2. **The justification engine is the foundation** — `lib/binary_justify.py`: a bet exists
   only as a machine-logged gate trace (G1 measurement → G2 net edge → G3 named mechanism
   → G4 calibration-must-beat-price → G5 blind zone → G6 staleness → G7 event blackout).
   24 tests. Every evaluation (traded or not) accrues to the calibration ledger.
3. **Council verdict on the taker longshot-fade: RESHAPE** (scores 3/8/6/6). It does NOT
   transfer from weather as-is: (a) EWMA vol is not a private measurement — the far-strike
   quoter is a professional MM with a better vol model, and our Gaussian/t tails are most
   wrong exactly at tradeable strikes (fresh tape: |move|>50bps = 2.0% vs 0.36%
   Gaussian-predicted); (b) the favorite-longshot premium accrues to the POSTER of the
   overpriced offer, not a taker hitting it.

## The two live hypotheses (in priority order)

### H1 — BRTI settlement tracking (the measurement candidate)
Kalshi BTC markets settle on the CF Benchmarks BRTI **60-second average**. In the final
minutes the settlement value is *partially realized* — tracking the accumulating average
is a MEASUREMENT of the settlement variable itself, the true weather-nowcast analog
(harvested from papabrosio/kalshi-btc-15min-trader's design; that repo's ML/threshold
trading logic is unvalidated and NOT adopted). Honest caveats: the measurement is public,
MMs compute it faster, and near-settlement fills at fair prices may not exist — which is
exactly what Stage 0 must measure before any model or money.

### H2 — Far-strike vol premium (only as MAKER, only after Stage 0)
If Stage 0 shows Kalshi 15-min longshots trade above realized settlement frequency by
more than friction + a fair jump premium, the harvest is posting offers (maker), never
taking. Without that bucket-study gap, G3 fails every trade by construction.

## Stage 0 — the $0 test that decides everything (~2 weeks)
Log every 15-min crypto market every cycle: contract price (tradeable bid AND ask),
book depth, spot, time-to-expiry, plus final-minute BRTI partial-average vs market price;
join to settlements. Deliverables: (a) price-bucket vs realized-frequency table (n≥1500)
— does ANY bucket misprice beyond friction?; (b) final-minute mispricing distribution —
does the market lag the partially-realized BRTI?; (c) fill-realism (book depth at the
prices any edge would need). Parameters frozen before logging starts; calibration
stratified by price bucket; no tuning mid-sample (the composite died of iterative tuning).

## The TradingView Pine Script (`tools/tradingview/kalshi_15m_intel.pine`)
The operator's 7-vote confluence panel, ported to Pine. Role: **monitoring and regime
awareness ONLY** — the chart panel, state classifier (UPTREND/DOWNTREND/RANGING/STABLE),
vol regime, and alerts are eyes on the market and a legitimate G7 input (e.g. treat High
volatility / state flips as blackout context). Its direction votes NEVER gate money —
that composite is the exact architecture the ledger killed. The justification engine is
the only money gate.

## Is there an "ultimate source" of trade-placement logic? (asked 2026-08-12)

**No — structurally.** A downloadable profitable mechanism is a near-contradiction: an
edge is a disagreement with the market price, and once logic is public, the price absorbs
it. We ran this experiment twice: the downloaded-pattern 7-indicator composite (lost,
anti-predictive) and the papabrosio repo (no backtests, unsupported claims; only its BRTI
settlement-tracking *idea* survived review). What IS permanently downloadable is the math
that survives publication BECAUSE it isn't a tradeable secret: option pricing, Kelly,
calibration scoring, the peer-reviewed favorite-longshot bias. All of it is already
implemented here. The closest thing to an ultimate source for THIS venue is the dataset
our own collector builds — minute-by-minute books + settlements that nobody else hands out.

## Does openclaw-wheel-trader code apply here? (same session)

**It already was applied — polybot IS the openclaw clone.** Evidence: openclaw's
PREDICTION_MARKET_RESEARCH.md (2026-04-15) states its purpose as "research to inform
cloning openclaw-wheel-trader for prediction market trading"; 11 lib modules are shared by
name/lineage (audit, calibration, circuit_breaker, backtest, forecaster, market_scanner,
dashboards...); tradingcore is the extracted shared core. The deep structural parallel is
real and already exploited: a Kalshi binary is a digital option, and our favorite-side
rules are short-vol premium harvesting — the same trade as the wheel's cash-secured puts
(frequent small wins, occasional large loss). The wheel's CSP scoring (rank candidates by
premium richness vs risk) is convergently the same design as our fair-value strike
selection. Remaining port candidate: the Hermes optimizer PATTERN (disciplined post-data
parameter updates with bounds + audit trail) — but only AFTER the 600-window gate, and
subordinated to walk-forward validation, because openclaw's own git log shows the failure
mode ("HARD REVERT to winning-era config", threshold churn) that killed our composite.
Volume + OI are already logged in every Stage-0 observation for future pre-registered
hypotheses.

## FINAL VERDICT (2026-08-27, 67-day backfill: 12,821 independent windows)

The deep-history analysis at ~7x the measured discrimination threshold:

  TAKER : 0 HELD. The three "persist-but-thin" residuals are +0.6c to +2.2c per bet -
          inside friction and inside the CI even at n=2,780 per cell. Every cell with a
          large gap earlier flipped or shrank as n grew. No cell was tradeable in June,
          July, or August - the market did not decay from an earlier edge; it was
          efficient for its entire recorded life.
  MAKER : replicated at 72,452 postings - every fillable cell "maker -EV even filled",
          both fill bounds, all bands, all 67 days. The adverse-selection measurement
          (fills happen exactly when the market has learned something bad about your
          side) is structural, not a three-week fluke. (The <2min band shows 0% fills in
          backfill data - a granularity artifact of minute-close candles, not a finding;
          the live-collector replay covered that band and it was negative there too.)

CASE CLOSED for KXBTC15M/KXETH15M at retail latency: no taker edge, no naive-maker edge,
never was one. Total cost of this certainty: $0 risked, ~3 weeks of unattended collection,
one 2.5-hour backfill. The frozen paper rules keep running as a zero-cost control; no
further engineering is justified against this venue short of professional market-making
infrastructure.

THE REFRAME THAT SURVIVES: history_backfill + export + edge_analysis is a SAME-DAY VENUE
SCREENER for any Kalshi series with candle history. The three-week question "does this
venue have an edge?" now takes one afternoon per series. The path to winning is running
that screen across venues until the math finds another weather - the one venue where this
exact methodology DID find a real, disciplined edge.
