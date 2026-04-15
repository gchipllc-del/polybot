"""
Circuit Breakers — hard limits that cannot be bypassed.
If any breaker trips, trading halts until conditions clear or human intervenes.

9 breakers total:
  1. Paper mode guard
  2. Daily loss limit
  3. Position size limit
  4. Open positions limit
  5. Quantity per order limit
  6. Cooldown after loss
  7. Category exposure limit      [NEW for prediction markets]
  8. Minimum market liquidity     [NEW for prediction markets]
  9. Resolution date concentration [NEW for prediction markets]
"""

from datetime import datetime, timezone
from pathlib import Path

import yaml

from lib.audit import log_event

CONFIG_PATH = Path(__file__).parent.parent / "config" / "settings.yaml"


def _load_settings() -> dict:
    with open(CONFIG_PATH, "r") as f:
        return yaml.safe_load(f)


class CircuitBreakerTripped(Exception):
    """Raised when a circuit breaker condition is violated."""
    pass


def check_paper_mode(settings: dict | None = None):
    """CRITICAL: Ensure we're in paper/manifold mode unless explicitly migrated."""
    if settings is None:
        settings = _load_settings()
    mode = settings.get("mode", "manifold")
    approved = settings.get("live_migration_approved", False)

    if mode not in ("manifold", "paper") and not approved:
        log_event("circuit_breaker", "paper_mode_violation", {
            "mode": mode,
            "approved": approved,
        }, result="blocked")
        raise CircuitBreakerTripped(
            "BLOCKED: Live trading not approved. Set live_migration_approved: true in settings.yaml"
        )


def check_daily_loss(current_daily_pnl: float, settings: dict | None = None) -> bool:
    """Check if daily loss limit has been breached."""
    if settings is None:
        settings = _load_settings()
    max_loss = settings["circuit_breakers"]["max_daily_loss"]

    if current_daily_pnl <= max_loss:
        log_event("circuit_breaker", "daily_loss_breached", {
            "current_pnl": current_daily_pnl,
            "max_loss": max_loss,
        }, result="blocked")
        raise CircuitBreakerTripped(
            f"HALTED: Daily P/L ${current_daily_pnl:.2f} breached limit ${max_loss}"
        )
    return True


def check_position_size(order_value: float, bankroll: float, settings: dict | None = None) -> bool:
    """Ensure no single position exceeds max allocation."""
    if settings is None:
        settings = _load_settings()
    max_pct = settings["circuit_breakers"]["max_per_market_pct"]
    position_pct = order_value / bankroll if bankroll > 0 else 1.0

    if position_pct > max_pct:
        log_event("circuit_breaker", "position_size_exceeded", {
            "order_value": order_value,
            "bankroll": bankroll,
            "position_pct": round(position_pct, 4),
            "max_pct": max_pct,
        }, result="blocked")
        raise CircuitBreakerTripped(
            f"BLOCKED: Position {position_pct:.1%} exceeds {max_pct:.0%} limit"
        )
    return True


def check_open_positions(current_count: int, settings: dict | None = None) -> bool:
    """Limit concurrent open positions."""
    if settings is None:
        settings = _load_settings()
    max_positions = settings["circuit_breakers"]["max_open_positions"]

    if current_count >= max_positions:
        log_event("circuit_breaker", "max_open_positions", {
            "current": current_count,
            "max": max_positions,
        }, result="blocked")
        raise CircuitBreakerTripped(
            f"BLOCKED: {current_count} open positions, max is {max_positions}"
        )
    return True


def check_quantity_per_order(quantity: int, settings: dict | None = None) -> bool:
    """Limit contracts per single order."""
    if settings is None:
        settings = _load_settings()
    max_qty = settings["circuit_breakers"]["max_quantity_per_order"]

    if quantity > max_qty:
        log_event("circuit_breaker", "max_quantity_exceeded", {
            "requested": quantity,
            "max": max_qty,
        }, result="blocked")
        raise CircuitBreakerTripped(
            f"BLOCKED: {quantity} contracts exceeds max {max_qty}"
        )
    return True


def check_cooldown(last_loss_time: datetime | None, settings: dict | None = None) -> bool:
    """Enforce cooldown period after a losing trade."""
    if last_loss_time is None:
        return True

    if settings is None:
        settings = _load_settings()
    cooldown_min = settings["circuit_breakers"]["cooldown_after_loss_minutes"]
    elapsed = (datetime.now(timezone.utc) - last_loss_time).total_seconds() / 60

    if elapsed < cooldown_min:
        remaining = cooldown_min - elapsed
        log_event("circuit_breaker", "cooldown_active", {
            "last_loss": last_loss_time.isoformat(),
            "remaining_minutes": round(remaining, 1),
        }, result="blocked")
        raise CircuitBreakerTripped(
            f"BLOCKED: Cooldown active. {remaining:.0f} minutes remaining."
        )
    return True


