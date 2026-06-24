#!/usr/bin/env python3
"""fc2s_cost_attrib — why is a >50%-WR fc2s cohort still losing money?

A binary contract bought at cost c (per $1 payout) needs win-rate ≥ c+fee just to break
even. So a 67%-WR book that bets favorites (high entry cost) loses by the ODDS, not by
direction — and no amount of forecast tuning fixes that; only refusing the cheap-payout
side does. This breaks fc2s_paper.jsonl down by side×kind and reports, per cohort, the
breakeven WR (avg entry cost+fee) vs the actual WR, so you can see whether the bleed is
odds (favorite-betting), fees, or variance — plus the entry-price cap that would fix it.
Read-only.

  python scripts/fc2s_cost_attrib.py
  python scripts/fc2s_cost_attrib.py --selftest
"""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "data" / "fc2s_paper.jsonl"


def _cost_per_ct(r: dict) -> float:
    """Entry cost per contract incl. fee: fill_price + fee/size."""
    fill = float(r.get("fill_price") or 0)
    size = float(r.get("our_size") or 0)
    fee = float(r.get("fee") or 0)
    return fill + (fee / size if size else 0.0)


def cohort_stats(rows: list) -> dict:
    n = len(rows)
    if n == 0:
        return {"n": 0}
    won = sum(1 for r in rows if r.get("status") == "won")
    wr = won / n
    avg_cost = sum(_cost_per_ct(r) for r in rows) / n          # = breakeven WR
    net = sum(float(r.get("paper_pnl") or 0) for r in rows)
    wins = [float(r.get("paper_pnl") or 0) for r in rows if r.get("status") == "won"]
    losses = [float(r.get("paper_pnl") or 0) for r in rows if r.get("status") == "lost"]
    return {
        "n": n, "wr": wr, "breakeven_wr": avg_cost, "edge_wr": wr - avg_cost,
        "net": round(net, 2),
        "avg_win": round(sum(wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
    }


def price_cap_scan(rows: list) -> list:
    """For each candidate entry-price cap, the WR / breakeven / net of trades AT OR BELOW
    it — to find a cap where actual WR clears breakeven (i.e. the bleed is favorite-betting)."""
    out = []
    for cap in (0.50, 0.60, 0.70, 0.80, 0.90):
        kept = [r for r in rows if _cost_per_ct(r) <= cap]
        s = cohort_stats(kept)
        if s["n"]:
            out.append((cap, s["n"], s["wr"], s["breakeven_wr"], s["net"]))
    return out


def _load(path: Path) -> list:
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and '"status"' in line:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(r.get("status")) in ("won", "lost") and not r.get("is_live"):
                rows.append(r)
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--path", default=str(LEDGER))
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        raise SystemExit(_selftest())

    rows = _load(Path(args.path))
    print("=== fc2s_cost_attrib — is the bleed odds, fees, or direction? ===")
    if not rows:
        print(f"  no settled paper trades in {args.path}")
        return
    print(f"  {len(rows)} settled paper trades")
    print("\n  cohort        n    WR   breakeven  edge(WR−be)   net$   avgWin  avgLoss")
    cohorts = {}
    for side in ("YES", "NO"):
        for kind in ("above", "band"):
            cohorts[f"{side}/{kind}"] = [r for r in rows
                                         if r.get("side") == side and r.get("strike_kind") == kind]
    cohorts["ALL"] = rows
    for name, rs in cohorts.items():
        s = cohort_stats(rs)
        if not s["n"]:
            continue
        flag = ""
        if s["wr"] > 0.5 and s["edge_wr"] < 0:
            flag = "  ← wins >50% but LOSES on odds (favorite-betting)"
        elif s["edge_wr"] > 0 and s["net"] < 0:
            flag = "  ← WR clears breakeven but net<0 (fees/variance)"
        print(f"  {name:<11} {s['n']:>3}  {s['wr']:4.0%}    {s['breakeven_wr']:4.0%}      "
              f"{s['edge_wr']:+5.0%}     {s['net']:+6.2f}   {s['avg_win']:+5.2f}  {s['avg_loss']:+6.2f}{flag}")

    print("\n  price-cap scan (trades with entry cost ≤ cap):")
    print("   cap    n    WR   breakeven   net$")
    for cap, n, wr, be, net in price_cap_scan(rows):
        mark = "  ✓ clears breakeven" if wr > be else ""
        print(f"   {cap:.2f}  {n:>3}  {wr:4.0%}    {be:4.0%}    {net:+6.2f}{mark}")
    print("\n  Read: if a cohort wins >50% but edge(WR−breakeven)<0, it's betting favorites —")
    print("  the fix is an entry-price cap (skip the cheap-payout side), not forecast tuning.")


def _selftest() -> int:
    # Favorite-betting cohort: enter NO at cost 0.75 (breakeven 75%), win only 67% → loses.
    rows = []
    for i in range(100):
        won = i < 67
        rows.append({"side": "NO", "strike_kind": "above", "status": "won" if won else "lost",
                     "fill_price": 0.75, "our_size": 10, "fee": 0.0,
                     "paper_pnl": 0.25 if won else -0.75})
    s = cohort_stats(rows)
    assert s["n"] == 100 and abs(s["breakeven_wr"] - 0.75) < 1e-9, s
    assert abs(s["wr"] - 0.67) < 1e-9 and s["edge_wr"] < 0 and s["net"] < 0, s   # loses on odds
    # cheaper entries (cost 0.40, win 67%) clear breakeven and profit
    cheap = []
    for i in range(100):
        won = i < 67
        cheap.append({"side": "NO", "strike_kind": "above", "status": "won" if won else "lost",
                      "fill_price": 0.40, "our_size": 10, "fee": 0.0,
                      "paper_pnl": 0.60 if won else -0.40})
    cs = cohort_stats(cheap)
    assert cs["edge_wr"] > 0 and cs["net"] > 0, cs
    # price-cap scan keeps only the cheap ones
    scan = price_cap_scan(rows + cheap)
    cap50 = next(x for x in scan if x[0] == 0.50)
    assert cap50[1] == 100 and cap50[2] > cap50[3], cap50   # ≤0.50 keeps the 100 cheap, WR>breakeven
    print("cohort_stats + price_cap_scan OK (favorite-betting detected, cap fixes it)")
    print("PASS")
    return 0


if __name__ == "__main__":
    main()
