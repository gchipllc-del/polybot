#!/usr/bin/env python3
"""Polybot MCP server — read-only tools.

Wraps polybot's CLI as MCP tools so Claude Code can query positions,
forecasts, calibration, and signals without executing trades.

All tools here are READ-ONLY. Destructive commands (trade, kill,
harvest live, wheel) are intentionally NOT exposed. If you later want
those tools, add them in a separate file (mcp_server_destructive.py)
so the read-only surface stays unambiguous.

Run:
    python mcp_server.py            # stdio transport (for Claude Code)

Wire up:
    .mcp.json in this repo registers this server with Claude Code
    automatically when you `cd` into polybot/ before launching the CLI.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

POLYBOT_ROOT = Path(__file__).parent.resolve()
PYTHON = "/Users/jesse/anaconda3/bin/python"
DEFAULT_TIMEOUT_S = 120
KRONOS_TIMEOUT_S = 240  # model warm-up can be slow on first call

mcp = FastMCP("polybot")


async def _run_cli(args: list[str], timeout: int = DEFAULT_TIMEOUT_S) -> str:
    """Invoke `python main.py <args>` from polybot's root and return stdout.

    Strips terminal control sequences by setting NO_COLOR / TERM=dumb so
    the LLM sees plain text. Raises RuntimeError on non-zero exit so the
    MCP layer surfaces failure to the caller.
    """
    env = {
        **os.environ,
        "NO_COLOR": "1",
        "TERM": "dumb",
        "PYTHONUNBUFFERED": "1",
    }
    proc = await asyncio.create_subprocess_exec(
        PYTHON,
        str(POLYBOT_ROOT / "main.py"),
        *args,
        cwd=str(POLYBOT_ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(
            f"polybot {' '.join(args)} timed out after {timeout}s"
        )
    if proc.returncode != 0:
        err = stderr.decode(errors="replace").strip()
        raise RuntimeError(
            f"polybot {' '.join(args)} exited {proc.returncode}: {err}"
        )
    return stdout.decode(errors="replace")


@mcp.tool()
async def status() -> str:
    """Show polybot's current open positions, bankroll, win rate, and circuit-breaker state.

    Read-only. Returns a Rich-rendered status panel as plain text.
    Equivalent to running `python main.py status` in the polybot repo.
    """
    return await _run_cli(["status"])


@mcp.tool()
async def scan() -> str:
    """Scan prediction markets and score candidates without executing trades.

    Read-only. Runs polybot's market scanner across Kalshi, Polymarket,
    Manifold, and Metaculus, returning candidate trades with scores,
    edge estimates, and Bayesian probability decompositions. Does NOT
    place orders.

    May take 30-120s depending on how many markets are active.
    """
    return await _run_cli(["scan"], timeout=180)


@mcp.tool()
async def calibrate() -> str:
    """Print the calibration report — Brier score, log loss, per-source accuracy.

    Read-only. Returns a structured calibration breakdown showing
    historical forecast accuracy across all signal sources (LLM, Kronos,
    news, base rates).
    """
    return await _run_cli(["calibrate"])


@mcp.tool()
async def forecast(market_id: str) -> str:
    """Run the full Bayesian forecaster on a specific market ID.

    Read-only. Aggregates LLM analysis, Kronos price forecast (if
    applicable), news sentiment, community consensus, and base rates
    into a single probability estimate. Returns reasoning + final
    number.

    Args:
        market_id: The platform-prefixed market identifier
            (e.g. 'kalshi:PRESELECT-26', 'manifold:abc123').
    """
    if not market_id or not isinstance(market_id, str):
        raise ValueError("market_id must be a non-empty string")
    return await _run_cli(["forecast", market_id], timeout=180)


@mcp.tool()
async def news(question: str) -> str:
    """Query news sentiment for a free-form question.

    Read-only. Searches NewsAPI, RSS feeds, and Reddit for relevant
    articles, scores sentiment 0.0-1.0 (bearish-to-bullish), and
    returns the top headlines with relevance scores.

    Args:
        question: The natural-language question to evaluate sentiment
            against (e.g. 'Will the Fed cut rates in December?').
    """
    if not question or not isinstance(question, str):
        raise ValueError("question must be a non-empty string")
    return await _run_cli(["news", question])


@mcp.tool()
async def kronos(ticker: str) -> str:
    """Run the Kronos zero-shot price forecaster on a ticker.

    Read-only. Loads the pretrained 102M-parameter Kronos transformer
    and predicts future OHLCV candlesticks from historical yfinance
    data. Returns direction, expected return, and confidence.

    First call may take 60-120s for model load on cold cache.

    Args:
        ticker: A yfinance-compatible ticker (e.g. 'AAPL', 'BTC-USD').
    """
    if not ticker or not isinstance(ticker, str):
        raise ValueError("ticker must be a non-empty string")
    return await _run_cli(["kronos", ticker], timeout=KRONOS_TIMEOUT_S)


@mcp.tool()
async def kronos_prob(ticker: str, target: float, direction: str = "above") -> str:
    """Estimate the probability a ticker's price crosses a target.

    Read-only. Runs N independent Kronos sample paths via Monte Carlo
    and returns the fraction that end above (or below) the target by
    horizon end.

    Args:
        ticker: yfinance ticker (e.g. 'SPY').
        target: Numeric price target.
        direction: 'above' or 'below'. Defaults to 'above'.
    """
    if direction not in ("above", "below"):
        raise ValueError("direction must be 'above' or 'below'")
    return await _run_cli(
        ["kronos-prob", ticker, str(target), direction],
        timeout=KRONOS_TIMEOUT_S,
    )


@mcp.tool()
async def arb_report() -> str:
    """Cross-platform arbitrage report — read-only.

    Read-only. Scans Kalshi vs Polymarket vs Manifold for the same
    underlying question priced differently. Returns potential arbitrage
    pairs with edge estimates. Does NOT execute any trades.
    """
    return await _run_cli(["arb"])


@mcp.tool()
async def harvest_dry() -> str:
    """Show what the mechanical near-resolution harvester would buy, without buying.

    Read-only. Runs the harvester in dry-run mode: lists markets the
    harvester would have purchased given current prices and rules.
    No orders are placed.
    """
    return await _run_cli(["harvest", "--dry-run"])


if __name__ == "__main__":
    # Stdio transport — FastMCP wires this up automatically.
    # When run by Claude Code via .mcp.json, this runs as a child process
    # and communicates over stdin/stdout in JSON-RPC.
    try:
        mcp.run()
    except KeyboardInterrupt:
        sys.exit(0)
