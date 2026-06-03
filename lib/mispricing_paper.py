"""Mispricing-edge paper sleeve — the THIRD A/B arm (PAPER-ONLY).

Trades polybot's MEASURED edge directly: for each weather market it bets the side
whose realized win-rate-at-this-fill exceeds the fill price — the favorite-longshot
mispricing (see lib/mispricing_gauge.py + scripts/mispricing_map.py + memory
"mispricing-edge"). It IGNORES the forecast entirely; the signal is purely
measured_edge(fill) = measured_p_win(fill) − fill, from the realized history.

This is the third arm alongside:
  * run_weather_original.sh  — ungated forecast-edge ("original" replica)
  * run_weather_edge.sh      — current forecast gates ("edge" / control)
  * THIS (run_weather_mispricing.sh) — measured-mispricing edge

LIVE (2026-06-02): opt-in via MISPRICING_LIVE=1 (+ the global live switch). When
armed, selected NO trades route through the same kalshi_live_executor rails as
the hourly sleeve, capped by MISPRICING_LIVE_BUDGET (default $40) on top of all
executor rails. Default OFF → paper-only. See the LIVE block below for details.

REUSE: the live weather SCAN (sample_signals) for candidates, and the existing
SETTLEMENT (weather-paper-settle, via the WEATHER_PAPER_LOG env override) so all
three arms settle under identical realistic logic.

SIDE: weather's measured edge is a NO-side phenomenon — the gauge is built from a
mostly-NO history and the favorite-longshot bias rewards betting *against* the
longshot YES. So this sleeve trades NO, sized by the measured edge. (BTC/daily
can be added later with their own gauges.)

Built 2026-06-02 (mispricing A/B arm).
"""
from __future__ import annotations

import json
import os
import random
from datetime import datetime, timezone
from pathlib import Path

from tradingcore import log_event
from lib.weather_signal import sample_signals
from lib.mispricing_gauge import build_gauge, measured_p_win, measured_edge

ROOT = Path(__file__).resolve().parent.parent

# Output ledger — env-overridable so the runner points it at a separate file.
OUT_LOG = Path(os.environ.get("WEATHER_PAPER_LOG")
               or (ROOT / "data" / "weather_paper_mispricing.jsonl"))
# Build the gauge from the rich existing weather history (cold-start bootstrap).
GAUGE_SOURCE = Path(os.environ.get("MISPRICING_GAUGE_SOURCE")
                    or (ROOT / "data" / "weather_paper.jsonl"))

# Selection / sizing params (env-overridable for tuning). Sizing params mirror
# the other arms (kelly 0.25, $20 cap, $1000 paper bankroll) so P&L is comparable.
MIN_EDGE  = float(os.environ.get("MISPRICING_MIN_EDGE", "0.05"))   # measured edge floor
MAX_FILL  = float(os.environ.get("MISPRICING_MAX_FILL", "0.70"))   # avoid the razor-thin band
KELLY_MULT = float(os.environ.get("MISPRICING_KELLY_MULT", "0.25"))
MAX_USD    = float(os.environ.get("MISPRICING_MAX_USD", "20.0"))
BANKROLL   = float(os.environ.get("MISPRICING_BANKROLL", "1000.0"))
PRICE_FLOOR, PRICE_CEIL = 0.05, 0.97

# ── LIVE execution (opt-in via MISPRICING_LIVE=1) ──────────────────────────
# When armed, selected NO trades route through the SAME live executor + rails as
# the hourly weather sleeve: balance floor, kill-switch, daily-loss halt, per-
# asset budget, and the 24h (ticker,side) dedup. That dedup is SHARED across all
# live sleeves (via order_gate), so it ALSO blocks double-exposure against the
# live hourly weather sleeve on the same KXTEMPNYC market. Orders bucket under
# asset="weather" (the executor keys budget off the ticker prefix), so they draw
# from the existing weather pool — total real weather exposure stays ≤ that pool.
# An ADDITIONAL module-level self-cap limits mispricing's OWN open live exposure
# to MISPRICING_LIVE_BUDGET. Default off → paper-only (zero behavior change).
MISPRICING_LIVE        = os.environ.get("MISPRICING_LIVE") == "1"
MISPRICING_LIVE_BUDGET = float(os.environ.get("MISPRICING_LIVE_BUDGET", "40.0"))


def _load(path: Path) -> list[dict]:
    out: list[dict] = []
    if path.exists():
        for ln in path.read_text().splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
    return out


