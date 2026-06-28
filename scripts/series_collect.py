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
DEFAULT_BANKROLL = 63.0   # this sleeve's own paper bankroll = live account balance


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


def _p01(m: dict, *names):
    """First present price as a 0-1 probability. Kalshi /markets serves prices as
    *_dollars (0-1); the bare yes_bid/yes_ask are absent → None, the field-name bug that
    made entry_p never record. Bare cents (/100) fallback retained."""
    for n in names:
        v = m.get(n)
        if v is not None:
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            return v if v <= 1.0 else v / 100.0
    return None


def _entry_prob(m: dict):
    """Best available entry probability for a market. Kalshi books are
    quote-on-demand (often NO resting bid/ask even where the market trades), so
    fall back to last_price — the last traded price IS a valid probability estimate
    when there's no resting quote. Returns (prob, source) or (None, None)."""
    yb = _p01(m, "yes_bid_dollars", "yes_bid")
    ya = _p01(m, "yes_ask_dollars", "yes_ask")
    if yb is not None and ya is not None and 0 < yb and ya < 1.0:
        return (yb + ya) / 2.0, "mid"
    for f in (("last_price_dollars", "last_price"), ("yes_price_dollars", "yes_price"),
              ("previous_price_dollars", "previous_price")):
        p = _p01(m, *f)
        if p is not None and 0 < p < 1.0:
            return p, f[-1]
    return None, None


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


