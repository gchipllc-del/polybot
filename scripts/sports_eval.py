#!/usr/bin/env python3
"""sports_eval — the shared scorer for BOTH sports sleeves (sports_lock + devig_check).

A signal log is not an edge until its trades RESOLVE and clear a significance bar.
This reads each sleeve's JSONL, dedupes to one paper position per market (first time
it was flagged = our entry), settles each against the Kalshi market result, and reports:

  • per-day return series → PSR / Deflated-SR / Min-Track-Record-Length
        (lib/hermes_significance — the same Bailey & López de Prado stack the
         weather/daily sleeves use). DSR raises the bar by how many things we tried.
  • calibration of the win-prob that drove the trade → Brier score + a reliability
        table (does "98% locked" actually win 98% of the time? is the sharp fair
        prob honest?). A sleeve can be PROFITABLE but MIS-calibrated, or vice-versa —
        we want to see both.

Returns are reported GROSS and NET of the Kalshi taker fee, on capital-at-risk.
READ-ONLY: resolution results are cached in data/sports_eval_resolutions.json so
re-runs only fetch still-unsettled tickers. Places no orders, mutates no signal log.

  python scripts/sports_eval.py eval                 # both sleeves
  python scripts/sports_eval.py eval --log lock      # just sports_lock
  python scripts/sports_eval.py eval --log devig --trials 12
  python scripts/sports_eval.py selftest

A trade only counts once its Kalshi market has SETTLED; unsettled signals are reported
as "pending". With <5 settled trades PSR/DSR are withheld (too few to mean anything) —
that's the point: collect first, judge later.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from lib.hermes_significance import (probabilistic_sharpe_ratio,  # noqa: E402
                                     deflated_sharpe_ratio,
                                     min_track_record_length)

LOGS = {
    "lock":  ROOT / "data" / "sports_lock.jsonl",
    "devig": ROOT / "data" / "devig_check.jsonl",
}
RESOLUTIONS = ROOT / "data" / "sports_eval_resolutions.json"
REL_BINS = [(0.50, 0.60), (0.60, 0.70), (0.70, 0.80), (0.80, 0.90),
            (0.90, 0.95), (0.95, 0.99), (0.99, 1.0001)]


# ── pure scoring core (testable, no network) ─────────────────────────────────

def kalshi_fee(entry: float, contracts: int = 1) -> float:
    """Kalshi taker fee in dollars per contract: ceil(0.07·C·P·(1−P)) cents."""
    import math
    p = max(0.0, min(1.0, entry))
    return math.ceil(0.07 * contracts * p * (1 - p) * 100) / 100.0


def base_yes(rec: dict):
    """The market's YES price, whichever sleeve wrote the record."""
    v = rec.get("kalshi_yes", rec.get("market_yes"))
    return v if isinstance(v, (int, float)) else None


def entry_price(rec: dict):
    """What we'd pay per $1 contract for the side this signal says to buy.
    YES → the YES price; NO → 1 − YES price. None if unpriced or degenerate."""
    y = base_yes(rec)
    if y is None:
        return None
    e = y if rec.get("side") == "YES" else (1.0 - y)
    return e if 0.0 < e < 1.0 else None


def predicted_prob(rec: dict):
    """Our model's probability that the side we bought WINS.
    lock  → win_prob (we always back the current leader, so our side = the leader).
    devig → fair_yes for a YES buy, 1−fair_yes for a NO buy."""
    if "fair_yes" in rec:                                   # devig_check schema
        fy = rec.get("fair_yes")
        if not isinstance(fy, (int, float)):
            return None
        return fy if rec.get("side") == "YES" else (1.0 - fy)
    wp = rec.get("win_prob")                                # sports_lock schema
    return wp if isinstance(wp, (int, float)) else None


def side_won(side: str, result: str) -> bool:
    """Did the side we bought settle in-the-money? result is Kalshi 'yes'/'no'."""
    return (result == "yes") if side == "YES" else (result == "no")


