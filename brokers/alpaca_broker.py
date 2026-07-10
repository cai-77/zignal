"""
Alpaca broker implementation using the alpaca-py SDK.

Paper trading:  set base_url to https://paper-api.alpaca.markets
Live trading:   set base_url to https://api.alpaca.markets
"""

from datetime import datetime
from typing import List, Optional

import pytz

from brokers.base_broker import (
    AccountInfo,
    BrokerBase,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    TradeOrder,
)


class AlpacaBroker(BrokerBase):

    def __init__(self, config: dict):
        self._api_key = config["api_key"]
        self._secret = config["secret_key"]
        self._base_url = config.get("base_url", "https://paper-api.alpaca.markets")
        self._client = None       # TradingClient
        self._data_client = None  # StockHistoricalDataClient

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self) -> None:
        from alpaca.trading.client import TradingClient
        from alpaca.data.historical import StockHistoricalDataClient

        paper = "paper-api" in self._base_url
        self._client = TradingClient(
            api_key=self._api_key,
            secret_key=self._secret,
            paper=paper,
        )
        self._data_client = StockHistoricalDataClient(
            api_key=self._api_key,
            secret_key=self._secret,
        )

    def disconnect(self) -> None:
        # alpaca-py HTTP clients are stateless; nothing to close
        self._client = None
        self._data_client = None

    def is_connected(self) -> bool:
        return self._client is not None

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------

    def get_account(self) -> AccountInfo:
        acct = self._client.get_account()
        return AccountInfo(
            cash=float(acct.cash),
            portfolio_value=float(acct.portfolio_value),
            buying_power=float(acct.buying_power),
            equity=float(acct.equity),
            daily_pl=float(acct.equity) - float(acct.last_equity),
            unrealized_pl=float(acct.unrealized_pl) if acct.unrealized_pl else 0.0,
        )

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    def get_positions(self) -> List[Position]:
        positions = []
        for p in self._client.get_all_positions():
            positions.append(Position(
                symbol=p.symbol,
                qty=float(p.qty),
                avg_entry_price=float(p.avg_entry_price),
                current_price=float(p.current_price) if p.current_price else 0.0,
                unrealized_pl=float(p.unrealized_pl) if p.unrealized_pl else 0.0,
                unrealized_pl_pct=float(p.unrealized_plpc) if p.unrealized_plpc else 0.0,
                side="long" if float(p.qty) > 0 else "short",
            ))
        return positions

    def get_position(self, symbol: str) -> Optional[Position]:
        try:
            p = self._client.get_open_position(symbol)
            return Position(
                symbol=p.symbol,
                qty=float(p.qty),
                avg_entry_price=float(p.avg_entry_price),
                current_price=float(p.current_price) if p.current_price else 0.0,
                unrealized_pl=float(p.unrealized_pl) if p.unrealized_pl else 0.0,
                unrealized_pl_pct=float(p.unrealized_plpc) if p.unrealized_plpc else 0.0,
                side="long" if float(p.qty) > 0 else "short",
            )
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def place_order(self, order: TradeOrder) -> TradeOrder:
        from alpaca.trading.enums import OrderSide as AlpacaSide, TimeInForce
        from alpaca.trading.requests import (
            LimitOrderRequest,
            MarketOrderRequest,
            StopOrderRequest,
            StopLimitOrderRequest,
        )

        side = AlpacaSide.BUY if order.side == OrderSide.BUY else AlpacaSide.SELL

        if order.order_type == OrderType.MARKET:
            req = MarketOrderRequest(
                symbol=order.symbol,
                qty=order.qty,
                side=side,
                time_in_force=TimeInForce.DAY,
            )
        elif order.order_type == OrderType.LIMIT:
            req = LimitOrderRequest(
                symbol=order.symbol,
                qty=order.qty,
                side=side,
                limit_price=order.limit_price,
                time_in_force=TimeInForce.DAY,
            )
        elif order.order_type == OrderType.STOP:
            req = StopOrderRequest(
                symbol=order.symbol,
                qty=order.qty,
                side=side,
                stop_price=order.stop_price,
                time_in_force=TimeInForce.DAY,
            )
        elif order.order_type == OrderType.STOP_LIMIT:
            req = StopLimitOrderRequest(
                symbol=order.symbol,
                qty=order.qty,
                side=side,
                stop_price=order.stop_price,
                limit_price=order.limit_price,
                time_in_force=TimeInForce.DAY,
            )
        else:
            raise ValueError(f"Unsupported order type: {order.order_type}")

        resp = self._client.submit_order(req)
        order.order_id = str(resp.id)
        order.status = OrderStatus.OPEN
        return order

    def cancel_order(self, order_id: str) -> bool:
        try:
            self._client.cancel_order_by_id(order_id)
            return True
        except Exception:
            return False

    def get_open_orders(self) -> List[TradeOrder]:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus

        req = GetOrdersRequest(status=QueryOrderStatus.OPEN)
        orders = []
        for o in self._client.get_orders(req):
            orders.append(TradeOrder(
                symbol=o.symbol,
                qty=float(o.qty),
                side=OrderSide.BUY if str(o.side) == "buy" else OrderSide.SELL,
                order_type=OrderType.MARKET,  # simplified; extend if needed
                order_id=str(o.id),
                status=OrderStatus.OPEN,
                filled_qty=float(o.filled_qty) if o.filled_qty else 0.0,
                filled_avg_price=float(o.filled_avg_price) if o.filled_avg_price else None,
            ))
        return orders

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    def get_latest_price(self, symbol: str) -> float:
        from alpaca.data.requests import StockLatestTradeRequest

        req = StockLatestTradeRequest(symbol_or_symbols=symbol)
        resp = self._data_client.get_stock_latest_trade(req)
        return float(resp[symbol].price)

    # ------------------------------------------------------------------
    # Market hours
    # ------------------------------------------------------------------

    def is_market_open(self) -> bool:
        clock = self._client.get_clock()
        return clock.is_open
