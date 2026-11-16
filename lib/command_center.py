"""command_center - the one page that answers: what is running, what is it learning,
and has anything earned real money yet? (http://127.0.0.1:5155)

It reads the same files everything else writes - nothing here trades, fetches markets,
or changes state:
  data/paper_crypto15.jsonl     crypto 15-min forward paper ledger
  data/paper_sleeves.jsonl      multi-sleeve forward paper ledger (+ data/sleeve_rules.jsonl)
  data/news_regime.jsonl        the news agent's hourly regime
  data/sleeves/screen.json      sleeve scanner nominations
  logs/healthcheck.log          the watchdog's last verdict

ONE STANDARD FOR EVERY RULE. All rules - crypto and sleeves - are scored in one family:
per independent window/event, against the variance a fair price would produce, with
Benjamini-Hochberg across every rule at once (the same machinery as sleeve_paper and
sleeve_scan). Retired sleeve rules are not shown, but their records stay in the family,
and `sleeve_paper.py report` corrects over this same family (crypto_family). Crypto
rules are judged ONLY on fill-checked (v4 gated) trades: before 2026-09-25 the fill gate
was a silent no-op, and the slice evidence says the late-longshot record lived in quotes
nobody could fill. The all-era record is still shown.

Design follows the dataviz method: the headline is a number (rules that earned an edge),
color is reserved for identity (two series) and status (good/bad, always with an icon
and a label), every chart has a table view, and every label is inserted as text.
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DATA = ROOT / "data"
CRYPTO_LEDGER = Path(os.environ.get("PAPER_CRYPTO_LEDGER") or (DATA / "paper_crypto15.jsonl"))
HEALTH_LOG = Path(os.environ.get("POLYBOT_HEALTH_LOG") or (ROOT / "logs" / "healthcheck.log"))
SCREEN = Path(os.environ.get("SLEEVE_DIR") or (DATA / "sleeves")) / "screen.json"
MIN_EVENTS = 100
_CACHE: dict = {"t": 0.0, "payload": None}


def _load_jsonl(path: Path) -> list[dict]:
    rows = []
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(d, dict):
                rows.append(d)
    return rows


def _ts(iso) -> float | None:
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _downsample(points: list, n: int = 40) -> list:
    if len(points) <= n:
        return points
    step = (len(points) - 1) / (n - 1)
    return [points[round(i * step)] for i in range(n)]


# ── trades from both ledgers, one shape ───────────────────────────────────────

def crypto_trades(rows: list[dict]) -> list[dict]:
    from vol_replay import era_of, window_of
    opens = {(r.get("ticker"), r.get("rule")): r for r in rows if r.get("t") == "open"}
    out = []
    for r in rows:
        if r.get("t") != "close":
            continue
        o = opens.get((r.get("ticker"), r.get("rule"))) or {}
        n = max(1, int(o.get("contracts") or 1))
        pnl = float(r.get("pnl") or 0.0)
        out.append({"rule": r.get("rule"), "venue": "crypto", "event": window_of(r.get("ticker")),
                    "pnl": pnl, "pnl_pc": pnl / n,
                    "fee_pc": float(o["fee"]) / n if o.get("fee") is not None else None,
                    "won": bool(r.get("won")),
                    "ask": float(r.get("price") or o.get("price") or 0.5),
                    "ts": _ts(r.get("ts")) or _ts(o.get("ts")) or 0.0,
                    "era": era_of({"v": o.get("v"), "depth": o.get("depth")})})
    return out


def sleeve_trades() -> tuple[list[dict], dict, list[dict]]:
    """Trades the sleeve report counts, by the sleeve report's own code (trades_of: after
    named_at, per contract), labelled for the table - plus the RETIRED rules' records
    (sleeve_paper.retired_stats): not shown, but they stay in the family correction."""
    try:
        import sleeve_paper as sp
    except Exception:  # noqa: BLE001
        return [], {}, []
    rules = sp.active_rules(sp.RULES)          # explicit: the default arg binds at def time
    rows = _load_jsonl(sp.LEDGER)
    out = []
    for name, ts in sp.trades_of(rows, rules).items():
        label = sp.series_label(rules[name])
        out += [dict(t, venue="sleeves", series=label,
                     era="v4 gated" if t["depth"] is not None else "v4 ungated") for t in ts]
    return out, rules, sp.retired_stats(rows, sp.RULES)


def _series_count(srules: dict) -> int:
    try:
        import sleeve_paper as sp
        return len({x for r in srules.values() for x in sp.series_of(r)})
    except Exception:  # noqa: BLE001
        return 0


# ── the one scoring family ────────────────────────────────────────────────────

def _stats(trades: list[dict]) -> dict:
    import sleeve_paper as sp
    return sp.forward_stats(trades)


def _decision_basis(ts: list[dict], venue: str) -> list[dict]:
    """The trades a verdict is decided on: a crypto rule's fill-checked (v4 gated) trades
    only; a sleeve rule's every trade after named_at."""
    return [t for t in ts if t["era"] == "v4 gated"] if venue == "crypto" else ts


def crypto_family(path: Path | None = None) -> list[dict]:
    """The crypto rules' members of the one forward family: each rule's decision stats,
    built exactly as score_rules builds them. The sleeve report corrects over these too,
    so both surfaces judge a sleeve rule against the same family."""
    by_rule: dict[str, list] = {}
    for t in crypto_trades(_load_jsonl(path or CRYPTO_LEDGER)):
        by_rule.setdefault(t["rule"], []).append(t)
    return [_stats(_decision_basis(ts, "crypto")) for _, ts in sorted(by_rule.items())]