def trade_return(entry: float, won: bool, fee: float = 0.0) -> float:
    """Return on capital-at-risk for a $1-settling contract bought at `entry`:
    win → (1 − entry − fee)/entry ; loss → (−entry − fee)/entry."""
    payoff = 1.0 if won else 0.0
    return (payoff - entry - fee) / entry


def per_day_returns(dated: list) -> list:
    """[(date, ret), …] → daily mean-return series, ordered by date. 'Per-day PSR'
    means we judge the equal-weight daily return, so a busy day isn't overweighted."""
    by_day = defaultdict(list)
    for d, r in dated:
        by_day[d].append(r)
    return [sum(v) / len(v) for _, v in sorted(by_day.items())]


def calibration(pairs: list, bins=REL_BINS) -> dict:
    """[(pred, realized 0/1), …] → Brier score + reliability table.
    Each bin: (lo, hi, n, mean_pred, mean_realized). Well-calibrated ⇒ mean_pred ≈
    mean_realized in every populated bin."""
    pairs = [(p, r) for p, r in pairs if p is not None]
    if not pairs:
        return {"n": 0, "brier": None, "bins": []}
    brier = sum((p - r) ** 2 for p, r in pairs) / len(pairs)
    table = []
    for lo, hi in bins:
        b = [(p, r) for p, r in pairs if lo <= p < hi]
        if b:
            table.append((lo, hi, len(b),
                          round(sum(p for p, _ in b) / len(b), 4),
                          round(sum(r for _, r in b) / len(b), 4)))
    return {"n": len(pairs), "brier": round(brier, 4), "bins": table}


def score_trades(trades: list, n_trials: int) -> dict:
    """trades: [dict(date, entry, won, pred)] → full metric block (gross + net)."""
    gross_dated = [(t["date"], trade_return(t["entry"], t["won"])) for t in trades]
    net_dated = [(t["date"], trade_return(t["entry"], t["won"], kalshi_fee(t["entry"])))
                 for t in trades]
    g_daily, n_daily = per_day_returns(gross_dated), per_day_returns(net_dated)
    g_trade = [r for _, r in gross_dated]
    n_trade = [r for _, r in net_dated]
    cal = calibration([(t["pred"], 1.0 if t["won"] else 0.0) for t in trades])
    return {
        "n_trades": len(trades),
        "hit_rate": round(sum(1 for t in trades if t["won"]) / len(trades), 4) if trades else None,
        "mean_ret_gross": round(sum(g_trade) / len(g_trade), 4) if g_trade else None,
        "mean_ret_net": round(sum(n_trade) / len(n_trade), 4) if n_trade else None,
        "days": len(g_daily),
        "psr_gross": probabilistic_sharpe_ratio(g_daily),
        "psr_net": probabilistic_sharpe_ratio(n_daily),
        "dsr_net": deflated_sharpe_ratio(n_daily, n_trials),
        "mintrl_net": min_track_record_length(n_daily),
        "calibration": cal,
    }


# ── resolution (needs network) ───────────────────────────────────────────────

def _load_resolutions() -> dict:
    if RESOLUTIONS.exists():
        try:
            return json.loads(RESOLUTIONS.read_text())
        except Exception:
            return {}
    return {}


def _save_resolutions(d: dict) -> None:
    RESOLUTIONS.parent.mkdir(parents=True, exist_ok=True)
    RESOLUTIONS.write_text(json.dumps(d, indent=0, sort_keys=True))


def fetch_result(ticker: str):
    """Kalshi settlement for a ticker → 'yes' / 'no', or None if not yet settled."""
    from fetch_backtest_data import _kalshi_get
    try:
        d = _kalshi_get(f"/markets/{ticker}", {})
    except Exception:
        return None
    m = (d or {}).get("market") or {}
    if m.get("status") == "settled" and m.get("result") in ("yes", "no"):
        return m["result"]
    return None


def load_signals(path: Path) -> dict:
    """Read a signal JSONL → {ticker: earliest record} (one paper entry per market)."""
    by_ticker = {}
    if not path.exists():
        return by_ticker
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        tk = rec.get("ticker")
        if not tk:
            continue
        if tk not in by_ticker or rec.get("ts", "") < by_ticker[tk].get("ts", ""):
            by_ticker[tk] = rec
    return by_ticker


