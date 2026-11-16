#!/usr/bin/env python3
"""sleeve_scan - search ALL of Kalshi for a consistent edge, without fooling ourselves.

WHY. Two sleeves have been screened one at a time (15-min crypto, daily-high weather)
and both came back closed. Kalshi lists hundreds of other series - index ranges, crypto
dailies, economics releases, sports, politics, entertainment - and some of them may be
priced worse. This scans them all with ONE consistent, honest test.

THE TRAP THIS IS BUILT AROUND. Searching many sleeves manufactures fake edges: test 40
sleeves x 80 cells each at 95% and ~160 "winners" appear by luck. So the whole search is
ONE family of hypotheses with a false-discovery-rate correction (Benjamini-Hochberg),
and what it produces is NOT a verdict - it is a NOMINATION for the forward paper test,
which remains the only thing that can earn money.

FOUR GUARDS (each one paid for by an earlier mistake in this project):
  1. INDEPENDENCE  - the unit is the EVENT. All strikes of one event (13 temperature
     buckets of one day, every strike of one crypto window, the brackets of one CPI print)
     resolve on ONE outcome: they are one bet, not thirteen.
  2. TIME, FAIRLY  - entries are banded by FRACTION OF MARKET LIFE (early/mid/late/final),
     so a 15-minute market and a 30-day market are compared on the same footing.
  3. WALK-FORWARD  - every cell must be positive on the first half of dates (in sample)
     AND pass the corrected test on the second half (out of sample).
  4. FILLS         - a candle's QUOTE is not a fill. Each nominated cell is re-scored on
     trade-confirmed entries only (a trade printed at or through our price in that candle).
     An edge that lives only in untraded quotes is the phantom-liquidity pattern that
     produced the late-longshot "edge" - flagged, never nominated as clean.

  py scripts/sleeve_scan.py probe                        # what the API returns (paste me)
  py scripts/sleeve_scan.py discover [--pages 40]        # rank every series with settled events
  py scripts/sleeve_scan.py backfill [--top 20] [--events 60] [--series A,B]
  py scripts/sleeve_scan.py screen [--files a.jsonl b.jsonl]
  py scripts/sleeve_scan.py selftest                     # fixtures only, no network

Runs on the host (the dev sandbox cannot reach Kalshi). Public endpoints only, no auth,
no orders. Backfill is resumable: re-run the same command after any interruption.
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from forecast_replay import MIN_CELL_WINDOWS, mean_ci, norm_cdf, taker_fee  # noqa: E402
import history_backfill as hb  # noqa: E402

SLEEVE_DIR = Path(os.environ.get("SLEEVE_DIR") or (ROOT / "data" / "sleeves"))
CATALOG = SLEEVE_DIR / "catalog.json"

# ── frozen screen design (pre-registered 2026-11-16; never tuned to results) ──────
LIFE_BANDS = (("early", 0.00, 0.25), ("mid", 0.25, 0.75),
              ("late", 0.75, 0.95), ("final", 0.95, 1.0001))
BUCKETS = ((0.01, 0.05), (0.05, 0.10), (0.10, 0.20), (0.20, 0.35), (0.35, 0.50),
           (0.50, 0.65), (0.65, 0.80), (0.80, 0.90), (0.90, 0.95), (0.95, 0.99))
MAX_SPREAD = 0.15        # wider than this is a placeholder shell, not a book
FDR_Q = 0.10             # nomination threshold: <=10% of nominations expected false
MIN_EVENTS = MIN_CELL_WINDOWS   # per half - the same floor as every screen here
MAX_MARKETS_PER_EVENT = 15


# ── small pure helpers ────────────────────────────────────────────────────────

def bucket_of(price: float) -> str | None:
    for lo, hi in BUCKETS:
        if lo <= price < hi:
            return f"{int(round(lo * 100)):02d}-{int(round(hi * 100)):02d}c"
    return None


def band_of(frac: float) -> str | None:
    for name, lo, hi in LIFE_BANDS:
        if lo <= frac < hi:
            return name
    return None


def candle_interval(open_ts: float | None, close_ts: float | None) -> int:
    """Kalshi candle period (minutes) that resolves a market's life in ~100-1500 bars."""
    if open_ts is None or close_ts is None:
        return 60
    life_h = (close_ts - open_ts) / 3600.0
    if life_h <= 6:
        return 1
    if life_h <= 24 * 30:
        return 60
    return 1440


