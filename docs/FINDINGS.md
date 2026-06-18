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
- **Confirmed dead LIVE too (2026-06-17):** the live account's apparent weather win (+$186 / "PSR 0.97" over 8 days) was THIS strategy — **30/33 positions were NO-side buys = weather-fade.** It was ~4 longshot wins in `KXTEMPNYCH` (NYC hourly temp, since **DELISTED** by Kalshi); `breakdown --since 2026-05-31` strips them → **+$8.26, n<5, no measurable edge.** 33 lucky live trades can't override 187 paper trades — larger sample wins, and the profitable venue no longer exists.

**Disposition:** code retained as a documented negative result; do not feed it
capital. Reproduce with `weather_fade.py analyze --validated-only --psr`.

### BTC SHORT-horizon (5-min / hourly) — earlier
**Verdict: efficient market, no edge.** Tested and ruled out — at minute/hour
horizons the market prices the available data. NOTE: this is the *short* horizon
only; BTC *daily* (KXBTCD) was tested live and is **also ruled out** — see below.

---

### live BTC-DAILY (kalshi_daily, KXBTCD) — REAL account (2026-06-17)
**Verdict: RULED OUT — losing.** The earlier "+$84.94 / PSR 0.64" was a *combined*
account figure and was a **Simpson's paradox** — averaging a winning weather family
with a losing BTC family. Split by family (`kalshi_live_psr breakdown`): BTC-daily is
**−$91.05, 18% WR, per-day PSR 0.02, MinTRL ∞** — a structural loser, same verdict as
weather-fade. The `kalshi_daily`/`kalshi_daily_hermes` agents staying paused is CORRECT;
do NOT resume BTC. (Balance gap to ~$280 = user-confirmed withdrawals, not drawdown.)

---

## 🟡 PROVISIONAL — none

The live "WEATHER PSR 0.97" lead was investigated and **collapsed** (it was
weather-fade catching a lucky window in a now-delisted market — see weather-fade
above). After this session, **nothing on the live account is a proven or even
provisional edge.** The entire +$84.94 realized was a handful of weather-fade
longshot wins a larger sample says won't repeat; pausing on 2026-06-06 was, in
hindsight, the right outcome. Do not un-pause anything.

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
  native terminal (TCC blocks the assistant). Balance reconciled: the $63.33 vs
  +$84.94 gap is **user-confirmed WITHDRAWALS**, not a drawdown.
- **RESOLVED — it was WEATHER that made the live money, not BTC.** `breakdown`
  split it: WEATHER +$186 (weather-fade longshots, now-delisted KXTEMPNYCH),
  BTC-daily −$91, ETH ~0. Both correctly ruled out (see above) — so the *why it
  paused* question is now moot for action: there's nothing here worth resuming.
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
