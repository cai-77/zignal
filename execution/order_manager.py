"""
Order Manager — central routing layer between strategies and the broker.

Responsibilities:
  - Convenience helpers (place_market_buy, place_market_sell, place_stop)
  - Deduplication: prevents the same symbol from getting two simultaneous orders
  - Fill tracking: logs fills and updates daily P&L
  - Retry logic: retries failed market orders once before giving up
"""

from datetime import datetime
from typing import Optional

from brokers.base_broker import BrokerBase, OrderSide, OrderStatus, OrderType, TradeOrder
from monitor.logger import TradeLogger, TradeRecord


class OrderManager:

    def __init__(self, broker: BrokerBase, logger: TradeLogger, db=None, session_type: str = "backtest"):
        self._broker = broker
        self._logger = logger
        self._db = db
        self._session_type = session_type
        # Tracks symbols that have a pending (un-filled) order to prevent duplicates
        self._pending: set[str] = set()
        # entry price cache for P&L calculation on sells
        self._entry_cache: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Public convenience methods
    # ------------------------------------------------------------------

    def place_market_buy(
        self,
        symbol: str,
        qty: float,
        reason: str = "",
        strategy: str = "unknown",
    ) -> Optional[TradeOrder]:
        if symbol in self._pending:
            self._logger.log_warning(
                f"[OrderManager] Skipping duplicate buy for {symbol} (order pending)"
            )
            return None

        order = TradeOrder(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
        )
        return self._submit(order, reason, strategy)

    def place_market_sell(
        self,
        symbol: str,
        qty: float,
        reason: str = "",
        strategy: str = "unknown",
    ) -> Optional[TradeOrder]:
        if symbol in self._pending:
            self._logger.log_warning(
                f"[OrderManager] Skipping duplicate sell for {symbol} (order pending)"
            )
            return None

        order = TradeOrder(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
        )
        return self._submit(order, reason, strategy)

    def place_limit_buy(
        self,
        symbol: str,
        qty: float,
        limit_price: float,
        reason: str = "",
        strategy: str = "unknown",
    ) -> Optional[TradeOrder]:
        order = TradeOrder(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            limit_price=limit_price,
        )
        return self._submit(order, reason, strategy)

    def place_stop(
        self,
        symbol: str,
        qty: float,
        stop_price: float,
        reason: str = "",
        strategy: str = "unknown",
    ) -> Optional[TradeOrder]:
        """Place a stop-sell order (used for bracket / hard stop orders)."""
        order = TradeOrder(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            order_type=OrderType.STOP,
            stop_price=stop_price,
        )
        return self._submit(order, reason, strategy)

    def cancel_all_for_symbol(self, symbol: str) -> None:
        for o in self._broker.get_open_orders():
            if o.symbol == symbol and o.order_id:
                self._broker.cancel_order(o.order_id)
        self._pending.discard(symbol)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _submit(
        self,
        order: TradeOrder,
        reason: str,
        strategy: str,
        max_retries: int = 1,
    ) -> Optional[TradeOrder]:
        self._pending.add(order.symbol)
        last_exc: Optional[Exception] = None

        for attempt in range(max_retries + 1):
            try:
                filled = self._broker.place_order(order)
                self._pending.discard(order.symbol)
                self._on_fill(filled, reason, strategy)
                return filled
            except Exception as exc:
                last_exc = exc
                self._logger.log_error(
                    f"[OrderManager] Order failed (attempt {attempt + 1}): "
                    f"{order.side.value} {order.qty} {order.symbol}",
                    exc,
                )

        self._pending.discard(order.symbol)
        self._logger.log_error(
            f"[OrderManager] Giving up on {order.symbol} after {max_retries + 1} attempts"
        )
        return None

    def _on_fill(self, order: TradeOrder, reason: str, strategy: str) -> None:
        price = order.filled_avg_price or 0.0
        qty = order.filled_qty or order.qty

        pnl: Optional[float] = None
        if order.side == OrderSide.BUY:
            self._entry_cache[order.symbol] = price
        elif order.side == OrderSide.SELL:
            entry = self._entry_cache.pop(order.symbol, None)
            if entry and price > 0:
                pnl = (price - entry) * qty
                self._logger.update_daily_pnl(pnl)

        record = TradeRecord(
            timestamp=datetime.now().isoformat(),
            symbol=order.symbol,
            action=order.side.value,
            qty=qty,
            price=price,
            order_id=order.order_id or "",
            strategy=strategy,
            reason=reason,
            pnl=pnl,
        )
        self._logger.log_trade(record)

        if self._db and self._session_type != "backtest":
            self._db.save_live_trade(
                session_type=self._session_type,
                symbol=order.symbol,
                action=order.side.value,
                qty=qty,
                price=price,
                pnl=pnl,
                strategy=strategy,
                reason=reason,
            )
            event_type = "trade_open" if order.side.value == "buy" else "trade_close"
            pnl_str = f" | P&L ${pnl:+,.2f}" if pnl is not None else ""
            self._db.save_event(
                event_type=event_type,
                symbol=order.symbol,
                message=f"{order.side.value.upper()} {qty:.0f} {order.symbol} @ ${price:.2f}{pnl_str} — {reason}",
                data={"symbol": order.symbol, "qty": qty, "price": price, "pnl": pnl},
            )
