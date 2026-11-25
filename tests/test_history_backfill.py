"""history_backfill - the public-endpoint fetcher and the 15-minute backfill loop.

Nothing here touches the network or sleeps for real: fetchers are injected and
time.sleep is replaced by a recorder.
"""
import json
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


class Resp:
    """A response as _get sees one: status_code, headers, reason, raise_for_status, json."""

    def __init__(self, status=200, body=None, headers=None, reason=""):
        self.status_code, self.headers, self.reason = status, headers or {}, reason
        self._body = {} if body is None else body

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code} Error for url: https://x/y",
                                     response=self)

    def json(self):
        return self._body


def _transport(script):
    """request(url, params) that plays `script` in order: an int is an answer with that
    HTTP status, an exception is raised, a Resp is returned, a dict is a 200 with that
    body. Asking for more than the script holds fails the test."""
    calls: list[tuple] = []
    steps = list(script)

    def request(url, params):
        calls.append((url, dict(params)))
        assert steps, "the fetcher asked again after the script ended"
        step = steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, Resp):
            return step
        return Resp(step) if isinstance(step, int) else Resp(200, step)

    return request, calls


def _wait_lines(capsys) -> list[str]:
    return [ln for ln in capsys.readouterr().out.splitlines() if "[wait]" in ln]


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


# ── the fetcher: _get ─────────────────────────────────────────────────────────

def test_get_waits_out_429_5xx_timeouts_and_dropped_connections(slept, capsys):
    """One refused request used to be one skipped market. Each failure of the passing
    kind is now waited out - 1, 2, 4, 8, 16 s - and announced, one line per wait."""
    import requests
    request, calls = _transport([
        Resp(429, reason="Too Many Requests"), 503,
        requests.ReadTimeout("Read timed out. (read timeout=20)"),
        requests.ConnectionError("('Connection aborted.', RemoteDisconnected())"),
        requests.exceptions.ChunkedEncodingError("Connection broken: IncompleteRead"),
        {"markets": [1, 2, 3]}])
    assert hb._get("https://x/markets", {"limit": 5}, request=request) == {"markets": [1, 2, 3]}
    assert len(calls) == 6 and all(c == ("https://x/markets", {"limit": 5}) for c in calls)
    assert slept == [1, 2, 4, 8, 16]
    lines = _wait_lines(capsys)
    assert len(lines) == 5                                   # a waiting command never looks hung
    assert "HTTP 429 Too Many Requests" in lines[0] and "in 1s (try 2 of 8)" in lines[0]
    assert "HTTP 503" in lines[1] and "ReadTimeout" in lines[2] and "in 16s" in lines[4]


def test_get_gives_up_after_eight_tries_and_raises_the_last_error(slept, capsys):
    """About a minute and a half of patience (1+2+4+8+16+30+30 s), then the caller gets
    the last error itself, status and all, to print and to act on."""
    import requests
    request, calls = _transport([429] * 7 + [Resp(503, reason="Service Unavailable")])
    with pytest.raises(requests.HTTPError) as err:
        hb._get("https://x/markets", request=request)
    assert len(calls) == hb.RETRY_TRIES == 8
    assert slept == [1, 2, 4, 8, 16, 30, 30] and sum(slept) == 91
    assert len(_wait_lines(capsys)) == 7
    assert hb.http_status(err.value) == 503 and hb.is_transient(err.value)
    assert hb.describe_error(err.value) == "HTTP 503 Service Unavailable"


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 422])
def test_get_does_not_retry_an_answer_that_is_final(status, slept, capsys):
    """A market that is really gone answers 404 every time: retrying it would cost a
    minute and a half per market. Only 429, 5xx, timeouts and dropped connections wait."""
    import requests
    request, calls = _transport([status])
    with pytest.raises(requests.HTTPError) as err:
        hb._get("https://x/candlesticks", request=request)
    assert len(calls) == 1 and slept == [] and _wait_lines(capsys) == []
    assert hb.http_status(err.value) == status and not hb.is_transient(err.value)


def test_get_does_not_retry_an_unreadable_answer(slept):
    class NotJson(Resp):
        def json(self):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

    request, calls = _transport([NotJson(200)])
    with pytest.raises(ValueError):
        hb._get("https://x/markets", request=request)
    assert len(calls) == 1 and slept == []


