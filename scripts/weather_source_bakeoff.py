#!/usr/bin/env python3
"""weather_source_bakeoff — which temperature source best matches what Kalshi SETTLES on,
and is blending multiple sources helping or hurting?

forecaster_accuracy.py ranks the Open-Meteo models against ERA5 reanalysis and openly
flags the gap: "Kalshi settles on the exact NWS station obs, so absolute bias may differ."
That gap IS the question. This ranks each source against the SETTLEMENT-EXACT truth — the
IEM ASOS station's own realized daily max (the station Kalshi settles on) — so the basis
risk (your source vs the settlement source) is measured, not approximated.

For each settlement station, over a recent window, it scores each Open-Meteo model's
day-ahead high (ecmwf/gfs/icon/gem) + the best_match blend against the station's realized
max, and reports:
  (1) the single most settlement-consistent source (lowest MAE vs the station),
  (2) whether the 4-model BLEND beats the single best (i.e. is "multiple thermometers"
      adding skill or just noise),
  (3) the ERA5-vs-station gap (how wrong the reanalysis truth used elsewhere is).

Both data sources are HISTORICAL, so one run answers it — no multi-day wait. Read-only.

  python scripts/weather_source_bakeoff.py --days 14
  python scripts/weather_source_bakeoff.py --selftest
"""
import argparse
import json
import statistics as st
import sys
from collections import defaultdict
from datetime import date as _date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

LOG = ROOT / "data" / "weather_source_bakeoff.jsonl"
MODELS = ["ecmwf_ifs025", "gfs_seamless", "icon_seamless", "gem_seamless"]


# ── pure analysis (testable, no network) ────────────────────────────────────

def error_stats(pairs: list) -> dict:
    """pairs = [(predicted, truth), …] → MAE, mean signed bias, σ of error, n."""
    errs = [p - t for p, t in pairs]
    n = len(errs)
    if n == 0:
        return {"n": 0, "mae": None, "bias": None, "sigma": None}
    return {"n": n,
            "mae": round(sum(abs(e) for e in errs) / n, 2),
            "bias": round(sum(errs) / n, 2),
            "sigma": round(st.pstdev(errs), 2) if n > 1 else 0.0}


def per_source(records: list, truth_key: str = "truth_iem") -> dict:
    """{source: [(pred, truth)]} for every source present, vs the chosen truth."""
    by = defaultdict(list)
    for r in records:
        t = r.get(truth_key)
        if t is None:
            continue
        for src, v in (r.get("preds") or {}).items():
            if v is not None:
                by[src].append((float(v), float(t)))
    return by


def blend_pairs(records: list, models: list, truth_key: str = "truth_iem") -> list:
    """Mean of the given models per record vs truth — the 'multi-thermometer' estimate."""
    out = []
    for r in records:
        t = r.get(truth_key)
        if t is None:
            continue
        preds = r.get("preds") or {}
        vals = [float(preds[m]) for m in models if preds.get(m) is not None]
        if vals:
            out.append((sum(vals) / len(vals), float(t)))
    return out


def rank(by_source: dict) -> list:
    """[(source, stats)] sorted by MAE ascending (most settlement-consistent first)."""
    rows = [(s, error_stats(p)) for s, p in by_source.items() if p]
    return sorted(rows, key=lambda x: (x[1]["mae"] if x[1]["mae"] is not None else 9e9))


def era5_gap(records: list) -> dict:
    """How far ERA5 reanalysis sits from the settlement-exact station max — the basis risk
    baked into any model-ranking that uses ERA5 as 'truth'."""
    return error_stats([(float(r["truth_era5"]), float(r["truth_iem"]))
                        for r in records
                        if r.get("truth_era5") is not None and r.get("truth_iem") is not None])


# ── network collect (runs where there's an outbound connection) ──────────────

def collect(days: int) -> list:
    from forecaster_accuracy import _fetch_model_forecasts, _get, ARCHIVE  # noqa: E402
    from asos_tracker import fetch_iem_day, STATIONS                       # noqa: E402
    from fc_two_sided import series_geo                                    # noqa: E402

    geo = series_geo()
    end = _date.today() - timedelta(days=1)                # yesterday = last complete day
    start = end - timedelta(days=days - 1)
    s_iso, e_iso = start.isoformat(), end.isoformat()
    records = []
    for series, st_tz in STATIONS.items():
        station, tz = st_tz
        ll = geo.get(series)
        if not ll:
            continue
        lat, lon = ll
        models = _fetch_model_forecasts(lat, lon, s_iso, e_iso, "max")     # {model:{date:f}} + openmeteo_default
        era5 = {}
        j = _get(ARCHIVE, {"latitude": lat, "longitude": lon, "start_date": s_iso,
                           "end_date": e_iso, "daily": "temperature_2m_max",
                           "temperature_unit": "fahrenheit", "timezone": "auto"})
        if j and "daily" in j:
            for d, v in zip(j["daily"].get("time", []), j["daily"].get("temperature_2m_max", [])):
                if v is not None:
                    era5[d] = float(v)
        d = start
        while d <= end:
            ds = d.isoformat()
            try:
                obs = fetch_iem_day(station, tz, d)
            except Exception:                                              # noqa: BLE001
                obs = []
            iem_max = max((float(v) for _, v in obs if v not in (None, "", "M")), default=None)
            preds = {m: models.get(m, {}).get(ds) for m in list(models)}
            preds = {k: v for k, v in preds.items() if v is not None}
            records.append({"series": series, "station": station, "date": ds,
                            "truth_iem": iem_max, "truth_era5": era5.get(ds), "preds": preds})
            d += timedelta(days=1)
    return records