def score_rules(trades: list[dict], sleeve_rule_names: set | None = None,
                extra_stats=()) -> list[dict]:
    """Every rule, one family. Crypto rules are judged on fill-checked (v4 gated) trades
    only; sleeve rules on everything after named_at. BH across all testable rules - the
    rules shown plus extra_stats (the retired sleeve rules' records, never shown)."""
    import sleeve_paper as sp
    by_rule: dict[str, list] = {}
    for t in trades:
        by_rule.setdefault(t["rule"], []).append(t)
    for name in sleeve_rule_names or ():
        by_rule.setdefault(name, [])
    rows = []
    for name, ts in by_rule.items():
        venue = ts[0]["venue"] if ts else "sleeves"
        basis = _decision_basis(ts, venue)
        s_all, s_dec = _stats(ts), _stats(basis)
        cum, acc = [], 0.0
        for t in sorted(ts, key=lambda x: x["ts"]):
            acc += t["pnl"]
            cum.append(round(acc, 4))
        rows.append({"rule": name, "venue": venue,
                     "series": next((t.get("series") for t in ts if t.get("series")), None),
                     "all": s_all, "decision": s_dec, "spark": _downsample(cum)})
    keep = sp.survivors([r["decision"] for r in rows] + list(extra_stats))
    for i, r in enumerate(rows):                  # rows come first: index i is rows[i]
        d = r["decision"]
        basis = "fill-checked" if r["venue"] == "crypto" else "after registration"
        cat = sp.classify(d, i in keep)
        r["verdict"], r["status"] = {
            "collecting": (f"collecting · {d['n']}/{MIN_EVENTS} {basis}", "neutral"),
            "edge": ("EDGE · survives the family correction", "good"),
            "promising": (f"promising · needs {MIN_EVENTS} events", "warning"),
            "negative": ("negative · significant", "critical"),
        }.get(cat, ("no edge yet", "neutral"))
    order = {"good": 0, "warning": 1, "neutral": 2, "critical": 3}
    rows.sort(key=lambda r: (order[r["status"]], -r["decision"]["n"], r["rule"]))
    return rows


def equity_series(trades: list[dict]) -> dict:
    out = {}
    for venue in ("crypto", "sleeves"):
        by_day: dict[str, float] = {}
        for t in trades:
            if t["venue"] == venue and t["ts"]:
                d = datetime.fromtimestamp(t["ts"], tz=timezone.utc).date().isoformat()
                by_day[d] = by_day.get(d, 0.0) + t["pnl"]
        acc, pts = 0.0, []
        for d in sorted(by_day):
            acc += by_day[d]
            pts.append([d, round(acc, 2)])
        out[venue] = pts
    return out


# ── the other panels ──────────────────────────────────────────────────────────

def regime_panel(days: int = 14) -> dict:
    try:
        from lib import news_regime as nr
    except Exception:  # noqa: BLE001
        return {"current": {"regime": "unknown", "why": "news agent unavailable"}, "cells": []}
    cur = nr.current_regime()
    cutoff = time.time() - days * 86400
    cells: dict[tuple, str] = {}
    for r in nr.load_history():
        t = _ts(r.get("ts"))
        if t is None or t < cutoff:
            continue
        utc = datetime.fromtimestamp(t, tz=timezone.utc)
        et = utc + timedelta(hours=nr.et_offset_hours(utc))
        cells[(et.date().isoformat(), et.hour)] = r.get("regime", "unknown")
    return {"current": {"regime": cur["regime"], "mult": cur["mult"], "why": cur["why"],
                        "ts": cur["ts"]},
            "cells": [{"d": d, "h": h, "r": v} for (d, h), v in sorted(cells.items())]}


def health_panel() -> dict:
    if not HEALTH_LOG.exists():
        return {"state": "unknown", "ts": None, "lines": ["the watchdog has not run yet"]}
    try:
        lines = HEALTH_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-400:]
    except OSError:
        return {"state": "unknown", "ts": None, "lines": ["watchdog log unreadable"]}
    head = max((i for i, l in enumerate(lines) if l and not l[0].isspace()), default=None)
    if head is None:
        return {"state": "unknown", "ts": None, "lines": []}
    parts = lines[head].split()
    state = "healthy" if len(parts) > 1 and parts[1] == "healthy" else "unhealthy"
    detail = [l.strip() for l in lines[head + 1:] if l.startswith(" ")]
    age = (time.time() - (_ts(parts[0]) or 0)) / 60.0
    if age > 30:
        state = "stale"
        detail.insert(0, f"last watchdog run was {age:.0f} min ago (it runs every 15)")
    return {"state": state, "ts": parts[0], "lines": detail[:20]}


def nominations_panel() -> list[dict]:
    p = SCREEN
    if not p.exists():
        return []
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [{k: c.get(k) for k in ("series", "band", "side", "bucket", "n_out", "mean_out",
                                   "p_out", "mean_out_traded", "verdict")}
            for c in d.get("nominated") or []][:30]


