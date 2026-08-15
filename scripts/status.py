#!/usr/bin/env python3
"""status — "how are our trades going?" answered in one screen.

Pulls the three surfaces that matter and states the honest verdict:
  * PAPER  - forward-only, out-of-sample. The only record that can earn a live phase.
  * STAGE0 - the mispricing table, filtered to cells with real sample size.
  * SHADOW - the in-sample replay (context only; it can never prove anything).

Sample size is reported in INDEPENDENT WINDOWS, not trades. Many strikes share one
15-minute window and resolve on the same price move, so trade counts overstate evidence -
that is how a control rule appeared to "win 68% over 44 trades" that were really ~8 bets.

  py scripts/status.py
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

MIN_WINDOWS = 100          # windows needed before a rule's number means anything


def _wilson(p: float, n: int, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return (0.0, 1.0)
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z / d * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, c - h), min(1.0, c + h))


def paper_section() -> list[str]:
    out = ["PAPER BOOK  (forward-only, out-of-sample - the record that counts)"]
    try:
        import paper_trader as pt
        rows_for_windows = pt._load(pt.LEDGER)
        rep = pt.build_report(rows_for_windows)
        _win_of = pt._window_of
    except Exception as e:  # noqa: BLE001
        return out + [f"  unavailable: {type(e).__name__}: {str(e)[:60]}"]
    if not rep["n_closed"]:
        return out + ["  no closed trades yet - a frozen rule must fire on a live market."]
    wr = "-" if rep["win_rate"] is None else f"{rep['win_rate']*100:.1f}%"
    out.append(f"  {rep['n_closed']} closed trades across {rep.get('windows', 0)} "
               f"independent windows | {rep['n_open']} open")
    out.append(f"  net ${rep['net']:+.2f}   equity ${rep['equity']:.2f}   WR {wr}")
    if rep.get("since"):
        out.append(f"  since {str(rep['since'])[:16]}")
    # Per-window $ significance. The old verdict tested win-rate CI > 0.5, which a
    # longshot rule (WR ~14%) can NEVER pass even when genuinely profitable - it
    # mislabeled the book's only positive rule "inconclusive" forever. The honest test
    # for every rule shape is the same one: mean $ per independent WINDOW vs zero.
    win_pnl: dict = {}
    for r in rows_for_windows:
        if r.get("t") != "close":
            continue
        key = (r.get("rule"), _win_of(r.get("ticker")))
        win_pnl[key] = win_pnl.get(key, 0.0) + float(r.get("pnl") or 0.0)
    out.append("")
    out.append("  rule                    trades  windows    WR     net$   $/win     verdict")
    for name, b in sorted(rep["by_rule"].items()):
        n, w = b["n"], b.get("windows", 0)
        p = b["wins"] / n if n else 0.0
        vals = [v for (rn, _), v in win_pnl.items() if rn == name]
        nw = len(vals)
        mean = sum(vals) / nw if nw else 0.0
        var = (sum((x - mean) ** 2 for x in vals) / (nw - 1)) if nw > 1 else 0.0
        se = math.sqrt(var / nw) if nw else 0.0
        if w < MIN_WINDOWS:
            v = f"thin ({w}/{MIN_WINDOWS} windows)"
        elif nw > 1 and mean - 1.96 * se > 0:
            v = "POSITIVE (significant)"
        elif nw > 1 and mean + 1.96 * se < 0:
            v = "NEGATIVE (significant)"
        else:
            v = ("positive, not significant" if b["pnl"] > 0
                 else "negative, not significant")
        out.append(f"  {name:<22} {n:>6} {w:>8}  {p*100:5.1f}%  {b['pnl']:+7.2f}  "
                   f"{mean:+7.3f}  {v}")
    if rep.get("open_positions"):
        out.append("")
        out.append("  open now:")
        for o in rep["open_positions"][:6]:
            out.append(f"    {o.get('ticker','?'):<30} {o.get('rule','?'):<22} "
                       f"{o.get('side','?')} @ {float(o.get('price',0)):.2f}")
    return out


def stage0_section() -> list[str]:
    out = ["STAGE-0 MISPRICING TABLE  (cells with real sample size only)"]
    try:
        import stage0_collector as s0
        rows = []
        if s0.LOG.exists():
            import json
            for line in s0.LOG.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        rep = s0.build_report(rows)
    except Exception as e:  # noqa: BLE001
        return out + [f"  unavailable: {type(e).__name__}: {str(e)[:60]}"]
    if not rep.get("joined"):
        return out + ["  nothing joined yet - the collector must see a market before it settles."]
    out.append(f"  {rep['joined']} joined observations, {rep['n_settles']} settled markets")
    out.append("")
    out.append("  band     bucket     n   cost  realized    gap   CI95 low  read")
    interesting = []
    for (band, bucket), c in rep["table"].items():
        n = c["n"]
        if n < 100:
            continue
        cost, real, fee = c["cost"] / n, c["wins"] / n, c["fee"] / n
        lo, hi = _wilson(real, n)
        be = cost + fee                       # breakeven win rate after friction
        if lo > be:
            read = "SIGNIFICANT +EV"
        elif hi < be:
            read = "SIGNIFICANT -EV"
        else:
            read = "not significant"
        interesting.append((abs(real - cost), band, bucket, n, cost, real,
                            real - cost, lo, read))
    if not interesting:
        out.append("  (no cell has reached n=100 yet)")
    for _, band, bucket, n, cost, real, gap, lo, read in sorted(interesting, reverse=True):
        out.append(f"  {band:8} {bucket:8} {n:>4} {cost:6.3f} {real:9.3f} {gap:+7.3f} "
                   f"{lo:9.3f}  {read}")
    return out


def shadow_section() -> list[str]:
    out = ["SHADOW BOOK  (in-sample replay - context only, proves nothing)"]
    try:
        import shadow_book as sb
        rep = sb.build(sb._load(sb.LOG))
    except Exception as e:  # noqa: BLE001
        return out + [f"  unavailable: {type(e).__name__}: {str(e)[:60]}"]
    any_row = False
    for name, r in rep["rules"].items():
        if not r["n"]:
            continue
        any_row = True
        wr = "-" if r["win_rate"] is None else f"{r['win_rate']*100:.1f}%"
        out.append(f"  {name:<24} n={r['n']:<5} WR {wr:<7} net ${r['net']:+.2f}")
    if not any_row:
        out.append("  no replayed trades yet.")
    return out


def verdict(lines_ctx: dict) -> list[str]:
    return [
        "WHERE WE STAND",
        "  Real money: OFF. It stays off until a rule is positive out-of-sample at",
        f"  {MIN_WINDOWS}+ independent windows - not trades, windows.",
        "  The shadow book can never earn that; only the paper book can.",
    ]


def main() -> int:
    print("=" * 74)
    print("POLYBOT CRYPTO-15 STATUS")
    print("=" * 74)
    for section in (paper_section, stage0_section, shadow_section):
        for line in section():
            print(line)
        print()
    for line in verdict({}):
        print(line)
    print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
