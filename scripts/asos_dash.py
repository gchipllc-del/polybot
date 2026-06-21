#!/usr/bin/env python3
"""asos_dash — live scorecard for the ASOS bucket-lock sleeve (observation edge).

Read-only (stdlib server, like the other sleeve dashboards). Reads data/asos_lock.jsonl
and shows: open locks awaiting settlement, the settled hit-rate (vs the 98% target — a
lock that resolves below that was WRONG, not unlucky), paper net, and per-day PSR/DSR
once there are ≥5 distinct days. This is a fast-OBSERVATION edge (the realized high is
already on the settlement thermometer), so the bar is high: ≥98% hit-rate AND DSR≥0.95.

Run:  python scripts/asos_dash.py             # writes HTML file
      python scripts/asos_dash.py serve        # http://127.0.0.1:5058
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from asos_tracker import _load_ledger, NEAR_CERTAIN, STATIONS   # noqa: E402

OUT = ROOT / "data" / "asos_dash.html"


def build_summary(trials: int) -> dict:
    rows = _load_ledger()
    settled = [r for r in rows if r.get("status") in ("won", "lost")]
    openn = [r for r in rows if r.get("status") == "open"]
    hits = sum(1 for r in settled if r["status"] == "won")
    priced = [r for r in settled if r.get("paper_pnl") is not None]
    net = round(sum(float(r["paper_pnl"]) for r in priced), 2)
    by_day: dict = {}
    for r in priced:
        d = str(r.get("ts", ""))[:10]
        by_day[d] = by_day.get(d, 0.0) + float(r["paper_pnl"])
    daily = [by_day[d] for d in sorted(by_day)]
    psr = dsr = mtrl = None
    if len(daily) >= 5:
        from lib.hermes_significance import (probabilistic_sharpe_ratio,
                                             deflated_sharpe_ratio,
                                             min_track_record_length)
        psr = probabilistic_sharpe_ratio(daily)
        dsr = deflated_sharpe_ratio(daily, n_trials=trials)
        mtrl = min_track_record_length(daily)
    return {
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"),
        "n_settled": len(settled), "n_open": len(openn), "hits": hits,
        "hit_rate": (hits / len(settled)) if settled else None,
        "net": net, "n_days": len(daily), "psr": psr, "dsr": dsr,
        "mtrl": ("∞" if mtrl == float("inf") else (None if mtrl is None else int(mtrl))),
        "trials": trials, "n_stations": len(STATIONS),
        "open_rows": sorted(openn, key=lambda r: -(r.get("edge") or 0))[:25],
    }


def _cls(v):
    try:
        return "pos" if float(v) > 0 else ("neg" if float(v) < 0 else "")
    except (TypeError, ValueError):
        return ""


def render_html(s: dict) -> str:
    hr = s["hit_rate"]
    hr_s = "—" if hr is None else f"{hr:.0%}"
    hr_cls = "" if hr is None else ("pos" if hr >= NEAR_CERTAIN else "neg")
    psr_s = "n<5" if s["psr"] is None else f"{s['psr']:.2f}"
    dsr_s = "n<5" if s["dsr"] is None else f"{s['dsr']:.2f}"
    mt_s = "n<5" if s["mtrl"] is None else str(s["mtrl"])
    verdict = ("collecting…" if s["n_days"] < 5 else
               "REAL (≥98% & DSR≥0.95)" if (hr is not None and hr >= NEAR_CERTAIN
                                            and s["dsr"] and s["dsr"] >= 0.95 and s["net"] > 0)
               else "LOCKS WRONG — hit-rate<98%" if (hr is not None and hr < NEAR_CERTAIN)
               else "no edge / efficient" if (s["psr"] is not None and s["psr"] < 0.5)
               else "provisional — keep collecting")
    def _edge_cell(e):
        return "" if e is None else f"{e:+.2f}"
    rows = "".join(
        f"<tr><td>{str(r.get('ts',''))[5:16]}</td><td>{r.get('series','')}</td>"
        f"<td>{r.get('station','')}</td><td>{(r.get('ticker') or '')[:26]}</td>"
        f"<td>{r.get('side','')}</td><td>{r.get('realized_high','')}</td>"
        f"<td>{r.get('strike','')}</td><td>{r.get('market_yes')}</td>"
        f"<td class={_cls(r.get('edge'))}>{_edge_cell(r.get('edge'))}</td></tr>"
        for r in s["open_rows"]) or "<tr><td colspan=9 class=dim>no open locks right now</td></tr>"
    return f"""<!doctype html><html><head><meta charset=utf-8>
