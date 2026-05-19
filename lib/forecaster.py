"""
Bayesian Forecasting Engine — the core differentiator.

Takes a market, gathers evidence from multiple sources, applies Bayesian
updating to produce a probability estimate. This estimate is what we bet on.

Pipeline:
    1. Start with base rate prior (category-level historical frequency)
    2. Bayesian update with LLM superforecaster analysis
    3. Bayesian update with Metaculus community forecasts (when available)
    4. Blend in news sentiment signal
    5. Light anchor toward market consensus (the market is usually ~right)
    6. Score the result: evidence quality (0-3), calibration (0-3), edge (0-3)

The output ForecastResult drives everything downstream:
    - Kelly fraction for position sizing
    - Composite score for order gate validation
    - Calibration tracking for Hermes tuning
"""

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from tradingcore.audit import log_event
from lib.base_rates import get_base_rate
from tradingcore.calibration import brier_score, source_accuracy
from tradingcore.kelly import expected_value, fractional_kelly, min_edge_for_trade
from lib.market_client import MarketInfo

CONFIG_PATH = Path(__file__).parent.parent / "config" / "strategy.yaml"


def _load_strategy() -> dict:
    with open(CONFIG_PATH, "r") as f:
        return yaml.safe_load(f)


@dataclass
class ForecastResult:
    """Output of the forecasting engine for a single market."""
    market_id: str
    platform: str
    probability: float              # Our final estimate (0.0 - 1.0)
    confidence: float               # How confident we are in the estimate (0.0 - 1.0)
    market_probability: float       # What the market says
    edge: float                     # probability - market_probability (for YES side)
    sources: dict[str, float] = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=dict)
    evidence_score: int = 0         # 0-3
    calibration_score: int = 0      # 0-3
    edge_score: int = 0             # 0-3
    composite_score: int = 0        # 0-9
    kelly_fraction: float = 0.0
    expected_value: float = 0.0
    best_side: str = "YES"          # Which side to trade
    evidence_summary: str = ""
    bayesian_chain: list[dict] = field(default_factory=list)
    # LLM ensemble spread (max-min across providers) — feeds the Kelly
    # dampener. 0.0 = one provider or perfect agreement, 1.0 = total
    # disagreement. Defaults to 0.0 so legacy callers that skip the LLM
    # path don't accidentally trigger any dampening.
    llm_spread: float = 0.0


# ── Bayesian Math ─────────────────────────────────────────────────

import math


def bayesian_update(prior: float, source_estimate: float, base_rate: float = 0.5) -> float:
    """
    Update a prior probability given an independent source's estimate, using
    the odds-ratio form of Bayes' theorem.

        posterior_odds = prior_odds × likelihood_ratio

    where `likelihood_ratio = odds(source_estimate) / odds(base_rate)`.

    Intuition:
        - If the source agrees with the base rate, no update (LR=1).
        - If the source says 80% when base is 50%, LR = (4/1) / (1/1) = 4:
          prior odds multiply by 4.
        - Symmetric: if a second source agrees with the first, the effect
          compounds multiplicatively — correct for independent evidence.

    This replaces an earlier pseudo-Bayesian form that treated the source
    estimate directly as P(E|H), which systematically under-updated.

    Args:
        prior: Current probability estimate (0.0 - 1.0)
        source_estimate: Independent source's estimate of P(H) (0.0 - 1.0)
        base_rate: Unconditional prior for the hypothesis (0.0 - 1.0)

    Returns:
        Updated probability (0.0 - 1.0), clamped to [0.01, 0.99].
    """
    prior = max(0.01, min(prior, 0.99))
    source_estimate = max(0.01, min(source_estimate, 0.99))
    base_rate = max(0.01, min(base_rate, 0.99))

    prior_odds = prior / (1.0 - prior)
    source_odds = source_estimate / (1.0 - source_estimate)
    base_odds = base_rate / (1.0 - base_rate)

    likelihood_ratio = source_odds / base_odds
    # Dampen extreme likelihood ratios so a single 99%→base-50% source can't
    # overwhelm multiple moderate signals. Caps effective update at ~20:1.
    likelihood_ratio = max(0.05, min(likelihood_ratio, 20.0))

    posterior_odds = prior_odds * likelihood_ratio
    posterior = posterior_odds / (1.0 + posterior_odds)

    return max(0.01, min(posterior, 0.99))


