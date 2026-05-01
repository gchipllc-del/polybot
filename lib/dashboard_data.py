"""
Dashboard Data Layer — aggregates all data sources for web + terminal dashboards.

Every function returns a plain dict (JSON-serializable) and handles errors
gracefully so the presentation layer never crashes.

Security:
    - No secrets in any returned data
    - Error messages sanitized (no stack traces to frontend)
    - All data read-only (no mutations)
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from lib.audit import get_recent_events
from lib.calibration import brier_score, calibration_curve, log_loss, source_accuracy
from lib.resolution_tracker import get_performance_summary

DATA_DIR = Path(__file__).parent.parent / "data"
POSITIONS_PATH = DATA_DIR / "positions.json"
TRADE_HISTORY_PATH = DATA_DIR / "trade_history.json"
CONFIG_PATH = Path(__file__).parent.parent / "config"

# Live-price cache: {(platform, market_id): (fetched_at_epoch, yes_price, no_price)}
# 30-second TTL keeps the dashboard responsive without hammering platform APIs.
_PRICE_CACHE: dict[tuple[str, str], tuple[float, float, float]] = {}
_PRICE_CACHE_TTL_SEC = 30.0


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def _fetch_live_price(platform: str, market_id: str) -> tuple[float, float] | None:
    """Fetch current (yes_price, no_price) with TTL caching. Returns None on failure."""
    if not platform or not market_id:
        return None

    key = (platform, market_id)
    now = time.time()

    cached = _PRICE_CACHE.get(key)
    if cached and (now - cached[0]) < _PRICE_CACHE_TTL_SEC:
        return (cached[1], cached[2])

    try:
        from lib.market_client import get_client
        client = get_client(platform)
        market = client.get_market(market_id)
        yes_p = float(getattr(market, "yes_price", 0) or 0)
        no_p = float(getattr(market, "no_price", 0) or 0)
        if 0 <= yes_p <= 1 and 0 <= no_p <= 1:
            _PRICE_CACHE[key] = (now, yes_p, no_p)
            return (yes_p, no_p)
    except Exception:
        # Never crash the dashboard on platform errors — fall back to stored price.
        pass

    return None


def _load_positions_with_live_prices() -> list[dict]:
    """Load positions and refresh current_price for open positions from the platform.

    Returns a list of position dicts with `current_price` replaced by the live
    market price when available; falls back to the stored value otherwise.
    Settled positions are returned unchanged (their exit price is final).
    """
    positions = _load_json(POSITIONS_PATH, [])
    refreshed = []
    for p in positions:
        if p.get("status") != "open":
            refreshed.append(p)
            continue

        live = _fetch_live_price(p.get("platform", ""), p.get("market_id", ""))
        if live is None:
            refreshed.append(p)
            continue

        yes_p, no_p = live
        side = (p.get("side") or "").upper()
        live_price = yes_p if side == "YES" else no_p if side == "NO" else None
        if live_price is None or live_price <= 0:
            refreshed.append(p)
            continue

        # Return a shallow copy so we never mutate the on-disk record
        updated = dict(p)
        updated["current_price"] = live_price
        refreshed.append(updated)

    return refreshed


def get_portfolio_summary() -> dict:
    """Portfolio value, bankroll, growth phase, calibration grade."""
    try:
        with open(CONFIG_PATH / "settings.yaml") as f:
            settings = yaml.safe_load(f)
        with open(CONFIG_PATH / "strategy.yaml") as f:
            strategy = yaml.safe_load(f)

        mode = settings.get("mode", "manifold")
        growth = strategy.get("growth", {})
        phase_labels = {
            1: "Survival ($50-$200)",
            2: "Acceleration ($200-$2k)",
            3: "Scaling ($2k-$10k)",
            4: "Preservation ($10k-$25k)",
        }
        # Phase computed below once we know live bankroll (auto-graduation).

        # Calculate total bankroll from platform balances
        total_balance = 0.0
        platform_balances = {}
        try:
            from lib.market_client import get_active_clients
            for client in get_active_clients():
                try:
                    bal = client.get_balance()
                    total_balance += bal
                    platform_balances[client.platform_name] = round(bal, 2)
                except Exception:
                    platform_balances[client.platform_name] = "unavailable"
        except Exception:
            pass

        # Positions value — refresh prices so the dashboard reflects reality
        positions = _load_positions_with_live_prices()
        open_positions = [p for p in positions if p.get("status") == "open"]
        position_value = sum(
            p.get("current_price", 0) * p.get("quantity", 0) for p in open_positions
        )

        # Calibration
        bs = brier_score()
        cal_grade = "N/A"
        if bs >= 0:
            cal_grade = "Excellent" if bs < 0.10 else "Good" if bs < 0.15 else "Fair" if bs < 0.20 else "Poor"

        # Unrealized P/L on currently-open positions (mark-to-market vs entry).
        # Despite the historical "daily_pl" name kept for backwards compat, this
        # is NOT today's P/L — it's the floating gain/loss on open inventory.
        unrealized_pnl = sum(
            (p.get("current_price", 0) - p.get("entry_price", 0)) * p.get("quantity", 0)
            for p in open_positions
        )

        # Realized P/L: net_profit summed across every settled trade since inception.
        trade_history = _load_json(TRADE_HISTORY_PATH, [])
        realized_pnl = sum(t.get("net_profit", 0) for t in trade_history)

        # Lifetime ("total dollar gains from inception") = realized + unrealized.
        lifetime_pnl = realized_pnl + unrealized_pnl

        # Phase auto-graduates with bankroll (Wave 2 polybot follow-up).
        from lib.phase import effective_phase, effective_max_positions
        live_bankroll = total_balance + position_value
        phase = effective_phase(live_bankroll, strategy)
        phase_max_positions = effective_max_positions(live_bankroll, strategy)

        return {
            "total_bankroll": round(total_balance + position_value, 2),
            "cash_balance": round(total_balance, 2),
            "position_value": round(position_value, 2),
            "platform_balances": platform_balances,
            "open_positions": len(open_positions),
            "daily_pl": round(unrealized_pnl, 2),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "realized_pnl": round(realized_pnl, 2),
            "lifetime_pnl": round(lifetime_pnl, 2),
            "mode": mode,
            "phase": phase,
            "phase_max_positions": phase_max_positions,
            "phase_label": phase_labels.get(phase, ""),
            "kelly_multiplier": strategy.get("kelly_multiplier", 0.25),
            "min_edge": strategy.get("scoring", {}).get("min_edge", 0.08),
            "brier_score": round(bs, 4) if bs >= 0 else None,
            "calibration_grade": cal_grade,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        return {"error": str(e)[:200], "timestamp": datetime.now(timezone.utc).isoformat()}


def _count_bets_for_market(market_id: str) -> int:
    """Count how many times we've placed a bet on this market across all history."""
    history = _load_json(TRADE_HISTORY_PATH, [])
    return sum(1 for t in history if t.get("market_id") == market_id)


