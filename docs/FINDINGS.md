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
  never significant. Keep accumulating; judge by per-day PSR. ABOVE-strike (tail)
  trades stay VETOED (σ=3 claimed ~73% exceedance, realized ~18%).
- **fc2s_shadow** (NEW) — measurement-only collector that re-enables the vetoed tail.
  Decoupled from trading (never books, own ledger): logs the day-ahead forecast high
  per city-day, fills the realized high after, so the (forecast, realized) error
  distribution accrues. `report` shows measured bias/σ and an exceedance-calibration
  table — model(σ=3) vs realized vs a recalibrated(bias+σ̂) prediction — so the tail
  can be re-enabled with measured params once recal≈realized. Runs hourly via the
  weather-fade installer (collect :34, settle :48). selftest green.
- **ensemble_collect** — forward A/B of an ensemble forecast vs flat σ=3. Still
  accumulating settled rows; needs ~200+ over ~15–20 days before `eval`.
- **bucket_arb** — structural (prediction-free) arbitrage on Kalshi
  mutually-exclusive ladders. Opt-in; collects the near-miss margin distribution.
  Real locks are expected to be rare; the question is whether they ever appear
  *and* fill. (Scans Kalshi ladders only — NOT perpetuals.)
- **sports_eval** (NEW) — the shared scorer for BOTH sports sleeves. Dedupes each
  signal log to one paper position per market, settles via Kalshi market result
  (cached in `data/sports_eval_resolutions.json`), then reports per-day PSR / DSR /
  MinTRL (lib/hermes_significance) and win-prob calibration (Brier + reliability
  table), gross and net of the taker fee. `python scripts/sports_eval.py eval`.
  selftest green; confirms a coin-flip is NOT significant and a 98%-calibrated edge
  is. The honest gate before any real money — collect signals, then judge.
- **devig_check** (NEW) — the second sports edge, runs ALONGSIDE the lock: strip the
  vig off a sharp book's (Pinnacle) 2-way moneyline → fair prob, flag when Kalshi's YES
  diverges by ≥ `MIN_EDGE` (net of the taker fee). Devig four ways
  (multiplicative/additive/power/Shin) for a robustness BAND — skip when the methods
  disagree (`band > MAX_BAND`). Works the *whole game* (not just garbage time) and
  doubles as a second opinion on lock signals. Odds via The Odds API (`ODDS_API_KEY`,
  free tier); Kalshi ladder + team-match reused from `sports_lock`. **Unproven** — the
  edge assumes Kalshi is wrong *relative to the sharp book*, which it may not be; that's
  why it's paired with pypbo/netcal (validate by per-day PSR on `data/devig_check.jsonl`
  before real money). selftest green. Paper only.
- **sports sleeves — all-sports sweep** (NEW): both `sports_lock` and `devig_check`
  now default to scanning EVERY live per-game series. `scan`/`probe` with no league
  auto-discover via Kalshi `/series?category=Sports` (`discover_game_series`) and map
  each to a league (`infer_league`); no hardcoded tickers, auto-adapts to season.
  `--confirm` is now per-league lenient (confirmable leagues gated on a 2nd feed, others
  single-source) so the sweep covers every sport. devig adds **mlb** (no clock needed);
  the lock skips mlb. launchd installer simplified to three agents (lock sweep / devig
  sweep / eval) with no PAIRS config. Watch the Odds API quota: a devig sweep costs ~1
  credit per live league per run.
- **sports_lock — two-source confirmation gate** (NEW): `scan --confirm` only fires a
  lock when an INDEPENDENT-origin feed (NBA.com CDN / NHLE, different servers from ESPN,
  no new deps) agrees on score + period + clock (within `CLOCK_TOL_SEC`). Kills the
  lock's worst failure — acting on one glitchy live number. Default behavior unchanged
  (opt-in). `reconcile()`/`_iso_clock_to_sec()` are pure and selftested; the live
  secondaries need local verification (sandbox blocks network). Deliberately did NOT
  swap to sportsdataverse-py: it wraps the *same* ESPN endpoint we already poll, so it
  adds a dependency with zero independence gain — an independent origin is the real win.
- **sports_lock** (NEW) — the sports sibling of `asos_tracker`: a live-score LOCK,
  not a forecast. Late in a game a lead becomes mathematically near-safe
  (`P = Φ(margin / (σ_league·√time_left))`) well before the moneyline crawls to 99¢;
  log when a *near-locked* winner is still mispriced. This is the user's "sports
  market determined by stats and numbers." Free data via ESPN's public scoreboard
  JSON (no key); Kalshi side via the repo client. **Unproven** — same bucket-lock
  open question as weather: does the gap *fill* before settlement, net of fees, and
  is the ESPN↔Kalshi team mapping right (run `probe` first)? Judge by per-day PSR on
  `data/sports_lock.jsonl` once forward-collected. Paper only.
