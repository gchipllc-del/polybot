#!/usr/bin/env python3
"""maker_replay — would POSTING quotes (maker) have earned what takers lose?

Motivation: at 1,800+ independent windows the taker experiment converged on a clean
negative — buying either side, in nearly every band, loses roughly spread+fees (eight
cells significantly -EV, zero +EV). That pattern is itself a measurement of the MAKER's
revenue. This replays the maker side from the SAME collected history, no new data needed:

  At the first observation of each market in each time band, "post" a bid at the current
  best bid on each side (joining the queue). The posting FILLS only if a later observation
  of that market shows that side's ASK at or below our posted price - i.e. the market
  actually traded/quoted through our level. Filled positions settle on Kalshi's own
  result and pay a maker fee.

HONESTY BOX - the two ways this model lies, in opposite directions:
  * OPTIMISTIC: queue position is ignored. When price trades through our level we assume
    OUR order filled; in reality others were ahead of us. Real fill rates are LOWER.
  * PESSIMISTIC: we require the visible top-of-book ask to cross our bid. Fills that
    happen when a market sell order lifts resting bids WITHOUT the quote crossing are
    missed. Real fill rates are HIGHER.
  These do not cancel; they bound. Treat results as a feasibility screen, not a P&L
  forecast. Weather taught us break-even maker fill rates of 83-95% kill naive maker
  dreams - the fill-rate column here is as important as the EV column.
  * ADVERSE SELECTION is partially captured (we fill exactly when price moves toward us,
    then settle on the real outcome) but latency games are not.
  Maker fee assumed ceil(0.0175*P*(1-P)) per contract - conservative; many Kalshi series
  charge makers zero.

  py scripts/maker_replay.py            # replay + report
  py scripts/maker_replay.py --json
  py scripts/maker_replay.py selftest
"""
from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

LOG = Path(os.environ.get("STAGE0_LOG") or (ROOT / "data" / "stage0_crypto.jsonl"))

BANDS = {">10min": (10.0, 1e9), "2-10min": (2.0, 10.0), "<2min": (0.0, 2.0)}
MAX_SPREAD = 0.15   # data-validity: see edge_analysis - never post into a placeholder book


def _book_formed(r) -> bool:
    try:
        yb, ya = float(r.get("yes_bid")), float(r.get("yes_ask"))
    except (TypeError, ValueError):
        return False
    return 0.01 <= yb < ya <= 0.99 and (ya - yb) <= MAX_SPREAD
BUCKETS = [(1, 5), (5, 10), (10, 20), (20, 35), (35, 50),
           (50, 65), (65, 80), (80, 90), (90, 95), (95, 99)]
MIN_N = 100


def maker_fee(price: float, contracts: int = 1) -> float:
    """Conservative maker fee: quarter of the taker rate, still ceil'd to the cent."""
    if not (0 < price < 1):
        return 0.0
    return math.ceil(0.0175 * contracts * price * (1.0 - price) * 100) / 100.0


def taker_fee(price: float) -> float:
    if not (0 < price < 1):
        return 0.0
    return math.ceil(0.07 * price * (1.0 - price) * 100) / 100.0


def wilson(p: float, n: int, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return (0.0, 1.0)
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z / d * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, c - h), min(1.0, c + h))


def _window_of(ticker: str) -> str:
    parts = str(ticker or "").rsplit("-", 1)
    return parts[0] if len(parts) == 2 else str(ticker)


def _band_of(mins_left) -> str | None:
    if mins_left is None:
        return None
    for name, (lo, hi) in BANDS.items():
        if lo <= mins_left < hi:
            return name
    return None


def _bucket_of(price: float) -> str | None:
    return next((f"{lo:02d}-{hi:02d}c" for lo, hi in BUCKETS
                 if lo <= price * 100 < hi), None)


