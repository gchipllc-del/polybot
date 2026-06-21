#!/usr/bin/env python3
"""preflight_live_check — answer "is any real money at risk, and can a new real trade
fire?" in one read-only command. Built after a _paper-named ledger (weather_paper.jsonl)
was found to contain 25 REAL Kalshi orders — so "paper" in a filename is not a guarantee.

Two independent checks:
  1. LEDGER SCAN — every data/*.jsonl for records with is_live==true. Reports historical
     live trades per file and, critically, any OPEN live position (= current real exposure).
  2. LIVE SWITCH — is kalshi_live_executor.is_live_enabled() armed (settings.yaml enabled +
     smoke marker + kill-switch)? If armed, a scheduled live module COULD place a real order.

Exit non-zero (so it works as a pre-flight gate / cron alarm) if there is any OPEN live
position OR the live switch is armed. Clean paper-only state → exit 0.

  python scripts/preflight_live_check.py
  python scripts/preflight_live_check.py --selftest
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OPEN_STATES = {"open", "pending", "", None}     # anything not clearly resolved = still live


def scan_live_rows(data_dir: Path) -> dict:
    """Every *.jsonl in data_dir → {file: {total, live, open_live, net_notional, tickers}}.
    A row counts as OPEN live if is_live is true and status is not a resolved state."""
    out = {}
    for path in sorted(glob.glob(str(data_dir / "*.jsonl"))):
        live, open_live, notional, tickers = 0, 0, 0.0, []
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("is_live") is True:
                live += 1
                notional += float(r.get("live_notional_usd") or r.get("notional") or 0.0)
                tickers.append(r.get("market_ticker") or r.get("ticker") or "?")
                if str(r.get("status")).lower() in {s for s in OPEN_STATES if s} or r.get("status") in OPEN_STATES:
                    open_live += 1
        if live:
            out[Path(path).name] = {"live": live, "open_live": open_live,
                                    "net_notional": round(notional, 2),
                                    "tickers": sorted(set(tickers))}
    return out


def live_switch_state() -> dict:
    """Best-effort read of the live arming. Uses kalshi_live_executor if importable
    (the source of truth), else a defensive manual read of settings + marker."""
    try:
        sys.path.insert(0, str(ROOT))
        from lib.kalshi_live_executor import is_live_enabled, _load_live_config, SMOKE_MARKER_PATH
        cfg = _load_live_config()
        return {"armed": bool(is_live_enabled()),
                "enabled": bool(cfg.get("enabled", False)),
                "smoke_marker": SMOKE_MARKER_PATH.exists(),
                "source": "kalshi_live_executor.is_live_enabled()"}
    except Exception as e:  # config missing / import error → report what we can
        enabled = False
        sp = ROOT / "config" / "settings.yaml"
        if sp.exists():
            txt = sp.read_text()
            # crude but dependency-free: look for an enabled: true under the live block
            import re
            m = re.search(r"kalshi_daily_live:.*?enabled:\s*(true|false)", txt, re.S | re.I)
            enabled = bool(m and m.group(1).lower() == "true")
        marker = (DATA / "kalshi_live_smoke_passed.marker").exists()
        return {"armed": enabled and marker, "enabled": enabled,
                "smoke_marker": marker, "source": f"manual read ({type(e).__name__})"}


def render(rows: dict, switch: dict) -> tuple[str, int]:
    lines = ["=== preflight live check (read-only) ==="]
    total_open = sum(v["open_live"] for v in rows.values())
    total_live = sum(v["live"] for v in rows.values())
    if rows:
        lines.append(f"LEDGERS with is_live=true rows ({total_live} live trades total):")
        for fn, v in rows.items():
            flag = "  ⚠ OPEN LIVE EXPOSURE" if v["open_live"] else ""
            lines.append(f"  {fn:32} live={v['live']:<4} open={v['open_live']:<3} "
                         f"net_notional=${v['net_notional']:<9.2f}{flag}")
            lines.append(f"      tickers: {', '.join(v['tickers'][:6])}"
                         + (" …" if len(v["tickers"]) > 6 else ""))
    else:
        lines.append("LEDGERS: no is_live=true rows anywhere — all ledgers are paper. ✓")
    armed = switch["armed"]
    lines.append(f"LIVE SWITCH: {'⚠ ARMED' if armed else 'disarmed ✓'} "
                 f"(enabled={switch['enabled']}, smoke_marker={switch['smoke_marker']}; "
                 f"via {switch['source']})")
    bad = total_open > 0 or armed
    lines.append("")
    if bad:
        lines.append("RESULT: ⚠ NOT clean-paper — " + ", ".join(
            ([f"{total_open} OPEN live position(s)"] if total_open else [])
            + (["live switch ARMED (a scheduled module could place a real order)"] if armed else [])))
    else:
        lines.append("RESULT: clean — no open real exposure and the live switch is disarmed. ✓")
    return "\n".join(lines), (1 if bad else 0)


def _selftest() -> int:
    import tempfile
    d = Path(tempfile.mkdtemp())
    (d / "weather_paper.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"is_live": True, "status": "won", "market_ticker": "KXTEMPNYCH-A", "live_notional_usd": 10},
        {"is_live": True, "status": "lost", "market_ticker": "KXTEMPNYCH-B", "live_notional_usd": 5},
        {"is_live": False, "status": "open", "market_ticker": "PAPER-C", "notional": 99},
    ]))
    (d / "fc2s_paper.jsonl").write_text(json.dumps(
        {"is_live": False, "status": "open", "ticker": "X", "notional": 3}) + "\n")
    rows = scan_live_rows(d)
    assert "weather_paper.jsonl" in rows and rows["weather_paper.jsonl"]["live"] == 2, rows
    assert rows["weather_paper.jsonl"]["open_live"] == 0, rows           # both resolved
    assert "fc2s_paper.jsonl" not in rows, rows                          # no live rows → omitted
    print("scan_live_rows (settled live) OK")
    # an OPEN live row must be flagged + non-zero exit
    (d / "weather_paper.jsonl").write_text(json.dumps(
        {"is_live": True, "status": "open", "market_ticker": "KXTEMPNYCH-OPEN", "live_notional_usd": 7}) + "\n")
    rows = scan_live_rows(d)
    assert rows["weather_paper.jsonl"]["open_live"] == 1, rows
    _, code = render(rows, {"armed": False, "enabled": False, "smoke_marker": False, "source": "test"})
    assert code == 1, "open live exposure must exit non-zero"
    # clean state → exit 0
    _, code0 = render({}, {"armed": False, "enabled": False, "smoke_marker": False, "source": "test"})
    assert code0 == 0
    # armed switch → exit 1 even with no ledgers
    _, code1 = render({}, {"armed": True, "enabled": True, "smoke_marker": True, "source": "test"})
    assert code1 == 1
    print("render exit codes OK")
    print("PASS")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--data", default=str(DATA))
    args = ap.parse_args()
    if args.selftest:
        raise SystemExit(_selftest())
    text, code = render(scan_live_rows(Path(args.data)), live_switch_state())
    print(text)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
