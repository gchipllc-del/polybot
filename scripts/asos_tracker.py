#!/usr/bin/env python3
"""asos_tracker — the 'bucket-lock' play: read the REALIZED daily high from the
ASOS ground station that actually SETTLES Kalshi weather markets, in near-real-time,
and flag when the high is physically locked but the market still misprices the
now-near-certain bucket.

Why this and not a satellite: Kalshi weather settles on the NWS CLI daily MAX from a
specific ASOS station (e.g. KNYC = Central Park) — a 2 m ground thermometer, not
satellite radiance. So the fastest 'truth' is that station's own obs (free, via the
Iowa Environmental Mesonet). By evening, with temps falling, the day's high is set
hours before next-morning settlement — a fast-OBSERVATION edge, not a forecast.

READ-ONLY / paper. Logs lock signals for forward-collection; places NO orders.

  python scripts/asos_tracker.py probe KXHIGHNY   # RUN FIRST: raw IEM + Kalshi ladder, VERIFY station
  python scripts/asos_tracker.py scan              # all cities: realized high vs ladder, flag locks
  python scripts/asos_tracker.py selftest

⚠ VERIFY the STATIONS map below against each market's contract terms before trusting
  a signal — the realized high must come from the EXACT station Kalshi settles on.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import NormalDist
from zoneinfo import ZoneInfo

_N = NormalDist()

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
LOG = ROOT / "data" / "asos_lock.jsonl"

# series → (ASOS station id, IANA tz). VERIFIED 2026-06-21 against the repo's
# canonical settlement coords (fetch_backtest_data._cities, derived from Kalshi
# settlement-source URLs) + lib/weather_daily_signal.DAILY_CITIES. Chicago was
# MDW → corrected to ORD (both sources place it at O'Hare). DFW (not DAL) and IAH
# (not HOU) confirmed. Re-`probe` any city if Kalshi changes its contract terms.
STATIONS = {
    "KXHIGHNY":   ("NYC", "America/New_York"),     # Central Park (verified)
    "KXHIGHCHI":  ("ORD", "America/Chicago"),       # O'Hare (verified — was MDW, wrong)
    "KXHIGHMIA":  ("MIA", "America/New_York"),
    "KXHIGHLAX":  ("LAX", "America/Los_Angeles"),
    "KXHIGHDEN":  ("DEN", "America/Denver"),
    "KXHIGHAUS":  ("AUS", "America/Chicago"),
    "KXHIGHPHIL": ("PHL", "America/New_York"),
    "KXHIGHHOU":  ("IAH", "America/Chicago"),        # Bush (verified — not HOU/Hobby)
    "KXHIGHTATL": ("ATL", "America/New_York"),
    "KXHIGHTBOS": ("BOS", "America/New_York"),
    "KXHIGHTDAL": ("DFW", "America/Chicago"),        # DFW (verified — not DAL/Love)
    "KXHIGHTDC":  ("DCA", "America/New_York"),
    "KXHIGHTLV":  ("LAS", "America/Los_Angeles"),
    "KXHIGHTMIN": ("MSP", "America/Chicago"),
    "KXHIGHTNOLA":("MSY", "America/Chicago"),
    "KXHIGHTOKC": ("OKC", "America/Chicago"),
    "KXHIGHTPHX": ("PHX", "America/Phoenix"),
    "KXHIGHTSATX":("SAT", "America/Chicago"),
    "KXHIGHTSEA": ("SEA", "America/Los_Angeles"),
    "KXHIGHTSFO": ("SFO", "America/Los_Angeles"),
}

LOCK_MIN_HOUR = 19      # local hour after which the daily high is almost always set
LOCK_MIN_DROP = 3.0     # °F current temp must be below the realized high (clearly past peak)
SPIKE_TOL = 2.5         # °F: a reading with no neighbour within this is treated as a spike
# Distance-aware certainty (replaces the old flat NEAR_CERTAIN + hard YES/NO margins).
# We model the SETTLED daily max as Normal around our observed high with σ = the sensor +
# CLI-revision uncertainty — "the degree or two the thermometer isn't sure of". So
# confidence fades smoothly near a strike instead of a hard margin cliff:
#   P(settles above strike) = Φ((observed_high − strike) / SETTLE_SIGMA_F)
# A lock only fires when the modeled P(correct side) ≥ MIN_LOCK_PROB. With σ=1.5 and
# min_prob=0.95 that's a ~2.5°F effective margin, but it now SCALES with the measured σ.
# Calibrate SETTLE_SIGMA_F from settled locks with: python scripts/asos_sigma.py
SETTLE_SIGMA_F = 1.5    # °F sensor + CLI-revision uncertainty (the "degree or two")
MIN_LOCK_PROB = 0.95    # don't call it a lock unless modeled P(correct side) ≥ this
NEAR_CERTAIN = 0.99     # cap on assigned certainty — never claim 1.0 (CLI can always revise)


# ── pure logic (testable) ───────────────────────────────────────────────────

def event_local_date(rec: dict) -> str:
    """The station-LOCAL calendar date a lock belongs to (the settlement day), as
    'YYYY-MM-DD'. Prefers the stored `local_date`; otherwise converts the UTC `ts` to the
    series' station tz. ts is stored in UTC and locks fire in the evening (≥19:00 local),
    so its UTC date is usually the NEXT day — using ts[:10] directly is an off-by-one that
    corrupts cohort splits and backtest re-fetches (audit 2026-06-23)."""
    ld = rec.get("local_date")
    if ld:
        return str(ld)[:10]
    ts = str(rec.get("ts", ""))
    st = STATIONS.get(rec.get("series", ""))
    tz = st[1] if st else "America/New_York"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ZoneInfo(tz)).date().isoformat()
    except (ValueError, KeyError):
        return ts[:10]


def _local_hour(iso_local: str):
    try:
        return int(str(iso_local)[11:13])
    except (ValueError, TypeError):
        return None


def _minute_index(iso) -> int | None:
    """Rough within-day minute index from an IEM timestamp ('YYYY-MM-DD HH:MM' or with
    a 'T'). Only used for same-day time *differences*, so day-of-month granularity is
    enough; returns None if unparseable."""
    try:
        s = str(iso).replace("T", " ")
        date_part, time_part = s.split(" ")[0], s.split(" ")[1]
        dd = int(date_part.split("-")[2])
        hh, mm = (int(x) for x in time_part.split(":")[:2])
        return (dd * 24 + hh) * 60 + mm
    except (ValueError, IndexError, TypeError):
        return None


def _qc_high(pts: list, tol: float = SPIKE_TOL, window_min: int = 30) -> float:
    """QC'd daily high: trust the raw max UNLESS it is a dense, uncorroborated spike.
    Walking high→low, the first reading that is either time-isolated (no other reading
    within `window_min` min — a sparse/hourly peak we must trust) OR corroborated (a
    neighbour within `window_min` agrees to within `tol`°F) is the QC'd high. Only a
    reading with dense neighbours that ALL disagree by >tol is demoted as a lone sensor
    spike. This drops 1-min glitches on dense feeds without ever discarding a genuine peak
    on a sparse feed (the over-correction bug fixed 2026-06-23). NOTE: does NOT fix a
    consistently-wrong day-window or station — those are upstream of QC."""
    vals = [v for _, v in pts]
    times = [_minute_index(t) for t, _ in pts]
    for i in sorted(range(len(vals)), key=lambda k: -vals[k]):       # high → low
        if times[i] is None:
            return vals[i]                                           # can't assess → trust
        near = [vals[j] for j in range(len(vals))
                if j != i and times[j] is not None
                and abs(times[i] - times[j]) <= window_min]
        if not near or any(abs(vals[i] - nb) <= tol for nb in near):
            return vals[i]                  # isolated (sparse peak) OR corroborated → real
        # dense neighbours all >tol below → lone spike → demote, try the next-highest
    return max(vals)


def realized_high(obs: list):
    """obs = [(iso_local_time, tmpf), …] → dict(high, qc_high, last_temp, last_time, n)
    or None. `high` is the raw max (conservative upper bound, used for NO locks);
    `qc_high` removes lone spikes (used for YES locks) to approximate the QC'd NWS CLI
    daily max Kalshi settles on."""
    pts = [(t, float(v)) for t, v in obs if v not in (None, "", "M")]
    if not pts:
        return None
    return {"high": max(v for _, v in pts), "qc_high": _qc_high(pts),
            "last_temp": pts[-1][1], "last_time": pts[-1][0], "n": len(pts)}


def is_locked(local_hr, high, last_temp,
              min_hour: int = LOCK_MIN_HOUR, min_drop: float = LOCK_MIN_DROP) -> bool:
    """The high is 'locked' once it's evening AND temps have clearly fallen off the
    peak — the daily max won't be beaten before midnight."""
    if local_hr is None or high is None or last_temp is None:
        return False
    return local_hr >= min_hour and (high - last_temp) >= min_drop


