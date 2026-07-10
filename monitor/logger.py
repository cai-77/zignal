"""
Structured logging for trades, signals, errors, and P&L.

- System messages go to logs/system.log and console
- Every trade is appended to a JSON Lines file (logs/trades_YYYYMMDD.jsonl)
  for easy post-session analysis
"""

import json
import logging
import logging.handlers
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional


@dataclass
class TradeRecord:
    timestamp: str
    symbol: str
    action: str          # "buy" | "sell"
    qty: float
    price: float
    order_id: str
    strategy: str
    reason: str
    pnl: Optional[float] = None  # populated on sells when known


class TradeLogger:
    def __init__(self, log_dir: str = "logs"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(exist_ok=True)

        self._setup_system_logger()

        today = datetime.now().strftime("%Y%m%d")
        self._trade_log = self.log_dir / f"trades_{today}.jsonl"

        self.daily_pnl: float = 0.0
        self.daily_trades: list[TradeRecord] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def log_trade(self, record: TradeRecord) -> None:
        self.daily_trades.append(record)
        with open(self._trade_log, "a") as fh:
            fh.write(json.dumps(asdict(record)) + "\n")
        side_label = record.action.upper()
        pnl_str = f"  P&L ${record.pnl:+.2f}" if record.pnl is not None else ""
        self._sys.info(
            f"TRADE  | {side_label:4s} {record.qty:>8.2f} {record.symbol:<6} "
            f"@ ${record.price:>9.2f}{pnl_str} | {record.reason}"
        )

    def log_signal(
        self,
        symbol: str,
        strategy: str,
        action: str,
        reason: str,
        price: float,
    ) -> None:
        self._sys.info(
            f"SIGNAL | {strategy} | {action.upper():4s} {symbol:<6} "
            f"@ ${price:>9.2f} | {reason}"
        )

    def log_info(self, message: str) -> None:
        self._sys.info(message)

    def log_warning(self, message: str) -> None:
        self._sys.warning(message)

    def log_error(self, message: str, exc: Optional[Exception] = None) -> None:
        if exc:
            self._sys.error(message, exc_info=True)
        else:
            self._sys.error(message)

    def update_daily_pnl(self, delta: float) -> None:
        self.daily_pnl += delta
        self._sys.info(f"Daily P&L updated: ${self.daily_pnl:+.2f}")

    def print_daily_summary(self) -> None:
        buys = [t for t in self.daily_trades if t.action == "buy"]
        sells = [t for t in self.daily_trades if t.action == "sell"]
        realized = sum(t.pnl for t in sells if t.pnl is not None)

        separator = "=" * 64
        print(f"\n{separator}")
        print(f"  DAILY SUMMARY  {datetime.now().strftime('%Y-%m-%d')}")
        print(separator)
        print(f"  Realized P&L : ${realized:+.2f}")
        print(f"  Total Trades : {len(self.daily_trades)}  "
              f"(buys={len(buys)}, sells={len(sells)})")
        print(f"  Trade log    : {self._trade_log}")
        print(separator)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _setup_system_logger(self) -> None:
        self._sys = logging.getLogger("trading_system")
        if self._sys.handlers:
            return  # already configured

        self._sys.setLevel(logging.INFO)
        fmt = logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s",
                                datefmt="%Y-%m-%d %H:%M:%S")

        console = logging.StreamHandler()
        console.setFormatter(fmt)
        self._sys.addHandler(console)

        file_handler = logging.FileHandler(self.log_dir / "system.log")
        file_handler.setFormatter(fmt)
        self._sys.addHandler(file_handler)
