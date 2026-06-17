#!/usr/bin/env python3
"""kalshi_live_psr — pull your REAL Kalshi settled trades and run the SAME
per-day PSR / MinTRL we use on the paper sleeves, so a live result (e.g. the
$50→$280 run) can be judged as edge vs. a lucky streak on the dataset that
actually matters: your account.

READ-ONLY. Only issues GET /portfolio/* requests — it never places, cancels, or
modifies an order. Needs your signed Kalshi client (lib/kalshi_auth: KALSHI_API_KEY
+ KALSHI_PRIVATE_KEY_PATH in .env), so run it on the machine that holds the keys.

  python scripts/kalshi_live_psr.py probe    # RUN FIRST — dump raw sample + counts
  python scripts/kalshi_live_psr.py psr       # per-day PSR on settled positions
  python scripts/kalshi_live_psr.py account   # balance + open positions (paused vs holding?)
  python scripts/kalshi_live_psr.py breakdown # P&L by family (BTC/WEATHER/…) + daily timeline
  python scripts/kalshi_live_psr.py psr --since 2026-06-01

Why probe first: Kalshi's settlement/fill field names aren't visible from the
dev box this was written on. `probe` prints one raw settlement + one raw fill so
we confirm the schema; if `psr` then misreads a field, the fix is obvious.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load_dotenv() -> None:
    """Load KEY=VALUE lines from the repo-root .env into os.environ (without
    overriding already-set vars), so a manual `python scripts/...` run picks up
    Kalshi creds the same way the launchd runners do when they `source .env`."""
    import os
    for envp in (ROOT / ".env", Path.cwd() / ".env"):
        if not envp.exists():
            continue
        for line in envp.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v


def _signed_get(path: str, params: dict):
    from lib.kalshi_auth import signed_get, can_sign
    if not can_sign():
        raise SystemExit("can't sign Kalshi requests — set KALSHI_API_KEY and "
                         "KALSHI_PRIVATE_KEY_PATH in .env on the machine with your keys.")
    try:
        return signed_get(path, params=params)
    except Exception as e:
        msg = str(e)
        if "401" in msg or "Unauthorized" in msg.lower():
            raise SystemExit(
                "Kalshi returned 401 Unauthorized. Your .pem signed fine, so the "
                "KALSHI_API_KEY (Key ID) is wrong — it must be the Key ID that "
                "PAIRS with that .pem (Kalshi → Settings → API), not a placeholder "
                "or a different key. Fix .env and re-run.")
        raise SystemExit(f"Kalshi request to {path} failed: {msg[:200]}")


def _page(path: str, key: str, limit: int = 200, max_pages: int = 50) -> list:
    """Pull every page of a /portfolio list endpoint (cursor-paginated)."""
    out, cursor = [], None
    for _ in range(max_pages):
        params = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        data = _signed_get(path, params)
        out.extend(data.get(key, []) or [])
        cursor = data.get("cursor")
        if not cursor:
            break
    return out


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def fetch_settlements() -> list:
    return _page("/portfolio/settlements", "settlements")


def fetch_balance() -> dict:
    return _signed_get("/portfolio/balance", {})


def fetch_positions() -> list:
    return _page("/portfolio/positions", "market_positions")


def fetch_orders() -> list:
    return _page("/portfolio/orders", "orders")


def build_returns(settlements: list) -> tuple[list, dict]:
    """Per-settlement net P&L + return from the settlement's OWN fields (no fills
    join needed — confirmed against Kalshi's settlements schema):
      cost  = yes_total_cost_dollars + no_total_cost_dollars   (DOLLARS, strings)
      payout = revenue                                          (CENTS → /100)
      fee   = fee_cost                                          (DOLLARS, string)
      net = payout − cost − fee ; return = net / cost.
    _num() parses both numbers and dollar-strings like '0.5600'."""
    rows, skipped = [], 0
    for s in settlements:
        yc = _num(s.get("yes_total_cost_dollars"))
        nc = _num(s.get("no_total_cost_dollars"))
        cost = yc + nc
        if cost <= 0:                       # never opened a paid position here → skip
            skipped += 1
            continue
        # which side did we BUY? discriminates weather-fade (NO) from a directional
        # YES strategy — the key to identifying which strategy made the money.
        side = "NO" if nc > yc else "YES"
        payout = _num(s.get("revenue")) / 100.0     # revenue is in cents
        fee = _num(s.get("fee_cost"))
        net = payout - cost - fee
        day = str(s.get("settled_time") or "")[:10] or "?"
        rows.append({"date": day, "ticker": s.get("ticker", ""),
                     "result": s.get("market_result", ""), "side": side,
                     "cost": cost, "net": net, "ret": net / cost})
    return rows, {"settlements": len(settlements), "matched": len(rows),
                  "skipped": skipped}


def cmd_account(_args) -> None:
    """Live account state — balance + open positions. Answers 'is the strategy
    PAUSED or just HOLDING?' without needing the TCC-blocked Desktop checkout,
    since this is all server-side. Read-only."""
    bal = _num(fetch_balance().get("balance")) / 100.0
    print(f"=== LIVE account ===\n  balance: ${bal:,.2f}")
    pos = fetch_positions()
    openp = [p for p in pos if _num(p.get("position")) != 0]
    print(f"  open market positions: {len(openp)}")
    for p in openp[:40]:
        q = _num(p.get("position"))
        exp = _num(p.get("market_exposure")) / 100.0
        print(f"    {str(p.get('ticker',''))[:34]:34} pos {q:+.0f}  exposure ${exp:,.2f}")
    if not openp:
        print("    none — no capital deployed → the strategy is PAUSED, not holding.")
    else:
        print("    → capital is deployed; recent lack of settlements may just be "
              "open positions not yet resolved, not a pause.")

    # Order history narrows WHY it paused, without needing the Desktop logs:
    # last-order date ≈ when it last even TRIED to trade.
    try:
        orders = fetch_orders()
    except SystemExit:
        orders = []
    if orders:
        from collections import Counter
        dates = sorted(str(o.get("created_time") or "")[:10] for o in orders
                       if o.get("created_time"))
        statuses = Counter(o.get("status", "?") for o in orders[:60])
        print(f"  orders on record: {len(orders)} · last order placed: "
              f"{dates[-1] if dates else '?'}")
        print(f"  recent order statuses: {dict(statuses)}")
        print("  READ: if 'last order placed' ≈ the last settlement date, it stopped "
              "TRYING (review-mode/disabled/crashed-pre-order), not 'trying but not "
              "filling'. That points the Desktop check straight at the mode/gate.")
    else:
        print("  no orders on record (or endpoint empty) — consistent with a long pause.")


def cmd_probe(_args) -> None:
    settlements = fetch_settlements()
    print(f"=== PROBE — {len(settlements)} settlements ===")
    if settlements:
        print("\nraw settlement[0]:")
        print(json.dumps(settlements[0], indent=2)[:1200])
    rows, meta = build_returns(settlements)
    print(f"\nmatched {meta['matched']} settlements to a cost basis "
          f"({meta['skipped']} skipped, cost≤0). If matched is ~0, the field names "
          f"above differ from the schema — paste this output and I'll fix the mapping.")
    if rows:
        net = sum(r["net"] for r in rows)
        days = sorted({r["date"] for r in rows})
        print(f"preview: net ${net:+.2f} across {len(rows)} settled positions, "
              f"{len(days)} days ({days[0]}…{days[-1]}).")


def _family(ticker: str) -> str:
    """Group a ticker into a strategy family so we can see WHICH one made money."""
    t = (ticker or "").upper()
    if t.startswith("KXBTC"):
        return "BTC"
    if t.startswith("KXETH"):
        return "ETH"
    if t.startswith(("KXHIGH", "KXLOW", "KXTEMP")):
        return "WEATHER"
    return t.split("-")[0] or "?"


def _psr_pair(per_day: list):
    """(psr_str, mintrl_str) for a per-day return series."""
    from lib.hermes_significance import (probabilistic_sharpe_ratio,
                                         min_track_record_length)
    psr = probabilistic_sharpe_ratio(per_day)
    mt = min_track_record_length(per_day)
    psr_s = "n<5" if psr is None else f"{psr:.2f}"
    mt_s = "n<5" if mt is None else ("∞" if mt == float("inf") else str(int(mt)))
    return psr_s, mt_s


def cmd_breakdown(args) -> None:
    """Split realized P&L by strategy family (BTC / WEATHER / ETH / …) AND show a
    chronological per-day timeline. Answers 'was it BTC or weather (or both) that
    made money?' and 'was the recent tail losing before it stopped?'"""
    from collections import Counter
    rows, meta = build_returns(fetch_settlements())
    if args.since:
        rows = [r for r in rows if r["date"] >= args.since]
    if args.family:
        rows = [r for r in rows if _family(r["ticker"]) == args.family.upper()]
    if not rows:
        print(f"no matched settled positions ({meta}; filters since={args.since} "
              f"family={args.family}).")
        return

    fam = defaultdict(list)
    for r in rows:
        fam[_family(r["ticker"])].append(r)
    print(f"=== LIVE account by family — {len(rows)} positions, "
          f"matched {meta['matched']}/{meta['settlements']} "
          f"(since={args.since or 'all'}) ===")
    print(f"{'family':10} {'pos':>4} {'days':>4} {'net$':>9} {'WR':>4} "
          f"{'side(Y/N)':>9} {'PSR/day':>8} {'MinTRL':>7}")
    for f, rs in sorted(fam.items(), key=lambda kv: -sum(x["net"] for x in kv[1])):
        days = defaultdict(lambda: [0.0, 0.0])
        for r in rs:
            days[r["date"]][0] += r["net"]
            days[r["date"]][1] += r["cost"]
        per_day = [n / c for n, c in days.values() if c > 0]
        net = sum(r["net"] for r in rs)
        wins = sum(1 for r in rs if r["net"] > 0)
        sd = Counter(r["side"] for r in rs)
        psr_s, mt_s = _psr_pair(per_day)
        print(f"{f[:10]:10} {len(rs):>4} {len(days):>4} {net:>+9.2f} "
              f"{wins/len(rs)*100:>3.0f}% {str(sd['YES'])+'/'+str(sd['NO']):>9} "
              f"{psr_s:>8} {mt_s:>7}")

    print("\n=== per-day timeline — is the recent tail losing? ===")
    byday = defaultdict(float)
    for r in rows:
        byday[r["date"]] += r["net"]
    run = 0.0
    for d in sorted(byday):
        run += byday[d]
        print(f"  {d}  net {byday[d]:>+8.2f}   running {run:>+8.2f}")

    # With a single --family selected, dump per-position detail so we can SEE
    # exactly what the strategy traded (tickers + side) and identify it.
    if args.family:
        print(f"\n=== {args.family.upper()} positions (side: which side we bought) ===")
        for r in sorted(rows, key=lambda r: (r["date"], -r["net"])):
            print(f"  {r['date']}  {str(r['ticker'])[:30]:30} {r['side']:>3} "
                  f"→{r['result']:>3}  net {r['net']:>+7.2f}")
    print("\n  READ: side Y/N tells you the strategy. Mostly-NO weather = the "
          "weather-FADE family we ruled out on 187 paper trades (so a winning live "
          "window is suspect); mostly-YES = a different, directional strategy.")


