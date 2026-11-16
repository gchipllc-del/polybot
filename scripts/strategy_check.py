#!/usr/bin/env python3
"""strategy_check - test ONE named strategy on real Kalshi history, honestly.

Built to answer "is this a proven method?" for a strategy someone else designed. Because
the rule is fixed in advance (not found by searching our data), it is ONE pre-specified
hypothesis: no search correction is owed, and the test is as powerful as it can be.

It replays the rule on settled markets from a backfill file and reports what a trader
running it would actually have lived through:
  * win rate against the BREAK-EVEN win rate after Kalshi's fee (charged per ORDER,
    ceil to the cent - which is why order size changes the math on cheap profits)
  * P&L per order, per event, total; a 95% interval and a one-sided p-value judged
    against the variance a fair price would produce (a favorite that has not lost yet
    is not proof - the house rule from sleeve_scan)
  * the worst days, the max drawdown, the longest winning streak, and how many wins one
    loss erases - the experience of a 97-99c strategy is long calm runs, then a cliff
  * the same numbers on TRADE-CONFIRMED entries only (a trade printed at or through the
    price in that candle): a quote nobody could fill is not a fill
  * by series, and by hours before close

  py scripts/strategy_check.py favorites --lo 0.97 --hi 0.99 --contracts 10 \\
      --file data/backfill_weather.jsonl
  py scripts/strategy_check.py favorites --lo 0.97 --hi 0.99 --max-hours 1 \\
      --one-per-event --file data/sleeves/final          # a folder, or a,b,c files
  py scripts/strategy_check.py selftest

Entry rule ("favorites"): the FIRST moment a side's ask sits in [lo, hi] (inclusive), on a
formed book, buy `contracts` of that side at that ask; hold to settlement. One entry per
market, always strictly BEFORE the close (the bar ending at the close quotes the answer).
Optional --min-hours / --max-hours restrict entries by time before close.
--one-per-event keeps only each event's FIRST entry - exactly what the forward paper
trader does - and makes the p-values exact (sleeve_paper.forward_stats). Last-hour tests
need 1-minute candles: `sleeve_scan.py backfill --final-hours 2` (it warns otherwise). It
also warns when the data starts after the test window opens (2 hours of final/ candles
tested over 3, or over the whole life): an unseen first touch leaves only survivors.
Streams the files: a 3M-candle backfill never has to fit in memory.
"""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from forecast_replay import mean_ci, norm_cdf  # noqa: E402
from sleeve_scan import _num, _ohlc, candle_volume, event_of  # noqa: E402
import history_backfill as hb  # noqa: E402

MAX_SPREAD = 0.15
HOUR_BUCKETS = ((0, 2), (2, 6), (6, 12), (12, 24), (24, 1e9))


def order_fee(price: float, contracts: int) -> float:
    """Kalshi taker fee for ONE order: ceil(0.07 * C * P * (1-P)) to the cent."""
    if not (0 < price < 1) or contracts <= 0:
        return 0.0
    return math.ceil(0.07 * contracts * price * (1.0 - price) * 100 - 1e-9) / 100.0


