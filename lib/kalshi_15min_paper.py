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

from tradingcore.audit import log_event

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
DEFAULT_MIN_SECONDS_TO_CLOSE = 30.0     # don't enter under 30s — slippage zone

# Intra-window exit thresholds. When our side's price spikes above
# this, sell to lock the gain rather than ride out reversion. When it
# craters below the stop, cut the loss before the inevitable zero.
#
# Tuned 2026-05-18 from 0.85 → 0.75. Rationale: in 39 BTC trades the
# 0.85 threshold triggered 24 times — clearly hittable. Lowering to
# 0.75 should trigger more often (price reaches 0.75 before 0.85 in
# most runners), capturing wins that otherwise would have reverted
# back below 0.85 before resolution. Per-trigger profit shrinks
# (e.g. fill 0.55 → was locking $0.30/share at 0.85, now $0.20/share
# at 0.75) but the higher trigger rate should more than compensate
# in aggregate. Will reassess after ~50 more trades.
INTRA_WINDOW_TAKE_PROFIT = 0.75
INTRA_WINDOW_STOP_LOSS = 0.15     # our side falls this low → cut
                                  # (locking ~0.15-0.20 loss vs total)

# ── Kelly Criterion sizing ──────────────────────────────────────────
#
# Replaces flat-$5 sizing with edge-proportional sizing. The math:
#   f* = (b·p - q) / b
# where b = (1-fill)/fill is net odds, p is our estimated win prob,
# q = 1-p. We use HALF-Kelly (multiplier 0.5) for prudence — full
# Kelly is mathematically optimal for log-wealth growth but ruinous
# when p is misestimated. Half-Kelly cuts variance in half while
# capturing ~75% of the long-run growth.
#
# When kelly_f ≤ 0 (no edge), we skip the trade entirely — Kelly's
# "don't bet" signal is itself information.
#
# Bankroll = paper bankroll ($1000 default). When we go live, this
# should be set to the actual Kalshi balance so cap/floor scale.
DEFAULT_KELLY_MULTIPLIER = 0.5       # half-Kelly
DEFAULT_MIN_TRADE_USD = 1.0          # floor — don't fire if Kelly
                                     # rounds below this
DEFAULT_MAX_TRADE_USD = 25.0         # cap — 2.5% of $1000 paper, or
                                     # 50% of real $50 Kalshi balance.
                                     # Scale this down when going live.