def lock_signal(kind: str, strike: float, high: float, market_yes,
                qc_high: float = None,
                sigma: float = SETTLE_SIGMA_F, min_prob: float = MIN_LOCK_PROB):
    """Is a bucket near-certain and mispriced, given a LOCKED realized high?

    Models the SETTLED daily max as Normal(observed_high, σ) — σ = sensor + CLI-revision
    uncertainty — so confidence fades smoothly near a strike (no hard margin cliff).
    YES uses `qc_high` (spikes removed → Mode-B defence); NO uses the raw `high` (a
    conservative upper bound → Mode-A late-warming defence). Fires only when the modeled
    P(correct side) ≥ min_prob. Returns (side, certainty, edge) or None. certainty is
    capped at NEAR_CERTAIN (the CLI can always revise). edge = certainty − market's
    implied prob for that side; market_yes may be None (edge=None, signal still shown)."""
    from join_weather_trials import BAND_HALF_WIDTH
    if qc_high is None:
        qc_high = high
    if sigma <= 0:
        return None
    if kind == "above":
        p_yes = _N.cdf((qc_high - strike) / sigma)            # P(settled > strike)
        p_no = _N.cdf((strike - high) / sigma)                # P(settled < strike), raw=conservative
    elif kind == "band":
        lo, hi = strike - BAND_HALF_WIDTH, strike + BAND_HALF_WIDTH
        # Both sides share the qc-defended center. Using the RAW high for NO was wrong:
        # for a band, a lone spike inflates the raw center ABOVE the band → p_no→1 and a
        # correct YES flips to a losing NO (audit 2026-06-23). p_no = 1 − p_yes is the
        # consistent two-sided exclusion. (For 'above', raw stays the conservative bound.)
        p_yes = max(0.0, _N.cdf((hi - qc_high) / sigma) - _N.cdf((lo - qc_high) / sigma))
        p_no = 1.0 - p_yes
    else:
        return None
    if p_yes >= min_prob and p_yes >= p_no:
        cert = min(p_yes, NEAR_CERTAIN)
        edge = (cert - market_yes) if market_yes is not None else None
        return ("YES", round(cert, 3), (round(edge, 3) if edge is not None else None))
    if p_no >= min_prob:
        cert = min(p_no, NEAR_CERTAIN)
        edge = (cert - (1.0 - market_yes)) if market_yes is not None else None
        return ("NO", round(cert, 3), (round(edge, 3) if edge is not None else None))
    return None                                               # neither side near-certain → skip


