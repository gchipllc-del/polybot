"""
Polymarket Client — largest prediction market by volume.

Built on Polygon L2 (Ethereum). Uses CLOB (Central Limit Order Book) for trading.
Two APIs: Gamma (market metadata) and CLOB (order execution).

~2% fee on winnings. Gray area for US users — use at your own risk.

SDK: py-clob-client
API docs: https://docs.polymarket.com/
"""

import json as _json
import os
import time
from typing import Literal

import requests
from dotenv import load_dotenv

from tradingcore.audit import log_event
from lib.market_client import MarketClient, MarketInfo, OrderResult, PositionInfo

load_dotenv()


def _parse_json_list(raw, *, default=None) -> list:
    """Tolerant parse of a Gamma-API field that's a JSON-encoded list.

    Polymarket's Gamma API serializes ``outcomePrices`` and
    ``clobTokenIds`` as JSON strings inside the JSON response. They're
    occasionally malformed (we've seen empty strings, truncated values,
    and the rare `null`). This helper always returns a list — falls
    back to ``default`` on any parse failure so callers never crash on
    a flaky API response. Pattern from predmarket's `_normalize_outcomes`.
    """
    if isinstance(raw, list):
        return raw
    if not isinstance(raw, str) or not raw:
        return list(default) if default is not None else []
    try:
        parsed = _json.loads(raw)
        return parsed if isinstance(parsed, list) else (list(default) if default is not None else [])
    except (_json.JSONDecodeError, TypeError, ValueError):
        return list(default) if default is not None else []

GAMMA_API_BASE = "https://gamma-api.polymarket.com"
CLOB_API_BASE = "https://clob.polymarket.com"


