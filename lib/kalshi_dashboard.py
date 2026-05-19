"""
Kalshi 15-min trader dashboard — live view of the Phase 2 pipeline.

Flask app + single-page HTML at localhost:5053. Auto-refreshes every
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
    settled.sort(key=lambda r: r.get("opened_at", ""), reverse=True)
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
        return jsonify({
            "live": _live_markets_payload(),
            "paper": _paper_payload(),
            "account": _account_payload(),
            "cron": _cron_health_payload(),
            "enabled_assets": _enabled_assets(),
            "ts": datetime.now(timezone.utc).isoformat(),
        })

    return app


def _enabled_assets() -> list[str]:
    """Return the list of asset keys currently enabled in
    config/kalshi_assets.yaml. Used by the dashboard to visually
    distinguish active assets from historical/disabled ones — so
    operators don't conflate frozen ETH/SOL losses with current
    BTC-only performance.
    """
    try:
        from lib.kalshi_15min_signal import enabled_assets
        return sorted(enabled_assets().keys())
    except Exception:
        return []


def run_dashboard(port: int = 5053):
    """Boot the Flask app. Port defaults to 5053 to avoid colliding
    with the existing polybot dashboards on 5050/5051/5052.
    """
    import socket
    # Pre-flight: confirm the port is free so we fail loudly, not silently.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
    except OSError as e:
        s.close()
        raise RuntimeError(
            f"Port {port} is already in use ({e}). "
            f"Try --port=5054 or check `lsof -i :{port}`."
        )
    s.close()

    app = make_app()
    print(f"Kalshi dashboard → http://localhost:{port}")
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)


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
</style>
</head>
<body>
  <h1>Kalshi 15-min Trader</h1>
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
  if (s === 'won') return 'green'; if (s === 'lost') return 'red';
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

    // Live markets
    const liveBody = document.getElementById('live-tbody');
    const live = data.live || [];
    liveBody.innerHTML = live.length === 0
      ? '<tr><td colspan="10" class="muted">No active markets right now.</td></tr>'
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

    // Per-asset rollup. Visually distinguish disabled assets — their
    // P&L is historical/frozen, not live.
    const byAssetBody = document.getElementById('by-asset-tbody');
    const ba = (s.by_asset || {});
    const enabledSet = new Set((data.enabled_assets || []).map(a => a.toLowerCase()));
    const baEntries = Object.entries(ba).sort();
    byAssetBody.innerHTML = baEntries.length === 0
      ? '<tr><td colspan="7" class="muted">No data.</td></tr>'
      : baEntries.map(([asset, b]) => {
          const isEnabled = enabledSet.has(asset.toLowerCase());
          const rowClass = isEnabled ? '' : ' class="muted"';
          const badge = isEnabled
            ? '<span class="badge ok">ACTIVE</span>'
            : '<span class="badge warn" title="historical only — bot is no longer trading this asset">DISABLED</span>';
          return '<tr' + rowClass + '><td>' + asset + ' ' + badge + '</td>' +
            '<td class="right">' + b.total + '</td>' +
            '<td class="right green">' + (b.won||0) + '</td>' +
            '<td class="right red">' + (b.lost||0) + '</td>' +
            '<td class="right">' + fmtPct(b.win_rate) + '</td>' +
            '<td class="right">' + fmtPnl(b.pnl) + '</td>' +
            '<td class="right ' + pnlClass(b.roi_pct) + '">' + fmtPct(b.roi_pct) + '</td></tr>';
        }).join('');

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
