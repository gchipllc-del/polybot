# Sports sleeve — repos that stack our odds (verified 2026-06-19)

Deep-research pass (5 tracks, every URL verified to exist) for upgrading the
`sports_lock` sleeve. Context: Kalshi sports moneylines, ~$63 paper bankroll,
current win-prob is a crude Brownian model `P = Φ(margin/(σ·√time_left))`.

## TL;DR — the one strategic insight

The research surfaced a **second, arguably stronger sports edge sitting right next
to our lock play**: *devig a sharp book (Pinnacle) → compare to the Kalshi ladder →
flag +EV*. It works the **whole game**, not just garbage time, and gives an
**independent fair-value benchmark** to tell whether a lock signal is real mispricing
or just us being wrong. Someone already wrote almost exactly our strategy:
`NateDeMoro/prediction-market-ev-engine` (devig Pinnacle → walk Kalshi YES-ask →
+EV with a calibration haircut). Treat it as the reference architecture.

So the upgrade path is two-pronged:
1. **Make the lock smarter** — replace Brownian σ with a calibrated win-prob model + faster data.
2. **Add the devig cross-check** — a sharp-book fair price as a second opinion + a CLV yardstick.

(Note: `becker-edge`, the branch this sleeve lives on, is likely named for
`Jon-Becker/prediction-market-analysis` — the biggest public Kalshi+Polymarket
historical dataset, listed below. That dataset is the backtest fuel for both prongs.)

---

## TOP 5 — adopt these first (with how to wire into `sports_lock`)

### 1. `sportsdataverse/sportsdataverse-py` — live data, all our sports, one library
- https://github.com/sportsdataverse/sportsdataverse-py · Python · **MIT** · active (v0.0.67, Jun 18 2026), 104★
- ESPN-backed live scoreboard + play-by-play (score, `clock.displayValue`, period) for **NBA, NFL, NHL, NCAA M/W basketball, CFB** (+WNBA/MLB). Free, no key.
- **Wire-in:** replace `sports_lock.fetch_espn_games()`'s hand-rolled ESPN call with `espn_<league>_schedule()` (live score/status) + `espn_<league>_pbp()` (clock). One dependency covers every league in our `LEAGUES` table, and it rides the *same* ESPN endpoints we already chose — so it's a drop-in hardening, not a rewrite.

### 2. `nflverse/nflreadpy` (→ nflfastR `vegas_wp`) — kills the Brownian model for NFL
- https://github.com/nflverse/nflreadpy · Python · **MIT** · active (Nov 2025), 168★ · bridges R **nflfastR** (MIT)
- nflfastR ships a calibrated, spread-aware per-play win prob (`wp`, `vegas_wp`) keyed on down/distance/yardline/score/time/timeouts. `nflreadpy` loads those columns into Python.
- **Wire-in:** for NFL, swap `win_prob(margin, tf, σ)` for a lookup/merge on `vegas_wp` at the live game state. Properly handles non-linearities (two-score leads, two-minute drill) a single-σ Brownian fit can't. This is the single biggest quality jump for football.

### 3. `mberk/shin` + `gotoConversion/goto_conversion` — the devig benchmark
- https://github.com/mberk/shin · Python/Rust · **MIT** · active (v0.2.2 Oct 2025), 102★ · `pip install shin`
- https://github.com/gotoConversion/goto_conversion · Python · **MIT** · active, 111★ · `pip install goto-conversion`
- Extract sharp fair probability from 2-way odds, correcting favorite-longshot bias (Shin 1993 + the goto method). Two independent estimators → a robustness band.
- **Wire-in:** new module `devig_check.py` — feed Pinnacle's 2-way moneyline → `shin.calculate_implied_probabilities([a,b])` and `goto_conversion(...)`; if both fair-probs disagree with Kalshi's YES by > threshold, flag. Use it (a) as a standalone edge and (b) as a sanity gate on lock signals.

### 4. `arshka/pykalshi` — real Kalshi client (WebSocket order book + execution)
- https://github.com/arshka/pykalshi · Python · **MIT** · active (May 2026), 107★, 144 commits
- Full REST + typed WebSocket, a local `OrderbookManager` that maintains book state from WS deltas, auto-retry/backoff, rate-limit handling, pandas export.
- **Wire-in:** replace the polling `_kalshi_get` ladder fetch with `pykalshi`'s WebSocket book for live, low-latency YES/NO quotes on the matched ticker — the lock's whole premise is reacting before the market crawls to 99¢, so a streaming book beats REST polling. (Official `kalshi-python` is proprietary/OpenAPI-gen; this MIT community client is the better base.)

### 5. `esvhd/pypbo` + `netcal` — prove the edge before risking the $63
- https://github.com/esvhd/pypbo · Python · **AGPL-3.0** · 134★ — PSR, **DSR**, **MinTRL**, **PBO** (overfit probability)
- https://github.com/EFS-OpenSource/calibration-framework (`netcal`) · Python · **Apache-2.0** · v1.3.6, 377★ — reliability diagrams, ECE/MCE
- **Wire-in:** `pypbo` reproduces our exact significance stack and adds PBO (multiple-testing guard) — point it at `data/sports_lock.jsonl` per-day returns. `netcal` proves the win-prob model is actually calibrated (does "98% locked" resolve YES 98% of the time?) before we trust any edge. AGPL on pypbo: fine internally, mind redistribution — or just port the formulas (we already have most in `lib/hermes_significance.py`).

---

## The rest (verified, by bucket)

