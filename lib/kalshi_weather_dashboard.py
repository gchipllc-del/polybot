"""
Kalshi weather sleeve dashboard — live view of the hourly-temperature
paper pipeline. Flask app + single-page HTML at localhost:5054
(crypto sleeve owns 5053). Auto-refreshes every 20s.

Panels:
  * Live hourly markets (latest sample per still-open market: city,
    T-close, window, bucket, blended forecast, fair vs market, edge,
    fires?)
  * Open paper trades
  * Recent settled (color-coded)
  * Per-city rollup (WR / P&L / ROI)
  * Edge-bucket calibration (does realized WR rise with claimed edge?)
  * Kalshi balance (signed; degrades gracefully)
  * Cron health

Every endpoint reads local JSONL or hits public Kalshi/weather endpoints;
the balance call is the only signed one.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).parent.parent
PAPER_PATH = ROOT / "data" / "kalshi_weather_paper.jsonl"
SIGNAL_PATH = ROOT / "data" / "kalshi_weather_signal.jsonl"
LOG_PATH = ROOT / "logs" / "launchd_kalshi_weather.log"


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


def _min_edge() -> float:
    try:
        from lib.kalshi_weather_signal import load_config
        return float((load_config().get("params") or {}).get("min_edge", 0.08))
    except Exception:
        return 0.08


def _latest_signal_per_market() -> dict:
    rows = _load_jsonl(SIGNAL_PATH)
    rows = rows[-1000:] if len(rows) > 1000 else rows
    out: dict = {}
    for r in rows:
        tk = r.get("market_ticker")
        if tk:
            out[tk] = r
    return out


def _live_markets_payload() -> list[dict]:
    min_edge = _min_edge()
    latest = _latest_signal_per_market()
    now = datetime.now(timezone.utc)
    out: list[dict] = []
    for tk, s in latest.items():
        close_iso = s.get("close_time") or ""
        try:
            close_dt = datetime.fromisoformat(close_iso.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if (close_dt - now).total_seconds() < -30:
            continue
        ey, en = s.get("edge_yes"), s.get("edge_no")
        best_side = "YES" if (ey or -9) >= (en or -9) else "NO"
        best_edge = ey if best_side == "YES" else en
        out.append({
            "ticker": tk,
            "city": s.get("city", "?"),
            "title": (s.get("title", "") or "")[:60],
            "is_hourly": s.get("is_hourly"),
            "window_minutes": s.get("window_minutes"),
            "seconds_to_close": (close_dt - now).total_seconds(),
            "floor": s.get("floor_strike"),
            "cap": s.get("cap_strike"),
            "fair_yes": s.get("fair_yes"),
            "yes_ask": s.get("yes_ask"),
            "no_ask": s.get("no_ask"),
            "forecast_mu": s.get("forecast_mu"),
            "forecast_sigma": s.get("forecast_sigma"),
            "n_sources": s.get("n_sources"),
            "best_side": best_side,
            "best_edge": best_edge,
            "passes_edge": best_edge is not None and best_edge >= min_edge,
        })
    out.sort(key=lambda x: x["seconds_to_close"])
    return out


def _paper_payload() -> dict:
    from lib.kalshi_weather_paper import summary
    rows = _load_jsonl(PAPER_PATH)
    open_trades = [r for r in rows if r.get("status") == "open"]
    settled = [r for r in rows if r.get("status") != "open"]
    settled.sort(key=lambda r: r.get("resolved_at") or r.get("opened_at", ""),
                 reverse=True)
    return {
        "summary": summary(),
        "open_trades": open_trades[:20],
        "recent_settled": settled[:15],
    }


def _account_payload() -> dict:
    from lib.kalshi_auth import can_sign, signed_get, status as auth_status
    base = {"auth": auth_status(), "balance_dollars": None, "error": None}
    if not can_sign():
        base["error"] = "auth not configured"
        return base
    try:
        data = signed_get("/portfolio/balance")
        base["balance_dollars"] = round(data.get("balance", 0) / 100.0, 2)
    except Exception as e:
        base["error"] = str(e)[:200]
    return base


def _cron_health_payload() -> dict:
    if not LOG_PATH.exists():
        return {"log_exists": False}
    try:
        mtime = datetime.fromtimestamp(LOG_PATH.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return {"log_exists": False}
    age_min = (datetime.now(timezone.utc) - mtime).total_seconds() / 60.0
    try:
        cycles = LOG_PATH.read_text().count("=== Kalshi weather signal cycle")
    except OSError:
        cycles = None
    return {
        "log_exists": True,
        "last_modified": mtime.isoformat(),
        "age_minutes": round(age_min, 1),
        "total_cycles_in_log": cycles,
    }


def make_app():
    from flask import Flask, jsonify, render_template_string
    app = Flask(__name__)

    @app.route("/")
    def index():
        return render_template_string(_TEMPLATE)

    @app.route("/api/all")
    def api_all():
        return jsonify({
            "live": _live_markets_payload(),
            "paper": _paper_payload(),
            "account": _account_payload(),
            "cron": _cron_health_payload(),
            "ts": datetime.now(timezone.utc).isoformat(),
        })

    return app


def run_dashboard(port: int = 5054):
    """Boot the Flask app. Port 5054 avoids the crypto sleeve on 5053."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
    except OSError as e:
        s.close()
        raise RuntimeError(f"Port {port} is already in use ({e}). Try --port=5055.")
    s.close()
    app = make_app()
    print(f"Kalshi weather dashboard → http://localhost:{port}")
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)


