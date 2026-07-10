"""
Interactive Brokers broker implementation using ib_insync.

Prerequisites:
  - TWS or IB Gateway must be running and accepting API connections
  - Enable "Allow connections from localhost only" in TWS API settings
  - Port 7497 = TWS paper | 7496 = TWS live | 4002 = IB Gateway paper | 4001 = Gateway live

Install: pip install ib_insync
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


class IBKRBroker(BrokerBase):

    def __init__(self, config: dict):
        self._host = config.get("host", "127.0.0.1")
        self._port = config.get("port", 7497)
        self._client_id = config.get("client_id", 1)
        self._ib = None   # ib_insync.IB instance

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self) -> None:
        from ib_insync import IB
        self._ib = IB()
        self._ib.connect(self._host, self._port, clientId=self._client_id)

    def disconnect(self) -> None:
        if self._ib and self._ib.isConnected():
            self._ib.disconnect()

    def is_connected(self) -> bool:
        return self._ib is not None and self._ib.isConnected()

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------

    def get_account(self) -> AccountInfo:
        summary = self._ib.accountSummary()
        vals: dict[str, float] = {}
        for item in summary:
            if item.currency in ("USD", "BASE"):
                try:
                    vals[item.tag] = float(item.value)
                except ValueError:
                    pass

        return AccountInfo(
            cash=vals.get("CashBalance", 0.0),
            portfolio_value=vals.get("NetLiquidation", 0.0),
            buying_power=vals.get("BuyingPower", 0.0),
            equity=vals.get("EquityWithLoanValue", 0.0),
            daily_pl=vals.get("RealizedPnL", 0.0),
            unrealized_pl=vals.get("UnrealizedPnL", 0.0),
        )

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    def get_positions(self) -> List[Position]:
        positions = []
        for p in self._ib.positions():
            contract = p.contract
            if not hasattr(contract, "symbol"):
                continue
            positions.append(Position(
                symbol=contract.symbol,
                qty=float(p.position),
                avg_entry_price=float(p.avgCost),
                side="long" if p.position > 0 else "short",
            ))
        return positions

    def get_position(self, symbol: str) -> Optional[Position]:
        for pos in self.get_positions():
            if pos.symbol == symbol:
                return pos
        return None

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def place_order(self, order: TradeOrder) -> TradeOrder:
        from ib_insync import Stock, MarketOrder, LimitOrder, StopOrder, StopLimitOrder

        contract = Stock(order.symbol, "SMART", "USD")
        self._ib.qualifyContracts(contract)

        action = "BUY" if order.side == OrderSide.BUY else "SELL"

        if order.order_type == OrderType.MARKET:
            ib_order = MarketOrder(action, order.qty)
        elif order.order_type == OrderType.LIMIT:
            ib_order = LimitOrder(action, order.qty, order.limit_price)
        elif order.order_type == OrderType.STOP:
            ib_order = StopOrder(action, order.qty, order.stop_price)
        elif order.order_type == OrderType.STOP_LIMIT:
            ib_order = StopLimitOrder(action, order.qty, order.limit_price, order.stop_price)
        else:
            raise ValueError(f"Unsupported order type: {order.order_type}")

        trade = self._ib.placeOrder(contract, ib_order)
        self._ib.sleep(0)  # give IB a tick to process

        order.order_id = str(trade.order.orderId)
        order.status = OrderStatus.OPEN
        return order

    def cancel_order(self, order_id: str) -> bool:
        for trade in self._ib.trades():
            if str(trade.order.orderId) == order_id:
                self._ib.cancelOrder(trade.order)
                return True
        return False

    def get_open_orders(self) -> List[TradeOrder]:
        orders = []
        for trade in self._ib.trades():
            if trade.isDone():
                continue
            o = trade.order
            c = trade.contract
            orders.append(TradeOrder(
                symbol=getattr(c, "symbol", "UNKNOWN"),
                qty=float(o.totalQuantity),
                side=OrderSide.BUY if o.action == "BUY" else OrderSide.SELL,
                order_type=OrderType.MARKET,
                order_id=str(o.orderId),
                status=OrderStatus.OPEN,
            ))
        return orders

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    def get_latest_price(self, symbol: str) -> float:
        from ib_insync import Stock

        contract = Stock(symbol, "SMART", "USD")
        self._ib.qualifyContracts(contract)

        # snapshot=True means a one-shot request (no streaming subscription)
        ticker = self._ib.reqMktData(contract, "", snapshot=True, regulatorySnapshot=False)
        self._ib.sleep(1)  # wait for snapshot to populate

        price = ticker.last if ticker.last and ticker.last > 0 else ticker.close
        self._ib.cancelMktData(contract)

        if not price or price <= 0:
            raise RuntimeError(f"Could not retrieve price for {symbol}")
        return float(price)

    # ------------------------------------------------------------------
    # Market hours  (NYSE schedule)
    # ------------------------------------------------------------------

    def is_market_open(self) -> bool:
        ny = pytz.timezone("America/New_York")
        now = datetime.now(ny)
        if now.weekday() >= 5:  # Saturday / Sunday
            return False
        open_time = now.replace(hour=9, minute=30, second=0, microsecond=0)
        close_time = now.replace(hour=16, minute=0, second=0, microsecond=0)
        return open_time <= now < close_time
