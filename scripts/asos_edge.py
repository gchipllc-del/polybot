#!/usr/bin/env python3
"""asos_edge — the tradeability test the hit-rate work never answered: on the asos locks
that were actually PRICED, did the lock beat the market after fees?

A high lock hit-rate is necessary but NOT sufficient. Money requires the market to
MISPRICE a near-certain outcome (else you pay ~99c for a 99% YES and net nothing after
fees), and it requires a price at all (asos books are quote-on-demand — many locks have
market_yes=None = no fillable liquidity). This reads data/asos_lock.jsonl, keeps settled
locks WITH a real market_yes, and scores the realized P&L of taking the lock side after
fees, plus whether our certainty out-predicts the market (Brier). Read-only.

  python scripts/asos_edge.py
  python scripts/asos_edge.py --selftest
"""
import argparse
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "data" / "asos_lock.jsonl"


def kalshi_fee(price: float) -> float:
    """Kalshi taker fee per contract, $: ceil(0.07·P·(1−P)·100) cents."""
    p = max(0.0, min(1.0, price))
    return math.ceil(0.07 * p * (1 - p) * 100) / 100.0


def trade_pnl(side: str, market_yes: float, result: str) -> float:
    """Realized P&L per $1 contract of taking `side` at the market, fee on entry."""
    if side.lower() == "yes":
        cost, win = market_yes, (result == "yes")
    else:
        cost, win = 1.0 - market_yes, (result == "no")
    fee = kalshi_fee(cost)
    return (1.0 - cost - fee) if win else (-cost - fee)


def analyze(rows: list) -> dict:
    out = {"settled": len(rows), "priced": 0, "unpriced": 0, "won": 0,
           "pnl": 0.0, "brier_model": 0.0, "brier_mkt": 0.0, "trades": []}
    priced = []
    for r in rows:
        my = r.get("market_yes")
        if my is None:
            out["unpriced"] += 1
            continue
        priced.append(r)
    out["priced"] = len(priced)
    if not priced:
        return out
    for r in priced:
        my = float(r["market_yes"])
        side = str(r.get("side", ""))
        res = str(r.get("result", "")).lower()
        yes_out = 1 if res == "yes" else 0
        cert = float(r.get("cert") or 0.0)
        model_p_yes = cert if side.lower() == "yes" else (1.0 - cert)
        pnl = trade_pnl(side, my, res)
        out["won"] += 1 if (side.lower() == res) else 0
        out["pnl"] += pnl
        out["brier_model"] += (model_p_yes - yes_out) ** 2
        out["brier_mkt"] += (my - yes_out) ** 2
        out["trades"].append({"series": r.get("series"), "side": side, "cert": cert,
                              "market_yes": my, "result": res, "pnl": round(pnl, 3)})
    n = len(priced)
    out["wr"] = out["won"] / n
    out["pnl_per"] = out["pnl"] / n
    out["pnl"] = round(out["pnl"], 2)
    out["brier_model"] = round(out["brier_model"] / n, 3)
    out["brier_mkt"] = round(out["brier_mkt"] / n, 3)
    return out


def _load_settled(path: Path) -> list:
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
            if str(r.get("status")) in ("won", "lost"):
                rows.append(r)
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--path", default=str(LOG))
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        raise SystemExit(_selftest())

    rows = _load_settled(Path(args.path))
    a = analyze(rows)
    print("=== asos_edge — tradeability of priced locks ===")
    print(f"  {a['settled']} settled locks · {a['priced']} priced · {a['unpriced']} unpriced (no fillable book)")
    if a["priced"] == 0:
        print("\n  NO priced locks at all — every asos lock had market_yes=None.")
        print("  The books are quote-on-demand with no resting liquidity, so even a perfect")
        print("  hit-rate is unfillable. There is no edge to capture here. → park the sleeve.")
        return
    print(f"\n  priced-lock win-rate {a['wr']:.0%}   realized P&L per contract ${a['pnl_per']:+.3f}  "
          f"(total ${a['pnl']:+.2f} over {a['priced']})")
    print(f"  Brier (lower=better):  our cert {a['brier_model']:.3f}   market {a['brier_mkt']:.3f}  → "
          f"{'we' if a['brier_model'] < a['brier_mkt'] else 'market'} predict(s) better")
    edge = a["pnl_per"] > 0 and a["brier_model"] < a["brier_mkt"]
    print("\n  VERDICT: " + (
        "the lock BEATS the market after fees on priced locks — a real (if thin) edge; "
        "size by available liquidity."
        if edge else
        "no edge after fees — the market already prices the locks correctly. Hit-rate ≠ money; "
        "park the sleeve like fc2s's tail."))
    if a["priced"] <= 40:
        print(f"\n  (only {a['priced']} priced — thin; treat the verdict as provisional either way.)")


def _selftest() -> int:
    assert abs(trade_pnl("YES", 0.20, "yes") - (1 - 0.20 - kalshi_fee(0.20))) < 1e-9
    assert abs(trade_pnl("NO", 0.20, "yes") - (-(1 - 0.20) - kalshi_fee(0.80))) < 1e-9
    # all unpriced → priced 0, the "park" path
    a0 = analyze([{"status": "won", "side": "YES", "market_yes": None, "result": "yes", "cert": 0.99}])
    assert a0["priced"] == 0 and a0["unpriced"] == 1, a0
    # market efficient: YES locks priced AT our cert 0.97, win 97% → ~ -fee, no edge
    eff = []
    for i in range(100):
        res = "yes" if i < 97 else "no"
        eff.append({"status": "won" if res == "yes" else "lost", "side": "YES",
                    "market_yes": 0.97, "result": res, "cert": 0.97})
    ae = analyze(eff)
    assert ae["priced"] == 100 and ae["pnl_per"] < 0, ae      # paying 0.97 for a 0.97 event loses the fee
    # genuine edge: market badly underprices a near-certain YES (priced 0.60, wins 97%)
    edgey = []
    for i in range(100):
        res = "yes" if i < 97 else "no"
        edgey.append({"status": "won" if res == "yes" else "lost", "side": "YES",
                      "market_yes": 0.60, "result": res, "cert": 0.97})
    ag = analyze(edgey)
    assert ag["pnl_per"] > 0 and ag["brier_model"] < ag["brier_mkt"], ag
    print("trade_pnl + unpriced + efficient + edge cases OK")
    print("PASS")
    return 0


if __name__ == "__main__":
    main()
