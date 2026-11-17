#!/usr/bin/env python3
"""forecast_replay - does a weather forecast beat the Kalshi price? Deep-history answer.

WHY THIS EXISTS. edge_analysis and maker_replay screen a venue for UNCONDITIONAL
mispricing: they ask "at price P, does the market settle YES more often than P?" That is
the right question for 15-min crypto, where nobody has better information than the tape.
It is the WRONG question for weather. A weather market can be perfectly calibrated on
average - every 30c contract settling 30% of the time - and still be beatable every single
day, because the edge is CONDITIONAL: it only appears when a forecast disagrees with the
price. Averaged over all days the disagreements cancel, so the screener sees nothing. That
is why "no HELD cells" in the weather screen was never a refutation of the weather sleeve.

This script asks the conditional question directly, over the whole 2,852-market backfill:

  for each city-day, take what the forecast said, price every temperature bucket from it,
  compare to the book that actually existed at that moment, bet only where they disagree
  by more than a threshold, and settle on Kalshi's own result.

THREE THINGS THAT KEEP THIS HONEST (each one killed an earlier version of this idea):

 1. NO LOOKAHEAD. The headline replay ("strict") prices the remaining hours of a day using
    the forecast run issued the PREVIOUS day, plus the temperature actually observed so far
    that day. Both are genuinely available to a trader standing at that minute. The
    tempting shortcut - using the archived best-match series for the whole day - contains
    analysis of hours that had not happened yet at entry time, and turns a 2 degree
    forecast error into a 0 degree one. That variant is still computed, clearly labelled
    DIAGNOSTIC, and must never be quoted as a result.

 2. THE FORECAST MUST BEAT THE PRICE, not merely have an opinion. Before any P&L, the
    report scores our forecast-implied probabilities and the market's own prices against
    the same settled outcomes with the Brier score. If the market wins that, there is no
    edge and no threshold can manufacture one - the rest of the report is noise.
    (This is gate G4 of lib/binary_justify, applied to history instead of to a live quote.)

 3. WALK-FORWARD, PER WINDOW. sigma and bias are MEASURED on the first half and frozen;
    the second half is scored with no further choices. A window is a city-day, not a
    contract: the eight buckets of one city-day resolve on ONE temperature, so they are
    one bet, not eight. All P&L is reported per window.

HOW MUCH WEATHER DATA IS ENOUGH? Measured the same way the crypto threshold was, by
running this against two synthetic worlds: one where the book is priced AT the forecast's
own fair value (nothing to find), one where the book is anchored to climatology while the
forecast knows the day (a real, large conditional edge).

    city-day windows   beatable world              efficient world
    ~80                gate: FORECAST WINS         gate: PRICE WINS   <- the GATE alone
                       0 HELD, 8 persist-thin      0 HELD                already separates
    ~160               5 HELD                      0 HELD
    ~320               15 HELD                     0 HELD             <- clean
    ~600               16 HELD                     0 HELD
    ~800               16 HELD                     0 HELD

So the Brier gate discriminates at well under a hundred city-days, while the P&L grid
needs ~300 before it commits. That is cheaper than the ~600 independent windows the
15-min crypto screen needed, for a structural reason worth keeping in mind: one weather
bet is a whole day's disagreement, while one crypto bet is a coin flip with a spread on
it. CAVEAT: the planted edge above is large (a book anchored to climatology). A one- or
two-cent conditional edge would need far more data than this table suggests.

  py scripts/forecast_replay.py probe          # which Open-Meteo archive works from here
  py scripts/forecast_replay.py fetch          # cache forecasts for the backfilled days
  py scripts/forecast_replay.py replay         # the report
  py scripts/forecast_replay.py selftest       # fixtures, no network

Inputs  : data/backfill_weather.jsonl   (override: $env:BACKFILL_LOG)
Cache   : data/forecast_archive.jsonl   (override: $env:FORECAST_CACHE)
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

WX_LOG = Path(os.environ.get("BACKFILL_LOG") or (ROOT / "data" / "backfill_weather.jsonl"))
FC_CACHE = Path(os.environ.get("FORECAST_CACHE")
                or (ROOT / "data" / "forecast_archive.jsonl"))
SLEEP_S = 0.5

# Entry bands are LOCAL HOURS OF THE MARKET DAY, not minutes-to-close. A daily-high
# market closes at local midnight, so "minutes left" buries the only thing that matters:
# whether the day's high has already happened. Before ~11am the high is still forecast;
# after ~5pm it is essentially known and the book prices it.
BANDS = {
    "D-1 eve":   (-1e9, 0.0),
    "overnight": (0.0, 6.0),
    "morning":   (6.0, 11.0),
    "midday":    (11.0, 15.0),
    "afternoon": (15.0, 19.0),
    "evening":   (19.0, 24.0),
}
BAND_ORDER = list(BANDS)
# Frozen grid. Chosen once, before looking at any result, and never widened: every extra
# cell is another chance for noise to look like an edge.
THRESHOLDS = (0.03, 0.05, 0.08, 0.12)

MAX_SPREAD = 0.15        # data-validity, same constant as edge_analysis/maker_replay
# A cell needs this many city-days on EACH side of the split before it may be called
# actionable. Not a preference: at a 0.12 threshold a cell can qualify on four days, and
# four low-variance days produce an interval that clears zero. Caught by the "efficient
# world" fixture, which handed back a +$0.55/window cell backed by 4 windows.
MIN_CELL_WINDOWS = 30
DEFAULT_SIGMA_F = 3.0    # fallback only; the replay MEASURES sigma from the first half
DEFAULT_BUCKET_W = 2.0   # used only when an event has a single bucket to infer width from

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}

# Series -> IANA zone. Kalshi daily-high markets resolve on the LOCAL calendar day at the
# station, so every hour mapping in this file is local-time mapping.
SERIES_TZ = {
    "KXHIGHNY": "America/New_York",   "KXHIGHPHIL": "America/New_York",
    "KXHIGHDC": "America/New_York",   "KXHIGHMIA": "America/New_York",
    "KXHIGHCHI": "America/Chicago",   "KXHIGHAUS": "America/Chicago",
    "KXHIGHDEN": "America/Denver",    "KXHIGHLAX": "America/Los_Angeles",
}
# Fallback coordinates, used only if config/kalshi_weather.yaml can't be read. Same
# stations the registry maps (Central Park, Midway, MIA, Camp Mabry/AUS, DEN, LAX, PHL).
FALLBACK_LATLON = {
    "KXHIGHNY": (40.7790, -73.9693), "KXHIGHCHI": (41.7860, -87.7524),
    "KXHIGHMIA": (25.7932, -80.2906), "KXHIGHAUS": (30.1945, -97.6699),
    "KXHIGHDEN": (39.8466, -104.6562), "KXHIGHLAX": (33.9381, -118.3889),
    "KXHIGHPHIL": (39.8729, -75.2437), "KXHIGHDC": (38.8472, -77.0349),
}

# The archive ladder. Retention and variable availability are exactly what a remote code
# read cannot verify, so `probe` walks this list and prints what each host really returns.
#   *_previous_day1 is the whole point: it is the run issued the day BEFORE, the only
#   forecast that is unambiguously available for every entry time on the market day.
SOURCES = [
    ("previous-runs", "https://previous-runs-api.open-meteo.com/v1/forecast",
     "temperature_2m,temperature_2m_previous_day1"),
    ("historical-forecast", "https://historical-forecast-api.open-meteo.com/v1/forecast",
     "temperature_2m,temperature_2m_previous_day1"),
    ("historical-basic", "https://historical-forecast-api.open-meteo.com/v1/forecast",
     "temperature_2m"),
    ("archive-era5", "https://archive-api.open-meteo.com/v1/archive", "temperature_2m"),
]


# ── small math ───────────────────────────────────────────────────────────────

def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def taker_fee(price: float) -> float:
    """Kalshi taker fee, ceil'd to the cent - the rounding IS the tax on cheap contracts."""
    if not (0 < price < 1):
        return 0.0
    return math.ceil(0.07 * price * (1.0 - price) * 100) / 100.0


