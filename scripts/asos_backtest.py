#!/usr/bin/env python3
"""asos_backtest — replay past ASOS locks with the FIXED high logic to see how we'd have done.

IEM is a historical archive, so for each settled lock we re-fetch the corrected
station-local-day obs, recompute the high (fixed day-window + fixed qc), re-run
lock_signal, and score the new decision against the ACTUAL settled outcome — which is
already in the ledger (ground truth). We don't need the official temperature; we just
check whether the corrected decision matches what really settled.

  python scripts/asos_backtest.py
  python scripts/asos_backtest.py --selftest

LIMIT (read this): it can only re-decide the markets we ALREADY locked under the old
logic — it can't recover markets the buggy logic skipped (no price/obs record for those).
So it's a LOWER BOUND on the fix's value: "on the locks we made, does the corrected high
avoid the losers and keep the winners?" P&L assumes fills at the recorded market price.
"""
import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from asos_tracker import (                                   # noqa: E402
    realized_high, lock_signal, lock_pnl, fetch_iem_day, STATIONS, LOG,
)


def _load_settled(path: Path) -> list:
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(r.get("status")) in ("won", "lost") and r.get("strike") is not None:
            rows.append(r)
    return rows


def replay(records: list, fetch_fn, tz_of, sleep: float = 0.0) -> dict:
    """Re-decide each settled lock with the corrected high. fetch_fn(station, tz, date)→obs;
    tz_of(series)→tz. OLD stats are scored on the SAME re-fetched set as FIXED (fair
    comparison), and fetch failures are bucketed by reason."""
    a = {"n": 0, "bad_date": 0, "fetch_error": 0, "empty_obs": 0,
         "scored": 0, "old_won": 0, "old_pnl": 0.0,
         "new_lock": 0, "new_won": 0, "new_pnl": 0.0,
         "skip": 0, "skip_old_loss": 0, "skip_old_win": 0,
         "flips": [], "per_station": {}}
    cache: dict = {}
    for r in records:
        a["n"] += 1
        try:
            d = date.fromisoformat(str(r.get("ts"))[:10])
        except ValueError:
            a["bad_date"] += 1
            continue
        series, station = r.get("series"), r.get("station")
        tz = tz_of(series)
        key = (station, tz, d)
        if key not in cache:
            try:
                cache[key] = fetch_fn(station, tz, d)
                if sleep:
                    time.sleep(sleep)
            except Exception:
                cache[key] = None
        obs = cache[key]
        if obs is None:
            a["fetch_error"] += 1
            continue
        rh = realized_high(obs)
        if not rh:
            a["empty_obs"] += 1
            continue

        # Scored: count OLD on the SAME set the FIXED decision is judged on.
        a["scored"] += 1
        old_won = r.get("status") == "won"
        a["old_won"] += 1 if old_won else 0
        a["old_pnl"] += float(r.get("paper_pnl") or 0)
        ps = a["per_station"].setdefault(station, {"old_n": 0, "old_won": 0,
                                                   "new_lock": 0, "new_won": 0, "skip": 0})
        ps["old_n"] += 1
        ps["old_won"] += 1 if old_won else 0

        actual = str(r.get("result")).lower()
        sig = lock_signal(r.get("kind"), float(r["strike"]), rh["high"],
                          r.get("market_yes"), qc_high=rh["qc_high"])
        if sig is None:                                       # corrected logic would NOT lock
            a["skip"] += 1
            ps["skip"] += 1
            a["skip_old_win" if old_won else "skip_old_loss"] += 1
            if old_won:
                a["flips"].append((d.isoformat(), series, "WIN→skip",
                                   r.get("realized_high"), rh["high"], r.get("strike")))
            continue
        side, _cert, _edge = sig
        won = side.lower() == actual
        a["new_lock"] += 1
        a["new_won"] += 1 if won else 0
        ps["new_lock"] += 1
        ps["new_won"] += 1 if won else 0
        a["new_pnl"] += float(lock_pnl(side, r.get("market_yes"), actual) or 0)
        if side != r.get("side") or won != old_won:
            tag = f"{'WIN' if old_won else 'LOSS'}→{side}{'win' if won else 'lose'}"
            a["flips"].append((d.isoformat(), series, tag,
                               r.get("realized_high"), rh["high"], r.get("strike")))
    return a


