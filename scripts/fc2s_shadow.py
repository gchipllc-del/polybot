#!/usr/bin/env python3
"""fc2s_shadow — measurement-ONLY collector that re-enables tail recalibration for fc2s.

fc2s vetoes ABOVE-strike (tail) trades (TRADE_ABOVE_STRIKES=False) because the σ=3
forecast model claimed ~73% exceedance and realized ~18% — overconfident on the tail.
But the veto also stops collecting the data needed to ever fix it. This sleeve closes
that gap WITHOUT trading: it logs the day-ahead forecast high for every city-day, then
after the day fetches the realized high, so the (forecast, realized) error distribution
accrues — exactly what's needed to recalibrate σ and the high-bias and re-enable the tail.

It never reads the order book, never books a trade, never touches fc2s_paper.jsonl. Pure
forecast-vs-reality measurement, its own ledger (data/fc2s_shadow.jsonl).

  python scripts/fc2s_shadow.py collect    # capture day-ahead forecast highs (run daily)
  python scripts/fc2s_shadow.py settle      # fill realized highs for past dates
  python scripts/fc2s_shadow.py report      # measured bias/σ + exceedance calibration table
  python scripts/fc2s_shadow.py selftest

Why it answers the veto: report() shows, per strike-offset o (strike = forecast + o), the
model's predicted exceedance P(realized>strike) under σ=3 vs the REALIZED rate — and a
RECALIBRATED prediction using the measured bias+σ. When recal≈realized, the tail can be
re-enabled with those params. (No money rides on this; it's the evidence the veto waits on.)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from math import sqrt
from pathlib import Path
from statistics import NormalDist

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from fc_two_sided import series_geo, _city_local_date, SIGMA_F   # noqa: E402

LEDGER = ROOT / "data" / "fc2s_shadow.jsonl"
_N = NormalDist()
OFFSETS = (-6.0, -4.0, -2.0, 0.0, 2.0, 4.0, 6.0)   # strike − forecast, °F (neg = forecast above strike)


# ── pure stats (testable, no network) ────────────────────────────────────────

def error_stats(pairs: list) -> dict:
    """pairs = [(forecast_high, realized_high), …] → measured forecast-error stats.
    error = forecast − realized (positive = forecast ran HOT, the suspected tail bias)."""
    errs = [f - r for f, r in pairs]
    n = len(errs)
    if n == 0:
        return {"n": 0, "bias": None, "sigma": None, "mae": None}
    bias = sum(errs) / n
    var = sum((e - bias) ** 2 for e in errs) / (n - 1) if n > 1 else 0.0
    return {"n": n, "bias": round(bias, 3), "sigma": round(sqrt(var), 3),
            "mae": round(sum(abs(e) for e in errs) / n, 3)}


def exceedance_table(pairs: list, offsets=OFFSETS, model_sigma: float = SIGMA_F,
                     recal_bias: float | None = None, recal_sigma: float | None = None) -> list:
    """For each offset o (strike = forecast + o), compare three exceedance probabilities
    P(realized > strike):
      • model   — the live σ=3, zero-bias normal the veto distrusts: 1−Φ(o/σ)
      • recal   — using measured bias b and σ̂ (realized ~ N(forecast−b, σ̂)): 1−Φ((o+b)/σ̂)
      • realized— the empirical rate: mean[ (realized − forecast) > o ]
    Returns [(o, n, p_model, p_recal|None, p_realized)]. The veto is justified where
    p_model ≫ p_realized; it can be lifted where p_recal ≈ p_realized."""
    diffs = [r - f for f, r in pairs]          # realized − forecast
    n = len(diffs)
    rows = []
    for o in offsets:
        p_model = 1.0 - _N.cdf(o / model_sigma) if model_sigma > 0 else float("nan")
        p_recal = None
        if recal_bias is not None and recal_sigma and recal_sigma > 0:
            p_recal = 1.0 - _N.cdf((o + recal_bias) / recal_sigma)
        p_real = (sum(1 for d in diffs if d > o) / n) if n else None
        rows.append((o, n, round(p_model, 3),
                     (round(p_recal, 3) if p_recal is not None else None),
                     (round(p_real, 3) if p_real is not None else None)))
    return rows


# ── ledger I/O ───────────────────────────────────────────────────────────────

def _load() -> list:
    if not LEDGER.exists():
        return []
    out = []
    for line in LEDGER.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def _rewrite(rows: list) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


# ── forecast / realized fetch (needs network) ────────────────────────────────

def _fetch_highs(series_list, *, past_days: int = 0, forecast_days: int = 4) -> dict:
    """(series, iso_date) → daily high °F = max of hourly temperature_2m, the SAME
    definition fc2s's forecast uses (so forecast and realized are apples-to-apples).
    Open-Meteo forecast endpoint; past_days pulls recent realized highs."""
    import requests
    geo = series_geo()
    out = {}
    for s in series_list:
        ll = geo.get(s)
        if not ll:
            continue
        try:
            resp = requests.get(
                "https://api.open-meteo.com/v1/forecast",
                params={"latitude": ll[0], "longitude": ll[1], "hourly": "temperature_2m",
                        "past_days": past_days, "forecast_days": forecast_days,
                        "temperature_unit": "fahrenheit", "timezone": "auto"},
                timeout=20).json()
            h = resp.get("hourly", {})
            by_day: dict = {}
            for t, v in zip(h.get("time", []), h.get("temperature_2m", [])):
                if v is not None:
                    by_day.setdefault(str(t)[:10], []).append(float(v))
            for day, vals in by_day.items():
                out[(s, day)] = round(max(vals), 1)
        except Exception as e:
            print(f"  ! {s}: {e}", file=sys.stderr)
    return out


def cmd_collect(args) -> None:
    geo = series_geo()
    now = datetime.now(timezone.utc)
    fc = _fetch_highs(list(geo), forecast_days=4)
    rows = _load()
    seen = {(r["series"], r["date"]) for r in rows}
    new = []
    for (s, date), high in fc.items():
        # DAY-AHEAD only: capture the forecast for a date not yet begun at the city,
        # so the pair is a genuine forecast (not a nowcast of an already-realized high).
        if date <= _city_local_date(s, now, geo):
            continue
        if (s, date) in seen:
            continue
        new.append({"series": s, "date": date, "forecast_high_f": high,
                    "captured_at": now.isoformat(), "realized_high_f": None, "error_f": None})
    if new:
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with LEDGER.open("a") as f:
            for r in new:
                f.write(json.dumps(r) + "\n")
    print(f"fc2s_shadow collect: {len(new)} new day-ahead forecasts captured "
          f"({len(rows) + len(new)} total rows, {len(fc)} city-days seen).")


def cmd_settle(args) -> None:
    geo = series_geo()
    now = datetime.now(timezone.utc)
    rows = _load()
    pending = [r for r in rows if r.get("realized_high_f") is None]
    if not pending:
        print("fc2s_shadow settle: nothing pending.")
        return
    realized = _fetch_highs(sorted({r["series"] for r in pending}),
                            past_days=14, forecast_days=1)
    changed = 0
    for r in rows:
        if r.get("realized_high_f") is not None:
            continue
        if r["date"] >= _city_local_date(r["series"], now, geo):   # not over yet
            continue
        rh = realized.get((r["series"], r["date"]))
        if rh is None:
            continue
        r["realized_high_f"] = rh
        r["error_f"] = round(r["forecast_high_f"] - rh, 2)         # +ve = forecast ran hot
        changed += 1
    if changed:
        _rewrite(rows)
    print(f"fc2s_shadow settle: filled {changed} realized highs "
          f"({sum(1 for r in rows if r.get('realized_high_f') is not None)} settled total).")


def cmd_report(args) -> None:
    rows = _load()
    settled = [r for r in rows if r.get("realized_high_f") is not None
               and r.get("forecast_high_f") is not None]
    pending = [r for r in rows if r.get("realized_high_f") is None]
    print("=== fc2s_shadow — forecast-error recalibration (measurement only) ===")
    print(f"{len(settled)} settled city-days, {len(pending)} pending.")
    if len(settled) < 5:
        print("  need ≥5 settled to report — keep collecting (collect daily, settle daily).")
        return
    pairs = [(r["forecast_high_f"], r["realized_high_f"]) for r in settled]
    st = error_stats(pairs)
    print(f"  bias (forecast−realized) {st['bias']:+.2f}°F  ·  measured σ {st['sigma']:.2f}°F "
          f"(live model uses σ={SIGMA_F})  ·  MAE {st['mae']:.2f}°F  ·  n={st['n']}")
    if st["bias"] > 0:
        print(f"  → forecast runs HOT by {st['bias']:.2f}°F on average (the suspected max-hourly bias).")
    tbl = exceedance_table(pairs, recal_bias=st["bias"], recal_sigma=st["sigma"])
    print("\n  exceedance P(realized > strike), strike = forecast + offset:")
    print("   offset   model(σ=3)   recal(bias+σ̂)   realized")
    for o, n, pm, pr, pe in tbl:
        rec = "—" if pr is None else f"{pr:>6.2f}"
        flag = "  ⚠ model overconfident" if (pe is not None and pm - pe > 0.15) else ""
        print(f"   {o:+5.0f}    {pm:>6.2f}      {rec:>7}       {pe:>6.2f}{flag}")
    print("\n  Read: where model(σ=3) ≫ realized = the tail the veto blocks. Where "
          "recal(bias+σ̂) ≈ realized, re-enabling above-strikes with those params is justified.")
    print("  This sleeve never trades — it's the evidence TRADE_ABOVE_STRIKES waits on.")


def selftest() -> int:
    # error_stats: forecast consistently 2° hot, small spread
    pairs = [(72.0, 70.0), (80.0, 78.0), (65.0, 63.0), (90.0, 88.0), (55.0, 53.0)]
    st = error_stats(pairs)
    assert st["n"] == 5 and abs(st["bias"] - 2.0) < 1e-9 and st["sigma"] == 0.0, st
    assert st["mae"] == 2.0, st
    print("error_stats OK")
    # error_stats with spread: errors [3,-1,1,1] → bias 1.0
    st2 = error_stats([(10, 7), (10, 11), (10, 9), (10, 9)])
    assert abs(st2["bias"] - 1.0) < 1e-9 and st2["sigma"] > 0, st2
    print("error_stats (spread) OK")
    # exceedance_table: realized−forecast all = -2 (forecast 2° hot). At offset o=0,
    # realized exceed rate = mean(diff>0) = 0; model(σ=3) at o=0 = 0.5 → overconfident.
    pairs2 = [(f, f - 2.0) for f in (70, 75, 80, 85, 90)]    # diff = -2 for all
    tbl = exceedance_table(pairs2, offsets=(0.0, -2.0, -4.0), recal_bias=2.0, recal_sigma=0.5)
    row0 = next(r for r in tbl if r[0] == 0.0)
    assert abs(row0[2] - 0.5) < 1e-6, row0          # model P at o=0 is 0.5
    assert row0[4] == 0.0, row0                      # realized never exceeds (diff=-2 < 0)
    # at o=-2, every diff (-2) is NOT > -2 (strict), so realized rate 0; recal with
    # bias=2,σ=0.5 → 1−Φ((-2+2)/0.5)=1−Φ(0)=0.5 (recal still says 0.5 at the mean) —
    # the point is the table is computed correctly, asserted via model column:
    row2 = next(r for r in tbl if r[0] == -2.0)
    assert row2[2] == round(1 - _N.cdf(-2.0 / SIGMA_F), 3), row2
    print("exceedance_table OK")
    # empirical exceedance counts strictly: diffs > o
    p3 = [(0.0, 1.0), (0.0, 3.0), (0.0, -1.0)]      # diffs realized−forecast = 1,3,-1
    t3 = exceedance_table(p3, offsets=(0.0, 2.0), model_sigma=3.0)
    assert next(r for r in t3 if r[0] == 0.0)[4] == round(2 / 3, 3)   # 1,3 > 0
    assert next(r for r in t3 if r[0] == 2.0)[4] == round(1 / 3, 3)   # only 3 > 2
    print("exceedance_table (empirical) OK")
    print("PASS")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["collect", "settle", "report", "selftest"])
    args = ap.parse_args()
    if args.mode == "selftest":
        raise SystemExit(selftest())
    {"collect": cmd_collect, "settle": cmd_settle, "report": cmd_report}[args.mode](args)


if __name__ == "__main__":
    main()
