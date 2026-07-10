"""
RSI Mean Reversion — day trading strategy on 5-minute bars.

Entry rule  : Buy when 14-period RSI drops below 30 (oversold)
Exit rule   : Sell when RSI rises above 70 (overbought), OR stop-loss hit
EOD rule    : All positions closed 15 minutes before market close

This strategy only trades during regular market hours and respects
the EOD flattening window from config.
"""

from datetime import datetime, time

import pandas as pd
import pytz

from brokers.base_broker import BrokerBase, OrderSide, OrderType, TradeOrder
from monitor.logger import TradeLogger
from strategies.base_strategy import BaseStrategy, compute_rsi


NY_TZ = pytz.timezone("America/New_York")
MARKET_CLOSE = time(16, 0)


class RsiMeanReversion(BaseStrategy):

    def __init__(self, config: dict, broker: BrokerBase, risk_manager, order_manager, logger: TradeLogger):
        super().__init__(config, broker, risk_manager, order_manager, logger)

        dt = config.get("day_trading", {})
        self.rsi_period: int = dt.get("rsi_period", 14)
        self.rsi_oversold: float = dt.get("rsi_oversold", 30)
        self.rsi_overbought: float = dt.get("rsi_overbought", 70)
        self.close_mins_before_eod: int = dt.get("close_minutes_before_eod", 15)

    def on_start(self) -> None:
        self.logger.log_info(
            f"[{self.name}] started | "
            f"RSI({self.rsi_period}) oversold<{self.rsi_oversold} overbought>{self.rsi_overbought}"
        )

    def on_bar(self, bar: pd.Series) -> None:
        symbol = bar["symbol"]
        df = self._append_bar(bar)

        # Need at least rsi_period + 1 bars to produce a valid RSI value
        if len(df) < self.rsi_period + 2:
            return

        if self._is_eod_window(bar):
            self._close_position_if_open(symbol, "EOD flatten")
            return

        rsi = compute_rsi(df["close"], self.rsi_period).iloc[-1]
        if pd.isna(rsi):
            return

        position = self.broker.get_position(symbol)

        if position is None:
            # Look for entry
            if rsi < self.rsi_oversold:
                price = float(bar["close"])
                if self.risk_manager.can_open_position(symbol, price):
                    qty = self.risk_manager.calculate_position_size(price)
                    if qty > 0:
                        self.order_manager.place_market_buy(
                            symbol, qty,
                            reason=f"RSI oversold @ {rsi:.1f}",
                            strategy=self.name,
                        )
        else:
            # Manage open position
            price = float(bar["close"])

            # Stop-loss check
            stop_price = position.avg_entry_price * (1 - self.risk_manager.stop_loss_pct)
            if price <= stop_price:
                self.order_manager.place_market_sell(
                    symbol, position.qty,
                    reason=f"Stop-loss hit: close={price:.2f} stop={stop_price:.2f}",
                    strategy=self.name,
                )
                return

            # Take-profit on RSI overbought
            if rsi > self.rsi_overbought:
                self.order_manager.place_market_sell(
                    symbol, position.qty,
                    reason=f"RSI overbought @ {rsi:.1f}",
                    strategy=self.name,
                )

    def on_stop(self) -> None:
        self.logger.log_info(f"[{self.name}] shutting down — closing all positions")
        for pos in self.broker.get_positions():
            self._close_position_if_open(pos.symbol, "Strategy shutdown")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _is_eod_window(self, bar: pd.Series) -> bool:
        """True if we're within *close_mins_before_eod* minutes of 16:00 ET."""
        ts = bar.name
        if ts is None:
            return False
        if hasattr(ts, "tzinfo") and ts.tzinfo is not None:
            ts_ny = ts.astimezone(NY_TZ)
        else:
            ts_ny = NY_TZ.localize(ts)

        close_dt = ts_ny.replace(hour=16, minute=0, second=0, microsecond=0)
        delta_minutes = (close_dt - ts_ny).total_seconds() / 60
        return 0 <= delta_minutes <= self.close_mins_before_eod

    def _close_position_if_open(self, symbol: str, reason: str) -> None:
        position = self.broker.get_position(symbol)
        if position and position.qty > 0:
            self.order_manager.place_market_sell(
                symbol, position.qty, reason=reason, strategy=self.name
            )