def z_for(alpha: float) -> float:
    """Two-sided critical value for `alpha`, by bisection on the normal CDF.

    Needed because the grid below is searched: 24 cells at the textbook 95% means roughly
    one cell shows a "significant" edge by luck alone every single run. Validated: on a
    fixture where the book is priced AT the forecast's own fair value - nothing to find by
    construction - the uncorrected test reported one HELD cell. It is corrected here.
    """
    target = 1.0 - alpha / 2.0
    lo, hi = 0.0, 12.0
    for _ in range(80):
        mid = (lo + hi) / 2.0
        if norm_cdf(mid) < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def mean_ci(xs: list[float], z: float = 1.96) -> tuple[float, float, float]:
    """(mean, lo, hi) - a normal interval on the MEAN of per-window dollars.

    Per-window, not per-bet, and on dollars, not win rate: a 14%-win-rate longshot rule can
    be the most profitable thing in the book, and a win-rate CI can never say so.
    """
    n = len(xs)
    if n == 0:
        return (0.0, 0.0, 0.0)
    m = sum(xs) / n
    if n < 2:
        return (m, -1e9, 1e9)
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    h = z * math.sqrt(var / n)
    return (m, m - h, m + h)


def brier(pairs: list[tuple[float, bool]]) -> float:
    """Mean squared error of probabilistic forecasts. Lower is better; 0.25 = coin flip."""
    if not pairs:
        return float("nan")
    return sum((p - (1.0 if o else 0.0)) ** 2 for p, o in pairs) / len(pairs)


# ── tickers, cities, buckets ─────────────────────────────────────────────────

def parse_ticker(ticker: str) -> tuple[str, str, str] | None:
    """'KXHIGHNY-26SEP11-B75.5' -> ('KXHIGHNY', '2026-09-11', 'B75.5').

    The DATE MUST come from the ticker, not from close_time: close_time is UTC, and for
    every US city the local market day ends after midnight UTC, so a UTC date is the
    wrong day for roughly a third of the sample.
    """
    parts = str(ticker or "").split("-")
    if len(parts) < 3:
        return None
    series, dcode, scode = parts[0], parts[1], "-".join(parts[2:])
    if len(dcode) < 7:
        return None
    try:
        yy, mon, dd = int(dcode[:2]), dcode[2:5].upper(), int(dcode[5:7])
        mm = _MONTHS[mon]
        return series, f"{2000 + yy:04d}-{mm:02d}-{dd:02d}", scode
    except (ValueError, KeyError):
        return None


def load_cities() -> dict[str, tuple[float, float]]:
    """series -> (lat, lon), from the registry when readable, else the fallback table."""
    out = dict(FALLBACK_LATLON)
    try:
        import yaml
        cfg = yaml.safe_load((ROOT / "config" / "kalshi_weather.yaml").read_text()) or {}
        for c in (cfg.get("cities") or {}).values():
            lat, lon = c.get("lat"), c.get("lon")
            if lat is None or lon is None:
                continue
            for alias in (c.get("aliases") or []):
                a = str(alias).upper()
                if a.startswith("KXHIGH"):
                    out[a] = (float(lat), float(lon))
    except Exception:  # noqa: BLE001 - registry is a convenience, never a requirement
        pass
    return out


