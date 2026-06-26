#!/usr/bin/env python3
"""sports_dash — live scorecard for the two sports sleeves (sports_lock + devig_check).

Mirrors the granularity of the weather-fade dashboard: a portfolio strip, a collector
HEALTH panel (is the signal agent still firing? are settlements being graded?), a
per-LEAGUE breakdown, the OPEN/pending positions, the recently SETTLED trades with their
net P&L, by-day activity, plus the significance + calibration block sports already had
(per-day PSR / DSR / MinTRL and a Brier reliability table).

Reuses sports_eval's pure scoring (CACHED resolutions only — no network, read-only). P&L
is reported as paper return at $1 capital-at-risk per trade (sports signals carry no
notional), so "Net P&L" = the sum of per-trade net returns — judge by DSR + calibration.

Run:  python scripts/sports_dash.py                # writes HTML file
      python scripts/sports_dash.py serve           # http://127.0.0.1:5056
      python scripts/sports_dash.py --trials 12
      python scripts/sports_dash.py selftest        # synthetic fixture, asserts panels
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from sports_eval import (LOGS, load_signals, score_trades, _load_resolutions,  # noqa: E402
                         entry_price, predicted_prob, side_won, trade_return, kalshi_fee)

OUT = ROOT / "data" / "sports_dash.html"

# A pending game flagged more than this long ago should already have settled; if it
# hasn't been graded, the eval/resolution agent is probably stopped (cache is stale).
STALE_PENDING_HRS = 24


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


def _parse_ts(ts: str):
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _age_min(ts: str, now: datetime):
    t = _parse_ts(ts)
    return None if t is None else int((now - t).total_seconds() / 60)


def _league(rec: dict) -> str:
    return (rec.get("league") or "?").upper()


def _detail(signals: dict, resolutions: dict, now: datetime) -> dict:
    """Join deduped signals with cached resolutions → settled + pending detail rows,
    a per-league rollup, and a $1/trade cumulative net P&L. Pure (no network)."""
    settled, pending = [], []
    by_league = defaultdict(lambda: {"w": 0, "l": 0, "net": 0.0})
    cum_net = 0.0
    for tk, rec in signals.items():
        entry = entry_price(rec)
        if entry is None:
            continue
        lg, game, side = _league(rec), rec.get("game") or "", rec.get("side") or ""
        ts = rec.get("ts", "")
        edge = rec.get("edge", rec.get("net_edge", ""))
        res = resolutions.get(tk)
        if res not in ("yes", "no"):
            pending.append({"ticker": tk, "league": lg, "game": game, "side": side,
                            "entry": entry, "edge": edge, "ts": ts,
                            "age_min": _age_min(ts, now)})
            continue
        won = side_won(side, res)
        net_ret = trade_return(entry, won, kalshi_fee(entry))
        cum_net += net_ret
        b = by_league[lg]
        b["w" if won else "l"] += 1
        b["net"] += net_ret
        settled.append({"ticker": tk, "league": lg, "game": game, "side": side,
                        "entry": entry, "won": won, "net_ret": net_ret,
                        "pred": predicted_prob(rec), "ts": ts, "date": str(ts)[:10]})
    league_rows = []
    for lg, b in sorted(by_league.items(), key=lambda kv: kv[1]["net"]):
        n = b["w"] + b["l"]
        league_rows.append({"league": lg, "w": b["w"], "l": b["l"],
                            "wr": round(b["w"] / n * 100) if n else 0,
                            "net": round(b["net"], 3)})
    return {"settled": settled, "pending": pending, "per_league": league_rows,
            "cum_net": round(cum_net, 3)}


def build_sleeve(key: str, resolutions: dict, trials: int, now: datetime,
                 logs=LOGS) -> dict:
    raw = _read_jsonl(logs[key])               # every line — for activity + freshness
    signals = load_signals(logs[key])          # deduped to one entry per market
    det = _detail(signals, resolutions, now)
    settled = det["settled"]
    trades = [{"date": t["date"], "entry": t["entry"], "won": t["won"], "pred": t["pred"]}
              for t in settled]
    scored = score_trades(trades, trials) if trades else None

    last_ts = max((r.get("ts", "") for r in raw), default="")
    by_day = Counter(str(r.get("ts", ""))[:10] for r in raw if r.get("ts"))
    stale_pending = sum(1 for p in det["pending"]
                        if p["age_min"] is not None and p["age_min"] > STALE_PENDING_HRS * 60)
    return {
        "n_markets": len(signals), "n_logged": len(raw),
        "pending": sorted(det["pending"], key=lambda p: p["ts"], reverse=True),
        "settled_recent": sorted(settled, key=lambda t: t["ts"], reverse=True)[:15],
        "per_league": det["per_league"], "cum_net": det["cum_net"],
        "scored": scored, "recent": raw[-8:][::-1],
        "last_ts": last_ts, "age_min": _age_min(last_ts, now),
        "stale_pending": stale_pending,
        "by_day": [{"d": d, "n": by_day[d]} for d in sorted(by_day)][-14:],
    }


def build_summary(trials: int, logs=LOGS, resolutions=None, now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    resolutions = _load_resolutions() if resolutions is None else resolutions
    return {
        "as_of": now.strftime("%Y-%m-%d %H:%M:%SZ"),
        "trials": trials,
        "sleeves": {k: build_sleeve(k, resolutions, trials, now, logs)
                    for k in ("lock", "devig")},
    }


# ── rendering ────────────────────────────────────────────────────────────────

def _cls(v):
    try:
        return "pos" if float(v) > 0 else ("neg" if float(v) < 0 else "")
    except (TypeError, ValueError):
        return ""


def _pct(v):
    return "—" if v is None else f"{v*100:.0f}%"


def _verdict(s: dict) -> str:
    if not s or s["days"] < 5 or s["psr_net"] is None:
        return "collecting…"
    if s["dsr_net"] and s["dsr_net"] >= 0.95 and (s["mean_ret_net"] or 0) > 0:
        return "REAL (DSR≥0.95)"
    if s["psr_net"] < 0.5:
        return "no edge / efficient"
    return "provisional — keep collecting"


def _health_panel(name: str, d: dict) -> str:
    """Is the collector firing and are settlements being graded? A stale last-signal
    age or a pile of un-graded pendings explains an empty/frozen scorecard at a glance."""
    age = d["age_min"]
    if not d["n_logged"]:
        return (f"<div class=warn>⚠ {name}: no signals logged yet — start the "
                f"collector agent.</div>")
    if age is None:
        fresh, note = "dim", "last-signal age unknown"
    elif age <= 180:
        fresh, note = "pos", f"last signal {age} min ago"
    elif age <= 1440:
        fresh, note = "dim", (f"last signal {age // 60}h ago — normal between game days")
    else:
        fresh, note = "warn", (f"last signal {age // 60}h ago — collector may be STOPPED "
                               f"(figures below are HISTORICAL, not live)")
    stale = ""
    if d["stale_pending"]:
        stale = (f" · <span class=neg>{d['stale_pending']} pending &gt;"
                 f"{STALE_PENDING_HRS}h ungraded</span> — the eval/resolution agent isn't "
                 f"settling them, so P&amp;L below EXCLUDES these")
    return (f"<div class={fresh}>⏱ {name}: <b>{note}</b> · {d['n_logged']} signals logged · "
            f"{d['n_markets']} markets · {len(d['pending'])} pending{stale}</div>")


def _sleeve_html(name: str, blurb: str, d: dict, trials: int) -> str:
    s = d["scored"]
    head = (f"<h2>{name} <span class=dim>· {d['n_markets']} markets · "
            f"{len(d['pending'])} pending</span></h2><div class=note>{blurb}</div>")
    if not s:
        body = ("<div class=dim>no settled trades yet — collect, then the eval agent "
                "fills resolutions.</div>")
    else:
        mt = s["mintrl_net"]
        mt_s = ("∞" if mt == float("inf") else "n<5" if mt is None else f"{mt:.0f}d")
        psr = "n<5" if s["psr_net"] is None else _pct(s["psr_net"])
        dsr = "n<5" if s["dsr_net"] is None else _pct(s["dsr_net"])
        body = f"""<div>
 <span class=stat><div class=lbl>Net P&amp;L ($1/trade)</div><div class="big {_cls(d['cum_net'])}">{d['cum_net']:+.2f}</div><div class=dim>sum of net returns</div></span>
 <span class=stat><div class=lbl>Mean ret (net)</div><div class="big {_cls(s['mean_ret_net'])}">{('—' if s['mean_ret_net'] is None else f"{s['mean_ret_net']:+.3f}")}</div><div class=dim>gross {('—' if s['mean_ret_gross'] is None else f"{s['mean_ret_gross']:+.3f}")}</div></span>
 <span class=stat><div class=lbl>Hit-rate</div><div class=big>{_pct(s['hit_rate'])}</div><div class=dim>{s['n_trades']} settled</div></span>
 <span class=stat><div class=lbl>Days</div><div class=big>{s['days']}</div></span>
 <span class=stat><div class=lbl>Brier</div><div class=big>{('—' if s['calibration']['brier'] is None else f"{s['calibration']['brier']:.3f}")}</div><div class=dim>0.25=coin</div></span>
