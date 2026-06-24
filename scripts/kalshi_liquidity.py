#!/usr/bin/env python3
"""kalshi_liquidity — the check that should run BEFORE any forecasting effort.

ASOS taught us the hard way: a perfect signal on a market with no resting book is
worthless (0/155 fillable). This lists the open markets in one or more Kalshi series and
reports, per market, whether there's a two-sided resting book, the spread, volume, and
open interest — so you can tell a LIQUID venue (worth modeling) from a quote-on-demand
DEAD one (skip) in seconds. Uses the public /markets endpoint (no auth).

  python scripts/kalshi_liquidity.py --series KXHIGHNY,KXTEMPNYC
  python scripts/kalshi_liquidity.py --series KXHIGHNY            # one series
  python scripts/kalshi_liquidity.py --selftest

Finding tickers: browse kalshi.com's Weather section for the current hourly-temp series
(KXTEMPNYCH was delisted) and pass them with --series. Run now AND mid-morning — hourly
temp markets list/quote differently through the day.
"""
import argparse
import json
import sys
from pathlib import Path

# Known daily-high series (control — expect these to look DEAD, matching asos_edge).
DEFAULT_SERIES = ["KXHIGHNY", "KXHIGHCHI", "KXHIGHMIA", "KXHIGHLAX"]


def market_liquidity(m: dict) -> dict:
    yb, ya = int(m.get("yes_bid") or 0), int(m.get("yes_ask") or 0)
    nb, na = int(m.get("no_bid") or 0), int(m.get("no_ask") or 0)
    two_sided = (yb > 0 and ya > 0) or (nb > 0 and na > 0)
    spread = (ya - yb) if (yb > 0 and ya > 0) else None
    return {"ticker": m.get("ticker", "?"), "yes_bid": yb, "yes_ask": ya,
            "two_sided": two_sided, "spread": spread,
            "volume": int(m.get("volume") or 0),
            "open_interest": int(m.get("open_interest") or 0),
            "liquidity": int(m.get("liquidity") or 0)}


def summarize(series: str, markets: list) -> dict:
    rows = [market_liquidity(m) for m in markets]
    fillable = [r for r in rows if r["two_sided"]]
    return {"series": series, "n": len(rows), "fillable": len(fillable),
            "volume": sum(r["volume"] for r in rows),
            "open_interest": sum(r["open_interest"] for r in rows),
            "rows": rows}


def verdict(s: dict) -> str:
    if s["n"] == 0:
        return "NO OPEN MARKETS (delisted or none today)"
    if s["fillable"] == 0:
        return "DEAD — no resting two-sided book (ASOS-style; unfillable)"
    if s["volume"] == 0 and s["open_interest"] == 0:
        return f"THIN — {s['fillable']}/{s['n']} quoted but 0 volume/OI (depth unknown — probe orderbook)"
    return f"LIQUID — {s['fillable']}/{s['n']} two-sided, vol {s['volume']}, OI {s['open_interest']}"


def _fetch(series: str) -> list:
    from fetch_backtest_data import _kalshi_get
    data = _kalshi_get("/markets", {"series_ticker": series, "status": "open", "limit": 200})
    return (data or {}).get("markets", []) or []


def run(series_list: list, fetch=_fetch) -> list:
    out = []
    for s in series_list:
        try:
            markets = fetch(s)
        except Exception as e:                       # noqa: BLE001
            out.append({"series": s, "error": str(e)[:120]})
            continue
        out.append(summarize(s, markets))
    return out


def report_log(rows: list) -> list:
    """Roll a liquidity_log.jsonl into a per-series summary: did it EVER show a book, and
    in which ET hours. Turns a night of 20-min samples into one glance."""
    from collections import defaultdict
    by = defaultdict(list)
    for r in rows:
        by[r.get("series", "?")].append(r)
    out = []
    for s, rs in sorted(by.items()):
        fillable = [r for r in rs if (r.get("fillable") or 0) > 0]
        out.append({
            "series": s,
            "samples": len(rs),
            "fillable_samples": len(fillable),
            "max_fillable": max((r.get("fillable") or 0) for r in rs),
            "max_volume": max((r.get("volume") or 0) for r in rs),
            "no_markets": all((r.get("n") or 0) == 0 for r in rs),
            "fillable_ts": [r.get("ts") for r in fillable],
        })
    return out


def _et_hours(ts_list: list) -> str:
    if not ts_list:
        return "never"
    try:
        from zoneinfo import ZoneInfo
        from datetime import datetime
        hrs = sorted({datetime.fromisoformat(t).astimezone(ZoneInfo("America/New_York")).hour
                      for t in ts_list if t})
        return "ET h " + ",".join(str(h) for h in hrs)
    except Exception:                                    # noqa: BLE001
        return f"{len(ts_list)} samples"


