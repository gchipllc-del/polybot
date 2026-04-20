"""
Metaculus Client — pull community forecasts as a forecasting signal.

Metaculus is a calibrated-forecasting community that reliably outperforms
most individual forecasters (published Brier ~0.149 on binary questions,
per Halawi 2024). Their community forecast is one of the best free signals
for prediction-market trading.

This module:
    1. Searches Metaculus for questions matching our market
    2. Scores candidates by keyword overlap + resolution-date proximity
    3. Returns the top match's current community forecast
    4. Falls back to None if no good match (forecaster degrades gracefully)

No auth required for read access to public questions.

Security & reliability:
    - All Metaculus responses treated as untrusted (validated per-field)
    - Timeouts on every call; hard cap on search-result pagination
    - 60s TTL in-process cache to avoid hammering during a scan
    - Never fails the caller on API error — returns None

Rationale for not using Metaculus/forecasting-tools (pypi) directly:
Their wrapper is thin but adds pydantic + extra deps. This raw client
covers the two endpoints we need (search + community prediction) with
zero external deps beyond `requests`.
"""

import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import requests
from dotenv import load_dotenv

from lib.audit import log_event

load_dotenv()

METACULUS_API_BASE = "https://www.metaculus.com/api2"
SEARCH_TIMEOUT_SEC = 10
CACHE_TTL_SEC = 60.0
MAX_SEARCH_RESULTS = 10

# Metaculus requires auth for their public API as of 2026. Set
# METACULUS_API_TOKEN in .env (https://www.metaculus.com/accounts/profile/
# → API tokens). Absent a token, find_metaculus_match() returns None so
# the forecaster still works without this signal.
_METACULUS_TOKEN = os.getenv("METACULUS_API_TOKEN", "")


_STOPWORDS = frozenset({
    "a", "an", "the", "will", "is", "are", "be", "been", "was", "were",
    "in", "on", "at", "to", "for", "of", "and", "or", "by", "with",
    "this", "that", "these", "those", "it", "its", "than", "more", "less",
    "who", "what", "when", "where", "why", "how", "which",
    "there", "any", "if", "but", "before", "after", "during",
    "would", "could", "should", "may", "might", "can", "does", "do",
})

# Simple in-process cache: query -> (timestamp, result)
_QUERY_CACHE: dict[str, tuple[float, "MetaculusMatch | None"]] = {}


@dataclass
class MetaculusMatch:
    """A Metaculus question matched to one of our markets."""
    question_id: int
    title: str
    url: str
    community_prediction: float          # 0.0 - 1.0
    resolved: bool
    resolution_date: str                 # ISO 8601 or ""
    match_score: float                   # 0.0 - 1.0 keyword overlap


# ── Internal helpers ───────────────────────────────────────────────

def _content_tokens(text: str) -> set[str]:
    """Lowercase content words with stopwords removed."""
    import re
    text = re.sub(r"[^\w\s]", " ", (text or "").lower())
    return {w for w in text.split() if w not in _STOPWORDS and len(w) > 2}


def _search_query_from(question: str) -> str:
    """Build a Metaculus search query from a prediction market question.

    Metaculus search is token-matching, so stopwords help zero results.
    We extract the content tokens and take the ~6 most-informative
    (longest first — proxy for proper nouns / rare terms)."""
    tokens = sorted(_content_tokens(question), key=lambda t: -len(t))
    return " ".join(tokens[:6])


def _parse_iso(date_str: str | None) -> Optional[datetime]:
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _score_candidate(
    query_tokens: set[str],
    our_resolution: Optional[datetime],
    metac_title: str,
    metac_resolution: Optional[datetime],
) -> float:
    """Rate a Metaculus question as a match for ours (0-1)."""
    title_tokens = _content_tokens(metac_title)
    if not title_tokens or not query_tokens:
        return 0.0

    intersection = query_tokens & title_tokens
    if not intersection:
        return 0.0

    # Overlap coefficient (favors subset / superset matches)
    overlap = len(intersection) / min(len(query_tokens), len(title_tokens))

    # Jaccard as secondary signal
    union = query_tokens | title_tokens
    jaccard = len(intersection) / len(union)

    score = 0.7 * overlap + 0.3 * jaccard

    # Bonus if resolution dates are close
    if our_resolution and metac_resolution:
        days_apart = abs((metac_resolution - our_resolution).total_seconds()) / 86400
        if days_apart < 14:
            score += 0.10
        elif days_apart < 60:
            score += 0.05

    return min(score, 1.0)


