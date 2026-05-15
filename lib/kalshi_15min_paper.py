"""
Kalshi 15-min paper-trading — parallel to ``btc_5min_paper.py``.

Records hypothetical YES/NO entries when the composite signal fires on
a sample within the entry window, then settles via the public Kalshi
markets endpoint (no auth needed — the ``result`` field is public once
the market closes).

Settlement semantics for KXBTC15M:
  * Market resolves YES if BTC_close ≥ strike (Kalshi-derived spot)
  * Resolves NO otherwise
  * The market's ``result`` field flips to "yes" or "no" after close

**No real orders. Pure measurement.**
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

from lib.audit import log_event

PAPER_PATH = Path(__file__).parent.parent / "data" / "kalshi_15min_paper.jsonl"
KALSHI_HOST = "https://api.elections.kalshi.com/trade-api/v2"

# Defaults mirror btc_5min_paper but slightly more conservative — Kalshi
# charges 7% on profit (vs Polymarket's ~1.5% taker) so per-trade size
# is smaller to leave more headroom for fee drag.
DEFAULT_BANKROLL = 1000.0
DEFAULT_RISK_PER_TRADE = 0.005          # 0.5% — half the 5-min default
DEFAULT_MIN_CONFIDENCE = 0.35           # slightly higher bar; Kalshi WIN
                                        # pays only 0.93x post-fee
DEFAULT_MAX_SECONDS_TO_CLOSE = 300.0    # 5 min — last third of the 15-min window
EXTREME_PRICE_FLOOR = 0.05
EXTREME_PRICE_CEIL = 0.95


def _asset_from_ticker(ticker: str) -> str:
    """Fall back: extract asset shortname from a Kalshi ticker if the
    sample didn't carry an explicit asset field (legacy compatibility).

    KXBTC15M-26MAY150830-30 → "btc"
    KXETH15M-... → "eth"
    """
    if not ticker.startswith("KX"):
        return ""
    rest = ticker[2:]
    # Strip trailing digits/Day/Month suffix — keep the leading alpha run
    asset_chars: list[str] = []
    for c in rest:
        if c.isalpha():
            asset_chars.append(c)
        else:
            break
    # KXBTC15M → "BTC" (we want just the asset, drop the "15M" part)
    name = "".join(asset_chars).rstrip("M")
    # Some series end in a frequency hint like "BTCD" (daily); strip
    # trailing D if it makes the asset name 4+ chars (BTCD→BTC,
    # ETHD→ETH, but leave DOGE alone)
    if len(name) > 3 and name.endswith("D"):
        name = name[:-1]
    return name.lower()


@dataclass
class KalshiFifteenMinPaperTrade:
    """One paper-traded YES/NO position on a Kalshi 15-min crypto market.

    ``asset`` is the registry key (btc/eth/sol/...) so the report can
    demux by asset without parsing tickers.
    """
    trade_id: str
    asset: str
    market_ticker: str
    event_ticker: str
    title: str
    side: str                    # "YES" | "NO"
    fill_price: float
    our_size: float
    notional: float
    composite: float
    confidence: float
    strike: float
    spot_at_entry: float
    window_delta_pct: float | None
    seconds_to_close_at_entry: float
    close_time: str
    opened_at: str
    status: str = "open"         # "open" | "won" | "lost" | "void"
    resolved_at: str = ""
    paper_pnl: float = 0.0


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


# ── Recording ────────────────────────────────────────────────────────

def record_paper_trades_from_samples(
    samples: list[dict] | list,
    *,
    bankroll: float = DEFAULT_BANKROLL,
    risk_per_trade: float = DEFAULT_RISK_PER_TRADE,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    max_seconds_to_close: float = DEFAULT_MAX_SECONDS_TO_CLOSE,
) -> list[KalshiFifteenMinPaperTrade]:
    """Record paper trades for any qualifying Kalshi 15-min sample.

    Side selection from composite sign:
      composite > 0 → buy YES at yes_ask (or last_price fallback)
      composite < 0 → buy NO  at no_ask  (or 1 - yes_ask fallback)
    """
    if not samples:
        return []

    existing = _load_all()
    open_tickers = {r.get("market_ticker") for r in existing
                    if r.get("status") == "open"}

    notional_per_trade = bankroll * risk_per_trade
    now_iso = datetime.now(timezone.utc).isoformat()
    new_trades: list[KalshiFifteenMinPaperTrade] = []
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
        if not (0 < seconds_to_close <= max_seconds_to_close):
            continue

        ticker = s.get("market_ticker") or ""
        if not ticker or ticker in open_tickers:
            continue

        # Side selection
        if composite > 0:
            side = "YES"
            fill = s.get("yes_ask") or s.get("last_price")
            if fill is None:
                continue
        elif composite < 0:
            side = "NO"
            fill = s.get("no_ask")
            if fill is None and s.get("yes_ask") is not None:
                fill = round(1.0 - float(s["yes_ask"]), 4)
            if fill is None:
                continue
        else:
            continue

        fill = float(fill)
        if not (EXTREME_PRICE_FLOOR <= fill <= EXTREME_PRICE_CEIL):
            continue

        contracts = round(notional_per_trade / fill, 4)
        trade = KalshiFifteenMinPaperTrade(
            trade_id=f"{ticker[:24]}_{int(datetime.now(timezone.utc).timestamp())}",
            asset=str(s.get("asset", "")) or _asset_from_ticker(ticker),
            market_ticker=ticker,
            event_ticker=str(s.get("event_ticker", ""))[:60],
            title=str(s.get("title", ""))[:200],
            side=side,
            fill_price=fill,
            our_size=contracts,
            notional=round(fill * contracts, 4),
            composite=round(composite, 4),
            confidence=round(confidence, 4),
            strike=float(s.get("strike", 0) or 0),
            spot_at_entry=float(s.get("spot_usd", 0) or 0),
            window_delta_pct=indicators.get("window_delta_pct"),
            seconds_to_close_at_entry=round(seconds_to_close, 2),
            close_time=str(s.get("close_time", "")),
            opened_at=now_iso,
            status="open",
        )
        new_trades.append(trade)
        new_rows.append(asdict(trade))
        open_tickers.add(ticker)

    if new_rows:
        existing.extend(new_rows)
        _save_all(existing)
        log_event("kalshi_15min_paper", "recorded", {
            "n_new_trades": len(new_rows),
            "min_confidence": min_confidence,
        })
    return new_trades


# ── Settlement ───────────────────────────────────────────────────────

def settle_paper_trades() -> dict:
    """Poll the public Kalshi markets endpoint for resolutions.

    No auth needed — ``GET /markets/{ticker}`` returns ``result``
    ("yes" / "no" / "") for any market. Won when our paper side
    matches the result; lost otherwise; void if Kalshi voided the
    market.
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
        ticker = r.get("market_ticker")
        if not ticker:
            continue
        try:
            resp = requests.get(
                f"{KALSHI_HOST}/markets/{ticker}", timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            continue
        market = data.get("market") if isinstance(data, dict) else None
        if not isinstance(market, dict):
            continue
        result = str(market.get("result") or "").lower()
        # "" = still open, "yes" / "no" = resolved, "void"/"voided" = void
        if result == "":
            continue

        side = str(r.get("side", "")).upper()
        size = float(r.get("our_size", 0) or 0)
        fill = float(r.get("fill_price", 0) or 0)

        if result in ("void", "voided"):
            r["status"] = "void"
            r["paper_pnl"] = 0.0
            r["resolved_at"] = now_iso
            settled_now += 1
            continue

        won = (result == "yes" and side == "YES") or \
              (result == "no" and side == "NO")
        if won:
            r["status"] = "won"
            # Kalshi pays $1 per winning contract minus 7% fee on profit
            gross_profit = (1.0 - fill) * size
            r["paper_pnl"] = round(gross_profit * (1.0 - 0.07), 4)
        else:
            r["status"] = "lost"
            r["paper_pnl"] = round(-fill * size, 4)
        r["resolved_at"] = now_iso
        settled_now += 1
        pnl_now += r["paper_pnl"]

    _save_all(rows)
    open_count = sum(1 for r in rows if r.get("status") == "open")
    log_event("kalshi_15min_paper", "settle_cycle", {
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

def summary(asset_filter: str | None = None) -> dict:
    """Aggregate paper P&L stats. Optional ``asset_filter`` restricts to
    one asset (btc/eth/sol/...) — pass None for all.

    Adds ``by_asset`` and ``by_confidence_bucket`` breakdowns so the
    operator can see whether the composite signal calibrates
    differently per asset (it likely does — different liquidity).
    """
    rows = _load_all()
    if asset_filter:
        rows = [
            r for r in rows
            if (r.get("asset") or _asset_from_ticker(r.get("market_ticker", "")))
            == asset_filter
        ]
    s = {
        "total_trades": len(rows),
        "asset_filter": asset_filter,
        "open": 0, "won": 0, "lost": 0, "void": 0,
        "total_paper_pnl": 0.0, "capital_deployed": 0.0,
        "per_day_pnl": {},
        "by_confidence_bucket": {},
        "by_asset": {},
    }
    for r in rows:
        status = r.get("status", "open")
        notional = float(r.get("notional", 0) or 0)
        pnl = float(r.get("paper_pnl", 0) or 0)
        opened = (r.get("opened_at") or "")[:10]
        conf = float(r.get("confidence", 0) or 0)
        bucket = f"{int(conf * 10) * 10}-{int(conf * 10) * 10 + 10}%"
        asset = (
            r.get("asset")
            or _asset_from_ticker(r.get("market_ticker", ""))
            or "?"
        )

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

        # Per-asset breakdown
        a = s["by_asset"].setdefault(
            asset, {"total": 0, "open": 0, "won": 0, "lost": 0, "void": 0,
                    "pnl": 0.0, "capital": 0.0},
        )
        a["total"] += 1
        a[status] = a.get(status, 0) + 1
        a["pnl"] = round(a["pnl"] + pnl, 4)
        a["capital"] = round(a["capital"] + notional, 4)

    settled = s["won"] + s["lost"]
    s["win_rate"] = round(s["won"] / settled, 4) if settled > 0 else 0.0
    s["roi_pct"] = (
        round(s["total_paper_pnl"] / s["capital_deployed"], 4)
        if s["capital_deployed"] > 0 else 0.0
    )
    s["total_paper_pnl"] = round(s["total_paper_pnl"], 4)
    s["capital_deployed"] = round(s["capital_deployed"], 4)

    # Per-asset rollup: WR and ROI
    for asset, a in s["by_asset"].items():
        asettled = a.get("won", 0) + a.get("lost", 0)
        a["win_rate"] = (
            round(a.get("won", 0) / asettled, 4) if asettled > 0 else 0.0
        )
        a["roi_pct"] = (
            round(a["pnl"] / a["capital"], 4) if a["capital"] > 0 else 0.0
        )
    return s
