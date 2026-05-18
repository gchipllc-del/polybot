"""
News Feed — Multi-source news aggregation for prediction market forecasting.

Gathers recent news related to a market question, then converts it into a
probability-like sentiment signal (0.0 = strong NO, 1.0 = strong YES) that
the forecaster can Bayesian-update against.

Sources (in priority order):
    1. NewsAPI — broad headline search (requires NEWSAPI_KEY env var)
    2. RSS feeds — curated domain-specific feeds (no API key needed)
    3. Reddit — subreddit search via public JSON API (no key needed)

Security:
    - API keys loaded ONLY from environment variables
    - All external responses treated as untrusted input
    - Input sanitized before outbound queries (no injection)
    - Results cached with TTL to control costs
    - No secrets in any log or error message
    - Rate limiting on every external call
"""

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import quote_plus

import yaml

from tradingcore.audit import log_event

CONFIG_PATH = Path(__file__).parent.parent / "config"
CACHE_DIR = Path(__file__).parent.parent / "data" / "news_cache"


def _load_settings() -> dict:
    with open(CONFIG_PATH / "settings.yaml", "r") as f:
        return yaml.safe_load(f)


def _load_strategy() -> dict:
    with open(CONFIG_PATH / "strategy.yaml", "r") as f:
        return yaml.safe_load(f)


# ── Data Structures ──────────────────────────────────────────────

@dataclass
class NewsArticle:
    """A single news article from any source."""
    title: str
    source: str               # "newsapi", "rss", "reddit"
    published: str             # ISO timestamp
    url: str
    snippet: str = ""         # First ~200 chars of body
    relevance: float = 0.0    # 0.0-1.0 keyword match score
    sentiment_hint: float = 0.5  # 0.0 = bearish/NO, 1.0 = bullish/YES


@dataclass
class NewsFeedResult:
    """Aggregated news for a market question."""
    market_id: str
    query: str                         # Cleaned search query
    articles: list[NewsArticle] = field(default_factory=list)
    sentiment: float = 0.5             # Aggregated probability signal (0.0-1.0)
    confidence: float = 0.0            # How much we trust this signal (0.0-1.0)
    sources_queried: list[str] = field(default_factory=list)
    article_count: int = 0
    cached: bool = False


# ── Rate Limiter ─────────────────────────────────────────────────

class _RateLimiter:
    """Per-source rate limiter. Single-process safe."""

    def __init__(self, calls_per_minute: int = 30):
        self._interval = 60.0 / max(calls_per_minute, 1)
        self._last_call = 0.0

    def wait(self):
        now = time.time()
        elapsed = now - self._last_call
        if elapsed < self._interval:
            time.sleep(self._interval - elapsed)
        self._last_call = time.time()


_limiters: dict[str, _RateLimiter] = {}


def _get_limiter(source: str, calls_per_minute: int = 30) -> _RateLimiter:
    if source not in _limiters:
        _limiters[source] = _RateLimiter(calls_per_minute)
    return _limiters[source]


# ── Cache ────────────────────────────────────────────────────────

def _cache_key(market_id: str, query: str) -> str:
    """SHA-256 cache key from market ID + query."""
    raw = f"{market_id}:{query}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _cache_get(key: str, ttl_minutes: int = 30) -> dict | None:
    """Read cached news result if still valid."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{key}.json"
    if not path.exists():
        return None

    try:
        with open(path, "r") as f:
            data = json.load(f)
        cached_at = datetime.fromisoformat(data.get("cached_at", ""))
        if datetime.now(timezone.utc) - cached_at > timedelta(minutes=ttl_minutes):
            return None
        return data
    except (json.JSONDecodeError, ValueError, KeyError):
        return None


def _cache_put(key: str, data: dict):
    """Write news result to cache."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    data["cached_at"] = datetime.now(timezone.utc).isoformat()
    path = CACHE_DIR / f"{key}.json"

    # Atomic write via temp file
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    tmp.rename(path)


# ── Query Construction ───────────────────────────────────────────

