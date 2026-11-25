"""history_backfill - the public-endpoint fetcher and the 15-minute backfill loop.

Nothing here touches the network or sleeps for real: fetchers are injected and
time.sleep is replaced by a recorder.
"""
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import history_backfill as hb  # noqa: E402


@pytest.fixture
def slept(monkeypatch):
    """Every time.sleep() the code under test asks for, recorded instead of slept."""
    waits: list[float] = []
    monkeypatch.setattr(time, "sleep", waits.append)
    return waits


def _listing_api(n_markets: int, server_cap: int = 1000):
    """A /markets endpoint holding `n_markets` settled markets, newest first, that serves
    at most `server_cap` per page whatever the caller asks for."""
    calls: list[dict] = []

    def get(url, params=None):
        assert url.endswith("/markets"), url
        p = dict(params or {})
        calls.append(p)
        off = int(p.get("cursor") or 0)
        end = min(n_markets, off + min(int(p["limit"]), server_cap))
        ms = [{"ticker": f"KXT-{j:05d}", "result": "yes"} for j in range(off, end)]
        return {"markets": ms, "cursor": str(end) if end < n_markets else ""}

    return get, calls


# ── the settled-market listing ────────────────────────────────────────────────

def test_settled_listing_asks_for_1000_markets_a_page(slept):
    """The endpoint serves up to 1,000 markets a page. Asking for 200 made every backfill
    run and every resume spend five times the listing calls for the same markets."""
    get, calls = _listing_api(2500)
    ms = hb.fetch_settled_markets("KXT", get=get, max_markets=9000)
    assert [m["ticker"] for m in ms] == [f"KXT-{j:05d}" for j in range(2500)]
    assert [c["limit"] for c in calls] == [1000, 1000, 1000]          # was 13 calls of 200
    assert [c.get("cursor") for c in calls] == [None, "1000", "2000"]
    assert all(c["series_ticker"] == "KXT" and c["status"] == "settled" for c in calls)


def test_settled_listing_is_the_same_when_the_server_gives_smaller_pages(slept):
    """The loop follows the cursor, so a server that answers with fewer markets than it
    was asked for yields the same list - only in more calls. max_markets still cuts it."""
    small, small_calls = _listing_api(2500, server_cap=200)
    big, big_calls = _listing_api(2500)
    assert hb.fetch_settled_markets("KXT", get=small, max_markets=9000) == \
        hb.fetch_settled_markets("KXT", get=big, max_markets=9000)
    assert len(small_calls) == 13 and len(big_calls) == 3
    del big_calls[:]
    cut = hb.fetch_settled_markets("KXT", get=big, max_markets=1200)
    assert [m["ticker"] for m in cut] == [f"KXT-{j:05d}" for j in range(1200)]
    assert len(big_calls) == 2
