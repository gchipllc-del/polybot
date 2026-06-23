#!/usr/bin/env python3
"""fc2s_tail_edge — does the recalibrated model beat the MARKET on the above-strike tail?

fc2s_shadow answers "is recal calibrated vs realized." That's necessary but NOT
sufficient to lift the TRADE_ABOVE_STRIKES veto: a forecast can be perfectly calibrated
and still have zero edge if the market already prices it correctly. The live blowup
(73% claimed / 18% realized) happened where the bot most *disagreed with the market* —
classic adverse selection — so the decisive test is recal-prob vs market price, not
recal-prob vs realized.

This reads fc2s_paper.jsonl, takes the SETTLED above-strike trades, and for each compares
the recal P(realized > strike) to the market-implied prob, then scores (a) which is the
better predictor of the outcome (Brier) and (b) the realized P&L of betting the side recal
favours vs the market, after fees. Read-only.

  python scripts/fc2s_tail_edge.py                       # bias/sigma default to the shadow's measured values
  python scripts/fc2s_tail_edge.py --bias 0.14 --sigma 2.50 --fee 0.01
  python scripts/fc2s_tail_edge.py --selftest
"""
import argparse
import json
from pathlib import Path
from statistics import NormalDist

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "data" / "fc2s_paper.jsonl"
_N = NormalDist()


def recal_p_yes(strike: float, forecast: float, bias: float, sigma: float) -> float:
    """P(realized > strike) under realized ~ N(forecast − bias, sigma). Matches
    fc2s_shadow.exceedance_table: 1 − Φ((offset + bias)/sigma), offset = strike − forecast."""
    if sigma <= 0:
        return float("nan")
    o = strike - forecast
    return 1.0 - _N.cdf((o + bias) / sigma)


def _pnl(side: str, mkt_yes: float, realized_yes: int, fee: float) -> float:
    """Net P&L per $1 contract of taking `side` at the market price, paid `fee` on entry."""
    if side == "YES":
        cost = mkt_yes
        win = realized_yes == 1
    else:                                   # NO
        cost = 1.0 - mkt_yes
        win = realized_yes == 0
    return (1.0 - cost - fee) if win else (-cost - fee)


def analyze(rows: list, bias: float, sigma: float, fee: float) -> dict:
    out = {"n": len(rows), "recal_mean": 0.0, "mkt_mean": 0.0, "realized_rate": 0.0,
           "brier_recal": 0.0, "brier_mkt": 0.0, "recal_pnl": 0.0, "follow_market_pnl": 0.0,
           "trades": []}
    if not rows:
        return out
    for r in rows:
        f = float(r["forecast_high_f"]); k = float(r["strike_f"])
        mkt = float(r["yes_price"]); ry = 1 if str(r.get("result")).lower() == "yes" else 0
        rp = recal_p_yes(k, f, bias, sigma)
        side = "YES" if rp > mkt else "NO"          # bet where recal disagrees with market
        out["trades"].append({
            "ticker": r.get("ticker"), "offset": round(k - f, 1), "forecast": f, "strike": k,
            "recal_p": round(rp, 3), "mkt_yes": round(mkt, 3), "realized_yes": ry,
            "recal_side": side, "recal_pnl": round(_pnl(side, mkt, ry, fee), 3),
        })
        out["recal_mean"] += rp
        out["mkt_mean"] += mkt
        out["realized_rate"] += ry
        out["brier_recal"] += (rp - ry) ** 2
        out["brier_mkt"] += (mkt - ry) ** 2
        out["recal_pnl"] += _pnl(side, mkt, ry, fee)
        # baseline: follow the market's own lean (no edge → ~ -fee on average)
        out["follow_market_pnl"] += _pnl("YES" if mkt >= 0.5 else "NO", mkt, ry, fee)
    n = out["n"]
    for key in ("recal_mean", "mkt_mean", "realized_rate", "brier_recal", "brier_mkt"):
        out[key] = round(out[key] / n, 3)
    out["recal_pnl"] = round(out["recal_pnl"], 2)
    out["follow_market_pnl"] = round(out["follow_market_pnl"], 2)
    return out