def load_rows(path: Path) -> list[dict]:
    rows = []
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def build(rows: list[dict]) -> dict:
    """Replay maker postings. One posting per (ticker, band, side): join the bid at the
    first observation in the band; fill iff any LATER obs of that ticker shows that
    side's ask <= our posted price; settle on the real result."""
    settles = {r["ticker"]: r["result"] for r in rows if r.get("t") == "settle"}

    # obs sequences per ticker, in time order (ISO timestamps sort lexicographically)
    by_ticker: dict[str, list[dict]] = {}
    for r in rows:
        if r.get("t") == "obs" and r.get("ticker"):
            by_ticker.setdefault(r["ticker"], []).append(r)
    for seq in by_ticker.values():
        seq.sort(key=lambda r: r.get("ts", ""))

    postings = []
    for ticker, seq in by_ticker.items():
        result = settles.get(ticker)
        if result is None:
            continue
        posted: set = set()                      # bands already posted for this ticker
        for i, r in enumerate(seq):
            band = _band_of(r.get("mins_left"))
            if band is None or band in posted:
                continue
            if not _book_formed(r):
                continue                 # wait for a formed book before posting
            posted.add(band)
            for side in ("yes", "no"):
                bid = r.get(f"{side}_bid")
                if bid is None:
                    continue
                try:
                    bid = float(bid)
                except (TypeError, ValueError):
                    continue
                if not (0 < bid < 1):
                    continue
                # Dual fill bounds, per hftbacktest's queue-model semantics (repo scan,
                # verified at source): with queue position unknowable from snapshots,
                # honest replay reports a BAND, not a point.
                #   touch  (optimistic):   later ask <= our bid - price reached our level
                #   cross  (conservative): later ask <  our bid - price traded THROUGH
                #                          our level, so even the back of the queue filled
                touch = cross = False
                for later in seq[i + 1:]:
                    ask = later.get(f"{side}_ask")
                    if ask is None:
                        continue
                    try:
                        a = float(ask)
                    except (TypeError, ValueError):
                        continue
                    if a <= bid + 1e-9:
                        touch = True
                        if a < bid - 1e-9:
                            cross = True
                            break
                won = (result == side)
                fee = maker_fee(bid)
                fill_pnl = ((1.0 - bid) if won else -bid) - fee
                postings.append({"ticker": ticker, "window": _window_of(ticker),
                                 "band": band, "side": side, "price": bid,
                                 "bucket": _bucket_of(bid),
                                 "filled": touch, "filled_cross": cross,
                                 "won": won if touch else None,
                                 "pnl": round(fill_pnl if touch else 0.0, 4),
                                 "pnl_cross": round(fill_pnl if cross else 0.0, 4)})
    return {"postings": postings, "n_settled": len(settles)}


def cell_table(postings: list[dict]) -> dict:
    out: dict[tuple, dict] = {}
    for p in postings:
        if p["bucket"] is None:
            continue
        c = out.setdefault((p["band"], p["bucket"]), {
            "posts": 0, "fills": 0, "wins": 0, "cost": 0.0, "pnl": 0.0,
            "fills_cross": 0, "pnl_cross": 0.0, "windows": set()})
        c["posts"] += 1
        c["windows"].add(p["window"])
        if p["filled"]:
            c["fills"] += 1
            c["cost"] += p["price"]
            c["wins"] += 1 if p["won"] else 0
            c["pnl"] += p["pnl"]
        if p.get("filled_cross"):
            c["fills_cross"] += 1
            c["pnl_cross"] += p["pnl_cross"]
    return out


def taker_cells() -> dict:
    """Taker EV per cell from the same dataset, for the side-by-side comparison."""
    from edge_analysis import load_events, cell_stats
    return cell_stats(load_events(LOG))