### Win-probability models
- `sportsdataverse/hoopR` (R, MIT) — NBA/CBB live WP; access via sportsdataverse-py in Python.
- `doganjr/LWPNBA` (Python, MIT, new/0★) — calibration-first deep-learning live NBA WP; **template** to train our own, not a dependency.
- `danmorse314/hockeyR` (R) — NHL PBP + xG; **no live WP yet** (planned). 
- Reference-grade hobby NBA/NFL WP: `kmd6225/NBA-Play-By-Play-Win-Probability`, `cmunch1/nba-prediction`, `albertkuo/nba_comeback`, `lakenrivet/nfl-win-probability`.
- ⚠ `nflverse/nfl_data_py` — **archived Sep 2025**, use `nflreadpy` instead.

### Live data feeds
- `swar/nba_api` (Python, MIT, 3.7k★) — authoritative NBA live (`nba_api.live`); heavier rate limits.
- `coreyjs/nhl-api-py` (Python, Apache-2.0, 140★) — best free live NHL.
- `fenneh/espn-sports-api` (Python, MIT, ~1★) — lightweight ESPN live wrapper w/ backoff.
- `pseudo-r/Public-ESPN-API` (MIT, 578★) — **spec** for a thin lowest-latency DIY poller (`cdn.espn.com/core/{sport}/scoreboard?xhr=1`).
- `toddrob99/MLB-StatsAPI` (GPL-3.0) — MLB live (out of scope but solid).
- ⚠ DROP: `roclark/sportsipy` (abandoned 2021, historical only), `cwendt94/espn-api` (fantasy only), `statsbombpy` (live = paid), `hoopR-py`/`cfbfastR-py` (consolidated into sportsdataverse-py).

### Kalshi / prediction-market
- `Kalshi/kalshi-starter-code-python` (official, no license) — auth/WebSocket scaffolding; start for RSA signing.
- `TexasCoding/kalshi-python-sdk` (MIT, v4.2.0, ~0★) — widest endpoint coverage; low community vetting, read before trusting.
- `Jon-Becker/prediction-market-analysis` (MIT, **3.5k★**) — **largest public Kalshi+Polymarket market + trade-tape dataset**; the backtest fuel. (Likely the namesake of `becker-edge`.)
- `machina-sports/sports-skills` (MIT, 154★) — Kalshi+Polymarket+ESPN sports odds joiner; personal/research license.
- `harrodyuan/kalshi-data-collector`, `mickbransfield/kalshi` — DIY snapshot/bulk collectors (older).
- Polymarket (if we extend): `Polymarket/py-sdk` (MIT, current) — ⚠ `py-clob-client` is **archived/non-functional**.
- Situational: `rodlaf/KalshiMarketMaker`, `ryanfrigo/kalshi-ai-trading-bot`, `9crusher/mcp-server-kalshi`, `CarlosIbCu/polymarket-kalshi-btc-arbitrage-bot` (crypto, but good dual-venue skeleton).

### Odds / devig / arbitrage / CLV
- `martineastwood/penaltyblog` (Python, MIT, 182★) — batteries-included devig (multiple methods) + sports modeling.
- `the-odds-api/samples-python` — official Odds API client; **paid tiers expose historical/closing snapshots → enables CLV**; includes Pinnacle/DK/FD. `sarartur/oddsapi` (MIT) = installable thin wrapper.
- `NateDeMoro/prediction-market-ev-engine` (no license, new) — **our exact strategy already built** (devig Pinnacle → Kalshi ladder → +EV w/ calibration haircut, 15s/60s snapshots, 429 backoff). Architecture reference; don't copy code wholesale (no license).
- `daankoning/ArbitrageFinder` (GPL-2.0) / `carterlasalle/SportsArbFinder` (MIT) — line-shopping skeletons (prefer the MIT one).
- Pinnacle direct: `iliyasone/ps3838api` (account/region/ToS-gated) — sharpest book; scrapers exist but violate ToS, avoid for production.

### Validation / backtest
- `georgedouzas/sports-betting` (MIT, 722★) — domain-fit value-bet backtester w/ walk-forward splits + staking.
- `kernc/backtesting.py` (AGPL, 8.5k★) — general engine; model contract resolution as a 0/1 series.
- `ranaroussi/quantstats` (Apache-2.0, 7.3k★) — tear sheets incl. probabilistic Sharpe.
- `baobach/mlfinpy` (MIT) — open successor to the now-closed `mlfinlab`.

---

## License cautions
- **Permissive (safe to vendor):** sportsdataverse-py, nflreadpy/nflfastR, shin, goto_conversion, penaltyblog, pykalshi, nba_api, nhl-api-py, netcal, quantstats, sports-betting, mlfinpy, Jon-Becker dataset, SportsArbFinder.
- **AGPL/GPL (copyleft — fine internal, mind redistribution):** pypbo, backtesting.py, ArbitrageFinder.
- **No license = all-rights-reserved (port logic, don't copy):** Kalshi starter code, prediction-market-ev-engine, rubenbriones PSR.
- **Proprietary/closed:** official `kalshi-python` pip SDK, `mlfinlab`.

## Recommended concrete next step
Smallest high-value move on `becker-edge`: build `devig_check.py` (Odds API → shin + goto_conversion → fair prob → compare to Kalshi YES) as a **new sleeve that runs alongside the lock**, and re-point `sports_lock`'s data layer at `sportsdataverse-py`. Then validate both with `pypbo`/`netcal` on forward-collected data before any real money.
