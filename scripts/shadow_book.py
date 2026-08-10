#!/usr/bin/env python3
"""shadow_book — what would the pre-registered hypotheses have EARNED, in dollars?

Why this exists: Stage-0's bucket table shows raw mispricing gaps, which answers "is the
market wrong?" but not "would trading it have made money after costs?" Those are different
questions — a 3c gap on a 7c contract dies to a 1c fee. This replays the FROZEN rules
below against data the collector already recorded, buying at the ask you actually saw,
paying real Kalshi fees, and (when order-book depth was captured) checking whether the
size was even there. No orders, no paper ledger, no money: a pure replay of history.

HONESTY LABEL, always printed: these rules were pre-registered in docs/CRYPTO15_RESTART.md
BEFORE the data existed, but the replay is still IN-SAMPLE until a rule's cell reaches
n>=100 AND holds on data collected after the rule was named. In-sample P&L is a hypothesis
with a dollar sign attached, not a track record. This is the number that gets checked, not
the number that earns size.

  py scripts/shadow_book.py report
  py scripts/shadow_book.py report --json
  py scripts/shadow_book.py selftest
"""
from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = Path(os.environ.get("STAGE0_LOG") or (ROOT / "data" / "stage0_crypto.jsonl"))

# ── FROZEN rules (pre-registered; do not add rules to fit observed data) ──────
# Each rule buys ONE contract of `side` when its ask falls in [lo, hi) inside the
# time band, entering at the FIRST such observation per market (entry realism).
#   side "favorite" = whichever side's ask is >= 0.50 (the market's expected winner)
#   side "longshot" = whichever side's ask is < 0.50
RULES = [
    {"name": "H1_settlement_lag", "band": "<2min", "side": "favorite",
     "lo": 0.80, "hi": 0.96,
     "thesis": "final-minutes favorites lag the forming 60s settlement average"},
    {"name": "H1b_fade_late_longshot", "band": "<2min", "side": "longshot",
     "lo": 0.05, "hi": 0.20,
     "thesis": "mirror of H1: late longshots are overpriced (SELL side proxy)"},
    {"name": "H2_far_strike_premium", "band": "2-10min", "side": "longshot",
     "lo": 0.01, "hi": 0.10,
     "thesis": "far-strike lottery premium (council: harvest as MAKER only)"},
    {"name": "CONTROL_midprice", "band": ">10min", "side": "favorite",
     "lo": 0.35, "hi": 0.65,
     "thesis": "control: the efficient core. Should print ~0 or negative."},
    # NAMED 2026-08-10 from Stage-0 data (n=1356). NOT pre-registered from the start, so
    # its shadow-book row is pure data-dredging and must be ignored; only FORWARD paper
    # trades stamped after this date count as evidence. Motivation: at n=150 the mirror
    # cell (2-10min longshot 20-35c) is SIGNIFICANTLY -EV (Wilson CI 0.138-0.257 vs 0.295
    # breakeven), and the favorite side of the same band shows +7.5c gap at n=169 with CI
    # low 0.726 vs 0.738 breakeven - just short of significance. Same phenomenon, two
    # sides; the forward test decides it.
    {"name": "H3_midband_favorite", "band": "2-10min", "side": "favorite",
     "lo": 0.65, "hi": 0.90, "named_at": "2026-08-10",
     "thesis": "2-10min favorites underpriced (mirror of a proven -EV longshot cell)"},
]
BANDS = {">10min": (10.0, 1e9), "2-10min": (2.0, 10.0), "<2min": (0.0, 2.0)}
CONTRACT = 1          # contracts per shadow trade — sizing is NOT the question here
MIN_N_MEANINGFUL = 100


def kalshi_taker_fee(price: float, contracts: int = 1) -> float:
    """Kalshi taker fee, rounded UP to the cent: ceil(0.07 * C * P * (1-P))."""
    if not (0 < price < 1):
        return 0.0
    return math.ceil(0.07 * contracts * price * (1.0 - price) * 100) / 100.0


def _band_of(mins_left) -> str | None:
    if mins_left is None:
        return None
    for name, (lo, hi) in BANDS.items():
        if lo <= mins_left < hi:
            return name
    return None


