"""
Trading System Dashboard — Streamlit UI

Pages:
  Live      — real-time positions, account stats, event feed (auto-refresh 30s)
  Backtests — history of all backtest runs with equity curves and trade logs
  Trades    — full trade history across paper/live sessions
  Control   — edit config knobs and launch paper/live/backtest runs
"""

import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import yaml
from streamlit_autorefresh import st_autorefresh

# Resolve project root so imports work when launched from any directory
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from db.database import DatabaseManager

DB_PATH = str(ROOT / "db" / "trading.db")

st.set_page_config(
    page_title="Trading Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

db = DatabaseManager(DB_PATH)


# ======================================================================
# Helpers
# ======================================================================

def pnl_color(val: float) -> str:
    return "green" if val >= 0 else "red"


def fmt_pnl(val: float) -> str:
    return f"${val:+,.2f}"


def equity_chart(dates, values, title: str = "Portfolio Value") -> go.Figure:
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=dates, y=values,
        mode="lines",
        line=dict(color="#00b4d8", width=2),
        fill="tozeroy",
        fillcolor="rgba(0,180,216,0.08)",
        name="Portfolio Value",
    ))
    fig.update_layout(
        title=title,
        xaxis_title="Date",
        yaxis_title="Value ($)",
        height=350,
        margin=dict(l=0, r=0, t=40, b=0),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        xaxis=dict(gridcolor="rgba(255,255,255,0.1)"),
        yaxis=dict(gridcolor="rgba(255,255,255,0.1)"),
    )
    return fig


# ======================================================================
# Sidebar navigation
# ======================================================================

CONFIG_PATH = ROOT / "config" / "config.yaml"
SESSION_LOG = ROOT / "logs" / "session_output.log"
VENV_PYTHON = ROOT / ".venv" / "bin" / "python3"


def load_cfg() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def save_cfg(cfg: dict) -> None:
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)


def _python() -> str:
    return str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable


def _running_proc() -> "subprocess.Popen | None":
    proc = st.session_state.get("_proc")
    if proc is not None and proc.poll() is not None:
        st.session_state["_proc"] = None
        return None
    return proc


st.sidebar.title("Trading System")
page = st.sidebar.radio("Navigate", ["Live", "Backtests", "Trades", "Control", "Analyze", "Signal Audit"])
st.sidebar.markdown("---")
st.sidebar.caption("Auto-refreshes every 30s on Live page.")


# ======================================================================
# LIVE PAGE
# ======================================================================