def logit(p: float) -> float:
    """Log-odds of a probability. Useful for geomean-of-log-odds aggregation."""
    p = max(1e-6, min(p, 1 - 1e-6))
    return math.log(p / (1.0 - p))


def inv_logit(x: float) -> float:
    """Inverse log-odds (sigmoid), converting back to probability."""
    if x > 500:  # avoid overflow
        return 1.0 - 1e-6
    if x < -500:
        return 1e-6
    return 1.0 / (1.0 + math.exp(-x))


def geomean_log_odds(estimates: dict[str, float], weights: dict[str, float]) -> float:
    """
    Aggregate probability estimates using the geometric mean of log-odds.

    This is the aggregator used in ForecastBench (Halawi 2024, NeurIPS) —
    well-behaved at extremes (unlike arithmetic mean, which is pulled toward
    0.5), invariant under YES/NO flip, and mathematically equivalent to the
    log-odds-average. The weighted form weights evidence by trust.

    Args:
        estimates: {"llm": 0.65, "metaculus": 0.58, ...}
        weights:   {"llm": 0.30, "metaculus": 0.25, ...}

    Returns:
        Aggregated probability (0.01 - 0.99).
    """
    active = {k: v for k, v in estimates.items() if k in weights and weights[k] > 0}
    if not active:
        return 0.50

    total_w = sum(weights[k] for k in active)
    if total_w <= 0:
        return 0.50

    log_odds_sum = sum(logit(estimates[k]) * (weights[k] / total_w) for k in active)
    return max(0.01, min(inv_logit(log_odds_sum), 0.99))


