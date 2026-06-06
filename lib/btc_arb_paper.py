"""
BTC arb paper-trading — Phase 2 of the latency-arb stack.

Reads the BtcArbSignal output and records "would-have-bought" entries
whenever the gap between Polymarket's YES price and our implied
probability exceeds a threshold. Later, ``settle_btc_arb_paper`` polls
the underlying markets, marks resolved entries won/lost, computes paper
P&L. The aggregate report tells us if the signal is profitable in
practice before Phase 3 risks real USDC.

**No real orders. Pure measurement.**

Risk controls from Gravia (translated to our paper bankroll):
  * Risk per trade: 0.5% of paper bankroll
  * Daily limit:    2% of paper bankroll (recorded; doesn't halt)
  * Hard stop:      0.4% of paper bankroll
  * Min gap to fire: 3% (only trade when our model says
                          the quote is materially off)

Paper bankroll defaults to \$233 — mirrors the live Kalshi account.

Dedup: at most ONE open paper trade per ``market_id`` at a time. The
signal cycle can fire the same gap repeatedly; we only record once.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

from tradingcore.audit import log_event

PAPER_PATH = Path(__file__).parent.parent / "data" / "btc_arb_paper.jsonl"

# Defaults — tuneable by callers / future Hermes pass.
# Mirrors the live Kalshi account so paper sizing matches real-money sizing.
DEFAULT_BANKROLL = 233.0
DEFAULT_RISK_PER_TRADE = 0.005      # 0.5%
DEFAULT_DAILY_LIMIT = 0.02          # 2%
DEFAULT_HARD_STOP = 0.004           # 0.4%
DEFAULT_MIN_GAP = 0.02              # absolute gap ≥ 2% (drops to ~0%
                                    # net after Polymarket's ~2% taker
                                    # fee — Phase 2 measurement only;
                                    # Phase 3 will tighten to 3% or
                                    # whatever the live P&L data prefers)


@dataclass
class BtcArbPaperTrade:
    """One paper-traded BTC arb position."""
    trade_id: str               # market_id + opened_at
    market_id: str
    question: str
    strike_usd: float
    side: str                   # "YES" or "NO" — chosen to face the gap
    fill_price: float           # the side's quoted price at entry
    implied_prob: float         # what our model said it should be
    gap_at_entry: float         # signed
    our_size: float             # contracts (paper units)
    notional: float             # fill_price * our_size — capital at risk
    spot_at_entry: float
    hours_to_close_at_entry: float
    opened_at: str
    status: str = "open"        # "open" | "won" | "lost" | "void"
    resolved_at: str = ""
    paper_pnl: float = 0.0


# ── Recording ────────────────────────────────────────────────────────

def _load_all() -> list[dict]:
    if not PAPER_PATH.exists():
        return []
    rows: list[dict] = []
    try:
        with open(PAPER_PATH) as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except (json.JSONDecodeError, TypeError):
                    continue
    except OSError:
        return []
    return rows


def _save_all(rows: list[dict]) -> None:
    PAPER_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PAPER_PATH.with_suffix(".tmp")
    with open(tmp, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    tmp.replace(PAPER_PATH)


def reset() -> dict:
    """Zero the BTC arb paper ledger. Archives the current file to a timestamped
    .bak.jsonl first (reversible), then empties it so P&L starts fresh at zero."""
    rows = _load_all()
    cleared = len(rows)
    net = sum(float(r.get("paper_pnl", 0) or 0) for r in rows)
    archive = None
    if PAPER_PATH.exists() and PAPER_PATH.stat().st_size > 0:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archive = PAPER_PATH.with_name(f"{PAPER_PATH.stem}.{stamp}.bak.jsonl")
        PAPER_PATH.replace(archive)
    _save_all([])
    return {
        "cleared_trades": cleared,
        "cleared_net_pnl": round(net, 2),
        "archived_to": str(archive) if archive else None,
        "ledger": str(PAPER_PATH),
    }


def record_paper_trades_from_signals(
    signals: list[dict] | list,
    *,
    bankroll: float = DEFAULT_BANKROLL,
    risk_per_trade: float = DEFAULT_RISK_PER_TRADE,
    min_gap: float = DEFAULT_MIN_GAP,
) -> list[BtcArbPaperTrade]:
    """For each signal whose absolute gap ≥ ``min_gap``, open a paper
    trade if we don't already have one on that market.

    Side selection: if ``gap > 0`` (YES looks cheap), buy YES;
    if ``gap < 0`` (NO looks cheap), buy NO at price ``1 - yes_price``.

    Sizing: notional capital = ``bankroll * risk_per_trade``.
    Contract count = notional / fill_price.
    """
    if not signals:
        return []

    existing = _load_all()
    # Dedup keys: open market_ids (no need to re-enter on a still-open trade)
    open_ids = {r.get("market_id") for r in existing if r.get("status") == "open"}

    notional_per_trade = bankroll * risk_per_trade
    now_iso = datetime.now(timezone.utc).isoformat()
    new_trades: list[BtcArbPaperTrade] = []
    new_rows: list[dict] = []

    for sig in signals:
        # Accept both dict and dataclass-style
        s = sig if isinstance(sig, dict) else asdict(sig)
        gap = float(s.get("gap", 0) or 0)
        if abs(gap) < min_gap:
            continue
        market_id = s.get("market_id") or ""
        if not market_id or market_id in open_ids:
            continue

        yes_price = float(s.get("yes_price", 0) or 0)
        if not (0.0 < yes_price < 1.0):
            continue
        if gap > 0:
            side = "YES"
            fill = yes_price
        else:
            side = "NO"
            fill = 1.0 - yes_price
        if not (0.05 <= fill <= 0.95):
            # Same extreme-price gate the real order_gate enforces.
            continue

        contracts = round(notional_per_trade / fill, 4)
        trade = BtcArbPaperTrade(
            trade_id=f"{market_id[:12]}_{int(datetime.now(timezone.utc).timestamp())}",
            market_id=market_id,
            question=str(s.get("question", ""))[:200],
            strike_usd=float(s.get("strike_usd", 0) or 0),
            side=side,
            fill_price=fill,
            implied_prob=float(s.get("implied_yes_prob", 0) or 0),
            gap_at_entry=gap,
            our_size=contracts,
            notional=round(fill * contracts, 4),
            spot_at_entry=float(s.get("spot_usd", 0) or 0),
            hours_to_close_at_entry=float(s.get("hours_to_close", 0) or 0),
            opened_at=now_iso,
            status="open",
        )
        new_trades.append(trade)
        new_rows.append(asdict(trade))
        open_ids.add(market_id)  # in case the same signal repeats in this batch

    if new_rows:
        existing.extend(new_rows)
        _save_all(existing)
        log_event("btc_arb_paper", "recorded", {
            "n_new_trades": len(new_rows),
            "min_gap": min_gap,
        })
    return new_trades


# ── Settlement ───────────────────────────────────────────────────────

def settle_paper_trades() -> dict:
    """Poll open paper trades for resolution via the Gamma API.

    Each open trade's ``market_id`` is the Polymarket conditionId.
    Gamma's ``/markets`` endpoint returns ``closed=true`` + ``outcome``
    or ``umaResolutionStatus`` for resolved markets.

    Marks won/lost/void in place; rewrites the JSONL.
    """
    import requests

    rows = _load_all()
    if not rows:
        return {"settled_now": 0, "paper_pnl_this_cycle": 0.0,
                "total_open": 0, "total_settled": 0}

    open_rows = [r for r in rows if r.get("status") == "open"]
    if not open_rows:
        return {"settled_now": 0, "paper_pnl_this_cycle": 0.0,
                "total_open": 0, "total_settled": len(rows)}

    settled_now = 0
    pnl_now = 0.0
    now_iso = datetime.now(timezone.utc).isoformat()

    for r in open_rows:
        mid = r.get("market_id")
        if not mid:
            continue
        # Gamma's /markets defaults to closed=false, so resolved
        # markets are invisible without explicitly asking for them.
        # Without this filter, settlement never fires.
        try:
            resp = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={"condition_ids": mid, "closed": "true"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            continue
        if not isinstance(data, list) or not data:
            continue
        m = data[0]
        # Belt-and-suspenders: if Gamma ever stops respecting the filter,
        # the explicit closed check below still guards us.
        if not m.get("closed"):
            continue
        # Determine the winning outcome from final outcomePrices
        try:
            outcomes = m.get("outcomePrices") or "[]"
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            yes_final = float(outcomes[0]) if outcomes else None
        except (json.JSONDecodeError, ValueError, TypeError):
            yes_final = None
        if yes_final is None:
            continue
        side = str(r.get("side", "")).upper()
        size = float(r.get("our_size", 0) or 0)
        fill = float(r.get("fill_price", 0) or 0)

        # YES wins if yes_final ≈ 1.0, NO wins if ≈ 0.0.
        if yes_final >= 0.98:
            won = (side == "YES")
        elif yes_final <= 0.02:
            won = (side == "NO")
        else:
            # Ambiguous / partial resolution — treat as void
            r["status"] = "void"
            r["paper_pnl"] = 0.0
            r["resolved_at"] = now_iso
            settled_now += 1
            continue

        if won:
            r["status"] = "won"
            r["paper_pnl"] = round((1.0 - fill) * size, 4)
        else:
            r["status"] = "lost"
            r["paper_pnl"] = round(-fill * size, 4)
        r["resolved_at"] = now_iso
        settled_now += 1
        pnl_now += r["paper_pnl"]

    _save_all(rows)
    open_count = sum(1 for r in rows if r.get("status") == "open")
    log_event("btc_arb_paper", "settle_cycle", {
        "settled_now": settled_now,
        "paper_pnl_this_cycle": round(pnl_now, 2),
        "open": open_count,
    })
    return {
        "settled_now": settled_now,
        "paper_pnl_this_cycle": round(pnl_now, 2),
        "total_open": open_count,
        "total_settled": sum(1 for r in rows if r.get("status") != "open"),
    }


# ── Reporting ────────────────────────────────────────────────────────

def summary() -> dict:
    """Aggregate paper P&L stats across all recorded trades.

    Returns counts, total P&L, capital deployed, and per-day buckets
    so the operator can see whether a hypothetical daily-2%-stop
    would have triggered.
    """
    rows = _load_all()
    s = {
        "total_trades": len(rows),
        "open": 0, "won": 0, "lost": 0, "void": 0,
        "total_paper_pnl": 0.0, "capital_deployed": 0.0,
        "per_day_pnl": {},
    }
    for r in rows:
        status = r.get("status", "open")
        notional = float(r.get("notional", 0) or 0)
        pnl = float(r.get("paper_pnl", 0) or 0)
        opened = (r.get("opened_at") or "")[:10]
        s["capital_deployed"] += notional
        s["total_paper_pnl"] += pnl
        if status == "open":
            s["open"] += 1
        elif status == "won":
            s["won"] += 1
        elif status == "lost":
            s["lost"] += 1
        else:
            s["void"] += 1
        if opened:
            s["per_day_pnl"][opened] = round(
                s["per_day_pnl"].get(opened, 0.0) + pnl, 4
            )

    settled = s["won"] + s["lost"]
    s["win_rate"] = round(s["won"] / settled, 4) if settled > 0 else 0.0
    s["roi_pct"] = round(s["total_paper_pnl"] / s["capital_deployed"], 4) \
        if s["capital_deployed"] > 0 else 0.0
    return s
