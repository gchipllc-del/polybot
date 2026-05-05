"""
Position Monitor — continuous monitoring loop for prediction markets.

Checks (in order):
    1. Resolution detection — market resolved, needs settlement (immediate)
    2. Stop loss — position dropped past stop loss threshold (immediate)
    3. Take profit — position hit take profit price (immediate)
    4. Early exit — position gained past the early exit threshold (advisory)
    5. Edge gone (static) — market moved past original estimate (advisory)
    6. Reforecast — re-run full forecast; exit if edge has flipped (advisory)
    7. Price drift — advisory-only alert on large move (logged, not exit)
    8. Stale position — no market activity in 24+ hours (advisory)

Runs on a configurable interval (default 120s). Missed checks trigger
alerts; 10 consecutive misses trigger the kill switch.

Security:
    - Read-only monitoring — never places orders directly
    - Missed check detection for reliability monitoring
    - All findings logged to audit trail
    - Exit signals returned to caller for order gate processing
    - Reforecast is throttled per-position and per-interval to prevent
      runaway LLM spend; all three gates must pass before the call fires
"""

import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from lib.audit import log_event
from lib.forecaster import ForecastResult, build_forecast_for_market
from lib.market_client import MarketClient, MarketInfo, get_active_clients

DATA_DIR = Path(__file__).parent.parent / "data"
POSITIONS_PATH = DATA_DIR / "positions.json"
CONFIG_PATH = Path(__file__).parent.parent / "config"


def _load_settings() -> dict:
    with open(CONFIG_PATH / "settings.yaml", "r") as f:
        return yaml.safe_load(f)


def _load_strategy() -> dict:
    with open(CONFIG_PATH / "strategy.yaml", "r") as f:
        return yaml.safe_load(f)


def _load_positions() -> list[dict]:
    if not POSITIONS_PATH.exists():
        return []
    try:
        with open(POSITIONS_PATH, "r") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save_positions(positions: list[dict]):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(POSITIONS_PATH, "w") as f:
        json.dump(positions, f, indent=2)


@dataclass
class ExitSignal:
    """A signal to exit a position, returned by the monitor."""
    market_id: str
    platform: str
    reason: str              # "stop_loss", "take_profit", "edge_gone", "reforecast_*", "price_drift", "resolved", "stale"
    side: str                # Current side of our position
    entry_price: float
    current_price: float
    urgency: str             # "immediate" (stop loss, resolved, strong_against) or "advisory" (drift, stale, flipped)
    details: dict


