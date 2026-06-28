#!/usr/bin/env python3
"""Dedicated bucket-arb dashboard — its OWN scoreboard with its OWN paper
bankroll, deliberately separate from the weather sleeve so the two edges'
results never get confused. Reads:

  data/bucket_arb_paper.jsonl     — paper baskets booked against the arb bankroll
  data/bucket_arb_collect.jsonl   — every ladder's near-miss margin (the thing
                                    that actually accumulates day to day)

Run:  python scripts/bucket_arb_dash.py            # file -> open with open(1)
      python scripts/bucket_arb_dash.py serve       # http://127.0.0.1:5053
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from bucket_arb import LEDGER, COLLECT_LOG, DEFAULT_BANKROLL  # noqa: E402


def _read_jsonl(p: Path) -> list:
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def _near_miss(rows: list) -> dict:
    """Distribution of NO/YES sweep margins (cents) — the live read on whether
    ladders ever approach exploitable."""
    out = {}
    for key, label in (("no_margin_cents", "no"), ("yes_margin_cents", "yes")):
        xs = [r[key] for r in rows if r.get(key) is not None]
        if not xs:
            out[label] = None
            continue
        xs_sorted = sorted(xs)
        out[label] = {"n": len(xs), "best": max(xs),
                      "median": xs_sorted[len(xs) // 2],
                      "locked": sum(1 for x in xs if x > 0),
                      "near5": sum(1 for x in xs if -5 <= x <= 0)}
    return out


def build_summary() -> dict:
    """All bucket-arb metrics in one pure, testable dict."""
    rows = _read_jsonl(LEDGER)
    openn = [r for r in rows if r.get("status") == "open"]
    closed = [r for r in rows if r.get("status") in ("won", "lost")]
    won = sum(1 for r in closed if r["status"] == "won")
    net = sum(float(r.get("paper_pnl", 0) or 0) for r in closed)
    at_risk = sum(float(r.get("cost_cents", 0) or 0) for r in openn) / 100.0
    coll = _read_jsonl(COLLECT_LOG)
    return {
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"),
        "bankroll": round(DEFAULT_BANKROLL, 2),
        "portfolio": round(DEFAULT_BANKROLL + net, 2),
        "net": round(net, 2),
        "open": len(openn), "closed": len(closed),
        "won": won, "lost": len(closed) - won,
        "wr": round(won / len(closed) * 100, 1) if closed else None,
        "at_risk": round(at_risk, 2),
        "open_baskets": sorted(openn, key=lambda r: r.get("opened_at", ""), reverse=True)[:20],
        "recent_settled": sorted(closed, key=lambda r: r.get("resolved_at", ""), reverse=True)[:15],
        "collected": len(coll),
        "near_miss": _near_miss(coll),
    }


def _cls(v):
    try:
        return "pos" if float(v) > 0 else ("neg" if float(v) < 0 else "")
    except (TypeError, ValueError):
        return ""


def _nm_panel(nm: dict) -> str:
    def one(label, d, note):
        if not d:
            return f"<tr><td>{label}</td><td colspan=4 class=dim>never executable yet</td></tr>"
        return (f"<tr><td>{label}<br><span class=dim style=font-size:11px>{note}</span></td>"
                f"<td class={_cls(d['best'])}>{d['best']:+d}¢</td>"
                f"<td>{d['median']:+d}¢</td><td>{d['locked']}</td><td>{d['near5']}</td></tr>")
    return (one("NO-sweep", nm.get("no"), "robust — needs only mutual-exclusivity")
            + one("YES-sweep", nm.get("yes"), "⚠ needs exhaustive ladder"))


def render_html(s: dict) -> str:
    def money(v):
        return "—" if v is None else f"${v:+,.2f}"
    open_html = "".join(
        f"<tr><td>{r.get('event','')}</td><td>{r.get('type','')}</td>"
        f"<td>{r.get('legs','')}×{r.get('size','')}</td>"
        f"<td>${r.get('cost_cents',0)/100:.2f}</td>"
        f"<td class=pos>+${r.get('expected_profit_cents',0)/100:.2f}</td>"
        f"<td class=dim>{str(r.get('opened_at',''))[:16]}</td></tr>"
        for r in s["open_baskets"]) or "<tr><td colspan=6 class=dim>none open</td></tr>"
    settled_html = "".join(
        f"<tr><td>{r.get('event','')}</td><td>{r.get('type','')}</td>"
        f"<td>{r.get('winning_legs','')}/{r.get('legs','')} won</td>"
        f"<td class={_cls(r.get('paper_pnl'))}>{float(r.get('paper_pnl',0) or 0):+.2f}</td>"
        f"<td class=dim>{str(r.get('resolved_at',''))[:16]}</td></tr>"
        for r in s["recent_settled"]) or "<tr><td colspan=5 class=dim>none settled yet</td></tr>"
    return f"""<!doctype html><html><head><meta charset=utf-8>