def confidence_to_winprob(confidence: float) -> float:
    """Map composite-signal confidence (0..1) to estimated win prob.

    Deliberately CONSERVATIVE — anchored well below the empirical 82%
    BTC win rate we've seen, because the sample size (39 trades) isn't
    enough to commit Kelly to an aggressive prior. Half-Kelly + this
    underestimate is our two-belt safety system.

    Linear mapping:
      confidence 0.00 → p_win 0.50  (no edge)
      confidence 0.65 → p_win 0.64  (just above threshold)
      confidence 1.00 → p_win 0.72  (max — capped, never above 0.85)

    Will recalibrate from empirical WR-vs-confidence once we have
    100+ settled trades. The function lives in one place so the
    recalibration touches only this code.
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
    """Compute the dollar size to bet via half-Kelly + floor/cap.

    Reuses ``lib.kelly.kelly_fraction`` so the math stays in ONE place;
    this wrapper just adds confidence→p_win calibration + floor/cap.

    Returns ``(notional_usd, meta)`` where ``meta`` carries the
    diagnostic numbers we want persisted on the trade record:
    ``{p_win, kelly_fraction, half_kelly_fraction, sized_before_caps}``.

    Returns ``(0.0, meta)`` when Kelly says no edge. The caller MUST
    treat 0 as "skip this trade" — do NOT fall back to a flat size.
    """
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
    notional = max(floor, min(cap, sized))
    return round(notional, 4), meta

# Tightened from the old 0.05-0.95 band: at the extremes risk/reward
# is brutal (winning $0.15 on a $0.85 bet, or vice versa). Stay in
# the meat of the distribution where edge actually pays.
EXTREME_PRICE_FLOOR = 0.15
EXTREME_PRICE_CEIL = 0.85

# Neutral-market skip: if yes_ask is between these, the market is
# saying "I have no idea." No edge to extract.
NEUTRAL_MARKET_FLOOR = 0.45
NEUTRAL_MARKET_CEIL = 0.55

# Spread filter: skip when yes_ask - yes_bid exceeds this. Wide
# spreads eat any edge we think we have.
MAX_BID_ASK_SPREAD = 0.05


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
                                 #   | "won_early" (TP triggered)
                                 #   | "cut_loss" (SL triggered)
    resolved_at: str = ""
    paper_pnl: float = 0.0
    exit_price: float = 0.0      # the our-side price we exited at
                                 # (0 = never exited intra-window)
    exit_reason: str = ""        # "take_profit" | "stop_loss" |
                                 # "settled" | ""
    # Kelly sizing diagnostics (populated on entry). Lets us look back
    # and see how the sizer was thinking; recalibrate over time.
    p_win_estimated: float = 0.0     # our estimated win prob from
                                     # confidence (not actual outcome)
    kelly_fraction: float = 0.0      # raw Kelly suggestion (pre-half)
    half_kelly_fraction: float = 0.0  # the fraction actually applied


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
    min_seconds_to_close: float = DEFAULT_MIN_SECONDS_TO_CLOSE,
    debug_skips: bool = False,
) -> list[KalshiFifteenMinPaperTrade]:
    """Record paper trades for any qualifying Kalshi 15-min sample.

    Side selection from composite sign:
      composite > 0 → buy YES at yes_ask (or last_price fallback)
      composite < 0 → buy NO  at no_ask  (or 1 - yes_ask fallback)

    Hard filter chain (in order, short-circuit on first failure):
      1. confidence >= min_confidence
      2. min_seconds_to_close < T-close <= max_seconds_to_close
      3. ticker not already open
      4. market not in neutral zone (yes_ask 0.45-0.55 → no edge)
      5. yes/no fill inside meat of distribution (0.15-0.85)
      6. bid-ask spread <= MAX_BID_ASK_SPREAD (skip illiquid markets)
    """
    if not samples:
        return []

    existing = _load_all()
    open_tickers = {r.get("market_ticker") for r in existing
                    if r.get("status") == "open"}

    # Kelly sizing replaces the old `bankroll * risk_per_trade` flat
    # notional. We still pass `risk_per_trade` through as a soft cap
    # check (no trade should exceed that fraction of bankroll), but
    # the per-trade size now varies with edge.
    soft_cap = bankroll * risk_per_trade * 5.0  # ≤ 2.5% of bankroll per trade
    now_iso = datetime.now(timezone.utc).isoformat()
    new_trades: list[KalshiFifteenMinPaperTrade] = []
    new_rows: list[dict] = []
    skip_counts: dict = {}

    for sig in samples:
        s = sig if isinstance(sig, dict) else asdict(sig)
        indicators = s.get("indicators")
        if not isinstance(indicators, dict):
            skip_counts["no_indicators"] = skip_counts.get("no_indicators", 0) + 1
            continue
        confidence = float(indicators.get("confidence", 0) or 0)
        composite = float(indicators.get("composite", 0) or 0)
        if confidence < min_confidence:
            skip_counts["low_confidence"] = skip_counts.get("low_confidence", 0) + 1
            continue

        seconds_to_close = float(s.get("seconds_to_close", 0) or 0)
        if not (min_seconds_to_close <= seconds_to_close <= max_seconds_to_close):
            skip_counts["out_of_window"] = skip_counts.get("out_of_window", 0) + 1
            continue

        ticker = s.get("market_ticker") or ""
        if not ticker or ticker in open_tickers:
            skip_counts["dup_open"] = skip_counts.get("dup_open", 0) + 1
            continue

        # Neutral-market skip — if the market itself has no opinion,
        # we have no edge to extract. This is the cleanest filter
        # added in the revamp.
        yes_ask = s.get("yes_ask")
        yes_bid = s.get("yes_bid")
        if yes_ask is not None and NEUTRAL_MARKET_FLOOR <= float(yes_ask) <= NEUTRAL_MARKET_CEIL:
            skip_counts["neutral_market"] = skip_counts.get("neutral_market", 0) + 1
            continue

        # Spread filter — wide spreads eat any edge
        if (yes_ask is not None and yes_bid is not None
                and float(yes_ask) - float(yes_bid) > MAX_BID_ASK_SPREAD):
            skip_counts["wide_spread"] = skip_counts.get("wide_spread", 0) + 1
            continue

        # Side selection
        if composite > 0:
            side = "YES"
            fill = yes_ask if yes_ask is not None else s.get("last_price")
            if fill is None:
                skip_counts["no_fill"] = skip_counts.get("no_fill", 0) + 1
                continue
        elif composite < 0:
            side = "NO"
            fill = s.get("no_ask")
            if fill is None and yes_ask is not None:
                fill = round(1.0 - float(yes_ask), 4)
            if fill is None:
                skip_counts["no_fill"] = skip_counts.get("no_fill", 0) + 1
                continue
        else:
            skip_counts["zero_composite"] = skip_counts.get("zero_composite", 0) + 1
            continue

        fill = float(fill)
        if not (EXTREME_PRICE_FLOOR <= fill <= EXTREME_PRICE_CEIL):
            skip_counts["extreme_price"] = skip_counts.get("extreme_price", 0) + 1
            continue

        # ── Multi-timeframe agreement gate (5m + 15m + 1h) ──────
        # Require multiple timeframes to lean the same direction as
        # the composite signal before firing. Disagreement = no trend,
        # no edge. Configurable required_agreement (default 2-of-3 —
        # 3-of-3 is too strict and rejects most candidates).
        try:
            from lib.kalshi_multi_timeframe import (
                check_multi_timeframe_agreement,
            )
            mtf_pass, mtf_meta = check_multi_timeframe_agreement(
                primary_direction=side,
                symbol="BTCUSDT",
                required_agreement=2,
            )
            if not mtf_pass:
                skip_counts["mtf_disagreement"] = skip_counts.get("mtf_disagreement", 0) + 1
                continue
        except Exception:
            pass

        # ── Conformal-prediction safety gate (split CP) ─────────
        # If the strike sits comfortably outside our prediction interval
        # on our side, the trade has strong distribution-free directional
        # support. If it's INSIDE the interval, we're in coin-flip
        # territory — even our forecaster can't be confident, so skip.
        # Falls open (no skip) when calibrator hasn't been fit yet.
        try:
            from lib.kalshi_conformal import (
                load_calibrator as _load_cp,
                predict_interval as _cp_interval,
                confidence_from_interval as _cp_conf,
            )
            cp_cal = _load_cp()
            strike = float(s.get("strike", 0) or 0)
            spot = float(s.get("spot_usd", 0) or 0)
            klines_local = (s.get("klines") or []) if isinstance(s.get("klines"), list) else []
            if cp_cal is not None and not cp_cal.get("is_identity") and strike > 0 and spot > 0:
                lo, hi, _ = _cp_interval(spot, klines_local, calibrator=cp_cal)
                cp_conf, _ = _cp_conf(strike, side, lo, hi)
                # Hard skip when our directional confidence per CP is
                # below 0.50 — the interval contains the strike on our
                # side. Tunable via the gate threshold.
                if cp_conf is not None and cp_conf < 0.50:
                    skip_counts["conformal_skip"] = skip_counts.get("conformal_skip", 0) + 1
                    continue
        except Exception:
            # Conformal is opt-in observability — never block trades on
            # a failure to evaluate.
            pass

        # ── Calibrated confidence (isotonic regression) ─────────
        # Raw confidence isn't a probability — it's the bot's internal
        # signal-strength number. Isotonic calibration fits an empirical
        # mapping raw → realized win rate over resolved trades, giving
        # us a TRUE probability to size against. Below MIN_SAMPLES
        # resolved trades, calibrator returns identity (no change), so
        # this is safe to flip on with no historical baseline.
        try:
            from lib.kalshi_calibration import calibrate, fit_calibrator
            calibrator = fit_calibrator()  # cached for 1h
            calibrated_confidence = calibrate(confidence, calibrator)
        except Exception:
            calibrated_confidence = confidence

        # ── Kelly sizing ────────────────────────────────────────
        # Replaces the old flat $5 per trade with edge-proportional
        # sizing. Half-Kelly + caps for safety. Returns 0 when there's
        # no positive edge (rare given our other filters, but possible
        # when fill is between our threshold-implied prob and 0.5).
        # Uses the CALIBRATED confidence so Kelly sizes against the
        # true probability, not the raw composite-ratio.
        kelly_cap = min(DEFAULT_MAX_TRADE_USD, soft_cap)
        notional, kelly_meta = kelly_sized_notional(
            confidence=calibrated_confidence,
            fill_price=fill,
            bankroll=bankroll,
            multiplier=DEFAULT_KELLY_MULTIPLIER,
            floor=DEFAULT_MIN_TRADE_USD,
            cap=kelly_cap,
        )
        # Record both raw and calibrated on the trade for later analysis.
        kelly_meta["confidence_raw"] = round(confidence, 4)
        kelly_meta["confidence_calibrated"] = round(calibrated_confidence, 4)
        if notional <= 0:
            skip_counts["kelly_no_edge"] = skip_counts.get("kelly_no_edge", 0) + 1
            continue

        contracts = round(notional / fill, 4)
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
            p_win_estimated=kelly_meta["p_win"],
            kelly_fraction=kelly_meta["kelly_fraction"],
            half_kelly_fraction=kelly_meta["half_kelly_fraction"],
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
            "skip_counts": skip_counts,
        })
    if debug_skips and skip_counts:
        log_event("kalshi_15min_paper", "filter_skips",
                  {"counts": skip_counts}, result="degraded")
    return new_trades


# ── Intra-window exit ────────────────────────────────────────────────

def check_open_trades_for_exit() -> dict:
    """Walk every open paper trade and check if intra-window
    take-profit or stop-loss has triggered.

    For each open trade, fetch the live Kalshi market and look at
    the price of OUR side:
      * YES position → look at yes_ask
      * NO position → look at no_ask

    Triggers:
      * our_side_price >= INTRA_WINDOW_TAKE_PROFIT → exit at TP
        paper_pnl = (exit_price - fill) * size, applies 7% Kalshi fee
      * our_side_price <= INTRA_WINDOW_STOP_LOSS → exit at SL
        paper_pnl = (exit_price - fill) * size (negative; no fee on loss)

    Returns counts dict for logging.
    """
    import requests
    rows = _load_all()
    if not rows:
        return {"checked": 0, "tp_exits": 0, "sl_exits": 0, "pnl": 0.0}

    open_rows = [r for r in rows if r.get("status") == "open"]
    if not open_rows:
        return {"checked": 0, "tp_exits": 0, "sl_exits": 0, "pnl": 0.0}

    tp_exits = 0
    sl_exits = 0
    pnl_locked = 0.0
    now_iso = datetime.now(timezone.utc).isoformat()

    for r in open_rows:
        ticker = r.get("market_ticker")
        side = str(r.get("side", "")).upper()
        if not ticker or side not in ("YES", "NO"):
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
        # Already resolved? Skip — settlement will handle.
        if market.get("status") not in ("active", "open"):
            continue

        # Our-side current price (prefer the _dollars form).
        def _price(key):
            v = market.get(key + "_dollars")
            if v is None:
                v = market.get(key)
                if v is not None and float(v) > 1.5:
                    v = float(v) / 100.0
            try:
                return float(v) if v is not None else None
            except (ValueError, TypeError):
                return None

        our_price = _price("yes_ask" if side == "YES" else "no_ask")
        if our_price is None:
            continue

        size = float(r.get("our_size", 0) or 0)
        fill = float(r.get("fill_price", 0) or 0)

        if our_price >= INTRA_WINDOW_TAKE_PROFIT:
            # Take profit — exit at TP threshold
            gross_profit = (INTRA_WINDOW_TAKE_PROFIT - fill) * size
            r["status"] = "won_early"
            r["exit_price"] = INTRA_WINDOW_TAKE_PROFIT
            r["exit_reason"] = "take_profit"
            r["resolved_at"] = now_iso
            # Apply Kalshi 7% fee on profit
            r["paper_pnl"] = round(gross_profit * (1.0 - 0.07), 4)
            tp_exits += 1
            pnl_locked += r["paper_pnl"]
        elif our_price <= INTRA_WINDOW_STOP_LOSS:
            # Stop loss — cut at SL threshold (saves remaining capital)
            r["status"] = "cut_loss"
            r["exit_price"] = INTRA_WINDOW_STOP_LOSS
            r["exit_reason"] = "stop_loss"
            r["resolved_at"] = now_iso
            r["paper_pnl"] = round((INTRA_WINDOW_STOP_LOSS - fill) * size, 4)
            sl_exits += 1
            pnl_locked += r["paper_pnl"]

    if tp_exits + sl_exits > 0:
        _save_all(rows)
        log_event("kalshi_15min_paper", "intra_window_exits", {
            "tp_exits": tp_exits, "sl_exits": sl_exits,
            "pnl_locked": round(pnl_locked, 2),
        })
    return {
        "checked": len(open_rows),
        "tp_exits": tp_exits, "sl_exits": sl_exits,
        "pnl": round(pnl_locked, 2),
    }


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
        "won_early": 0, "cut_loss": 0,
        "total_paper_pnl": 0.0, "capital_deployed": 0.0,
        "per_day_pnl": {},
        "by_confidence_bucket": {},
        "by_asset": {},
        "by_exit_reason": {},
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
        elif status == "won_early":
            s["won_early"] += 1
            s["won"] += 1   # rolls into wins for headline WR
        elif status == "cut_loss":
            s["cut_loss"] += 1
            s["lost"] += 1  # rolls into losses for headline WR
        else:
            s["void"] += 1
        if opened:
            s["per_day_pnl"][opened] = round(
                s["per_day_pnl"].get(opened, 0.0) + pnl, 4
            )
        # Bucket calibration uses ALL settled outcomes (regular + intra-window)
        if status in ("won", "lost", "won_early", "cut_loss"):
            b = s["by_confidence_bucket"].setdefault(
                bucket, {"settled": 0, "wins": 0, "pnl": 0.0},
            )
            b["settled"] += 1
            if status in ("won", "won_early"):
                b["wins"] += 1
            b["pnl"] = round(b["pnl"] + pnl, 4)

        # Track exit reasons (informative for "did intra-window save us?")
        reason = r.get("exit_reason") or ("settled" if status in ("won", "lost") else "")
        if reason:
            er = s["by_exit_reason"].setdefault(
                reason, {"count": 0, "pnl": 0.0},
            )
            er["count"] += 1
            er["pnl"] = round(er["pnl"] + pnl, 4)

        # Per-asset breakdown
        a = s["by_asset"].setdefault(
            asset, {"total": 0, "open": 0, "won": 0, "lost": 0, "void": 0,
                    "won_early": 0, "cut_loss": 0,
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

    # Per-asset rollup: WR and ROI. Roll up won_early into wins and
    # cut_loss into losses so the headline numbers match the all-up
    # view above.
    for asset, a in s["by_asset"].items():
        total_wins = a.get("won", 0) + a.get("won_early", 0)
        total_losses = a.get("lost", 0) + a.get("cut_loss", 0)
        asettled = total_wins + total_losses
        a["win_rate"] = (
            round(total_wins / asettled, 4) if asettled > 0 else 0.0
        )
        a["roi_pct"] = (
            round(a["pnl"] / a["capital"], 4) if a["capital"] > 0 else 0.0
        )
        # Surface rolled-up totals for any caller wanting the
        # "what really happened" view
        a["total_wins"] = total_wins
        a["total_losses"] = total_losses
    return s