_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Kalshi Weather (Hourly) Trader</title>
<style>
:root{--bg:#0e1116;--panel:#161b22;--border:#30363d;--text:#c9d1d9;--muted:#8b949e;
--green:#3fb950;--red:#f85149;--yellow:#d29922;--blue:#58a6ff;}
*{box-sizing:border-box;}body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
background:var(--bg);color:var(--text);padding:16px;}
h1{margin:0 0 6px 0;font-size:20px;}.sub{color:var(--muted);font-size:12px;margin-bottom:16px;}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(380px,1fr));}
.panel{background:var(--panel);border:1px solid var(--border);border-radius:6px;padding:12px;overflow-x:auto;}
.panel h2{margin:0 0 8px 0;font-size:14px;color:var(--blue);}
table{width:100%;border-collapse:collapse;font-size:12px;}
th,td{text-align:left;padding:4px 6px;border-bottom:1px solid var(--border);white-space:nowrap;}
th{color:var(--muted);font-weight:500;font-size:11px;}tr:last-child td{border-bottom:none;}
.green{color:var(--green);}.red{color:var(--red);}.yellow{color:var(--yellow);}.muted{color:var(--muted);}
.pass{background:rgba(63,185,80,0.15);}.right{text-align:right;}.tiny{font-size:10px;color:var(--muted);}
.bigstat{display:inline-block;margin-right:18px;font-size:13px;}
.bigstat .v{display:block;font-size:18px;color:var(--text);}.bigstat .k{color:var(--muted);font-size:11px;}
.pill{font-size:10px;padding:1px 5px;border-radius:8px;border:1px solid var(--border);}
.nav{display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap;}
.nav a{font-size:12px;padding:4px 10px;border:1px solid var(--border);border-radius:6px;color:var(--muted);text-decoration:none;}
.nav a:hover{color:var(--text);border-color:var(--blue);}
.nav a.active{color:var(--blue);border-color:var(--blue);background:rgba(88,166,255,0.12);}
</style></head><body>
<h1>Kalshi Weather — Hourly Temperature</h1>
<div class="nav">
  <a href="http://localhost:5050/">📊 Main</a>
  <a href="http://localhost:5053/">₿ Crypto 15-min</a>
  <a href="http://localhost:5054/" class="active">🌡 Weather</a>
</div>
<div class="sub" id="ts">loading…</div>
<div class="grid">
  <div class="panel"><h2>Account</h2><div id="account">…</div></div>
  <div class="panel"><h2>Cron Health</h2><div id="cron">…</div></div>
  <div class="panel"><h2>All-up P&L</h2><div id="summary">…</div></div>
</div>
<div class="grid" style="margin-top:12px;">
  <div class="panel"><h2>Live Hourly Markets</h2>
    <table><thead><tr><th>city</th><th>T-close</th><th>win</th><th>bucket</th>
    <th>forecast</th><th>fair</th><th>mkt</th><th>edge</th><th>fires?</th></tr></thead>
    <tbody id="live-tbody"></tbody></table></div>
  <div class="panel"><h2>Open Paper Trades</h2>
    <table><thead><tr><th>city</th><th>side</th><th>fill</th><th>size</th>
    <th>fair</th><th>edge</th><th>bucket</th><th>title</th></tr></thead>
    <tbody id="open-tbody"></tbody></table></div>
