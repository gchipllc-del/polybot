"""
Paper copy-trading — Stage 3 of the copy-trading roadmap.

When a watched wallet opens a position (alert fired by wallet_watch),
we record a "would-have-copied" entry in ``data/paper_copy_trades.jsonl``.
The entry captures market, side, the wallet's actual fill price, and
a synthetic position size scaled to our (configurable) paper bankroll.

Later, ``settle_paper_copies(client)`` polls the underlying markets,
detects resolution, computes paper P&L per copy, and updates the
record. Output reports aggregate P&L per source wallet so we can see
which wallets are actually worth copying with real money before
Stage 4 risks any capital.

Sizing for paper mode is deliberately straightforward: we don't need
to match the original wallet's percentage-of-bankroll (we don't know
their bankroll). We just need a denominator for P&L math. Default:
``min(their_amount, max_per_trade=10)`` mana/USDC.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

from tradingcore.audit import log_event

PAPER_COPY_PATH = Path(__file__).parent.parent / "data" / "paper_copy_trades.jsonl"


@dataclass
class PaperCopyTrade:
    """One paper-copied position recorded from a wallet alert."""
    copy_id: str                # unique ID derived from source bet
    source_handle: str          # watched wallet handle
    platform: str               # "manifold" | "polymarket"
    market_id: str
    market_question: str
    side: str                   # "YES" | "NO"
    fill_price: float           # at the moment our copy would have entered
    our_size: float             # synthetic position size in platform units
    opened_at: str              # ISO timestamp
    source_bet_id: str
    status: str = "open"        # "open" | "won" | "lost" | "void"
    resolved_at: str = ""
    paper_pnl: float = 0.0      # signed; only set after settlement


# ── Recording ────────────────────────────────────────────────────────

def record_paper_copy_from_alert(
    alert: dict,
    *,
    max_per_trade: float = 10.0,
) -> PaperCopyTrade | None:
    """Translate a ``WalletAlert``-shaped dict into a paper-copy record
    and append to disk. Idempotent on ``source_bet_id`` — if we already
    recorded this bet, return the existing entry without duplicating.

    Returns the trade dict, or None if the alert can't be paper-copied
    (e.g. missing market_id, zero fill price, NO-side bet with prob 1.0,
    etc. — same exclusions the real order_gate would apply).
    """
    bet_id = str(alert.get("bet_id") or "").strip()
    market_id = str(alert.get("market_id") or "").strip()
    side = str(alert.get("side") or "").upper()
    fill_price = float(alert.get("prob_after") or 0)

    if not bet_id or not market_id or side not in ("YES", "NO"):
        return None
    # Skip extreme-confidence trades — same gate the real order_gate
    # applies. Manifold/Polymarket reject these; paper-copying them
    # would inflate fake P&L without reflecting reality.
    if fill_price <= 0.05 or fill_price >= 0.95:
        return None

    # Dedup: scan existing JSONL for this source_bet_id
    if PAPER_COPY_PATH.exists():
        try:
            with open(PAPER_COPY_PATH) as f:
                for line in f:
                    try:
                        existing = json.loads(line)
                        if existing.get("source_bet_id") == bet_id:
                            return PaperCopyTrade(**existing)
                    except (json.JSONDecodeError, TypeError):
                        continue
        except OSError:
            pass

    their_amount = float(alert.get("amount") or 0)
    our_size = min(abs(their_amount), max_per_trade)
    if our_size <= 0:
        return None

    trade = PaperCopyTrade(
        copy_id=f"{alert.get('platform','?')}_{bet_id[:12]}",
        source_handle=str(alert.get("handle") or ""),
        platform=str(alert.get("platform") or ""),
        market_id=market_id,
        market_question=str(alert.get("market_question") or "")[:200],
        side=side,
        fill_price=fill_price,
        our_size=our_size,
        opened_at=str(alert.get("created_at") or datetime.now(timezone.utc).isoformat()),
        source_bet_id=bet_id,
        status="open",
    )

    PAPER_COPY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(PAPER_COPY_PATH, "a") as f:
        f.write(json.dumps(asdict(trade)) + "\n")

    log_event("paper_copy", "recorded", {
        "copy_id": trade.copy_id, "source": trade.source_handle,
        "platform": trade.platform, "side": side,
        "fill_price": fill_price, "our_size": our_size,
    })
    return trade


# ── Settlement ───────────────────────────────────────────────────────

def _load_all() -> list[dict]:
    if not PAPER_COPY_PATH.exists():
        return []
    out: list[dict] = []
    try:
        with open(PAPER_COPY_PATH) as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except (json.JSONDecodeError, TypeError):
                    continue
    except OSError:
        return []
    return out


def _save_all(rows: list[dict]) -> None:
    """Rewrite the JSONL atomically. Used after settlement updates."""
    PAPER_COPY_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PAPER_COPY_PATH.with_suffix(".tmp")
    with open(tmp, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    tmp.replace(PAPER_COPY_PATH)


def settle_paper_copies(*, manifold_client=None) -> dict:
    """Poll open paper-copy trades for resolution. Updates their
    ``status`` + ``paper_pnl`` fields in place and rewrites the JSONL.

    Logic per trade:
      * Fetch market via platform client
      * If market.status == "resolved" and market.outcome matches our
        side → status="won", pnl = (1 - fill_price) * our_size
      * If resolved and outcome doesn't match → status="lost",
        pnl = -fill_price * our_size
      * If market resolved as a refund/cancel → status="void", pnl=0
      * Otherwise still "open"

    Returns a summary dict with newly-resolved count + total paper P&L
    realized this cycle.
    """
    rows = _load_all()
    if not rows:
        return {"settled_now": 0, "paper_pnl_this_cycle": 0.0,
                "total_open": 0, "total_settled": 0}

    if manifold_client is None:
        from lib.manifold_client import ManifoldClient
        manifold_client = ManifoldClient()

    settled_now = 0
    pnl_now = 0.0
    market_cache: dict[tuple[str, str], object] = {}

    for r in rows:
        if r.get("status") != "open":
            continue
        platform = r.get("platform")
        mid = r.get("market_id")
        if not mid or not platform:
            continue
        key = (platform, mid)
        if key not in market_cache:
            try:
                if platform == "manifold":
                    market_cache[key] = manifold_client.get_market(mid)
                elif platform == "polymarket":
                    # Polymarket resolution requires a different lookup
                    # (Gamma API by conditionId). Stage 3 MVP supports
                    # Manifold settle only; Polymarket comes next.
                    market_cache[key] = None
                else:
                    market_cache[key] = None
            except Exception:
                market_cache[key] = None
        market = market_cache[key]
        if market is None:
            continue
        status = getattr(market, "status", "")
        outcome = getattr(market, "outcome", "") or ""
        if status != "resolved":
            continue
        outcome_upper = str(outcome).upper()
        side = str(r.get("side", "")).upper()
        fill = float(r.get("fill_price", 0) or 0)
        size = float(r.get("our_size", 0) or 0)

        if outcome_upper in ("CANCEL", "N/A", "VOID", ""):
            r["status"] = "void"
            r["paper_pnl"] = 0.0
        elif outcome_upper == side:
            # Won — receive (1 - fill_price) per share of profit
            r["status"] = "won"
            r["paper_pnl"] = round((1.0 - fill) * size, 4)
        elif outcome_upper in ("YES", "NO"):
            # Lost — lost our entire stake (fill_price × size)
            r["status"] = "lost"
            r["paper_pnl"] = round(-fill * size, 4)
        else:
            # Numeric / MKT resolution — too platform-specific for MVP
            r["status"] = "void"
            r["paper_pnl"] = 0.0

        r["resolved_at"] = datetime.now(timezone.utc).isoformat()
        settled_now += 1
        pnl_now += r["paper_pnl"]

    _save_all(rows)

    open_count = sum(1 for r in rows if r.get("status") == "open")
    settled_total = sum(1 for r in rows if r.get("status") != "open")
    log_event("paper_copy", "settle_cycle", {
        "settled_now": settled_now,
        "paper_pnl_this_cycle": round(pnl_now, 2),
        "open": open_count, "settled_total": settled_total,
    })
    return {
        "settled_now": settled_now,
        "paper_pnl_this_cycle": round(pnl_now, 2),
        "total_open": open_count,
        "total_settled": settled_total,
    }


# ── Reporting ────────────────────────────────────────────────────────

def summary_by_wallet() -> dict:
    """Aggregate paper P&L per source wallet. Used by the CLI report.

    Returns a dict ``{handle: {wins, losses, voids, open, total_pnl,
    capital_at_risk, win_rate, roi_pct}}``. Sorted by ROI desc when
    rendered by the caller.
    """
    rows = _load_all()
    out: dict[str, dict] = {}
    for r in rows:
        h = r.get("source_handle", "?")
        stats = out.setdefault(h, {
            "wins": 0, "losses": 0, "voids": 0, "open": 0,
            "total_pnl": 0.0, "capital_at_risk": 0.0,
        })
        status = r.get("status", "open")
        size = float(r.get("our_size", 0) or 0)
        fill = float(r.get("fill_price", 0) or 0)
        pnl = float(r.get("paper_pnl", 0) or 0)
        # Capital at risk = our stake (fill_price * size for YES; mirror for NO)
        stats["capital_at_risk"] += fill * size
        stats["total_pnl"] += pnl
        if status == "won":
            stats["wins"] += 1
        elif status == "lost":
            stats["losses"] += 1
        elif status == "void":
            stats["voids"] += 1
        else:
            stats["open"] += 1

    for h, s in out.items():
        settled = s["wins"] + s["losses"]
        s["win_rate"] = round(s["wins"] / settled, 4) if settled > 0 else 0.0
        s["roi_pct"] = round(s["total_pnl"] / s["capital_at_risk"], 4) if s["capital_at_risk"] > 0 else 0.0
    return out
