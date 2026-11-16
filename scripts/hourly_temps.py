#!/usr/bin/env python3
"""hourly_temps - Kalshi HOURLY temperature markets: settlements, station readings and
the conditional "buy the side the live reading already favors" history check.

THE MARKETS. One event per city per clock hour ("Temperature in Coastal Los Angeles on
Nov 15, 2026 at 10pm EST?"), ~10 "greater" strikes 1 degF apart ("67 deg or above" =
floor_strike 66.99). Each market opens exactly 1 hour before the settlement time and
closes AT it. Settlement: the "Kalshi Weather Index" value from Synoptic Data for that
exact time (the market's expiration_value). See docs/HOURLY_TEMPS.md for the quoted rules.

THE METHOD UNDER TEST. Late in the hour, the live temperature is already known to
within a degree or two. Strikes whose line sits well away from the current reading are
near-certain; buy that side at 97-99c and collect 1-3c. The question is whether the
market's 1-3c discount is MORE than the true chance of a reading crossing the line.

  py scripts/hourly_temps.py settlements --series A,B [--max 3000]   # -> data/hourly/
  py scripts/hourly_temps.py stations --series A,B                   # IEM ASOS -> data/hourly/
  py scripts/hourly_temps.py calibrate                               # which station/reading
  py scripts/hourly_temps.py conditional [--final data/sleeves/final]
  py scripts/hourly_temps.py selftest

Public endpoints only (Kalshi trade API v2, no auth; Iowa Environmental Mesonet ASOS
archive). Read-only, no orders. Every fetcher is injectable; tests use fakes.
"""
from __future__ import annotations

import csv
import io
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

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
IEM_ASOS = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
IEM_1MIN = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos1min.py"
OUT_DIR = Path(os.environ.get("HOURLY_DIR") or (ROOT / "data" / "hourly"))

# Current (Synoptic-settled) hourly series -> candidate ASOS stations to calibrate.
# The FIRST candidate is the prior guess; `calibrate` reports which one actually matches.
SERIES = {
    "KXTEMPNYCHS": {"city": "New York City", "stations": ["NYC", "LGA", "JFK", "EWR"]},
    "KXTEMPCHIHS": {"city": "Chicago Metro Area", "stations": ["ORD", "MDW"]},
    "KXTEMPMIAH": {"city": "Miami", "stations": ["MIA", "TMB", "OPF", "FLL"]},
    "KXTEMPLAXHS": {"city": "Coastal Los Angeles", "stations": ["LAX", "SMO", "LGB", "HHR"]},
}

# Pre-declared conditional grid (2026-11-16, before any conditional result was seen).
MARGINS = (1.0, 2.0, 3.0)
ENTRY_MINUTES = (60, 30)
LO, HI = 0.97, 0.99
CONTRACTS = 10


# ── fetchers (injectable) ─────────────────────────────────────────────────────

def retrying_get(url: str, params: dict | None = None, tries: int = 8) -> dict:
    """GET JSON; backs off on 429/5xx (Kalshi rate-limits bursts of public calls)."""
    import requests
    r = None
    for i in range(tries):
        r = requests.get(url, params=params or {}, timeout=30)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(min(1.0 * (i + 1), 8.0))
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()
    return {}


def retrying_text(url: str, params: dict | None = None, tries: int = 6) -> str:
    import requests
    r = None
    last_exc = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params or {}, timeout=120)
        except requests.RequestException as e:      # proxy resets happen on long pulls
            last_exc = e
            time.sleep(5.0 * (i + 1))
            continue
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(5.0 * (i + 1))
            continue
        r.raise_for_status()
        return r.text
    if r is not None:
        r.raise_for_status()
    raise last_exc or RuntimeError(f"no response from {url}")


# ── small pure helpers ────────────────────────────────────────────────────────

