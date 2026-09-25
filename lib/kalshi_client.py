"""
Kalshi Client — CFTC-regulated prediction market.

Fully legal for US users. 7% fee on profit (only charged on winning trades).
Uses the official kalshi-python SDK.

API docs: https://trading-api.readme.io/reference/
"""

import os
import time
from datetime import datetime, timezone
from typing import Literal

from dotenv import load_dotenv

from tradingcore.audit import log_event
from lib.market_client import MarketClient, MarketInfo, OrderResult, PositionInfo

load_dotenv()

# Category mapping from Kalshi's categories to our unified categories
KALSHI_CATEGORY_MAP = {
    "Politics": "politics",
    "Economics": "economics",
    "Climate and Weather": "weather",
    "Crypto": "crypto",
    "Sports": "sports",
    "Entertainment": "entertainment",
    "Tech": "ai_tech",
    "World": "geopolitical",
    "Science": "science",
    "Culture": "entertainment",
    "Finance": "economics",
}


class KalshiClient(MarketClient):
    """Kalshi prediction market client."""

    def __init__(self):
        self._api_key = os.getenv("KALSHI_API_KEY", "")
        self._private_key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
        self._client = None
        self._last_call = 0.0
        self._min_interval = 1.0  # Rate limit: ~60 calls/min

        if self._api_key:
            self._init_client()

    def _init_client(self):
        """Initialize the Kalshi SDK client."""
        try:
            import kalshi_python
            config = kalshi_python.Configuration()
            config.host = "https://api.elections.kalshi.com/trade-api/v2"
            self._client = kalshi_python.ApiInstance(
                email="",
                password="",
                configuration=config,
            )
            # If using API key auth
            if self._api_key:
                self._client.api_key = self._api_key
        except ImportError:
            log_event("market_client", "kalshi_sdk_missing", {
                "error": "kalshi-python not installed. Run: pip install kalshi-python",
            }, result="failed")
        except Exception as e:
            log_event("market_client", "kalshi_init_failed", {
                "error": str(e),
            }, result="failed")

    def _rate_limit(self):
        """Enforce rate limiting between API calls."""
        elapsed = time.time() - self._last_call
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_call = time.time()

    def _map_category(self, kalshi_category: str) -> str:
        return KALSHI_CATEGORY_MAP.get(kalshi_category, "other")

    @property
    def platform_name(self) -> str:
        return "kalshi"

    @property
    def fee_rate(self) -> float:
        return 0.07  # 7% of profit on winning trades

    def get_markets(
        self,
        category: str | None = None,
        status: str = "open",
        limit: int = 100,
    ) -> list[MarketInfo]:
        """Fetch available Kalshi markets."""
        if not self._client:
            return []

        self._rate_limit()
        try:
            # Kalshi API: GET /markets
            params = {"limit": limit, "status": status}
            if category:
                params["series_ticker"] = category

            response = self._client.get_markets(**params)
            markets = []

            for m in response.get("markets", []):
                yes_price = m.get("yes_bid", 0) / 100.0  # Kalshi uses cents
                no_price = m.get("no_bid", 0) / 100.0

                info = MarketInfo(
                    market_id=m.get("ticker", ""),
                    platform="kalshi",
                    question=m.get("title", ""),
                    description=m.get("subtitle", ""),
                    category=self._map_category(m.get("category", "")),
                    status="open" if m.get("status") == "active" else m.get("status", ""),
                    yes_price=yes_price,
                    no_price=no_price,
                    volume_24h=m.get("volume_24h", 0),
                    total_volume=m.get("volume", 0),
                    resolution_date=m.get("close_time", ""),
                    resolution_source=m.get("settlement_source_url", ""),
                    url=f"https://kalshi.com/markets/{m.get('ticker', '')}",
                    extra={"series_ticker": m.get("series_ticker", "")},
                )
                markets.append(info)

            return markets

        except Exception as e:
            log_event("market_client", "kalshi_get_markets_failed", {
                "error": str(e),
            }, result="failed")
            return []

    def get_market(self, market_id: str) -> MarketInfo:
        """Get details for a specific Kalshi market."""
        if not self._client:
            raise ConnectionError("Kalshi client not initialized")

        self._rate_limit()
        m = self._client.get_market(market_id)
        market_data = m.get("market", m)

        yes_price = market_data.get("yes_bid", 0) / 100.0
        no_price = market_data.get("no_bid", 0) / 100.0

        return MarketInfo(
            market_id=market_data.get("ticker", market_id),
            platform="kalshi",
            question=market_data.get("title", ""),
            description=market_data.get("subtitle", ""),
            category=self._map_category(market_data.get("category", "")),
            status="open" if market_data.get("status") == "active" else market_data.get("status", ""),
            yes_price=yes_price,
            no_price=no_price,
            volume_24h=market_data.get("volume_24h", 0),
            total_volume=market_data.get("volume", 0),
            resolution_date=market_data.get("close_time", ""),
            resolution_source=market_data.get("settlement_source_url", ""),
            url=f"https://kalshi.com/markets/{market_id}",
        )

    def get_orderbook(self, market_id: str) -> dict:
        if not self._client:
            return {"bids": [], "asks": []}

        self._rate_limit()
        try:
            ob = self._client.get_market_orderbook(market_id)
            # Kalshi's orderbook is two lists of resting BIDS in cents:
            # `yes` = bids to buy YES, `no` = bids to buy NO. A YES ask is
            # the synthetic complement of a NO bid: yes_ask = 1 - no_bid.
            # Taking the raw no-bid price as a yes ask is wrong.
            return {
                "bids": [{"price": b[0] / 100.0, "quantity": b[1]} for b in ob.get("yes", [])],
                "asks": [{"price": (100 - a[0]) / 100.0, "quantity": a[1]} for a in ob.get("no", [])],
            }
        except Exception:
            return {"bids": [], "asks": []}

    def place_order(
        self,
        market_id: str,
        side: Literal["YES", "NO"],
        price: float,
        quantity: int,
        order_type: Literal["limit", "market"] = "limit",
    ) -> OrderResult:
        if not self._client:
            raise ConnectionError("Kalshi client not initialized")

        self._rate_limit()

        kalshi_side = "yes" if side == "YES" else "no"
        price_cents = int(price * 100)

        log_event("market_client", "kalshi_place_order", {
            "market_id": market_id,
            "side": side,
            "price": price,
            "quantity": quantity,
            "order_type": order_type,
        }, result="pending")

        try:
            order_params = {
                "ticker": market_id,
                "action": "buy",
                "side": kalshi_side,
                "count": quantity,
                "type": order_type,
            }
            if order_type == "limit":
                order_params["yes_price"] = price_cents if side == "YES" else None
                order_params["no_price"] = price_cents if side == "NO" else None

            response = self._client.create_order(**order_params)
            order = response.get("order", response)

            # Kalshi reports `remaining_count` (the UNFILLED quantity),
            # not the filled amount. Filled = count - remaining.
            remaining = order.get("remaining_count", quantity)
            filled = max(0, quantity - remaining)
            return OrderResult(
                order_id=order.get("order_id", ""),
                platform="kalshi",
                market_id=market_id,
                side=side,
                price=price,
                quantity=quantity,
                status=order.get("status", "pending"),
                filled_quantity=filled,
                filled_price=price,
            )

        except Exception as e:
            log_event("market_client", "kalshi_order_failed", {
                "market_id": market_id,
                "error": str(e),
            }, result="failed")
            raise

    def cancel_order(self, order_id: str) -> bool:
        if not self._client:
            return False
        self._rate_limit()
        try:
            self._client.cancel_order(order_id)
            return True
        except Exception:
            return False

    def cancel_all_orders(self) -> int:
        if not self._client:
            return 0
        self._rate_limit()
        try:
            response = self._client.batch_cancel_orders()
            return response.get("count", 0)
        except Exception:
            return 0

    def get_positions(self) -> list[PositionInfo]:
        if not self._client:
            return []
        self._rate_limit()
        try:
            response = self._client.get_positions()
            positions = []
            for p in response.get("market_positions", []):
                market_id = p.get("ticker", "")
                qty_yes = p.get("position", 0)
                qty_no = p.get("position", 0)  # Kalshi reports net position

                if qty_yes > 0:
                    positions.append(PositionInfo(
                        position_id=f"kalshi-{market_id}-yes",
                        platform="kalshi",
                        market_id=market_id,
                        question=p.get("title", ""),
                        side="YES",
                        quantity=abs(qty_yes),
                        avg_price=p.get("average_price", 0) / 100.0,
                        current_price=p.get("market_price", 0) / 100.0,
                        unrealized_pnl=p.get("total_traded", 0) / 100.0,
                    ))
                elif qty_no < 0:
                    positions.append(PositionInfo(
                        position_id=f"kalshi-{market_id}-no",
                        platform="kalshi",
                        market_id=market_id,
                        question=p.get("title", ""),
                        side="NO",
                        quantity=abs(qty_no),
                        avg_price=p.get("average_price", 0) / 100.0,
                        current_price=p.get("market_price", 0) / 100.0,
                        unrealized_pnl=p.get("total_traded", 0) / 100.0,
                    ))

            return positions
        except Exception:
            return []

    def get_balance(self) -> float:
        if not self._client:
            return 0.0
        self._rate_limit()
        try:
            response = self._client.get_balance()
            return response.get("balance", 0) / 100.0  # Cents to dollars
        except Exception:
            return 0.0

    def close_position(self, market_id: str, side: str) -> OrderResult:
        """Close a position by selling at market."""
        positions = self.get_positions()
        for pos in positions:
            if pos.market_id == market_id and pos.side == side:
                # Sell the opposite side to close
                return self.place_order(
                    market_id=market_id,
                    side=side,
                    price=0.01 if side == "YES" else 0.99,  # Market order equivalent
                    quantity=pos.quantity,
                    order_type="market",
                )
        raise ValueError(f"No {side} position found for {market_id}")