def test_get_honours_retry_after_as_seconds_or_a_date_within_a_cap(slept, capsys):
    """Retry-After replaces the usual step when the server asks for longer - as a number
    or as an HTTP date - but an hour-long one must not park the run, and one that asks
    for less than the usual step (or is unreadable) never makes the retries faster."""
    now = 1_795_000_000.0                                    # the PC clock, held still
    from email.utils import formatdate
    request, calls = _transport([
        Resp(429, headers={"Retry-After": "7"}),                          # try 1: step 1 s
        Resp(503, headers={"retry-after": formatdate(now + 12, usegmt=True)}),   # step 2 s
        Resp(429, headers={"Retry-After": "3600"}),                       # step 4 s
        Resp(429, headers={"Retry-After": "0"}),                          # step 8 s
        Resp(429, headers={"Retry-After": formatdate(now - 500, usegmt=True)}),  # 16 s
        Resp(429, headers={"Retry-After": "soon"}),                       # step 30 s
        {"ok": 1}])
    assert hb._get("https://x/markets", request=request, now=lambda: now) == {"ok": 1}
    assert slept == [7, 12, 60, 8, 16, 30]
    lines = _wait_lines(capsys)
    assert "in 7s as the server asked" in lines[0] and "in 12s as the server asked" in lines[1]
    assert "in 60s (the server asked for 3600s - capped)" in lines[2]
    assert "asked" not in lines[3] and "asked" not in lines[5]


def test_retry_after_parsing():
    now = 1_795_000_000.0
    from email.utils import formatdate
    assert hb.parse_retry_after("5") == 5.0 and hb.parse_retry_after(" 1.5 ") == 1.5
    assert hb.parse_retry_after(30) == 30.0 and hb.parse_retry_after("-3") == 0.0
    assert hb.parse_retry_after(formatdate(now + 45, usegmt=True), now) == 45.0
    assert hb.parse_retry_after(formatdate(now - 45, usegmt=True), now) == 0.0   # never negative
    for junk in (None, "", "   ", "soon", "inf", "nan", "1e999", "Wed, 99 Foo 2026"):
        assert hb.parse_retry_after(junk, now) is None, junk
    assert hb.RETRY_AFTER_CAP_S == 60 and hb.RETRY_CAP_S == 30


def test_which_failures_are_worth_waiting_out():
    import requests
    ex = requests.exceptions

    def http(status):
        return requests.HTTPError("x", response=Resp(status))

    for e in (http(429), http(500), http(502), http(503), http(504),
              ex.Timeout("t"), ex.ReadTimeout("t"), ex.ConnectTimeout("t"),
              ex.ConnectionError("c"), ex.SSLError("s"), ex.ChunkedEncodingError("c"),
              TimeoutError("t"), ConnectionResetError("r"), ConnectionRefusedError("r")):
        assert hb.is_transient(e), repr(e)
    for e in (http(400), http(401), http(403), http(404), ValueError("v"), KeyError("k"),
              ex.JSONDecodeError("m", "doc", 0), ex.InvalidURL("u"), RuntimeError("r"),
              requests.HTTPError("no response attached")):
        assert not hb.is_transient(e), repr(e)


def test_a_warning_names_the_status_or_the_cause_not_the_url():
    import requests
    assert hb.describe_error(requests.HTTPError(
        "429 Client Error: Too Many Requests for url: https://api/x?start_ts=1",
        response=Resp(429, reason="Too Many Requests"))) == "HTTP 429 Too Many Requests"
    assert hb.describe_error(requests.HTTPError("x", response=Resp(404))) == "HTTP 404"

    class Fake(Exception):                       # an injected fetcher's own error class
        status = 429

    assert hb.http_status(Fake("refused")) == 429 and hb.describe_error(Fake("refused")) == "HTTP 429"
    wrapped = requests.ConnectionError(
        "HTTPSConnectionPool(host='api.elections.kalshi.com', port=443): Max retries "
        "exceeded with url: /trade-api/v2/markets?limit=1000 (Caused by "
        "NameResolutionError(\"HTTPSConnection(host='api.elections.kalshi.com', port=443): "
        "Failed to resolve 'api.elections.kalshi.com' ([Errno 11001] getaddrinfo failed)\"))")
    assert hb.describe_error(wrapped) == (
        "ConnectionError: NameResolutionError(\"Failed to resolve "
        "'api.elections.kalshi.com' ([Errno 11001] getaddrinfo failed)\")")
    assert hb.describe_error(TimeoutError()) == "TimeoutError"
    assert hb.http_status(ValueError("no status here")) is None


