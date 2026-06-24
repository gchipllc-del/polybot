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
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        raise SystemExit(_selftest())

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
    print("market_liquidity + verdict OK (DEAD vs LIQUID classified correctly)")
    print("PASS")
    return 0


if __name__ == "__main__":
    main()
