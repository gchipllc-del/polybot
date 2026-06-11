#!/usr/bin/env python3
"""Forecast-skill DECORRELATION test over the Becker weather history.

The per-day reconstruction (analyze_history_days.py) showed the price-only fade
is +EV but corr(net, day YES-rate) ≈ −0.8: a directional short-vol bet that
bleeds on hot days. This tool asks the follow-up question: if we only fade when
the WEATHER FORECAST disagrees with the market — not whenever the price looks
rich — does the edge survive AND decorrelate from the weather?

  PASS  → net stays positive, green% holds, corr(net, YES-rate) → ~0
          (a real information edge: the forecast knows something the market
           hasn't priced, on hot days and cool days alike)
  FAIL  → corr stays ≲ −0.5 (the "forecast gate" is just re-selecting the same
          short-heat exposure) → the weather book is definitively closed.

Inputs (both produced by fetch_backtest_data.py):
  data/backtest/becker_kalshi_weather.jsonl   markets + settled results
  data/backtest/weather_truth.jsonl           per city/day HISTORICAL forecast
                                              (historical-forecast-api: what the
                                              forecast said, no look-ahead)

Run on the Mac:
  python scripts/fetch_backtest_data.py openmeteo --start 2024-10-01 --end 2025-11-30
  python scripts/forecast_skill_days.py

Strategies compared on the SAME joined, out-of-sample slice:
  price-only   fade YES when calibration fair − p ≤ −thr   (the live rule)
  fc-fade      fade YES only when forecast_p − p ≤ −thr    (forecast gate)
  fc-2side     fc-fade + buy YES when forecast_p − p ≥ +thr (direction-neutral)

All trades: taker fill, 0.10–0.90 band, net of Kalshi fee 0.07·P·(1−P).
Verdict is per distinct event DAY (correlated city-fades collapse to one sample).

Self-test (no data files needed):
  python scripts/forecast_skill_days.py --selftest
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from becker_edge import fit_calibration, fair_prob                    # noqa: E402
from weather_fade import FILL_FLOOR, FILL_CEIL                        # noqa: E402
from join_weather_trials import (SERIES_CITY, parse_series,           # noqa: E402
                                 parse_event_date, parse_strike2,
                                 forecast_p_yes, build_truth_index, _load_jsonl)
from analyze_history_days import pearson                              # noqa: E402

THRESHOLDS = (0.03, 0.05, 0.08, 0.10, 0.12)


# ---------------------------------------------------------------- data loading

def load_joined_markets(markets_path: Path, truth_path: Path, sigma: float):
    """One row per market (earliest priced sample), joined to the historical
    forecast: (p_market, p_forecast, yes_bool, date, sample_at). Returns
    (rows, stats)."""
    truth = build_truth_index(_load_jsonl(truth_path))
    by_tk: dict = {}
    stats = {"markets": 0, "no_parse": 0, "no_truth": 0, "joined": 0}
    with open(markets_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            p = r.get("market_p_yes")
            res = str(r.get("result", "")).lower()
            if not isinstance(p, (int, float)) or res not in ("yes", "no"):
                continue
            tk = r.get("market_ticker") or r.get("ticker") or ""
            t = str(r.get("sample_at") or "")
            cur = by_tk.get(tk)
            if cur is None or t < cur[1]:
                by_tk[tk] = (r, t)
    rows = []
    for tk, (r, t) in by_tk.items():
        stats["markets"] += 1
        sc = SERIES_CITY.get(parse_series(tk))
        date = parse_event_date(r.get("event_ticker", ""), tk)
        kind, strike = parse_strike2(tk, r.get("yes_sub_title", ""))
        if not sc or date is None or strike is None:
            stats["no_parse"] += 1
            continue
        info = truth.get((sc[0], date))
        if info is None or info.get("forecast_temp") is None:
            stats["no_truth"] += 1
            continue
        p_fc = forecast_p_yes(kind, strike, float(info["forecast_temp"]), sigma)
        stats["joined"] += 1
        rows.append((float(r["market_p_yes"]), p_fc,
                     str(r.get("result", "")).lower() == "yes", date, t))
    return rows, stats


# ---------------------------------------------------------------- the engine

def run_strategy(rows, decide):
    """decide(p_market, p_forecast) -> 'no' | 'yes' | None.
    Returns per-day buckets {date: {net, n, yes}} using the live fill/fee model."""
    days: dict = {}
    for p, p_fc, yes, date, _t in rows:
        if not (FILL_FLOOR <= p <= FILL_CEIL):
            continue
        side = decide(p, p_fc)
        if side is None:
            continue
        fill = (1.0 - p) if side == "no" else p
        won = (not yes) if side == "no" else yes
        # Un-ceiled per-contract fee: multi-contract orders amortize the
        # ceil-to-cent, so ceiling a 1-lot would overstate ~30%.
        fee = 0.07 * fill * (1.0 - fill)
        pnl = ((1.0 - fill) if won else -fill) - fee
        b = days.setdefault(date, {"net": 0.0, "n": 0, "yes": 0})
        b["net"] += pnl
        b["n"] += 1
        b["yes"] += 1 if yes else 0
    return days


def day_stats(days):
    """(days, green%, trades, net, ev/ct, corr, hot_avg, cool_avg) or None."""
    if not days:
        return None
    nets = [d["net"] for d in days.values()]
    yes_rates = [d["yes"] / d["n"] for d in days.values()]
    ntr = sum(d["n"] for d in days.values())
    net = sum(nets)
    green = sum(1 for x in nets if x > 0) / len(nets) * 100
    corr = pearson(yes_rates, nets)
    med = sorted(yes_rates)[len(yes_rates) // 2]
    hot = [n for n, r in zip(nets, yes_rates) if r >= med]
    cool = [n for n, r in zip(nets, yes_rates) if r < med]
    hot_avg = sum(hot) / len(hot) if hot else 0.0
    cool_avg = sum(cool) / len(cool) if cool else 0.0
    return (len(days), green, ntr, net, net / ntr, corr, hot_avg, cool_avg)


def print_table(name: str, rows, strategies):
    """One table: a row per thr, columns per the analyze_history_days format."""
    print(f"\n--- {name} ---")
    print(f"{'thr':>5} {'days':>5} {'green%':>7} {'trades':>7} {'net$':>9} "
          f"{'EV/ct':>7} {'corr(net,YESrate)':>18} {'hot-day net':>11} {'cool-day net':>12}")
    headline = None
    prev_ntr = None
    for thr in THRESHOLDS:
        st = day_stats(run_strategy(rows, strategies(thr)))
        if st is None:
            continue
        ndays, green, ntr, net, ev, corr, hot_avg, cool_avg = st
        # Stricter thr trades a subset — trades must not grow.
        if prev_ntr is not None and ntr > prev_ntr:
            print(f"  !! internal error: trades grew {prev_ntr}->{ntr} at thr {thr} "
                  f"— do not trust this table")
        prev_ntr = ntr
        corr_s = f"{corr:+.2f}" if corr is not None else "  n/a"
        print(f"{thr:>5.2f} {ndays:>5} {green:>6.0f}% {ntr:>7} {net:>+9.2f} "
              f"{ev:>+7.3f} {corr_s:>18} {hot_avg:>+11.2f} {cool_avg:>+12.2f}")
        if thr == 0.05:
            headline = st
    return headline


def rank_skill_check(rows, nbins=10, min_side=20):
    """Does the forecast carry information BEYOND the price level?

    Within each market-price bin, split markets into forecast-cheap (p_fc > p)
    vs forecast-rich (p_fc < p) and compare realized YES rates. The price level
    is held fixed, so a positive lift (cheap resolves YES more often than rich
    at the SAME price) can only come from the forecast — Brier-miscalibration
    in level can't fake it. Returns the trade-weighted average lift."""
    bins: dict = {}
    for p, p_fc, yes, *_ in rows:
        if not (FILL_FLOOR <= p <= FILL_CEIL):
            continue
        b = min(nbins - 1, int(p * nbins))
        bins.setdefault(b, []).append((p_fc - p, 1.0 if yes else 0.0))
    print(f"\n--- forecast rank-skill within price bins (lift = YES-rate of "
          f"forecast-cheap − forecast-rich at the same price) ---")
    print(f"{'price bin':>10} {'n_cheap':>8} {'n_rich':>7} {'yes%cheap':>10} "
          f"{'yes%rich':>9} {'lift':>7}")
    wsum = wn = 0.0
    for b in sorted(bins):
        items = bins[b]
        cheap = [y for d, y in items if d > 0]
        rich = [y for d, y in items if d < 0]
        if len(cheap) < min_side or len(rich) < min_side:
            continue
        yc, yr = sum(cheap) / len(cheap), sum(rich) / len(rich)
        n = len(cheap) + len(rich)
        wsum += (yc - yr) * n
        wn += n
        print(f"{b/nbins:>5.2f}-{(b+1)/nbins:<4.2f} {len(cheap):>8} {len(rich):>7} "
              f"{yc*100:>9.1f}% {yr*100:>8.1f}% {yc-yr:>+7.3f}")
    if wn:
        avg = wsum / wn
        print(f"{'weighted avg lift':>43}: {avg:+.3f}  "
              f"({'forecast HAS rank skill beyond price' if avg > 0.03 else 'forecast adds ~no information beyond price' if avg > -0.03 else 'forecast is ANTI-informative'})")
        return avg
    print("  (not enough data in any bin)")
    return None


