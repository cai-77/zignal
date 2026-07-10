"""
Moving Average Crossover — day trading strategy on 5-minute bars.

Entry rule  : Buy when fast EMA (9) crosses ABOVE slow EMA (21)
Exit rule   : Sell when fast EMA crosses BACK BELOW slow EMA, OR stop-loss hit
EOD rule    : All positions closed 15 minutes before market close
"""

from datetime import time

import pandas as pd
import pytz

from brokers.base_broker import BrokerBase
from monitor.logger import TradeLogger
from strategies.base_strategy import BaseStrategy, compute_ema


NY_TZ = pytz.timezone("America/New_York")


class MovingAverageCrossover(BaseStrategy):

    def __init__(self, config: dict, broker: BrokerBase, risk_manager, order_manager, logger: TradeLogger):
        super().__init__(config, broker, risk_manager, order_manager, logger)

        dt = config.get("day_trading", {})
        self.fast_period: int = dt.get("fast_ema_period", 9)
        self.slow_period: int = dt.get("slow_ema_period", 21)
        self.close_mins_before_eod: int = dt.get("close_minutes_before_eod", 15)

        # Track previous EMA relationship to detect crossovers
        # {symbol: bool}  True = fast was above slow on the previous bar
        self._prev_fast_above: dict[str, bool] = {}

    def on_start(self) -> None:
        self.logger.log_info(
            f"[{self.name}] started | "
            f"EMA({self.fast_period}) / EMA({self.slow_period})"
        )

    def on_bar(self, bar: pd.Series) -> None:
        symbol = bar["symbol"]
        df = self._append_bar(bar)

        # Require enough bars to compute the slow EMA reliably
        if len(df) < self.slow_period + 2:
            return

        if self._is_eod_window(bar):
            self._close_position_if_open(symbol, "EOD flatten")
            return

        fast_ema = compute_ema(df["close"], self.fast_period)
        slow_ema = compute_ema(df["close"], self.slow_period)

        if pd.isna(fast_ema.iloc[-1]) or pd.isna(slow_ema.iloc[-1]):
            return

        fast_above_now = fast_ema.iloc[-1] > slow_ema.iloc[-1]
        fast_above_prev = self._prev_fast_above.get(symbol)
        self._prev_fast_above[symbol] = fast_above_now

        # Not enough history to detect a crossover yet
        if fast_above_prev is None:
            return

        crossed_up = (not fast_above_prev) and fast_above_now
        crossed_down = fast_above_prev and (not fast_above_now)

        position = self.broker.get_position(symbol)
        price = float(bar["close"])

        if position is None:
            if crossed_up:
                if self.risk_manager.can_open_position(symbol, price):
                    qty = self.risk_manager.calculate_position_size(price)
                    if qty > 0:
                        self.order_manager.place_market_buy(
                            symbol, qty,
                            reason=(
                                f"EMA crossover UP: fast={fast_ema.iloc[-1]:.2f} "
                                f"slow={slow_ema.iloc[-1]:.2f}"
                            ),
                            strategy=self.name,
                        )
        else:
            # Stop-loss check
            stop_price = position.avg_entry_price * (1 - self.risk_manager.stop_loss_pct)
            if price <= stop_price:
                self.order_manager.place_market_sell(
                    symbol, position.qty,
                    reason=f"Stop-loss hit: close={price:.2f} stop={stop_price:.2f}",
                    strategy=self.name,
                )
                return

            if crossed_down:
                self.order_manager.place_market_sell(
                    symbol, position.qty,
                    reason=(
                        f"EMA crossover DOWN: fast={fast_ema.iloc[-1]:.2f} "
                        f"slow={slow_ema.iloc[-1]:.2f}"
                    ),
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
