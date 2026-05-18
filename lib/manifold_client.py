"""
Manifold Client — play-money prediction market for paper trading.

Free, legal, no risk. Perfect for validating strategies before deploying real capital.
Uses Manifold's REST API.

API docs: https://docs.manifold.markets/api
"""

import os
import time
from datetime import datetime, timezone
from typing import Literal

import requests
from dotenv import load_dotenv

from tradingcore.audit import log_event
from lib.market_client import MarketClient, MarketInfo, OrderResult, PositionInfo

load_dotenv()

MANIFOLD_API_BASE = "https://api.manifold.markets/v0"


class ManifoldClient(MarketClient):
    """Manifold Markets client for paper trading."""

    def __init__(self):
        self._api_key = os.getenv("MANIFOLD_API_KEY", "")
        self._last_call = 0.0
        self._min_interval = 0.5

    def _rate_limit(self):
        elapsed = time.time() - self._last_call
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_call = time.time()

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Key {self._api_key}"
        return headers

    def _get(self, endpoint: str, params: dict | None = None):
        self._rate_limit()
        url = f"{MANIFOLD_API_BASE}{endpoint}"
        resp = requests.get(url, headers=self._headers(), params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def _post(self, endpoint: str, data: dict):
        self._rate_limit()
        url = f"{MANIFOLD_API_BASE}{endpoint}"
        resp = requests.post(url, headers=self._headers(), json=data, timeout=15)
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _normalize_resolution_date(close_time) -> str:
        """Convert Manifold closeTime (epoch ms int or str) to ISO 8601 string."""
        if not close_time:
            return ""
        try:
            if isinstance(close_time, (int, float)):
                return datetime.fromtimestamp(close_time / 1000, tz=timezone.utc).isoformat()
            if isinstance(close_time, str):
                # Already ISO? Return as-is.
                datetime.fromisoformat(close_time.replace("Z", "+00:00"))
                return close_time
        except (ValueError, TypeError, OSError):
            pass
        return ""

    def _infer_category(self, groups: list[str], question: str) -> str:
        text = " ".join(groups + [question]).lower()
        if any(w in text for w in ["election", "president", "congress", "vote", "political", "politics"]):
            return "politics"
        if any(w in text for w in ["fed", "inflation", "gdp", "cpi", "economic", "rate", "stock"]):
            return "economics"
        if any(w in text for w in ["weather", "temperature", "climate"]):
            return "weather"
        if any(w in text for w in ["bitcoin", "ethereum", "crypto"]):
            return "crypto"
        if any(w in text for w in ["nfl", "nba", "mlb", "sport"]):
            return "sports"
        if any(w in text for w in ["ai", "artificial intelligence", "openai", "gpt", "llm"]):
            return "ai_tech"
        return "other"

    @property
    def platform_name(self) -> str:
        return "manifold"

    @property
    def fee_rate(self) -> float:
        return 0.0  # Free (play money)

    def get_markets(
        self,
        category: str | None = None,
        status: str = "open",
        limit: int = 100,
    ) -> list[MarketInfo]:
        """Fetch markets from Manifold.

        Strategy: union two sorts so the candidate set spans both
        well-bonded markets (sort=liquidity) AND markets where humans are
        actively trading right now (sort=last-updated). The single-sort
        approach was missing all the fresh activity — markets get added to
        bonded liquidity once at creation but only show up in last-updated
        when they have live betting, which is exactly where mispricings live.
        """
        # Manifold caps /search-markets at 100 per call; pull both sorts in
        # parallel-equivalent order and dedupe by market_id.
        per_sort = min(limit, 100)
        markets_by_id: dict[str, MarketInfo] = {}

        for sort_mode in ("last-updated", "liquidity"):
            try:
                params = {"term": "", "limit": per_sort, "sort": sort_mode}
                if status == "open":
                    params["filter"] = "open"
                elif status == "resolved":
                    params["filter"] = "resolved"

                data = self._get("/search-markets", params)

                for m in data:
                    if m.get("outcomeType") != "BINARY":
                        continue

                    question = m.get("question", "")
                    groups = [g.get("name", "") for g in m.get("groups", [])]
                    market_category = self._infer_category(groups, question)

                    if category and market_category != category:
                        continue

                    market_id = m.get("id", "")
                    if not market_id or market_id in markets_by_id:
                        continue  # already pulled in the other sort

                    yes_price = m.get("probability", 0.5)
                    info = MarketInfo(
                        market_id=market_id,
                        platform="manifold",
                        question=question,
                        description=m.get("textDescription", "")[:500],
                        category=market_category,
                        status="open" if m.get("isResolved") is False else "resolved",
                        yes_price=yes_price,
                        no_price=1.0 - yes_price,
                        volume_24h=float(m.get("volume24Hours", 0) or 0),
                        total_volume=float(m.get("volume", 0) or 0),
                        resolution_date=self._normalize_resolution_date(m.get("closeTime")),
                        resolution_source="Manifold community",
                        outcome=m.get("resolution") if m.get("isResolved") else None,
                        url=m.get("url", ""),
                    )
                    markets_by_id[market_id] = info

            except Exception as e:
                log_event("market_client", "manifold_get_markets_failed", {
                    "error": str(e),
                    "sort_mode": sort_mode,
                }, result="failed")
                # Don't bail — try the other sort

        return list(markets_by_id.values())

    def get_market(self, market_id: str) -> MarketInfo:
        m = self._get(f"/market/{market_id}")

        question = m.get("question", "")
        groups = [g.get("name", "") for g in m.get("groups", [])]
        yes_price = m.get("probability", 0.5)

        return MarketInfo(
            market_id=m.get("id", market_id),
            platform="manifold",
            question=question,
            description=m.get("textDescription", "")[:500],
            category=self._infer_category(groups, question),
            status="open" if not m.get("isResolved") else "resolved",
            yes_price=yes_price,
            no_price=1.0 - yes_price,
            volume_24h=float(m.get("volume24Hours", 0) or 0),
            total_volume=float(m.get("volume", 0) or 0),
            resolution_date=self._normalize_resolution_date(m.get("closeTime")),
            resolution_source="Manifold community",
            outcome=m.get("resolution") if m.get("isResolved") else None,
            url=m.get("url", ""),
        )

    def get_orderbook(self, market_id: str) -> dict:
        # Manifold uses AMM, not orderbook — return simulated book from probability
        try:
            market = self.get_market(market_id)
            return {
                "bids": [{"price": market.yes_price - 0.01, "quantity": 100}],
                "asks": [{"price": market.yes_price + 0.01, "quantity": 100}],
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
        if not self._api_key:
            raise ConnectionError("Manifold API key not set")

        log_event("market_client", "manifold_place_order", {
            "market_id": market_id,
            "side": side,
            "price": price,
            "quantity": quantity,
        }, result="pending")

        try:
            # Manifold uses "mana" amounts, not contract quantities
            # Convert: amount = price * quantity (in mana)
            amount = price * quantity

            if order_type == "market":
                # Market order via /bet endpoint
                data = {
                    "contractId": market_id,
                    "outcome": side,
                    "amount": int(amount),
                }
                response = self._post("/bet", data)
            else:
                # Limit order
                data = {
                    "contractId": market_id,
                    "outcome": side,
                    "amount": int(amount),
                    "limitProb": price,
                }
                response = self._post("/bet", data)

            return OrderResult(
                order_id=response.get("betId", response.get("id", "")),
                platform="manifold",
                market_id=market_id,
                side=side,
                price=price,
                quantity=quantity,
                status="filled",
                filled_quantity=quantity,
                filled_price=price,
            )

        except Exception as e:
            log_event("market_client", "manifold_order_failed", {
                "market_id": market_id,
                "error": str(e),
            }, result="failed")
            raise

    def cancel_order(self, order_id: str) -> bool:
        if not self._api_key:
            return False
        try:
            self._post(f"/bet/cancel/{order_id}", {})
            return True
        except Exception:
            return False

    def cancel_all_orders(self) -> int:
        # Manifold doesn't have a batch cancel — would need to iterate
        return 0

    def get_positions(self) -> list[PositionInfo]:
        if not self._api_key:
            return []

        try:
            # Get current user's bets
            bets = self._get("/bets", {"limit": 100})
            # Aggregate by market
            positions_map: dict[str, PositionInfo] = {}

            for bet in bets:
                market_id = bet.get("contractId", "")
                if market_id not in positions_map:
                    positions_map[market_id] = PositionInfo(
                        position_id=f"manifold-{market_id}",
                        platform="manifold",
                        market_id=market_id,
                        question=bet.get("contractQuestion", ""),
                        side="YES" if bet.get("outcome") == "YES" else "NO",
                        quantity=0,
                        avg_price=0.0,
                        current_price=bet.get("probAfter", 0.5),
                        unrealized_pnl=0.0,
                    )
                pos = positions_map[market_id]
                pos.quantity += int(abs(bet.get("shares", 0)))

            return list(positions_map.values())

        except Exception:
            return []

    def get_balance(self) -> float:
        if not self._api_key:
            return 0.0
        try:
            me = self._get("/me")
            return float(me.get("balance", 0))
        except Exception:
            return 0.0

    def close_position(self, market_id: str, side: str) -> OrderResult:
        """Sell shares on Manifold."""
        positions = self.get_positions()
        for pos in positions:
            if pos.market_id == market_id:
                return self.place_order(
                    market_id=market_id,
                    side="NO" if side == "YES" else "YES",  # Sell by buying opposite
                    price=pos.current_price,
                    quantity=pos.quantity,
                    order_type="market",
                )
        raise ValueError(f"No position found for {market_id}")
