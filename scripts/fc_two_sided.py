#!/usr/bin/env python3
"""fc2s — the forecast two-sided paper sleeve (LIVE EXECUTION TEST, paper only).

The backtest verdict (forecast_skill_days.py over 28,415 markets / 285 OOS
days): the Open-Meteo forecast carries real RANK skill — at a fixed price,
forecast-cheap markets resolve YES ~15pp more often than forecast-rich ones —
and trading it two-sided (buy NO when the market's YES looks rich vs the
forecast, buy YES when it looks cheap) is +0.04/ct net of fees, 67% green by
day, with the directional heat-beta mostly cancelled (corr −0.37 vs −0.84 for
the price-only fade). What the backtest CANNOT test is execution: thin,
flickering books, spreads, and whether ~20 two-sided fills/day exist at all.
This sleeve answers that — on paper, next to (not replacing) the weather_fade
sleeve, so the two rules accumulate live scorecards side by side.

Rules (mirrors the backtest exactly, plus live-only guards):
  * day-ahead ONLY — skip markets whose event day has already begun at the
             city: by afternoon the high is realized, the market knows it, and
             a day-ahead forecast model fading those prices is pure adverse
             selection (the backtest entered at the earliest/open sample, and
             entry-realism showed the edge gone near close)
  * gate     |p_forecast − market_yes| ≥ thr (default 0.05, the headline cell)
  * fill     taker on the chosen side, from the REAL order book
  * band     0.10 ≤ market YES ≤ 0.90, sane fill, ≤ MAX_SLIP from the signal price
  * fee      Kalshi fee modeled at entry (honest after-fee scorecard)
  * cap      per-event-date risk cap ($20 paper / ~$6 live) — the residual corr
             −0.37 means one hot day still hits multiple cities; the cap bounds
             that drawdown without freezing the paper sample (--day-cap to tune)
  * forecast Open-Meteo LIVE forecast (same source the backtest validated),
             p_yes via the same σ=3°F normal model, band-aware (B-strikes)

The forecast is a RANK signal (its Brier is worse than the market's): it picks
the side, it is never trusted as a price — so EV is reported vs the market
price, and the scorecard (not the model) is the judge.

Commands:
  python scripts/fc_two_sided.py scan [--thr 0.05] [--show]
  python scripts/fc_two_sided.py settle
  python scripts/fc_two_sided.py report
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from weather_fade import (kalshi_fee, _open_weather_quotes, WEATHER_SERIES,   # noqa: E402
                          FILL_FLOOR, FILL_CEIL, MIN_HOURS_TO_CLOSE,
                          MAX_HOURS_TO_CLOSE, DEFAULT_BANKROLL,
                          DEFAULT_RISK_PCT, DEFAULT_MAX_TRADE_USD, _city_of)
from join_weather_trials import parse_strike2, parse_event_date, forecast_p_yes  # noqa: E402

LEDGER = ROOT / "data" / "fc2s_paper.jsonl"
SCAN_STATUS = ROOT / "data" / "fc2s_scan_status.json"
DEFAULT_THR = 0.05            # the backtest's headline cell
SIGMA_F = 3.0                 # forecast-error °F — same σ the backtest validated
MAX_SLIP = 0.05               # taker fill may cost ≤ 5¢ over the bid-implied price
# Max total notional per event DATE — the correlated-heat-beta guard. Set to
# $20 for the PAPER data-gathering phase (~7 trades/date): at $6 a date filled
# after only 2 trades and clustered weather dates froze the whole sleeve to
# ~2-4 trades/day, far too thin to ever test a +0.014/ct-floor edge. This is a
# PAPER cap — a real-money deployment would re-tighten it (≈$6, ~4% of a $143
# bankroll/date). Override per-run with `scan --day-cap N`.
DAY_RISK_CAP_USD = 20.0

# series → (lat, lon) of the settlement station, for the live forecast pull.
# First 12 come from fetch_backtest_data._cities(); the rest are the standard
# Kalshi settlement airports for the remaining KXHIGHT* cities.
_EXTRA_GEO = {
    "KXHIGHTBOS": (42.3656, -71.0096), "KXHIGHTDC": (38.8512, -77.0402),
    "KXHIGHTLV": (36.0840, -115.1537), "KXHIGHTMIN": (44.8848, -93.2223),
    "KXHIGHTNOLA": (29.9934, -90.2581), "KXHIGHTOKC": (35.3931, -97.6008),
    "KXHIGHTSATX": (29.5337, -98.4698), "KXHIGHTSFO": (37.6213, -122.3790),
}


def series_geo() -> dict:
    """All live daily-high series → (lat, lon)."""
    from fetch_backtest_data import _cities
    geo = {}
    for c in _cities().values():
        s = c.get("series")
        if s and c.get("lat") is not None:
            geo[s] = (c["lat"], c["lon"])
    geo.update(_EXTRA_GEO)
    return geo


def live_forecast_highs(series_list) -> dict:
    """(series, iso_date) → forecast daily-high °F from the live Open-Meteo
    forecast (the same source/model family the backtest validated)."""
    import requests
    geo = series_geo()
    out = {}
    for s in series_list:
        ll = geo.get(s)
        if not ll:
            continue
        try:
            resp = requests.get(
                "https://api.open-meteo.com/v1/forecast",
                params={"latitude": ll[0], "longitude": ll[1],
                        "hourly": "temperature_2m", "forecast_days": 4,
                        "temperature_unit": "fahrenheit", "timezone": "auto"},
                timeout=20).json()
            h = resp.get("hourly", {})
            by_day: dict = {}
            for t, v in zip(h.get("time", []), h.get("temperature_2m", [])):
                if v is not None:
                    by_day.setdefault(str(t)[:10], []).append(float(v))
            for day, vals in by_day.items():
                out[(s, day)] = round(max(vals), 1)
        except Exception as e:
            print(f"  ! forecast {s}: {e}", file=sys.stderr)
    return out


# ── Decision (pure — fully testable) ────────────────────────────────────────
def two_sided_decision(quote: dict, p_fc: float, *, thr: float = DEFAULT_THR,
                       bankroll: float = DEFAULT_BANKROLL,
                       risk_pct: float = DEFAULT_RISK_PCT,
                       max_trade_usd: float = DEFAULT_MAX_TRADE_USD,
                       max_slip: float = MAX_SLIP) -> dict | None:
    """One market + the forecast's P(YES) → a YES or NO paper order, or None.
    quote needs: ticker, yes_price (0-1), yes_bid_c, no_bid_c (0-1 bids)."""
    yp = quote.get("yes_price")
    if not isinstance(yp, (int, float)) or not (FILL_FLOOR <= yp <= FILL_CEIL):
        return None
    gap = p_fc - float(yp)
    if abs(gap) < thr:
        return None
    side = "YES" if gap > 0 else "NO"
    # Taker fill: cross the spread to the other side's bid.
    yes_bid, no_bid = quote.get("yes_bid_c"), quote.get("no_bid_c")
    if side == "NO":
        if not isinstance(yes_bid, (int, float)):
            return None                      # no YES bid → can't take the NO side
        fill = 1.0 - float(yes_bid)
        ref = 1.0 - float(yp)                # signal-implied NO price
    else:
        if not isinstance(no_bid, (int, float)):
            return None                      # no NO bid → can't take the YES side
        fill = 1.0 - float(no_bid)
        ref = float(yp)
    if not (0.01 < fill < 0.99):
        return None
    if fill - ref > max_slip:                # spread too wide to cross honestly
        return None
    notional = min(max_trade_usd, bankroll * risk_pct)
    # WHOLE contracts only — a real Kalshi order can't fill 12.4 contracts.
    size = float(int(notional / fill))
    if size < 1:
        return None
    fee = kalshi_fee(size, fill)
    # EV per contract vs the MARKET price (the forecast is rank-only — never
    # priced): what we give up to the spread + fee. The edge itself is only
    # measurable in the scorecard.
    cost_ct = round((fill - ref) + (fee / size if size else 0.0), 4)
    return {
        "ticker": quote.get("ticker"),
        "event_ticker": quote.get("event_ticker", ""),
        "side": side,
        "yes_price": round(float(yp), 4),
        "p_forecast": round(float(p_fc), 4),
        "gap": round(gap, 4),
        "fill_price": round(fill, 4),
        "our_size": size,
        "notional": round(size * fill, 2),
        "fee": round(fee, 2),
        "entry_cost_per_ct": cost_ct,
        "hours_to_close": quote.get("hours_to_close"),
    }


def _iso_event_date(ticker: str) -> str:
    return parse_event_date("", ticker) or "?"


def _load_ledger() -> list[dict]:
    rows = []
    if LEDGER.exists():
        with open(LEDGER) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    return rows


def _city_local_date(series: str, now_utc: datetime, geo: dict) -> str:
    """Approximate local calendar date at the series' settlement station, from
    its longitude (15°/hour). Off by ≤1h vs civil DST time — irrelevant for a
    day-boundary check anchored at local afternoon."""
    ll = geo.get(series)
    offset_h = (ll[1] / 15.0) if ll else -6.0     # default: US central-ish
    from datetime import timedelta
    return (now_utc + timedelta(hours=offset_h)).date().isoformat()


def cmd_scan(args) -> None:
    try:
        quotes = _open_weather_quotes(WEATHER_SERIES)
    except Exception as e:
        print(f"! live scan failed ({e}) — run on home IP with Kalshi auth", file=sys.stderr)
        return
    fc = live_forecast_highs(sorted({(q.get("ticker") or "").split("-")[0]
                                     for q in quotes if q.get("ticker")}))
    ledger = _load_ledger()
    seen = {r.get("ticker") for r in ledger}            # one entry per market, ever
    # Per-event-date notional already at risk (open + settled today both count
    # toward the day's exposure decision at entry time; only open rows still
    # carry risk, but counting all booked-today keeps the cap conservative).
    day_risk: dict = {}
    for r in ledger:
        if r.get("status") == "open":
            d = _iso_event_date(r.get("ticker", ""))
            day_risk[d] = day_risk.get(d, 0.0) + float(r.get("notional") or 0.0)

    booked, skipped_cap, no_fc, skipped_same_day = [], 0, 0, 0
    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.isoformat()
    geo = series_geo()
    # Pass 1: decide every market (no booking yet).
    candidates = []
    for q in quotes:
        tk = q.get("ticker") or ""
        if tk in seen:
            continue
        series = tk.split("-")[0]
        date = _iso_event_date(tk)
        # DAY-AHEAD ONLY: if the event day has already begun at the city, its
        # high may already be realized — the market knows, our day-ahead
        # forecast doesn't (the backtest edge was at the OPEN; entry-realism
        # showed it gone near close). Fading informed same-day prices is pure
        # adverse selection, so skip them regardless of hours-to-close.
        if date <= _city_local_date(series, now_utc, geo):
            skipped_same_day += 1
            continue
        kind, strike = parse_strike2(tk)
        high = fc.get((series, date))
        if high is None or strike is None:
            no_fc += 1
            continue
        p_fc = forecast_p_yes(kind, strike, high, SIGMA_F)
        d = two_sided_decision(q, p_fc, thr=args.thr, bankroll=args.bankroll)
        if d is not None:
            candidates.append((date, high, strike, kind, d))
    # Pass 2: book BIGGEST |gap| first, so when the day-cap binds it keeps the
    # strongest forecast-vs-market disagreements instead of whatever the API
    # happened to list first. (Within a date the trades are correlated anyway —
    # if we can only take ~7, take the 7 best.)
    candidates.sort(key=lambda c: -abs(c[4]["gap"]))
    for date, high, strike, kind, d in candidates:
        if day_risk.get(date, 0.0) + d["notional"] > args.day_cap:
            skipped_cap += 1
            continue
        day_risk[date] = day_risk.get(date, 0.0) + d["notional"]
        d.update({"forecast_high_f": high, "strike_f": strike, "strike_kind": kind,
                  "status": "open", "opened_at": now_iso, "result": "",
                  "resolved_at": "", "paper_pnl": 0.0, "is_live": False})
        booked.append(d)
        seen.add(d["ticker"])

    if booked:
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with open(LEDGER, "a") as f:
            for r in booked:
                f.write(json.dumps(r) + "\n")
    SCAN_STATUS.parent.mkdir(parents=True, exist_ok=True)
    SCAN_STATUS.write_text(json.dumps({
        "last_scan": now_iso, "markets_seen": len(quotes), "booked": len(booked),
        "skipped_day_cap": skipped_cap, "no_forecast_or_strike": no_fc,
        "skipped_same_day": skipped_same_day,
        "thr": args.thr, "bankroll": args.bankroll, "day_cap": args.day_cap}))
    print(f"fc2s scan: {len(quotes)} open markets, booked {len(booked)} "
          f"({sum(1 for b in booked if b['side']=='YES')} YES / "
          f"{sum(1 for b in booked if b['side']=='NO')} NO), "
          f"{skipped_same_day} same-day (event already underway — adverse selection), "
          f"{skipped_cap} skipped by day-cap, {no_fc} no forecast/strike")
    if getattr(args, "show", False) and booked:
        for b in booked:
            print(f"  {b['side']:>3} {b['ticker']:<26} yes={b['yes_price']:.2f} "
                  f"p_fc={b['p_forecast']:.2f} gap={b['gap']:+.2f} "
                  f"fill={b['fill_price']:.2f} x{b['our_size']}")


def cmd_settle(args) -> None:
    from fetch_backtest_data import _kalshi_get
    rows = _load_ledger()
    if not rows:
        print("no fc2s paper trades yet.")
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
        won = (res == str(r.get("side", "")).lower())
        fill = float(r["fill_price"]); size = float(r["our_size"])
        fee = float(r.get("fee") or kalshi_fee(size, fill))
        gross = (size * (1.0 - fill)) if won else (-size * fill)
        r["paper_pnl"] = round(gross - fee, 2)
        r["status"] = "won" if won else "lost"
        r["result"] = res
        r["resolved_at"] = datetime.now(timezone.utc).isoformat()
        pnl_now += r["paper_pnl"]
        changed += 1
    if changed:
        with open(LEDGER, "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
    print(f"fc2s settled {changed} trades, P&L this run ${pnl_now:+.2f}")


def cmd_report(args) -> None:
    rows = _load_ledger()
    if not rows:
        print("no fc2s paper trades yet — the scan agent will book as books go live.")
        return
    settled = [r for r in rows if r.get("status") in ("won", "lost")]
    open_ = [r for r in rows if r.get("status") == "open"]
    net = sum(float(r.get("paper_pnl") or 0) for r in settled)
    w = sum(1 for r in settled if r["status"] == "won")
    print(f"=== fc2s (forecast two-sided) paper scorecard ===")
    print(f"settled {len(settled)} ({w}W/{len(settled)-w}L), net ${net:+.2f} after fees; "
          f"{len(open_)} open (${sum(float(r.get('notional') or 0) for r in open_):.2f} at risk)")
    # Quarantine pre-fix SAME-DAY entries (event day already underway at the
    # city when opened — adverse selection, not the strategy). The rule is what
    # the day-ahead cohort says; the same-day cohort just shows the damage.
    geo = series_geo()
    def _same_day(r):
        try:
            opened = datetime.fromisoformat(str(r.get("opened_at", "")).replace("Z", "+00:00"))
        except ValueError:
            return False
        tk = r.get("ticker", "")
        return _iso_event_date(tk) <= _city_local_date(tk.split("-")[0], opened, geo)
    tainted = [r for r in settled if _same_day(r)]
    if tainted:
        tnet = sum(float(r.get("paper_pnl") or 0) for r in tainted)
        clean = [r for r in settled if not _same_day(r)]
        cnet = sum(float(r.get("paper_pnl") or 0) for r in clean)
        cw = sum(1 for r in clean if r["status"] == "won")
        print(f"  !! cohorts: DAY-AHEAD (the strategy) {len(clean)} settled "
              f"({cw}W/{len(clean)-cw}L) net ${cnet:+.2f}  ·  "
              f"SAME-DAY (pre-fix adverse selection) {len(tainted)} settled net ${tnet:+.2f}")
    for side in ("YES", "NO"):
        ss = [r for r in settled if r.get("side") == side]
        if ss:
            sw = sum(1 for r in ss if r["status"] == "won")
            snet = sum(float(r.get("paper_pnl") or 0) for r in ss)
            print(f"  {side:>3}: {len(ss):>3} settled ({sw}W/{len(ss)-sw}L)  net ${snet:+.2f}")
    # Per-day panel — the unit of independence. The backtest's promise is GREEN
    # DAYS on both hot and cool patterns; judge this sleeve the same way.
    by_day: dict = {}
    for r in settled:
        d = _iso_event_date(r.get("ticker", ""))
        b = by_day.setdefault(d, {"net": 0.0, "n": 0, "y": 0})
        b["net"] += float(r.get("paper_pnl") or 0)
        b["n"] += 1
        b["y"] += 1 if str(r.get("result")) == "yes" else 0
    if by_day:
        green = sum(1 for b in by_day.values() if b["net"] > 0)
        print(f"\nper-day ({green}/{len(by_day)} green):")
        print(f"  {'date':>10} {'trades':>7} {'YESrate':>8} {'net$':>8}")
        for d in sorted(by_day):
            b = by_day[d]
            print(f"  {d:>10} {b['n']:>7} {b['y']/b['n']*100:>7.0f}% {b['net']:>+8.2f}")
    # Per-city
    by_city: dict = {}
    for r in settled:
        c = _city_of(r.get("ticker", ""))
        b = by_city.setdefault(c, {"net": 0.0, "n": 0, "w": 0})
        b["net"] += float(r.get("paper_pnl") or 0)
        b["n"] += 1
        b["w"] += 1 if r["status"] == "won" else 0
    if by_city:
        print("\nper-city:")
        for c in sorted(by_city, key=lambda c: -by_city[c]["net"]):
            b = by_city[c]
            print(f"  {c:>6}: {b['n']:>3} trades {b['w']:>3}W  net ${b['net']:+.2f}")


def cmd_status(args) -> None:
    """Is this sleeve actually scanning + booking? One-glance liveness check:
    last scan age, what it saw/booked, ledger growth, and whether right now is
    even a live window (weather books are dead ~04–13 UTC)."""
    now = datetime.now(timezone.utc)
    tick = lambda b: "OK " if b else "!! "
    issues = []

    st = {}
    if SCAN_STATUS.exists():
        try:
            st = json.loads(SCAN_STATUS.read_text())
        except Exception:
            st = {}
    age = None
    if st.get("last_scan"):
        try:
            age = (now - datetime.fromisoformat(
                st["last_scan"].replace("Z", "+00:00"))).total_seconds() / 60
        except Exception:
            age = None
    dead = 4 <= now.hour <= 13   # mapped overnight dead window (no open markets)
    if age is None:
        print(f"[!! ] fc2s scan has NEVER run (no {SCAN_STATUS.name}). "
              f"Install the agents + wait for :20 past the hour in a live window.")
        issues.append("scan never ran")
    elif age <= 75:
        print(f"[OK ] last fc2s scan {age:.0f} min ago — saw {st.get('markets_seen',0)} "
              f"markets, booked {st.get('booked',0)} "
              f"(skipped {st.get('skipped_day_cap',0)} day-cap, "
              f"{st.get('no_forecast_or_strike',0)} no-forecast)")
    elif dead:
        print(f"[OK ] last fc2s scan {age:.0f} min ago — normal: overnight DEAD window "
              f"(04–13 UTC), no markets to scan. Resumes ~14 UTC.")
    else:
        print(f"[!! ] last fc2s scan {age:.0f} min ago — STALE in a live window "
              f"(Mac asleep / agent not firing?).")
        issues.append("scan stale")

    rows = _load_ledger()
    settled = [r for r in rows if r.get("status") in ("won", "lost")]
    openn = [r for r in rows if r.get("status") == "open"]
    ndays = len({_iso_event_date(r.get("ticker", "")) for r in settled})
    print(f"[{tick(bool(rows))}] ledger: {len(openn)} open · {len(settled)} settled "
          f"across {ndays} distinct days "
          f"(${sum(float(r.get('notional') or 0) for r in openn):.2f} at risk)")
    if not rows:
        if dead:
            print("      (empty is expected right now — dead window. Check again ~14–03 UTC.)")
        else:
            print("      no trades booked yet. Force one now:  "
                  "python scripts/fc_two_sided.py scan --show")

    booked = st.get("booked", 0)
    if age is not None and not dead and st.get("markets_seen", 0) and not booked and not openn:
        why = []
        if st.get("no_forecast_or_strike"): why.append(f"{st['no_forecast_or_strike']} no forecast/strike")
        if st.get("skipped_day_cap"): why.append(f"{st['skipped_day_cap']} hit day-cap")
        print("[!! ] saw markets but booked 0" + (": " + ", ".join(why) if why else
              " — likely all inside the gate or spread too wide (MAX_SLIP)."))
        issues.append("scanning but not booking")

    print("==> fc2s " + ("LIVE & BOOKING." if not issues else
                          "needs attention: " + "; ".join(issues)))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("scan", help="book two-sided paper trades at the live book")
    p.add_argument("--thr", type=float, default=DEFAULT_THR)
    p.add_argument("--bankroll", type=float, default=DEFAULT_BANKROLL)
    p.add_argument("--day-cap", type=float, default=DAY_RISK_CAP_USD, dest="day_cap",
                   help="max total notional per event DATE (paper default $20; "
                        "real-money would use ~$6)")
    p.add_argument("--show", action="store_true")
    p.set_defaults(fn=cmd_scan)
    p = sub.add_parser("settle", help="resolve booked trades → scorecard")
    p.set_defaults(fn=cmd_settle)
    p = sub.add_parser("report", help="scorecard: totals, per-side, per-day, per-city")
    p.set_defaults(fn=cmd_report)
    p = sub.add_parser("status", help="liveness check: is it scanning + booking right now?")
    p.set_defaults(fn=cmd_status)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
