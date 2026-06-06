#!/usr/bin/env python3
"""
build_trials.py — turn a signal ledger into a backtester "trials" file by
joining each market to its ACTUAL Kalshi settlement result.

This is the "ledgers first" data adapter for backtest_gates.py. It reads one of
the sleeves' signal JSONLs (which sample EVERY live market each cycle — so we
get markets we *didn't* trade too, minimal survivorship bias), picks one
decision-point sample per market, fetches the real Kalshi `result` (public
GET /markets/{ticker} — no auth), and writes trials rows in the schema
backtest_gates.py expects.

Per sleeve, the field mapping is:
  weather_daily  : fair_yes=nws_p_yes, forecast_f, strike_f, sigma_f
  weather_hourly : fair_yes=nws_p_yes, forecast_f=nws_forecast_f, strike_f,
                   sigma_f=blend_meta.sigma_f
  crypto_15min   : fair_yes=indicators.theoretical_yes   (no °F margin fields)

Decision point: for each market_ticker we keep the single sample whose
seconds_to_close is closest to --decision-lead (default 3600s), among samples
that were still open (seconds_to_close > 0). Re-run with a different
--decision-lead to study entry timing.

NETWORK: fetches api.elections.kalshi.com (public). Run on the host. Results
are cached per ticker so N samples of one market cost one request.

Usage:
  python scripts/build_trials.py --signal-log data/weather_daily_signal.jsonl \
      --sleeve weather_daily --out data/trials_weather_daily.jsonl
  python scripts/backtest_gates.py data/trials_weather_daily.jsonl --sweep min_margin_sigma
"""

from __future__ import annotations

import argparse
import json
import time

KALSHI_HOST = "https://api.elections.kalshi.com/trade-api/v2"


def _f(d: dict, *keys):
    """First present key, coerced to float; None if absent/unparseable.
    Supports dotted paths like 'blend_meta.sigma_f' and 'indicators.theoretical_yes'."""
    for key in keys:
        cur = d
        ok = True
        for part in key.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok and cur is not None:
            try:
                return float(cur)
            except (TypeError, ValueError):
                pass
    return None


def map_row(row: dict, sleeve: str) -> dict | None:
    """Project a signal-ledger row to the trial fields for its sleeve.
    Returns None if the essential fields are missing."""
    tk = row.get("market_ticker")
    if not tk:
        return None
    out = {
        "market_ticker": tk,
        "yes_ask": _f(row, "yes_ask"),
        "no_ask": _f(row, "no_ask"),
        "seconds_to_close": _f(row, "seconds_to_close"),
        "close_time": row.get("close_time", ""),
    }
    if sleeve == "weather_daily":
        out["fair_yes"] = _f(row, "nws_p_yes")
        out["forecast_f"] = _f(row, "forecast_f")
        out["strike_f"] = _f(row, "strike_f")
        out["sigma_f"] = _f(row, "sigma_f")
    elif sleeve == "weather_hourly":
        out["fair_yes"] = _f(row, "nws_p_yes")
        out["forecast_f"] = _f(row, "nws_forecast_f", "forecast_f")
        out["strike_f"] = _f(row, "strike_f")
        out["sigma_f"] = _f(row, "blend_meta.sigma_f", "sigma_f")
    elif sleeve == "crypto_15min":
        out["fair_yes"] = _f(row, "indicators.theoretical_yes")
    else:
        raise ValueError(f"unknown sleeve {sleeve!r}")
    if out["fair_yes"] is None:
        return None
    return out


def pick_decision_samples(rows: list[dict], sleeve: str, decision_lead: float) -> dict[str, dict]:
    """One trial per market_ticker: the open sample whose seconds_to_close is
    closest to decision_lead."""
    best: dict[str, tuple[float, dict]] = {}
    for r in rows:
        t = map_row(r, sleeve)
        if t is None:
            continue
        stc = t.get("seconds_to_close")
        if stc is None or stc <= 0:
            continue
        d = abs(stc - decision_lead)
        if t["market_ticker"] not in best or d < best[t["market_ticker"]][0]:
            best[t["market_ticker"]] = (d, t)
    return {tk: v[1] for tk, v in best.items()}


def fetch_result(ticker: str, *, timeout: int = 10) -> str | None:
    """Actual Kalshi settlement: 'yes' | 'no' | 'void' | None (still open / error)."""
    import requests
    try:
        r = requests.get(f"{KALSHI_HOST}/markets/{ticker}", timeout=timeout)
        r.raise_for_status()
        data = r.json()
    except Exception:
        return None
    m = data.get("market") if isinstance(data, dict) else None
    if not isinstance(m, dict):
        return None
    res = str(m.get("result") or "").lower()
    if res in ("yes", "no"):
        return res
    if res in ("void", "voided"):
        return "void"
    return None  # "" → still open


def build(signal_log: str, sleeve: str, decision_lead: float,
          result_fn=fetch_result, sleep: float = 0.0) -> list[dict]:
    rows = []
    with open(signal_log) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    samples = pick_decision_samples(rows, sleeve, decision_lead)
    trials = []
    n_open = n_err = 0
    for tk, t in samples.items():
        res = result_fn(tk)
        if res is None:
            n_open += 1
            continue
        t["result"] = res
        trials.append(t)
        if sleep:
            time.sleep(sleep)
    return trials, {"markets": len(samples), "settled": len(trials), "unresolved": n_open}


def main() -> None:
    ap = argparse.ArgumentParser(description="Build a backtester trials file from a signal ledger")
    ap.add_argument("--signal-log", required=True)
    ap.add_argument("--sleeve", required=True,
                    choices=["weather_daily", "weather_hourly", "crypto_15min"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--decision-lead", type=float, default=3600.0,
                    help="seconds-to-close of the sample to treat as the decision point")
    ap.add_argument("--sleep", type=float, default=0.1, help="pause between Kalshi calls")
    args = ap.parse_args()

    trials, stats = build(args.signal_log, args.sleeve, args.decision_lead, sleep=args.sleep)
    with open(args.out, "w") as f:
        for t in trials:
            f.write(json.dumps(t) + "\n")
    print(f"{stats['markets']} markets → {stats['settled']} settled trials "
          f"({stats['unresolved']} still open/unresolved) → {args.out}")
    print(f"next: python scripts/backtest_gates.py {args.out} --sweep min_margin_sigma")


if __name__ == "__main__":
    main()
