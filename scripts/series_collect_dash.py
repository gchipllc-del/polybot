#!/usr/bin/env python3
"""series_collect_dash — dashboard for the sports/calibration sleeve, with its OWN
paper bankroll, separate from every other sleeve. Reads series_collect_*.jsonl
ledgers and shows: the calibration curve (priced prob vs realized = the test),
the pre-registered 90–93% favorite-fade band, and that band's paper P&L judged by
per-day PSR + DSR. Read-only; places nothing.

Run:  python scripts/series_collect_dash.py            # writes HTML file
      python scripts/series_collect_dash.py serve       # http://127.0.0.1:5054
      python scripts/series_collect_dash.py --series KXNFLGAME,KXNBA
"""
from __future__ import annotations

import argparse
import glob
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from series_collect import (_load, calibration, band_fade_returns,  # noqa: E402
                            DEFAULT_BANKROLL, DATA)


def _series_list(arg: str | None) -> list:
    if arg:
        return [s.strip() for s in arg.split(",") if s.strip()]
    # default: every series we're collecting
    out = []
    for p in glob.glob(str(DATA / "series_collect_*.jsonl")):
        out.append(Path(p).stem.replace("series_collect_", ""))
    return sorted(out)


def build_summary(series_arg: str | None, lo: float, hi: float, trials: int) -> dict:
    from lib.hermes_significance import (probabilistic_sharpe_ratio,
                                         min_track_record_length,
                                         deflated_sharpe_ratio)
    series = _series_list(series_arg)
    rows = []
    for s in series:
        rows.extend(_load(s))
    settled = [r for r in rows if r.get("outcome") in (0, 1) and r.get("entry_p") is not None]
    openn = [r for r in rows if r.get("outcome") is None]
    days = sorted({str(r.get("settled_at") or "")[:10] for r in settled})

    bpt, bpd, daily, bn, real_yes = band_fade_returns(settled, lo, hi)
    psr = probabilistic_sharpe_ratio(bpd)
    dsr = deflated_sharpe_ratio(bpd, n_trials=trials)
    mtrl = min_track_record_length(bpd)
    # bankroll curve: $1 of cost staked per qualifying trade
    curve, run = [], DEFAULT_BANKROLL
    for d, net in daily:
        run += net
        curve.append((d, round(run, 2)))

    return {
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"),
        "series": series, "n_settled": len(settled), "n_open": len(openn),
        "n_days": len(days), "span": (f"{days[0]}…{days[-1]}" if days else "—"),
        "calibration": calibration(settled, bins=10),
        "band": (lo, hi), "band_n": bn, "band_real_yes": real_yes,
        "band_edge": (None if real_yes is None else (1 - real_yes) - (1 - (lo + hi) / 2)),
        "band_sum": round(sum(bpt), 2) if bpt else 0.0,
        "band_days": len(bpd),
        "psr": psr, "dsr": dsr, "trials": trials,
        "mtrl": ("∞" if mtrl == float("inf") else (None if mtrl is None else int(mtrl))),
        "bankroll": DEFAULT_BANKROLL, "portfolio": curve[-1][1] if curve else DEFAULT_BANKROLL,
        "curve": curve[-30:],
    }


def _cls(v):
    try:
        return "pos" if float(v) > 0 else ("neg" if float(v) < 0 else "")
    except (TypeError, ValueError):
        return ""


