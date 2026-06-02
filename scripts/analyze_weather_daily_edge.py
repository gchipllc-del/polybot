#!/usr/bin/env python3
"""Read-only validation analyzer for the Kalshi DAILY weather sleeve.

WHY THIS EXISTS
---------------
On 2026-05-31 a strike-type bug was fixed in lib/weather_daily_signal.py: the
old code parsed the T<n> ticker suffix and ALWAYS computed P(temp >= strike),
which inverted every 'less'-type market (fake +0.97 edges -> bought near-certain
losers) and silently dropped every 'between' bucket. The 23 paper records
written before the fix were quarantined (entry_schema="pre_strike_type_fix").

Before building any LIVE path for this sleeve, we must validate two things on
POST-FIX data:
  1. WIN RATE on settled paper trades recovers (target the ~60-70% band).
  2. AT-BATS: does the (now-correct) gate fire often enough to be worth wiring
     live, or do the wider daily-horizon sigmas keep edges sub-threshold so it
     sits flat forever? The daily signal log captures EVERY market scanned, so
     we can measure would-qualify frequency without placing anything.

WHAT IT REPORTS
---------------
  [paper]  settled post-fix paper trades: n, WR, net (filters to
           entry_schema=="strike_type_aware_v1"; pre-fix records excluded).
  [atbats] over the post-fix signal log, how many DISTINCT markets cleared the
           edge threshold + forecast-direction margin (a proxy for "would the
           gate have fired"), bucketed by strike_type and city.
  [edges]  edge distribution (how close the near-misses are) so we can see
           whether a small, defensible threshold change would add at-bats.

Everything is READ-ONLY: reads the signal + paper JSONL and the live gate
params. No network, no order, no file writes.

USAGE
-----
  python -m scripts.analyze_weather_daily_edge
  python -m scripts.analyze_weather_daily_edge --since 2026-05-31T21:00:00
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

SIGNAL_LOG = _ROOT / "data" / "weather_daily_signal.jsonl"
PAPER_LOG = _ROOT / "data" / "weather_daily_paper.jsonl"

# The fix landed 2026-05-31 ~21:00 UTC. Records sampled at/after this carry the
# strike-type-aware schema. Default --since to that boundary.
DEFAULT_SINCE = "2026-05-31T21:00:00+00:00"

POSTFIX_SCHEMA = "strike_type_aware_v1"


def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _gate_params() -> dict:
    """Pull the LIVE daily-weather gate params (same source the paper recorder
    uses) so the at-bats proxy matches production thresholds."""
    try:
        from lib.weather_daily_paper import _effective_params
        return _effective_params()
    except Exception as e:  # keep runnable if import path shifts
        print(f"  [warn] could not load live params ({e}); using defaults")
        return {"min_edge_threshold": 0.10, "forecast_buffer_f": 2.0,
                "forecast_buffer_f_yes": 3.0, "max_fill_for_buy": 0.45,
                "no_side_only": False}


def _would_fire(s: dict, p: dict) -> tuple[bool, str]:
    """Replicate the paper-module gate's ACCEPT logic on a signal record.
    Returns (would_fire, reason_if_not). Proxy only — does not re-run Kelly
    sizing or the dup-open check (those don't bear on signal quality)."""
    nws_p = s.get("nws_p_yes")
    market_p = s.get("market_p_yes")
    if market_p is None:
        market_p = s.get("yes_ask")
    if nws_p is None or market_p is None:
        return False, "missing_data"
    edge = nws_p - market_p
    if abs(edge) < p["min_edge_threshold"]:
        return False, "edge_too_small"
    side = "YES" if edge > 0 else "NO"
    ym = s.get("yes_margin_f")
    if ym is None:
        return False, "no_margin"
    ym = float(ym)
    if side == "YES" and ym < p["forecast_buffer_f_yes"]:
        return False, "forecast_dir_yes"
    if side == "NO" and ym > -p["forecast_buffer_f"]:
        return False, "forecast_dir_no"
    if side == "YES" and p.get("no_side_only"):
        return False, "yes_disabled"
    # fill check (NO uses no_ask, else 1-yes_ask)
    fill = s.get("yes_ask") if side == "YES" else s.get("no_ask")
    if fill is None and side == "NO" and s.get("yes_ask") is not None:
        fill = round(1.0 - float(s["yes_ask"]), 4)
    if fill is None:
        return False, "no_fill"
    fill = float(fill)
    if not (0.05 <= fill <= 0.95):
        return False, "extreme_price"
    if fill > p["max_fill_for_buy"]:
        return False, "fill_too_high"
    return True, "FIRE"


def analyze_paper(since: datetime) -> None:
    recs = _load(PAPER_LOG)
    postfix = [r for r in recs if r.get("entry_schema") == POSTFIX_SCHEMA]
    prefix = [r for r in recs if r.get("entry_schema") != POSTFIX_SCHEMA]
    settled = [r for r in postfix if r.get("status") in ("won", "lost")]
    print(f"\n{'='*68}\n[paper] POST-FIX settled paper trades (entry_schema={POSTFIX_SCHEMA})\n{'='*68}")
    print(f"  pre-fix quarantined (excluded): {len(prefix)}   "
          f"post-fix total: {len(postfix)}   post-fix settled: {len(settled)}")
    if not settled:
        print("  No settled post-fix paper trades yet — sleeve has not opened a")
        print("  qualifying trade since the fix. WR validation pending more data.")
        opens = [r for r in postfix if r.get("status") == "open"]
        if opens:
            print(f"  ({len(opens)} post-fix trades currently OPEN, awaiting settle.)")
        return
    n = len(settled)
    w = sum(1 for r in settled if r["status"] == "won")
    net = sum(float(r.get("paper_pnl") or 0) for r in settled)
    print(f"  n={n}  WR={w/n*100:.1f}%  net=${net:+.2f}  avg=${net/n:+.3f}")
    by_side = defaultdict(list)
    for r in settled:
        by_side[str(r.get("side"))].append(r)
    for side, rs in sorted(by_side.items()):
        ww = sum(1 for r in rs if r["status"] == "won")
        nn = len(rs)
        pp = sum(float(r.get("paper_pnl") or 0) for r in rs)
        print(f"    {side:4s} n={nn:3d} WR={ww/nn*100:5.1f}% net=${pp:+8.2f}")


def analyze_atbats(since: datetime) -> None:
    recs = _load(SIGNAL_LOG)
    postfix = [r for r in recs
               if r.get("strike_type") and (_dt(r.get("sample_at")) or datetime.min.replace(tzinfo=timezone.utc)) >= since]
    print(f"\n{'='*68}\n[atbats] would-fire proxy over post-fix signal scans (since {since:%Y-%m-%d %H:%M}Z)\n{'='*68}")
    if not postfix:
        print("  No post-fix signal records in window.")
        return
    p = _gate_params()
    print(f"  gate: min_edge={p['min_edge_threshold']}  buf_no={p['forecast_buffer_f']}°F "
          f"buf_yes={p['forecast_buffer_f_yes']}°F  max_fill={p['max_fill_for_buy']}  "
          f"no_side_only={p.get('no_side_only')}")
    print(f"  post-fix signal samples: {len(postfix)}  "
          f"(distinct markets: {len({r.get('market_ticker') for r in postfix})})")

    reasons = Counter()
    fire_markets: set[str] = set()
    fire_by_type = Counter()
    fire_by_city = Counter()
    fire_examples = []
    for s in postfix:
        ok, reason = _would_fire(s, p)
        reasons[reason] += 1
        if ok:
            tkr = s.get("market_ticker")
            if tkr not in fire_markets:
                fire_markets.add(tkr)
                fire_by_type[s.get("strike_type")] += 1
                fire_by_city[s.get("city_key")] += 1
                if len(fire_examples) < 12:
                    fire_examples.append(s)

    print(f"\n  WOULD-FIRE distinct markets: {len(fire_markets)}")
    print(f"  sample-level disposition: {dict(reasons)}")
    if fire_by_type:
        print(f"  by strike_type: {dict(fire_by_type)}")
        print(f"  by city: {dict(fire_by_city)}")
    if fire_examples:
        print(f"\n  would-fire examples (distinct markets):")
        print(f"    {'city':9s} {'type':8s} {'sd':3s} {'edge':>7s} {'ymarg':>6s} "
              f"{'p_yes':>6s} {'fill':>5s}  ticker")
        for s in fire_examples:
            edge = (s.get("nws_p_yes") or 0) - (s.get("market_p_yes") or s.get("yes_ask") or 0)
            side = "YES" if edge > 0 else "NO"
            fill = s.get("yes_ask") if side == "YES" else s.get("no_ask")
            print(f"    {(s.get('city_key') or '')[:9]:9s} {(s.get('strike_type') or '')[:8]:8s} "
                  f"{side:3s} {edge:+7.3f} {(s.get('yes_margin_f') or 0):+6.1f} "
                  f"{(s.get('nws_p_yes') or 0):6.3f} {(fill if fill is not None else 0):5.2f}  "
                  f"{s.get('market_ticker','')}")


def analyze_edges(since: datetime) -> None:
    """Edge distribution among NEAR-MISS markets (failed only edge_too_small) so
    we can see if a modest threshold change would responsibly add at-bats."""
    recs = _load(SIGNAL_LOG)
    p = _gate_params()
    postfix = [r for r in recs
               if r.get("strike_type") and (_dt(r.get("sample_at")) or datetime.min.replace(tzinfo=timezone.utc)) >= since]
    # one row per market: its max |edge| seen
    best: dict[str, float] = {}
    for s in postfix:
        nws_p = s.get("nws_p_yes")
        mp = s.get("market_p_yes") or s.get("yes_ask")
        if nws_p is None or mp is None:
            continue
        e = abs(nws_p - mp)
        t = s.get("market_ticker")
        if t and e > best.get(t, -1):
            best[t] = e
    if not best:
        print("\n[edges] no post-fix markets to profile.")
        return
    vals = sorted(best.values(), reverse=True)
    thr = p["min_edge_threshold"]
    print(f"\n{'='*68}\n[edges] per-market best |edge| distribution (post-fix)\n{'='*68}")
    print(f"  markets={len(vals)}  max={vals[0]:.3f}  median={statistics.median(vals):.3f}")
    for band in (0.30, 0.20, 0.15, 0.10, 0.08, 0.05):
        c = sum(1 for v in vals if v >= band)
        flag = "  <- current threshold" if abs(band - thr) < 1e-9 else ""
        print(f"    |edge| >= {band:.2f} : {c:4d} markets{flag}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Weather DAILY edge validation (read-only).")
    ap.add_argument("--since", default=DEFAULT_SINCE,
                    help=f"ISO ts; only signal records at/after this (default {DEFAULT_SINCE})")
    args = ap.parse_args()
    since = _dt(args.since) or _dt(DEFAULT_SINCE)
    print("Weather DAILY sleeve — POST-FIX validation (READ-ONLY).")
    print("NOTE: at-bats is a would-fire PROXY over the signal log (no Kelly/dup-open);")
    print("      WR is the live-faithful metric once settled post-fix trades exist.")
    analyze_paper(since)
    analyze_atbats(since)
    analyze_edges(since)


if __name__ == "__main__":
    main()
