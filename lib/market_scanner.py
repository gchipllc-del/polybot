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

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from lib.audit import log_event
from lib.forecaster import ForecastResult, estimate_probability
from lib.kelly import (
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
    kelly_bet_usd: float = 0.0     # Dollar amount to bet (slippage-aware)
    naive_kelly_usd: float = 0.0   # What naive Kelly would have said (pre-slippage)
    effective_price: float = 0.0   # Price we'd actually fill at
    slippage_pct: float = 0.0      # (effective - market) / market
    edge_post_slip: float = 0.0    # Edge after accounting for slippage
    correlation_group: str = ""    # Markets in same group are correlated
    skip_reason: str = ""          # If filtered out, why


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

    # Cap inference costs — only forecast top candidates by volume
    passed.sort(key=lambda m: m.volume_24h, reverse=True)
    to_forecast = passed[:max_per_cycle]

    # ── Step 3: Forecast each candidate ───────────────────────────
    candidates: list[MarketCandidate] = []

    for market in to_forecast:
        try:
            # Get LLM estimate if enabled
            llm_estimate = None
            if llm_enabled:
                try:
                    from lib.llm_analyst import analyze_market
                    analysis = analyze_market(
                        market_id=market.market_id,
                        question=market.question,
                        description=market.description,
                        market_price=market.yes_price,
                        category=market.category,
                        resolution_date=market.resolution_date,
                    )
                    llm_estimate = analysis.probability
                except RuntimeError:
                    # API key missing or API down — forecast without LLM
                    pass

            # Get news sentiment signal
            news_sentiment = None
            try:
                from lib.news_feed import get_news_sentiment
                news_result = get_news_sentiment(
                    market_id=market.market_id,
                    question=market.question,
                    category=market.category or "other",
                )
                if news_result.confidence > 0.1:
                    news_sentiment = news_result.sentiment
            except Exception:
                pass  # News is optional — degrade gracefully

            # Get Metaculus community forecast (calibrated crowd signal)
            metaculus_estimate = None
            try:
                from lib.metaculus_client import get_metaculus_estimate
                metaculus_estimate = get_metaculus_estimate(
                    question=market.question,
                    resolution_date=market.resolution_date,
                )
            except Exception:
                pass  # Metaculus is optional

            # Get Kronos zero-shot price estimate — price-series markets only.
            # Kronos is a financial candle model; it should not contribute to
            # categorical outcomes (elections, court decisions, sports, etc.).
            # Gate at category + question-parse level as defense in depth.
            kronos_estimate = None
            PRICE_SERIES_CATEGORIES = {"crypto", "economics", "stocks", "markets", "finance"}
            category_ok = (market.category or "").lower() in PRICE_SERIES_CATEGORIES
            if category_ok:
                try:
                    from lib.kronos_forecaster import get_kronos_estimate
                    kronos_estimate = get_kronos_estimate(
                        market_question=market.question,
                        horizon_days=30,
                    )
                except Exception:
                    pass  # Kronos is optional — degrade gracefully

            # Determine fee rate by platform
            fee_rate = _get_fee_rate(market.platform)

            # Run forecaster
            forecast = estimate_probability(
                market=market,
                llm_estimate=llm_estimate,
                metaculus_estimate=metaculus_estimate,
                news_sentiment=news_sentiment,
                kronos_estimate=kronos_estimate,
                fee_rate=fee_rate,
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

            candidate = MarketCandidate(
                market=market,
                forecast=forecast,
                kelly_bet_usd=round(bet_usd, 2),
                naive_kelly_usd=round(naive_bet, 2),
                effective_price=round(effective_price, 4),
                slippage_pct=round(slippage_pct, 4),
                edge_post_slip=round(edge_post_slip, 4),
            )

            # Apply score, edge, and slippage-adjusted edge thresholds.
            # edge_post_slip check ensures we're not taking trades where
            # slippage eats the entire margin of safety — a 9% edge in a
            # thin book that slips 8% is a losing trade after fees.
            if forecast.composite_score < min_score:
                candidate.skip_reason = f"low_score:{forecast.composite_score}/{min_score}"
            elif abs(forecast.edge) < min_edge:
                candidate.skip_reason = f"low_edge:{forecast.edge:.2%}<{min_edge:.0%}"
            elif bet_usd > 0 and edge_post_slip < min_edge:
                candidate.skip_reason = (
                    f"slippage_eats_edge:{edge_post_slip:.2%}<{min_edge:.0%}"
                    f"(slip={slippage_pct:.1%})"
                )

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

    # ── Step 5: Rank by capital efficiency ───────────────────────────
    # The old ranking was EV × confidence, which equal-weighted a 5-day
    # bet and a 74-day bet. A $64 position held 74 days is $0.86/day of
    # capital use; a $157 position held 5 days is $31.40/day. To compound
    # $50 → $25k we must rank by EV per day of capital locked up.
    #
    # capital_efficiency = (EV × confidence) / max(1, days_to_resolution)
    # Markets with unknown resolution dates default to 30 days (neutral).
    def _capital_efficiency(c: MarketCandidate) -> float:
        days = _days_to_resolution(c.market.resolution_date) or 30.0
        days = max(1.0, days)
        return (c.forecast.expected_value * c.forecast.confidence) / days

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