<meta http-equiv=refresh content=30>
<title>bucket-arb</title><style>
body{{background:#0d1117;color:#c9d1d9;font-family:Menlo,monospace;font-size:14px;max-width:980px;margin:0 auto;padding:20px}}
h1{{color:#2dd4bf;font-size:20px}} h2{{color:#58a6ff;font-size:13px;text-transform:uppercase;margin-top:22px}}
.big{{font-size:28px;font-weight:bold}} .pos{{color:#3fb950}} .neg{{color:#f85149}} .dim{{color:#8b949e}}
.warn{{color:#d29922}} div.note{{border:1px solid #30363d;border-radius:6px;padding:8px 12px;margin:10px 0;background:#161b22}}
table{{width:100%;border-collapse:collapse;margin-top:6px}} td,th{{text-align:left;padding:5px 6px;border-bottom:1px solid #21262d}}
th{{color:#8b949e;font-size:11px}} .stat{{display:inline-block;margin-right:32px}} .lbl{{color:#8b949e;font-size:12px}}
</style></head><body>
<h1>🧮 bucket-arb paper sleeve <span class=dim style=font-size:12px>as of {s['as_of']} · auto-refresh 30s</span></h1>
<div class=note>SEPARATE bankroll from weather — structural (prediction-free) ladder arbitrage.
Books only <b>NO-sweeps</b> by default (guaranteed by mutual-exclusivity); YES-sweeps need an
exhaustive ladder. <b>Read-only / paper</b> — places no real orders.</div>
<div>
 <span class=stat><div class=lbl>Net P&amp;L (settled)</div><div class="big {_cls(s['net'])}">{money(s['net'])}</div></span>
 <span class=stat><div class=lbl>Win rate</div><div class=big>{'—' if s['wr'] is None else f"{s['wr']:.0f}%"}</div></span>
 <span class=stat><div class=lbl>Settled</div><div class=big>{s['closed']}</div><div class=dim>{s['won']}W/{s['lost']}L</div></span>
 <span class=stat><div class=lbl>Open</div><div class=big>{s['open']}</div><div class=dim>${s['at_risk']:.2f} at risk</div></span>
 <span class=stat><div class=lbl>Bankroll (own)</div><div class=big>${s['bankroll']:,.2f}</div><div class=dim>portfolio ${s['portfolio']:,.2f}</div></span>
</div>
<h2>Near-miss distribution — {s['collected']} ladder observations collected</h2>
<p class=dim>How close ladders actually get to a locked arb. Best stuck well below 0 over many days ⇒ lane is dead. Repeatedly &gt;0 AND fillable ⇒ real.</p>
<table><tr><th>Sweep</th><th>Best</th><th>Median</th><th>Locked(&gt;0)</th><th>Within 5¢</th></tr>{_nm_panel(s['near_miss'])}</table>
<h2>Open baskets ({s['open']})</h2>
<table><tr><th>Event</th><th>Type</th><th>Legs×Size</th><th>Cost</th><th>Exp. profit</th><th>Opened</th></tr>{open_html}</table>
<h2>Recently settled</h2>
<table><tr><th>Event</th><th>Type</th><th>Legs won</th><th>P&amp;L</th><th>Settled</th></tr>{settled_html}</table>
<p class=dim>NOTE: margins are top-of-book — a lock is only real if every leg is fillable at depth.</p>
</body></html>"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", nargs="?", default="render", choices=["serve", "render"],
                    help="render = write HTML FILE (open directly); serve = live link")
    ap.add_argument("--out", default=str(ROOT / "data" / "bucket_arb_dash.html"))
    ap.add_argument("--port", type=int, default=5053)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    if args.mode == "render":
        try:
            html = render_html(build_summary())
        except Exception:
            import traceback
            html = ("<!doctype html><body style='background:#0d1117;color:#f85149;"
                    "font-family:monospace;padding:20px'><pre>"
                    + traceback.format_exc() + "</pre></body>")
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(html)
        print(f"wrote dashboard -> {args.out}\nopen it with:  open {args.out}")
        return

    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    def _page() -> bytes:
        try:
            return render_html(build_summary()).encode("utf-8")
        except Exception:
            import traceback
            return ("<!doctype html><meta http-equiv=refresh content=15>"
                    "<body style='background:#0d1117;color:#f85149;font-family:monospace;"
                    f"padding:20px'><pre style='color:#c9d1d9;white-space:pre-wrap'>"
                    f"{traceback.format_exc()}</pre></body>").encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            body = _page()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"bucket-arb dashboard → http://{args.host}:{args.port}  (http, not https; Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
