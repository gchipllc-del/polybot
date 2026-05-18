"""
News-impact classifier — adapted from brodyautomates/polymarket-pipeline (270★).

Wave A of the post-TradingAgents-paper polybot upgrade. The insight from
the polymarket-pipeline repo: instead of asking an LLM to estimate a
probability directly (which they're notoriously bad at calibrating),
ask the simpler question:

    "Does this news make YES MORE LIKELY, MORE LIKELY NO, or NOT RELEVANT?"
    "How material is this news? (0.0 = trivial, 1.0 = decisive)"

Then translate the classification + materiality into a probability nudge.
LLMs are dramatically better at directional classification than at
producing a calibrated 0.0-1.0 probability, so the resulting forecasts
are more accurate without spending more tokens.

This module produces ONE additional signal that slots into the existing
Bayesian aggregation chain in lib/forecaster.estimate_probability as
`news_impact_estimate`. It does NOT replace the existing `news_sentiment`
source — they measure different things:

    news_sentiment:   "what's the overall tone of recent coverage?"
    news_impact:      "given THIS specific market question, do these
                      articles shift the outcome more YES or more NO?"

The two are complementary. Sentiment is an aggregate mood; impact is
question-conditioned shift assessment.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from tradingcore.audit import log_event


@dataclass
class NewsClassification:
    """Output of one classify_news_impact() call."""
    direction: str           # "MORE_LIKELY_YES" | "MORE_LIKELY_NO" | "NOT_RELEVANT"
    materiality: float       # 0.0 = no shift, 1.0 = decisive
    reasoning: str
    provider: str = ""
    raw_response: str = ""


VALID_DIRECTIONS = {"MORE_LIKELY_YES", "MORE_LIKELY_NO", "NOT_RELEVANT"}


def _build_prompt(
    question: str,
    articles: list[dict],
    current_yes_price: float | None = None,
) -> str:
    """Compose the classification prompt. Articles are dicts with at least
    `headline` and optionally `summary`/`source`/`published_at`."""
    article_lines = []
    for i, a in enumerate(articles[:8], 1):  # cap at 8 to keep prompt tight
        head = str(a.get("headline") or a.get("title") or "")[:200]
        summ = str(a.get("summary") or a.get("description") or "")[:300]
        src = str(a.get("source") or a.get("publisher") or "")
        when = str(a.get("published_at") or a.get("date") or "")[:10]
        line = f"{i}. [{when} · {src}] {head}"
        if summ and summ != head:
            line += f"\n   {summ}"
        article_lines.append(line)

    articles_block = "\n".join(article_lines) if article_lines else "(no articles)"
    price_block = (
        f"\nCurrent market YES price: {current_yes_price:.3f}"
        if current_yes_price is not None else ""
    )

    return f"""You are evaluating whether news shifts a binary prediction market outcome.

Market question: {question}{price_block}

Recent news (most recent last):
{articles_block}

Answer ONLY in this JSON format, nothing else:
{{
  "direction": "MORE_LIKELY_YES" | "MORE_LIKELY_NO" | "NOT_RELEVANT",
  "materiality": <float 0.0-1.0>,
  "reasoning": "<1-2 sentences>"
}}

Direction rules:
- MORE_LIKELY_YES: these articles, on net, push the outcome toward YES resolution
- MORE_LIKELY_NO: they push toward NO resolution
- NOT_RELEVANT: the articles don't directly bear on this specific question

Materiality rules (be conservative — most news is NOT decisive):
- 0.0-0.2: weak/incidental signal
- 0.3-0.5: moderate signal that adjusts probability somewhat
- 0.6-0.8: strong signal — clearly shifts the outlook
- 0.9-1.0: decisive — outcome is essentially determined

