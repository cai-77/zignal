"""
Backtest Engine — runs any strategy against Polygon historical data.

Architecture:
  - SimulatedBroker tracks cash, positions, and fills locally (no network calls)
  - BacktestEngine feeds daily bars to the strategy one at a time (no look-ahead)
  - Orders are filled at the NEXT bar's open price (realistic execution assumption)
  - A BacktestResult is returned with full trade history and performance metrics

Usage:
    engine = BacktestEngine(config, polygon_feed, logger)
    result = engine.run(
        strategy_class=EarningsSwing,
        symbols=["AAPL", "MSFT"],
        start_date="2023-01-01",
        end_date="2023-12-31",
    )
    result.print_summary()
"""

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Type

import pandas as pd

from brokers.base_broker import (
    AccountInfo,
    BrokerBase,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    TradeOrder,
)
from data.polygon_feed import PolygonDataFeed
from execution.order_manager import OrderManager
from monitor.logger import TradeLogger
from risk.risk_manager import RiskManager
from strategies.base_strategy import BaseStrategy


# ======================================================================
# Simulated Broker
# ======================================================================

class SimulatedBroker(BrokerBase):
    """
    In-memory broker used exclusively by the backtest engine.

    Fills are executed at the open price of the bar AFTER the signal bar
    to simulate realistic next-bar execution.
    """

    def __init__(self, initial_capital: float, commission: float = 0.0):
        self._cash = initial_capital
        self._commission = commission
        self._positions: dict[str, Position] = {}
        self._current_prices: dict[str, float] = {}
        # Pending orders queued for next-bar fill
        self._pending_orders: list[TradeOrder] = []
        self._trade_history: list[dict] = []
        self._order_counter = 0

    # ------------------------------------------------------------------
    # BrokerBase interface
    # ------------------------------------------------------------------

    def connect(self) -> None:
        pass  # no-op

    def disconnect(self) -> None:
        pass

    def is_connected(self) -> bool:
        return True

    def get_account(self) -> AccountInfo:
        portfolio_value = self._cash + sum(
            p.qty * self._current_prices.get(s, p.avg_entry_price)
            for s, p in self._positions.items()
        )
        unrealized = sum(
            p.qty * (self._current_prices.get(s, p.avg_entry_price) - p.avg_entry_price)
            for s, p in self._positions.items()
        )
        return AccountInfo(
            cash=self._cash,
            portfolio_value=portfolio_value,
            buying_power=self._cash,
            equity=portfolio_value,
            unrealized_pl=unrealized,
        )

    def get_positions(self) -> List[Position]:
        return list(self._positions.values())

    def get_position(self, symbol: str) -> Optional[Position]:
        return self._positions.get(symbol)

    def place_order(self, order: TradeOrder) -> TradeOrder:
        self._order_counter += 1
        order.order_id = f"SIM-{self._order_counter:05d}"
        order.status = OrderStatus.OPEN
        self._pending_orders.append(order)
        return order

    def cancel_order(self, order_id: str) -> bool:
        before = len(self._pending_orders)
        self._pending_orders = [o for o in self._pending_orders if o.order_id != order_id]
        return len(self._pending_orders) < before

    def get_open_orders(self) -> List[TradeOrder]:
        return list(self._pending_orders)

    def get_latest_price(self, symbol: str) -> float:
        return self._current_prices.get(symbol, 0.0)

    def is_market_open(self) -> bool:
        return True  # backtest assumes market is always open

    # ------------------------------------------------------------------
    # Backtest-specific methods
    # ------------------------------------------------------------------

    def update_price(self, symbol: str, price: float) -> None:
        """Called by the engine to set the current bar's closing price."""
        self._current_prices[symbol] = price
        if symbol in self._positions:
            pos = self._positions[symbol]
            pos.current_price = price
            pos.unrealized_pl = (price - pos.avg_entry_price) * pos.qty
            pos.unrealized_pl_pct = (price / pos.avg_entry_price - 1.0) * 100

    def fill_pending_orders(self, bar_opens: dict[str, float]) -> List[TradeOrder]:
        """
        Fill all pending orders at the open price of the current bar.

        Returns the list of filled orders.
        """
        filled = []
        remaining = []

        for order in self._pending_orders:
            fill_price = bar_opens.get(order.symbol)
            if fill_price is None or fill_price <= 0:
                remaining.append(order)
                continue

            if order.side == OrderSide.BUY:
                cost = fill_price * order.qty + self._commission
                if cost > self._cash:
                    # Not enough cash — partial fill
                    affordable_qty = math.floor(
                        (self._cash - self._commission) / fill_price
                    )
                    if affordable_qty <= 0:
                        remaining.append(order)
                        continue
                    order.qty = float(affordable_qty)
                    cost = fill_price * affordable_qty + self._commission

                self._cash -= cost

                if order.symbol in self._positions:
                    # Average into existing position
                    pos = self._positions[order.symbol]
                    total_qty = pos.qty + order.qty
                    pos.avg_entry_price = (
                        (pos.avg_entry_price * pos.qty + fill_price * order.qty)
                        / total_qty
                    )
                    pos.qty = total_qty
                else:
                    self._positions[order.symbol] = Position(
                        symbol=order.symbol,
                        qty=order.qty,
                        avg_entry_price=fill_price,
                        current_price=fill_price,
                    )

            elif order.side == OrderSide.SELL:
                pos = self._positions.get(order.symbol)
                if pos is None:
                    continue  # nothing to sell

                sell_qty = min(order.qty, pos.qty)
                proceeds = fill_price * sell_qty - self._commission
                self._cash += proceeds

                pnl = (fill_price - pos.avg_entry_price) * sell_qty
                self._trade_history.append({
                    "symbol": order.symbol,
                    "qty": sell_qty,
                    "entry": pos.avg_entry_price,
                    "exit": fill_price,
                    "pnl": pnl,
                    "order_id": order.order_id,
                })

                pos.qty -= sell_qty
                if pos.qty <= 0:
                    del self._positions[order.symbol]

            order.filled_qty = order.qty
            order.filled_avg_price = fill_price
            order.status = OrderStatus.FILLED
            filled.append(order)

        self._pending_orders = remaining
        return filled

    def get_trade_history(self) -> List[dict]:
        return list(self._trade_history)