def print_report(rep: dict) -> None:
    postings = rep["postings"]
    print("=" * 78)
    print("MAKER REPLAY - would posting quotes have earned what takers lose?")
    print("=" * 78)
    if not postings:
        print("no postings replayable yet.")
        return
    n_fills = sum(1 for p in postings if p["filled"])
    print(f"{len(postings)} postings on {rep['n_settled']} settled markets | "
          f"{n_fills} filled ({100*n_fills/len(postings):.0f}% fill rate overall)")
    print()
    print("HONESTY BOX: queue position ignored (optimistic) but fills require the visible")
    print("quote to trade through (pessimistic). Feasibility screen, not a P&L forecast.")
    print()
    tk = {}
    try:
        tk = taker_cells()
    except Exception:  # noqa: BLE001
        pass
    print("  band     bucket   posts  touch%  cross%  WR(fill)  $/fill  $/post[T]  $/post[C]  taker$/bet  read")
    cells = cell_table(postings)
    rows_out = []
    for (band, bucket), c in cells.items():
        if c["posts"] < MIN_N:
            continue
        fr = c["fills"] / c["posts"]
        fr_x = c["fills_cross"] / c["posts"]
        wr = (c["wins"] / c["fills"]) if c["fills"] else None
        per_fill = (c["pnl"] / c["fills"]) if c["fills"] else 0.0
        per_post = c["pnl"] / c["posts"]
        per_post_x = c["pnl_cross"] / c["posts"]
        t = tk.get((band, bucket))
        tk_ev = (t["pnl"] / t["n"]) if t and t["n"] else None
        # significance on per-POSTING EV over windows is what matters; quick screen:
        # fills' win rate CI vs fill-price breakeven
        read = ""
        if c["fills"] >= 50 and wr is not None:
            cost = c["cost"] / c["fills"]
            lo, hi = wilson(wr, c["fills"])
            be = cost + 0.01
            if lo > be:
                read = "maker +EV (screen)"
            elif hi < be:
                read = "maker -EV even filled"
            else:
                read = "inconclusive"
        else:
            read = "few fills"
        rows_out.append((per_post, band, bucket, c["posts"], fr, fr_x, wr,
                         per_fill, per_post_x, tk_ev, read))
    for per_post, band, bucket, posts, fr, fr_x, wr, per_fill, per_post_x, tk_ev, read \
            in sorted(rows_out, reverse=True):
        wr_s = "  -  " if wr is None else f"{wr*100:5.1f}%"
        tk_s = "   -   " if tk_ev is None else f"{tk_ev:+7.3f}"
        print(f"  {band:8} {bucket:8} {posts:>5} {fr*100:5.1f}%  {fr_x*100:5.1f}%  {wr_s}  "
              f"{per_fill:+7.3f}  {per_post:+7.3f}   {per_post_x:+7.3f}  {tk_s}   {read}")
    print()
    print("$/post[T] counts TOUCH fills (optimistic: front of queue); $/post[C] counts only")
    print("CROSS fills (conservative: even the back of the queue filled). Truth is between;")
    print("a maker cell is credible only when [C] is also positive.")
    print("Compare $/post (maker) with taker$/bet: cells where takers bleed and makers")
    print("print are the spread being harvested - IF real fill rates cooperate.")
    print("=" * 78)


# ── selftest ─────────────────────────────────────────────────────────────────

