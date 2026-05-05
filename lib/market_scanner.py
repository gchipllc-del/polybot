"""
Market Scanner — discovers, scores, and ranks trading opportunities.

This is the brain that decides WHAT to look at. The forecaster decides
what probability to assign. The scanner decides which markets are worth
forecasting and which to skip.

Pipeline:
    1. Fetch open markets from all active platforms
    2. Apply hard filters (liquidity, resolution date, price bounds, spread)
    3. Run forecaster on surviving candidates
    4. Score each candidate (evidence 0-3, calibration 0-3, edge 0-3 = composite 0-9)
    5. Detect correlated markets (don't bet the same event twice)
    6. Rank by expected value * confidence
    7. Return top candidates for the order gate

Security:
    - All market data treated as untrusted (validated on ingest)
    - Correlation detection prevents hidden concentration risk
    - Max markets per cycle caps inference costs
    - Full audit trail on every scan
"""

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from lib.audit import log_event
from lib.forecaster import ForecastResult, build_forecast_for_market
from lib.kelly import (
    ensemble_dampener,
    expected_value,
    kelly_bet_size,
    kelly_bet_size_slippage_aware,
    min_edge_for_trade,
)
from lib.market_client import MarketClient, MarketInfo

CONFIG_PATH = Path(__file__).parent.parent / "config"


def _load_strategy() -> dict:
    with open(CONFIG_PATH / "strategy.yaml", "r") as f:
        return yaml.safe_load(f)


def _load_settings() -> dict:
    with open(CONFIG_PATH / "settings.yaml", "r") as f:
        return yaml.safe_load(f)


@dataclass
class MarketCandidate:
    """A scored, ranked trading opportunity ready for the order gate."""
    market: MarketInfo
    forecast: ForecastResult
    rank: int = 0                   # Position in ranked list (1 = best)
    kelly_bet_usd: float = 0.0     # Dollar amount to bet (slippage + dampener applied)
    naive_kelly_usd: float = 0.0   # What naive Kelly would have said (pre-slippage)
    effective_price: float = 0.0   # Price we'd actually fill at
    slippage_pct: float = 0.0      # (effective - market) / market
    edge_post_slip: float = 0.0    # Edge after accounting for slippage
    correlation_group: str = ""    # Markets in same group are correlated
    skip_reason: str = ""          # If filtered out, why
    time_bonus: float = 1.0        # Gaussian resolution-window multiplier (1.0 = peak)
    # Ensemble-disagreement dampener. 1.0 = no dampening (providers agree),
    # 0.5 (default floor) = max dampening applied. The pre-dampener bet is
    # recorded here so the UI can show "we cut this from $X to $Y because
    # our models disagreed by Z%."
    disagreement_dampener: float = 1.0
    pre_dampener_kelly_usd: float = 0.0


# ── Hard Filters ──────────────────────────────────────────────────

def _passes_filters(market: MarketInfo, filters: dict, strategy: dict) -> tuple[bool, str]:
    """
    Apply hard filters to decide if a market is worth forecasting.

    Returns (passes: bool, reason: str).
    Reason is empty on pass, describes failure on reject.

    All thresholds come from strategy.yaml — no magic numbers.
    """
    # Liquidity check
    min_liq = filters.get("min_liquidity", 10000)
    if market.total_volume < min_liq:
        return False, f"low_liquidity:{market.total_volume:.0f}<{min_liq}"

    # 24h volume check
    min_vol = filters.get("min_volume_24h", 500)
    if market.volume_24h < min_vol:
        return False, f"low_24h_volume:{market.volume_24h:.0f}<{min_vol}"

    # Price bounds — avoid extreme odds (near 0 or 1)
    bounds = filters.get("price_bounds", [0.10, 0.90])
    if market.yes_price < bounds[0] or market.yes_price > bounds[1]:
        return False, f"price_out_of_bounds:{market.yes_price:.2f}"

    # Resolution date check
    markets_cfg = strategy.get("markets", {})
    min_days = markets_cfg.get("min_resolution_days", 1)
    max_days = markets_cfg.get("max_resolution_days", 90)

    if market.resolution_date:
        try:
            res_str = market.resolution_date
            if isinstance(res_str, str):
                res_date = datetime.fromisoformat(res_str.replace("Z", "+00:00"))
            else:
                res_date = datetime.fromtimestamp(res_str / 1000, tz=timezone.utc)

            now = datetime.now(timezone.utc)
            days_to_res = (res_date - now).total_seconds() / 86400

            # Min hours check (from filters)
            min_hours = filters.get("min_hours_to_resolution", 48)
            if days_to_res * 24 < min_hours:
                return False, f"resolves_too_soon:{days_to_res:.1f}d"

            if days_to_res > max_days:
                return False, f"resolves_too_late:{days_to_res:.0f}d>{max_days}d"

        except (ValueError, TypeError, OSError):
            pass  # Can't parse date — don't filter on it

    # Category avoidance
    avoid = markets_cfg.get("avoid_categories", [])
    if market.category and market.category.lower() in [c.lower() for c in avoid]:
        return False, f"avoided_category:{market.category}"

    return True, ""


