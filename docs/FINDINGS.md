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

### BTC SHORT-horizon (5-min / hourly) — earlier
**Verdict: efficient market, no edge.** Tested and ruled out — at minute/hour
horizons the market prices the available data. NOTE: this is the *short* horizon
only; BTC *daily* is a different animal — see PROVISIONAL below.

---

### live BTC-DAILY (kalshi_daily, KXBTCD) — REAL account (2026-06-17)
**Verdict: RULED OUT — losing.** The earlier "+$84.94 / PSR 0.64" was a *combined*
account figure and was a **Simpson's paradox** — averaging a winning weather family
with a losing BTC family. Split by family (`kalshi_live_psr breakdown`): BTC-daily is
**−$91.05, 18% WR, per-day PSR 0.02, MinTRL ∞** — a structural loser, same verdict as
weather-fade. The `kalshi_daily`/`kalshi_daily_hermes` agents staying paused is CORRECT;
do NOT resume BTC. (Balance gap to ~$280 = user-confirmed withdrawals, not drawdown.)

---

## 🟡 PROVISIONAL — positive but not yet proven (needs stress-testing)

### live WEATHER — REAL account (2026-06-17)
**What it is:** the live weather family (`KXHIGH*`/`KXLOW*`) on the real account — the
strategy that ACTUALLY made the live money (user recalled this correctly). NOTE: NOT
necessarily the same as the paper `weather-fade` we ruled out — must identify it.

**Verdict: PROMISING but UNCONFIRMED — do not crown or scale yet.**
- 33 settled positions, 8 days, realized **+$186.06**, 45% WR, **per-day PSR 0.97**, MinTRL 6.
- ⚠ **Concentrated:** +$154 of the +$186 came from a 3-day run (May 28–30). PSR 0.97 on
  8 days carried by 3 is the same "one hot run" shape that fooled us on weather-fade —
  MinTRL 6 is deceptively low (it assumes the lucky Sharpe is the true Sharpe).
- ⚠ **Strategy not yet identified.** If these were mostly **NO-side** buys it's the
  weather-FADE family we ruled out on 187 paper trades (→ the larger sample wins, this
  is a lucky window). If mostly **YES-side**, it's a different, directional strategy
  (genuinely untested). Resolve with `kalshi_live_psr breakdown --family WEATHER`
  (side Y/N + per-position tickers) and `--since 2026-05-31` (does it survive without
  the 3-day run?).

**Disposition:** the best live lead, but treat exactly like weather-fade until proven —
identify the strategy, check concentration, then judge by per-day PSR on the un-lucky
subset. Do NOT scale on 8 concentrated days.

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

## 🗺️ OPERATIONAL MAP (read before debugging agents)

- **Two checkouts.** `~/Desktop/projects/polybot` is the **LIVE** tree — it holds
  the `.env` with Kalshi creds and the running launchd agents. It is
  **TCC-protected**: a sandboxed assistant shell gets "Operation not permitted"
  reading it (only `ls` metadata works). To let the assistant read it, grant the
  terminal/Claude **Full Disk Access**; otherwise diagnose live issues in a
  native Terminal. `~/polybot-backtest` is the **readable analysis/data mirror**
  (now also has a read-only Kalshi key in its `.env` for `kalshi_live_psr`).
- **CONFIRMED PAUSED — live BTC-daily stopped trading ~2026-06-06.** `account`
  check (server-side, TCC-free) shows **$63.33 cash, 0 open positions** — so it's
  not "holding," it simply stopped placing trades. WHY is still open (bug vs
  `review`/shadow mode vs a deliberate risk-off) — needs the Desktop logs via a
  native terminal. Balance reconciled: the $63.33 vs +$84.94 gap is
  **user-confirmed WITHDRAWALS**, not a drawdown — so the measured edge stands.
- **OPEN — was it BTC or WEATHER (or both) that made the live money?** User recalls
  *both* weather and BTC were profitable live, and that BTC "wasn't making money"
  right before the stop. The +$84.94/PSR-0.64 figure is COMBINED and the sample
  looked BTC-only — split it with `kalshi_live_psr.py breakdown` (per-family P&L +
  per-day PSR + daily timeline) before crediting either strategy.
- **DO NOT unload the exit-127 agents** (`kalshi_daily`, `kalshi_daily_conservative`,
  `trade`, `monitor`, `harvester`, `weather_daily`) until the above is resolved —
  one of them may be the *broken live trader*, not a harmless orphan. Removing it
  would delete a fixable earner. Cleanup is cosmetic; correctness first.

---

## 🔭 SURVEYED, NOT YET TESTED

`kalshi_survey.py` ranks catalog families by where a data-processing edge is
*possible*. Top untested CANDIDATEs (frequent + data-predictable + not already
efficient): **Economics**, **Companies**, **Transportation**. EFFICIENT (Crypto,
Financials) and THIN (Entertainment, etc.) families are not worth forward-collecting.
