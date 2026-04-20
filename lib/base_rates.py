"""
Base Rates — learned historical resolution frequencies by category.

Replaces the flat 0.50 placeholder priors in forecaster.py with empirical
rates derived from Manifold's resolved-market history. Methodology:

    1. Paginate /v0/markets on Manifold, filter to isResolved + BINARY
    2. Map each market to our canonical category via group slugs + keyword
       heuristics on the question text
    3. Compute per-category YES-resolution rate (sample mean)
    4. Require N ≥ MIN_SAMPLES_PER_CATEGORY before trusting the rate —
       otherwise fall back to curated static priors
    5. Cache to data/base_rates.json; refresh every CACHE_TTL_DAYS

Security & reliability:
    - All Manifold responses treated as untrusted; each market validated
    - Timeouts on every HTTP call; no infinite pagination loop
    - Cache survives on-disk across runs; degrades gracefully to static
      priors if the API is down
    - No secrets needed (public endpoint)
    - Full audit trail of refreshes

This module is the FIRST place in polybot where priors come from actual
data, not gut-feel. Used by lib/forecaster.py as the base_rate argument
to bayesian_update() and as the base rate in geomean-log-odds weighting.
"""

import json
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests

from lib.audit import log_event

DATA_DIR = Path(__file__).parent.parent / "data"
BASE_RATES_CACHE = DATA_DIR / "base_rates.json"
MANIFOLD_API = "https://api.manifold.markets/v0"

# Fallback curated priors used when empirical data is insufficient. These
# deviate from 0.50 in the direction practitioners consistently report
# prediction markets mispricing (challengers in politics over-priced,
# dramatic geopolitical events over-priced, etc.).
STATIC_FALLBACK: dict[str, float] = {
    "politics":     0.42,  # Challengers over-priced; incumbents tend to hold
    "economics":    0.55,  # Status-quo bias: rates hold, no recession
    "weather":      0.50,
    "crypto":       0.45,  # Bullish-thesis markets often over-shoot
    "sports":       0.50,
    "entertainment": 0.45,
    "ai_tech":      0.48,
    "geopolitical": 0.35,  # Dramatic events less likely than bettors assume
    "science":      0.40,  # Breakthrough claims usually over-stated
    "other":        0.50,
}

CACHE_TTL_DAYS = 7
MIN_SAMPLES_PER_CATEGORY = 25   # Need ≥25 resolved markets before trusting
MAX_MARKETS_TO_FETCH = 2000     # Bound on paginated sweep
REQUEST_TIMEOUT_SEC = 15
PAGE_DELAY_SEC = 0.3            # Rate-limit politeness


# ── Category Taxonomy ──────────────────────────────────────────────

# Maps canonical categories → keywords. A market matches a category if any
# keyword appears in its question, description, or group slugs. Order
# matters: first match wins, so more specific categories come first.
_CATEGORY_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("crypto",       ("bitcoin", "btc", "ethereum", "eth", "crypto", "coin",
                      "nft", "defi", "altcoin", "solana", "blockchain")),
    ("ai_tech",      ("openai", "anthropic", "claude", "gpt", "llm", "agi",
                      "ai model", "deepmind", "nvidia gpu", "meta ai",
                      "ai safety", "neural")),
    ("politics",     ("election", "senate", "congress", "house", "president",
                      "primary", "caucus", "biden", "trump", "harris", "vance",
                      "governor", "mayor", "campaign", "ballot", "vote",
                      "impeach", "scotus", "supreme court", "republican",
                      "democrat", "parliament", "starmer", "sunak", "macron")),
    ("geopolitical", ("ukraine", "russia", "war", "nato", "israel", "gaza",
                      "iran", "china", "taiwan", "north korea", "putin",
                      "zelensky", "cease-fire", "ceasefire", "invasion",
                      "sanction", "treaty", "nuclear")),
    ("economics",    ("fed", "federal reserve", "interest rate", "rate cut",
                      "rate hike", "inflation", "cpi", "ppi", "gdp",
                      "recession", "unemployment", "jobs report", "jerome powell",
                      "yield", "treasury", "tariff")),
    ("weather",      ("hurricane", "temperature", "snowfall", "rainfall",
                      "storm", "tornado", "heat wave", "el niño", "la niña",
                      "climate", "degrees")),
    ("sports",       ("nfl", "nba", "mlb", "nhl", "super bowl", "world cup",
                      "olympics", "championship", "playoff", "fifa", "uefa",
                      "soccer", "football", "basketball", "baseball", "hockey",
                      "tennis", "golf", "f1", "formula 1")),
    ("entertainment", ("oscar", "emmy", "grammy", "golden globe", "box office",
                       "movie", "film", "album", "billboard", "netflix",
                       "disney", "streaming")),
    ("science",      ("vaccine", "fda", "clinical trial", "phase 3", "drug",
                      "peer-reviewed", "nature paper", "experiment", "physics",
                      "mars rover", "spacex", "rocket launch")),
]


def _categorize_market(market: dict) -> str:
    """Map a Manifold market dict to a canonical category string."""
    text_parts = [
        market.get("question", ""),
        market.get("textDescription", "") or "",
    ]
    # groupSlugs is a list like ["us-politics-2024", "elections"]
    for slug in (market.get("groupSlugs") or []):
        text_parts.append(str(slug).replace("-", " "))

    text = " ".join(text_parts).lower()

    for category, keywords in _CATEGORY_KEYWORDS:
        if any(kw in text for kw in keywords):
            return category

    return "other"


# ── Cache I/O ──────────────────────────────────────────────────────

def _empty_cache() -> dict:
    return {
        "generated_at": None,
        "samples_by_category": {},
        "rates_by_category": {},
        "total_samples": 0,
    }


