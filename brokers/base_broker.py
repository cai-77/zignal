"""
Abstract BrokerBase — the single interface all broker implementations must satisfy.

Swap brokers by changing `active_broker` in config.yaml.
All code above this layer (strategies, risk manager, order manager) depends only
on this interface, never on Alpaca or IBKR specifics.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class OrderSide(Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class OrderStatus(Enum):
    PENDING = "pending"
    OPEN = "open"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass
class TradeOrder:
    symbol: str
    qty: float
    side: OrderSide
    order_type: OrderType
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    order_id: Optional[str] = None
    client_order_id: Optional[str] = None
    status: OrderStatus = OrderStatus.PENDING
    filled_qty: float = 0.0
    filled_avg_price: Optional[float] = None


@dataclass
class Position:
    symbol: str
    qty: float
    avg_entry_price: float
    current_price: float = 0.0
    unrealized_pl: float = 0.0
    unrealized_pl_pct: float = 0.0
    side: str = "long"  # "long" | "short"


@dataclass
class AccountInfo:
    cash: float
    portfolio_value: float
    buying_power: float
    equity: float
    daily_pl: float = 0.0
    unrealized_pl: float = 0.0


class BrokerBase(ABC):
    """
    All broker implementations (Alpaca, IBKR, simulated backtest) derive from this.

    The public interface is intentionally minimal — just what strategies and the
    order manager actually need.  Each concrete broker handles API specifics
    internally, exposing only these methods.
    """

    @abstractmethod
    def connect(self) -> None:
        """Establish connection / authenticate with the broker."""

    @abstractmethod
    def disconnect(self) -> None:
        """Cleanly close the broker connection."""

    @abstractmethod
    def is_connected(self) -> bool:
        """Return True if the broker connection is active."""

    @abstractmethod
    def get_account(self) -> AccountInfo:
        """Return current account cash, equity, and P&L."""

    @abstractmethod
    def get_positions(self) -> List[Position]:
        """Return all currently open positions."""

    @abstractmethod
    def get_position(self, symbol: str) -> Optional[Position]:
        """Return the open position for *symbol*, or None if flat."""

    @abstractmethod
    def place_order(self, order: TradeOrder) -> TradeOrder:
        """Submit *order* to the broker; return the order with broker-assigned ID."""

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """Cancel *order_id*. Return True if the cancellation was accepted."""

    @abstractmethod
    def get_open_orders(self) -> List[TradeOrder]:
        """Return all orders that are still open / pending fill."""

    @abstractmethod
    def get_latest_price(self, symbol: str) -> float:
        """Return the most recent trade price for *symbol*."""

    @abstractmethod
    def is_market_open(self) -> bool:
        """Return True if the primary market is currently open for regular trading."""

    # ------------------------------------------------------------------
    # Convenience helpers (implemented here — concrete classes inherit)
    # ------------------------------------------------------------------

    def get_portfolio_value(self) -> float:
        return self.get_account().portfolio_value

    def get_cash(self) -> float:
        return self.get_account().cash