</div>
<div class="grid" style="margin-top:12px;">
  <div class="panel"><h2>Per City (settled)</h2>
    <table><thead><tr><th>city</th><th>total</th><th>W</th><th>L</th>
    <th>WR</th><th>P&L</th><th>ROI</th></tr></thead><tbody id="by-city-tbody"></tbody></table></div>
  <div class="panel"><h2>Calibration (by edge bucket)</h2>
    <table><thead><tr><th>edge</th><th>settled</th><th>wins</th><th>WR</th><th>P&L</th></tr></thead>
    <tbody id="bucket-tbody"></tbody></table>
    <div class="tiny" style="margin-top:8px;">Realized WR should rise with claimed edge.</div></div>
</div>
<div class="panel" style="margin-top:12px;"><h2>Recent Settled (last 15)</h2>
  <table><thead><tr><th>resolved</th><th>city</th><th>side</th><th>fill</th>
  <th>edge</th><th>status</th><th>pnl</th><th>title</th></tr></thead><tbody id="settled-tbody"></tbody></table></div>
<script>
function fmt(v,d=2){if(v===null||v===undefined)return '—';if(typeof v==='number')return v.toFixed(d);return String(v);}
function fmtT(s){if(s===null||s===undefined)return '—';if(s<0)return '<span class="muted">closed</span>';
if(s<60)return s.toFixed(0)+'s';const m=Math.floor(s/60),sec=Math.floor(s%60);return m+'m'+(sec<10?'0':'')+sec+'s';}
function fmtPnl(v){if(v===null||v===undefined)return '—';const c=v>0?'green':v<0?'red':'muted';return '<span class="'+c+'">$'+(v>=0?'+':'')+fmt(v)+'</span>';}
function fmtPct(v){if(v===null||v===undefined)return '—';return (v*100).toFixed(1)+'%';}
function pnlClass(v){return v>0?'green':v<0?'red':'muted';}
function statusClass(s){if(s==='won')return 'green';if(s==='lost')return 'red';if(s==='void')return 'yellow';return 'muted';}
function bucketStr(f,c){if(f!==null&&f!==undefined&&c!==null&&c!==undefined)return f+'–'+c+'°';
if(f!==null&&f!==undefined)return '≥'+f+'°';if(c!==null&&c!==undefined)return '≤'+c+'°';return '—';}
async function refresh(){try{
const r=await fetch('/api/all');const data=await r.json();
document.getElementById('ts').textContent='last refresh: '+new Date(data.ts).toLocaleTimeString();
const acc=data.account||{};const bal=acc.balance_dollars;
document.getElementById('account').innerHTML=(bal!==null&&bal!==undefined)
 ?'<div class="bigstat"><span class="v">$'+bal.toFixed(2)+'</span><span class="k">balance</span></div>'+(acc.error?'<div class="red tiny">'+acc.error+'</div>':'')
 :'<div class="red">'+(acc.error||'no balance')+'</div>';
const c=data.cron||{};const age=c.age_minutes;const ac=age==null?'muted':age<7?'green':age<15?'yellow':'red';
document.getElementById('cron').innerHTML=c.log_exists
 ?'<div class="bigstat"><span class="v '+ac+'">'+fmt(age,1)+'m</span><span class="k">since last cycle</span></div>'+
  '<div class="bigstat"><span class="v">'+(c.total_cycles_in_log||0)+'</span><span class="k">cycles in log</span></div>'
 :'<div class="muted">log not found — is the launchd job loaded?</div>';
const s=(data.paper&&data.paper.summary)||{};
document.getElementById('summary').innerHTML=s.total_trades>0
 ?'<div class="bigstat"><span class="v">'+s.total_trades+'</span><span class="k">trades</span></div>'+
  '<div class="bigstat"><span class="v">'+fmtPct(s.win_rate)+'</span><span class="k">WR (settled)</span></div>'+
  '<div class="bigstat"><span class="v '+pnlClass(s.total_paper_pnl)+'">$'+fmt(s.total_paper_pnl)+'</span><span class="k">paper P&L</span></div>'+
  '<div class="bigstat"><span class="v '+pnlClass(s.roi_pct)+'">'+fmtPct(s.roi_pct)+'</span><span class="k">ROI</span></div>'
 :'<div class="muted">No trades yet.</div>';
const live=data.live||[];
document.getElementById('live-tbody').innerHTML=live.length===0
 ?'<tr><td colspan="9" class="muted">No open hourly markets right now.</td></tr>'
 :live.map(m=>{const e=m.best_edge;const ec=e==null?'muted':e>0?'green':'red';
   return '<tr class="'+(m.passes_edge?'pass':'')+'">'+
   '<td>'+m.city+'</td><td>'+fmtT(m.seconds_to_close)+'</td>'+
   '<td class="tiny">'+(m.window_minutes!=null?Math.round(m.window_minutes)+'m':'—')+'</td>'+
   '<td>'+bucketStr(m.floor,m.cap)+'</td>'+
   '<td class="right">'+fmt(m.forecast_mu,1)+'±'+fmt(m.forecast_sigma,1)+'</td>'+
   '<td class="right">'+fmt(m.fair_yes,3)+'</td>'+
   '<td class="right">'+fmt(m.yes_ask,3)+'</td>'+
   '<td class="right '+ec+'">'+m.best_side+' '+(e==null?'—':(e>=0?'+':'')+fmt(e,2))+'</td>'+
   '<td>'+(m.passes_edge?'<span class="green">✓</span>':'<span class="muted">·</span>')+'</td></tr>';}).join('');
const open=(data.paper&&data.paper.open_trades)||[];
document.getElementById('open-tbody').innerHTML=open.length===0
 ?'<tr><td colspan="8" class="muted">No open paper trades.</td></tr>'
 :open.map(t=>'<tr><td>'+(t.city||'?')+'</td><td>'+t.side+'</td>'+
   '<td class="right">'+fmt(t.fill_price,3)+'</td><td class="right">'+fmt(t.our_size,1)+'</td>'+
   '<td class="right">'+fmt(t.fair_yes,3)+'</td><td class="right">'+fmt(t.edge,2)+'</td>'+
   '<td>'+bucketStr(t.floor_strike,t.cap_strike)+'</td>'+
   '<td class="muted">'+(t.title||'').slice(0,32)+'</td></tr>').join('');
const ba=(s.by_city||{});const be=Object.entries(ba).sort();
document.getElementById('by-city-tbody').innerHTML=be.length===0
 ?'<tr><td colspan="7" class="muted">No data.</td></tr>'
 :be.map(([city,b])=>'<tr><td>'+city+'</td><td class="right">'+b.total+'</td>'+
   '<td class="right green">'+(b.won||0)+'</td><td class="right red">'+(b.lost||0)+'</td>'+
   '<td class="right">'+fmtPct(b.win_rate)+'</td><td class="right">'+fmtPnl(b.pnl)+'</td>'+
   '<td class="right '+pnlClass(b.roi_pct)+'">'+fmtPct(b.roi_pct)+'</td></tr>').join('');
const bk=(s.by_edge_bucket||{});const bke=Object.entries(bk).sort();
document.getElementById('bucket-tbody').innerHTML=bke.length===0
 ?'<tr><td colspan="5" class="muted">No settled trades yet.</td></tr>'
 :bke.map(([bucket,b])=>{const wr=b.settled>0?b.wins/b.settled:0;
   return '<tr><td>'+bucket+'</td><td class="right">'+b.settled+'</td><td class="right">'+b.wins+'</td>'+
   '<td class="right">'+fmtPct(wr)+'</td><td class="right">'+fmtPnl(b.pnl)+'</td></tr>';}).join('');
const rs=(data.paper&&data.paper.recent_settled)||[];
document.getElementById('settled-tbody').innerHTML=rs.length===0
 ?'<tr><td colspan="8" class="muted">No settled trades yet.</td></tr>'
 :rs.map(t=>'<tr><td class="tiny">'+((t.resolved_at||t.opened_at||'').slice(11,19))+'</td>'+
   '<td>'+(t.city||'?')+'</td><td>'+t.side+'</td><td class="right">'+fmt(t.fill_price,3)+'</td>'+
   '<td class="right">'+fmt(t.edge,2)+'</td>'+
   '<td class="'+statusClass(t.status)+'">'+t.status+'</td>'+
   '<td class="right">'+fmtPnl(t.paper_pnl)+'</td>'+
   '<td class="muted">'+(t.title||'').slice(0,40)+'</td></tr>').join('');
}catch(e){document.getElementById('ts').textContent='error: '+e.message;}}
refresh();setInterval(refresh,20000);
</script></body></html>"""
