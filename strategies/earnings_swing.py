"""
Earnings Swing Strategy — enter BEFORE earnings, exit BEFORE the announcement.

This is NOT an earnings surprise play.  We are trading the pre-earnings
run-up that often happens as institutional investors position ahead of results.
We ALWAYS exit before the announcement to avoid binary event risk.

Entry conditions (all must be true):
  1. Next earnings date is approximately N weeks away (configurable, default 4)
  2. Price is above the 50-day SMA  (trending up, not broken down)
  3. RSI is between 40 and 60       (not already extended or overbought)
  4. Risk manager approves the position size and portfolio heat

Exit conditions (first one triggered wins):
  A. Days until earnings <= exit_days_before_earnings (hard pre-earnings exit)
  B. Price drops below the 2% stop-loss level

This strategy runs on DAILY bars. It should not be used with intraday feeds.
"""

from datetime import date, timedelta
from typing import Optional

import pandas as pd

from brokers.base_broker import BrokerBase
from data.finnhub_client import FinnhubClient
from monitor.logger import TradeLogger
from strategies.base_strategy import BaseStrategy, compute_rsi, compute_sma


class EarningsSwing(BaseStrategy):

    def __init__(
        self,
        config: dict,
        broker: BrokerBase,
        risk_manager,
        order_manager,
        logger: TradeLogger,
        finnhub: Optional[FinnhubClient] = None,
    ):
        super().__init__(config, broker, risk_manager, order_manager, logger)

        sw = config.get("swing", {})
        self.entry_weeks: int = sw.get("entry_weeks_before_earnings", 4)
        self.exit_days: int = sw.get("exit_days_before_earnings", 3)
        self.rsi_min: float = sw.get("rsi_min", 40)
        self.rsi_max: float = sw.get("rsi_max", 60)
        self.sma_period: int = sw.get("sma_period", 50)

        self.finnhub = finnhub

        # Track the entry price of each swing position for stop-loss
        self._entry_prices: dict[str, float] = {}

    def on_start(self) -> None:
        self.logger.log_info(
            f"[{self.name}] started | "
            f"entry {self.entry_weeks} weeks before earnings, "
            f"exit {self.exit_days} days before earnings"
        )

    def on_bar(self, bar: pd.Series) -> None:
        symbol = bar["symbol"]
        df = self._append_bar(bar)

        # Need enough history for SMA and RSI
        if len(df) < max(self.sma_period, 15) + 1:
            return

        position = self.broker.get_position(symbol)

        if position is not None:
            self._manage_position(symbol, bar, position)
        else:
            self._check_entry(symbol, bar, df)

    def on_stop(self) -> None:
        self.logger.log_info(f"[{self.name}] shutting down")
        # Swing trades are NOT forcibly closed on shutdown — they are multi-day holds.
        # The caller can decide whether to close them.

    # ------------------------------------------------------------------
    # Entry logic
    # ------------------------------------------------------------------

    def _check_entry(self, symbol: str, bar: pd.Series, df: pd.DataFrame) -> None:
        if self.finnhub is None:
            return

        earnings_date = self.finnhub.get_next_earnings_date(symbol)
        if earnings_date is None:
            return

        days_to_earnings = (earnings_date - date.today()).days

        # Target entry window: within ±7 days of entry_weeks_before_earnings
        target_days = self.entry_weeks * 7
        if not (target_days - 7 <= days_to_earnings <= target_days + 7):
            return

        # Technical filters
        close = float(bar["close"])

        sma50 = compute_sma(df["close"], self.sma_period).iloc[-1]
        if pd.isna(sma50) or close <= sma50:
            self.logger.log_info(
                f"[{self.name}] {symbol}: skip entry — price {close:.2f} <= SMA50 {sma50:.2f}"
            )
            return

        rsi = compute_rsi(df["close"], 14).iloc[-1]
        if pd.isna(rsi) or not (self.rsi_min <= rsi <= self.rsi_max):
            self.logger.log_info(
                f"[{self.name}] {symbol}: skip entry — RSI {rsi:.1f} outside [{self.rsi_min}, {self.rsi_max}]"
            )
            return

        # Risk check
        if not self.risk_manager.can_open_position(symbol, close):
            return

        qty = self.risk_manager.calculate_position_size(close)
        if qty <= 0:
            return

        self._entry_prices[symbol] = close
        self.order_manager.place_market_buy(
            symbol, qty,
            reason=(
                f"Swing entry: {days_to_earnings}d to earnings "
                f"({earnings_date}), RSI={rsi:.1f}, price/SMA50={close/sma50:.2f}"
            ),
            strategy=self.name,
        )

    # ------------------------------------------------------------------
    # Exit / position management
    # ------------------------------------------------------------------

    def _manage_position(self, symbol: str, bar: pd.Series, position) -> None:
        close = float(bar["close"])

        # Stop-loss
        entry_price = self._entry_prices.get(symbol, position.avg_entry_price)
        stop_price = entry_price * (1 - self.risk_manager.stop_loss_pct)
        if close <= stop_price:
            self.order_manager.place_market_sell(
                symbol, position.qty,
                reason=f"Stop-loss hit: close={close:.2f}, stop={stop_price:.2f}",
                strategy=self.name,
            )
            self._entry_prices.pop(symbol, None)
            return

        # Pre-earnings exit
        if self.finnhub is None:
            return

        earnings_date = self.finnhub.get_next_earnings_date(symbol)
        if earnings_date is None:
            return

        days_to_earnings = (earnings_date - date.today()).days
        self.logger.log_info(
            f"[{self.name}] {symbol}: {days_to_earnings} days to earnings "
            f"({earnings_date}) — exit trigger at {self.exit_days}d"
        )

        if days_to_earnings <= self.exit_days:
            self.order_manager.place_market_sell(
                symbol, position.qty,
                reason=(
                    f"Pre-earnings exit: {days_to_earnings}d to earnings "
                    f"({earnings_date}), threshold={self.exit_days}d"
                ),
                strategy=self.name,
            )
            self._entry_prices.pop(symbol, None)