def _minutes_since_iso(ts: str | None) -> float | None:
    """Best-effort: parse an ISO-8601 timestamp and return minutes since it.
    Returns None if the timestamp is missing or malformed."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds() / 60.0


def _reforecast_implied_edge(
    new_probability: float,
    current_market_price: float,
    side: str,
) -> float:
    """
    Compute the current edge implied by a fresh forecast + live market price.

    Edge convention mirrors the scanner: positive means the market is
    under-pricing our side, negative means it has moved past us.

    For YES:   edge = our_prob - market_yes_price
    For NO:    edge = (1 - our_prob) - (1 - market_yes_price) = market_yes_price - our_prob

    The market price here is the YES price in both cases — `side` flips the
    inequality. Using market_yes_price directly (not current_price, which
    is the NO price for NO positions) avoids a subtle sign error.
    """
    p = max(0.0, min(float(new_probability), 1.0))
    m = max(0.0, min(float(current_market_price), 1.0))
    return (p - m) if side == "YES" else (m - p)


def _maybe_reforecast(
    position: dict,
    current_market: MarketInfo,
    strategy: dict,
) -> ForecastResult | None:
    """
    Run the full forecast pipeline on an open position IF the throttle gates
    all pass. Returns the fresh `ForecastResult`, or None when we skip.

    Gates, in order (fail any → return None):
        1. Feature disabled (`exits.reforecast.enabled: false`)
        2. Position too fresh (younger than `min_age_minutes`) — the original
           forecast is still the best we can do
        3. Already reforecasted too recently (< `min_interval_minutes`)
        4. Market hasn't drifted enough from entry to warrant burning an
           LLM call (`min_price_drift`) — if the book hasn't moved AND we
           just reforecasted, nothing has changed that an ensemble would see
        5. The pipeline itself raises — we swallow the exception and return
           None so a flaky provider doesn't take down the monitor

    This is the key cost guardrail. Each reforecast is ~1 ensemble LLM call
    + news + Metaculus; at 10 open positions and a 30m throttle, worst-case
    spend is ~20 ensemble calls/hour. Without the drift gate, every cycle
    of the 120s loop would trip it.
    """
    cfg = (strategy.get("exits", {}) or {}).get("reforecast", {}) or {}
    if not cfg.get("enabled", True):
        return None

    # Gate 2: position age
    min_age = float(cfg.get("min_age_minutes", 30))
    age = _minutes_since_iso(position.get("opened_at"))
    if age is not None and age < min_age:
        return None

    # Gate 3: reforecast interval
    min_interval = float(cfg.get("min_interval_minutes", 30))
    since_last = _minutes_since_iso(position.get("reforecast_at"))
    if since_last is not None and since_last < min_interval:
        return None

    # Gate 4: price drift from entry (cheap pre-check before we pay LLM cost)
    min_drift = float(cfg.get("min_price_drift", 0.03))
    entry_price = float(position.get("entry_price", 0) or 0)
    side = position.get("side", "YES")
    current_price = (
        current_market.yes_price if side == "YES" else current_market.no_price
    )
    if entry_price > 0:
        drift = abs(current_price - entry_price) / entry_price
        if drift < min_drift:
            return None

    # All gates passed — run the forecast. Any failure returns None so the
    # caller treats "couldn't reforecast" identically to "edge still valid".
    try:
        llm_enabled = bool(cfg.get("llm_enabled", True))
        return build_forecast_for_market(
            market=current_market,
            strategy=strategy,
            llm_enabled=llm_enabled,
        )
    except Exception as e:
        log_event("monitor", "reforecast_failed", {
            "market_id": position.get("market_id", ""),
            "platform": position.get("platform", ""),
            "error": str(e)[:200],
        }, result="failed")
        return None


def check_position(
    position: dict,
    current_market: MarketInfo | None,
    strategy: dict,
) -> ExitSignal | None:
    """
    Run all exit checks on a single position.

    Args:
        position: Position dict from positions.json
        current_market: Live market data (None if market not found)
        strategy: Strategy config

    Returns:
        ExitSignal if position should be exited, None if OK.
    """
    exits = strategy.get("exits", {})
    market_id = position.get("market_id", "")
    platform = position.get("platform", "")
    side = position.get("side", "YES")
    entry_price = position.get("entry_price", 0)

    # If market not found, flag as stale
    if current_market is None:
        return ExitSignal(
            market_id=market_id, platform=platform,
            reason="market_not_found", side=side,
            entry_price=entry_price, current_price=0,
            urgency="advisory",
            details={"note": "Market not found on platform — may have been delisted"},
        )

    current_price = current_market.yes_price if side == "YES" else current_market.no_price

    # Update current price in position
    position["current_price"] = current_price
    position["last_checked"] = datetime.now(timezone.utc).isoformat()

    # ── 1. Resolution Detection ───────────────────────────────────
    if current_market.status == "resolved":
        return ExitSignal(
            market_id=market_id, platform=platform,
            reason="resolved", side=side,
            entry_price=entry_price, current_price=current_price,
            urgency="immediate",
            details={"outcome": current_market.outcome, "resolution_source": current_market.resolution_source},
        )

    # ── 2. Stop Loss ─────────────────────────────────────────────
    stop_loss_pct = exits.get("stop_loss_pct", 0.25)
    price_drop = entry_price - current_price
    drop_pct = price_drop / entry_price if entry_price > 0 else 0

    if drop_pct >= stop_loss_pct:
        return ExitSignal(
            market_id=market_id, platform=platform,
            reason="stop_loss", side=side,
            entry_price=entry_price, current_price=current_price,
            urgency="immediate",
            details={"drop_pct": round(drop_pct, 4), "threshold": stop_loss_pct},
        )

    # ── 3. Take Profit ───────────────────────────────────────────
    take_profit = exits.get("take_profit_price", 0.95)
    if current_price >= take_profit:
        return ExitSignal(
            market_id=market_id, platform=platform,
            reason="take_profit", side=side,
            entry_price=entry_price, current_price=current_price,
            urgency="immediate",
            details={"price": current_price, "threshold": take_profit},
        )

    # ── 4. Early Exit (gain threshold) ───────────────────────────
    early_exit = exits.get("early_exit_threshold", 0.15)
    gain_pct = (current_price - entry_price) / entry_price if entry_price > 0 else 0
    if gain_pct >= early_exit:
        return ExitSignal(
            market_id=market_id, platform=platform,
            reason="early_exit", side=side,
            entry_price=entry_price, current_price=current_price,
            urgency="advisory",
            details={"gain_pct": round(gain_pct, 4), "threshold": early_exit},
        )

    # ── 5. Edge Gone (static — vs. original entry probability) ────
    edge_buffer = exits.get("edge_gone_buffer", 0.03)
    our_prob = position.get("our_probability", 0)
    if our_prob > 0:
        market_prob = current_market.yes_price
        current_edge = our_prob - market_prob if side == "YES" else (1 - our_prob) - (1 - market_prob)
        if current_edge < -edge_buffer:
            return ExitSignal(
                market_id=market_id, platform=platform,
                reason="edge_gone", side=side,
                entry_price=entry_price, current_price=current_price,
                urgency="advisory",
                details={"current_edge": round(current_edge, 4), "buffer": edge_buffer},
            )

    # ── 5b. Reforecast (fresh probability vs. live market) ───────
    # The static check above compares the market to our ENTRY probability.
    # This one re-runs the full forecast pipeline — catches cases where
    # new information has moved our estimate but the market hasn't caught
    # up yet, or where our original forecast was miscalibrated.
    reforecast_cfg = exits.get("reforecast", {}) or {}
    new_forecast = _maybe_reforecast(position, current_market, strategy)
    if new_forecast is not None:
        implied_edge = _reforecast_implied_edge(
            new_probability=new_forecast.probability,
            current_market_price=current_market.yes_price,
            side=side,
        )

        # Stamp the trajectory onto the position so subsequent cycles can
        # see it (and the dashboard can display drift from entry probability).
        position["our_probability_current"] = round(new_forecast.probability, 4)
        position["reforecast_at"] = datetime.now(timezone.utc).isoformat()
        position["reforecast_edge"] = round(implied_edge, 4)
        position["reforecast_composite_score"] = new_forecast.composite_score

        strong_against = float(reforecast_cfg.get("strong_against_threshold", -0.10))
        flipped = float(reforecast_cfg.get("flipped_threshold", -0.03))

        if implied_edge < strong_against:
            return ExitSignal(
                market_id=market_id, platform=platform,
                reason="reforecast_strong_against", side=side,
                entry_price=entry_price, current_price=current_price,
                urgency="immediate",
                details={
                    "implied_edge": round(implied_edge, 4),
                    "threshold": strong_against,
                    "new_probability": round(new_forecast.probability, 4),
                    "entry_probability": round(our_prob, 4) if our_prob else None,
                    "composite_score": new_forecast.composite_score,
                },
            )
        if implied_edge < flipped:
            return ExitSignal(
                market_id=market_id, platform=platform,
                reason="reforecast_edge_gone", side=side,
                entry_price=entry_price, current_price=current_price,
                urgency="advisory",
                details={
                    "implied_edge": round(implied_edge, 4),
                    "threshold": flipped,
                    "new_probability": round(new_forecast.probability, 4),
                    "entry_probability": round(our_prob, 4) if our_prob else None,
                    "composite_score": new_forecast.composite_score,
                },
            )

    # ── 6. Price Drift Alert ─────────────────────────────────────
    drift_threshold = exits.get("price_drift_alert", 0.10)
    if abs(current_price - entry_price) / max(entry_price, 0.01) >= drift_threshold:
        # Only alert, don't exit — this is informational
        log_event("monitor", "price_drift", {
            "market_id": market_id,
            "entry": entry_price,
            "current": current_price,
            "drift_pct": round(abs(current_price - entry_price) / max(entry_price, 0.01), 4),
        })

    # ── 7. Stale Position ────────────────────────────────────────
    stale_hours = exits.get("stale_position_hours", 24)
    last_activity = position.get("last_activity", position.get("opened_at", ""))
    if last_activity:
        try:
            last_dt = datetime.fromisoformat(last_activity.replace("Z", "+00:00"))
            hours_since = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
            if hours_since > stale_hours and current_market.volume_24h < 100:
                return ExitSignal(
                    market_id=market_id, platform=platform,
                    reason="stale", side=side,
                    entry_price=entry_price, current_price=current_price,
                    urgency="advisory",
                    details={"hours_since_activity": round(hours_since, 1), "volume_24h": current_market.volume_24h},
                )
        except (ValueError, TypeError):
            pass

    return None


# ─────────────────────────────────────────────────────────────────
# Auto-exit execution
# ─────────────────────────────────────────────────────────────────
#
# The monitor emits ExitSignals but historically stopped there — a human
# had to read the logs and close by hand. These helpers close the loop
# when `exits.auto_exit.enabled: true` in strategy.yaml.
#
# Dispatch table (immediate-urgency signals only; advisory NEVER auto-
# executes):
#     resolved                   → settle_position() (P/L, calibration, memory)
#     stop_loss                  → client.close_position()
#     take_profit                → client.close_position()
#     reforecast_strong_against  → client.close_position()
#
# Closes bypass the order gate on purpose. You want to exit a stopped-
# out position even if the daily-loss breaker has tripped — that's what
# "immediate" means. Circuit breakers govern ENTRIES, not EXITS. The
# kill_switch follows the same convention.

# Signal reasons that trigger a market close (not settlement).
_CLOSE_REASONS = frozenset({
    "stop_loss",
    "take_profit",
    "reforecast_strong_against",
})

# Signal reasons that trigger local settlement (market already resolved).
_SETTLE_REASONS = frozenset({
    "resolved",
})


def _execute_resolved_signal(signal: "ExitSignal") -> dict:
    """
    Handle an `immediate` signal with reason=resolved: delegate to
    resolution_tracker.settle_position, which computes P/L, records
    calibration, and updates positions.json atomically.

    Returns a dispatch record for the caller to log + aggregate.
    """
    from lib.resolution_tracker import settle_position

    outcome = signal.details.get("outcome") if isinstance(signal.details, dict) else None
    source = signal.details.get("resolution_source", "") if isinstance(signal.details, dict) else ""

    if not outcome:
        # The market claims "resolved" but has no outcome yet — disputed
        # or mid-settlement. Skip rather than settle with a bad value.
        return {
            "market_id": signal.market_id,
            "platform": signal.platform,
            "reason": signal.reason,
            "action": "settle",
            "status": "skipped",
            "note": "market resolved without outcome — deferred",
        }

    settlement = settle_position(
        market_id=signal.market_id,
        outcome=outcome,
        resolution_source=source,
    )
    if settlement is None:
        # No matching open position — race with another settler, or
        # the position was already closed. Not an error.
        return {
            "market_id": signal.market_id,
            "platform": signal.platform,
            "reason": signal.reason,
            "action": "settle",
            "status": "not_found",
        }

    return {
        "market_id": signal.market_id,
        "platform": signal.platform,
        "reason": signal.reason,
        "action": "settle",
        "status": "executed",
        "outcome": outcome,
        "net_profit": settlement.get("net_profit"),
    }


def _execute_close_signal(
    signal: "ExitSignal",
    position: dict,
    client: MarketClient,
    dry_run: bool = False,
) -> dict:
    """
    Handle an `immediate` signal with reason in _CLOSE_REASONS:
    call client.close_position(), update the position dict in place,
    audit both the attempt and the result.

    Returns a dispatch record. Caller persists the mutated `position`.
    On failure the position is left open with a `last_close_attempt_at`
    stamp so retries can be throttled.
    """
    now = datetime.now(timezone.utc).isoformat()
    position["last_close_attempt_at"] = now

    log_event("monitor", "auto_exit_attempt", {
        "market_id": signal.market_id,
        "platform": signal.platform,
        "side": signal.side,
        "reason": signal.reason,
        "entry": signal.entry_price,
        "current": signal.current_price,
        "dry_run": dry_run,
    })

    if dry_run:
        return {
            "market_id": signal.market_id,
            "platform": signal.platform,
            "reason": signal.reason,
            "action": "close",
            "status": "dry_run",
        }

    try:
        result = client.close_position(signal.market_id, signal.side)
    except Exception as exc:
        err = str(exc)[:200]
        position["last_close_error"] = err
        log_event("monitor", "auto_exit_failed", {
            "market_id": signal.market_id,
            "platform": signal.platform,
            "reason": signal.reason,
            "error": err,
        }, result="error")
        return {
            "market_id": signal.market_id,
            "platform": signal.platform,
            "reason": signal.reason,
            "action": "close",
            "status": "failed",
            "error": err,
        }

    # Success — mutate the position to reflect the close.
    position["status"] = "closed"
    position["close_reason"] = signal.reason
    position["closed_at"] = now
    position["close_method"] = "auto_exit"
    position["close_price"] = signal.current_price
    if getattr(result, "order_id", None):
        position["close_order_id"] = result.order_id
    # Clear the error field if a previous attempt had failed.
    position.pop("last_close_error", None)

    log_event("monitor", "auto_exit_executed", {
        "market_id": signal.market_id,
        "platform": signal.platform,
        "side": signal.side,
        "reason": signal.reason,
        "close_price": signal.current_price,
        "order_id": getattr(result, "order_id", None),
    }, result="success")

    return {
        "market_id": signal.market_id,
        "platform": signal.platform,
        "reason": signal.reason,
        "action": "close",
        "status": "executed",
        "order_id": getattr(result, "order_id", None),
    }


def _execute_pending_exits(
    exit_signals: list["ExitSignal"],
    positions: list[dict],
    client_map: dict[str, MarketClient],
    strategy: dict,
    dry_run: bool = False,
) -> dict:
    """
    Dispatch auto-exit execution across the signals emitted this cycle.

    Honors:
        - auto_exit.enabled gate (returns early if disabled)
        - immediate-urgency-only filter
        - max_per_cycle cap (cascading-exit protection)
        - min_seconds_between_attempts cool-off per position
    """
    cfg = (strategy.get("exits", {}) or {}).get("auto_exit", {}) or {}
    if not cfg.get("enabled", False):
        return {
            "enabled": False,
            "executed": [],
            "failed": [],
            "skipped": [],
        }

    max_per_cycle = int(cfg.get("max_per_cycle", 3))
    cool_off_seconds = float(cfg.get("min_seconds_between_attempts", 300))

    # Index positions by (market_id, platform, side) for quick lookup.
    pos_index: dict[tuple[str, str, str], dict] = {}
    for p in positions:
        key = (p.get("market_id", ""), p.get("platform", ""), p.get("side", ""))
        pos_index[key] = p

    executed: list[dict] = []
    failed: list[dict] = []
    skipped: list[dict] = []

    for signal in exit_signals:
        # Only immediate-urgency signals ever reach the exchange.
        if signal.urgency != "immediate":
            continue

        if len(executed) >= max_per_cycle:
            skipped.append({
                "market_id": signal.market_id,
                "reason": signal.reason,
                "note": f"max_per_cycle={max_per_cycle} reached",
            })
            continue

        # Branch on signal kind.
        if signal.reason in _SETTLE_REASONS:
            record = _execute_resolved_signal(signal)
            if record.get("status") == "executed":
                executed.append(record)
            else:
                skipped.append(record)
            continue

        if signal.reason not in _CLOSE_REASONS:
            # Immediate-urgency but not one we know how to auto-execute.
            # Log and skip — adding a new immediate reason should be an
            # explicit code change, not a silent expansion.
            skipped.append({
                "market_id": signal.market_id,
                "reason": signal.reason,
                "note": "unknown immediate reason — add to _CLOSE_REASONS if intended",
            })
            continue

        position = pos_index.get((signal.market_id, signal.platform, signal.side))
        if position is None:
            skipped.append({
                "market_id": signal.market_id,
                "reason": signal.reason,
                "note": "no matching open position (already closed?)",
            })
            continue

        # Per-position retry throttle.
        last_attempt = position.get("last_close_attempt_at")
        if last_attempt:
            age = _minutes_since_iso(last_attempt)
            if age is not None and age * 60.0 < cool_off_seconds:
                skipped.append({
                    "market_id": signal.market_id,
                    "reason": signal.reason,
                    "note": f"cool-off active ({age*60:.0f}s < {cool_off_seconds:.0f}s)",
                })
                continue

        client = client_map.get(signal.platform)
        if client is None:
            skipped.append({
                "market_id": signal.market_id,
                "reason": signal.reason,
                "note": f"no client for platform '{signal.platform}'",
            })
            continue

        record = _execute_close_signal(signal, position, client, dry_run=dry_run)
        if record.get("status") == "executed" or record.get("status") == "dry_run":
            executed.append(record)
        else:
            failed.append(record)

    return {
        "enabled": True,
        "dry_run": dry_run,
        "executed": executed,
        "failed": failed,
        "skipped": skipped,
    }


def run_monitoring_cycle(dry_run: bool = False) -> dict:
    """
    Run one full monitoring cycle across all positions.

    Args:
        dry_run: If True, auto-exit detects but does not place close orders.

    Returns:
        {
            "positions_checked": int,
            "exit_signals": [ExitSignal, ...],
            "exit_execution": dict,  # result of _execute_pending_exits
            "errors": [str, ...],
            "timestamp": str,
        }
    """
    strategy = _load_strategy()
    positions = _load_positions()
    open_positions = [p for p in positions if p.get("status", "open") == "open"]

    if not open_positions:
        return {
            "positions_checked": 0,
            "exit_signals": [],
            "exit_execution": {"enabled": False, "executed": [], "failed": [], "skipped": []},
            "errors": [],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    clients = get_active_clients()
    client_map: dict[str, MarketClient] = {c.platform_name: c for c in clients}

    exit_signals: list[ExitSignal] = []
    errors: list[str] = []

    for pos in open_positions:
        market_id = pos.get("market_id", "")
        platform = pos.get("platform", "")

        client = client_map.get(platform)
        if not client:
            errors.append(f"No client for platform '{platform}' (market {market_id})")
            continue

        # Fetch current market data
        current_market = None
        try:
            current_market = client.get_market(market_id)
        except Exception as e:
            errors.append(f"Failed to fetch {market_id} on {platform}: {str(e)[:100]}")

        # Run checks
        signal = check_position(pos, current_market, strategy)
        if signal:
            exit_signals.append(signal)
            log_event("monitor", f"exit_signal_{signal.reason}", {
                "market_id": signal.market_id,
                "platform": signal.platform,
                "side": signal.side,
                "entry": signal.entry_price,
                "current": signal.current_price,
                "urgency": signal.urgency,
            })

    # Auto-exit: execute the immediate-urgency signals (if enabled).
    # Mutates `positions` in place for successful closes. `resolved`
    # signals are delegated to resolution_tracker.settle_position,
    # which reloads/rewrites positions.json itself — so re-load from
    # disk afterward to pick up those settled-state changes before we
    # save the price-update mutations from the scan loop above.
    exit_execution = _execute_pending_exits(
        exit_signals=exit_signals,
        positions=positions,
        client_map=client_map,
        strategy=strategy,
        dry_run=dry_run,
    )

    settled_any = any(
        rec.get("action") == "settle" and rec.get("status") == "executed"
        for rec in exit_execution.get("executed", [])
    )
    if settled_any:
        # settle_position has already persisted the settled records. Merge
        # our in-memory price updates onto that authoritative view.
        persisted = _load_positions()
        persisted_map = {p.get("position_id"): p for p in persisted}
        for p in positions:
            pid = p.get("position_id")
            if pid in persisted_map and persisted_map[pid].get("status") in {"settled", "closed"}:
                # The resolution tracker owns this record now — don't clobber it.
                continue
            persisted_map[pid] = p
        positions = list(persisted_map.values())

    # Save updated positions (with current prices + close mutations)
    _save_positions(positions)

    log_event("monitor", "cycle_complete", {
        "positions_checked": len(open_positions),
        "exit_signals": len(exit_signals),
        "auto_exits_executed": len(exit_execution.get("executed", [])),
        "auto_exits_failed": len(exit_execution.get("failed", [])),
        "errors": len(errors),
    }, result="success")

    return {
        "positions_checked": len(open_positions),
        "exit_signals": exit_signals,
        "exit_execution": exit_execution,
        "errors": errors,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def start_monitoring_loop():
    """
    Start the continuous monitoring loop.

    Runs check cycles at the configured interval. Tracks missed checks
    for reliability alerting. Caller handles KeyboardInterrupt.
    """
    settings = _load_settings()
    interval = settings["monitoring"]["check_interval_seconds"]
    missed_alert = settings["monitoring"]["missed_check_alert"]
    missed_kill = settings["monitoring"]["missed_check_kill"]

    consecutive_misses = 0

    log_event("monitor", "loop_started", {
        "interval_seconds": interval,
        "missed_alert_threshold": missed_alert,
        "missed_kill_threshold": missed_kill,
    })

    print(f"Monitoring loop started (every {interval}s)")
    print(f"  Missed check alert at {missed_alert}, kill switch at {missed_kill}")

    while True:
        cycle_start = time.time()

        try:
            result = run_monitoring_cycle()
            consecutive_misses = 0

            n_checked = result["positions_checked"]
            n_signals = len(result["exit_signals"])
            n_errors = len(result["errors"])
            exec_result = result.get("exit_execution", {"enabled": False,
                                                         "executed": [], "failed": [], "skipped": []})
            n_exec = len(exec_result.get("executed", []))
            n_exec_fail = len(exec_result.get("failed", []))

            status_tail = f" | auto-exit {n_exec}✓/{n_exec_fail}✗" if exec_result.get("enabled") else ""
            print(f"[{datetime.now().strftime('%H:%M:%S')}] "
                  f"Checked {n_checked} positions | "
                  f"{n_signals} exit signals | {n_errors} errors"
                  f"{status_tail}")

            # Print exit signals
            for sig in result["exit_signals"]:
                urgency_marker = "!!!" if sig.urgency == "immediate" else "..."
                print(f"  {urgency_marker} [{sig.reason}] {sig.market_id} "
                      f"{sig.side} {sig.entry_price:.2f}->{sig.current_price:.2f}")

            for rec in exec_result.get("executed", []):
                print(f"  EXIT ✓ [{rec.get('reason')}] {rec.get('market_id')} "
                      f"({rec.get('action')})")
            for rec in exec_result.get("failed", []):
                print(f"  EXIT ✗ [{rec.get('reason')}] {rec.get('market_id')}: "
                      f"{rec.get('error','?')[:80]}")

            for err in result["errors"]:
                print(f"  ERR: {err}")

        except Exception as e:
            consecutive_misses += 1
            log_event("monitor", "cycle_failed", {
                "error": str(e)[:200],
                "consecutive_misses": consecutive_misses,
            }, result="failed")

            print(f"  MISS #{consecutive_misses}: {e}")

            if consecutive_misses >= missed_kill:
                print(f"  KILL SWITCH: {consecutive_misses} consecutive misses!")
                log_event("monitor", "kill_switch_triggered", {
                    "reason": f"{consecutive_misses} consecutive monitoring failures",
                })
                from lib.kill_switch import activate_kill_switch
                activate_kill_switch(f"monitoring_missed_{consecutive_misses}")
                return

            if consecutive_misses >= missed_alert:
                log_event("monitor", "missed_check_alert", {
                    "consecutive_misses": consecutive_misses,
                })

        # Sleep for remainder of interval
        elapsed = time.time() - cycle_start
        sleep_time = max(0, interval - elapsed)
        if sleep_time > 0:
            time.sleep(sleep_time)
