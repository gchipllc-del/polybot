#!/usr/bin/env python3
"""Can a BIAS-CORRECTED forecast lift the rank-skill above the cost line?

The raw Open-Meteo forecast is real-but-marginal: within-bin lift +0.079
(p=0.0055, CI excludes 0) yet badly calibrated in level (Brier 0.228 vs the
market's 0.127) and worth only ~+0.01/ct two-sided — below realistic friction.
A chunk of that miscalibration is almost certainly SYSTEMATIC: Open-Meteo's
grid cell vs Kalshi's exact settlement station differ by a near-constant per-
city offset, and the flat σ=3°F error model is probably wrong. Both are
fixable WITHOUT look-ahead: learn each city's median (forecast−actual) error
and the Brier-optimal σ on the EARLIER fit slice, then apply those constants
forward to the trade slice — exactly how you'd run it live.

This re-runs the whole comparison (Brier, within-bin lift + permutation/
bootstrap significance, two-sided EV vs the volume-matched scrambled floor) for
the RAW vs the BIAS-CORRECTED forecast, side by side, and asks the only
question that matters: does correction push the side-pick value clear of the
~0.02–0.03/ct friction band? If not, daily is done.

  python scripts/forecast_bias_corrected.py
  python scripts/forecast_bias_corrected.py --selftest
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from join_weather_trials import (SERIES_CITY, parse_series, parse_event_date,  # noqa: E402
                                 parse_strike2, forecast_p_yes, build_truth_index,
                                 _load_jsonl)
from forecast_skill_days import (run_strategy, day_stats, strat_fc_two_sided,    # noqa: E402
                                 strat_fc_scrambled, rank_skill_check,
                                 lift_significance, _weighted_lift)
from weather_fade import FILL_FLOOR, FILL_CEIL                                   # noqa: E402

# Realistic taker friction for the two-sided book: the un-ceiled fee
# (~0.015/ct at mid) is already inside the EV; the spread you cross as a taker
# adds ~0.01–0.02. So a side-pick worth less than this band can't survive live.
COST_CT_LOW, COST_CT_HIGH = 0.020, 0.030
SIGMA_GRID = [round(1.0 + 0.25 * i, 2) for i in range(21)]   # 1.0 … 6.0


def load_rich(markets_path: Path, truth_path: Path):
    """One row per market (earliest sample) carrying the RAW temps + city, so
    the forecast probability can be recomputed after bias correction:
    dict(p_market, yes, city, date, t, kind, strike, fc_temp, ac_temp)."""
    truth = build_truth_index(_load_jsonl(truth_path))
    by_tk: dict = {}
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
        sc = SERIES_CITY.get(parse_series(tk))
        date = parse_event_date(r.get("event_ticker", ""), tk)
        kind, strike = parse_strike2(tk)
        if not sc or date is None or strike is None:
            continue
        info = truth.get((sc[0], date))
        if info is None or info.get("forecast_temp") is None:
            continue
        rows.append({"p_market": float(r["market_p_yes"]),
                     "yes": str(r.get("result", "")).lower() == "yes",
                     "city": sc[0], "date": date, "t": t,
                     "kind": kind, "strike": float(strike),
                     "fc_temp": float(info["forecast_temp"]),
                     "ac_temp": (None if info.get("actual_temp") is None
                                 else float(info["actual_temp"]))})
    return rows


def _median(xs):
    s = sorted(xs)
    n = len(s)
    if not n:
        return 0.0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def fit_bias(fit_rows) -> dict:
    """Per-city systematic error = median(forecast_temp − actual_temp) on the
    fit slice. Subtracting it removes the station offset. Median, not mean, so
    a few wild reanalysis days don't move it."""
    per: dict = {}
    for r in fit_rows:
        if r["ac_temp"] is not None:
            per.setdefault(r["city"], []).append(r["fc_temp"] - r["ac_temp"])
    return {c: _median(v) for c, v in per.items() if len(v) >= 20}


