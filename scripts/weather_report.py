#!/usr/bin/env python3
"""
Weather sleeve P&L report — trust-aware breakdown of the paper ledger.

Headline paper P&L on the weather sleeve is misleading because most rows
settle via the NWS observation, which diverges from Kalshi's official
settlement (the documented reason paper looked profitable while live
didn't). This report separates the trustworthy signal from the noise:

  * Settlement source split: kalshi_result (REAL) vs nws_observation
    (overstated) vs unknown/legacy rows.
  * Settlement-rule consistency: how many recorded outcomes contradict
    "YES wins iff observed_temp >= strike" — i.e. NWS/Kalshi divergence.
  * Cheap-NO cohort (fill < threshold): the 19:1-payoff bets that carry
    most paper P&L but historically didn't replicate live.
  * Near-strike (rounding-fragile) settlements.
  * Live vs paper (when the rows carry an is_live flag).
  * Per-side, per-city, per-day, edge-bucket calibration.

Read-only. No network, no trading. Pure measurement on the JSONL ledger.

Usage:
    python scripts/weather_report.py
    python scripts/weather_report.py --log data/weather_daily_paper.jsonl
    python scripts/weather_report.py --since 2026-06-01 --cheap-fill 0.15
"""

from __future__ import annotations

import argparse
import collections
import json
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG = ROOT / "data" / "weather_paper.jsonl"


def _f(row: dict, key: str, default: float = 0.0) -> float:
    v = row.get(key)
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def load_rows(path: Path, since: str | None) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if since and (r.get("opened_at") or "") < since:
                continue
            rows.append(r)
    return rows


def _wr(rs: list[dict]) -> tuple[int, int, float]:
    """(wins, losses, win_rate) over settled rows."""
    w = sum(1 for r in rs if r.get("status") == "won")
    l = sum(1 for r in rs if r.get("status") == "lost")
    return w, l, (w / (w + l) if (w + l) else 0.0)


def _pnl(rs: list[dict]) -> float:
    return sum(_f(r, "paper_pnl") for r in rs)


def _line(label: str, rs: list[dict], width: int = 22) -> str:
    w, l, wr = _wr(rs)
    return (f"  {label:<{width}} n={len(rs):>4}  "
            f"WR={wr * 100:5.1f}% ({w}W/{l}L)  pnl=${_pnl(rs):+10.2f}")


def _settlement_source(r: dict) -> str:
    """Trust bucket for a row. Prefer the explicit settled_via; else infer:
    a recorded observed temp means it went through the NWS path; otherwise
    we can't tell."""
    via = r.get("settled_via")
    if via:
        return str(via)
    if r.get("actual_temp_f") is not None:
        return "nws_observation?(inferred)"
    return "unknown(no settled_via)"