def _selftest() -> int:
    # fee
    assert maker_fee(0.80) == 0.01
    assert taker_fee(0.80) == 0.02

    def obs(tk, ts, mins, yb, ya, nb, na):
        return {"t": "obs", "ticker": tk, "ts": ts, "mins_left": mins,
                "yes_bid": yb, "yes_ask": ya, "no_bid": nb, "no_ask": na}

    rows = [
        # A: post yes@0.80; later ask drops to 0.79 -> FILLED; settles yes -> +0.20-fee
        obs("W1-45", "t1", 5.0, 0.80, 0.84, 0.14, 0.18),
        obs("W1-45", "t2", 4.0, 0.76, 0.79, 0.19, 0.23),
        {"t": "settle", "ticker": "W1-45", "result": "yes"},
        # B: post yes@0.80; ask never reaches 0.80 -> UNFILLED; pnl 0
        obs("W2-45", "t1", 5.0, 0.80, 0.84, 0.14, 0.18),
        obs("W2-45", "t2", 4.0, 0.82, 0.86, 0.12, 0.16),
        {"t": "settle", "ticker": "W2-45", "result": "yes"},
        # C: filled then LOSES (adverse selection captured: price moved to us, we lose)
        obs("W3-45", "t1", 5.0, 0.80, 0.84, 0.14, 0.18),
        obs("W3-45", "t2", 4.0, 0.60, 0.78, 0.20, 0.38),
        {"t": "settle", "ticker": "W3-45", "result": "no"},
        # D: unsettled market -> excluded entirely
        obs("W4-45", "t1", 5.0, 0.80, 0.84, 0.14, 0.18),
    ]
    rep = build(rows)
    ps = {(p["ticker"], p["side"]): p for p in rep["postings"]}
    a = ps[("W1-45", "yes")]
    assert a["filled"] and a["won"] and abs(a["pnl"] - (0.20 - 0.01)) < 1e-9, a
    assert a["filled_cross"], a               # 0.79 < 0.80: strict cross too
    b = ps[("W2-45", "yes")]
    assert not b["filled"] and b["pnl"] == 0.0, b
    c = ps[("W3-45", "yes")]
    assert c["filled"] and c["won"] is False and abs(c["pnl"] - (-0.80 - 0.01)) < 1e-9, c
    # TOUCH-only vs CROSS distinction: post yes@0.80, later ask EXACTLY 0.80 ->
    # touch fills, cross does not
    rows_t = [
        {"t": "obs", "ticker": "W7-45", "ts": "t1", "mins_left": 5.0,
         "yes_bid": 0.80, "yes_ask": 0.84, "no_bid": 0.14, "no_ask": 0.18},
        {"t": "obs", "ticker": "W7-45", "ts": "t2", "mins_left": 4.0,
         "yes_bid": 0.78, "yes_ask": 0.80, "no_bid": 0.18, "no_ask": 0.21},
        {"t": "settle", "ticker": "W7-45", "result": "yes"},
    ]
    rt = build(rows_t)
    t7 = {(p2["ticker"], p2["side"]): p2 for p2 in rt["postings"]}[("W7-45", "yes")]
    assert t7["filled"] and not t7["filled_cross"], t7
    assert t7["pnl"] > 0 and t7["pnl_cross"] == 0.0, t7
    assert ("W4-45", "yes") not in ps
    # NO-side postings exist too (B's no side: bid 0.14; later no_ask 0.16 > 0.14 unfilled;
    # A's no side: later no_ask 0.23 > 0.14 unfilled; C's no side: later no_ask 0.38 unfilled)
    assert not ps[("W1-45", "no")]["filled"]

    # one posting per band per ticker, even with many obs in the band
    rows2 = [obs("W5-45", f"t{i}", 5.0 - i * 0.1, 0.70, 0.74, 0.24, 0.28) for i in range(5)]
    rows2.append({"t": "settle", "ticker": "W5-45", "result": "yes"})
    rep2 = build(rows2)
    yes_posts = [p for p in rep2["postings"] if p["side"] == "yes"]
    assert len(yes_posts) == 1, yes_posts

    # band transitions create separate postings
    rows3 = [obs("W6-45", "t1", 12.0, 0.70, 0.74, 0.24, 0.28),
             obs("W6-45", "t2", 5.0, 0.70, 0.74, 0.24, 0.28),
             obs("W6-45", "t3", 1.0, 0.70, 0.74, 0.24, 0.28),
             {"t": "settle", "ticker": "W6-45", "result": "yes"}]
    rep3 = build(rows3)
    bands = sorted(p["band"] for p in rep3["postings"] if p["side"] == "yes")
    assert bands == sorted([">10min", "2-10min", "<2min"]), bands
    # placeholder-book skip: the junk opening obs (bid 0.001/ask 0.999) must NOT be the
    # posting point; the first FORMED obs is.
    rows4 = [obs("W8-45", "t1", 30.0, 0.001, 0.999, 0.001, 0.999),
             obs("W8-45", "t2", 29.0, 0.70, 0.74, 0.24, 0.28),
             {"t": "settle", "ticker": "W8-45", "result": "yes"}]
    rep4 = build(rows4)
    y8 = [p2 for p2 in rep4["postings"] if p2["ticker"] == "W8-45" and p2["side"] == "yes"]
    assert len(y8) == 1 and y8[0]["price"] == 0.70, y8
    print("selftest OK")
    return 0


def main() -> int:
    if "selftest" in sys.argv[1:]:
        return _selftest()
    rep = build(load_rows(LOG))
    if "--json" in sys.argv:
        cells = {f"{b}|{k}": {kk: (len(vv) if isinstance(vv, set) else vv)
                              for kk, vv in c.items()}
                 for (b, k), c in cell_table(rep["postings"]).items()}
        print(json.dumps({"n_postings": len(rep["postings"]),
                          "n_settled": rep["n_settled"], "cells": cells}, indent=1))
    else:
        print_report(rep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
