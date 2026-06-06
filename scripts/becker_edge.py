#!/usr/bin/env python3
"""
becker_edge.py — calibration / favorite-longshot mispricing edge across ALL
markets, fit from the Jon-Becker historical Kalshi dataset.

THE IDEA (your "gate = the edge Becker says a trade has")
--------------------------------------------------------
The edge is  fair_prob(market_price) - market_price , where fair_prob is an
EMPIRICAL CALIBRATION map fit from history: of all markets that traded near
price p, what fraction actually resolved YES? If longshots are systematically
overpriced and favorites underpriced (the classic favorite-longshot bias),
that gap is a real edge — and it's computable from market data ALONE (no
weather/crypto forecast), so it scans every category.

WHY THIS IS THE HONEST VERSION
------------------------------
Calibration fit on the SAME data you trade is guaranteed to look profitable and
means nothing. So this splits history by TIME: fit the calibration on the
earlier portion, then trade `edge > threshold` on the LATER portion and settle
on real outcomes. ONLY the out-of-sample (OOS) result counts. If the OOS sweep
is flat/negative, there is no calibration edge — and we do NOT build a sleeve.

INPUT
-----
Historical RESOLVED markets, JSONL or Parquet, one row per market (a decision-
time snapshot). Columns are configurable:
  --price-col   market YES price in [0,1]  (e.g. last_price / yes_ask / mid)
  --result-col  settlement: yes/no or 1/0/true/false
  --time-col    timestamp for the train/test split (optional; else row order)
Parquet needs pandas+pyarrow; JSONL needs nothing.

USAGE
-----
  python scripts/becker_edge.py --data data/becker/kalshi_markets.parquet \\
      --price-col last_price --result-col result --time-col close_time --sweep
  python scripts/becker_edge.py --data pairs.jsonl --price-col p --result-col y

READ-ONLY. No network, no trading. Pure measurement — the make-or-break test
for whether a scan-all-markets mispricing sleeve is worth building.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

FEE = 0.07  # Kalshi profit fee


def _as_yes(v) -> bool | None:
    s = str(v).strip().lower()
    if s in ("yes", "1", "true", "y", "t"):
        return True
    if s in ("no", "0", "false", "n", "f"):
        return False
    return None


def load_rows(path: Path, price_col: str, result_col: str, time_col: str | None):
    """Return [(price, result_yes_bool, time_or_None)] from JSONL or Parquet."""
    rows: list[tuple[float, bool, object]] = []
    if path.suffix in (".parquet", ".pq"):
        try:
            import pandas as pd
        except ImportError:
            raise SystemExit("parquet needs pandas+pyarrow (pip install pandas pyarrow), "
                             "or pass a .jsonl")
        df = pd.read_parquet(path, columns=[c for c in (price_col, result_col, time_col) if c])
        for rec in df.to_dict("records"):
            _add(rows, rec, price_col, result_col, time_col)
    else:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    _add(rows, json.loads(line), price_col, result_col, time_col)
                except json.JSONDecodeError:
                    continue
    return rows


def _add(rows, rec, price_col, result_col, time_col):
    try:
        p = float(rec[price_col])
    except (KeyError, TypeError, ValueError):
        return
    y = _as_yes(rec.get(result_col))
    if y is None or not (0.0 < p < 1.0):
        return
    rows.append((p, y, rec.get(time_col) if time_col else None))


def fit_calibration(pairs: list[tuple[float, bool]], nbins: int):
    """Binned empirical YES-rate by price bin. Returns (centers, rates) for the
    non-empty bins, usable by fair_prob()."""
    buckets: list[list[bool]] = [[] for _ in range(nbins)]
    for p, y in pairs:
        buckets[min(nbins - 1, int(p * nbins))].append(y)
    centers, rates, counts = [], [], []
    for i, b in enumerate(buckets):
        if b:
            centers.append((i + 0.5) / nbins)
            rates.append(sum(b) / len(b))
            counts.append(len(b))
    return centers, rates, counts


def fair_prob(price: float, centers: list[float], rates: list[float]) -> float:
    """Calibrated P(YES) at `price` via linear interpolation between bin rates."""
    if not centers:
        return price
    if price <= centers[0]:
        return rates[0]
    if price >= centers[-1]:
        return rates[-1]
    for i in range(1, len(centers)):
        if price <= centers[i]:
            t = (price - centers[i - 1]) / (centers[i] - centers[i - 1])
            return rates[i - 1] + t * (rates[i] - rates[i - 1])
    return rates[-1]


def trade_pnl(price: float, result_yes: bool, fair: float, thr: float, fee: float = FEE):
    """Per-$1-stake P&L (1 contract) for the calibration trade, or (None, None)
    if no edge clears the threshold. Buy YES at `price` if underpriced; buy NO
    at (1-price) if overpriced."""
    e = fair - price
    if e > thr:                       # YES underpriced → buy YES at price
        if result_yes:
            g = 1.0 - price
            return g - max(0.0, g * fee), "YES"
        return -price, "YES"
    if -e > thr:                      # YES overpriced → buy NO at (1-price)
        q = 1.0 - price
        if not result_yes:
            g = 1.0 - q
            return g - max(0.0, g * fee), "NO"
        return -q, "NO"
    return None, None


def backtest_oos(rows, nbins: int, thr: float, split_frac: float):
    """Fit calibration on the first split_frac of (time-sorted) rows, trade
    edge>thr on the rest, settle on real outcomes. Returns stats dict."""
    ordered = sorted(rows, key=lambda r: (r[2] is None, r[2]))  # by time if present
    k = int(len(ordered) * split_frac)
    train = [(p, y) for p, y, _ in ordered[:k]]
    test = ordered[k:]
    centers, rates, _ = fit_calibration(train, nbins)
    n = wins = 0
    pnl = 0.0
    for p, y, _ in test:
        f = fair_prob(p, centers, rates)
        x, side = trade_pnl(p, y, f, thr)
        if x is None:
            continue
        n += 1
        pnl += x
        if x > 0:
            wins += 1
    return {"n": n, "wr": wins / n if n else 0.0, "pnl": round(pnl, 3),
            "ev": round(pnl / n, 4) if n else 0.0,
            "train": len(train), "test": len(test)}


def main() -> None:
    ap = argparse.ArgumentParser(description="Becker calibration-edge OOS backtest")
    ap.add_argument("--data", required=True, type=Path)
    ap.add_argument("--price-col", default="last_price")
    ap.add_argument("--result-col", default="result")
    ap.add_argument("--time-col", default=None)
    ap.add_argument("--bins", type=int, default=20)
    ap.add_argument("--split", type=float, default=0.6, help="train fraction (time-ordered)")
    ap.add_argument("--thr", type=float, default=0.05)
    ap.add_argument("--sweep", action="store_true", help="sweep the edge threshold")
    args = ap.parse_args()

    rows = load_rows(args.data, args.price_col, args.result_col, args.time_col)
    print(f"loaded {len(rows)} resolved markets from {args.data}")
    if len(rows) < 50:
        print("  too few rows for a meaningful calibration — need hundreds+.")
    # show the in-sample calibration table (diagnostic)
    centers, rates, counts = fit_calibration([(p, y) for p, y, _ in rows], args.bins)
    print("\ncalibration (all data) — price bin vs realized YES rate:")
    print(f"  {'price':>6} {'realized':>9} {'gap(real-price)':>16} {'n':>6}")
    for c, r, cnt in zip(centers, rates, counts):
        print(f"  {c:>6.2f} {r:>9.3f} {r-c:>+16.3f} {cnt:>6}")

    if args.sweep:
        print(f"\nOUT-OF-SAMPLE backtest (fit on first {args.split:.0%}, trade the rest):")
        print(f"  {'thr':>6} {'trades':>7} {'WR%':>6} {'net$':>9} {'EV$/ct':>8}")
        for thr in (0.02, 0.03, 0.05, 0.08, 0.10, 0.15, 0.20):
            s = backtest_oos(rows, args.bins, thr, args.split)
            print(f"  {thr:>6.2f} {s['n']:>7} {s['wr']*100:>5.1f} "
                  f"{s['pnl']:>+9.2f} {s['ev']:>+8.4f}")
        print("\n  Read the EV$/ct column: positive across thresholds = a real,"
              "\n  out-of-sample calibration edge worth building a sleeve on."
              "\n  Flat/negative = no edge; do NOT build the sleeve.")
    else:
        s = backtest_oos(rows, args.bins, args.thr, args.split)
        print(f"\nOOS @ thr={args.thr}: {s}")


if __name__ == "__main__":
    main()
