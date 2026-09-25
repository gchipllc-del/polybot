"""
Kalshi weather sleeve — paper trading + settlement.

Records hypothetical YES/NO entries on temperature buckets when the
blended forecast shows enough edge over the market, then settles via the
public Kalshi ``result`` field once the market closes. Mirrors
``kalshi_15min_paper`` (atomic JSONL ledger, half-Kelly sizing, public
settlement) but the entry signal is forecast EDGE rather than a composite.

No real orders. Pure measurement until promoted past the
live_migration_approved gate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

try:
    from tradingcore.audit import log_event
except Exception:  # pragma: no cover
    def log_event(*_a, **_k):
        return None

try:
    from tradingcore.kelly import kelly_fraction
except Exception:  # pragma: no cover
    def kelly_fraction(our_prob, market_prob):
        if market_prob <= 0 or market_prob >= 1:
            return 0.0
        if our_prob <= 0 or our_prob >= 1:
            return 0.0
        b = (1.0 - market_prob) / market_prob
        return (our_prob * b - (1.0 - our_prob)) / b

PAPER_PATH = Path(__file__).parent.parent / "data" / "kalshi_weather_paper.jsonl"
KALSHI_HOST = "https://api.elections.kalshi.com/trade-api/v2"

# Defaults; per-cycle values come from config/kalshi_weather.yaml params.
DEFAULT_MIN_EDGE = 0.08
DEFAULT_EXTREME_FLOOR = 0.10
DEFAULT_EXTREME_CEIL = 0.90
DEFAULT_MAX_SPREAD = 0.06
DEFAULT_KELLY_MULTIPLIER = 0.5
DEFAULT_MIN_TRADE_USD = 1.0
DEFAULT_MAX_TRADE_USD = 25.0
DEFAULT_BANKROLL = 1000.0
KALSHI_FEE = 0.07

# Forecast LEAD-TIME guard. This is the critical realism control for the
# weather sleeve: a market resolving in ~2 minutes has a "forecast" that is
# essentially the CURRENT observed temperature (the provider's hourly value
# for the closing hour ≈ what's already happening), so the model shows huge,
# near-certain edge and "wins" almost every such trade on paper — edge that
# (a) isn't a real forecast and (b) you couldn't actually fill into a thin,
# about-to-settle book. We therefore require a genuine lead so the fair value
# is a real PREDICTION, not a peek at the outcome. 30 min minimum.
DEFAULT_MIN_SECONDS_TO_CLOSE = 1800.0      # 30 min — genuine forecast lead
DEFAULT_MAX_SECONDS_TO_CLOSE = 6 * 3600.0  # 6 h


def _load_params() -> dict:
    try:
        from lib.kalshi_weather_signal import load_config
        return dict((load_config().get("params") or {}))
    except Exception:
        return {}


@dataclass
class WeatherPaperTrade:
    trade_id: str
    city: str
    market_ticker: str
    event_ticker: str
    title: str
    side: str                 # "YES" | "NO"
    fill_price: float
    our_size: float
    notional: float
    fair_yes: float
    edge: float
    forecast_mu: float | None
    forecast_sigma: float | None
    floor_strike: float | None
    cap_strike: float | None
    close_time: str
    opened_at: str
    # Forecast lead at entry. Recorded so we can PROVE no near-resolution
    # (phantom-edge) trades slip through — post lead-fix this should always
    # be >= min_seconds_to_close (1800s / 30 min).
    seconds_to_close_at_entry: float = 0.0
    status: str = "open"      # open | won | lost | void
    resolved_at: str = ""
    paper_pnl: float = 0.0
    p_win_estimated: float = 0.0
    kelly_fraction: float = 0.0


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


def _kelly_notional(our_prob: float, fill: float, bankroll: float,
                    mult: float, floor: float, cap: float) -> tuple[float, dict]:
    kf = kelly_fraction(our_prob, fill)
    half = kf * mult
    sized = bankroll * half
    meta = {"p_win": round(our_prob, 4), "kelly_fraction": round(kf, 4),
            "half_kelly_fraction": round(half, 4)}
    if kf <= 0.0:
        return 0.0, meta
    return round(max(floor, min(cap, sized)), 4), meta


def record_paper_trades_from_samples(
    samples: list[dict], *, params: dict | None = None,
) -> list[WeatherPaperTrade]:
    """Open paper trades on buckets where the forecast beats the market by
    >= min_edge. Side = whichever of YES/NO is underpriced more.

    Filter chain (short-circuit in order):
      1. has a fair value + a tradeable price
      2. min_seconds_to_close <= T-close <= max_seconds_to_close
      3. ticker not already open
      4. not neutral / not extreme price / spread ok
      5. edge >= min_edge on the chosen side
      6. Kelly shows positive edge
    """
    if not samples:
        return []
    p = params or _load_params()
    min_edge = float(p.get("min_edge", DEFAULT_MIN_EDGE))
    extreme_floor = float(p.get("extreme_floor", DEFAULT_EXTREME_FLOOR))
    extreme_ceil = float(p.get("extreme_ceil", DEFAULT_EXTREME_CEIL))
    max_spread = float(p.get("max_spread", DEFAULT_MAX_SPREAD))
    bankroll = float(p.get("bankroll", DEFAULT_BANKROLL))
    mult = float(p.get("kelly_multiplier", DEFAULT_KELLY_MULTIPLIER))
    floor_usd = float(p.get("min_trade_usd", DEFAULT_MIN_TRADE_USD))
    cap_usd = float(p.get("max_trade_usd", DEFAULT_MAX_TRADE_USD))
    min_tc = float(p.get("min_seconds_to_close", DEFAULT_MIN_SECONDS_TO_CLOSE))
    max_tc = float(p.get("max_seconds_to_close", DEFAULT_MAX_SECONDS_TO_CLOSE))

    existing = _load_all()
    open_tickers = {r.get("market_ticker") for r in existing
                    if r.get("status") == "open"}
    now_iso = datetime.now(timezone.utc).isoformat()
    new_trades: list[WeatherPaperTrade] = []
    new_rows: list[dict] = []
    skips: dict = {}

    def _skip(reason):
        skips[reason] = skips.get(reason, 0) + 1

    for s in samples:
        fair = s.get("fair_yes")
        if fair is None:
            _skip("no_fair")
            continue
        stc = float(s.get("seconds_to_close", 0) or 0)
        if not (min_tc <= stc <= max_tc):
            _skip("out_of_window")
            continue
        ticker = s.get("market_ticker") or ""
        if not ticker or ticker in open_tickers:
            _skip("dup_open")
            continue

        yes_ask = s.get("yes_ask")
        yes_bid = s.get("yes_bid")
        no_ask = s.get("no_ask")
        fair = float(fair)

        # Spread filter (YES side spread as the liquidity proxy).
        if (yes_ask is not None and yes_bid is not None
                and float(yes_ask) - float(yes_bid) > max_spread):
            _skip("wide_spread")
            continue

        edge_yes = (fair - float(yes_ask)) if yes_ask is not None else None
        edge_no = ((1.0 - fair) - float(no_ask)) if no_ask is not None else None

        # Pick the side with the larger positive edge.
        side = fill = our_prob = edge = None
        if edge_yes is not None and (edge_no is None or edge_yes >= edge_no):
            if edge_yes >= min_edge:
                side, fill, our_prob, edge = "YES", float(yes_ask), fair, edge_yes
        if side is None and edge_no is not None and edge_no >= min_edge:
            side, fill, our_prob, edge = "NO", float(no_ask), 1.0 - fair, edge_no
        if side is None:
            _skip("no_edge")
            continue

        # NOTE: no "neutral market" skip here (unlike the crypto sleeve).
        # Our edge comes from the forecast, not the market's confidence —
        # a bucket priced mid-range that our forecast disagrees with is
        # precisely the mispricing we want. The min_edge gate handles
        # quality. We still avoid the brutal extremes.
        if not (extreme_floor <= fill <= extreme_ceil):
            _skip("extreme_price")
            continue

        notional, meta = _kelly_notional(
            our_prob, fill, bankroll, mult, floor_usd, cap_usd)
        if notional <= 0:
            _skip("kelly_no_edge")
            continue

        contracts = round(notional / fill, 4)
        trade = WeatherPaperTrade(
            trade_id=f"{ticker[:24]}_{int(datetime.now(timezone.utc).timestamp())}",
            city=str(s.get("city", "")),
            market_ticker=ticker,
            event_ticker=str(s.get("event_ticker", ""))[:60],
            title=str(s.get("title", ""))[:200],
            side=side,
            fill_price=fill,
            our_size=contracts,
            notional=round(fill * contracts, 4),
            fair_yes=round(fair, 4),
            edge=round(edge, 4),
            forecast_mu=s.get("forecast_mu"),
            forecast_sigma=s.get("forecast_sigma"),
            floor_strike=s.get("floor_strike"),
            cap_strike=s.get("cap_strike"),
            close_time=str(s.get("close_time", "")),
            opened_at=now_iso,
            seconds_to_close_at_entry=round(stc, 1),
            status="open",
            p_win_estimated=meta["p_win"],
            kelly_fraction=meta["kelly_fraction"],
        )
        new_trades.append(trade)
        new_rows.append(asdict(trade))
        open_tickers.add(ticker)

    if new_rows:
        existing.extend(new_rows)
        _save_all(existing)
        log_event("kalshi_weather_paper", "recorded", {
            "n_new_trades": len(new_rows), "min_edge": min_edge,
            "skip_counts": skips,
        })
    return new_trades


def settle_paper_trades() -> dict:
    """Poll public Kalshi markets for resolutions and book P&L."""
    import requests

    rows = _load_all()
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
            resp = requests.get(f"{KALSHI_HOST}/markets/{ticker}", timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            continue
        market = data.get("market") if isinstance(data, dict) else None
        if not isinstance(market, dict):
            continue
        result = str(market.get("result") or "").lower()
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
            gross = (1.0 - fill) * size
            r["paper_pnl"] = round(gross * (1.0 - KALSHI_FEE), 4)
        else:
            r["status"] = "lost"
            r["paper_pnl"] = round(-fill * size, 4)
        r["resolved_at"] = now_iso
        settled_now += 1
        pnl_now += r["paper_pnl"]

    _save_all(rows)
    open_count = sum(1 for r in rows if r.get("status") == "open")
    log_event("kalshi_weather_paper", "settle_cycle", {
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


def summary(city_filter: str | None = None) -> dict:
    """Aggregate weather paper P&L, with a per-city breakdown."""
    rows = _load_all()
    if city_filter:
        rows = [r for r in rows if r.get("city") == city_filter]
    # Trades entered with less than this lead are "near-close" — at that
    # horizon the forecast is ~the observed temp, so they're the phantom-edge
    # trades the lead-fix removed. Splitting P&L by this exposes how much of
    # any run-up came from them (pre-fix contamination).
    lead_guard = float((_load_params() or {}).get("min_seconds_to_close",
                                                   DEFAULT_MIN_SECONDS_TO_CLOSE))
    s = {
        "total_trades": len(rows), "city_filter": city_filter,
        "open": 0, "won": 0, "lost": 0, "void": 0,
        "total_paper_pnl": 0.0, "capital_deployed": 0.0,
        "by_city": {}, "by_edge_bucket": {},
        # near = entered < lead_guard (contaminated); genuine = >= lead_guard
        "lead_split": {
            "lead_guard_seconds": lead_guard,
            "near_close": {"settled": 0, "wins": 0, "pnl": 0.0},
            "genuine_lead": {"settled": 0, "wins": 0, "pnl": 0.0},
            "unknown_lead": {"settled": 0, "wins": 0, "pnl": 0.0},
            "min_entry_lead_min": None,
        },
    }
    leads_seen: list[float] = []
    for r in rows:
        status = r.get("status", "open")
        notional = float(r.get("notional", 0) or 0)
        pnl = float(r.get("paper_pnl", 0) or 0)
        city = r.get("city") or "?"
        edge = float(r.get("edge", 0) or 0)
        bucket = f"{int(edge * 100 // 5) * 5}-{int(edge * 100 // 5) * 5 + 5}%"

        # Lead-time split (settled trades only; older rows may lack the field).
        lead = r.get("seconds_to_close_at_entry")
        if lead is not None:
            leads_seen.append(float(lead))
        if status in ("won", "lost"):
            if lead is None:
                key = "unknown_lead"
            elif float(lead) < lead_guard:
                key = "near_close"
            else:
                key = "genuine_lead"
            ls = s["lead_split"][key]
            ls["settled"] += 1
            if status == "won":
                ls["wins"] += 1
            ls["pnl"] = round(ls["pnl"] + pnl, 4)

        s["capital_deployed"] += notional
        s["total_paper_pnl"] += pnl
        s[status] = s.get(status, 0) + 1

        c = s["by_city"].setdefault(
            city, {"total": 0, "open": 0, "won": 0, "lost": 0, "void": 0,
                   "pnl": 0.0, "capital": 0.0})
        c["total"] += 1
        c[status] = c.get(status, 0) + 1
        c["pnl"] = round(c["pnl"] + pnl, 4)
        c["capital"] = round(c["capital"] + notional, 4)

        if status in ("won", "lost"):
            b = s["by_edge_bucket"].setdefault(
                bucket, {"settled": 0, "wins": 0, "pnl": 0.0})
            b["settled"] += 1
            if status == "won":
                b["wins"] += 1
            b["pnl"] = round(b["pnl"] + pnl, 4)

    settled = s["won"] + s["lost"]
    s["win_rate"] = round(s["won"] / settled, 4) if settled else 0.0
    s["roi_pct"] = (round(s["total_paper_pnl"] / s["capital_deployed"], 4)
                    if s["capital_deployed"] > 0 else 0.0)
    s["total_paper_pnl"] = round(s["total_paper_pnl"], 4)
    s["capital_deployed"] = round(s["capital_deployed"], 4)
    for city, c in s["by_city"].items():
        cs = c["won"] + c["lost"]
        c["win_rate"] = round(c["won"] / cs, 4) if cs else 0.0
        c["roi_pct"] = round(c["pnl"] / c["capital"], 4) if c["capital"] > 0 else 0.0
    if leads_seen:
        s["lead_split"]["min_entry_lead_min"] = round(min(leads_seen) / 60.0, 1)
    return s
