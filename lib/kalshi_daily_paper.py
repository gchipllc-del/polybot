"""Paper-trade recorder for the Kalshi DAILY-crypto strategy.

Gates and sizing mirror the 15-min path (kalshi_15min_paper.py) — same
R:R discipline, same SL/TP scheme, same Kelly sizing. Differences:

  * Horizon: 1 day vs 15 minutes. The signal-to-noise advantage means we
    can lower the confidence floor without inviting noise (set in
    config/kalshi_daily_assets.yaml).
  * Settlement happens once a day; no intra-window TP/SL polling is
    needed — just compare close to strike at expiry.
  * Trade size: same $5 cap until calibration is proven.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from tradingcore import log_event

ROOT = Path(__file__).resolve().parent.parent
PAPER_LOG = ROOT / "data" / "kalshi_daily_paper.jsonl"
STRATEGY_PATH = ROOT / "config" / "kalshi_daily_strategy.yaml"

# Sizing & gates — start conservative, tighten or loosen after the first
# 30-50 paper trades inform calibration.
DEFAULT_BANKROLL = 1000.0
DEFAULT_MIN_TRADE_USD = 1.0
DEFAULT_MAX_TRADE_USD = 5.0
DEFAULT_KELLY_MULTIPLIER = 0.5   # half-Kelly
DEFAULT_MIN_CONFIDENCE = 0.20
MAX_FILL_FOR_BUY = 0.45          # R:R discipline — same as 15-min
EXTREME_PRICE_FLOOR = 0.05       # Kalshi quote floor
EXTREME_PRICE_CEIL = 0.95
KALSHI_PROFIT_FEE = 0.07         # ~7% Kalshi fee on profits

# Minimum distance from spot the strike must be (in vol units) — at the
# daily horizon, a strike at or near current spot is pure coin flip.
# 0.25× σ·√T means "the strike must be at least a quarter standard
# deviation from spot to admit directional edge".
MIN_STRIKE_DISTANCE_SIGMAS = 0.25


def _load_overrides() -> dict:
    """Read kalshi_daily_strategy.yaml — written by hermes_daily. Falls
    back to {} so module defaults stay authoritative when no file exists."""
    if not STRATEGY_PATH.exists():
        return {}
    try:
        import yaml
        with open(STRATEGY_PATH) as f:
            data = yaml.safe_load(f) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _effective_params() -> dict:
    """Module defaults overlaid with YAML overrides. Re-read each cycle
    so Hermes writes take effect on the very next signal pass."""
    o = _load_overrides()
    return {
        "min_confidence":             float(o.get("min_confidence",             DEFAULT_MIN_CONFIDENCE)),
        "max_fill_for_buy":           float(o.get("max_fill_for_buy",           MAX_FILL_FOR_BUY)),
        "max_trade_usd":              float(o.get("default_max_trade_usd",      DEFAULT_MAX_TRADE_USD)),
        "kelly_multiplier":           float(o.get("default_kelly_multiplier",   DEFAULT_KELLY_MULTIPLIER)),
        "min_strike_distance_sigmas": float(o.get("min_strike_distance_sigmas", MIN_STRIKE_DISTANCE_SIGMAS)),
        # 2026-05-23: NO-side toggle. Default OFF after 25 trades showed
        # NO at 16% WR / -$73 P&L while YES held 48% WR / +$227. Flip back
        # to True via kalshi_daily_strategy.yaml after signal recalibration.
        "no_side_enabled":            bool(o.get("kalshi_daily_no_side_enabled", False)),
    }


@dataclass
class KalshiDailyPaperTrade:
    trade_id: str
    asset: str
    market_ticker: str
    event_ticker: str
    title: str
    side: str
    fill_price: float
    our_size: float
    notional: float
    composite: float
    confidence: float
    strike: float
    spot_at_entry: float
    distance_to_spot_pct: float
    seconds_to_close_at_entry: float
    close_time: str
    opened_at: str
    status: str
    resolved_at: str = ""
    paper_pnl: float = 0.0
    exit_price: float = 0.0
    p_win_estimated: float = 0.0
    kelly_fraction: float = 0.0
    half_kelly_fraction: float = 0.0
    theo_yes: float = 0.0


def _load_all() -> list[dict]:
    if not PAPER_LOG.exists():
        return []
    out = []
    with open(PAPER_LOG) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def _save_all(records: list[dict]) -> None:
    PAPER_LOG.parent.mkdir(parents=True, exist_ok=True)
    tmp = PAPER_LOG.with_suffix(PAPER_LOG.suffix + ".tmp")
    with open(tmp, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    tmp.replace(PAPER_LOG)


def _kelly_size(*, p_win: float, fill: float, bankroll: float,
                multiplier: float, floor: float, cap: float) -> tuple[float, dict]:
    """Half-Kelly sizing in dollar notional. Returns (notional, meta)."""
    if fill <= 0 or fill >= 1:
        return 0.0, {"reason": "fill_out_of_range", "p_win": p_win, "kelly_fraction": 0.0,
                     "half_kelly_fraction": 0.0}
    b = (1 - fill) / fill
    q = 1 - p_win
    kelly = (b * p_win - q) / b
    half = kelly * multiplier
    if half <= 0:
        return 0.0, {"reason": "no_edge", "p_win": p_win,
                     "kelly_fraction": round(kelly, 4),
                     "half_kelly_fraction": round(half, 4)}
    notional = max(floor, min(cap, bankroll * half))
    return round(notional, 4), {"p_win": round(p_win, 4),
                                "kelly_fraction": round(kelly, 4),
                                "half_kelly_fraction": round(half, 4)}


def record_paper_trades_from_samples(
    samples: list[dict],
    *,
    bankroll: float = DEFAULT_BANKROLL,
    min_confidence: float | None = None,
) -> list[KalshiDailyPaperTrade]:
    """Open paper trades for any qualifying daily samples.

    Gates (short-circuit on first failure):
      1. confidence ≥ min_confidence  (YAML override wins over default)
      2. composite is non-zero (must have a direction)
      3. strike not within min_strike_distance_sigmas of spot (YAML override)
      4. ticker not already open
      5. fill ≤ max_fill_for_buy (YAML override)
      6. fill in [EXTREME_PRICE_FLOOR, EXTREME_PRICE_CEIL]
      7. Kelly > 0
    """
    if not samples:
        return []

    # Pull live params; allow caller's min_confidence to override the YAML
    # (callers can still pin a per-cycle floor for reproducibility tests).
    params = _effective_params()
    eff_min_conf = params["min_confidence"] if min_confidence is None else float(min_confidence)
    eff_max_fill = params["max_fill_for_buy"]
    eff_max_usd  = params["max_trade_usd"]
    eff_kelly    = params["kelly_multiplier"]
    eff_min_sigmas = params["min_strike_distance_sigmas"]

    # 2026-06-15 (ported from traderbot): evidence-gated bet sizing. The
    # Kelly multiplier is capped by what the Kalshi sleeve's OWN resolved
    # record statistically supports (lib/psr_gate) — monotone, can only
    # shrink. SHADOW by default: with kalshi_daily_psr_gate_enabled unset
    # we log what it WOULD cap to but don't apply it (live bot — operator
    # opts in). With 0 resolved Kalshi trades today it reads
    # 'no_measured_edge' → would cap the multiplier to 0.25.
    try:
        from lib.psr_gate import gated_kelly_multiplier
        _gated, _gmeta = gated_kelly_multiplier(eff_kelly, platform="kalshi")
        _gate_on = bool(_load_overrides().get("kalshi_daily_psr_gate_enabled", False))
        _gmeta["enforced"] = _gate_on
        log_event("kalshi_daily", "psr_sizing_gate", _gmeta)
        if _gate_on:
            eff_kelly = _gated
    except Exception as _e:
        log_event("kalshi_daily", "psr_gate_error", {"error": str(_e)[:200]})

    existing = _load_all()
    open_tickers = {r["market_ticker"] for r in existing if r.get("status") == "open"}

    now_iso = datetime.now(timezone.utc).isoformat()
    new_trades: list[KalshiDailyPaperTrade] = []
    skip_counts: dict[str, int] = {}

    for s in samples:
        ticker = s.get("market_ticker") or ""
        if not ticker or ticker in open_tickers:
            skip_counts["dup_open"] = skip_counts.get("dup_open", 0) + 1
            continue
        indicators = s.get("indicators") or {}
        confidence = float(indicators.get("confidence") or 0)
        composite = float(indicators.get("composite") or 0)
        theo_yes = float(indicators.get("theoretical_yes") or 0)
        sigma_sqrt_t = float(indicators.get("sigma_sqrt_T") or 0) if "sigma_sqrt_T" in indicators else 0

        if confidence < eff_min_conf:
            skip_counts["low_confidence"] = skip_counts.get("low_confidence", 0) + 1
            continue
        if composite == 0:
            skip_counts["zero_composite"] = skip_counts.get("zero_composite", 0) + 1
            continue

        spot = float(s.get("spot_usd") or 0)
        strike = float(s.get("strike") or 0)
        if spot <= 0 or strike <= 0:
            skip_counts["invalid_strike_spot"] = skip_counts.get("invalid_strike_spot", 0) + 1
            continue

        # Strike-distance gate: need at least N σ·√T between spot and strike
        # so the directional bet has room to play.
        if sigma_sqrt_t > 0:
            import math
            sd_distance = abs(math.log(spot / strike)) / sigma_sqrt_t
            if sd_distance < eff_min_sigmas:
                skip_counts["too_close_to_strike"] = skip_counts.get("too_close_to_strike", 0) + 1
                continue

        yes_ask = s.get("yes_ask")
        no_ask = s.get("no_ask")
        if composite > 0:
            side = "YES"
            fill = yes_ask if yes_ask is not None else s.get("last_price")
        else:
            side = "NO"
            fill = no_ask if no_ask is not None else (1 - yes_ask) if yes_ask is not None else None
        if fill is None:
            skip_counts["no_fill"] = skip_counts.get("no_fill", 0) + 1
            continue

        # 2026-05-23: NO-side disable gate. First 25 daily NO trades:
        # 4W/21L = 16% WR, -$73.70 P&L. Strong-negative composite calls
        # do NOT predict BTC-down on daily horizon. YES side over the
        # same window: 12W/13L = 48% WR, +$227.04. Toggle via
        # kalshi_daily_strategy.yaml :: kalshi_daily_no_side_enabled.
        if side == "NO" and not params.get("no_side_enabled", False):
            skip_counts["no_side_disabled"] = skip_counts.get("no_side_disabled", 0) + 1
            continue
        fill = float(fill)
        if not (EXTREME_PRICE_FLOOR <= fill <= EXTREME_PRICE_CEIL):
            skip_counts["extreme_price"] = skip_counts.get("extreme_price", 0) + 1
            continue
        if fill > eff_max_fill:
            skip_counts["fill_too_high"] = skip_counts.get("fill_too_high", 0) + 1
            continue

        # Use model's theoretical YES probability for sizing (more
        # principled than confidence-as-probability heuristic).
        p_win = theo_yes if side == "YES" else (1 - theo_yes)
        p_win = max(0.05, min(0.95, p_win))
        notional, meta = _kelly_size(
            p_win=p_win, fill=fill, bankroll=bankroll,
            multiplier=eff_kelly,
            floor=DEFAULT_MIN_TRADE_USD, cap=eff_max_usd,
        )
        if notional <= 0:
            skip_counts["kelly_no_edge"] = skip_counts.get("kelly_no_edge", 0) + 1
            continue

        contracts = round(notional / fill, 4)
        trade = KalshiDailyPaperTrade(
            trade_id=f"{ticker[:24]}_{int(datetime.now(timezone.utc).timestamp())}",
            asset=str(s.get("asset", "")),
            market_ticker=ticker,
            event_ticker=str(s.get("event_ticker", ""))[:60],
            title=str(s.get("title", ""))[:200],
            side=side, fill_price=fill, our_size=contracts,
            notional=round(fill * contracts, 4),
            composite=round(composite, 4),
            confidence=round(confidence, 4),
            strike=strike, spot_at_entry=spot,
            distance_to_spot_pct=float(s.get("distance_to_spot_pct") or 0),
            seconds_to_close_at_entry=round(float(s.get("seconds_to_close") or 0), 2),
            close_time=str(s.get("close_time") or ""),
            opened_at=now_iso, status="open",
            p_win_estimated=meta["p_win"],
            kelly_fraction=meta["kelly_fraction"],
            half_kelly_fraction=meta["half_kelly_fraction"],
            theo_yes=theo_yes,
        )
        new_trades.append(trade)
        log_event("kalshi_daily", "paper_trade_opened", {
            "ticker": ticker, "asset": s.get("asset"), "side": side,
            "fill": fill, "strike": strike, "spot": spot,
            "notional": notional, "p_win": meta["p_win"],
        }, result="success")

    if new_trades:
        all_records = existing + [asdict(t) for t in new_trades]
        _save_all(all_records)
    return new_trades


def settle_paper_trades() -> dict:
    """Settle any paper trades whose markets have closed.

    For daily markets we settle by comparing current spot to strike at
    or after close_time. (For a perfect backtest we'd want Kalshi's
    actual resolution; this is a good approximation for paper P&L.)
    """
    records = _load_all()
    open_ones = [r for r in records if r.get("status") == "open"]
    if not open_ones:
        return {"settled_now": 0, "total_open": 0}

    import requests
    # One-shot spot fetch per symbol to avoid hammering Binance.
    symbol_map = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT",
                  "xrp": "XRPUSDT", "doge": "DOGEUSDT", "ada": "ADAUSDT"}
    spot_cache: dict[str, float] = {}
    now = datetime.now(timezone.utc)
    settled = 0
    for r in open_ones:
        close_iso = r.get("close_time", "")
        if not close_iso:
            continue
        try:
            ct = datetime.fromisoformat(close_iso.replace("Z", "+00:00"))
        except ValueError:
            continue
        if now < ct:
            continue   # market still open

        asset = r.get("asset", "btc").lower()
        sym = symbol_map.get(asset)
        if not sym:
            continue
        if sym not in spot_cache:
            try:
                resp = requests.get(
                    "https://api.binance.us/api/v3/ticker/price",
                    params={"symbol": sym}, timeout=8,
                )
                resp.raise_for_status()
                spot_cache[sym] = float(resp.json()["price"])
            except Exception:
                continue
        spot_now = spot_cache[sym]

        strike = float(r.get("strike") or 0)
        if strike <= 0:
            continue
        market_yes_won = spot_now > strike
        side = r.get("side", "YES")
        we_won = (side == "YES" and market_yes_won) or (side == "NO" and not market_yes_won)
        fill = float(r.get("fill_price") or 0)
        size = float(r.get("our_size") or 0)
        if we_won:
            gross_payout = size * (1 - fill)
            net = gross_payout * (1 - KALSHI_PROFIT_FEE)
            r["status"] = "won"
            r["exit_price"] = 1.0
            r["paper_pnl"] = round(net, 4)
        else:
            r["status"] = "lost"
            r["exit_price"] = 0.0
            r["paper_pnl"] = round(-float(r.get("notional") or 0), 4)
        r["resolved_at"] = now.isoformat()
        settled += 1

    if settled:
        _save_all(records)
        log_event("kalshi_daily", "paper_settled_batch",
                  {"settled_now": settled, "total_open_before": len(open_ones)})
    return {"settled_now": settled, "total_open": sum(1 for r in records if r.get("status") == "open")}
