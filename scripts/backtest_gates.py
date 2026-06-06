#!/usr/bin/env python3
"""Replay gate thresholds against a TRIALS dataset on REAL settlement outcomes.

INPUT
-----
A trials file from scripts/build_trials.py — one trial per scanned signal
sample of a SETTLED market, carrying the decision fields (signed edge, side,
σ-normalized forecast margin, entry fill) and the real outcome (won,
pnl_per_contract).

ENTRY MODEL (faithful to the live sleeve)
-----------------------------------------
A market is scanned many times. The live sleeve fires ONCE — at the first scan
that clears every gate. So for a given gate config we group trials by market,
keep the samples that pass ALL gates, and take the EARLIEST one (smallest
sample_at) as the entry. Its fill / pnl is the trade. Markets with no passing
sample contribute no trade. This makes `max_seconds_to_close` meaningful: a
tighter cap makes the sleeve "wait" for a closer-in scan before entering.

GATES (all overridable; defaults track config/weather_daily_strategy.yaml)
  min_edge                |edge| floor
  max_disagreement_edge   |edge| ceiling (#174 — fade-the-market humility cap)
  min_margin_sigma        σ-normalized forecast-direction floor (0 = off) [NEW]
  forecast_buffer_f       NO-side °F direction buffer
  forecast_buffer_f_yes   YES-side °F direction buffer
  max_fill_for_buy        entry-price ceiling
  min_seconds_to_close    don't enter inside this many s of close
  max_seconds_to_close    don't enter earlier than this many s before close (inf = off)

USAGE
-----
  python scripts/backtest_gates.py data/trials_daily.jsonl
  python scripts/backtest_gates.py data/trials_daily.jsonl --sweep min_margin_sigma
  python scripts/backtest_gates.py data/trials_daily.jsonl --sweep max_seconds_to_close
  python scripts/backtest_gates.py data/trials_daily.jsonl --sweep min_edge
  python scripts/backtest_gates.py data/trials_daily.jsonl --min-edge 0.05 --max-fill 0.6

READ-ONLY: reads the trials file, prints a report. No network, no writes.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _default_gates() -> dict:
    """Pull live gate params so the no-sweep report mirrors production. The two
    σ/horizon knobs the sweeps target default to OFF (min_margin_sigma=0,
    max_seconds_to_close=inf) so they're additive, not surprise filters."""
    try:
        from lib.weather_daily_paper import _effective_params
        p = _effective_params()
    except Exception as e:
        print(f"  [warn] could not load live params ({e}); using yaml defaults")
        p = {"min_edge_threshold": 0.0, "max_disagreement_edge": 0.40,
             "forecast_buffer_f": 1.5, "forecast_buffer_f_yes": 2.5,
             "max_fill_for_buy": 0.70, "min_seconds_to_close": 600.0}
    return {
        "min_edge": float(p.get("min_edge_threshold", 0.0)),
        "max_disagreement_edge": float(p.get("max_disagreement_edge", 0.40)),
        "min_margin_sigma": 0.0,
        "forecast_buffer_f": float(p.get("forecast_buffer_f", 1.5)),
        "forecast_buffer_f_yes": float(p.get("forecast_buffer_f_yes", 2.5)),
        "max_fill_for_buy": float(p.get("max_fill_for_buy", 0.70)),
        "min_seconds_to_close": float(p.get("min_seconds_to_close", 600.0)),
        "max_seconds_to_close": math.inf,
    }


# Sweep ranges per gate. seconds expressed in hours for readability.
_SWEEPS: dict[str, list[float]] = {
    "min_edge": [0.0, 0.025, 0.05, 0.075, 0.10, 0.15, 0.20, 0.25, 0.30],
    "max_disagreement_edge": [0.20, 0.30, 0.40, 0.50, 0.60, 0.80, 1.0],
    "min_margin_sigma": [0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0],
    "forecast_buffer_f": [0.0, 1.0, 1.5, 2.0, 3.0, 4.0],
    "forecast_buffer_f_yes": [0.0, 1.0, 2.0, 2.5, 3.0, 4.0],
    "max_fill_for_buy": [0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 0.95],
    "min_seconds_to_close": [0, 600, 1800, 3600, 7200],
    "max_seconds_to_close": [h * 3600 for h in (3, 6, 12, 18, 24, 36, 48)] + [math.inf],
}


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        raise SystemExit(f"trials file not found: {path} (run build_trials.py first)")
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _ts(s: str | None) -> datetime:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return datetime.min.replace(tzinfo=timezone.utc)


def _passes(t: dict, g: dict) -> bool:
    """True if a single trial sample clears every gate."""
    ae = t.get("abs_edge")
    if ae is None or ae < g["min_edge"] or ae > g["max_disagreement_edge"]:
        return False
    side = t.get("side")
    # σ-normalized forecast-direction floor (NEW gate; 0 = off).
    if g["min_margin_sigma"] > 0:
        ms = t.get("margin_sigma")
        if ms is None:
            return False
        if side == "YES" and ms < g["min_margin_sigma"]:
            return False
        if side == "NO" and ms > -g["min_margin_sigma"]:
            return False
    # °F forecast-direction buffer (matches live _would_fire).
    ym = t.get("yes_margin_f")
    if ym is None:
        return False
    if side == "YES" and ym < g["forecast_buffer_f_yes"]:
        return False
    if side == "NO" and ym > -g["forecast_buffer_f"]:
        return False
    # fill price band + ceiling.
    fill = t.get("fill")
    if fill is None or not (0.05 <= fill <= 0.95) or fill > g["max_fill_for_buy"]:
        return False
    stc = t.get("seconds_to_close")
    if stc is None:
        return False
    if stc < g["min_seconds_to_close"] or stc > g["max_seconds_to_close"]:
        return False
    return True