def lock_entry_cost(side: str, market_yes) -> float | None:
    """Paper cost per 1 contract of the locked side: YES costs market_yes, NO costs
    (1 − market_yes). None if unpriced (can't score P&L)."""
    if market_yes is None:
        return None
    return float(market_yes) if side == "YES" else 1.0 - float(market_yes)


def lock_pnl(side: str, market_yes, result: str) -> float | None:
    """Realized paper P&L per 1 contract once the market settles (gross, $1 stake
    convention). result ∈ {'yes','no'}. won → 1 − cost, lost → −cost."""
    cost = lock_entry_cost(side, market_yes)
    if cost is None or result not in ("yes", "no"):
        return None
    won = (result == side.lower())
    return round((1.0 - cost) if won else (-cost), 4)


# ── live fetch (needs network / Kalshi auth) ────────────────────────────────

def fetch_iem_day(station: str, tz: str, target=None) -> list:
    """ASOS temperature obs for a station on a STATION-LOCAL calendar day (default: today)
    via the Iowa Environmental Mesonet. Returns [(iso_local, tmpf), …].

    `target` (a date) lets the backtest replay past days — IEM is a historical archive.
    Fixed 2026-06-23 (the +3.75°F hot bias): the date is taken in the *station's* tz (was
    the server's), and obs are CLIPPED client-side to the target local date (was a fragile
    day1==day2 window that could include a hotter neighbouring day). Free, no key."""
    import requests
    from zoneinfo import ZoneInfo
    if target is None:
        target = datetime.now(ZoneInfo(tz)).date()
    nxt = target + timedelta(days=1)
    url = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
    params = {"station": station, "data": "tmpf", "tz": tz,
              "year1": target.year, "month1": target.month, "day1": target.day,
              "year2": nxt.year, "month2": nxt.month, "day2": nxt.day,
              "format": "onlycomma", "missing": "M", "latlon": "no"}
    r = requests.get(url, params=params, timeout=25)
    r.raise_for_status()
    tgt = target.strftime("%Y-%m-%d")
    out = []
    for line in r.text.splitlines()[1:]:               # skip header
        parts = line.split(",")
        if len(parts) >= 3 and parts[1].strip()[:10] == tgt:   # clip to the settlement day
            out.append((parts[1].strip(), parts[2].strip()))
    return out