def _book_formed(yb, ya) -> bool:
    try:
        yb, ya = float(yb), float(ya)
    except (TypeError, ValueError):
        return False
    return 0.01 <= yb < ya <= 0.99 and (ya - yb) <= MAX_SPREAD


def infer_buckets(markets: list[dict]) -> None:
    """Fill lo/hi on every market of ONE city-day, in place.

    The backfill stored floor_strike but not cap_strike, so caps are recovered the only
    way that cannot be wrong about Kalshi's ladder: from the next strike up in the same
    event. 'T' markets are thresholds (>= strike) and need no cap.
    """
    bs = sorted([m for m in markets if m["kind"] == "B" and m["floor"] is not None],
                key=lambda m: m["floor"])
    widths = [b2["floor"] - b1["floor"] for b1, b2 in zip(bs, bs[1:])]
    w = sorted(widths)[len(widths) // 2] if widths else DEFAULT_BUCKET_W
    for i, m in enumerate(bs):
        m["lo"] = m["floor"]
        m["hi"] = bs[i + 1]["floor"] if i + 1 < len(bs) else m["floor"] + w
    for m in markets:
        if m["kind"] == "T" and m["floor"] is not None:
            m["lo"], m["hi"] = m["floor"], None       # None == unbounded above


def fair_p(m: dict, mu: float, sigma: float) -> float | None:
    """P(the day's high settles this contract YES), from a normal around the forecast."""
    if sigma is None or sigma <= 0 or m.get("lo") is None:
        return None
    hi_p = 1.0 if m.get("hi") is None else norm_cdf((m["hi"] - mu) / sigma)
    lo_p = norm_cdf((m["lo"] - mu) / sigma)
    return max(0.0, min(1.0, hi_p - lo_p))


# ── loading the backfill (streaming: 3M candles must never all be resident) ──

def load_backfill(path: Path) -> dict[tuple[str, str], dict]:
    """-> {(series, date): {'markets': [...], 'quotes': {ticker: {utc_hour: (ts, yb, ya)}}}}

    Streams the file. Market rows precede their own candles (that is how the backfill
    writes them), and only the FIRST formed book of each UTC hour is kept, so memory is
    bounded by markets x 30-ish hours instead of by candle count.
    """
    events: dict[tuple[str, str], dict] = {}
    meta: dict[str, dict] = {}
    if not path.exists():
        return events
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = d.get("t")
            if t == "market":
                p = parse_ticker(d.get("ticker", ""))
                if not p:
                    continue
                series, dt, scode = p
                if d.get("result") not in ("yes", "no"):
                    continue
                try:
                    floor = float(d["floor_strike"]) if d.get("floor_strike") is not None \
                        else None
                except (TypeError, ValueError):
                    floor = None
                m = {"ticker": d["ticker"], "series": series, "date": dt,
                     "kind": ("B" if scode[:1].upper() == "B" else
                              "T" if scode[:1].upper() == "T" else "?"),
                     "floor": floor, "result": d["result"],
                     "volume": d.get("volume") or 0, "lo": None, "hi": None}
                ev = events.setdefault((series, dt), {"markets": [], "quotes": {}})
                ev["markets"].append(m)
                meta[d["ticker"]] = m
            elif t == "candle":
                m = meta.get(d.get("ticker"))
                if m is None:
                    continue
                ts = d.get("end_period_ts")
                if ts is None:
                    continue
                yb, ya = _candle_px(d, "yes_bid"), _candle_px(d, "yes_ask")
                if not _book_formed(yb, ya):
                    continue
                q = events[(m["series"], m["date"])]["quotes"].setdefault(m["ticker"], {})
                hr = int(float(ts) // 3600)
                if hr not in q:
                    q[hr] = (float(ts), float(yb), float(ya))
    for ev in events.values():
        infer_buckets(ev["markets"])
    return events


def _candle_px(c: dict, name: str):
    """Candle prices arrive nested or flat, in cents or dollars (ccxt-verified variety)."""
    v = c.get(f"{name}_dollars", c.get(name))
    if isinstance(v, dict):
        v = v.get("close_dollars", v.get("close"))
    if v is None:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if v <= 1.0 else v / 100.0


def realized_mid(markets: list[dict]) -> float | None:
    """The day's high, recovered from Kalshi's own settlement - no actuals API needed.

    Exactly one bucket resolves YES, so the high lies inside it; its midpoint is the
    estimate. The +-0.6F of rounding noise this adds is charged honestly to the measured
    sigma (it makes sigma slightly too big, i.e. our claimed edge slightly too small).
    """
    for m in markets:
        if m["kind"] == "B" and m["result"] == "yes" and m.get("lo") is not None \
                and m.get("hi") is not None:
            return (m["lo"] + m["hi"]) / 2.0
    return None


# ── forecast cache ───────────────────────────────────────────────────────────

def _get(url: str, params: dict) -> dict:
    import requests
    r = requests.get(url, params=params, timeout=45)
    r.raise_for_status()
    return r.json()


def fetch_range(url: str, hourly: str, lat: float, lon: float, tz: str,
                start: str, end: str, get=_get) -> dict:
    return get(url, {"latitude": lat, "longitude": lon, "hourly": hourly,
                     "temperature_unit": "fahrenheit", "timezone": tz,
                     "start_date": start, "end_date": end})


def rows_from_response(series: str, resp: dict) -> list[dict]:
    """Open-Meteo hourly response -> one compact row per LOCAL day.

    Open-Meteo does the timezone arithmetic (times come back as local wall clock), so a
    day's 24 values are correct across DST without us owning a tz database.
    """
    h = resp.get("hourly") or {}
    times = h.get("time") or []
    temp = h.get("temperature_2m") or []
    prev = h.get("temperature_2m_previous_day1") or []
    off = resp.get("utc_offset_seconds")
    days: dict[str, dict] = {}
    for i, t in enumerate(times):
        day, _, hh = str(t).partition("T")
        try:
            idx = int(hh[:2])
        except (ValueError, IndexError):
            continue
        d = days.setdefault(day, {"t": "fcday", "series": series, "date": day,
                                  "utc_offset": off,
                                  "temp": [None] * 24, "prev": [None] * 24})
        if 0 <= idx < 24:
            if i < len(temp) and temp[i] is not None:
                d["temp"][idx] = float(temp[i])
            if i < len(prev) and prev[i] is not None:
                d["prev"][idx] = float(prev[i])
    for d in days.values():
        if not any(v is not None for v in d["prev"]):
            d["prev"] = None          # be explicit: no lookahead-free run for this day
    return [days[k] for k in sorted(days)]


def load_forecasts(path: Path) -> dict[tuple[str, str], dict]:
    out: dict[tuple[str, str], dict] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("t") == "fcday":
            out[(d["series"], d["date"])] = d      # later rows win (re-fetch = refresh)
    return out


def mu_at(day: dict, local_h: float, strict: bool) -> float | None:
    """Best estimate, AT local hour `local_h`, of the day's eventual high.

    max( highest temperature already observed today , highest still to come per forecast )

    strict=True uses the previous day's forecast run for the hours still to come - a
    number that existed before the market day opened. strict=False substitutes the
    archived best-match series, which for elapsed-but-future hours is analysis of weather
    that had not happened yet at entry. That is the lookahead; it is why the two numbers
    are reported separately and only the strict one counts.
    """
    temp = day.get("temp") or []
    fut_src = day.get("prev") if strict else temp
    if strict and not fut_src:
        return None
    seen = [v for i, v in enumerate(temp) if v is not None and i <= local_h]
    ahead = [v for i, v in enumerate(fut_src or []) if v is not None and i > local_h]
    vals = seen + ahead
    return max(vals) if vals else None


# ── the replay ───────────────────────────────────────────────────────────────

def _local_hours(ts: float, series: str, day: dict, date_str: str) -> float | None:
    """UTC epoch -> hours since local midnight of the market date (may be negative)."""
    try:
        y, m, d = (int(x) for x in date_str.split("-"))
    except ValueError:
        return None
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(SERIES_TZ.get(series, "UTC"))
        midnight = datetime(y, m, d, tzinfo=tz)
        return (datetime.fromtimestamp(ts, tz=timezone.utc) - midnight).total_seconds() / 3600.0
    except Exception:  # noqa: BLE001 - no tzdata on this machine: use the cached offset
        off = day.get("utc_offset")
        if off is None:
            return None
        midnight = datetime(y, m, d, tzinfo=timezone.utc).timestamp() - float(off)
        return (ts - midnight) / 3600.0


def build_entries(events: dict, forecasts: dict, strict: bool) -> list[dict]:
    """One candidate bet per (city-day, band, contract): the first formed book in the band,
    the forecast as it stood at that minute, and the settled truth."""
    out = []
    for (series, dt), ev in sorted(events.items(), key=lambda kv: kv[0][1]):
        day = forecasts.get((series, dt))
        if not day:
            continue
        real = realized_mid(ev["markets"])
        for m in ev["markets"]:
            if m.get("lo") is None:
                continue
            picked: dict[str, tuple] = {}
            for hr in sorted(ev["quotes"].get(m["ticker"], {})):
                ts, yb, ya = ev["quotes"][m["ticker"]][hr]
                lh = _local_hours(ts, series, day, dt)
                if lh is None or lh >= 24.0:
                    continue
                band = next((b for b, (lo, hi) in BANDS.items() if lo <= lh < hi), None)
                if band is None or band in picked:
                    continue
                mu = mu_at(day, lh, strict)
                if mu is None:
                    continue
                picked[band] = (ts, yb, ya, lh, mu)
            for band, (ts, yb, ya, lh, mu) in picked.items():
                out.append({"series": series, "date": dt, "window": f"{series}|{dt}",
                            "ticker": m["ticker"], "band": band, "ts": ts,
                            "local_h": lh, "mu": mu, "lo": m["lo"], "hi": m["hi"],
                            "yes_ask": ya, "no_ask": round(1.0 - yb, 4),
                            "won_yes": m["result"] == "yes", "realized": real,
                            "m": m})
    out.sort(key=lambda e: (e["date"], e["ts"]))
    return out


def calibrate(entries: list[dict]) -> tuple[dict, float, int]:
    """Measure forecast bias and error sigma PER BAND on the given (in-sample) entries.

    One value per band, measured, never tuned - and the out-of-sample half never gets to
    influence them. Sigma naturally widens in the morning and collapses by evening, which
    is exactly the shape that stops us claiming edge when the day is already decided.
    """
    per: dict[str, list[float]] = {}
    seen: set = set()
    for e in entries:
        if e["realized"] is None:
            continue
        key = (e["window"], e["band"])           # one sample per city-day per band
        if key in seen:
            continue
        seen.add(key)
        per.setdefault(e["band"], []).append(e["realized"] - e["mu"])
    out = {}
    n_tot = 0
    for band, errs in per.items():
        n = len(errs)
        n_tot += n
        if n < 10:
            out[band] = (0.0, DEFAULT_SIGMA_F, n)
            continue
        bias = sum(errs) / n
        var = sum((x - bias) ** 2 for x in errs) / (n - 1)
        out[band] = (bias, max(0.5, math.sqrt(var)), n)
    allerr = [x for v in per.values() for x in v]
    mae = sum(abs(x) for x in allerr) / len(allerr) if allerr else float("nan")
    return out, mae, n_tot


def score(entries: list[dict], cal: dict, band: str, thresh: float) -> list[float]:
    """Per-window dollars for one (band, threshold) cell, one bet per city-day.

    The per-window cap is not a risk preference, it is arithmetic: the buckets of a city-day
    all resolve on ONE temperature. Taking six of them is one bet at six times the size,
    and counting them as six independent samples is how a backtest lies about its t-stat.
    """
    best: dict[str, tuple] = {}
    bias, sigma, _ = cal.get(band, (0.0, DEFAULT_SIGMA_F, 0))
    for e in entries:
        if e["band"] != band:
            continue
        p = fair_p(e["m"], e["mu"] + bias, sigma)
        if p is None:
            continue
        for side, cost, win in (("yes", e["yes_ask"], e["won_yes"]),
                                ("no", e["no_ask"], not e["won_yes"])):
            if not (0 < cost < 1):
                continue
            pw = p if side == "yes" else 1.0 - p
            fee = taker_fee(cost)
            edge = pw - cost - fee
            if edge < thresh:
                continue
            pnl = (1.0 - cost - fee) if win else (-cost - fee)
            cur = best.get(e["window"])
            if cur is None or edge > cur[0]:
                best[e["window"]] = (edge, pnl)
    return [v[1] for v in best.values()]


def cell_verdict(na: int, ma: float, nb: int, mb: float, lob: float,
                 gate_ok: bool) -> str:
    """The single definition of "is this cell actionable". Report and tests both call it,
    so the printed label and the tested rule can never drift apart."""
    if not gate_ok:
        return "blocked: price wins gate"
    if na < MIN_CELL_WINDOWS or nb < MIN_CELL_WINDOWS:
        return f"thin: <{MIN_CELL_WINDOWS} windows"
    if ma > 0 and mb > 0 and lob > 0:
        return "HELD (act)"
    if ma > 0 and mb > 0:
        return "persists, CI thin"
    if ma > 0:
        return "FLIPPED (noise)"
    return "-"


def split_by_date(entries: list[dict]) -> tuple[list[dict], list[dict], str]:
    """Chronological halves. The cut is a DATE, so every city's day falls on one side."""
    dates = sorted({e["date"] for e in entries})
    if not dates:
        return ([], [], "")
    cut = dates[max(0, len(dates) // 2 - 1)]
    return ([e for e in entries if e["date"] <= cut],
            [e for e in entries if e["date"] > cut], cut)


def calibration_check(entries: list[dict], cal: dict) -> tuple[float, float, int]:
    """Brier of our forecast vs Brier of the market price, on the same contracts.

    This is the gate. If the price scores better, every dollar below is a fit.
    """
    ours, theirs = [], []
    for e in entries:
        bias, sigma, _ = cal.get(e["band"], (0.0, DEFAULT_SIGMA_F, 0))
        p = fair_p(e["m"], e["mu"] + bias, sigma)
        if p is None:
            continue
        mid = (e["yes_ask"] + (1.0 - e["no_ask"])) / 2.0      # ask/bid midpoint
        ours.append((p, e["won_yes"]))
        theirs.append((mid, e["won_yes"]))
    return brier(ours), brier(theirs), len(ours)


# ── commands ─────────────────────────────────────────────────────────────────

def cmd_probe(get=_get) -> int:
    print("=" * 78)
    print("FORECAST ARCHIVE PROBE - which history is reachable, and does it carry the")
    print("previous-day run (the only lookahead-free forecast)?")
    print("=" * 78)
    lat, lon = FALLBACK_LATLON["KXHIGHNY"]
    end = date.today() - timedelta(days=20)
    start = end - timedelta(days=2)
    ok = []
    for name, url, hourly in SOURCES:
        try:
            resp = fetch_range(url, hourly, lat, lon, "America/New_York",
                               start.isoformat(), end.isoformat(), get=get)
        except Exception as e:  # noqa: BLE001
            print(f"  {name:20} FAILED  {type(e).__name__}: {str(e)[:70]}")
            continue
        rows = rows_from_response("KXHIGHNY", resp)
        has_prev = sum(1 for r in rows if r["prev"])
        sample = next((r for r in rows if any(v is not None for v in r["temp"])), None)
        hi = max([v for v in (sample or {}).get("temp", []) if v is not None], default=None)
        print(f"  {name:20} OK      {len(rows)} days, {has_prev} with previous-day run, "
              f"sample high {hi}")
        if rows:
            ok.append((name, has_prev > 0))
        time.sleep(SLEEP_S)
    print()
    if not ok:
        print("READ: nothing reachable. This host cannot see Open-Meteo - run `fetch` on")
        print("the machine that runs the collector, or allowlist *.open-meteo.com there.")
        return 1
    strict = [n for n, p in ok if p]
    if strict:
        print(f"READ: use --source {strict[0]} - it carries the previous-day run, so the")
        print("replay can be strictly lookahead-free.")
    else:
        print("READ: reachable, but NO source returned a previous-day run. The replay will")
        print("only be able to report the DIAGNOSTIC (lookahead) number, which cannot be")
        print("used to justify a trade. Paste this output back before acting on anything.")
    return 0


def cmd_fetch(source: str | None, get=_get) -> int:
    events = load_backfill(WX_LOG)
    if not events:
        print(f"no weather backfill at {WX_LOG} - run history_backfill first.")
        return 1
    want: dict[str, list[str]] = {}
    for (series, dt) in events:
        want.setdefault(series, []).append(dt)
    have = load_forecasts(FC_CACHE)
    cities = load_cities()
    picked = next((s for s in SOURCES if source in (None, s[0])), None)
    if picked is None:
        print(f"unknown --source {source!r}. Known: "
              f"{', '.join(s[0] for s in SOURCES)}")
        return 1
    name, url, hourly = picked
    print(f"forecast fetch via {name} -> {FC_CACHE}")
    FC_CACHE.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with open(FC_CACHE, "a", encoding="utf-8") as f:
        for series, dates in sorted(want.items()):
            todo = sorted(d for d in set(dates) if (series, d) not in have)
            if not todo:
                print(f"  {series}: cached")
                continue
            if series not in cities:
                print(f"  {series}: no coordinates in registry - skipped")
                continue
            lat, lon = cities[series]
            tz = SERIES_TZ.get(series, "UTC")
            # One request per contiguous ~30-day chunk: cheap, and small enough that a
            # single failure costs almost nothing on resume.
            lo = datetime.strptime(todo[0], "%Y-%m-%d").date()
            hi = datetime.strptime(todo[-1], "%Y-%m-%d").date()
            n = 0
            cur = lo
            missing = set(todo)
            while cur <= hi:
                chunk_end = min(cur + timedelta(days=30), hi)
                span = {(cur + timedelta(days=i)).isoformat()
                        for i in range((chunk_end - cur).days + 1)}
                if not (span & missing):
                    cur = chunk_end + timedelta(days=1)
                    continue          # whole chunk already cached: don't re-download it
                try:
                    resp = fetch_range(url, hourly, lat, lon, tz,
                                       cur.isoformat(), chunk_end.isoformat(), get=get)
                except Exception as e:  # noqa: BLE001
                    print(f"  [warn] {series} {cur}..{chunk_end}: "
                          f"{type(e).__name__}: {str(e)[:70]}")
                    cur = chunk_end + timedelta(days=1)
                    continue
                for row in rows_from_response(series, resp):
                    f.write(json.dumps(row, separators=(",", ":")) + "\n")
                    n += 1
                cur = chunk_end + timedelta(days=1)
                time.sleep(SLEEP_S)
            print(f"  {series}: {n} days cached ({todo[0]} .. {todo[-1]})")
            total += n
    print(f"done. {total} city-days written. Now: py scripts/forecast_replay.py replay")
    return 0


def _grid(entries_a: list[dict], entries_b: list[dict], cal: dict) -> tuple[list, float]:
    """Every cell of the frozen grid, scored in both halves, with a SEARCH-CORRECTED
    interval on the out-of-sample half."""
    cells = []
    for band in BAND_ORDER:
        for th in THRESHOLDS:
            a, b = score(entries_a, cal, band, th), score(entries_b, cal, band, th)
            if not a and not b:
                continue
            cells.append((band, th, a, b))
    z = z_for(0.05 / max(1, len(cells)))       # Bonferroni over the cells actually tested
    rows = []
    for band, th, a, b in cells:
        ma, _, _ = mean_ci(a)
        mb, lob, hib = mean_ci(b, z=z)
        rows.append((band, th, len(a), ma, len(b), mb, lob, hib))
    return rows, z


def cmd_replay() -> int:
    events = load_backfill(WX_LOG)
    forecasts = load_forecasts(FC_CACHE)
    print("=" * 78)
    print("FORECAST REPLAY - does the forecast beat the book? (deep weather history)")
    print("=" * 78)
    if not events:
        print(f"no weather backfill at {WX_LOG}.")
        return 1
    if not forecasts:
        print(f"no forecast cache at {FC_CACHE} - run `probe` then `fetch` first.")
        return 1
    dates = sorted({d for _, d in events})
    n_mk = sum(len(ev["markets"]) for ev in events.values())
    covered = sum(1 for k in events if k in forecasts)
    print(f"{len(events)} city-days ({dates[0]} .. {dates[-1]}), {n_mk} settled markets, "
          f"{covered} days with forecasts")
    unmapped = sorted({s for s, _ in events if s not in SERIES_TZ})
    if unmapped:
        # A missing timezone would silently band a market by UTC hour, putting a
        # Los Angeles morning in the "midday" bucket. Refuse rather than mislabel.
        print(f"  SKIPPED, no timezone mapping: {', '.join(unmapped)} - add them to "
              f"SERIES_TZ before trusting any number for those cities.")
        events = {k: v for k, v in events.items() if k[0] in SERIES_TZ}
        if not events:
            return 1
    print()

    for strict in (True, False):
        label = ("STRICT - previous-day run only, no lookahead"
                 if strict else
                 "DIAGNOSTIC - archived best-match, CONTAINS LOOKAHEAD, not a result")
        print("-" * 78)
        print(label)
        entries = build_entries(events, forecasts, strict)
        if not entries:
            print("  no entries. " + ("The cache has no previous-day runs - re-fetch with "
                                      "a source that provides them (see `probe`)."
                                      if strict else "No usable quotes."))
            print()
            continue
        wins = {e["window"] for e in entries}
        print(f"  {len(entries)} candidate entries across {len(wins)} independent "
              f"city-day windows")

        # Split on the DATE, not on the window id: window ids start with the city, so
        # splitting on them sorts Austin before New York and produces a by-city split
        # wearing a walk-forward costume. Splitting on the date keeps it chronological
        # and still lets no city-day straddle the boundary.
        first, second, cut = split_by_date(entries)
        print(f"  walk-forward cut at {cut}: "
              f"{len({e['window'] for e in first})} windows in sample, "
              f"{len({e['window'] for e in second})} out")
        cal, mae, ncal = calibrate(first)
        print(f"  forecast error measured on the first half ({ncal} city-day samples, "
              f"MAE {mae:.2f} F):")
        for band in BAND_ORDER:
            if band in cal:
                bias, sigma, n = cal[band]
                print(f"    {band:10} bias {bias:+5.2f} F   sigma {sigma:4.2f} F   n={n}")

        bm, bp, nb = calibration_check(second, cal)
        print()
        print(f"  GATE - out-of-sample Brier on {nb} contracts: "
              f"forecast {bm:.4f} vs price {bp:.4f}  "
              f"-> {'FORECAST WINS' if bm < bp else 'PRICE WINS'}")
        if not (bm < bp):
            print("    The market's own price predicts these settlements better than our")
            print("    forecast does. No threshold can rescue that: any positive cell below")
            print("    is selection, not edge. This is gate G4 refusing to trade.")

        rows, z = _grid(first, second, cal)
        gate_ok = bm < bp
        print()
        print(f"  {'band':10} {'thr':>5} {'--- first half ---':>22}   "
              f"{'--- second half (out of sample) ---':>38}")
        print(f"  {'':10} {'':>5} {'wins':>6} {'$/window':>13}   "
              f"{'wins':>6} {'$/window':>13} {'CI (search-corr)':>20}  verdict")
        rows.sort(key=lambda r: -r[5])
        held = thin = flipped = 0
        for band, th, na, ma, nb2, mb, lob, hib in rows:
            verdict = cell_verdict(na, ma, nb2, mb, lob, gate_ok)
            if verdict.startswith("HELD"):
                held += 1
            elif verdict.startswith("persists"):
                thin += 1
            elif verdict.startswith("FLIPPED"):
                flipped += 1
            ci = f"[{lob:+.3f},{hib:+.3f}]" if nb2 >= 2 else "-"
            print(f"  {band:10} {th:>5.2f} {na:>6} {ma:>+13.4f}   "
                  f"{nb2:>6} {mb:>+13.4f} {ci:>20}  {verdict}")
        print()
        print(f"  intervals are Bonferroni-corrected over the {len(rows)} cells searched "
              f"(z={z:.2f}); the")
        print("  uncorrected 95% version reports a winner on data with nothing in it.")
        print(f"  VERDICT: {held} HELD | {thin} persist-but-thin | {flipped} flipped")
        if strict:
            if not gate_ok:
                print("    Blocked at the gate, so no cell counts regardless of its P&L.")
            elif held:
                print("    A cell is positive in both halves AND its out-of-sample interval")
                print("    clears zero after correcting for the search, with the forecast")
                print("    beating the price on the same contracts. That is the shape of a")
                print("    real conditional edge - what the unconditional screener is blind")
                print("    to by construction.")
            elif thin:
                print("    Survives both halves, interval still touches zero. Keep the data")
                print("    growing; do not tune, do not size up.")
            else:
                print("    Nothing survived. On this history the book already contains the")
                print("    forecast - which is the honest, cheap answer we came for.")
        print()
    print("  Caveats that belong next to any number above: quotes are minute-OHLC closes,")
    print("  not snapshots; 'observed so far' is reanalysis, not the settling station's")
    print("  own reading; and bucket caps are recovered from neighbouring strikes.")
    print("=" * 78)
    return 0


# ── selftest ─────────────────────────────────────────────────────────────────

def _selftest() -> int:
    # ticker parsing: the date must come from the ticker, not from UTC close time
    assert parse_ticker("KXHIGHNY-26SEP11-B75.5") == ("KXHIGHNY", "2026-09-11", "B75.5")
    assert parse_ticker("KXHIGHCHI-26JAN02-T88") == ("KXHIGHCHI", "2026-01-02", "T88")
    assert parse_ticker("garbage") is None

    # bucket inference recovers caps from the neighbouring strike
    ms = [{"kind": "B", "floor": 74.0, "lo": None, "hi": None},
          {"kind": "B", "floor": 76.0, "lo": None, "hi": None},
          {"kind": "B", "floor": 78.0, "lo": None, "hi": None},
          {"kind": "T", "floor": 80.0, "lo": None, "hi": None}]
    infer_buckets(ms)
    assert [(m["lo"], m["hi"]) for m in ms[:3]] == [(74.0, 76.0), (76.0, 78.0), (78.0, 80.0)]
    assert ms[3]["hi"] is None                      # threshold market is unbounded above

    # fair value: a bucket straddling the forecast is the likeliest; probabilities of a
    # complete ladder sum to ~1
    mu, sig = 76.5, 3.0
    ps = [fair_p(m, mu, sig) for m in ms[:3]]
    assert ps[1] > ps[0] and ps[1] > ps[2], ps
    assert abs(fair_p({"lo": -99.0, "hi": None}, mu, sig) - 1.0) < 1e-6

    # fee + interval math
    assert taker_fee(0.50) == 0.02 and taker_fee(0.03) == 0.01
    m, lo, hi = mean_ci([0.1] * 25)
    assert abs(m - 0.1) < 1e-9 and abs(hi - lo) < 1e-9  # no variance -> no CI width
    m2, lo2, hi2 = mean_ci([-0.5, 0.5] * 20)          # a coin flip must straddle zero
    assert lo2 < m2 < hi2 and lo2 < 0 < hi2
    m3, lo3, _ = mean_ci([0.05] * 100 + [0.06] * 100)  # small but consistent: clears zero
    assert lo3 > 0 and abs(m3 - 0.055) < 1e-9
    assert brier([(1.0, True), (0.0, False)]) == 0.0
    assert abs(brier([(0.5, True), (0.5, False)]) - 0.25) < 1e-12

    # mu_at: strict mode must refuse a day with no previous-day run, and must ignore
    # future best-match temperatures (the lookahead we are guarding against)
    day = {"temp": [50.0] * 12 + [90.0] * 12, "prev": [50.0] * 12 + [70.0] * 12,
           "utc_offset": -14400}
    assert mu_at(day, 6.0, strict=True) == 70.0      # forecast says 70 for the afternoon
    assert mu_at(day, 6.0, strict=False) == 90.0     # best-match already "knows" it hit 90
    assert mu_at(day, 20.0, strict=True) == 90.0     # after the fact, observed wins
    assert mu_at({"temp": [1.0], "prev": None}, 0.0, strict=True) is None

    # response -> rows
    resp = {"utc_offset_seconds": -18000,
            "hourly": {"time": [f"2026-03-01T{h:02d}:00" for h in range(24)],
                       "temperature_2m": [40.0 + h for h in range(24)],
                       "temperature_2m_previous_day1": [39.0 + h for h in range(24)]}}
    rows = rows_from_response("KXHIGHNY", resp)
    assert len(rows) == 1 and rows[0]["date"] == "2026-03-01"
    assert rows[0]["temp"][23] == 63.0 and rows[0]["prev"][0] == 39.0
    bare = rows_from_response("KXHIGHNY", {"hourly": {"time": ["2026-03-01T00:00"],
                                                      "temperature_2m": [40.0]}})
    assert bare[0]["prev"] is None                   # explicit: no lookahead-free run

    # end-to-end on a fixture with a PLANTED edge: the book prices every bucket at a flat
    # 0.20 while the forecast is accurate, so the correct bucket is a screaming buy.
    import tempfile
    global WX_LOG, FC_CACHE
    old = (WX_LOG, FC_CACHE)
    with tempfile.TemporaryDirectory() as td:
        WX_LOG, FC_CACHE = Path(td) / "wx.jsonl", Path(td) / "fc.jsonl"
        base = datetime(2026, 6, 1, tzinfo=timezone.utc)
        with open(WX_LOG, "w", encoding="utf-8") as f, \
                open(FC_CACHE, "w", encoding="utf-8") as g:
            for d in range(40):
                day = (base + timedelta(days=d)).date().isoformat()
                dcode = (base + timedelta(days=d)).strftime("%y%b%d").upper()
                high = 74.0 + (d % 3) * 2.0          # truth: 74, 76 or 78
                g.write(json.dumps({"t": "fcday", "series": "KXHIGHNY", "date": day,
                                    "utc_offset": -14400,
                                    "temp": [55.0] * 12 + [high] * 12,
                                    "prev": [55.0] * 12 + [high] * 12}) + "\n")
                for floor in (72.0, 74.0, 76.0, 78.0):
                    tk = f"KXHIGHNY-{dcode}-B{floor}"
                    won = floor <= high + 0.5 < floor + 2.0
                    f.write(json.dumps({"t": "market", "series": "KXHIGHNY", "ticker": tk,
                                        "result": "yes" if won else "no",
                                        "floor_strike": floor,
                                        "close_time": day + "T23:59:00Z"}) + "\n")
                    for hh in (13, 15):              # 09:00 and 11:00 local
                        ts = int((base + timedelta(days=d, hours=hh)).timestamp())
                        f.write(json.dumps({"t": "candle", "series": "KXHIGHNY",
                                            "ticker": tk, "end_period_ts": ts,
                                            "yes_bid": 18, "yes_ask": 20}) + "\n")
        evs = load_backfill(WX_LOG)
        assert len(evs) == 40, len(evs)
        fcs = load_forecasts(FC_CACHE)
        ents = build_entries(evs, fcs, strict=True)
        assert ents, "fixture produced no entries"
        # 13:00 and 15:00 UTC are 09:00 and 11:00 local in June - one entry per band,
        # which also pins that banding is done in LOCAL time, not UTC.
        assert {e["band"] for e in ents} == {"morning", "midday"}, \
            {e["band"] for e in ents}
        # the walk-forward cut must be chronological, never alphabetical-by-city
        a, b, cutd = split_by_date(ents)
        assert cutd and all(e["date"] <= cutd for e in a) and \
            all(e["date"] > cutd for e in b)
        assert not ({e["window"] for e in a} & {e["window"] for e in b})
        cal, mae, n = calibrate(ents)
        assert mae < 1.5, mae                        # forecast is accurate by construction
        pnl = score(ents, cal, "morning", 0.05)
        assert len(pnl) == 40, len(pnl)
        assert sum(pnl) / len(pnl) > 0.2, sum(pnl) / len(pnl)   # planted edge is found
        bm, bp, nb = calibration_check(ents, cal)
        assert bm < bp, (bm, bp)                     # and it beats the flat book
        # ... and with the SAME data but an uninformative forecast, it must find nothing
        for k in fcs:
            fcs[k]["prev"] = [70.0] * 24
            fcs[k]["temp"] = [70.0] * 12 + fcs[k]["temp"][12:]
        ents2 = build_entries(evs, fcs, strict=True)
        cal2, _, _ = calibrate(ents2)
        pnl2 = score(ents2, cal2, "morning", 0.05)
        assert sum(pnl2) / max(1, len(pnl2)) < sum(pnl) / len(pnl)
    WX_LOG, FC_CACHE = old

    # network-shaped probe with an injected transport
    def fake_get(url, params):
        assert params["temperature_unit"] == "fahrenheit"
        return {"utc_offset_seconds": -14400,
                "hourly": {"time": ["2026-05-01T00:00", "2026-05-01T01:00"],
                           "temperature_2m": [60.0, 61.0],
                           "temperature_2m_previous_day1": [59.0, 60.0]}}
    assert cmd_probe(get=fake_get) == 0
    print("selftest OK")
    return 0


def main() -> int:
    args = sys.argv[1:]
    cmd = args[0] if args else "replay"
    src = args[args.index("--source") + 1] if "--source" in args else None
    if cmd == "selftest":
        return _selftest()
    if cmd == "probe":
        return cmd_probe()
    if cmd == "fetch":
        return cmd_fetch(src)
    if cmd == "replay":
        return cmd_replay()
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