def build_payload() -> dict:
    now = time.time()
    if _CACHE["payload"] is not None and now - _CACHE["t"] < 30:
        return _CACHE["payload"]
    ct = crypto_trades(_load_jsonl(CRYPTO_LEDGER))
    st, srules, retired = sleeve_trades()
    trades = ct + st
    rules = score_rules(trades, set(srules), retired)
    payload = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "live_money": "OFF",
        "kpi": {"rules": len(rules), "edge": sum(r["status"] == "good" for r in rules),
                "net": round(sum(t["pnl"] for t in trades), 2),
                "net_crypto": round(sum(t["pnl"] for t in ct), 2),
                "net_sleeves": round(sum(t["pnl"] for t in st), 2),
                "windows": len({t["event"] for t in ct}),
                "events": len({t["event"] for t in st}),
                "sleeves": _series_count(srules)},
        "rules": rules, "equity": equity_series(trades), "regime": regime_panel(),
        "health": health_panel(), "nominations": nominations_panel(),
    }
    _CACHE.update(t=now, payload=payload)
    return payload


# ── server ────────────────────────────────────────────────────────────────────

def make_app():
    from flask import Flask, jsonify, Response
    app = Flask(__name__)

    @app.route("/")
    def index():
        return Response(_PAGE, mimetype="text/html")

    @app.route("/api/command")
    def api():
        try:
            return jsonify(build_payload())
        except Exception as e:  # noqa: BLE001 - a panel error must not 500 the page
            return jsonify({"error": f"{type(e).__name__}: {str(e)[:200]}"}), 200
    return app


def run_dashboard(port: int = 5155):
    import socket
    host = os.environ.get("POLYBOT_DASH_HOST", "127.0.0.1")
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
    except OSError as e:
        s.close()
        raise RuntimeError(f"Port {port} is already in use ({e}).")
    s.close()
    print(f"Command center -> http://localhost:{port}")
    make_app().run(host=host, port=port, debug=False, use_reloader=False)


# ── the page ──────────────────────────────────────────────────────────────────

_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Polybot Command Center</title>
<style>
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --ring: rgba(11,11,11,0.10);
  --s1: #2a78d6; --s2: #eb6834; --deemph: #c3c2b7;
  --good: #0ca30c; --warning: #fab219; --serious: #ec835a; --critical: #d03b3b;
  --good-text: #006300; --neutral-cell: #e1e0d9;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,0.10);
    --s1: #3987e5; --s2: #d95926; --deemph: #52514e; --good-text: #0ca30c;
    --neutral-cell: #383835;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink-2: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,0.10);
  --s1: #3987e5; --s2: #d95926; --deemph: #52514e; --good-text: #0ca30c;
  --neutral-cell: #383835;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--ink);
       font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1200px; margin: 0 auto; padding: 20px 16px 48px; }
header { display: flex; flex-wrap: wrap; align-items: baseline; gap: 8px 16px; }
h1 { font-size: 20px; font-weight: 600; margin: 0; }
.sub { color: var(--muted); font-size: 12px; }
.pill { display: inline-flex; align-items: center; gap: 6px; border: 1px solid var(--ring);
        border-radius: 999px; padding: 2px 10px; font-size: 12px; color: var(--ink-2);
        background: var(--surface); }
.dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.filters { display: flex; gap: 8px; margin: 16px 0; flex-wrap: wrap; }
.seg { display: inline-flex; border: 1px solid var(--ring); border-radius: 8px; overflow: hidden; }
.seg button { border: 0; background: var(--surface); color: var(--ink-2); padding: 6px 12px;
              font: inherit; cursor: pointer; }
.seg button[aria-pressed="true"] { background: var(--ink); color: var(--surface); }
.grid { display: grid; gap: 12px; }
.kpis { grid-template-columns: 2fr repeat(4, 1fr); }
@media (max-width: 900px) { .kpis { grid-template-columns: repeat(2, 1fr); }
                            .kpis .hero { grid-column: 1 / -1; } }
.two { grid-template-columns: 1fr 1fr; align-items: start; }
@media (max-width: 900px) { .two { grid-template-columns: 1fr; } }
.card { background: var(--surface); border: 1px solid var(--ring); border-radius: 12px;
        padding: 16px; min-width: 0; }
.card h2 { font-size: 13px; font-weight: 600; margin: 0 0 4px; }
.card .cap { color: var(--muted); font-size: 12px; margin: 0 0 12px; }
.label { color: var(--ink-2); font-size: 12px; }
.value { font-size: 24px; font-weight: 600; margin-top: 4px; }
.hero .value { font-size: 56px; line-height: 1.05; }
.value .status { font-size: 20px; }
#eq-chart, #rg-chart { overflow-x: auto; }
.hero .of { font-size: 20px; color: var(--ink-2); font-weight: 500; }
.note { color: var(--ink-2); font-size: 12px; margin-top: 6px; }
.status { display: inline-flex; align-items: center; gap: 6px; font-weight: 600; }
.icon { width: 16px; height: 16px; flex: none; }
.cardhead { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }
.linkbtn { border: 0; background: none; color: var(--ink-2); font: inherit; font-size: 12px;
           text-decoration: underline; cursor: pointer; padding: 0; }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }
th { text-align: left; font-weight: 500; color: var(--muted); font-size: 12px;
     border-bottom: 1px solid var(--grid); padding: 6px 8px; white-space: nowrap; }
td { border-bottom: 1px solid var(--grid); padding: 6px 8px; vertical-align: middle;
     white-space: nowrap; }
td.num, th.num { text-align: right; }
.rule { font-weight: 500; }
.venue { color: var(--muted); font-size: 12px; }
.legend { display: flex; gap: 16px; flex-wrap: wrap; font-size: 12px; color: var(--ink-2);
          margin-bottom: 8px; }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.key { width: 14px; height: 2px; display: inline-block; border-radius: 1px; }