def _depth_at(book: dict | None, side: str, price: float) -> float | None:
    """Contracts available at <= price on `side`, from a captured book snapshot.
    Kalshi serves levels as [[price_cents, size], ...] under 'yes'/'no'. Returns None
    when no book was captured (auth off at the time)."""
    if not isinstance(book, dict):
        return None
    levels = book.get(side)
    if not isinstance(levels, list):
        return None
    total = 0.0
    for lvl in levels:
        try:
            p_c, size = float(lvl[0]), float(lvl[1])
        except (TypeError, ValueError, IndexError):
            continue
        # a resting order on the OPPOSITE side at (100 - p) is what we lift; accept
        # either encoding by taking any level whose implied ask is <= our price
        for implied in (p_c / 100.0, 1.0 - p_c / 100.0):
            if implied <= price + 1e-9:
                total += size
                break
    return total


def build(rows: list[dict]) -> dict:
    settles = {r["ticker"]: r["result"] for r in rows if r.get("t") == "settle"}
    # first qualifying observation per (ticker, rule)
    picked: dict[tuple, dict] = {}
    for r in rows:
        if r.get("t") != "obs":
            continue
        band = _band_of(r.get("mins_left"))
        if band is None:
            continue
        ya, na = r.get("yes_ask"), r.get("no_ask")
        if ya is None or na is None:
            continue
        try:
            ya, na = float(ya), float(na)
        except (TypeError, ValueError):
            continue
        fav_side, fav_ask = ("yes", ya) if ya >= na else ("no", na)
        lng_side, lng_ask = ("no", na) if ya >= na else ("yes", ya)
        for rule in RULES:
            if rule["band"] != band:
                continue
            side, ask = (fav_side, fav_ask) if rule["side"] == "favorite" else (lng_side, lng_ask)
            if not (rule["lo"] <= ask < rule["hi"]):
                continue
            key = (r.get("ticker"), rule["name"])
            if key in picked:
                continue
            picked[key] = {"ticker": r.get("ticker"), "rule": rule["name"], "side": side,
                           "ask": ask, "book": r.get("book"), "ts": r.get("ts")}

    out = {}
    for rule in RULES:
        trades, wins, gross, fees = [], 0, 0.0, 0.0
        fillable = depth_known = 0
        for (ticker, rname), t in picked.items():
            if rname != rule["name"]:
                continue
            result = settles.get(ticker)
            if result is None:
                continue                      # not settled yet — never counted
            won = (result == t["side"])
            fee = kalshi_taker_fee(t["ask"], CONTRACT)
            pnl_gross = (CONTRACT * (1.0 - t["ask"])) if won else (-CONTRACT * t["ask"])
            gross += pnl_gross
            fees += fee
            wins += 1 if won else 0
            d = _depth_at(t.get("book"), t["side"], t["ask"])
            if d is not None:
                depth_known += 1
                if d >= CONTRACT:
                    fillable += 1
            trades.append({"ticker": ticker, "side": t["side"], "ask": round(t["ask"], 3),
                           "won": won, "pnl": round(pnl_gross - fee, 3)})
        n = len(trades)
        net = gross - fees
        se = (math.sqrt(0.25 / n) if n else None)     # conservative WR standard error
        out[rule["name"]] = {
            "thesis": rule["thesis"], "band": rule["band"], "side": rule["side"],
            "price_range": f"{int(rule['lo']*100)}-{int(rule['hi']*100)}c",
            "n": n, "wins": wins, "win_rate": (wins / n if n else None),
            "gross": round(gross, 2), "fees": round(fees, 2), "net": round(net, 2),
            "net_per_trade": (round(net / n, 4) if n else None),
            "wr_ci95": (round(1.96 * se, 3) if se else None),
            "depth_known": depth_known, "fillable": fillable,
            "meaningful": n >= MIN_N_MEANINGFUL,
            "trades": trades[-10:],
        }
    return {"rules": out, "n_settled_markets": len(settles)}


def print_report(rep: dict) -> None:
    print("=" * 74)
    print("SHADOW BOOK - what the frozen hypotheses WOULD have earned (no orders placed)")
    print("=" * 74)
    print(f"settled markets in log: {rep['n_settled_markets']}")
    print()
    print("rule                    n   WR      +/-    net$    $/trade  fills   status")
    for name, r in rep["rules"].items():
        wr = "  -  " if r["win_rate"] is None else f"{r['win_rate']*100:5.1f}%"
        ci = "     " if r["wr_ci95"] is None else f"{r['wr_ci95']*100:4.1f}%"
        npt = "   -   " if r["net_per_trade"] is None else f"{r['net_per_trade']:+7.3f}"
        fills = (f"{r['fillable']}/{r['depth_known']}" if r["depth_known"]
                 else "  n/a")
        if r["n"] == 0:
            status = "no trades yet"
        elif not r["meaningful"]:
            status = f"THIN (need {MIN_N_MEANINGFUL})"
        elif r["net"] > 0:
            status = "positive - VERIFY"
        else:
            status = "negative"
        print(f"{name:<22} {r['n']:>4}  {wr} {ci}  {r['net']:+7.2f} {npt}  {fills:>6}  {status}")
    print()
    print("WR +/- is the 95% band on win rate: if it dwarfs your edge, the number is noise.")
    print("fills = shadow trades whose captured order book actually had size at that price")
    print("        (n/a before Kalshi auth was configured).")
    print()
    print("IN-SAMPLE WARNING: rules were pre-registered, but this replay scores them on the")
    print("same data that suggested them. A rule earns a real paper phase only after n>=100")
    print("AND holding on data collected after it was named. No money moves on this table.")
    print("=" * 74)