def scan(path: Path, lo: float, hi: float, min_h: float = 0.0, max_h: float = 1e9) -> dict:
    """Stream a history_backfill-format file -> {"markets": {...}, "entries": {...}}.
    Keeps one market record and at most one (earliest) entry per ticker.
    starts: ticker -> (where its data begins, whether a --final-hours fetch window is
    recorded for it) - see late_data()."""
    markets: dict[str, dict] = {}
    entries: dict[str, dict] = {}
    orphans = 0
    last: dict[str, float] = {}
    first: dict[str, float] = {}        # each ticker's earliest candle
    step: dict[str, float] = {}         # and its candle spacing (the smallest gap seen)
    gaps = [0, 0]                       # [under an hour, an hour or more] between candles
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = d.get("t")
            if t == "market":
                if d.get("result") not in ("yes", "no"):
                    continue
                fh = max(_num(d.get("final_hours")) or 0.0,          # a re-fetch adds a row
                         (markets.get(d["ticker"]) or {}).get("final_hours") or 0.0)
                markets[d["ticker"]] = {
                    "ticker": d["ticker"], "series": d.get("series"), "event": event_of(d),
                    "result": d["result"], "close_ts": hb._iso_to_ts(d.get("close_time")),
                    "open_ts": hb._iso_to_ts(d.get("open_time")), "final_hours": fh or None}
            elif t == "candle":
                tk = d.get("ticker")
                m = markets.get(tk)
                if m is None:
                    orphans += 1
                    continue
                ts = d.get("end_period_ts")
                try:
                    ts = float(ts)
                except (TypeError, ValueError):
                    continue
                if tk in last and ts > last[tk]:
                    gaps[ts - last[tk] >= 3600] += 1
                    step[tk] = min(step.get(tk, math.inf), ts - last[tk])
                last[tk] = ts
                first[tk] = min(first.get(tk, ts), ts)
                if tk in entries and entries[tk]["ts"] <= ts:
                    continue                       # first touch already recorded
                yb, ya = _ohlc(d, "yes_bid", "close"), _ohlc(d, "yes_ask", "close")
                if yb is None or ya is None or not (0.01 <= yb < ya <= 0.99
                                                    and ya - yb <= MAX_SPREAD):
                    continue
                hours = ((m["close_ts"] - ts) / 3600.0) if m["close_ts"] else None
                # never at or after the close: the bar ending AT the close quotes the
                # settled price, and the forward trader only buys while now < close
                if hours is not None and (hours <= 0 or not (min_h <= hours <= max_h)):
                    continue
                na = round(1.0 - yb, 4)
                side = ask = None
                if lo - 1e-9 <= ya <= hi + 1e-9:
                    side, ask = "yes", ya
                elif lo - 1e-9 <= na <= hi + 1e-9:
                    side, ask = "no", na
                if side is None:
                    continue
                vol = candle_volume(d)
                t_lo, t_hi = _ohlc(d, "price", "low"), _ohlc(d, "price", "high")
                traded = bool(vol) and (
                    (side == "yes" and t_lo is not None and t_lo <= ya + 1e-9) or
                    (side == "no" and t_hi is not None and t_hi >= yb - 1e-9))
                entries[tk] = {"ts": ts, "side": side, "ask": ask, "hours": hours,
                               "trade_ok": traded}
    starts = {}
    for tk, m in markets.items():
        c, o, fh = m["close_ts"], m["open_ts"], m["final_hours"]
        rec = None
        if fh and c is not None:                   # where the recorded fetch began
            rec = c - fh * 3600.0 if o is None else max(o, c - fh * 3600.0)
        got = [x for x in (rec, first[tk] - step[tk] if tk in step else None)
               if x is not None]                   # ... or the first candle's own period
        if got:
            starts[tk] = (min(got), rec is not None)
    return {"markets": markets, "entries": entries, "orphans": orphans, "gaps": gaps,
            "starts": starts}


def late_data(res: dict, max_h: float = 1e9) -> dict[str, float]:
    """ticker -> hours by which its data starts AFTER the test window opens (the window is
    the last max_h hours before close, or the market's whole life). A first touch before
    the data starts is invisible, so a short history only sees the markets still in range
    later - survivors. Data starts where a recorded --final-hours fetch began, or one
    candle step before the first candle, whichever is earlier. A whole-life test checks
    only markets with a recorded fetch window: in candles alone, a book that formed late
    looks the same as a history cut short."""
    out = {}
    for tk, (start, recorded) in res.get("starts", {}).items():
        m = res["markets"].get(tk)
        if m is None or m["close_ts"] is None:
            continue
        o, want = m.get("open_ts"), m["close_ts"] - max_h * 3600.0
        if o is None and max_h >= 1e8:
            continue
        if o is not None and want <= o:            # the window is the whole life
            if not recorded:
                continue
            want = o
        if start - want > 1.0:
            out[tk] = (start - want) / 3600.0
    return out