# ======================================================================
# Backtest Result
# ======================================================================

@dataclass
class BacktestResult:
    initial_capital: float
    final_value: float
    trade_history: List[dict] = field(default_factory=list)
    portfolio_curve: pd.Series = field(default_factory=pd.Series)  # date -> value
    strategy_name: str = ""

    @property
    def total_return_pct(self) -> float:
        return (self.final_value / self.initial_capital - 1.0) * 100

    @property
    def total_pnl(self) -> float:
        return self.final_value - self.initial_capital

    @property
    def num_trades(self) -> int:
        return len(self.trade_history)

    @property
    def win_rate(self) -> float:
        if not self.trade_history:
            return 0.0
        wins = sum(1 for t in self.trade_history if t["pnl"] > 0)
        return wins / len(self.trade_history) * 100

    @property
    def avg_pnl_per_trade(self) -> float:
        if not self.trade_history:
            return 0.0
        return sum(t["pnl"] for t in self.trade_history) / len(self.trade_history)

    @property
    def max_drawdown_pct(self) -> float:
        if self.portfolio_curve.empty:
            return 0.0
        roll_max = self.portfolio_curve.cummax()
        drawdown = (self.portfolio_curve - roll_max) / roll_max * 100
        return float(drawdown.min())

    @property
    def sharpe_ratio(self) -> float:
        """Annualised Sharpe (risk-free = 0) from daily portfolio returns."""
        if len(self.portfolio_curve) < 2:
            return 0.0
        daily_returns = self.portfolio_curve.pct_change().dropna()
        if daily_returns.std() == 0:
            return 0.0
        return float(daily_returns.mean() / daily_returns.std() * (252 ** 0.5))

    def print_summary(self) -> None:
        sep = "=" * 60
        print(f"\n{sep}")
        print(f"  BACKTEST RESULTS — {self.strategy_name}")
        print(sep)
        print(f"  Initial Capital : ${self.initial_capital:>12,.2f}")
        print(f"  Final Value     : ${self.final_value:>12,.2f}")
        print(f"  Total Return    : {self.total_return_pct:>+11.2f}%")
        print(f"  Total P&L       : ${self.total_pnl:>+11.2f}")
        print(f"  Max Drawdown    : {self.max_drawdown_pct:>+11.2f}%")
        print(f"  Sharpe Ratio    : {self.sharpe_ratio:>12.2f}")
        print(f"  Num Trades      : {self.num_trades:>12d}")
        print(f"  Win Rate        : {self.win_rate:>11.1f}%")
        print(f"  Avg P&L / Trade : ${self.avg_pnl_per_trade:>+11.2f}")
        print(sep)

        if self.trade_history:
            print("\n  --- Trade Log (first 20) ---")
            print(f"  {'Symbol':<8} {'Qty':>6} {'Entry':>9} {'Exit':>9} {'P&L':>10}")
            print(f"  {'-'*8} {'-'*6} {'-'*9} {'-'*9} {'-'*10}")
            for t in self.trade_history[:20]:
                print(
                    f"  {t['symbol']:<8} {t['qty']:>6.0f} "
                    f"${t['entry']:>8.2f} ${t['exit']:>8.2f} "
                    f"${t['pnl']:>+9.2f}"
                )
            if len(self.trade_history) > 20:
                print(f"  ... and {len(self.trade_history) - 20} more trades")
        print()


