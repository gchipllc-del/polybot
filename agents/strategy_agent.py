"""
Strategy Agent — proposes prediction market trades based on forecasts.

Part of the 3-agent governance system:
    Strategy Agent → proposes trades (this file)
    Risk Agent → validates risk, can VETO
    Compliance Agent → checks platform rules + market validity

Trade proceeds ONLY with unanimous consent. This agent cannot execute.

Security:
    - Cannot call platform APIs directly
    - All proposals logged to audit trail before review
    - Diary entries compressed and append-only
"""

from datetime import datetime, timezone

from tradingcore.audit import log_event
from lib.market_scanner import MarketCandidate


class StrategyAgent:
    """Proposes trades. Cannot execute — only Risk + Compliance can approve."""

    name = "strategy_agent"

    def propose_trade(self, candidate: MarketCandidate) -> dict:
        """
        Build a trade proposal from a scored MarketCandidate for agent consensus.

        The proposal contains everything Risk and Compliance need to review,
        plus the full evidence chain for audit.

        Args:
            candidate: A scored, ranked MarketCandidate from the scanner.

        Returns:
            Proposal dict with market info, forecast data, and scoring.
        """
        market = candidate.market
        forecast = candidate.forecast

        proposal = {
            "agent": self.name,
            "action": f"buy_{forecast.best_side.lower()}",
            "market_id": market.market_id,
            "platform": market.platform,
            "question": market.question,
            "category": market.category,
            "resolution_date": market.resolution_date,

            # Forecast data
            "side": forecast.best_side,
            "our_probability": forecast.probability,
            "market_probability": forecast.market_probability,
            "edge": forecast.edge,
            "confidence": forecast.confidence,

            # Scoring
            "evidence_score": forecast.evidence_score,
            "calibration_score": forecast.calibration_score,
            "edge_score": forecast.edge_score,
            "composite_score": forecast.composite_score,

            # Sizing
            "kelly_fraction": forecast.kelly_fraction,
            "kelly_bet_usd": candidate.kelly_bet_usd,
            "expected_value": forecast.expected_value,

            # Evidence chain
            "sources": forecast.sources,
            "evidence_summary": forecast.evidence_summary,

            # Metadata
            "rank": candidate.rank,
            "correlation_group": candidate.correlation_group,
            "volume_24h": market.volume_24h,
            "total_volume": market.total_volume,
            "proposed_at": datetime.now(timezone.utc).isoformat(),
        }

        # Write to diary (memory palace)
        try:
            from lib.memory_palace import diary_write
            diary_write(
                self.name,
                f"PROPOSE|{market.platform}|{market.market_id}|"
                f"{forecast.best_side}|edge_{forecast.edge:+.2%}|"
                f"score_{forecast.composite_score}/9|kelly_${candidate.kelly_bet_usd:.2f}",
            )
        except ImportError:
            pass  # Memory palace not yet built

        log_event("agent", "strategy_proposed", {
            "market_id": market.market_id,
            "platform": market.platform,
            "side": forecast.best_side,
            "edge": forecast.edge,
            "score": forecast.composite_score,
            "kelly_usd": candidate.kelly_bet_usd,
        })

        return proposal