def check_category_exposure(
    category: str,
    category_exposure: dict[str, float],
    bankroll: float,
    settings: dict | None = None,
) -> bool:
    """Prevent over-concentration in a single market category.

    Prediction markets cluster by topic (politics, crypto, etc.).
    If all our bets are politics and one upset happens, we lose everything.
    """
    if settings is None:
        settings = _load_settings()
    max_pct = settings["circuit_breakers"]["max_category_pct"]

    current_exposure = category_exposure.get(category, 0.0)
    exposure_pct = current_exposure / bankroll if bankroll > 0 else 0.0

    if exposure_pct >= max_pct:
        log_event("circuit_breaker", "category_exposure_exceeded", {
            "category": category,
            "exposure": current_exposure,
            "exposure_pct": round(exposure_pct, 4),
            "max_pct": max_pct,
        }, result="blocked")
        raise CircuitBreakerTripped(
            f"BLOCKED: {category} exposure {exposure_pct:.0%} exceeds {max_pct:.0%} limit"
        )
    return True


def check_min_liquidity(market_volume_24h: float, settings: dict | None = None) -> bool:
    """Refuse to trade illiquid markets.

    Low-volume markets have wide spreads, slippage, and harder exits.
    """
    if settings is None:
        settings = _load_settings()
    min_volume = settings["circuit_breakers"]["min_market_volume"]

    if market_volume_24h < min_volume:
        log_event("circuit_breaker", "low_liquidity", {
            "volume_24h": market_volume_24h,
            "min_required": min_volume,
        }, result="blocked")
        raise CircuitBreakerTripped(
            f"BLOCKED: Market 24h volume ${market_volume_24h:.0f} below minimum ${min_volume}"
        )
    return True


def check_resolution_concentration(
    resolution_date: str,
    resolution_date_exposure: dict[str, float],
    bankroll: float,
    settings: dict | None = None,
) -> bool:
    """Prevent too much bankroll resolving on the same date.

    If 50% of our bankroll resolves on election day and we're wrong,
    we lose half our capital in one night. Spread resolution dates.
    """
    if not resolution_date:
        return True

    if settings is None:
        settings = _load_settings()
    max_pct = settings["circuit_breakers"]["max_resolution_date_pct"]

    # Normalize to date only (strip time)
    date_key = resolution_date[:10]
    current_exposure = resolution_date_exposure.get(date_key, 0.0)
    exposure_pct = current_exposure / bankroll if bankroll > 0 else 0.0

    if exposure_pct >= max_pct:
        log_event("circuit_breaker", "resolution_date_concentration", {
            "date": date_key,
            "exposure": current_exposure,
            "exposure_pct": round(exposure_pct, 4),
            "max_pct": max_pct,
        }, result="blocked")
        raise CircuitBreakerTripped(
            f"BLOCKED: {date_key} resolution exposure {exposure_pct:.0%} exceeds {max_pct:.0%}"
        )
    return True


def run_all_checks(
    order_value: float,
    bankroll: float,
    current_daily_pnl: float,
    current_open_positions: int,
    quantity: int,
    category: str = "",
    category_exposure: dict[str, float] | None = None,
    resolution_date: str = "",
    resolution_date_exposure: dict[str, float] | None = None,
    market_volume_24h: float = 0.0,
    last_loss_time: datetime | None = None,
) -> bool:
    """
    Run every circuit breaker check. Raises CircuitBreakerTripped on any failure.
    Call this before ANY order execution.
    """
    settings = _load_settings()

    # Core safety
    check_paper_mode(settings)
    check_daily_loss(current_daily_pnl, settings)
    check_position_size(order_value, bankroll, settings)
    check_open_positions(current_open_positions, settings)
    check_quantity_per_order(quantity, settings)
    check_cooldown(last_loss_time, settings)

    # Prediction market specific
    if category and category_exposure is not None:
        check_category_exposure(category, category_exposure, bankroll, settings)
    if market_volume_24h > 0:
        check_min_liquidity(market_volume_24h, settings)
    if resolution_date and resolution_date_exposure is not None:
        check_resolution_concentration(
            resolution_date, resolution_date_exposure, bankroll, settings
        )

    log_event("circuit_breaker", "all_checks_passed", {
        "order_value": order_value,
        "daily_pnl": current_daily_pnl,
        "open_positions": current_open_positions,
        "quantity": quantity,
        "category": category,
    }, result="success")
    return True
