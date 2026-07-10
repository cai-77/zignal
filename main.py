"""
Trading System — Entry Point

Usage:
    python main.py --mode backtest    # run backtest on Polygon historical data
    python main.py --mode paper       # connect to Alpaca paper trading, run live
    python main.py --mode live        # connect to active_broker, run with real money
    python main.py --mode setup       # print setup checklist and exit
    python main.py --mode dashboard   # launch Streamlit dashboard

Options:
    --config PATH        path to config.yaml  (default: config/config.yaml)
    --strategy NAME      override active_strategy from config
    --symbols SYM...     override watchlist from config  (e.g. --symbols AAPL MSFT)
    --start YYYY-MM-DD   backtest start date
    --end   YYYY-MM-DD   backtest end date
"""

import argparse
import subprocess
import sys
from pathlib import Path

import yaml

from db.database import DatabaseManager


# ======================================================================
# Config
# ======================================================================

def load_config(path: str) -> dict:
    with open(path) as fh:
        return yaml.safe_load(fh)


# ======================================================================
# Factory helpers
# ======================================================================

def build_broker(config: dict, logger):
    active = config.get("active_broker", "alpaca")
    if active == "alpaca":
        from brokers.alpaca_broker import AlpacaBroker
        broker = AlpacaBroker(config["alpaca"])
    elif active == "ibkr":
        from brokers.ibkr_broker import IBKRBroker
        broker = IBKRBroker(config["ibkr"])
    else:
        raise ValueError(f"Unknown broker '{active}'. Choose 'alpaca' or 'ibkr' in config.yaml.")
    broker.connect()
    logger.log_info(f"Broker connected: {active}")
    return broker


def build_strategy(name: str, config: dict, broker, risk_manager, order_manager, logger, finnhub=None):
    if name == "rsi_mean_reversion":
        from strategies.rsi_mean_reversion import RsiMeanReversion
        return RsiMeanReversion(config, broker, risk_manager, order_manager, logger)

    elif name == "moving_average_crossover":
        from strategies.moving_average_crossover import MovingAverageCrossover
        return MovingAverageCrossover(config, broker, risk_manager, order_manager, logger)

    elif name == "earnings_swing":
        from strategies.earnings_swing import EarningsSwing
        return EarningsSwing(config, broker, risk_manager, order_manager, logger, finnhub=finnhub)

    elif name == "volume_rsi_swing":
        from strategies.volume_rsi_swing import VolumeRsiSwing
        return VolumeRsiSwing(config, broker, risk_manager, order_manager, logger)

    else:
        raise ValueError(
            f"Unknown strategy '{name}'. "
            "Choose: rsi_mean_reversion | moving_average_crossover | earnings_swing | volume_rsi_swing"
        )


def build_strategy_class(name: str):
    if name == "rsi_mean_reversion":
        from strategies.rsi_mean_reversion import RsiMeanReversion
        return RsiMeanReversion
    elif name == "moving_average_crossover":
        from strategies.moving_average_crossover import MovingAverageCrossover
        return MovingAverageCrossover
    elif name == "earnings_swing":
        from strategies.earnings_swing import EarningsSwing
        return EarningsSwing
    elif name == "volume_rsi_swing":
        from strategies.volume_rsi_swing import VolumeRsiSwing
        return VolumeRsiSwing
    else:
        raise ValueError(f"Unknown strategy '{name}'")


# ======================================================================
# Modes
# ======================================================================

def run_backtest(config: dict, strategy_name: str, symbols: list[str], start: str, end: str, db: DatabaseManager):
    from data.polygon_feed import PolygonDataFeed
    from data.finnhub_client import FinnhubClient
    from monitor.logger import TradeLogger
    from backtesting.backtest_engine import BacktestEngine

    logger = TradeLogger()
    logger.log_info("=== BACKTEST MODE ===")

    polygon = PolygonDataFeed(config["polygon"]["api_key"], logger)
    polygon.connect()

    extra_kwargs: dict = {}
    if strategy_name == "earnings_swing":
        finnhub = FinnhubClient(config["finnhub"]["api_key"], logger)
        finnhub.connect()
        extra_kwargs["finnhub"] = finnhub

    engine = BacktestEngine(config, polygon, logger)
    strategy_class = build_strategy_class(strategy_name)

    result = engine.run(
        strategy_class=strategy_class,
        symbols=symbols,
        start_date=start,
        end_date=end,
        extra_kwargs=extra_kwargs,
    )
    result.print_summary()

    run_id = db.save_backtest_run(result, strategy_name, symbols, start, end)
    print(f"  Backtest saved to database (id={run_id})\n")

    # Save portfolio curve to CSV
    curve_path = Path("logs") / f"backtest_curve_{strategy_name}.csv"
    curve_path.parent.mkdir(exist_ok=True)
    result.portfolio_curve.to_csv(curve_path, header=["portfolio_value"])
    print(f"  Portfolio curve saved to {curve_path}\n")


