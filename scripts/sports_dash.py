#!/usr/bin/env python3
"""sports_dash — live scorecard for the two sports sleeves (sports_lock + devig_check).

Reuses sports_eval's scoring (CACHED resolutions only — no network, so the page is fast
and read-only; the eval agent refreshes the cache separately). For each sleeve it shows
settled/pending counts, gross+net mean return, per-day PSR / DSR / MinTRL, and the
win-prob calibration (Brier + reliability table) — plus the most recent signals.

Run:  python scripts/sports_dash.py             # writes HTML file
      python scripts/sports_dash.py serve        # http://127.0.0.1:5056
      python scripts/sports_dash.py --trials 12
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from sports_eval import (LOGS, load_signals, build_trades,  # noqa: E402
                         score_trades, _load_resolutions)

OUT = ROOT / "data" / "sports_dash.html"


def _recent_signals(path: Path, k: int = 8) -> list:
    """Last k raw signal records from a sleeve's JSONL (newest first)."""
    if not path.exists():
        return []
    recs = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return recs[-k:][::-1]


def _verdict(s: dict) -> str:
    if s["days"] < 5 or s["psr_net"] is None:
        return "collecting…"
    if s["dsr_net"] and s["dsr_net"] >= 0.95 and (s["mean_ret_net"] or 0) > 0:
        return "REAL (DSR≥0.95)"
    if s["psr_net"] < 0.5:
        return "no edge / efficient"
    return "provisional — keep collecting"


def build_summary(trials: int) -> dict:
    resolutions = _load_resolutions()
    sleeves = {}
    for key in ("lock", "devig"):
        sigs = load_signals(LOGS[key])
        trades, pending = build_trades(sigs, resolutions, refresh=False)  # cache only
        scored = score_trades(trades, trials) if trades else None
        sleeves[key] = {
            "n_markets": len(sigs), "pending": len(pending),
            "scored": scored, "recent": _recent_signals(LOGS[key]),
        }
    return {
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"),
        "trials": trials, "sleeves": sleeves,
    }


def _cls(v):
    try:
        return "pos" if float(v) > 0 else ("neg" if float(v) < 0 else "")
    except (TypeError, ValueError):
        return ""


def _pct(v):
    return "—" if v is None else f"{v*100:.0f}%"


def _sleeve_html(name: str, blurb: str, d: dict, trials: int) -> str:
    s = d["scored"]
    head = (f"<h2>{name} <span class=dim>· {d['n_markets']} markets seen · "
            f"{d['pending'] if d['pending'] else 0} pending</span></h2>"
            f"<div class=note>{blurb}</div>")
    if not s:
        body = "<div class=dim>no settled trades yet — collect, then the eval agent fills resolutions.</div>"
    else:
        cal = s["calibration"]
        bins = "".join(
            f"<tr><td>{lo:.2f}–{hi:.2f}</td><td>{n}</td><td>{mp:.3f}</td>"
            f"<td>{ar:.3f}</td><td class={_cls(ar-mp)}>{ar-mp:+.3f}</td></tr>"
            for lo, hi, n, mp, ar in cal["bins"]) or "<tr><td colspan=5 class=dim>—</td></tr>"
        psr = "n<5" if s["psr_net"] is None else _pct(s["psr_net"])
        dsr = "n<5" if s["dsr_net"] is None else _pct(s["dsr_net"])
        mt = s["mintrl_net"]
        mt_s = ("∞" if mt == float("inf") else "n<5" if mt is None else f"{mt:.0f}d")
        body = f"""<div>
 <span class=stat><div class=lbl>Settled</div><div class=big>{s['n_trades']}</div><div class=dim>{s['days']} day(s)</div></span>
 <span class=stat><div class=lbl>Hit-rate</div><div class=big>{_pct(s['hit_rate'])}</div></span>
 <span class=stat><div class=lbl>Mean ret (net)</div><div class="big {_cls(s['mean_ret_net'])}">{('—' if s['mean_ret_net'] is None else f"{s['mean_ret_net']:+.3f}")}</div><div class=dim>gross {('—' if s['mean_ret_gross'] is None else f"{s['mean_ret_gross']:+.3f}")}</div></span>
 <span class=stat><div class=lbl>Brier</div><div class=big>{('—' if cal['brier'] is None else f"{cal['brier']:.3f}")}</div><div class=dim>0.25=coin</div></span>
</div>
<div class=note>per-day <b>PSR(net) {psr}</b> · <b>DSR(@{trials}) {dsr}</b> · MinTRL {mt_s} → <b>{_verdict(s)}</b></div>
<table><tr><th>predicted</th><th>n</th><th>mean_pred</th><th>actual</th><th>gap</th></tr>{bins}</table>
<p class=dim>gap = actual − predicted. Big gaps (⚠ in eval) = the win-prob is mis-calibrated there.</p>"""
    rec = "".join(
        f"<tr><td>{str(r.get('ts',''))[5:16]}</td><td>{r.get('league','')}</td>"
        f"<td>{(r.get('game') or '')[:30]}</td><td>{r.get('side','')}</td>"
        f"<td>{r.get('edge', r.get('net_edge',''))}</td></tr>"
        for r in d["recent"]) or "<tr><td colspan=5 class=dim>no signals logged yet</td></tr>"
    recent = (f"<table><tr><th>when</th><th>lg</th><th>game</th><th>side</th><th>edge</th></tr>"
              f"{rec}</table>")
    return head + body + "<p class=lbl>recent signals</p>" + recent


