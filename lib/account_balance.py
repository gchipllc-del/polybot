"""Single source of truth for the bankroll the paper sleeves mirror.

`live_account_balance()` returns the REAL Kalshi account balance when the
signed `/portfolio/balance` call is available, falls back to the operator-set
config value (`kalshi_daily_live.account_balance_fallback`), then to a hard
default. Paper sizing and the dashboard both call this, so the paper bankroll
mirrors the live account instead of a static number — edit the config (or fund
the account) and everything tracks it without a code change.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Final fallback if both the signed call and config are unavailable. Matches the
# account size at the time the mirror was introduced.
HARD_DEFAULT = 233.0


def config_balance() -> float | None:
    """Operator-known balance from settings.yaml, or None. Read fresh each call
    so editing the config takes effect without a restart."""
    try:
        import yaml
        cfg = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text()) or {}
        fb = (cfg.get("kalshi_daily_live", {}) or {}).get("account_balance_fallback")
        return round(float(fb), 2) if fb is not None else None
    except Exception:
        return None


def live_account_balance(*, prefer_live: bool = True) -> float:
    """Current account bankroll, mirroring the live Kalshi account.

    Resolution order: signed live balance → config fallback → HARD_DEFAULT.
    Always returns a positive float; never raises.
    """
    if prefer_live:
        try:
            from lib.kalshi_auth import can_sign, signed_get
            if can_sign():
                data = signed_get("/portfolio/balance")
                bal = round(float(data.get("balance", 0)) / 100.0, 2)  # cents → $
                if bal > 0:
                    return bal
        except Exception:
            pass  # fall through to config / default
    cfg = config_balance()
    if cfg is not None and cfg > 0:
        return cfg
    return HARD_DEFAULT


def balance_with_source(*, prefer_live: bool = True) -> tuple[float, str]:
    """Same as live_account_balance but also reports where the number came from
    ('live' | 'config' | 'default') so the UI can label a fallback."""
    if prefer_live:
        try:
            from lib.kalshi_auth import can_sign, signed_get
            if can_sign():
                data = signed_get("/portfolio/balance")
                bal = round(float(data.get("balance", 0)) / 100.0, 2)
                if bal > 0:
                    return bal, "live"
        except Exception:
            pass
    cfg = config_balance()
    if cfg is not None and cfg > 0:
        return cfg, "config"
    return HARD_DEFAULT, "default"