.sw { width: 10px; height: 10px; display: inline-block; border-radius: 2px; }
.tip { position: fixed; pointer-events: none; background: var(--surface); color: var(--ink);
       border: 1px solid var(--ring); border-radius: 8px; padding: 8px 10px; font-size: 12px;
       box-shadow: 0 4px 16px rgba(0,0,0,.12); display: none; z-index: 10; min-width: 140px; }
.tip .row { display: flex; align-items: center; gap: 8px; justify-content: space-between; }
.tip .v { font-weight: 600; }
.tip .n { color: var(--ink-2); }
.loading { opacity: .55; transition: opacity .2s; }
.empty { color: var(--muted); font-size: 13px; padding: 12px 0; }
svg text { fill: var(--muted); font-size: 11px; }
.health li { margin: 4px 0; display: flex; gap: 8px; align-items: flex-start; }
.health ul { list-style: none; padding: 0; margin: 0; font-size: 12px; color: var(--ink-2); }
section { margin-top: 12px; }
</style>
</head>
<body>
<main id="app">
  <header>
    <h1>Polybot command center</h1>
    <span class="pill" id="live"></span>
    <span class="sub" id="gen"></span>
  </header>

  <div class="filters" role="group" aria-label="Venue filter">
    <div class="seg" id="venue">
      <button aria-pressed="true" data-v="all">All rules</button>
      <button aria-pressed="false" data-v="crypto">Crypto 15-min</button>
      <button aria-pressed="false" data-v="sleeves">Sleeves</button>
    </div>
  </div>

  <div class="grid kpis" id="kpis"></div>

  <section class="card" id="equity-card">
    <div class="cardhead"><h2>Paper P&amp;L, cumulative</h2>
      <button class="linkbtn" id="eq-toggle">Show table</button></div>
    <p class="cap">Forward paper only - every trade stamped before its market settled.</p>
    <div class="legend" id="eq-legend"></div>
    <div id="eq-chart"></div>
    <div id="eq-table" hidden class="scroll"></div>
  </section>

  <section class="card">
    <div class="cardhead"><h2>Every rule, one standard</h2></div>
    <p class="cap">Judged per independent window/event and per contract, by the exact odds
      of the record with no edge (a favorite that hasn't lost yet is not proof), with one
      false-discovery correction across all rules. Crypto rules count only
      fill-checked trades (the fill gate was a no-op before Sep 25). EDGE needs
      100 events and survival of the correction.</p>
    <div class="scroll"><table id="rules"></table></div>
  </section>

  <div class="grid two">
    <section class="card">
      <div class="cardhead"><h2>News regime, last 14 days</h2>
        <button class="linkbtn" id="rg-toggle">Show table</button></div>
      <p class="cap" id="rg-cap"></p>
      <div class="legend" id="rg-legend"></div>
      <div id="rg-chart"></div>
      <div id="rg-table" hidden class="scroll"></div>
    </section>
    <section class="card health">
      <h2>System health</h2>
      <p class="cap" id="hl-cap"></p>
      <ul id="hl"></ul>
    </section>
  </div>

  <section class="card">
    <h2>Sleeve scanner nominations</h2>
    <p class="cap">Candidates only - each must be registered and earn its keep on forward paper.</p>
    <div class="scroll"><table id="noms"></table></div>
  </section>
</main>
<div class="tip" id="tip" role="tooltip"></div>

<script>
"use strict";
const SVGNS = "http://www.w3.org/2000/svg";
const STATUS = {
  good:     {label: "Good",      color: "var(--good)"},
  warning:  {label: "Watch",     color: "var(--warning)"},
  critical: {label: "Negative",  color: "var(--critical)"},
  neutral:  {label: "Collecting", color: "var(--muted)"},
};
const REGIME = {
  good:     {label: "Good day",     color: "var(--good)",    icon: "check"},
  cautious: {label: "Cautious day", color: "var(--warning)", icon: "alert"},
  unknown:  {label: "Unknown",      color: "var(--neutral-cell)", icon: "dash"},
};
let DATA = null, VENUE = "all";

function el(tag, attrs, text) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v);
  if (text !== undefined) e.textContent = text;
  return e;
}
function svg(tag, attrs) {
  const e = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v);
  return e;
}
function icon(kind, color) {
  const s = svg("svg", {viewBox: "0 0 16 16", class: "icon", "aria-hidden": "true"});
  s.appendChild(svg("circle", {cx: 8, cy: 8, r: 7, fill: color}));
  const ink = kind === "alert" ? "#0b0b0b" : "#ffffff";
  const d = {check: "M4.5 8.2l2.2 2.2 4.8-4.8", alert: "M8 4.2v4.6M8 11.2v.4",
             cross: "M5.2 5.2l5.6 5.6M10.8 5.2l-5.6 5.6", dash: "M5 8h6"}[kind];
  s.appendChild(svg("path", {d, stroke: ink, "stroke-width": 1.8, fill: "none",
                             "stroke-linecap": "round", "stroke-linejoin": "round"}));
  return s;
}
const usd = (v) => (v < 0 ? "-$" : "+$") + Math.abs(v).toFixed(2);
const cents = (v) => (v >= 0 ? "+" : "") + (v * 100).toFixed(1) + "¢";
const compact = (n) => n >= 10000 ? (n / 1000).toFixed(1) + "K" : n.toLocaleString();

