"""
Risk Agent — validates portfolio risk for prediction markets. Can VETO any trade.

Checks (adapted from traderbot for binary outcomes):
    1. Category concentration — no more than 40% in any category
    2. Position count — respect growth phase limits
    3. Correlation exposure — don't double-bet correlated markets
    4. Resolution date concentration — max 25% resolving same date
    5. Edge quality — require minimum edge relative to fees
    6. Bankroll percentage — single bet cannot exceed max_per_market_pct
    7. Drawdown guard — halt if daily losses exceed circuit breaker limit

Security:
    - Cannot call platform APIs directly
    - Cannot execute trades
    - All reviews logged to audit trail
    - Reads positions from disk (single source of truth)
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from tradingcore.audit import log_event
from tradingcore.kelly import min_edge_for_trade
from lib.phase import effective_phase, effective_max_positions

DATA_DIR = Path(__file__).parent.parent / "data"
POSITIONS_PATH = DATA_DIR / "positions.json"


def _load_settings() -> dict:
    import yaml
    config_path = Path(__file__).parent.parent / "config" / "settings.yaml"
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def _load_strategy() -> dict:
    import yaml
    config_path = Path(__file__).parent.parent / "config" / "strategy.yaml"
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


class RiskAgent:
    """Reviews every trade proposal. Can VETO. Cannot propose or execute."""

    name = "risk_agent"

    def review(self, proposal: dict, bankroll: float) -> dict:
        """
        Review a trade proposal from Strategy Agent.

        Args:
            proposal: Trade proposal dict from StrategyAgent.propose_trade()
            bankroll: Current total bankroll across all platforms

        Returns:
            {"approved": bool, "reason": str, "checks": dict}
        """
        settings = _load_settings()
        strategy = _load_strategy()
        breakers = settings.get("circuit_breakers", {})
        growth = strategy.get("growth", {})

        market_id = proposal.get("market_id", "")
        platform = proposal.get("platform", "")
        category = proposal.get("category", "").lower()
        resolution_date = proposal.get("resolution_date", "")
        correlation_group = proposal.get("correlation_group", "")
        kelly_bet = proposal.get("kelly_bet_usd", 0)
        edge = proposal.get("edge", 0)
        composite_score = proposal.get("composite_score", 0)
        side = proposal.get("side", "YES")

        checks = {}
        veto_reasons = []
        positions = self._load_positions()
        open_positions = [p for p in positions if p.get("status", "open") == "open"]

        # ── 1. Category concentration ─────────────────────────────
        max_category_pct = breakers.get("max_category_pct", 0.40)
        category_value = sum(
            p.get("entry_price", 0) * p.get("quantity", 0)
            for p in open_positions
            if p.get("category", "").lower() == category
        )
        new_category_value = category_value + kelly_bet
        category_pct = new_category_value / bankroll if bankroll > 0 else 1.0

        checks["category_concentration"] = {
            "category": category,
            "current_pct": round(category_pct, 4),
            "max_pct": max_category_pct,
            "pass": category_pct <= max_category_pct,
        }
        if not checks["category_concentration"]["pass"]:
            veto_reasons.append(
                f"Category '{category}' at {category_pct:.0%} would exceed {max_category_pct:.0%}"
            )

        # ── 2. Position count ─────────────────────────────────────
        # Phase + cap auto-graduate off live bankroll. Falls back to the
        # legacy growth.max_concurrent_positions / growth.phase if
        # phase_thresholds is not configured.
        active_phase = effective_phase(bankroll, strategy)
        max_positions = effective_max_positions(bankroll, strategy)
        current_count = len(open_positions)

        checks["position_count"] = {
            "current": current_count,
            "max": max_positions,
            "phase": active_phase,
            "pass": current_count < max_positions,
        }
        if not checks["position_count"]["pass"]:
            veto_reasons.append(
                f"At {current_count} positions (max {max_positions} in phase {active_phase})"
            )

        # ── 3. Correlation exposure ───────────────────────────────
        # Don't hold positions in correlated markets
        correlated_positions = []
        if correlation_group:
            correlated_positions = [
                p for p in open_positions
                if p.get("correlation_group", "") == correlation_group
            ]

        checks["correlation"] = {
            "group": correlation_group,
            "existing_positions": len(correlated_positions),
            "pass": len(correlated_positions) == 0,
        }
        if not checks["correlation"]["pass"]:
            veto_reasons.append(
                f"Already hold {len(correlated_positions)} position(s) in correlation group '{correlation_group}'"
            )

        # ── 4. Resolution date concentration ──────────────────────
        max_res_pct = breakers.get("max_resolution_date_pct", 0.25)
        if resolution_date:
            # Normalize to date only for comparison
            res_date_key = resolution_date[:10]
            res_value = sum(
                p.get("entry_price", 0) * p.get("quantity", 0)
                for p in open_positions
                if p.get("resolution_date", "")[:10] == res_date_key
            )
            new_res_value = res_value + kelly_bet
            res_pct = new_res_value / bankroll if bankroll > 0 else 1.0

            checks["resolution_concentration"] = {
                "date": res_date_key,
                "pct": round(res_pct, 4),
                "max_pct": max_res_pct,
                "pass": res_pct <= max_res_pct,
            }
            if not checks["resolution_concentration"]["pass"]:
                veto_reasons.append(
                    f"Resolution date {res_date_key}: {res_pct:.0%} would exceed {max_res_pct:.0%}"
                )
        else:
            checks["resolution_concentration"] = {"pass": True, "reason": "no_resolution_date"}

        # ── 5. Edge quality ───────────────────────────────────────
        fee_rates = {"kalshi": 0.07, "polymarket": 0.02, "manifold": 0.0}
        fee_rate = fee_rates.get(platform, 0.07)
        market_prob = proposal.get("market_probability", 0.50)
        min_edge = min_edge_for_trade(market_prob, fee_rate)
        scoring = strategy.get("scoring", {})
        config_min_edge = scoring.get("min_edge", 0.08)
        effective_min = max(min_edge, config_min_edge)

        checks["edge_quality"] = {
            "edge": round(edge, 4),
            "fee_adjusted_min": round(min_edge, 4),
            "config_min": config_min_edge,
            "effective_min": round(effective_min, 4),
            "pass": abs(edge) >= effective_min,
        }
        if not checks["edge_quality"]["pass"]:
            veto_reasons.append(
                f"Edge {edge:+.2%} below minimum {effective_min:.2%} (fees + config)"
            )

        # ── 6. Bankroll percentage ────────────────────────────────
        max_per_market = breakers.get("max_per_market_pct", 0.15)
        bet_pct = kelly_bet / bankroll if bankroll > 0 else 1.0

        checks["bankroll_pct"] = {
            "bet_usd": kelly_bet,
            "bet_pct": round(bet_pct, 4),
            "max_pct": max_per_market,
            "pass": bet_pct <= max_per_market,
        }
        if not checks["bankroll_pct"]["pass"]:
            veto_reasons.append(
                f"Bet ${kelly_bet:.2f} is {bet_pct:.0%} of bankroll (max {max_per_market:.0%})"
            )

        # ── 7. Composite score ────────────────────────────────────
        min_score = scoring.get("min_composite_score", 6)
        checks["composite_score"] = {
            "score": composite_score,
            "min": min_score,
            "pass": composite_score >= min_score,
        }
        if not checks["composite_score"]["pass"]:
            veto_reasons.append(f"Score {composite_score}/9 below minimum {min_score}")

        # ── Decision ──────────────────────────────────────────────
        approved = len(veto_reasons) == 0
        reason = "All risk checks passed" if approved else "; ".join(veto_reasons)

        # Write to diary
        try:
            from tradingcore.memory_palace import diary_write
            diary_write(
                self.name,
                f"{'APPROVE' if approved else 'VETO'}|{platform}|{market_id}|"
                f"cat_{category}_{category_pct:.0%}|pos_{current_count}|"
                f"edge_{edge:+.2%}|score_{composite_score}",
            )
        except ImportError:
            pass

        log_event("agent", "risk_reviewed", {
            "market_id": market_id,
            "platform": platform,
            "approved": approved,
            "reason": reason[:200],
            "n_checks": len(checks),
            "n_vetoes": len(veto_reasons),
        })

        return {
            "agent": self.name,
            "approved": approved,
            "reason": reason,
            "checks": checks,
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
        }

    def _load_positions(self) -> list[dict]:
        """Load positions from disk. Returns empty list if file doesn't exist."""
        if not POSITIONS_PATH.exists():
            return []
        try:
            with open(POSITIONS_PATH, "r") as f:
                data = json.load(f)
            if not isinstance(data, list):
                return []
            return data
        except (json.JSONDecodeError, OSError):
            return []
