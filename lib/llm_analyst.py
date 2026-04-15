"""
LLM Superforecaster — Claude API-powered market analysis.

Uses the 5-step probabilistic reasoning framework from top Superforecasters:
    1. Reference class: What category of event is this? Base rate?
    2. Inside view: What specific evidence applies to THIS market?
    3. Key drivers: What are the 3-5 most important factors?
    4. Probability estimate: Synthesize into a number with reasoning
    5. Confidence check: What would change your mind? How wide is the range?

Security:
    - API key loaded ONLY from environment variable (never from config files)
    - All API responses treated as untrusted input (parsed defensively)
    - No market data or user info sent beyond what's needed for analysis
    - Responses cached with TTL to control costs and rate limits
    - Rate limiting enforced per settings.yaml
    - No secrets in any log or error message
"""

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

from lib.audit import log_event

CONFIG_PATH = Path(__file__).parent.parent / "config"
CACHE_DIR = Path(__file__).parent.parent / "data" / "llm_cache"


def _load_settings() -> dict:
    with open(CONFIG_PATH / "settings.yaml", "r") as f:
        return yaml.safe_load(f)


def _load_strategy() -> dict:
    with open(CONFIG_PATH / "strategy.yaml", "r") as f:
        return yaml.safe_load(f)


@dataclass
class LLMAnalysis:
    """Structured output from Claude superforecaster analysis."""
    probability: float          # YES probability estimate (0.0 - 1.0)
    confidence: float           # Self-assessed confidence (0.0 - 1.0)
    reference_class: str        # What category/base rate reasoning
    key_factors: list[str]      # Top 3-5 drivers
    reasoning: str              # Full chain of thought
    reversal_triggers: list[str]  # What would change the estimate
    raw_response: str           # Full LLM output for audit


# ── Rate Limiter ──────────────────────────────────────────────────

class _RateLimiter:
    """Token bucket rate limiter for API calls. Thread-safe enough for single-process."""

    def __init__(self, calls_per_minute: int = 30):
        self._interval = 60.0 / max(calls_per_minute, 1)
        self._last_call = 0.0

    def wait(self):
        """Block until we can make the next call."""
        now = time.time()
        elapsed = now - self._last_call
        if elapsed < self._interval:
            time.sleep(self._interval - elapsed)
        self._last_call = time.time()


_limiter: _RateLimiter | None = None


def _get_limiter() -> _RateLimiter:
    global _limiter
    if _limiter is None:
        settings = _load_settings()
        rpm = settings.get("rate_limits", {}).get("anthropic_calls_per_minute", 30)
        _limiter = _RateLimiter(rpm)
    return _limiter


# ── Cache ─────────────────────────────────────────────────────────

def _cache_key(market_id: str, question: str) -> str:
    """Deterministic cache key for a market analysis."""
    raw = f"{market_id}:{question}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _cache_get(key: str, ttl_minutes: int) -> LLMAnalysis | None:
    """Read from cache if entry exists and hasn't expired."""
    cache_file = CACHE_DIR / f"{key}.json"
    if not cache_file.exists():
        return None

    try:
        with open(cache_file, "r") as f:
            data = json.load(f)

        # Check TTL
        cached_at = data.get("_cached_at", 0)
        if time.time() - cached_at > ttl_minutes * 60:
            cache_file.unlink(missing_ok=True)
            return None

        return LLMAnalysis(
            probability=data["probability"],
            confidence=data["confidence"],
            reference_class=data["reference_class"],
            key_factors=data["key_factors"],
            reasoning=data["reasoning"],
            reversal_triggers=data["reversal_triggers"],
            raw_response=data.get("raw_response", "(cached)"),
        )
    except (json.JSONDecodeError, KeyError, OSError):
        # Corrupted cache — delete and re-fetch
        cache_file.unlink(missing_ok=True)
        return None


def _cache_put(key: str, analysis: LLMAnalysis):
    """Write analysis to cache."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "probability": analysis.probability,
        "confidence": analysis.confidence,
        "reference_class": analysis.reference_class,
        "key_factors": analysis.key_factors,
        "reasoning": analysis.reasoning,
        "reversal_triggers": analysis.reversal_triggers,
        "raw_response": analysis.raw_response,
        "_cached_at": time.time(),
    }
    cache_file = CACHE_DIR / f"{key}.json"
    with open(cache_file, "w") as f:
        json.dump(data, f, indent=2)


# ── Prompt Construction ───────────────────────────────────────────

SUPERFORECASTER_PROMPT = """You are a world-class Superforecaster tasked with estimating the probability that the following prediction market question resolves YES.

