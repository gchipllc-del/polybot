#!/usr/bin/env python3
"""Dedicated weather-fade dashboard — shows ONLY the weather_fade paper sleeve,
reading the right ledgers (the main poly dash reads the old sleeve's files and
doesn't know weather_fade exists). Self-contained Flask app, server-rendered,
auto-refreshes every 30s. Reads:

  data/weather_fade_paper.jsonl       — the fades (scorecard + per-city)
  data/weather_fade_book_probe.jsonl  — liquidity probe (by-hour)
  data/hourly_weather_collect.jsonl   — hourly forward-collection count

Run:  python scripts/weather_fade_dash.py            # http://127.0.0.1:5060
      python scripts/weather_fade_dash.py --port 5061
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from weather_fade import (_load_ledger, _city_of, LEDGER, COLLECT_LEDGER,  # noqa: E402
                          VALIDATED_SERIES)

PROBE_LOG = ROOT / "data" / "weather_fade_book_probe.jsonl"
SCAN_STATUS = ROOT / "data" / "weather_fade_scan_status.json"


def _read_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    out = []
    with open(p) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out


def build_summary() -> dict:
    """All weather_fade metrics in one dict — pure, testable."""
    rows = _load_ledger()
    closed = [r for r in rows if r.get("status") in ("won", "lost")]
    openn = [r for r in rows if r.get("status") == "open"]
    won = sum(1 for r in closed if r["status"] == "won")
    net = sum(float(r.get("paper_pnl", 0) or 0) for r in closed)
    inv = sum(float(r.get("notional", 0) or 0) for r in closed)
    val_cities = {_city_of(s + "-x") for s in VALIDATED_SERIES}

    by_city = {}
    for r in closed:
        c = _city_of(r.get("market_ticker", ""))
        b = by_city.setdefault(c, {"w": 0, "l": 0, "net": 0.0})
        b["w" if r["status"] == "won" else "l"] += 1
        b["net"] += float(r.get("paper_pnl", 0) or 0)
    city_rows = []
    for c, b in sorted(by_city.items(), key=lambda kv: kv[1]["net"]):
        n = b["w"] + b["l"]
        city_rows.append({"city": c, "w": b["w"], "l": b["l"], "net": round(b["net"], 2),
                          "wr": round(b["w"] / n * 100, 0) if n else 0,
                          "new": c not in val_cities})

    # liquidity: latest probe run's takeable/total + by-hour takeable
    probe = _read_jsonl(PROBE_LOG)
    byhr_tot = collections.Counter(); byhr_tak = collections.Counter()
    for r in probe:
        h = str(r.get("ts", ""))[11:13]
        if h:
            byhr_tot[h] += 1
            byhr_tak[h] += 1 if r.get("takeable") else 0
    byhr = [{"hr": h, "tak": byhr_tak[h], "tot": byhr_tot[h]} for h in sorted(byhr_tot)]

    # scan health: last run + how stale (stale ⇒ Mac asleep / agent not firing)
    scan = {}
    if SCAN_STATUS.exists():
        try:
            scan = json.loads(SCAN_STATUS.read_text())
        except Exception:
            scan = {}
    age_min = None
    if scan.get("ts"):
        try:
            age_min = int((datetime.now(timezone.utc)
                           - datetime.fromisoformat(scan["ts"].replace("Z", "+00:00"))
                           ).total_seconds() / 60)
        except Exception:
            age_min = None
    scan["age_min"] = age_min

    return {
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"),
        "scan": scan,
        "open": len(openn), "closed": len(closed), "won": won, "lost": len(closed) - won,
        "wr": round(won / len(closed) * 100, 1) if closed else None,
        "net": round(net, 2), "invested": round(inv, 2),
        "roi": round(net / inv * 100, 1) if inv else None,
        "open_fades": sorted(openn, key=lambda r: r.get("opened_at", ""), reverse=True)[:25],
        "recent_settled": sorted(closed, key=lambda r: r.get("resolved_at", ""), reverse=True)[:15],
        "per_city": city_rows,
        "by_hour": byhr,
        "hourly_collected": len(_read_jsonl(COLLECT_LEDGER)),
    }


def _cls(v):
    try:
        return "pos" if float(v) > 0 else ("neg" if float(v) < 0 else "")
    except (TypeError, ValueError):
        return ""


def _scan_panel(sc: dict) -> str:
    """Health banner: is the scan actually firing, and what is it finding?
    A stale age or all-empty books explains an empty scorecard at a glance."""
    if not sc:
        return ("<div class=warn>⚠ scan hasn't run yet — start the scan agent "
                "(no <code>weather_fade_scan_status.json</code> yet).</div>")
    age = sc.get("age_min")
    if age is None:
        fresh, note = "dim", "age unknown"
    elif age <= 45:
        fresh, note = "pos", f"{age} min ago"
    else:
        fresh, note = "warn", (f"{age} min ago — STALE. The scan isn't firing "
                               f"(Mac asleep during the liquid window?).")
    booked = sc.get("booked", 0)
    why = ""
    if sc.get("day_ahead", 0) and not booked:
        bits = []
        if sc.get("empty_book"): bits.append(f"{sc['empty_book']} empty books")
        if sc.get("one_sided"): bits.append(f"{sc['one_sided']} one-sided")
        if not sc.get("in_band"): bits.append("0 in tradeable band")
        elif not sc.get("qualified"): bits.append(f"{sc['in_band']} in-band but none overpriced enough")
        why = " · booked 0 because: " + (", ".join(bits) if bits else "no qualifiers")
    return (f"<div class={fresh}>⏱ last scan <b>{note}</b> · "
            f"{sc.get('open_markets',0)} open mkts → {sc.get('day_ahead',0)} day-ahead → "
            f"{sc.get('in_band',0)} in-band → <b>{booked} booked</b> "
            f"(thr {sc.get('thr','?')}){why}</div>")


def render_html(s: dict) -> str:
    def money(v):
        return "—" if v is None else f"${v:+,.2f}"
    city_html = "".join(
        f"<tr><td>{c['city']}{' <span class=new>*new</span>' if c['new'] else ''}</td>"
        f"<td>{c['w']}W/{c['l']}L</td><td>{c['wr']:.0f}%</td>"
        f"<td class={_cls(c['net'])}>{c['net']:+.2f}</td></tr>"
        for c in s["per_city"]) or "<tr><td colspan=4 class=dim>no settled fades yet</td></tr>"
    open_html = "".join(
        f"<tr><td>{r.get('market_ticker','')}</td><td>{r.get('fill_price','')}</td>"
        f"<td>{r.get('our_size','')}</td><td>{r.get('edge','')}</td>"
        f"<td class=dim>{str(r.get('opened_at',''))[:16]}</td></tr>"
        for r in s["open_fades"]) or "<tr><td colspan=5 class=dim>none open</td></tr>"
    settled_html = "".join(
        f"<tr><td>{r.get('market_ticker','')}</td><td>{r.get('result','')}</td>"
        f"<td class={_cls(r.get('paper_pnl'))}>{float(r.get('paper_pnl',0) or 0):+.2f}</td>"
        f"<td class=dim>{str(r.get('resolved_at',''))[:16]}</td></tr>"
        for r in s["recent_settled"]) or "<tr><td colspan=4 class=dim>none settled yet</td></tr>"
    hr_html = "".join(
        f"<span class=hr>{h['hr']}:00 <b>{h['tak']}</b>/{h['tot']}</span>"
        for h in s["by_hour"]) or "<span class=dim>no probe data yet</span>"
    return f"""<!doctype html><html><head><meta charset=utf-8>