def _report(records: list) -> None:
    scored = [r for r in records if r.get("truth_iem") is not None and r.get("preds")]
    print(f"=== weather_source_bakeoff — sources vs SETTLEMENT-EXACT station max ===")
    print(f"  {len(scored)} station-days scored (of {len(records)} collected)")
    if not scored:
        print("  nothing scored — no IEM truth or model preds (network? station ids?).")
        return
    by = per_source(scored, "truth_iem")
    print("\n  source                 n    MAE    bias    σ   (vs the station Kalshi settles on)")
    for src, s in rank(by):
        print(f"  {src:<20} {s['n']:>4}  {s['mae']:>5.2f}  {s['bias']:>+5.2f}  {s['sigma']:>4.2f}")

    # (2) blend vs single best — the "is multiple hurting?" answer
    bp = blend_pairs(scored, MODELS, "truth_iem")
    bstats = error_stats(bp)
    singles = [(s, st_["mae"]) for s, st_ in rank(by) if s in MODELS and st_["mae"] is not None]
    if bstats["mae"] is not None and singles:
        best_src, best_mae = singles[0]
        verdict = ("BLEND helps" if bstats["mae"] < best_mae - 0.01 else
                   "single best wins — blending adds noise" if bstats["mae"] > best_mae + 0.01 else
                   "tie")
        print(f"\n  4-model BLEND  MAE {bstats['mae']:.2f}   vs best single ({best_src}) {best_mae:.2f}"
              f"  → {verdict}")

    # (3) the reanalysis basis risk
    g = era5_gap(scored)
    if g["n"]:
        print(f"\n  ERA5 reanalysis vs station: MAE {g['mae']:.2f}, bias {g['bias']:+.2f} over {g['n']} days")
        print("  (that's the error baked into any ranking that scores against ERA5 instead of the station)")

    print("\n  Read: the lowest-MAE source is the most settlement-consistent single 'thermometer'.")
    print("  If BLEND's MAE isn't below the best single's, averaging sources is NOT helping —")
    print("  trust the one source. NOTE: even the best MAE here (~the weather's day-ahead limit)")
    print("  must beat the MARKET's own forecast to be tradeable — accuracy ≠ edge.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=14, help="lookback window (complete past days)")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        raise SystemExit(_selftest())

    print(f"collecting {args.days} days of model forecasts + station truth …")
    records = collect(args.days)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
    except OSError:
        pass
    _report(records)


def _selftest() -> int:
    # ecmwf is accurate (±0.5), gfs biased hot (+3), icon noisy. Build pairs vs truth=80.
    def rec(e, g, ic, truth):
        return {"truth_iem": truth, "truth_era5": truth + 1.5,  # ERA5 sits 1.5 above station
                "preds": {"ecmwf_ifs025": e, "gfs_seamless": g, "icon_seamless": ic}}
    recs = [rec(80.5, 83, 78, 80), rec(79.5, 82, 82, 80), rec(80.0, 83, 79, 80),
            rec(80.5, 83, 81, 80), rec(79.5, 82, 78, 80)]
    by = per_source(recs)
    r = rank(by)
    assert r[0][0] == "ecmwf_ifs025", r                         # most accurate ranks first
    assert dict(by).keys() == {"ecmwf_ifs025", "gfs_seamless", "icon_seamless"}, by
    assert error_stats(by["gfs_seamless"])["bias"] > 2.5, by    # gfs hot bias detected
    # blend of the three vs ecmwf alone: ecmwf is best, blend dragged by gfs's +3 → worse
    bp = blend_pairs(recs, ["ecmwf_ifs025", "gfs_seamless", "icon_seamless"])
    assert error_stats(bp)["mae"] > error_stats(by["ecmwf_ifs025"])["mae"], (
        error_stats(bp), error_stats(by["ecmwf_ifs025"]))
    # offsetting biases: A hot +2, B cold −2 → blend (mean) nails truth, beats either single
    off = [{"truth_iem": 80, "preds": {"A": 82, "B": 78}} for _ in range(4)]
    assert error_stats(blend_pairs(off, ["A", "B"]))["mae"] < error_stats(per_source(off)["A"])["mae"]
    # era5 gap surfaces the +1.5 reanalysis offset
    assert abs(era5_gap(recs)["bias"] - 1.5) < 1e-9, era5_gap(recs)
    print("error_stats + rank + blend-vs-single + offsetting-blend + era5_gap OK")
    print("PASS")
    return 0


if __name__ == "__main__":
    main()