def _estimate_spread(market: MarketInfo) -> float:
    """
    Estimate bid-ask spread from YES/NO prices.

    In efficient markets: yes + no ≈ 1.00. The overround is the spread.
    """
    if market.yes_price > 0 and market.no_price > 0:
        overround = (market.yes_price + market.no_price) - 1.0
        return max(overround, 0.0)
    return 0.0


def _days_to_resolution(resolution_date) -> float | None:
    """
    Compute days from now to resolution, accepting either ISO 8601 string
    or epoch-ms (int/float). Returns None if unparseable.
    """
    if not resolution_date:
        return None
    try:
        if isinstance(resolution_date, str):
            res_dt = datetime.fromisoformat(resolution_date.replace("Z", "+00:00"))
        elif isinstance(resolution_date, (int, float)):
            res_dt = datetime.fromtimestamp(resolution_date / 1000, tz=timezone.utc)
        else:
            return None
        delta = res_dt - datetime.now(timezone.utc)
        return delta.total_seconds() / 86400.0
    except (ValueError, TypeError, OSError):
        return None


def _resolution_bonus(days: float | None, markets_cfg: dict) -> float:
    """
    Gaussian score multiplier based on days-to-resolution.

    Biases scoring toward the preferred resolution window so short-dated
    bets earn full credit while long-dated ones (within the hard cap)
    take a graded penalty. Prediction-market capital stuck in a 60-day
    bet is dead money for compounding — this penalty rewards velocity.

    Returns a value in [1 - weight, 1.0]:
        - 1.0 at the preferred window (no penalty)
        - decays to `1 - weight` as days move away (Gaussian)

    Example with preferred=10, decay=14, weight=0.15:
        days=10  -> 1.00   (no penalty — sweet spot)
        days=24  -> 0.92   (~8% penalty at one sigma)
        days=30  -> 0.87   (13% penalty near the 30d hard cap)
        days>50  -> 0.85   (floor = 1 - weight)
    """
    if days is None or days <= 0:
        return 1.0  # Neutral when unknown — don't punish missing metadata
    preferred = markets_cfg.get("preferred_resolution_days", 10)
    decay = max(1.0, float(markets_cfg.get("decay_resolution_days", 14)))
    weight = max(0.0, min(float(markets_cfg.get("resolution_score_weight", 0.15)), 0.5))

    gauss = math.exp(-((float(days) - float(preferred)) / decay) ** 2)
    return (1.0 - weight) + weight * gauss


def _niche_volume_bonus(volume_24h: float, markets_cfg: dict) -> float:
    """
    Wave D: niche-market scoring bonus (polymarket-pipeline pattern).

    Biases candidate ranking toward the inefficient-edge sweet spot — markets
    with enough liquidity to be tradeable but small enough that sophisticated
    bots aren't already dominating. polymarket-pipeline's research found
    sub-$500K-volume markets had measurably more mispricing than the high-
    volume "whale" markets where edge has been arb'd away.

    Gaussian centered on `niche_preferred_volume` (default $50K), decaying
    toward `niche_floor_score` for both very-thin and very-fat markets.

    Returns a value in [niche_floor_score, 1.0]:
      - 1.0 at the sweet spot (full credit)
      - decays smoothly toward the floor in both directions (log-space, so
        the curve is symmetric in volume orders-of-magnitude, not dollars)

    Example with preferred=50_000, decay_octaves=1.5, floor=0.85:
      vol=$1k        -> ~0.86  (too thin, near floor)
      vol=$10k       -> ~0.95  (climbing — niche territory)
      vol=$50k       -> 1.00   (sweet spot)
      vol=$500k      -> ~0.92  (sophisticated bots present)
      vol=$5M        -> ~0.86  (whale market, near floor)

    Returns 1.0 (neutral) if volume is unknown or non-positive.
    """
    if volume_24h is None or volume_24h <= 0:
        return 1.0
    preferred = max(1.0, float(markets_cfg.get("niche_preferred_volume", 50_000)))
    decay_oct = max(0.1, float(markets_cfg.get("niche_decay_octaves", 1.5)))
    floor = max(0.0, min(float(markets_cfg.get("niche_floor_score", 0.85)), 1.0))
    weight = 1.0 - floor

    # Octave distance — symmetric in log-space so $5K vs $500K (each ~10x
    # off the $50K center) gets the same penalty.
    octaves = math.log2(float(volume_24h) / preferred)
    gauss = math.exp(-(octaves / decay_oct) ** 2)
    return floor + weight * gauss