def _load(path: Path, include_live: bool) -> list:
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("strike_kind") != "above":
            continue
        if str(r.get("status")) not in ("won", "lost"):
            continue
        if r.get("forecast_high_f") is None or r.get("strike_f") is None or r.get("yes_price") is None:
            continue
        if not include_live and r.get("is_live"):
            continue
        rows.append(r)
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bias", type=float, default=0.14, help="forecast−realized bias (shadow: 0.14)")
    ap.add_argument("--sigma", type=float, default=2.50, help="measured sigma (shadow: 2.50)")
    ap.add_argument("--fee", type=float, default=0.01, help="per-contract entry fee, $ (default 0.01)")
    ap.add_argument("--path", default=str(LEDGER))
    ap.add_argument("--include-live", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        raise SystemExit(_selftest())

    rows = _load(Path(args.path), args.include_live)
    print("=== fc2s_tail_edge — recal vs MARKET on settled above-strike trades ===")
    print(f"params: bias {args.bias:+.2f}  sigma {args.sigma:.2f}  fee ${args.fee:.2f}")
    if not rows:
        print(f"\n  No settled above-strike trades in {args.path}.")
        print("  Expected if the veto (TRADE_ABOVE_STRIKES=False) was set BEFORE any above-strike")
        print("  trade booked here — the real losing set lives in the live weather ledger, not")
        print("  this paper file. Point --path at that ledger, or re-enable briefly to gather a")
        print("  paper above-strike sample, before this test can rule on the veto.")
        return
    a = analyze(rows, args.bias, args.sigma, args.fee)
    print(f"\n  n={a['n']} settled above-strike trades")
    print(f"  realized P(above) {a['realized_rate']:.2f}   "
          f"recal mean {a['recal_mean']:.2f}   market mean {a['mkt_mean']:.2f}")
    print(f"  Brier (lower=better predictor):  recal {a['brier_recal']:.3f}   "
          f"market {a['brier_mkt']:.3f}  → {'recal' if a['brier_recal'] < a['brier_mkt'] else 'market'} predicts better")
    print(f"  realized P&L per contract: follow-recal ${a['recal_pnl']:+.2f}   "
          f"follow-market ${a['follow_market_pnl']:+.2f}")
    edge = a["recal_pnl"] > 0 and a["brier_recal"] < a["brier_mkt"]
    print("\n  VERDICT: " + (
        "recal BEATS the market (positive P&L + better Brier) → a lift is justified; "
        "validate on more tail samples before staking."
        if edge else
        "no edge vs market → keep TRADE_ABOVE_STRIKES vetoed. The miscalibration was real "
        "but the market already prices it; the live losses were adverse selection, not a fixable σ."))
    worst = sorted(a["trades"], key=lambda t: t["recal_pnl"])[:8]
    print("\n  worst recal-follow trades (eyeball adverse selection):")
    for t in worst:
        print(f"   off {t['offset']:+4.1f}  recal {t['recal_p']:.2f} vs mkt {t['mkt_yes']:.2f}  "
              f"{t['recal_side']}  realized={'YES' if t['realized_yes'] else 'NO '}  "
              f"pnl ${t['recal_pnl']:+.2f}  {t['ticker']}")


def _selftest() -> int:
    # recal_p_yes matches the shadow's formula sign: strike 4 above forecast, small bias.
    p = recal_p_yes(strike=88, forecast=84, bias=0.14, sigma=2.5)
    assert abs(p - (1 - _N.cdf((4 + 0.14) / 2.5))) < 1e-12 and 0.0 < p < 0.10, p
    # _pnl bookkeeping
    assert abs(_pnl("YES", 0.20, 1, 0.01) - (1 - 0.20 - 0.01)) < 1e-9     # YES wins
    assert abs(_pnl("YES", 0.20, 0, 0.01) - (-0.20 - 0.01)) < 1e-9         # YES loses
    assert abs(_pnl("NO", 0.20, 0, 0.01) - (1 - 0.80 - 0.01)) < 1e-9       # NO wins
    # Market efficient: recal == market, realized matches both → no edge, equal Brier.
    eff = [{"forecast_high_f": 84, "strike_f": 88, "yes_price": round(recal_p_yes(88, 84, 0.14, 2.5), 4),
            "result": "no", "status": "lost", "strike_kind": "above"} for _ in range(6)]
    a = analyze(eff, 0.14, 2.5, 0.0)
    assert abs(a["brier_recal"] - a["brier_mkt"]) < 1e-6, a                # identical predictors
    # Recal edge: market badly overprices YES (0.40) but realized almost always NO and
    # recal says ~0.03 → recal should win Brier and follow-recal (NO) should profit.
    edgey = [{"forecast_high_f": 84, "strike_f": 88, "yes_price": 0.40,
              "result": "no", "status": "lost", "strike_kind": "above"} for _ in range(10)]
    b = analyze(edgey, 0.14, 2.5, 0.01)
    assert b["brier_recal"] < b["brier_mkt"] and b["recal_pnl"] > 0, b
    print("recal_p_yes + _pnl OK")
    print("edge/efficient cases OK")
    print("PASS")
    return 0


if __name__ == "__main__":
    main()