def iso_ts(iso) -> float | None:
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _num(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def line_of(strike: float) -> float:
    """The degree a 'greater' strike's YES starts at: 66.99 -> 67.0 ('67 or above')."""
    return round(strike + 0.01, 2)


def favored(strike: float, reading: float) -> tuple[str, float]:
    """(side the reading currently favors, margin in degF to the strike's line).
    YES iff value > strike, so a reading AT the line (67 vs 66.99) favors YES with
    margin 0; 68 vs '>= 69' favors NO with margin 1."""
    k = line_of(strike)
    if reading > strike:
        return "yes", round(reading - k, 2)
    return "no", round(k - reading, 2)


def latest_at(obs: list[tuple[float, float]], ts: float, max_age_s: float = 3 * 3600):
    """Latest (obs_ts, value) with obs_ts <= ts and not older than max_age_s; obs sorted."""
    import bisect
    i = bisect.bisect_right(obs, (ts, float("inf"))) - 1
    if i < 0 or ts - obs[i][0] > max_age_s:
        return None
    return obs[i]


def event_rows(markets: list[dict]) -> list[dict]:
    """Settled market dicts -> one row per event: settle time, value, strikes."""
    ev: dict[str, dict] = {}
    for m in markets:
        if m.get("result") not in ("yes", "no") or m.get("strike_type") != "greater":
            continue
        strike = _num(m.get("floor_strike"))
        if strike is None:
            continue
        e = ev.setdefault(m["event_ticker"], {
            "event": m["event_ticker"], "series": m["event_ticker"].split("-")[0],
            "open_ts": iso_ts(m.get("open_time")), "settle_ts": iso_ts(m.get("close_time")),
            "value": _num(m.get("expiration_value")), "strikes": []})
        e["strikes"].append({"ticker": m["ticker"], "strike": strike, "result": m["result"]})
    for e in ev.values():
        e["strikes"].sort(key=lambda s: s["strike"])
    return sorted(ev.values(), key=lambda e: e["settle_ts"] or 0)


def consistent(e: dict) -> bool:
    """Do the strikes' results agree with the reported value (value > strike <=> yes)?"""
    v = e.get("value")
    if v is None:
        return False
    return all((v > s["strike"]) == (s["result"] == "yes") for s in e["strikes"])


def parse_iem_csv(text: str, value_col: str = "tmpf") -> dict[str, list[tuple[float, float]]]:
    """IEM asos.py / asos1min.py CSV -> {station: sorted [(ts, degF)]}; 'M' rows skipped."""
    out: dict[str, list] = {}
    rdr = csv.DictReader(io.StringIO(text))
    for row in rdr:
        st = row.get("station")
        t = row.get("valid") or row.get("valid(UTC)")
        v = _num(row.get(value_col))
        if not st or not t or v is None:
            continue
        try:
            ts = datetime.strptime(t.strip(), "%Y-%m-%d %H:%M").replace(
                tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
        out.setdefault(st, []).append((ts, v))
    for v in out.values():
        v.sort()
    return out


def f_to_c_tenths(f: float) -> float:
    return round((f - 32.0) / 1.8, 1)


def c_tenths_to_f(c: float) -> float:
    return round(c * 1.8 + 32.0, 2)


# ── settlements ───────────────────────────────────────────────────────────────

def fetch_settled(series: str, get=retrying_get, max_markets: int = 30000,
                  sleep_s: float = 0.3) -> list[dict]:
    """Every settled market of a series (cursor-paginated), slimmed to what we use."""
    keep = ("ticker", "event_ticker", "strike_type", "floor_strike", "cap_strike",
            "open_time", "close_time", "result", "expiration_value", "yes_sub_title")
    out, cursor = [], None
    while len(out) < max_markets:
        params = {"series_ticker": series, "status": "settled", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        d = get(f"{KALSHI}/markets", params)
        ms = d.get("markets", []) or []
        out.extend({k: m.get(k) for k in keep} for m in ms)
        cursor = d.get("cursor")
        if not cursor or not ms:
            break
        time.sleep(sleep_s)
    return out


def cmd_settlements(series_list: list[str], get=retrying_get, max_markets: int = 30000) -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for s in series_list:
        ms = fetch_settled(s, get=get, max_markets=max_markets)
        evs = event_rows(ms)
        path = OUT_DIR / f"settle_{s}.jsonl"
        with open(path, "w", encoding="utf-8") as f:
            for e in evs:
                f.write(json.dumps(e, separators=(",", ":")) + "\n")
        bad = sum(1 for e in evs if not consistent(e))
        span = ""
        if evs:
            span = (f"{datetime.fromtimestamp(evs[0]['settle_ts'], tz=timezone.utc):%Y-%m-%d} .. "
                    f"{datetime.fromtimestamp(evs[-1]['settle_ts'], tz=timezone.utc):%Y-%m-%d}")
        print(f"{s}: {len(ms)} markets, {len(evs)} events {span}; "
              f"{bad} events whose results disagree with the reported value -> {path.name}")
    return 0


def load_settlements(series: str) -> list[dict]:
    path = OUT_DIR / f"settle_{series}.jsonl"
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


# ── station readings (IEM ASOS archive) ───────────────────────────────────────

# Proxy members for the index's minute-to-minute CHANGE. Miami's five are the members
# named in Kalshi's CFTC filing (per a web-search summary; the filing itself could not
# be fetched from this sandbox). The others are plausible metro ASOS sites: the proxy
# is only used for the change over the last 30 minutes, anchored to the previous hour's
# published settlement value, and `calibrate` measures how well that works.
PROXY = {
    "KXTEMPNYCHS": ["NYC", "LGA", "JFK", "EWR", "TEB", "HPN", "FRG", "CDW"],
    "KXTEMPCHIHS": ["ORD", "MDW", "PWK", "DPA", "ARR", "LOT", "GYY", "UGN"],
    "KXTEMPMIAH": ["MIA", "OPF", "FLL", "FXE", "PMP"],
    "KXTEMPLAXHS": ["LAX", "SMO", "HHR", "LGB", "TOA"],
}


def fetch_1min(station: str, start_ts: float, end_ts: float, get_text=retrying_text,
               chunk_days: int = 10) -> list[tuple[float, float]]:
    """1-minute ASOS air temperature (whole degF) from the IEM archive."""
    out = []
    t = start_ts
    while t < end_ts:
        u = min(end_ts, t + chunk_days * 86400)
        f = lambda x: datetime.fromtimestamp(x, tz=timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
        txt = get_text(IEM_1MIN, {"station": station, "vars": "tmpf", "sts": f(t), "ets": f(u),
                                  "sample": "1min", "what": "download", "tz": "UTC"})
        out.extend(parse_iem_csv(txt).get(station, []))
        t = u
    return sorted(set(out))


def cmd_stations(series_list: list[str], get_text=retrying_text) -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for s in series_list:
        evs = load_settlements(s)
        if not evs:
            print(f"{s}: no settlements file - run `settlements` first")
            continue
        a, b = evs[0]["settle_ts"] - 3 * 3600, evs[-1]["settle_ts"] + 3600
        for st in PROXY[s]:
            path = OUT_DIR / f"asos1min_{st}.csv"
            if path.exists():
                continue
            obs = fetch_1min(st, a, b, get_text=get_text)
            with open(path, "w", encoding="utf-8") as f:
                f.write("station,valid,tmpf\n")
                for ts, v in obs:
                    f.write(f"{st},{datetime.fromtimestamp(ts, tz=timezone.utc):%Y-%m-%d %H:%M},{v}\n")
            print(f"{s} {st}: {len(obs)} one-minute readings -> {path.name}")
    return 0


def load_1min(stations: list[str]) -> dict[str, list[tuple[float, float]]]:
    out = {}
    for st in stations:
        path = OUT_DIR / f"asos1min_{st}.csv"
        if path.exists():
            out.update(parse_iem_csv(path.read_text(encoding="utf-8")))
    return out


# ── the index reading available at entry ─────────────────────────────────────

APP_DELAY_S = 300      # in-app data is "5 minutes delayed" (series important_info)


def proxy_at(one_min: dict[str, list], stations: list[str], ts: float,
             max_age_s: float = 600) -> dict[str, float]:
    """{station: latest 1-minute reading at or before ts} for stations that have one."""
    out = {}
    for st in stations:
        r = latest_at(one_min.get(st, []), ts, max_age_s)
        if r is not None:
            out[st] = r[1]
    return out


def proxy_change(one_min: dict, stations: list[str], t0: float, t1: float) -> float | None:
    """Mean change t0 -> t1 over stations that report at BOTH times (no mix shift)."""
    a, b = proxy_at(one_min, stations, t0), proxy_at(one_min, stations, t1)
    both = [st for st in a if st in b]
    if not both:
        return None
    return sum(b[st] - a[st] for st in both) / len(both)


def prev_values(events: list[dict]) -> dict[str, float]:
    """event -> the index value published for the PREVIOUS clock hour (same series)."""
    by_ts = {e["settle_ts"]: e for e in events if e.get("value") is not None}
    out = {}
    for e in events:
        p = by_ts.get((e["settle_ts"] or 0) - 3600)
        if p is not None:
            out[e["event"]] = p["value"]
    return out


def reading_at_entry(e: dict, minutes: int, prev: dict, one_min: dict,
                     stations: list[str]) -> float | None:
    """What a trader could know `minutes` before settlement.
    60: the previous hour's published index value - the index AT the entry moment.
    otherwise: that value carried forward by the proxy stations' change from the
    previous hour mark to (entry - the app's 5-minute delay)."""
    v0 = prev.get(e["event"])
    if v0 is None:
        return None
    if minutes >= 60:
        return v0
    t_prev = e["settle_ts"] - 3600
    d = proxy_change(one_min, stations, t_prev, e["settle_ts"] - minutes * 60 - APP_DELAY_S)
    return None if d is None else round(v0 + d, 2)


def quote_time(settle_ts: float, minutes: int) -> float:
    """When the entry quote is read. At 60 the previous value is only certainly public
    5 minutes after the hour (expected_expiration_time = close + 5 min)."""
    return settle_ts - minutes * 60 + (APP_DELAY_S if minutes >= 60 else 0)


# ── candles ───────────────────────────────────────────────────────────────────

def load_candles(path: Path) -> dict[str, list[tuple]]:
    """Raw minute candles (no book filter): {ticker: sorted [(end_ts, yes_bid, yes_ask,
    trade_lo, trade_hi, volume)]}. yes_bid 0 means no NO seller below $1."""
    from sleeve_scan import _ohlc, candle_volume
    out: dict[str, list] = {}
    if not path.exists():
        return out
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"candle"' not in line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("t") != "candle":
                continue
            ts = _num(d.get("end_period_ts"))
            yb, ya = _ohlc(d, "yes_bid", "close"), _ohlc(d, "yes_ask", "close")
            if ts is None or yb is None or ya is None:
                continue
            out.setdefault(d["ticker"], []).append(
                (ts, yb, ya, _ohlc(d, "price", "low"), _ohlc(d, "price", "high"),
                 candle_volume(d)))
    for v in out.values():
        v.sort()
    return out


def quote_at(cs: list[tuple], ts: float, max_age_s: float = 600):
    """The book at ts: the latest candle closing at or before ts (fresh within 10 min)."""
    best = None
    for c in cs:
        if c[0] <= ts:
            best = c
        else:
            break
    if best is None or ts - best[0] > max_age_s:
        return None
    return best


def side_ask(c: tuple, side: str) -> float | None:
    """Ask to BUY `side`: YES at yes_ask; NO at 1 - yes_bid. None = nobody selling <$1."""
    yb, ya = c[1], c[2]
    if side == "yes":
        return ya if 0 < ya < 1.0 else None
    return round(1.0 - yb, 4) if 0 < yb < 1.0 else None


def trade_confirmed(cs: list[tuple], ts: float, side: str, ask: float) -> bool:
    """A trade printed at or through our price in the minute after entry."""
    for c in cs:
        if ts < c[0] <= ts + 60:
            if not c[5]:
                return False
            if side == "yes":
                return c[3] is not None and c[3] <= ask + 1e-9
            return c[4] is not None and c[4] >= (1.0 - ask) - 1e-9
    return False


# ── the conditional test ──────────────────────────────────────────────────────

def pick(e: dict, reading: float, candles: dict, qts: float, margin_min: float,
         lo: float = LO, hi: float = HI) -> dict:
    """One bet per event: among strikes whose reading-favored side has margin >=
    margin_min, the in-band one with the LARGEST margin (tie: cheaper ask).
    Returns {"status": "bet"|"priced_out"|"no_quote"|"none", ...}."""
    eligible = in_band = 0
    priced_out = cheap = 0
    best = None
    for s in e["strikes"]:
        side, mg = favored(s["strike"], reading)
        if mg < margin_min - 1e-9:
            continue
        eligible += 1
        c = quote_at(candles.get(s["ticker"], []), qts)
        if c is None:
            continue
        ask = side_ask(c, side)
        if ask is None or ask > hi + 1e-9:
            priced_out += 1
            continue
        if ask < lo - 1e-9:
            cheap += 1
            continue
        in_band += 1
        key = (mg, -ask)
        if best is None or key > best[0]:
            best = (key, {"strike": s["strike"], "ticker": s["ticker"], "side": side,
                          "margin": mg, "ask": ask,
                          "won": (s["result"] == side),
                          "trade_ok": trade_confirmed(candles.get(s["ticker"], []), qts,
                                                      side, ask)})
    if best is not None:
        return {"status": "bet", **best[1], "eligible": eligible}
    if priced_out:
        return {"status": "priced_out", "eligible": eligible}
    if cheap:
        return {"status": "below_band", "eligible": eligible}
    return {"status": "no_quote" if eligible else "none", "eligible": eligible}


def score(bets: list[dict], contracts: int = CONTRACTS, maker: bool = False) -> dict:
    """Per-contract P&L at the 10-lot taker fee (or no fee, as a resting maker), win rate
    vs break-even, exact p from sleeve_paper.forward_stats (one bet per event)."""
    from shadow_book import kalshi_taker_fee
    from sleeve_paper import forward_stats
    trades = []
    for b in bets:
        fee_pc = 0.0 if maker else kalshi_taker_fee(b["ask"], contracts) / contracts
        pnl_pc = (1.0 - b["ask"] - fee_pc) if b["won"] else (-b["ask"] - fee_pc)
        trades.append({"event": b["event"], "won": b["won"], "ask": b["ask"],
                       "fee_pc": fee_pc, "pnl_pc": pnl_pc, "pnl": pnl_pc * contracts})
    st = forward_stats(trades)
    n = len(trades)
    st["wins"] = sum(t["won"] for t in trades)
    st["breakeven"] = (sum(t["ask"] + t["fee_pc"] for t in trades) / n) if n else None
    st["pnl_pc"] = (sum(t["pnl_pc"] for t in trades) / n) if n else None
    return st


def run_conditional(series_list: list[str], final_dir: Path,
                    margins=MARGINS, entries=ENTRY_MINUTES) -> dict:
    """-> {(minutes, D): {"bets": [...], "counts": {status: n}}} plus per-series bets."""
    res: dict = {}
    for s in series_list:
        events = load_settlements(s)
        candles = load_candles(final_dir / f"{s}.jsonl")
        if not events or not candles:
            continue
        prev = prev_values(events)
        one_min = load_1min(PROXY[s])
        have = {tk for tk in candles}
        for e in events:
            if not any(x["ticker"] in have for x in e["strikes"]):
                continue                            # no price history for this event
            for mins in entries:
                r = reading_at_entry(e, mins, prev, one_min, PROXY[s])
                qts = quote_time(e["settle_ts"], mins)
                for D in margins:
                    cell = res.setdefault((mins, D), {"bets": [], "counts": {}})
                    if r is None:
                        out = {"status": "no_reading"}
                    else:
                        out = pick(e, r, candles, qts, D)
                    cell["counts"][out["status"]] = cell["counts"].get(out["status"], 0) + 1
                    if out["status"] == "bet":
                        cell["bets"].append({**out, "event": e["event"], "series": s,
                                             "reading": r, "value": e["value"]})
    return res


# ── calibration and the price-free base rate ─────────────────────────────────

def calibrate(series: str, events: list[dict], one_min: dict) -> dict:
    """How well can the settlement value be known in advance?
    persistence: V(t) - V(t-1h)            (what the 60-minute entry knows)
    proxy:       V(t) - [V(t-1h) + proxy change over the hour]   (the 30-minute method,
                 measured over the full hour, where its error is largest)"""
    prev = prev_values(events)
    pers, prox = [], []
    for e in events:
        v0 = prev.get(e["event"])
        if v0 is None or e["value"] is None:
            continue
        pers.append(e["value"] - v0)
        d = proxy_change(one_min, PROXY[series], e["settle_ts"] - 3600, e["settle_ts"])
        if d is not None:
            prox.append(e["value"] - (v0 + d))

    def summ(xs):
        if not xs:
            return {"n": 0}
        n = len(xs)
        mae = sum(abs(x) for x in xs) / n
        rmse = math.sqrt(sum(x * x for x in xs) / n)
        return {"n": n, "mae": mae, "rmse": rmse,
                "w05": sum(abs(x) <= 0.5 for x in xs) / n,
                "w1": sum(abs(x) <= 1.0 for x in xs) / n,
                "w2": sum(abs(x) <= 2.0 for x in xs) / n,
                "max": max(abs(x) for x in xs)}
    return {"persistence": summ(pers), "proxy": summ(prox)}


def flip_table(events: list[dict], readings: dict[str, float],
               edges=(0, 1, 2, 3, 4, 6)) -> list[dict]:
    """Price-free base rate: for every strike, the side the entry reading favored and
    its margin; how often did that side LOSE? Rows by margin bucket [a, b).
    Counted per strike AND per event (an event counts as flipped if any strike in the
    bucket flipped) - strikes of one hour are one outcome, not ten."""
    rows = []
    for a, b in zip(edges, list(edges[1:]) + [1e9]):
        n = fl = 0
        ev_n: set = set()
        ev_fl: set = set()
        for e in events:
            r = readings.get(e["event"])
            if r is None:
                continue
            for s in e["strikes"]:
                side, mg = favored(s["strike"], r)
                if not (a - 1e-9 <= mg < b - 1e-9):
                    continue
                n += 1
                ev_n.add(e["event"])
                if s["result"] != side:
                    fl += 1
                    ev_fl.add(e["event"])
        rows.append({"lo": a, "hi": b, "strikes": n, "flips": fl,
                     "events": len(ev_n), "events_flipped": len(ev_fl)})
    return rows


# ── report ────────────────────────────────────────────────────────────────────

def _f(x, nd=3):
    return "-" if x is None else f"{x:.{nd}f}"


def report_conditional(res: dict) -> str:
    lines = ["entry  D   N  wins  win%   b/even  pnl/ct(taker)  p(taker)  pnl/ct(maker)  "
             "p(maker)  trade-ok(N,wins)  priced>99c  below97c  no-quote  no-reading"]
    for (mins, D) in sorted(res, key=lambda k: (-k[0], k[1])):
        cell = res[(mins, D)]
        bets = cell["bets"]
        t, m = score(bets), score(bets, maker=True)
        tok = [b for b in bets if b["trade_ok"]]
        c = cell["counts"]
        wr = (t["wins"] / t["trades"] * 100) if t["trades"] else None
        lines.append(
            f"{mins:>4}m {D:>3.0f} {t['trades']:>4} {t['wins']:>5} {_f(wr, 1):>6} "
            f"{_f(t['breakeven']):>7}  {_f(t['pnl_pc'], 4):>12}  {_f(t['p']):>8}  "
            f"{_f(m['pnl_pc'], 4):>12}  {_f(m['p']):>8}  "
            f"{len(tok):>6},{sum(b['won'] for b in tok):<6}    "
            f"{c.get('priced_out', 0):>8}  {c.get('below_band', 0):>8}  "
            f"{c.get('no_quote', 0) + c.get('none', 0):>8}  {c.get('no_reading', 0):>8}")
    return "\n".join(lines)


def cmd_report(series_list: list[str], final_dir: Path) -> int:
    print("== calibration: can the settlement value be known in advance? (degF) ==")
    for s in series_list:
        ev = load_settlements(s)
        if not ev:
            print(f"{s}: no settlements file")
            continue
        c = calibrate(s, ev, load_1min(PROXY[s]))
        for k in ("persistence", "proxy"):
            x = c[k]
            if x["n"]:
                print(f"{s:12} {k:11} n={x['n']:5} MAE {x['mae']:.2f} RMSE {x['rmse']:.2f} "
                      f"|err|<=0.5 {x['w05']:.0%} <=1 {x['w1']:.0%} <=2 {x['w2']:.0%} "
                      f"max {x['max']:.1f}")
    print("\n== price-free base rate: how often the side the entry reading favored LOST ==")
    print("   (all settled events; per strike, and per event = any strike in bucket lost)")
    for s in series_list:
        ev = load_settlements(s)
        if not ev:
            continue
        om = load_1min(PROXY[s])
        prev = prev_values(ev)
        rd = {"60": prev}
        r30 = {}
        for e in ev:
            r = reading_at_entry(e, 30, prev, om, PROXY[s])
            if r is not None:
                r30[e["event"]] = r
        rd["30"] = r30
        for name, r in rd.items():
            cells = []
            for row in flip_table(ev, r):
                hi = "+" if row["hi"] >= 1e8 else f"-{row['hi']:.0f}"
                rate = row["flips"] / row["strikes"] if row["strikes"] else 0
                cells.append(f"[{row['lo']:.0f}{hi}) {row['flips']}/{row['strikes']}={rate:.1%}")
            print(f"{s:12} {name}m  " + "  ".join(cells))
    print("\n== conditional: buy the reading-favored side at 97-99c when margin >= D ==")
    print("   one bet per event (in-band strike with the largest margin), 10 contracts,")
    print("   taker fee ceil(0.07*10*P*(1-P)) per order; maker = no fee. p = exact, one-sided")
    print("   (sleeve_paper.forward_stats). Counts at right are events with NO bet.")
    res = run_conditional(series_list, final_dir)
    print(report_conditional(res))
    print("\nby series (taker):")
    for (mins, D) in sorted(res, key=lambda k: (-k[0], k[1])):
        parts = []
        for s in series_list:
            bs = [b for b in res[(mins, D)]["bets"] if b["series"] == s]
            if bs:
                st = score(bs)
                parts.append(f"{s} {st['wins']}/{st['trades']} {_f(st['pnl_pc'], 4)}")
        print(f"  {mins}m D={D:.0f}: " + " | ".join(parts))
    print("\nlosses (taker cells):")
    for (mins, D) in sorted(res, key=lambda k: (-k[0], k[1])):
        for b in res[(mins, D)]["bets"]:
            if not b["won"]:
                print(f"  {mins}m D={D:.0f} {b['event']} {b['side']} >={line_of(b['strike']):.0f}"
                      f" ask {b['ask']:.2f} reading {b['reading']} -> settled {b['value']}")
    return 0


def _arg(args: list[str], name: str, default=None):
    return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) \
        else default


def main() -> int:
    a = sys.argv[1:]
    cmd = a[0] if a else ""
    series = [s for s in (_arg(a, "--series") or ",".join(SERIES)).split(",") if s]
    if cmd == "selftest":
        return _selftest()
    if cmd == "settlements":
        return cmd_settlements(series, max_markets=int(_arg(a, "--max", 30000)))
    if cmd == "stations":
        return cmd_stations(series)
    if cmd in ("report", "conditional", "calibrate"):
        return cmd_report(series, Path(_arg(a, "--final", str(ROOT / "data" / "sleeves" / "final"))))
    print(__doc__)
    return 1


def _selftest() -> int:
    """Fixtures only, no network."""
    assert line_of(66.99) == 67.0
    assert favored(66.99, 68.0) == ("yes", 1.0)
    assert favored(68.99, 68.0) == ("no", 1.0)
    assert favored(66.99, 67.0) == ("yes", 0.0)
    assert favored(40.99, 38.84) == ("no", 2.16)
    obs = [(0.0, 50.0), (60.0, 51.0), (120.0, 52.0)]
    assert latest_at(obs, 90.0) == (60.0, 51.0) and latest_at(obs, -1.0) is None
    t0 = 1_790_000_000.0
    evs = [{"event": f"S-{i}", "settle_ts": t0 + i * 3600, "value": 60.0 + i,
            "strikes": []} for i in range(3)]
    pv = prev_values(evs)
    assert pv == {"S-1": 60.0, "S-2": 61.0}
    om = {"A": [(t0 + 3600, 70.0), (t0 + 3600 + 25 * 60, 72.0)],
          "B": [(t0 + 3600, 50.0), (t0 + 3600 + 25 * 60, 50.0)]}
    e = evs[2]
    assert reading_at_entry(e, 60, pv, om, ["A", "B"]) == 61.0
    # 30 min out, app delay 5 -> proxy at settle-35m = prev hour + 25m: mean change +1
    assert reading_at_entry(e, 30, pv, om, ["A", "B"]) == 62.0
    c = (t0, 0.02, 0.98, None, None, 0.0)
    assert side_ask(c, "yes") == 0.98 and side_ask(c, "no") == 0.98
    assert side_ask((t0, 0.0, 0.01, None, None, 0.0), "no") is None
    print("selftest OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