def run_paper_or_live(config: dict, strategy_name: str, symbols: list[str], db: DatabaseManager, session_type: str):
    """
    Connect to Alpaca (paper) or the configured active broker (live),
    subscribe to real-time bars, and run the strategy in an event loop.
    """
    from data.alpaca_feed import AlpacaDataFeed
    from data.finnhub_client import FinnhubClient
    from monitor.logger import TradeLogger
    from risk.risk_manager import RiskManager
    from execution.order_manager import OrderManager

    logger = TradeLogger()
    logger.log_info(f"=== LIVE/PAPER MODE | strategy={strategy_name} ===")

    broker = build_broker(config, logger)
    risk_mgr = RiskManager(config["risk"], logger, broker=broker)
    order_mgr = OrderManager(broker, logger, db=db, session_type=session_type)

    # Initialise daily risk tracking
    acct = broker.get_account()
    risk_mgr.start_of_day(acct.portfolio_value)
    logger.log_info(f"Account: equity=${acct.equity:,.2f}  cash=${acct.cash:,.2f}")

    db.save_event("session_start", f"Started {session_type} trading | strategy={strategy_name} | symbols={symbols}")

    finnhub = None
    if strategy_name == "earnings_swing":
        finnhub = FinnhubClient(config["finnhub"]["api_key"], logger)
        finnhub.connect()

    strategy = build_strategy(
        strategy_name, config, broker, risk_mgr, order_mgr, logger, finnhub
    )
    strategy.on_start()

    _bar_count = 0

    def on_bar(bar_data):
        """Callback from the data feed; called in the streaming thread."""
        nonlocal _bar_count
        import pandas as pd
        bar = pd.Series({
            "symbol": bar_data.symbol,
            "open": bar_data.open,
            "high": bar_data.high,
            "low": bar_data.low,
            "close": bar_data.close,
            "volume": bar_data.volume,
        })
        bar.name = bar_data.timestamp

        # Daily loss check before every bar
        acct = broker.get_account()
        if not risk_mgr.check_daily_loss_limit(acct.portfolio_value):
            logger.log_warning("Daily loss limit active — skipping bar")
            db.save_event("daily_limit", "Daily loss limit reached — trading halted for today")
            return

        strategy.on_bar(bar)

        # Save portfolio snapshot every 6 bars (~30 min for 5-min bars)
        _bar_count += 1
        if _bar_count % 6 == 0:
            acct = broker.get_account()
            db.save_snapshot(
                session_type=session_type,
                portfolio_value=acct.portfolio_value,
                cash=acct.cash,
                daily_pnl=logger.daily_pnl,
            )

    alpaca_cfg = config["alpaca"]
    feed = AlpacaDataFeed(
        api_key=alpaca_cfg["api_key"],
        secret_key=alpaca_cfg["secret_key"],
        logger=logger,
    )
    feed.subscribe(symbols, callback=on_bar)

    try:
        logger.log_info(f"Streaming bars for: {symbols}")
        feed.start()  # blocks until Ctrl-C
    except KeyboardInterrupt:
        logger.log_info("Interrupted by user")
    finally:
        strategy.on_stop()
        feed.stop()
        broker.disconnect()
        logger.print_daily_summary()
        db.save_event("session_stop", f"Stopped {session_type} trading")


# ======================================================================
# Setup Checklist
# ======================================================================