def build_trades(signals: dict, resolutions: dict, refresh: bool):
    """Turn deduped signals into resolved trades; returns (trades, pending). Fills the
    resolution cache for any settled tickers we don't yet have (unless refresh=False)."""
    trades, pending = [], []
    for tk, rec in signals.items():
        entry = entry_price(rec)
        if entry is None:
            continue                                        # unpriced at flag time
        res = resolutions.get(tk)
        if res not in ("yes", "no") and refresh:
            res = fetch_result(tk)
            if res in ("yes", "no"):
                resolutions[tk] = res
        if res not in ("yes", "no"):
            pending.append(tk)
            continue
        trades.append({"date": str(rec.get("ts", ""))[:10], "entry": entry,
                       "won": side_won(rec.get("side"), res),
                       "pred": predicted_prob(rec)})
    return trades, pending


# ── reporting ────────────────────────────────────────────────────────────────

def _fmt(x, pct=False):
    if x is None:
        return "—"
    if x == float("inf"):
        return "∞"
    return f"{x*100:.1f}%" if pct else f"{x:+.3f}" if isinstance(x, float) else str(x)


def _report(name: str, trades: list, pending: list, n_trials: int) -> None:
    print(f"\n=== {name} === ({len(trades)} settled, {len(pending)} pending)")
    if not trades:
        print("  no settled trades yet — collect more, then re-run.")
        return
    s = score_trades(trades, n_trials)
    print(f"  hit-rate {_fmt(s['hit_rate'], pct=True)}  "
          f"mean-ret gross {_fmt(s['mean_ret_gross'])}  net {_fmt(s['mean_ret_net'])}  "
          f"over {s['days']} day(s)")
    if s["psr_net"] is None:
        print(f"  PSR/DSR withheld: only {s['days']} day(s) of returns (need ≥5).")
    else:
        print(f"  PSR  gross {_fmt(s['psr_gross'], pct=True)}  net {_fmt(s['psr_net'], pct=True)}"
              f"   (P[true Sharpe>0]; want >95%)")
        print(f"  DSR  net {_fmt(s['dsr_net'], pct=True)}   (vs best-of-{n_trials}-trials; "
              f"want >95% to survive the search)")
        print(f"  MinTRL net {_fmt(s['mintrl_net'])} days   (days needed to confirm at 95%)")
    cal = s["calibration"]
    print(f"  calibration: Brier {_fmt(cal['brier'])} on {cal['n']} graded "
          f"(lower=better; 0.25=coin-flip)")
    if cal["bins"]:
        print("    predicted   n   mean_pred  actual")
        for lo, hi, n, mp, ar in cal["bins"]:
            flag = "  ⚠" if abs(mp - ar) > 0.10 and n >= 3 else ""
            print(f"    [{lo:.2f},{hi:.2f}) {n:>3}   {mp:.3f}    {ar:.3f}{flag}")


def cmd_eval(args) -> None:
    which = ["lock", "devig"] if args.log == "both" else [args.log]
    resolutions = _load_resolutions()
    refresh = not args.no_refresh
    combined_trades, combined_pending = [], []
    for key in which:
        sigs = load_signals(LOGS[key])
        trades, pending = build_trades(sigs, resolutions, refresh)
        _report(f"sports_{key}", trades, pending, args.trials)
        combined_trades += trades
        combined_pending += pending
    if args.log == "both" and combined_trades:
        _report("BOTH sleeves combined", combined_trades, combined_pending, args.trials)
    if refresh:
        _save_resolutions(resolutions)
    print("\nNOTE: paper P&L on Kalshi settlement. PSR/DSR/MinTRL from lib/hermes_significance "
          "(Bailey & López de Prado). Edge is real only when net DSR clears 95% AND calibration "
          "holds. Forward-collect before trusting.")


