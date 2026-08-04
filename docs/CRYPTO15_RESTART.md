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