def band_fade_returns(rows: list, lo: float = 0.90, hi: float = 0.93):
    """The PRE-REGISTERED test: in the [lo,hi] favorite band, FADE the favorite
    (buy NO at 1-p). Returns (per_trade, per_day, daily_chrono, n, realized_yes):
    daily_chrono = [(date, net_per_$1_cost)] for a bankroll curve; realized_yes =
    actual YES-rate in the band (the claim: realized < priced ⇒ favorites overpriced)."""
    band = [r for r in rows if r.get("outcome") in (0, 1)
            and r.get("entry_p") is not None and lo <= r["entry_p"] <= hi]
    per_trade, byday = [], defaultdict(lambda: [0.0, 0.0])
    for r in band:
        p, o = r["entry_p"], r["outcome"]
        cost, won = 1.0 - p, (o == 0)        # buy NO
        if cost <= 0:
            continue
        per_trade.append(((1.0 - cost) if won else -cost) / cost)
        d = str(r.get("settled_at") or r.get("ts") or "")[:10]
        byday[d][0] += (1.0 - cost) if won else -cost
        byday[d][1] += cost
    per_day = [n / c for n, c in byday.values() if c > 0]
    daily = [(d, byday[d][0]) for d in sorted(byday)]
    realized_yes = (sum(r["outcome"] for r in band) / len(band)) if band else None
    return per_trade, per_day, daily, len(band), realized_yes


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
    by_ticker = {r["ticker"]: r for r in rows}
    added = upgraded = 0
    for m in ms:
        tk = m.get("ticker", "")
        if not tk:
            continue
        prob, src = _entry_prob(m)          # mid if quoted, else last_price (quote-on-demand)
        if tk in by_ticker:
            # already recorded — but if we logged it with NO price and one has since
            # appeared (a quote or a trade), capture the first real price.
            r = by_ticker[tk]
            if r.get("entry_p") is None and prob is not None and r.get("outcome") is None:
                r.update(entry_p=prob, entry_src=src, yes_bid=m.get("yes_bid"),
                         yes_ask=m.get("yes_ask"), last_price=m.get("last_price"),
                         quote_seen_at=ts)
                upgraded += 1
            continue
        rows.append({"ts": ts, "ticker": tk, "entry_p": prob, "entry_src": src,
                     "yes_bid": m.get("yes_bid"), "yes_ask": m.get("yes_ask"),
                     "last_price": m.get("last_price"), "volume": m.get("volume"),
                     "open_interest": m.get("open_interest"),
                     "close_time": m.get("close_time", ""), "status": "open",
                     "outcome": None})
        by_ticker[tk] = rows[-1]
        added += 1
    _save(args.series, rows)
    priced = sum(1 for r in rows if r.get("entry_p") is not None)
    via_last = sum(1 for r in rows if r.get("entry_src") in ("last_price", "yes_price", "previous_price"))
    print(f"{args.series}: saw {len(ms)} open, +{added} new, +{upgraded} now-priced "
          f"(total {len(rows)}, {priced} ever-priced, {via_last} via last-trade not resting book).")


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
    # PRE-REGISTERED band-fade test (the documented 90–93% favorite-overpricing claim)
    lo, hi = getattr(args, "band_lo", 0.90), getattr(args, "band_hi", 0.93)
    trials = getattr(args, "trials", 1)
    bpt, bpd, _daily, bn, real_yes = band_fade_returns(settled, lo, hi)
    print(f"\nPRE-REGISTERED FADE TEST — band [{lo:.2f}, {hi:.2f}] (buy NO):")
    if bn == 0:
        print(f"  no settled markets in band yet — keep collecting.")
    else:
        priced = (lo + hi) / 2
        print(f"  n={bn} · market priced ~{priced:.0%} YES · realized YES {real_yes:.0%} "
              f"→ fade edge {(1-real_yes)-(1-priced):+.0%} (positive = favorites overpriced)")
        psr_s, mt_s = _psr(bpd)
        try:
            from lib.hermes_significance import deflated_sharpe_ratio
            dsr = deflated_sharpe_ratio(bpd, n_trials=trials)
            dsr_s = "n<5" if dsr is None else f"{dsr:.2f}"
        except Exception:
            dsr_s = "n/a"
        print(f"  per-day PSR {psr_s} · DSR(@{trials} trials) {dsr_s} · MinTRL {mt_s} · "
              f"sum-return {sum(bpt):+.2f} over {len(bpd)} days")
        print(f"  READ: edge>0 AND DSR≥0.95 ⇒ real & survives the search. Edge≤0 or "
              f"DSR<0.5 ⇒ band is efficient, drop it. (DSR uses n_trials to deflate "
              f"for how many bands/configs you tested — keep it honest.)")


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
    # band-fade: favorites at 0.91 that always lose → fading nets +, realized YES 0
    bover = [{"entry_p": 0.91, "outcome": 0, "settled_at": f"2026-06-{d:02d}"} for d in range(1, 9)]
    bpt, bpd, daily, bn, ry = band_fade_returns(bover, 0.90, 0.93)
    assert bn == 8 and ry == 0.0 and sum(bpt) > 0, (bn, ry, sum(bpt))
    assert band_fade_returns([{"entry_p": 0.80, "outcome": 0, "settled_at": "2026-06-01"}],
                             0.90, 0.93)[3] == 0   # out-of-band excluded
    print(f"band-fade OK (n={bn}, realized {ry:.0%}, fade sum {sum(bpt):+.2f})")
    print("PASS")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["collect", "settle", "eval", "status", "selftest"])
    ap.add_argument("series", nargs="?", help="Kalshi series ticker, e.g. KXAAAGASD")
    ap.add_argument("--band-lo", type=float, default=0.90, dest="band_lo",
                    help="eval: low edge of the pre-registered fade band (default 0.90)")
    ap.add_argument("--band-hi", type=float, default=0.93, dest="band_hi",
                    help="eval: high edge of the fade band (default 0.93)")
    ap.add_argument("--trials", type=int, default=1,
                    help="eval: how many bands/configs you tested, for DSR deflation "
                         "(keep at 1 if testing only the pre-registered band)")
    args = ap.parse_args()
    if args.mode == "selftest":
        raise SystemExit(selftest())
    if not args.series:
        ap.error("series ticker required (e.g. KXAAAGASD)")
    {"collect": cmd_collect, "settle": cmd_settle, "eval": cmd_eval,
     "status": cmd_status}[args.mode](args)


if __name__ == "__main__":
    main()
