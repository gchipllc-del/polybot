"""
Kalshi Uptime Check — detect scanner gaps that indicate the Mac slept.

Reads kalshi_15min_signal.jsonl, looks at scan timestamps over the
last 24h, and flags any gap longer than `max_gap_minutes` (default
10). Anything bigger than that almost certainly means the host
machine was asleep — Kalshi BTC markets are 24/7 so the scanner
should never have a 10+ minute pause on its own.

Designed to be cheap: O(N) scan of the JSONL, single pass, no deps.
Suitable for a cron-driven daily check or a dashboard widget.

Output:
    {
        "status": "healthy" | "gap_detected",
        "n_gaps": int,
        "longest_gap_minutes": float,
        "gaps": [{"start_iso": str, "duration_min": float}, ...],
        "expected_samples_24h": int,
        "actual_samples_24h": int,
        "uptime_pct": float,
    }
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional


_ROOT = Path(__file__).resolve().parent.parent
_SIGNAL_PATH = _ROOT / "data" / "kalshi_15min_signal.jsonl"

# Scanner cadence: 60s → expected sample density 60/min × 60 × 24 = 1440/day
# in optimistic case. In practice there's only one entry per asset per cycle
# (~1 sample/min) so 24h = ~1440 samples per asset.
EXPECTED_SAMPLES_PER_24H = 60 * 24  # 1440


def check_uptime(*, max_gap_minutes: float = 10.0, hours: float = 24.0) -> dict:
    """Walk the signal log, find gaps > max_gap_minutes within the last
    `hours` hours. Returns a structured health report."""
    if not _SIGNAL_PATH.exists():
        return {
            "status": "no_data",
            "reason": "signal log not found",
            "path": str(_SIGNAL_PATH),
        }

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    timestamps: list[datetime] = []
    try:
        with open(_SIGNAL_PATH) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts_str = row.get("sample_at") or row.get("timestamp")
                if not ts_str:
                    continue
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    continue
                if ts >= cutoff:
                    timestamps.append(ts)
    except OSError as e:
        return {"status": "error", "reason": "read_failed", "error": str(e)[:200]}

    if not timestamps:
        return {
            "status": "no_data",
            "reason": "no samples in the lookback window",
            "lookback_hours": hours,
        }

    timestamps.sort()
    # Dedup near-duplicate timestamps (multiple markets sampled in same cycle)
    # — round to 30-second buckets so we count each cycle, not each market.
    unique_cycles: list[datetime] = []
    last_bucket: Optional[int] = None
    for ts in timestamps:
        bucket = int(ts.timestamp() // 30)
        if bucket != last_bucket:
            unique_cycles.append(ts)
            last_bucket = bucket

    gaps = []
    longest = 0.0
    for i in range(1, len(unique_cycles)):
        gap_seconds = (unique_cycles[i] - unique_cycles[i - 1]).total_seconds()
        gap_min = gap_seconds / 60
        if gap_min > max_gap_minutes:
            gaps.append({
                "start_iso": unique_cycles[i - 1].isoformat(),
                "duration_min": round(gap_min, 1),
            })
        longest = max(longest, gap_min)

    expected = int(EXPECTED_SAMPLES_PER_24H * (hours / 24))
    actual = len(unique_cycles)
    uptime_pct = round(min(100.0, actual / expected * 100), 1) if expected > 0 else 0.0

    return {
        "status": "gap_detected" if gaps else "healthy",
        "n_gaps": len(gaps),
        "longest_gap_minutes": round(longest, 1),
        "gaps": gaps,
        "expected_samples": expected,
        "actual_cycles": actual,
        "uptime_pct": uptime_pct,
        "first_sample": unique_cycles[0].isoformat() if unique_cycles else None,
        "last_sample": unique_cycles[-1].isoformat() if unique_cycles else None,
        "lookback_hours": hours,
        "max_gap_threshold_minutes": max_gap_minutes,
    }


def render_report(report: dict) -> str:
    """Render a human-readable uptime report."""
    if report.get("status") == "no_data":
        return f"⚠ NO DATA — {report.get('reason', 'unknown')}"
    if report.get("status") == "error":
        return f"✗ ERROR — {report.get('reason', 'unknown')}"

    lines = [
        "=" * 60,
        f"  KALSHI SCANNER UPTIME (last {report['lookback_hours']}h)",
        "=" * 60,
        f"  Status:           {report['status'].upper()}",
        f"  Uptime:           {report['uptime_pct']}%  "
        f"({report['actual_cycles']}/{report['expected_samples']} cycles)",
        f"  Last sample:      {report['last_sample']}",
        f"  Longest gap:      {report['longest_gap_minutes']} min",
        f"  Gaps > {report['max_gap_threshold_minutes']} min:    {report['n_gaps']}",
    ]
    if report.get("gaps"):
        lines.append("")
        lines.append("  Detected gaps:")
        for g in report["gaps"][:10]:
            lines.append(f"    {g['start_iso']}  →  {g['duration_min']} min")
        if len(report["gaps"]) > 10:
            lines.append(f"    ... and {len(report['gaps']) - 10} more")
    lines.append("=" * 60)
    return "\n".join(lines)
