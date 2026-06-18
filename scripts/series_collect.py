#!/usr/bin/env python3
"""series_collect — generic forward price→outcome collector for ONE Kalshi series.

The disciplined first test for any candidate lead (e.g. KXAAAGASD daily gas): before
building a data predictor or a bankroll/dashboard sleeve, just record each market's
ENTRY price and its eventual OUTCOME, then check whether the market is even
MISCALIBRATED. If "yes" priced at p resolves yes at rate ≈ p across the book, it's
efficient → no edge → walk away cheaply. If there's a systematic gap (favorite-
longshot bias), THEN it's worth building a predictor to exploit it.

Needs only the Kalshi API (no external data feed). Records nothing but observations;
places NO orders. Per-day PSR uses the same lib/hermes_significance bar as everything else.

  python scripts/series_collect.py collect KXAAAGASD   # snapshot open markets (schedule this)
  python scripts/series_collect.py settle  KXAAAGASD   # resolve outcomes
  python scripts/series_collect.py eval    KXAAAGASD   # calibration + per-day PSR
  python scripts/series_collect.py status  KXAAAGASD
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
DATA = ROOT / "data"


def _ledger(series: str) -> Path:
    safe = "".join(c for c in series if c.isalnum() or c in "-_").upper()
    return DATA / f"series_collect_{safe}.jsonl"


def _load(series: str) -> list:
    p = _ledger(series)
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def _save(series: str, rows: list) -> None:
    p = _ledger(series)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _mid(yes_bid, yes_ask):
    """Entry probability proxy = mid of the YES book (cents→prob). None if the book
    is one-sided/empty (can't fairly say what 'the market price' was)."""
    try:
        yb, ya = float(yes_bid), float(yes_ask)
    except (TypeError, ValueError):
        return None
    if yb <= 0 or ya <= 0 or ya >= 100:
        return None
    return (yb + ya) / 200.0


# ── pure analysis (testable) ────────────────────────────────────────────────

def calibration(rows: list, bins: int = 5) -> list:
    """Bucket settled rows by entry mid-price; return [(lo, hi, n, mkt_p, realized)]
    so we can see if priced prob ≈ realized yes-rate (calibrated = efficient)."""
    settled = [r for r in rows if r.get("outcome") in (0, 1) and r.get("entry_p") is not None]
    out = []
    for i in range(bins):
        lo, hi = i / bins, (i + 1) / bins
        b = [r for r in settled if (lo <= r["entry_p"] < hi or (i == bins - 1 and r["entry_p"] == 1.0))]
        if not b:
            continue
        mkt = sum(r["entry_p"] for r in b) / len(b)
        realized = sum(r["outcome"] for r in b) / len(b)
        out.append((lo, hi, len(b), mkt, realized))
    return out


def fade_returns(rows: list):
    """Naïve no-predictor FLB probe: 'fade the favorite' — when entry mid > 0.5 buy
    NO, else buy YES; 1 unit. Per-resolution return + per-day grouping. This is just
    to see if a calibration gap is monetizable AT ALL, not a real strategy."""
    per_trade, byday = [], defaultdict(lambda: [0.0, 0.0])
    for r in rows:
        p, o = r.get("entry_p"), r.get("outcome")
        if p is None or o not in (0, 1):
            continue
        if p > 0.5:                       # fade favorite → buy NO at (1-p)
            cost, won = 1.0 - p, (o == 0)
        else:                             # back longshot's complement → buy YES at p
            cost, won = p, (o == 1)
        if cost <= 0:
            continue
        ret = ((1.0 - cost) if won else -cost) / cost
        per_trade.append(ret)
        d = str(r.get("settled_at") or r.get("ts") or "")[:10]
        byday[d][0] += (1.0 - cost) if won else -cost
        byday[d][1] += cost
    per_day = [n / c for n, c in byday.values() if c > 0]
    return per_trade, per_day


# ── live (Kalshi API) ───────────────────────────────────────────────────────

def cmd_collect(args) -> None:
    from fetch_backtest_data import _kalshi_get
    rows = _load(args.series)
    seen = {r["ticker"] for r in rows}
    try:
        data = _kalshi_get("/markets", {"series_ticker": args.series,
                                        "status": "open", "limit": 200})
    except Exception as e:
        print(f"! {args.series}: {e}", file=sys.stderr)
        return
    ms = data.get("markets", []) or []
    ts = datetime.now(timezone.utc).isoformat()
    added = 0
    for m in ms:
        tk = m.get("ticker", "")
        if not tk or tk in seen:
            continue
        rows.append({"ts": ts, "ticker": tk,
                     "entry_p": _mid(m.get("yes_bid"), m.get("yes_ask")),
                     "yes_bid": m.get("yes_bid"), "yes_ask": m.get("yes_ask"),
                     "close_time": m.get("close_time", ""), "status": "open",
                     "outcome": None})
        seen.add(tk)
        added += 1
    _save(args.series, rows)
    print(f"{args.series}: saw {len(ms)} open, recorded {added} new "
          f"(total {len(rows)}).")


def cmd_settle(args) -> None:
    from fetch_backtest_data import _kalshi_get
    rows = _load(args.series)
    now = datetime.now(timezone.utc).isoformat()
    changed = 0
    for r in rows:
        if r.get("outcome") in (0, 1) or r.get("status") == "settled":
            continue
        try:
            m = _kalshi_get(f"/markets/{r['ticker']}", {}).get("market", {})
        except Exception:
            continue
        res = str(m.get("result", "") or "").lower()
        if res not in ("yes", "no"):
            continue
        r["outcome"] = 1 if res == "yes" else 0
        r["status"] = "settled"
        r["settled_at"] = now
        changed += 1
    if changed:
        _save(args.series, rows)
    print(f"{args.series}: settled {changed} markets.")


def _psr(per_day):
    from lib.hermes_significance import (probabilistic_sharpe_ratio,
                                         min_track_record_length)
    p = probabilistic_sharpe_ratio(per_day)
    m = min_track_record_length(per_day)
    return ("n<5" if p is None else f"{p:.2f}",
            "n<5" if m is None else ("∞" if m == float("inf") else str(int(m))))


def cmd_eval(args) -> None:
    rows = _load(args.series)
    settled = [r for r in rows if r.get("outcome") in (0, 1)]
    if len(settled) < 5:
        print(f"{args.series}: only {len(settled)} settled — keep collecting "
              f"(need ~weeks of distinct days before calibration means anything).")
        return
    days = len({str(r.get('settled_at') or '')[:10] for r in settled})
    print(f"=== {args.series} — {len(settled)} settled across {days} days ===")
    print("CALIBRATION (is priced prob ≈ realized? then it's efficient = no edge):")
    print(f"  {'price band':>12} {'n':>4} {'mkt_p':>6} {'realized':>9} {'gap':>7}")
    for lo, hi, n, mkt, real in calibration(settled):
        print(f"  {f'{lo:.2f}-{hi:.2f}':>12} {n:>4} {mkt:>6.2f} {real:>9.2f} "
              f"{real-mkt:>+7.2f}")
    pt, pd = fade_returns(settled)
    psr_s, mt_s = _psr(pd)
    net = sum(r for r in pt)
    print(f"\nNAÏVE fade-probe (no predictor): {len(pt)} trades, "
          f"per-day PSR {psr_s}, MinTRL {mt_s}, sum-return {net:+.2f}")
    print("  READ: flat calibration gaps + PSR<0.5 ⇒ efficient, drop it. A "
          "consistent gap ⇒ worth a real predictor + bankroll/dash sleeve.")


def cmd_status(args) -> None:
    rows = _load(args.series)
    openn = [r for r in rows if r.get("outcome") is None]
    settled = [r for r in rows if r.get("outcome") in (0, 1)]
    days = len({str(r.get('settled_at') or '')[:10] for r in settled})
    print(f"{args.series}: {len(openn)} awaiting outcome · {len(settled)} settled "
          f"across {days} days · ledger {_ledger(args.series).name}")


def selftest() -> int:
    # calibration: a perfectly efficient book (realized == priced) shows ~0 gap
    eff = [{"entry_p": 0.2, "outcome": 0}, {"entry_p": 0.2, "outcome": 0},
           {"entry_p": 0.2, "outcome": 0}, {"entry_p": 0.2, "outcome": 0},
           {"entry_p": 0.2, "outcome": 1},                     # 1/5 = 0.20 realized
           {"entry_p": 0.8, "outcome": 1}, {"entry_p": 0.8, "outcome": 1},
           {"entry_p": 0.8, "outcome": 1}, {"entry_p": 0.8, "outcome": 1},
           {"entry_p": 0.8, "outcome": 0}]                     # 4/5 = 0.80 realized
    cal = {round(lo, 1): (real - mkt) for lo, hi, n, mkt, real in calibration(eff, bins=5)}
    assert abs(cal.get(0.2, 9)) < 1e-9 and abs(cal.get(0.8, 9)) < 1e-9, cal
    print("calibration OK (efficient book → ~0 gap)")
    # _mid guards
    assert _mid(40, 60) == 0.5 and _mid(0, 60) is None and _mid(40, 100) is None
    print("_mid OK")
    # fade-probe on a MISCALIBRATED book (favorites overpriced) should net > 0
    over = [{"entry_p": 0.8, "outcome": 0, "settled_at": f"2026-06-{d:02d}"}
            for d in range(1, 9)]          # 'favorite' at .80 always loses → fading NO wins
    pt, pd = fade_returns(over)
    assert pt and sum(pt) > 0, (pt, pd)
    print(f"fade-probe OK (overpriced favorites → fade nets +, sum {sum(pt):.2f})")
    print("PASS")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["collect", "settle", "eval", "status", "selftest"])
    ap.add_argument("series", nargs="?", help="Kalshi series ticker, e.g. KXAAAGASD")
    args = ap.parse_args()
    if args.mode == "selftest":
        raise SystemExit(selftest())
    if not args.series:
        ap.error("series ticker required (e.g. KXAAAGASD)")
    {"collect": cmd_collect, "settle": cmd_settle, "eval": cmd_eval,
     "status": cmd_status}[args.mode](args)


if __name__ == "__main__":
    main()
