#!/usr/bin/env python3
"""weather_no_fill_probe — the ONE check the paper ledger can't do: were the cheap-NO
fills actually available IN SIZE?

weather-NO is the book's only real winner (+$3,216, PSR/DSR=1.0, legit 0.95°F nowcast),
and the profit concentrates in the <0.15 NO bucket where the market priced YES at ~0.97.
Every other artifact was ruled out — EXCEPT fill realism: a NO ask at 3-10c near close
may be 1 contract deep, not the ~20 we 'fill' in paper. If that depth isn't there, the
cheap-bucket profit is phantom. This probe measures it directly off the live order book.

  capture : for the current weather-NO candidate markets (or --tickers), snapshot the
            Kalshi order book and log the NO-side DEPTH curve (size buyable at each price).
            Run it as an agent every few minutes during live windows on the trading host.
  report  : join the snapshots to data/weather_paper.jsonl NO fills and report, per trade,
            whether our paper size was actually available at <= our fill price — and what
            FRACTION OF THE P&L came from fills that were really there.

Order-book semantics are venue-subtle, so we DON'T bake one in: every snapshot keeps the
RAW book and we compute NO-availability TWO ways (via YES bids, and via the NO array). The
report declares which interpretation is correct by matching its best-ask to the actual
paper fill price — data decides, not an assumption.

  python scripts/weather_no_fill_probe.py capture --tickers KXTEMPNYCH-26..-T54.99
  python scripts/weather_no_fill_probe.py capture --from-signals
  python scripts/weather_no_fill_probe.py report
  python scripts/weather_no_fill_probe.py selftest
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PROBE_LOG = ROOT / "data" / "weather_no_fill_probe.jsonl"
LEDGER = ROOT / "data" / "weather_paper.jsonl"


# ── pure order-book math (testable; two interpretations) ─────────────────────
# get_orderbook() returns {"bids": [{price,quantity}...]  # the YES-side levels
#                          "asks": [{price,quantity}...]} # the NO-side levels
# On a binary CLOB, buying NO at limit p can be matched EITHER by selling YES into a YES
# bid at price >= 1-p (interpretation A), OR by lifting a resting NO offer at price <= p
# (interpretation B). We compute both and let the report pick the one whose best price
# matches the recorded paper fill.

def no_size_via_yes_bids(book: dict, limit_p: float) -> float:
    """A: contracts of NO buyable at <= limit_p by selling YES into YES bids >= 1-limit_p."""
    thr = 1.0 - limit_p
    return sum(float(b["quantity"]) for b in book.get("bids", [])
               if float(b["price"]) >= thr - 1e-9)


def no_size_via_no_asks(book: dict, limit_p: float) -> float:
    """B: contracts of NO buyable at <= limit_p by lifting NO offers priced <= limit_p."""
    return sum(float(a["quantity"]) for a in book.get("asks", [])
               if float(a["price"]) <= limit_p + 1e-9)


def best_no_ask_via_yes(book: dict):
    ys = [float(b["price"]) for b in book.get("bids", [])]
    return round(1.0 - max(ys), 4) if ys else None


def best_no_ask_via_no(book: dict):
    ns = [float(a["price"]) for a in book.get("asks", [])]
    return round(min(ns), 4) if ns else None


def snapshot(ticker: str, book: dict, strike_f, ts: str) -> dict:
    """One probe row: raw book kept verbatim + both best-ask interpretations + a NO-depth
    curve (cumulative size buyable at a ladder of prices) under each interpretation."""
    ladder = [0.03, 0.05, 0.08, 0.10, 0.15, 0.20, 0.30, 0.45]
    return {
        "ts": ts, "ticker": ticker, "strike_f": strike_f,
        "best_no_ask_via_yes": best_no_ask_via_yes(book),
        "best_no_ask_via_no": best_no_ask_via_no(book),
        "depth_via_yes": {f"{p:.2f}": no_size_via_yes_bids(book, p) for p in ladder},
        "depth_via_no": {f"{p:.2f}": no_size_via_no_asks(book, p) for p in ladder},
        "raw_bids": book.get("bids", []), "raw_asks": book.get("asks", []),
    }


# ── capture (needs the live host / Kalshi creds) ─────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _discover_weather_no_tickers() -> list:
    """Current weather-NO candidate (ticker, strike) pairs, via the live signal pass."""
    try:
        from lib.weather_signal import sample_signals
    except Exception as e:  # noqa: BLE001
        print(f"cannot import weather_signal: {e}")
        return []
    out = []
    for s in sample_signals():
        tk = s.get("market_ticker") or s.get("ticker")
        if tk and (s.get("no_ask") is not None):
            out.append((tk, s.get("strike_f")))
    return out


def capture(tickers: list, get_book=None) -> int:
    """Snapshot each ticker's book and append to PROBE_LOG. get_book is injectable for
    tests; defaults to a live KalshiClient().get_orderbook."""
    if get_book is None:
        from lib.kalshi_client import KalshiClient
        client = KalshiClient()
        get_book = client.get_orderbook
    ts = _now_iso()
    rows = []
    for item in tickers:
        ticker, strike = item if isinstance(item, tuple) else (item, None)
        try:
            book = get_book(ticker)
        except Exception as e:  # noqa: BLE001
            print(f"  {ticker}: book fetch failed ({e})")
            continue
        rows.append(snapshot(ticker, book or {}, strike, ts))
    if rows:
        PROBE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(PROBE_LOG, "a") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
    print(f"captured {len(rows)} book snapshot(s) → {PROBE_LOG}")
    return 0


# ── report (join snapshots to paper fills) ───────────────────────────────────

def _ts(x):
    try:
        t = datetime.fromisoformat(str(x).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _nearest_probe(probes_by_tk: dict, ticker: str, when, window_s: float):
    cands = probes_by_tk.get(ticker, [])
    best, bestdt = None, None
    wt = _ts(when)
    if wt is None:
        return None
    for p in cands:
        pt = _ts(p.get("ts"))
        if pt is None:
            continue
        dt = abs((pt - wt).total_seconds())
        if dt <= window_s and (bestdt is None or dt < bestdt):
            best, bestdt = p, dt
    return best


def _depth_at(probe: dict, interp: str, limit_p: float) -> float:
    """Cumulative NO size buyable at <= limit_p from a snapshot's depth curve (nearest
    ladder rung at or above limit_p), for interpretation 'yes' or 'no'."""
    curve = probe.get(f"depth_via_{interp}", {})
    rungs = sorted(float(k) for k in curve)
    pick = next((r for r in rungs if r >= limit_p - 1e-9), rungs[-1] if rungs else None)
    return float(curve.get(f"{pick:.2f}", 0.0)) if pick is not None else 0.0


def build_report(ledger_rows: list, probe_rows: list, window_min: float = 30.0) -> dict:
    """For each NO paper fill, decide if our size was really available. Picks the correct
    book interpretation by which best-ask matches the paper fills more closely."""
    no = [r for r in ledger_rows if str(r.get("side")).upper() == "NO"
          and r.get("market_ticker") and r.get("fill_price")]
    by_tk = {}
    for p in probe_rows:
        by_tk.setdefault(p.get("ticker"), []).append(p)

    # choose interpretation: smaller mean |best_no_ask - paper fill| wins
    err = {"yes": [], "no": []}
    matched = []
    for r in no:
        pr = _nearest_probe(by_tk, r["market_ticker"], r.get("opened_at"), window_min * 60)
        if not pr:
            continue
        matched.append((r, pr))
        for k in ("yes", "no"):
            ba = pr.get(f"best_no_ask_via_{k}")
            if ba is not None:
                err[k].append(abs(ba - float(r["fill_price"])))
    if not matched:
        return {"matched": 0, "n_no": len(no), "n_probes": len(probe_rows)}
    interp = min(("yes", "no"), key=lambda k: (sum(err[k]) / len(err[k])) if err[k] else 9e9)

    rows, fillable, pnl_real, pnl_tot = [], 0, 0.0, 0.0
    cheap_real = cheap_tot = 0.0
    for r, pr in matched:
        p = float(r["fill_price"])
        need = float(r.get("our_size") or 0)
        avail = _depth_at(pr, interp, p)
        ok = avail >= need - 1e-9
        pnl = float(r.get("paper_pnl") or 0)
        pnl_tot += pnl
        if ok:
            fillable += 1
            pnl_real += pnl
        if p < 0.15:
            cheap_tot += pnl
            if ok:
                cheap_real += pnl
        rows.append({"ticker": r["market_ticker"], "fill": p, "need": round(need, 1),
                     "avail": round(avail, 1), "fillable": ok, "pnl": round(pnl, 2)})
    return {
        "matched": len(matched), "n_no": len(no), "n_probes": len(probe_rows),
        "interp": interp, "fillable": fillable,
        "fillable_frac": round(fillable / len(matched), 3),
        "pnl_total_matched": round(pnl_tot, 2), "pnl_fillable": round(pnl_real, 2),
        "pnl_real_frac": round(pnl_real / pnl_tot, 3) if pnl_tot else None,
        "cheap_pnl_total": round(cheap_tot, 2), "cheap_pnl_fillable": round(cheap_real, 2),
        "cheap_real_frac": round(cheap_real / cheap_tot, 3) if cheap_tot else None,
        "rows": rows,
    }


def _print_report(rep: dict) -> None:
    if rep.get("matched", 0) == 0:
        print(f"no joinable probe data yet — {rep.get('n_probes',0)} snapshots, "
              f"{rep.get('n_no',0)} NO fills, 0 matched within the time window.\n"
              f"Run `capture` on the live host during weather windows first.")
        return
    real_pct = "—" if rep["pnl_real_frac"] is None else f"{100*rep['pnl_real_frac']:.0f}%"
    print(f"matched {rep['matched']}/{rep['n_no']} NO fills to book snapshots "
          f"(interpretation: NO-depth via '{rep['interp']}')")
    print(f"  fillable at our size : {rep['fillable']}/{rep['matched']} "
          f"= {100*rep['fillable_frac']:.0f}%")
    print(f"  P&L from REAL fills   : ${rep['pnl_fillable']:+.2f} of ${rep['pnl_total_matched']:+.2f} "
          f"= {real_pct} (the rest is PHANTOM — unfillable in size)")
    if rep["cheap_real_frac"] is not None:
        print(f"  cheap-NO (<0.15) real : ${rep['cheap_pnl_fillable']:+.2f} of "
              f"${rep['cheap_pnl_total']:+.2f} = {100*rep['cheap_real_frac']:.0f}% "
              f"← the make-or-break bucket")
    bad = [r for r in rep["rows"] if not r["fillable"]]
    if bad:
        print("  worst phantom fills (size not there):")
        for r in sorted(bad, key=lambda r: r["pnl"], reverse=True)[:8]:
            print(f"    {r['ticker']:<30} fill {r['fill']:.2f} need {r['need']:.0f} "
                  f"avail {r['avail']:.0f}  pnl ${r['pnl']:+.2f}")


# ── selftest ─────────────────────────────────────────────────────────────────

def _selftest() -> int:
    # book: YES bids at 0.97 (x3), 0.90 (x10); NO offers at 0.03 (x3), 0.10 (x10)
    book = {"bids": [{"price": 0.97, "quantity": 3}, {"price": 0.90, "quantity": 10}],
            "asks": [{"price": 0.03, "quantity": 3}, {"price": 0.10, "quantity": 10}]}
    # buy NO at 0.03: via YES bids needs y>=0.97 → 3 contracts; via NO asks <=0.03 → 3
    assert no_size_via_yes_bids(book, 0.03) == 3, no_size_via_yes_bids(book, 0.03)
    assert no_size_via_no_asks(book, 0.03) == 3
    # buy NO at 0.10: via YES bids y>=0.90 → 13; via NO asks <=0.10 → 13
    assert no_size_via_yes_bids(book, 0.10) == 13
    assert no_size_via_no_asks(book, 0.10) == 13
    assert best_no_ask_via_yes(book) == 0.03 and best_no_ask_via_no(book) == 0.03
    snap = snapshot("KXT-T55", book, 55.0, "2026-06-01T16:50:00Z")
    assert snap["depth_via_yes"]["0.03"] == 3 and snap["depth_via_no"]["0.10"] == 13

    # report: one fillable cheap fill (need 3 @0.03, avail 3) + one phantom (need 20 @0.03)
    probes = [snap,
              snapshot("KXT-T60", {"bids": [{"price": 0.97, "quantity": 1}], "asks":
                                   [{"price": 0.03, "quantity": 1}]}, 60.0,
                       "2026-06-01T17:50:00Z")]
    ledger = [
        {"side": "NO", "market_ticker": "KXT-T55", "fill_price": 0.03, "our_size": 3,
         "opened_at": "2026-06-01T16:50:30Z", "paper_pnl": 30.0},
        {"side": "NO", "market_ticker": "KXT-T60", "fill_price": 0.03, "our_size": 20,
         "opened_at": "2026-06-01T17:50:30Z", "paper_pnl": 18.0},
    ]
    rep = build_report(ledger, probes, window_min=30)
    assert rep["matched"] == 2, rep
    assert rep["fillable"] == 1, rep                        # only the size-3 fill is real
    assert rep["interp"] in ("yes", "no")
    assert rep["cheap_real_frac"] == round(30.0 / 48.0, 3), rep   # 30 real of 48 cheap pnl
    print("selftest OK")
    _print_report(rep)
    return 0


def _load(p: Path) -> list:
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    cap = sub.add_parser("capture")
    cap.add_argument("--tickers", help="comma-separated market tickers")
    cap.add_argument("--from-signals", action="store_true",
                     help="discover current weather-NO candidate markets via the live signal")
    rep = sub.add_parser("report")
    rep.add_argument("--window-min", type=float, default=30.0)
    sub.add_parser("selftest")
    args = ap.parse_args()

    if args.cmd == "capture":
        if args.from_signals:
            tickers = _discover_weather_no_tickers()
        elif args.tickers:
            tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
        else:
            cap.error("need --tickers or --from-signals")
        if not tickers:
            print("no weather-NO candidate markets right now."); raise SystemExit(0)
        raise SystemExit(capture(tickers))
    if args.cmd == "report":
        _print_report(build_report(_load(LEDGER), _load(PROBE_LOG), args.window_min))
        return
    if args.cmd == "selftest":
        raise SystemExit(_selftest())
    ap.print_help()


if __name__ == "__main__":
    main()