class PolymarketClient(MarketClient):
    """Polymarket prediction market client."""

    def __init__(self):
        self._private_key = os.getenv("POLY_PRIVATE_KEY", "")
        self._api_key = os.getenv("POLY_API_KEY", "")
        self._api_secret = os.getenv("POLY_API_SECRET", "")
        self._api_passphrase = os.getenv("POLY_API_PASSPHRASE", "")
        self._clob_client = None
        self._last_call = 0.0
        self._min_interval = 0.5  # Rate limit

        if self._private_key:
            self._init_client()

    def _init_client(self):
        """Initialize the py-clob-client."""
        try:
            from py_clob_client.client import ClobClient

            self._clob_client = ClobClient(
                host=CLOB_API_BASE,
                key=self._private_key,
                chain_id=137,  # Polygon
            )

            # Derive or set API credentials
            if self._api_key:
                self._clob_client.set_api_creds({
                    "api_key": self._api_key,
                    "api_secret": self._api_secret,
                    "api_passphrase": self._api_passphrase,
                })
            else:
                self._clob_client.create_or_derive_api_creds()

        except ImportError:
            log_event("market_client", "polymarket_sdk_missing", {
                "error": "py-clob-client not installed. Run: pip install py-clob-client",
            }, result="failed")
        except Exception as e:
            log_event("market_client", "polymarket_init_failed", {
                "error": str(e),
            }, result="failed")

    def _rate_limit(self):
        elapsed = time.time() - self._last_call
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_call = time.time()

    def _gamma_get(self, endpoint: str, params: dict | None = None) -> dict | list:
        """Make a GET request to the Gamma API."""
        self._rate_limit()
        url = f"{GAMMA_API_BASE}{endpoint}"
        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            log_event("market_client", "gamma_request_failed", {
                "endpoint": endpoint,
                "error": str(e),
            }, result="failed")
            return []

    def _infer_category(self, tags: list[str], question: str) -> str:
        """Infer category from Polymarket tags and question text."""
        text = " ".join(tags + [question]).lower()
        if any(w in text for w in ["election", "president", "congress", "vote", "political"]):
            return "politics"
        if any(w in text for w in ["fed", "inflation", "gdp", "cpi", "economic", "rate"]):
            return "economics"
        if any(w in text for w in ["weather", "temperature", "hurricane", "storm"]):
            return "weather"
        if any(w in text for w in ["bitcoin", "ethereum", "crypto", "btc", "eth"]):
            return "crypto"
        if any(w in text for w in ["nfl", "nba", "mlb", "sport", "game", "match"]):
            return "sports"
        if any(w in text for w in ["ai", "artificial intelligence", "openai", "gpt"]):
            return "ai_tech"
        return "other"

    @property
    def platform_name(self) -> str:
        return "polymarket"

    @property
    def fee_rate(self) -> float:
        return 0.02  # ~2% on winnings

    def get_markets(
        self,
        category: str | None = None,
        status: str = "open",
        limit: int = 100,
    ) -> list[MarketInfo]:
        """Fetch markets from Polymarket's Gamma API."""
        params = {
            "limit": limit,
            "active": "true" if status == "open" else "false",
            "closed": "false" if status == "open" else "true",
        }

        data = self._gamma_get("/markets", params)
        if not isinstance(data, list):
            data = data.get("markets", []) if isinstance(data, dict) else []

        markets = []
        for m in data:
            tags = m.get("tags", []) or []
            question = m.get("question", "")
            market_category = self._infer_category(tags, question)

            if category and market_category != category:
                continue

            # Polymarket prices are 0.00 - 1.00. outcomePrices may arrive as
            # a JSON-encoded string from Gamma — _parse_json_list normalizes.
            outcomes = _parse_json_list(m.get("outcomePrices"), default=[0.5, 0.5])
            yes_price = float(outcomes[0]) if len(outcomes) > 0 else 0.5
            no_price = float(outcomes[1]) if len(outcomes) > 1 else 1.0 - yes_price

            info = MarketInfo(
                market_id=m.get("conditionId", m.get("id", "")),
                platform="polymarket",
                question=question,
                description=m.get("description", ""),
                category=market_category,
                status="open" if m.get("active") else "closed",
                yes_price=yes_price,
                no_price=no_price,
                volume_24h=float(m.get("volume24hr", 0) or 0),
                total_volume=float(m.get("volume", 0) or 0),
                resolution_date=m.get("endDate", ""),
                resolution_source=m.get("resolutionSource", "UMA Oracle"),
                url=f"https://polymarket.com/event/{m.get('slug', '')}",
                extra={
                    "condition_id": m.get("conditionId", ""),
                    # Pre-parsed token_ids so order/orderbook paths don't
                    # re-fetch + re-parse (was a real bug in place_order
                    # where the parse had no try/except).
                    "token_ids": _parse_json_list(m.get("clobTokenIds")),
                    "tags": tags,
                },
            )
            markets.append(info)

        return markets

    def get_market(self, market_id: str) -> MarketInfo:
        data = self._gamma_get(f"/markets/{market_id}")
        if not data:
            raise ValueError(f"Market {market_id} not found on Polymarket")

        m = data if isinstance(data, dict) else {}
        tags = m.get("tags", []) or []
        question = m.get("question", "")

        outcomes = _parse_json_list(m.get("outcomePrices"), default=[0.5, 0.5])
        yes_price = float(outcomes[0]) if len(outcomes) > 0 else 0.5
        no_price = float(outcomes[1]) if len(outcomes) > 1 else 1.0 - yes_price

        return MarketInfo(
            market_id=m.get("conditionId", market_id),
            platform="polymarket",
            question=question,
            description=m.get("description", ""),
            category=self._infer_category(tags, question),
            status="open" if m.get("active") else "closed",
            yes_price=yes_price,
            no_price=no_price,
            volume_24h=float(m.get("volume24hr", 0) or 0),
            total_volume=float(m.get("volume", 0) or 0),
            resolution_date=m.get("endDate", ""),
            resolution_source=m.get("resolutionSource", "UMA Oracle"),
            url=f"https://polymarket.com/event/{m.get('slug', '')}",
            extra={
                "condition_id": m.get("conditionId", ""),
                "token_ids": _parse_json_list(m.get("clobTokenIds")),
            },
        )

    def get_orderbook(self, market_id: str) -> dict:
        if not self._clob_client:
            return {"bids": [], "asks": []}

        self._rate_limit()
        try:
            # Need the token_id for the CLOB. token_ids is pre-parsed
            # by get_markets/get_market into a list — but accept a raw
            # string too (defensive for any caller that bypassed those).
            market = self.get_market(market_id)
            token_ids = _parse_json_list(market.extra.get("token_ids"))
            if not token_ids:
                return {"bids": [], "asks": []}

            # Get orderbook for YES token
            ob = self._clob_client.get_order_book(token_ids[0])
            return {
                "bids": [{"price": float(b.get("price", 0)), "quantity": int(float(b.get("size", 0)))}
                         for b in ob.get("bids", [])],
                "asks": [{"price": float(a.get("price", 0)), "quantity": int(float(a.get("size", 0)))}
                         for a in ob.get("asks", [])],
            }
        except Exception as e:
            log_event("market_client", "polymarket_orderbook_failed", {
                "market_id": market_id,
                "error": str(e),
            }, result="failed")
            return {"bids": [], "asks": []}

    def place_order(
        self,
        market_id: str,
        side: Literal["YES", "NO"],
        price: float,
        quantity: int,
        order_type: Literal["limit", "market"] = "limit",
    ) -> OrderResult:
        if not self._clob_client:
            raise ConnectionError("Polymarket CLOB client not initialized")

        self._rate_limit()

        log_event("market_client", "polymarket_place_order", {
            "market_id": market_id,
            "side": side,
            "price": price,
            "quantity": quantity,
        }, result="pending")

        try:
            from py_clob_client.order_builder.constants import BUY

            market = self.get_market(market_id)
            # token_ids is normally a list (pre-parsed by get_market); the
            # tolerant parser is belt-and-braces for any path that stashed
            # a raw string. This was the real bug — the previous code did
            # a bare json.loads here with no try/except, so a malformed
            # API payload crashed place_order.
            token_ids = _parse_json_list(market.extra.get("token_ids"))
            if len(token_ids) < 2:
                log_event("market_client", "polymarket_place_order_failed", {
                    "reason": "missing_token_ids",
                    "market_id": market_id,
                }, result="failed")
                return OrderResult(
                    success=False, order_id="", platform="polymarket",
                    market_id=market_id, side=side, quantity=quantity,
                    fill_price=0.0, error="missing token_ids for market",
                )

            # YES = token_ids[0], NO = token_ids[1]
            token_id = token_ids[0] if side == "YES" else token_ids[1]

            order = self._clob_client.create_and_post_order({
                "token_id": token_id,
                "price": price,
                "size": quantity,
                "side": BUY,
            })

            return OrderResult(
                order_id=order.get("orderID", order.get("id", "")),
                platform="polymarket",
                market_id=market_id,
                side=side,
                price=price,
                quantity=quantity,
                status=order.get("status", "pending"),
            )

        except Exception as e:
            log_event("market_client", "polymarket_order_failed", {
                "market_id": market_id,
                "error": str(e),
            }, result="failed")
            raise

    def cancel_order(self, order_id: str) -> bool:
        if not self._clob_client:
            return False
        self._rate_limit()
        try:
            self._clob_client.cancel(order_id)
            return True
        except Exception:
            return False

    def cancel_all_orders(self) -> int:
        if not self._clob_client:
            return 0
        self._rate_limit()
        try:
            self._clob_client.cancel_all()
            return -1  # Polymarket doesn't return count
        except Exception:
            return 0

    def get_positions(self) -> list[PositionInfo]:
        # Polymarket positions require querying the subgraph or CLOB API
        if not self._clob_client:
            return []

        self._rate_limit()
        try:
            # This depends on the specific py-clob-client version
            positions_data = self._clob_client.get_positions()
            positions = []
            for p in positions_data:
                positions.append(PositionInfo(
                    position_id=f"poly-{p.get('asset_id', '')}",
                    platform="polymarket",
                    market_id=p.get("condition_id", ""),
                    question=p.get("title", ""),
                    side="YES" if p.get("outcome", "") == "Yes" else "NO",
                    quantity=int(float(p.get("size", 0))),
                    avg_price=float(p.get("avg_price", 0)),
                    current_price=float(p.get("cur_price", 0)),
                    unrealized_pnl=float(p.get("pnl", 0)),
                ))
            return positions
        except Exception:
            return []

    def get_balance(self) -> float:
        # Polymarket balance is USDC on Polygon — requires on-chain query
        if not self._clob_client:
            return 0.0
        try:
            # Balance check depends on the wallet integration
            return 0.0  # TODO: Implement USDC balance check via Web3
        except Exception:
            return 0.0

    def close_position(self, market_id: str, side: str) -> OrderResult:
        """Close by selling position at market."""
        positions = self.get_positions()
        for pos in positions:
            if pos.market_id == market_id and pos.side == side:
                # Sell at a very aggressive price to simulate market order
                sell_price = 0.01 if side == "YES" else 0.99
                return self.place_order(
                    market_id=market_id,
                    side=side,
                    price=sell_price,
                    quantity=pos.quantity,
                    order_type="market",
                )
        raise ValueError(f"No {side} position found for {market_id}")