// ── tooltip ──
const tip = document.getElementById("tip");
function showTip(x, y, rows, title) {
  tip.replaceChildren();
  if (title) tip.appendChild(el("div", {class: "n"}, title));
  for (const r of rows) {
    const row = el("div", {class: "row"});
    const left = el("span", {style: "display:inline-flex;align-items:center;gap:6px"});
    if (r.color) left.appendChild(el("span", {class: "key", style: `background:${r.color}`}));
    left.appendChild(el("span", {class: "n"}, r.name));
    row.appendChild(el("span", {class: "v"}, r.value));
    row.appendChild(left);
    tip.appendChild(row);
  }
  tip.style.display = "block";
  const w = tip.offsetWidth, h = tip.offsetHeight;
  tip.style.left = Math.min(window.innerWidth - w - 8, x + 14) + "px";
  tip.style.top = Math.max(8, y - h - 10) + "px";
}
function hideTip() { tip.style.display = "none"; }

// ── KPI row ──
function tile(label, value, note, opts) {
  const c = el("div", {class: "card" + (opts && opts.hero ? " hero" : "")});
  c.appendChild(el("div", {class: "label"}, label));
  const v = el("div", {class: "value"});
  if (opts && opts.node) v.appendChild(opts.node); else v.textContent = value;
  if (opts && opts.of) v.appendChild(el("span", {class: "of"}, opts.of));
  c.appendChild(v);
  if (note) c.appendChild(el("div", {class: "note"}, note));
  return c;
}
function renderKpis() {
  const k = DATA.kpi, box = document.getElementById("kpis");
  box.replaceChildren();
  box.appendChild(tile("Rules that have earned an edge", String(k.edge),
    k.edge ? "Survived 100+ events and the family correction - still paper until you decide."
           : "None yet. That is the honest number, and the reason live money stays off.",
    {hero: true, of: ` of ${k.rules}`}));
  box.appendChild(tile("Paper P&L, all rules", usd(k.net),
    `crypto ${usd(k.net_crypto)} · sleeves ${usd(k.net_sleeves)}`));
  box.appendChild(tile("Evidence collected", compact(k.windows + k.events),
    `${compact(k.windows)} crypto windows · ${compact(k.events)} sleeve events`));
  const rg = DATA.regime.current, meta = REGIME[rg.regime] || REGIME.unknown;
  const st = el("span", {class: "status"});
  st.appendChild(icon(meta.icon, meta.color));
  st.appendChild(document.createTextNode(meta.label));
  box.appendChild(tile("News regime now", "", `size ×${rg.mult ?? 0.5}`, {node: st}));
  const hl = DATA.health, ok = hl.state === "healthy";
  const hs = el("span", {class: "status"});
  hs.appendChild(icon(ok ? "check" : (hl.state === "unknown" ? "dash" : "cross"),
                      ok ? "var(--good)" : (hl.state === "unknown" ? "var(--neutral-cell)"
                                                                    : "var(--critical)")));
  hs.appendChild(document.createTextNode(ok ? "Healthy" : hl.state === "stale" ? "Stale"
                                         : hl.state === "unknown" ? "Unknown" : "Unhealthy"));
  box.appendChild(tile("System health", "", hl.ts ? `watchdog ${hl.ts.slice(0, 16)}Z` : "",
                       {node: hs}));
}

// ── equity chart: 2 series, one axis, crosshair tooltip ──
const SERIES = [{key: "crypto", name: "Crypto 15-min", color: "var(--s1)"},
                {key: "sleeves", name: "Sleeves", color: "var(--s2)"}];