- **asos_tracker / bucket-lock** (STOOD UP 2026-06-21) — the weather observation edge,
  the legitimate version of "we can see the temp beforehand." NOT a forecast and NOT
  satellite: reads the REALIZED daily high off the EXACT ASOS station Kalshi settles on
  (Iowa Environmental Mesonet, free) and, once the high is physically locked (evening,
  temps fallen ≥2°F off peak), flags the now-near-certain bucket the thin overnight book
  still misprices until the 11:59pm ET cutoff. **Why gate-tuning the forecast sleeves
  won't add wins:** once the temp is observable the *market sees it too*, so the
  day-ahead-only rule in weather_fade/fc2s is correct — the edge that survives observation
  is this structural/speed play, not a sharper forecast. **Station map VERIFIED** against
  `_cities()` settlement coords + `weather_daily_signal.DAILY_CITIES`: Chicago corrected
  MDW→**ORD** (both sources put it at O'Hare); DFW (not DAL) and IAH (not HOU) confirmed.
  Now has `scan`/`settle`/`report` (per-day PSR/DSR, hit-rate vs the 98% target — below it
  the lock was *wrong*, not unlucky), a dashboard (`asos_dash`, :5058), and a launchd
  installer (`install_asos_agents.sh`: scan 30 min, settle hourly). **Unproven** — the
  risks the scorecard can't see are CLI revision near a bucket edge and overnight
  fillability; forward-collect, judge by DSR≥0.95 AND hit-rate≥98%. Paper only.

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

## 🔭 SURVEYED → BLOCKED ON LIQUIDITY

`kalshi_survey.py` ranked the candidate families; drilling into them (`--drill`,
`--markets`) revealed the binding constraint is **liquidity, not predictability**:
- **Transportation** — right cadence (flight delays daily) but **open=0/vol=0**, untradeable.
- **Companies** — structurally one-off events (single resolution → PSR can't accumulate).
- **Economics** — richest + only family with *listed* daily markets. The lead,
  **KXAAAGASD** (daily gas, forecastable AAA underlying), has 15 listed strikes but
  **every one is unquoted (ybid/yask=None, vol=0)** — listed ≠ tradeable.

**The pattern across the whole search: liquid Kalshi markets are efficient (weather,
BTC-daily had fills, no edge); forecastable ones are illiquid (gas, transport).** That's
a fundamental constraint, not a tooling gap — consistent with sports being ~80% of
Kalshi volume and these niches a rounding error.

**Parked (free probe):** `series_collect KXAAAGASD` runs every 3h to answer two questions
over time — does a quote *ever* appear, and does AAA settle predictably within the strike
grid? Not a found edge; a long-shot "does liquidity show up" watch. Do NOT spin up parallel
collectors on the open=0 families — collecting price→outcome where there are no prices is
motion without progress.

**If hunting continues, the liquidity is in Sports/Politics** (survey: SHARP — pros arb
them) — a different, harder game we have not attempted.

---

## 🔬 DEEP RESEARCH (2026-06-18) — where consistent edge actually is

5-angle multi-source research (BTC short-horizon, weather, academic FLB/efficiency,
Kalshi market-making, practitioner reports). Strong cross-validated convergence on
ONE theme:

**The consistent edge is MAKER-SIDE / variance harvesting — NOT prediction.**
- Kalshi favorite-longshot "behavioral surplus": takers buy YES ~61% of the time but
  YES wins only ~32%; **makers earn ~2× per contract net of adverse selection**
  (Bartlett & O'Hara, 41.6M trades, SSRN 6615739). Maker fee ≈ ¼ taker (often 0% on
  standard series); resting/cancelling is free.
- Direction-forecasting is efficient or a latency war *everywhere we looked*: BTC
  final-second BRTI settlement = HFT; weather forecast-bots overfit (86%→60% live).

**BTC-15min:** no documented consistent post-fee retail edge. Reliable money (final-
second settlement, cross-exchange basis) is HFT, not retail. Only retail-plausible
angles: take *near-decided* contracts at low-fee **price extremes** (fee→0 as P→0/1)
via your own running 60-s BRTI average; and volatility-reversion (fade intra-window
panic). Thin, crowded. Maker-ing BTC = worst HFT adverse selection. → not where to focus.

**Weather — the one NEW documented idea: sell over-priced UNCERTAINTY.** Kalshi temp
ladders price ~**1.27× more uncertainty than forecasts actually carry** (Oalkhadra,
1,911 city-dates, Diebold-Mariano p=0.006) → fade the over-wide tails (variance harvest,
NOT direction — *different* from the ruled-out calibration fade). Plus intraday
"bucket-lock" (buy the near-certain bucket after the afternoon high physically realizes,
until the 11:59pm ET cutoff; structurally real but unmeasured + CLI determination risk).
Only ~4 liquid cities (NYC/CHI/MIA/AUS), $10K–500K/day.

**Universal caveats (cross-validated, high confidence):** the ~7% taker fee
`ceil(0.07·C·P·(1-P))` brutally taxes *low-priced* contracts (one trader went 0/32 and
quit over it) — so any play must avoid cheap contracts / be maker-side; **overfitting is
THE retail failure** (Sharpe 2.0→0.5 backtest-to-live — validates our PSR/DSR discipline);
adverse selection from bots (~89 trades/day vs human ~2.2) picks off slow manual quotes;
**no audited retail Sharpe exists** for any of these (all bot READMEs are illustrative);
sports ≈ 80% of Kalshi volume, the data-niches are a rounding error.

**Skeptic flags:** treat the one repo's Sharpe 4.9 / 49.8% as **unbankable** ($1 sizing,
$0.02 assumed spread, self-reported, thin books) — the *idea* (1.27× over-pricing) is more
credible than the *P&L*. Maker rebate program (~1%, ~$7k/wk) needs application/approval.

**Recommendation:** the single new, documented, discipline-fitting hypothesis is the
**weather uncertainty/tail-fade on the 3-4 liquid cities** — forward-test paper-only,
judge by per-day PSR **and DSR**, expect it likely washes out (base rate = 0 so far).
Maker-ing proper is the academically strongest edge but needs automation + inventory
control + a volume-earned rate-tier — a bigger, riskier build, dubious on a $63 account.

Sources: Bartlett & O'Hara SSRN 6615739; Whelan "Makers and Takers" (Kalshi); Snowberg &
Wolfers (FLB); Oalkhadra GitHub (uncertainty 1.27×, DM p=0.006); Kalshi NHIGH.pdf (CLI
settlement); Turbine (overfitting + 1000-strategy backtest); kalshibacktest.com (BRTI).


---

## 🔬 DEEP RESEARCH ROUND 2 (2026-06-18) — Kalshi SPORTS (the liquid frontier)

5-angle research into whether sports (the liquid, retail-heavy ~80% of Kalshi volume)
offers a retail-*makeable* edge. Verdict: **no free strategy; only structural edges,
none accessible to a small/manual account.**

- **Fade-the-public is a myth** (Winkelmann 2024: biases non-persistent; Levitt: books
  already shade lines to price the public side worse). The only surviving signal is
  **closing-line value (CLV)** — thin (~+3-4% over 20k+ bets) and requires genuine
  handicapping skill.
- **Sports are well-calibrated at game time** (292M-trade calibration study, arXiv
  2602.19520: slopes 0.90-1.10, 0-48h pre-resolution). Underconfidence only in
  long-horizon futures.
- **The favorite-longshot behavioral surplus is real but already harvested by ~2,000+
  bots/makers** (~95% of fills); it's strongest in POLITICS, not sports. Average taker
  loses ~32%; only ~5-13% of participants are net profitable (matches the <5% who beat
  the close in sports betting).
- **Market-making sports is NOT retail-feasible**: needs full automation + news/vol
  kill-switch + low-to-mid 4-figure capital + a volume-earned API tier; Kalshi's order
  book never pauses for in-game events (worse than Betfair's protective delay), and
  ~95% of Betfair sports traders lose.
- **The genuine structural advantages** — Kalshi's ~3-pt-lower cost (~0.85-1.5% vs
  ~4.5% sportsbook vig) and **no winner-limiting/banning** — only AMPLIFY a pre-existing
  edge (handicapping or making); they don't create one. "Low vig ≠ +EV."
- **Extra retail risks**: settlement at "last fair price" not void (one trader lost
  ~$30k), and regulatory void risk (state injunctions).

**The ONE cheap, falsifiable, in-our-wheelhouse residual:** an analyst claim that Kalshi
sports are efficient at 95-99% but **miscalibrated in the 90-93% favorite band** (public
overpays for blue-blood/favorite certainty → fade). Calibration-shaped → testable on
historical sports settlements with becker_edge / series_collect + PSR/DSR, no capital or
automation. Most likely already arbed; cheap to check.

## 🧭 OVERALL VERDICT (both research rounds + the whole empirical session)

There is **no free, retail-feasible, small-account edge** anywhere we looked — weather,
BTC (short & daily), gas, transportation, sports. Durable edges are **structural**:
(1) maker-side liquidity provision (needs automation + 4-figure capital), (2) genuine
CLV handicapping (needs skill most lack; Kalshi's low fee/no-limits amplifies it), or
(3) speed/data latency (HFT/bot). For a ~$63 manual account, **Kalshi is "efficient-
with-fees."** The value produced this project: a rigorous survey→collect→PSR/DSR toolkit
that reliably tells edge from luck, and a cheap ruling-out of every mirage (which already
saved us from scaling a +$84.94 weather "winner" that was 4 lucky longshots).