def cmd_psr(args) -> None:
    from lib.hermes_significance import (probabilistic_sharpe_ratio,
                                         min_track_record_length)
    rows, meta = build_returns(fetch_settlements())
    if args.since:
        rows = [r for r in rows if r["date"] >= args.since]
    if not rows:
        print(f"no matched settled positions ({meta}). Run `probe` first to check "
              f"the schema.")
        return

    per_trade = [r["ret"] for r in rows]
    byday = defaultdict(lambda: [0.0, 0.0])
    for r in rows:
        byday[r["date"]][0] += r["net"]
        byday[r["date"]][1] += r["cost"]
    per_day = [net / cost for net, cost in byday.values() if cost > 0]

    net = sum(r["net"] for r in rows)
    wins = sum(1 for r in rows if r["net"] > 0)
    print(f"=== LIVE Kalshi account — {len(rows)} settled positions across "
          f"{len(byday)} days ===")
    print(f"  realized P&L ${net:+.2f} · {wins}W/{len(rows)-wins}L "
          f"({wins/len(rows)*100:.0f}% WR) · matched {meta['matched']}/"
          f"{meta['settlements']} settlements")

    def tier(p):
        return ("n<5" if p is None else "NO MEASURED EDGE" if p < 0.50
                else "provisional" if p < 0.95 else "EVIDENCE-BACKED")

    def mtrl(m):
        return "n<5" if m is None else ("∞" if m == float("inf") else str(int(m)))

    print("\n=== SIGNIFICANCE — PSR / MinTRL (return = net P&L / cost) ===")
    for label, xs, unit, note in (
        ("per-TRADE", per_trade, "trades", "each position independent → optimistic"),
        ("per-DAY  ", per_day, "days", "correlated within a day → the honest sample"),
    ):
        p = probabilistic_sharpe_ratio(xs)
        ps = "n<5 " if p is None else f"{p:.2f}"
        print(f"  {label}  n={len(xs):<4} PSR(edge>0)={ps}  "
              f"MinTRL={mtrl(min_track_record_length(xs)):>4} {unit:<6} [{tier(p)}]  · {note}")
    print("  READ: judge by per-DAY. PSR<0.50 = not even probably positive; the "
          "$50→$280 run is edge only if this clears it. MinTRL ∞ = non-positive "
          "central tendency, a lucky streak rather than a repeatable edge.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", nargs="?", default="probe",
                    choices=["probe", "psr", "account", "breakdown"])
    ap.add_argument("--since", help="only positions settled on/after YYYY-MM-DD")
    ap.add_argument("--family", help="breakdown: filter to one family (BTC/WEATHER/ETH) "
                    "+ dump per-position detail")
    args = ap.parse_args()
    _load_dotenv()
    {"probe": cmd_probe, "psr": cmd_psr, "account": cmd_account,
     "breakdown": cmd_breakdown}[args.mode](args)


if __name__ == "__main__":
    main()