def _save(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    tmp.replace(path)


def _full_kelly(p: float, fill: float) -> float:
    """Full-Kelly fraction for buying our side at `fill` (pays $1 on win).
    f* = p − (1−p)·fill/(1−fill). Clamped at 0 (never bet a negative edge)."""
    if not (0.0 < fill < 1.0):
        return 0.0
    return max(0.0, p - (1.0 - p) * fill / (1.0 - fill))


def record_mispricing_trades() -> list[dict]:
    """Scan → build gauge → select NO trades by measured edge → append records
    to OUT_LOG. Returns the new records. Routes live orders through the executor
    rails ONLY when MISPRICING_LIVE=1 and the global live switch is on; otherwise
    paper-only."""
    samples = sample_signals()
    gauge = build_gauge(_load(GAUGE_SOURCE))
    existing = _load(OUT_LOG)
    open_tickers = {r.get("market_ticker") for r in existing
                    if r.get("status") == "open"}
    now_iso = datetime.now(timezone.utc).isoformat()

    # ── live-execution setup (paper-only unless MISPRICING_LIVE armed AND the
    # global live switch is on). Failing to load the executor → stay paper. ──
    _live = MISPRICING_LIVE
    _cfg = None
    _bal = None
    if _live:
        try:
            from lib.kalshi_live_executor import is_live_enabled, _load_live_config
            _live = is_live_enabled()
            _cfg = _load_live_config() if _live else None
        except Exception:
            _live = False
    # mispricing's OWN open live exposure — the self-cap denominator.
    open_live_notional = sum(
        float(r.get("live_notional_usd") or 0.0) for r in existing
        if r.get("is_live") is True and str(r.get("status")).lower() == "open"
    )
    committed_in_cycle = 0.0
    committed_count = 0
    n_live = 0

    new: list[dict] = []
    skip: dict[str, int] = {}
    for s in samples:
        ticker = s.get("market_ticker", "")
        if not ticker or ticker in open_tickers:
            skip["dup_open"] = skip.get("dup_open", 0) + 1
            continue
        # NO side: fill = no_ask, else derive from yes_ask.
        no_ask = s.get("no_ask")
        if no_ask is None and s.get("yes_ask") is not None:
            no_ask = round(1.0 - float(s["yes_ask"]), 4)
        if no_ask is None:
            skip["no_fill"] = skip.get("no_fill", 0) + 1
            continue
        raw_fill = float(no_ask)
        if not (PRICE_FLOOR <= raw_fill <= PRICE_CEIL):
            skip["extreme_price"] = skip.get("extreme_price", 0) + 1
            continue
        if raw_fill > MAX_FILL:
            skip["fill_too_high"] = skip.get("fill_too_high", 0) + 1
            continue
        # THE SIGNAL: measured mispricing at this fill (no forecast).
        m_edge = measured_edge(raw_fill, gauge)
        if m_edge < MIN_EDGE:
            skip["edge_too_small"] = skip.get("edge_too_small", 0) + 1
            continue
        p_win = measured_p_win(raw_fill, gauge)

        # Paper slippage model — same shape as weather_paper for comparability.
        slip = 0.01 + (min(MAX_USD, 3.0) * 0.5 / 3.0) * 0.01 + random.uniform(-0.005, 0.01)
        fill_paper = max(0.01, min(0.99, raw_fill + slip))

        kf = _full_kelly(p_win, fill_paper)
        notional = min(MAX_USD, KELLY_MULT * kf * BANKROLL)
        if notional <= 0:
            skip["kelly_no_edge"] = skip.get("kelly_no_edge", 0) + 1
            continue
        contracts = round(notional / fill_paper, 4)

        # ── LIVE branch (opt-in). place_live_order runs ALL rails internally
        # (balance floor, kill-switch, daily-loss, concurrent, per-asset budget,
        # 24h ticker+side dedup, trade-size) and returns None on any refusal —
        # paper still records below. The module self-cap stops once mispricing's
        # own OPEN live exposure reaches MISPRICING_LIVE_BUDGET. ──
        live_order = None
        if _live and open_live_notional < MISPRICING_LIVE_BUDGET:
            try:
                from lib.kalshi_live_executor import (
                    place_live_order, effective_max_trade_usd)
                if _bal is None:
                    try:
                        from lib.kalshi_client import KalshiClient as _KC
                        _bal = _KC().get_balance()
                    except Exception:
                        _bal = None
                remaining = MISPRICING_LIVE_BUDGET - open_live_notional
                per_trade_cap = effective_max_trade_usd(_cfg, available_balance=_bal)
                live_cap = max(0.0, min(per_trade_cap, remaining))
                max_live_ct = int(live_cap / raw_fill) if raw_fill > 0 else 0
                live_ct = max(0, min(int(contracts), max_live_ct))
                if live_ct >= 1:
                    live_order = place_live_order(
                        market_ticker=ticker, side="NO", fill_price=raw_fill,
                        contracts=live_ct,
                        metadata={
                            "p_win": round(p_win, 4),
                            "kelly_fraction": round(kf, 4),
                            "strike_f": s.get("strike_f"),
                            "nws_forecast_f": s.get("nws_forecast_f"),
                            "edge": round(m_edge, 4),
                            "close_time": str(s.get("close_time", "")),
                            "city": s.get("city"),
                            # MUST be "weather" — the executor allowlist + budget
                            # bucket + shared dedup all key off this.
                            "asset": "weather",
                            "sleeve": "mispricing",   # provenance only
                            "paper_contracts": int(contracts),
                        },
                        committed_in_cycle=committed_in_cycle,
                        committed_count_in_cycle=committed_count,
                    )
                    if live_order is not None:
                        _ln = float(live_order.get("filled_notional_usd",
                                    live_order.get("notional_usd", 0.0)) or 0.0)
                        committed_in_cycle += float(live_order.get("notional_usd") or 0.0)
                        committed_count += 1
                        open_live_notional += _ln
                        n_live += 1
            except Exception as e:
                log_event("mispricing_paper", "live_branch_error",
                          {"ticker": ticker, "error": str(e)[:200]}, result="degraded")
                live_order = None

        # Live trades record the REAL ask + real notional; paper uses slipped.
        recorded_fill = raw_fill if live_order is not None else fill_paper
        recorded_notional = (float(live_order.get("notional_usd") or 0.0)
                             if live_order is not None
                             else round(fill_paper * contracts, 4))

        rec = {
            "trade_id": f"{ticker[:24]}_{int(datetime.now(timezone.utc).timestamp())}",
            "city": s.get("city", "?"),
            "market_ticker": ticker,
            "event_ticker": str(s.get("event_ticker", ""))[:60],
            "title": str(s.get("title", ""))[:200],
            "side": "NO",
            "fill_price": recorded_fill,
            "our_size": contracts,
            "notional": recorded_notional,
            "strike_f": float(s.get("strike_f", 0)),
            "nws_forecast_f": float(s.get("nws_forecast_f", 0) or 0),
            "nws_p_yes": float(s.get("nws_p_yes", 0) or 0),
            "market_p_yes": float(s.get("market_p_yes", 0) or 0),
            "edge": round(m_edge, 4),              # MEASURED edge, not forecast edge
            "close_time": str(s.get("close_time", "")),
            "opened_at": now_iso,
            "status": "open",
            "resolved_at": "",
            "paper_pnl": 0.0,
            "actual_temp_f": None,
            "kelly_fraction": round(kf, 4),
            "half_kelly_fraction": round(kf / 2.0, 4),
            "is_live": bool(live_order),
            "live_order_id": str(live_order.get("order_id", "")) if live_order else "",
            "live_contracts": int(live_order.get("filled_quantity", live_order.get("contracts", 0))) if live_order else 0,
            "live_notional_usd": float(live_order.get("filled_notional_usd", live_order.get("notional_usd", 0.0))) if live_order else 0.0,
            # provenance markers so this arm is identifiable in the ledger:
            "entry_schema": "mispricing_v1",
            "selection": "mispricing",
            "measured_p_win": round(p_win, 4),
            "raw_fill": round(raw_fill, 4),
        }
        new.append(rec)
        open_tickers.add(ticker)

    if new:
        _save(OUT_LOG, existing + new)
    log_event("mispricing_paper", "cycle", {
        "n_markets": len(samples), "opened": len(new), "live": n_live,
        "open_live_notional": round(open_live_notional, 2), "skips": skip,
        "gauge_bands": {b: gauge[b].get("edge") for b in gauge},
    }, result="success")
    # One-line human summary to stdout (mirrors the weather sleeve's style).
    bands = " ".join(f"{b}:{c.get('edge'):+.2f}(n{c.get('n')})" for b, c in sorted(gauge.items()))
    print(f"  [mispricing] sampled {len(samples)} | opened {len(new)} "
          f"({n_live} LIVE) | skips {skip or '{}'}")
    print(f"  [mispricing] gauge edge by band: {bands or '(empty)'}")
    if _live:
        print(f"  [mispricing] LIVE ARMED | open live exposure "
              f"${open_live_notional:.2f} / ${MISPRICING_LIVE_BUDGET:.0f} cap")
    return new