MARKET QUESTION: {question}

MARKET DESCRIPTION: {description}

CURRENT MARKET PRICE: {market_price:.0%} (this is the crowd's implied probability)

CATEGORY: {category}

RESOLUTION DATE: {resolution_date}

Use the following 5-step framework. Be rigorous and quantitative.

## Step 1: Reference Class
What category of event is this? What is the historical base rate for similar events? Name specific reference classes and their frequencies.

## Step 2: Inside View
What specific evidence applies to THIS particular market that might deviate from the base rate? Consider recent developments, unique circumstances, and domain-specific factors.

## Step 3: Key Drivers (list exactly 3-5)
What are the most important factors that will determine the outcome? For each, state whether it pushes toward YES or NO and by how much.

## Step 4: Probability Estimate
Synthesize the above into a single probability. Show your reasoning for the final number. Be precise — don't round to multiples of 5 or 10 unless truly warranted.

## Step 5: Confidence & Reversal
How confident are you in this estimate? What specific new information would cause you to revise significantly? What's the reasonable range?

## OUTPUT FORMAT (required)
End your response with EXACTLY this block:

PROBABILITY: [number between 0.01 and 0.99]
CONFIDENCE: [number between 0.0 and 1.0]
REFERENCE_CLASS: [one-line summary]
KEY_FACTORS: [factor1 | factor2 | factor3]
REVERSAL_TRIGGERS: [trigger1 | trigger2]"""


def _build_prompt(question: str, description: str, market_price: float,
                  category: str, resolution_date: str) -> str:
    """Build the superforecaster prompt. Sanitizes inputs to prevent injection."""
    # Sanitize inputs — strip control characters and limit length
    def sanitize(s: str, max_len: int = 2000) -> str:
        s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s)  # Strip control chars
        return s[:max_len]

    return SUPERFORECASTER_PROMPT.format(
        question=sanitize(question, 500),
        description=sanitize(description, 2000),
        market_price=max(0.01, min(market_price, 0.99)),
        category=sanitize(category, 50),
        resolution_date=sanitize(resolution_date, 50),
    )


# ── Response Parsing ──────────────────────────────────────────────

def _parse_response(text: str) -> dict:
    """
    Extract structured data from LLM response.

    Treats LLM output as untrusted — validates and clamps all values.
    Falls back to defaults on parse failure rather than crashing.
    """
    result = {
        "probability": 0.50,
        "confidence": 0.50,
        "reference_class": "",
        "key_factors": [],
        "reversal_triggers": [],
    }

    # Extract PROBABILITY (handle negative/out-of-range from untrusted LLM output)
    prob_match = re.search(r"PROBABILITY:\s*(-?[\d.]+)", text)
    if prob_match:
        try:
            p = float(prob_match.group(1))
            result["probability"] = max(0.01, min(p, 0.99))
        except ValueError:
            pass

    # Extract CONFIDENCE
    conf_match = re.search(r"CONFIDENCE:\s*(-?[\d.]+)", text)
    if conf_match:
        try:
            c = float(conf_match.group(1))
            result["confidence"] = max(0.0, min(c, 1.0))
        except ValueError:
            pass

    # Extract REFERENCE_CLASS
    ref_match = re.search(r"REFERENCE_CLASS:\s*(.+)", text)
    if ref_match:
        result["reference_class"] = ref_match.group(1).strip()[:200]

    # Extract KEY_FACTORS
    kf_match = re.search(r"KEY_FACTORS:\s*(.+)", text)
    if kf_match:
        factors = [f.strip() for f in kf_match.group(1).split("|")]
        result["key_factors"] = [f[:200] for f in factors if f][:5]

    # Extract REVERSAL_TRIGGERS
    rt_match = re.search(r"REVERSAL_TRIGGERS:\s*(.+)", text)
    if rt_match:
        triggers = [t.strip() for t in rt_match.group(1).split("|")]
        result["reversal_triggers"] = [t[:200] for t in triggers if t][:5]

    return result


# ── Main Entry Point ──────────────────────────────────────────────

def analyze_market(
    market_id: str,
    question: str,
    description: str = "",
    market_price: float = 0.50,
    category: str = "other",
    resolution_date: str = "",
    bypass_cache: bool = False,
) -> LLMAnalysis:
    """
    Run Claude superforecaster analysis on a prediction market.

    Security:
        - API key sourced ONLY from ANTHROPIC_API_KEY env var
        - Market data sent to API is limited to public market info only
        - Response is parsed defensively (all values validated + clamped)
        - Result is cached to reduce API calls and costs
        - Rate limiting enforced before every API call

    Args:
        market_id: Market identifier (for caching, not sent to API)
        question: The prediction market question
        description: Market description / resolution criteria
        market_price: Current market-implied probability
        category: Market category
        resolution_date: When the market resolves
        bypass_cache: Force fresh analysis (skip cache)

    Returns:
        LLMAnalysis with probability estimate and reasoning.

    Raises:
        RuntimeError: If API key is missing or API call fails after retries.
    """
    strategy = _load_strategy()
    fc = strategy.get("forecasting", {})
    cache_ttl = fc.get("llm_cache_ttl_minutes", 60)
    model = fc.get("llm_model", "claude-sonnet-4-6")
    max_tokens = fc.get("max_tokens_per_analysis", 2000)

    # Check cache first
    if not bypass_cache:
        key = _cache_key(market_id, question)
        cached = _cache_get(key, cache_ttl)
        if cached is not None:
            log_event("llm_analyst", "cache_hit", {
                "market_id": market_id,
                "probability": cached.probability,
            }, result="success")
            return cached

    # Load API key from environment ONLY — never from config files
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log_event("llm_analyst", "missing_api_key", {
            "market_id": market_id,
        }, result="failed")
        raise RuntimeError(
            "ANTHROPIC_API_KEY not set. Set it in your environment: "
            "export ANTHROPIC_API_KEY=sk-ant-..."
        )

    # Build prompt
    prompt = _build_prompt(question, description, market_price, category, resolution_date)

    # Rate limit
    _get_limiter().wait()

    # Call Claude API with retries
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    last_error = None

    for attempt in range(3):
        try:
            log_event("llm_analyst", "api_call_start", {
                "market_id": market_id,
                "model": model,
                "attempt": attempt + 1,
            }, result="pending")

            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )

            raw_text = response.content[0].text if response.content else ""

            if not raw_text:
                raise ValueError("Empty response from API")

            break

        except anthropic.RateLimitError:
            # Back off exponentially
            wait = min(2 ** (attempt + 1), 30)
            log_event("llm_analyst", "rate_limited", {
                "market_id": market_id,
                "wait_seconds": wait,
                "attempt": attempt + 1,
            }, result="retrying")
            time.sleep(wait)
            last_error = "Rate limited by Anthropic API"
            continue

        except anthropic.APIError as e:
            last_error = f"API error: {type(e).__name__}"
            log_event("llm_analyst", "api_error", {
                "market_id": market_id,
                "error_type": type(e).__name__,
                "attempt": attempt + 1,
            }, result="failed")
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"Claude API failed after 3 attempts: {last_error}")

    else:
        raise RuntimeError(f"Claude API failed after 3 attempts: {last_error}")

    # Parse response (treat as untrusted)
    parsed = _parse_response(raw_text)

    # Extract reasoning (everything before the structured output block)
    reasoning = raw_text
    output_start = raw_text.find("PROBABILITY:")
    if output_start > 0:
        reasoning = raw_text[:output_start].strip()

    analysis = LLMAnalysis(
        probability=parsed["probability"],
        confidence=parsed["confidence"],
        reference_class=parsed["reference_class"],
        key_factors=parsed["key_factors"],
        reasoning=reasoning[:5000],  # Cap reasoning length
        reversal_triggers=parsed["reversal_triggers"],
        raw_response=raw_text[:8000],  # Cap raw response for audit
    )

    # Cache the result
    key = _cache_key(market_id, question)
    _cache_put(key, analysis)

    log_event("llm_analyst", "analysis_complete", {
        "market_id": market_id,
        "probability": analysis.probability,
        "confidence": analysis.confidence,
        "n_factors": len(analysis.key_factors),
        "model": model,
    }, result="success")

    return analysis


def clear_cache():
    """Clear all cached LLM analyses. Used by Hermes after weight changes."""
    if CACHE_DIR.exists():
        for f in CACHE_DIR.glob("*.json"):
            f.unlink()
        log_event("llm_analyst", "cache_cleared", {}, result="success")