# Prediction market questions often include noise words — strip them
_NOISE_WORDS = {
    "will", "does", "do", "is", "are", "was", "were", "be", "been",
    "the", "a", "an", "of", "in", "on", "at", "to", "for", "by",
    "or", "and", "this", "that", "it", "with", "from", "before",
    "after", "between", "than", "not", "yes", "no", "resolve",
    "market", "question", "contract", "prediction",
}

# Control chars / special chars stripped from queries
_SANITIZE_RE = re.compile(r"[^\w\s\-']", re.UNICODE)


def _build_search_query(question: str, max_terms: int = 6) -> str:
    """
    Convert a prediction market question into a news search query.

    Strips noise words, special characters, and truncates to the most
    meaningful terms.

    Args:
        question: Market question text
        max_terms: Maximum number of terms to keep

    Returns:
        Cleaned search query string
    """
    if not question or not isinstance(question, str):
        return ""

    # Strip control chars and special chars
    cleaned = _SANITIZE_RE.sub(" ", question)
    # Normalize whitespace
    cleaned = " ".join(cleaned.split())

    # Tokenize and remove noise
    tokens = [t.strip().lower() for t in cleaned.split() if len(t.strip()) > 1]
    meaningful = [t for t in tokens if t not in _NOISE_WORDS]

    # Keep the most meaningful terms (longer words first — they're more specific)
    meaningful.sort(key=len, reverse=True)
    selected = meaningful[:max_terms]

    return " ".join(selected)


# ── Keyword Relevance ────────────────────────────────────────────

def _relevance_score(title: str, query_terms: list[str]) -> float:
    """
    Score how relevant an article title is to our search query.

    Returns 0.0-1.0 based on term overlap.
    """
    if not title or not query_terms:
        return 0.0

    title_lower = title.lower()
    matches = sum(1 for term in query_terms if term in title_lower)
    return min(matches / max(len(query_terms), 1), 1.0)


# ── Source: NewsAPI ──────────────────────────────────────────────

def _fetch_newsapi(query: str, max_articles: int = 10) -> list[NewsArticle]:
    """
    Fetch articles from NewsAPI.org (requires NEWSAPI_KEY env var).

    Returns empty list if key not set or API fails.
    """
    import requests

    api_key = os.environ.get("NEWSAPI_KEY")
    if not api_key:
        return []

    limiter = _get_limiter("newsapi", calls_per_minute=30)
    limiter.wait()

    # Search last 3 days of news
    from_date = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%d")

    url = "https://newsapi.org/v2/everything"
    params = {
        "q": query[:500],  # NewsAPI query length limit
        "from": from_date,
        "sortBy": "relevancy",
        "pageSize": min(max_articles, 20),
        "language": "en",
    }
    headers = {"X-Api-Key": api_key}

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        log_event("news_feed", "newsapi_fetch_failed", {
            "error": str(e)[:200],
        }, result="failed")
        return []

    # Parse — treat all response data as untrusted
    articles = []
    query_terms = query.lower().split()

    for item in data.get("articles", [])[:max_articles]:
        if not isinstance(item, dict):
            continue

        title = str(item.get("title", ""))[:500]
        if not title or title == "[Removed]":
            continue

        source_name = ""
        src = item.get("source")
        if isinstance(src, dict):
            source_name = str(src.get("name", ""))[:100]

        published = str(item.get("publishedAt", ""))[:50]
        article_url = str(item.get("url", ""))[:2000]
        description = str(item.get("description", ""))[:200]

        relevance = _relevance_score(title, query_terms)

        articles.append(NewsArticle(
            title=title,
            source=f"newsapi:{source_name}",
            published=published,
            url=article_url,
            snippet=description,
            relevance=relevance,
        ))

    return articles


# ── Source: RSS Feeds ────────────────────────────────────────────

