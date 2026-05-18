"""
ForecastBench public dataset loader.

Source: https://github.com/forecastingresearch/forecastbench-datasets
Paper: "ForecastBench: A Dynamic Benchmark of AI Forecasting Capabilities"

This module pulls resolved forecasting questions from the public
ForecastBench repository and normalizes them into a form our
forecaster can replay. We use it to measure out-of-sample Brier score
*before* we have a live track record — a cold-start calibration check.

The dataset is split across two kinds of files:
  - question_sets/{DATE}-llm.json    — 500 questions with full text,
                                        background, resolution criteria,
                                        and a "freeze" crowd probability.
  - resolution_sets/{DATE}_resolution_set.json — per-question ground truth.

We join on `id`, keep only the cleanly-resolved binary outcomes
(resolved_to in {0.0, 1.0}), and cache the joined records to
`data/forecastbench/` so repeated runs don't re-download.

Security posture:
  - Dataset URL is hardcoded (GitHub raw, HTTPS-only). We never accept
    an arbitrary base URL from caller input.
  - Downloads use a 60s timeout and reject anything >10 MB.
  - Parsed JSON is type-checked before use; malformed files raise.
  - Cached paths are composed from a strict `YYYY-MM-DD` regex so
    caller-supplied dates can't escape the cache directory.
  - Question text, background, and source are treated as untrusted —
    the downstream forecaster sanitizes these before putting them into
    any LLM prompt.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from tradingcore.audit import log_event

# ── Dataset location (hardcoded, do not parameterize) ─────────────
_BASE_URL = (
    "https://raw.githubusercontent.com/forecastingresearch/"
    "forecastbench-datasets/main/datasets"
)
_CACHE_DIR = Path(__file__).parent.parent / "data" / "forecastbench"
_MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024  # 10 MB ceiling per file
_DOWNLOAD_TIMEOUT_SEC = 60
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Known-good forecast-due dates as of 2026-04-20. This list bounds what
# we'll attempt to load; a caller asking for an unknown date gets a
# clear error rather than a silent miss.
KNOWN_DATES: tuple[str, ...] = (
    "2024-07-21",
    "2024-12-08",
    "2024-12-22",
    "2025-01-05",
    "2025-01-19",
    "2025-02-02",
    "2025-02-16",
    "2025-03-02",
    "2025-03-16",
    "2025-03-30",
    "2025-04-13",
    "2025-04-27",
    "2025-05-11",
    "2025-05-25",
    "2025-06-08",
    "2025-06-22",
    "2025-07-06",
    "2025-07-20",
    "2025-08-03",
    "2025-08-17",
    "2025-08-31",
    "2025-09-14",
    "2026-03-01",
    "2026-03-15",
    "2026-03-29",
)


# ── Data model ───────────────────────────────────────────────────

@dataclass(frozen=True)
class ForecastBenchQuestion:
    """One resolved forecasting question, ready for replay."""
    question_id: str
    source: str                 # "manifold", "metaculus", "polymarket", etc.
    question_text: str
    background: str
    resolution_criteria: str
    market_price: float         # Crowd-implied probability at freeze time
    resolved_to: float          # 0.0 or 1.0 (ground truth, YES/NO)
    resolution_date: str        # ISO date string
    category: str               # Our inferred category label

    @property
    def actual_outcome(self) -> bool:
        """True if the question resolved YES."""
        return self.resolved_to >= 0.5


# ── Source → category mapping ────────────────────────────────────
# ForecastBench question sources map loosely onto our internal
# category taxonomy. We use these to feed base-rate priors correctly
# and to slice Brier scores by category later.
_SOURCE_TO_CATEGORY = {
    "manifold": "other",
    "metaculus": "other",
    "polymarket": "other",
    "infer": "geopolitical",
    "acled": "geopolitical",       # Armed conflict event data
    "dbnomics": "economics",
    "fred": "economics",
    "yfinance": "crypto",          # Closest existing bucket; finance-adjacent
    "wikipedia": "other",
}


def _category_for_source(source: str) -> str:
    return _SOURCE_TO_CATEGORY.get(source, "other")


# ── Download + cache ─────────────────────────────────────────────

def _validate_date(date: str) -> None:
    """Reject anything that isn't a strict YYYY-MM-DD — prevents path
    traversal via caller-supplied dates."""
    if not _DATE_RE.match(date):
        raise ValueError(f"Invalid date format (expected YYYY-MM-DD): {date!r}")


def _download_json(url: str, dest: Path) -> None:
    """Stream a JSON file to disk with size + timeout guards.

    Atomic write (tmp + rename) so a crash mid-download can't leave a
    truncated cache file in place.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "polybot-forecastbench-loader/1.0"})

    try:
        with urllib.request.urlopen(req, timeout=_DOWNLOAD_TIMEOUT_SEC) as resp:
            content_len = resp.headers.get("Content-Length")
            if content_len is not None and int(content_len) > _MAX_DOWNLOAD_BYTES:
                raise RuntimeError(
                    f"Refusing to download {url}: Content-Length "
                    f"{content_len} exceeds {_MAX_DOWNLOAD_BYTES} ceiling"
                )

            tmp_fd, tmp_path = tempfile.mkstemp(dir=dest.parent, suffix=".tmp")
            total = 0
            try:
                with os.fdopen(tmp_fd, "wb") as tmp_f:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > _MAX_DOWNLOAD_BYTES:
                            raise RuntimeError(
                                f"Download exceeded {_MAX_DOWNLOAD_BYTES} bytes mid-stream"
                            )
                        tmp_f.write(chunk)
                os.replace(tmp_path, dest)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except FileNotFoundError:
                    pass
                raise
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} fetching {url}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error fetching {url}: {e.reason}") from e