def fetch_iem_today(station: str, tz: str) -> list:
    """Today's station-local obs — thin wrapper over fetch_iem_day (see it for the fix)."""
    return fetch_iem_day(station, tz)


def _price01(m: dict, *names):
    """Best price as a 0-1 probability: Kalshi *_dollars fields are already 0-1; bare
    cent names are /100. None if absent. (The bare-name read was the field-name bug.)"""
    for n in names:
        v = m.get(n)
        if v is not None:
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            return v if v <= 1.0 else v / 100.0
    return None


def fetch_market_ladder(series: str) -> list:
    """Open markets for the series' nearest event → [(ticker, kind, strike, yes_p)].
    yes_p from last_price (quote-on-demand books are often empty) else the mid."""
    from fetch_backtest_data import _kalshi_get
    from join_weather_trials import parse_strike2
    try:
        data = _kalshi_get("/markets", {"series_ticker": series, "status": "open", "limit": 200})
    except Exception as e:
        print(f"  ! {series}: {e}", file=sys.stderr)
        return []
    out = []
    for m in data.get("markets", []) or []:
        tk = m.get("ticker", "")
        kind, strike = parse_strike2(tk, m.get("yes_sub_title", "") or "")
        if strike is None:
            continue
        # Kalshi /markets serves prices as *_dollars (float 0-1). The old code read the
        # bare yes_bid/yes_ask, which are absent here → always None → market_yes never
        # recorded (the field-name bug that made asos_edge read "0/155 priced"). Read the
        # _dollars fields (0-1), with the bare cents names as a fallback.
        yb = _price01(m, "yes_bid_dollars", "yes_bid")
        ya = _price01(m, "yes_ask_dollars", "yes_ask")
        last = _price01(m, "last_price_dollars", "last_price")
        if yb is not None and ya is not None and 0 < yb and ya < 1.0:
            yes_p = (yb + ya) / 2.0
        elif last is not None and 0 < last < 1.0:
            yes_p = last
        else:
            yes_p = None
        out.append((tk, kind, strike, yes_p))
    return out


def cmd_probe(args) -> None:
    s = args.series
    st = STATIONS.get(s)
    if not st:
        print(f"no station mapped for {s} — add it to STATIONS (and verify vs contract terms).")
        return
    station, tz = st
    print(f"=== PROBE {s} → station {station} ({tz}) — VERIFY this is the settlement station ===")
    obs = fetch_iem_today(station, tz)
    print(f"IEM obs today: {len(obs)} rows; last 5: {obs[-5:]}")
    rh = realized_high(obs)
    print(f"realized high so far: {rh}")
    ladder = fetch_market_ladder(s)
    print(f"Kalshi ladder ({len(ladder)} markets): {ladder[:8]}")
    print("\nCHECK: does 'realized high so far' look right for that city today, and is "
          "the station the one Kalshi names in its contract terms? If yes, trust scan.")


