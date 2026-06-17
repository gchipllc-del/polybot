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
  python scripts/kalshi_live_psr.py psr --since 26MAY01

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
        cost = _num(s.get("yes_total_cost_dollars")) + _num(s.get("no_total_cost_dollars"))
        if cost <= 0:                       # never opened a paid position here → skip
            skipped += 1
            continue
        payout = _num(s.get("revenue")) / 100.0     # revenue is in cents
        fee = _num(s.get("fee_cost"))
        net = payout - cost - fee
        day = str(s.get("settled_time") or "")[:10] or "?"
        rows.append({"date": day, "ticker": s.get("ticker", ""),
                     "result": s.get("market_result", ""),
                     "cost": cost, "net": net, "ret": net / cost})
    return rows, {"settlements": len(settlements), "matched": len(rows),
                  "skipped": skipped}


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
    ap.add_argument("mode", nargs="?", default="probe", choices=["probe", "psr"])
    ap.add_argument("--since", help="only positions settled on/after YYYY-MM-DD")
    args = ap.parse_args()
    _load_dotenv()
    {"probe": cmd_probe, "psr": cmd_psr}[args.mode](args)


if __name__ == "__main__":
    main()