def _print_report(path: Path) -> None:
    if not path.exists():
        print(f"no log at {path} yet — the capture agent writes every 20 min.")
        return
    rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    print(f"=== kalshi_liquidity rollup — {len(rows)} samples from {path.name} ===")
    print("  series          samples  fillable  maxVol   when-fillable (ET hour)   verdict")
    for s in report_log(rows):
        if s["no_markets"]:
            v = "NO MARKETS (off-season/delisted)"
        elif s["fillable_samples"] == 0:
            v = "DEAD across all samples"
        else:
            v = f"LIVE in {s['fillable_samples']}/{s['samples']} samples"
        print(f"  {s['series']:<14} {s['samples']:>6}  {s['fillable_samples']:>7}  "
              f"{s['max_volume']:>6}   {_et_hours(s['fillable_ts']):<24} {v}")
    print("\n  A series LIVE during its active window (equities ET 9-16, sports mid-game) is")
    print("  the real survivor; one DEAD across its active hours earns the ASOS verdict.")


def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--series", default=",".join(DEFAULT_SERIES),
                    help="comma-separated Kalshi series tickers")
    ap.add_argument("--top", type=int, default=5, help="show top-N markets by volume per series")
    ap.add_argument("--log", metavar="PATH", default=None,
                    help="append a timestamped one-line summary per series (for scheduled "
                         "capture during live-game windows — event-driven books only show "
                         "liquidity while the event is live)")
    ap.add_argument("--report", metavar="PATH", default=None,
                    help="instead of probing, roll up a liquidity_log.jsonl into a per-series summary")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        raise SystemExit(_selftest())
    if args.report:
        _print_report(Path(args.report))
        return

    series_list = [s.strip() for s in args.series.split(",") if s.strip()]
    print("=== kalshi_liquidity — resting-book check (run BEFORE modeling) ===")
    results = run(series_list)
    for s in results:
        if "error" in s:
            print(f"\n  {s['series']:<14} ERROR: {s['error']}")
            continue
        print(f"\n  {s['series']:<14} {verdict(s)}")
        live = [r for r in s["rows"] if r["two_sided"]]
        for r in sorted(live, key=lambda x: -x["volume"])[:args.top]:
            sp = f"{r['spread']}c" if r["spread"] is not None else "—"
            print(f"     {r['ticker']:<28} bid {r['yes_bid']:>2} ask {r['yes_ask']:>2} "
                  f"spread {sp:<4} vol {r['volume']:>4} OI {r['open_interest']:>4}")
    if args.log:
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).isoformat()
        with open(args.log, "a") as f:
            for s in results:
                if "error" in s:
                    continue
                f.write(json.dumps({"ts": ts, "series": s["series"], "n": s["n"],
                                    "fillable": s["fillable"], "volume": s["volume"],
                                    "open_interest": s["open_interest"]}) + "\n")
        print(f"\n  logged {len([s for s in results if 'error' not in s])} series → {args.log}")
    print("\n  Read: only a LIQUID series is worth modeling. For a borderline one, confirm")
    print("  resting DEPTH (sizes) via the orderbook before trusting the quote.")


def _selftest() -> int:
    dead = [{"ticker": "KXHIGHNY-T87", "yes_bid": 0, "yes_ask": 0, "no_bid": 0, "no_ask": 0,
             "volume": 0, "open_interest": 0}]          # ASOS-style empty book
    liquid = [{"ticker": "KXTEMPNYC-T70", "yes_bid": 45, "yes_ask": 52, "volume": 120, "open_interest": 80},
              {"ticker": "KXTEMPNYC-T72", "yes_bid": 0, "yes_ask": 0, "volume": 0, "open_interest": 0}]
    fake = {"DEAD": dead, "LIVE": liquid}
    res = run(["DEAD", "LIVE"], fetch=lambda s: fake[s])
    d, lv = res[0], res[1]
    assert d["fillable"] == 0 and "DEAD" in verdict(d), verdict(d)
    assert lv["fillable"] == 1 and lv["volume"] == 120 and "LIQUID" in verdict(lv), (lv, verdict(lv))
    ml = market_liquidity(liquid[0])
    assert ml["two_sided"] and ml["spread"] == 7, ml
    # rollup: KXINX live in 1 of 2 samples, KXHIGHNY dead in both
    log_rows = [
        {"ts": "2026-06-24T14:00:00+00:00", "series": "KXINX", "n": 5, "fillable": 4, "volume": 900, "open_interest": 500},
        {"ts": "2026-06-24T06:00:00+00:00", "series": "KXINX", "n": 5, "fillable": 0, "volume": 0, "open_interest": 0},
        {"ts": "2026-06-24T14:00:00+00:00", "series": "KXHIGHNY", "n": 8, "fillable": 0, "volume": 0, "open_interest": 0},
    ]
    rep = {r["series"]: r for r in report_log(log_rows)}
    assert rep["KXINX"]["fillable_samples"] == 1 and rep["KXINX"]["max_volume"] == 900, rep["KXINX"]
    assert rep["KXHIGHNY"]["fillable_samples"] == 0 and not rep["KXHIGHNY"]["no_markets"], rep["KXHIGHNY"]
    print("market_liquidity + verdict + rollup OK")
    print("PASS")
    return 0


if __name__ == "__main__":
    main()
