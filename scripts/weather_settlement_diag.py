#!/usr/bin/env python3
"""weather_settlement_diag — does the cheap-NO edge come from FORECAST SKILL, or from
luck/settlement noise? The roast council's decisive test (the Logician's contradiction).

The claim under attack: a 0.95°F-MAE nowcast cannot justify a 43-point edge in the exact
zone where its own error bars go blind — i.e. when the temperature settles right AT the
strike. So we split the settled NO trades by HOW DECISIVELY they resolved:

  settle_margin = strike_f - actual_temp_f      # >0 = NO won, by this many °F of cushion
  fc_margin     = strike_f - nws_forecast_f     # >0 = the forecast (known at entry) said NO

Two outcomes, opposite conclusions:
  * REAL edge   — cheap-NO winners settled DECISIVELY below strike (market priced YES ~97%
                  but the temp wasn't close), AND trades in the forecast's blind zone
                  (|fc_margin| < MAE) are ~coin flips. Profit tracks forecast decisiveness.
  * LUCK/noise  — winners cluster inside the settlement noise band (|actual-strike| < ~1°F),
                  indistinguishable from losers, AND the blind zone is still "profitable"
                  (so the profit is NOT coming from forecast skill — it's price/variance).

Read-only. Runs on the settled ledger you already have; no network, no fills needed.

  python scripts/weather_settlement_diag.py
  python scripts/weather_settlement_diag.py --mae 0.95 --cheap 0.15
  python scripts/weather_settlement_diag.py --selftest
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "data" / "weather_paper.jsonl"


def _f(r, k):
    try:
        return float(r.get(k))
    except (TypeError, ValueError):
        return None


def _won(r):
    return str(r.get("status", "")).lower() == "won"


def _rows(path: Path):
    raw = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    out = []
    for r in raw:
        if str(r.get("side")).upper() != "NO":
            continue
        if str(r.get("status", "")).lower() not in ("won", "lost"):
            continue
        strike, actual, fc = _f(r, "strike_f"), _f(r, "actual_temp_f"), _f(r, "nws_forecast_f")
        if strike is None or actual is None:
            continue
        out.append({
            "fill": _f(r, "fill_price"), "won": _won(r), "pnl": _f(r, "paper_pnl") or 0.0,
            "settle_margin": strike - actual,            # >0 NO won by this cushion
            "fc_margin": (strike - fc) if fc is not None else None,
            "near_strike": abs(actual - strike),         # settlement noise distance
            "mkt_yes": _f(r, "market_p_yes"),
        })
    return out


def _wr(rows):
    return (sum(1 for r in rows if r["won"]) / len(rows)) if rows else None


def _net(rows):
    return sum(r["pnl"] for r in rows)


def analyze(rows, *, mae=0.95, cheap=0.15) -> dict:
    cheap_rows = [r for r in rows if (r["fill"] or 1) < cheap]
    winners = [r for r in cheap_rows if r["won"]]
    losers = [r for r in cheap_rows if not r["won"]]

    def med(rs, key):
        vals = [r[key] for r in rs if r[key] is not None]
        return round(statistics.median(vals), 2) if vals else None

    # blind zone: forecast within MAE of the strike → the nowcast genuinely can't call it.
    blind = [r for r in rows if r["fc_margin"] is not None and abs(r["fc_margin"]) < mae]
    decisive = [r for r in rows if r["fc_margin"] is not None and abs(r["fc_margin"]) >= mae]

    # how decisively did cheap-NO winners actually resolve?
    decisive_winners = [r for r in winners if r["settle_margin"] >= 1.0]
    noise_winners = [r for r in winners if r["near_strike"] < 1.0]

    def avg_fill(rs):
        vals = [r["fill"] for r in rs if r["fill"] is not None]
        return round(statistics.mean(vals), 3) if vals else None

    def cheapfrac(rs):
        return round(sum(1 for r in rs if (r["fill"] or 1) < cheap) / len(rs), 2) if rs else None

    return {
        "blind_avg_fill": avg_fill(blind), "blind_cheap_frac": cheapfrac(blind),
        "decisive_avg_fill": avg_fill(decisive), "decisive_cheap_frac": cheapfrac(decisive),
        "n_all": len(rows), "n_cheap": len(cheap_rows),
        "cheap_win_settle_med": med(winners, "settle_margin"),
        "cheap_loss_settle_med": med(losers, "settle_margin"),
        "cheap_win_decisive_frac": round(len(decisive_winners) / len(winners), 3) if winners else None,
        "cheap_win_noise_frac": round(len(noise_winners) / len(winners), 3) if winners else None,
        "blind_n": len(blind), "blind_wr": _wr(blind), "blind_net": round(_net(blind), 2),
        "decisive_n": len(decisive), "decisive_wr": _wr(decisive),
        "decisive_net": round(_net(decisive), 2),
        "cheap_mkt_yes_med": med(cheap_rows, "mkt_yes"),
        "cheap_settle_med": med(cheap_rows, "settle_margin"),
    }


def _verdict(a: dict, mae: float) -> str:
    """Data-driven read — not a binary oracle, but a clear lean."""
    signals = []
    # 1) did the forecast's blind zone still print? Positive net there CANNOT be forecast
    # skill (the forecast couldn't call these) — it's either the structural longshot bias
    # or pure payoff-asymmetry variance. Sub-50% WR + positive net is the asymmetry tell.
    if a["blind_n"] >= 8 and a["blind_net"] > 50:
        src = ("payoff-asymmetry/variance" if (a["blind_wr"] or 0) < 0.5
               else "structural longshot bias")
        signals.append(f"BLIND-ZONE PROFITABLE: trades the forecast couldn't call still net "
                       f"${a['blind_net']:+.0f} ({a['blind_wr']*100:.0f}% WR) → this profit is NOT "
                       f"forecast skill ({src}); it's the part most exposed to fills+fees. ⚠")
    elif a["blind_n"] >= 8:
        signals.append(f"blind-zone ~flat ({a['blind_wr']*100:.0f}% WR, ${a['blind_net']:+.0f}) "
                       "→ the edge lives in DECISIVE cases, as it should. ✓")
    # 1b) where is the REAL (decisive) edge priced — is it fillable?
    if a["decisive_avg_fill"] is not None:
        signals.append(f"the decisive-forecast edge (the real one) entered at avg "
                       f"${a['decisive_avg_fill']} ({int((a['decisive_cheap_frac'] or 0)*100)}% in the "
                       f"cheap <{0.15} bucket) vs blind-zone avg ${a['blind_avg_fill']} "
                       f"({int((a['blind_cheap_frac'] or 0)*100)}% cheap) — higher entry price = "
                       "MORE fillable, so the skill edge is the more capturable one.")
    # 2) did cheap winners settle decisively or in the noise band?
    if a["cheap_win_decisive_frac"] is not None:
        if a["cheap_win_decisive_frac"] >= 0.6:
            signals.append(f"{a['cheap_win_decisive_frac']*100:.0f}% of cheap-NO winners settled "
                           "≥1°F below strike (decisive, market was genuinely wrong). ✓ leans REAL.")
        elif (a["cheap_win_noise_frac"] or 0) >= 0.5:
            signals.append(f"{a['cheap_win_noise_frac']*100:.0f}% of cheap-NO winners settled within "
                           "1°F of strike (inside settlement noise). ⚠ leans LUCK.")
    # 3) decisive-forecast edge
    if a["decisive_n"] >= 8 and (a["decisive_wr"] or 0) > 0.6:
        signals.append(f"when the forecast was decisive (|fc−strike|≥{mae}°F) WR is "
                       f"{a['decisive_wr']*100:.0f}% (${a['decisive_net']:+.0f}). ✓ skill shows up "
                       "where the forecast can actually call it.")
    return "\n".join("  - " + s for s in signals) or "  - inconclusive (too few trades)."


def run(mae=0.95, cheap=0.15) -> int:
    if not LEDGER.exists():
        print(f"no ledger at {LEDGER} (it lives on the trading host).")
        return 1
    rows = _rows(LEDGER)
    if not rows:
        print("no settled NO trades found.")
        return 1
    a = analyze(rows, mae=mae, cheap=cheap)

    def pct(v):
        return "—" if v is None else f"{v*100:.0f}%"

    dec_frac = pct(a["cheap_win_decisive_frac"])
    noise_frac = pct(a["cheap_win_noise_frac"])
    blind_wr = pct(a["blind_wr"])
    decisive_wr = pct(a["decisive_wr"])
    print(f"# weather settlement diagnostic — {a['n_all']} settled NO trades "
          f"({a['n_cheap']} cheap <{cheap})\n")
    print("CHEAP-NO bucket (where ~100% of the profit lives):")
    print(f"  market priced YES (median)         : {a['cheap_mkt_yes_med']}")
    print(f"  temp settled below strike (median) : {a['cheap_settle_med']}°F "
          "(how wrong the YES market was)")
    print(f"  winners settled below strike (med) : {a['cheap_win_settle_med']}°F")
    print(f"  losers   settled below strike (med): {a['cheap_loss_settle_med']}°F  (>0 means a "
          "NO that 'lost' was actually below strike — settlement/timing mismatch)")
    print(f"  winners that were DECISIVE (≥1°F)  : {dec_frac}")
    print(f"  winners INSIDE noise band (<1°F)   : {noise_frac}")
    print(f"\nFORECAST BLIND ZONE (|forecast−strike| < {mae}°F MAE — the nowcast can't call it):")
    print(f"  n={a['blind_n']}  WR={blind_wr}  net=${a['blind_net']:+.2f}")
    print(f"DECISIVE FORECAST (|forecast−strike| ≥ {mae}°F):")
    print(f"  n={a['decisive_n']}  WR={decisive_wr}  net=${a['decisive_net']:+.2f}")
    print("\nREAD:")
    print(_verdict(a, mae))
    print("\nNOTE: 'decisive winners' = the market was genuinely wrong (real edge). 'noise-band'")
    print("or 'blind-zone profitable' = the profit isn't forecast skill; it's variance riding")
    print("the payoff asymmetry, and it will NOT survive real fills + fees. Pair with")
    print("weather_no_fill_probe.py before committing real money.")
    return 0


def _selftest() -> int:
    # REAL-pattern fixture: cheap winners settle decisively (3°F below), blind zone is coin-flip.
    rows = [
        {"fill": 0.05, "won": True, "pnl": 40, "settle_margin": 3.0, "fc_margin": 2.5,
         "near_strike": 3.0, "mkt_yes": 0.96},
        {"fill": 0.08, "won": True, "pnl": 35, "settle_margin": 2.5, "fc_margin": 2.0,
         "near_strike": 2.5, "mkt_yes": 0.94},
        {"fill": 0.10, "won": False, "pnl": -5, "settle_margin": -0.3, "fc_margin": 0.4,
         "near_strike": 0.3, "mkt_yes": 0.90},
    ]
    a = analyze(rows, mae=0.95, cheap=0.15)
    assert a["n_cheap"] == 3, a
    assert a["cheap_win_decisive_frac"] == 1.0, a            # both winners ≥1°F decisive
    assert a["cheap_win_settle_med"] == 2.75, a
    # blind zone = the one trade with |fc_margin|<0.95 (the loser) → not profitable
    assert a["blind_n"] == 1 and a["blind_net"] == -5, a
    assert a["decisive_n"] == 2, a
    txt = _verdict(a, 0.95)
    assert "REAL" in txt, txt
    print("selftest OK")
    print(txt)
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mae", type=float, default=0.95, help="forecast MAE °F (blind-zone width)")
    ap.add_argument("--cheap", type=float, default=0.15, help="cheap-NO entry-price cutoff")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        raise SystemExit(_selftest())
    raise SystemExit(run(a.mae, a.cheap))


if __name__ == "__main__":
    main()