def _load_ledger() -> list:
    if not LOG.exists():
        return []
    out = []
    for line in LOG.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def cmd_scan(args) -> None:
    series = [args.series] if args.series else list(STATIONS)
    now_utc = datetime.now(timezone.utc)
    ts = now_utc.isoformat()
    seen = {r.get("ticker") for r in _load_ledger()}    # one lock entry per market, ever
    hits = []
    for s in series:
        st = STATIONS.get(s)
        if not st:
            continue
        station, tz = st
        try:
            obs = fetch_iem_today(station, tz)
        except Exception as e:
            print(f"  ! {s}: {e}", file=sys.stderr)
            continue
        rh = realized_high(obs)
        if not rh:
            continue
        locked = is_locked(_local_hour(rh["last_time"]), rh["high"], rh["last_temp"])
        if not locked:
            continue
        for tk, kind, strike, yes_p in fetch_market_ladder(s):
            if tk in seen:
                continue                                # already locked this market earlier
            sig = lock_signal(kind, strike, rh["high"], yes_p, qc_high=rh["qc_high"])
            if sig is None:
                continue
            side, prob, edge = sig
            rec = {"ts": ts, "local_date": now_utc.astimezone(ZoneInfo(tz)).date().isoformat(),
                   "series": s, "station": station, "ticker": tk,
                   "kind": kind, "strike": strike, "realized_high": rh["high"],
                   "qc_high": rh["qc_high"], "last_temp": rh["last_temp"],
                   "side": side, "cert": prob, "market_yes": yes_p,
                   "edge": edge, "status": "open", "result": "", "paper_pnl": None,
                   "resolved_at": ""}
            hits.append(rec)
            seen.add(tk)
    if hits:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as f:
            for h in hits:
                f.write(json.dumps(h) + "\n")
    flagged = [h for h in hits if h["edge"] is not None and h["edge"] > 0]
    print(f"=== asos bucket-lock scan — {len(hits)} locked buckets, "
          f"{len(flagged)} mispriced (edge>0) ===")
    for h in sorted(flagged, key=lambda x: -x["edge"])[:20]:
        print(f"  {h['ticker'][:30]:30} {h['side']:>3} realized {h['realized_high']:.1f}°F "
              f"strike {h['strike']:.1f} mkt_yes {h['market_yes']} edge {h['edge']:+.2f}")
    print("\n  Logged to data/asos_lock.jsonl. NOTE: top-of-book/last-price + an unrevised "
          "live high — confirm fillability and CLI-revision risk before trusting. Paper only.")