def _extract_community_forecast(question: dict) -> Optional[float]:
    """Metaculus returns community forecasts in several possible shapes
    depending on API version. Try them in order, return None on miss."""
    # v2 shape: community_prediction.full.q2 (median)
    cp = question.get("community_prediction")
    if isinstance(cp, dict):
        full = cp.get("full", {})
        if isinstance(full, dict):
            for key in ("q2", "median", "center"):
                val = full.get(key)
                if isinstance(val, (int, float)) and 0 <= val <= 1:
                    return float(val)

    # Older shape: community_prediction as a flat dict
    if isinstance(cp, dict):
        val = cp.get("y")
        if isinstance(val, (int, float)) and 0 <= val <= 1:
            return float(val)

    # Fallback: prediction_count / percentile on question directly
    for key in ("community_prediction_value", "metaculus_prediction"):
        val = question.get(key)
        if isinstance(val, (int, float)) and 0 <= val <= 1:
            return float(val)

    return None


# ── Public API ─────────────────────────────────────────────────────

def find_metaculus_match(
    question: str,
    resolution_date: str | None = None,
    min_match_score: float = 0.50,
) -> MetaculusMatch | None:
    """
    Search Metaculus for a question that matches `question` and return
    its community forecast.

    Args:
        question: Our prediction market question text
        resolution_date: Our market's resolution date (ISO) — used as
                         tiebreaker for scoring
        min_match_score: Minimum keyword-overlap score to consider a match.
                         0.50 avoids weak cross-topic false positives.

    Returns:
        MetaculusMatch with community_prediction, or None.
    """
    if not question or len(question) < 8:
        return None

    # Metaculus now requires auth — skip silently when token missing
    if not _METACULUS_TOKEN:
        return None

    query = _search_query_from(question)
    if not query:
        return None

    # Cache check
    cache_key = f"{query}|{resolution_date or ''}"
    now = time.time()
    cached = _QUERY_CACHE.get(cache_key)
    if cached and (now - cached[0]) < CACHE_TTL_SEC:
        return cached[1]

    try:
        resp = requests.get(
            f"{METACULUS_API_BASE}/questions/",
            params={
                "search": query,
                "type": "forecast",
                "limit": MAX_SEARCH_RESULTS,
                "order_by": "-publish_time",
            },
            headers={"Authorization": f"Token {_METACULUS_TOKEN}"},
            timeout=SEARCH_TIMEOUT_SEC,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        log_event("metaculus", "search_failed", {
            "query": query[:100],
            "error": str(e)[:200],
        }, result="failed")
        _QUERY_CACHE[cache_key] = (now, None)
        return None

    results = data.get("results", []) if isinstance(data, dict) else []
    if not results:
        _QUERY_CACHE[cache_key] = (now, None)
        return None

    our_resolution = _parse_iso(resolution_date)
    query_tokens = _content_tokens(question)

    best: MetaculusMatch | None = None

    for q in results:
        if not isinstance(q, dict):
            continue
        title = q.get("title", "") or q.get("question", "")
        if not title:
            continue

        # Only binary questions
        if q.get("type") != "forecast" and q.get("possibilities", {}).get("type") != "binary":
            # Skip MC/numeric/date questions — they don't map cleanly
            continue

        metac_resolution = _parse_iso(q.get("resolve_time") or q.get("close_time"))
        score = _score_candidate(query_tokens, our_resolution, title, metac_resolution)
        if score < min_match_score:
            continue

        community = _extract_community_forecast(q)
        if community is None:
            continue

        qid = q.get("id")
        if not isinstance(qid, int):
            continue

        match = MetaculusMatch(
            question_id=qid,
            title=title[:200],
            url=f"https://www.metaculus.com/questions/{qid}/",
            community_prediction=community,
            resolved=bool(q.get("resolved")),
            resolution_date=(q.get("resolve_time") or "")[:30],
            match_score=round(score, 3),
        )

        if not best or match.match_score > best.match_score:
            best = match

    _QUERY_CACHE[cache_key] = (now, best)

    if best:
        log_event("metaculus", "match_found", {
            "query": query[:100],
            "match_title": best.title[:120],
            "community_prediction": best.community_prediction,
            "match_score": best.match_score,
        }, result="success")

    return best


def get_metaculus_estimate(
    question: str,
    resolution_date: str | None = None,
) -> float | None:
    """Convenience wrapper returning just the community probability, or None."""
    match = find_metaculus_match(question, resolution_date)
    return match.community_prediction if match else None
