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
from datetime import datetime, timezone
from pathlib import Path

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
LOCK_MIN_DROP = 2.0     # °F current temp must be below the realized high (clearly past peak)
BOUNDARY_MARGIN = 1.0   # °F: don't trust a lock within this of a bucket edge (T-group/revision risk)
NEAR_CERTAIN = 0.98     # prob we assign a locked outcome (not 1.0 — CLI can still revise)


# ── pure logic (testable) ───────────────────────────────────────────────────

def _local_hour(iso_local: str):
    try:
        return int(str(iso_local)[11:13])
    except (ValueError, TypeError):
        return None


def realized_high(obs: list):
    """obs = [(iso_local_time, tmpf), …] → dict(high, last_temp, last_time, n) or None."""
    pts = [(t, float(v)) for t, v in obs if v not in (None, "", "M")]
    if not pts:
        return None
    return {"high": max(v for _, v in pts), "last_temp": pts[-1][1],
            "last_time": pts[-1][0], "n": len(pts)}


def is_locked(local_hr, high, last_temp,
              min_hour: int = LOCK_MIN_HOUR, min_drop: float = LOCK_MIN_DROP) -> bool:
    """The high is 'locked' once it's evening AND temps have clearly fallen off the
    peak — the daily max won't be beaten before midnight."""
    if local_hr is None or high is None or last_temp is None:
        return False
    return local_hr >= min_hour and (high - last_temp) >= min_drop


def lock_signal(kind: str, strike: float, high: float, market_yes,
                margin: float = BOUNDARY_MARGIN):
    """Given a LOCKED realized high, is a bucket near-certain and mispriced?
    Returns (side, certain_prob, edge) or None if too close to a boundary to trust.
    edge = certain_prob − market's implied prob for that side (positive = mispriced
    in our favor). market_yes may be None (then edge=None, signal still shown)."""
    from join_weather_trials import BAND_HALF_WIDTH
    yes_is, no_is = None, None
    if kind == "above":
        if high >= strike + margin:
            yes_is = True
        elif high <= strike - margin:
            yes_is = False
    elif kind == "band":
        lo, hi = strike - BAND_HALF_WIDTH, strike + BAND_HALF_WIDTH
        if lo + margin <= high <= hi - margin:
            yes_is = True
        elif high <= lo - margin or high >= hi + margin:
            yes_is = False
    if yes_is is None:
        return None                                   # within margin of an edge → skip
    if yes_is:                                         # YES near-certain → buy YES
        edge = (NEAR_CERTAIN - market_yes) if market_yes is not None else None
        return ("YES", NEAR_CERTAIN, (round(edge, 3) if edge is not None else None))
    # NO near-certain → buy NO (implied NO cost = market_yes; near-certain payout)
    edge = (market_yes - (1 - NEAR_CERTAIN)) if market_yes is not None else None
    return ("NO", NEAR_CERTAIN, (round(edge, 3) if edge is not None else None))


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

def fetch_iem_today(station: str, tz: str) -> list:
    """Today's (local) ASOS temperature obs for a station via the Iowa Environmental
    Mesonet. Returns [(iso_local, tmpf), …]. Free, no key."""
    import requests
    now_local_date = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")  # rough; tz refined by IEM
    y, m, d = now_local_date.split("-")
    url = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
    params = {"station": station, "data": "tmpf", "tz": tz,
              "year1": y, "month1": m, "day1": d,
              "year2": y, "month2": m, "day2": d,
              "format": "onlycomma", "missing": "M", "latlon": "no"}
    r = requests.get(url, params=params, timeout=25)
    r.raise_for_status()
    out = []
    for line in r.text.splitlines()[1:]:               # skip header
        parts = line.split(",")
        if len(parts) >= 3:
            out.append((parts[1].strip(), parts[2].strip()))
    return out


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
        yb, ya, last = m.get("yes_bid"), m.get("yes_ask"), m.get("last_price")
        if isinstance(yb, (int, float)) and isinstance(ya, (int, float)) and 0 < yb and ya < 100:
            yes_p = (yb + ya) / 200.0
        elif isinstance(last, (int, float)) and 0 < last < 100:
            yes_p = last / 100.0
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
    ts = datetime.now(timezone.utc).isoformat()
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
            sig = lock_signal(kind, strike, rh["high"], yes_p)
            if sig is None:
                continue
            side, prob, edge = sig
            rec = {"ts": ts, "series": s, "station": station, "ticker": tk,
                   "kind": kind, "strike": strike, "realized_high": rh["high"],
                   "last_temp": rh["last_temp"], "side": side, "market_yes": yes_p,
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
          f"(target ≥ {NEAR_CERTAIN:.0%}; below it = the lock is wrong, not just unlucky)")
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
    # realized high
    rh = realized_high([("2026-06-19T13:00", 80), ("2026-06-19T17:00", 88),
                        ("2026-06-19T20:00", 79)])
    assert rh["high"] == 88 and rh["last_temp"] == 79, rh
    print("realized_high OK")
    # lock: evening + clearly off the peak
    assert is_locked(20, 88, 79) is True
    assert is_locked(14, 88, 87) is False          # midday, still near peak
    assert is_locked(20, 88, 87) is False          # evening but barely dropped
    print("is_locked OK")
    # lock_signal — above strike, realized clearly above → YES near-certain, mispriced
    sig = lock_signal("above", 85.0, 88.0, market_yes=0.60)
    assert sig == ("YES", 0.98, 0.38), sig
    # above strike, realized clearly below → NO near-certain
    sig = lock_signal("above", 92.0, 88.0, market_yes=0.30)
    assert sig[0] == "NO" and abs(sig[2] - (0.30 - 0.02)) < 1e-9, sig
    # within boundary margin → no signal (T-group/revision risk)
    assert lock_signal("above", 88.5, 88.0, market_yes=0.5) is None
    # band: a 1°F-wide band CAN'T be safely locked with a 1°F margin (honest:
    # T-group/revision risk) → None at default margin, YES only with tight margin.
    assert lock_signal("band", 88.0, 88.0, market_yes=0.4) is None
    assert lock_signal("band", 88.0, 88.0, market_yes=0.4, margin=0.3)[0] == "YES"
    # band: realized well outside → NO (works at default margin)
    assert lock_signal("band", 80.0, 88.0, market_yes=0.4)[0] == "NO"
    print("lock_signal OK (above/band, YES/NO, boundary-margin skip; tight bands not lockable)")
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
