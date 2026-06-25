#!/usr/bin/env python3
"""weather_maker_scorecard — would RESTING (maker) beat TAKING on weather YES/NO?

The deep-research survey (docs/EDGE_RESEARCH.md) found the one documented retail Kalshi
edge is the maker/taker structure: takers lose ~32%, makers ~10%, because takers cross the
spread. fc2s_cost_attrib already showed our weather book bleeds on entry cost. This asks the
follow-on: on the REAL settled weather trades, does paying the BID (maker, + rebate) instead
of the ASK (taker) flip a side into the black — and what FILL RATE does it take to get there?

Why this is self-contained (no candle API needed):
  Per contract, pnl = payout - cost. Resting `spread` cents below the ask lowers cost by
  `spread`, so it raises pnl by exactly `spread` regardless of win/loss, plus the maker
  REBATE -- BUT only on the fraction of trades that FILL. Unfilled => you miss the trade (0).
  So with the ledger's REAL per-contract pnl as the taker baseline, the only modeled inputs
  are the spread captured and the fill rate. We solve for the break-even fill rate per side.

  taker_pc          = paper_pnl / our_size                      (REAL, measured)
  maker_pc(filled)  = taker_pc + spread + REBATE                (entry `spread` cheaper)
  E[maker/attempt]  = fill_rate * maker_pc(filled)              (unfilled attempts earn 0)
  break-even fill*  = sum(taker_pc) / sum(maker_pc_filled)

HONEST CAVEAT: the break-even fill rate is a NECESSARY, not sufficient, bar. It assumes fills
are independent of outcome. In reality a resting maker is adversely selected -- you get filled
when the market moves against you (the eventual LOSERS fill more than the winners), so realized
maker pnl is worse than this optimistic bound. Measuring the true fill/adverse-selection needs
the Kalshi candlestick API (a separate live tool). This scorecard bounds the BEST case.

Read-only. No orders.

  python scripts/weather_maker_scorecard.py
  python scripts/weather_maker_scorecard.py --spreads 1,2,3,5 --rebate 0.5
  python scripts/weather_maker_scorecard.py --selftest
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "data" / "weather_paper.jsonl"


def _load(path: Path) -> list:
    rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    return [r for r in rows
            if str(r.get("status", "")).lower() in ("won", "lost")
            and r.get("our_size") and r.get("side")]


def _taker_pc(r: dict) -> float:
    """REAL per-contract net pnl from the ledger (fees already baked into paper_pnl)."""
    size = float(r.get("our_size") or 0)
    return float(r.get("paper_pnl") or 0) / size if size else 0.0


def side_scorecard(rows: list, spreads_c: list, rebate_c: float) -> dict:
    """Measured taker baseline + break-even fill rate per modeled spread, for one side."""
    n = len(rows)
    if n == 0:
        return {"n": 0}
    won = sum(1 for r in rows if str(r.get("status")).lower() == "won")
    wr = won / n
    avg_cost = sum(float(r.get("fill_price") or 0) for r in rows) / n   # breakeven-WR proxy
    taker_pcs = [_taker_pc(r) for r in rows]
    taker_tot = sum(taker_pcs)                                          # per-contract total
    rebate = rebate_c / 100.0

    breakevens = {}
    for sc in spreads_c:
        spread = sc / 100.0
        maker_filled = [t + spread + rebate for t in taker_pcs]
        denom = sum(maker_filled)
        # maker (at 100% fill) per-contract total, and the fill rate that ties taker.
        be = (taker_tot / denom) if denom > 0 else None
        breakevens[sc] = {
            "maker_full_fill_pc": denom / n,          # avg per-contract if every rest filled
            "uplift_pc": spread + rebate,             # per-contract gain on a fill
            "breakeven_fill": be,                     # fill rate where maker == taker
        }
    return {
        "n": n, "wr": wr, "breakeven_wr": avg_cost, "edge_wr": wr - avg_cost,
        "taker_total_pc": taker_tot, "taker_avg_pc": taker_tot / n,
        "breakevens": breakevens,
    }


def _fmt_side(name: str, s: dict, spreads_c: list) -> str:
    if s.get("n", 0) == 0:
        return f"\n=== WEATHER {name} === (no settled trades)"
    out = [f"\n=== WEATHER {name}  ({s['n']} settled trades) ==="]
    out.append(f"  win rate          : {s['wr']*100:5.1f}%")
    out.append(f"  breakeven WR (cost): {s['breakeven_wr']*100:5.1f}%   "
               f"(avg entry cost incl. fee proxy)")
    out.append(f"  edge vs breakeven : {s['edge_wr']*100:+5.1f}pp   "
               f"{'(+EV by odds)' if s['edge_wr'] > 0 else '(LOSES by odds)'}")
    out.append(f"  TAKER net (real)  : ${s['taker_total_pc']:+.3f}/ct total  "
               f"(${s['taker_avg_pc']:+.4f}/contract)  <- measured baseline")
    out.append("  MAKER counterfactual -- break-even fill rate to beat taker:")
    for sc in spreads_c:
        b = s["breakevens"][sc]
        be = b["breakeven_fill"]
        if be is None:
            verdict = "maker loses at ANY fill (uplift can't cover the misses)"
        elif be <= 0:
            verdict = "maker wins even at low fill (taker already < 0)"
        elif be > 1:
            verdict = f"UNREACHABLE -- needs {be*100:.0f}% fill (>100%); maker can't win"
        else:
            verdict = f"need >= {be*100:4.0f}% fill rate"
        out.append(f"    spread {sc}c (+{b['uplift_pc']*100:.1f}c/fill): {verdict}")
    return "\n".join(out)


def run(spreads_c: list, rebate_c: float):
    if not LEDGER.exists():
        print(f"ERROR: {LEDGER} not found"); return 1
    rows = _load(LEDGER)
    no = [r for r in rows if str(r.get("side")).upper() == "NO"]
    yes = [r for r in rows if str(r.get("side")).upper() == "YES"]
    print(f"weather maker scorecard -- {len(rows)} settled trades "
          f"({len(no)} NO, {len(yes)} YES), rebate {rebate_c}c/fill")
    print(_fmt_side("NO", side_scorecard(no, spreads_c, rebate_c), spreads_c))
    print(_fmt_side("YES", side_scorecard(yes, spreads_c, rebate_c), spreads_c))
    print("\nNOTE: break-even fill rate is the BEST case -- it assumes fills are independent")
    print("of outcome. A resting maker is adversely selected (losers fill more than winners),")
    print("so true maker pnl is WORSE. Treat the threshold as necessary, not sufficient.")
    return 0


def _selftest() -> int:
    # 2 trades, 1c spread, 0.5c rebate. taker_pc: win +0.50 (size2,pnl1.0), loss -0.20 (size2,pnl-0.4)
    rows = [
        {"side": "NO", "status": "won", "our_size": 2, "paper_pnl": 1.0, "fill_price": 0.50},
        {"side": "NO", "status": "lost", "our_size": 2, "paper_pnl": -0.40, "fill_price": 0.20},
    ]
    s = side_scorecard(rows, [1], 0.5)
    assert s["n"] == 2 and abs(s["wr"] - 0.5) < 1e-9, s
    assert abs(s["taker_total_pc"] - 0.30) < 1e-9, s["taker_total_pc"]   # 0.50 + (-0.20)
    b = s["breakevens"][1]
    # uplift = 0.01 + 0.005 = 0.015 per ct; maker_filled = 0.515 + -0.185 = 0.33; be = 0.30/0.33
    assert abs(b["uplift_pc"] - 0.015) < 1e-9, b
    assert abs(b["breakeven_fill"] - (0.30 / 0.33)) < 1e-9, b
    print("selftest OK")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--spreads", default="1,2,3,5", help="comma cents of spread captured")
    ap.add_argument("--rebate", type=float, default=0.5, help="maker rebate, cents/contract")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(_selftest())
    spreads = [int(x) for x in a.spreads.split(",") if x.strip()]
    sys.exit(run(spreads, a.rebate))