# ── Correlation Detection ─────────────────────────────────────────

_STOPWORDS = frozenset({
    "a", "an", "the", "will", "is", "are", "be", "been", "was", "were",
    "in", "on", "at", "to", "for", "of", "and", "or", "by", "with",
    "this", "that", "these", "those", "it", "its", "than", "more", "less",
    "who", "what", "when", "where", "why", "how", "which",
    "there", "any", "if", "but", "before", "after", "during",
    "would", "could", "should", "may", "might", "can",
})


def _normalize_text(text: str) -> str:
    """Normalize text for comparison: lowercase, strip punctuation and
    single-digit tokens (which make unrelated markets look similar)."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _content_tokens(text: str) -> set[str]:
    """Return content words (stopwords removed) from a question string."""
    return {
        w for w in _normalize_text(text).split()
        if w not in _STOPWORDS and len(w) > 2
    }


def detect_correlated_markets(candidates: list[MarketCandidate]) -> list[MarketCandidate]:
    """
    Detect correlated markets to prevent hidden concentration risk.

    Two markets are considered correlated if:
    1. They share the same series/event on the same platform (e.g., "Who wins X?" outcomes)
    2. Their questions have high text overlap (>60% shared words)

    Correlated markets get the same correlation_group tag.
    The scanner should only trade the best one per group.
    """
    if len(candidates) < 2:
        return candidates

    # Group by platform event keys
    event_groups: dict[str, list[int]] = {}
    for i, c in enumerate(candidates):
        # Check platform-specific event grouping
        event_key = (
            c.market.extra.get("series_ticker", "")
            or c.market.extra.get("condition_id", "")
            or c.market.extra.get("group_id", "")
        )
        if event_key:
            full_key = f"{c.market.platform}:{event_key}"
            event_groups.setdefault(full_key, []).append(i)

    # Tag event-based correlations
    group_id = 0
    for key, indices in event_groups.items():
        if len(indices) > 1:
            group_id += 1
            for idx in indices:
                candidates[idx].correlation_group = f"event_{group_id}"

    # Text similarity for remaining ungrouped candidates
    ungrouped = [i for i, c in enumerate(candidates) if not c.correlation_group]

    # Precompute content-token sets (stopwords filtered) to make similarity
    # robust to filler words. Also gather rare-proper-noun sets (tokens that
    # appear in ≤ 2 questions) — shared rare tokens are a strong correlation
    # signal even when overall overlap is low.
    token_sets = [
        _content_tokens(candidates[idx].market.question) for idx in ungrouped
    ]
    token_freq: dict[str, int] = {}
    for ts in token_sets:
        for t in ts:
            token_freq[t] = token_freq.get(t, 0) + 1
    rare_tokens = {t for t, n in token_freq.items() if n <= 2}

    for i in range(len(ungrouped)):
        if candidates[ungrouped[i]].correlation_group:
            continue

        words_i = token_sets[i]
        if not words_i:
            continue

        for j in range(i + 1, len(ungrouped)):
            if candidates[ungrouped[j]].correlation_group:
                continue

            words_j = token_sets[j]
            if not words_j:
                continue

            intersection = words_i & words_j
            if not intersection:
                continue

            # Jaccard similarity
            union = words_i | words_j
            jaccard = len(intersection) / len(union)

            # Szymkiewicz–Simpson overlap: catches subset relations
            # ("Will Fed cut rates in June" ⊂ "Will Fed cut rates in June meeting?")
            overlap = len(intersection) / min(len(words_i), len(words_j))

            # Shared rare tokens (proper nouns, specific entities)
            shared_rare = intersection & rare_tokens

            correlated = (
                jaccard > 0.55
                or overlap > 0.80
                or len(shared_rare) >= 2  # Two distinctive entities in common
            )

            if correlated:
                group_id += 1
                tag = f"text_{group_id}"
                candidates[ungrouped[i]].correlation_group = tag
                candidates[ungrouped[j]].correlation_group = tag

    return candidates


def _deduplicate_correlated(
    candidates: list[MarketCandidate],
) -> list[MarketCandidate]:
    """
    From each correlation group, keep only the best candidate (highest EV).

    The others get tagged with skip_reason but stay in the list for audit.
    """
    groups: dict[str, list[MarketCandidate]] = {}

    for c in candidates:
        if c.correlation_group:
            groups.setdefault(c.correlation_group, []).append(c)

    for group_tag, group_members in groups.items():
        # Sort by expected value descending
        group_members.sort(key=lambda x: x.forecast.expected_value, reverse=True)

        # Mark duplicates
        for duplicate in group_members[1:]:
            duplicate.skip_reason = f"correlated_with:{group_members[0].market.market_id}"

    return candidates


# ── Main Entry Points ─────────────────────────────────────────────

def scan_all_markets(
    clients: list[MarketClient],
    llm_enabled: bool = True,
    bankroll: float = 50.0,
) -> list[MarketCandidate]:
    """
    Full scan pipeline: fetch → filter → forecast → score → rank.

    This is the main entry point called by `main.py scan`.

    Args:
        clients: Active MarketClient instances
        llm_enabled: Whether to call Claude API (disable for fast scans)
        bankroll: Current bankroll for Kelly sizing

    Returns:
        List of MarketCandidates, ranked by expected value.
        Includes filtered-out candidates with skip_reason set (for audit).
    """
    strategy = _load_strategy()
    settings = _load_settings()
    filters = strategy.get("market_filters", {})
    max_per_cycle = filters.get("max_markets_per_cycle", 20)
    scoring = strategy.get("scoring", {})
    min_edge = scoring.get("min_edge", 0.08)
    min_score = scoring.get("min_composite_score", 6)

    log_event("scanner", "scan_started", {
        "platforms": [c.platform_name for c in clients],
        "llm_enabled": llm_enabled,
        "bankroll": bankroll,
    }, result="pending")

    # ── Step 1: Fetch all open markets ────────────────────────────
    all_markets: list[MarketInfo] = []
    for client in clients:
        try:
            markets = client.get_markets(status="open", limit=200)
            all_markets.extend(markets)
        except Exception as e:
            log_event("scanner", "fetch_failed", {
                "platform": client.platform_name,
                "error": str(e)[:200],
            }, result="failed")

    if not all_markets:
        log_event("scanner", "no_markets_found", {}, result="failed")
        return []

    # ── Step 2: Hard filters ──────────────────────────────────────
    passed: list[MarketInfo] = []
    filtered_out: list[MarketCandidate] = []

    for market in all_markets:
        ok, reason = _passes_filters(market, filters, strategy)
        if ok:
            # Spread check (requires both prices)
            spread = _estimate_spread(market)
            max_spread = filters.get("max_spread_pct", 0.04)
            if spread > max_spread:
                filtered_out.append(MarketCandidate(
                    market=market,
                    forecast=ForecastResult(
                        market_id=market.market_id, platform=market.platform,
                        probability=0, confidence=0, market_probability=market.yes_price, edge=0,
                    ),
                    skip_reason=f"spread_too_wide:{spread:.2%}>{max_spread:.2%}",
                ))
            else:
                passed.append(market)
        else:
            filtered_out.append(MarketCandidate(
                market=market,
                forecast=ForecastResult(
                    market_id=market.market_id, platform=market.platform,
                    probability=0, confidence=0, market_probability=market.yes_price, edge=0,
                ),
                skip_reason=reason,
            ))

    log_event("scanner", "filters_applied", {
        "total_markets": len(all_markets),
        "passed_filters": len(passed),
        "filtered_out": len(filtered_out),
    }, result="success")

    # Cap inference costs — pick top candidates by NICHE-AWARE priority.
    # Wave D: pure volume-DESC sorting was selecting whale markets where
    # sophisticated bots have already arb'd the edge away. Now we rank by
    # `_niche_volume_bonus(volume_24h)` × log10(volume) so we still prefer
    # liquid markets but bias the chosen set toward the $50K sweet spot.
    # polymarket-pipeline research: sub-$500K markets have measurably more
    # mispricing than the headline-volume tier.
    def _candidate_priority(m) -> float:
        vol = max(1.0, float(m.volume_24h or 0))
        return _niche_volume_bonus(vol, markets_cfg) * math.log10(vol)

    passed.sort(key=_candidate_priority, reverse=True)
    to_forecast = passed[:max_per_cycle]

    # ── Step 3: Forecast each candidate ───────────────────────────
    candidates: list[MarketCandidate] = []

    for market in to_forecast:
        try:
            # Run the full forecast pipeline (news → LLM ensemble →
            # Metaculus → Kronos → smart money → Bayesian aggregation).
            # The same orchestrator is used by the monitor for reforecasting
            # open positions, so entry and exit see the same evidence model.
            forecast = build_forecast_for_market(
                market=market,
                strategy=strategy,
                llm_enabled=llm_enabled,
            )

            # Calculate slippage-aware Kelly bet size.
            # Naive Kelly assumes you fill at top-of-book; thin books punish
            # that assumption hard (a narrow edge can be eaten entirely by
            # slippage). Fixed-point iteration re-sizes the bet so it's
            # priced against the price *after* its own impact.
            bet_usd = 0.0
            naive_bet = 0.0
            effective_price = market.yes_price
            slippage_pct = 0.0
            edge_post_slip = forecast.edge

            dampener = 1.0
            pre_dampener_bet = 0.0

            if forecast.edge > 0 and forecast.best_side:
                trade_prob = forecast.probability if forecast.best_side == "YES" else (1.0 - forecast.probability)
                trade_market = market.yes_price if forecast.best_side == "YES" else (1.0 - market.yes_price)
                market_type = "CPMM" if market.platform.lower() == "manifold" else "CLOB"

                slip_result = kelly_bet_size_slippage_aware(
                    bankroll=bankroll,
                    our_prob=trade_prob,
                    market_prob=trade_market,
                    volume_24h=market.volume_24h,
                    market_type=market_type,
                )
                bet_usd = slip_result["bet_usd"]
                naive_bet = slip_result["naive_kelly_usd"]
                effective_price = slip_result["effective_price"]
                slippage_pct = slip_result["slippage_pct"]
                edge_post_slip = slip_result["edge_post_slip"]

                # Audit trail — every slippage calc is inspectable
                forecast.bayesian_chain.append({
                    "step": "slippage_sizing",
                    "market_type": market_type,
                    "volume_24h": market.volume_24h,
                    "naive_kelly_usd": naive_bet,
                    "sized_kelly_usd": bet_usd,
                    "effective_price": effective_price,
                    "slippage_pct": slippage_pct,
                    "edge_post_slip": edge_post_slip,
                    "converged": slip_result["converged"],
                    "iterations": slip_result["iterations"],
                })

                # ── Ensemble-Disagreement Dampener ──────────────────
                # When the LLM providers disagreed on this market (e.g.,
                # Claude 70%, DeepSeek 40%), the ensemble spread is a
                # revealed "I don't know" signal across independent
                # calibrations — stronger than any single model's
                # self-reported confidence. Size down the Kelly bet
                # proportionally so we pay less for disagreement.
                disagreement_cfg = (
                    strategy.get("forecasting", {}).get("disagreement", {}) or {}
                )
                if disagreement_cfg.get("enabled", True) and forecast.llm_spread > 0:
                    dampener = ensemble_dampener(
                        spread=forecast.llm_spread,
                        mild_threshold=disagreement_cfg.get("mild_threshold", 0.10),
                        strong_threshold=disagreement_cfg.get("strong_threshold", 0.20),
                        floor=disagreement_cfg.get("kelly_floor", 0.5),
                    )
                    pre_dampener_bet = bet_usd
                    bet_usd = bet_usd * dampener

                    # Audit: always record the decision, even when the
                    # dampener passes through at 1.0 (for observability
                    # of why a bet was NOT cut).
                    forecast.bayesian_chain.append({
                        "step": "disagreement_dampener",
                        "llm_spread": round(forecast.llm_spread, 4),
                        "dampener": round(dampener, 4),
                        "pre_dampener_usd": round(pre_dampener_bet, 2),
                        "post_dampener_usd": round(bet_usd, 2),
                    })

            candidate = MarketCandidate(
                market=market,
                forecast=forecast,
                kelly_bet_usd=round(bet_usd, 2),
                naive_kelly_usd=round(naive_bet, 2),
                effective_price=round(effective_price, 4),
                slippage_pct=round(slippage_pct, 4),
                edge_post_slip=round(edge_post_slip, 4),
                disagreement_dampener=round(dampener, 4),
                pre_dampener_kelly_usd=round(pre_dampener_bet, 2),
            )

            # --- Compute time-window bonus for ranking ---
            # The Gaussian favors markets in the preferred-resolution sweet
            # spot (gentle — it's a ranking signal, not a gate). The raw
            # composite_score gate below keeps the integer quality bar
            # intact; the bonus just nudges closer-to-preferred bets up in
            # the rankings. See `_resolution_bonus` for the math.
            markets_cfg = strategy.get("markets", {})
            days_to_res = _days_to_resolution(market.resolution_date)
            time_bonus = _resolution_bonus(days_to_res, markets_cfg)
            # Stash on the candidate so ranking can pick it up later
            candidate.time_bonus = time_bonus

            # --- Adverse-selection check ---
            # If the edge is extreme (>40% by default) but conviction is
            # below 9/9, the market probably knows something we don't.
            # Prediction markets are usually right about large edges.
            adverse_edge = filters.get("adverse_selection_edge", 0.40)

            if forecast.composite_score < min_score:
                candidate.skip_reason = (
                    f"low_score:{forecast.composite_score}/9<{min_score}"
                )
            elif abs(forecast.edge) < min_edge:
                candidate.skip_reason = f"low_edge:{forecast.edge:.2%}<{min_edge:.0%}"
            elif abs(forecast.edge) > adverse_edge and forecast.composite_score < 9:
                candidate.skip_reason = (
                    f"adverse_selection_suspect:edge={forecast.edge:.0%}"
                    f">{adverse_edge:.0%}_requires_9/9_conviction"
                    f"(got_{forecast.composite_score}/9)"
                )
            elif bet_usd > 0 and edge_post_slip < min_edge:
                candidate.skip_reason = (
                    f"slippage_eats_edge:{edge_post_slip:.2%}<{min_edge:.0%}"
                    f"(slip={slippage_pct:.1%})"
                )

            # Stash the adjustment for transparency in audit/UI
            forecast.bayesian_chain.append({
                "step": "time_window_bonus",
                "days_to_resolution": days_to_res,
                "raw_composite_score": forecast.composite_score,
                "time_bonus": round(time_bonus, 4),
            })

            candidates.append(candidate)

        except Exception as e:
            log_event("scanner", "forecast_error", {
                "market_id": market.market_id,
                "error": str(e)[:200],
            }, result="failed")

    # ── Step 4: Detect correlations ───────────────────────────────
    tradeable = [c for c in candidates if not c.skip_reason]
    if tradeable:
        tradeable = detect_correlated_markets(tradeable)
        tradeable = _deduplicate_correlated(tradeable)

    # ── Step 4b: Per-category concentration cap ───────────────────
    # Prevent 4 simultaneous "election" bets correlating into one loss.
    # We keep the highest-EV candidate per category up to the cap and
    # mark the rest with a transparent skip_reason so they show up in
    # the scan report as "would-have-been" trades.
    max_per_cat = filters.get("max_positions_same_category", 2)
    if tradeable and max_per_cat:
        # Sort by EV so the best N per category survive the cap
        tradeable.sort(
            key=lambda c: c.forecast.expected_value,
            reverse=True,
        )
        by_cat: dict[str, int] = {}
        for c in tradeable:
            if c.skip_reason:
                continue
            cat = (c.market.category or "other").lower()
            if by_cat.get(cat, 0) >= max_per_cat:
                c.skip_reason = (
                    f"category_cap:{cat}_has_{by_cat[cat]}>={max_per_cat}"
                )
                continue
            by_cat[cat] = by_cat.get(cat, 0) + 1

    # ── Step 5: Rank by capital efficiency ───────────────────────────
    # The old ranking was EV × confidence, which equal-weighted a 5-day
    # bet and a 74-day bet. A $64 position held 74 days is $0.86/day of
    # capital use; a $157 position held 5 days is $31.40/day. To compound
    # $50 → $25k we must rank by EV per day of capital locked up.
    #
    # capital_efficiency = (EV × confidence × time_bonus) / max(1, days)
    # - time_bonus is the Gaussian peak at preferred-resolution-days, so
    #   very-imminent (<3d) and near-cap (>25d) bets get a gentle ranking
    #   penalty on top of the /days divisor
    # - Markets with unknown resolution dates default to 30 days (neutral)
    def _capital_efficiency(c: MarketCandidate) -> float:
        days = _days_to_resolution(c.market.resolution_date) or 30.0
        days = max(1.0, days)
        bonus = getattr(c, "time_bonus", 1.0) or 1.0
        return (c.forecast.expected_value * c.forecast.confidence * bonus) / days

    tradeable_clean = [c for c in tradeable if not c.skip_reason]
    # Stash the metric on the candidate for transparency in the UI/audit
    for c in tradeable_clean:
        c.forecast.bayesian_chain.append({
            "step": "capital_efficiency",
            "days_to_resolution": _days_to_resolution(c.market.resolution_date),
            "ev_per_day": round(_capital_efficiency(c), 6),
        })

    tradeable_clean.sort(key=_capital_efficiency, reverse=True)

    for i, c in enumerate(tradeable_clean):
        c.rank = i + 1

    # Combine all results (tradeable first, then filtered/skipped for audit)
    all_candidates = tradeable_clean + [c for c in tradeable if c.skip_reason]
    all_candidates.extend([c for c in candidates if c.skip_reason])
    all_candidates.extend(filtered_out)

    log_event("scanner", "scan_complete", {
        "total_markets": len(all_markets),
        "passed_filters": len(passed),
        "forecasted": len(candidates),
        "tradeable": len(tradeable_clean),
        "top_ev": tradeable_clean[0].forecast.expected_value if tradeable_clean else 0,
    }, result="success")

    return all_candidates


def _get_fee_rate(platform: str) -> float:
    """Get platform fee rate. Hardcoded as safety fallback."""
    rates = {
        "kalshi": 0.07,
        "polymarket": 0.02,
        "manifold": 0.0,
    }
    return rates.get(platform.lower(), 0.07)  # Default to highest fee


def get_top_candidates(
    candidates: list[MarketCandidate],
    max_positions: int | None = None,
) -> list[MarketCandidate]:
    """
    Return the top N tradeable candidates.

    Respects growth phase max_concurrent_positions from strategy.yaml.
    """
    if max_positions is None:
        strategy = _load_strategy()
        max_positions = strategy.get("growth", {}).get("max_concurrent_positions", 3)

    tradeable = [c for c in candidates if not c.skip_reason and c.rank > 0]
    tradeable.sort(key=lambda c: c.rank)
    return tradeable[:max_positions]


def print_scan_report(candidates: list[MarketCandidate]):
    """Print a formatted scan report to terminal."""
    tradeable = [c for c in candidates if not c.skip_reason and c.rank > 0]
    skipped = [c for c in candidates if c.skip_reason]

    print("=" * 70)
    print("  POLYBOT MARKET SCAN")
    print("=" * 70)

    if tradeable:
        print(f"\n  Tradeable Opportunities ({len(tradeable)}):")
        print(f"  {'#':>3} {'Score':>5} {'Edge':>6} {'EV':>6} {'Kelly$':>7} {'Side':>4} {'Platform':>10} | Question")
        print(f"  {'-'*3} {'-'*5} {'-'*6} {'-'*6} {'-'*7} {'-'*4} {'-'*10}-+-{'-'*40}")
        for c in tradeable:
            f = c.forecast
            q = c.market.question[:40]
            print(f"  {c.rank:>3} {f.composite_score:>3}/9 {f.edge:>+5.1%} "
                  f"${f.expected_value:>5.3f} ${c.kelly_bet_usd:>6.2f} "
                  f"{f.best_side:>4} {c.market.platform:>10} | {q}")
    else:
        print("\n  No tradeable opportunities found.")

    if skipped:
        # Summarize skip reasons
        reasons: dict[str, int] = {}
        for c in skipped:
            reason_type = c.skip_reason.split(":")[0]
            reasons[reason_type] = reasons.get(reason_type, 0) + 1

        print(f"\n  Filtered Out ({len(skipped)}):")
        for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
            print(f"    {reason}: {count}")

    print("=" * 70)
