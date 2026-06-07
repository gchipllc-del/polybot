"""
Web Dashboard Server — Flask API + HTML dashboard at localhost:5050.

Usage:
    python main.py dashboard
    python main.py dashboard --port 8080

Security:
    - Binds to 127.0.0.1 only (not exposed to network)
    - No secrets in any API response
    - Read-only endpoints (no mutations via web)
"""

import hmac
import os
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from lib.dashboard_data import (
    get_calibration_data,
    get_circuit_breaker_status,
    get_events,
    get_full_dashboard_state,
    get_portfolio_summary,
    get_positions_table,
    get_trade_history,
)
from lib.resolution_tracker import get_performance_summary

TEMPLATE_DIR = Path(__file__).parent.parent / "templates"

app = Flask(__name__, template_folder=str(TEMPLATE_DIR))


@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/state")
def api_state():
    return jsonify(get_full_dashboard_state())


@app.route("/api/portfolio")
def api_portfolio():
    return jsonify(get_portfolio_summary())


@app.route("/api/positions")
def api_positions():
    return jsonify(get_positions_table())


@app.route("/api/all_open_trades")
def api_all_open_trades():
    """Unified open-trades view across every sibling bot.
    Same response shape across polybot, cryptobot, wheel-trader.
    """
    from lib.all_bots_positions import get_all_bots_open_positions
    return jsonify(get_all_bots_open_positions())


