"""Fallback calibration metrics.

API matches how this repo actually calls them (verified against every call site):
each function takes an optional list of forecast RECORDS and, when given none, scores the
records written by record_forecast(). A record is a dict carrying a probability under
"our_probability" (or "forecast"/"probability") and a truthy/falsy "outcome".
Stdlib only; every function degrades to nan/empty rather than raising, because these feed
dashboards and must never take a page down.
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
_LOG = Path(os.environ.get("TRADINGCORE_FORECAST_LOG")
            or (_ROOT / "data" / "forecast_records.jsonl"))

_P_KEYS = ("our_probability", "forecast", "probability", "p")


def _stored() -> list[dict]:
    if not _LOG.exists():
        return []
    out = []
    try:
        for line in _LOG.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return out


def _pairs(records=None) -> list[tuple[float, float]]:
    """(probability, outcome) pairs from records, skipping unresolved/malformed ones."""
    recs = _stored() if records is None else records
    pairs = []
    for r in recs or []:
        if not isinstance(r, dict):
            continue
        p = next((r[k] for k in _P_KEYS if r.get(k) is not None), None)
        o = r.get("outcome")
        if p is None or o is None:
            continue
        try:
            pairs.append((float(p), 1.0 if o else 0.0))
        except (TypeError, ValueError):
            continue
    return pairs


def brier_score(records=None) -> float:
    """Mean squared error of probabilistic forecasts. 0 perfect, 0.25 = coin flip."""
    pairs = _pairs(records)
    if not pairs:
        return float("nan")
    return sum((p - o) ** 2 for p, o in pairs) / len(pairs)


def log_loss(records=None, eps: float = 1e-15) -> float:
    pairs = _pairs(records)
    if not pairs:
        return float("nan")
    tot = 0.0
    for p, o in pairs:
        p = min(max(p, eps), 1 - eps)
        tot += -(o * math.log(p) + (1 - o) * math.log(1 - p))
    return tot / len(pairs)


def calibration_curve(records=None, bins: int = 10) -> list[dict]:
    """Per-bin mean forecast vs realized frequency — the honest calibration picture."""
    buckets: dict[int, list] = {}
    for p, o in _pairs(records):
        buckets.setdefault(min(int(p * bins), bins - 1), []).append((p, o))
    out = []
    for idx in sorted(buckets):
        vals = buckets[idx]
        n = len(vals)
        out.append({"bin": idx, "lo": idx / bins, "hi": (idx + 1) / bins, "n": n,
                    "mean_forecast": sum(v[0] for v in vals) / n,
                    "realized": sum(v[1] for v in vals) / n})
    return out


def source_accuracy(records=None) -> dict:
    """Brier per source for records carrying a "source" field."""
    recs = _stored() if records is None else records
    by: dict[str, list] = {}
    for r in recs or []:
        if isinstance(r, dict):
            by.setdefault(r.get("source") or "unknown", []).append(r)
    return {s: {"n": len(_pairs(v)), "brier": brier_score(v)} for s, v in by.items()}


def record_forecast(source: str = "", forecast: float | None = None,
                    outcome=None, **extra) -> None:
    rec = {"ts": datetime.now(timezone.utc).isoformat(), "source": source,
           "forecast": forecast, "outcome": outcome}
    rec.update(extra)
    try:
        _LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str, separators=(",", ":")) + "\n")
    except OSError:
        pass
