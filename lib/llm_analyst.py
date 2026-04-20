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

SUPERFORECASTER_PROMPT = """You are a world-class Superforecaster estimating the probability that the following prediction market question resolves YES by its resolution date. Your output will be aggregated with independent samples — optimize for calibration, not for agreeing with yourself.

MARKET QUESTION: {question}

MARKET DESCRIPTION: {description}

CURRENT MARKET PRICE: {market_price:.0%} (crowd-implied probability — you may agree or disagree)

CATEGORY: {category}

RESOLUTION DATE: {resolution_date}

Follow this 7-step protocol. Be rigorous, quantitative, and willing to disagree with the crowd when evidence warrants. Show your work.

## Step 1: Decompose the question
Restate the question in your own words. List 2-3 sub-questions whose answers determine the outcome. Clarify ambiguities (what exactly counts as YES? what counts as NO?).

## Step 2: Reference class forecasting (outside view)
Name 2-3 specific reference classes with historical base rates. For each, cite the relevant frequency — e.g., "incumbent Senators up for re-election win ~84% of the time (Ballotpedia, 2012–2024)". Take the weighted average as your OUTSIDE view probability.

## Step 3: Inside view — drivers (4-6)
List the most important factors for THIS specific market. For each:
   - Direction: pushes toward YES or NO
   - Magnitude: how strongly (weak/moderate/strong)
   - Evidence: what do we actually know?

## Step 4: Steelman the other side
Argue the strongest case for the OPPOSITE of your leaning so far. What's the best evidence against your current direction? If the steelman is strong, widen your uncertainty.

## Step 5: Check for common biases
Audit for:
   - Anchoring on market price (you disagree with the crowd too little? too much?)
   - Availability bias (recent news over-weighted?)
   - Status-quo bias (under-weighting change?)
   - Scope insensitivity (treating "by Dec 31" same as "by Mar 1"?)
Adjust if any bias is distorting.

## Step 6: Probability estimate
Combine outside view, inside view, and bias adjustments into a single probability. Be precise — do not round to multiples of 5 or 10 unless genuinely uncertain. Show the math: "Outside view 40%, inside view pushes +8%, steelman pulls -3% → final 45%".

## Step 7: Meta-uncertainty
How wide is your plausible range (e.g., 10-percentile to 90-percentile)? What single piece of new information would most change your estimate? If you'd revise >15% on one new data point, widen uncertainty now.

## OUTPUT FORMAT (REQUIRED — the aggregator parses this exactly)
End your response with this block:

PROBABILITY: [number between 0.01 and 0.99]
CONFIDENCE: [number between 0.0 and 1.0]
REFERENCE_CLASS: [one-line summary of primary reference class + base rate]
KEY_FACTORS: [factor1 | factor2 | factor3]
REVERSAL_TRIGGERS: [what info would move the estimate >15% | another trigger]"""


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

    # Ensemble settings — sample N independent reasoning paths and aggregate
    # with geomean-log-odds (ForecastBench default). 3 samples balances cost
    # with variance reduction; 1 disables ensemble for fast/cheap scans.
    n_samples = fc.get("llm_ensemble_samples", 3)
    temperature = fc.get("llm_temperature", 0.7)
    n_samples = max(1, min(int(n_samples), 5))

    import anthropic
    from lib.forecaster import geomean_log_odds

    client = anthropic.Anthropic(api_key=api_key)

    def _call_claude_once() -> str:
        """One API call with retries. Returns raw text or raises RuntimeError."""
        _get_limiter().wait()
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
                    temperature=temperature,
                    messages=[{"role": "user", "content": prompt}],
                )
                raw_text = response.content[0].text if response.content else ""
                if not raw_text:
                    raise ValueError("Empty response from API")
                return raw_text
            except anthropic.RateLimitError:
                wait = min(2 ** (attempt + 1), 30)
                log_event("llm_analyst", "rate_limited", {
                    "market_id": market_id, "wait_seconds": wait,
                    "attempt": attempt + 1,
                }, result="retrying")
                time.sleep(wait)
                last_error = "Rate limited by Anthropic API"
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
        raise RuntimeError(f"Claude API failed after 3 attempts: {last_error}")

    # Collect N independent samples
    samples: list[dict] = []
    raw_texts: list[str] = []
    for i in range(n_samples):
        try:
            raw = _call_claude_once()
            raw_texts.append(raw)
            parsed = _parse_response(raw)
            samples.append(parsed)
        except RuntimeError as e:
            # If at least one sample succeeded, continue with what we have
            if samples:
                log_event("llm_analyst", "sample_failed_partial", {
                    "market_id": market_id, "samples_completed": len(samples),
                    "error": str(e)[:200],
                }, result="degraded")
                break
            raise

    # Aggregate probabilities via geomean-log-odds (well-behaved at extremes,
    # symmetric under YES/NO flip, matches ForecastBench best practice)
    probs = [s["probability"] for s in samples]
    if len(samples) == 1:
        final_prob = probs[0]
        sample_confidence = samples[0]["confidence"]
    else:
        ensemble_estimates = {f"sample_{i}": p for i, p in enumerate(probs)}
        ensemble_weights = {f"sample_{i}": 1.0 for i in range(len(probs))}
        final_prob = geomean_log_odds(ensemble_estimates, ensemble_weights)
        # Confidence = avg self-reported conf × (1 - spread across samples)
        spread = max(probs) - min(probs)
        avg_conf = sum(s["confidence"] for s in samples) / len(samples)
        agreement_bonus = max(0.0, 1.0 - spread * 2.5)  # 0.4 spread -> 0 agreement
        sample_confidence = 0.6 * avg_conf + 0.4 * agreement_bonus

    # Pick reasoning from the sample closest to the aggregate probability
    # (median-ish — avoids outlier reasoning dominating the cached output)
    best_idx = min(range(len(samples)), key=lambda i: abs(samples[i]["probability"] - final_prob))
    best_sample = samples[best_idx]
    best_raw = raw_texts[best_idx]

    # Extract reasoning (everything before the structured output block)
    reasoning = best_raw
    output_start = best_raw.find("PROBABILITY:")
    if output_start > 0:
        reasoning = best_raw[:output_start].strip()

    # Merge reference class + key factors from all samples (dedup, cap)
    all_factors: list[str] = []
    for s in samples:
        for f in s.get("key_factors", []):
            if f not in all_factors:
                all_factors.append(f)
    all_factors = all_factors[:6]

    all_triggers: list[str] = []
    for s in samples:
        for t in s.get("reversal_triggers", []):
            if t not in all_triggers:
                all_triggers.append(t)
    all_triggers = all_triggers[:4]

    analysis = LLMAnalysis(
        probability=round(final_prob, 4),
        confidence=round(sample_confidence, 4),
        reference_class=best_sample.get("reference_class", ""),
        key_factors=all_factors,
        reasoning=reasoning[:5000],
        reversal_triggers=all_triggers,
        raw_response=f"[ensemble n={len(samples)}]\n" + best_raw[:8000],
    )

    # Log ensemble statistics for calibration analysis
    log_event("llm_analyst", "ensemble_complete", {
        "market_id": market_id,
        "n_samples": len(samples),
        "probs": [round(p, 3) for p in probs],
        "final_probability": analysis.probability,
        "sample_spread": round(max(probs) - min(probs), 4) if len(probs) > 1 else 0,
    }, result="success")

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
