# GitHub Edge Scan — verified verdicts (2026-08-25)

Five search agents (Kalshi tooling, market-making, validation math, crypto vol,
prediction-market research) surfaced 29 candidates; 25 unique; the top 8 each got an
adversarial verification agent instructed to default to REJECT and to read actual source,
not READMEs. All 8 survived with APPLY. That unusual hit-rate is itself informative: the
searchers were pre-filtered by the same skepticism (the "99%-accuracy, 6-commit" lesson
is written into the search prompts).

## Applied immediately (in this repo now)

1. **ccxt/ccxt — Kalshi candlesticks integration** (MIT, 43k stars, verified at source:
   `python/ccxt/prediction/kalshi.py`). Public endpoint
   `GET /series/{series}/markets/{ticker}/candlesticks?period_interval=1&start_ts&end_ts`
   serves 1-minute candles for settled markets; `GET /markets?status=settled` paginates
   with cursors; a `historical/*` endpoint family suggests old data migrates rather than
   vanishes. → `scripts/history_backfill.py`: `probe` (retention gate on one old ticker —
   the single unverifiable-remotely fact), `run` (resumable backfill of settled
   KXBTC15M/KXETH15M windows + results), `export` (stage0-format, provenance-tagged
   `src:backfill`, SEPARATE file — candle-derived quotes are minute-OHLC approximations,
   never silently mixed with collector snapshots). Potentially months more history ≈ 20x
   our dataset.

2. **nkaz001/hftbacktest — queue-position fill semantics** (MIT, Rust core, verified:
   `src/backtest/models/queue.rs`). Its core premise: with queue position unknowable,
   honest maker replay must report a BAND. → `maker_replay.py` now reports dual bounds:
   `$/post[T]` (touch: ask <= our bid, optimistic front-of-queue) vs `$/post[C]` (strict
   cross: ask < our bid, even the back of the queue filled). A maker cell is credible
   only when [C] is also positive.

## Apply next (gated, in order)

3. **Jon-Becker/prediction-market-analysis** (MIT, 3.8k stars, cited by academic papers;
   verified: full-trade indexer with resume, markets parquet carries settlement
   `result`). 36GiB parquet dump of Kalshi markets+trades. GATE before relying on it:
   check the crypto-15min series actually appears with real date coverage (DuckDB query
   over the markets parquet). Trades+markets only — no order books, so it extends
   settlement/price-print history, not the maker quote replay.

4. **bashtage/arch** (huge, standard econometrics lib). Hansen's SPA test +
   stationary/block bootstrap → the correct multiple-testing check across our 60-cell
   grid and honest CIs under correlated windows. Wire into edge_analysis as an optional
   `--spa` pass once deep history lands (more data first, better stats second).

## Maker-phase references (when maker feasibility screens positive)

5. **hummingbot** (Apache-2.0): the Avellaneda-Stoikov strategy internals + the
   trading-intensity estimator (fit lambda(delta)=alpha*exp(-kappa*delta)) — a
   fill-probability-vs-distance model far better than our binary bounds, portable once
   we quote for real.
6. **warproxxx/poly-maker** (production Polymarket binary-market maker): depth-weighted
   microprice fair value + inventory reservation pricing; the closest working analogue
   to what a Kalshi 15-min maker would be. Check license before porting code; ideas free.
7. **rodlaf/KalshiMarketMaker**: the one credible Kalshi-native A-S maker; reference
   architecture for market selection (volume/spread screens) + per-market quoting worker.
8. **routsiddharth/vela** (NO LICENSE — reimplement from scratch, never copy): stat-arb
   on exactly KXBTC15M/KXETH15M; its core idea — reconstruct the 60s settlement TWAP in
   real time and de-bias exchange-feed vs Kalshi-settlement differences with a trailing
   median — is the best concrete design for a settlement-aware fair value near expiry.

## Standing conclusion

Nothing in 29 candidates contradicts our finding that taker strategies lose spread+fees
here; the two credible Kalshi-native projects are BOTH makers. The scan's real yield is
data depth (backfill, dump), honesty upgrades (fill bounds, SPA), and a ready-made maker
playbook for if/when the feasibility screen says the spread is harvestable.
