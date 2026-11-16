"""Fallback calibration metrics - written against THIS repo's call sites.

The contract (tests/test_tradingcore_fallback.py replays each site):

  lib/backtest.py:859-862    brier_score(records) / log_loss(records) score THE LIST GIVEN
  lib/forecaster.py:322,526, agents/hermes_optimizer.py:183, lib/dashboard_data.py:148,291
                             brier_score() bare - every caller treats a NEGATIVE value as
                             "no resolved forecasts yet" (`if bs < 0` / `if bs >= 0`), so
                             no data returns -1.0, never nan (nan silently fails both tests
                             and turned "no data yet" into "worst calibration")
  lib/dashboard_terminal.py:179  calibration_curve() -> {bucket: {predicted_mean,
                             actual_rate, gap, count}}  (it iterates .items())
  templates/dashboard.html   source_accuracy() -> {source: {brier, count}}
  lib/resolution_tracker.py:201  record_forecast(market_id=, platform=, question=,
                             our_probability=, market_probability=, side=, sources=, outcome=)

A record is a dict with a probability under "our_probability" (or "forecast" /
"probability") and a truthy/falsy "outcome"; per-source probabilities live in its
"sources" dict. Stdlib only; nothing here raises on bad data - these feed dashboards.
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
NO_DATA = -1.0          # the callers' sentinel: negative means "nothing resolved yet"


def _stored() -> list[dict]:
    if not _LOG.exists():
        return []
    out = []
    try:
        for line in _LOG.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(d, dict):
                out.append(d)
    except OSError:
        return []
    return out


def _prob(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) and 0.0 <= x <= 1.0 else None


def _pairs(records=None) -> list[tuple[float, float]]:
    """(probability, outcome) pairs, skipping unresolved or malformed records."""
    recs = _stored() if records is None else records
    pairs = []
    for r in recs or []:
        if not isinstance(r, dict) or r.get("outcome") is None:
            continue
        p = next((_prob(r[k]) for k in _P_KEYS if r.get(k) is not None), None)
        if p is not None:
            pairs.append((p, 1.0 if r["outcome"] else 0.0))
    return pairs


def brier_score(records=None) -> float:
    """Mean squared error of probability vs outcome; NO_DATA (-1.0) when empty."""
    pairs = _pairs(records)
    if not pairs:
        return NO_DATA
    return sum((p - o) ** 2 for p, o in pairs) / len(pairs)


def log_loss(records=None, eps: float = 1e-15) -> float:
    """Mean negative log-likelihood; NO_DATA (-1.0) when empty."""
    pairs = _pairs(records)
    if not pairs:
        return NO_DATA
    tot = 0.0
    for p, o in pairs:
        p = min(1.0 - eps, max(eps, p))
        tot -= o * math.log(p) + (1.0 - o) * math.log(1.0 - p)
    return tot / len(pairs)


def calibration_curve(records=None, bins: int = 10) -> dict:
    """{"0.6-0.7": {predicted_mean, actual_rate, gap, count}, ...} for non-empty
    buckets, in bucket order. gap = |predicted_mean - actual_rate|."""
    bins = max(1, int(bins))
    buckets: dict[int, list] = {}
    for p, o in _pairs(records):
        buckets.setdefault(min(int(p * bins), bins - 1), []).append((p, o))
    out = {}
    for idx in sorted(buckets):
        vals = buckets[idx]
        n = len(vals)
        pm = sum(v[0] for v in vals) / n
        ar = sum(v[1] for v in vals) / n
        out[f"{idx / bins:.1f}-{(idx + 1) / bins:.1f}"] = {
            "predicted_mean": pm, "actual_rate": ar, "gap": abs(pm - ar), "count": n}
    return out


def source_accuracy(records=None) -> dict:
    """{source: {brier, count}} - each source's OWN probability scored against the
    outcome. Sources come from a record's "sources" dict ({"llm": 0.7, ...}); a legacy
    record carrying a single "source" name is scored on its main probability."""
    recs = _stored() if records is None else records
    by: dict[str, list[tuple[float, float]]] = {}
    for r in recs or []:
        if not isinstance(r, dict) or r.get("outcome") is None:
            continue
        o = 1.0 if r["outcome"] else 0.0
        srcs = r.get("sources")
        if isinstance(srcs, dict) and srcs:
            for name, v in srcs.items():
                p = _prob(v)
                if p is not None:
                    by.setdefault(str(name), []).append((p, o))
        elif r.get("source"):
            p = next((_prob(r[k]) for k in _P_KEYS if r.get(k) is not None), None)
            if p is not None:
                by.setdefault(str(r["source"]), []).append((p, o))
    return {s: {"brier": sum((p - o) ** 2 for p, o in v) / len(v), "count": len(v)}
            for s, v in by.items()}


def record_forecast(market_id: str = "", platform: str = "", question: str = "",
                    our_probability: float | None = None,
                    market_probability: float | None = None, side: str = "",
                    sources: dict | None = None, outcome=None, **extra) -> None:
    """Append one resolved forecast. Accepts the resolution tracker's keywords; legacy
    callers passing source=/forecast= still work through **extra."""
    if our_probability is None and extra.get("forecast") is not None:
        our_probability = extra.pop("forecast")
    rec = {"ts": datetime.now(timezone.utc).isoformat(), "market_id": market_id,
           "platform": platform, "question": str(question)[:500],
           "our_probability": our_probability, "market_probability": market_probability,
           "side": side, "sources": sources if isinstance(sources, dict) else {},
           "outcome": outcome}
    rec.update(extra)
    try:
        _LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str, separators=(",", ":")) + "\n")
    except OSError:
        pass                                   # calibration logging never takes a caller down


def print_calibration_report() -> None:
    """main.py's `calibration` command."""
    bs, ll = brier_score(), log_loss()
    n = len(_pairs())
    print(f"Resolved forecasts: {n}")
    if bs < 0:
        print("No resolved forecasts yet.")
        return
    print(f"Brier score: {bs:.4f}   (0 = perfect, 0.25 = coin flip)")
    print(f"Log loss:    {ll:.4f}")
    for bucket, d in calibration_curve().items():
        print(f"  {bucket}: predicted {d['predicted_mean']:.2f}  actual "
              f"{d['actual_rate']:.2f}  gap {d['gap']:.3f}  n={d['count']}")
    for src, d in sorted(source_accuracy().items()):
        print(f"  source {src:16} brier {d['brier']:.4f}  n={d['count']}")
