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

## 🟡 PROVISIONAL — positive central tendency, not yet proven

### live BTC-DAILY (kalshi_daily) — REAL account (2026-06-17)
**What it is:** the `kalshi_daily` strategy (daily BTC price contracts, `KXBTCD-*`),
run on the **live account** — this is what actually produced the "$50→$280"-type
run, NOT weather. Distinct from BTC short-horizon (above): the 1-day horizon has
far better signal-to-noise.

**Verdict: PROVISIONAL edge — keep, but do NOT scale up.** Measured on real
settled positions via `kalshi_live_psr.py`:
- 68 settled positions, 10 days (2026-05-24 → 06-06), realized **+$84.94**, 21W/47L (31% WR).
- **per-day PSR 0.64** (above the 0.50 "probably positive" bar, below 0.95 "evidence-backed").
- **MinTRL 196 days** — *finite*, so the central tendency is POSITIVE (unlike weather-fade's ∞). But with 10 days collected you're ~5% of the way to statistical certainty.
- Cheap-longshot shape (31% WR, result carried by a few big winners) — at n=10, real edge and lucky streak look identical.

**Disposition:** the most promising signal found so far. It's been OFF since ~06-06
(broken `kalshi_daily` agent, exit 127). To resolve edge-vs-luck it must keep
running — but at **psr_gate-governed fractional size** (PSR 0.50–0.95 → "provisional"
tier → ~0.40 Kelly cap), NOT scaled up on 10 days. Re-run `kalshi_live_psr.py psr`
periodically; watch per-day PSR move toward 0.95 (real) or back below 0.50 (variance).
Reconcile: settled P&L +$84.94 vs the remembered ~+$230 (deposits / open positions?).

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
- **OPEN QUESTION — live BTC-daily stopped settling ~2026-06-06.** Cause NOT yet
  confirmed. Candidates: the order-placing agent broke (a missing local launcher
  `run_kalshi_daily.sh` — never in git — breaks `kalshi_daily`/`_conservative`
  with exit 127), OR `kalshi_daily_hermes` is in `review` (shadow) mode, OR
  capital is tied up in open positions. Check with `kalshi_live_psr.py account`
  (balance/positions, server-side, no TCC) + the Desktop logs (native terminal).
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