def render_html(s: dict) -> str:
    lo, hi = s["band"]
    cal = "".join(
        f"<tr{' style=background:#1c2733' if (lo <= c[0] < hi or lo < c[1] <= hi) else ''}>"
        f"<td>{c[0]:.2f}–{c[1]:.2f}</td><td>{c[2]}</td><td>{c[3]:.2f}</td>"
        f"<td>{c[4]:.2f}</td><td class={_cls(c[4]-c[3])}>{c[4]-c[3]:+.2f}</td></tr>"
        for c in s["calibration"]) or "<tr><td colspan=5 class=dim>no settled data yet</td></tr>"
    curve = "".join(f"<span class=hr>{d[5:]}: <b>${v:.0f}</b></span>" for d, v in s["curve"]) \
        or "<span class=dim>no band trades yet</span>"
    psr_s = "n<5" if s["psr"] is None else f"{s['psr']:.2f}"
    dsr_s = "n<5" if s["dsr"] is None else f"{s['dsr']:.2f}"
    mt_s = "n<5" if s["mtrl"] is None else str(s["mtrl"])
    edge_s = "—" if s["band_edge"] is None else f"{s['band_edge']:+.0%}"
    verdict = ("collecting…" if s["band_n"] < 5 else
               "REAL (edge>0 & DSR≥0.95)" if (s["band_edge"] and s["band_edge"] > 0
                                              and s["dsr"] and s["dsr"] >= 0.95)
               else "no edge / efficient" if (s["psr"] is not None and s["psr"] < 0.5)
               else "provisional — keep collecting")
    return f"""<!doctype html><html><head><meta charset=utf-8>
<meta http-equiv=refresh content=30><title>sports-calibration</title><style>
body{{background:#0d1117;color:#c9d1d9;font-family:Menlo,monospace;font-size:14px;max-width:980px;margin:0 auto;padding:20px}}
h1{{color:#c084fc;font-size:20px}} h2{{color:#58a6ff;font-size:13px;text-transform:uppercase;margin-top:22px}}
.big{{font-size:28px;font-weight:bold}} .pos{{color:#3fb950}} .neg{{color:#f85149}} .dim{{color:#8b949e}}
.note{{border:1px solid #30363d;border-radius:6px;padding:8px 12px;margin:10px 0;background:#161b22}}
table{{width:100%;border-collapse:collapse;margin-top:6px}} td,th{{text-align:left;padding:5px 6px;border-bottom:1px solid #21262d}}
th{{color:#8b949e;font-size:11px}} .stat{{display:inline-block;margin-right:32px}} .lbl{{color:#8b949e;font-size:12px}}
.hr{{display:inline-block;margin:2px 10px 2px 0}}
</style></head><body>
<h1>🎯 sports-calibration sleeve <span class=dim style=font-size:12px>as of {s['as_of']} · auto-refresh 30s</span></h1>
<div class=note>Pre-registered test: are Kalshi favorites in the <b>{lo:.0%}–{hi:.0%}</b> band
overpriced (realized YES &lt; priced) → fade (buy NO)? Own ${s['bankroll']:.0f} paper bankroll,
separate from every other sleeve. <b>Read-only / paper.</b> Series: {', '.join(s['series']) or '—'}</div>
<div>
 <span class=stat><div class=lbl>Settled</div><div class=big>{s['n_settled']}</div><div class=dim>{s['n_days']} days · {s['span']}</div></span>
 <span class=stat><div class=lbl>Open</div><div class=big>{s['n_open']}</div></span>
 <span class=stat><div class=lbl>Band trades</div><div class=big>{s['band_n']}</div></span>
 <span class=stat><div class=lbl>Band fade edge</div><div class="big {_cls(s['band_edge'])}">{edge_s}</div><div class=dim>realized {('—' if s['band_real_yes'] is None else f"{s['band_real_yes']:.0%}")} YES</div></span>
 <span class=stat><div class=lbl>Bankroll (own)</div><div class=big>${s['bankroll']:.2f}</div><div class=dim>portfolio ${s['portfolio']:.2f}</div></span>
</div>
<div class=note>per-day <b>PSR {psr_s}</b> · <b>DSR(@{s['trials']} trials) {dsr_s}</b> · MinTRL {mt_s} ·
band sum-return {s['band_sum']:+.2f} over {s['band_days']} days → <b>{verdict}</b></div>
<h2>Calibration — priced prob vs realized (the fade band is highlighted)</h2>
<table><tr><th>price band</th><th>n</th><th>mkt_p</th><th>realized</th><th>gap</th></tr>{cal}</table>
<p class=dim>gap = realized − priced. Persistent NEGATIVE gap in a band = that price is overpriced YES (fadeable).</p>
<h2>Band-fade bankroll (last 30 days, $1 cost/trade)</h2>
<div>{curve}</div>
<p class=dim>Judge by DSR, not raw P&amp;L: edge&gt;0 AND DSR≥0.95 = real & survives the search;
DSR&lt;0.5 = efficient, drop it. Most likely outcome: efficient.</p>
</body></html>"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", nargs="?", default="render", choices=["render", "serve"])
    ap.add_argument("--series", help="comma-separated series; default = all collected")
    ap.add_argument("--band-lo", type=float, default=0.90)
    ap.add_argument("--band-hi", type=float, default=0.93)
    ap.add_argument("--trials", type=int, default=1)
    ap.add_argument("--out", default=str(DATA / "series_collect_dash.html"))
    ap.add_argument("--port", type=int, default=5054)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    def _html():
        try:
            return render_html(build_summary(args.series, args.band_lo, args.band_hi, args.trials))
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
    print(f"sports-calibration dashboard → http://{args.host}:{args.port}  (http, not https)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