def _select_entries(trials: list[dict], g: dict) -> list[dict]:
    """One entry per market: earliest passing sample (how the live sleeve fires
    once, on the first qualifying scan)."""
    by_market: dict[str, dict] = {}
    for t in trials:
        if not _passes(t, g):
            continue
        m = t["market_ticker"]
        cur = by_market.get(m)
        if cur is None or _ts(t.get("sample_at")) < _ts(cur.get("sample_at")):
            by_market[m] = t
    return list(by_market.values())


def _stats(entries: list[dict]) -> dict:
    n = len(entries)
    if n == 0:
        return {"n": 0, "wr": 0.0, "pnl": 0.0, "ev": 0.0}
    wins = sum(1 for e in entries if e["won"])
    pnl = sum(float(e["pnl_per_contract"]) for e in entries)
    return {"n": n, "wr": wins / n, "pnl": pnl, "ev": pnl / n}


def report_single(trials: list[dict], g: dict) -> None:
    entries = _select_entries(trials, g)
    s = _stats(entries)
    print(f"\n{'='*64}\n[gates] entries at current thresholds\n{'='*64}")
    for k in ("min_edge", "max_disagreement_edge", "min_margin_sigma",
              "forecast_buffer_f", "forecast_buffer_f_yes", "max_fill_for_buy",
              "min_seconds_to_close", "max_seconds_to_close"):
        v = g[k]
        print(f"    {k:24s} = {'inf' if v == math.inf else v}")
    print(f"\n  trades={s['n']}  WR={s['wr']*100:.1f}%  "
          f"net=${s['pnl']:+.3f}/contract  EV=${s['ev']:+.4f}/contract")
    by_side = defaultdict(list)
    for e in entries:
        by_side[e["side"]].append(e)
    for side in ("YES", "NO"):
        ss = _stats(by_side.get(side, []))
        if ss["n"]:
            print(f"    {side:3s}  n={ss['n']:3d}  WR={ss['wr']*100:5.1f}%  "
                  f"net=${ss['pnl']:+7.3f}  EV=${ss['ev']:+.4f}")


def report_sweep(trials: list[dict], g: dict, param: str) -> None:
    if param not in _SWEEPS:
        raise SystemExit(f"--sweep {param!r} unknown. choices: {', '.join(_SWEEPS)}")
    print(f"\n{'='*64}\n[sweep] {param}  (other gates held at current values)\n{'='*64}")
    is_secs = param in ("min_seconds_to_close", "max_seconds_to_close")
    hdr_val = f"{param} (h)" if is_secs else param
    print(f"  {hdr_val:>16s} | {'trades':>6s} {'WR%':>6s} {'net$/ct':>9s} {'EV$/ct':>9s}")
    print(f"  {'-'*16}-+-{'-'*6}-{'-'*6}-{'-'*9}-{'-'*9}")
    best = None
    for v in _SWEEPS[param]:
        gg = dict(g)
        gg[param] = v
        s = _stats(_select_entries(trials, gg))
        disp = ("inf" if v == math.inf else f"{v/3600:g}") if is_secs else f"{v:g}"
        print(f"  {disp:>16s} | {s['n']:6d} {s['wr']*100:6.1f} "
              f"{s['pnl']:+9.3f} {s['ev']:+9.4f}")
        if s["n"] > 0 and (best is None or s["ev"] > best[1]):
            best = (disp, s["ev"], s["n"])
    if best:
        print(f"\n  best EV at {param}={best[0]} "
              f"(EV=${best[1]:+.4f}/ct, {best[2]} trades) — "
              f"WATCH small-n; see in-sample caveat below.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Backtest / sweep gate thresholds over trials.")
    ap.add_argument("trials", type=Path)
    ap.add_argument("--sweep", default=None, help="gate to sweep: " + ", ".join(_SWEEPS))
    # per-gate overrides (apply to both single-report and as the held values in a sweep)
    ap.add_argument("--min-edge", type=float, dest="min_edge")
    ap.add_argument("--max-disagreement-edge", type=float, dest="max_disagreement_edge")
    ap.add_argument("--min-margin-sigma", type=float, dest="min_margin_sigma")
    ap.add_argument("--forecast-buffer-f", type=float, dest="forecast_buffer_f")
    ap.add_argument("--forecast-buffer-f-yes", type=float, dest="forecast_buffer_f_yes")
    ap.add_argument("--max-fill", type=float, dest="max_fill_for_buy")
    ap.add_argument("--min-seconds-to-close", type=float, dest="min_seconds_to_close")
    ap.add_argument("--max-seconds-to-close", type=float, dest="max_seconds_to_close")
    args = ap.parse_args()

    trials = _load_jsonl(args.trials)
    g = _default_gates()
    for k in list(g.keys()):
        v = getattr(args, k, None)
        if v is not None:
            g[k] = v

    n_mkts = len({t["market_ticker"] for t in trials})
    print(f"[backtest_gates] {len(trials)} trials over {n_mkts} settled markets "
          f"<- {args.trials}")

    if args.sweep:
        report_sweep(trials, g, args.sweep)
    else:
        report_single(trials, g)

    print("\n  CAVEAT: outcomes are real, but this is an IN-SAMPLE cut over a few")
    print("  settled days. Treat best-EV thresholds as hypotheses, re-check as")
    print("  more markets settle. Small trade counts are not significant.")


if __name__ == "__main__":
    main()