Output JSON only."""


def _parse_response(text: str) -> NewsClassification:
    """Extract the JSON blob from the LLM response. Tolerant of markdown
    code fences and extra prose around the JSON."""
    # Try to find the first {...} block
    match = re.search(r"\{[^{}]*\"direction\"[^{}]*\}", text, re.DOTALL)
    if not match:
        # Fallback: try to extract from a code fence
        fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fence_match:
            match = fence_match
        else:
            raise ValueError("no JSON block in response")

    try:
        data = json.loads(match.group(0) if match.lastindex is None
                          else match.group(1))
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid JSON: {e}") from e

    direction = str(data.get("direction", "")).upper()
    if direction not in VALID_DIRECTIONS:
        raise ValueError(f"invalid direction: {direction!r}")

    try:
        mat = float(data.get("materiality", 0))
    except (TypeError, ValueError):
        mat = 0.0
    mat = max(0.0, min(1.0, mat))  # clamp

    reasoning = str(data.get("reasoning", ""))[:500]
    return NewsClassification(
        direction=direction,
        materiality=mat,
        reasoning=reasoning,
        raw_response=text[:2000],
    )


def classify_news_impact(
    question: str,
    articles: list[dict],
    current_yes_price: float | None = None,
    *,
    complete_fn: Callable[[str], tuple[str, str]] | None = None,
) -> NewsClassification | None:
    """
    Classify whether the news shifts a binary market outcome more YES
    or more NO, with a materiality score.

    Args:
        question: the market question text
        articles: list of article dicts (headline + optional summary/source/date)
        current_yes_price: the market's current YES price (for context)
        complete_fn: optional callable(prompt) -> (response_text, provider_name).
                     Defaults to using the cheapest available provider via
                     lib.llm_analyst._load_providers().

    Returns:
        NewsClassification, or None if no provider is available / parse fails.
        Never raises — failures are logged to the audit trail.
    """
    if not articles:
        return None

    if complete_fn is None:
        complete_fn = _default_complete

    prompt = _build_prompt(question, articles, current_yes_price)

    try:
        text, provider = complete_fn(prompt)
    except Exception as e:
        log_event("news_classifier", "provider_call_failed",
                  {"error": str(e)[:200]}, result="degraded")
        return None

    try:
        result = _parse_response(text)
    except (ValueError, json.JSONDecodeError) as e:
        log_event("news_classifier", "parse_failed",
                  {"error": str(e)[:200], "raw": text[:300]}, result="degraded")
        return None

    result.provider = provider
    log_event("news_classifier", "classified", {
        "direction": result.direction,
        "materiality": round(result.materiality, 3),
        "provider": provider,
        "n_articles": len(articles),
    })
    return result


def classification_to_probability(
    classification: NewsClassification,
    prior: float = 0.5,
    materiality_threshold: float = 0.3,
) -> float:
    """
    Convert a classification + materiality to a probability estimate.

    The shift is proportional to materiality. Below the threshold, the
    classification has no effect (returns the prior). At materiality=1.0
    the estimate is pushed all the way to 1.0 (YES) or 0.0 (NO).

    Args:
        classification: the LLM's directional + materiality output
        prior: starting probability (default 0.5 = neutral)
        materiality_threshold: minimum materiality to apply any shift

    Returns:
        Adjusted probability in [0.01, 0.99] (avoids extremes that break
        downstream Bayesian updates).
    """
    if classification.materiality < materiality_threshold:
        return prior
    if classification.direction == "NOT_RELEVANT":
        return prior

    m = classification.materiality
    if classification.direction == "MORE_LIKELY_YES":
        adjusted = prior + (1.0 - prior) * m
    elif classification.direction == "MORE_LIKELY_NO":
        adjusted = prior * (1.0 - m)
    else:
        return prior

    # Clamp to keep downstream Bayesian updates well-behaved.
    return max(0.01, min(0.99, adjusted))


# ── Default provider integration ─────────────────────────────────────


def _default_complete(prompt: str) -> tuple[str, str]:
    """Use the existing llm_analyst provider chain. Picks the first
    available provider in priority order. Raises on total unavailability."""
    from lib.llm_analyst import _load_providers, _load_strategy
    strategy = _load_strategy()
    providers = _load_providers(strategy)
    if not providers:
        raise RuntimeError("no LLM providers available")

    # Use the cheapest model (smallest quick-task model) for classification —
    # the task is simpler than full superforecaster reasoning.
    spec = providers[0]
    text = spec.client.complete(
        prompt,
        model=spec.quick_model or spec.model,
        max_tokens=400,
        temperature=0.0,  # deterministic classification
    )
    return text, spec.name
