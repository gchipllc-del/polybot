"""
Agent Consensus Protocol — 3-gate unanimous consent for prediction markets.

Trade proceeds ONLY if:
    1. Strategy Agent PROPOSES (builds the trade proposal)
    2. Risk Agent APPROVES (does not VETO — risk checks pass)
    3. Compliance Agent APPROVES (no platform/validity issues)

Unanimous consent required. Any single agent can block a trade.
The execution layer (order_gate.step3_execute) lives behind ALL three gates.
No agent can directly call the platform API.

Security:
    - Sequential evaluation: Risk sees Strategy's proposal, Compliance sees both
    - Short-circuit on first rejection (no wasted API calls)
    - Full audit trail of every consensus decision
    - Immutable once decided — no retries without new proposal
"""

from datetime import datetime, timezone

from agents.compliance_agent import ComplianceAgent
from agents.risk_agent import RiskAgent
from agents.strategy_agent import StrategyAgent
from tradingcore.audit import log_event
from lib.market_scanner import MarketCandidate

strategy = StrategyAgent()
risk = RiskAgent()
compliance = ComplianceAgent()


def seek_consensus(
    candidate: MarketCandidate,
    bankroll: float,
) -> dict:
    """
    Run the 3-agent consensus process for a prediction market trade.

    Args:
        candidate: Scored MarketCandidate from the scanner
        bankroll: Current total bankroll for risk sizing

    Returns:
        {
            "approved": bool,
            "proposal": dict,
            "risk_review": dict | None,
            "compliance_review": dict | None,
            "decision": "EXECUTE" | "VETOED" | "BLOCKED",
            "blocking_agent": str | None,
            "reason": str | None,
        }
    """
    market_id = candidate.market.market_id
    platform = candidate.market.platform

    # ── Step 1: Strategy proposes ─────────────────────────────────
    proposal = strategy.propose_trade(candidate)

    # ── Step 2: Risk reviews ──────────────────────────────────────
    risk_review = risk.review(proposal, bankroll)

    if not risk_review["approved"]:
        log_event("consensus", "vetoed_by_risk", {
            "market_id": market_id,
            "platform": platform,
            "reason": risk_review["reason"][:200],
        })

        # Write to diary
        try:
            from lib.memory_palace import diary_write
            diary_write("consensus",
                f"VETOED|{platform}|{market_id}|by_risk|{risk_review['reason'][:80]}")
        except ImportError:
            pass

        return {
            "approved": False,
            "proposal": proposal,
            "risk_review": risk_review,
            "compliance_review": None,
            "decision": "VETOED",
            "blocking_agent": "risk_agent",
            "reason": risk_review["reason"],
        }

    # ── Step 3: Compliance reviews ────────────────────────────────
    compliance_review = compliance.review(proposal)

    if not compliance_review["approved"]:
        log_event("consensus", "blocked_by_compliance", {
            "market_id": market_id,
            "platform": platform,
            "reason": compliance_review["reason"][:200],
        })

        try:
            from lib.memory_palace import diary_write
            diary_write("consensus",
                f"BLOCKED|{platform}|{market_id}|by_compliance|{compliance_review['reason'][:80]}")
        except ImportError:
            pass

        return {
            "approved": False,
            "proposal": proposal,
            "risk_review": risk_review,
            "compliance_review": compliance_review,
            "decision": "BLOCKED",
            "blocking_agent": "compliance_agent",
            "reason": compliance_review["reason"],
        }

    # ── UNANIMOUS CONSENT — proceed to execution ──────────────────
    log_event("consensus", "approved", {
        "market_id": market_id,
        "platform": platform,
        "side": proposal.get("side"),
        "edge": proposal.get("edge"),
        "score": proposal.get("composite_score"),
        "kelly_usd": proposal.get("kelly_bet_usd"),
    }, result="success")

    try:
        from lib.memory_palace import diary_write
        diary_write("consensus",
            f"APPROVED|{platform}|{market_id}|"
            f"{proposal.get('side')}|edge_{proposal.get('edge', 0):+.2%}|"
            f"score_{proposal.get('composite_score')}/9|"
            f"kelly_${proposal.get('kelly_bet_usd', 0):.2f}")
    except ImportError:
        pass

    return {
        "approved": True,
        "proposal": proposal,
        "risk_review": risk_review,
        "compliance_review": compliance_review,
        "decision": "EXECUTE",
        "blocking_agent": None,
        "reason": None,
    }


def print_consensus_result(result: dict):
    """Pretty-print a consensus decision."""
    decision = result["decision"]
    proposal = result.get("proposal", {})
    market_id = proposal.get("market_id", "?")
    platform = proposal.get("platform", "?")
    question = proposal.get("question", "?")[:50]

    if decision == "EXECUTE":
        side = proposal.get("side", "?")
        edge = proposal.get("edge", 0)
        kelly = proposal.get("kelly_bet_usd", 0)
        print(f"  APPROVED  {side} {question}")
        print(f"            [{platform}] edge={edge:+.1%} kelly=${kelly:.2f}")
    elif decision == "VETOED":
        agent = result.get("blocking_agent", "?")
        reason = result.get("reason", "?")[:80]
        print(f"  VETOED    {question}")
        print(f"            by {agent}: {reason}")
    elif decision == "BLOCKED":
        agent = result.get("blocking_agent", "?")
        reason = result.get("reason", "?")[:80]
        print(f"  BLOCKED   {question}")
        print(f"            by {agent}: {reason}")