<meta http-equiv=refresh content=30><title>asos bucket-lock</title><style>
body{{background:#0d1117;color:#c9d1d9;font-family:Menlo,monospace;font-size:14px;max-width:1000px;margin:0 auto;padding:20px}}
h1{{color:#c084fc;font-size:20px}} h2{{color:#58a6ff;font-size:13px;text-transform:uppercase;margin-top:22px}}
.big{{font-size:28px;font-weight:bold}} .pos{{color:#3fb950}} .neg{{color:#f85149}} .dim{{color:#8b949e}}
.note{{border:1px solid #30363d;border-radius:6px;padding:8px 12px;margin:10px 0;background:#161b22}}
table{{width:100%;border-collapse:collapse;margin-top:6px}} td,th{{text-align:left;padding:5px 6px;border-bottom:1px solid #21262d}}
th{{color:#8b949e;font-size:11px}} .stat{{display:inline-block;margin-right:32px}} .lbl{{color:#8b949e;font-size:12px}}
</style></head><body>
<h1>🌡️🔒 asos bucket-lock <span class=dim style=font-size:12px>as of {s['as_of']} · auto-refresh 30s · paper</span></h1>
<div class=note>Observation edge: read the <b>realized</b> daily high off the exact ASOS station Kalshi
settles on ({s['n_stations']} cities, station map verified), and once the high is physically locked
(evening, temps falling) flag the now-near-certain bucket the overnight book still misprices.
<b>Not a forecast.</b> Bar is high: ≥{NEAR_CERTAIN:.0%} hit-rate AND DSR≥0.95. <b>Read-only / paper.</b></div>
<div>
 <span class=stat><div class=lbl>Settled locks</div><div class=big>{s['n_settled']}</div><div class=dim>{s['n_days']} days</div></span>
 <span class=stat><div class=lbl>Open locks</div><div class=big>{s['n_open']}</div></span>
 <span class=stat><div class=lbl>Hit-rate</div><div class="big {hr_cls}">{hr_s}</div><div class=dim>target ≥{NEAR_CERTAIN:.0%}</div></span>
 <span class=stat><div class=lbl>Paper net</div><div class="big {_cls(s['net'])}">${s['net']:+.2f}</div></span>
</div>
<div class=note>per-day <b>PSR {psr_s}</b> · <b>DSR(@{s['trials']}) {dsr_s}</b> · MinTRL {mt_s} over {s['n_days']} days → <b>{verdict}</b></div>
<h2>Open locks (awaiting settle)</h2>
<table><tr><th>when</th><th>series</th><th>stn</th><th>ticker</th><th>side</th><th>realized°F</th><th>strike</th><th>mkt_yes</th><th>edge</th></tr>{rows}</table>
<p class=dim>edge = our near-certain prob − the market's implied prob for that side. The live risks the
scorecard can't see: CLI revision near a bucket edge, and whether the overnight book actually fills.</p>
</body></html>"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", nargs="?", default="render", choices=["render", "serve"])
    ap.add_argument("--trials", type=int, default=8)
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--port", type=int, default=5058)
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
    print(f"asos bucket-lock dashboard → http://{args.host}:{args.port}  (http, not https)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
