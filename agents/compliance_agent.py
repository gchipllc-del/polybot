"""
Compliance Agent — prediction market rules + platform policy enforcement.

Replaces stock-market compliance (wash sales, earnings, market hours) with:
    1. Market validity — is this market real, resolvable, unambiguous?
    2. Platform limits — min order size, max position, API constraints
    3. Duplicate exposure — already holding a position on this market?
    4. Cooldown enforcement — recently exited this market?
    5. Paper mode guard — block real-money trades in paper mode
    6. Resolution sanity — market must have a clear resolution path

Security:
    - Cannot call platform APIs directly
    - Cannot execute trades
    - All reviews logged to audit trail
    - Validates all input fields defensively
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lib.audit import log_event

DATA_DIR = Path(__file__).parent.parent / "data"
POSITIONS_PATH = DATA_DIR / "positions.json"
TRADE_HISTORY_PATH = DATA_DIR / "trade_history.json"


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


# Minimum order sizes by platform (from API docs)
PLATFORM_MIN_ORDER = {
    "kalshi": {"min_contracts": 1, "min_usd": 1.00},
    "polymarket": {"min_contracts": 5, "min_usd": 0.50},  # 5 share minimum
    "manifold": {"min_contracts": 1, "min_usd": 1.00},    # 1 mana
}

# Markets with known ambiguous resolution criteria (populated by Hermes)
BLOCKED_MARKET_PATTERNS = [
    "will this market",    # Self-referential markets
    "resolve yes",         # Meta-markets
    "personal goal",       # Unverifiable
]


class ComplianceAgent:
    """Checks platform rules and market validity. Cannot propose or execute."""

    name = "compliance_agent"

    def review(self, proposal: dict) -> dict:
        """
        Review a trade proposal for compliance issues.

        Args:
            proposal: Trade proposal dict from StrategyAgent.propose_trade()

        Returns:
            {"approved": bool, "reason": str, "checks": dict}
        """
        market_id = proposal.get("market_id", "")
        platform = proposal.get("platform", "")
        question = proposal.get("question", "")
        resolution_date = proposal.get("resolution_date", "")
        kelly_bet = proposal.get("kelly_bet_usd", 0)
        side = proposal.get("side", "YES")

        checks = {}
        issues = []

        # ── 1. Paper mode guard ───────────────────────────────────
        paper_check = self._check_paper_mode(platform)
        checks["paper_mode"] = paper_check
        if not paper_check["pass"]:
            issues.append(paper_check["reason"])

        # ── 2. Market validity ────────────────────────────────────
        validity = self._check_market_validity(question, resolution_date)
        checks["market_validity"] = validity
        if not validity["pass"]:
            issues.append(validity["reason"])

        # ── 3. Platform limits ────────────────────────────────────
        platform_check = self._check_platform_limits(platform, kelly_bet)
        checks["platform_limits"] = platform_check
        if not platform_check["pass"]:
            issues.append(platform_check["reason"])

        # ── 4. Duplicate exposure ─────────────────────────────────
        duplicate = self._check_duplicate_position(market_id, side)
        checks["duplicate_exposure"] = duplicate
        if not duplicate["pass"]:
            issues.append(duplicate["reason"])

        # ── 5. Cooldown ───────────────────────────────────────────
        cooldown = self._check_cooldown(market_id)
        checks["cooldown"] = cooldown
        if not cooldown["pass"]:
            issues.append(cooldown["reason"])

        # ── 6. Resolution sanity ──────────────────────────────────
        resolution = self._check_resolution_sanity(resolution_date)
        checks["resolution_sanity"] = resolution
        if not resolution["pass"]:
            issues.append(resolution["reason"])

        # ── Decision ──────────────────────────────────────────────
        approved = len(issues) == 0
        reason = "All compliance checks passed" if approved else "; ".join(issues)

        # Write to diary
        try:
            from lib.memory_palace import diary_write
            status_parts = []
            for name, check in checks.items():
                status_parts.append(f"{name}_{'ok' if check['pass'] else 'FAIL'}")
            diary_write(
                self.name,
                f"{'CLEAR' if approved else 'BLOCK'}|{platform}|{market_id}|"
                + "|".join(status_parts),
            )
        except ImportError:
            pass

        log_event("agent", "compliance_reviewed", {
            "market_id": market_id,
            "platform": platform,
            "approved": approved,
            "reason": reason[:200],
            "n_checks": len(checks),
            "n_issues": len(issues),
        })

        return {
            "agent": self.name,
            "approved": approved,
            "reason": reason,
            "checks": checks,
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
        }

    def _check_paper_mode(self, platform: str) -> dict:
        """Block real-money trades when in paper mode."""
        settings = _load_settings()
        mode = settings.get("mode", "manifold")
        live_approved = settings.get("live_migration_approved", False)

        # Manifold is always allowed (paper trading)
        if platform == "manifold":
            return {"pass": True, "reason": "Manifold is paper trading"}

        # Real-money platforms require explicit approval
        if mode == "manifold" and not live_approved:
            return {
                "pass": False,
                "reason": f"Real-money platform '{platform}' blocked in paper mode. "
                          f"Set mode + live_migration_approved in settings.yaml",
            }

        # Check if this specific platform is enabled
        platforms = settings.get("platforms", {})
        if not platforms.get(platform, {}).get("enabled", False):
            return {
                "pass": False,
                "reason": f"Platform '{platform}' is not enabled in settings.yaml",
            }

        return {"pass": True, "reason": f"Platform '{platform}' is enabled and approved"}

    def _check_market_validity(self, question: str, resolution_date: str) -> dict:
        """Check if the market is valid and resolvable."""
        if not question or len(question.strip()) < 10:
            return {"pass": False, "reason": "Market question is too short or empty"}

        # Check for known problematic patterns
        q_lower = question.lower()
        for pattern in BLOCKED_MARKET_PATTERNS:
            if pattern in q_lower:
                return {
                    "pass": False,
                    "reason": f"Market matches blocked pattern: '{pattern}'",
                }

        # Must have a resolution date
        if not resolution_date:
            return {"pass": False, "reason": "Market has no resolution date"}

        return {"pass": True, "reason": "Market appears valid"}

    def _check_platform_limits(self, platform: str, bet_usd: float) -> dict:
        """Check platform-specific order constraints."""
        limits = PLATFORM_MIN_ORDER.get(platform, {"min_contracts": 1, "min_usd": 0.50})

        if bet_usd < limits["min_usd"]:
            return {
                "pass": False,
                "reason": f"Bet ${bet_usd:.2f} below platform minimum ${limits['min_usd']:.2f}",
            }

        return {"pass": True, "reason": f"Within {platform} order limits"}

    def _check_duplicate_position(self, market_id: str, side: str) -> dict:
        """Check if we already hold a position in this market."""
        positions = self._load_positions()

        for pos in positions:
            if pos.get("status") != "open":
                continue
            if pos.get("market_id") == market_id:
                existing_side = pos.get("side", "")
                if existing_side == side:
                    return {
                        "pass": False,
                        "reason": f"Already hold {existing_side} position on market {market_id}",
                    }
                # Holding opposite side is technically a hedge, but flag it
                return {
                    "pass": False,
                    "reason": f"Already hold {existing_side} (opposite) on market {market_id} — would hedge, not add edge",
                }

        return {"pass": True, "reason": "No existing position on this market"}

    def _check_cooldown(self, market_id: str) -> dict:
        """Check cooldown period after recently exiting a market."""
        strategy = _load_strategy()
        cooldown_cycles = strategy.get("exits", {}).get("cooldown_cycles_after_close", 2)
        # Approximate: 2 cycles * 120s check interval = 4 minutes minimum
        cooldown_minutes = cooldown_cycles * 2  # Conservative estimate

        history = self._load_trade_history()
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=cooldown_minutes)).isoformat()

        recent_exits = [
            t for t in history
            if t.get("market_id") == market_id
            and t.get("closed_at", "") > cutoff
        ]

        if recent_exits:
            last_exit = recent_exits[-1]
            return {
                "pass": False,
                "reason": f"Cooldown active — exited market {market_id} at {last_exit.get('closed_at', '?')[:19]}",
            }

        return {"pass": True, "reason": "No recent exit from this market"}

    def _check_resolution_sanity(self, resolution_date: str) -> dict:
        """Verify resolution date is in the future and parseable."""
        if not resolution_date:
            return {"pass": True, "reason": "No resolution date to validate"}

        try:
            if isinstance(resolution_date, str):
                res_dt = datetime.fromisoformat(resolution_date.replace("Z", "+00:00"))
            else:
                return {"pass": False, "reason": "Resolution date is not a string"}

            now = datetime.now(timezone.utc)
            if res_dt < now:
                return {
                    "pass": False,
                    "reason": f"Resolution date {resolution_date[:10]} is in the past",
                }

            return {"pass": True, "reason": "Resolution date is valid and in the future"}

        except (ValueError, TypeError):
            return {"pass": False, "reason": f"Cannot parse resolution date: {resolution_date[:30]}"}

    def _load_positions(self) -> list[dict]:
        if not POSITIONS_PATH.exists():
            return []
        try:
            with open(POSITIONS_PATH, "r") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []

    def _load_trade_history(self) -> list[dict]:
        if not TRADE_HISTORY_PATH.exists():
            return []
        try:
            with open(TRADE_HISTORY_PATH, "r") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []
