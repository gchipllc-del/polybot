"""
Order Gate — 3-step pipeline that every order must pass through.

Step 1: PROPOSE — Generate order intent, log it
Step 2: VALIDATE — Run circuit breakers + score check
Step 3: EXECUTE — Only after steps 1 & 2 pass, send to market client

No single function call can place an order. This is by design.
"""

import hashlib
import json
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from tradingcore.audit import log_event
from lib.circuit_breaker import run_all_checks, CircuitBreakerTripped


# Persistent dedup state — survives process restarts. Without this,
# the in-memory _recent_intents dict was wiped on every monitor/scan
# kick, allowing the same trade to be placed minutes apart in different
# scan cycles. The Hormuz duplicate (-$148.52 loss across 2 identical
# orders) is the exact failure mode this prevents.
_DEDUP_PATH = Path(__file__).resolve().parent.parent / "data" / "order_gate_dedup.json"

# Lookback for cross-process duplicate detection. 24h covers nightly
# restarts AND ensures we don't re-bet a market we already have an open
# position in. Was a 60s in-memory window — promoting to 24h on-disk.
PERSISTENT_DUPLICATE_WINDOW_SECONDS = 86400  # 24h


def _load_dedup() -> dict[str, float]:
    """Load the persistent dedup map. Returns {} on missing/corrupt."""
    try:
        if _DEDUP_PATH.exists():
            with open(_DEDUP_PATH) as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {str(k): float(v) for k, v in data.items()}
    except (OSError, json.JSONDecodeError, ValueError):
        # Corrupt file — start fresh rather than blocking trades.
        log_event("order_gate", "dedup_load_failed",
                  {"path": str(_DEDUP_PATH)}, result="degraded")
    return {}


def _save_dedup(d: dict[str, float]) -> None:
    """Atomic-ish save. Never raises into the caller — a dedup save
    failure must not block trade execution.
    """
    try:
        _DEDUP_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _DEDUP_PATH.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(d, f)
        tmp.replace(_DEDUP_PATH)
    except OSError as e:
        log_event("order_gate", "dedup_save_failed",
                  {"error": str(e)[:200]}, result="degraded")


def _dedup_key(intent: "OrderIntent") -> str:
    """Stable dedup key — market + platform + side. Intentionally OMITS
    quantity (so re-attempts at a slightly different size still get
    blocked) and prices (which change between scan cycles)."""
    return f"{intent.platform}:{intent.market_id}:{intent.side}"


@dataclass
class OrderIntent:
    """Immutable order proposal for prediction markets. Created in Step 1, validated in Step 2."""

    # Market identification
    market_id: str
    platform: Literal["kalshi", "polymarket", "manifold"]
    question: str
    side: Literal["YES", "NO"]
    order_type: Literal["limit", "market"]
    quantity: int
    limit_price: float | None = None   # 0.00 - 1.00

    # Forecasting metadata
    our_probability: float = 0.0       # Our Bayesian estimate
    market_probability: float = 0.0    # Current market price
    edge: float = 0.0                  # our_probability - market_probability
    kelly_fraction: float = 0.0        # Optimal bet sizing
    evidence_score: int = 0            # 0-3
    calibration_score: int = 0         # 0-3
    edge_score: int = 0                # 0-3
    composite_score: int = 0           # 0-9 (sum of above)

    # Context
    category: str = ""
    resolution_date: str = ""
    reason: str = ""
    created_at: str = ""
    intent_hash: str = ""
    _validated: bool = False

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()
        if not self.intent_hash:
            hash_input = f"{self.market_id}:{self.platform}:{self.side}:{self.quantity}"
            self.intent_hash = hashlib.sha256(hash_input.encode()).hexdigest()[:16]


# Track recent intent hashes to prevent duplicates
_recent_intents: dict[str, float] = {}  # hash -> timestamp
DUPLICATE_WINDOW_SECONDS = 60