def report(rows: list[dict], *, cheap_fill: float, near_strike: float) -> None:
    settled = [r for r in rows if r.get("status") in ("won", "lost")]
    open_n = sum(1 for r in rows if r.get("status") == "open")
    other = collections.Counter(
        r.get("status") for r in rows
        if r.get("status") not in ("won", "lost", "open"))

    cap = sum(_f(r, "notional") for r in rows)
    pnl = _pnl(rows)
    print("=" * 64)
    print(f"WEATHER SLEEVE REPORT — {len(rows)} trades "
          f"({settled and (rows[0].get('opened_at') or '')[:10]} → "
          f"{settled and (rows[-1].get('opened_at') or '')[:10]})")
    print("=" * 64)
    w, l, wr = _wr(settled)
    print(f"settled={len(settled)}  open={open_n}  other={dict(other)}")
    print(f"W/L={w}/{l}  WR={wr * 100:.1f}%  "
          f"P&L=${pnl:+.2f}  capital=${cap:.2f}  "
          f"ROI={pnl / cap * 100 if cap else 0:+.1f}%")

    # ── THE key view: settlement source ──────────────────────────────
    print("\n── by settlement source (kalshi_result = the only trustworthy P&L) ──")
    by_src: dict[str, list] = collections.defaultdict(list)
    for r in settled:
        by_src[_settlement_source(r)].append(r)
    for src in sorted(by_src, key=lambda s: -_pnl(by_src[s])):
        print(_line(src, by_src[src]))
    kalshi = [r for r in settled if str(r.get("settled_via")) == "kalshi_result"]
    if kalshi:
        print(f"\n  → TRUSTWORTHY (kalshi_result only): {_line('', kalshi).strip()}")
    else:
        print("\n  → No rows tagged settled_via='kalshi_result'. This ledger predates"
              "\n    the settled_via field, so live-grade P&L can't be isolated here."
              "\n    Re-run after the sleeve writes settled_via to get the real number.")

    # ── Settlement-rule consistency (NWS divergence proxy) ────────────
    checkable = [r for r in settled if r.get("actual_temp_f") is not None]
    mism = []
    for r in checkable:
        yes_won = _f(r, "actual_temp_f") >= _f(r, "strike_f")
        should_win = ((r.get("side") == "YES" and yes_won)
                      or (r.get("side") == "NO" and not yes_won))
        if should_win != (r.get("status") == "won"):
            mism.append(r)
    if checkable:
        print(f"\n── settlement consistency (vs 'YES wins iff temp>=strike') ──")
        print(f"  checked={len(checkable)}  contradicting outcomes="
              f"{len(mism)} ({len(mism) / len(checkable) * 100:.0f}%)  "
              f"pnl on those=${_pnl(mism):+.2f}")
        print("  (a high rate = NWS/Kalshi settlement divergence — paper P&L is "
              "built on a shaky basis)")

    # ── Cheap-NO cohort ──────────────────────────────────────────────
    deep = [r for r in settled
            if r.get("side") == "NO" and _f(r, "fill_price") < cheap_fill]
    rest = [r for r in settled if r not in deep]
    print(f"\n── cheap-NO cohort (NO, fill < {cheap_fill}) — the 19:1 bets ──")
    print(_line(f"cheap-NO", deep))
    print(_line("everything else", rest))
    if settled and _pnl(settled):
        print(f"  cheap-NO share of total P&L: "
              f"{_pnl(deep) / _pnl(settled) * 100:.0f}%")

    # ── Near-strike (rounding-fragile) ───────────────────────────────
    near = [r for r in checkable
            if abs(_f(r, "actual_temp_f") - _f(r, "strike_f")) <= near_strike]
    if checkable:
        print(f"\n── rounding-fragile (|observed - strike| <= {near_strike}°F) ──")
        print(_line(f"near-strike", near))

    # ── live vs paper (if flagged) ───────────────────────────────────
    if any("is_live" in r for r in rows):
        print("\n── live vs paper ──")
        print(_line("LIVE", [r for r in settled if r.get("is_live")]))
        print(_line("paper-only", [r for r in settled if not r.get("is_live")]))

    # ── side / city ──────────────────────────────────────────────────
    print("\n── by side (settled) ──")
    for side in ("YES", "NO"):
        print(_line(side, [r for r in settled if r.get("side") == side]))
    print("\n── by city (settled) ──")
    for city in sorted(set(str(r.get("city")) for r in settled)):
        print(_line(city, [r for r in settled if str(r.get("city")) == city]))

    # ── edge-bucket calibration ──────────────────────────────────────
    print("\n── edge-bucket calibration (WR should rise with edge) ──")
    buckets: dict[str, list] = collections.defaultdict(list)
    for r in settled:
        e = abs(_f(r, "edge"))
        lo = int(e * 100 // 5) * 5
        buckets[f"{lo}-{lo + 5}%"].append(r)
    for k in sorted(buckets, key=lambda x: int(x.split("-")[0])):
        print(_line(f"edge {k}", buckets[k]))

    # ── win/loss asymmetry ───────────────────────────────────────────
    wins = [_f(r, "paper_pnl") for r in settled if r.get("status") == "won"]
    loss = [_f(r, "paper_pnl") for r in settled if r.get("status") == "lost"]
    if wins and loss:
        aw, al = statistics.mean(wins), statistics.mean(loss)
        print(f"\n── payoff shape ──")
        print(f"  avg win=${aw:+.2f}  avg loss=${al:+.2f}  "
              f"ratio={aw / abs(al):.1f}:1  "
              f"(at {wr * 100:.0f}% WR, breakeven ratio is "
              f"{(1 - wr) / wr if wr else float('inf'):.2f}:1)")

    # ── by day ───────────────────────────────────────────────────────
    print("\n── by day ──")
    byday: dict[str, list] = collections.defaultdict(list)
    for r in rows:
        byday[(r.get("opened_at") or "")[:10]].append(r)
    for d in sorted(byday):
        rs = byday[d]
        print(f"  {d}  n={len(rs):>3}  pnl=${_pnl(rs):+10.2f}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Trust-aware weather paper P&L report")
    ap.add_argument("--log", default=str(DEFAULT_LOG),
                    help="ledger path (default: data/weather_paper.jsonl)")
    ap.add_argument("--since", default=None,
                    help="only trades opened on/after this ISO date (YYYY-MM-DD)")
    ap.add_argument("--cheap-fill", type=float, default=0.15,
                    help="fill price below which a NO bet is 'cheap-NO' (default 0.15)")
    ap.add_argument("--near-strike", type=float, default=0.5,
                    help="°F band around strike counted as rounding-fragile (default 0.5)")
    args = ap.parse_args()

    path = Path(args.log)
    rows = load_rows(path, args.since)
    if not rows:
        print(f"No trades found in {path}"
              + (f" since {args.since}" if args.since else ""))
        return
    report(rows, cheap_fill=args.cheap_fill, near_strike=args.near_strike)


if __name__ == "__main__":
    main()