# Curated domain-specific RSS feeds by category
RSS_FEEDS: dict[str, list[str]] = {
    "politics": [
        "https://feeds.bbci.co.uk/news/politics/rss.xml",
        "https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml",
    ],
    "economics": [
        "https://feeds.bbci.co.uk/news/business/rss.xml",
        "https://rss.nytimes.com/services/xml/rss/nyt/Economy.xml",
    ],
    "crypto": [
        "https://cointelegraph.com/rss",
    ],
    "ai_tech": [
        "https://feeds.bbci.co.uk/news/technology/rss.xml",
        "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml",
    ],
    "science": [
        "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml",
        "https://rss.nytimes.com/services/xml/rss/nyt/Science.xml",
    ],
    "sports": [
        "https://feeds.bbci.co.uk/sport/rss.xml",
    ],
    "geopolitical": [
        "https://feeds.bbci.co.uk/news/world/rss.xml",
        "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
    ],
}


def _fetch_rss(query: str, category: str = "other", max_articles: int = 10) -> list[NewsArticle]:
    """
    Fetch articles from curated RSS feeds relevant to the category.

    Falls back to general feeds if category has no specific feeds.
    """
    import xml.etree.ElementTree as ET

    import requests

    feeds = RSS_FEEDS.get(category, [])
    if not feeds:
        # Fall back to general news
        feeds = [
            "https://feeds.bbci.co.uk/news/rss.xml",
        ]

    limiter = _get_limiter("rss", calls_per_minute=60)
    articles = []
    query_terms = query.lower().split()

    for feed_url in feeds[:3]:  # Max 3 feeds per query
        limiter.wait()

        try:
            resp = requests.get(feed_url, timeout=8, headers={
                "User-Agent": "Polybot/1.0 (news aggregator)"
            })
            resp.raise_for_status()
        except requests.RequestException:
            continue

        # Parse XML — all content is untrusted
        try:
            root = ET.fromstring(resp.content[:500_000])  # Cap at 500KB
        except ET.ParseError:
            continue

        # Standard RSS 2.0 structure
        for item in root.iter("item"):
            title_el = item.find("title")
            link_el = item.find("link")
            pub_el = item.find("pubDate")
            desc_el = item.find("description")

            title = (title_el.text or "")[:500] if title_el is not None else ""
            if not title:
                continue

            relevance = _relevance_score(title, query_terms)
            if relevance < 0.15:  # Skip clearly irrelevant articles
                continue

            article_url = (link_el.text or "")[:2000] if link_el is not None else ""
            published = (pub_el.text or "")[:50] if pub_el is not None else ""
            snippet = ""
            if desc_el is not None and desc_el.text:
                # Strip HTML tags from description
                snippet = re.sub(r"<[^>]+>", "", desc_el.text)[:200]

            articles.append(NewsArticle(
                title=title,
                source=f"rss:{feed_url.split('/')[2]}",
                published=published,
                url=article_url,
                snippet=snippet,
                relevance=relevance,
            ))

    # Sort by relevance, take top N
    articles.sort(key=lambda a: a.relevance, reverse=True)
    return articles[:max_articles]


# ── Source: Reddit ───────────────────────────────────────────────

# Subreddits by category for relevant discussions
REDDIT_SUBS: dict[str, list[str]] = {
    "politics": ["politics", "geopolitics"],
    "economics": ["economics", "finance"],
    "crypto": ["cryptocurrency", "bitcoin"],
    "ai_tech": ["artificial", "technology"],
    "sports": ["sports"],
    "weather": ["weather"],
    "science": ["science"],
}