if page == "Live":
    # Auto-refresh every 30 seconds
    st_autorefresh(interval=30_000, key="live_refresh")

    st.title("Live Trading")

    # ── Event feed (push-style: highlight events from last 30s) ──────
    all_events = db.get_recent_events(limit=50)

    # Show toast for very recent events (within last 35s to account for refresh lag)
    from datetime import datetime, timedelta
    cutoff = (datetime.now() - timedelta(seconds=35)).isoformat()
    new_events = [e for e in all_events if e["ts"] > cutoff]
    for ev in reversed(new_events):
        icon = {
            "trade_open": "🟢",
            "trade_close": "🔴",
            "stop_loss": "🛑",
            "daily_limit": "⚠️",
            "session_start": "▶️",
            "session_stop": "⏹️",
        }.get(ev["event_type"], "ℹ️")
        st.toast(f"{icon} {ev['message']}", icon=icon[0] if icon else "ℹ")

    # ── Portfolio curve (paper) ───────────────────────────────────────
    curve = db.get_live_curve(session_type="paper", limit=500)
    if curve:
        df_curve = pd.DataFrame(curve)
        df_curve["ts"] = pd.to_datetime(df_curve["ts"])

        col1, col2, col3 = st.columns(3)
        latest = df_curve.iloc[-1]
        first = df_curve.iloc[0]
        pnl_today = float(latest["daily_pnl"])
        total_change = float(latest["portfolio_value"]) - float(first["portfolio_value"])

        col1.metric("Portfolio Value", f"${latest['portfolio_value']:,.2f}", fmt_pnl(total_change))
        col2.metric("Cash", f"${latest['cash']:,.2f}")
        col3.metric("Daily P&L", fmt_pnl(pnl_today), delta_color="normal")

        st.plotly_chart(
            equity_chart(df_curve["ts"], df_curve["portfolio_value"], "Portfolio Curve — Paper"),
            use_container_width=True,
        )
    else:
        st.info("No live session data yet. Start paper trading with `./trade paper`.")

    # ── Recent events ─────────────────────────────────────────────────
    st.subheader("Event Feed")
    if all_events:
        rows = []
        for e in all_events:
            icon = {
                "trade_open": "🟢",
                "trade_close": "🔴",
                "stop_loss": "🛑",
                "daily_limit": "⚠️",
                "session_start": "▶️",
                "session_stop": "⏹️",
            }.get(e["event_type"], "ℹ️")
            rows.append({
                "Time": e["ts"][:19].replace("T", " "),
                "Type": f"{icon} {e['event_type']}",
                "Symbol": e["symbol"] or "—",
                "Message": e["message"],
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.caption("No events yet.")

    # ── Recent live trades ────────────────────────────────────────────
    st.subheader("Recent Trades")
    trades = db.get_live_trades(limit=50)
    if trades:
        df_t = pd.DataFrame(trades)
        df_t["ts"] = df_t["ts"].str[:19].str.replace("T", " ")
        df_t["pnl"] = df_t["pnl"].apply(lambda x: fmt_pnl(x) if x is not None else "—")
        df_t = df_t[["ts", "session_type", "symbol", "action", "qty", "price", "pnl", "strategy", "reason"]]
        df_t.columns = ["Time", "Session", "Symbol", "Action", "Qty", "Price", "P&L", "Strategy", "Reason"]
        st.dataframe(df_t, use_container_width=True, hide_index=True)
    else:
        st.caption("No live trades yet.")


# ======================================================================
# BACKTESTS PAGE
# ======================================================================

elif page == "Backtests":
    st.title("Backtest History")

    runs = db.get_backtest_runs()
    if not runs:
        st.info("No backtest runs yet. Run `./trade backtest` to get started.")
    else:
        # Summary table
        df_runs = pd.DataFrame(runs)
        df_runs["run_at"] = df_runs["run_at"].str[:19].str.replace("T", " ")
        df_runs["symbols"] = df_runs["symbols"].apply(lambda x: ", ".join(json.loads(x)))
        display = df_runs[[
            "id", "run_at", "strategy", "start_date", "end_date",
            "symbols", "total_return", "total_pnl", "sharpe_ratio",
            "num_trades", "win_rate", "max_drawdown",
        ]].copy()
        display.columns = [
            "ID", "Run At", "Strategy", "Start", "End",
            "Symbols", "Return %", "P&L $", "Sharpe",
            "Trades", "Win %", "Max DD %",
        ]
        display["Return %"] = display["Return %"].apply(lambda x: f"{x:+.2f}%")
        display["P&L $"] = display["P&L $"].apply(lambda x: f"${x:+,.2f}")
        display["Sharpe"] = display["Sharpe"].apply(lambda x: f"{x:.2f}")
        display["Win %"] = display["Win %"].apply(lambda x: f"{x:.1f}%")
        display["Max DD %"] = display["Max DD %"].apply(lambda x: f"{x:.2f}%")

        st.dataframe(display, use_container_width=True, hide_index=True)

        # Drill into a specific run
        st.markdown("---")
        run_ids = [r["id"] for r in runs]
        selected_id = st.selectbox(
            "Select a run to inspect",
            options=run_ids,
            format_func=lambda i: next(
                f"#{i} — {r['strategy']} ({r['start_date']} → {r['end_date']})"
                for r in runs if r["id"] == i
            ),
        )

        selected = next(r for r in runs if r["id"] == selected_id)

        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Total Return", f"{selected['total_return']:+.2f}%")
        col2.metric("Sharpe Ratio", f"{selected['sharpe_ratio']:.2f}")
        col3.metric("Win Rate", f"{selected['win_rate']:.1f}%")
        col4.metric("Max Drawdown", f"{selected['max_drawdown']:.2f}%")

        # Equity curve
        curve = db.get_backtest_curve(selected_id)
        if curve:
            df_c = pd.DataFrame(curve)
            df_c["ts"] = pd.to_datetime(df_c["ts"])
            st.plotly_chart(
                equity_chart(
                    df_c["ts"], df_c["value"],
                    f"Equity Curve — {selected['strategy']} ({selected['start_date']} → {selected['end_date']})"
                ),
                use_container_width=True,
            )

        # Trade log
        trades = db.get_backtest_trades(selected_id)
        if trades:
            st.subheader(f"Trades ({len(trades)})")
            df_t = pd.DataFrame(trades)
            df_t["pnl"] = df_t["pnl"].apply(fmt_pnl)
            df_t["entry"] = df_t["entry"].apply(lambda x: f"${x:.2f}")
            df_t["exit"] = df_t["exit"].apply(lambda x: f"${x:.2f}")
            df_t = df_t[["symbol", "qty", "entry", "exit", "pnl"]]
            df_t.columns = ["Symbol", "Qty", "Entry", "Exit", "P&L"]
            st.dataframe(df_t, use_container_width=True, hide_index=True)
        else:
            st.caption("No trades in this run.")


# ======================================================================
# TRADES PAGE
# ======================================================================

elif page == "Trades":
    st.title("Live & Paper Trade History")

    trades = db.get_live_trades(limit=500)
    if not trades:
        st.info("No live or paper trades yet.")
    else:
        df = pd.DataFrame(trades)
        df["ts"] = df["ts"].str[:19].str.replace("T", " ")
        df["pnl"] = df["pnl"].apply(lambda x: fmt_pnl(x) if x is not None else "—")
        df["price"] = df["price"].apply(lambda x: f"${x:.2f}")

        # Filters
        col1, col2 = st.columns(2)
        sessions = ["All"] + sorted(df["session_type"].unique().tolist())
        symbols = ["All"] + sorted(df["symbol"].unique().tolist())
        sel_session = col1.selectbox("Session", sessions)
        sel_symbol = col2.selectbox("Symbol", symbols)

        filtered = df.copy()
        if sel_session != "All":
            filtered = filtered[filtered["session_type"] == sel_session]
        if sel_symbol != "All":
            filtered = filtered[filtered["symbol"] == sel_symbol]

        filtered = filtered[["ts", "session_type", "symbol", "action", "qty", "price", "pnl", "strategy", "reason"]]
        filtered.columns = ["Time", "Session", "Symbol", "Action", "Qty", "Price", "P&L", "Strategy", "Reason"]

        st.dataframe(filtered, use_container_width=True, hide_index=True)
        st.caption(f"Showing {len(filtered)} trades")


# ======================================================================
# CONTROL PAGE
# ======================================================================

elif page == "Control":
    proc = _running_proc()
    if proc is not None:
        st_autorefresh(interval=5_000, key="ctrl_refresh")

    st.title("Control Center")

    cfg = load_cfg()

    # ── Config Form ───────────────────────────────────────────────────
    with st.form("config_form"):
        st.subheader("System")
        col1, col2 = st.columns(2)
        STRATEGIES = [
            "volume_rsi_swing",
            "rsi_mean_reversion",
            "moving_average_crossover",
            "earnings_swing",
        ]
        new_broker = col1.selectbox(
            "Active Broker",
            ["alpaca", "ibkr"],
            index=["alpaca", "ibkr"].index(cfg.get("active_broker", "alpaca")),
        )
        cur_strat = cfg.get("active_strategy", "volume_rsi_swing")
        new_strategy = col2.selectbox(
            "Active Strategy",
            STRATEGIES,
            index=STRATEGIES.index(cur_strat) if cur_strat in STRATEGIES else 0,
        )
        new_watchlist = st.text_input(
            "Watchlist (comma-separated)",
            ", ".join(cfg.get("watchlist", [])),
        )

        st.divider()

        # ── Risk ──────────────────────────────────────────────────────
        st.subheader("Risk Management")
        risk = cfg.get("risk", {})
        c1, c2, c3, c4 = st.columns(4)
        new_max_pos = c1.number_input(
            "Max Position %", 0.5, 25.0, float(risk.get("max_position_pct", 0.05)) * 100,
            step=0.5, format="%.1f", key="risk_max_pos",
            help="Max % of portfolio in a single position",
        )
        new_max_heat = c2.number_input(
            "Max Portfolio Heat %", 5.0, 80.0, float(risk.get("max_portfolio_heat", 0.20)) * 100,
            step=1.0, format="%.0f", key="risk_max_heat",
            help="Max total capital at risk across all open positions",
        )
        new_stop_loss = c3.number_input(
            "Stop Loss %", 0.5, 15.0, float(risk.get("stop_loss_pct", 0.02)) * 100,
            step=0.1, format="%.1f", key="risk_stop_loss",
            help="Hard stop-loss below entry price",
        )
        new_daily_loss = c4.number_input(
            "Max Daily Loss %", 0.5, 15.0, float(risk.get("max_daily_loss_pct", 0.03)) * 100,
            step=0.1, format="%.1f", key="risk_daily_loss",
            help="Halt all trading if daily loss exceeds this",
        )

        st.divider()

        # ── Strategy Params ───────────────────────────────────────────
        st.subheader("Strategy Parameters")
        tab_vrs, tab_es, tab_rsi, tab_mac = st.tabs([
            "Volume RSI Swing", "Earnings Swing", "RSI Mean Reversion", "MA Crossover",
        ])

        # Volume RSI Swing
        with tab_vrs:
            vrs = cfg.get("volume_rsi_swing", {})
            v1, v2, v3, v4 = st.columns(4)
            vrs_rsi_period     = v1.number_input("RSI Period",         5, 30,  int(vrs.get("rsi_period", 14)),          key="vrs_rsi_period")
            vrs_rsi_oversold   = v2.number_input("RSI Oversold",      15, 55,  int(vrs.get("rsi_oversold", 40)),        key="vrs_rsi_oversold")
            vrs_rsi_overbought = v3.number_input("RSI Overbought",    50, 85,  int(vrs.get("rsi_overbought", 65)),      key="vrs_rsi_overbought")
            vrs_rsi_lookback   = v4.number_input("RSI Lookback Bars",  3, 30,  int(vrs.get("rsi_lookback_bars", 10)),   key="vrs_rsi_lookback",
                                                  help="How many bars back to look for an oversold touch")
            va, vb = st.columns(2)
            vrs_sma_period     = va.number_input("SMA Period",        10, 200, int(vrs.get("sma_period", 50)),          key="vrs_sma_period")
            vrs_sma_slope      = vb.number_input("SMA Slope Window",   2, 20,  int(vrs.get("sma_slope_period", 5)),     key="vrs_sma_slope")
            st.caption("Volume")
            vc, vd, ve = st.columns(3)
            vrs_vol_lookback   = vc.number_input("Volume Lookback",    5, 60,  int(vrs.get("volume_lookback", 20)),     key="vrs_vol_lookback")
            vrs_vol_avg        = vd.number_input("Volume Avg Period",  5, 60,  int(vrs.get("volume_avg_period", 20)),   key="vrs_vol_avg")
            vrs_dry_up         = ve.number_input("Dry-Up Ratio",    0.30, 1.50, float(vrs.get("volume_dry_up_ratio", 0.80)),
                                                  step=0.05, format="%.2f", key="vrs_dry_up",
                                                  help="Up-day avg vol must be >= this ratio of down-day avg vol")
            st.caption("Distribution Detection")
            vf, vg, vh, vi = st.columns(4)
            vrs_dist_lookback  = vf.number_input("Dist Lookback",      5, 30,  int(vrs.get("distribution_lookback", 10)),       key="vrs_dist_lookback")
            vrs_dist_vol       = vg.number_input("Dist Vol Ratio",   1.1, 4.0, float(vrs.get("distribution_vol_ratio", 1.5)),   key="vrs_dist_vol",  step=0.1, format="%.1f")
            vrs_dist_drop      = vh.number_input("Dist Price Drop %", 0.1, 5.0, float(vrs.get("distribution_price_drop_pct", 0.01)) * 100, key="vrs_dist_drop", step=0.1, format="%.1f")
            vrs_max_dist       = vi.number_input("Max Dist Days",       0, 5,   int(vrs.get("max_distribution_days", 1)),        key="vrs_max_dist")

        # Earnings Swing
        with tab_es:
            sw = cfg.get("swing", {})
            e1, e2 = st.columns(2)
            sw_entry_weeks = e1.number_input("Entry Weeks Before Earnings", 1, 8,   int(sw.get("entry_weeks_before_earnings", 4)), key="es_entry_weeks")
            sw_exit_days   = e2.number_input("Exit Days Before Earnings",   1, 10,  int(sw.get("exit_days_before_earnings", 3)),   key="es_exit_days")
            e3, e4, e5 = st.columns(3)
            sw_rsi_min   = e3.number_input("RSI Min at Entry", 20, 60,  int(sw.get("rsi_min", 40)),    key="es_rsi_min")
            sw_rsi_max   = e4.number_input("RSI Max at Entry", 40, 80,  int(sw.get("rsi_max", 60)),    key="es_rsi_max")
            sw_sma       = e5.number_input("SMA Period",       10, 200, int(sw.get("sma_period", 50)), key="es_sma_period")

        # RSI Mean Reversion
        with tab_rsi:
            dt = cfg.get("day_trading", {})
            r1, r2, r3 = st.columns(3)
            dt_rsi_period     = r1.number_input("RSI Period",      5, 30, int(dt.get("rsi_period", 14)),      key="dt_rsi_period")
            dt_rsi_oversold   = r2.number_input("RSI Oversold",   15, 50, int(dt.get("rsi_oversold", 30)),    key="dt_rsi_oversold")
            dt_rsi_overbought = r3.number_input("RSI Overbought", 50, 85, int(dt.get("rsi_overbought", 70)),  key="dt_rsi_overbought")

        # MA Crossover
        with tab_mac:
            m1, m2 = st.columns(2)
            dt_fast_ema = m1.number_input("Fast EMA Period",  2,  50, int(dt.get("fast_ema_period", 9)),   key="mac_fast_ema")
            dt_slow_ema = m2.number_input("Slow EMA Period",  5, 200, int(dt.get("slow_ema_period", 21)),  key="mac_slow_ema")

        st.divider()

        # ── Backtest Settings ──────────────────────────────────────────
        st.subheader("Backtest Settings")
        bt = cfg.get("backtest", {})
        import datetime as _dt
        b1, b2, b3, b4 = st.columns(4)
        bt_start = b1.date_input(
            "Start Date",
            value=_dt.date.fromisoformat(bt.get("start_date", "2024-01-01")),
            key="bt_start",
        )
        bt_end = b2.date_input(
            "End Date",
            value=_dt.date.fromisoformat(bt.get("end_date", "2024-12-31")),
            key="bt_end",
        )
        bt_capital = b3.number_input(
            "Initial Capital ($)", 1000.0, 10_000_000.0,
            float(bt.get("initial_capital", 100_000.0)),
            step=1000.0, format="%.0f", key="bt_capital",
        )
        bt_commission = b4.number_input(
            "Commission / Trade ($)", 0.0, 50.0,
            float(bt.get("commission_per_trade", 0.0)),
            step=0.5, format="%.2f", key="bt_commission",
        )

        saved = st.form_submit_button("Save Config", type="primary")

    if saved:
        cfg["active_broker"]   = new_broker
        cfg["active_strategy"] = new_strategy
        cfg["watchlist"]       = [s.strip() for s in new_watchlist.split(",") if s.strip()]

        cfg["risk"] = {
            "max_position_pct":   round(new_max_pos   / 100, 4),
            "max_portfolio_heat": round(new_max_heat  / 100, 4),
            "stop_loss_pct":      round(new_stop_loss / 100, 4),
            "max_daily_loss_pct": round(new_daily_loss/ 100, 4),
        }

        cfg["volume_rsi_swing"] = {
            "rsi_period":                int(vrs_rsi_period),
            "rsi_oversold":              int(vrs_rsi_oversold),
            "rsi_overbought":            int(vrs_rsi_overbought),
            "rsi_lookback_bars":         int(vrs_rsi_lookback),
            "sma_period":                int(vrs_sma_period),
            "sma_slope_period":          int(vrs_sma_slope),
            "volume_lookback":           int(vrs_vol_lookback),
            "volume_avg_period":         int(vrs_vol_avg),
            "volume_dry_up_ratio":       round(float(vrs_dry_up), 2),
            "distribution_lookback":     int(vrs_dist_lookback),
            "distribution_vol_ratio":    round(float(vrs_dist_vol), 2),
            "distribution_price_drop_pct": round(float(vrs_dist_drop) / 100, 4),
            "max_distribution_days":     int(vrs_max_dist),
        }

        cfg["swing"] = {
            "entry_weeks_before_earnings": int(sw_entry_weeks),
            "exit_days_before_earnings":   int(sw_exit_days),
            "rsi_min":                     int(sw_rsi_min),
            "rsi_max":                     int(sw_rsi_max),
            "sma_period":                  int(sw_sma),
        }

        cfg["day_trading"].update({
            "rsi_period":     int(dt_rsi_period),
            "rsi_oversold":   int(dt_rsi_oversold),
            "rsi_overbought": int(dt_rsi_overbought),
            "fast_ema_period": int(dt_fast_ema),
            "slow_ema_period": int(dt_slow_ema),
        })

        cfg["backtest"] = {
            "start_date":          str(bt_start),
            "end_date":            str(bt_end),
            "initial_capital":     float(bt_capital),
            "commission_per_trade": float(bt_commission),
        }

        save_cfg(cfg)
        st.success("Config saved to config/config.yaml")
        st.rerun()

    st.divider()

    # ── Run Controls ──────────────────────────────────────────────────
    st.subheader("Run")

    proc = _running_proc()
    is_running = proc is not None

    if is_running:
        mode_label = st.session_state.get("_proc_mode", "process")
        st.info(f"Running: **{mode_label}** (PID {proc.pid})")
        if st.button("Stop", type="primary"):
            proc.terminate()
            st.session_state["_proc"] = None
            st.rerun()
    else:
        st.caption("Config is saved before each launch.")
        col1, col2, col3 = st.columns(3)

        if col1.button("Run Backtest", use_container_width=True):
            save_cfg(cfg)
            log_fh = open(SESSION_LOG, "w")
            cmd = [_python(), str(ROOT / "main.py"), "--mode", "backtest",
                   "--start", str(bt_start), "--end", str(bt_end)]
            st.session_state["_proc"] = subprocess.Popen(
                cmd, stdout=log_fh, stderr=subprocess.STDOUT, cwd=str(ROOT),
            )
            st.session_state["_proc_mode"] = f"backtest ({bt_start} → {bt_end})"
            st.rerun()

        if col2.button("Start Paper Trading", use_container_width=True):
            save_cfg(cfg)
            log_fh = open(SESSION_LOG, "w")
            st.session_state["_proc"] = subprocess.Popen(
                [_python(), str(ROOT / "main.py"), "--mode", "paper"],
                stdout=log_fh, stderr=subprocess.STDOUT, cwd=str(ROOT),
            )
            st.session_state["_proc_mode"] = "paper trading"
            st.rerun()

        with col3:
            live_confirmed = st.checkbox("I confirm — this trades REAL money")
            if st.button("Start Live Trading", use_container_width=True, disabled=not live_confirmed):
                save_cfg(cfg)
                log_fh = open(SESSION_LOG, "w")
                st.session_state["_proc"] = subprocess.Popen(
                    [_python(), str(ROOT / "main.py"), "--mode", "live"],
                    input=b"yes\n",
                    stdout=log_fh, stderr=subprocess.STDOUT,
                    stdin=subprocess.PIPE,
                    cwd=str(ROOT),
                )
                st.session_state["_proc_mode"] = "LIVE trading"
                st.rerun()

    # ── Session Output ────────────────────────────────────────────────
    st.divider()
    st.subheader("Session Output")
    if SESSION_LOG.exists():
        content = SESSION_LOG.read_text()
        if content.strip():
            lines = content.splitlines()
            display = "\n".join(lines[-200:])  # last 200 lines
            st.code(display, language=None)
            st.caption(f"{len(lines)} lines total — showing last 200")
        else:
            st.caption("Log is empty.")
    else:
        st.caption("No session log yet — launch a run to see output here.")


# ======================================================================
# ANALYZE PAGE
# ======================================================================

elif page == "Analyze":
    import datetime as _dt
    from datetime import datetime
    import os as _os
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    from dashboard.signal_analyzer import fetch_bars, analyze, analyze_exit
    from dashboard.llm_analyzer import (
        build_prompt, call_llm,
        fetch_earnings_context, fetch_news_context,
        fetch_insider_context, fetch_market_context,
    )
    from dashboard.analysis_store import init_db, save_analysis, load_analysis
    init_db()

    st.title("Trade Signal Analyzer")
    st.caption(
        "Run Layer 1 (rule engine) first. Review the verdict and conditions, "
        "then request AI analysis when you want a deeper read."
    )

    # ── Session state init ────────────────────────────────────────────
    _az_keys = [
        "az_result", "az_df", "az_vrs_cfg", "az_symbol", "az_end_date",
        "az_start_date", "az_analysis_type", "az_cost_basis",
        "az_polygon_key", "az_finnhub_key", "az_llm_key", "az_llm_model",
        "az_llm_result", "az_llm_ctx_meta", "az_cached_at",
    ]
    for _k in _az_keys:
        if _k not in st.session_state:
            st.session_state[_k] = None

    # ── Inputs — row 1 ───────────────────────────────────────────────
    col1, col2, col3, col4 = st.columns([2, 2, 2, 1])
    symbol     = col1.text_input("Symbol", st.session_state.get("az_symbol") or "MSFT").upper().strip()
    _today     = _dt.date.today()
    start_date = col2.date_input("From (start of window)", _today - _dt.timedelta(days=30))
    end_date   = col3.date_input("To (end of window)", _today)
    col4.markdown("<br>", unsafe_allow_html=True)
    run_btn    = col4.button("Analyze", type="primary", use_container_width=True, key="az_run_btn")

    # ── Inputs — row 2: analysis type + cost basis + auto-AI checkbox ─
    r2a, r2b, r2c = st.columns([2, 2, 2])
    analysis_type_label = r2a.radio(
        "Analysis type",
        ["Entry (Buy)", "Exit (Sell)"],
        horizontal=True,
        key="az_analysis_type_radio",
    )
    analysis_type = "exit" if analysis_type_label.startswith("Exit") else "entry"
    cost_basis: "float | None" = None
    if analysis_type == "exit":
        cb_val = r2b.number_input(
            "Your entry price / cost basis ($)",
            min_value=0.01, value=100.00, step=0.01, format="%.2f",
            key="az_cost_basis_input",
        )
        cost_basis = float(cb_val)
    auto_ai = r2c.checkbox(
        "Auto-run AI on ENTER / WAIT",
        value=False,
        key="az_auto_ai",
        help="When checked, AI analysis runs automatically after Layer 1 if the verdict is ENTER or WAIT.",
    )

    # ── Helper: run AI and store results in session state ─────────────
    def _run_ai(force_refresh: bool = False):
        _sym      = st.session_state.az_symbol
        _df       = st.session_state.az_df
        _result   = st.session_state.az_result
        _vrs      = st.session_state.az_vrs_cfg
        _atype    = st.session_state.az_analysis_type
        _cb       = st.session_state.az_cost_basis
        _end      = str(st.session_state.az_end_date)
        _start    = str(st.session_state.az_start_date)
        _poly_key = st.session_state.az_polygon_key
        _fh_key   = st.session_state.az_finnhub_key
        _llm_key  = st.session_state.az_llm_key
        _llm_mdl  = st.session_state.az_llm_model

        # ── Check cache first (unless user explicitly wants a fresh call) ──
        if not force_refresh:
            cached_res, cached_meta, cached_at = load_analysis(_sym, _end, _atype)
            if cached_res is not None:
                st.session_state.az_llm_result   = cached_res
                st.session_state.az_llm_ctx_meta = cached_meta
                st.session_state.az_cached_at    = cached_at
                return

        # ── Cache miss — call the API ──────────────────────────────────────
        st.session_state.az_cached_at = None
        ctx_status = st.status("Gathering context for AI analysis…", expanded=False)
        with ctx_status:
            st.write("Fetching market context (SPY / VIXY)…")
            market_ctx = fetch_market_context(_poly_key, _end)

            st.write("Fetching earnings schedule…")
            earnings_ctx = fetch_earnings_context(_sym, _fh_key) if _fh_key else None

            st.write("Fetching recent news…")
            news_ctx = fetch_news_context(_sym, _fh_key, start_date=_start) if _fh_key else []

            st.write("Fetching insider transactions…")
            insider_ctx = fetch_insider_context(_sym, _fh_key, start_date=_start) if _fh_key else []

            st.write(f"Running AI analysis ({_llm_mdl})…")
            prompt_data = build_prompt(
                _sym, _df, _result, _vrs,
                analysis_type=_atype,
                cost_basis=_cb,
                earnings_ctx=earnings_ctx,
                news_ctx=news_ctx,
                insider_ctx=insider_ctx,
                market_ctx=market_ctx,
            )
            llm_res = call_llm(prompt_data, _llm_key, _llm_mdl, _atype)
        ctx_status.update(label="AI analysis complete", state="complete", expanded=False)

        ctx_meta = {
            "market": market_ctx is not None,
            "earnings": earnings_ctx,
            "news_count": len(news_ctx) if news_ctx else 0,
            "insider_count": len(insider_ctx) if insider_ctx else 0,
        }
        st.session_state.az_llm_result   = llm_res
        st.session_state.az_llm_ctx_meta = ctx_meta

        # ── Save to DB (only on success) ───────────────────────────────────
        if not llm_res.error:
            save_analysis(
                symbol=_sym,
                analysis_date=_end,
                analysis_type=_atype,
                layer1_verdict=_result.verdict,
                llm_result=llm_res,
                ctx_meta=ctx_meta,
                cost_basis=_cb,
            )

    # ── Layer 1: run on Analyze click ────────────────────────────────
    if run_btn and symbol:
        cfg = load_cfg()
        polygon_key = cfg.get("polygon", {}).get("api_key", "")
        if not polygon_key:
            st.error("Polygon API key missing — add it in config/config.yaml under `polygon.api_key`.")
            st.stop()

        warmup_start = start_date - _dt.timedelta(days=150)
        with st.spinner(f"Fetching {symbol} bars from Polygon ({warmup_start} → {end_date})…"):
            df_full = fetch_bars(symbol, str(warmup_start), str(end_date), polygon_key)

        if df_full is None or df_full.empty:
            st.error(f"No data returned for **{symbol}**. Check the symbol and your Polygon API key.")
            st.stop()

        vrs_cfg = cfg.get("volume_rsi_swing", {})
        result  = (
            analyze_exit(df_full, symbol, vrs_cfg, cost_basis)
            if analysis_type == "exit"
            else analyze(df_full, symbol, vrs_cfg)
        )

        if result.error:
            st.warning(result.error)
            st.stop()

        llm_cfg = cfg.get("llm", {})

        # Store Layer 1 results; clear any previous AI result
        st.session_state.az_result        = result
        st.session_state.az_df            = df_full
        st.session_state.az_vrs_cfg       = vrs_cfg
        st.session_state.az_symbol        = symbol
        st.session_state.az_end_date      = end_date
        st.session_state.az_start_date    = start_date
        st.session_state.az_analysis_type = analysis_type
        st.session_state.az_cost_basis    = cost_basis
        st.session_state.az_polygon_key   = polygon_key
        st.session_state.az_finnhub_key   = cfg.get("finnhub", {}).get("api_key", "")
        st.session_state.az_llm_key       = _os.environ.get("ANTHROPIC_API_KEY") or llm_cfg.get("api_key", "")
        st.session_state.az_llm_model     = llm_cfg.get("model", "claude-opus-4-8")
        st.session_state.az_llm_result    = None
        st.session_state.az_llm_ctx_meta  = None
        st.session_state.az_cached_at     = None

        # Auto-run AI immediately if checkbox enabled and verdict warrants it
        if auto_ai and result.verdict in ("ENTER", "WAIT") and st.session_state.az_llm_key:
            _run_ai()

    # ── Display Layer 1 results (persisted in session state) ──────────
    if st.session_state.az_result is not None:
        result        = st.session_state.az_result
        df_full       = st.session_state.az_df
        vrs_cfg       = st.session_state.az_vrs_cfg
        symbol        = st.session_state.az_symbol
        end_date      = st.session_state.az_end_date
        start_date    = st.session_state.az_start_date
        analysis_type = st.session_state.az_analysis_type

        # ── Verdict badge ─────────────────────────────────────────────
        _verdict_colors = {"ENTER": "#00c853", "WAIT": "#ff9800", "REJECT": "#f44336"}
        # Exit mode remaps internal ENTER/WAIT/REJECT → HOLD/CAUTION/EXIT for display
        _exit_label_map = {"ENTER": "HOLD", "WAIT": "CAUTION", "REJECT": "EXIT"}
        _display_verdict = (
            _exit_label_map.get(result.verdict, result.verdict)
            if analysis_type == "exit" else result.verdict
        )
        bg = _verdict_colors.get(result.verdict, "#888")
        st.markdown(
            f'<div style="background:{bg};color:white;padding:16px 24px;border-radius:10px;'
            f'font-size:2rem;font-weight:700;letter-spacing:2px;display:inline-block;'
            f'margin-bottom:8px;">{_display_verdict}</div>',
            unsafe_allow_html=True,
        )
        st.markdown(f"**{result.verdict_reason}**")
        st.markdown(f"*Analysis as of last bar: {str(df_full.index[-1])[:10]}*")

        st.divider()

        # ── Condition breakdown with ✅ / ❌ and info popovers ───────────
        st.subheader("Exit Condition Breakdown" if analysis_type == "exit" else "Entry Condition Breakdown")

        _CONDITION_DOCS = {
            "RSI": (
                "#### RSI — Oversold & Curling Up\n\n"
                "The **Relative Strength Index (RSI)** recently fell *below* the oversold "
                "threshold (default: 40), signalling that selling pressure may be exhausted. "
                "The condition also requires the RSI to start **curling back up** within the "
                "lookback window — a turn in momentum, not just a low reading.\n\n"
                "**Why it matters:** An oversold RSI alone can mean a stock is in freefall. "
                "The curl confirms buyers are beginning to step in."
            ),
            "SMA": (
                "#### SMA — Trend Flattening (Not Freefall)\n\n"
                "Compares the **slope of the SMA** over recent bars vs the prior window. "
                "A deeply negative slope (freefall) is a REJECT. A flattening or upward-"
                "turning slope passes.\n\n"
                "**Why it matters:** Entering while the trend is still in freefall risks "
                "catching a falling knife. A flattening SMA suggests the downtrend is "
                "losing momentum and a base may be forming."
            ),
            "Volume — Accumulation": (
                "#### Volume — Accumulation (Buyers vs Sellers)\n\n"
                "Compares average **up-day volume** vs average **down-day volume** over the "
                "lookback window. Passes when up-day volume is ≥ 80% of down-day volume.\n\n"
                "**Why it matters:** If institutions are quietly accumulating, up-day volume "
                "will keep pace with down-day volume even as price drifts lower. This pattern "
                "— price weak but buyers absorbing supply — precedes many strong breakouts."
            ),
            "Institutional Dumping": (
                "#### Institutional Dumping — Distribution Days\n\n"
                "A **distribution day** is a session where price drops > 1% on volume "
                "significantly above the average (default: 1.5× avg). Too many distribution "
                "days in the lookback window (default: max 1) triggers a REJECT.\n\n"
                "**Why it matters:** Distribution days reveal that large institutions are "
                "actively selling (distributing) their shares. Entering while smart money is "
                "exiting is a losing proposition regardless of other signals."
            ),
        }

        def _get_cond_doc(name: str) -> str:
            for key, doc in _CONDITION_DOCS.items():
                if key.lower() in name.lower():
                    return doc
            return "No additional description available for this condition."

        # Style condition name popovers to look like plain text links
        st.markdown("""
        <style>
        div[data-testid="stPopoverButton"] button {
            background: transparent !important;
            border: none !important;
            padding: 0 !important;
            color: #58a6ff !important;
            font-weight: 600 !important;
            font-size: 0.95rem !important;
            text-decoration: underline !important;
            text-decoration-style: dotted !important;
            text-underline-offset: 3px !important;
            cursor: pointer !important;
            min-height: 0 !important;
            height: auto !important;
            line-height: 1.4 !important;
            text-align: left !important;
            justify-content: flex-start !important;
        }
        div[data-testid="stPopoverButton"] button:hover {
            color: #79b8ff !important;
        }
        div[data-testid="stPopoverButton"] button p {
            font-size: 0.95rem !important;
            font-weight: 600 !important;
            text-decoration: underline !important;
            text-decoration-style: dotted !important;
        }
        </style>
        """, unsafe_allow_html=True)

        # Column headers
        hc0, hc1, hc2 = st.columns([0.4, 4.5, 6])
        hc0.markdown("<span style='color:#888;font-size:0.8rem'>PASS</span>", unsafe_allow_html=True)
        hc1.markdown("<span style='color:#888;font-size:0.8rem'>CONDITION  (click to learn more)</span>", unsafe_allow_html=True)
        hc2.markdown("<span style='color:#888;font-size:0.8rem'>DETAIL</span>", unsafe_allow_html=True)
        st.markdown("<hr style='margin:2px 0 8px 0;border-color:#333'>", unsafe_allow_html=True)

        for i, c in enumerate(result.conditions):
            rc0, rc1, rc2 = st.columns([0.4, 4.5, 6])
            rc0.markdown(
                f"<div style='font-size:1.2rem;line-height:2'>{'✅' if c.passed else '❌'}</div>",
                unsafe_allow_html=True,
            )
            with rc1.popover(c.name, use_container_width=False):
                st.markdown(_get_cond_doc(c.name))
            rc2.markdown(
                f"<span style='color:#ccc;font-size:0.85rem'>{c.detail}</span>",
                unsafe_allow_html=True,
            )
            if i < len(result.conditions) - 1:
                st.markdown("<hr style='margin:4px 0;border-color:#222'>", unsafe_allow_html=True)

        # ── AI Analysis — shown immediately after conditions ──────────
        st.divider()
        st.subheader("AI Analysis")

        llm_key = st.session_state.az_llm_key

        if not llm_key:
            st.info(
                "No Anthropic API key found. Set the `ANTHROPIC_API_KEY` environment variable "
                "or add `llm.api_key` in **config/config.yaml** under `llm.api_key` to enable AI analysis."
            )
        elif st.session_state.az_llm_result is None:
            ai_btn = st.button("Get AI Analysis", type="primary", key="az_ai_btn")
            if ai_btn:
                _run_ai()
                st.rerun()

        if st.session_state.az_llm_result is not None:
            llm_result = st.session_state.az_llm_result
            cached_at  = st.session_state.get("az_cached_at")
            meta       = st.session_state.az_llm_ctx_meta or {}

            # Escape characters that break Streamlit's markdown renderer.
            # $ triggers LaTeX math mode (strips spaces, renders ' as prime ′).
            def _md(text: str) -> str:
                return text.replace('$', r'\$')

            if llm_result.error:
                st.error(f"AI analysis failed: {llm_result.error}")
            else:
                _llm_colors = {
                    "ENTER": "#00c853", "WAIT": "#ff9800",
                    "REJECT": "#f44336", "CAUTION": "#9c27b0",
                    "HOLD": "#1976d2", "EXIT": "#f44336",
                    "EXIT_PARTIAL": "#ff9800", "TIGHTEN_STOP": "#9c27b0",
                }
                llm_bg    = _llm_colors.get(llm_result.verdict, "#555")
                conf_dots = {"high": "●●●", "medium": "●●○", "low": "●○○"}.get(llm_result.confidence, "")

                # ── Action bar: cache badge | refresh | download ──────────
                _left, _right = st.columns([6, 4])
                with _left:
                    if cached_at:
                        _age = datetime.utcnow() - datetime.fromisoformat(cached_at)
                        _h, _m = divmod(int(_age.total_seconds() // 60), 60)
                        _age_str = f"{_h}h {_m}m ago" if _h else f"{_m}m ago"
                        st.success(f"💾 Loaded from cache · saved {_age_str} · no API cost")
                with _right:
                    _rb, _db = st.columns(2)
                    if _rb.button("🔄 Refresh", key="az_refresh_btn",
                                  help="Discard cache and call AI again"):
                        _run_ai(force_refresh=True)
                        st.rerun()

                    # ── Build HTML report for download ────────────────────
                    def _build_report_html() -> str:
                        _verdict_color    = _llm_colors.get(llm_result.verdict, "#555")
                        _l1_verdict_color = {"ENTER": "#00c853", "WAIT": "#ff9800", "REJECT": "#f44336"}.get(result.verdict, "#888")
                        _l1_display_label = (
                            {"ENTER": "HOLD", "WAIT": "CAUTION", "REJECT": "EXIT"}.get(result.verdict, result.verdict)
                            if analysis_type == "exit" else result.verdict
                        )
                        _generated = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

                        # Condition table rows
                        _l1_rows = "".join(
                            f"<tr><td style='font-size:1.1rem;width:32px'>{'✅' if c.passed else '❌'}</td>"
                            f"<td style='width:220px'><b>{c.name}</b></td>"
                            f"<td style='color:#555;font-size:0.88rem'>{c.detail}</td></tr>"
                            for c in result.conditions
                        )

                        # AI analysis text — split on double newlines to get proper paragraphs
                        _analysis_paras = "".join(
                            f"<p>{para.strip().replace(chr(10), '<br>')}</p>"
                            for para in llm_result.analysis.split("\n\n") if para.strip()
                        )
                        _obs_items  = "".join(f"<li>{o}</li>" for o in llm_result.key_observations)
                        _risk_items = "".join(f"<li>{r}</li>" for r in llm_result.risks)
                        _watch      = f'<div class="watch"><b>Watch for:</b> {llm_result.watch_for}</div>' if llm_result.watch_for else ""

                        # Context footer items
                        _ctx_parts = []
                        if meta.get("market"):
                            _ctx_parts.append("Market context (SPY / VIXY)")
                        if meta.get("earnings"):
                            _e = meta["earnings"]
                            try:
                                import datetime as _dt2
                                _fresh_days = (_dt2.date.fromisoformat(_e['date']) - _dt2.date.today()).days
                            except Exception:
                                _fresh_days = _e.get("days_away", "?")
                            _ctx_parts.append(f"Earnings: {_e['date']} ({_fresh_days}d away — verify independently)")
                        if meta.get("news_count"):
                            _ctx_parts.append(f"{meta['news_count']} recent news articles")
                        if meta.get("insider_count"):
                            _ctx_parts.append(f"{meta['insider_count']} insider transactions")
                        _ctx_html = "".join(f"<li>{p}</li>" for p in _ctx_parts) or "<li>Price / indicator data only</li>"

                        # Chart — rebuild from session state data
                        _display_start = pd.Timestamp(start_date, tz="UTC")
                        _dfd = df_full[df_full.index >= _display_start].copy()
                        _chart_html = ""
                        if not _dfd.empty:
                            _rsi_d  = result.rsi_series.reindex(_dfd.index)
                            _sma_d  = result.sma_series.reindex(_dfd.index)
                            _dist_d = result.dist_day_flags.reindex(_dfd.index, fill_value=False)
                            _dates  = _dfd.index
                            _rsi_os = float(vrs_cfg.get("rsi_oversold", 40))
                            _rsi_ob = float(vrs_cfg.get("rsi_overbought", 65))
                            _sma_p  = vrs_cfg.get("sma_period", 50)
                            _cfig = make_subplots(
                                rows=3, cols=1, shared_xaxes=True,
                                row_heights=[0.55, 0.25, 0.20], vertical_spacing=0.08,
                                subplot_titles=(f"{symbol} — Price & SMA{_sma_p}", "RSI", "Volume"),
                            )
                            _cfig.add_trace(go.Candlestick(
                                x=_dates, open=_dfd["open"], high=_dfd["high"],
                                low=_dfd["low"], close=_dfd["close"],
                                increasing_line_color="#26a69a", decreasing_line_color="#ef5350",
                                name="Price", showlegend=False,
                            ), row=1, col=1)
                            _cfig.add_trace(go.Scatter(x=_dates, y=_sma_d,
                                line=dict(color="#ff9800", width=1.5), name=f"SMA{_sma_p}"), row=1, col=1)
                            _dd = _dates[_dist_d.values]
                            if len(_dd):
                                _cfig.add_trace(go.Scatter(x=_dd, y=_dfd.loc[_dd,"high"]*1.01,
                                    mode="markers", marker=dict(symbol="triangle-down", color="#f44336", size=10),
                                    name="Distribution Day"), row=1, col=1)
                            _x0, _x1 = _dates[0], _dates[-1]
                            for _y0, _y1, _col in [
                                (0, _rsi_os, "rgba(0,200,83,0.10)"),
                                (_rsi_ob, 100, "rgba(244,67,54,0.10)"),
                            ]:
                                _cfig.add_trace(go.Scatter(x=[_x0,_x1,_x1,_x0], y=[_y0,_y0,_y1,_y1],
                                    fill="toself", fillcolor=_col, line=dict(width=0),
                                    showlegend=False, hoverinfo="skip"), row=2, col=1)
                            _cfig.add_trace(go.Scatter(x=_dates, y=_rsi_d,
                                line=dict(color="#00b4d8", width=2), name="RSI"), row=2, col=1)
                            _up = _dfd["close"] >= _dfd["open"]
                            _cfig.add_trace(go.Bar(x=_dates, y=_dfd["volume"],
                                marker_color=["#26a69a" if u else "#ef5350" for u in _up],
                                showlegend=False), row=3, col=1)
                            _cfig.update_layout(
                                height=620,
                                margin=dict(l=60, r=20, t=50, b=40),
                                paper_bgcolor="white",
                                plot_bgcolor="#fafafa",
                                font=dict(color="#222"),
                                xaxis=dict(rangeslider_visible=False, gridcolor="#e0e0e0"),
                                xaxis2=dict(gridcolor="#e0e0e0"),
                                xaxis3=dict(gridcolor="#e0e0e0"),
                                yaxis=dict(gridcolor="#e0e0e0"),
                                yaxis2=dict(gridcolor="#e0e0e0", range=[0, 100]),
                                yaxis3=dict(gridcolor="#e0e0e0"),
                                legend=dict(orientation="h", y=1.02, x=1, xanchor="right"),
                            )
                            # include_plotlyjs=True embeds the full Plotly.js library so
                            # the file is self-contained and works offline / on any device
                            _chart_html = _cfig.to_html(
                                include_plotlyjs=True, full_html=False,
                                config={"displayModeBar": False},
                            )

                        return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>{symbol} — Trade Analysis Report</title>
<style>
  body      {{ font-family: Georgia, serif; max-width: 900px; margin: 40px auto; padding: 0 28px; color: #222; line-height: 1.6; }}
  h1        {{ font-size: 1.7rem; border-bottom: 2px solid #333; padding-bottom: 10px; margin-bottom: 4px; }}
  h2        {{ font-size: 1.05rem; margin-top: 32px; margin-bottom: 6px; color: #333;
               border-bottom: 1px solid #ddd; padding-bottom: 4px; text-transform: uppercase;
               letter-spacing: 0.5px; }}
  p         {{ line-height: 1.8; margin: 0 0 14px 0; }}
  .verdict  {{ display:inline-block; padding: 8px 22px; border-radius: 6px; color: white;
               font-size: 1.3rem; font-weight: 700; letter-spacing: 2px; margin: 8px 0; }}
  .summary  {{ font-style: italic; color: #444; margin: 10px 0 18px 0; font-size: 1.02rem; line-height: 1.7; }}
  table     {{ width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 0.9rem; }}
  th        {{ text-align: left; border-bottom: 2px solid #ccc; padding: 5px 8px; color: #666;
               font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.4px; }}
  td        {{ padding: 8px 8px; border-bottom: 1px solid #eee; vertical-align: top; }}
  .two-col  {{ display: grid; grid-template-columns: 1fr 1fr; gap: 28px; margin-top: 8px; }}
  ul        {{ margin: 6px 0; padding-left: 20px; }}
  li        {{ margin-bottom: 6px; line-height: 1.6; }}
  .watch    {{ background: #fffbe6; border-left: 4px solid #f0a500; padding: 10px 14px;
               margin: 16px 0; border-radius: 0 4px 4px 0; font-size: 0.95rem; }}
  .chart    {{ margin: 16px 0; }}
  .meta     {{ font-size: 0.8rem; color: #999; margin-top: 36px; border-top: 1px solid #ddd;
               padding-top: 10px; line-height: 1.8; }}
  @media print {{
    body   {{ margin: 16px; }}
    .chart {{ page-break-inside: avoid; }}
  }}
</style>
</head><body>
<h1>{symbol} — Trade Analysis Report</h1>
<p style="color:#777;font-size:0.88rem;margin:0 0 24px 0">
  Analysis date: {str(end_date)} &nbsp;·&nbsp;
  Type: {analysis_type.title()} &nbsp;·&nbsp;
  Generated: {_generated}
</p>

<h2>Layer 1 — Rule Engine Verdict</h2>
<span class="verdict" style="background:{_l1_verdict_color}">{_l1_display_label}</span>
<p style="margin-top:10px">{result.verdict_reason}</p>
<table>
  <tr><th></th><th>Condition</th><th>Detail</th></tr>
  {_l1_rows}
</table>

<h2>Layer 2 — AI Analysis Verdict</h2>
<span class="verdict" style="background:{_verdict_color}">{llm_result.verdict}</span>
&nbsp;&nbsp;<b>Confidence:</b> {llm_result.confidence} {conf_dots}
<div class="summary">{llm_result.summary}</div>
{_analysis_paras}
{_watch}

<div class="two-col">
  <div>
    <h2>Key Observations</h2>
    <ul>{_obs_items}</ul>
  </div>
  <div>
    <h2>Risks</h2>
    <ul>{_risk_items}</ul>
  </div>
</div>

<h2>Charts</h2>
<div class="chart">{_chart_html}</div>

<h2>Context Used</h2>
<ul>{_ctx_html}</ul>

<div class="meta">
  Model: {llm_result.model_used} &nbsp;·&nbsp;
  {"Loaded from cache · saved " + cached_at if cached_at else "Live API call · " + _generated} &nbsp;·&nbsp;
  Data: Polygon.io (price/volume) · Finnhub (earnings/news/insiders)
</div>
</body></html>"""

                    _report_html = _build_report_html()
                    _filename = f"{symbol}_{str(end_date)}_{analysis_type}_analysis.html"
                    _db.download_button(
                        "⬇ Download Report",
                        data=_report_html,
                        file_name=_filename,
                        mime="text/html",
                        key="az_download_btn",
                        help="Download as HTML — open in browser then Ctrl+P → Save as PDF",
                    )

                # ── AI result display ─────────────────────────────────────
                v_col, s_col = st.columns([1, 3])
                with v_col:
                    st.markdown(
                        f'<div style="background:{llm_bg};color:white;padding:12px 20px;'
                        f'border-radius:8px;font-size:1.6rem;font-weight:700;'
                        f'letter-spacing:2px;text-align:center;">{llm_result.verdict}</div>',
                        unsafe_allow_html=True,
                    )
                with s_col:
                    st.markdown(
                        f"**Confidence:** {llm_result.confidence} {conf_dots}  \n"
                        f"*{_md(llm_result.summary)}*"
                    )

                st.markdown(_md(llm_result.analysis))

                obs_col, risk_col = st.columns(2)
                with obs_col:
                    with st.expander("Key Observations", expanded=True):
                        for obs in llm_result.key_observations:
                            st.markdown(f"- {_md(obs)}")
                with risk_col:
                    with st.expander("Risks", expanded=True):
                        for risk in llm_result.risks:
                            st.markdown(f"- {_md(risk)}")

                if llm_result.watch_for:
                    st.info(f"**Watch for:** {_md(llm_result.watch_for)}")

                ctx_parts = []
                if meta.get("market"):
                    ctx_parts.append("market context")
                if meta.get("earnings"):
                    _e = meta["earnings"]
                    try:
                        import datetime as _dt2
                        _fresh_days = (_dt2.date.fromisoformat(_e['date']) - _dt2.date.today()).days
                    except Exception:
                        _fresh_days = _e.get("days_away", "?")
                    ctx_parts.append(f"earnings {_e['date']} ({_fresh_days}d away — verify independently)")
                if meta.get("news_count"):
                    ctx_parts.append(f"{meta['news_count']} news articles")
                if meta.get("insider_count"):
                    ctx_parts.append(f"{meta['insider_count']} insider transactions")
                ctx_summary = ", ".join(ctx_parts) if ctx_parts else "price/indicator data only"
                st.caption(f"Analysis by {llm_result.model_used} · Context: {ctx_summary}")

        st.divider()

        # ── Charts ────────────────────────────────────────────────────
        st.subheader("Charts")
        display_start = pd.Timestamp(start_date, tz="UTC")
        df_display    = df_full[df_full.index >= display_start].copy()

        if df_display.empty:
            st.warning("No bars in the selected display range.")
        else:
            rsi_display  = result.rsi_series.reindex(df_display.index)
            sma_display  = result.sma_series.reindex(df_display.index)
            dist_display = result.dist_day_flags.reindex(df_display.index, fill_value=False)
            dates        = df_display.index

            fig = make_subplots(
                rows=3, cols=1,
                shared_xaxes=True,
                row_heights=[0.55, 0.25, 0.20],
                vertical_spacing=0.08,
                subplot_titles=(f"{symbol} — Price & SMA{vrs_cfg.get('sma_period', 50)}", "RSI", "Volume"),
            )

            fig.add_trace(go.Candlestick(
                x=dates, open=df_display["open"], high=df_display["high"],
                low=df_display["low"], close=df_display["close"],
                increasing_line_color="#26a69a", decreasing_line_color="#ef5350",
                name="Price", showlegend=False,
            ), row=1, col=1)
            fig.add_trace(go.Scatter(
                x=dates, y=sma_display,
                line=dict(color="#ff9800", width=1.5),
                name=f"SMA{vrs_cfg.get('sma_period', 50)}",
            ), row=1, col=1)
            dist_dates = dates[dist_display.values]
            if len(dist_dates) > 0:
                fig.add_trace(go.Scatter(
                    x=dist_dates, y=df_display.loc[dist_dates, "high"] * 1.01,
                    mode="markers", marker=dict(symbol="triangle-down", color="#f44336", size=12),
                    name="Distribution Day",
                ), row=1, col=1)

            rsi_os  = float(vrs_cfg.get("rsi_oversold", 40))
            rsi_ob  = float(vrs_cfg.get("rsi_overbought", 65))
            x0, x1 = dates[0], dates[-1]
            fig.add_trace(go.Scatter(x=[x0,x1,x1,x0], y=[0,0,rsi_os,rsi_os],
                fill="toself", fillcolor="rgba(0,200,83,0.10)", line=dict(width=0),
                showlegend=False, hoverinfo="skip"), row=2, col=1)
            fig.add_trace(go.Scatter(x=[x0,x1,x1,x0], y=[rsi_ob,rsi_ob,100,100],
                fill="toself", fillcolor="rgba(244,67,54,0.10)", line=dict(width=0),
                showlegend=False, hoverinfo="skip"), row=2, col=1)
            fig.add_trace(go.Scatter(x=[x0,x1], y=[rsi_os,rsi_os],
                line=dict(color="rgba(0,200,83,0.6)", width=1, dash="dot"),
                showlegend=False, hoverinfo="skip"), row=2, col=1)
            fig.add_trace(go.Scatter(x=[x0,x1], y=[rsi_ob,rsi_ob],
                line=dict(color="rgba(244,67,54,0.6)", width=1, dash="dot"),
                showlegend=False, hoverinfo="skip"), row=2, col=1)
            fig.add_trace(go.Scatter(x=dates, y=rsi_display,
                line=dict(color="#00b4d8", width=2), name="RSI"), row=2, col=1)

            up_days    = df_display["close"] >= df_display["open"]
            vol_colors = ["#26a69a" if u else "#ef5350" for u in up_days]
            fig.add_trace(go.Bar(x=dates, y=df_display["volume"],
                marker_color=vol_colors, name="Volume", showlegend=False), row=3, col=1)
            fig.add_hline(y=float(df_display["volume"].mean()),
                line=dict(color="rgba(255,255,255,0.4)", width=1, dash="dot"), row=3, col=1)

            fig.update_layout(
                height=700, margin=dict(l=0, r=0, t=40, b=0),
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                xaxis=dict(gridcolor="rgba(255,255,255,0.07)", rangeslider_visible=False),
                xaxis2=dict(gridcolor="rgba(255,255,255,0.07)"),
                xaxis3=dict(gridcolor="rgba(255,255,255,0.07)"),
                yaxis=dict(gridcolor="rgba(255,255,255,0.07)"),
                yaxis2=dict(gridcolor="rgba(255,255,255,0.07)", range=[0, 100]),
                yaxis3=dict(gridcolor="rgba(255,255,255,0.07)"),
                legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="right", x=1),
            )
            st.plotly_chart(fig, use_container_width=True)

# ── Signal Audit page ─────────────────────────────────────────────────────
elif page == "Signal Audit":
    import datetime as _dt
    import pandas as pd
    from dashboard.signal_analyzer import fetch_bars, analyze

    st.title("Signal Audit")
    st.caption(
        "Measures how often the Layer 1 rule engine predicted price direction correctly. "
        "CORRECT = signal matched what price did. WRONG = it didn't. "
        "No AI calls — fast and free."
    )

    cfg = load_cfg()
    polygon_key = cfg.get("polygon", {}).get("api_key", "")
    if not polygon_key:
        st.error("Polygon API key missing — add it in config/config.yaml under `polygon.api_key`.")
        st.stop()

    vrs_cfg = cfg.get("volume_rsi_swing", {})

    # ── Inputs ────────────────────────────────────────────────────────
    c1, c2, c3 = st.columns(3)
    sa_symbol  = c1.text_input("Symbol", "MSFT").upper().strip()
    _today     = _dt.date.today()
    sa_start   = c2.date_input("Start date", _today - _dt.timedelta(days=180), key="sa_start")
    sa_end     = c3.date_input("End date",   _today,                            key="sa_end")

    c4, c5, c6, c7 = st.columns(4)
    sa_step    = c4.number_input(
        "Sample every N trading days",
        min_value=1, max_value=30, value=5, step=1, key="sa_step",
        help="1 = every day, 5 = roughly weekly, 10 = bi-weekly",
    )
    sa_forward = c5.number_input(
        "Forward window (trading days)",
        min_value=1, max_value=60, value=10, step=1, key="sa_forward",
        help="How many trading days to watch after the signal date",
    )
    sa_threshold = c6.number_input(
        "Win/loss threshold (%)",
        min_value=0.5, max_value=20.0, value=5.0, step=0.5, key="sa_threshold",
        help="If price hits +X% at any point in the window → WIN. If it hits -X% first → LOSS. Neither hit → NEUTRAL.",
    )
    c7.markdown("<br>", unsafe_allow_html=True)
    sa_run = c7.button("Run Audit", type="primary", use_container_width=True, key="sa_run_btn")

    if sa_run and sa_symbol:
        if sa_end <= sa_start:
            st.error("End date must be after start date.")
            st.stop()

        # Fetch bars — warmup before start so indicators are stable on day 1
        warmup_start = sa_start - _dt.timedelta(days=200)
        with st.spinner(f"Fetching {sa_symbol} bars ({warmup_start} → {sa_end})…"):
            df_full = fetch_bars(sa_symbol, str(warmup_start), str(sa_end), polygon_key)

        if df_full is None or df_full.empty:
            st.error(f"No data returned for **{sa_symbol}**.")
            st.stop()

        # Trading days within the user's window
        window_start_ts = pd.Timestamp(sa_start, tz="UTC")
        trading_days = [ts for ts in df_full.index if ts >= window_start_ts]

        if not trading_days:
            st.error("No trading days found in the selected range.")
            st.stop()

        # Sample every sa_step-th trading day
        sampled = trading_days[::int(sa_step)]

        rows = []
        progress = st.progress(0, text="Analysing sampled dates…")

        for i, ts in enumerate(sampled):
            progress.progress((i + 1) / len(sampled), text=f"Analysing {str(ts)[:10]} ({i+1}/{len(sampled)})…")

            # Slice df up to (and including) this date — include full warmup
            df_slice = df_full[df_full.index <= ts]
            if len(df_slice) < 60:
                continue

            result = analyze(df_slice, sa_symbol, vrs_cfg)
            if result.error:
                continue

            price_at_signal = float(df_full.loc[ts, "close"])
            rsi_at_signal   = float(result.rsi_series.iloc[-1]) if result.rsi_series is not None else None

            # Scan the forward window — capped at sa_end so bars never bleed past the user's range
            sa_end_ts   = pd.Timestamp(sa_end, tz="UTC") + pd.Timedelta(days=1)
            future_bars = [t for t in df_full.index if ts < t <= sa_end_ts]
            window_bars = future_bars[:int(sa_forward)]
            threshold   = float(sa_threshold)

            # Price at end of window (for display)
            fwd_price = float(df_full.loc[window_bars[-1], "close"]) if window_bars else None
            fwd_ret   = (fwd_price - price_at_signal) / price_at_signal * 100 if fwd_price else None

            window_complete = len(window_bars) >= int(sa_forward)

            # Always scan available bars — peak/trough shown even for OPEN rows
            hit_up = hit_down = False
            peak_ret     = None
            trough_ret   = None
            peak_price   = None
            trough_price = None
            if window_bars:
                peak_ret     = float("-inf")
                trough_ret   = float("inf")
                peak_price   = price_at_signal
                trough_price = price_at_signal
                for fwd_bar in window_bars:
                    bar_close = float(df_full.loc[fwd_bar, "close"])
                    pct = (bar_close - price_at_signal) / price_at_signal * 100
                    if pct >= threshold:
                        hit_up = True
                    if pct <= -threshold:
                        hit_down = True
                    if pct > peak_ret:
                        peak_ret   = pct
                        peak_price = bar_close
                    if pct < trough_ret:
                        trough_ret   = pct
                        trough_price = bar_close

            if not window_bars or not window_complete:
                outcome = "OPEN"
            elif hit_up and hit_down:
                outcome = "NEUTRAL"  # both thresholds hit — too choppy to score
            elif not hit_up and not hit_down:
                outcome = "NEUTRAL"  # neither threshold reached in full window
            elif result.verdict == "ENTER":
                outcome = "WIN" if hit_up else "LOSS"
            elif result.verdict == "REJECT":
                outcome = "WIN" if hit_down else "LOSS"
            else:  # WAIT
                outcome = "WIN" if hit_down else "LOSS"

            cond_details = [
                {"name": c.name, "passed": c.passed, "detail": c.detail}
                for c in result.conditions
            ]

            rows.append({
                "date":         str(ts)[:10],
                "verdict":      result.verdict,
                "price":        price_at_signal,
                "rsi":          rsi_at_signal,
                "cond_details": cond_details,
                "fwd_price":    fwd_price,
                "fwd_ret_pct":  fwd_ret,
                "peak_ret":     peak_ret,
                "trough_ret":   trough_ret,
                "peak_price":   peak_price,
                "trough_price": trough_price,
                "outcome":      outcome,
            })

        progress.empty()

        if not rows:
            st.warning("No valid analysis rows produced — try a wider date range.")
            st.stop()

        df_results = pd.DataFrame(rows)

        # ── Summary stats ─────────────────────────────────────────────
        st.divider()
        enters  = df_results[df_results["verdict"] == "ENTER"]
        waits   = df_results[df_results["verdict"] == "WAIT"]
        rejects = df_results[df_results["verdict"] == "REJECT"]

        # Accuracy: scored signals only (exclude NEUTRAL and OPEN)
        scored     = df_results[df_results["outcome"].isin(["WIN", "LOSS"])]
        correct    = scored[scored["outcome"] == "WIN"]
        overall_pct = len(correct) / len(scored) * 100 if len(scored) else 0

        enter_scored  = scored[scored["verdict"] == "ENTER"]
        wait_scored   = scored[scored["verdict"] == "WAIT"]
        reject_scored = scored[scored["verdict"] == "REJECT"]

        enter_acc  = len(enter_scored[enter_scored["outcome"]  == "WIN"]) / len(enter_scored)  * 100 if len(enter_scored)  else None
        wait_acc   = len(wait_scored[wait_scored["outcome"]    == "WIN"]) / len(wait_scored)   * 100 if len(wait_scored)   else None
        reject_acc = len(reject_scored[reject_scored["outcome"]== "WIN"]) / len(reject_scored) * 100 if len(reject_scored) else None

        ms1, ms2, ms3, ms4, ms5 = st.columns(5)
        ms1.metric("Overall accuracy",  f"{overall_pct:.0f}%" if scored.__len__() else "n/a",
                   help=f"{len(correct)} correct out of {len(scored)} scored signals (NEUTRAL and OPEN excluded)")
        ms2.metric("ENTER accuracy",  f"{enter_acc:.0f}%"  if enter_acc  is not None else "n/a",
                   help="Signal said buy — was price up by threshold within the window?")
        ms3.metric("WAIT accuracy",   f"{wait_acc:.0f}%"   if wait_acc   is not None else "n/a",
                   help="Signal said wait — did price drop (avoiding a loss) or was a move missed?")
        ms4.metric("REJECT accuracy", f"{reject_acc:.0f}%" if reject_acc is not None else "n/a",
                   help="Signal said stay out — did price fall within the window?")
        ms5.metric("Signals scored",  f"{len(scored)} / {len(df_results)}",
                   help=f"NEUTRAL: {len(df_results[df_results['outcome']=='NEUTRAL'])}  OPEN: {len(df_results[df_results['outcome']=='OPEN'])}")

        st.divider()

        # ── Results table ─────────────────────────────────────────────
        st.subheader(f"{sa_symbol} — Signal audit  ({str(sa_start)} → {str(sa_end)},  every {sa_step} trading days,  {sa_forward}-day window,  ±{sa_threshold:.1f}% threshold)")

        _verdict_badge = {
            "ENTER":  '<span style="background:#00c853;color:#fff;padding:2px 10px;border-radius:4px;font-weight:700;font-size:0.82rem">ENTER</span>',
            "WAIT":   '<span style="background:#ff9800;color:#fff;padding:2px 10px;border-radius:4px;font-weight:700;font-size:0.82rem">WAIT</span>',
            "REJECT": '<span style="background:#f44336;color:#fff;padding:2px 10px;border-radius:4px;font-weight:700;font-size:0.82rem">REJECT</span>',
        }
        _outcome_badge = {
            "WIN":     '<span style="color:#00c853;font-weight:700">✓ CORRECT</span>',
            "LOSS":    '<span style="color:#f44336;font-weight:700">✗ WRONG</span>',
            "NEUTRAL": '<span style="color:#888">~ NEUTRAL</span>',
            "OPEN":    '<span style="color:#888">— OPEN</span>',
        }

        header_cols = st.columns([1.4, 1.2, 1, 0.8, 2.2, 1, 1.2, 1.6, 1.2])
        for col, label in zip(header_cols, ["Date", "Verdict", "Price", "RSI", "Conditions (1–4)", f"+{sa_forward}d price", f"+{sa_forward}d return", f"Best / Worst ({sa_forward}d)", "Prediction"]):
            col.markdown(f"<span style='color:#888;font-size:0.8rem'>{label}</span>", unsafe_allow_html=True)
        st.markdown("<hr style='margin:2px 0 6px 0;border-color:#333'>", unsafe_allow_html=True)

        for _, row in df_results.iterrows():
            rc = st.columns([1.4, 1.2, 1, 0.8, 2.2, 1, 1.2, 1.6, 1.2])
            rc[0].markdown(f"<span style='font-size:0.9rem'>{row['date']}</span>", unsafe_allow_html=True)
            rc[1].markdown(_verdict_badge.get(row["verdict"], row["verdict"]), unsafe_allow_html=True)
            rc[2].markdown(f"<span style='font-size:0.9rem'>${row['price']:.2f}</span>", unsafe_allow_html=True)
            rc[3].markdown(
                f"<span style='font-size:0.9rem'>{row['rsi']:.1f}</span>" if pd.notna(row["rsi"]) else "<span style='color:#888'>—</span>",
                unsafe_allow_html=True,
            )
            _cond_html = " ".join(
                f'<span title="{c["name"]}&#10;&#10;{c["detail"]}" '
                f'style="font-size:1.1rem;cursor:help">{"✅" if c["passed"] else "❌"}</span>'
                for c in row["cond_details"]
            )
            rc[4].markdown(_cond_html, unsafe_allow_html=True)
            rc[5].markdown(
                f"<span style='font-size:0.9rem'>${row['fwd_price']:.2f}</span>" if pd.notna(row["fwd_price"]) else "<span style='color:#888'>—</span>",
                unsafe_allow_html=True,
            )
            ret = row["fwd_ret_pct"]
            if pd.notna(ret):
                color = "#00c853" if ret >= 0 else "#f44336"
                sign  = "+" if ret >= 0 else ""
                rc[6].markdown(f"<span style='color:{color};font-weight:600'>{sign}{ret:.1f}%</span>", unsafe_allow_html=True)
            else:
                rc[6].markdown("<span style='color:#888'>—</span>", unsafe_allow_html=True)

            # Peak / Trough column
            pk  = row.get("peak_ret")
            tr  = row.get("trough_ret")
            pkp = row.get("peak_price")
            trp = row.get("trough_price")
            if pd.notna(pk) and pd.notna(tr) and pd.notna(pkp) and pd.notna(trp):
                pk_sign = "+" if pk >= 0 else ""
                tr_sign = "+" if tr >= 0 else ""
                pk_color = "#00c853" if pk >= 0 else "#f44336"
                tr_color = "#00c853" if tr >= 0 else "#f44336"
                pk_arrow = "▲" if pk >= 0 else "▼"
                tr_arrow = "▲" if tr >= 0 else "▼"
                rc[7].markdown(
                    f'<span title="Best close within window: ${pkp:.2f}" '
                    f'style="color:{pk_color};font-size:0.85rem;cursor:help">{pk_arrow} {pk_sign}{pk:.1f}%</span>'
                    f'<span style="color:#888;font-size:0.85rem"> / </span>'
                    f'<span title="Worst close within window: ${trp:.2f}" '
                    f'style="color:{tr_color};font-size:0.85rem;cursor:help">{tr_arrow} {tr_sign}{tr:.1f}%</span>',
                    unsafe_allow_html=True,
                )
            else:
                rc[7].markdown("<span style='color:#888'>—</span>", unsafe_allow_html=True)

            rc[8].markdown(_outcome_badge.get(row["outcome"], row["outcome"]), unsafe_allow_html=True)
            st.markdown("<hr style='margin:2px 0;border-color:#1a1a1a'>", unsafe_allow_html=True)
