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


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def get_portfolio_summary() -> dict:
    """Portfolio value, bankroll, growth phase, calibration grade."""
    try:
        with open(CONFIG_PATH / "settings.yaml") as f:
            settings = yaml.safe_load(f)
        with open(CONFIG_PATH / "strategy.yaml") as f:
            strategy = yaml.safe_load(f)

        mode = settings.get("mode", "manifold")
        growth = strategy.get("growth", {})
        phase = growth.get("phase", 1)
        phase_labels = {
            1: "Survival ($50-$200)",
            2: "Acceleration ($200-$2k)",
            3: "Scaling ($2k-$10k)",
            4: "Preservation ($10k-$25k)",
        }

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

        # Positions value
        positions = _load_json(POSITIONS_PATH, [])
        open_positions = [p for p in positions if p.get("status") == "open"]
        position_value = sum(
            p.get("current_price", 0) * p.get("quantity", 0) for p in open_positions
        )

        # Calibration
        bs = brier_score()
        cal_grade = "N/A"
        if bs >= 0:
            cal_grade = "Excellent" if bs < 0.10 else "Good" if bs < 0.15 else "Fair" if bs < 0.20 else "Poor"

        # Daily P/L from open positions
        daily_pl = sum(
            (p.get("current_price", 0) - p.get("entry_price", 0)) * p.get("quantity", 0)
            for p in open_positions
        )

        return {
            "total_bankroll": round(total_balance + position_value, 2),
            "cash_balance": round(total_balance, 2),
            "position_value": round(position_value, 2),
            "platform_balances": platform_balances,
            "open_positions": len(open_positions),
            "daily_pl": round(daily_pl, 2),
            "mode": mode,
            "phase": phase,
            "phase_label": phase_labels.get(phase, ""),
            "kelly_multiplier": strategy.get("kelly_multiplier", 0.25),
            "min_edge": strategy.get("scoring", {}).get("min_edge", 0.08),
            "brier_score": round(bs, 4) if bs >= 0 else None,
            "calibration_grade": cal_grade,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        return {"error": str(e)[:200], "timestamp": datetime.now(timezone.utc).isoformat()}


def get_positions_table() -> list[dict]:
    """All positions with current P/L."""
    positions = _load_json(POSITIONS_PATH, [])
    result = []

    for p in positions:
        if p.get("status") not in ("open", "settled"):
            continue

        entry = p.get("entry_price", 0)
        current = p.get("current_price", 0)
        qty = p.get("quantity", 0)
        pnl = (current - entry) * qty
        pnl_pct = (current - entry) / entry if entry > 0 else 0

        result.append({
            "market_id": p.get("market_id", ""),
            "platform": p.get("platform", ""),
            "question": p.get("question", "")[:60],
            "category": p.get("category", ""),
            "side": p.get("side", ""),
            "quantity": qty,
            "entry_price": entry,
            "current_price": current,
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 4),
            "composite_score": p.get("composite_score", 0),
            "our_probability": p.get("our_probability", 0),
            "status": p.get("status", "open"),
            "opened_at": p.get("opened_at", ""),
            "resolution_date": p.get("resolution_date", ""),
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
    """Completed trades with cumulative P/L series for charting."""
    history = _load_json(TRADE_HISTORY_PATH, [])
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
        positions = _load_json(POSITIONS_PATH, [])
        open_positions = [p for p in positions if p.get("status") == "open"]

        # Daily P/L
        daily_pl = sum(
            (p.get("current_price", 0) - p.get("entry_price", 0)) * p.get("quantity", 0)
            for p in open_positions
        )

        # Largest position pct
        # Approximate bankroll from config
        max_pos_pct = 0
        total_invested = sum(p.get("entry_price", 0) * p.get("quantity", 0) for p in open_positions)
        bankroll = total_invested + 50  # rough estimate
        for p in open_positions:
            pos_val = p.get("entry_price", 0) * p.get("quantity", 0)
            pct = pos_val / bankroll if bankroll > 0 else 0
            max_pos_pct = max(max_pos_pct, pct)

        breakers = {
            "daily_loss": {
                "limit": cb.get("max_daily_loss", -10),
                "current": round(daily_pl, 2),
                "pct_used": round(abs(daily_pl / cb.get("max_daily_loss", -10)), 2) if daily_pl < 0 else 0,
                "tripped": daily_pl <= cb.get("max_daily_loss", -10),
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
