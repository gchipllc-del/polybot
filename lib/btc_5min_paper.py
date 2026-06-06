"""
BTC 5-min paper-trading — Phase 2 of the Gravia-style latency stack.

Reads BtcFiveMin signal samples (from ``btc_5min_signal.run_signal_cycle``)
and records "would-have-bought" entries whenever:

  1. Confidence (|composite| / max_possible) ≥ ``min_confidence``
  2. ``seconds_to_close`` is within ``max_seconds_to_close`` (concentrate
     entries near the close, where direction is most-locked)
  3. Fill price is inside the 0.05-0.95 sanity band
  4. No open paper trade already exists on this market_id

When the 5-minute window resolves (Gamma flags ``closed=True``), the
settle path marks each trade ``won``/``lost``/``void`` and computes
paper P&L. The aggregate report tells us whether the 6-indicator
composite has edge BEFORE Phase 3 risks real USDC.

**No real orders. Pure measurement.**

Risk controls (paper bankroll defaults to $1000):
  * Risk per trade: 1.0% of bankroll
  * Daily limit:    2.0% (recorded; doesn't halt — Phase 3 will halt)
  * Hard stop:      0.4%
  * Min gap: replaced by ``min_confidence`` — the 6-indicator composite
    is the gating criterion here, not a single gap number

Honest caveat: 60s cron cadence means we miss the T-10s entry window
the reference bots use. Phase 2 measures whether the signal HAS edge
at coarse polling; Phase 3 (a long-running daemon) will hit the
T-10s pocket. If Phase 2 paper P&L is positive, the daemon's
fine-grained version will only be more profitable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

from tradingcore.audit import log_event

PAPER_PATH = Path(__file__).parent.parent / "data" / "btc_5min_paper.jsonl"

DEFAULT_BANKROLL = 1000.0
DEFAULT_RISK_PER_TRADE = 0.01           # 1% — now used as soft-cap
                                        # input to Kelly, not flat
DEFAULT_MIN_CONFIDENCE = 0.30           # |composite|/max ≥ 0.30
DEFAULT_MAX_SECONDS_TO_CLOSE = 120.0    # only enter within last 2 min
DEFAULT_MIN_SECONDS_TO_CLOSE = 20.0     # don't enter under 20s — slippage zone
# Tightened from 0.05-0.95 to 0.15-0.85 — at the extremes the
# risk/reward is brutal and edge gets eaten by spread + fees.
EXTREME_PRICE_FLOOR = 0.15
EXTREME_PRICE_CEIL = 0.85
NEUTRAL_MARKET_FLOOR = 0.45
NEUTRAL_MARKET_CEIL = 0.55

# ── Kelly sizing ──────────────────────────────────────────────────
# Mirrors kalshi_15min_paper's setup. Shared math lives in
# tradingcore.kelly so both paper modules stay consistent.
DEFAULT_KELLY_MULTIPLIER = 0.5
DEFAULT_MIN_TRADE_USD = 1.0
DEFAULT_MAX_TRADE_USD = 25.0


def confidence_to_winprob(confidence: float) -> float:
    """Same conservative linear mapping as kalshi_15min_paper.
    Recalibrate from empirical data after 100+ settled trades.
    """
    return max(0.50, min(0.85, 0.50 + 0.22 * float(confidence)))


def kelly_sized_notional(
    *,
    confidence: float,
    fill_price: float,
    bankroll: float,
    multiplier: float = DEFAULT_KELLY_MULTIPLIER,
    floor: float = DEFAULT_MIN_TRADE_USD,
    cap: float = DEFAULT_MAX_TRADE_USD,
) -> tuple[float, dict]:
    """Half-Kelly notional with floor/cap. Returns (0, meta) on no edge."""
    from tradingcore.kelly import kelly_fraction as _shared_kelly_fraction
    p_win = confidence_to_winprob(confidence)
    kelly_f = _shared_kelly_fraction(p_win, fill_price)
    half_f = kelly_f * multiplier
    sized = bankroll * half_f
    meta = {
        "p_win": round(p_win, 4),
        "kelly_fraction": round(kelly_f, 4),
        "half_kelly_fraction": round(half_f, 4),
        "sized_before_caps": round(sized, 4),
    }
    if kelly_f <= 0.0:
        return 0.0, meta
    return round(max(floor, min(cap, sized)), 4), meta


@dataclass
class BtcFiveMinPaperTrade:
    """One paper-traded 5-min UP/DOWN position."""
    trade_id: str
    market_id: str
    slug: str
    question: str
    side: str                    # "UP" | "DOWN"
    fill_price: float            # what we paid (up_price or 1-up_price)
    our_size: float              # paper contracts
    notional: float              # fill_price * our_size
    composite: float
    confidence: float
    window_delta_pct: float | None
    spot_at_entry: float
    seconds_to_close_at_entry: float
    window_end_ts: int
    opened_at: str
    status: str = "open"         # "open" | "won" | "lost" | "void"
    resolved_at: str = ""
    paper_pnl: float = 0.0
    # Kelly sizing diagnostics
    p_win_estimated: float = 0.0
    kelly_fraction: float = 0.0
    half_kelly_fraction: float = 0.0


# ── State ────────────────────────────────────────────────────────────

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
    """Zero the BTC 5-min paper ledger. The existing file is ARCHIVED to a
    timestamped sibling first (reversible — nothing is destroyed), then the
    live ledger is emptied so P&L / trade counts start fresh at zero.

    Returns a summary of what was cleared. Does NOT touch any live position or
    the btc_arb sleeve — only this paper ledger.
    """
    from datetime import datetime, timezone
    rows = _load_all()
    cleared = len(rows)
    net = 0.0
    for r in rows:
        v = r.get("paper_pnl", r.get("net_profit"))
        if isinstance(v, (int, float)):
            net += float(v)
    archive = None
    if PAPER_PATH.exists() and PAPER_PATH.stat().st_size > 0:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archive = PAPER_PATH.with_name(f"{PAPER_PATH.stem}.{stamp}.bak.jsonl")
        PAPER_PATH.replace(archive)
    # Recreate an empty ledger so downstream readers find a clean file.
    _save_all([])
    return {
        "cleared_trades": cleared,
        "cleared_net_pnl": round(net, 2),
        "archived_to": str(archive) if archive else None,
        "ledger": str(PAPER_PATH),
    }


# ── Recording ────────────────────────────────────────────────────────

def record_paper_trades_from_samples(
    samples: list[dict] | list,
    *,
    bankroll: float = DEFAULT_BANKROLL,
    risk_per_trade: float = DEFAULT_RISK_PER_TRADE,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    max_seconds_to_close: float = DEFAULT_MAX_SECONDS_TO_CLOSE,
) -> list[BtcFiveMinPaperTrade]:
    """Open a paper trade for any qualifying sample.

    A sample qualifies when:
      * indicators dict is present
      * confidence ≥ min_confidence
      * 0 < seconds_to_close ≤ max_seconds_to_close (we don't enter
        already-resolved markets, and we wait until near close)
      * fill price inside extreme-price band
      * no existing open paper trade on the same market_id
    """
    if not samples:
        return []

    existing = _load_all()
    open_ids = {r.get("market_id") for r in existing
                if r.get("status") == "open"}

    # Kelly sizing replaces flat $5. Soft cap = 5x the legacy
    # risk_per_trade fraction; the hard cap (DEFAULT_MAX_TRADE_USD)
    # still bounds it.
    soft_cap = bankroll * risk_per_trade * 5.0
    now_iso = datetime.now(timezone.utc).isoformat()
    new_trades: list[BtcFiveMinPaperTrade] = []
    new_rows: list[dict] = []

    for sig in samples:
        s = sig if isinstance(sig, dict) else asdict(sig)
        indicators = s.get("indicators")
        if not isinstance(indicators, dict):
            continue
        confidence = float(indicators.get("confidence", 0) or 0)
        composite = float(indicators.get("composite", 0) or 0)
        if confidence < min_confidence:
            continue

        seconds_to_close = float(s.get("seconds_to_close", 0) or 0)
        if not (DEFAULT_MIN_SECONDS_TO_CLOSE <= seconds_to_close <= max_seconds_to_close):
            continue

        market_id = s.get("market_id") or ""
        if not market_id or market_id in open_ids:
            continue

        up_price = float(s.get("up_price", 0) or 0)
        if not (0.0 < up_price < 1.0):
            continue

        # Neutral-market skip — market itself has no opinion, no edge.
        if NEUTRAL_MARKET_FLOOR <= up_price <= NEUTRAL_MARKET_CEIL:
            continue

        # Side selection from composite sign
        if composite > 0:
            side = "UP"
            fill = up_price
        elif composite < 0:
            side = "DOWN"
            fill = round(1.0 - up_price, 4)
        else:
            continue

        if not (EXTREME_PRICE_FLOOR <= fill <= EXTREME_PRICE_CEIL):
            continue

        # Kelly sizing — replaces flat $5
        kelly_cap = min(DEFAULT_MAX_TRADE_USD, soft_cap)
        notional, kelly_meta = kelly_sized_notional(
            confidence=confidence,
            fill_price=fill,
            bankroll=bankroll,
            multiplier=DEFAULT_KELLY_MULTIPLIER,
            floor=DEFAULT_MIN_TRADE_USD,
            cap=kelly_cap,
        )
        if notional <= 0:
            continue

        contracts = round(notional / fill, 4)
        trade = BtcFiveMinPaperTrade(
            trade_id=f"{market_id[:12]}_{int(datetime.now(timezone.utc).timestamp())}",
            market_id=market_id,
            slug=str(s.get("slug", ""))[:60],
            question=str(s.get("question", ""))[:200],
            side=side,
            fill_price=fill,
            our_size=contracts,
            notional=round(fill * contracts, 4),
            composite=round(composite, 4),
            confidence=round(confidence, 4),
            window_delta_pct=indicators.get("window_delta_pct"),
            spot_at_entry=float(s.get("spot_usd", 0) or 0),
            seconds_to_close_at_entry=round(seconds_to_close, 2),
            window_end_ts=int(s.get("window_end_ts", 0) or 0),
            opened_at=now_iso,
            status="open",
            p_win_estimated=kelly_meta["p_win"],
            kelly_fraction=kelly_meta["kelly_fraction"],
            half_kelly_fraction=kelly_meta["half_kelly_fraction"],
        )
        new_trades.append(trade)
        new_rows.append(asdict(trade))
        open_ids.add(market_id)

    if new_rows:
        existing.extend(new_rows)
        _save_all(existing)
        log_event("btc_5min_paper", "recorded", {
            "n_new_trades": len(new_rows),
            "min_confidence": min_confidence,
            "max_seconds_to_close": max_seconds_to_close,
        })
    return new_trades


# ── Settlement ───────────────────────────────────────────────────────

def settle_paper_trades() -> dict:
    """Poll open paper trades and mark them won/lost/void.

    Uses Gamma's /markets with ``closed=true`` (defaults to closed=false
    so resolved markets are otherwise invisible — same fix as
    btc_arb_paper.settle_paper_trades).

    Winning outcome inferred from outcomePrices: YES (UP) wins if
    outcomePrices[0] ≈ 1.0, NO (DOWN) wins if ≈ 0.0. Ambiguous
    resolutions → void.
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
        if not m.get("closed"):
            continue
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

        if yes_final >= 0.98:
            won = (side == "UP")
        elif yes_final <= 0.02:
            won = (side == "DOWN")
        else:
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
    log_event("btc_5min_paper", "settle_cycle", {
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
    """Aggregate P&L stats — counts, ROI, per-day, and confidence-bucket
    win rates so we can see whether the composite signal calibrates.
    """
    rows = _load_all()
    s = {
        "total_trades": len(rows),
        "open": 0, "won": 0, "lost": 0, "void": 0,
        "total_paper_pnl": 0.0, "capital_deployed": 0.0,
        "per_day_pnl": {},
        "by_confidence_bucket": {},
    }
    for r in rows:
        status = r.get("status", "open")
        notional = float(r.get("notional", 0) or 0)
        pnl = float(r.get("paper_pnl", 0) or 0)
        opened = (r.get("opened_at") or "")[:10]
        conf = float(r.get("confidence", 0) or 0)
        # Bucket confidence into 10pp tranches: "30-40%", "40-50%", etc.
        bucket = f"{int(conf * 10) * 10}-{int(conf * 10) * 10 + 10}%"

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
        if status in ("won", "lost"):
            b = s["by_confidence_bucket"].setdefault(
                bucket, {"settled": 0, "wins": 0, "pnl": 0.0},
            )
            b["settled"] += 1
            if status == "won":
                b["wins"] += 1
            b["pnl"] = round(b["pnl"] + pnl, 4)

    settled = s["won"] + s["lost"]
    s["win_rate"] = round(s["won"] / settled, 4) if settled > 0 else 0.0
    s["roi_pct"] = (
        round(s["total_paper_pnl"] / s["capital_deployed"], 4)
        if s["capital_deployed"] > 0 else 0.0
    )
    s["total_paper_pnl"] = round(s["total_paper_pnl"], 4)
    s["capital_deployed"] = round(s["capital_deployed"], 4)
    return s