def _fetch_reddit(query: str, category: str = "other", max_articles: int = 10) -> list[NewsArticle]:
    """
    Fetch recent posts from Reddit's public JSON API.

    No API key needed — uses the public .json endpoint.
    Respects rate limits (Reddit public API: ~10 req/min).
    """
    import requests

    limiter = _get_limiter("reddit", calls_per_minute=10)

    subs = REDDIT_SUBS.get(category, ["news"])
    articles = []
    query_terms = query.lower().split()

    # Search across relevant subreddits
    search_query = quote_plus(query[:200])

    for sub in subs[:2]:  # Max 2 subreddits per query
        limiter.wait()

        url = f"https://www.reddit.com/r/{sub}/search.json"
        params = {
            "q": search_query,
            "sort": "relevance",
            "t": "week",
            "limit": min(max_articles, 10),
            "restrict_sr": "on",
        }
        headers = {"User-Agent": "Polybot/1.0 (prediction market research)"}

        try:
            resp = requests.get(url, params=params, headers=headers, timeout=8)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError):
            continue

        # Parse — all Reddit data is untrusted
        children = data.get("data", {}).get("children", [])
        if not isinstance(children, list):
            continue

        for child in children[:max_articles]:
            if not isinstance(child, dict):
                continue
            post = child.get("data", {})
            if not isinstance(post, dict):
                continue

            title = str(post.get("title", ""))[:500]
            if not title:
                continue

            relevance = _relevance_score(title, query_terms)
            if relevance < 0.15:
                continue

            created = post.get("created_utc", 0)
            try:
                published = datetime.fromtimestamp(float(created), tz=timezone.utc).isoformat()
            except (ValueError, TypeError, OSError):
                published = ""

            permalink = str(post.get("permalink", ""))[:2000]
            article_url = f"https://reddit.com{permalink}" if permalink else ""
            snippet = str(post.get("selftext", ""))[:200]

            # Reddit score as rough quality signal
            score = int(post.get("score", 0)) if isinstance(post.get("score"), (int, float)) else 0

            articles.append(NewsArticle(
                title=title,
                source=f"reddit:r/{sub}",
                published=published,
                url=article_url,
                snippet=snippet,
                relevance=relevance,
            ))

    articles.sort(key=lambda a: a.relevance, reverse=True)
    return articles[:max_articles]


# ── Sentiment Aggregation ────────────────────────────────────────

# Keywords that lean toward YES resolution
_YES_SIGNALS = {
    "confirmed", "approved", "passed", "signed", "agreed", "wins",
    "victory", "successful", "launches", "announces", "surges",
    "breaks", "exceeds", "record", "positive", "gains", "rises",
    "soars", "jumps", "climbs",
}

# Keywords that lean toward NO resolution
_NO_SIGNALS = {
    "rejected", "denied", "failed", "blocked", "delayed", "canceled",
    "cancelled", "withdrawn", "defeated", "loses", "drops", "falls",
    "plunges", "crashes", "declines", "negative", "stalls", "sinks",
    "collapses", "vetoed",
}


def _keyword_sentiment(articles: list[NewsArticle], question: str) -> float:
    """
    Compute a keyword-based sentiment signal from article titles.

    This is a coarse signal — it counts YES/NO leaning keywords and
    converts to a 0.0-1.0 probability-like score.

    Returns 0.5 when evidence is mixed or absent.
    """
    if not articles:
        return 0.5

    yes_count = 0
    no_count = 0
    total_weight = 0.0

    for article in articles:
        title_lower = article.title.lower()
        weight = max(article.relevance, 0.1)

        for word in _YES_SIGNALS:
            if word in title_lower:
                yes_count += weight
                break

        for word in _NO_SIGNALS:
            if word in title_lower:
                no_count += weight
                break

        total_weight += weight

    if total_weight <= 0 or (yes_count + no_count) == 0:
        return 0.5

    # Convert to 0.0-1.0 scale
    yes_ratio = yes_count / (yes_count + no_count)

    # Dampen toward 0.5 — news sentiment is a weak signal
    dampened = 0.5 + (yes_ratio - 0.5) * 0.6

    return max(0.05, min(dampened, 0.95))


# ── Main Entry Point ─────────────────────────────────────────────