def render_html(s: dict) -> str:
    sl = _sleeve_html("🔒 sports_lock", "Garbage-time LOCK: near-decided game still mispriced "
                      "on Kalshi. Predicted = model win-prob. Net = after Kalshi taker fee.",
                      s["sleeves"]["lock"], s["trials"])
    dv = _sleeve_html("⚖️ devig_check", "Whole-game DEVIG: sharp-book fair price vs Kalshi YES. "
                      "Predicted = devigged fair prob of the side bought.",
                      s["sleeves"]["devig"], s["trials"])
    return f"""<!doctype html><html><head><meta charset=utf-8>
<meta http-equiv=refresh content=30><title>sports sleeves</title><style>
body{{background:#0d1117;color:#c9d1d9;font-family:Menlo,monospace;font-size:14px;max-width:980px;margin:0 auto;padding:20px}}
h1{{color:#c084fc;font-size:20px}} h2{{color:#58a6ff;font-size:14px;margin-top:26px}}
.big{{font-size:26px;font-weight:bold}} .pos{{color:#3fb950}} .neg{{color:#f85149}} .dim{{color:#8b949e}}
.note{{border:1px solid #30363d;border-radius:6px;padding:8px 12px;margin:10px 0;background:#161b22}}
table{{width:100%;border-collapse:collapse;margin-top:6px}} td,th{{text-align:left;padding:5px 6px;border-bottom:1px solid #21262d}}
th{{color:#8b949e;font-size:11px}} .stat{{display:inline-block;margin-right:32px}} .lbl{{color:#8b949e;font-size:12px;margin-top:14px}}
</style></head><body>
<h1>🏟️ sports sleeves <span class=dim style=font-size:12px>as of {s['as_of']} · auto-refresh 30s · paper only</span></h1>
<div class=note>Two paper edges, scored on Kalshi settlement (cached). Real only when net
<b>DSR ≥ 95%</b> AND calibration holds — judge by that, not raw P&amp;L. The eval agent
refreshes resolutions; this page reads the cache.</div>
{sl}
{dv}
</body></html>"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", nargs="?", default="render", choices=["render", "serve"])
    ap.add_argument("--trials", type=int, default=8)
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--port", type=int, default=5056)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    def _html():
        try:
            return render_html(build_summary(args.trials))
        except Exception:
            import traceback
            return ("<!doctype html><body style='background:#0d1117;color:#f85149;"
                    "font-family:monospace;padding:20px'><pre>" + traceback.format_exc()
                    + "</pre></body>")

    if args.mode == "render":
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(_html())
        print(f"wrote dashboard -> {args.out}\nopen it with:  open {args.out}")
        return

    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/favicon.ico":
                self.send_response(204); self.end_headers(); return
            body = _html().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"sports dashboard → http://{args.host}:{args.port}  (http, not https)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