def _resolution_countdown(resolution_date: str) -> dict:
    """Compute human-readable time-to-resolution from an ISO 8601 date string."""
    if not resolution_date:
        return {"days": None, "label": "—", "iso": ""}
    try:
        if isinstance(resolution_date, str):
            res_dt = datetime.fromisoformat(resolution_date.replace("Z", "+00:00"))
        elif isinstance(resolution_date, (int, float)):
            res_dt = datetime.fromtimestamp(resolution_date / 1000, tz=timezone.utc)
        else:
            return {"days": None, "label": "—", "iso": ""}

        now = datetime.now(timezone.utc)
        delta = res_dt - now

        total_hours = delta.total_seconds() / 3600
        days = int(delta.days)
        hours = int(total_hours % 24)

        if total_hours <= 0:
            label = "RESOLVING"
        elif days == 0:
            label = f"{hours}h"
        elif days < 7:
            label = f"{days}d {hours}h"
        elif days < 30:
            label = f"{days}d"
        else:
            label = f"{days}d ({res_dt.strftime('%b %-d')})"

        return {
            "days": round(delta.total_seconds() / 86400, 1),
            "label": label,
            "iso": res_dt.isoformat(),
            "date_short": res_dt.strftime("%b %-d"),
        }
    except (ValueError, TypeError, OSError):
        return {"days": None, "label": "—", "iso": ""}


def get_positions_table() -> list[dict]:
    """All positions with current P/L, bet count, invested amount, and resolution countdown."""
    positions = _load_positions_with_live_prices()
    result = []

    for p in positions:
        if p.get("status") not in ("open", "settled"):
            continue

        entry = p.get("entry_price", 0)
        current = p.get("current_price", 0)
        qty = p.get("quantity", 0)
        pnl = (current - entry) * qty
        pnl_pct = (current - entry) / entry if entry > 0 else 0

        market_id = p.get("market_id", "")
        total_invested = entry * qty
        bet_count = _count_bets_for_market(market_id)
        resolution = _resolution_countdown(p.get("resolution_date", ""))

        result.append({
            "market_id": market_id,
            "platform": p.get("platform", ""),
            "question": p.get("question", "")[:60],
            "category": p.get("category", ""),
            "side": p.get("side", ""),
            "quantity": qty,
            "entry_price": entry,
            "current_price": current,
            "total_invested": round(total_invested, 2),
            "bet_count": bet_count,
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 4),
            "composite_score": p.get("composite_score", 0),
            "edge_at_entry": p.get("edge_at_entry", 0),
            "our_probability": p.get("our_probability", 0),
            "status": p.get("status", "open"),
            "opened_at": p.get("opened_at", ""),
            "resolution_date": p.get("resolution_date", ""),
            "resolution_countdown": resolution,
        })

    return result


def get_calibration_data() -> dict:
    """Calibration stats for the dashboard."""
    bs = brier_score()
    ll = log_loss()
    curve = calibration_curve()
    sa = source_accuracy()

    return {
        "brier_score": round(bs, 4) if bs >= 0 else None,
        "log_loss": round(ll, 4) if ll >= 0 else None,
        "calibration_curve": curve,
        "source_accuracy": sa,
    }


