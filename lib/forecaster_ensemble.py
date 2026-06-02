"""Robust multi-model forecast combiner — shared by the daily and hourly
weather sleeves so both blend NWS + the named Open-Meteo models (ECMWF/ICON/GFS)
the same way.

Why this exists: Open-Meteo's unnamed "default" blend is GFS for US points, and a
single model busting (e.g. GFS reading 70°F when the truth is 84-85°) dragging a
naive 2-model average is what manufactured the #174 cold-blend bleed. The
combiner is two-stage and robust:
  1. with >=3 members, DROP any member more than `outlier_reject_f` from the
     ensemble median (a busted model is ignored entirely);
  2. inverse-MAE weight the survivors so the measured-most-accurate forecaster
     (ECMWF) leads.

Per-forecaster MAE comes from scripts/forecaster_accuracy.py (airport-station
verification vs ERA5 actuals). Callers pass the MAE table for the relevant
quantity (daily max, daily min, or hourly temp). These are WEIGHTS, not truths —
re-derive as data accrues.
"""
from __future__ import annotations

import math


def median(xs: list) -> float:
    s = sorted(xs)
    n = len(s)
    if n == 0:
        raise ValueError("median of empty sequence")
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def skill_weighted_point(
    mae: dict,
    contributions: dict,
    *,
    outlier_reject_f: float = 5.0,
    default_mae: float = 1.6,
):
    """Return (point_f | None, kept_member_names) from {source_name: forecast_f}.

    mae: {source_name: MAE_f} for THIS quantity. Unknown sources get
    `default_mae` (a modest weight). With >=3 finite members, members more than
    `outlier_reject_f` from the median are dropped before weighting."""
    pts = {k: float(v) for k, v in contributions.items()
           if v is not None and isinstance(v, (int, float)) and math.isfinite(v)}
    if not pts:
        return None, []
    if len(pts) >= 3:
        med = median(list(pts.values()))
        kept = {k: v for k, v in pts.items()
                if abs(v - med) <= outlier_reject_f}
        if kept:
            pts = kept
    num = den = 0.0
    for name, val in pts.items():
        w = 1.0 / float(mae.get(name, default_mae) or default_mae)
        num += w * val
        den += w
    return ((num / den) if den > 0 else None), sorted(pts)
