"""
Bankroll-driven growth-phase selection.

Replaces the hardcoded `growth.phase` setting in strategy.yaml with a
function of current bankroll. Manual override still works — if
`growth.phase_thresholds` isn't configured, callers fall back to the
manual `growth.phase` value.

Background (2026-04-30 polybot bite): bankroll grew from $50 to $739
but `growth.phase` stayed at 1, so `max_concurrent_positions: 3`
blocked every new entry while the book already held 7 positions opened
at an earlier (more permissive) cap.

Configuration in strategy.yaml:

    growth:
      phase_thresholds:        # bankroll → minimum to enter that phase
        - {phase: 1, min_bankroll: 0}
        - {phase: 2, min_bankroll: 200}
        - {phase: 3, min_bankroll: 2000}
        - {phase: 4, min_bankroll: 10000}
      phase_caps:              # phase → max_concurrent_positions
        1: 3
        2: 8
        3: 15
        4: 25

If `phase_thresholds` is absent, the legacy `growth.phase` integer is
used unchanged.
"""

from __future__ import annotations

DEFAULT_THRESHOLDS = [
    {"phase": 1, "min_bankroll": 0.0},
    {"phase": 2, "min_bankroll": 200.0},
    {"phase": 3, "min_bankroll": 2000.0},
    {"phase": 4, "min_bankroll": 10000.0},
]

DEFAULT_CAPS = {1: 3, 2: 8, 3: 15, 4: 25}

DEFAULT_LABELS = {
    1: "Survival",
    2: "Acceleration",
    3: "Scaling",
    4: "Preservation",
}


def _normalize_thresholds(thresholds) -> list[dict]:
    """Accept either the dict-list form or a flat dict {phase: min_bankroll}."""
    if isinstance(thresholds, dict):
        return [
            {"phase": int(p), "min_bankroll": float(v)}
            for p, v in sorted(thresholds.items(), key=lambda kv: int(kv[0]))
        ]
    if isinstance(thresholds, list):
        return [
            {"phase": int(t["phase"]), "min_bankroll": float(t["min_bankroll"])}
            for t in thresholds
        ]
    return list(DEFAULT_THRESHOLDS)


def effective_phase(bankroll: float, strategy: dict | None = None) -> int:
    """
    Return the highest phase whose `min_bankroll` is ≤ current bankroll.

    Falls back to the manual `growth.phase` integer if no thresholds are
    configured (legacy behavior).
    """
    growth = (strategy or {}).get("growth", {})
    thresholds = growth.get("phase_thresholds")
    if not thresholds:
        return int(growth.get("phase", 1))

    normalized = _normalize_thresholds(thresholds)
    if not normalized:
        return int(growth.get("phase", 1))

    selected = normalized[0]["phase"]
    for t in normalized:
        if bankroll >= t["min_bankroll"]:
            selected = t["phase"]
        else:
            break
    return int(selected)


def effective_max_positions(bankroll: float, strategy: dict | None = None) -> int:
    """
    Return max_concurrent_positions for the bankroll-derived phase.

    Resolution order:
      1. growth.phase_caps[<effective_phase>]
      2. growth.max_concurrent_positions (legacy single-cap setting)
      3. DEFAULT_CAPS[<effective_phase>]
    """
    growth = (strategy or {}).get("growth", {})
    phase = effective_phase(bankroll, strategy)

    caps = growth.get("phase_caps") or {}
    # YAML may parse keys as ints OR strings; accept both.
    if phase in caps:
        return int(caps[phase])
    if str(phase) in caps:
        return int(caps[str(phase)])

    legacy = growth.get("max_concurrent_positions")
    if legacy is not None:
        return int(legacy)

    return DEFAULT_CAPS.get(phase, 3)


def phase_label(phase: int) -> str:
    """Human label for the phase (Survival/Acceleration/Scaling/Preservation)."""
    return DEFAULT_LABELS.get(phase, f"Phase {phase}")