def tune_sigma(fit_rows, bias: dict) -> float:
    """Pick the global σ minimizing Brier of the bias-corrected forecast's
    P(YES) vs the realized outcome, on the fit slice only."""
    best_sig, best_brier = 3.0, float("inf")
    for sig in SIGMA_GRID:
        se = n = 0.0
        for r in fit_rows:
            fc = r["fc_temp"] - bias.get(r["city"], 0.0)
            p = forecast_p_yes(r["kind"], r["strike"], fc, sig)
            se += (p - (1.0 if r["yes"] else 0.0)) ** 2
            n += 1
        if n and se / n < best_brier:
            best_brier, best_sig = se / n, sig
    return best_sig


def to_engine_rows(rows, bias: dict, sigma: float):
    """Project rich rows into the (p_market, p_fc, yes, date, t) tuples the
    forecast_skill_days engine consumes, using the (possibly corrected) forecast."""
    out = []
    for r in rows:
        fc = r["fc_temp"] - bias.get(r["city"], 0.0)
        p_fc = forecast_p_yes(r["kind"], r["strike"], fc, sigma)
        out.append((r["p_market"], p_fc, r["yes"], r["date"], r["t"]))
    return out


def brier(engine_rows):
    n = len(engine_rows)
    fc = sum((pf - (1.0 if y else 0.0)) ** 2 for _p, pf, y, *_ in engine_rows) / n
    mkt = sum((p - (1.0 if y else 0.0)) ** 2 for p, _pf, y, *_ in engine_rows) / n
    return fc, mkt


def evaluate(label: str, engine_rows, thr: float, significance: bool):
    fc_b, mkt_b = brier(engine_rows)
    print(f"\n========== {label} ==========")
    print(f"Brier: forecast {fc_b:.4f}  vs  market {mkt_b:.4f}  "
          f"({'better' if fc_b < mkt_b else 'worse'} than price)")
    lift = rank_skill_check(engine_rows)
    sig = lift_significance(engine_rows) if significance else None
    st_2s = day_stats(run_strategy(engine_rows, strat_fc_two_sided(thr)))
    st_fl = day_stats(run_strategy(engine_rows, strat_fc_scrambled(thr)))
    if st_2s and st_fl:
        ev_2s, ev_fl = st_2s[4], st_fl[4]
        side = ev_2s - ev_fl
        print(f"\n@ thr {thr}: two-sided EV/ct {ev_2s:+.3f} (net ${st_2s[3]:+.2f}, "
              f"corr {st_2s[5]:+.2f}, hot {st_2s[6]:+.2f}/cool {st_2s[7]:+.2f}); "
              f"floor EV/ct {ev_fl:+.3f}")
        print(f"   side-pick value: {side:+.3f}/ct   "
              f"(cost line ~{COST_CT_LOW:.3f}-{COST_CT_HIGH:.3f}/ct)")
        return {"lift": lift, "sig": sig, "ev_2s": ev_2s, "side": side,
                "brier": fc_b, "st_2s": st_2s}
    return {"lift": lift, "sig": sig}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--markets", type=Path,
                    default=ROOT / "data" / "backtest" / "becker_kalshi_weather.jsonl")
    ap.add_argument("--truth", type=Path,
                    default=ROOT / "data" / "backtest" / "weather_truth.jsonl")
    ap.add_argument("--split", type=float, default=0.6)
    ap.add_argument("--thr", type=float, default=0.05)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        raise SystemExit(selftest())

    for p, hint in ((args.markets, "becker"), (args.truth, "openmeteo")):
        if not p.exists():
            raise SystemExit(f"not found: {p} — run fetch_backtest_data.py {hint} first")

    rows = load_rich(args.markets, args.truth)
    # Tripwire for load bugs (a stale-variable bug here once made every market
    # read as "no"): real settled weather data is always a mix of outcomes.
    n_yes = sum(1 for r in rows if r["yes"])
    if rows and (n_yes == 0 or n_yes == len(rows)):
        raise SystemExit(f"!! all {len(rows)} markets loaded with the same outcome "
                         f"(yes={n_yes}) — load_rich is broken, do not trust output")
    rows.sort(key=lambda r: (r["t"] == "", r["t"]))
    k = int(len(rows) * args.split)
    fit, trade = rows[:k], rows[k:]
    print(f"=== bias-corrected forecast test === {len(rows)} markets "
          f"({len(fit)} fit / {len(trade)} trade), {len({r['date'] for r in trade})} OOS days")

    bias = fit_bias(fit)
    sigma = tune_sigma(fit, bias)
    show = sorted(bias.items(), key=lambda kv: -abs(kv[1]))
    print(f"learned on fit slice: σ*={sigma}°F; per-city bias (°F, forecast−actual): "
          + ", ".join(f"{c}{v:+.1f}" for c, v in show[:8])
          + (" …" if len(show) > 8 else ""))

    raw = evaluate("RAW forecast (flat σ=3.0, no bias correction)",
                   to_engine_rows(trade, {}, 3.0), args.thr, significance=True)
    cor = evaluate(f"BIAS-CORRECTED forecast (per-city offset, σ*={sigma})",
                   to_engine_rows(trade, bias, sigma), args.thr, significance=True)

    print("\n================= VERDICT =================")
    dl = (cor.get("lift") or 0) - (raw.get("lift") or 0)
    print(f"within-bin lift: {raw.get('lift'):+.3f} (raw) -> {cor.get('lift'):+.3f} "
          f"(corrected)   Δ{dl:+.3f}")
    if "side" in raw and "side" in cor:
        print(f"side-pick value: {raw['side']:+.3f} -> {cor['side']:+.3f}/ct   "
              f"(need > ~{COST_CT_HIGH:.3f} to clear friction)")
        clears = cor["side"] > COST_CT_HIGH
        sig = cor.get("sig")
        ci_lo = sig["ci"][0] if sig else None
        if clears and sig and sig["real"]:
            print("  -> BIAS CORRECTION CLEARS THE COST LINE — re-point fc2s at the "
                  "corrected forecast and let the live scorecard confirm.")
        elif cor["side"] > raw["side"] + 0.005:
            print("  -> correction HELPS but still inside the friction band — better, "
                  "not yet tradeable. Diminishing returns; hourly is the better frontier.")
        else:
            print("  -> correction does NOT move it clear of friction. Daily forecast "
                  "edge is real but not deployable — log it and stop optimizing daily.")


