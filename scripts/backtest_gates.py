#!/usr/bin/env python3
"""
backtest_gates.py — replay historical markets through the entry gates and
report REAL-settlement win-rate / P&L, so we can tune the gates (PR #3:
timing + margin, plus min_edge) against history instead of waiting weeks of
live samples.

This is the lightweight, dependency-free distillation of the "best parts" of
two external repos (no heavy/GPL deps, live order path untouched):
  * Jon-Becker/prediction-market-analysis (MIT) — provides the DATA: real
    Kalshi markets *with settlement results*, including markets we didn't
    trade (no survivorship bias). See `_load_trials` for the expected schema
    and `docs` below for converting that dataset / a result-joined signal log.
  * evan-kolberg/prediction-market-backtesting (GPL — ideas only) — the
    METHOD: settle replayed signals against realized outcomes, model fee +
    slippage, and sweep parameters.

INPUT — a JSONL "trial" file, one row per (market sample we could have traded):
    {
      "market_ticker": "KXHIGHTATL-26JUN05-B80",
      "result": "yes" | "no" | "void",     # ACTUAL Kalshi settlement (required)
      "yes_ask": 0.55, "no_ask": 0.43,      # prices at decision time
      "seconds_to_close": 21600,            # at decision time
      # weather (optional — enables the margin-in-sigma gate):
      "forecast_f": 84.0, "strike_f": 81.0, "sigma_f": 3.0,
      # OR a precomputed fair value (either is fine):
      "fair_yes": 0.80
    }
Edge is derived from fair_yes when present, else from the normal model on
(forecast_f, strike_f, sigma_f). Rows missing both are skipped.

Produce a trials file from the Jon-Becker dataset (kalshi/markets has the
`result`) joined to your signal snapshots, or from a result-augmented signal
ledger. This script is the measurement engine; the loader is your adapter.

Read-only. No network, no trading.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass

FEE = 0.07  # Kalshi profit fee


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


@dataclass
class Gates:
    min_edge: float = 0.05
    min_margin_sigma: float = 0.0          # 0 = off
    min_seconds_to_close: float = 0.0
    max_seconds_to_close: float = 1e12     # 1e12 = off
    extreme_floor: float = 0.05
    extreme_ceil: float = 0.95
    slippage: float = 0.0                  # added to fill (worse) per trade


def _fair_yes(row: dict) -> float | None:
    """P(YES) from a precomputed fair_yes, else a normal model around the
    forecast vs strike (weather)."""
    if row.get("fair_yes") is not None:
        try:
            return max(0.0, min(1.0, float(row["fair_yes"])))
        except (TypeError, ValueError):
            return None
    try:
        fc = float(row["forecast_f"]); k = float(row["strike_f"]); sg = float(row["sigma_f"])
    except (KeyError, TypeError, ValueError):
        return None
    if sg <= 0:
        return 1.0 if fc >= k else 0.0
    return 1.0 - _norm_cdf((k - fc) / sg)   # P(temp >= strike)


def _f(row: dict, k: str):
    v = row.get(k)
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def evaluate(rows: list[dict], g: Gates) -> dict:
    """Replay every row through the gates; settle the entered trades on the
    real `result`; return aggregate stats. $1 staked per trade for
    comparability (contracts = 1/fill)."""
    won = lost = void = 0
    pnl = 0.0
    skips: dict[str, int] = {}

    def skip(r):
        skips[r] = skips.get(r, 0) + 1

    for row in rows:
        result = str(row.get("result") or "").lower()
        if result not in ("yes", "no", "void"):
            skip("no_result"); continue
        fair = _fair_yes(row)
        if fair is None:
            skip("no_fair"); continue
        stc = _f(row, "seconds_to_close")
        if stc is not None and not (g.min_seconds_to_close <= stc <= g.max_seconds_to_close):
            skip("timing"); continue
        yes_ask = _f(row, "yes_ask"); no_ask = _f(row, "no_ask")
        edge_yes = (fair - yes_ask) if yes_ask is not None else None
        edge_no = ((1.0 - fair) - no_ask) if no_ask is not None else None
        # pick the larger positive edge
        side = fill = None
        if edge_yes is not None and (edge_no is None or edge_yes >= edge_no) and edge_yes >= g.min_edge:
            side, fill = "yes", yes_ask
        elif edge_no is not None and edge_no >= g.min_edge:
            side, fill = "no", no_ask
        if side is None:
            skip("no_edge"); continue
        # margin-in-sigma gate (weather): forecast must be k*sigma from strike
        if g.min_margin_sigma > 0:
            fc = _f(row, "forecast_f"); k = _f(row, "strike_f"); sg = _f(row, "sigma_f")
            if fc is not None and k is not None and sg and sg > 0:
                if abs(fc - k) < g.min_margin_sigma * sg:
                    skip("margin"); continue
        fill = min(0.99, max(0.01, fill + g.slippage))
        if not (g.extreme_floor <= fill <= g.extreme_ceil):
            skip("extreme"); continue
        # settle on real result, $1 stake
        if result == "void":
            void += 1; continue
        win = (result == side)
        if win:
            profit = (1.0 / fill - 1.0) * (1.0 - FEE)   # $ profit per $1 staked
            pnl += profit; won += 1
        else:
            pnl += -1.0; lost += 1

    settled = won + lost
    return {
        "entered": settled + void, "won": won, "lost": lost, "void": void,
        "win_rate": round(won / settled, 4) if settled else 0.0,
        "pnl_per_$1": round(pnl, 4),
        "roi": round(pnl / settled, 4) if settled else 0.0,
        "skips": skips,
    }


def _load_trials(path: str) -> list[dict]:
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description="Backtest entry gates vs real settlement")
    ap.add_argument("trials", help="JSONL of trial rows (see module docstring)")
    ap.add_argument("--min-edge", type=float, default=0.05)
    ap.add_argument("--min-margin-sigma", type=float, default=0.0)
    ap.add_argument("--max-seconds-to-close", type=float, default=1e12)
    ap.add_argument("--min-seconds-to-close", type=float, default=0.0)
    ap.add_argument("--slippage", type=float, default=0.0)
    ap.add_argument("--sweep", choices=["min_margin_sigma", "max_seconds_to_close", "min_edge"],
                    help="grid-sweep one gate and print the table")
    args = ap.parse_args()

    rows = _load_trials(args.trials)
    base = Gates(min_edge=args.min_edge, min_margin_sigma=args.min_margin_sigma,
                 max_seconds_to_close=args.max_seconds_to_close,
                 min_seconds_to_close=args.min_seconds_to_close, slippage=args.slippage)
    print(f"loaded {len(rows)} trial rows")
    print("baseline:", evaluate(rows, base))

    if args.sweep:
        grids = {
            "min_margin_sigma": [0.0, 0.5, 1.0, 1.5, 2.0, 2.5],
            "max_seconds_to_close": [1800, 3600, 7200, 14400, 28800, 1e12],
            "min_edge": [0.03, 0.05, 0.08, 0.10, 0.15, 0.20],
        }
        print(f"\nsweep {args.sweep}:")
        print(f"  {'value':>12} {'entered':>8} {'WR':>7} {'pnl/$1':>9} {'roi':>8}")
        for v in grids[args.sweep]:
            g = Gates(**{**base.__dict__, args.sweep: v})
            r = evaluate(rows, g)
            print(f"  {v:>12} {r['won']+r['lost']:>8} {r['win_rate']*100:>6.1f}% "
                  f"{r['pnl_per_$1']:>+9.2f} {r['roi']*100:>+7.1f}%")


if __name__ == "__main__":
    main()
