#!/usr/bin/env python3
"""Trend-aware weather edge finder (READ-ONLY).

WHY THIS EXISTS
---------------
The live hourly weather signal anchors P(YES) to the CURRENT observed temp.
When temperature is moving (e.g. falling -2F/hr near close), that anchor lags:
it shows a big "edge" on a market that will actually resolve the other way as
the temp crosses the strike. That obs-anchor trap caused real live losses
(2026-06-01, two NYC NO bets that settled the wrong side) and also FAKE edges
like the +0.51 YES we caught at 03:04 (NYC 57.9F falling through a 57 strike →
market correctly priced 4% YES, model said 55%).

The signal module already computes a per-market `shadow_trendaware` block that
projects the observed temp ALONG ITS TREND to close and recomputes P(YES) from
that trajectory. This script ranks markets by the TREND-AWARE edge (not the
obs-anchored one) and surfaces only setups that are trustworthy AND actionable:

  * trend_confirms == True   — the obs trajectory agrees with the directional
                               call (so we're not fighting a moving temp)
  * |trend_edge| >= --min-edge
  * fillable: the side we'd take has an ask in [--fill-floor, --fill-ceil]
  * sized so payoff asymmetry is favorable (cheap side preferred)

It compares the trend-aware view to the LIVE obs-anchored view so you can SEE
which markets the anchor would have mis-signed. Nothing is written, no order
can fire — pure scan + rank.

USAGE
  python -m scripts.find_weather_edges
  python -m scripts.find_weather_edges --min-edge 0.10 --fill-ceil 0.45
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _norm_cdf(x: float) -> float:
    import math
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _trend_edge_row(s: dict) -> dict | None:
    """Compute the trend-aware edge for one signal sample. Returns a dict with
    the chosen side + edge, or None if the sample lacks trend data."""
    sh = s.get("shadow_trendaware") or {}
    p_yes_trend = sh.get("p_yes")
    if p_yes_trend is None:
        return None
    yes_ask = s.get("yes_ask")
    no_ask = s.get("no_ask")
    if no_ask is None and yes_ask is not None:
        no_ask = round(1.0 - float(yes_ask), 4)
    # Edge per side: model P minus what we'd PAY for that side.
    yes_edge = (float(p_yes_trend) - float(yes_ask)) if yes_ask is not None else None
    no_edge = ((1.0 - float(p_yes_trend)) - float(no_ask)) if no_ask is not None else None
    # Pick the side with the larger positive edge.
    best_side, best_edge, best_fill = None, -9.9, None
    if yes_edge is not None and yes_edge > best_edge:
        best_side, best_edge, best_fill = "YES", yes_edge, yes_ask
    if no_edge is not None and no_edge > best_edge:
        best_side, best_edge, best_fill = "NO", no_edge, no_ask
    # Cushion: how far (in sigma) the trend-projected temp clears the strike on
    # the side we're taking. For NO (YES=above strike), NO wins when temp stays
    # BELOW strike, so cushion = (strike - projected)/sigma. For YES, the
    # reverse. Positive cushion = projected outcome is on our side with room.
    point = sh.get("point_f")
    sigma = sh.get("sigma_f")
    strike = s.get("strike_f")
    cushion_sigma = None
    if point is not None and sigma and strike is not None and best_side:
        if best_side == "NO":
            cushion_sigma = (float(strike) - float(point)) / float(sigma)
        else:  # YES wins when temp is ABOVE strike
            cushion_sigma = (float(point) - float(strike)) / float(sigma)
    return {
        "ticker": s.get("market_ticker", ""),
        "city": s.get("city_key", s.get("city", "?")),
        "strike_f": s.get("strike_f"),
        "secs_to_close": s.get("seconds_to_close") or 0,
        "obs_f": s.get("current_obs_f"),
        "obs_trend_f_per_hr": sh.get("obs_trend_f_per_hr"),
        "lead_h": s.get("forecast_lead_hours"),
        "trend_point_f": sh.get("point_f"),
        "sigma_f": sigma,
        "cushion_sigma": cushion_sigma,
        "p_yes_trend": float(p_yes_trend),
        "p_yes_live": s.get("nws_p_yes"),
        "trend_confirms": bool(sh.get("trend_confirms")),
        "side": best_side,
        "trend_edge": best_edge,
        "fill": best_fill,
        # The obs-anchored edge the LIVE bot currently sees on the same side,
        # so we can show where the anchor disagrees with the trend view.
        "live_edge_same_side": (
            (float(s.get("nws_p_yes")) - float(best_fill)) if best_side == "YES" and best_fill is not None
            else ((1.0 - float(s.get("nws_p_yes"))) - float(best_fill)) if best_side == "NO" and best_fill is not None
            else None) if s.get("nws_p_yes") is not None else None,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Trend-aware weather edge finder (read-only).")
    ap.add_argument("--profile", choices=["any", "no-cheap"], default="no-cheap",
                    help="'no-cheap' (default) = hunt ONLY the proven live winner shape: "
                         "NO side, cheap fill, trend keeps temp clear of the strike by a "
                         "real margin (6-9x asymmetric payout). 'any' = both sides, the "
                         "generic trend-aware scan.")
    ap.add_argument("--min-edge", type=float, default=0.08,
                    help="minimum trend-aware edge to surface (default 0.08)")
    ap.add_argument("--fill-floor", type=float, default=0.05,
                    help="reject fills below this (longshot junk) (default 0.05)")
    ap.add_argument("--fill-ceil", type=float, default=0.55,
                    help="reject fills above this (poor payoff asymmetry) (default 0.55)")
    ap.add_argument("--min-cushion-sigma", type=float, default=0.75,
                    help="no-cheap: require the projected temp to clear the strike by at "
                         "least this many sigma (default 0.75 — the winners had real room)")
    ap.add_argument("--require-trend-confirm", action="store_true", default=True,
                    help="only surface markets whose obs trend confirms the side (default on)")
    ap.add_argument("--all", action="store_true",
                    help="show ALL rows incl. rejected, with the reason")
    args = ap.parse_args()

    # The proven-winner profile (from 9 live wins, 8 of them NO @ 0.10-0.15,
    # 6-9x payout): NO side, cheap fill, and the projected temp comfortably
    # AWAY from the strike (not a near-money coin flip). 'no-cheap' encodes it.
    if args.profile == "no-cheap":
        args.fill_ceil = min(args.fill_ceil, 0.18)   # cheap NO only
        args.fill_floor = max(args.fill_floor, 0.05)

    from lib.weather_signal import run_signal_cycle
    res = run_signal_cycle(record_paper_trades=False, settle_paper_trades=False)
    samples = res.get("samples") or []

    rows = []
    for s in samples:
        r = _trend_edge_row(s)
        if r is None:
            continue
        # Classify
        reason = "FIRE"
        if r["side"] is None or r["fill"] is None:
            reason = "no_side"
        elif args.profile == "no-cheap" and r["side"] != "NO":
            reason = "not_no_side"          # winner profile is NO-only
        elif r["trend_edge"] < args.min_edge:
            reason = "edge_too_small"
        elif not (args.fill_floor <= float(r["fill"]) <= args.fill_ceil):
            reason = "fill_oob"
        elif args.require_trend_confirm and not r["trend_confirms"]:
            reason = "trend_not_confirmed"
        elif (args.profile == "no-cheap"
              and (r["cushion_sigma"] is None
                   or r["cushion_sigma"] < args.min_cushion_sigma)):
            reason = "thin_cushion"          # near-money coin flip — not the winner shape
        r["reason"] = reason
        rows.append(r)

    fire = [r for r in rows if r["reason"] == "FIRE"]
    fire.sort(key=lambda r: -r["trend_edge"])

    print("Trend-aware weather edge finder — READ-ONLY (no orders, no writes).")
    print(f"scanned {len(samples)} markets · {len(fire)} trustworthy edge(s) "
          f"(min_edge={args.min_edge}, fill∈[{args.fill_floor},{args.fill_ceil}], "
          f"trend_confirm={args.require_trend_confirm})\n")

    def _fmt(rws, label):
        if not rws:
            print(f"  ({label}: none)")
            return
        print(f"  {label}:")
        print(f"    {'city':9s} {'side':4s} {'strike':>7s} {'obs':>6s} {'trend/h':>8s} "
              f"{'→close':>7s} {'cush_σ':>7s} {'fill':>5s} {'payout':>7s} "
              f"{'edge_tr':>8s} {'close_in':>8s}  ticker")
        for r in rws:
            mins = int((r["secs_to_close"] or 0) // 60)
            proj = (r["obs_f"] or 0) + (r["obs_trend_f_per_hr"] or 0) * (r["lead_h"] or 0)
            fill = float(r["fill"] or 0)
            payout = ((1 - fill) / fill) if 0 < fill < 1 else 0.0
            print(f"    {str(r['city'])[:9]:9s} {str(r['side']):4s} "
                  f"{(r['strike_f'] or 0):7.1f} {(r['obs_f'] or 0):6.1f} "
                  f"{(r['obs_trend_f_per_hr'] or 0):+8.2f} {proj:7.1f} "
                  f"{(r['cushion_sigma'] if r['cushion_sigma'] is not None else 0):+7.2f} "
                  f"{fill:5.2f} {payout:6.1f}x {r['trend_edge']:+8.3f} "
                  f"{(str(mins)+'m'):>8s}  {r['ticker'][:26]}")

    _fmt(fire, "TRUSTWORTHY EDGES (trend-confirmed, fillable)")

    if args.all:
        print()
        from collections import Counter
        c = Counter(r["reason"] for r in rows)
        print("  disposition:", dict(c))
        rej = [r for r in rows if r["reason"] != "FIRE" and r["side"]]
        rej.sort(key=lambda r: -(r["trend_edge"] or 0))
        _fmt(rej[:12], "REJECTED (top by raw edge — why each was filtered shown via disposition)")

    # Honesty note: flag any market where the LIVE obs-anchored edge is big but
    # the trend view kills it — those are the traps this finder exists to avoid.
    traps = [r for r in rows
             if r["live_edge_same_side"] is not None and r["live_edge_same_side"] > 0.20
             and r["reason"] != "FIRE"]
    if traps:
        print(f"\n  ⚠️  {len(traps)} obs-anchor TRAP(s) avoided — live sees a big edge the "
              f"trend view rejects (temp moving through the strike):")
        for r in sorted(traps, key=lambda x: -(x['live_edge_same_side'] or 0))[:6]:
            print(f"     {r['city']} {r['side']} {r['ticker'][:24]}: "
                  f"live_edge {r['live_edge_same_side']:+.2f} but {r['reason']} "
                  f"(obs {r['obs_f']:.1f} trend {r['obs_trend_f_per_hr']:+.1f}/h)")


if __name__ == "__main__":
    main()