def _weighted_lift(bins, min_side=20):
    """Trade-weighted within-bin lift from prebuilt {bin: [(signed_gap, y)]}."""
    wsum = wn = 0.0
    for items in bins.values():
        cheap = [y for d, y in items if d > 0]
        rich = [y for d, y in items if d < 0]
        if len(cheap) < min_side or len(rich) < min_side:
            continue
        n = len(cheap) + len(rich)
        wsum += (sum(cheap) / len(cheap) - sum(rich) / len(rich)) * n
        wn += n
    return (wsum / wn) if wn else None


def lift_significance(rows, nbins=10, min_side=20, n_perm=2000, n_boot=2000,
                      seed=0):
    """Is the within-bin rank lift real, or noise that more data would wash out?

    Two model-free tests on the SAME OOS data (no new history exists — the
    universe is already fully recovered):
      • permutation p-value: shuffle the forecast's cheap/rich SIGN within each
        price bin (kills any forecast↔outcome link, keeps the marginals) and
        recompute the lift n_perm times. p = P(random forecast ≥ observed). The
        scrambled-floor control is one draw of this; this is the full null.
      • bootstrap 95% CI: resample markets with replacement n_boot times. If the
        CI straddles 0, the lift is not distinguishable from zero.
    """
    rng = random.Random(seed)
    base: dict = {}
    for p, p_fc, yes, *_ in rows:
        if FILL_FLOOR <= p <= FILL_CEIL:
            base.setdefault(min(nbins - 1, int(p * nbins)), []).append(
                (p_fc - p, 1.0 if yes else 0.0))
    obs = _weighted_lift(base, min_side)
    if obs is None:
        print("\n(significance: not enough data)")
        return None

    # Permutation null: re-pair each bin's signed gaps with shuffled outcomes.
    ge = 0
    for _ in range(n_perm):
        perm = {}
        for b, items in base.items():
            ys = [y for _d, y in items]
            rng.shuffle(ys)
            perm[b] = [(d, ys[i]) for i, (d, _y) in enumerate(items)]
        L = _weighted_lift(perm, min_side)
        if L is not None and L >= obs:
            ge += 1
    p_perm = (ge + 1) / (n_perm + 1)

    # Bootstrap CI: resample markets with replacement.
    allrows = [(b, d, y) for b, items in base.items() for d, y in items]
    boots = []
    for _ in range(n_boot):
        samp: dict = {}
        for _ in range(len(allrows)):
            b, d, y = allrows[rng.randrange(len(allrows))]
            samp.setdefault(b, []).append((d, y))
        L = _weighted_lift(samp, min_side)
        if L is not None:
            boots.append(L)
    boots.sort()
    lo = boots[int(0.025 * len(boots))] if boots else float("nan")
    hi = boots[int(0.975 * len(boots))] if boots else float("nan")

    print(f"\n--- significance of the rank-skill lift (model-free, on the existing "
          f"{len(allrows)} OOS markets) ---")
    print(f"  observed weighted lift : {obs:+.3f}")
    print(f"  permutation p-value     : {p_perm:.4f}   "
          f"(P[random forecast ≥ observed]; {n_perm} shuffles)")
    print(f"  bootstrap 95% CI        : [{lo:+.3f}, {hi:+.3f}]   ({len(boots)} resamples)")
    real = p_perm < 0.05 and lo > 0
    verdict = ("REAL signal (p<0.05 AND CI excludes 0) — small but not noise"
               if real else
               "NOISE — indistinguishable from a random forecast at this sample size"
               if (p_perm >= 0.10 or lo <= 0 <= hi) else
               "BORDERLINE — leans real but not conclusive")
    print(f"  -> {verdict}")
    return {"obs": obs, "p_perm": p_perm, "ci": (lo, hi), "real": real}


