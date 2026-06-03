#!/usr/bin/env python3
"""Read-only LIVE watch — one command covering every live Kalshi weather sleeve.

Covers:
  * DAILY-weather   (KXHIGHT*/KXLOWT*, asset=weather_daily)  — live + paper
  * MISPRICING edge (KXTEMPNYC*, asset=weather, is_live)     — live + $40 cap
  * Fresh (<60 min) live-order alerts for ALL weather prefixes, BLOCK-AWARE
    (the alert blocks are multi-line: [ts] / FAILED|PLACED / ticker).

STRICTLY READ-ONLY: no network, no writes. Run from the polybot root:
    /Users/jesse/anaconda3/bin/python3 scripts/live_watch.py
"""
from __future__ import annotations
import json
import os
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOW = datetime.now(timezone.utc)
CUTOFF = NOW - timedelta(minutes=60)


def _load(rel: str) -> list[dict]:
    p = os.path.join(ROOT, rel)
    out = []
    if os.path.exists(p):
        for ln in open(p):
            ln = ln.strip()
            if ln:
                try:
                    out.append(json.loads(ln))
                except Exception:
                    pass
    return out


def _pnl(r) -> float:
    for k in ("paper_pnl", "pnl"):
        if r.get(k) is not None:
            try:
                return float(r[k])
            except Exception:
                pass
    return 0.0


def _stats(rows):
    s = [r for r in rows if str(r.get("status")).lower() in ("won", "lost")]
    w = sum(1 for r in s if str(r.get("status")).lower() == "won")
    op = sum(1 for r in rows if str(r.get("status")).lower() == "open")
    net = sum(_pnl(r) for r in s)
    wr = 100 * w / len(s) if s else 0
    return op, len(s), w, wr, net


def _fresh_alerts():
    """Block-aware: associate each ticker line with its [timestamp] header."""
    p = os.path.join(ROOT, "logs", "live_alerts.log")
    fresh = []
    cur = None
    buf = []

    def flush():
        if cur and cur >= CUTOFF:
            b = " ".join(buf)
            for pref in ("KXHIGHT", "KXLOWT", "KXTEMPNYC"):
                if pref in b:
                    kind = ("FAILED" if "FAILED" in b
                            else "PLACED/FILL" if ("PLACED" in b or "fill" in b.lower())
                            else "INFO")
                    fresh.append((cur, pref, kind))
                    break
    if os.path.exists(p):
        for line in open(p):
            if line.startswith("[") and "UTC]" in line:
                flush()
                try:
                    cur = datetime.strptime(line[1:line.index("]")].replace(" UTC", "").strip(),
                                            "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                except Exception:
                    cur = None
                buf = [line.strip()]
            else:
                buf.append(line.strip())
        flush()
    return fresh


def main():
    # kill-switch state (single JSON object, not jsonl)
    st = {}
    _sp = os.path.join(ROOT, "data", "kalshi_live_state.json")
    if os.path.exists(_sp):
        try:
            st = json.load(open(_sp))
        except Exception:
            st = {}
    ks, cl = st.get("kill_switch_tripped"), st.get("consecutive_losses")

    print(f"NOW {NOW:%Y-%m-%d %H:%M:%S}Z | kill-switch={'TRIPPED' if ks else 'armed'} ({cl} losses)")

    # DAILY-weather
    POST = {"strike_type_aware_v1", "blended_v2"}
    d = [r for r in _load("data/weather_daily_paper.jsonl") if r.get("entry_schema") in POST]
    dlive = [r for r in d if r.get("is_live") is True and int(r.get("live_contracts") or 0) > 0]
    dop, dse, dw, dwr, dnet = _stats(d)
    dlive_str = ", ".join(
        f"{r.get('ticker') or r.get('market_ticker')} {r.get('side')} "
        f"{r.get('live_contracts')}@{r.get('fill_price')}" for r in dlive) or "none"
    print(f"DAILY-WX : LIVE filled={len(dlive)} [{dlive_str}] "
          f"| PAPER {dse} settled WR {dwr:.0f}% net ${dnet:.2f} ({dop} open)")

    # MISPRICING (live + paper) — the new edge sleeve
    m = _load("data/weather_paper_mispricing.jsonl")
    mlive = [r for r in m if r.get("is_live") is True]
    mlive_open = sum(float(r.get("live_notional_usd") or 0.0)
                     for r in mlive if str(r.get("status")).lower() == "open")
    mop, mse, mw, mwr, mnet = _stats(m)
    print(f"MISPRICE : LIVE trades={len(mlive)} open-exposure ${mlive_open:.2f}/$40 cap "
          f"| PAPER {mse} settled WR {mwr:.0f}% net ${mnet:.2f} ({mop} open)")
    for r in [x for x in mlive if str(x.get("status")).lower() == "open"]:
        print(f"           🎯 LIVE {r.get('market_ticker')} NO {r.get('live_contracts')}@{r.get('fill_price')} "
              f"(${r.get('live_notional_usd')}) order={r.get('live_order_id')}")

    # Fresh alerts
    fr = _fresh_alerts()
    if fr:
        print(f"FRESH(<60m) alerts: {len(fr)}")
        for ts, pref, kind in sorted(fr):
            print(f"  [{ts:%H:%M} | {int((NOW-ts).total_seconds()//60)}m | {pref} {kind}]")
    else:
        print("FRESH(<60m) alerts: none")


if __name__ == "__main__":
    main()
