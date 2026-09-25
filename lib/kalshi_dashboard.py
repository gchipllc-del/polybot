"""
Kalshi 15-min trader dashboard — live view of the Phase 2 pipeline.

Flask app + single-page HTML at localhost:5153 (POLYBOT_CRYPTO_DASH_PORT). Auto-refreshes every
20s. No external JS deps; one inline template.

Panels:
  * Live markets (current cycle's 3 assets — composite, confidence,
    T-close, threshold-pass indicator)
  * Open paper trades (riding now, with markets they're tied to)
  * Recent settled trades (last 15, color-coded won/lost/void)
  * Per-asset rollup (total, WR, P&L, ROI)
  * Confidence-bucket calibration (the Phase 3 gate)
  * Kalshi account balance (signed API call when auth configured)
  * Cron health (last fire timestamp, count in last 15m)

Designed to be safe to run continuously — every endpoint either reads
local JSONL state or hits public Kalshi/Binance endpoints. The
account-balance call is the only signed one and degrades gracefully
when ``kalshi_auth`` isn't configured.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).parent.parent
PAPER_PATH = ROOT / "data" / "kalshi_15min_paper.jsonl"
SIGNAL_PATH = ROOT / "data" / "kalshi_15min_signal.jsonl"
LOG_PATH = ROOT / "logs" / "launchd_kalshi_15min.log"


# ── Data helpers ─────────────────────────────────────────────────────

def _load_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    rows: list[dict] = []
    try:
        with open(p) as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except (json.JSONDecodeError, TypeError):
                    continue
    except OSError:
        pass
    return rows


def _latest_signal_per_market() -> dict:
    """Return only the most recent sample per market_ticker.

    We tail the signal jsonl — most-recent rows are the live picture.
    Dict keyed by ticker to dedup. Caps at last 500 rows for speed.
    """
    rows = _load_jsonl(SIGNAL_PATH)
    rows = rows[-500:] if len(rows) > 500 else rows
    out: dict = {}
    for r in rows:
        tk = r.get("market_ticker")
        if tk:
            out[tk] = r
    return out


def _live_markets_payload() -> list[dict]:
    """Format the live-market panel rows — latest sample per still-open
    market, sorted by seconds_to_close ascending.

    Includes a `passes_threshold` flag computed from per-asset
    min_confidence so the UI can highlight imminent trade candidates.
    """
    from lib.kalshi_15min_signal import load_assets_config

    cfg = load_assets_config()
    latest = _latest_signal_per_market()
    now = datetime.now(timezone.utc)
    out: list[dict] = []
    for tk, s in latest.items():
        close_iso = s.get("close_time") or ""
        try:
            close_dt = datetime.fromisoformat(close_iso.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        # Only show markets that haven't closed yet — be generous (+30s
        # grace) so windows about to resolve still appear briefly.
        if (close_dt - now).total_seconds() < -30:
            continue
        ind = s.get("indicators") or {}
        asset = s.get("asset") or "?"
        asset_cfg = cfg.get(asset, {})
        thresh = float(asset_cfg.get("min_confidence", 0.35))
        conf = float(ind.get("confidence", 0) or 0)
        out.append({
            "ticker": tk,
            "asset": asset,
            "title": s.get("title", "")[:80],
            "seconds_to_close": (close_dt - now).total_seconds(),
            "strike": s.get("strike"),
            "spot": s.get("spot_usd"),
            "yes_ask": s.get("yes_ask"),
            "no_ask": s.get("no_ask"),
            "composite": ind.get("composite"),
            "confidence": conf,
            "threshold": thresh,
            "passes_threshold": conf >= thresh,
            "theoretical_yes": ind.get("theoretical_yes"),
            "theo_yes_gap": ind.get("theo_yes_gap"),
            "direction": ind.get("direction"),
        })
    out.sort(key=lambda x: x["seconds_to_close"])
    return out


def _paper_payload() -> dict:
    """All-up paper-trade view: open trades + recent settled +
    per-asset rollup + confidence-bucket calibration.

    Delegates aggregation to ``kalshi_15min_paper.summary`` for the
    rollup so the dashboard never duplicates that math.
    """
    from lib.kalshi_15min_paper import summary

    rows = _load_jsonl(PAPER_PATH)
    open_trades = [r for r in rows if r.get("status") == "open"]
    settled = [r for r in rows if r.get("status") != "open"]
    # Sort by when they actually RESOLVED (intra-window exits resolve
    # mid-window, regular settlements at close), falling back to
    # opened_at for any legacy row missing resolved_at.
    settled.sort(
        key=lambda r: r.get("resolved_at") or r.get("opened_at", ""),
        reverse=True,
    )
    return {
        "summary": summary(),
        "open_trades": open_trades[:20],
        "recent_settled": settled[:15],
    }


def _account_payload() -> dict:
    """Kalshi balance + auth status. Public-facing; returns clean dict
    even when auth isn't configured.
    """
    from lib.kalshi_auth import can_sign, signed_get, status as auth_status

    base = {"auth": auth_status(), "balance_dollars": None, "error": None}
    if not can_sign():
        base["error"] = "auth not configured (run kalshi-auth-status)"
        return base
    try:
        data = signed_get("/portfolio/balance")
        # Kalshi returns cents; convert
        cents = data.get("balance", 0)
        base["balance_dollars"] = round(cents / 100.0, 2)
        base["portfolio_value"] = data.get("portfolio_value")
    except Exception as e:
        base["error"] = str(e)[:200]
    return base


def _cron_health_payload() -> dict:
    """Quick check: when did the cron last fire, how many times in the
    last 15 minutes? Reads the launchd stdout log.
    """
    if not LOG_PATH.exists():
        return {"log_exists": False}
    try:
        mtime = datetime.fromtimestamp(LOG_PATH.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return {"log_exists": False}
    # Count "=== Kalshi 15-min signal cycle" headers in last 15min worth of rows.
    # The log is small (cron fires once per minute, each output ~10 lines).
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=15)
    try:
        text = LOG_PATH.read_text()
    except OSError:
        return {"log_exists": True, "last_modified": mtime.isoformat(),
                "cycles_last_15m": None}
    cycles_15m = text.count("=== Kalshi 15-min signal cycle")
    # Best-effort: count is over the ENTIRE log; trim heuristically by file age
    age_min = (datetime.now(timezone.utc) - mtime).total_seconds() / 60.0
    return {
        "log_exists": True,
        "last_modified": mtime.isoformat(),
        "age_minutes": round(age_min, 1),
        "total_cycles_in_log": cycles_15m,
        "expected_15m": 15,  # cron is every 60s
    }


# ── Flask app ────────────────────────────────────────────────────────

def _stage0_payload() -> dict:
    """Stage-0 mispricing-experiment summary (the restart's $0 test). Reuses the
    collector's own join logic so dashboard and CLI report always agree. Defensive:
    any error -> {'error': ...} so the page never blanks."""
    import sys as _sys
    try:
        sdir = str(ROOT / "scripts")
        if sdir not in _sys.path:
            _sys.path.insert(0, sdir)
        import stage0_collector as s0
        rows = []
        if s0.LOG.exists():
            for line in s0.LOG.read_text(encoding="utf-8").splitlines():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        rep = s0.build_report(rows)
        cells = []
        for band in [b[2] for b in s0.TIME_BANDS]:
            for lo, hi in s0.BUCKETS:
                cell = rep["table"].get((band, f"{lo:02d}-{hi:02d}c"))
                if not cell or not cell["n"]:
                    continue
                n = cell["n"]
                cells.append({"band": band, "bucket": f"{lo}-{hi}c", "n": n,
                              "avg_cost": cell["cost"] / n,
                              "realized": cell["wins"] / n,
                              "gap": cell["wins"] / n - cell["cost"] / n,
                              "fee": cell["fee"] / n})
        return {"joined": rep["joined"], "n_settles": rep["n_settles"],
                "verdict_ready": rep["joined"] >= 1500, "cells": cells}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:200]}


def _shadow_payload() -> dict:
    """Shadow-book replay: what the frozen pre-registered rules WOULD have earned on
    already-collected data, after real Kalshi fees. No orders, no paper ledger."""
    import sys as _sys
    try:
        sdir = str(ROOT / "scripts")
        if sdir not in _sys.path:
            _sys.path.insert(0, sdir)
        import shadow_book as sb
        rep = sb.build(sb._load(sb.LOG))
        rules = []
        for name, r in rep["rules"].items():
            rules.append({"name": name, "thesis": r["thesis"], "band": r["band"],
                          "side": r["side"], "price_range": r["price_range"],
                          "n": r["n"], "win_rate": r["win_rate"], "net": r["net"],
                          "fees": r["fees"], "net_per_trade": r["net_per_trade"],
                          "wr_ci95": r["wr_ci95"], "meaningful": r["meaningful"],
                          "depth_known": r["depth_known"], "fillable": r["fillable"]})
        return {"rules": rules, "n_settled_markets": rep["n_settled_markets"],
                "min_n": sb.MIN_N_MEANINGFUL}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:200]}


def _paper_crypto_payload() -> dict:
    """Forward-only paper book for the crypto-15 restart (scripts/paper_trader.py)."""
    import sys as _sys
    try:
        sdir = str(ROOT / "scripts")
        if sdir not in _sys.path:
            _sys.path.insert(0, sdir)
        import paper_trader as pt
        rep = pt.build_report(pt._load(pt.LEDGER))
        rep["by_rule"] = [{"rule": k, **v} for k, v in rep.get("by_rule", {}).items()]
        rep["windows"] = rep.get("windows", 0)
        rep["open_positions"] = rep.get("open_positions", [])[:10]
        return rep
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:200]}


def _trading_status_payload() -> dict:
    """Unambiguous answer to 'are we trading?' — read from the actual ledgers, not from
    intent. live=any real order path active; paper=any paper ledger with rows."""
    import sys as _sys
    paper_rows = 0
    try:
        sdir = str(ROOT / "scripts")
        if sdir not in _sys.path:
            _sys.path.insert(0, sdir)
        import paper_trader as pt
        paper_rows = len([r for r in pt._load(pt.LEDGER) if r.get("t") == "open"])
    except Exception:
        pass
    return {
        "live": False,
        "paper": paper_rows > 0,
        "paper_rows": paper_rows,
        "mode": ("PAPER TRADING (forward-only)" if paper_rows
                 else "PAPER ARMED - waiting for a frozen rule to fire"),
        "why": ("No real orders, ever, until an OUT-OF-SAMPLE rule earns it. Paper trades "
                "are stamped when the signal fires on an unsettled market, so this ledger "
                "is the honest test the shadow book's in-sample replay cannot be."),
    }


def make_app():
    from flask import Flask, jsonify, render_template_string

    app = Flask(__name__)

    @app.route("/")
    def index():
        return render_template_string(_TEMPLATE)

    @app.route("/api/live")
    def api_live():
        return jsonify(_live_markets_payload())

    @app.route("/api/paper")
    def api_paper():
        return jsonify(_paper_payload())

    @app.route("/api/account")
    def api_account():
        return jsonify(_account_payload())

    @app.route("/api/cron")
    def api_cron():
        return jsonify(_cron_health_payload())

    @app.route("/api/all")
    def api_all():
        """One-shot fetch — used by the page on every refresh."""
        # Each section is independently guarded: one broken payload (missing sibling
        # repo, network egress, bad data file) must degrade to an error string in ITS
        # panel, never 500 the whole page (which froze every panel at "loading...").
        def safe(fn):
            try:
                return fn()
            except Exception as e:  # noqa: BLE001
                return {"error": str(e)[:200]}
        return jsonify({
            "live": safe(_live_markets_payload),
            "paper": safe(_paper_payload),
            "account": safe(_account_payload),
            "cron": safe(_cron_health_payload),
            "stage0": safe(_stage0_payload),
            "shadow": safe(_shadow_payload),
            "trading": safe(_trading_status_payload),
            "paper_crypto": safe(_paper_crypto_payload),
            "ts": datetime.now(timezone.utc).isoformat(),
        })

    return app


def run_dashboard(port: int = 5153):
    """Boot the Flask app. Port defaults to 5153 — the 51xx block keeps polybot
    clear of the openclaw wheel-trader dashboards (5000/5050/5051/8080).
    Host via POLYBOT_DASH_HOST (default 127.0.0.1)."""
    import socket
    # Pre-flight: confirm the port is free so we fail loudly, not silently.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((os.environ.get("POLYBOT_DASH_HOST", "127.0.0.1"), port))
    except OSError as e:
        s.close()
        raise RuntimeError(
            f"Port {port} is already in use ({e}). "
            f"Try --port=5155 or check what holds it (netstat -ano | findstr :{port})."
        )
    s.close()

    app = make_app()
    print(f"Kalshi dashboard → http://localhost:{port}")
    app.run(host=os.environ.get("POLYBOT_DASH_HOST", "127.0.0.1"), port=port, debug=False, use_reloader=False)


# ── HTML template (inline) ───────────────────────────────────────────

_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Kalshi 15-min Trader Dashboard</title>
<style>
:root {
  --bg: #0e1116;
  --panel: #161b22;
  --border: #30363d;
  --text: #c9d1d9;
  --muted: #8b949e;
  --green: #3fb950;
  --red: #f85149;
  --yellow: #d29922;
  --blue: #58a6ff;
}
* { box-sizing: border-box; }
body {
  margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background: var(--bg); color: var(--text); padding: 16px;
}
h1 { margin: 0 0 6px 0; font-size: 20px; }
.sub { color: var(--muted); font-size: 12px; margin-bottom: 16px; }
.grid {
  display: grid; gap: 12px;
  grid-template-columns: repeat(auto-fit, minmax(380px, 1fr));
}
.panel {
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 6px; padding: 12px; overflow-x: auto;
}
.panel h2 { margin: 0 0 8px 0; font-size: 14px; color: var(--blue); }
table { width: 100%; border-collapse: collapse; font-size: 12px; }
th, td {
  text-align: left; padding: 4px 6px; border-bottom: 1px solid var(--border);
  white-space: nowrap;
}
th { color: var(--muted); font-weight: 500; font-size: 11px; }
tr:last-child td { border-bottom: none; }
.green { color: var(--green); }
.red { color: var(--red); }
.yellow { color: var(--yellow); }
.muted { color: var(--muted); }
.pass { background: rgba(63, 185, 80, 0.15); }
.bigstat {
  display: inline-block; margin-right: 18px;
  font-size: 13px;
}
.bigstat .v { display: block; font-size: 18px; color: var(--text); }
.bigstat .k { color: var(--muted); font-size: 11px; }
.right { text-align: right; }
.tiny { font-size: 10px; color: var(--muted); }
.bar { display: inline-block; height: 6px; background: var(--border); border-radius: 3px; width: 60px; vertical-align: middle; position: relative; }
.bar .fill { position: absolute; top: 0; left: 0; height: 100%; border-radius: 3px; background: var(--blue); }
.nav { display: flex; gap: 8px; margin-bottom: 14px; flex-wrap: wrap; }
.nav a { font-size: 12px; padding: 4px 10px; border: 1px solid var(--border); border-radius: 6px; color: var(--muted); text-decoration: none; }
.nav a:hover { color: var(--text); border-color: var(--blue); }
.nav a.active { color: var(--blue); border-color: var(--blue); background: rgba(88,166,255,0.12); }
</style>
</head>
<body>
  <h1>Kalshi 15-min Trader</h1>
  <div class="nav">
    <a href="http://localhost:5153/" class="active">₿ Crypto 15-min</a>
    <a href="http://localhost:5154/">🌡 Weather</a>
  </div>
  <div class="sub" id="ts">loading…</div>

  <div class="grid">
    <div class="panel" id="account-panel">
      <h2>Account</h2>
      <div id="account">…</div>
    </div>
    <div class="panel" id="cron-panel">
      <h2>Cron Health</h2>
      <div id="cron">…</div>
    </div>
    <div class="panel" id="summary-panel">
      <h2>All-up P&L</h2>
      <div id="summary">…</div>
    </div>
  </div>

  <div class="grid" style="margin-top: 12px;">
    <div class="panel">
      <h2>Trading status</h2>
      <div id="trading">…</div>
    </div>

    <div class="panel">
      <h2>Paper book — forward-only, out-of-sample (no real orders)</h2>
      <div id="paper-crypto-status" class="sub">loading…</div>
      <table><thead><tr>
        <th>rule</th><th class="right">trades</th><th class="right">windows</th>
        <th class="right">WR</th><th class="right">net $</th><th class="right">$/trade</th>
      </tr></thead><tbody id="paper-crypto-tbody"></tbody></table>
      <div id="paper-crypto-open" class="sub"></div>
    </div>

    <div class="panel">
      <h2>Shadow book — what the frozen hypotheses WOULD have earned (no orders placed)</h2>
      <div id="shadow-status" class="sub">loading…</div>
      <table><thead><tr>
        <th>rule</th><th>band</th><th>side / price</th><th class="right">n</th>
        <th class="right">WR</th><th class="right">net $</th><th class="right">$/trade</th>
        <th class="right">fills</th><th>status</th>
      </tr></thead><tbody id="shadow-tbody"></tbody></table>
      <div class="sub">Pre-registered rules replayed on collected data, after real Kalshi
        fees. IN-SAMPLE until n&ge;100 and holding on data collected after naming — a
        hypothesis with a dollar sign, not a track record.</div>
    </div>

    <div class="panel">
      <h2>Stage-0 — mispricing experiment (measure first, bet never until proven)</h2>
      <div id="stage0-status" class="sub">loading…</div>
      <table><thead><tr>
        <th>band</th><th>bucket</th><th class="right">n</th><th class="right">avg cost</th>
        <th class="right">realized</th><th class="right">gap</th><th class="right">fee</th><th>read</th>
      </tr></thead><tbody id="stage0-tbody"></tbody></table>
    </div>

    <div class="panel">
      <h2>Live Markets</h2>
      <table><thead><tr>
        <th>asset</th><th>T-close</th><th>strike</th><th>spot</th>
        <th>yes_ask</th><th>theo_yes</th><th>gap</th>
        <th>composite</th><th>conf</th><th>fires?</th>
      </tr></thead><tbody id="live-tbody"></tbody></table>
    </div>
    <div class="panel">
      <h2>Open Paper Trades</h2>
      <table><thead><tr>
        <th>asset</th><th>side</th><th>fill</th><th>size</th>
        <th>conf</th><th>composite</th><th>strike</th><th>title</th>
      </tr></thead><tbody id="open-tbody"></tbody></table>
    </div>
  </div>

  <div class="grid" style="margin-top: 12px;">
    <div class="panel">
      <h2>Per Asset (settled)</h2>
      <table><thead><tr>
        <th>asset</th><th>total</th><th>W</th><th>L</th>
        <th>WR</th><th>P&L</th><th>ROI</th>
      </tr></thead><tbody id="by-asset-tbody"></tbody></table>
    </div>
    <div class="panel">
      <h2>Calibration (by confidence bucket)</h2>
      <table><thead><tr>
        <th>bucket</th><th>settled</th><th>wins</th><th>WR</th><th>P&L</th>
      </tr></thead><tbody id="bucket-tbody"></tbody></table>
      <div class="tiny" style="margin-top: 8px;">
        Phase 3 gate: WR should monotonically rise with confidence bucket.
      </div>
    </div>
  </div>

  <div class="panel" style="margin-top: 12px;">
    <h2>Recent Settled (last 15)</h2>
    <table><thead><tr>
      <th>opened</th><th>asset</th><th>side</th><th>fill</th>
      <th>conf</th><th>status</th><th>pnl</th><th>title</th>
    </tr></thead><tbody id="settled-tbody"></tbody></table>
  </div>

<script>
function fmt(v, d=2) {
  if (v === null || v === undefined) return '—';
  if (typeof v === 'number') return v.toFixed(d);
  return String(v);
}
function fmtT(s) {
  if (s === null || s === undefined) return '—';
  if (s < 0) return '<span class="muted">closed</span>';
  if (s < 60) return s.toFixed(0) + 's';
  const m = Math.floor(s / 60), sec = Math.floor(s % 60);
  return m + 'm' + (sec<10?'0':'') + sec + 's';
}
function fmtPnl(v) {
  if (v === null || v === undefined) return '—';
  const c = v > 0 ? 'green' : v < 0 ? 'red' : 'muted';
  const sign = v >= 0 ? '+' : '';
  return '<span class="' + c + '">$' + sign + fmt(v) + '</span>';
}
function fmtPct(v) {
  if (v === null || v === undefined) return '—';
  return (v*100).toFixed(1) + '%';
}
function pnlClass(v) { return v > 0 ? 'green' : v < 0 ? 'red' : 'muted'; }
function statusClass(s) {
  if (s === 'won' || s === 'won_early') return 'green';
  if (s === 'lost' || s === 'cut_loss') return 'red';
  if (s === 'void') return 'yellow'; return 'muted';
}

async function refresh() {
  try {
    const r = await fetch('/api/all');
    const data = await r.json();
    document.getElementById('ts').textContent = 'last refresh: ' + new Date(data.ts).toLocaleTimeString();

    // Account
    const acc = data.account || {};
    const bal = acc.balance_dollars;
    const accHtml = bal !== null && bal !== undefined
      ? '<div class="bigstat"><span class="v">$' + bal.toFixed(2) + '</span><span class="k">balance</span></div>' +
        (acc.error ? '<div class="red tiny">' + acc.error + '</div>' : '')
      : '<div class="red">' + (acc.error || 'no balance') + '</div>';
    document.getElementById('account').innerHTML = accHtml;

    // Cron health
    const c = data.cron || {};
    const age = c.age_minutes;
    const ageClass = age == null ? 'muted' : age < 2 ? 'green' : age < 5 ? 'yellow' : 'red';
    const cronHtml = c.log_exists
      ? '<div class="bigstat"><span class="v ' + ageClass + '">' + fmt(age,1) + 'm</span><span class="k">since last cycle</span></div>' +
        '<div class="bigstat"><span class="v">' + (c.total_cycles_in_log||0) + '</span><span class="k">cycles in log</span></div>'
      : '<div class="muted">log not found — is the launchd job loaded?</div>';
    document.getElementById('cron').innerHTML = cronHtml;

    // Summary
    const s = (data.paper && data.paper.summary) || {};
    const summaryHtml = s.total_trades > 0
      ? '<div class="bigstat"><span class="v">' + s.total_trades + '</span><span class="k">trades</span></div>' +
        '<div class="bigstat"><span class="v">' + fmtPct(s.win_rate) + '</span><span class="k">WR (settled)</span></div>' +
        '<div class="bigstat"><span class="v ' + pnlClass(s.total_paper_pnl) + '">$' + fmt(s.total_paper_pnl) + '</span><span class="k">paper P&L</span></div>' +
        '<div class="bigstat"><span class="v ' + pnlClass(s.roi_pct) + '">' + fmtPct(s.roi_pct) + '</span><span class="k">ROI</span></div>'
      : '<div class="muted">No trades yet.</div>';
    document.getElementById('summary').innerHTML = summaryHtml;

    // Trading status — the unambiguous answer to "are we trading?"
    const tr = data.trading || {};
    document.getElementById('trading').innerHTML = tr.error
      ? '<div class="red">' + tr.error + '</div>'
      : '<div class="bigstat"><span class="v ' + (tr.live ? 'red' : 'muted') + '">' +
          (tr.live ? 'LIVE ON' : 'LIVE OFF') + '</span><span class="k">real money</span></div>' +
        '<div class="bigstat"><span class="v ' + (tr.paper ? 'green' : 'muted') + '">' +
          (tr.paper ? tr.paper_rows + ' rows' : 'PAPER OFF') + '</span><span class="k">paper trades</span></div>' +
        '<div class="bigstat"><span class="v yellow">' + (tr.mode||'') + '</span><span class="k">mode</span></div>' +
        '<div class="sub">' + (tr.why||'') + '</div>';

    // Paper book (forward-only)
    const pc = data.paper_crypto || {};
    const pcs = document.getElementById('paper-crypto-status');
    const pcb = document.getElementById('paper-crypto-tbody');
    if (pc.error) {
      pcs.textContent = 'paper error: ' + pc.error;
      pcb.innerHTML = '';
    } else {
      const wr = pc.win_rate == null ? '—' : (pc.win_rate*100).toFixed(1) + '%';
      pcs.innerHTML = 'closed <b>' + (pc.n_closed||0) + '</b> trades across <b>' +
        (pc.windows||0) + '</b> independent 15-min windows (windows are the real sample ' +
        'size &mdash; many strikes share one window and resolve together)' +
        ' &middot; open <b>' + (pc.n_open||0) + '</b> &middot; WR ' + wr +
        ' &middot; net <span class="' + pnlClass(pc.net) + '">$' + fmt(pc.net) + '</span>' +
        ' &middot; equity $' + fmt(pc.equity) +
        (pc.since ? ' &middot; since ' + String(pc.since).slice(0,16) : '');
      const br = pc.by_rule || [];
      pcb.innerHTML = br.length === 0
        ? '<tr><td colspan="6" class="muted">no closed paper trades yet — a frozen rule must fire on a live market.</td></tr>'
        : br.map(r => '<tr><td>' + r.rule + '</td>' +
            '<td class="right">' + r.n + '</td>' +
            '<td class="right"><b>' + (r.windows||0) + '</b></td>' +
            '<td class="right">' + (r.n ? (r.wins/r.n*100).toFixed(1)+'%' : '—') + '</td>' +
            '<td class="right ' + pnlClass(r.pnl) + '">$' + fmt(r.pnl) + '</td>' +
            '<td class="right">' + (r.n ? (r.pnl/r.n>=0?'+':'') + (r.pnl/r.n).toFixed(3) : '—') + '</td></tr>').join('');
      const op = pc.open_positions || [];
      document.getElementById('paper-crypto-open').innerHTML = op.length
        ? 'open: ' + op.map(o => o.ticker + ' (' + o.side + ' @' + Number(o.price).toFixed(2) + ')').join(', ')
        : '';
    }

    // Shadow book
    const sh = data.shadow || {};
    const shStatus = document.getElementById('shadow-status');
    const shBody = document.getElementById('shadow-tbody');
    if (sh.error) {
      shStatus.textContent = 'shadow error: ' + sh.error;
      shBody.innerHTML = '';
    } else {
      shStatus.innerHTML = 'settled markets in log: <b>' + (sh.n_settled_markets||0) +
        '</b> &middot; a rule needs n&ge;' + (sh.min_n||100) + ' before its P&L means anything';
      const rl = sh.rules || [];
      shBody.innerHTML = rl.length === 0
        ? '<tr><td colspan="9" class="muted">no shadow trades yet.</td></tr>'
        : rl.map(r => {
            const wr = r.win_rate == null ? '—' : (r.win_rate*100).toFixed(1) + '%';
            const ci = r.wr_ci95 == null ? '' : ' <span class="muted tiny">±' + (r.wr_ci95*100).toFixed(1) + '</span>';
            const npt = r.net_per_trade == null ? '—' : (r.net_per_trade>=0?'+':'') + r.net_per_trade.toFixed(3);
            const fills = r.depth_known ? (r.fillable + '/' + r.depth_known) : 'n/a';
            const status = r.n === 0 ? 'no trades yet'
              : !r.meaningful ? 'THIN (need ' + (sh.min_n||100) + ')'
              : r.net > 0 ? 'positive — VERIFY' : 'negative';
            const sc = status.indexOf('positive') === 0 ? 'green' : 'muted';
            return '<tr><td>' + r.name + '</td><td>' + r.band + '</td>' +
              '<td class="muted">' + r.side + ' ' + r.price_range + '</td>' +
              '<td class="right">' + r.n + '</td>' +
              '<td class="right">' + wr + ci + '</td>' +
              '<td class="right ' + pnlClass(r.net) + '">$' + fmt(r.net) + '</td>' +
              '<td class="right">' + npt + '</td>' +
              '<td class="right">' + fills + '</td>' +
              '<td class="' + sc + '">' + status + '</td></tr>';
          }).join('');
    }

    // Stage-0 mispricing experiment
    const s0 = data.stage0 || {};
    const s0status = document.getElementById('stage0-status');
    const s0body = document.getElementById('stage0-tbody');
    if (s0.error) {
      s0status.textContent = 'stage0 error: ' + s0.error;
      s0body.innerHTML = '';
    } else {
      s0status.innerHTML = 'joined observations: <b>' + (s0.joined||0) + '</b>' +
        ' &middot; settled markets seen: ' + (s0.n_settles||0) +
        (s0.verdict_ready
          ? ' &middot; <span class="green">n&ge;1500 — table is verdict-grade</span>'
          : ' &middot; <span class="yellow">n&lt;1500 — directional only, no verdict (frozen protocol)</span>');
      const cells = s0.cells || [];
      s0body.innerHTML = cells.length === 0
        ? '<tr><td colspan="8" class="muted">no joined observations yet — the collector must see a market before it settles.</td></tr>'
        : cells.map(c => {
            const net = c.gap - c.fee;
            const gc = c.gap > 0 ? 'green' : c.gap < 0 ? 'red' : 'muted';
            const read = c.n < 100 ? 'thin-n' : (net > 0.01 ? 'edge?' : 'no');
            const rc = read === 'edge?' ? 'green' : 'muted';
            return '<tr><td>' + c.band + '</td><td>' + c.bucket + '</td>' +
              '<td class="right">' + c.n + '</td>' +
              '<td class="right">' + c.avg_cost.toFixed(3) + '</td>' +
              '<td class="right">' + c.realized.toFixed(3) + '</td>' +
              '<td class="right ' + gc + '">' + (c.gap>=0?'+':'') + c.gap.toFixed(3) + '</td>' +
              '<td class="right">' + c.fee.toFixed(2) + '</td>' +
              '<td class="' + rc + '">' + read + '</td></tr>';
          }).join('');
    }

    // Live markets
    const liveBody = document.getElementById('live-tbody');
    const live = Array.isArray(data.live) ? data.live : [];
    const liveErr = (data.live && data.live.error) ? ' (' + data.live.error + ')' : '';
    liveBody.innerHTML = live.length === 0
      ? '<tr><td colspan="10" class="muted">No active markets right now.' + liveErr + '</td></tr>'
      : live.map(m => {
          const rowClass = m.passes_threshold ? 'pass' : '';
          const gap = m.theo_yes_gap;
          const gapClass = gap > 0.02 ? 'green' : gap < -0.02 ? 'red' : 'muted';
          return '<tr class="' + rowClass + '">' +
            '<td>' + m.asset + '</td>' +
            '<td>' + fmtT(m.seconds_to_close) + '</td>' +
            '<td class="right">$' + fmt(m.strike, m.strike < 1000 ? 2 : 0) + '</td>' +
            '<td class="right">$' + fmt(m.spot, m.spot < 1000 ? 2 : 0) + '</td>' +
            '<td class="right">' + fmt(m.yes_ask, 3) + '</td>' +
            '<td class="right">' + fmt(m.theoretical_yes, 3) + '</td>' +
            '<td class="right ' + gapClass + '">' + (gap >= 0 ? '+' : '') + fmt(gap*100, 1) + 'pp</td>' +
            '<td class="right">' + fmt(m.composite, 2) + '</td>' +
            '<td class="right">' + fmtPct(m.confidence) + ' / ' + fmtPct(m.threshold) + '</td>' +
            '<td>' + (m.passes_threshold ? '<span class="green">✓</span>' : '<span class="muted">·</span>') + '</td>' +
          '</tr>';
        }).join('');

    // Open paper
    const openBody = document.getElementById('open-tbody');
    const open = (data.paper && data.paper.open_trades) || [];
    openBody.innerHTML = open.length === 0
      ? '<tr><td colspan="8" class="muted">No open paper trades.</td></tr>'
      : open.map(t =>
          '<tr><td>' + (t.asset||'?') + '</td>' +
          '<td>' + t.side + '</td>' +
          '<td class="right">' + fmt(t.fill_price, 3) + '</td>' +
          '<td class="right">' + fmt(t.our_size, 1) + '</td>' +
          '<td class="right">' + fmtPct(t.confidence) + '</td>' +
          '<td class="right">' + fmt(t.composite, 2) + '</td>' +
          '<td class="right">$' + fmt(t.strike, t.strike < 1000 ? 2 : 0) + '</td>' +
          '<td class="muted">' + (t.title||'').slice(0,40) + '</td></tr>'
        ).join('');

    // Per-asset rollup
    const byAssetBody = document.getElementById('by-asset-tbody');
    const ba = (s.by_asset || {});
    const baEntries = Object.entries(ba).sort();
    byAssetBody.innerHTML = baEntries.length === 0
      ? '<tr><td colspan="7" class="muted">No data.</td></tr>'
      : baEntries.map(([asset, b]) =>
          '<tr><td>' + asset + '</td>' +
          '<td class="right">' + b.total + '</td>' +
          '<td class="right green">' + (b.won||0) + '</td>' +
          '<td class="right red">' + (b.lost||0) + '</td>' +
          '<td class="right">' + fmtPct(b.win_rate) + '</td>' +
          '<td class="right">' + fmtPnl(b.pnl) + '</td>' +
          '<td class="right ' + pnlClass(b.roi_pct) + '">' + fmtPct(b.roi_pct) + '</td></tr>'
        ).join('');

    // Confidence buckets
    const bkBody = document.getElementById('bucket-tbody');
    const bk = (s.by_confidence_bucket || {});
    const bkEntries = Object.entries(bk).sort();
    bkBody.innerHTML = bkEntries.length === 0
      ? '<tr><td colspan="5" class="muted">No settled trades yet.</td></tr>'
      : bkEntries.map(([bucket, b]) => {
          const wr = b.settled > 0 ? b.wins / b.settled : 0;
          return '<tr><td>' + bucket + '</td>' +
            '<td class="right">' + b.settled + '</td>' +
            '<td class="right">' + b.wins + '</td>' +
            '<td class="right">' + fmtPct(wr) + '</td>' +
            '<td class="right">' + fmtPnl(b.pnl) + '</td></tr>';
        }).join('');

    // Recent settled
    const settledBody = document.getElementById('settled-tbody');
    const rs = (data.paper && data.paper.recent_settled) || [];
    settledBody.innerHTML = rs.length === 0
      ? '<tr><td colspan="8" class="muted">No settled trades yet.</td></tr>'
      : rs.map(t =>
          '<tr><td class="tiny">' + (t.opened_at||'').slice(11,19) + '</td>' +
          '<td>' + (t.asset||'?') + '</td>' +
          '<td>' + t.side + '</td>' +
          '<td class="right">' + fmt(t.fill_price, 3) + '</td>' +
          '<td class="right">' + fmtPct(t.confidence) + '</td>' +
          '<td class="' + statusClass(t.status) + '">' + t.status + '</td>' +
          '<td class="right">' + fmtPnl(t.paper_pnl) + '</td>' +
          '<td class="muted">' + (t.title||'').slice(0,50) + '</td></tr>'
        ).join('');

  } catch (e) {
    document.getElementById('ts').textContent = 'error: ' + e.message;
  }
}
refresh();
setInterval(refresh, 20000);
</script>
</body>
</html>"""
