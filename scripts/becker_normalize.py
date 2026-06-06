#!/usr/bin/env python3
"""becker_normalize.py — turn a Jon-Becker Polymarket dump into a normalized
(price, result) PAIRS file that `becker_edge.py --fit-data` can fit a
calibration curve on, to TRADE/settle on Kalshi outcomes (cross-venue transfer).

WHY
---
Becker's data is Polymarket-only. We don't trade on it — we fit the
favorite-longshot / calibration CURVE on its deep history, then check whether
that curve still prints +EV on REAL Kalshi settlements (becker_edge's
positional trade file). If it does, the bias is structural, not a 41-market
fluke.

WHAT IT PRODUCES
----------------
A JSONL where each line is one (YES-price, YES-outcome) observation of a RESOLVED
Polymarket market:
    {"market": <id>, "time": <iso/epoch>, "price": <0..1 YES prob>,
     "result": "yes"|"no", "yes_bid": <opt>, "no_bid": <opt>}
Feed it to becker_edge with:
    --fit-price-col price --fit-result-col result --fit-time-col time
    --fit-market-col market

TWO POLYMARKET GOTCHAS THIS HANDLES
-----------------------------------
1. Per-token prices. A binary market has a YES token and a NO token, each with
   its own price. A trade at 0.30 on the NO token == a YES price of 0.70. Pass
   --trade-outcome-col + --yes-labels so NO-token trades get inverted to YES.
   Omit --trade-outcome-col only if the price column is ALREADY the YES price.
2. Resolution. The market's winning outcome -> result yes/no via --yes-labels
   (also accepts numeric: >0.5 == yes, e.g. an outcomePrices [1,0]).

DEDUP (mirrors becker_edge's per-market collapse)
-------------------------------------------------
A market has many trades; emitting all of them double-counts one outcome and
correlates the fit. Default --per-market last keeps ONE pair per market (the
last trade before resolution = the market's settled-in price). Use `all` only
if you deliberately want every trade as a calibration point.

WORKFLOW (you can't show me your files, so inspect first)
---------------------------------------------------------
  1. INSPECT — see the real format/columns/sample rows, decide the mapping:
       python scripts/becker_normalize.py --inspect \\
           --markets <markets file> --trades <trades file>
  2. BUILD — with the columns inspect revealed:
       python scripts/becker_normalize.py \\
           --markets <markets file> --trades <trades file> \\
           --market-id-col conditionId --market-result-col winningOutcome \\
           --trade-market-col conditionId --trade-price-col price \\
           --trade-time-col timestamp --trade-outcome-col outcome \\
           --yes-labels Yes,1,true --out data/poly_pairs.jsonl
  3. CROSS-VENUE TEST:
       python scripts/becker_edge.py data/trials_daily.jsonl \\
           --fit-data data/poly_pairs.jsonl \\
           --price-col market_p_yes --result-col result --time-col sample_at \\
           --market-col market_ticker --dedup earliest \\
           --fit-price-col price --fit-result-col result --fit-time-col time \\
           --fit-market-col market --sweep

Supports .jsonl / .json / .csv / .parquet (parquet & csv need pandas).
READ-ONLY w.r.t. everything except --out. No network.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def _load_any(path: Path) -> list[dict]:
    """Load csv/json/jsonl/parquet into a list of dict rows."""
    suf = path.suffix.lower()
    if suf in (".jsonl", ".ndjson"):
        out = []
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        return out
    if suf == ".json":
        data = json.loads(Path(path).read_text())
        if isinstance(data, dict):
            # common shapes: {"data": [...]} / {"markets": [...]} / {"trades": [...]}
            for k in ("data", "markets", "trades", "results", "rows"):
                if isinstance(data.get(k), list):
                    return data[k]
            return [data]
        return list(data)
    if suf in (".parquet", ".pq"):
        import pandas as pd
        return pd.read_parquet(path).to_dict("records")
    if suf == ".csv":
        import pandas as pd
        return pd.read_csv(path).to_dict("records")
    raise SystemExit(f"unsupported file type: {path} (use jsonl/json/csv/parquet)")


def _inspect(path: Path) -> None:
    rows = _load_any(path)
    print(f"\n=== {path}  ({path.suffix or 'no-ext'}) ===")
    print(f"  rows: {len(rows)}")
    if not rows:
        return
    cols = list(rows[0].keys())
    print(f"  columns ({len(cols)}): {', '.join(map(str, cols))}")
    print("  sample rows:")
    for r in rows[:3]:
        # truncate long values so the dump stays readable
        slim = {k: (str(v)[:60] + "…" if len(str(v)) > 60 else v) for k, v in r.items()}
        print(f"    {json.dumps(slim, default=str)}")


def _fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _is_yes(v, yes_labels: set[str]) -> bool | None:
    """Map a value to YES/NO. Accepts labels (yes_labels), and numeric (>0.5)."""
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in yes_labels:
        return True
    if s in ("no", "0", "false", "n", "f"):
        return False
    n = _fnum(v)
    if n is not None:
        return n > 0.5
    return None


def build(args) -> None:
    yes_labels = {x.strip().lower() for x in args.yes_labels.split(",") if x.strip()}

    # --- resolutions: {market_id: yes_bool} -------------------------------
    market_rows = _load_any(args.markets)
    resolved: dict[str, bool] = {}
    for m in market_rows:
        mid = m.get(args.market_id_col)
        if mid is None:
            continue
        y = _is_yes(m.get(args.market_result_col), yes_labels)
        if y is not None:
            resolved[str(mid)] = y
    print(f"[normalize] resolved markets: {len(resolved)} / {len(market_rows)} "
          f"(rest unresolved/parse-failed — excluded)")

    # --- trades -> YES-price observations on resolved markets -------------
    trade_rows = _load_any(args.trades) if args.trades else market_rows
    pairs_by_market: dict[str, dict] = {}
    all_pairs: list[dict] = []
    kept = inverted = skipped = 0
    for t in trade_rows:
        mid = t.get(args.trade_market_col)
        if mid is None or str(mid) not in resolved:
            skipped += 1
            continue
        p = _fnum(t.get(args.trade_price_col))
        if p is None or not (0.0 < p < 1.0):
            skipped += 1
            continue
        # invert NO-token price to a YES price if an outcome col is given
        yes_price = p
        if args.trade_outcome_col:
            side_yes = _is_yes(t.get(args.trade_outcome_col), yes_labels)
            if side_yes is None:
                skipped += 1
                continue
            if not side_yes:
                yes_price = 1.0 - p
                inverted += 1
        rec = {
            "market": str(mid),
            "time": t.get(args.trade_time_col) if args.trade_time_col else None,
            "price": round(yes_price, 6),
            "result": "yes" if resolved[str(mid)] else "no",
        }
        if args.trade_yes_bid_col:
            yb = _fnum(t.get(args.trade_yes_bid_col))
            if yb is not None:
                rec["yes_bid"] = yb
        if args.trade_no_bid_col:
            nb = _fnum(t.get(args.trade_no_bid_col))
            if nb is not None:
                rec["no_bid"] = nb
        kept += 1
        if args.per_market == "all":
            all_pairs.append(rec)
        else:
            cur = pairs_by_market.get(str(mid))
            if cur is None:
                pairs_by_market[str(mid)] = rec
            else:
                rt, ct = rec["time"], cur["time"]
                if rt is not None and ct is not None:
                    take = (rt < ct) if args.per_market == "first" else (rt > ct)
                    if take:
                        pairs_by_market[str(mid)] = rec

    pairs = all_pairs if args.per_market == "all" else list(pairs_by_market.values())
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as f:
        for r in pairs:
            f.write(json.dumps(r) + "\n")

    n_mkts = len({r["market"] for r in pairs})
    yes_n = sum(1 for r in pairs if r["result"] == "yes")
    print(f"[normalize] kept {kept} trades on resolved markets "
          f"({inverted} NO-token prices inverted to YES; {skipped} skipped)")
    print(f"[normalize] wrote {len(pairs)} pairs over {n_mkts} markets "
          f"({yes_n} yes / {len(pairs)-yes_n} no) -> {out}")
    print(f"  per-market={args.per_market}. Feed to becker_edge with "
          f"--fit-price-col price --fit-result-col result --fit-time-col time "
          f"--fit-market-col market")


def main() -> None:
    ap = argparse.ArgumentParser(description="Normalize a Becker Polymarket dump to (price,result) pairs.")
    ap.add_argument("--inspect", action="store_true",
                    help="print format/columns/sample rows of --markets/--trades and exit")
    ap.add_argument("--markets", type=Path, required=True,
                    help="markets/resolutions file (carries the winning outcome)")
    ap.add_argument("--trades", type=Path, default=None,
                    help="trades/prices file. If omitted, --markets is used for both "
                         "(when a single file carries price+result per row).")
    ap.add_argument("--out", type=Path, default=Path("data/poly_pairs.jsonl"))
    # market/resolution mapping
    ap.add_argument("--market-id-col", default="conditionId")
    ap.add_argument("--market-result-col", default="winningOutcome")
    # trade mapping
    ap.add_argument("--trade-market-col", default="conditionId")
    ap.add_argument("--trade-price-col", default="price")
    ap.add_argument("--trade-time-col", default="timestamp")
    ap.add_argument("--trade-outcome-col", default=None,
                    help="column saying which token the price is for (Yes/No). "
                         "Omit ONLY if --trade-price-col is already the YES price.")
    ap.add_argument("--trade-yes-bid-col", default=None)
    ap.add_argument("--trade-no-bid-col", default=None)
    ap.add_argument("--yes-labels", default="yes,1,true,y,t",
                    help="comma values that mean YES (for outcome + resolution)")
    ap.add_argument("--per-market", choices=("last", "first", "all"), default="last",
                    help="one pair per market (last/first trade) or every trade")
    args = ap.parse_args()

    if args.inspect:
        _inspect(args.markets)
        if args.trades:
            _inspect(args.trades)
        return
    build(args)


if __name__ == "__main__":
    main()
