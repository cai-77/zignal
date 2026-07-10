"""
Abstract base class for all trading strategies.

Every strategy receives the same set of collaborators:
  - config       : the full config dict (strategy reads its own sub-section)
  - broker       : BrokerBase — position queries, prices
  - risk_manager : RiskManager — position sizing, trade permission checks
  - order_manager: OrderManager — order placement / cancellation
  - logger       : TradeLogger — logging

The lifecycle hooks are:
  on_start()     called once when the strategy is activated
  on_bar(bar)    called on every new price bar (pd.Series with 'symbol' key)
  on_stop()      called on clean shutdown (e.g. end of backtest)
"""

from abc import ABC, abstractmethod
from typing import Optional

import pandas as pd

from monitor.logger import TradeLogger


# ------------------------------------------------------------------
# Technical indicator helpers (pure pandas — no extra C libraries)
# ------------------------------------------------------------------

def compute_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder-smoothed RSI."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    return 100.0 - (100.0 / (1.0 + rs))


def compute_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def compute_sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period).mean()


# ------------------------------------------------------------------
# Base strategy
# ------------------------------------------------------------------

class BaseStrategy(ABC):

    def __init__(
        self,
        config: dict,
        broker,          # BrokerBase — avoid circular import with string annotation
        risk_manager,    # RiskManager
        order_manager,   # OrderManager
        logger: TradeLogger,
    ):
        self.config = config
        self.broker = broker
        self.risk_manager = risk_manager
        self.order_manager = order_manager
        self.logger = logger

        # Per-symbol rolling bar buffer used by on_bar() implementations
        self._buffers: dict[str, pd.DataFrame] = {}

    # ------------------------------------------------------------------
    # Lifecycle — must override
    # ------------------------------------------------------------------

    @abstractmethod
    def on_bar(self, bar: pd.Series) -> None:
        """
        Called for each new closed bar.

        bar must contain at minimum: symbol, open, high, low, close, volume.
        bar.name (or bar['timestamp']) holds the bar's datetime.
        """

    def on_start(self) -> None:
        """Optional: initialise state, pre-load data, log strategy params."""

    def on_stop(self) -> None:
        """Optional: clean up, flatten positions if required."""

    # ------------------------------------------------------------------
    # Helpers available to all strategies
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return self.__class__.__name__

    def _append_bar(self, bar: pd.Series, max_bars: int = 500) -> pd.DataFrame:
        """
        Append *bar* to the rolling buffer for its symbol and return the
        updated DataFrame.  Keeps at most *max_bars* rows.
        """
        symbol = bar["symbol"]
        row = bar.drop("symbol").to_frame().T
        row.index = [bar.name if bar.name is not None else pd.Timestamp.now()]

        if symbol not in self._buffers:
            self._buffers[symbol] = pd.DataFrame()

        self._buffers[symbol] = pd.concat([self._buffers[symbol], row])
        if len(self._buffers[symbol]) > max_bars:
            self._buffers[symbol] = self._buffers[symbol].iloc[-max_bars:]

        # Ensure numeric dtypes (concat can widen to object)
        for col in ("open", "high", "low", "close", "volume"):
            if col in self._buffers[symbol].columns:
                self._buffers[symbol][col] = pd.to_numeric(
                    self._buffers[symbol][col], errors="coerce"
                )

        return self._buffers[symbol]

    def _get_buffer(self, symbol: str) -> Optional[pd.DataFrame]:
        return self._buffers.get(symbol)