def test_the_commands_default_fetcher_is_the_patient_one():
    import inspect
    import sleeve_scan as ss
    for fn in (hb.fetch_settled_markets, hb.fetch_candles, hb.cmd_probe, hb.cmd_run,
               ss.cmd_probe, ss.cmd_discover, ss.cmd_backfill):
        assert inspect.signature(fn).parameters["get"].default is hb._get, fn.__name__


# ── the backfill loop: cmd_run ────────────────────────────────────────────────

def _refused(status=429):
    import requests
    return requests.HTTPError(f"{status} Error", response=Resp(status, reason="Too Many Requests"
                                                               if status == 429 else ""))


def _run_api(series_markets: dict, fail: dict | None = None):
    """get() for cmd_run: /markets lists `series_markets[series]` (a list of tickers);
    candles come back as one candle each. `fail` maps a series (listing) or a ticker
    (candles) to the exception raised for it. Every request is logged in order."""
    fail = fail or {}
    log: list[str] = []

    def get(url, params=None):
        if url.endswith("/markets"):
            s = params["series_ticker"]
            log.append(f"list {s}")
            if s in fail:
                raise fail[s]
            return {"markets": [{"ticker": tk, "result": "yes",
                                 "open_time": "2026-11-16T10:00:00Z",
                                 "close_time": "2026-11-16T10:15:00Z"}
                                for tk in series_markets[s]], "cursor": None}
        tk = url.split("/")[-2]
        log.append(tk)
        if tk in fail:
            raise fail[tk]
        return {"candlesticks": [{"end_period_ts": 1, "price": 50}]}

    return get, log


def test_run_counts_paces_and_reports_a_market_it_could_not_fetch(tmp_path, monkeypatch,
                                                                  slept, capsys):
    """A given-up request used to print '[warn] <ticker>: <url>', skip the pause and end
    in 'done.' with exit 0. Now it names the status, is paced like any request, is
    counted per series, and the run returns 1 so a re-run is asked for."""
    monkeypatch.setattr(hb, "OUT", tmp_path / "bf.jsonl")
    get, log = _run_api({"KXA": ["A-1", "A-2", "A-3"]}, fail={"A-2": _refused(429)})
    assert hb.cmd_run(["KXA"], 100, get=get) == 1
    out = capsys.readouterr().out
    assert "[warn] A-2: HTTP 429 Too Many Requests - not fetched" in out
    assert len(slept) == 3                    # one pause per candle request, failed or not
    assert "KXA" in out and "2 fetched, 1 NOT fetched" in out and "NOT COMPLETE" in out
    stored = {json.loads(x)["ticker"] for x in (tmp_path / "bf.jsonl").read_text().splitlines()}
    assert stored == {"A-1", "A-3"}
    log.clear()                               # the re-run fetches only what is missing
    get2, log2 = _run_api({"KXA": ["A-1", "A-2", "A-3"]})
    assert hb.cmd_run(["KXA"], 100, get=get2) == 0
    assert log2 == ["list KXA", "A-2"] and "COMPLETE" in capsys.readouterr().out


def test_run_survives_a_failed_listing_and_goes_on_to_the_next_series(tmp_path, monkeypatch,
                                                                       slept, capsys):
    """One refused listing call used to end a multi-series run in a traceback."""
    monkeypatch.setattr(hb, "OUT", tmp_path / "bf.jsonl")
    get, log = _run_api({"KXA": ["A-1"], "KXB": ["B-1"], "KXC": ["C-1"]},
                        fail={"KXB": _refused(503)})
    assert hb.cmd_run(["KXA", "KXB", "KXC"], 100, get=get) == 1
    assert log == ["list KXA", "A-1", "list KXB", "list KXC", "C-1"]
    out = capsys.readouterr().out
    assert "KXB: [warn] could not list its settled markets (HTTP 503) - series skipped" in out
    assert "NOT fetched - its markets could not be listed (HTTP 503)" in out
    assert "1 whole series not fetched" in out


