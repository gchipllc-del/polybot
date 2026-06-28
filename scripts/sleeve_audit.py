#!/usr/bin/env python3
"""sleeve_audit — is a paper sleeve's edge REAL, and where is the bleed fixable?

Codifies the math we use to separate a real edge from a mirage, on any *_paper.jsonl:

  • net / WR / ROI                         — the headline
  • entry-price buckets                    — favorite-longshot structure (is profit from
                                             cheap-side asymmetry?)
  • concentration                          — do a few trades make the P&L (fragile)?
  • out-of-sample halves                   — does the edge persist or decay?
  • per-day PSR / DSR (Bailey/LdP)         — is it statistically distinguishable from luck?
  • forecast realism (optional)            — |forecast − actual| MAE: ~0 ⇒ LOOK-AHEAD leak,
                                             realistic ⇒ legit nowcast, large ⇒ no skill
  • signal cohort split (optional)         — bucket by a signal field; if the HIGH-signal
                                             cohort does WORSE, the signal is anti-predictive
  • non-positive-edge entries (optional)   — a real arb never enters edge ≤ 0; counts how
                                             many trades had no edge at entry (broken gate)

What it CANNOT see: whether the recorded fills were actually available in size (needs the
live order book). That fill-realism check is the one thing this tool flags but can't settle.

  python scripts/sleeve_audit.py data/weather_paper.jsonl --preset weather_no
  python scripts/sleeve_audit.py data/kalshi_15min_paper.jsonl --signal composite
  python scripts/sleeve_audit.py data/btc_arb_paper.jsonl --edge gap
  python scripts/sleeve_audit.py --selftest
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PRESETS = {
    # field names per known sleeve, so the right realism/signal checks fire
    "weather_no": {"side": "NO", "forecast": "nws_forecast_f", "actual": "actual_temp_f",
                   "strike": "strike_f"},
    "kalshi_15min": {"signal": "composite"},
    "btc_arb": {"edge": "gap"},
}


def _f(r, k):
    try:
        return float(r.get(k))
    except (TypeError, ValueError):
        return None


def _won(r):
    return str(r.get("status", "")).lower() == "won"


def _settled(rows, side=None):
    out = [r for r in rows if str(r.get("status", "")).lower() in ("won", "lost")]
    if side:
        out = [r for r in out if str(r.get("side", "")).upper() == side.upper()]
    return out


def _line(name, sub):
    if not sub:
        return f"  {name:34}: (empty)"
    net = sum(_f(r, "paper_pnl") or 0 for r in sub)
    w = sum(_won(r) for r in sub)
    return f"  {name:34}: n={len(sub):4} WR={100*w/len(sub):3.0f}% net=${net:+9.2f}  avg=${net/len(sub):+.2f}"


def audit(rows, *, side=None, forecast=None, actual=None, strike=None,
          signal=None, edge=None, trials=8) -> list:
    """Return a list of printable report lines. Pure (PSR import is optional)."""
    s = _settled(rows, side)
    out = [f"=== {len(s)} settled trades" + (f" (side={side})" if side else "") + " ==="]
    if not s:
        return out + ["  no settled trades."]
    net = sum(_f(r, "paper_pnl") or 0 for r in s)
    inv = sum(_f(r, "notional") or 0 for r in s)
    w = sum(_won(r) for r in s)
    out.append(f"  net ${net:+.2f}  WR {100*w/len(s):.1f}%"
               + (f"  ROI {100*net/inv:+.1f}%" if inv else ""))

    # 1) entry-price buckets
    out.append("\n  entry-price buckets (favorite-longshot structure):")
    buck = defaultdict(list)
    for r in s:
        p = _f(r, "fill_price")
        if p is None:
            continue
        b = ("<0.15", "0.15-0.30", "0.30-0.45", "0.45+")[(p >= .15)+(p >= .30)+(p >= .45)]
        buck[b].append(r)
    for b in ("<0.15", "0.15-0.30", "0.30-0.45", "0.45+"):
        if buck[b]:
            out.append(_line(f"  cost {b}", buck[b]))

    # 2) concentration
    pnls = sorted((_f(r, "paper_pnl") or 0 for r in s), reverse=True)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    if net:
        out.append(f"\n  concentration: top5 winners = {100*sum(pnls[:5])/net:.0f}% of net, "
                   f"top10 = {100*sum(pnls[:10])/net:.0f}%")
    out.append(f"  avg win ${statistics.mean(wins):+.2f}  avg loss "
               f"${statistics.mean(losses):+.2f}" if wins and losses else "  (one-sided P&L)")

    # 3) out-of-sample halves
    sc = sorted(s, key=lambda r: str(r.get("opened_at", "")))
    h = len(sc) // 2
    out.append("\n  out-of-sample (chronological halves):")
    out.append(_line("  1st half", sc[:h]))
    out.append(_line("  2nd half", sc[h:]))

    # 4) per-day PSR / DSR
    try:
        from lib.hermes_significance import (probabilistic_sharpe_ratio,
                                             deflated_sharpe_ratio)
        byday = defaultdict(list)
        for r in s:
            d = str(r.get("opened_at", ""))[:10]
            notion, pnl = _f(r, "notional"), _f(r, "paper_pnl")
            if notion and notion > 0 and pnl is not None:
                byday[d].append(pnl / notion)
        daily = [statistics.mean(v) for _, v in sorted(byday.items())]
        if len(daily) >= 5:
            psr = probabilistic_sharpe_ratio(daily)
            dsr = deflated_sharpe_ratio(daily, trials)
            out.append(f"\n  significance: {len(daily)} days  PSR(>0)={psr:.3f}  "
                       f"DSR(@{trials})={dsr:.3f}  "
                       f"{'→ distinguishable from luck' if psr and psr > .95 else '→ NOT significant'}")
        else:
            out.append(f"\n  significance: only {len(daily)} days (need ≥5) — withheld")
    except Exception as e:  # noqa: BLE001
        out.append(f"\n  significance: (unavailable: {e})")

    # 5) forecast realism (look-ahead)
    if forecast and actual:
        errs = [abs(_f(r, forecast) - _f(r, actual))
                for r in s if _f(r, forecast) is not None and _f(r, actual) is not None]
        if errs:
            mae = statistics.mean(errs)
            tag = ("LOOK-AHEAD LEAK (forecast≈answer)" if mae < 0.15
                   else "legit nowcast" if mae < 3.0 else "weak/no skill")
            out.append(f"\n  forecast realism: |{forecast}−{actual}| MAE={mae:.2f} "
                       f"median={statistics.median(errs):.2f} → {tag}")
            if strike:
                # only-agree cohort: forecast actually predicts the side we bought
                if side and side.upper() == "NO":
                    agree = [r for r in s if _f(r, forecast) is not None
                             and _f(r, forecast) < _f(r, strike)]
                    out.append("  tune — keep only trades the forecast AGREES with "
                               "(forecast<strike for a NO):")
                    out.append(_line("  forecast-agree NO", agree))

    # 6) signal cohort split (anti-predictive detector)
    if signal:
        vals = [_f(r, signal) for r in s if _f(r, signal) is not None]
        if vals:
            med = statistics.median(vals)
            hi = [r for r in s if _f(r, signal) is not None and _f(r, signal) >= med]
            lo = [r for r in s if _f(r, signal) is not None and _f(r, signal) < med]
            out.append(f"\n  signal '{signal}' cohort (median {med:.2f}):")
            out.append(_line(f"  {signal}>=median", hi))
            out.append(_line(f"  {signal}<median", lo))
            nh = sum(_f(r, "paper_pnl") or 0 for r in hi)
            nl = sum(_f(r, "paper_pnl") or 0 for r in lo)
            if nh < 0 < nl or (nl > nh and nh <= 0):
                out.append(f"  ⚠ '{signal}' is ANTI-PREDICTIVE — the high-signal cohort does "
                           f"WORSE. The signal doesn't rank; no edge to harvest.")

    # 7) non-positive-edge entries (broken arb gate)
    if edge:
        ev = [_f(r, edge) for r in s if _f(r, edge) is not None]
        if ev:
            nonpos = sum(1 for v in ev if v <= 0)
            out.append(f"\n  edge field '{edge}': range {min(ev):+.3f}..{max(ev):+.3f}; "
                       f"{nonpos}/{len(ev)} entries had edge ≤ 0")
            if nonpos:
                out.append(f"  ⚠ {nonpos} trades entered with NON-POSITIVE edge — a real arb "
                           f"never does. The entry gate ranks on |magnitude|, not signed edge.")
    return out


def _load(path: Path):
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def _selftest() -> int:
    rows = [
        {"status": "won", "side": "NO", "fill_price": 0.10, "notional": 5, "paper_pnl": 45,
         "opened_at": "2026-06-01", "nws_forecast_f": 50.0, "actual_temp_f": 50.4,
         "strike_f": 55.0, "composite": 2.0, "gap": -0.2},
        {"status": "lost", "side": "NO", "fill_price": 0.40, "notional": 5, "paper_pnl": -5,
         "opened_at": "2026-06-02", "nws_forecast_f": 56.0, "actual_temp_f": 56.1,
         "strike_f": 55.0, "composite": 9.0, "gap": 0.3},
    ]
    lines = audit(rows, side="NO", forecast="nws_forecast_f", actual="actual_temp_f",
                  strike="strike_f", signal="composite", edge="gap")
    txt = "\n".join(lines)
    assert "2 settled" in txt and "net $+40.00" in txt, txt
    assert "legit nowcast" in txt, txt                 # MAE ~0.25
    assert "ANTI-PREDICTIVE" in txt, txt               # composite 9 lost, 2 won
    assert "NON-POSITIVE edge" in txt, txt             # gap -0.2 entered
    assert "forecast-agree NO" in txt, txt
    print("selftest OK")
    print(txt)
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ledger", nargs="?", help="path to a *_paper.jsonl")
    ap.add_argument("--preset", choices=list(PRESETS))
    ap.add_argument("--side")
    ap.add_argument("--forecast"); ap.add_argument("--actual"); ap.add_argument("--strike")
    ap.add_argument("--signal"); ap.add_argument("--edge")
    ap.add_argument("--trials", type=int, default=8)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        raise SystemExit(_selftest())
    if not a.ledger:
        ap.error("need a ledger path (or --selftest)")
    cfg = dict(PRESETS.get(a.preset, {}))
    for k in ("side", "forecast", "actual", "strike", "signal", "edge"):
        if getattr(a, k):
            cfg[k] = getattr(a, k)
    p = Path(a.ledger)
    if not p.exists():
        print(f"no ledger at {p} (it may live on the trading host, not here)")
        raise SystemExit(1)
    print(f"# audit {p.name}\n")
    print("\n".join(audit(_load(p), trials=a.trials, **cfg)))


if __name__ == "__main__":
    main()