def expand_files(spec: str) -> list[Path]:
    """'a.jsonl,b.jsonl' or a folder (every *.jsonl in it) -> files, in order."""
    out: list[Path] = []
    for part in (x.strip() for x in spec.split(",")):
        if not part:
            continue
        q = Path(part)
        out += sorted(q.glob("*.jsonl")) if q.is_dir() else [q]
    return out


def scan_many(paths: list[Path], lo: float, hi: float, min_h: float = 0.0,
              max_h: float = 1e9) -> dict:
    """scan() over several files (one per series), merged. Each file holds its own market
    rows before their candles. A ticker found in several files (`data/sleeves` hourly AND
    `data/sleeves/final` 1-minute) keeps its EARLIEST entry - the first touch the forward
    trader takes - whatever order the files are read in.
    hourly_files: the files whose OWN candles are mostly an hour or more apart - voted per
    file, because one dense 1-minute file would outvote any number of hourly ones."""
    res = {"markets": {}, "entries": {}, "orphans": 0, "gaps": [0, 0], "hourly_files": [],
           "starts": {}}
    for path in paths:
        r = scan(path, lo, hi, min_h, max_h)
        res["markets"].update(r["markets"])
        for tk, e in r["entries"].items():
            if tk not in res["entries"] or e["ts"] < res["entries"][tk]["ts"]:
                res["entries"][tk] = e
        for tk, (s, rec) in r["starts"].items():    # the data begins at its earliest file
            s0, rec0 = res["starts"].get(tk, (s, rec))
            res["starts"][tk] = (min(s0, s), rec0 or rec)
        res["orphans"] += r["orphans"]
        res["gaps"] = [a + b for a, b in zip(res["gaps"], r["gaps"])]
        if r["gaps"][1] > r["gaps"][0]:
            res["hourly_files"].append(path)
    return res


def first_per_event(trades: list[dict]) -> list[dict]:
    """Each event's FIRST entry only - the forward paper trader's one bet per event."""
    seen: set = set()
    out = []
    for t in sorted(trades, key=lambda x: (x["ts"], x["ticker"])):
        if t["event"] not in seen:
            seen.add(t["event"])
            out.append(t)
    return out


def trades_from(scan_res: dict, contracts: int) -> list[dict]:
    out = []
    for tk, e in scan_res["entries"].items():
        m = scan_res["markets"][tk]
        won = m["result"] == e["side"]
        fee = order_fee(e["ask"], contracts)
        pnl = (contracts * (1.0 - e["ask"]) if won else -contracts * e["ask"]) - fee
        out.append({"ticker": tk, "series": m["series"], "event": m["event"],
                    "side": e["side"], "ask": e["ask"], "won": won, "fee": fee,
                    "pnl": round(pnl, 4), "ts": e["ts"], "hours": e["hours"],
                    "trade_ok": e["trade_ok"], "contracts": contracts})
    out.sort(key=lambda r: r["ts"])
    return out