def get_news_sentiment(
    market_id: str,
    question: str,
    category: str = "other",
    max_articles_per_source: int = 8,
) -> NewsFeedResult:
    """
    Aggregate news from all sources and produce a sentiment signal.

    This is the function called by the market scanner to feed the forecaster.

    Args:
        market_id: Platform-specific market ID
        question: Market question text
        category: Market category (politics, economics, etc.)
        max_articles_per_source: Max articles from each source

    Returns:
        NewsFeedResult with sentiment signal and supporting articles.
    """
    query = _build_search_query(question)
    if not query:
        return NewsFeedResult(
            market_id=market_id,
            query="",
            sentiment=0.5,
            confidence=0.0,
        )

    # Check cache first
    key = _cache_key(market_id, query)
    cached = _cache_get(key, ttl_minutes=30)
    if cached:
        result = NewsFeedResult(
            market_id=market_id,
            query=query,
            articles=[],  # Don't reconstruct articles from cache
            sentiment=cached.get("sentiment", 0.5),
            confidence=cached.get("confidence", 0.0),
            sources_queried=cached.get("sources_queried", []),
            article_count=cached.get("article_count", 0),
            cached=True,
        )
        return result

    # Gather from all sources
    all_articles: list[NewsArticle] = []
    sources_queried: list[str] = []

    # NewsAPI (richest source, but requires key)
    try:
        newsapi_articles = _fetch_newsapi(query, max_articles=max_articles_per_source)
        all_articles.extend(newsapi_articles)
        if newsapi_articles:
            sources_queried.append("newsapi")
    except Exception as e:
        log_event("news_feed", "newsapi_error", {"error": str(e)[:200]}, result="failed")

    # RSS feeds (free, category-targeted)
    try:
        rss_articles = _fetch_rss(query, category=category, max_articles=max_articles_per_source)
        all_articles.extend(rss_articles)
        if rss_articles:
            sources_queried.append("rss")
    except Exception as e:
        log_event("news_feed", "rss_error", {"error": str(e)[:200]}, result="failed")

    # Reddit (community discussion signal)
    try:
        reddit_articles = _fetch_reddit(query, category=category, max_articles=max_articles_per_source)
        all_articles.extend(reddit_articles)
        if reddit_articles:
            sources_queried.append("reddit")
    except Exception as e:
        log_event("news_feed", "reddit_error", {"error": str(e)[:200]}, result="failed")

    # Compute sentiment
    sentiment = _keyword_sentiment(all_articles, question)

    # Confidence based on volume + source diversity
    # More articles from more sources = higher confidence
    article_count = len(all_articles)
    source_diversity = len(sources_queried) / 3.0  # 3 possible sources
    volume_factor = min(article_count / 15.0, 1.0)  # 15+ articles = full volume

    # Mean relevance of top articles
    sorted_articles = sorted(all_articles, key=lambda a: a.relevance, reverse=True)
    top_relevance = 0.0
    if sorted_articles:
        top_5 = sorted_articles[:5]
        top_relevance = sum(a.relevance for a in top_5) / len(top_5)

    confidence = 0.3 * source_diversity + 0.3 * volume_factor + 0.4 * top_relevance
    confidence = max(0.0, min(confidence, 1.0))

    result = NewsFeedResult(
        market_id=market_id,
        query=query,
        articles=sorted_articles[:20],  # Keep top 20 by relevance
        sentiment=round(sentiment, 4),
        confidence=round(confidence, 4),
        sources_queried=sources_queried,
        article_count=article_count,
    )

    # Cache the result
    _cache_put(key, {
        "sentiment": result.sentiment,
        "confidence": result.confidence,
        "sources_queried": result.sources_queried,
        "article_count": result.article_count,
    })

    log_event("news_feed", "sentiment_computed", {
        "market_id": market_id,
        "query": query[:100],
        "sentiment": result.sentiment,
        "confidence": result.confidence,
        "article_count": article_count,
        "sources": sources_queried,
    }, result="success")

    return result


def clear_cache():
    """Clear all cached news results. Called by Hermes after weight changes."""
    if CACHE_DIR.exists():
        for f in CACHE_DIR.glob("*.json"):
            f.unlink(missing_ok=True)
        log_event("news_feed", "cache_cleared", {})