def selftest() -> int:
    """Synthetic: a forecast with a known per-city offset + wrong σ. Bias
    correction must (1) recover the offsets, (2) lower Brier, (3) not crash the
    downstream engine."""
    import random
    rng = random.Random(3)
    cities = {"A": +4.0, "B": -3.0, "C": +1.5}   # true station offsets °F
    rows = []
    for d in range(300):
        date = f"D{d:04d}"
        for c, off in cities.items():
            strike = rng.choice([60, 65, 70, 75, 80])
            actual = rng.gauss(70, 8)
            yes = actual >= strike
            fc_temp = actual + off + rng.gauss(0, 2.5)   # offset + noise
            rows.append({"p_market": min(0.9, max(0.1, 0.5 + rng.gauss(0, 0.15))),
                         "yes": yes, "city": c, "date": date, "t": date,
                         "kind": "above", "strike": float(strike),
                         "fc_temp": fc_temp, "ac_temp": actual})
    k = int(len(rows) * 0.6)
    fit, trade = rows[:k], rows[k:]
    bias = fit_bias(fit)
    ok = True
    for c, off in cities.items():
        if abs(bias.get(c, 0) - off) > 1.0:
            print(f"  FAIL: bias[{c}]={bias.get(c)} should be ~{off}")
            ok = False
    sigma = tune_sigma(fit, bias)
    fc_raw, _ = brier(to_engine_rows(trade, {}, 3.0))
    fc_cor, _ = brier(to_engine_rows(trade, bias, sigma))
    print(f"selftest: recovered bias {{{', '.join(f'{c}:{bias[c]:+.1f}' for c in cities)}}}, "
          f"σ*={sigma}; Brier {fc_raw:.3f} -> {fc_cor:.3f}")
    if fc_cor >= fc_raw:
        print("  FAIL: bias correction should lower Brier")
        ok = False
    print("  PASS" if ok else "  *** SELFTEST FAILED ***")
    return 0 if ok else 1


if __name__ == "__main__":
    main()
