"""
Volume-RSI Swing Strategy — buy confirmed bottoms, avoid institutional dumping.

The core idea: when a stock is oversold (RSI), sellers are exhausting themselves
(volume drying up on down days), and there is no sign of institutions distributing
(no high-volume down days), a reversal is imminent.  We enter as RSI starts to curl
up and exit when overbought or stop-loss hits.

Entry conditions (ALL must be true):
  1. RSI < rsi_oversold AND RSI today > RSI yesterday (oversold + curling up)
  2. SMA50 is flattening: the rate of SMA decline is slowing over the last two
     sma_slope_period windows — catches stocks transitioning from downtrend to base,
     not stocks still in freefall
  3. Accumulation: avg volume on up days >= volume_dry_up_ratio * avg volume on down days
     (buyers stepping in on up days — confirms demand at the bottom)
  4. No institutional dumping: at most max_distribution_days in last
     distribution_lookback bars where volume > distribution_vol_ratio * avg
     AND price declined > distribution_price_drop_pct

Exit conditions (first triggered wins):
  A. RSI >= rsi_overbought  (take profit)
  B. Price drops stop_loss_pct below entry  (hard stop)

Config section: volume_rsi_swing
"""

import pandas as pd

from brokers.base_broker import BrokerBase
from monitor.logger import TradeLogger
from strategies.base_strategy import BaseStrategy, compute_rsi, compute_sma