# ---------------------------------------------------------------- strategies

def strat_price_only(centers, cal_rates):
    def make(thr):
        def decide(p, _p_fc):
            return "no" if (fair_prob(p, centers, cal_rates) - p) <= -thr else None
        return decide
    return make


def strat_fc_fade(thr):
    def decide(p, p_fc):
        return "no" if (p_fc - p) <= -thr else None
    return decide


def strat_fc_two_sided(thr):
    def decide(p, p_fc):
        d = p_fc - p
        if d <= -thr:
            return "no"
        if d >= thr:
            return "yes"
        return None
    return decide


def strat_calib_two_sided(centers, cal_rates):
    """THE CONTROL: two-sided, but cheap/rich is decided by the price→outcome
    calibration curve alone — NO weather forecast. If this decorrelates and
    profits as well as the forecast version, the forecast adds nothing and the
    edge is just direction-neutral favorite-longshot harvesting."""
    def make(thr):
        def decide(p, _p_fc):
            d = fair_prob(p, centers, cal_rates) - p
            if d <= -thr:
                return "no"
            if d >= thr:
                return "yes"
            return None
        return decide
    return make


def strat_fc_scrambled(thr, seed=7):
    """FLOOR reference: trades the EXACT same markets as forecast two-sided
    (same |forecast−market| ≥ thr gate, same volume) but the side is a coin
    flip. Any net gap between the forecast version and this floor is purely
    the value of KNOWING which side is rich — volume-matched by construction."""
    rng = random.Random(seed)
    def decide(p, p_fc):
        if abs(p_fc - p) < thr:
            return None
        return "no" if rng.random() < 0.5 else "yes"
    return decide