def summarize(trades: list[dict], contracts: int) -> dict:
    """The numbers a trader running this would live through."""
    if not trades:
        return {"n": 0}
    n = len(trades)
    wins = sum(t["won"] for t in trades)
    avg_ask = sum(t["ask"] for t in trades) / n
    avg_fee_c = sum(t["fee"] for t in trades) / n / contracts
    breakeven = avg_ask + avg_fee_c
    # per EVENT, per CONTRACT: every bucket of one city-day settles on one temperature
    ev: dict[str, list] = {}
    for t in trades:
        ev.setdefault(t["event"], []).append(t)
    per_ev = [sum(x["pnl"] for x in v) / (len(v) * contracts) for v in ev.values()]
    m, lo, hi = mean_ci(per_ev)
    # null variance: each contract bought at A wins with prob A + fee if there is no edge
    null_var = sum(sum((min(.999, x["ask"] + x["fee"] / contracts)) *
                       (1 - min(.999, x["ask"] + x["fee"] / contracts)) for x in v) / len(v)
                   for v in ev.values()) / len(ev)
    se = max((hi - m) / 1.96 if len(per_ev) > 1 else 0.0, math.sqrt(null_var / len(ev)))
    p = 1.0 - norm_cdf(m / se) if se > 0 else (0.0 if m > 0 else 1.0)
    p_neg, exact = None, all(len(v) == 1 for v in ev.values())
    if exact:
        # one bet per event: the record is decided by the number of losses - exact tails
        from sleeve_paper import forward_stats
        st = forward_stats([{"event": t["event"], "won": t["won"], "ask": t["ask"],
                             "fee_pc": t["fee"] / contracts, "pnl": t["pnl"],
                             "pnl_pc": t["pnl"] / contracts} for t in trades])
        p, p_neg = st["p"], st["p_neg"]
    cum = peak = dd = 0.0
    streak = best = 0
    for t in trades:
        cum += t["pnl"]
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
        streak = streak + 1 if t["won"] else 0
        best = max(best, streak)
    win_gain = sum(t["pnl"] for t in trades if t["won"]) / max(1, wins)
    losses = [t for t in trades if not t["won"]]
    loss_cost = -sum(t["pnl"] for t in losses) / max(1, len(losses)) if losses else None
    return {"n": n, "events": len(ev), "wins": wins, "wr": wins / n, "breakeven": breakeven,
            "total": sum(t["pnl"] for t in trades), "per_order": sum(t["pnl"] for t in trades) / n,
            "per_contract_event": m, "lo": lo, "hi": hi, "p": p, "p_neg": p_neg,
            "exact": exact, "max_dd": dd,
            "best_streak": best, "losses": len(losses),
            "wins_per_loss": (loss_cost / win_gain) if loss_cost and win_gain > 0 else None,
            "needed_events": (math.ceil((2.5 * math.sqrt(null_var) / m) ** 2)
                              if m > 0 else None)}


def verdict(s: dict) -> str:
    if not s.get("n"):
        return "NO DATA - no market ever traded in this price range here."
    if s["per_contract_event"] > 0 and s["p"] < 0.05:
        return ("POSITIVE AND SIGNIFICANT on history - worth a forward paper test, which is "
                "the only thing that can make it 'proven'.")
    if s["per_contract_event"] > 0:
        return (f"POSITIVE BUT NOT PROVEN - the sample cannot tell this from luck "
                f"(p={s['p']:.2f}). At this edge it needs ~{s['needed_events']} independent "
                f"events to say.")
    if (s["p_neg"] < 0.025) if s.get("p_neg") is not None else (s["hi"] < 0):
        return "NEGATIVE AND SIGNIFICANT - on this history the method loses money after fees."
    return "NEGATIVE - no evidence it makes money after fees."


