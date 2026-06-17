# FINDINGS — what we tested and what survived

A durable, cross-session record of edges investigated on this repo: the verdict,
the evidence, and the date. The point is to **not re-litigate dead strategies**.
Negative results are kept deliberately — ruling an edge out cheaply is a win.

**Standard of proof:** an edge is judged by the **per-DAY** PSR (Probabilistic
Sharpe Ratio), not per-trade — a day's correlated positions are one independent
observation, not many. PSR < 0.50 = not even probably positive · 0.50–0.95 =
provisional · ≥ 0.95 = evidence-backed. MinTRL = ∞ means the central tendency is
non-positive, so no amount of additional data rescues it. (See
`lib/hermes_significance.py`.)

---

## ❌ RULED OUT

### weather-fade — price-calibration / favorite-longshot fade (2026-06-17)
**What it was:** fade "overpriced YES" daily high-temp favorites — buy NO when
the becker_edge empirical price→outcome calibration says YES is too high; hold to
settle. Validated in backtest (`becker_edge.py`): 13 months, 8 cities, OOS
+0.17–0.28/contract, monotone in threshold.

**Verdict: NO MEASURED EDGE — do not allocate.** It did not survive live paper fills.
- Full live book (187 settled fades, 9 distinct days): **per-day PSR 0.18, MinTRL ∞**, 46% WR vs 47% breakeven.
- Restricted to the **8 pre-validated cities** (a scope fixed *before* this live data, via `VALIDATED_SERIES` — a genuine pre-registered OOS test, not a post-hoc slice): **per-day PSR 0.24, MinTRL ∞**, 48% WR vs 51% breakeven — *worse*, not better.
- The apparent "+EV" in the full run was **unvalidated cities running lucky** (SFO/MIN/DC/ATL, all outside the proven 8). No reason unproven cities should out-earn proven ones except noise.
- **Conviction inverted** vs the backtest: bigger calibration edge did *worse* live (0.08–0.12 bucket at 20% WR), the opposite of the backtest's monotone-in-threshold. Strong sign the backtested edge wasn't real on live fills.
- **Directional, not a pricing edge:** loses on high-YES-rate (hot) days, wins on cool days — the P&L tracks the temperature regime, not mispricing.

**Disposition:** code retained as a documented negative result; do not feed it
capital. Reproduce with `weather_fade.py analyze --validated-only --psr`.

### BTC short-horizon (earlier)
**Verdict: efficient market, no edge.** Tested and ruled out — the market prices
the available data. The disciplined "don't build for a phantom edge" outcome.

---

## ⏳ IN FLIGHT (unproven, NOT disproven — judge by the same per-day PSR bar)

- **fc2s** — forecast two-sided (bets on *forecast skill*, a different hypothesis
  than the price-fade above). Thin so far; the early "+$5.43" was NY-on-one-day,
  never significant. Keep accumulating; judge by per-day PSR.
- **ensemble_collect** — forward A/B of an ensemble forecast vs flat σ=3. Still
  accumulating settled rows; needs ~200+ over ~15–20 days before `eval`.
- **bucket_arb** — structural (prediction-free) arbitrage on Kalshi
  mutually-exclusive ladders. Opt-in; collects the near-miss margin distribution.
  Real locks are expected to be rare; the question is whether they ever appear
  *and* fill. (Scans Kalshi ladders only — NOT perpetuals.)

---

## 🔭 SURVEYED, NOT YET TESTED

`kalshi_survey.py` ranks catalog families by where a data-processing edge is
*possible*. Top untested CANDIDATEs (frequent + data-predictable + not already
efficient): **Economics**, **Companies**, **Transportation**. EFFICIENT (Crypto,
Financials) and THIN (Entertainment, etc.) families are not worth forward-collecting.