</div>
<div class=note>per-day <b>PSR(net) {psr}</b> · <b>DSR(@{trials}) {dsr}</b> · MinTRL {mt_s} → <b>{_verdict(s)}</b></div>"""

    # per-league breakdown (mirrors weather's per-city)
    lg_rows = "".join(
        f"<tr><td>{r['league']}</td><td>{r['w']}W/{r['l']}L</td><td>{r['wr']:.0f}%</td>"
        f"<td class={_cls(r['net'])}>{r['net']:+.3f}</td></tr>"
        for r in d["per_league"]) or "<tr><td colspan=4 class=dim>no settled trades</td></tr>"
    league = (f"<p class=lbl>per-league (settled, net $1/trade)</p>"
              f"<table><tr><th>league</th><th>W/L</th><th>WR</th><th>net</th></tr>{lg_rows}</table>")

    # open / pending positions (mirrors weather's open fades)
    stale_warn = ("" if not d["stale_pending"] else
                  f"<div class=warn>⚠ {d['stale_pending']} pending flagged &gt;"
                  f"{STALE_PENDING_HRS}h ago should have settled — the resolution agent "
                  f"appears stopped; grade them with <code>python scripts/sports_eval.py "
                  f"eval --log {name.split()[-1]}</code>.</div>")
    pend_rows = "".join(
        f"<tr><td>{p['ticker']}</td><td>{p['league']}</td><td>{(p['game'] or '')[:26]}</td>"
        f"<td>{p['side']}</td><td>{p['entry']:.2f}</td><td>{p['edge']}</td>"
        f"<td class=dim>{str(p['ts'])[5:16]}</td></tr>"
        for p in d["pending"][:20]) or "<tr><td colspan=7 class=dim>none pending</td></tr>"
    pending = (f"<p class=lbl>open / pending ({len(d['pending'])})</p>{stale_warn}"
               f"<table><tr><th>ticker</th><th>lg</th><th>game</th><th>side</th>"
               f"<th>entry</th><th>edge</th><th>flagged</th></tr>{pend_rows}</table>")

    # recently settled with P&L (mirrors weather's recently settled)
    set_rows = "".join(
        f"<tr><td>{t['ticker']}</td><td>{t['league']}</td><td>{t['side']}</td>"
        f"<td class={'pos' if t['won'] else 'neg'}>{'WON' if t['won'] else 'LOST'}</td>"
        f"<td class={_cls(t['net_ret'])}>{t['net_ret']:+.3f}</td>"
        f"<td class=dim>{str(t['ts'])[5:16]}</td></tr>"
        for t in d["settled_recent"]) or "<tr><td colspan=6 class=dim>none settled yet</td></tr>"
    settled = (f"<p class=lbl>recently settled</p>"
               f"<table><tr><th>ticker</th><th>lg</th><th>side</th><th>result</th>"
               f"<th>net ret</th><th>settled</th></tr>{set_rows}</table>")

    # calibration reliability table (kept from the original)
    cal_html = ""
    if s and s["calibration"]["bins"]:
        bins = "".join(
            f"<tr><td>{lo:.2f}–{hi:.2f}</td><td>{n}</td><td>{mp:.3f}</td>"
            f"<td>{ar:.3f}</td><td class={_cls(ar-mp)}>{ar-mp:+.3f}</td></tr>"
            for lo, hi, n, mp, ar in s["calibration"]["bins"])
        cal_html = (f"<p class=lbl>win-prob calibration — gap = actual − predicted</p>"
                    f"<table><tr><th>predicted</th><th>n</th><th>mean_pred</th>"
                    f"<th>actual</th><th>gap</th></tr>{bins}</table>")

    # activity by day (mirrors weather's by-hour)
    day_html = "".join(f"<span class=hr>{x['d'][5:]} <b>{x['n']}</b></span>"
                       for x in d["by_day"]) or "<span class=dim>no activity yet</span>"
    activity = f"<p class=lbl>signals by day (last {len(d['by_day'])})</p><div>{day_html}</div>"

    # recent raw signals (kept)
    rec = "".join(
        f"<tr><td>{str(r.get('ts',''))[5:16]}</td><td>{_league(r)}</td>"
        f"<td>{(r.get('game') or '')[:30]}</td><td>{r.get('side','')}</td>"
        f"<td>{r.get('edge', r.get('net_edge',''))}</td></tr>"
        for r in d["recent"]) or "<tr><td colspan=5 class=dim>no signals logged yet</td></tr>"
    recent = (f"<p class=lbl>recent signals</p><table><tr><th>when</th><th>lg</th>"
              f"<th>game</th><th>side</th><th>edge</th></tr>{rec}</table>")

    return head + body + league + pending + settled + cal_html + activity + recent


def render_html(s: dict) -> str:
    lk, dv = s["sleeves"]["lock"], s["sleeves"]["devig"]
    sl = _sleeve_html("🔒 sports_lock", "Garbage-time LOCK: near-decided game still mispriced "
                      "on Kalshi. Predicted = model win-prob. Net = after Kalshi taker fee.",
                      lk, s["trials"])
    dvh = _sleeve_html("⚖️ devig_check", "Whole-game DEVIG: sharp-book fair price vs Kalshi YES. "
                       "Predicted = devigged fair prob of the side bought.", dv, s["trials"])
    return f"""<!doctype html><html><head><meta charset=utf-8>