def _load_cache() -> dict:
    if not BASE_RATES_CACHE.exists():
        return _empty_cache()
    try:
        with open(BASE_RATES_CACHE) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return _empty_cache()
        return data
    except (json.JSONDecodeError, OSError):
        return _empty_cache()


def _save_cache(cache: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = BASE_RATES_CACHE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(cache, f, indent=2)
    tmp.replace(BASE_RATES_CACHE)  # Atomic on POSIX


def _cache_is_fresh(cache: dict) -> bool:
    gen = cache.get("generated_at")
    if not gen:
        return False
    try:
        gen_dt = datetime.fromisoformat(gen.replace("Z", "+00:00"))
        age_days = (datetime.now(timezone.utc) - gen_dt).total_seconds() / 86400
        return age_days < CACHE_TTL_DAYS
    except (ValueError, TypeError):
        return False


# ── Manifold Scrape ────────────────────────────────────────────────

def _fetch_resolved_markets(max_markets: int = MAX_MARKETS_TO_FETCH) -> list[dict]:
    """Paginate /v0/markets and return the first max_markets resolved binary."""
    collected: list[dict] = []
    before: str | None = None
    pages = 0
    max_pages = 20  # Hard cap to prevent infinite loop

    while len(collected) < max_markets and pages < max_pages:
        params = {"limit": 500}
        if before:
            params["before"] = before

        try:
            resp = requests.get(
                f"{MANIFOLD_API}/markets",
                params=params,
                timeout=REQUEST_TIMEOUT_SEC,
            )
            resp.raise_for_status()
            batch = resp.json()
        except (requests.RequestException, json.JSONDecodeError) as e:
            log_event("base_rates", "fetch_page_failed", {
                "page": pages,
                "error": str(e)[:200],
            }, result="failed")
            break

        if not isinstance(batch, list) or not batch:
            break

        for m in batch:
            if not isinstance(m, dict):
                continue
            if not m.get("isResolved"):
                continue
            if m.get("outcomeType") != "BINARY":
                continue
            if m.get("resolution") not in ("YES", "NO"):
                continue  # MKT or CANCEL resolutions aren't useful here
            collected.append(m)

        before = batch[-1].get("id") if batch else None
        if not before:
            break

        pages += 1
        time.sleep(PAGE_DELAY_SEC)

    return collected[:max_markets]


def compute_base_rates() -> dict:
    """Scrape Manifold, categorize, compute rates, write cache."""
    log_event("base_rates", "refresh_started", {
        "max_markets": MAX_MARKETS_TO_FETCH,
    }, result="pending")

    markets = _fetch_resolved_markets()

    samples: dict[str, list[str]] = defaultdict(list)
    for m in markets:
        cat = _categorize_market(m)
        samples[cat].append(m.get("resolution"))

    rates: dict[str, float] = {}
    sample_counts: dict[str, int] = {}
    for cat, outcomes in samples.items():
        sample_counts[cat] = len(outcomes)
        if len(outcomes) < MIN_SAMPLES_PER_CATEGORY:
            continue
        yes_count = sum(1 for o in outcomes if o == "YES")
        rates[cat] = round(yes_count / len(outcomes), 4)

    cache = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "samples_by_category": sample_counts,
        "rates_by_category": rates,
        "total_samples": len(markets),
    }

    _save_cache(cache)

    log_event("base_rates", "refresh_complete", {
        "total_samples": len(markets),
        "categories_with_data": len(rates),
        "sample_counts": sample_counts,
        "rates": rates,
    }, result="success")

    return cache


# ── Public API ─────────────────────────────────────────────────────

def get_base_rate(category: str | None, refresh_if_stale: bool = False) -> float:
    """
    Return the YES-resolution rate for a category.

    Resolution order:
        1. Cached empirical rate (if samples ≥ MIN_SAMPLES_PER_CATEGORY)
        2. Static curated fallback for the category
        3. 0.50 for unrecognized categories

    Args:
        category: Canonical category string (politics/economics/etc)
        refresh_if_stale: If True, refetch from Manifold when cache is stale.
                          Defaults to False so the forecaster doesn't block
                          on every call — callers should trigger refresh
                          explicitly via `main.py calibrate` or on a cron.

    Returns:
        Probability in [0.01, 0.99].
    """
    cat = (category or "other").lower()
    cache = _load_cache()

    if refresh_if_stale and not _cache_is_fresh(cache):
        try:
            cache = compute_base_rates()
        except Exception as e:
            log_event("base_rates", "refresh_failed", {
                "error": str(e)[:200],
            }, result="failed")
            # Continue with whatever cache we have

    rates = cache.get("rates_by_category", {})
    if cat in rates:
        rate = rates[cat]
    else:
        rate = STATIC_FALLBACK.get(cat, 0.50)

    return max(0.01, min(float(rate), 0.99))


def get_all_base_rates() -> dict[str, dict]:
    """
    Return a dict of {category: {"rate": float, "n": int, "source": str}}
    for inspection / dashboard display.
    """
    cache = _load_cache()
    rates = cache.get("rates_by_category", {})
    counts = cache.get("samples_by_category", {})

    out: dict[str, dict] = {}
    # All known categories (empirical + static)
    for cat in set(list(STATIC_FALLBACK.keys()) + list(rates.keys())):
        if cat in rates:
            out[cat] = {
                "rate": rates[cat],
                "n": counts.get(cat, 0),
                "source": "empirical",
            }
        else:
            out[cat] = {
                "rate": STATIC_FALLBACK.get(cat, 0.50),
                "n": counts.get(cat, 0),
                "source": "fallback",
            }
    return out