# ---------------------------------------------------------------- self-test

def selftest() -> int:
    """Synthetic check that the harness can tell skill from no-skill.

    Builds days where outcomes follow a hidden true probability with a shared
    per-day weather shock and a market that systematically overprices YES.
    Checks the three properties the real test relies on:
      1. fade-everything baseline is +EV but STRONGLY weather-correlated,
      2. a SKILLED forecast traded two-sided stays net-positive while its
         corr(net, YES-rate) moves materially toward 0 vs that baseline,
      3. a NOISE forecast (independent of truth) does clearly worse."""
    rng = random.Random(42)
    rows_skill, rows_noise = [], []
    for d in range(400):
        date = f"D{d:04d}"
        shock = rng.gauss(0, 0.18)                  # the shared weather pattern
        for _city in range(8):
            true_p = min(0.95, max(0.05, 0.5 + shock + rng.gauss(0, 0.10)))
            yes = rng.random() < true_p
            # Market: noisy + systematically overprices YES (the FLB-ish bias)
            p_mkt = min(0.90, max(0.10, true_p + 0.04 + rng.gauss(0, 0.08)))
            p_skill = min(0.98, max(0.02, true_p + rng.gauss(0, 0.04)))
            p_noise = min(0.98, max(0.02, 0.5 + rng.gauss(0, 0.20)))
            rows_skill.append((p_mkt, p_skill, yes, date, ""))
            rows_noise.append((p_mkt, p_noise, yes, date, ""))

    ok = True
    # Baseline analog of the live price-only fade: with the YES bias known to
    # be ~+0.04, thr 0.03 fades every in-band market — pure short-heat book.
    st_base = day_stats(run_strategy(rows_skill, lambda p, fc: "no"))
    st_skill = day_stats(run_strategy(rows_skill, strat_fc_two_sided(0.05)))
    st_noise = day_stats(run_strategy(rows_noise, strat_fc_two_sided(0.05)))
    _, _, ntr_b, net_b, _, corr_b, _, _ = st_base
    _, green_s, ntr_s, net_s, _, corr_s, _, _ = st_skill
    _, _, ntr_n, net_n, _, corr_n, _, _ = st_noise
    print("selftest @ thr 0.05:")
    print(f"  fade-everything : {ntr_b} trades, net {net_b:+.2f}, corr {corr_b:+.2f}")
    print(f"  skilled 2-sided : {ntr_s} trades, net {net_s:+.2f}, "
          f"green {green_s:.0f}%, corr {corr_s:+.2f}")
    print(f"  noise   2-sided : {ntr_n} trades, net {net_n:+.2f}, corr {corr_n:+.2f}")
    if not (corr_b is not None and corr_b < -0.5):
        print("  FAIL: fade-everything baseline should be strongly weather-correlated")
        ok = False
    if not (net_s > 0 and corr_s is not None and corr_s - corr_b >= 0.3):
        print("  FAIL: skilled forecast should stay net-positive while moving corr "
              "materially toward 0 vs the baseline")
        ok = False
    if not (net_s - net_n > 0.2 * abs(net_s)):
        print("  FAIL: skilled forecast should clearly beat a noise forecast")
        ok = False

    # Control discriminator: fit a price calibration on the skill rows and run
    # the calibration two-sided control. With a genuinely skilled forecast, the
    # forecast version should BEAT the price-only control; if the control alone
    # already captured everything, the real test couldn't tell them apart.
    centers, cal_rates, _ = fit_calibration(
        [(p, yes) for p, _fc, yes, *_ in rows_skill], 20)
    st_ctrl = day_stats(run_strategy(rows_skill,
                                     strat_calib_two_sided(centers, cal_rates)(0.05)))
    net_c = st_ctrl[3]
    print(f"  calib control   : {st_ctrl[2]} trades, net {net_c:+.2f}, corr {st_ctrl[5]:+.2f}")
    if not (net_s - net_c > 0.15 * abs(net_s)):
        print("  FAIL: with a truly skilled forecast, the forecast version should "
              "beat the price-only control")
        ok = False

    # Volume-matched floor: same trade set as skilled 2-sided, coin-flip side.
    # The skilled side-pick must be worth real EV/ct over the flip.
    st_fl = day_stats(run_strategy(rows_skill, strat_fc_scrambled(0.05)))
    ev_s, ev_fl = st_skill[4], st_fl[4]
    print(f"  scrambled floor : {st_fl[2]} trades, net {st_fl[3]:+.2f}, "
          f"EV/ct {ev_fl:+.3f} (skilled EV/ct {ev_s:+.3f})")
    if st_fl[2] != ntr_s:
        print(f"  FAIL: floor must trade the same volume as skilled "
              f"({st_fl[2]} != {ntr_s})")
        ok = False
    if not (ev_s - ev_fl > 0.01):
        print("  FAIL: skilled side-pick should be worth > +0.01/ct over a coin flip")
        ok = False

    # Significance harness must call a skilled forecast REAL and a noise one NOISE.
    sig_s = lift_significance(rows_skill, n_perm=300, n_boot=300, seed=1)
    sig_n = lift_significance(rows_noise, n_perm=300, n_boot=300, seed=1)
    if not (sig_s and sig_s["real"]):
        print("  FAIL: significance should call the skilled forecast REAL")
        ok = False
    if not (sig_n and not sig_n["real"]):
        print("  FAIL: significance should NOT call a noise forecast real")
        ok = False
    print("  PASS" if ok else "  *** SELFTEST FAILED ***")
    return 0 if ok else 1


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--markets", type=Path,
                    default=ROOT / "data" / "backtest" / "becker_kalshi_weather.jsonl")
    ap.add_argument("--truth", type=Path,
                    default=ROOT / "data" / "backtest" / "weather_truth.jsonl")
    ap.add_argument("--sigma", type=float, default=3.0,
                    help="forecast-error std-dev in °F for p_above (daily ~3-4)")
    ap.add_argument("--split", type=float, default=0.6,
                    help="calibration fit fraction (time-split, baseline only)")
    ap.add_argument("--bins", type=int, default=20)
    ap.add_argument("--selftest", action="store_true",
                    help="run the synthetic skill-vs-noise harness check and exit")
    ap.add_argument("--significance", action="store_true",
                    help="permutation p-value + bootstrap CI on the rank-skill lift "
                         "(is the residual real or noise?)")
    args = ap.parse_args()

    if args.selftest:
        raise SystemExit(selftest())

    for p, hint in ((args.markets, "becker"), (args.truth,
                    "openmeteo --start 2024-10-01 --end 2025-11-30")):
        if not p.exists():
            raise SystemExit(f"not found: {p} — run fetch_backtest_data.py {hint} first")

    rows, stats = load_joined_markets(args.markets, args.truth, args.sigma)
    print(f"=== forecast-skill decorrelation test (σ={args.sigma}°F) ===")
    print(f"joined {stats['joined']}/{stats['markets']} markets "
          f"({stats['no_parse']} unparseable, {stats['no_truth']} no forecast for city/day)")
    if len(rows) < 100:
        raise SystemExit("too few joined rows — check weather_truth.jsonl date range "
                         "(needs --start 2024-10-01 --end 2025-11-30)")

    # Same OOS protocol as analyze_history_days: fit the price calibration on
    # the earlier 60% by time, evaluate EVERY strategy on the later 40% only.
    rows.sort(key=lambda r: (r[4] == "", r[4]))
    k = int(len(rows) * args.split)
    fit, trade = rows[:k], rows[k:]
    centers, cal_rates, _ = fit_calibration([(p, yes) for p, _fc, yes, *_ in fit],
                                            args.bins)
    n_days = len({r[3] for r in trade})
    print(f"OOS slice: {len(trade)} markets over {n_days} distinct days")

    # Forecast skill sanity before any trading: does the forecast even predict?
    brier_fc = sum((p_fc - (1.0 if yes else 0.0)) ** 2
                   for _p, p_fc, yes, *_ in trade) / len(trade)
    brier_mkt = sum((p - (1.0 if yes else 0.0)) ** 2
                    for p, _fc, yes, *_ in trade) / len(trade)
    print(f"Brier (lower=better): forecast {brier_fc:.4f}  vs  market {brier_mkt:.4f}"
          f"  -> forecast {'HAS independent signal' if brier_fc < brier_mkt else 'is WORSE than the market price'}")
    # Brier measures LEVEL calibration; a level-miscalibrated forecast can still
    # rank same-priced markets correctly. This is the direct test of that:
    rank_lift = rank_skill_check(trade)
    if args.significance:
        lift_significance(trade)

    print_table("price-only fade (live rule, baseline on the joined subset)",
                trade, strat_price_only(centers, cal_rates))
    h_fade = print_table("forecast-gated fade (NO only when forecast says YES is rich)",
                         trade, lambda thr: strat_fc_fade(thr))
    h_2s = print_table("forecast two-sided (NO when rich, YES when cheap — direction-neutral)",
                       trade, lambda thr: strat_fc_two_sided(thr))
    # THE CONTROLS — decide cheap/rich WITHOUT any weather forecast.
    h_calib = print_table("CONTROL: calibration two-sided (price-only side pick, NO forecast)",
                          trade, strat_calib_two_sided(centers, cal_rates))
    h_scram = print_table("FLOOR: side-scrambled (SAME markets as forecast 2-sided, coin-flip side)",
                          trade, lambda thr: strat_fc_scrambled(thr))

    print("\nHOW TO READ THIS — the forecast-attribution question:")
    print("  • Two-sided cuts cancel weather-direction exposure by construction,")
    print("    so decorrelation alone proves nothing about the forecast.")
    print("  • FLOOR is the clean comparison: identical trade set and volume as")
    print("    forecast two-sided, side coin-flipped. The per-contract gap between")
    print("    them is exactly the value of the forecast's side-pick.")
    print("  • CONTROL shows what the price curve alone offers two-sided (it mostly")
    print("    finds NO-side trades, so compare it on EV/ct, not total net).")
    print("  • The rank-skill table above is the model-free check: positive lift =")
    print("    the forecast separates winners from losers AT THE SAME PRICE.")

    if h_2s and h_scram:
        ev_f, ev_s = h_2s[4], h_scram[4]
        ev_c = h_calib[4] if h_calib else None
        side_value = ev_f - ev_s
        print(f"\n  @ thr 0.05 per-contract: forecast 2-sided {ev_f:+.3f}  vs  "
              f"side-scrambled floor {ev_s:+.3f}  ->  side-pick worth {side_value:+.3f}/ct"
              + (f"  (calibration control {ev_c:+.3f}/ct on its own, smaller book)"
                 if ev_c is not None else ""))
        if side_value > 0.015 and h_2s[3] > 0 and (rank_lift or 0) > 0.02:
            call = ("FORECAST CARRIES REAL SIDE INFORMATION — beats its own "
                    "volume-matched floor AND shows rank skill at fixed price.")
        elif side_value <= 0.005 or (rank_lift is not None and rank_lift < 0.0):
            call = ("FORECAST ADDS NOTHING — its side-pick is no better than a coin "
                    "flip on the same markets. Drop the forecast.")
        else:
            call = "MARGINAL — side value exists but is thin; needs more data."
        print(f"  -> {call}")


if __name__ == "__main__":
    main()