def selftest() -> int:
    # entry_price: YES uses the yes price; NO uses 1−yes; both schemas
    assert entry_price({"side": "YES", "kalshi_yes": 0.60}) == 0.60
    assert abs(entry_price({"side": "NO", "kalshi_yes": 0.60}) - 0.40) < 1e-9
    assert abs(entry_price({"side": "NO", "market_yes": 0.70}) - 0.30) < 1e-9
    assert entry_price({"side": "YES", "market_yes": None}) is None
    assert entry_price({"side": "YES", "kalshi_yes": 1.0}) is None      # degenerate
    print("entry_price OK")
    # predicted_prob: devig flips on side; lock uses win_prob
    assert predicted_prob({"fair_yes": 0.62, "side": "YES"}) == 0.62
    assert abs(predicted_prob({"fair_yes": 0.62, "side": "NO"}) - 0.38) < 1e-9
    assert predicted_prob({"win_prob": 0.99, "side": "NO"}) == 0.99     # we back the leader
    print("predicted_prob OK")
    # side_won
    assert side_won("YES", "yes") and not side_won("YES", "no")
    assert side_won("NO", "no") and not side_won("NO", "yes")
    print("side_won OK")
    # trade_return: buy at 0.40, win → (1−.4)/.4=1.5; lose → −1; net subtracts fee
    assert abs(trade_return(0.40, True) - 1.5) < 1e-9
    assert abs(trade_return(0.40, False) - (-1.0)) < 1e-9
    assert abs(trade_return(0.50, True, kalshi_fee(0.50)) - ((1 - 0.5 - 0.02) / 0.5)) < 1e-9
    print("trade_return OK")
    # per_day_returns: equal-weight within a day, ordered across days
    assert per_day_returns([("2026-01-02", 1.0), ("2026-01-01", 0.0),
                            ("2026-01-02", 0.0)]) == [0.0, 0.5]
    print("per_day_returns OK")
    # calibration: perfect preds → Brier 0; bins land in mean_pred≈actual
    cal = calibration([(0.99, 1.0), (0.99, 1.0), (0.55, 0.0), (0.55, 0.0)])
    assert cal["brier"] == round((0.01**2 * 2 + 0.55**2 * 2) / 4, 4), cal
    assert any(lo <= 0.99 < hi for lo, hi, *_ in cal["bins"])
    print("calibration OK")
    # score_trades end-to-end: a strong, well-calibrated edge → high PSR, low Brier
    import random
    random.seed(7)
    trades = []
    for i in range(40):
        won = random.random() < 0.98                       # 98%-calibrated locks
        trades.append({"date": f"2026-03-{i+1:02d}", "entry": 0.90,
                       "won": won, "pred": 0.98})
    s = score_trades(trades, n_trials=8)
    assert s["psr_net"] is not None and s["psr_net"] > 0.90, s["psr_net"]
    assert s["calibration"]["brier"] < 0.05, s["calibration"]
    print(f"score_trades OK (psr_net {s['psr_net']:.3f}, brier {s['calibration']['brier']})")
    # a coin-flip priced at 0.50 should NOT look significant
    random.seed(1)
    flips = [{"date": f"2026-04-{i+1:02d}", "entry": 0.50,
              "won": random.random() < 0.50, "pred": 0.50} for i in range(40)]
    sf = score_trades(flips, n_trials=8)
    assert sf["dsr_net"] is None or sf["dsr_net"] < 0.95, sf["dsr_net"]
    print("score_trades (coin-flip → not significant) OK")
    print("PASS")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["eval", "selftest"])
    ap.add_argument("--log", choices=["lock", "devig", "both"], default="both",
                    help="which sleeve(s) to score (default both)")
    ap.add_argument("--trials", type=int, default=8,
                    help="configs/sleeves explored, for the DSR bar (default 8)")
    ap.add_argument("--no-refresh", action="store_true",
                    help="use only the cached resolutions; don't hit Kalshi")
    args = ap.parse_args()
    if args.mode == "selftest":
        raise SystemExit(selftest())
    cmd_eval(args)


if __name__ == "__main__":
    main()