def event_of(market: dict) -> str:
    ev = market.get("event_ticker")
    if ev:
        return str(ev)
    parts = str(market.get("ticker") or "").rsplit("-", 1)
    return parts[0] if len(parts) == 2 else str(market.get("ticker"))


def _num(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _px(v) -> float | None:
    x = _num(v)
    if x is None:
        return None
    return x / 100.0 if x > 1.0 else x


def _ohlc(c: dict, name: str, field: str) -> float | None:
    """One OHLC field of a candle series (yes_bid/yes_ask/price), cents or dollars,
    nested or flat - the Kalshi/ccxt shapes history_backfill already met."""
    for key in (f"{name}_dollars", name):
        v = c.get(key)
        if isinstance(v, dict):
            got = v.get(f"{field}_dollars", v.get(field))
            if got is not None:
                return _px(got)
        elif v is not None and field == "close":
            return _px(v)
    return None


def p_value(mean: float, sample_se: float, rows: list[dict]) -> float:
    """One-sided p for 'this cell's per-event P&L is > 0', judged against the variance a
    FAIR price would produce - never less.

    Why the floor: a bet on a 94c favorite rarely loses, so a small sample often contains
    NO loss at all; every outcome is then the same +5c, the sample variance is zero, and a
    naive test reports p = 0 - "proven edge". That is the "it hasn't lost yet" trap, and
    the measured detection table caught it (a 3c edge was 'found' at 80 events and missed
    at 640). Under no edge, a contract bought at ask A wins with probability A + fee, so
    each entry's null variance is (A+fee)(1-A-fee); an event's mean over several entries
    has at most the mean of those variances. The test uses max(sample, null) variance."""
    events: dict[str, list[float]] = {}
    for r in rows:
        pi0 = min(0.999, max(0.001, r["ask"] + taker_fee(r["ask"])))
        events.setdefault(r["event"], []).append(pi0 * (1.0 - pi0))
    if not events:
        return 1.0
    null_var = sum(sum(v) / len(v) for v in events.values()) / len(events)
    se = max(sample_se, math.sqrt(null_var / len(events)))
    return 1.0 - norm_cdf(mean / se) if se > 0 else (0.0 if mean > 0 else 1.0)


def bh_select(pvals: list[float], q: float = FDR_Q) -> set[int]:
    """Benjamini-Hochberg: indices whose p-value survives FDR control at level q."""
    m = len(pvals)
    order = sorted(range(m), key=lambda i: pvals[i])
    k = 0
    for rank, i in enumerate(order, start=1):
        if pvals[i] <= q * rank / m:
            k = rank
    return set(order[:k])


# ── loading: any history_backfill-format file ─────────────────────────────────

def load_sleeve_file(path: Path) -> dict:
    """-> {series: {"markets": {ticker: {...}}, "candles": {ticker: [(ts, yb, ya,
    t_lo, t_hi, vol), ...]}}}. Streams; keeps only formed-book candles."""
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            series = d.get("series")
            if not series:
                continue
            s = out.setdefault(series, {"markets": {}, "candles": {}})
            if d.get("t") == "market":
                if d.get("result") not in ("yes", "no"):
                    continue
                s["markets"][d["ticker"]] = {
                    "ticker": d["ticker"], "event": event_of(d),
                    "open_ts": hb._iso_to_ts(d.get("open_time")),
                    "close_ts": hb._iso_to_ts(d.get("close_time")),
                    "result": d["result"], "category": d.get("category")}
            elif d.get("t") == "candle":
                ts = _num(d.get("end_period_ts"))
                yb, ya = _ohlc(d, "yes_bid", "close"), _ohlc(d, "yes_ask", "close")
                if ts is None or yb is None or ya is None:
                    continue
                if not (0.01 <= yb < ya <= 0.99 and ya - yb <= MAX_SPREAD):
                    continue                       # unformed / placeholder book
                vol = _num(d.get("volume")) or 0.0
                s["candles"].setdefault(d["ticker"], []).append(
                    (ts, yb, ya, _ohlc(d, "price", "low"), _ohlc(d, "price", "high"), vol))
    return out


# ── entries ───────────────────────────────────────────────────────────────────

def entries_for(sleeve: dict, series: str) -> list[dict]:
    """First formed book per (market, life band); both sides at their asks.
    trade_ok: a trade printed in that candle at or through our price - buying YES at
    ask A needs a print <= A; buying NO (= selling YES at bid B) needs a print >= B."""
    out = []
    for tk, m in sleeve["markets"].items():
        o, c = m["open_ts"], m["close_ts"]
        if o is None or c is None or c <= o:
            continue
        seen_bands: set[str] = set()
        for ts, yb, ya, t_lo, t_hi, vol in sorted(sleeve["candles"].get(tk, [])):
            band = band_of((ts - o) / (c - o))
            if band is None or band in seen_bands:
                continue
            seen_bands.add(band)
            won_yes = m["result"] == "yes"
            day = datetime.fromtimestamp(c, tz=timezone.utc).date().isoformat()
            traded = vol > 0
            for side, ask, win, tok in (
                    ("yes", ya, won_yes, traded and t_lo is not None and t_lo <= ya + 1e-9),
                    ("no", round(1.0 - yb, 4), not won_yes,
                     traded and t_hi is not None and t_hi >= yb - 1e-9)):
                b = bucket_of(ask)
                if b is None:
                    continue
                fee = taker_fee(ask)
                out.append({"series": series, "event": m["event"], "day": day,
                            "band": band, "side": side, "bucket": b, "ask": ask,
                            "pnl": (1.0 - ask - fee) if win else (-ask - fee),
                            "win": win, "trade_ok": tok, "category": m.get("category")})
    return out


def _per_event(rows: list[dict]) -> list[tuple[str, float]]:
    """One observation per EVENT: the mean P&L of that event's entries in the cell."""
    agg: dict[str, list] = {}
    for r in rows:
        agg.setdefault(r["event"], []).append(r)
    return [(v[0]["day"], sum(x["pnl"] for x in v) / len(v)) for v in agg.values()]


# ── the screen ────────────────────────────────────────────────────────────────

def screen(entries: list[dict]) -> dict:
    """One family: every (series, band, side, bucket) cell with MIN_EVENTS events on each
    side of its sleeve's date cut. Walk-forward, BH across the WHOLE family."""
    by_series: dict[str, list] = {}
    for e in entries:
        by_series.setdefault(e["series"], []).append(e)
    cells = []
    for series, rows in by_series.items():
        days = sorted({r["day"] for r in rows})
        if len(days) < 4:
            continue
        cut = days[len(days) // 2 - 1]            # each sleeve split at its own midpoint
        grouped: dict[tuple, list] = {}
        for r in rows:
            grouped.setdefault((r["band"], r["side"], r["bucket"]), []).append(r)
        for (band, side, bucket), rs in grouped.items():
            ev = _per_event(rs)
            a = [p for d, p in ev if d <= cut]
            b = [p for d, p in ev if d > cut]
            if len(a) < MIN_EVENTS or len(b) < MIN_EVENTS:
                continue
            ma, _, _ = mean_ci(a)
            mb, _, hi = mean_ci(b)
            p = p_value(mb, (hi - mb) / 1.96, [x for x in rs if x["day"] > cut])
            tr = [x for x in rs if x["trade_ok"]]
            tr_ev = _per_event(tr)
            tr_b = [pp for d, pp in tr_ev if d > cut]
            cells.append({"series": series, "band": band, "side": side, "bucket": bucket,
                          "n_in": len(a), "mean_in": ma, "n_out": len(b), "mean_out": mb,
                          "p_out": p, "cut": cut,
                          "n_out_traded": len(tr_b),
                          "mean_out_traded": (sum(tr_b) / len(tr_b)) if tr_b else None})
    survivors = bh_select([c["p_out"] for c in cells])
    for i, c in enumerate(cells):
        if i in survivors and c["mean_in"] > 0 and c["mean_out"] > 0:
            phantom = (c["n_out_traded"] < MIN_EVENTS // 2 or c["mean_out_traded"] is None
                       or c["mean_out_traded"] <= 0)
            c["verdict"] = "NOMINATED - phantom risk" if phantom else "NOMINATED"
        elif c["mean_in"] > 0 and c["mean_out"] > 0:
            c["verdict"] = "persists, not significant"
        elif c["mean_in"] > 0:
            c["verdict"] = "flipped"
        else:
            c["verdict"] = "-"
    return {"cells": cells, "family": len(cells),
            "nominated": [c for c in cells if c["verdict"].startswith("NOMINATED")]}


def calibration_by_category(entries: list[dict]) -> dict:
    """Descriptive: realized win rate vs paid price per bucket, per category (excluding
    the final band, where prices converge on the known answer)."""
    out: dict[str, dict] = {}
    for e in entries:
        if e["band"] == "final":
            continue
        cat = e.get("category") or "uncategorized"
        cell = out.setdefault(cat, {}).setdefault(e["bucket"], [0, 0.0, 0])
        cell[0] += 1
        cell[1] += e["ask"]
        cell[2] += 1 if e["win"] else 0
    return out


# ── network commands (host only) ──────────────────────────────────────────────

def cmd_probe(get=hb._get) -> int:
    print("=" * 74)
    print("SLEEVE PROBE - what do Kalshi's public endpoints return from this machine?")
    print("=" * 74)
    try:
        d = get(f"{hb.KALSHI}/events", {"status": "settled", "limit": 3})
        evs = d.get("events", [])
        print(f"/events?status=settled -> {len(evs)} events; keys of first: "
              f"{sorted(evs[0])[:14] if evs else '-'}")
        for e in evs[:3]:
            print(f"  {e.get('series_ticker')} | {e.get('category')} | {str(e.get('title'))[:60]}")
    except Exception as e:  # noqa: BLE001
        print(f"/events FAILED: {type(e).__name__}: {str(e)[:100]}")
        return 1
    return 0


def cmd_discover(pages: int = 40, get=hb._get) -> int:
    """Rank series by settled events seen in the most recent `pages` x 200 events."""
    seen: dict[str, dict] = {}
    cursor = None
    for i in range(pages):
        params = {"status": "settled", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        try:
            d = get(f"{hb.KALSHI}/events", params)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] page {i}: {type(e).__name__}: {str(e)[:80]}")
            break
        for e in d.get("events", []):
            s = e.get("series_ticker")
            if not s:
                continue
            rec = seen.setdefault(s, {"series": s, "category": e.get("category"),
                                      "title": str(e.get("title") or "")[:80], "events": 0})
            rec["events"] += 1
        cursor = d.get("cursor")
        if not cursor:
            break
        time.sleep(hb.SLEEP_S)
    cat = sorted(seen.values(), key=lambda r: -r["events"])
    SLEEVE_DIR.mkdir(parents=True, exist_ok=True)
    CATALOG.write_text(json.dumps(cat, indent=1), encoding="utf-8")
    print(f"{len(cat)} series with settled events -> {CATALOG}")
    print(f"  {'series':22} {'category':16} {'events':>6}  title")
    for r in cat[:40]:
        print(f"  {r['series']:22} {str(r['category'])[:16]:16} {r['events']:>6}  {r['title'][:44]}")
    return 0


def cmd_backfill(series_list: list[str], events_per_series: int, get=hb._get) -> int:
    """Most recent `events_per_series` settled events per sleeve, every market in them
    (capped), candles at a resolution matched to market life. Resumable."""
    SLEEVE_DIR.mkdir(parents=True, exist_ok=True)
    cats = {}
    if CATALOG.exists():
        cats = {r["series"]: r.get("category") for r in json.loads(CATALOG.read_text())}
    for series in series_list:
        path = SLEEVE_DIR / f"{series}.jsonl"
        done = hb._seen_tickers(path)
        ms = hb.fetch_settled_markets(series, get=get,
                                      max_markets=events_per_series * MAX_MARKETS_PER_EVENT * 2)
        by_ev: dict[str, list] = {}
        for m in ms:
            if m.get("result") in ("yes", "no") and m.get("ticker"):
                by_ev.setdefault(event_of(m), []).append(m)
        evs = sorted(by_ev.items(), key=lambda kv: max(str(x.get("close_time")) for x in kv[1]),
                     reverse=True)[:events_per_series]
        todo = [m for _, group in evs for m in group[:MAX_MARKETS_PER_EVENT]
                if m["ticker"] not in done]
        print(f"{series}: {len(evs)} events, {len(todo)} markets to fetch -> {path.name}")
        n = 0
        with open(path, "a", encoding="utf-8") as f:
            for m in todo:
                o, c = hb._iso_to_ts(m.get("open_time")), hb._iso_to_ts(m.get("close_time"))
                if o is None or c is None:
                    continue
                try:
                    d = get(f"{hb.KALSHI}/series/{series}/markets/{m['ticker']}/candlesticks",
                            {"period_interval": candle_interval(o, c),
                             "start_ts": o, "end_ts": c})
                except Exception as e:  # noqa: BLE001
                    print(f"  [warn] {m['ticker']}: {type(e).__name__}")
                    continue
                cs = d.get("candlesticks", []) or d.get("candles", []) or []
                f.write(json.dumps({"t": "market", "series": series, "ticker": m["ticker"],
                                    "event_ticker": m.get("event_ticker") or event_of(m),
                                    "category": cats.get(series), "result": m.get("result"),
                                    "open_time": m.get("open_time"),
                                    "close_time": m.get("close_time"),
                                    "volume": m.get("volume")}, separators=(",", ":")) + "\n")
                for cd in cs:
                    f.write(json.dumps({"t": "candle", "series": series,
                                        "ticker": m["ticker"], **cd},
                                       separators=(",", ":")) + "\n")
                n += 1
                if n % 50 == 0:
                    print(f"  {n}/{len(todo)}")
                time.sleep(hb.SLEEP_S)
    return 0


# ── report ────────────────────────────────────────────────────────────────────

def cmd_screen(files: list[Path]) -> int:
    print("=" * 78)
    print("SLEEVE SCAN - every Kalshi sleeve, one honest test")
    print("=" * 78)
    entries = []
    for p in files:
        for series, sleeve in load_sleeve_file(p).items():
            es = entries_for(sleeve, series)
            entries.extend(es)
            print(f"  {series:22} {len(sleeve['markets']):>6} markets "
                  f"{len({e['event'] for e in es}):>5} events  ({p.name})")
    if not entries:
        print("no data - run discover + backfill first (or pass --files).")
        return 1
    res = screen(entries)
    print()
    print(f"FAMILY: {res['family']} cells with >= {MIN_EVENTS} events on each side of "
          f"their sleeve's date cut")
    print(f"Benjamini-Hochberg at q={FDR_Q}: nominations are expected to be <= "
          f"{int(FDR_Q * 100)}% false - which is why they go to forward paper, not money.")
    print()
    rows = sorted(res["cells"], key=lambda c: c["p_out"])
    print(f"  {'series':18} {'band':6} {'side':4} {'bucket':7} {'n_in':>5} {'$/ev in':>8} "
          f"{'n_out':>5} {'$/ev out':>9} {'p':>8} {'traded out':>11}  verdict")
    for c in rows[:30]:
        tr = (f"{c['mean_out_traded']:+.3f}/{c['n_out_traded']}"
              if c["mean_out_traded"] is not None else "-")
        print(f"  {c['series'][:18]:18} {c['band']:6} {c['side']:4} {c['bucket']:7} "
              f"{c['n_in']:>5} {c['mean_in']:>+8.3f} {c['n_out']:>5} {c['mean_out']:>+9.3f} "
              f"{c['p_out']:>8.1e} {tr:>11}  {c['verdict']}")
    print()
    nom = res["nominated"]
    clean = [c for c in nom if c["verdict"] == "NOMINATED"]
    print(f"VERDICT: {len(clean)} clean nomination(s), "
          f"{len(nom) - len(clean)} phantom-risk, out of {res['family']} cells searched")
    if clean:
        print("  Next step for each clean nomination: pre-register it (a named_at rule)")
        print("  and paper-trade it FORWARD with the depth gate. Nothing here moves money.")
    else:
        print("  No sleeve shows an unconditional mispricing that survives the search")
        print("  correction. The remaining route is CONDITIONAL sleeves - a stronger")
        print("  independent price (options-implied, de-vigged sharp sportsbook lines,")
        print("  official nowcasts, the settlement station itself) - see docs/SLEEVES.md.")
    cal = calibration_by_category(entries)
    if cal:
        print()
        print("CALIBRATION BY CATEGORY (descriptive; final band excluded): win% vs paid")
        for cat, buckets in sorted(cal.items()):
            parts = []
            for lo, hi in BUCKETS:
                b = f"{int(round(lo * 100)):02d}-{int(round(hi * 100)):02d}c"
                if b in buckets and buckets[b][0] >= 30:
                    n, cost, wins = buckets[b]
                    parts.append(f"{b} {wins / n:.0%}/{cost / n:.0%}")
            if parts:
                print(f"  {cat[:16]:16} " + "  ".join(parts[:6]))
    SLEEVE_DIR.mkdir(parents=True, exist_ok=True)
    (SLEEVE_DIR / "screen.json").write_text(json.dumps(
        {"generated": datetime.now(timezone.utc).isoformat(), "family": res["family"],
         "fdr_q": FDR_Q, "nominated": nom}, indent=1, default=str), encoding="utf-8")
    print("=" * 78)
    return 0


# ── selftest: three synthetic sleeves ─────────────────────────────────────────

def make_sleeve(series: str, mode: str, n_events: int = 400, strikes: int = 6,
                seed: int = 1, bias: float = 0.12) -> list[dict]:
    """Synthetic history_backfill rows. 'efficient': quotes = true probability +- 1c,
    trades print inside the spread. 'biased': longshots (true p < 0.2) quoted 12c rich
    on both sides of the book, so buying NO on them is +EV after fees - a LARGE planted
    favorite-longshot bias (a 4c one is undetectable at this size after the search
    correction - measured; see docs/SLEEVES.md). 'phantom': the same bias, but no trade ever prints near the
    biased quotes - the edge exists only in untraded quotes."""
    import random
    rnd = random.Random(seed)
    rows = []
    t0 = 1_780_000_000
    for ev in range(n_events):
        o = t0 + ev * 86400
        c = o + 6 * 3600
        for k in range(strikes):
            p_true = [0.04, 0.09, 0.15, 0.35, 0.6, 0.85][k % 6]
            won = rnd.random() < p_true
            tk = f"{series}-E{ev:04d}-S{k}"
            rows.append({"t": "market", "series": series, "ticker": tk,
                         "event_ticker": f"{series}-E{ev:04d}", "category": "Test",
                         "result": "yes" if won else "no",
                         "open_time": datetime.fromtimestamp(o, tz=timezone.utc).isoformat(),
                         "close_time": datetime.fromtimestamp(c, tz=timezone.utc).isoformat()})
            for j in (29, 119, 299, 349):     # one candle per life band: .08 .33 .83 .97
                q = p_true + (bias if mode in ("biased", "phantom") and p_true < 0.2 else 0.0)
                yb, ya = round(max(0.01, q - 0.01), 2), round(min(0.99, q + 0.01), 2)
                if mode == "phantom" and p_true < 0.2:
                    lo = hi = round(p_true, 2)         # trades print at fair, never at the quote
                else:
                    lo, hi = yb, ya
                rows.append({"t": "candle", "series": series, "ticker": tk,
                             "end_period_ts": o + j * 60 + 60,
                             "yes_bid": {"close": int(round(yb * 100))},
                             "yes_ask": {"close": int(round(ya * 100))},
                             "price": {"low": int(round(lo * 100)), "high": int(round(hi * 100))},
                             "volume": 10})
    return rows


def _write(rows: list[dict], path: Path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _selftest() -> int:
    import tempfile
    assert bucket_of(0.07) == "05-10c" and bucket_of(0.995) is None
    assert band_of(0.10) == "early" and band_of(0.97) == "final" and band_of(1.0) == "final"
    assert candle_interval(0, 3600) == 1 and candle_interval(0, 86400 * 5) == 60
    assert candle_interval(0, 86400 * 90) == 1440
    assert event_of({"ticker": "KXHIGHNY-26NOV16-B57.5"}) == "KXHIGHNY-26NOV16"
    assert _ohlc({"yes_bid": {"close": 42}}, "yes_bid", "close") == 0.42
    assert _ohlc({"yes_bid_dollars": {"close_dollars": "0.4200"}}, "yes_bid", "close") == 0.42
    # BH: one tiny p among noise survives; pure noise yields nothing
    assert bh_select([0.0001] + [0.5] * 99) == {0}
    assert bh_select([0.3, 0.6, 0.9, 0.5]) == set()
    never_lost = [{"event": f"E{i}", "ask": 0.94, "day": "d"} for i in range(40)]
    assert p_value(0.05, 0.0, never_lost) > 0.05, "40 straight wins at 94c is not proof"

    with tempfile.TemporaryDirectory() as td:
        files = []
        for name, mode in (("EFF", "efficient"), ("BIAS", "biased"), ("PHAN", "phantom")):
            p = Path(td) / f"{name}.jsonl"
            _write(make_sleeve(name, mode, seed=len(name)), p)
            files.append(p)
        entries = []
        for p in files:
            for series, sl in load_sleeve_file(p).items():
                entries.extend(entries_for(sl, series))
        # independence: 6 strikes per event must never count as 6 observations
        res = screen(entries)
        for c in res["cells"]:
            assert c["n_in"] + c["n_out"] <= 400, c
        noms = res["nominated"]
        clean = {c["series"] for c in noms if c["verdict"] == "NOMINATED"}
        phantom = {c["series"] for c in noms if c["verdict"] != "NOMINATED"}
        assert "EFF" not in clean | phantom, f"efficient sleeve nominated: {noms}"
        assert "BIAS" in clean, f"planted bias not found: {noms}"
        assert all(c["side"] == "no" for c in noms if c["series"] == "BIAS"), noms
        assert "PHAN" not in clean and "PHAN" in phantom, f"phantom edge not flagged: {noms}"
    print("selftest OK")
    return 0


def _arg(args: list[str], name: str, default=None):
    return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) \
        else default


def main() -> int:
    args = sys.argv[1:]
    cmd = args[0] if args else ""
    if cmd == "selftest":
        return _selftest()
    if cmd == "probe":
        return cmd_probe()
    if cmd == "discover":
        return cmd_discover(int(_arg(args, "--pages", 40)))
    if cmd == "backfill":
        if _arg(args, "--series"):
            series = [s.strip() for s in _arg(args, "--series").split(",") if s.strip()]
        else:
            if not CATALOG.exists():
                print("run `discover` first (or pass --series A,B)")
                return 1
            series = [r["series"] for r in json.loads(CATALOG.read_text())][
                :int(_arg(args, "--top", 20))]
        return cmd_backfill(series, int(_arg(args, "--events", 60)))
    if cmd == "screen":
        if "--files" in args:
            files = [Path(a) for a in args[args.index("--files") + 1:]]
        else:
            files = sorted(SLEEVE_DIR.glob("*.jsonl"))
        return cmd_screen(files)
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
