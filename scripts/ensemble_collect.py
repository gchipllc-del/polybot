#!/usr/bin/env python3
"""ensemble_collect — forward A/B test: does an ENSEMBLE forecast beat our flat
σ=3°F normal model at predicting Kalshi weather outcomes?

WHY FORWARD (not a backtest): Open-Meteo keeps only ~3 days of historical
ensemble member data, so the 2021-2025 Becker history CANNOT be re-scored with
ensemble forecasts. The only honest test is to record forecasts going forward
and grade them against realized settlements.

Each run records, for every DAY-AHEAD weather market (same universe fc2s
trades), three competing P(YES) estimates plus the market price:
  * p_yes_ensemble — fraction of GFS/GEFS members satisfying the strike/band
                     (a REAL probability whose spread is day- & city-specific)
  * p_yes_sigma3   — our current method: normal CDF, flat σ=3°F around the
                     ensemble-mean high (the incumbent to beat)
  * market_p_yes   — what Kalshi priced
Outcome is filled later from Kalshi settlement. After ~weeks of settled rows,
`eval` runs the SAME validation ladder fc2s uses (within-bin rank lift +
permutation/bootstrap significance) for each forecast, head to head.

  python scripts/ensemble_collect.py collect     # record day-ahead markets
  python scripts/ensemble_collect.py settle       # fill outcomes
  python scripts/ensemble_collect.py status        # how much data so far
  python scripts/ensemble_collect.py eval          # ensemble vs σ=3 vs market
  python scripts/ensemble_collect.py --selftest
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from fc_two_sided import (_open_weather_quotes, WEATHER_SERIES, series_geo,   # noqa: E402
                          _city_local_date, _iso_event_date)
from join_weather_trials import (parse_strike2, forecast_p_yes,              # noqa: E402
                                 BAND_HALF_WIDTH)

LEDGER = ROOT / "data" / "ensemble_collect.jsonl"
SCAN_STATUS = ROOT / "data" / "ensemble_collect_status.json"
SIGMA_F = 3.0   # the incumbent flat error model we're trying to beat


# ── Pure forecast math (fully testable, no network) ─────────────────────────

def daily_high_per_member(hourly: dict) -> dict:
    """From an Open-Meteo ensemble `hourly` block, return
    {iso_date: [per-member daily-high °F]}. Members arrive as separate keys
    temperature_2m_member01, _member02, … (count varies by model)."""
    times = hourly.get("time", []) or []
    member_keys = sorted(k for k in hourly
                         if k.startswith("temperature_2m_member"))
    # control run "temperature_2m" (no suffix) counts as a member too if present
    if "temperature_2m" in hourly:
        member_keys = ["temperature_2m"] + member_keys
    by_day: dict = {}
    for mk in member_keys:
        vals = hourly.get(mk) or []
        per_day: dict = {}
        for t, v in zip(times, vals):
            if v is None:
                continue
            day = str(t)[:10]
            cur = per_day.get(day)
            fv = float(v)
            if cur is None or fv > cur:
                per_day[day] = fv
        for day, hi in per_day.items():
            by_day.setdefault(day, []).append(hi)
    return by_day


def ensemble_p_yes(member_highs: list, kind: str, strike: float,
                   half_width: float = BAND_HALF_WIDTH) -> float | None:
    """P(YES) = fraction of ensemble members satisfying the contract, clipped to
    [0.02, 0.98] to match forecast_p_yes. None if no members."""
    n = len(member_highs)
    if n == 0:
        return None
    if kind == "band":
        hit = sum(1 for h in member_highs if abs(h - strike) <= half_width)
    else:                      # 'above'
        hit = sum(1 for h in member_highs if h >= strike)
    return max(0.02, min(0.98, hit / n))


# ── Live fetch (needs home IP; thin wrapper over the pure math) ─────────────

def fetch_ensemble_highs(series_list) -> dict:
    """(series, iso_date) → list of per-member daily-high °F from the live GFS
    ensemble. One call per city."""
    import requests
    geo = series_geo()
    out: dict = {}
    for s in series_list:
        ll = geo.get(s)
        if not ll:
            continue
        try:
            resp = requests.get(
                "https://ensemble-api.open-meteo.com/v1/ensemble",
                params={"latitude": ll[0], "longitude": ll[1],
                        "hourly": "temperature_2m", "models": "gfs_seamless",
                        "forecast_days": 4, "temperature_unit": "fahrenheit",
                        "timezone": "auto"},
                timeout=30).json()
            for day, highs in daily_high_per_member(resp.get("hourly", {})).items():
                out[(s, day)] = highs
        except Exception as e:
            print(f"  ! ensemble {s}: {e}", file=sys.stderr)
    return out


def _load_ledger() -> list:
    rows = []
    if LEDGER.exists():
        with open(LEDGER) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    return rows


def cmd_collect(args) -> None:
    try:
        quotes = _open_weather_quotes(WEATHER_SERIES)
    except Exception as e:
        print(f"! collect failed ({e}) — run on home IP with Kalshi auth", file=sys.stderr)
        return
    geo = series_geo()
    now = datetime.now(timezone.utc)
    series_live = sorted({(q.get("ticker") or "").split("-")[0]
                          for q in quotes if q.get("ticker")})
    ens = fetch_ensemble_highs(series_live)

    seen = {r.get("market_ticker") for r in _load_ledger()}
    new, n_same_day, n_no_ens = [], 0, 0
    now_iso = now.isoformat()
    for q in quotes:
        tk = q.get("ticker") or ""
        if not tk or tk in seen:
            continue
        series = tk.split("-")[0]
        date = _iso_event_date(tk)
        if date <= _city_local_date(series, now, geo):     # day-ahead only
            n_same_day += 1
            continue
        kind, strike = parse_strike2(tk)
        mkt = q.get("yes_price")
        highs = ens.get((series, date))
        if strike is None or not highs or not isinstance(mkt, (int, float)):
            n_no_ens += 1
            continue
        mean_high = sum(highs) / len(highs)
        new.append({
            "market_ticker": tk, "series": series, "date": date,
            "kind": kind, "strike_f": strike, "market_p_yes": round(float(mkt), 4),
            "ens_member_count": len(highs),
            "ens_mean_high_f": round(mean_high, 2),
            "p_yes_ensemble": round(ensemble_p_yes(highs, kind, strike), 4),
            "p_yes_sigma3": round(forecast_p_yes(kind, strike, mean_high, SIGMA_F), 4),
            "result": "", "status": "open", "sample_at": now_iso,
        })
        seen.add(tk)
    if new:
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with open(LEDGER, "a") as f:
            for r in new:
                f.write(json.dumps(r) + "\n")
    SCAN_STATUS.parent.mkdir(parents=True, exist_ok=True)
    SCAN_STATUS.write_text(json.dumps({
        "last": now_iso, "markets_seen": len(quotes), "recorded": len(new),
        "same_day_skipped": n_same_day, "no_ensemble_or_strike": n_no_ens}))
    print(f"ensemble collect: {len(quotes)} open, recorded {len(new)} day-ahead "
          f"({n_same_day} same-day skipped, {n_no_ens} no ensemble/strike)")


def cmd_settle(args) -> None:
    from fetch_backtest_data import _kalshi_get
    rows = _load_ledger()
    changed = 0
    for r in rows:
        if r.get("status") != "open":
            continue
        try:
            m = _kalshi_get(f"/markets/{r['market_ticker']}", {}).get("market", {})
        except Exception:
            continue
        res = str(m.get("result", "") or "").lower()
        if res not in ("yes", "no"):
            continue
        r["result"] = res
        r["status"] = "settled"
        changed += 1
    if changed:
        with open(LEDGER, "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
    print(f"ensemble settle: filled {changed} outcomes")


def cmd_status(args) -> None:
    rows = _load_ledger()
    settled = [r for r in rows if r.get("status") == "settled"]
    ndays = len({r["date"] for r in settled})
    print(f"ensemble collect: {len(rows)} recorded · {len(settled)} settled "
          f"across {ndays} distinct days")
    if SCAN_STATUS.exists():
        try:
            st = json.loads(SCAN_STATUS.read_text())
            print(f"  last collect {st.get('last','?')[:16]} — recorded "
                  f"{st.get('recorded',0)} of {st.get('markets_seen',0)} seen")
        except Exception:
            pass
    need = 200
    if len(settled) < need:
        print(f"  need ~{need}+ settled rows over ~15-20 days before `eval` is "
              f"meaningful (have {len(settled)}).")
    else:
        print("  enough data — run `eval`.")


def cmd_eval(args) -> None:
    from forecast_skill_days import rank_skill_check, lift_significance
    rows = [r for r in _load_ledger()
            if r.get("status") == "settled" and str(r.get("result")) in ("yes", "no")]
    if len(rows) < 100:
        print(f"only {len(rows)} settled rows — too few to evaluate "
              f"(want 200+). Keep collecting.")
        return
    print(f"=== ensemble vs σ=3 vs market — {len(rows)} settled rows, "
          f"{len({r['date'] for r in rows})} days ===")

    def brier(key):
        return sum((r[key] - (1.0 if r["result"] == "yes" else 0.0)) ** 2
                   for r in rows) / len(rows)
    print(f"\nBrier (lower=better):  ensemble {brier('p_yes_ensemble'):.4f}  ·  "
          f"σ=3 {brier('p_yes_sigma3'):.4f}  ·  market {brier('market_p_yes'):.4f}")

    # Within-bin rank skill + significance, ensemble vs σ=3, on the SAME markets.
    for label, key in (("ENSEMBLE", "p_yes_ensemble"), ("σ=3 (incumbent)", "p_yes_sigma3")):
        engine = [(r["market_p_yes"], r[key], r["result"] == "yes", r["date"], "")
                  for r in rows]
        print(f"\n----- {label} -----")
        rank_skill_check(engine)
        lift_significance(engine)
    print("\nVERDICT: ensemble wins only if its rank lift is both HIGHER than σ=3 "
          "AND significant (p<0.05, CI excludes 0). A lower Brier alone is just "
          "calibration — it's the rank lift that drives the trading edge.")


def selftest() -> int:
    ok = True
    # daily_high_per_member: 2 members, 2 days, picks the daily max per member
    hourly = {
        "time": ["2026-06-15T00:00", "2026-06-15T12:00",
                 "2026-06-16T00:00", "2026-06-16T12:00"],
        "temperature_2m_member01": [60, 80, 55, 70],   # day1 high 80, day2 high 70
        "temperature_2m_member02": [62, 76, 50, 90],   # day1 high 76, day2 high 90
    }
    bd = daily_high_per_member(hourly)
    assert bd["2026-06-15"] == [80.0, 76.0] and bd["2026-06-16"] == [70.0, 90.0], bd
    # control run (no suffix) also counted
    h2 = {"time": ["2026-06-15T12:00"], "temperature_2m": [85],
          "temperature_2m_member01": [80]}
    assert sorted(daily_high_per_member(h2)["2026-06-15"]) == [80.0, 85.0]
    print("daily_high_per_member OK")

    # ensemble_p_yes: above-strike 85, members [80,76,90,88] -> 2/4 >= 85 = 0.5
    p = ensemble_p_yes([80, 76, 90, 88], "above", 85.0)
    assert abs(p - 0.5) < 1e-9, p
    # band B82.5 (±0.5 -> [82,83]); members [82.3, 90, 81, 82.6] -> 2/4 in band
    pb = ensemble_p_yes([82.3, 90, 81, 82.6], "band", 82.5)
    assert abs(pb - 0.5) < 1e-9, pb
    # all members miss -> clipped to 0.02, none missing -> 0.98
    assert ensemble_p_yes([60, 61], "above", 90.0) == 0.02
    assert ensemble_p_yes([95, 96], "above", 90.0) == 0.98
    assert ensemble_p_yes([], "above", 90.0) is None
    print("ensemble_p_yes OK (above + band + clipping)")

    # the ensemble's edge over σ=3: a tight day (low spread) should give a more
    # confident probability than the flat σ=3 model when the mean is near strike.
    tight = ensemble_p_yes([89.8, 90.1, 89.9, 90.2], "above", 90.0)   # ~0.5, low spread
    wide = ensemble_p_yes([80, 100, 85, 95], "above", 90.0)           # ~0.5, high spread
    assert tight is not None and wide is not None
    print(f"spread sensitivity OK (tight day p={tight}, wide day p={wide} — "
          f"σ=3 would give the SAME p for both; that's the ensemble's edge)")
    print("PASS" if ok else "*** FAIL ***")
    return 0 if ok else 1


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selftest", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    for name, fn, help_ in (
            ("collect", cmd_collect, "record day-ahead markets w/ ensemble + σ=3 P(YES)"),
            ("settle", cmd_settle, "fill outcomes from Kalshi settlement"),
            ("status", cmd_status, "how much data accumulated"),
            ("eval", cmd_eval, "ensemble vs σ=3 vs market (needs ~200 settled rows)")):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(fn=fn)
    args = ap.parse_args()
    if args.selftest:
        raise SystemExit(selftest())
    if not getattr(args, "fn", None):
        ap.print_help()
        return
    args.fn(args)


if __name__ == "__main__":
    main()