def trimmed_mean_weighted(
    estimates: dict[str, float],
    weights: dict[str, float],
    trim: int = 1,
) -> float:
    """
    Trim the highest `trim` and lowest `trim` samples by value, then
    weighted mean of the remaining ones (renormalized weights).

    Wave B aggregation per Halawi et al. 2024 NeurIPS "Approaching
    Human-Level Forecasting with Language Models", which compared 5
    aggregators across N≥6 samples and found trimmed mean optimal.

    Falls back to a plain weighted mean when len(samples) ≤ 2×trim
    (not enough samples to trim safely). Callers should usually use
    the `aggregate_samples` dispatcher so small ensembles route to
    weighted geomean instead of degrading here.

    Args:
        estimates: {"s0": 0.65, "s1": 0.58, ...}
        weights:   {"s0": 1.0,  "s1": 1.0,  ...} (renormalized inside)
        trim: Count to drop from each tail. Default 1 = drop top-1 + bot-1.

    Returns:
        Aggregated probability (0.01 - 0.99).
    """
    active = {k: v for k, v in estimates.items() if k in weights}
    if not active:
        return 0.50

    n = len(active)
    if n <= 2 * trim:
        # Not enough samples to trim — fall back to plain weighted mean.
        total_w = sum(weights[k] for k in active)
        if total_w <= 0:
            return 0.50
        return max(0.01, min(
            sum(active[k] * weights[k] / total_w for k in active),
            0.99,
        ))

    pairs = sorted(
        ((active[k], weights[k]) for k in active),
        key=lambda pw: pw[0],
    )
    middle = pairs[trim:-trim]
    total_w = sum(w for _, w in middle)
    if total_w <= 0:
        # All-zero weights in the middle band — degenerate, return median.
        mid = middle[len(middle) // 2][0]
        return max(0.01, min(mid, 0.99))

    blended = sum(p * w / total_w for p, w in middle)
    return max(0.01, min(blended, 0.99))


def aggregate_samples(
    estimates: dict[str, float],
    weights: dict[str, float],
    method: str = "auto",
    trim: int = 1,
) -> float:
    """
    Single dispatch for ensemble aggregation. Use this from anywhere
    that combines N independent probability samples.

    Methods:
      "auto"             — trimmed_mean if N ≥ 5, else weighted_geomean
                           (preserves backward compatibility for the
                           default 3-provider × 1-sample setup)
      "weighted_geomean" — log-odds-weighted geomean (legacy default)
      "trimmed_mean"     — drop top + bottom `trim`, weighted mean of rest
      "median"           — middle sample by value (weights ignored)
      "mean"             — plain unweighted mean

    Returns:
        Aggregated probability (0.01 - 0.99).
    """
    n = len(estimates)
    if n == 0:
        return 0.50

    if method == "auto":
        method = "trimmed_mean" if n >= 5 else "weighted_geomean"

    if method == "weighted_geomean":
        return geomean_log_odds(estimates, weights)
    if method == "trimmed_mean":
        return trimmed_mean_weighted(estimates, weights, trim=trim)
    if method == "median":
        vals = sorted(estimates.values())
        mid = vals[len(vals) // 2]
        return max(0.01, min(mid, 0.99))
    if method == "mean":
        return max(0.01, min(sum(estimates.values()) / n, 0.99))

    raise ValueError(f"unknown aggregation method: {method!r}")


def weighted_blend(estimates: dict[str, float], weights: dict[str, float]) -> float:
    """
    Weighted average of probability estimates from multiple sources.

    Normalizes weights so they sum to 1.0 (handles missing sources gracefully).

    Args:
        estimates: {"llm": 0.65, "base_rate": 0.50, ...}
        weights: {"llm": 0.30, "base_rate": 0.25, ...}

    Returns:
        Blended probability (0.01 - 0.99).
    """
    active = {k: v for k, v in estimates.items() if k in weights}
    if not active:
        return 0.50

    total_weight = sum(weights[k] for k in active)
    if total_weight <= 0:
        return 0.50

    blended = sum(estimates[k] * weights[k] / total_weight for k in active)
    return max(0.01, min(blended, 0.99))


# ── Scoring ───────────────────────────────────────────────────────

def score_evidence(sources: dict[str, float]) -> int:
    """
    Score evidence quality 0-3 based on how many independent sources we have
    and whether they agree.

    0 = No sources (just base rate)
    1 = One source only (e.g., just market price)
    2 = Two+ sources that agree within 15%
    3 = Three+ sources that strongly agree within 10%
    """
    n = len(sources)
    if n == 0:
        return 0
    if n == 1:
        return 1

    probs = list(sources.values())
    spread = max(probs) - min(probs)

    if n >= 3 and spread <= 0.10:
        return 3
    if n >= 2 and spread <= 0.15:
        return 2
    return 1


def score_calibration() -> int:
    """
    Score our historical calibration quality 0-3.

    0 = No resolved forecasts yet (or Brier > 0.25 = worse than random)
    1 = Brier 0.20-0.25 (fair)
    2 = Brier 0.15-0.20 (good)
    3 = Brier < 0.15 (excellent)
    """
    bs = brier_score()
    if bs < 0:
        # No resolved forecasts — give benefit of doubt (1)
        return 1
    if bs < 0.15:
        return 3
    if bs < 0.20:
        return 2
    if bs < 0.25:
        return 1
    return 0


def score_edge(edge: float, market_prob: float, fee_rate: float) -> int:
    """
    Score edge quality 0-3.

    0 = Edge below minimum for trade (doesn't beat fees + uncertainty)
    1 = Edge meets minimum threshold
    2 = Edge is 2x minimum threshold
    3 = Edge is 3x+ minimum threshold (strong conviction)
    """
    min_e = min_edge_for_trade(market_prob, fee_rate)

    if edge < min_e:
        return 0
    if edge < min_e * 2:
        return 1
    if edge < min_e * 3:
        return 2
    return 3


# ── Main Entry Point ──────────────────────────────────────────────

def estimate_probability(
    market: MarketInfo,
    llm_estimate: float | None = None,
    metaculus_estimate: float | None = None,
    news_sentiment: float | None = None,
    kronos_estimate: float | None = None,
    smart_money_estimate: float | None = None,
    news_impact_estimate: float | None = None,
    fee_rate: float = 0.07,
    llm_spread: float = 0.0,
) -> ForecastResult:
    """
    Produce a probability estimate for a market by aggregating all sources.

    This is the central function of the bot. Everything flows from this estimate:
    - Whether we trade (edge > min_edge)
    - How much we bet (Kelly fraction)
    - What score we assign (composite 0-9)
    - How we track accuracy (calibration)

    Args:
        market: MarketInfo from any platform client
        llm_estimate: Claude superforecaster probability (from llm_analyst.py)
        metaculus_estimate: Metaculus community forecast if available
        news_sentiment: News-based probability signal
        kronos_estimate: Kronos zero-shot price model probability (for price-based markets)
        smart_money_estimate: Aggregate position flow from tracked profitable wallets
        fee_rate: Platform fee rate for edge scoring

    Returns:
        ForecastResult with probability, scores, kelly, and evidence chain.
    """
    strategy = _load_strategy()
    fc = strategy.get("forecasting", {})

    # Gather configured weights
    weights = {
        "llm": fc.get("llm_weight", 0.25),
        "base_rate": fc.get("base_rate_weight", 0.20),
        "metaculus": fc.get("metaculus_weight", 0.15),
        "news": fc.get("news_weight", 0.10),
        "kronos": fc.get("kronos_weight", 0.20),
        "smart_money": fc.get("smart_money_weight", 0.10),
        "market_consensus": fc.get("market_consensus_weight", 0.10),
        # Wave A (TauricResearch + polymarket-pipeline integration):
        # classification-not-probability signal. Weight defaults to the
        # same as legacy news_sentiment so the two are balanced.
        "news_impact": fc.get("news_impact_weight", 0.10),
    }

    # ── Step 1: Base Rate Prior ───────────────────────────────────
    # Empirical rate from lib/base_rates.py (Manifold historical), falling
    # back to curated static priors when empirical sample is insufficient.
    category = market.category.lower() if market.category else "other"
    base_rate = get_base_rate(category)

    sources: dict[str, float] = {"base_rate": base_rate}
    chain: list[dict] = [{"step": "base_rate", "value": base_rate, "source": f"category:{category}"}]
    current = base_rate

    # ── Step 2: LLM Superforecaster Update ────────────────────────
    if llm_estimate is not None:
        sources["llm"] = llm_estimate
        current = bayesian_update(current, llm_estimate, base_rate)
        chain.append({"step": "llm_update", "value": current, "llm_raw": llm_estimate})

    # ── Step 3: Metaculus Community Forecast ───────────────────────
    if metaculus_estimate is not None:
        sources["metaculus"] = metaculus_estimate
        current = bayesian_update(current, metaculus_estimate, base_rate)
        chain.append({"step": "metaculus_update", "value": current, "metaculus_raw": metaculus_estimate})

    # ── Step 4: News Sentiment Signal ─────────────────────────────
    if news_sentiment is not None:
        sources["news"] = news_sentiment
        current = bayesian_update(current, news_sentiment, base_rate)
        chain.append({"step": "news_update", "value": current, "news_raw": news_sentiment})

    # ── Step 4b: News-Impact Classification (Wave A) ──────────────
    # Complementary to sentiment: this is the LLM's question-conditioned
    # MORE_LIKELY_YES / NO / NOT_RELEVANT classification + materiality,
    # converted to a probability estimate via news_classifier. LLMs are
    # better at directional classification than calibrated probability,
    # so this signal tends to be cleaner than raw sentiment.
    if news_impact_estimate is not None:
        sources["news_impact"] = news_impact_estimate
        current = bayesian_update(current, news_impact_estimate, base_rate)
        chain.append({
            "step": "news_impact_update",
            "value": current,
            "news_impact_raw": news_impact_estimate,
        })

    # ── Step 5: Kronos Zero-Shot Price Model ───────────────────���──
    if kronos_estimate is not None:
        sources["kronos"] = kronos_estimate
        current = bayesian_update(current, kronos_estimate, base_rate)
        chain.append({"step": "kronos_update", "value": current, "kronos_raw": kronos_estimate})

    # ── Step 6: Smart Money (tracked whale wallets) ────────────────
    # Gently pulled toward, not anchored to — smart money is a signal of
    # *what informed actors think*, which is different from a calibrated
    # forecast. Low trust weight prevents single-source over-update.
    if smart_money_estimate is not None:
        sources["smart_money"] = smart_money_estimate
        current = bayesian_update(current, smart_money_estimate, base_rate)
        chain.append({
            "step": "smart_money_update",
            "value": current,
            "smart_money_raw": smart_money_estimate,
        })

    # ── Step 7: Market Consensus Anchor ───────────────────────────
    # The market is usually approximately right. Light anchor toward it.
    market_prob = market.yes_price
    sources["market_consensus"] = market_prob

    # Final blend: use geomean-of-log-odds (Halawi 2024 / ForecastBench standard).
    # This is symmetric under YES/NO flip, well-behaved at extremes, and the
    # weighted form combines sources by trust without arithmetic-mean bias
    # toward 0.5. We blend it 50/50 with the sequential Bayesian posterior:
    # the Bayesian chain captures independent-evidence compounding; the
    # geomean tempers it when the chain over-shoots on correlated sources.
    log_odds_blend = geomean_log_odds(sources, weights)
    probability = inv_logit(0.5 * logit(current) + 0.5 * logit(log_odds_blend))
    probability = max(0.01, min(probability, 0.99))

    chain.append({
        "step": "final_blend",
        "bayesian_posterior": round(current, 4),
        "log_odds_blend": round(log_odds_blend, 4),
        "final": round(probability, 4),
    })

    # ── Confidence ────────────────────────────────────────────────
    # Confidence blends three signals, each with its own trustworthiness:
    #   1. Coverage: how many *quality* sources weighed in (not a flat count)
    #   2. Agreement: how tight the distribution of estimates is
    #   3. Historical calibration: past Brier score against ground truth
    #
    # Each source has a trust weight reflecting how noisy it is. News
    # sentiment is noisier than Metaculus community forecasts; a raw LLM
    # estimate is noisier than one backed by retrieval. These weights are
    # used for coverage so adding a noisy source can't fake high confidence.
    SOURCE_TRUST = {
        "llm":              0.85,
        "metaculus":        0.95,
        "kronos":           0.70,   # Strong only on price-series markets
        "news":             0.50,   # Sentiment is a weak signal
        "smart_money":      0.75,   # Real capital at stake — strong but correlated with market
        "base_rate":        0.60,
        "market_consensus": 0.40,   # Included for reference, not primary
    }

    quality_sources = {k: v for k, v in sources.items() if k != "market_consensus"}
    weighted_coverage = sum(SOURCE_TRUST.get(k, 0.3) for k in quality_sources)
    # Normalize: full coverage = ~3 quality sources at avg trust 0.7
    coverage = min(weighted_coverage / 2.1, 1.0)

    if len(quality_sources) >= 2:
        values = list(quality_sources.values())
        spread = max(values) - min(values)
        agreement = max(0.0, 1.0 - spread * 3)  # 0.33 spread -> 0 agreement
    else:
        agreement = 0.5  # Neutral — can't measure agreement with one source

    # Historical calibration — did our past forecasts match reality?
    # Brier 0 = perfect, 0.25 = random for 50/50 markets.
    try:
        bs = brier_score()
        cal_quality = max(0.0, 1.0 - (bs / 0.25)) if bs >= 0 else 0.5
    except Exception:
        cal_quality = 0.5

    confidence = 0.40 * agreement + 0.40 * coverage + 0.20 * cal_quality
    confidence = max(0.0, min(confidence, 1.0))

    # ── Edge Calculation ──────────────────────────────────────────
    # Decide best side based on where our edge is
    yes_edge = probability - market_prob
    no_edge = (1.0 - probability) - (1.0 - market_prob)  # = market_prob - probability

    if yes_edge >= no_edge:
        best_side = "YES"
        edge = yes_edge
        trade_prob = probability
        trade_market_prob = market_prob
    else:
        best_side = "NO"
        edge = no_edge
        trade_prob = 1.0 - probability
        trade_market_prob = 1.0 - market_prob

    # ── Scoring ───────────────────────────────────────────────────
    ev_score = score_evidence(sources)
    cal_score = score_calibration()
    edg_score = score_edge(abs(edge), trade_market_prob, fee_rate)
    composite = ev_score + cal_score + edg_score

    # ── Kelly Sizing ──────────────────────────────────────────────
    if edge > 0:
        kf = fractional_kelly(trade_prob, trade_market_prob)
        ev = expected_value(trade_prob, trade_market_prob, fee_rate)
    else:
        kf = 0.0
        ev = 0.0

    # ── Build Evidence Summary ────────────────────────────────────
    parts = [f"Base rate ({category}): {base_rate:.0%}"]
    if llm_estimate is not None:
        parts.append(f"LLM: {llm_estimate:.0%}")
    if metaculus_estimate is not None:
        parts.append(f"Metaculus: {metaculus_estimate:.0%}")
    if news_sentiment is not None:
        parts.append(f"News: {news_sentiment:.0%}")
    if kronos_estimate is not None:
        parts.append(f"Kronos: {kronos_estimate:.0%}")
    if smart_money_estimate is not None:
        parts.append(f"SmartMoney: {smart_money_estimate:.0%}")
    parts.append(f"Market: {market_prob:.0%}")
    parts.append(f"→ Final: {probability:.0%} ({best_side} edge: {edge:+.1%})")
    summary = " | ".join(parts)

    result = ForecastResult(
        market_id=market.market_id,
        platform=market.platform,
        probability=round(probability, 4),
        confidence=round(confidence, 4),
        market_probability=market_prob,
        edge=round(edge, 4),
        sources=sources,
        weights=weights,
        evidence_score=ev_score,
        calibration_score=cal_score,
        edge_score=edg_score,
        composite_score=composite,
        kelly_fraction=round(kf, 4),
        expected_value=round(ev, 4),
        best_side=best_side,
        evidence_summary=summary,
        bayesian_chain=chain,
        llm_spread=round(max(0.0, min(float(llm_spread), 1.0)), 4),
    )

    log_event("forecaster", "estimate_complete", {
        "market_id": market.market_id,
        "platform": market.platform,
        "probability": result.probability,
        "edge": result.edge,
        "side": result.best_side,
        "composite_score": result.composite_score,
        "sources": {k: round(v, 3) for k, v in sources.items()},
    }, result="success")

    return result


# ── Pipeline Orchestrator ─────────────────────────────────────────

# Fee rates by platform. Hardcoded as a safety fallback so a config
# typo can't silently zero-fee a real-money platform.
_FEE_RATES = {
    "kalshi": 0.07,
    "polymarket": 0.02,
    "manifold": 0.0,
}

# Categories where Kronos (a financial candle model) has signal.
# Gated to prevent it from weighing in on elections, court decisions, etc.
_PRICE_SERIES_CATEGORIES = frozenset({
    "crypto", "economics", "stocks", "markets", "finance",
})


def build_forecast_for_market(
    market: MarketInfo,
    strategy: dict | None = None,
    llm_enabled: bool = True,
) -> ForecastResult:
    """
    Orchestrate the full forecast pipeline for a single market.

    Fetches news sentiment, LLM ensemble analysis, Metaculus community
    forecast, Kronos zero-shot estimate (price-series markets only), and
    smart-money flow, then aggregates via `estimate_probability()`.

    Each source is optional — if fetching fails, it's dropped silently and
    the forecast uses whatever signals did arrive. This is the same logic
    the market scanner uses during its Phase 3 pass, extracted here so the
    monitor can reforecast open positions on the same footing that got them
    opened in the first place (no shortcuts, no stale cached probability).

    Args:
        market: Normalized market data from a MarketClient
        strategy: Strategy config. Unused here (each sub-module reads its
            own slice) but accepted for future wiring + signature symmetry.
        llm_enabled: When False, skip the LLM call. Useful for fast unit
            tests, cheap "news-only" reforecasts, or when the budget is
            tight late in a session.

    Returns:
        ForecastResult with probability, confidence, edge, scoring, Kelly.
        Never raises — any sub-source failures are swallowed internally so
        a transient network hiccup can't take down a monitoring cycle.
    """
    # News first — it feeds both the news_sentiment signal AND the LLM's
    # retrieval-augmented context. Fetching once keeps the two in sync
    # (news_feed has its own TTL cache, so this is idempotent, but we
    # skip the duplicate latency either way).
    news_result = None
    news_sentiment = None
    try:
        from lib.news_feed import get_news_sentiment
        news_result = get_news_sentiment(
            market_id=market.market_id,
            question=market.question,
            category=market.category or "other",
        )
        # Low-confidence news gets dropped — it's noise without signal.
        if getattr(news_result, "confidence", 0) > 0.1:
            news_sentiment = news_result.sentiment
    except Exception:
        pass  # News is optional — degrade gracefully

    # LLM superforecaster ensemble (Claude + DeepSeek + Kimi if configured)
    # Also captures the cross-provider spread for downstream Kelly dampening
    # — high disagreement is a better "I don't know" signal than any single
    # model's self-reported confidence.
    llm_estimate = None
    llm_spread = 0.0
    if llm_enabled:
        try:
            from tradingcore.llm_analyst import analyze_market
            analysis = analyze_market(
                market_id=market.market_id,
                question=market.question,
                description=market.description,
                market_price=market.yes_price,
                category=market.category,
                resolution_date=market.resolution_date,
                news_result=news_result,
            )
            llm_estimate = analysis.probability
            # ensemble_spread is optional on the dataclass; default 0 if absent
            llm_spread = float(getattr(analysis, "ensemble_spread", 0.0) or 0.0)
        except RuntimeError:
            pass  # No API key or provider down — forecast without LLM

    # Metaculus community forecast (calibrated crowd signal)
    metaculus_estimate = None
    try:
        from lib.metaculus_client import get_metaculus_estimate
        metaculus_estimate = get_metaculus_estimate(
            question=market.question,
            resolution_date=market.resolution_date,
        )
    except Exception:
        pass

    # Kronos zero-shot price estimate — only for price-series markets.
    # A financial candle model has no business scoring an election outcome.
    kronos_estimate = None
    if (market.category or "").lower() in _PRICE_SERIES_CATEGORIES:
        try:
            from lib.kronos_forecaster import get_kronos_estimate
            kronos_estimate = get_kronos_estimate(
                market_question=market.question,
                horizon_days=30,
            )
        except Exception:
            pass

    # Smart-money signal (tracked whale wallets) — Polymarket only.
    smart_money_estimate = None
    if market.platform.lower() == "polymarket":
        try:
            from lib.smart_money import get_smart_money_estimate
            smart_money_estimate = get_smart_money_estimate(market.market_id)
        except Exception:
            pass

    # News-impact classification (Wave A — adapted from polymarket-pipeline).
    # Skips silently if no news, no LLM provider, or weight set to 0.
    # The classifier reuses news_result articles when available so we don't
    # double-spend the news API budget.
    news_impact_estimate = None
    fc_cfg = _load_strategy().get("forecasting", {})
    if fc_cfg.get("news_impact_weight", 0.10) > 0:
        try:
            from lib.news_classifier import (
                classify_news_impact,
                classification_to_probability,
            )
            articles = []
            if news_result is not None:
                articles = list(getattr(news_result, "articles", []) or [])
            if articles:
                classification = classify_news_impact(
                    question=market.question,
                    articles=articles,
                    current_yes_price=market.yes_price,
                )
                if classification is not None:
                    threshold = float(
                        fc_cfg.get("news_impact_materiality_threshold", 0.30)
                    )
                    news_impact_estimate = classification_to_probability(
                        classification,
                        prior=market.yes_price,  # nudge from current market price
                        materiality_threshold=threshold,
                    )
        except Exception:
            pass  # News-impact is optional; degrade gracefully

    fee_rate = _FEE_RATES.get(market.platform.lower(), 0.07)

    return estimate_probability(
        market=market,
        llm_estimate=llm_estimate,
        metaculus_estimate=metaculus_estimate,
        news_sentiment=news_sentiment,
        kronos_estimate=kronos_estimate,
        smart_money_estimate=smart_money_estimate,
        news_impact_estimate=news_impact_estimate,
        fee_rate=fee_rate,
        llm_spread=llm_spread,
    )
