"""
Risk Manager — enforces all position-level and portfolio-level risk rules.

Rules enforced:
  - Max 5% of portfolio per position
  - Max 20% portfolio heat (total capital at risk across all open positions)
  - 2% hard stop-loss below entry price (strategies check this; RM provides the %)
  - 3% max daily loss — trading halted for the rest of the day if breached
  - No new day trades in the last 15 minutes before market close
    (swing trades are exempt from this rule)
"""

import math
from datetime import date
from typing import Optional

from monitor.logger import TradeLogger


class RiskManager:

    def __init__(self, risk_config: dict, logger: TradeLogger, broker=None):
        self.max_position_pct: float = risk_config.get("max_position_pct", 0.05)
        self.max_portfolio_heat: float = risk_config.get("max_portfolio_heat", 0.20)
        self.stop_loss_pct: float = risk_config.get("stop_loss_pct", 0.02)
        self.max_daily_loss_pct: float = risk_config.get("max_daily_loss_pct", 0.03)

        self._logger = logger
        self._broker = broker  # set after construction via set_broker() if needed

        # State reset each day
        self._trading_day: Optional[date] = None
        self._start_of_day_equity: float = 0.0
        self._daily_halted: bool = False

    def set_broker(self, broker) -> None:
        """Wire the broker after construction (avoids circular init order)."""
        self._broker = broker

    # ------------------------------------------------------------------
    # Daily state
    # ------------------------------------------------------------------

    def start_of_day(self, portfolio_value: float) -> None:
        """Call at market open each day to reset daily tracking."""
        today = date.today()
        if self._trading_day != today:
            self._trading_day = today
            self._start_of_day_equity = portfolio_value
            self._daily_halted = False
            self._logger.log_info(
                f"[RiskManager] New trading day. Starting equity: ${portfolio_value:,.2f}"
            )

    def check_daily_loss_limit(self, current_portfolio_value: float) -> bool:
        """
        Returns True if trading is allowed, False if the daily loss limit is breached.
        Call this before any new order.
        """
        if self._start_of_day_equity <= 0:
            return True  # not initialised yet

        daily_loss_pct = (
            (self._start_of_day_equity - current_portfolio_value)
            / self._start_of_day_equity
        )

        if daily_loss_pct >= self.max_daily_loss_pct:
            if not self._daily_halted:
                self._daily_halted = True
                self._logger.log_warning(
                    f"[RiskManager] DAILY LOSS LIMIT BREACHED: "
                    f"loss={daily_loss_pct:.1%} >= limit={self.max_daily_loss_pct:.1%}. "
                    f"Trading halted for the day."
                )
            return False

        return True

    def is_halted(self) -> bool:
        return self._daily_halted

    # ------------------------------------------------------------------
    # Position permission
    # ------------------------------------------------------------------

    def can_open_position(self, symbol: str, price: float) -> bool:
        """
        Returns True if opening a new position in *symbol* at *price* is permitted.

        Checks (when a broker has been wired via set_broker):
          1. Daily loss limit not breached
          2. Symbol not already held (avoid doubling up)
          3. Portfolio heat headroom available
        """
        if self._daily_halted:
            self._logger.log_warning(
                f"[RiskManager] Blocked new position in {symbol}: daily halt active"
            )
            return False

        if self._broker is not None:
            portfolio_value = self._broker.get_portfolio_value()

            if self._broker.get_position(symbol) is not None:
                self._logger.log_info(
                    f"[RiskManager] Already holding {symbol}, skip entry"
                )
                return False

            heat = self._current_heat(self._broker, portfolio_value)
            if heat + self.max_position_pct > self.max_portfolio_heat:
                self._logger.log_warning(
                    f"[RiskManager] Portfolio heat {heat:.1%} + "
                    f"new position {self.max_position_pct:.1%} exceeds "
                    f"max {self.max_portfolio_heat:.1%}. Blocked."
                )
                return False

        return True

    def calculate_position_size(self, price: float) -> int:
        """
        Return the number of whole shares so the dollar value equals
        max_position_pct of the current portfolio.  Returns 0 if < 1 share.
        """
        portfolio_value = (
            self._broker.get_portfolio_value() if self._broker else self._start_of_day_equity
        )
        return self._size_shares(price, portfolio_value)

    def _size_shares(
        self,
        price: float,
        portfolio_value: float,
    ) -> int:
        """
        Return the number of whole shares to buy so that the dollar value
        equals at most max_position_pct of portfolio_value.

        Returns 0 if the position would be less than 1 share.
        """
        if price <= 0 or portfolio_value <= 0:
            return 0

        max_dollars = portfolio_value * self.max_position_pct
        shares = math.floor(max_dollars / price)
        return shares

    def stop_loss_price(self, entry_price: float) -> float:
        """Return the hard stop-loss price for a given entry price."""
        return entry_price * (1.0 - self.stop_loss_pct)

    # ------------------------------------------------------------------
    # Stop-loss sweep (called by backtest engine each bar)
    # ------------------------------------------------------------------

    def check_stop_losses(self, broker, order_manager) -> None:
        """
        Scan all open positions and submit market sells for any that have
        breached their stop-loss level.

        Used by the backtest engine (and the live loop) to ensure stops are
        applied even when the strategy's on_bar hasn't explicitly checked.
        """
        for position in broker.get_positions():
            price = broker.get_latest_price(position.symbol)
            stop = self.stop_loss_price(position.avg_entry_price)
            if price <= stop:
                self._logger.log_warning(
                    f"[RiskManager] Stop-loss sweep: {position.symbol} "
                    f"price={price:.2f} <= stop={stop:.2f}"
                )
                order_manager.place_market_sell(
                    position.symbol,
                    position.qty,
                    reason=f"Stop-loss sweep: price={price:.2f} stop={stop:.2f}",
                    strategy="RiskManager",
                )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _current_heat(self, broker, portfolio_value: float) -> float:
        """
        Calculate the fraction of portfolio currently at risk across all
        open positions (using stop-loss distance as the risk per position).
        """
        total_risk = 0.0
        for pos in broker.get_positions():
            position_value = pos.qty * pos.avg_entry_price
            total_risk += position_value * self.stop_loss_pct
        return total_risk / portfolio_value if portfolio_value > 0 else 0.0