def test_run_stops_after_a_few_given_up_requests_in_a_row(tmp_path, monkeypatch, slept,
                                                          capsys):
    """With a minute and a half of patience per request, pressing on through an outage
    would hold the run for days. GIVE_UP_AFTER in a row stop it; the rest are reported."""
    monkeypatch.setattr(hb, "OUT", tmp_path / "bf.jsonl")
    tks = [f"A-{j}" for j in range(10)]
    down = {tk: _refused(503) for tk in tks[2:]}
    get, log = _run_api({"KXA": tks, "KXB": ["B-1"]}, fail=down)
    assert hb.cmd_run(["KXA", "KXB"], 100, get=get) == 1
    assert log == ["list KXA"] + tks[:2 + hb.GIVE_UP_AFTER]          # nothing after the stop
    out = capsys.readouterr().out
    assert "[stop] 3 requests in a row were given up on" in out
    assert "2 fetched, 8 NOT fetched" in out
    assert "NOT started - the run stopped before it" in out


def test_a_final_answer_breaks_the_streak_and_does_not_stop_the_run(tmp_path, monkeypatch,
                                                                     slept, capsys):
    """A 404 is a market that is gone, not an outage: counted, but the run goes on."""
    monkeypatch.setattr(hb, "OUT", tmp_path / "bf.jsonl")
    tks = [f"A-{j}" for j in range(6)]
    get, log = _run_api({"KXA": tks}, fail={tk: _refused(404) for tk in tks[:5]})
    assert hb.cmd_run(["KXA"], 100, get=get) == 1
    assert log == ["list KXA"] + tks and "[stop]" not in capsys.readouterr().out


def test_close_run_exit_codes():
    clean = dict(hb.new_tally("KXA"), listed=True, todo=2, fetched=2)
    nothing = dict(hb.new_tally("KXB"), listed=True)
    assert hb.close_run([clean, nothing]) == 0
    assert hb.close_run([clean, dict(hb.new_tally("KXC"), error="HTTP 429")]) == 1
    assert hb.close_run([clean, hb.new_tally("KXD")]) == 1               # never started
    short = dict(hb.new_tally("KXE"), listed=True, todo=2, fetched=1, missing=[{"ticker": "x"}])
    assert hb.close_run([short]) == 1
    capped = dict(hb.new_tally("KXF"), listed=True, todo=15, fetched=15, capped=1,
                  capped_events=1)
    assert hb.close_run([capped], cap=15) == 0            # the cap is the design, not a fault


def test_run_writes_each_market_and_its_candles_in_one_write(tmp_path, monkeypatch, slept):
    """Same contract as sleeve_scan backfill: a market row, which the resume reads as
    'done', is written together with all its candles or not at all."""
    import lib.jsonl_io as jio
    monkeypatch.setattr(hb, "OUT", tmp_path / "bf.jsonl")
    writes: list[list[str]] = []

    def record(path, lines):
        lines = list(lines)
        writes.append(lines)
        return jio.append_lines(path, lines)

    monkeypatch.setattr(hb, "append_lines", record)
    get, _ = _run_api({"KXA": ["A-1", "A-2"]})
    assert hb.cmd_run(["KXA"], 100, get=get) == 0
    assert [[json.loads(x)["t"] for x in w] for w in writes] == [["market", "candle"]] * 2


def test_run_never_glues_a_new_row_onto_a_torn_one(tmp_path, monkeypatch, slept):
    out = tmp_path / "bf.jsonl"
    monkeypatch.setattr(hb, "OUT", out)
    out.write_text('{"t":"market","ticker":"A-0","res', encoding="utf-8")
    get, _ = _run_api({"KXA": ["A-1"]})
    assert hb.cmd_run(["KXA"], 100, get=get) == 0
    assert hb._seen_tickers(out) == {"A-1"}
