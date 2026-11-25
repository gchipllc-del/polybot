#!/usr/bin/env python3
r"""history_backfill — months of Kalshi 15-min market history via public endpoints.

Source: the ccxt Kalshi integration (MIT, verified at source level in our repo scan)
wraps GET /series/{series_ticker}/markets/{ticker}/candlesticks with period_interval
(1 = 1-minute) and start_ts/end_ts, and GET /markets supports status=settled with cursor
pagination. Both are PUBLIC (no auth). If candle retention reaches back months, this
multiplies our dataset ~20x for free: per-minute yes_bid/yes_ask paths + settlement
results for every expired KXBTC15M/KXETH15M window since the series listed.

GATED, because retention depth is the one thing the scan could not verify remotely:
  probe    fetch ONE old settled market's candles and show exactly what came back.
           Run this FIRST; if it returns empty for old tickers, stop - no backfill exists.
  run      full backfill: enumerate settled markets (paginated), pull 1-min candles per
           window, append market+candle rows to data/history_backfill.jsonl (resumable -
           already-fetched tickers are skipped).
  export   convert backfilled candles into stage0-format obs+settle rows
           (data/stage0_backfill.jsonl) so edge_analysis / maker_replay can be pointed
           at deep history via the STAGE0_LOG env var. Kept SEPARATE from the live
           collector's file on purpose: candle-derived quotes are minute-OHLC
           approximations, not our snapshot quality - never silently mix provenances.
  selftest fixtures only, no network.

  py scripts/history_backfill.py probe
  py scripts/history_backfill.py run --series KXBTC15M --max-markets 500
  py scripts/history_backfill.py export

Per-venue screening: scope BOTH files by env var so venues never mix -
  $env:BACKFILL_LOG="data\backfill_weather.jsonl"
  $env:BACKFILL_EXPORT="data\stage0_weather.jsonl"
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = ["KXBTC15M", "KXETH15M"]
OUT = Path(os.environ.get("BACKFILL_LOG") or (ROOT / "data" / "history_backfill.jsonl"))
EXPORT = Path(os.environ.get("BACKFILL_EXPORT")
              or (ROOT / "data" / "stage0_backfill.jsonl"))
SLEEP_S = 0.35              # polite pacing; these are public endpoints
LIST_PAGE = 1000            # markets per listing page: the most /markets serves (a
                            # smaller page from the server still works - the loop
                            # follows the cursor)
RETRY_TRIES = 8             # attempts per request before the caller sees the failure
RETRY_BASE_S = 1.0          # waits between them: 1, 2, 4, 8, 16, 30, 30 s - 91 s in all
RETRY_CAP_S = 30.0
RETRY_AFTER_CAP_S = 60.0    # a server's Retry-After is honoured up to this, never longer
GIVE_UP_AFTER = 3           # requests given up on IN A ROW before a backfill run stops


# ── the fetcher: one request, waited out when the failure is the passing kind ──

def http_status(e: BaseException) -> int | None:
    """The HTTP status an error carries, or None (requests.HTTPError keeps it on
    .response; urllib's HTTPError and injected fakes keep it on the error itself)."""
    for obj in (getattr(e, "response", None), e):
        for name in ("status_code", "status", "code"):
            v = getattr(obj, name, None)
            if isinstance(v, int) and 100 <= v <= 599:
                return v
    return None


def is_transient(e: BaseException) -> bool:
    """True for the failures worth waiting out: HTTP 429, any 5xx, a timeout, a dropped
    or refused connection. Every other answer is final: a 404 for a market that is gone
    must not cost a minute and a half of retries."""
    status = http_status(e)
    if status is not None:
        return status == 429 or status >= 500
    if isinstance(e, (ConnectionError, TimeoutError)):
        return True
    try:
        import requests
    except ImportError:
        return False
    return isinstance(e, (requests.ConnectionError, requests.Timeout,
                          requests.exceptions.ChunkedEncodingError))


def describe_error(e: BaseException) -> str:
    """What a warning line says about a failed request: 'HTTP 429 Too Many Requests', or
    the error's class and its cause without the connection-pool preamble."""
    status = http_status(e)
    if status is not None:
        reason = " ".join(str(getattr(getattr(e, "response", None), "reason", None)
                              or "").split())
        return f"HTTP {status}" + (f" {reason[:40]}" if reason else "")
    text = " ".join(str(e).split())
    if "(Caused by " in text:                  # requests wraps urllib3's own wrapper:
        text = text.rsplit("(Caused by ", 1)[1]            # keep the cause, not the URL
        text = text[:-1] if text.endswith(")") else text
    text = re.sub(r"<[^<>]*object at 0x[0-9a-fA-F]+>:\s*|HTTPS?Connection\([^()]*\):\s*",
                  "", text)
    return type(e).__name__ + (f": {text[:140]}" if text else "")


def parse_retry_after(value, now: float | None = None) -> float | None:
    """A Retry-After header as seconds from now. It comes as a number ('5') or as an HTTP
    date ('Wed, 25 Nov 2026 12:00:30 GMT'). None when absent or unreadable; a date
    already past (or a PC clock running ahead) reads as 0, never negative."""
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    try:
        secs = float(text)
    except ValueError:
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
        if when is None:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        try:
            secs = when.timestamp() - (time.time() if now is None else now)
        except (OverflowError, OSError, ValueError):
            return None
    return max(0.0, secs) if math.isfinite(secs) else None


def _header(e: BaseException, name: str):
    headers = getattr(getattr(e, "response", None), "headers", None)
    try:
        for k, v in (headers or {}).items():
            if str(k).lower() == name.lower():
                return v
    except Exception:  # noqa: BLE001 - an odd fake must not turn a retry into a crash
        pass
    return None


def retry_wait(e: BaseException, attempt: int, now: float | None = None) -> tuple[float, str]:
    """(seconds to wait after failed attempt number `attempt`, a note for the wait line).
    The usual step doubles from RETRY_BASE_S up to RETRY_CAP_S. A Retry-After from the
    server replaces it when it asks for longer, up to RETRY_AFTER_CAP_S - an hour-long
    Retry-After, or a date read against a PC clock that is off, must not park the run."""
    step = min(RETRY_CAP_S, RETRY_BASE_S * 2 ** (attempt - 1))
    asked = parse_retry_after(_header(e, "Retry-After"), now)
    if asked is None or asked <= step:
        return step, ""
    if asked <= RETRY_AFTER_CAP_S:
        return asked, " as the server asked"
    return RETRY_AFTER_CAP_S, f" (the server asked for {asked:.0f}s - capped)"


def _request(url: str, params: dict):
    """ONE request, no retry - the seam the tests replace."""
    import requests
    return requests.get(url, params=params, timeout=20)


def _get(url: str, params: dict | None = None, *, tries: int | None = None,
         request=None, sleep=None, now=None) -> dict:
    """GET JSON from a public endpoint, waiting out the failures that pass.

    Retried, RETRY_TRIES attempts in all: HTTP 429, any 5xx, a timeout, a dropped or
    refused connection (is_transient). Between attempts it waits 1, 2, 4, 8, 16, 30, 30 s
    - about a minute and a half of patience - or what the server's Retry-After asks for
    (retry_wait). One line is printed per wait, so a command that is waiting never looks
    hung. Before this, one 429 was one skipped market and the next request left at once.

    NOT retried: every other answer. A 404 for a market that is gone is final.

    When the tries run out the LAST error is raised unchanged: callers print it
    (describe_error) and decide what to do next (is_transient tells them it was the
    waiting-out kind). request / sleep / now are test seams; now is a clock function."""
    tries = RETRY_TRIES if tries is None else max(1, int(tries))
    request = request or _request
    for attempt in range(1, tries + 1):
        try:
            r = request(url, params or {})
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001 - sorted into retry / raise just below
            if attempt >= tries or not is_transient(e):
                raise
            wait, note = retry_wait(e, attempt, None if now is None else now())
            shown = f"{wait:.0f}" if wait >= 10 or float(wait).is_integer() else f"{wait:.1f}"
            print(f"  [wait] {describe_error(e)} - trying again in {shown}s{note} "
                  f"(try {attempt + 1} of {tries})", flush=True)
            (sleep or time.sleep)(wait)
    raise RuntimeError("unreachable")              # the loop returns or raises


# ── what a backfill run owes its reader: pacing, a count, a stop, a closing line ──

class Streak:
    """Requests in a row that were given up on after waiting (is_transient failures).
    One is a market for the next run to fetch. GIVE_UP_AFTER of them back to back is
    Kalshi or the network being down, and the run stops there: at a minute and a half of
    patience per request, pressing on would hold a 12,000-market run for days. An answer
    that is final (a 404) is not an outage and breaks the streak, as any success does."""

    def __init__(self, limit: int | None = None):
        self.n = 0
        self.limit = GIVE_UP_AFTER if limit is None else limit
        self.stopped = False

    def ok(self) -> None:
        self.n = 0

    def failed(self, e: BaseException) -> bool:
        """Count one failed request. True from the moment the run must stop."""
        self.n = self.n + 1 if is_transient(e) else 0
        if self.n >= self.limit and not self.stopped:
            self.stopped = True
            print(f"  [stop] {self.n} requests in a row were given up on - Kalshi or the "
                  f"network is not answering. Stopping the run here.", flush=True)
        return self.stopped


def new_tally(series: str) -> dict:
    """What a backfill run did for one series; close_run prints one line from each.
    listed: its settled markets were listed. error: why they were not. todo: markets the
    run set out to fetch. missing: those it did not fetch (market dicts). capped /
    capped_events: markets left out on purpose by a per-event cap. untimed: markets
    with no open or close time, which no run can fetch."""
    return {"series": series, "listed": False, "error": None, "todo": 0, "fetched": 0,
            "missing": [], "capped": 0, "capped_events": 0, "untimed": 0}


def close_run(tallies: list[dict], event_of=None, cap: int | None = None,
              hint: str = "") -> int:
    """The closing lines of a backfill run: one per series, then the verdict. Returns the
    exit code - 0 only when every market the run set out to fetch is on file. A market
    given up on, a series that could not be listed and a run that stopped early all
    return 1, so a chained command does not carry on as if the history were whole.
    Markets left out by the per-event cap are counted here but are not a failure."""
    print("SUMMARY")
    short = whole = 0
    for t in tallies:
        if t["error"]:
            whole += 1
            line = f"NOT fetched - its markets could not be listed ({t['error']})"
        elif not t["listed"]:
            whole += 1
            line = "NOT started - the run stopped before it"
        else:
            short += len(t["missing"])
            bits = [f"{t['fetched']} fetched" if t["todo"]
                    else "nothing to fetch (already on file)"]
            if t["missing"]:
                where = (f" in {len({event_of(m) for m in t['missing']})} event(s)"
                         if event_of else "")
                bits.append(f"{len(t['missing'])} NOT fetched{where}")
            if t["untimed"]:
                bits.append(f"{t['untimed']} with no open/close time (cannot be fetched)")
            if t["capped"]:
                bits.append(f"{t['capped']} left out by the {cap}-per-event cap "
                            f"in {t['capped_events']} event(s)")
            line = ", ".join(bits)
        print(f"  {t['series']:22} {line}")
    if not (short or whole):
        print("COMPLETE: every market this run set out to fetch is on file.")
        return 0
    what = " and ".join(x for x in (f"{short} market(s)" if short else "",
                                    f"{whole} whole series" if whole else "") if x)
    print(f"NOT COMPLETE: {what} not fetched. Re-run the same command - it resumes and "
          f"fetches only what is missing.{hint}")
    return 1


def _iso_to_ts(iso: str) -> int | None:
    try:
        return int(datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return None


def fetch_settled_markets(series: str, get=_get, max_markets: int = 10000) -> list[dict]:
    """All settled markets for a series, cursor-paginated, oldest data included."""
    out, cursor = [], None
    while len(out) < max_markets:
        params = {"series_ticker": series, "status": "settled", "limit": LIST_PAGE}
        if cursor:
            params["cursor"] = cursor
        data = get(f"{KALSHI}/markets", params)
        ms = data.get("markets", [])
        out.extend(ms)
        cursor = data.get("cursor")
        if not cursor or not ms:
            break
        time.sleep(SLEEP_S)
    return out[:max_markets]


def fetch_candles(series: str, ticker: str, open_iso: str, close_iso: str,
                  get=_get) -> list[dict]:
    """1-minute candles across the market's life. Params per the verified ccxt shape."""
    start = _iso_to_ts(open_iso)
    end = _iso_to_ts(close_iso)
    if start is None or end is None:
        return []
    data = get(f"{KALSHI}/series/{series}/markets/{ticker}/candlesticks",
               {"period_interval": 1, "start_ts": start, "end_ts": end})
    return data.get("candlesticks", []) or data.get("candles", []) or []


def _seen_tickers(path: Path) -> set:
    seen = set()
    if not path.exists():
        return seen
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("t") == "market":
            seen.add(d.get("ticker"))
    return seen


def _dollars(c: dict, *names):
    """Candle price fields arrive as nested OHLC dicts or scalars, cents or dollars,
    under bare or _dollars names (ccxt-verified variability). Returns close-ish value."""
    for n in names:
        v = c.get(f"{n}_dollars", c.get(n))
        if isinstance(v, dict):
            v = v.get("close_dollars", v.get("close"))
        if v is None:
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        return v if v <= 1.0 else v / 100.0
    return None


def cmd_probe(get=_get) -> int:
    print("=" * 72)
    print("BACKFILL PROBE - does Kalshi retain candles for long-settled markets?")
    print("=" * 72)
    # RETENTION LADDER. The first probe version enumerated newest-first and stopped at
    # its own 1200-market cap, so "oldest enumerated" (13 days) measured the CAP, not
    # retention. This asks the question directly: request settled markets CLOSED BEFORE
    # 30/60/90/150 days ago via max_close_ts and try candles on one from each rung.
    now_ts = int(time.time())
    for series in SERIES[:1]:                     # one series is enough for the ladder
        for days in (30, 60, 90, 150):
            try:
                data = get(f"{KALSHI}/markets",
                           {"series_ticker": series, "status": "settled", "limit": 5,
                            "max_close_ts": now_ts - days * 86400})
                ms = data.get("markets", [])
            except Exception as e:  # noqa: BLE001
                print(f"{series} closed>{days}d ago: enumeration FAILED ({str(e)[:80]})")
                continue
            if not ms:
                print(f"{series} closed>{days}d ago: no markets returned "
                      f"(series may be younger, or param unsupported)")
                continue
            m = ms[0]
            try:
                cs = fetch_candles(series, m["ticker"], m.get("open_time"),
                                   m.get("close_time"), get=get)
                print(f"{series} closed>{days}d ago: {m['ticker']} "
                      f"(close {str(m.get('close_time'))[:10]}) -> {len(cs)} candles")
            except Exception as e:  # noqa: BLE001
                print(f"{series} closed>{days}d ago: {m['ticker']} candle fetch "
                      f"FAILED ({str(e)[:80]})")
            time.sleep(SLEEP_S)
    print()
    for series in SERIES:
        try:
            ms = fetch_settled_markets(series, get=get, max_markets=1200)
        except Exception as e:  # noqa: BLE001
            print(f"{series}: settled-market enumeration FAILED: {e}")
            continue
        if not ms:
            print(f"{series}: no settled markets returned")
            continue
        ms.sort(key=lambda m: m.get("close_time") or "")
        oldest, newest = ms[0], ms[-1]
        print(f"{series}: {len(ms)} settled markets enumerated "
              f"({str(oldest.get('close_time'))[:10]} .. {str(newest.get('close_time'))[:10]})")
        for label, m in (("OLDEST", oldest), ("NEWEST", newest)):
            tk = m.get("ticker")
            try:
                cs = fetch_candles(series, tk, m.get("open_time"), m.get("close_time"),
                                   get=get)
            except Exception as e:  # noqa: BLE001
                print(f"  {label} {tk}: candle fetch FAILED: {e}")
                continue
            print(f"  {label} {tk} (result={m.get('result')}): {len(cs)} one-minute candles")
            if cs:
                sample = {k: cs[0][k] for k in list(cs[0])[:6]}
                print(f"    sample fields: {sample}")
        print()
    print("READ: if the OLDEST market returns candles, deep backfill is real - run `run`.")
    print("If oldest returns 0 but newest works, retention is shallow; backfill only helps")
    print("a little. If both fail, the endpoint shape changed - paste this output back.")
    return 0


def cmd_run(series_list: list[str], max_markets: int, get=_get) -> int:
    """Backfill every settled market of each series (resumable). A request the fetcher
    gives up on leaves that market for the next run: it is warned about with its HTTP
    status, counted, and paced like any other. GIVE_UP_AFTER of them in a row stop the
    run; a series whose listing fails is skipped, not fatal. Returns 1 unless every
    market it set out to fetch is on file (close_run)."""
    OUT.parent.mkdir(parents=True, exist_ok=True)
    seen = _seen_tickers(OUT)
    print(f"backfill -> {OUT} (resumable; {len(seen)} tickers already stored)")
    total_c = 0
    tallies = [new_tally(series) for series in series_list]
    streak = Streak()
    for t in tallies:
        if streak.stopped:
            break                                  # the rest stay "not started"
        series = t["series"]
        try:
            ms = fetch_settled_markets(series, get=get, max_markets=max_markets)
        except Exception as e:  # noqa: BLE001
            t["error"] = describe_error(e)
            print(f"{series}: [warn] could not list its settled markets "
                  f"({t['error']}) - series skipped")
            time.sleep(SLEEP_S)
            streak.failed(e)
            continue
        streak.ok()
        todo = [m for m in ms if m.get("ticker") and m["ticker"] not in seen
                and m.get("result") in ("yes", "no")]
        t["listed"], t["todo"] = True, len(todo)
        print(f"{series}: {len(ms)} settled, {len(todo)} to fetch")
        with open(OUT, "a", encoding="utf-8") as f:
            for i, m in enumerate(todo):
                tk = m["ticker"]
                try:
                    cs = fetch_candles(series, tk, m.get("open_time"),
                                       m.get("close_time"), get=get)
                except Exception as e:  # noqa: BLE001
                    t["missing"].append(m)
                    print(f"  [warn] {tk}: {describe_error(e)} - not fetched")
                    time.sleep(SLEEP_S)            # a failure is paced like a success
                    if streak.failed(e):
                        t["missing"].extend(todo[i + 1:])     # never asked for
                        break
                    continue
                streak.ok()
                f.write(json.dumps({"t": "market", "series": series, "ticker": tk,
                                    "result": m.get("result"),
                                    "open_time": m.get("open_time"),
                                    "close_time": m.get("close_time"),
                                    "floor_strike": m.get("floor_strike"),
                                    "volume": m.get("volume")},
                                   separators=(",", ":")) + "\n")
                for c in cs:
                    f.write(json.dumps({"t": "candle", "series": series, "ticker": tk,
                                        **c}, separators=(",", ":")) + "\n")
                total_c += len(cs)
                t["fetched"] += 1
                if (i + 1) % 25 == 0:
                    print(f"  {i+1}/{len(todo)} markets, {total_c} candles")
                time.sleep(SLEEP_S)
    print(f"total new candles: {total_c}")
    return close_run(tallies)


def cmd_export() -> int:
    """Backfill rows -> stage0-format obs+settle, provenance-tagged, separate file."""
    if not OUT.exists():
        print(f"nothing to export - run backfill first ({OUT} missing)")
        return 1
    markets: dict[str, dict] = {}
    candles: dict[str, list] = {}
    for line in OUT.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("t") == "market":
            markets[d["ticker"]] = d
        elif d.get("t") == "candle":
            candles.setdefault(d["ticker"], []).append(d)
    n_obs = n_set = 0
    with open(EXPORT, "w", encoding="utf-8") as f:
        for tk, m in markets.items():
            close_ts = _iso_to_ts(m.get("close_time"))
            if close_ts is None:
                continue
            for c in sorted(candles.get(tk, []),
                            key=lambda c: c.get("end_period_ts") or 0):
                ts = c.get("end_period_ts")
                if ts is None:
                    continue
                yb = _dollars(c, "yes_bid")
                ya = _dollars(c, "yes_ask")
                if yb is None and ya is None:
                    continue
                mins_left = (close_ts - float(ts)) / 60.0
                if mins_left <= 0:
                    continue
                f.write(json.dumps({
                    "t": "obs", "src": "backfill",
                    "ts": datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat(),
                    "series": m.get("series"), "ticker": tk,
                    "strike": m.get("floor_strike"),
                    "mins_left": round(mins_left, 2),
                    "yes_bid": yb, "yes_ask": ya,
                    "no_bid": (round(1 - ya, 4) if ya is not None else None),
                    "no_ask": (round(1 - yb, 4) if yb is not None else None),
                }, separators=(",", ":")) + "\n")
                n_obs += 1
            f.write(json.dumps({"t": "settle", "src": "backfill", "ticker": tk,
                                "series": m.get("series"), "result": m.get("result")},
                               separators=(",", ":")) + "\n")
            n_set += 1
    print(f"exported {n_obs} obs + {n_set} settles -> {EXPORT}")
    print("Analyze deep history WITHOUT touching the live file, e.g. (PowerShell):")
    print('  $env:STAGE0_LOG="data\\stage0_backfill.jsonl"; py scripts\\edge_analysis.py')
    print("NOTE: no-side quotes here are derived as 1 - yes quote (candles carry yes only)")
    print("      and quotes are minute-OHLC closes, coarser than the live collector.")
    return 0


def _selftest() -> int:
    calls = []

    def fake_get(url, params=None):
        calls.append((url, params or {}))
        if url.endswith("/markets"):
            cur = (params or {}).get("cursor")
            if cur is None:
                return {"markets": [{"ticker": "KXBTC15M-A", "result": "yes",
                                     "open_time": "2026-06-01T12:00:00Z",
                                     "close_time": "2026-06-01T12:15:00Z",
                                     "floor_strike": 64000.0, "volume": 50}],
                        "cursor": "c2"}
            return {"markets": [{"ticker": "KXBTC15M-B", "result": "no",
                                 "open_time": "2026-06-01T12:15:00Z",
                                 "close_time": "2026-06-01T12:30:00Z",
                                 "floor_strike": 64100.0, "volume": 10}],
                    "cursor": None}
        if "candlesticks" in url:
            assert (params or {}).get("period_interval") == 1
            return {"candlesticks": [
                {"end_period_ts": 1780315200,
                 "yes_bid": {"close": 82}, "yes_ask": {"close": 86}},
                {"end_period_ts": 1780315260,
                 "yes_bid": {"close": 84}, "yes_ask": {"close": 88}},
            ]}
        raise AssertionError(url)

    ms = fetch_settled_markets("KXBTC15M", get=fake_get)
    assert [m["ticker"] for m in ms] == ["KXBTC15M-A", "KXBTC15M-B"], ms   # pagination
    cs = fetch_candles("KXBTC15M", "KXBTC15M-A",
                       "2026-06-01T12:00:00Z", "2026-06-01T12:15:00Z", get=fake_get)
    assert len(cs) == 2
    # cents -> dollars via nested close
    assert _dollars(cs[0], "yes_bid") == 0.82 and _dollars(cs[0], "yes_ask") == 0.86
    # flat dollar variants too
    assert _dollars({"yes_bid_dollars": 0.5}, "yes_bid") == 0.5
    assert _dollars({"yes_bid": 45}, "yes_bid") == 0.45

    # run + export round-trip through temp files
    import tempfile
    global OUT, EXPORT
    old_out, old_exp = OUT, EXPORT
    with tempfile.TemporaryDirectory() as td:
        OUT, EXPORT = Path(td) / "bf.jsonl", Path(td) / "exp.jsonl"
        try:
            cmd_run(["KXBTC15M"], max_markets=10, get=fake_get)
            rows = [json.loads(l) for l in OUT.read_text().splitlines()]
            assert sum(1 for r in rows if r["t"] == "market") == 2
            assert sum(1 for r in rows if r["t"] == "candle") == 4
            # resume: second run fetches nothing new
            n_before = len(rows)
            cmd_run(["KXBTC15M"], max_markets=10, get=fake_get)
            assert len(OUT.read_text().splitlines()) == n_before
            cmd_export()
            exp = [json.loads(l) for l in EXPORT.read_text().splitlines()]
            obs = [r for r in exp if r["t"] == "obs"]
            setl = [r for r in exp if r["t"] == "settle"]
            assert setl and all(r["src"] == "backfill" for r in exp)
            assert obs and obs[0]["yes_bid"] == 0.82 and obs[0]["no_ask"] == 0.18
            assert all(o["mins_left"] > 0 for o in obs)
        finally:
            OUT, EXPORT = old_out, old_exp
    print("selftest OK")
    return 0


def main() -> int:
    args = sys.argv[1:]
    cmd = args[0] if args else "probe"
    if cmd == "selftest":
        return _selftest()
    if cmd == "probe":
        return cmd_probe()
    if cmd == "run":
        series = SERIES
        mm = 10000
        if "--series" in args:
            series = [args[args.index("--series") + 1]]
        if "--max-markets" in args:
            mm = int(args[args.index("--max-markets") + 1])
        return cmd_run(series, mm)
    if cmd == "export":
        return cmd_export()
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