def _report(a: dict) -> None:
    print("=== asos_backtest — old vs FIXED decision on settled locks ===")
    fails = a["bad_date"] + a["fetch_error"] + a["empty_obs"]
    print(f"  {a['n']} settled locks · {a['scored']} re-fetched & scored · {fails} unusable "
          f"(no-date {a['bad_date']}, fetch-error {a['fetch_error']}, empty-obs {a['empty_obs']})")
    scored = a["scored"]
    if scored <= 0:
        print("  nothing scored — could not re-fetch obs (network? station ids?).")
        return
    owr = a["old_won"] / scored
    print(f"\n  OLD (as traded):    {a['old_won']}/{scored} = {owr:.0%} hit   P&L ${a['old_pnl']:+.2f}")
    if a["new_lock"]:
        nwr = a["new_won"] / a["new_lock"]
        print(f"  FIXED — still locks {a['new_lock']}: {a['new_won']}/{a['new_lock']} = {nwr:.0%} hit   "
              f"P&L ${a['new_pnl']:+.2f}")
    else:
        print("  FIXED — locks nothing on this set.")
    print(f"  FIXED — would SKIP {a['skip']}:  {a['skip_old_loss']} losers avoided · "
          f"{a['skip_old_win']} winners missed")
    print(f"  net P&L change from the fix: ${a['new_pnl'] - a['old_pnl']:+.2f}  "
          f"(skips contribute $0 — no trade)")

    # Per-station: surfaces which stations the fix rescued vs which still need work.
    ps = a["per_station"]
    if ps:
        print("\n  per-station (re-fetched locks):")
        print("   series-station   oldN  old%   keep  new%   skip")
        for st in sorted(ps, key=lambda s: (ps[s]["old_won"] / ps[s]["old_n"] if ps[s]["old_n"] else 1)):
            g = ps[st]
            o = f"{g['old_won']/g['old_n']*100:3.0f}%" if g["old_n"] else "  –"
            n = f"{g['new_won']/g['new_lock']*100:3.0f}%" if g["new_lock"] else "  –"
            print(f"   {str(st):<14} {g['old_n']:>4}  {o:>5}   {g['new_lock']:>4}  {n:>5}   {g['skip']:>4}")

    if a["flips"]:
        print(f"\n  decision changes ({len(a['flips'])}; date · series · change · old_high→new_high · strike):")
        for d, s, tag, oh, nh, k in a["flips"][:20]:
            print(f"   {d}  {str(s):<12} {tag:<12} {oh}→{nh}  strike {k}")
    print("\n  NOTE: lower bound — only re-decides markets already locked; assumes fills at the")
    print("  recorded price. Validate forward too: a fresh post-fix cohort + asos_sigma bias→0.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--path", default=str(LOG))
    ap.add_argument("--sleep", type=float, default=0.3, help="seconds between IEM fetches (be polite)")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        raise SystemExit(_selftest())

    records = _load_settled(Path(args.path))
    if not records:
        print(f"no settled locks in {args.path}")
        return

    def tz_of(series):
        st = STATIONS.get(series)
        return st[1] if st else "America/New_York"

    print(f"re-fetching corrected obs for {len(records)} locks (cached by station+day)…")
    a = replay(records, fetch_iem_day, tz_of, sleep=args.sleep)
    _report(a)


def _selftest() -> int:
    # Three synthetic locks; a fake archive that returns the CORRECTED (cooler) obs.
    obs_by_max = lambda mx: [("2026-06-20 13:00", mx - 4), ("2026-06-20 16:00", mx),
                             ("2026-06-20 20:00", mx - 6)]                  # sparse → qc=raw=mx
    corrected = {  # (station) → corrected daily max after the fix
        "MIA": 84,   # was read hot at 93 → real 84 (below strike 88)
        "DFW": 86,   # NO lock, fine
        "NYC": 79,   # was 88 → real 79 (below strike 80)
    }
    recs = [
        # A: old locked YES@88 and LOST (hot bug). Corrected 84 → NO, actual no → win (flip).
        {"series": "KXHIGHMIA", "station": "MIA", "ts": "2026-06-20T20:00Z", "kind": "above",
         "strike": 88, "side": "YES", "status": "lost", "result": "no", "market_yes": 0.6,
         "realized_high": 93, "paper_pnl": -0.6},
        # B: old locked NO@92 and WON. Corrected 86 → NO, actual no → win (unchanged).
        {"series": "KXHIGHTDAL", "station": "DFW", "ts": "2026-06-20T20:00Z", "kind": "above",
         "strike": 92, "side": "NO", "status": "won", "result": "no", "market_yes": 0.3,
         "realized_high": 86, "paper_pnl": 0.3},
        # C: old locked YES@80 and WON. Corrected 79 → not confident → skip (missed winner).
        {"series": "KXHIGHNY", "station": "NYC", "ts": "2026-06-20T20:00Z", "kind": "above",
         "strike": 80, "side": "YES", "status": "won", "result": "yes", "market_yes": 0.5,
         "realized_high": 88, "paper_pnl": 0.5},
    ]
    fake_fetch = lambda station, tz, d: obs_by_max(corrected[station])
    a = replay(recs, fake_fetch, lambda s: "America/New_York")
    assert a["n"] == 3 and a["scored"] == 3, a
    assert a["bad_date"] == 0 and a["fetch_error"] == 0 and a["empty_obs"] == 0, a
    assert a["old_won"] == 2, a                              # B, C won; A lost
    assert a["per_station"]["NYC"]["skip"] == 1, a           # C (NYC) skipped under the fix
    assert a["new_lock"] == 2 and a["new_won"] == 2, a       # A flips loss→NO-win, B stays NO-win
    assert a["skip"] == 1 and a["skip_old_win"] == 1 and a["skip_old_loss"] == 0, a   # C skipped
    assert abs(a["new_pnl"] - (0.6 + 0.3)) < 1e-9, a["new_pnl"]   # A NO@.6 win=1−.4; B NO@.3 win=1−.7
    print("replay scoring OK (flip loss→win, unchanged win, missed-winner skip)")
    print("PASS")
    return 0


if __name__ == "__main__":
    main()
