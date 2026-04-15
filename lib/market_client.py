"""
Unified Market Client — abstract interface for all prediction market platforms.

Every platform client (Kalshi, Polymarket, Manifold) implements this interface.
The rest of the codebase only talks to MarketClient, never to platform-specific APIs.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal


@dataclass
class MarketInfo:
    """Normalized market data across all platforms."""
    market_id: str
    platform: str
    question: str
    description: str
    category: str
    status: Literal["open", "closed", "resolved"]
    yes_price: float              # 0.00 - 1.00
    no_price: float               # 0.00 - 1.00
    volume_24h: float             # USD
    total_volume: float           # USD lifetime
    resolution_date: str          # ISO 8601
    resolution_source: str        # Who decides the outcome
    outcome: str | None = None    # YES/NO if resolved
    url: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class OrderResult:
    """Normalized order response."""
    order_id: str
    platform: str
    market_id: str
    side: str                     # YES or NO
    price: float
    quantity: int
    status: str                   # filled, partial, pending, rejected, cancelled
    filled_quantity: int = 0
    filled_price: float = 0.0
    fees: float = 0.0
    extra: dict = field(default_factory=dict)


@dataclass
class PositionInfo:
    """Normalized position data."""
    position_id: str
    platform: str
    market_id: str
    question: str
    side: str                     # YES or NO
    quantity: int
    avg_price: float              # Average entry price
    current_price: float          # Current market price
    unrealized_pnl: float
    category: str = ""
    resolution_date: str = ""
    extra: dict = field(default_factory=dict)


class MarketClient(ABC):
    """Abstract base class for prediction market platform clients."""

    @property
    @abstractmethod
    def platform_name(self) -> str:
        """Return the platform identifier (kalshi, polymarket, manifold)."""
        ...

    @property
    @abstractmethod
    def fee_rate(self) -> float:
        """Return the platform's fee rate on profit (e.g., 0.07 for Kalshi 7%)."""
        ...

    @abstractmethod
    def get_markets(
        self,
        category: str | None = None,
        status: str = "open",
        limit: int = 100,
    ) -> list[MarketInfo]:
        """Fetch available markets, optionally filtered."""
        ...

    @abstractmethod
    def get_market(self, market_id: str) -> MarketInfo:
        """Get details for a specific market."""
        ...

    @abstractmethod
    def get_orderbook(self, market_id: str) -> dict:
        """Get the current orderbook for a market.

        Returns:
            {"bids": [{"price": float, "quantity": int}, ...],
             "asks": [{"price": float, "quantity": int}, ...]}
        """
        ...

    @abstractmethod
    def place_order(
        self,
        market_id: str,
        side: Literal["YES", "NO"],
        price: float,
        quantity: int,
        order_type: Literal["limit", "market"] = "limit",
    ) -> OrderResult:
        """Place an order on the market."""
        ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """Cancel a pending order. Returns True if successful."""
        ...

    @abstractmethod
    def cancel_all_orders(self) -> int:
        """Cancel all pending orders. Returns count cancelled."""
        ...

    @abstractmethod
    def get_positions(self) -> list[PositionInfo]:
        """Get all open positions."""
        ...

    @abstractmethod
    def get_balance(self) -> float:
        """Get available balance in USD."""
        ...

    @abstractmethod
    def close_position(self, market_id: str, side: str) -> OrderResult:
        """Close a position by selling at market."""
        ...

    def close_all_positions(self) -> int:
        """Close all open positions. Returns count closed."""
        positions = self.get_positions()
        closed = 0
        for pos in positions:
            try:
                self.close_position(pos.market_id, pos.side)
                closed += 1
            except Exception:
                continue
        return closed


def get_client(platform: str) -> MarketClient:
    """Factory function to get a market client by platform name."""
    if platform == "kalshi":
        from lib.kalshi_client import KalshiClient
        return KalshiClient()
    elif platform == "polymarket":
        from lib.polymarket_client import PolymarketClient
        return PolymarketClient()
    elif platform == "manifold":
        from lib.manifold_client import ManifoldClient
        return ManifoldClient()
    else:
        raise ValueError(f"Unknown platform: {platform}")


def get_active_clients() -> list[MarketClient]:
    """Get clients for all enabled platforms from settings."""
    from pathlib import Path
    import yaml

    config_path = Path(__file__).parent.parent / "config" / "settings.yaml"
    with open(config_path, "r") as f:
        settings = yaml.safe_load(f)

    clients = []
    for platform, cfg in settings.get("platforms", {}).items():
        if cfg.get("enabled", False):
            try:
                clients.append(get_client(platform))
            except Exception:
                continue

    return clients