<meta http-equiv=refresh content=30>
<title>weather-fade</title><style>
body{{background:#0d1117;color:#c9d1d9;font-family:Menlo,monospace;font-size:14px;max-width:980px;margin:0 auto;padding:20px}}
h1{{color:#ffd700;font-size:20px}} h2{{color:#58a6ff;font-size:13px;text-transform:uppercase;margin-top:22px}}
.big{{font-size:28px;font-weight:bold}} .pos{{color:#3fb950}} .neg{{color:#f85149}} .dim{{color:#8b949e}}
.warn{{color:#d29922}} div.pos,div.warn,div.dim{{border:1px solid #30363d;border-radius:6px;padding:8px 12px;margin:10px 0;background:#161b22}}
.new{{color:#d29922;font-size:11px}} .hr{{display:inline-block;margin:2px 10px 2px 0}}
table{{width:100%;border-collapse:collapse;margin-top:6px}} td,th{{text-align:left;padding:5px 6px;border-bottom:1px solid #21262d}}
th{{color:#8b949e;font-size:11px}} .stat{{display:inline-block;margin-right:32px}} .lbl{{color:#8b949e;font-size:12px}}
</style></head><body>
<h1>🌡️ weather-fade paper sleeve <span class=dim style=font-size:12px>as of {s['as_of']} · auto-refresh 30s</span></h1>
{_scan_panel(s.get('scan') or {})}
<div>
 <span class=stat><div class=lbl>Net P&amp;L (settled)</div><div class="big {_cls(s['net'])}">{money(s['net'])}</div></span>
 <span class=stat><div class=lbl>ROI</div><div class=big>{'—' if s['roi'] is None else f"{s['roi']:+.1f}%"}</div></span>
 <span class=stat><div class=lbl>Win rate</div><div class=big>{'—' if s['wr'] is None else f"{s['wr']:.0f}%"}</div></span>
 <span class=stat><div class=lbl>Settled</div><div class=big>{s['closed']}</div><div class=dim>{s['won']}W/{s['lost']}L</div></span>
 <span class=stat><div class=lbl>Open</div><div class=big>{s['open']}</div></span>
 <span class=stat><div class=lbl>Invested</div><div class=big>${s['invested']:,.2f}</div></span>
</div>
<p class=dim>↑ the real-fill verdict — judge the edge by this, not the backtest.</p>
<h2>Per-city (settled) — *new = outside the validated 8</h2>
<table><tr><th>City</th><th>W/L</th><th>WR</th><th>Net $</th></tr>{city_html}</table>
<h2>Open fades ({s['open']})</h2>
<table><tr><th>Ticker</th><th>Fill</th><th>Size</th><th>Edge</th><th>Opened</th></tr>{open_html}</table>
<h2>Recently settled</h2>
<table><tr><th>Ticker</th><th>Result</th><th>P&amp;L</th><th>Settled</th></tr>{settled_html}</table>
<h2>Liquidity by hour (takeable / total) — find the live window</h2>
<div>{hr_html}</div>
<h2>Hourly-weather collection</h2>
<p>{s['hourly_collected']} rows gathered <span class=dim>(forward data for a separate hourly-edge backtest)</span></p>
</body></html>"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=5060)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    from flask import Flask
    app = Flask(__name__)

    @app.route("/")
    def index():
        return render_html(build_summary())

    print(f"weather-fade dashboard → http://{args.host}:{args.port}  (Ctrl-C to stop)")
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