@app.route("/api/kalshi_daily")
def api_kalshi_daily():
    """Snapshot of the KXBTCD (daily crypto) scanner: latest signal cycle
    + open paper trades + recent settlements."""
    import json
    from pathlib import Path
    from datetime import datetime, timezone
    root = Path(__file__).resolve().parent.parent
    out: dict = {"as_of": datetime.now(timezone.utc).isoformat()}

    # Latest signal samples (last N from the jsonl)
    sig_path = root / "data" / "kalshi_daily_signal.jsonl"
    recent_samples = []
    if sig_path.exists():
        with open(sig_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try: recent_samples.append(json.loads(line))
                    except json.JSONDecodeError: pass
    # Take only the LAST cycle's samples (heuristic: same sample_at)
    if recent_samples:
        latest_at = recent_samples[-1].get("sample_at")
        out["latest_samples"] = [s for s in recent_samples if s.get("sample_at") == latest_at]
    else:
        out["latest_samples"] = []

    # Open + recently-closed paper trades
    paper_path = root / "data" / "kalshi_daily_paper.jsonl"
    open_trades, recent_closed = [], []
    if paper_path.exists():
        with open(paper_path) as f:
            for line in f:
                line = line.strip()
                if not line: continue
                try:
                    t = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if t.get("status") == "open":
                    open_trades.append(t)
                elif t.get("status") in ("won", "lost"):
                    recent_closed.append(t)
    out["open_trades"] = open_trades
    out["recent_closed"] = recent_closed[-10:]
    # Use the shared scorecard helper so this matches /api/kalshi_paper_summary
    # exactly — single source of truth for P&L + capital math.
    stats = _strategy_pnl_stats(paper_path, "KXBTCD Daily")
    out["counts"] = {
        "open": stats["open"],
        "closed_total": stats["closed"],
        "won": stats["won"],
        "lost": stats["lost"],
        "wr_pct": stats["wr_pct"],
        "net_pnl": stats["net_pnl"],
        "total_invested": stats["total_invested"],
        "open_notional": stats["open_notional"],
        "peak_capital": stats["peak_capital"],
        "avg_notional": stats["avg_notional"],
        "roi_invested_pct": stats["roi_invested_pct"],
        "roi_peak_pct": stats["roi_peak_pct"],
    }
    # Per-asset breakdown (BTC/ETH/SOL/SPY when SPY trading hours hit).
    # Uses the shared _group_paper_trades helper — same logic as the
    # weather by_city block + 15-min by_asset block.
    out["by_asset"] = _group_paper_trades(paper_path, key_field="asset")
    return jsonify(out)


@app.route("/api/weather")
def api_weather():
    """Snapshot of the weather scanner: NWS forecasts + Kalshi pricing
    + edge per market + open paper trades."""
    import json
    from pathlib import Path
    from datetime import datetime, timezone
    root = Path(__file__).resolve().parent.parent
    out: dict = {"as_of": datetime.now(timezone.utc).isoformat()}

    sig_path = root / "data" / "weather_signal.jsonl"
    recent_samples = []
    if sig_path.exists():
        with open(sig_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try: recent_samples.append(json.loads(line))
                    except json.JSONDecodeError: pass
    if recent_samples:
        latest_at = recent_samples[-1].get("sample_at")
        out["latest_samples"] = [s for s in recent_samples if s.get("sample_at") == latest_at]
    else:
        out["latest_samples"] = []

    paper_path = root / "data" / "weather_paper.jsonl"
    open_trades, recent_closed = [], []
    if paper_path.exists():
        with open(paper_path) as f:
            for line in f:
                line = line.strip()
                if not line: continue
                try:
                    t = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if t.get("status") == "open":
                    open_trades.append(t)
                elif t.get("status") in ("won", "lost"):
                    recent_closed.append(t)
    out["open_trades"] = open_trades
    out["recent_closed"] = recent_closed[-10:]
    stats = _strategy_pnl_stats(paper_path, "Weather")
    out["counts"] = {
        "open": stats["open"],
        "closed_total": stats["closed"],
        "won": stats["won"],
        "lost": stats["lost"],
        "wr_pct": stats["wr_pct"],
        "net_pnl": stats["net_pnl"],
        "total_invested": stats["total_invested"],
        "open_notional": stats["open_notional"],
        "peak_capital": stats["peak_capital"],
        "avg_notional": stats["avg_notional"],
        "roi_invested_pct": stats["roi_invested_pct"],
        "roi_peak_pct": stats["roi_peak_pct"],
    }
    # Per-city breakdown — useful because NWS bias varies by location
    # (OKX/LWX/LOT/BOS offices have different forecast biases). Lets the
    # user see at-a-glance which cities are working vs need calibration.
    out["by_city"] = _group_paper_trades(paper_path, key_field="city")
    # LIVE cheap-NO trend veto activity (the gauge). Surfaced so the user can
    # see it working: how many live orders it blocked last cycle + over the
    # rolling window, with reasons. Absent/empty when the veto is disabled.
    veto_path = root / "data" / "weather_live_veto_activity.json"
    veto = {}
    if veto_path.exists():
        try:
            with open(veto_path) as vf:
                veto = json.load(vf) or {}
        except (OSError, json.JSONDecodeError):
            veto = {}
    out["veto"] = {
        "enabled": bool(veto.get("enabled")),
        "updated_at": veto.get("updated_at"),
        "last_cycle_vetoed": veto.get("last_cycle_vetoed", 0),
        "last_cycle_reasons": veto.get("last_cycle_reasons", {}),
        "window_vetoed_total": veto.get("window_vetoed_total", 0),
        "window_reasons": veto.get("window_reasons", {}),
    }
    return jsonify(out)


@app.route("/api/live_alerts")
def api_live_alerts():
    """Recent live-trade alerts — the canonical local notification surface
    (logs/live_alerts.log) that _send_telegram() always writes, whether or not
    Telegram push is configured. Surfaces live order-placed / partial-fill /
    refusal / kill-switch events so the user sees real-money activity on the
    dashboard. Parses the `[ts]\\nmessage` block format; returns newest first."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    log_path = root / "logs" / "live_alerts.log"
    alerts = []
    if log_path.exists():
        try:
            raw = log_path.read_text()
        except OSError:
            raw = ""
        # Blocks are separated by a blank line then a "[...]" timestamp header.
        import re
        # Split on the timestamp header, keeping it.
        parts = re.split(r"\n*\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d UTC)\]\n", raw)
        # parts = [pre, ts1, body1, ts2, body2, ...]
        for i in range(1, len(parts) - 1, 2):
            ts = parts[i]
            body = (parts[i + 1] or "").strip()
            if not body:
                continue
            first = body.splitlines()[0].strip()
            # Classify for the UI badge color.
            if "KILL SWITCH" in body:
                kind = "kill"
            elif "order PLACED" in body:
                kind = "placed"
            elif ("REFUSED" in body or "refused" in body
                  or "BLOCKED" in body or "blocked" in body):
                kind = "refused"
            elif "only" in body and "filled" in body:
                kind = "partial"
            else:
                kind = "info"
            alerts.append({"ts": ts, "kind": kind, "summary": first,
                           "detail": body})
    alerts.reverse()  # newest first
    return jsonify({"alerts": alerts[:25], "total": len(alerts),
                    "telegram_configured": bool(
                        __import__("os").environ.get("TELEGRAM_BOT_TOKEN")
                        and __import__("os").environ.get("TELEGRAM_CHAT_ID"))})


@app.route("/api/weather_daily")
def api_weather_daily():
    """Daily max/min weather sleeve (KXHIGHT*/KXLOWT*) — the recalibrated
    pilot that the original dashboard predated. Paper-only; reads
    weather_daily_paper.jsonl. Surfaces open trades + per-city + the post-fix
    scorecard (filtered to entry_schema in {strike_type_aware_v1, blended_v2}
    so the corrupt pre-fix records don't pollute the validation numbers)."""
    import json
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    out: dict = {}
    paper_path = root / "data" / "weather_daily_paper.jsonl"
    POSTFIX = ("strike_type_aware_v1", "blended_v2")
    open_trades, recent_closed = [], []
    n_prefix = 0
    if paper_path.exists():
        with open(paper_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    t = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Exclude pre-fix (quarantined) records from the live view.
                if t.get("entry_schema") not in POSTFIX:
                    n_prefix += 1
                    continue
                if t.get("status") == "open":
                    open_trades.append(t)
                elif t.get("status") in ("won", "lost"):
                    recent_closed.append(t)
    out["open_trades"] = sorted(open_trades, key=lambda r: str(r.get("opened_at", "")))
    out["recent_closed"] = recent_closed[-10:]
    out["excluded_prefix_records"] = n_prefix
    # Scorecard — reuse the shared helper but on post-fix rows only. The helper
    # walks the whole file, so compute counts here from the filtered sets.
    closed = [t for t in (open_trades + recent_closed) if t.get("status") in ("won", "lost")]
    # recent_closed already only has settled; open_trades only open — combine
    settled = recent_closed  # all won/lost post-fix collected above (capped at -10 for display)
    # Full settled set for accurate counts (re-read, post-fix only):
    all_settled = []
    if paper_path.exists():
        for line in paper_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                t = json.loads(line)
            except json.JSONDecodeError:
                continue
            if t.get("entry_schema") in POSTFIX and t.get("status") in ("won", "lost"):
                all_settled.append(t)
    nset = len(all_settled)
    won = sum(1 for t in all_settled if t.get("status") == "won")
    net = sum(float(t.get("paper_pnl") or 0) for t in all_settled)
    invested = sum(float(t.get("notional") or 0) for t in all_settled)
    out["counts"] = {
        "open": len(open_trades),
        "closed_total": nset,
        "won": won,
        "lost": nset - won,
        "wr_pct": round(won / nset * 100, 1) if nset else None,
        "net_pnl": round(net, 2),
        "open_notional": round(sum(float(t.get("notional") or 0) for t in open_trades), 2),
        # Keys the shared renderSectionStats tile expects (kept consistent with
        # the other paper sections so the UI renders clean values, not undefined).
        "total_invested": round(invested, 2),
        "peak_capital": 0.0,
        "avg_notional": round(invested / nset, 2) if nset else 0.0,
        "roi_invested_pct": round(net / invested * 100, 1) if invested else None,
        "roi_peak_pct": None,
    }
    out["by_city"] = _group_paper_trades(paper_path, key_field="city_key")
    return jsonify(out)


@app.route("/api/kalshi_15min")
def api_kalshi_15min():
    """Snapshot of the 15-min crypto scanner: latest signal cycle +
    open paper trades + recent settlements + per-asset breakdown.

    Mirrors /api/kalshi_daily shape so the frontend can reuse the
    rendering function (renderKalshiDaily-style)."""
    import json
    from pathlib import Path
    from datetime import datetime, timezone
    root = Path(__file__).resolve().parent.parent
    out: dict = {"as_of": datetime.now(timezone.utc).isoformat()}

    sig_path = root / "data" / "kalshi_15min_signal.jsonl"
    recent_samples = []
    if sig_path.exists():
        try:
            with open(sig_path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try: recent_samples.append(json.loads(line))
                        except json.JSONDecodeError: pass
        except OSError:
            pass
    if recent_samples:
        latest_at = recent_samples[-1].get("sample_at")
        out["latest_samples"] = [s for s in recent_samples if s.get("sample_at") == latest_at]
    else:
        out["latest_samples"] = []

    paper_path = root / "data" / "kalshi_15min_paper.jsonl"
    open_trades, recent_closed = [], []
    if paper_path.exists():
        try:
            with open(paper_path) as f:
                for line in f:
                    line = line.strip()
                    if not line: continue
                    try:
                        t = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if t.get("status") == "open":
                        open_trades.append(t)
                    elif t.get("status") in ("won", "lost", "won_early", "cut_loss"):
                        recent_closed.append(t)
        except OSError:
            pass
    out["open_trades"] = open_trades
    out["recent_closed"] = recent_closed[-10:]
    stats = _strategy_pnl_stats(paper_path, "KX 15-min")
    out["counts"] = {
        "open": stats["open"],
        "closed_total": stats["closed"],
        "won": stats["won"],
        "lost": stats["lost"],
        "wr_pct": stats["wr_pct"],
        "net_pnl": stats["net_pnl"],
        "total_invested": stats["total_invested"],
        "open_notional": stats["open_notional"],
        "peak_capital": stats["peak_capital"],
        "avg_notional": stats["avg_notional"],
        "roi_invested_pct": stats["roi_invested_pct"],
        "roi_peak_pct": stats["roi_peak_pct"],
    }
    out["by_asset"] = _group_paper_trades(paper_path, key_field="asset")
    return jsonify(out)


def _group_paper_trades(jsonl_path, key_field: str,
                        include_live: bool = False) -> list:
    """Shared helper for per-asset / per-city breakdowns. Walks a paper
    jsonl, groups by the given key field, computes counts + P&L + open
    notional. Returns rows sorted by net_pnl descending so winners
    surface first.

    PAPER vs LIVE (2026-06-01 audit fix): the live executor appends real-money
    fills (is_live=true) into the SAME kalshi_daily_paper.jsonl / weather_paper
    .jsonl files the paper trader writes. By default this helper now SKIPS
    is_live rows so the "Paper P&L" the dashboard shows is genuinely paper-only
    (the number used for paper->live graduation). Pass include_live=True for a
    combined view; the dedicated live API (api_kalshi_live) owns the live cut.

    The grouping key appears in the returned dict under TWO names: its
    original field name (so JS can read `a.asset` or `a.city` naturally)
    AND a generic `label` field (so generic render functions can be
    schema-agnostic). DRY pattern — one helper, three sections."""
    import json
    from pathlib import Path
    p = Path(jsonl_path) if not isinstance(jsonl_path, Path) else jsonl_path
    if not p.exists():
        return []
    groups: dict = {}
    try:
        with open(p) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    t = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not include_live and t.get("is_live"):
                    continue  # real-money fill — excluded from the paper view
                k = str(t.get(key_field, "?")).upper() or "?"
                g = groups.setdefault(k, {
                    key_field: k, "label": k,
                    "total": 0, "open": 0, "closed": 0,
                    "won": 0, "lost": 0, "net_pnl": 0.0,
                    "open_notional": 0.0,
                })
                g["total"] += 1
                status = t.get("status")
                notional = float(t.get("notional", 0) or 0)
                if status == "open":
                    g["open"] += 1
                    g["open_notional"] += notional
                # Include 15-min's won_early / cut_loss as closed outcomes
                elif status in ("won", "lost", "won_early", "cut_loss"):
                    g["closed"] += 1
                    if status in ("won", "won_early"):
                        g["won"] += 1
                    else:
                        g["lost"] += 1
                    g["net_pnl"] += float(t.get("paper_pnl", 0) or 0)
    except OSError:
        return []
    for g in groups.values():
        g["wr_pct"] = round(g["won"] / g["closed"] * 100, 1) if g["closed"] else None
        g["net_pnl"] = round(g["net_pnl"], 2)
        g["open_notional"] = round(g["open_notional"], 2)
    return sorted(groups.values(), key=lambda x: -x["net_pnl"])


def _strategy_pnl_stats(jsonl_path, label: str,
                        include_live: bool = False,
                        live_only: bool = False) -> dict:
    """Per-strategy paper-trading scorecard: counts, P&L, capital
    deployed (cumulative + peak concurrent), and ROI on each.

    PAPER vs LIVE (2026-06-01 audit fix): skips is_live=true rows by default so
    this scorecard is paper-only (real-money fills share the same jsonl). Pass
    include_live=True for a combined view; api_kalshi_live owns the live cut.

    Why peak concurrent matters: cumulative notional double-counts capital
    that recycles between settlements. Peak shows what you'd actually need
    sitting in the account at one time — the real-money sizing question."""
    import json
    from pathlib import Path
    from datetime import datetime

    s = {"label": label, "total": 0, "open": 0, "closed": 0,
         "won": 0, "lost": 0, "net_pnl": 0.0, "wr_pct": None,
         "total_invested": 0.0, "open_notional": 0.0,
         "peak_capital": 0.0, "avg_notional": 0.0,
         "roi_invested_pct": None, "roi_peak_pct": None}
    p = Path(jsonl_path) if not isinstance(jsonl_path, Path) else jsonl_path
    if not p.exists():
        return s

    trades = []
    try:
        with open(p) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                is_live_row = bool(row.get("is_live"))
                if live_only:
                    if not is_live_row:
                        continue  # live-only view: skip paper rows
                elif not include_live and is_live_row:
                    continue  # paper view: skip real-money fills
                trades.append(row)
    except OSError:
        return s

    # Walk the trade timeline to find peak concurrent capital at risk.
    # Open events add notional, close events subtract. The maximum running
    # sum is the most real-account capital we'd ever have needed.
    events = []
    for t in trades:
        status = t.get("status")
        notional = float(t.get("notional", 0) or 0)
        s["total"] += 1
        if status == "open":
            s["open"] += 1
            s["open_notional"] += notional
            try:
                o = datetime.fromisoformat(t["opened_at"].replace("Z", "+00:00"))
                events.append((o, +notional))
            except (KeyError, ValueError):
                pass
        # 15-min strategies use won_early (early take-profit) + cut_loss
        # (stop-loss hit) as additional closed states. Treat them as
        # won / lost respectively so the per-section P&L reflects ALL
        # realized outcomes — without this, the 15-min headline drifts
        # from the per-asset detail.
        elif status in ("won", "lost", "won_early", "cut_loss"):
            s["closed"] += 1
            s["total_invested"] += notional
            if status in ("won", "won_early"):
                s["won"] += 1
            else:
                s["lost"] += 1
            s["net_pnl"] += float(t.get("paper_pnl", 0) or 0)
            try:
                o = datetime.fromisoformat(t["opened_at"].replace("Z", "+00:00"))
                events.append((o, +notional))
                r = t.get("resolved_at")
                if r:
                    rd = datetime.fromisoformat(r.replace("Z", "+00:00"))
                    events.append((rd, -notional))
            except (KeyError, ValueError):
                pass

    events.sort()
    running = peak = 0.0
    for _, delta in events:
        running += delta
        if running > peak:
            peak = running

    if s["closed"]:
        s["wr_pct"] = round(s["won"] / s["closed"] * 100, 1)
        s["avg_notional"] = round(s["total_invested"] / s["closed"], 2)
    if s["total_invested"]:
        s["roi_invested_pct"] = round(s["net_pnl"] / s["total_invested"] * 100, 1)
    if peak:
        s["roi_peak_pct"] = round(s["net_pnl"] / peak * 100, 1)

    s["net_pnl"] = round(s["net_pnl"], 2)
    s["total_invested"] = round(s["total_invested"], 2)
    s["open_notional"] = round(s["open_notional"], 2)
    s["peak_capital"] = round(peak, 2)
    return s


@app.route("/api/paper_vs_live")
def api_paper_vs_live():
    """Reality-gap panel. Paper books EVERY qualifying at-bat at hypothetical
    bankroll sizing assuming fills; live takes only the budget/gate-eligible
    subset at real sizing with real (often partial/no) fills. The capture rate
    (live closed ÷ paper closed) makes the selection+fill gap visible so a big
    paper P&L isn't misread as a live forecast (see #174 parity write-up)."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    sleeves = [
        ("BTC daily",      root / "data" / "kalshi_daily_paper.jsonl"),
        ("Weather hourly", root / "data" / "weather_paper.jsonl"),
        ("Weather daily",  root / "data" / "weather_daily_paper.jsonl"),
    ]
    out = []
    for label, p in sleeves:
        paper = _strategy_pnl_stats(p, label)                  # paper-only cut
        live = _strategy_pnl_stats(p, label, live_only=True)   # live-only cut
        pc, lc = paper["closed"], live["closed"]
        out.append({
            "label": label,
            "paper": {"closed": pc, "wr_pct": paper["wr_pct"],
                      "net_pnl": paper["net_pnl"], "avg_notional": paper["avg_notional"]},
            "live": {"closed": lc, "wr_pct": live["wr_pct"],
                     "net_pnl": live["net_pnl"], "avg_notional": live["avg_notional"]},
            # what fraction of paper's at-bats live actually took
            "capture_pct": (round(lc / pc * 100, 1) if pc else None),
        })
    return jsonify({
        "sleeves": out,
        "note": ("Paper books every qualifying at-bat at hypothetical sizing "
                 "assuming fills; live takes only the gate/budget-eligible subset "
                 "at real sizing with real fills. Low capture% means paper P&L "
                 "overstates what live would make — read paper as signal quality, "
                 "not a dollar forecast."),
    })


@app.route("/api/kalshi_paper_summary")
def api_kalshi_paper_summary():
    """Combined paper-trading P&L across all three Kalshi strategies
    (weather / KXBTCD daily / KX 15-min). Includes capital deployed +
    peak concurrent risk + ROI so each section can render a self-contained
    'how am I doing here' tile."""
    from pathlib import Path
    from datetime import datetime, timezone
    root = Path(__file__).resolve().parent.parent

    # 2026-05-28: KX 15-min removed — strategy was killed and the
    # historical -$45.60 was dragging down the rollup. Keep only the
    # strategies currently producing trades.
    strategies = [
        _strategy_pnl_stats(root / "data" / "weather_paper.jsonl",       "Weather"),
        _strategy_pnl_stats(root / "data" / "kalshi_daily_paper.jsonl",  "KXBTCD Daily"),
    ]
    totals = {
        "total":          sum(s["total"]          for s in strategies),
        "open":           sum(s["open"]           for s in strategies),
        "closed":         sum(s["closed"]         for s in strategies),
        "won":            sum(s["won"]            for s in strategies),
        "lost":           sum(s["lost"]           for s in strategies),
        "net_pnl":        round(sum(s["net_pnl"]        for s in strategies), 2),
        "total_invested": round(sum(s["total_invested"] for s in strategies), 2),
        "peak_capital":   round(sum(s["peak_capital"]   for s in strategies), 2),
    }
    totals["wr_pct"] = (round(totals["won"] / totals["closed"] * 100, 1)
                       if totals["closed"] else None)
    totals["roi_invested_pct"] = (round(totals["net_pnl"] / totals["total_invested"] * 100, 1)
                                  if totals["total_invested"] else None)
    totals["roi_peak_pct"] = (round(totals["net_pnl"] / totals["peak_capital"] * 100, 1)
                              if totals["peak_capital"] else None)

    # Paper bankroll: the same operator-set account size the paper sleeves now
    # size off ($233), read live from config so it tracks the real account.
    # Portfolio value = bankroll + realized paper P&L, so the tile shows the
    # $233 base and how paper trading has moved it.
    paper_bankroll = 233.0
    try:
        import yaml
        cfg = yaml.safe_load((root / "config" / "settings.yaml").read_text()) or {}
        fb = (cfg.get("kalshi_daily_live", {}) or {}).get("account_balance_fallback")
        if fb is not None:
            paper_bankroll = float(fb)
    except Exception:
        pass
    totals["bankroll"] = round(paper_bankroll, 2)
    totals["portfolio_value"] = round(paper_bankroll + totals["net_pnl"], 2)
    return jsonify({
        "as_of": datetime.now(timezone.utc).isoformat(),
        "strategies": strategies,
        "totals": totals,
    })


@app.route("/api/kalshi_live")
def api_kalshi_live():
    """LIVE TRADING dashboard endpoint. Returns real-time state of the
    live Kalshi executor: actual broker balance, open positions per
    Kalshi truth, today's realized PnL, kill-switch state, every
    safety-gate status, and a recent-orders feed from the audit log.

    This is the canonical 'is the bot losing my real money right now'
    view. Updates every cycle the dashboard polls."""
    import json
    from pathlib import Path
    from datetime import datetime, timezone
    root = Path(__file__).resolve().parent.parent

    out: dict = {"as_of": datetime.now(timezone.utc).isoformat()}

    # Pull current safety status (config + gate evaluation + balance)
    try:
        from lib.kalshi_live_executor import (
            get_current_safety_status, is_live_enabled, _load_state,
            SMOKE_MARKER_PATH,
        )
        safety = get_current_safety_status()
        out["safety"] = safety
        out["is_live_enabled"] = is_live_enabled()
        out["smoke_marker_present"] = SMOKE_MARKER_PATH.exists()
        live_state = _load_state()
        out["state"] = {
            "consecutive_losses":     int(live_state.get("consecutive_losses", 0)),
            "kill_switch_tripped":    bool(live_state.get("kill_switch_tripped", False)),
            "kill_switch_tripped_at": live_state.get("kill_switch_tripped_at", ""),
            "recent_losses_count":    len(live_state.get("recent_losses", [])),
        }
    except Exception as e:
        out["safety"] = {"error": str(e)[:200]}
        out["is_live_enabled"] = False
        out["smoke_marker_present"] = False
        out["state"] = {}

    # Pull real positions from Kalshi (broker truth, not local log)
    try:
        from lib.kalshi_client import KalshiClient
        c = KalshiClient()
        out["live_positions"] = [
            {"market_id": p.market_id, "side": p.side, "quantity": p.quantity,
             "avg_price": p.avg_price, "unrealized_pnl": p.unrealized_pnl}
            for p in (c.get_positions() or [])
        ]
    except Exception as e:
        out["live_positions"] = []
        out["live_positions_error"] = str(e)[:200]

    # Recent live-trade events from the audit log (last 20)
    audit_log = root / "logs" / "audit_log.jsonl"
    recent_events = []
    if audit_log.exists():
        try:
            with open(audit_log) as f:
                lines = f.readlines()[-2000:]   # cap how far back we scan
            for line in lines:
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                etype = ev.get("event_type", "")
                action = ev.get("action", "")
                if etype == "kalshi_live" or (
                    etype == "market_client" and "kalshi" in action
                ):
                    recent_events.append({
                        "timestamp": ev.get("timestamp", ""),
                        "action": action,
                        "result": ev.get("result", ""),
                        "details": ev.get("details", {}),
                    })
        except OSError:
            pass
    out["recent_events"] = recent_events[-20:]

    # Local live trades from BOTH paper logs — BTC (kalshi_daily_paper)
    # AND weather (weather_paper). Previously only BTC was read, so weather
    # live trades were invisible to the dashboard's Live Trade Summary.
    # 2026-05-26 PM: unified across both surfaces.
    paper_logs = [
        ("btc",     root / "data" / "kalshi_daily_paper.jsonl"),
        ("weather", root / "data" / "weather_paper.jsonl"),
    ]
    local_live = []
    for asset_kind, paper_log in paper_logs:
        if not paper_log.exists():
            continue
        try:
            with open(paper_log) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        t = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if t.get("is_live"):
                        local_live.append({
                            "market_ticker": t.get("market_ticker"),
                            "side": t.get("side"),
                            "live_order_id": t.get("live_order_id"),
                            "live_contracts": t.get("live_contracts"),
                            "live_notional_usd": t.get("live_notional_usd"),
                            "status": t.get("status"),
                            "paper_pnl": t.get("paper_pnl", 0),
                            "opened_at": t.get("opened_at"),
                            "resolved_at": t.get("resolved_at", ""),
                            "fill_price":    t.get("fill_price"),
                            # BTC uses 'strike' (USD), weather uses 'strike_f' (°F).
                            # Normalize so the dashboard can render either side.
                            "strike":        t.get("strike") or t.get("strike_f"),
                            "spot_at_entry": t.get("spot_at_entry"),
                            "p_win_estimated": t.get("p_win_estimated") or t.get("nws_p_yes"),
                            "close_time":    t.get("close_time"),
                            # Tag asset kind for the per-asset breakdown
                            "asset":         (t.get("asset")
                                              or ("weather" if asset_kind == "weather"
                                                  else "btc")),
                        })
        except OSError:
            pass
    # Sort chronologically — most recent first so the last-20 shows current
    local_live.sort(key=lambda x: x.get("opened_at", ""), reverse=False)
    out["local_live_trades"] = local_live[-20:]
    # Status taxonomy:
    #   open       → still in book
    #   won        → settled at close in our favor
    #   won_early  → take-profit fired and verified (locked-in gain)
    #   lost       → settled against us at close (or close_position void)
    # The summary must treat won + won_early as wins, and include both
    # in net_pnl. Previously won_early was excluded → PnL undercounted
    # whenever a take-profit exit fired.
    _settled = ("won", "won_early", "lost")
    out["local_live_summary"] = {
        "total": len(local_live),
        "open":  sum(1 for t in local_live if t["status"] == "open"),
        "won":   sum(1 for t in local_live if t["status"] in ("won", "won_early")),
        "won_early": sum(1 for t in local_live if t["status"] == "won_early"),
        "lost":  sum(1 for t in local_live if t["status"] == "lost"),
        "net_pnl": round(
            sum(float(t.get("paper_pnl", 0) or 0) for t in local_live
                if t["status"] in _settled), 2
        ),
    }

    # Latest signal-scan samples FILTERED to live-eligible assets only.
    # Shows the user exactly what the bot is looking at for live trade
    # decisions (vs the Kalshi Daily Markets section which shows all).
    signal_path = root / "data" / "kalshi_daily_signal.jsonl"
    live_scan: list = []
    try:
        # Reuse the allowlist set for the asset gate so the filter stays
        # in sync with what's actually trade-eligible.
        allowed_assets = set()
        try:
            from lib.kalshi_live_executor import _load_live_config
            allowed_assets = set(_load_live_config().get("live_assets") or [])
        except Exception:
            pass

        recent: list = []
        if signal_path.exists():
            with open(signal_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        recent.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        if recent:
            latest_at = recent[-1].get("sample_at")
            latest_samples = [s for s in recent if s.get("sample_at") == latest_at]
            for s in latest_samples:
                asset = str(s.get("asset", "")).lower()
                if allowed_assets and asset not in allowed_assets:
                    continue
                ind = s.get("indicators") or {}
                # Side-aware price edge — surfaces "high composite but no
                # actionable edge" cases that downstream gates would catch
                # silently. composite > 0 → bot picks YES; composite < 0 → NO.
                # Edge = our model's P(side wins) − market's P(side wins).
                # On a YES bet: theo_yes − yes_ask. On a NO: (1−theo_yes) − no_ask.
                comp = ind.get("composite")
                theo = ind.get("theoretical_yes")
                ya = s.get("yes_ask")
                na = s.get("no_ask")
                side = None
                edge = None
                if comp is not None and theo is not None:
                    if comp >= 0 and ya is not None:
                        side = "YES"
                        edge = float(theo) - float(ya)
                    elif comp < 0 and na is not None:
                        side = "NO"
                        edge = (1.0 - float(theo)) - float(na)
                live_scan.append({
                    "asset":            asset.upper(),
                    "seconds_to_close": s.get("seconds_to_close"),
                    "strike":           s.get("strike"),
                    "spot_usd":         s.get("spot_usd"),
                    "distance_to_spot_pct": s.get("distance_to_spot_pct"),
                    "yes_ask":          ya,
                    "no_ask":           na,
                    "theoretical_yes":  theo,
                    "composite":        comp,
                    "confidence":       ind.get("confidence"),
                    "side":             side,
                    "edge":             round(edge, 4) if edge is not None else None,
                    "market_ticker":    s.get("market_ticker", ""),
                })
            # Sort by absolute |composite| descending so strongest signals top
            live_scan.sort(key=lambda x: -abs(x.get("composite") or 0))
    except OSError:
        pass
    out["live_scan"] = live_scan[:15]   # cap to 15 rows
    return jsonify(out)


# ── Kill-switch reset (TOKEN-PROTECTED, money-affecting control) ──────────────
# This is the ONLY write endpoint on the dashboard and the only one that can
# re-arm real-money trading (clears kill_switch_tripped + resets the loss
# counter). The dashboard binds to 0.0.0.0 (LAN/phone reachable) and is
# otherwise unauthenticated, so this endpoint REQUIRES a shared secret token
# from the environment (KILL_SWITCH_RESET_TOKEN, sourced from .env by the
# launchd run script). Without that env var set, the endpoint is hard-disabled
# (503) — fail-closed. The token is compared with hmac.compare_digest to avoid
# timing leaks and is never echoed back in any response or rendered in the page.
def _reset_token_ok() -> tuple[bool, str]:
    expected = os.environ.get("KILL_SWITCH_RESET_TOKEN", "")
    if not expected:
        return False, "reset endpoint disabled: KILL_SWITCH_RESET_TOKEN not set"
    supplied = ""
    if request.is_json:
        body = request.get_json(silent=True) or {}
        supplied = str(body.get("token", ""))
    if not supplied:
        supplied = request.headers.get("X-Reset-Token", "")
    if supplied and hmac.compare_digest(supplied, expected):
        return True, ""
    return False, "invalid or missing reset token"


@app.route("/api/kalshi_live/reset_kill_switch", methods=["POST"])
def api_reset_kill_switch():
    """Clear the live kill switch + reset the consecutive-loss counter so the
    bot resumes trading next cycle. Requires the KILL_SWITCH_RESET_TOKEN secret
    (JSON body {"token": "..."} or X-Reset-Token header). Returns the live
    state before/after so the UI can confirm it actually cleared."""
    ok, reason = _reset_token_ok()
    if not ok:
        # 503 when the token isn't configured at all; 403 when it's just wrong.
        code = 503 if "disabled" in reason else 403
        return jsonify({"ok": False, "error": reason}), code
    try:
        from lib.kalshi_live_executor import reset_kill_switch, _load_state
        before = _load_state()
        was_tripped = bool(before.get("kill_switch_tripped", False))
        prior_consec = int(before.get("consecutive_losses", 0))
        reset_kill_switch()
        after = _load_state()
        return jsonify({
            "ok": True,
            "was_tripped": was_tripped,
            "prior_consecutive_losses": prior_consec,
            "kill_switch_tripped": bool(after.get("kill_switch_tripped", False)),
            "consecutive_losses": int(after.get("consecutive_losses", 0)),
            "note": ("kill switch cleared; trading resumes next cycle"
                     if was_tripped else
                     "kill switch was not tripped; counter reset to 0"),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


@app.route("/api/calibration")
def api_calibration():
    return jsonify(get_calibration_data())


@app.route("/api/events")
def api_events():
    return jsonify(get_events(30))


@app.route("/api/history")
def api_history():
    return jsonify(get_trade_history())


@app.route("/api/performance")
def api_performance():
    return jsonify(get_performance_summary())


@app.route("/api/breakers")
def api_breakers():
    return jsonify(get_circuit_breaker_status())


def run_dashboard(port: int = 5050):
    """Start the dashboard web server.

    Bind host is controlled by POLYBOT_DASHBOARD_HOST env var:
      • unset / "127.0.0.1" → localhost only (default for paper-only era)
      • "0.0.0.0"           → LAN-accessible (phone on same WiFi can hit
                              http://<mac-lan-ip>:<port>)

    2026-05-24: Default flipped to 0.0.0.0 so the live-trading dashboard
    is reachable from the user's phone while away from the desk. This
    exposes the dashboard to anyone on the same WiFi — fine on a home
    network, NOT safe on a public hotspot. The dashboard is read-only
    (no order-placement controls) and contains no API keys or secrets,
    but does show real $ balance + positions. To force localhost-only
    again, set POLYBOT_DASHBOARD_HOST=127.0.0.1 in the launchd plist or
    user env.
    """
    import errno
    import os
    import socket

    host = os.environ.get("POLYBOT_DASHBOARD_HOST", "0.0.0.0")

    # Pre-flight check: confirm the port is free before Flask starts, so we
    # can emit an actionable error instead of a Werkzeug stack trace.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    try:
        probe.bind((host, port))
    except OSError as exc:
        if exc.errno in (errno.EADDRINUSE, errno.EACCES):
            print(f"ERROR: Port {port} is already in use on {host}.")
            print(f"       Another dashboard may already be running.")
            print(f"       Check with:  lsof -i :{port}")
            print(f"       Or pick a different port:  python main.py dashboard --port <N>")
            raise SystemExit(2)
        raise
    finally:
        probe.close()

    # Print both the localhost URL and the LAN URL so the user can pick
    # the right one from desk vs phone.
    print(f"Polybot Dashboard: http://localhost:{port}")
    if host == "0.0.0.0":
        # Find the LAN IP for a friendlier message (best-effort)
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))   # doesn't actually send
                lan_ip = s.getsockname()[0]
            print(f"Polybot Dashboard (LAN): http://{lan_ip}:{port}  ← phone-accessible")
        except Exception:
            pass
    app.run(host=host, port=port, debug=False, threaded=True)
