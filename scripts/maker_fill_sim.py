"""Maker-first FILL SIMULATOR (Phase 1, paper, READ-ONLY).

Question: on the proven weather-NO sleeve, does RESTING a post_only NO limit
(maker — pay the bid, earn the rebate) beat TAKING (pay the ask) once you
account for fill risk in these thin books?

Method — replay each settled weather-NO trade against REAL Kalshi minute
candlesticks (lib.kalshi_historical_quotes.quote_series):
  * Taker baseline: buy NO at the entry no_ask (what we do today).
  * Maker:          rest a buy at the entry no_bid (+ optional improve cents).
                    FILL iff the NO ask later trades down to <= our rest price
                    (a seller meets our bid). Filled → pay bid + $0.005 rebate;
                    unfilled → we MISS the trade (the cost of being a maker).
Settlement (won/lost) is the real outcome; only the entry price + fill differ.

Per-contract economics. No trading, no orders — pure measurement.

Usage:  python scripts/maker_fill_sim.py --limit 40 [--improve 1]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from lib.kalshi_historical_quotes import quote_series  # noqa: E402

FEE = 0.07          # Kalshi taker profit fee
REBATE = 0.005      # $/contract maker rebate (Volume Incentive cap)


def _ts(iso):
    try:
        return int(datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def _load_no_trades(limit):
    p = ROOT / "data" / "weather_paper.jsonl"
    rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    no = [r for r in rows
          if str(r.get("side")) == "NO"
          and str(r.get("status")).lower() in ("won", "lost")
          and r.get("market_ticker") and r.get("opened_at") and r.get("close_time")]
    return no[-limit:] if limit else no


def _entry_quote(series, entry_ts):
    """Nearest two-sided quote at/after entry (within 5 min)."""
    cand = [q for q in series
            if q["ts"] >= entry_ts - 180
            and q.get("no_bid") is not None and q.get("no_ask") is not None]
    return cand[0] if cand else None


def run(limit=40, improve=0):
    trades = _load_no_trades(limit)
    res = []
    skipped = 0
    for r in trades:
        tk = r["market_ticker"]
        ots, cts = _ts(r["opened_at"]), _ts(r["close_time"])
        if not (ots and cts and cts > ots):
            skipped += 1; continue
        try:
            series = quote_series(tk, ots - 300, cts)
        except Exception:
            skipped += 1; continue
        eq = _entry_quote(series, ots) if series else None
        if not eq:
            skipped += 1; continue
        ask, bid = eq["no_ask"], eq["no_bid"]
        if ask is None or bid is None or ask <= 0 or bid <= 0:
            skipped += 1; continue
        rest = round(bid + improve / 100.0, 4)
        if rest >= ask:                       # must sit below the ask to be a maker
            rest = round(ask - 0.01, 4)
        # fill iff the NO ask trades down to <= our rest price after entry
        post = [q for q in series if q["ts"] > ots and q.get("no_ask") is not None]
        filled = any(q["no_ask"] <= rest for q in post)
        won = str(r.get("status")).lower() == "won"
        taker = ((1 - ask) * (1 - FEE)) if won else (-ask)
        if filled:
            maker = (((1 - rest) * (1 - FEE)) if won else (-rest)) + REBATE
        else:
            maker = 0.0
        res.append({"won": won, "spread": ask - bid, "filled": filled,
                    "taker": taker, "maker": maker})

    n = len(res)
    print(f"=== MAKER FILL SIM — {n} weather-NO trades simulated "
          f"(skipped {skipped}), rest at bid{f'+{improve}c' if improve else ''} ===")
    if not n:
        print("  (no trades with usable candle quotes)"); return
    f = sum(1 for x in res if x["filled"])
    spreads = sorted(x["spread"] for x in res)
    taker_tot = sum(x["taker"] for x in res)
    maker_tot = sum(x["maker"] for x in res)
    miss = [x for x in res if not x["filled"]]
    mw = sum(1 for x in miss if x["won"])
    print(f"  fill rate     : {f}/{n} = {100*f/n:.0f}%")
    print(f"  median spread : {spreads[n//2]*100:.1f}c")
    print(f"  TAKER (pay ask, all {n})           : ${taker_tot:+.3f} total  "
          f"(${taker_tot/n:+.4f}/contract)")
    print(f"  MAKER (pay bid+{REBATE*100:.1f}c rebate, {f} filled): ${maker_tot:+.3f} total  "
          f"(${maker_tot/n:+.4f}/attempt)")
    print(f"  unfilled      : {len(miss)} (missed {mw} wins / {len(miss)-mw} losses)")
    verdict = "BEATS" if maker_tot > taker_tot else "LOSES TO"
    print(f"  >>> maker {verdict} taker by ${maker_tot - taker_tot:+.3f} over {n} attempts "
          f"(${(maker_tot-taker_tot)/n:+.4f}/contract)")
    print("\n  GATE: maker wins only if fill rate is high enough that the spread+rebate "
          "saved beats the wins missed by not taking. Thin books → low fill → maker loses.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--improve", type=int, default=0, help="rest this many cents above bid")
    a = ap.parse_args()
    run(a.limit, a.improve)