def get_trade_history() -> dict:
    """Completed trades with cumulative P/L series for charting.

    Filters to entries that have actually resolved (`won` set to a bool by
    settle_position) — open positions written to history mid-flight would
    otherwise corrupt win-rate and tank the displayed total_pnl.
    """
    history_all = _load_json(TRADE_HISTORY_PATH, [])
    history = [t for t in history_all if isinstance(t.get("won"), bool)]
    if not history:
        return {"trades": [], "total_pnl": 0, "win_rate": 0, "total_trades": 0, "pl_series": []}

    total_pnl = sum(t.get("net_profit", 0) for t in history)
    wins = sum(1 for t in history if t.get("won", False))

    cumulative = 0
    pl_series = []
    for t in history:
        cumulative += t.get("net_profit", 0)
        pl_series.append({
            "date": t.get("closed_at", "")[:10],
            "pl": round(cumulative, 2),
            "market": t.get("question", "")[:40],
        })

    return {
        "trades": history,
        "total_pnl": round(total_pnl, 2),
        "win_rate": round(wins / len(history), 4) if history else 0,
        "total_trades": len(history),
        "pl_series": pl_series,
    }


def get_events(n: int = 30) -> list[dict]:
    """Recent audit events with summary lines."""
    events = get_recent_events(n)
    for e in events:
        details = e.get("details", {})
        summary_parts = []
        for key in ["market_id", "platform", "side", "edge", "score", "error", "reason"]:
            if key in details:
                val = details[key]
                if isinstance(val, float):
                    val = f"{val:.3f}"
                summary_parts.append(f"{key}={val}")
        e["summary"] = ", ".join(summary_parts[:5]) if summary_parts else ""
    return events


def get_circuit_breaker_status() -> dict:
    """Circuit breaker limits vs current values."""
    try:
        with open(CONFIG_PATH / "settings.yaml") as f:
            settings = yaml.safe_load(f)

        cb = settings.get("circuit_breakers", {})
        positions = _load_positions_with_live_prices()
        open_positions = [p for p in positions if p.get("status") == "open"]

        # Daily P/L
        daily_pl = sum(
            (p.get("current_price", 0) - p.get("entry_price", 0)) * p.get("quantity", 0)
            for p in open_positions
        )

        # Largest single-position pct vs actual bankroll.
        # max_per_market_pct is a per-market limit, so we compare the single
        # largest position's cost basis (matching check_position_size enforcement)
        # against real bankroll (cash + position market value).
        cash_balance = 0.0
        try:
            from lib.market_client import get_active_clients
            for client in get_active_clients():
                try:
                    cash_balance += client.get_balance()
                except Exception:
                    pass
        except Exception:
            pass

        position_market_value = sum(
            p.get("current_price", 0) * p.get("quantity", 0) for p in open_positions
        )
        bankroll = cash_balance + position_market_value

        max_pos_pct = 0
        for p in open_positions:
            pos_val = p.get("entry_price", 0) * p.get("quantity", 0)
            pct = pos_val / bankroll if bankroll > 0 else 0
            max_pos_pct = max(max_pos_pct, pct)

        # Daily-loss effective limit: same two-layer logic as check_daily_loss.
        # Whichever is more lenient (further from zero) wins; keeps the
        # absolute floor in play if bankroll falls below the pct threshold.
        max_loss_dollar = float(cb.get("max_daily_loss", -10))
        max_loss_pct = cb.get("max_daily_loss_pct")
        if max_loss_pct is not None and bankroll > 0:
            effective_loss_limit = min(max_loss_dollar, float(max_loss_pct) * bankroll)
        else:
            effective_loss_limit = max_loss_dollar
        breakers = {
            "daily_loss": {
                "limit": round(effective_loss_limit, 2),
                "current": round(daily_pl, 2),
                "pct_used": round(abs(daily_pl / effective_loss_limit), 2)
                            if daily_pl < 0 and effective_loss_limit < 0 else 0,
                "tripped": daily_pl <= effective_loss_limit,
            },
            "position_size": {
                "limit": cb.get("max_per_market_pct", 0.15),
                "current": round(max_pos_pct, 4),
                "pct_used": round(max_pos_pct / cb.get("max_per_market_pct", 0.15), 2) if cb.get("max_per_market_pct") else 0,
                "tripped": max_pos_pct > cb.get("max_per_market_pct", 0.15),
            },
            "open_positions": {
                "limit": cb.get("max_open_positions", 10),
                "current": len(open_positions),
                "pct_used": round(len(open_positions) / cb.get("max_open_positions", 10), 2),
                "tripped": len(open_positions) >= cb.get("max_open_positions", 10),
            },
        }

        return {
            "breakers": breakers,
            "paper_mode": settings.get("mode") == "manifold",
            "live_approved": settings.get("live_migration_approved", False),
        }
    except Exception as e:
        return {"error": str(e)[:200]}


def get_full_dashboard_state() -> dict:
    """All dashboard data in one call."""
    return {
        "portfolio": get_portfolio_summary(),
        "positions": get_positions_table(),
        "calibration": get_calibration_data(),
        "events": get_events(20),
        "history": get_trade_history(),
        "performance": get_performance_summary(),
        "breakers": get_circuit_breaker_status(),
    }
