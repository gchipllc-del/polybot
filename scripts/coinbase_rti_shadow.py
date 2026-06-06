#!/usr/bin/env python3
"""
coinbase_rti_shadow.py

Tracks live BTC-USD from Coinbase's PUBLIC market data feed (no login, no API key)
and computes a trailing 60-second average that shadows how Kalshi settles its crypto
contracts (a one-minute average of CF Benchmarks RTI prices, once per second).

Notes / caveats:
  - This needs NO Coinbase account. The ticker channel is public.
  - Coinbase is one constituent of the CF Benchmarks index, not the whole thing,
    so this is a proxy. Expect a small basis vs the official RTI settlement value.
  - Kalshi applies a trimmed mean (drop top/bottom 20%) on certain markets; both the
    plain and trimmed averages are printed so you can compare.

Run:
    pip install websockets
    python3 scripts/coinbase_rti_shadow.py
"""

import asyncio
import json
import time
from collections import deque

import websockets

FEED_URL = "wss://ws-feed.exchange.coinbase.com"
PRODUCT = "BTC-USD"
WINDOW_SECONDS = 60          # Kalshi settlement window
TRIM_FRACTION = 0.20         # drop top/bottom 20% for the trimmed mean


def trimmed_mean(values, trim):
    """Mean after dropping the top and bottom `trim` fraction of samples."""
    if not values:
        return None
    ordered = sorted(values)
    k = int(len(ordered) * trim)
    core = ordered[k: len(ordered) - k] or ordered
    return sum(core) / len(core)


async def run():
    # (timestamp, price) samples kept only for the trailing window
    samples = deque()

    subscribe = {
        "type": "subscribe",
        "product_ids": [PRODUCT],
        "channels": ["ticker"],
    }

    print(f"Connecting to public Coinbase feed for {PRODUCT} (no login needed)...")
    async for ws in websockets.connect(FEED_URL, ping_interval=20):
        try:
            await ws.send(json.dumps(subscribe))
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") != "ticker" or "price" not in msg:
                    continue

                price = float(msg["price"])
                now = time.time()
                samples.append((now, price))

                # drop anything older than the window
                cutoff = now - WINDOW_SECONDS
                while samples and samples[0][0] < cutoff:
                    samples.popleft()

                prices = [p for _, p in samples]
                plain = sum(prices) / len(prices)
                trimmed = trimmed_mean(prices, TRIM_FRACTION)

                print(
                    f"[{time.strftime('%H:%M:%S')}] "
                    f"last ${price:,.2f} | "
                    f"{WINDOW_SECONDS}s avg ${plain:,.2f} | "
                    f"trimmed ${trimmed:,.2f} | "
                    f"n={len(prices)}"
                )
        except websockets.ConnectionClosed:
            print("Connection dropped, reconnecting...")
            continue


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nStopped.")
