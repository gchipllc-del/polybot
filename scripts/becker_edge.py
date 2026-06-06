#!/usr/bin/env python3
"""
becker_edge.py — calibration / favorite-longshot mispricing edge, with two
powers: (1) test the edge on ANY (price, outcome) data, and (2) REPURPOSE a
curve fit on one venue to TRADE another (Polymarket → Kalshi).

THE IDEA
--------
edge = fair_prob(price) - price, where fair_prob is an EMPIRICAL CALIBRATION
map: of all markets near price p, what fraction resolved YES? Longshots
overpriced / favorites underpriced (favorite-longshot bias) = a real edge from
market data alone. Bias is a general market property, so the CURVE transfers
across venues even though the data doesn't.

REPURPOSING BECKER (POLYMARKET) FOR KALSHI
------------------------------------------
The Jon-Becker dataset is Polymarket-only (no Kalshi quotes — those aren't
public). But you can fit the calibration curve on Polymarket history (lots of
data) and TRADE/settle it on Kalshi outcomes:

  --fit-data  = where the calibration curve is fit (e.g. normalized Polymarket
                pairs).  If omitted, the curve is fit on the time-EARLIER split
                of the trade data (single-venue OOS).
  data (pos.) = where trades are placed + settled on REAL outcomes (e.g. your
                Kalshi trials file).

The fit file may use DIFFERENT column names than the trade file — override them
with --fit-price-col/--fit-result-col/--fit-time-col/--fit-market-col (each
falls back to the trade-side col). So a Polymarket pairs export keeps its own
schema while the Kalshi trials keep theirs.

If fitting on Polymarket and trading Kalshi turns a profit on real Kalshi
outcomes, the bias transfers — a genuine, data-backed Kalshi edge.

FILLS
-----
--fills taker (default): pay the ask side (price / 1-price).
--fills maker: rest at the bid (needs --yes-bid-col/--no-bid-col) + rebate;
falls back to taker for any row missing the needed bid.

INPUT: JSONL or Parquet; columns configurable. Both files (fit + trade) must use
the SAME column names — normalize a Polymarket export to (price,result[,bids])
first. Parquet needs pandas+pyarrow.

USAGE
-----
  # direct, on your Kalshi trials (no Becker needed):
  python scripts/becker_edge.py data/trials_daily.jsonl \\
      --price-col market_p_yes --result-col result --time-col sample_at --sweep
  # repurpose: fit on Polymarket, trade Kalshi (each file its own schema):
  python scripts/becker_edge.py data/trials_daily.jsonl --fit-data data/poly_pairs.jsonl \\
      --price-col market_p_yes --result-col result --time-col sample_at \\
      --market-col market_ticker --dedup earliest \\
      --fit-price-col price --fit-result-col result --fit-time-col time \\
      --fit-market-col market --sweep
  # maker fills on Kalshi trials (needs yes_bid/no_bid columns):
  python scripts/becker_edge.py data/trials_daily.jsonl --price-col market_p_yes \\
      --result-col result --fills maker --yes-bid-col yes_bid --no-bid-col no_bid --sweep

READ-ONLY. No network, no trading. Pure measurement.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

FEE = 0.07       # Kalshi profit fee
REBATE = 0.005   # maker rebate $/contract (from maker_fill_sim)


def _as_yes(v):
    s = str(v).strip().lower()
    if s in ("yes", "1", "true", "y", "t"):
        return True
    if s in ("no", "0", "false", "n", "f"):
        return False
    return None


def _fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load_rows(path: Path, price_col, result_col, time_col, yes_bid_col, no_bid_col, market_col=None):
    """[{price, yes, time, yes_bid, no_bid, market}] from JSONL or Parquet."""
    want = [c for c in (price_col, result_col, time_col, yes_bid_col, no_bid_col, market_col) if c]
    recs: list[dict]
    if path.suffix in (".parquet", ".pq"):
        try:
            import pandas as pd
        except ImportError:
            raise SystemExit("parquet needs pandas+pyarrow, or pass a .jsonl")
        recs = pd.read_parquet(path, columns=want).to_dict("records")
    else:
        recs = []
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        recs.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    rows = []
    for r in recs:
        p = _fnum(r.get(price_col))
        y = _as_yes(r.get(result_col))
        if p is None or y is None or not (0.0 < p < 1.0):
            continue
        rows.append({
            "price": p, "yes": y,
            "time": r.get(time_col) if time_col else None,
            "yes_bid": _fnum(r.get(yes_bid_col)) if yes_bid_col else None,
            "no_bid": _fnum(r.get(no_bid_col)) if no_bid_col else None,
            "market": r.get(market_col) if market_col else None,
        })
    return rows


def dedup_by_market(rows, how):
    """Collapse to ONE row per market (by the 'market' key), keeping the
    earliest/latest by 'time'. CRITICAL when the input is one-row-per-SAMPLE
    (a market scanned many times) — without it the calibration both leaks the
    train/test split and counts a single outcome dozens of times. 'none' = off
    (already one row per market). Rows with no market id are kept as-is."""
    if how == "none":
        return rows
    best: dict = {}
    passthrough: list = []
    for r in rows:
        m = r.get("market")
        if m is None:
            passthrough.append(r)
            continue
        cur = best.get(m)
        if cur is None:
            best[m] = r
            continue
        rt, ct = r.get("time"), cur.get("time")
        if rt is not None and ct is not None:
            if (rt < ct) if how == "earliest" else (rt > ct):
                best[m] = r
    return list(best.values()) + passthrough


def fit_calibration(pairs, nbins):
    buckets = [[] for _ in range(nbins)]
    for p, y in pairs:
        buckets[min(nbins - 1, int(p * nbins))].append(y)
    centers, rates, counts = [], [], []
    for i, b in enumerate(buckets):
        if b:
            centers.append((i + 0.5) / nbins)
            rates.append(sum(b) / len(b))
            counts.append(len(b))
    return centers, rates, counts


def fair_prob(price, centers, rates):
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


def trade_pnl(row, fair, thr, fills):
    """Per-$1 P&L for the calibration trade, or (None, None) if no edge clears
    thr. Buy YES if underpriced, NO if overpriced. Maker rests at the side's
    bid + rebate (falls back to taker if that bid is missing)."""
    price = row["price"]
    e = fair - price
    if e > thr:
        side, taker_fill, bid = "YES", price, row.get("yes_bid")
    elif -e > thr:
        side, taker_fill, bid = "NO", 1.0 - price, row.get("no_bid")
    else:
        return None, None
    if fills == "maker" and bid is not None and 0.0 < bid < 1.0:
        fill, reb = bid, REBATE
    else:
        fill, reb = taker_fill, 0.0
    won = (side == "YES" and row["yes"]) or (side == "NO" and not row["yes"])
    if won:
        g = 1.0 - fill
        return g - max(0.0, g * FEE) + reb, side
    return -fill + reb, side


def backtest(fit_rows, trade_rows, nbins, thr, fills):
    centers, rates, _ = fit_calibration([(r["price"], r["yes"]) for r in fit_rows], nbins)
    n = wins = 0
    pnl = 0.0
    for r in trade_rows:
        f = fair_prob(r["price"], centers, rates)
        x, _ = trade_pnl(r, f, thr, fills)
        if x is None:
            continue
        n += 1
        pnl += x
        if x > 0:
            wins += 1
    return {"n": n, "wr": wins / n if n else 0.0, "pnl": round(pnl, 3),
            "ev": round(pnl / n, 4) if n else 0.0}


def main() -> None:
    ap = argparse.ArgumentParser(description="Calibration-edge backtest (+cross-venue, +maker)")
    ap.add_argument("data", type=Path, help="trade-on file (settle on its real outcomes)")
    ap.add_argument("--fit-data", type=Path, default=None,
                    help="fit calibration on THIS file (e.g. Polymarket); else time-split data")
    ap.add_argument("--price-col", default="market_p_yes")
    ap.add_argument("--result-col", default="result")
    ap.add_argument("--time-col", default=None)
    ap.add_argument("--yes-bid-col", default=None)
    ap.add_argument("--no-bid-col", default=None)
    ap.add_argument("--market-col", default=None,
                    help="market-id column to collapse ONE row per market (e.g. market_ticker). "
                         "REQUIRED for per-sample inputs or the split leaks + outcomes double-count.")
    ap.add_argument("--dedup", choices=("earliest", "latest", "none"), default="earliest",
                    help="with --market-col: keep earliest (how live fires) or latest sample per market")
    # Optional fit-file column overrides — let the --fit-data file (e.g. a
    # Polymarket pairs export) use its OWN natural column names instead of being
    # forced to match the trade file's schema. Each defaults to the trade-side
    # column when omitted.
    ap.add_argument("--fit-price-col", default=None)
    ap.add_argument("--fit-result-col", default=None)
    ap.add_argument("--fit-time-col", default=None)
    ap.add_argument("--fit-yes-bid-col", default=None)
    ap.add_argument("--fit-no-bid-col", default=None)
    ap.add_argument("--fit-market-col", default=None)
    ap.add_argument("--bins", type=int, default=20)
    ap.add_argument("--split", type=float, default=0.6, help="train fraction (single-venue mode)")
    ap.add_argument("--thr", type=float, default=0.05)
    ap.add_argument("--fills", choices=("taker", "maker"), default="taker")
    ap.add_argument("--sweep", action="store_true")
    a = ap.parse_args()

    trade = load_rows(a.data, a.price_col, a.result_col, a.time_col, a.yes_bid_col, a.no_bid_col, a.market_col)
    if a.market_col:
        before = len(trade)
        trade = dedup_by_market(trade, a.dedup)
        print(f"loaded {before} rows from {a.data}; collapsed to {len(trade)} markets "
              f"(one-per-market, {a.dedup})")
    else:
        print(f"loaded {len(trade)} rows to TRADE from {a.data}\n"
              f"  ⚠️  no --market-col: NOT deduped. If this is a per-SAMPLE file (a market "
              f"scanned many times),\n      the OOS split LEAKS and outcomes double-count — "
              f"pass --market-col market_ticker.")
    if a.fit_data:
        # fit-side cols fall back to the trade-side cols when not overridden, so
        # a same-schema fit file "just works" while a Polymarket export can keep
        # its own column names via --fit-*-col.
        fp = a.fit_price_col or a.price_col
        fr = a.fit_result_col or a.result_col
        ft = a.fit_time_col or a.time_col
        fyb = a.fit_yes_bid_col or a.yes_bid_col
        fnb = a.fit_no_bid_col or a.no_bid_col
        fm = a.fit_market_col or a.market_col
        fit = load_rows(a.fit_data, fp, fr, ft, fyb, fnb, fm)
        if fm:
            fit = dedup_by_market(fit, a.dedup)
        print(f"fitting calibration on {len(fit)} markets from {a.fit_data} (cross-venue)")
        fit_rows, trade_rows = fit, trade
        mode = f"fit={a.fit_data.name} → trade={a.data.name}"
    else:
        ordered = sorted(trade, key=lambda r: (r["time"] is None, r["time"]))
        k = int(len(ordered) * a.split)
        fit_rows, trade_rows = ordered[:k], ordered[k:]
        mode = f"single-venue OOS (fit first {a.split:.0%}, trade rest)"
        print(f"  {mode}: fit={len(fit_rows)} trade={len(trade_rows)}")

    # diagnostic calibration table on the fit set
    c, r, cnt = fit_calibration([(x["price"], x["yes"]) for x in fit_rows], a.bins)
    print("\ncalibration (fit set) — price bin vs realized YES rate:")
    print(f"  {'price':>6} {'realized':>9} {'gap':>8} {'n':>6}")
    for cc, rr, nn in zip(c, r, cnt):
        print(f"  {cc:>6.2f} {rr:>9.3f} {rr-cc:>+8.3f} {nn:>6}")

    print(f"\nOUT-OF-SAMPLE backtest  [{mode}]  fills={a.fills}")
    if a.sweep:
        print(f"  {'thr':>6} {'trades':>7} {'WR%':>6} {'net$':>9} {'EV$/ct':>8}")
        for thr in (0.02, 0.03, 0.05, 0.08, 0.10, 0.15, 0.20):
            s = backtest(fit_rows, trade_rows, a.bins, thr, a.fills)
            print(f"  {thr:>6.2f} {s['n']:>7} {s['wr']*100:>5.1f} {s['pnl']:>+9.2f} {s['ev']:>+8.4f}")
        print("\n  EV$/ct positive across thresholds = real edge worth building on."
              "\n  Flat/negative = no edge; don't build.")
    else:
        print("  ", backtest(fit_rows, trade_rows, a.bins, a.thr, a.fills))


if __name__ == "__main__":
    main()