def step1_propose(intent: OrderIntent) -> OrderIntent:
    """
    Step 1: PROPOSE — Log the intent. No execution happens here.
    Returns the intent for Step 2.

    Dedup has two layers:
      • In-memory _recent_intents (60s, hash-based) — fast-path block
        for tight repeat-fire bugs within one process.
      • On-disk persistent map (24h, market+platform+side key) —
        survives process restarts and blocks cross-cycle duplicates
        that previously hit (Hormuz duplicate -$148.52, May 2026).

    The disk dedup is keyed on (platform, market_id, side) so the
    same market can never be re-opened on the same side within 24h
    even after a restart. Quantity and limit_price are intentionally
    excluded so a 1-share retry can't bypass the block.
    """
    now = time.time()

    # ── Layer 1: in-memory tight loop dedup (preserve existing behavior) ──
    if intent.intent_hash in _recent_intents:
        last_time = _recent_intents[intent.intent_hash]
        if now - last_time < DUPLICATE_WINDOW_SECONDS:
            log_event("order_gate", "duplicate_blocked", {
                "hash": intent.intent_hash,
                "market_id": intent.market_id,
                "platform": intent.platform,
                "seconds_since_last": round(now - last_time, 1),
                "layer": "in_memory",
            }, result="blocked")
            raise ValueError(
                f"Duplicate order detected for {intent.market_id} on {intent.platform} "
                f"within {DUPLICATE_WINDOW_SECONDS}s"
            )

    _recent_intents[intent.intent_hash] = now

    # ── Layer 2: persistent 24h dedup ──
    dedup = _load_dedup()
    key = _dedup_key(intent)
    last_persisted = dedup.get(key)
    if last_persisted is not None and (now - last_persisted) < PERSISTENT_DUPLICATE_WINDOW_SECONDS:
        log_event("order_gate", "duplicate_blocked", {
            "hash": intent.intent_hash,
            "key": key,
            "market_id": intent.market_id,
            "platform": intent.platform,
            "side": intent.side,
            "seconds_since_last": round(now - last_persisted, 1),
            "window_seconds": PERSISTENT_DUPLICATE_WINDOW_SECONDS,
            "layer": "persistent",
        }, result="blocked")
        raise ValueError(
            f"Duplicate order detected: same (market, side) traded "
            f"{round((now - last_persisted) / 3600, 1)}h ago on "
            f"{intent.platform} — 24h cooldown active "
            f"(prevents Hormuz-style repeat losses)"
        )

    # Record this intent BEFORE step2/step3 so a crash mid-execute
    # doesn't allow a duplicate on the next cycle. Cleanup old keys
    # while we're here.
    cutoff_persisted = now - PERSISTENT_DUPLICATE_WINDOW_SECONDS
    dedup = {k: t for k, t in dedup.items() if t >= cutoff_persisted}
    dedup[key] = now
    _save_dedup(dedup)

    # Clean up old in-memory entries
    cutoff = now - DUPLICATE_WINDOW_SECONDS * 2
    expired = [h for h, t in _recent_intents.items() if t < cutoff]
    for h in expired:
        del _recent_intents[h]

    log_event("order_gate", "step1_proposed", {
        "intent": asdict(intent),
    }, result="pending")

    return intent