def cmd_settle(args) -> None:
    from fetch_backtest_data import _kalshi_get
    rows = _load_ledger()
    if not rows:
        print("no asos lock signals yet.")
        return
    changed, pnl_now = 0, 0.0
    for r in rows:
        if r.get("status") != "open":
            continue
        try:
            m = _kalshi_get(f"/markets/{r['ticker']}", {}).get("market", {})
        except Exception:
            continue
        res = str(m.get("result", "") or "").lower()
        if res not in ("yes", "no"):
            continue
        pnl = lock_pnl(r.get("side", ""), r.get("market_yes"), res)
        r["result"] = res
        r["status"] = "won" if res == str(r.get("side", "")).lower() else "lost"
        r["paper_pnl"] = pnl
        r["resolved_at"] = datetime.now(timezone.utc).isoformat()
        if pnl is not None:
            pnl_now += pnl
        changed += 1
    if changed:
        with LOG.open("w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
    print(f"asos settle: resolved {changed} locks, paper P&L this run ${pnl_now:+.2f}")


def cmd_report(args) -> None:
    rows = _load_ledger()
    settled = [r for r in rows if r.get("status") in ("won", "lost")]
    openn = [r for r in rows if r.get("status") == "open"]
    print("=== asos bucket-lock scorecard (paper, observation edge) ===")
    print(f"{len(settled)} settled locks, {len(openn)} open.")
    if not settled:
        print("  no settled locks yet — scan logs them in the evening lock window; "
              "settle after next-day close. Judge by per-day PSR/DSR once n≥5 days.")
        return
    hits = sum(1 for r in settled if r["status"] == "won")
    priced = [r for r in settled if r.get("paper_pnl") is not None]
    net = sum(float(r["paper_pnl"]) for r in priced)
    print(f"  hit-rate {hits}/{len(settled)} = {hits/len(settled):.0%} "
          f"(target ≥ {MIN_LOCK_PROB:.0%}, the lock floor; below it = σ too small or station wrong)")
    print(f"  paper net ${net:+.2f} over {len(priced)} priced locks")
    # per-day returns → PSR/DSR/MinTRL (distinct days are the independent sample)
    by_day: dict = {}
    for r in priced:
        by_day.setdefault(str(r.get("ts", ""))[:10], 0.0)
        by_day[str(r.get("ts", ""))[:10]] += float(r["paper_pnl"])
    daily = [by_day[d] for d in sorted(by_day)]
    if len(daily) >= 5:
        from lib.hermes_significance import (probabilistic_sharpe_ratio,
                                             deflated_sharpe_ratio,
                                             min_track_record_length)
        psr = probabilistic_sharpe_ratio(daily)
        dsr = deflated_sharpe_ratio(daily, n_trials=args.trials)
        mtrl = min_track_record_length(daily)
        mt_s = "∞" if mtrl == float("inf") else (str(int(mtrl)) if mtrl is not None else "n<5")
        verdict = ("REAL (DSR≥0.95)" if (dsr and dsr >= 0.95 and net > 0)
                   else "no edge / efficient" if (psr is not None and psr < 0.5)
                   else "provisional — keep collecting")
        print(f"  per-day PSR {('n<5' if psr is None else f'{psr:.2f}')} · "
              f"DSR(@{args.trials}) {('n<5' if dsr is None else f'{dsr:.2f}')} · "
              f"MinTRL {mt_s} over {len(daily)} days → {verdict}")
    else:
        print(f"  only {len(daily)} day(s) — need ≥5 distinct days for PSR/DSR.")
    print("  NOTE: paper, top-of-book entry. The risk is CLI revision + fillability in "
          "the thin overnight book; a clean hit-rate ≥98% with DSR≥0.95 = the edge is real.")


def selftest() -> int:
    # realized high — sparse (hourly) feed: qc_high falls back to the raw max
    rh = realized_high([("2026-06-19T13:00", 80), ("2026-06-19T17:00", 88),
                        ("2026-06-19T20:00", 79)])
    assert rh["high"] == 88 and rh["qc_high"] == 88 and rh["last_temp"] == 79, rh
    # dense feed: a lone spike (99) is uncorroborated → dropped; corroborated peak (91) kept
    rhd = realized_high([("2026-06-19 14:00", 90), ("2026-06-19 14:05", 91),
                         ("2026-06-19 14:10", 90), ("2026-06-19 14:15", 99),
                         ("2026-06-19 14:20", 90)])
    assert rhd["high"] == 99 and rhd["qc_high"] == 91, rhd
    # mixed cadence: a dense morning cluster (74/74/75) + an ISOLATED real afternoon peak
    # (88). qc_high must keep 88, not demote to the corroborated 75 (the over-correction bug).
    rhm = realized_high([("2026-06-19 09:00", 74), ("2026-06-19 09:05", 74),
                         ("2026-06-19 09:10", 75), ("2026-06-19 15:00", 88)])
    assert rhm["high"] == 88 and rhm["qc_high"] == 88, rhm
    print("realized_high OK (sparse peak kept, dense spike dropped, isolated peak kept)")
    # lock: evening + clearly off the peak
    assert is_locked(20, 88, 79) is True
    assert is_locked(14, 88, 87) is False          # midday, still near peak
    assert is_locked(20, 88, 87) is False          # evening but barely dropped
    print("is_locked OK")
    # lock_signal — distance-aware certainty Φ((high−strike)/σ), σ=1.5, min_prob=0.95
    # YES: high 5°F over strike → Φ(3.33)=0.9996, capped at NEAR_CERTAIN 0.99
    sig = lock_signal("above", 83.0, 88.0, market_yes=0.60)          # qc=high=88
    assert sig == ("YES", 0.99, 0.39), sig
    # Mode B: raw 95 would lock, but qc_high 90 → Φ(2/1.5)=0.91 < 0.95 → no YES, no NO
    assert lock_signal("above", 88.0, 95.0, market_yes=0.5, qc_high=90.0) is None
    # qc_high 5°F over → YES regardless of the raw high
    assert lock_signal("above", 85.0, 95.0, market_yes=0.5, qc_high=90.0)[0] == "YES"
    # NO: raw high 4°F under strike → Φ(2.67)=0.996 → NO; edge = cert − (1−market_yes)
    sig = lock_signal("above", 92.0, 88.0, market_yes=0.30)
    assert sig[0] == "NO" and abs(sig[2] - (0.99 - 0.70)) < 1e-9, sig
    # Mode A: a thin NO (high 1°F under) → Φ(0.67)=0.75 < 0.95 → rejected
    assert lock_signal("above", 89.0, 88.0, market_yes=0.30) is None
    # right at the strike → ~0.5 each way → no signal
    assert lock_signal("above", 88.5, 88.0, market_yes=0.5) is None
    # σ scales the effective margin: 3°F over locks at σ=1.5 (Φ(2)=0.977) but not σ=3 (Φ(1)=0.84)
    assert lock_signal("above", 88.0, 91.0, market_yes=0.5)[0] == "YES"
    assert lock_signal("above", 88.0, 91.0, market_yes=0.5, sigma=3.0) is None
    # band: 1°F-wide band not lockable at σ=1.5, but YES at a tight σ; far-outside → NO
    assert lock_signal("band", 88.0, 88.0, market_yes=0.4) is None
    assert lock_signal("band", 88.0, 88.0, market_yes=0.4, sigma=0.2)[0] == "YES"
    assert lock_signal("band", 80.0, 88.0, market_yes=0.4)[0] == "NO"
    # band spike defence: qc_high is IN the band but a lone spike pushed raw high above it.
    # Must NOT flip to a losing NO (p_no now shares the qc center, not the raw high).
    assert lock_signal("band", 88.0, 94.0, market_yes=0.35, qc_high=88.0, sigma=0.2)[0] == "YES"
    assert lock_signal("band", 88.0, 91.0, market_yes=0.5, qc_high=88.0) is None
    print("lock_signal OK (distance-aware certainty, qc/raw split, σ-scaled margin, band spike-safe)")
    # event_local_date: an evening lock stored in UTC maps to the LOCAL settlement day
    assert event_local_date({"ts": "2026-06-21T03:00:00+00:00", "series": "KXHIGHTLV"}) == "2026-06-20"
    assert event_local_date({"ts": "2026-06-21T00:00:00+00:00", "series": "KXHIGHNY"}) == "2026-06-20"
    assert event_local_date({"local_date": "2026-07-04", "ts": "x", "series": "?"}) == "2026-07-04"
    print("event_local_date OK (UTC→local settlement day, stored local_date wins)")
    # P&L: YES locked at 0.60 cost, wins → +0.40; loses → −0.60
    assert lock_pnl("YES", 0.60, "yes") == 0.40, lock_pnl("YES", 0.60, "yes")
    assert lock_pnl("YES", 0.60, "no") == -0.60
    # NO locked at market_yes 0.30 → cost 0.70; wins (res no) → +0.30; loses → −0.70
    assert lock_pnl("NO", 0.30, "no") == 0.30
    assert lock_pnl("NO", 0.30, "yes") == -0.70
    assert lock_pnl("YES", None, "yes") is None and lock_pnl("YES", 0.6, "") is None
    print("lock_pnl OK")
    print("PASS")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["probe", "scan", "settle", "report", "selftest"])
    ap.add_argument("series", nargs="?", help="e.g. KXHIGHNY (probe needs it; scan optional)")
    ap.add_argument("--trials", type=int, default=8, help="DSR search-breadth penalty")
    args = ap.parse_args()
    if args.mode == "selftest":
        raise SystemExit(selftest())
    if args.mode == "probe":
        if not args.series:
            ap.error("probe needs a series, e.g. KXHIGHNY")
        cmd_probe(args)
    elif args.mode == "scan":
        cmd_scan(args)
    elif args.mode == "settle":
        cmd_settle(args)
    else:
        cmd_report(args)


if __name__ == "__main__":
    main()