# ======================================================================
# Backtest Engine
# ======================================================================

class BacktestEngine:

    def __init__(self, config: dict, polygon_feed: PolygonDataFeed, logger: TradeLogger):
        self._config = config
        self._polygon = polygon_feed
        self._logger = logger

    def run(
        self,
        strategy_class: Type[BaseStrategy],
        symbols: List[str],
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        initial_capital: Optional[float] = None,
        extra_kwargs: Optional[dict] = None,
    ) -> BacktestResult:
        """
        Run *strategy_class* against historical daily bars for *symbols*.

        Returns a BacktestResult with full performance metrics.

        *extra_kwargs* are forwarded to the strategy constructor (use this
        to pass a FinnhubClient to EarningsSwing, for example).
        """
        bt_cfg = self._config.get("backtest", {})
        start = start_date or bt_cfg.get("start_date", "2023-01-01")
        end = end_date or bt_cfg.get("end_date", "2023-12-31")
        capital = initial_capital or bt_cfg.get("initial_capital", 100_000.0)
        commission = bt_cfg.get("commission_per_trade", 0.0)

        self._logger.log_info(
            f"[Backtest] Running {strategy_class.__name__} | "
            f"{start} → {end} | capital=${capital:,.0f}"
        )

        # Fetch daily bars
        data: dict[str, pd.DataFrame] = {}
        for sym in symbols:
            df = self._polygon.get_daily_bars(sym, start, end)
            if df is not None and not df.empty:
                data[sym] = df
                self._logger.log_info(f"[Backtest] Loaded {len(df)} bars for {sym}")
            else:
                self._logger.log_warning(f"[Backtest] No data for {sym}, skipping")

        if not data:
            raise ValueError("No historical data was returned for any symbol")

        # Build a sorted list of all unique dates
        all_dates = sorted({ts for df in data.values() for ts in df.index})

        # Create simulated broker and collaborators
        sim_broker = SimulatedBroker(capital, commission)
        risk_cfg = self._config.get("risk", {})
        risk_mgr = RiskManager(risk_cfg, self._logger, broker=sim_broker)
        order_mgr = OrderManager(sim_broker, self._logger)

        # Instantiate the strategy
        kwargs = extra_kwargs or {}
        strategy = strategy_class(
            self._config, sim_broker, risk_mgr, order_mgr, self._logger, **kwargs
        )
        strategy.on_start()

        portfolio_values: dict = {}

        for i, ts in enumerate(all_dates):
            # 1. Fill any orders queued from the PREVIOUS bar using today's open
            bar_opens = {
                sym: float(df.loc[ts, "open"])
                for sym, df in data.items()
                if ts in df.index
            }
            sim_broker.fill_pending_orders(bar_opens)

            # 2. Update current prices with today's close
            for sym, df in data.items():
                if ts in df.index:
                    sim_broker.update_price(sym, float(df.loc[ts, "close"]))

            # 3. Start-of-day risk check
            risk_mgr.start_of_day(sim_broker.get_portfolio_value())

            # 4. Feed each symbol's bar to the strategy
            for sym, df in data.items():
                if ts not in df.index:
                    continue
                row = df.loc[ts].copy()
                row["symbol"] = sym
                row.name = ts
                strategy.on_bar(row)

            # 5. Record portfolio value
            portfolio_values[ts] = sim_broker.get_portfolio_value()

        # Close any remaining positions at the last known price
        for pos in sim_broker.get_positions():
            order_mgr.place_market_sell(
                pos.symbol, pos.qty, reason="Backtest end — close all"
            )
        # Final fill at last bar's close
        if all_dates:
            last_ts = all_dates[-1]
            last_closes = {
                sym: float(df.iloc[-1]["close"])
                for sym, df in data.items()
            }
            sim_broker.fill_pending_orders(last_closes)

        strategy.on_stop()

        portfolio_series = pd.Series(portfolio_values)

        result = BacktestResult(
            initial_capital=capital,
            final_value=sim_broker.get_portfolio_value(),
            trade_history=sim_broker.get_trade_history(),
            portfolio_curve=portfolio_series,
            strategy_name=strategy_class.__name__,
        )
        return result