def cmd_favorites(paths: list[Path], lo: float, hi: float, contracts: int,
                  min_h: float, max_h: float, one_per_event: bool = False) -> int:
    print("=" * 78)
    print(f"STRATEGY CHECK - buy {contracts} of any side priced {lo:.2f}-{hi:.2f}, hold to settlement")
    win = (f"; only {min_h:g}-{max_h:g}h before close" if max_h < 1e8 or min_h > 0 else "")
    print(f"{'ONE bet per event (its first entry)' if one_per_event else 'every market'}{win}")
    print(f"data: {', '.join(str(x) for x in paths[:4])}"
          f"{f' +{len(paths) - 4} more' if len(paths) > 4 else ''}")
    print("=" * 78)
    missing = [x for x in paths if not x.exists()]
    if not paths or missing:
        print(f"file not found: {', '.join(str(x) for x in missing) or '(none given)'}")
        return 1
    res = scan_many(paths, lo, hi, min_h, max_h)
    hourly = res["hourly_files"]
    if max_h <= 3 and hourly:
        print(f"WARNING: HOURLY candles in {len(hourly)} of {len(paths)} file(s): "
              f"{', '.join(str(x) for x in hourly[:6])}"
              f"{f' +{len(hourly) - 6} more' if len(hourly) > 6 else ''}")
        print("A last-hours test needs 1-minute candles - an hourly bar ends at the close,")
        print("so its closing quote is the settled price.")
        print("Run: py scripts/sleeve_scan.py backfill --series ... --final-hours 2")
        print("and point --file at data/sleeves/final.")
    late = late_data(res, max_h)
    if late:
        print(f"WARNING: for {len(late)} of {len(res['markets'])} markets the data starts AFTER "
              "the test window opens")
        print(f"(by up to {max(late.values()):.2f}h). A first touch before the data starts is "
              "invisible, so the")
        print("test only sees the markets still in range later - survivors. Narrow --max-hours")
        print("to the data, or re-fetch: py scripts/sleeve_scan.py backfill ... --final-hours H")
    trades = trades_from(res, contracts)
    if one_per_event:
        trades = first_per_event(trades)
    print(f"{len(res['markets'])} settled markets read; {len(trades)} entered "
          f"({sum(t['side'] == 'no' for t in trades)} NO, "
          f"{sum(t['side'] == 'yes' for t in trades)} YES)")
    for label, ts in (("ALL ENTRIES (quote-based)", trades),
                      ("TRADE-CONFIRMED ONLY (a trade printed at our price)",
                       [t for t in trades if t["trade_ok"]])):
        s = summarize(ts, contracts)
        print()
        print(f"-- {label}")
        if not s.get("n"):
            print("   none")
            continue
        print(f"   orders {s['n']}  |  independent events {s['events']}  |  "
              f"losses {s['losses']}")
        print(f"   win rate {s['wr']:.2%}   vs   break-even {s['breakeven']:.2%} "
              f"(price + fee)")
        print(f"   P&L total ${s['total']:+.2f}   per order ${s['per_order']:+.3f}   "
              f"per contract per event {s['per_contract_event'] * 100:+.2f}c "
              f"[{s['lo'] * 100:+.2f}c, {s['hi'] * 100:+.2f}c]   "
              f"p={s['p']:.3f}{' (exact)' if s['exact'] else ''}")
        wpl = f"{s['wins_per_loss']:.0f}" if s["wins_per_loss"] else "-"
        print(f"   max drawdown ${s['max_dd']:.2f}   longest win streak {s['best_streak']}   "
              f"one loss erases ~{wpl} wins")
        print(f"   VERDICT: {verdict(s)}")
    if trades:
        print()
        print("-- WORST EVENTS (what the cliff looks like)")
        ev: dict[str, list] = {}
        for t in trades:
            ev.setdefault(t["event"], []).append(t)
        worst = sorted(ev.items(), key=lambda kv: sum(x["pnl"] for x in kv[1]))[:5]
        for e, ts in worst:
            pnl = sum(x["pnl"] for x in ts)
            if pnl >= 0:
                break
            lost = [x for x in ts if not x["won"]]
            day = datetime.fromtimestamp(ts[0]["ts"], tz=timezone.utc).date()
            print(f"   {e:28} {day}  ${pnl:+.2f}  ({len(lost)} of {len(ts)} lost; e.g. "
                  f"{lost[0]['side'].upper()} @ {lost[0]['ask']:.2f}, "
                  f"{(lost[0]['hours'] or 0):.1f}h before close)" if lost else "")
        print()
        print("-- BY SERIES")
        by: dict[str, list] = {}
        for t in trades:
            by.setdefault(t["series"], []).append(t)
        for series, ts in sorted(by.items()):
            s = summarize(ts, contracts)
            print(f"   {series:14} orders {s['n']:>5}  win {s['wr']:.2%} vs need "
                  f"{s['breakeven']:.2%}  ${s['total']:+8.2f}")
        print()
        print("-- BY HOURS BEFORE CLOSE AT ENTRY")
        for a, b in HOUR_BUCKETS:
            ts = [t for t in trades if t["hours"] is not None and a <= t["hours"] < b]
            if ts:
                s = summarize(ts, contracts)
                lab = f"{a}-{b}h" if b < 1e8 else f">{a}h"
                print(f"   {lab:8} orders {s['n']:>5}  win {s['wr']:.2%} vs need "
                      f"{s['breakeven']:.2%}  ${s['total']:+8.2f}")
    print()
    print("History can REJECT a method or NOMINATE it. Only a forward record on prices you")
    print("could actually fill can call it proven - and live money waits for that.")
    print("=" * 78)
    return 0