<meta http-equiv=refresh content=30><title>sports sleeves</title><style>
body{{background:#0d1117;color:#c9d1d9;font-family:Menlo,monospace;font-size:14px;max-width:980px;margin:0 auto;padding:20px}}
h1{{color:#c084fc;font-size:20px}} h2{{color:#58a6ff;font-size:14px;margin-top:26px}}
.big{{font-size:26px;font-weight:bold}} .pos{{color:#3fb950}} .neg{{color:#f85149}} .dim{{color:#8b949e}}
.warn{{color:#d29922}} .note,div.pos,div.warn,div.dim{{border:1px solid #30363d;border-radius:6px;padding:8px 12px;margin:10px 0;background:#161b22}}
.hr{{display:inline-block;margin:2px 10px 2px 0}}
table{{width:100%;border-collapse:collapse;margin-top:6px}} td,th{{text-align:left;padding:5px 6px;border-bottom:1px solid #21262d}}
th{{color:#8b949e;font-size:11px}} .stat{{display:inline-block;margin-right:30px;vertical-align:top}} .lbl{{color:#8b949e;font-size:12px;margin-top:16px}}
</style></head><body>
<h1>🏟️ sports sleeves <span class=dim style=font-size:12px>as of {s['as_of']} · auto-refresh 30s · paper only</span></h1>
<div class=note>Two paper edges, scored on Kalshi settlement (cached). Real only when net
<b>DSR ≥ 95%</b> AND calibration holds — judge by that, not raw P&amp;L. P&amp;L is paper
at $1 capital-at-risk per trade. The eval agent refreshes resolutions; this page reads the cache.</div>
{_health_panel("sports_lock", lk)}
{_health_panel("devig_check", dv)}
{sl}
{dvh}
</body></html>"""


def selftest() -> int:
    """Synthetic fixture: exercises every panel without the real ledgers or network."""
    now = datetime(2026, 6, 26, 18, 0, tzinfo=timezone.utc)
    logs = {
        "lock": [
            {"ticker": "L1", "ts": "2026-06-25T20:00:00Z", "side": "YES", "league": "nba",
             "game": "BOS@MIA", "kalshi_yes": 0.90, "win_prob": 0.97, "edge": 0.07},
            {"ticker": "L2", "ts": "2026-06-26T01:00:00Z", "side": "NO", "league": "nfl",
             "game": "KC@BUF", "kalshi_yes": 0.12, "win_prob": 0.95, "edge": 0.07},
            {"ticker": "L3", "ts": "2026-06-26T17:30:00Z", "side": "YES", "league": "mlb",
             "game": "LAD@SF", "kalshi_yes": 0.80, "win_prob": 0.93, "edge": 0.13},  # pending
        ],
        "devig": [
            {"ticker": "D1", "ts": "2026-05-01T00:00:00Z", "side": "YES", "league": "nhl",
             "game": "EDM@FLA", "kalshi_yes": 0.55, "fair_yes": 0.62, "net_edge": 0.07},
        ],
    }

    class _P:                                  # tiny shim so build_sleeve's path read works
        def __init__(self, rows): self._rows = rows
        def exists(self): return True
        def read_text(self): return "\n".join(json.dumps(r) for r in self._rows)

    logs_p = {k: _P(v) for k, v in logs.items()}
    resolutions = {"L1": "yes", "L2": "no", "D1": "yes"}   # L3 ungraded → pending
    summ = build_summary(8, logs=logs_p, resolutions=resolutions, now=now)
    lk = summ["sleeves"]["lock"]
    assert lk["n_logged"] == 3 and lk["n_markets"] == 3, lk
    assert len(lk["pending"]) == 1 and lk["pending"][0]["ticker"] == "L3", lk["pending"]
    assert {r["league"] for r in lk["per_league"]} == {"NBA", "NFL"}, lk["per_league"]
    assert len(lk["settled_recent"]) == 2, lk["settled_recent"]
    assert lk["age_min"] == 30, lk["age_min"]              # 17:30 → 18:00 = 30 min
    # devig D1 is old (May) → collector reads STOPPED
    dv = summ["sleeves"]["devig"]
    assert dv["age_min"] > 1440, dv["age_min"]
    html = render_html(summ)
    for must in ["per-league", "open / pending", "recently settled", "Net P&amp;L",
                 "calibration", "signals by day", "L3", "collector may be STOPPED"]:
        assert must in html, f"missing panel: {must}"
    print(f"selftest OK — rendered {len(html)} bytes, all panels present")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", nargs="?", default="render",
                    choices=["render", "serve", "selftest"])
    ap.add_argument("--trials", type=int, default=8)
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--port", type=int, default=5056)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    if args.mode == "selftest":
        raise SystemExit(selftest())

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