def print_setup_checklist():
    print("""
╔══════════════════════════════════════════════════════════════════╗
║              TRADING SYSTEM — SETUP CHECKLIST                   ║
╚══════════════════════════════════════════════════════════════════╝

── STEP 1: API Keys ──────────────────────────────────────────────

  1. Alpaca (paper + live trading + real-time data)
     → Sign up at https://alpaca.markets
     → Create a paper trading app under "Paper Trading" → "API Keys"
     → Copy API Key + Secret Key into config/config.yaml

  2. Polygon.io (historical OHLCV for backtesting)
     → Sign up at https://polygon.io
     → Free tier supports end-of-day data (sufficient for daily backtests)
     → Paid tiers add real-time and intraday data
     → Copy your API Key into config/config.yaml

  3. Finnhub (earnings calendar)
     → Sign up at https://finnhub.io
     → Free tier: 60 API calls/min — enough for a watchlist of ~20 tickers
     → Copy your API Key into config/config.yaml

  4. Interactive Brokers (optional — for live day trading)
     → Requires a funded IBKR brokerage account
     → Install TWS (Trader Workstation) or IB Gateway
     → Enable API in TWS: File → Global Config → API → Settings
       ✓ Enable ActiveX and Socket Clients
       ✓ Socket port: 7497 (paper) or 7496 (live)
     → No separate API key needed — ib_insync connects via local socket

── STEP 2: Install Dependencies ─────────────────────────────────

  cd trading_system
  pip install -r requirements.txt

── STEP 3: Configure ─────────────────────────────────────────────

  Edit config/config.yaml:
    • Paste your API keys
    • Set active_broker: alpaca   (or ibkr)
    • Set active_strategy: earnings_swing   (or rsi_mean_reversion / moving_average_crossover)
    • Adjust watchlist, risk settings, and strategy parameters

── STEP 4: Run Your First Backtest ───────────────────────────────

  python main.py --mode backtest --strategy earnings_swing

  With custom date range:
  python main.py --mode backtest --strategy rsi_mean_reversion \\
    --start 2022-01-01 --end 2022-12-31 --symbols AAPL MSFT NVDA

── STEP 5: Paper Trading ─────────────────────────────────────────

  python main.py --mode paper

  This connects to Alpaca paper trading and streams real-time bars.
  Watch the logs/ directory for trade records and system logs.
  Press Ctrl-C to stop gracefully.

── STEP 6: Live Trading ──────────────────────────────────────────

  # Switch the broker and URL first:
  # In config.yaml:
  #   active_broker: alpaca
  #   alpaca.base_url: https://api.alpaca.markets

  python main.py --mode live

  ⚠  This trades real money. Run in paper mode for at least
     2-4 weeks before switching to live.

── Switching Brokers ─────────────────────────────────────────────

  In config.yaml, change:
    active_broker: ibkr

  Then ensure TWS or IB Gateway is running before starting.

── File Locations ────────────────────────────────────────────────

  Logs:            logs/system.log
  Trade journal:   logs/trades_YYYYMMDD.jsonl
  Backtest curve:  logs/backtest_curve_<strategy>.csv
  Config:          config/config.yaml

""")


# ======================================================================
# CLI
# ======================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Modular Algorithmic Trading System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--mode",
        choices=["backtest", "paper", "live", "setup", "dashboard"],
        default="setup",
        help="Operating mode (default: setup — prints checklist)",
    )
    parser.add_argument(
        "--config",
        default="config/config.yaml",
        help="Path to config.yaml (default: config/config.yaml)",
    )
    parser.add_argument(
        "--strategy",
        help="Override active_strategy from config.yaml",
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        help="Override watchlist from config.yaml",
    )
    parser.add_argument("--start", help="Backtest start date (YYYY-MM-DD)")
    parser.add_argument("--end", help="Backtest end date (YYYY-MM-DD)")

    args = parser.parse_args()

    if args.mode == "setup":
        print_setup_checklist()
        sys.exit(0)

    if args.mode == "dashboard":
        dashboard_path = Path(__file__).parent / "dashboard" / "app.py"
        subprocess.run([
            str(Path(__file__).parent / ".venv" / "bin" / "streamlit"),
            "run", str(dashboard_path),
            "--server.headless", "false",
        ])
        sys.exit(0)

    db = DatabaseManager("db/trading.db")

    config = load_config(args.config)
    strategy_name = args.strategy or config.get("active_strategy", "earnings_swing")
    symbols = args.symbols or config.get("watchlist", [])

    if not symbols:
        print("ERROR: No symbols specified. Set watchlist in config.yaml or pass --symbols.")
        sys.exit(1)

    if args.mode == "backtest":
        bt = config.get("backtest", {})
        start = args.start or bt.get("start_date", "2025-01-01")
        end = args.end or bt.get("end_date", "2025-06-01")
        run_backtest(config, strategy_name, symbols, start, end, db)

    elif args.mode in ("paper", "live"):
        if args.mode == "live":
            answer = input(
                "\n⚠  LIVE TRADING MODE — this will trade real money.\n"
                "Type 'yes' to confirm: "
            ).strip().lower()
            if answer != "yes":
                print("Aborted.")
                sys.exit(0)
        run_paper_or_live(config, strategy_name, symbols, db, session_type=args.mode)


if __name__ == "__main__":
    main()