# ── selftest ──────────────────────────────────────────────────────────────────

def _world(p_win: float, n_days: int = 400, seed: int = 3) -> list[dict]:
    """Daily events, 4 far buckets each, NO side quoted at 98c; true NO win rate p_win."""
    import random
    rnd = random.Random(seed)
    rows, t0 = [], 1_780_000_000
    for d in range(n_days):
        o, c = t0 + d * 86400, t0 + d * 86400 + 20 * 3600
        bust = rnd.random() > p_win            # a forecast bust sinks the whole day
        for k in range(4):
            tk = f"KXT-D{d:04d}-B{k}"
            rows.append({"t": "market", "series": "KXT", "ticker": tk,
                         "event_ticker": f"KXT-D{d:04d}", "result": "yes" if bust else "no",
                         "open_time": datetime.fromtimestamp(o, tz=timezone.utc).isoformat(),
                         "close_time": datetime.fromtimestamp(c, tz=timezone.utc).isoformat()})
            for h in (2, 8, 14):
                rows.append({"t": "candle", "series": "KXT", "ticker": tk,
                             "end_period_ts": o + h * 3600,
                             "yes_bid": {"close": 2}, "yes_ask": {"close": 4},
                             "price": {"low": 2, "high": 3}, "volume": 5})
    return rows


def _selftest() -> int:
    import tempfile
    # per-ORDER fee rounding: 10 contracts at 98c pay 2c total, one contract pays 1c
    assert order_fee(0.98, 10) == 0.02 and order_fee(0.98, 1) == 0.01
    assert order_fee(0.97, 10) == 0.03 and order_fee(0.99, 10) == 0.01
    with tempfile.TemporaryDirectory() as td:
        res = {}
        for name, pw in (("good", 0.995), ("bad", 0.93), ("quiet_bad", 0.965)):
            p = Path(td) / f"{name}.jsonl"
            with open(p, "w", encoding="utf-8") as f:
                for r in _world(pw):
                    f.write(json.dumps(r) + "\n")
            sc = scan(p, 0.97, 0.99)
            tr = trades_from(sc, 10)
            assert len(tr) == 1600 and all(t["side"] == "no" and t["ask"] == 0.98 for t in tr)
            assert all(t["hours"] == 18.0 for t in tr)        # FIRST touch, not a later one
            res[name] = summarize(tr, 10)
        assert verdict(res["good"]).startswith("POSITIVE"), res["good"]
        assert verdict(res["bad"]).startswith("NEGATIVE AND SIGNIFICANT"), res["bad"]
        # THE lesson for 97-99c methods: a version that loses 1.7c per contract still
        # runs hundreds of wins in a row and cannot even be PROVEN negative in 400 days
        quiet = res["quiet_bad"]
        assert verdict(quiet) == "NEGATIVE - no evidence it makes money after fees.", quiet
        assert quiet["best_streak"] > 100 and quiet["per_contract_event"] < 0
        assert res["good"]["events"] == 400                   # 4 buckets/day = 1 event
        assert 40 <= res["good"]["wins_per_loss"] <= 55       # ~49 wins erased per loss
        # time window: entries only within 4h of close -> none in this world
        sc = scan(Path(td) / "good.jsonl", 0.97, 0.99, 0.0, 4.0)
        assert not sc["entries"]
    print("selftest OK")
    return 0


def main() -> int:
    a = sys.argv[1:]
    if not a or a[0] == "selftest":
        return _selftest() if a else (print(__doc__) or 1)
    if a[0] == "favorites":
        def opt(name, default, cast=float):
            return cast(a[a.index(name) + 1]) if name in a else default
        paths = expand_files(opt("--file", str(ROOT / "data" / "backfill_weather.jsonl"), str))
        return cmd_favorites(paths, opt("--lo", 0.97), opt("--hi", 0.99),
                             opt("--contracts", 10, int), opt("--min-hours", 0.0),
                             opt("--max-hours", 1e9), one_per_event="--one-per-event" in a)
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
