#!/usr/bin/env python3
"""Build a TRIALS dataset from a recorded signal log + real settlement outcomes.

WHAT A "TRIAL" IS
-----------------
A trial is one scanned signal sample of a market that has since SETTLED on
Kalshi, enriched with:
  * the derived decision fields a gate looks at (signed edge, implied side,
    σ-normalized forecast margin, the price we'd actually pay to enter), and
  * the REAL outcome (Kalshi's yes/no settlement) plus the per-contract P&L we
    would have booked had we entered that side at that fill.

This file is deliberately GATE-AGNOSTIC. It does NOT decide what would trade —
it just attaches ground truth to every scan so `scripts/backtest_gates.py` can
replay arbitrary gate thresholds against real outcomes (and sweep them).

One trial per signal sample (a market is scanned many times). The gate
backtester collapses to one entry per market by taking the first sample that
passes its gates — faithful to how the live sleeve fires once.

OUTCOMES
--------
Ground truth is Kalshi's own resolution via the SAME helper live settlement
uses (`lib.weather_daily_paper._kalshi_market_result`): result ∈ {yes,no} on a
settled/finalized market, else None (still open → market excluded, no trial).
Distinct markets are fetched once and cached to --resolution-cache so reruns are
cheap; unsettled markets are NOT cached (retried next run).

USAGE
-----
  python scripts/build_trials.py --signal-log data/weather_daily_signal.jsonl \
      --sleeve weather_daily --out data/trials_daily.jsonl

Everything is READ-ONLY w.r.t. live state: it reads the signal log, hits public
Kalshi market endpoints, and writes ONLY the trials file + the resolution cache.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Reuse the live profit-fee constant so trial P&L == settlement P&L math.
from lib.weather_daily_paper import KALSHI_PROFIT_FEE, _kalshi_market_result

DEFAULT_CACHE = _ROOT / "data" / "_trial_resolution_cache.json"

# Sleeves this builder understands. weather_daily is the only one with a
# strike-type-aware signal log + per-market Kalshi settlement today; add others
# here as their signal schemas stabilize.
SUPPORTED_SLEEVES = ("weather_daily",)


def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _load_jsonl(path: Path) -> list[dict]:
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


def _load_cache(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        d = json.loads(path.read_text())
        return {k: v for k, v in d.items() if v in ("yes", "no")}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(path: Path, cache: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=0, sort_keys=True))


def _resolve_markets(tickers: list[str], cache: dict[str, str]) -> dict[str, str]:
    """Return {ticker: 'yes'|'no'} for every settled market in `tickers`,
    fetching only the ones not already cached. Unsettled markets are omitted
    (and left out of the cache so they retry next run)."""
    resolved = {t: cache[t] for t in tickers if t in cache}
    pending = [t for t in tickers if t not in cache]
    if pending:
        print(f"  fetching settlement for {len(pending)} uncached markets "
              f"({len(resolved)} already cached)...", flush=True)
    for i, t in enumerate(pending, 1):
        res = _kalshi_market_result(t)  # None if still open
        if res in ("yes", "no"):
            resolved[t] = res
            cache[t] = res
        if i % 25 == 0:
            print(f"    ...{i}/{len(pending)}", flush=True)
    return resolved


def _trial_from_sample(s: dict, result: str) -> dict | None:
    """Map one weather_daily signal sample + its real settlement to a trial.
    Returns None if the sample lacks the fields a gate/score needs."""
    nws_p = s.get("nws_p_yes")
    market_p = s.get("market_p_yes")
    if market_p is None:
        market_p = s.get("yes_ask")
    if nws_p is None or market_p is None:
        return None
    edge = float(nws_p) - float(market_p)          # signed: >0 favors YES
    side = "YES" if edge > 0 else "NO"

    ym = s.get("yes_margin_f")                      # signed °F; >0 favors YES
    sigma = s.get("sigma_f")
    margin_sigma = None
    if ym is not None and sigma not in (None, 0):
        margin_sigma = float(ym) / float(sigma)     # signed σ favoring YES

    # Entry price for the side we'd take (matches the live fill convention).
    yes_ask = s.get("yes_ask")
    no_ask = s.get("no_ask")
    if side == "YES":
        fill = yes_ask
    else:
        fill = no_ask
        if fill is None and yes_ask is not None:
            fill = round(1.0 - float(yes_ask), 4)
    if fill is None:
        return None
    fill = float(fill)

    # Real outcome → win/loss for the side we'd have taken, and per-contract P&L
    # using the SAME math as settle_paper_trades (1 contract, $1 payout).
    won = (side == "YES" and result == "yes") or (side == "NO" and result == "no")
    if won:
        gross = 1.0 - fill
        pnl = gross - max(0.0, gross * KALSHI_PROFIT_FEE)
    else:
        pnl = -fill

    return {
        "market_ticker": s.get("market_ticker"),
        "event_ticker": s.get("event_ticker"),
        "city_key": s.get("city_key"),
        "direction": s.get("direction"),
        "strike_type": s.get("strike_type"),
        "sample_at": s.get("sample_at"),
        "close_time": s.get("close_time"),
        "seconds_to_close": s.get("seconds_to_close"),
        "nws_p_yes": nws_p,
        "market_p_yes": market_p,
        "edge": round(edge, 6),
        "abs_edge": round(abs(edge), 6),
        "side": side,
        "yes_margin_f": ym,
        "sigma_f": sigma,
        "margin_sigma": round(margin_sigma, 6) if margin_sigma is not None else None,
        "yes_ask": yes_ask,
        "no_ask": no_ask,
        "fill": round(fill, 6),
        "result": result,
        "won": won,
        "pnl_per_contract": round(pnl, 6),
    }


def build(signal_log: Path, sleeve: str, out: Path, cache_path: Path,
          since: datetime | None) -> None:
    if sleeve not in SUPPORTED_SLEEVES:
        raise SystemExit(
            f"sleeve {sleeve!r} not supported (have: {', '.join(SUPPORTED_SLEEVES)})")
    samples = _load_jsonl(signal_log)
    if since is not None:
        samples = [s for s in samples
                   if (_dt(s.get("sample_at")) or datetime.min.replace(tzinfo=timezone.utc)) >= since]
    # strike_type marks the post-fix schema; pre-fix records are unscoreable.
    samples = [s for s in samples if s.get("strike_type") and s.get("market_ticker")]
    print(f"[build_trials] sleeve={sleeve}  post-fix samples={len(samples)}  "
          f"distinct markets={len({s['market_ticker'] for s in samples})}")
    if not samples:
        print("  nothing to build.")
        out.write_text("")
        return

    cache = _load_cache(cache_path)
    tickers = sorted({s["market_ticker"] for s in samples})
    resolved = _resolve_markets(tickers, cache)
    _save_cache(cache_path, cache)
    print(f"  settled markets: {len(resolved)} / {len(tickers)} "
          f"(unsettled excluded — no outcome yet)")

    trials, skipped = [], 0
    for s in samples:
        res = resolved.get(s["market_ticker"])
        if res is None:
            continue  # market not settled → no ground truth
        t = _trial_from_sample(s, res)
        if t is None:
            skipped += 1
            continue
        trials.append(t)

    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as f:
        for t in trials:
            f.write(json.dumps(t) + "\n")

    n_mkts = len({t["market_ticker"] for t in trials})
    wins = sum(1 for t in trials if t["won"])
    print(f"  wrote {len(trials)} trials over {n_mkts} settled markets "
          f"-> {out}")
    if trials:
        print(f"  (sample-level sanity: {wins}/{len(trials)} would-win at as-scanned "
              f"side; {skipped} samples skipped for missing fields)")


def main() -> None:
    ap = argparse.ArgumentParser(description="Build a trials dataset from a signal log.")
    ap.add_argument("--signal-log", required=True, type=Path)
    ap.add_argument("--sleeve", required=True, choices=SUPPORTED_SLEEVES)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--resolution-cache", type=Path, default=DEFAULT_CACHE,
                    help=f"settled-market cache (default {DEFAULT_CACHE})")
    ap.add_argument("--since", default=None,
                    help="ISO ts; only samples sampled at/after this")
    args = ap.parse_args()
    build(args.signal_log, args.sleeve, args.out, args.resolution_cache,
          _dt(args.since))


if __name__ == "__main__":
    main()