def step2_validate(
    intent: OrderIntent,
    bankroll: float,
    current_daily_pnl: float,
    current_open_positions: int,
    category_exposure: dict[str, float] | None = None,
    resolution_date_exposure: dict[str, float] | None = None,
    market_volume_24h: float = 0.0,
    last_loss_time: datetime | None = None,
    min_composite_score: int | None = None,
) -> bool:
    """
    Step 2: VALIDATE — Run circuit breakers and score check.
    Raises on failure. Returns True on pass.
    """
    # Calculate order value for binary contract
    price = intent.limit_price or intent.market_probability
    order_value = price * intent.quantity

    # Reject orders too close to certainty (≥0.95) or near zero (≤0.05).
    # Manifold returns 400 Bad Request on these — the platform refuses to
    # take the other side of a "free money" trade. Without this gate,
    # the trader keeps re-proposing the same impossible bet every cycle
    # and produces a retry-loop in the audit log (observed 2026-05-14:
    # ~24 retries on a YES-at-0.991 intent over multiple hours).
    # ``min_composite_score`` later catches some of these, but this
    # rejects them earlier and with a clearer reason.
    EXTREME_PRICE_FLOOR = 0.05
    EXTREME_PRICE_CEIL = 0.95
    if price is not None and not (EXTREME_PRICE_FLOOR <= price <= EXTREME_PRICE_CEIL):
        log_event("order_gate", "step2_extreme_price", {
            "hash": intent.intent_hash,
            "price": price,
            "floor": EXTREME_PRICE_FLOOR,
            "ceil": EXTREME_PRICE_CEIL,
        }, result="blocked")
        raise ValueError(
            f"Order price {price:.4f} outside tradable band "
            f"[{EXTREME_PRICE_FLOOR}, {EXTREME_PRICE_CEIL}] — "
            f"prediction market platforms reject extreme-confidence orders"
        )

    # Run all circuit breaker checks
    try:
        run_all_checks(
            order_value=order_value,
            bankroll=bankroll,
            current_daily_pnl=current_daily_pnl,
            current_open_positions=current_open_positions,
            quantity=intent.quantity,
            category=intent.category,
            category_exposure=category_exposure or {},
            resolution_date=intent.resolution_date,
            resolution_date_exposure=resolution_date_exposure or {},
            market_volume_24h=market_volume_24h,
            last_loss_time=last_loss_time,
        )
    except CircuitBreakerTripped as e:
        log_event("order_gate", "step2_breaker_tripped", {
            "hash": intent.intent_hash,
            "reason": str(e),
        }, result="blocked")
        raise

    # Load score threshold + max-stake ceiling from config if not provided.
    # Strategy.yaml is the source of truth for both.
    if min_composite_score is None:
        import yaml
        strategy_path = Path(__file__).parent.parent / "config" / "strategy.yaml"
        with open(strategy_path, "r") as f:
            strategy = yaml.safe_load(f)
        min_composite_score = strategy.get("scoring", {}).get("min_composite_score", 6)
        max_stake_usd = float(strategy.get("max_stake_usd", 0.0) or 0.0)
    else:
        # Caller specified score; still read max_stake_usd for the cap check.
        import yaml
        strategy_path = Path(__file__).parent.parent / "config" / "strategy.yaml"
        try:
            with open(strategy_path, "r") as f:
                strategy = yaml.safe_load(f)
            max_stake_usd = float(strategy.get("max_stake_usd", 0.0) or 0.0)
        except OSError:
            max_stake_usd = 0.0

    # Hard absolute stake cap. Layered ON TOP of max_per_market_pct so
    # even a bankroll explosion can't expose us to a single market beyond
    # this dollar amount. 0 disables the check (legacy callers).
    # The Virginia trade ($157) and Hormuz duplicate ($74 x 2) would
    # have been blocked here with max_stake_usd=30.
    if max_stake_usd > 0 and order_value > max_stake_usd:
        log_event("order_gate", "step2_stake_too_large", {
            "hash": intent.intent_hash,
            "order_value": round(order_value, 2),
            "max_stake_usd": max_stake_usd,
            "quantity": intent.quantity,
            "price": price,
        }, result="blocked")
        raise ValueError(
            f"Order value ${order_value:.2f} exceeds max_stake_usd "
            f"cap of ${max_stake_usd:.2f} — bot is sizing too aggressively "
            f"on a single market (Virginia/Hormuz-style hit prevention)"
        )

    if intent.composite_score < min_composite_score:
        log_event("order_gate", "step2_low_score", {
            "hash": intent.intent_hash,
            "score": intent.composite_score,
            "required": min_composite_score,
        }, result="blocked")
        raise ValueError(
            f"Composite score {intent.composite_score}/9 below minimum {min_composite_score}/9"
        )

    log_event("order_gate", "step2_validated", {
        "hash": intent.intent_hash,
        "order_value": order_value,
        "edge": intent.edge,
        "kelly": intent.kelly_fraction,
    }, result="success")

    intent._validated = True
    return True


def step3_execute(intent: OrderIntent, market_client) -> dict:
    """
    Step 3: EXECUTE — Send the validated order to the market.
    This is the ONLY function that calls the platform API.

    Args:
        intent: The validated OrderIntent (must have passed step2_validate)
        market_client: The platform's MarketClient instance

    Returns:
        OrderResult from the platform

    Raises:
        RuntimeError: If step2_validate was not called on this intent.
    """
    if not intent._validated:
        log_event("order_gate", "step3_not_validated", {
            "hash": intent.intent_hash,
            "market_id": intent.market_id,
        }, result="blocked")
        raise RuntimeError(
            "Cannot execute: OrderIntent was not validated by step2_validate. "
            "All orders must pass through the full propose -> validate -> execute pipeline."
        )

    log_event("order_gate", "step3_executing", {
        "hash": intent.intent_hash,
        "market_id": intent.market_id,
        "platform": intent.platform,
        "side": intent.side,
        "quantity": intent.quantity,
        "price": intent.limit_price,
    }, result="pending")

    try:
        result = market_client.place_order(
            market_id=intent.market_id,
            side=intent.side,
            price=intent.limit_price or intent.market_probability,
            quantity=intent.quantity,
            order_type=intent.order_type,
        )

        log_event("order_gate", "step3_executed", {
            "hash": intent.intent_hash,
            "order_id": result.order_id,
            "status": result.status,
        }, result="success")

        return asdict(result)

    except Exception as e:
        log_event("order_gate", "step3_failed", {
            "hash": intent.intent_hash,
            "error": str(e),
        }, result="failed")
        raise