def _cache_path(kind: str, date: str) -> Path:
    """Compose a cache path. `kind` is one of 'question', 'resolution'."""
    _validate_date(date)
    if kind not in ("question", "resolution"):
        raise ValueError(f"Unknown cache kind: {kind!r}")
    name = f"{date}_{kind}.json"
    return _CACHE_DIR / name


def _load_question_set(date: str) -> dict:
    """Fetch the question set for `date` (from cache if present)."""
    cache = _cache_path("question", date)
    if not cache.exists():
        url = f"{_BASE_URL}/question_sets/{date}-llm.json"
        log_event("forecastbench", "download_question_set", {
            "date": date, "url": url,
        }, result="pending")
        _download_json(url, cache)
        log_event("forecastbench", "download_question_set", {
            "date": date, "size_bytes": cache.stat().st_size,
        }, result="success")

    with open(cache, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or "questions" not in data:
        raise ValueError(f"Malformed question set {cache}: missing 'questions' key")
    return data


def _load_resolution_set(date: str) -> dict:
    """Fetch the resolution set for `date` (from cache if present)."""
    cache = _cache_path("resolution", date)
    if not cache.exists():
        url = f"{_BASE_URL}/resolution_sets/{date}_resolution_set.json"
        log_event("forecastbench", "download_resolution_set", {
            "date": date, "url": url,
        }, result="pending")
        _download_json(url, cache)
        log_event("forecastbench", "download_resolution_set", {
            "date": date, "size_bytes": cache.stat().st_size,
        }, result="success")

    with open(cache, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or "resolutions" not in data:
        raise ValueError(f"Malformed resolution set {cache}: missing 'resolutions' key")
    return data


# ── Normalization ────────────────────────────────────────────────

def _safe_str(val, default: str = "") -> str:
    """Coerce to string, handle None and non-string inputs safely."""
    if val is None:
        return default
    return str(val)


def _safe_float(val, default: float = 0.5) -> float:
    """Coerce to float in [0, 1], fall back to default."""
    try:
        f = float(val)
        if not (0.0 <= f <= 1.0):
            return default
        return f
    except (TypeError, ValueError):
        return default


def _render_placeholders(text: str, forecast_due_date: str, resolution_date: str) -> str:
    """Substitute ForecastBench's {forecast_due_date} / {resolution_date}
    placeholders with actual dates. We do this with explicit string
    replacement (not str.format) because question text can contain
    literal braces from source markets that would break format()."""
    return (
        text
        .replace("{forecast_due_date}", forecast_due_date)
        .replace("{resolution_date}", resolution_date)
    )


def _join_date(date: str) -> list[ForecastBenchQuestion]:
    """Load and join question + resolution sets for a single date."""
    qset = _load_question_set(date)
    rset = _load_resolution_set(date)

    # forecast_due_date is the "freeze" date — what the crowd saw at
    # prediction time. We need it to render question placeholders.
    forecast_due_date = _safe_str(qset.get("forecast_due_date"), date)

    qmap: dict[str, dict] = {}
    for q in qset.get("questions", []):
        if not isinstance(q, dict):
            continue
        qid = q.get("id")
        if isinstance(qid, str):
            qmap[qid] = q

    results: list[ForecastBenchQuestion] = []
    for r in rset.get("resolutions", []):
        if not isinstance(r, dict):
            continue
        if not r.get("resolved"):
            continue
        resolved_to = r.get("resolved_to")
        # Only clean binary outcomes — skip partial / ambiguous resolutions.
        if resolved_to not in (0.0, 1.0):
            continue

        qid = r.get("id")
        if not isinstance(qid, str) or qid not in qmap:
            continue
        q = qmap[qid]

        source = _safe_str(q.get("source"), "unknown")
        raw_question = _safe_str(q.get("question")).strip()
        if not raw_question:
            continue

        resolution_date = _safe_str(r.get("resolution_date"), date)
        question_text = _render_placeholders(raw_question, forecast_due_date, resolution_date)

        # `freeze_datetime_value` is the crowd-implied probability at
        # the forecast-due date. It's the right "market_price" analogue
        # — what the crowd thought *before* outcome was known.
        market_price = _safe_float(q.get("freeze_datetime_value"), 0.5)

        results.append(ForecastBenchQuestion(
            question_id=qid,
            source=source,
            question_text=question_text,
            background=_safe_str(q.get("background")).strip(),
            resolution_criteria=_safe_str(q.get("resolution_criteria")).strip(),
            market_price=market_price,
            resolved_to=float(resolved_to),
            resolution_date=resolution_date,
            category=_category_for_source(source),
        ))

    return results


# ── Public API ───────────────────────────────────────────────────

def load_resolved_questions(
    dates: list[str] | None = None,
    limit: int | None = None,
    sources: list[str] | None = None,
) -> list[ForecastBenchQuestion]:
    """Load resolved binary questions from one or more forecast-due dates.

    Args:
        dates: Which forecast-due dates to pull. Defaults to the 5 most
            recent known dates. Each date must be in KNOWN_DATES or a
            ValueError is raised.
        limit: If set, stop after N questions across all dates. Useful
            for fast smoke tests.
        sources: If set, restrict to these question sources (e.g. to
            focus on Polymarket or Metaculus).

    Returns:
        A list of ForecastBenchQuestion records. Duplicates across
        forecast-due dates are deduped by question_id (keeping the
        earliest appearance).

    Raises:
        ValueError: on malformed date strings or unknown dates.
        RuntimeError: on network or cache-write failures.
    """
    if dates is None:
        # Default: 5 most recent known dates (plenty of binary resolutions)
        dates = list(KNOWN_DATES[-5:])

    for d in dates:
        _validate_date(d)
        if d not in KNOWN_DATES:
            raise ValueError(
                f"Date {d!r} not in KNOWN_DATES. Valid: {list(KNOWN_DATES)}"
            )

    # limit=0 means zero, not unlimited. Short-circuit here to avoid
    # the "append then check" race in the loop below.
    if limit is not None and limit <= 0:
        return []

    seen_ids: set[str] = set()
    results: list[ForecastBenchQuestion] = []
    for d in dates:
        try:
            batch = _join_date(d)
        except Exception as e:
            log_event("forecastbench", "date_load_failed", {
                "date": d, "error": str(e)[:200],
            }, result="failed")
            continue

        for q in batch:
            if q.question_id in seen_ids:
                continue
            if sources is not None and q.source not in sources:
                continue
            seen_ids.add(q.question_id)
            results.append(q)
            if limit is not None and len(results) >= limit:
                log_event("forecastbench", "load_complete", {
                    "dates": dates, "returned": len(results), "limit": limit,
                }, result="success")
                return results

    log_event("forecastbench", "load_complete", {
        "dates": dates, "returned": len(results),
    }, result="success")
    return results


def iter_resolved_questions(
    dates: list[str] | None = None,
    sources: list[str] | None = None,
) -> Iterator[ForecastBenchQuestion]:
    """Streaming variant — iterate resolved questions one at a time.

    Useful for backtests over the full dataset where loading all
    records upfront would use too much memory (the full dataset is
    small today, but the function is provided for future-proofing).
    """
    yield from load_resolved_questions(dates=dates, sources=sources)
