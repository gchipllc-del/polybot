#!/usr/bin/env python3
"""asos_segment — diagnose the ASOS lock hit-rate.

Is the sub-98% lock hit-rate a real logic failure, or just the pre-station-verification
book draining? This splits SETTLED locks before/after a cutoff date (default 2026-06-21,
when the station map in asos_tracker.py was VERIFIED) and breaks the misses out per
series/station so you can see whether they cluster on specific cities (station mismatch)
or near bucket edges (CLI revision). Read-only; reads data/asos_lock.jsonl.

  python scripts/asos_segment.py                 # split at 2026-06-21
  python scripts/asos_segment.py --cutoff 2026-06-21
  python scripts/asos_segment.py --selftest
"""
import argparse
import json
import sys
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parent))
from asos_tracker import event_local_date              # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "data" / "asos_lock.jsonl"
TARGET = 0.98


def load(path: Path) -> list:
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return rows


def settled(rows: list) -> list:
    return [r for r in rows if r.get("status") in ("won", "lost")]


def _date(r: dict) -> str:
    # Station-LOCAL booking day (the cutoff is a wall-clock event), not the UTC prefix of
    # ts — evening locks roll to the next UTC day, leaking pre-cutoff locks into 'after'.
    return event_local_date(r)


def rate(rs: list):
    n = len(rs)
    w = sum(1 for r in rs if r.get("status") == "won")
    pnl = sum(float(r.get("paper_pnl") or 0) for r in rs)
    return n, w, (w / n if n else 0.0), pnl


def fmt_block(label: str, rs: list) -> None:
    n, w, hr, pnl = rate(rs)
    if n == 0:
        flag = "  (no settled)"
    else:
        flag = "  ✓" if hr >= TARGET else "  ⚠ below target"
    print(f"  {label:<26} n={n:<4} hit={w}/{n} = {hr:5.0%} (target ≥{TARGET:.0%}){flag}  P&L {pnl:+.2f}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cutoff", default="2026-06-21",
                    help="station-verification date; locks booked on/after this count as 'after'")
    ap.add_argument("--path", default=str(LOG))
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        raise SystemExit(_selftest())

    rows = settled(load(Path(args.path)))
    if not rows:
        print(f"no settled locks in {args.path}")
        return

    before = [r for r in rows if _date(r) < args.cutoff]
    after = [r for r in rows if _date(r) >= args.cutoff]

    print("=== ASOS lock hit-rate, split by station-verification cutoff ===")
    fmt_block("ALL settled", rows)
    fmt_block(f"before {args.cutoff}", before)
    fmt_block(f"on/after {args.cutoff}", after)
    print("  ↳ the 'on/after' line is the honest test of the corrected station map.")

    print("\n=== by series / station (sorted by # misses) ===")
    by = defaultdict(list)
    for r in rows:
        by[r.get("series", "?")].append(r)
    for s, rs in sorted(by.items(), key=lambda kv: -sum(1 for r in kv[1] if r.get("status") == "lost")):
        n, w, hr, pnl = rate(rs)
        # Show ALL stations in the group — a series can span the map correction (MDW→ORD),
        # so rs[0] alone would mislabel half the cohort.
        st = ",".join(sorted({str(r.get("station", "?")) for r in rs}))
        print(f"  {s:<14} [{st:<9}] {w}/{n} = {hr:4.0%}  losses={n - w}  P&L {pnl:+.2f}")

    print("\n=== the misses (eyeball station mismatch vs near-edge revision) ===")
    misses = [r for r in rows if r.get("status") == "lost"]
    if not misses:
        print("  none")
    for r in sorted(misses, key=_date):
        # YES locks decide on qc_high, NO on raw realized_high — show the gap from the
        # SIDE-APPROPRIATE basis so a spike-removed YES miss reads as near-edge, not a
        # phantom +Ndeg "station mismatch".
        is_yes = str(r.get("side", "")).lower() == "yes"
        basis = r.get("qc_high") if (is_yes and r.get("qc_high") is not None) else r.get("realized_high")
        gap = ""
        try:
            gap = f", basis−strike={float(basis) - float(r.get('strike')):+.0f}"
        except (TypeError, ValueError):
            pass
        qc = f" qc_high {r.get('qc_high')}" if r.get("qc_high") is not None else ""
        print(f"  {_date(r)}  {r.get('series', '?'):<12} [{r.get('station', '?'):<5}] "
              f"strike {r.get('strike')} side {r.get('side')} "
              f"realized_high {r.get('realized_high')}{qc} -> result {r.get('result')}{gap} "
              f"(yes_px {r.get('market_yes')})")


def _selftest() -> int:
    rows = [
        {"ts": "2026-06-18T20:00Z", "series": "KXHIGHNY", "station": "KNYC",
         "status": "lost", "result": "no", "side": "yes", "strike": 90,
         "realized_high": 89, "market_yes": 0.9, "paper_pnl": -0.9},
        {"ts": "2026-06-19T20:00Z", "series": "KXHIGHNY", "station": "KNYC",
         "status": "won", "result": "yes", "side": "yes", "strike": 85,
         "realized_high": 88, "market_yes": 0.8, "paper_pnl": 0.2},
        {"ts": "2026-06-21T20:00Z", "series": "KXHIGHCHI", "station": "KMDW",
         "status": "won", "result": "yes", "side": "yes", "strike": 80,
         "realized_high": 83, "market_yes": 0.7, "paper_pnl": 0.3},
        {"ts": "2026-06-20T20:00Z", "series": "KXHIGHNY", "station": "KNYC",
         "status": "open", "result": "", "side": "yes"},
    ]
    s = settled(rows)
    assert len(s) == 3, s
    n, w, _, _ = rate(s)
    assert n == 3 and w == 2, (n, w)
    before = [r for r in s if _date(r) < "2026-06-21"]
    after = [r for r in s if _date(r) >= "2026-06-21"]
    assert len(before) == 2 and len(after) == 1, (len(before), len(after))
    assert rate(after)[2] == 1.0, rate(after)        # corrected map: clean after cutoff
    assert abs(rate(before)[2] - 0.5) < 1e-9, rate(before)
    print("split + rate OK")
    print("PASS")
    return 0


if __name__ == "__main__":
    main()
