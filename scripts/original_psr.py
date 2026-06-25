#!/usr/bin/env python3
"""original_psr — hold the ORIGINAL system's realized trades to the per-DAY PSR bar.

Every weather/forecast sleeve got a per-day PSR this session; the original crypto/kalshi
system whose trades show on the :5050 dashboard never did. trade_history.json is REALIZED
net_profit (settled outcomes), so it's immune to the *_dollars price-field bug — a clean
test of whether the "old way that was winning" had edge or was longshot variance, judged
the same way we judged everything else (per-day PSR: <0.50 not even probably positive,
0.50-0.95 provisional, >=0.95 evidence-backed). Read-only.

  python scripts/original_psr.py
  python scripts/original_psr.py --by strategy     # split per strategy/source if present
  python scripts/original_psr.py --selftest
"""
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
HIST = ROOT / "data" / "trade_history.json"


def _date(t: dict) -> str:
    return str(t.get("closed_at") or t.get("resolved_at") or t.get("opened_at") or "")[:10]


def _group_key(t: dict, field: str) -> str:
    if field == "all":
        return "ALL"
    for f in (field, "strategy", "source", "asset", "platform", "sleeve"):
        v = t.get(f)
        if v:
            return str(v)
    return "?"


def per_day_returns(trades: list) -> list:
    by = defaultdict(float)
    for t in trades:
        d = _date(t)
        if d:
            by[d] += float(t.get("net_profit") or 0)
    return [by[d] for d in sorted(by)]


def analyze(trades: list, trials: int = 1) -> dict:
    resolved = [t for t in trades if isinstance(t.get("won"), bool)]
    daily = per_day_returns(resolved)
    n, wins = len(resolved), sum(1 for t in resolved if t.get("won"))
    net = sum(float(t.get("net_profit") or 0) for t in resolved)
    out = {"n": n, "wins": wins, "net": round(net, 2), "days": len(daily),
           "psr": None, "dsr": None, "mtrl": None, "verdict": "n<5 days — inconclusive"}
    if len(daily) >= 5:
        from lib.hermes_significance import (probabilistic_sharpe_ratio,
                                             deflated_sharpe_ratio,
                                             min_track_record_length)
        out["psr"] = probabilistic_sharpe_ratio(daily)
        out["dsr"] = deflated_sharpe_ratio(daily, n_trials=trials)
        m = min_track_record_length(daily)
        out["mtrl"] = ("inf" if m == float("inf") else (int(m) if m is not None else None))
        if out["dsr"] is not None and out["dsr"] >= 0.95 and net > 0:
            out["verdict"] = "REAL edge (DSR>=0.95)"
        elif out["psr"] is not None and out["psr"] < 0.50:
            out["verdict"] = "NO edge — not even probably positive (variance)"
        else:
            out["verdict"] = "provisional — keep collecting"
    return out


def _load(path: Path) -> list:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _print(label: str, a: dict) -> None:
    wr = f"{a['wins']}/{a['n']} = {a['wins']/a['n']*100:.0f}%" if a["n"] else "0"
    psr = "n<5" if a["psr"] is None else f"{a['psr']:.2f}"
    dsr = "n<5" if a["dsr"] is None else f"{a['dsr']:.2f}"
    print(f"  {label:<16} {a['n']:>4} trades · {a['days']:>3} days · WR {wr:<12} "
          f"net ${a['net']:+.2f} · PSR {psr} · DSR {dsr} → {a['verdict']}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--path", default=str(HIST))
    ap.add_argument("--by", default=None, help="split by a field (strategy/source/asset)")
    ap.add_argument("--trials", type=int, default=1, help="DSR deflation (configs tried)")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        raise SystemExit(_selftest())

    trades = _load(Path(args.path))
    print(f"=== original_psr — per-day edge of the original system ({Path(args.path).name}) ===")
    if not trades:
        print(f"  no trades in {args.path} (or not a JSON list).")
        return
    _print("ALL", analyze(trades, args.trials))
    if args.by:
        groups = defaultdict(list)
        for t in trades:
            groups[_group_key(t, args.by)].append(t)
        print(f"\n  by {args.by}:")
        for g in sorted(groups, key=lambda k: -sum(float(t.get('net_profit') or 0) for t in groups[k])):
            _print(g, analyze(groups[g], args.trials))
    print("\n  Read: PSR<0.50 = the realized P&L is consistent with luck (a few big wins over")
    print("  a losing majority); only DSR>=0.95 over the distinct trading DAYS is real edge.")


def _selftest() -> int:
    # A net LOSER (a few small wins under a losing majority) → PSR low, never "REAL edge".
    trades = []
    for i in range(30):
        won = i < 3
        trades.append({"won": won, "net_profit": 5.0 if won else -1.0,
                       "closed_at": f"2026-05-{(i % 28) + 1:02d}T12:00:00Z", "strategy": "btc"})
    a = analyze(trades)
    assert a["n"] == 30 and a["wins"] == 3, a
    assert a["net"] < 0 and "REAL edge" not in a["verdict"], a     # net<0 can't be real edge
    # a consistent winner (positive every day, small spread → high Sharpe) → REAL
    steady = [{"won": True, "net_profit": 1.0 + (d % 2) * 0.5, "closed_at": f"2026-05-{d:02d}T12:00Z"}
              for d in range(1, 21)]
    s = analyze(steady)
    assert s["psr"] is not None and s["psr"] > 0.9 and "REAL edge" in s["verdict"], s
    # per-day grouping: two trades same day collapse to one observation
    same = [{"won": True, "net_profit": 1.0, "closed_at": "2026-05-01T09:00Z"},
            {"won": False, "net_profit": -1.0, "closed_at": "2026-05-01T15:00Z"}]
    assert analyze(same)["days"] == 1, analyze(same)
    print(f"loser set: net ${a['net']} PSR {a['psr']:.2f} (not real) — OK")
    print(f"steady set: PSR {s['psr']:.2f} (real) — OK; per-day grouping OK")
    print("PASS")
    return 0


if __name__ == "__main__":
    main()