class VolumeRsiSwing(BaseStrategy):

    def __init__(self, config: dict, broker: BrokerBase, risk_manager, order_manager, logger: TradeLogger):
        super().__init__(config, broker, risk_manager, order_manager, logger)

        cfg = config.get("volume_rsi_swing", {})
        self.rsi_period: int        = cfg.get("rsi_period", 14)
        self.rsi_oversold: float    = cfg.get("rsi_oversold", 35)
        self.rsi_overbought: float  = cfg.get("rsi_overbought", 65)
        self.rsi_lookback: int      = cfg.get("rsi_lookback_bars", 10)
        self.sma_period: int        = cfg.get("sma_period", 50)
        self.sma_slope_period: int  = cfg.get("sma_slope_period", 5)  # bars per slope window

        # Volume dry-up
        self.vol_lookback: int      = cfg.get("volume_lookback", 20)
        self.vol_avg_period: int    = cfg.get("volume_avg_period", 20)
        self.dry_up_ratio: float    = cfg.get("volume_dry_up_ratio", 0.7)

        # Distribution day (institutional dumping) detection
        self.dist_lookback: int     = cfg.get("distribution_lookback", 10)
        self.dist_vol_ratio: float  = cfg.get("distribution_vol_ratio", 1.5)
        self.dist_price_drop: float = cfg.get("distribution_price_drop_pct", 0.01)
        self.max_dist_days: int     = cfg.get("max_distribution_days", 1)

        self._entry_prices: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_start(self) -> None:
        self.logger.log_info(
            f"[{self.name}] started | "
            f"RSI({self.rsi_period}) oversold<{self.rsi_oversold} "
            f"overbought>{self.rsi_overbought} | "
            f"SMA{self.sma_period} flattening (slope window={self.sma_slope_period}d) | "
            f"accumulation ratio>={self.dry_up_ratio:.0%} (up-vol/down-vol) | "
            f"max {self.max_dist_days} distribution day(s) in {self.dist_lookback}d"
        )

    def on_bar(self, bar: pd.Series) -> None:
        symbol = bar["symbol"]
        df = self._append_bar(bar)

        min_bars = max(self.sma_period, self.rsi_period, self.vol_avg_period) + 5
        if len(df) < min_bars:
            return

        position = self.broker.get_position(symbol)
        if position is not None:
            self._manage_exit(symbol, bar, position)
        else:
            self._check_entry(symbol, bar, df)

    def on_stop(self) -> None:
        self.logger.log_info(f"[{self.name}] shutting down")

    # ------------------------------------------------------------------
    # Entry
    # ------------------------------------------------------------------

    def _check_entry(self, symbol: str, bar: pd.Series, df: pd.DataFrame) -> None:
        close = float(bar["close"])
        closes = df["close"].astype(float)
        volumes = df["volume"].astype(float)

        rsi = compute_rsi(closes, self.rsi_period)
        rsi_now  = float(rsi.iloc[-1])
        rsi_prev = float(rsi.iloc[-2])

        if pd.isna(rsi_now) or pd.isna(rsi_prev):
            return

        # 1. RSI touched oversold within lookback window AND is now curling up
        rsi_window = rsi.iloc[-self.rsi_lookback:]
        rsi_recent_min = float(rsi_window.min())
        curling = rsi_now > rsi_prev
        touched_oversold = rsi_recent_min < self.rsi_oversold

        if not (touched_oversold and curling):
            return

        # 2. SMA flattening — rate of decline must be slowing (not still in freefall)
        flat_ok, flat_detail = self._check_sma_flattening(closes)
        if not flat_ok:
            self.logger.log_info(f"[{self.name}] {symbol}: skip — {flat_detail}")
            return

        # 3. Volume dry-up on down days
        dry_up_ok, dry_up_detail = self._check_volume_dry_up(df, closes, volumes)
        if not dry_up_ok:
            self.logger.log_info(f"[{self.name}] {symbol}: skip — {dry_up_detail}")
            return

        # 4. No institutional dumping
        dist_ok, dist_detail = self._check_no_distribution(df, closes, volumes)
        if not dist_ok:
            self.logger.log_info(f"[{self.name}] {symbol}: skip — {dist_detail}")
            return

        # Risk checks
        if not self.risk_manager.can_open_position(symbol, close):
            return
        qty = self.risk_manager.calculate_position_size(close)
        if qty <= 0:
            return

        self._entry_prices[symbol] = close
        self.order_manager.place_market_buy(
            symbol, qty,
            reason=(
                f"RSI={rsi_now:.1f} curling up (3-bar min={rsi_recent_min:.1f} touched <{self.rsi_oversold}) | "
                f"{flat_detail} | "
                f"{dry_up_detail} | {dist_detail}"
            ),
            strategy=self.name,
        )

    def _check_sma_flattening(self, closes: pd.Series):
        """
        Computes SMA slope over two consecutive windows and checks that the
        recent slope is less negative than the prior slope (decline slowing).

        recent_slope = SMA[-1] - SMA[-N]           (last N bars)
        prior_slope  = SMA[-N] - SMA[-2N]          (prior N bars)

        Passes when recent_slope > prior_slope — the downtrend is losing momentum.
        Also passes when both slopes are positive (uptrend — always fine to enter).
        Returns (passed: bool, detail: str).
        """
        sma = compute_sma(closes, self.sma_period)
        n = self.sma_slope_period
        need = self.sma_period + 2 * n

        if len(sma) < need or pd.isna(sma.iloc[-1]):
            return False, f"not enough bars for SMA{self.sma_period} flattening check"

        sma_now   = float(sma.iloc[-1])
        sma_mid   = float(sma.iloc[-(n + 1)])
        sma_old   = float(sma.iloc[-(2 * n + 1)])

        if sma_old == 0:
            return False, "SMA value is zero"

        recent_slope = (sma_now - sma_mid) / n   # change per bar, last N bars
        prior_slope  = (sma_mid - sma_old) / n   # change per bar, prior N bars

        # Express as % of price for readability
        price = float(closes.iloc[-1])
        recent_pct = recent_slope / price * 100
        prior_pct  = prior_slope  / price * 100

        # Block ONLY when SMA is declining AND accelerating AND price is also still falling.
        # If price has already turned up, SMA will follow — that's a normal bottoming pattern.
        price_3bar_chg = (float(closes.iloc[-1]) - float(closes.iloc[-4])) / float(closes.iloc[-4])
        price_turning  = price_3bar_chg > 0

        if prior_slope < 0 and recent_slope < prior_slope:
            if price_turning:
                return True, (
                    f"SMA{self.sma_period} still declining ({prior_pct:+.2f}%→{recent_pct:+.2f}%/bar) "
                    f"but price already turned up {price_3bar_chg:+.2%} in last 3 bars — bottoming pattern"
                )
            return False, (
                f"SMA{self.sma_period} accelerating down: "
                f"slope {prior_pct:+.2f}% → {recent_pct:+.2f}% per bar, price also falling (freefall)"
            )
        elif prior_slope >= 0:
            return True, (
                f"SMA{self.sma_period} in uptrend: "
                f"slope {prior_pct:+.2f}% → {recent_pct:+.2f}% per bar"
            )
        else:
            return True, (
                f"SMA{self.sma_period} decline slowing: "
                f"slope {prior_pct:+.2f}% → {recent_pct:+.2f}% per bar"
            )

    def _check_volume_dry_up(self, df: pd.DataFrame, closes: pd.Series, volumes: pd.Series):
        """
        Accumulation check: up-day avg volume must be at least dry_up_ratio of down-day avg volume.
        Buyers stepping in (up-vol >= down-vol) confirms accumulation at the bottom.
        Returns (passed: bool, detail: str).
        """
        recent = df.iloc[-self.vol_lookback:]
        prev_closes = closes.shift(1)
        up_mask   = closes >= prev_closes
        down_mask = closes <  prev_closes

        recent_up   = recent[up_mask.reindex(recent.index,   fill_value=False)]
        recent_down = recent[down_mask.reindex(recent.index, fill_value=False)]

        if recent_down.empty:
            return True, "no down days in lookback (very bullish)"
        if recent_up.empty:
            return False, "no up days in lookback (relentless selling)"

        up_vol_avg   = float(recent_up["volume"].astype(float).mean())
        down_vol_avg = float(recent_down["volume"].astype(float).mean())
        ratio = up_vol_avg / down_vol_avg  # >1 means buyers louder than sellers

        if ratio >= self.dry_up_ratio:
            return True, (
                f"accumulation: up-day vol {up_vol_avg:,.0f} = {ratio:.0%} of down-day vol "
                f"(buyers ≥ sellers)"
            )
        else:
            return False, (
                f"no accumulation: up-day vol only {ratio:.0%} of down-day vol "
                f"(<{self.dry_up_ratio:.0%} threshold — sellers still dominant)"
            )

    def _check_no_distribution(self, df: pd.DataFrame, closes: pd.Series, volumes: pd.Series):
        """
        Distribution day = volume > dist_vol_ratio * avg AND price fell > dist_price_drop.
        Returns (passed: bool, detail: str).
        """
        recent = df.iloc[-self.dist_lookback:]
        avg_vol = float(volumes.iloc[-self.vol_avg_period:].mean())
        if avg_vol == 0:
            return True, "no volume data"

        vol_threshold = avg_vol * self.dist_vol_ratio

        dist_days = 0
        prev_closes = closes.shift(1)
        for ts in recent.index:
            vol = float(recent.loc[ts, "volume"])
            c   = float(recent.loc[ts, "close"])
            pc  = float(prev_closes.loc[ts]) if ts in prev_closes.index else c
            if pc > 0 and vol > vol_threshold and (pc - c) / pc > self.dist_price_drop:
                dist_days += 1

        if dist_days <= self.max_dist_days:
            return True, f"{dist_days} distribution day(s) in {self.dist_lookback}d (≤{self.max_dist_days} allowed)"
        else:
            return False, f"{dist_days} distribution days in {self.dist_lookback}d — institutional dumping detected"

    # ------------------------------------------------------------------
    # Exit
    # ------------------------------------------------------------------

    def _manage_exit(self, symbol: str, bar: pd.Series, position) -> None:
        close = float(bar["close"])
        closes = self._buffers[symbol]["close"].astype(float)

        # Stop-loss
        entry = self._entry_prices.get(symbol, position.avg_entry_price)
        stop  = entry * (1 - self.risk_manager.stop_loss_pct)
        if close <= stop:
            self.order_manager.place_market_sell(
                symbol, position.qty,
                reason=f"Stop-loss hit: close={close:.2f} stop={stop:.2f}",
                strategy=self.name,
            )
            self._entry_prices.pop(symbol, None)
            return

        # RSI overbought — take profit
        rsi = compute_rsi(closes, self.rsi_period)
        rsi_now = float(rsi.iloc[-1])
        if not pd.isna(rsi_now) and rsi_now >= self.rsi_overbought:
            self.order_manager.place_market_sell(
                symbol, position.qty,
                reason=f"RSI overbought @ {rsi_now:.1f}",
                strategy=self.name,
            )
            self._entry_prices.pop(symbol, None)