function niceTicks(lo, hi, n) {
  const span = hi - lo || 1, raw = span / n, mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 5, 10].map(m => m * mag).find(s => span / s <= n) || 10 * mag;
  // ticks must ENCLOSE the data: floor the bottom AND ceil the top (rounding the top down
  // let the line run above the plot - caught by rendering it and looking)
  const out = [], top = Math.ceil(hi / step) * step;
  for (let v = Math.floor(lo / step) * step; v <= top + 1e-9; v += step) out.push(+v.toFixed(10));
  return out;
}
function renderEquity() {
  const box = document.getElementById("eq-chart"), legend = document.getElementById("eq-legend");
  box.replaceChildren(); legend.replaceChildren();
  const series = SERIES.filter(s => VENUE === "all" || VENUE === s.key)
                       .map(s => ({...s, pts: DATA.equity[s.key] || []}))
                       .filter(s => s.pts.length);
  if (!series.length) { box.appendChild(el("div", {class: "empty"}, "No closed paper trades yet.")); return; }
  if (series.length > 1) for (const s of series) {
    const sp = el("span"); sp.appendChild(el("span", {class: "key", style: `background:${s.color}`}));
    sp.appendChild(document.createTextNode(s.name)); legend.appendChild(sp);
  }
  const days = [...new Set(series.flatMap(s => s.pts.map(p => p[0])))].sort();
  const val = (s, d) => { let v = null; for (const p of s.pts) { if (p[0] <= d) v = p[1]; else break; } return v; };
  const all = series.flatMap(s => s.pts.map(p => p[1])).concat([0]);
  const ticks = niceTicks(Math.min(...all), Math.max(...all), 4);
  // wide: room for "Crypto 15-min -$808.09" so no end label is ever clipped; narrow
  // (phones): end dots only - the legend, crosshair tooltip and table view carry the rest
  const W = Math.max(300, box.clientWidth || 800), narrow = W < 560;
  const H = 240, L = 56, R = narrow ? 16 : 168, T = 10, B = 26;
  const x = (i) => L + (days.length < 2 ? 0 : i * (W - L - R) / (days.length - 1));
  const y0 = ticks[0], y1 = ticks[ticks.length - 1];
  const y = (v) => T + (H - T - B) * (1 - (v - y0) / ((y1 - y0) || 1));
  const s = svg("svg", {width: W, height: H, role: "img",
                        "aria-label": "Cumulative paper P&L by day"});
  for (const t of ticks) {
    s.appendChild(svg("line", {x1: L, x2: W - R, y1: y(t), y2: y(t),
      stroke: t === 0 ? "var(--axis)" : "var(--grid)", "stroke-width": 1}));
    const lab = svg("text", {x: L - 8, y: y(t) + 4, "text-anchor": "end"});
    lab.textContent = (t < 0 ? "-$" : "$") + Math.abs(t).toLocaleString();
    s.appendChild(lab);
  }
  for (const i of [0, Math.floor((days.length - 1) / 2), days.length - 1]) {
    const lab = svg("text", {x: x(i), y: H - 6, "text-anchor": i === 0 ? "start" : i === days.length - 1 ? "end" : "middle"});
    lab.textContent = days[i].slice(5); s.appendChild(lab);
  }
  const ends = [];
  for (const sr of series) {
    const d = days.map((day, i) => { const v = val(sr, day); return v === null ? null : [x(i), y(v)]; })
                  .filter(Boolean).map((p, j) => (j ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ");
    s.appendChild(svg("path", {d, fill: "none", stroke: sr.color, "stroke-width": 2,
                               "stroke-linejoin": "round", "stroke-linecap": "round"}));
    // cumulative P&L carries forward on days with no closes, so the series ends at the
    // RIGHT EDGE with its latest value - never mid-chart on top of its own line
    const lastV = val(sr, days[days.length - 1]);
    ends.push({sr, xy: [x(days.length - 1), y(lastV)], v: lastV});
  }
  ends.sort((a, b) => a.xy[1] - b.xy[1]);
  let prevY = -1e9;
  for (const e of ends) {
    s.appendChild(svg("circle", {cx: e.xy[0], cy: e.xy[1], r: 4, fill: e.sr.color,
                                 stroke: "var(--surface)", "stroke-width": 2}));
    if (narrow) continue;
    const ly = Math.max(e.xy[1] + 4, prevY + 14); prevY = ly;
    if (Math.abs(ly - e.xy[1] - 4) > 2)
      s.appendChild(svg("line", {x1: e.xy[0] + 6, x2: e.xy[0] + 10, y1: e.xy[1], y2: ly - 4,
                                 stroke: "var(--axis)", "stroke-width": 1}));
    const t = svg("text", {x: e.xy[0] + 12, y: ly});
    t.style.fill = "var(--ink-2)"; t.textContent = `${e.sr.name} ${usd(e.v)}`;
    s.appendChild(t);
  }
  const cross = svg("line", {y1: T, y2: H - B, stroke: "var(--axis)", "stroke-width": 1, visibility: "hidden"});
  s.appendChild(cross);
  const hit = svg("rect", {x: L, y: T, width: W - L - R, height: H - T - B, fill: "transparent", tabindex: 0});
  const at = (cx) => Math.max(0, Math.min(days.length - 1, Math.round((cx - L) / ((W - L - R) / Math.max(1, days.length - 1)))));
  function show(i, px, py) {
    cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("visibility", "visible");
    showTip(px, py, series.map(sr => ({name: sr.name, color: sr.color,
      value: val(sr, days[i]) === null ? "-" : usd(val(sr, days[i]))})), days[i]);
  }
  hit.addEventListener("pointermove", (ev) => { const r = s.getBoundingClientRect(); show(at(ev.clientX - r.left), ev.clientX, ev.clientY); });
  hit.addEventListener("pointerleave", () => { cross.setAttribute("visibility", "hidden"); hideTip(); });
  hit.addEventListener("focus", () => { const r = s.getBoundingClientRect(); show(days.length - 1, r.left + x(days.length - 1), r.top + 20); });
  hit.addEventListener("blur", () => { cross.setAttribute("visibility", "hidden"); hideTip(); });
  s.appendChild(hit);
  box.appendChild(s);
  const tb = el("table"), head = el("tr");
  head.appendChild(el("th", {}, "Day"));
  for (const sr of series) head.appendChild(el("th", {class: "num"}, sr.name));
  tb.appendChild(head);
  for (const d of days.slice().reverse()) {
    const tr = el("tr"); tr.appendChild(el("td", {}, d));
    for (const sr of series) { const v = val(sr, d); tr.appendChild(el("td", {class: "num"}, v === null ? "-" : usd(v))); }
    tb.appendChild(tr);
  }
  document.getElementById("eq-table").replaceChildren(tb);
}

// ── scoreboard ──
function spark(points) {
  const W = 96, H = 24, s = svg("svg", {width: W, height: H, "aria-hidden": "true"});
  if (!points || points.length < 2) return s;
  const lo = Math.min(0, ...points), hi = Math.max(0, ...points), span = (hi - lo) || 1;
  const px = (i) => 2 + i * (W - 8) / (points.length - 1), py = (v) => 2 + (H - 4) * (1 - (v - lo) / span);
  s.appendChild(svg("line", {x1: 2, x2: W - 6, y1: py(0), y2: py(0), stroke: "var(--grid)", "stroke-width": 1}));
  s.appendChild(svg("path", {d: points.map((v, i) => (i ? "L" : "M") + px(i).toFixed(1) + " " + py(v).toFixed(1)).join(" "),
                             fill: "none", stroke: "var(--deemph)", "stroke-width": 1.5}));
  s.appendChild(svg("circle", {cx: px(points.length - 1), cy: py(points[points.length - 1]), r: 3,
                               fill: "var(--s1)", stroke: "var(--surface)", "stroke-width": 1.5}));
  return s;
}
function ciBar(st, dom) {
  const W = 120, H = 16, s = svg("svg", {width: W, height: H, "aria-hidden": "true"});
  if (st.n < 2) return s;
  const x = (v) => 4 + (W - 8) * (Math.max(-dom, Math.min(dom, v)) + dom) / (2 * dom);
  s.appendChild(svg("line", {x1: x(0), x2: x(0), y1: 1, y2: H - 1, stroke: "var(--axis)", "stroke-width": 1}));
  s.appendChild(svg("line", {x1: x(st.lo), x2: x(st.hi), y1: H / 2, y2: H / 2, stroke: "var(--muted)",
                             "stroke-width": 2, "stroke-linecap": "round"}));
  s.appendChild(svg("circle", {cx: x(st.mean), cy: H / 2, r: 4, fill: "var(--s1)",
                               stroke: "var(--surface)", "stroke-width": 2}));
  return s;
}
function renderRules() {
  const t = document.getElementById("rules"); t.replaceChildren();
  const rows = DATA.rules.filter(r => VENUE === "all" || r.venue === VENUE);
  const head = el("tr");
  for (const [h, cls] of [["Rule", ""], ["Verdict", ""], ["Events", "num"], ["Win rate", "num"],
                          ["Edge/contract", "num"], ["95% interval", ""], ["Net $", "num"],
                          ["Trend", ""], ["All eras", "num"]]) head.appendChild(el("th", {class: cls}, h));
  t.appendChild(head);
  if (!rows.length) { const tr = el("tr"); const td = el("td", {colspan: 9, class: "empty"},
    "No rules here yet - register sleeve probes or wait for the first closed trades."); tr.appendChild(td); t.appendChild(tr); return; }
  const dom = Math.max(0.05, ...rows.filter(r => r.decision.n >= 2).flatMap(r => [Math.abs(r.decision.lo), Math.abs(r.decision.hi)]).filter(isFinite).map(v => Math.min(v, 1)));
  for (const r of rows) {
    const d = r.decision, a = r.all, tr = el("tr");
    const name = el("td"); name.appendChild(el("div", {class: "rule"}, r.rule));
    name.appendChild(el("div", {class: "venue"}, r.venue === "crypto" ? "crypto 15-min" : (r.series || "sleeve")));
    tr.appendChild(name);
    const vd = el("td"), vs = el("span", {class: "status", style: "font-weight:500"});
    const meta = STATUS[r.status];
    vs.appendChild(icon({good: "check", warning: "alert", critical: "cross", neutral: "dash"}[r.status],
                        r.status === "neutral" ? "var(--neutral-cell)" : meta.color));
    vs.appendChild(document.createTextNode(r.verdict)); vd.appendChild(vs); tr.appendChild(vd);
    tr.appendChild(el("td", {class: "num"}, String(d.n)));
    tr.appendChild(el("td", {class: "num"}, d.wr === null ? "-" : (d.wr * 100).toFixed(0) + "%"));
    tr.appendChild(el("td", {class: "num"}, d.n ? cents(d.mean) : "-"));
    const ci = el("td"); ci.appendChild(ciBar(d, dom));
    if (d.n >= 2) ci.title = `${cents(d.lo)} to ${cents(d.hi)} per contract per event`;
    tr.appendChild(ci);
    tr.appendChild(el("td", {class: "num"}, usd(d.net)));
    const sp = el("td"); sp.appendChild(spark(r.spark)); tr.appendChild(sp);
    tr.appendChild(el("td", {class: "num"}, a.n ? `${cents(a.mean)} · ${a.n}` : "-"));
    t.appendChild(tr);
  }
}

// ── regime strip ──
function renderRegime() {
  const rg = DATA.regime, cur = rg.current;
  document.getElementById("rg-cap").textContent = cur.why ? `Now: ${cur.why}` : "";
  const legend = document.getElementById("rg-legend"); legend.replaceChildren();
  for (const k of ["good", "cautious", "unknown"]) {
    const sp = el("span"); sp.appendChild(icon(REGIME[k].icon, REGIME[k].color));
    sp.appendChild(document.createTextNode(REGIME[k].label)); legend.appendChild(sp);
  }
  const box = document.getElementById("rg-chart"); box.replaceChildren();
  if (!rg.cells.length) { box.appendChild(el("div", {class: "empty"}, "The news agent has not run yet.")); return; }
  const days = [...new Set(rg.cells.map(c => c.d))].sort().slice(-14);
  const gap = 2, L = 44, T = 4, avail = (box.clientWidth || 380) - L;
  const cell = Math.max(6, Math.min(12, Math.floor(avail / 24) - gap));   // fits a phone
  const W = L + 24 * (cell + gap), H = T + days.length * (cell + gap) + 16;
  const s = svg("svg", {width: W, height: H, role: "img", "aria-label": "Hourly news regime, last 14 days"});
  const map = new Map(rg.cells.map(c => [c.d + "|" + c.h, c.r]));
  days.forEach((d, i) => {
    const lab = svg("text", {x: L - 6, y: T + i * (cell + gap) + cell - 2, "text-anchor": "end"});
    lab.textContent = d.slice(5); s.appendChild(lab);
    for (let h = 0; h < 24; h++) {
      const r = map.get(d + "|" + h);
      const rect = svg("rect", {x: L + h * (cell + gap), y: T + i * (cell + gap), width: cell, height: cell, rx: 2,
                                fill: r ? REGIME[r].color : "transparent",
                                stroke: r ? "none" : "var(--grid)", "stroke-width": 1});
      if (r) {
        rect.addEventListener("pointermove", (ev) => showTip(ev.clientX, ev.clientY,
          [{name: `${String(h).padStart(2, "0")}:00 ET`, value: REGIME[r].label}], d));
        rect.addEventListener("pointerleave", hideTip);
      }
      s.appendChild(rect);
    }
  });
  for (const h of [0, 6, 12, 18]) {
    const lab = svg("text", {x: L + h * (cell + gap), y: H - 2}); lab.textContent = String(h).padStart(2, "0"); s.appendChild(lab);
  }
  box.appendChild(s);
  const tb = el("table"), hd = el("tr");
  for (const h of ["Day (ET)", "Good hours", "Cautious hours", "Unknown hours"]) hd.appendChild(el("th", {}, h));
  tb.appendChild(hd);
  for (const d of days.slice().reverse()) {
    const tr = el("tr"); tr.appendChild(el("td", {}, d));
    for (const k of ["good", "cautious", "unknown"])
      tr.appendChild(el("td", {class: "num"}, String(rg.cells.filter(c => c.d === d && c.r === k).length)));
    tb.appendChild(tr);
  }
  document.getElementById("rg-table").replaceChildren(tb);
}

// ── health + nominations ──
function renderHealth() {
  const h = DATA.health;
  document.getElementById("hl-cap").textContent = h.ts ? `Watchdog verdict at ${h.ts.slice(0, 16)}Z` : "";
  const ul = document.getElementById("hl"); ul.replaceChildren();
  const lines = h.lines.length ? h.lines : (h.state === "healthy" ? ["Every check passed."] : []);
  for (const line of lines) {
    const li = el("li"), fail = line.includes("[FAIL]"), warn = line.includes("[WARN]");
    li.appendChild(icon(fail ? "cross" : warn ? "alert" : "check",
                        fail ? "var(--critical)" : warn ? "var(--warning)" : "var(--good)"));
    li.appendChild(el("span", {}, line.replace(/^\[(FAIL|WARN|OK  )\]\s*/, "")));
    ul.appendChild(li);
  }
}
function renderNoms() {
  const t = document.getElementById("noms"); t.replaceChildren();
  const hd = el("tr");
  for (const [h, c] of [["Sleeve", ""], ["Band", ""], ["Side", ""], ["Price", ""], ["Events out", "num"],
                        ["$/event out", "num"], ["Trade-confirmed", "num"], ["Verdict", ""]]) hd.appendChild(el("th", {class: c}, h));
  t.appendChild(hd);
  if (!DATA.nominations.length) {
    const tr = el("tr"); tr.appendChild(el("td", {colspan: 8, class: "empty"},
      "No nominations - run the sleeve scanner (discover, backfill, screen)."));
    t.appendChild(tr); return;
  }
  for (const n of DATA.nominations) {
    const tr = el("tr");
    for (const v of [n.series, n.band, n.side, n.bucket]) tr.appendChild(el("td", {}, String(v)));
    tr.appendChild(el("td", {class: "num"}, String(n.n_out)));
    tr.appendChild(el("td", {class: "num"}, cents(n.mean_out)));
    tr.appendChild(el("td", {class: "num"}, n.mean_out_traded === null ? "none" : cents(n.mean_out_traded)));
    const vd = el("td"), clean = n.verdict === "NOMINATED", s = el("span", {class: "status", style: "font-weight:500"});
    s.appendChild(icon(clean ? "check" : "alert", clean ? "var(--good)" : "var(--warning)"));
    s.appendChild(document.createTextNode(clean ? "Clean - register it" : "Phantom risk - not traded"));
    vd.appendChild(s); tr.appendChild(vd); t.appendChild(tr);
  }
}

function render() {
  if (!DATA) return;
  const live = document.getElementById("live"); live.replaceChildren();
  live.appendChild(icon("dash", "var(--neutral-cell)"));
  live.appendChild(document.createTextNode(`Live money ${DATA.live_money} - paper only`));
  document.getElementById("gen").textContent = `updated ${DATA.generated.slice(11, 19)}Z · refreshes every minute`;
  renderKpis(); renderEquity(); renderRules(); renderRegime(); renderHealth(); renderNoms();
}
async function load() {
  const app = document.getElementById("app");
  app.classList.add("loading");                       // hold the frame; no skeleton flash
  try {
    const r = await fetch("/api/command", {cache: "no-store"});
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    DATA = d; render();
  } catch (e) {
    document.getElementById("gen").textContent = `refresh failed (${String(e).slice(0, 80)}) - showing last data`;
  } finally { app.classList.remove("loading"); }
}
document.querySelectorAll("#venue button").forEach(b => b.addEventListener("click", () => {
  VENUE = b.dataset.v;
  document.querySelectorAll("#venue button").forEach(x => x.setAttribute("aria-pressed", String(x === b)));
  render();
}));
for (const [btn, chart, table] of [["eq-toggle", "eq-chart", "eq-table"], ["rg-toggle", "rg-chart", "rg-table"]]) {
  document.getElementById(btn).addEventListener("click", (ev) => {
    const t = document.getElementById(table), c = document.getElementById(chart), showing = t.hidden;
    t.hidden = !showing; c.hidden = showing; ev.target.textContent = showing ? "Show chart" : "Show table";
  });
}
window.addEventListener("resize", () => { clearTimeout(window._rz); window._rz = setTimeout(render, 150); });
load(); setInterval(load, 60000);
</script>
</body>
</html>
"""