def _load(p: Path) -> list[dict]:
    rows = []
    if p.exists():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def _selftest() -> int:
    # fee rounding
    assert kalshi_taker_fee(0.90) == 0.01 and kalshi_taker_fee(0.50) == 0.02

    rows = [
        # <2min favorite at 0.85 -> settles yes (side 'yes' is favorite) => WIN
        {"t": "obs", "ticker": "A", "mins_left": 1.0, "yes_ask": 0.85, "no_ask": 0.17,
         "book": {"yes": [[85, 40]]}},
        {"t": "settle", "ticker": "A", "result": "yes"},
        # <2min favorite at 0.90 -> settles no => LOSS
        {"t": "obs", "ticker": "B", "mins_left": 0.5, "yes_ask": 0.90, "no_ask": 0.12},
        {"t": "settle", "ticker": "B", "result": "no"},
        # control band, should land in CONTROL_midprice only
        {"t": "obs", "ticker": "C", "mins_left": 30.0, "yes_ask": 0.55, "no_ask": 0.47},
        {"t": "settle", "ticker": "C", "result": "yes"},
        # unsettled market must be ignored entirely
        {"t": "obs", "ticker": "D", "mins_left": 1.0, "yes_ask": 0.88, "no_ask": 0.14},
    ]
    rep = build(rows)
    h1 = rep["rules"]["H1_settlement_lag"]
    assert h1["n"] == 2, h1                    # A and B; D unsettled -> excluded
    assert h1["wins"] == 1
    # A: +0.15 gross, fee ceil(0.07*.85*.15)=ceil(0.89c)=0.01 -> +0.14
    # B: -0.90 gross, fee ceil(0.07*.90*.10)=ceil(0.63c)=0.01 -> -0.91
    assert abs(h1["gross"] - (0.15 - 0.90)) < 1e-9, h1["gross"]
    assert abs(h1["net"] - (0.15 - 0.90 - 0.02)) < 1e-9, h1["net"]
    assert h1["depth_known"] == 1 and h1["fillable"] == 1, h1
    assert not h1["meaningful"]

    ctrl = rep["rules"]["CONTROL_midprice"]
    assert ctrl["n"] == 1 and ctrl["wins"] == 1, ctrl

    # longshot side selection: yes_ask 0.12 is the longshot when no_ask is 0.90
    rows2 = [{"t": "obs", "ticker": "E", "mins_left": 1.0, "yes_ask": 0.12, "no_ask": 0.90},
             {"t": "settle", "ticker": "E", "result": "yes"}]
    r2 = build(rows2)["rules"]["H1b_fade_late_longshot"]
    assert r2["n"] == 1 and r2["wins"] == 1, r2      # bought the 12c longshot, it won
    assert abs(r2["gross"] - 0.88) < 1e-9, r2

    # first-observation entry realism: later cheaper obs must NOT replace the first
    rows3 = [{"t": "obs", "ts": "1", "ticker": "F", "mins_left": 1.9, "yes_ask": 0.85, "no_ask": 0.16},
             {"t": "obs", "ts": "2", "ticker": "F", "mins_left": 1.0, "yes_ask": 0.95, "no_ask": 0.06},
             {"t": "settle", "ticker": "F", "result": "yes"}]
    r3 = build(rows3)["rules"]["H1_settlement_lag"]
    assert r3["n"] == 1 and abs(r3["trades"][0]["ask"] - 0.85) < 1e-9, r3
    print("selftest OK")
    return 0


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    if cmd == "selftest":
        return _selftest()
    if cmd == "report":
        rep = build(_load(LOG))
        if "--json" in sys.argv:
            print(json.dumps(rep, indent=1))
        else:
            print_report(rep)
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
