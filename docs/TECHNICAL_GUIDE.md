# Zignal — Technical Guide

This document covers architecture, data sources, component design, configuration, and setup.
For day-to-day usage of the Analyze tool, see [USAGE_GUIDE.md](USAGE_GUIDE.md).

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Architecture](#2-architecture)
3. [Data Sources](#3-data-sources)
4. [Project Layout](#4-project-layout)
5. [Core Components](#5-core-components)
6. [The Analyze Tool (Hybrid Rule + AI Layer)](#6-the-analyze-tool-hybrid-rule--ai-layer)
7. [Configuration Reference](#7-configuration-reference)
8. [Setup & Installation](#8-setup--installation)
9. [Running the Dashboard](#9-running-the-dashboard)
10. [Broker Integrations](#10-broker-integrations)
11. [Dependencies](#11-dependencies)

---

## 1. System Overview

This is a modular Python algorithmic trading system built around a single core strategy: **Volume-RSI Swing** — a swing trading approach that identifies oversold reversals by combining RSI exhaustion signals, SMA trend analysis, and volume accumulation patterns.

The system has three operating modes:

| Mode | Purpose |
|------|---------|
| **Backtest** | Run the strategy against historical daily data from Polygon.io |
| **Paper / Live** | Stream real-time bars from Alpaca and execute signals automatically |
| **Analyze** | On-demand analysis of a single ticker — hybrid rule engine + AI narrative |

The Analyze mode (dashboard) is the primary interactive tool. Trading execution (paper/live) is automated but requires explicit operator start. **You always decide when to enter and exit — the system analyzes, you trade.**

---

## 2. Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                      Streamlit Dashboard                         │
│  ┌──────┐ ┌─────────┐ ┌──────────────┐ ┌──────────┐ ┌────────┐ ┌──────────┐ │
│  │ Live │ │ Analyze │ │ Signal Audit │ │Backtests │ │ Trades │ │ Settings │ │
│  └──────┘ └────┬────┘ └──────────────┘ └──────────┘ └────────┘ └──────────┘ │
└──────────────────────────────────────────────────────────┼──────┘
                                                           │
                            ┌──────────────────────────────┤
                            │                              │
                  ┌─────────▼──────────┐      ┌──────────▼───────┐
                  │  Rule Engine       │      │  LLM Analyzer    │
                  │  signal_analyzer   │      │  llm_analyzer    │
                  │                    │      │                  │
                  │  4 conditions:     │─────►│  claude-opus-4-8 │
                  │  • RSI oversold    │  if  │  tool use        │
                  │  • SMA flattening  │  not │  structured JSON │
                  │  • Volume accum.   │  3+  │                  │
                  │  • No inst. dump   │  fail│  adaptive think  │
                  └────────┬───────────┘      └──────────────────┘
                           │
                  ┌────────▼───────────┐
                  │  Polygon.io REST   │
                  │  Daily OHLCV bars  │
                  │  (adjusted, 150d   │
                  │   warmup prefix)   │
                  └────────────────────┘

Execution path (paper/live):
  Alpaca WebSocket ──► on_bar() ──► Strategy ──► RiskManager ──► OrderManager ──► Alpaca REST
  (or IBKR TWS/Gateway)                                                          (or IBKR)

Persistence:
  All trades, events, snapshots ──► SQLite (db/trading.db)
  Backtest curves ──► CSV (logs/)
```

### Key design decisions

- **Separation of analyze from execute.** The Analyze page and the live trading engine are completely independent. Analyzing a stock never places an order.
- **Rule engine as a cost-saving gate.** The LLM is only called when the rule result is ambiguous (fewer than 3 hard failures). Clear rejects skip the AI call entirely.
- **Indicator warmup.** When fetching data for analysis, 150 extra calendar days are prepended to the user's selected window. This ensures RSI, SMA, and volume averages are fully converged before the display range starts — no "insufficient data" false fails.
- **Pure-pandas indicators.** RSI and SMA are computed in-process using Wilder smoothing (`ewm`). No TA-Lib dependency required.

---

## 3. Data Sources

### Polygon.io (historical OHLCV)

Used by: Analyze page, Backtesting engine

- **Endpoint:** `GET /v2/aggs/ticker/{symbol}/range/1/day/{from}/{to}`
- **Auth:** API key from `config.yaml → polygon.api_key`
- **Data:** Adjusted daily OHLCV bars sorted ascending
- **Rate limits:** Free tier supports end-of-day data; one REST call per analysis
- **Client:** `polygon-api-client` Python package (`RESTClient.list_aggs()`)

The analyzer fetches a single contiguous request covering `(user start − 150 days)` to `user end`. No pagination issues for typical swing analysis windows (1–6 months visible + 5 months warmup).

### Alpaca (real-time bars + order execution)

Used by: Paper/Live trading mode

- **WebSocket stream:** Real-time 1-min and daily bars via `AlpacaDataFeed`
- **REST API:** Account info, position queries, order placement via `AlpacaBroker`
- **Paper vs live:** Controlled by `alpaca.base_url` in config — `paper-api.alpaca.markets` vs `api.alpaca.markets`
- **Auth:** `api_key` + `secret_key` in config

### Anthropic API (AI analysis)

Used by: Analyze page (AI layer only)

- **Model:** `claude-opus-4-8`
- **Feature:** Extended thinking (`thinking: {type: "adaptive"}`) — model reasons internally before answering
- **Structured output:** Tool use with a `trade_analysis` JSON schema — guarantees a parseable response
- **Auth:** `ANTHROPIC_API_KEY` environment variable (preferred) or `config.yaml → llm.api_key`
- **Cost gate:** LLM call is skipped when 3+ conditions hard-fail (the rule verdict is unambiguous)

### Finnhub (earnings calendar)

Used by: EarningsSwing strategy only

- **Purpose:** Fetch upcoming earnings dates for position sizing / exit timing
- **Auth:** `config.yaml → finnhub.api_key`
- **Free tier:** 60 calls/min — sufficient for a 20-ticker watchlist

### Interactive Brokers TWS / IB Gateway (optional)

Used by: Live trading mode when `active_broker: ibkr`

- **Connection:** Local TCP socket via `ib_insync`
- **Ports:** TWS paper=7497, TWS live=7496, IB Gateway paper=4002, live=4001
- **No separate API key** — authentication happens through the TWS/Gateway application

---

## 4. Project Layout

```
trading_system/
├── main.py                     Entry point (CLI: backtest / paper / live / dashboard)
├── config/
│   └── config.yaml             All API keys and strategy parameters
├── dashboard/
│   ├── app.py                  Streamlit 5-page dashboard
│   ├── signal_analyzer.py      Rule-based entry condition engine
│   └── llm_analyzer.py         AI hybrid analysis layer (Anthropic SDK)
├── strategies/
│   ├── base_strategy.py        Abstract base + RSI/SMA/EMA indicator helpers
│   ├── volume_rsi_swing.py     Primary strategy (live/backtest execution)
│   ├── rsi_mean_reversion.py   Mean reversion strategy
│   ├── moving_average_crossover.py
│   └── earnings_swing.py       Earnings-driven swing strategy
├── brokers/
│   ├── base_broker.py          Abstract broker interface
│   ├── alpaca_broker.py        Alpaca REST implementation
│   └── ibkr_broker.py          Interactive Brokers implementation
├── data/
│   ├── polygon_feed.py         Historical OHLCV via Polygon REST
│   ├── alpaca_feed.py          Real-time bars via Alpaca WebSocket
│   └── finnhub_client.py       Earnings calendar
├── execution/
│   └── order_manager.py        Order placement, cancellation, position tracking
├── risk/
│   └── risk_manager.py         Position sizing, daily loss limits, heat checks
├── backtesting/
│   └── backtest_engine.py      Event-driven backtest runner
├── db/
│   └── database.py             SQLite persistence (trades, events, snapshots)
├── monitor/
│   └── logger.py               Trade logger and daily P&L summary
├── docs/
│   ├── TECHNICAL_GUIDE.md      (this file)
│   └── USAGE_GUIDE.md          Day-to-day usage guide
├── logs/                       Trade journals (.jsonl) and backtest curves (.csv)
├── requirements.txt
└── trade                       Shell convenience script
```

---

## 5. Core Components

### `signal_analyzer.py` — Rule Engine

The heart of the Analyze tool. Takes a DataFrame of bars and a config dict and returns a structured `AnalysisResult`.

**`fetch_bars(symbol, start, end, api_key) → DataFrame`**
Single Polygon REST call. Returns a UTC-indexed DataFrame with `open, high, low, close, volume` columns. Returns `None` on API failure or empty response.

**`AnalysisResult`**
```
.symbol          str
.verdict         "ENTER" | "WAIT" | "REJECT"
.verdict_reason  str  — human-readable explanation
.conditions      list[Condition]  — one per check
.rsi_series      pd.Series  — RSI values aligned to full DataFrame index
.sma_series      pd.Series  — SMA values aligned to full DataFrame index
.dist_day_flags  pd.Series(bool)  — True where a distribution day was detected
.df              pd.DataFrame  — the full bar data
.error           str | None
```

**`Condition`**
```
.name    str   — display name for the check
.passed  bool
.detail  str   — what was found, formatted for human reading
.value   float | None  — numeric value (e.g. RSI, distribution day count)
```

**Four conditions checked on the last bar of the DataFrame:**

| # | Condition | Passes When |
|---|-----------|-------------|
| 1 | RSI oversold & curling up | RSI touched below threshold within `rsi_lookback_bars`, and today's RSI > yesterday's RSI |
| 2 | SMA flattening (not freefall) | SMA decline is slowing (recent slope less negative than prior slope), OR price already turned up despite SMA still declining (bottoming pattern) |
| 3 | Volume accumulation | Up-day avg volume ≥ `volume_dry_up_ratio` × down-day avg volume (buyers stepping in) |
| 4 | No institutional dumping | ≤ `max_distribution_days` distribution days in the lookback window (high-volume down days) |

**Verdict logic (in priority order):**
1. REJECT if distribution days exceeded (hard stop — smart money is selling)
2. REJECT if SMA still in freefall AND price also falling
3. REJECT if RSI is overbought (missed the entry window)
4. WAIT if RSI hasn't touched oversold territory yet
5. WAIT if RSI touched oversold but still falling (hasn't curled)
6. WAIT if RSI and SMA are fine but volume not confirming
7. ENTER if all four conditions pass

### `llm_analyzer.py` — AI Hybrid Layer

Wraps the Anthropic Python SDK for on-demand holistic analysis.

**`should_call_llm(result) → (bool, reason_str)`**
Returns `False` when 3+ conditions hard-fail. In that case the rule verdict is unambiguous — paying for an LLM call adds no value.

**`build_prompt(symbol, df, result, vrs_cfg) → {system, user}`**
Serializes the last 30 bars (date, OHLCV, RSI, SMA) into a fixed-width table, then composes a system prompt framing the model as an experienced swing trader, and a user prompt with the full rule results plus the bar table.

**`call_llm(prompt_data, api_key, model) → LLMAnalysis`**
Calls `anthropic.Anthropic.messages.create()` with:
- `thinking: {type: "adaptive"}` — model reasons internally before answering
- `tool_choice: {type: "tool", name: "trade_analysis"}` — forces structured JSON output
- A `trade_analysis` tool definition with a typed JSON schema

The tool schema enforces: `verdict` (enum), `confidence` (enum), `summary`, `analysis`, `key_observations` (array), `risks` (array), `watch_for`.

**`LLMAnalysis` dataclass**
```
.verdict           "ENTER" | "WAIT" | "REJECT" | "CAUTION"
.confidence        "high" | "medium" | "low"
.summary           one-line headline
.analysis          2-4 sentence holistic narrative
.key_observations  list[str]  — what the rules missed or confirmed
.risks             list[str]
.watch_for         str  — next 1-3 session watchpoints
.model_used        str
.skipped           bool
.skip_reason       str
.error             str | None
```

Note the AI adds a fourth verdict option — **CAUTION** — which means all conditions technically pass but contextual factors (momentum, market structure, volume narrative) raise concern. This distinction is not possible in the binary rule engine.

### `volume_rsi_swing.py` — Live/Backtest Strategy

The execution-side counterpart to `signal_analyzer.py`. Both implement the same four conditions, but `volume_rsi_swing.py` hooks into the broker/order/risk framework and fires orders. `signal_analyzer.py` is read-only and designed for interactive inspection.

Key parameters mirrored between both:

| Config key | Default | Purpose |
|------------|---------|---------|
| `rsi_period` | 14 | Wilder RSI period |
| `rsi_oversold` | 40 | Oversold threshold (stricter than classic 30) |
| `rsi_overbought` | 65 | Exit / reject threshold |
| `rsi_lookback_bars` | 10 | Window to look back for an oversold touch |
| `sma_period` | 50 | SMA to track for trend |
| `sma_slope_period` | 5 | Bars per slope window for flattening detection |
| `volume_dry_up_ratio` | 0.80 | Min ratio of up-day to down-day avg volume |
| `distribution_lookback` | 10 | Bars to check for distribution days |
| `max_distribution_days` | 1 | Max allowed before rejecting |

### `backtest_engine.py` — Event-Driven Backtester

Replays historical bars in chronological order, calling `strategy.on_bar()` for each. Tracks portfolio value, cash, positions, and produces a `BacktestResult` with trade log and equity curve.

### `risk_manager.py`

Per-trade and portfolio-level controls:
- `max_position_pct` — no single position > 5% of portfolio
- `max_portfolio_heat` — total capital at risk ≤ 20%
- `stop_loss_pct` — 2% hard stop below entry
- `max_daily_loss_pct` — halt all trading if daily drawdown exceeds 3%

---

## 6. The Analyze Tool (Hybrid Rule + AI Layer)

### Data flow when you click Analyze

```
User clicks Analyze
       │
       ▼
fetch_bars(symbol, warmup_start, end_date)  ← Polygon REST (1 call)
       │  returns ~220 bars (150 warmup + user window)
       ▼
analyze(df_full, symbol, vrs_cfg)           ← Rule engine
       │  returns AnalysisResult with 4 conditions
       ▼
should_call_llm(result)?
  ├─ No (3+ hard fails) → show "AI skipped" message
  └─ Yes
       ▼
build_prompt(symbol, df_full, result, vrs_cfg)   ← serialize last 30 bars
       ▼
call_llm(prompt_data, api_key, model)            ← Anthropic API
       │  model: claude-opus-4-8 + adaptive thinking + tool use
       ▼
Display: verdict badge, analysis, observations, risks, watch_for
```

### Why tool use for structured output?

The LLM is instructed with `tool_choice: {type: "tool", name: "trade_analysis"}`. This forces the model to always respond by calling that specific tool with typed arguments matching the JSON schema. The result is guaranteed-parseable structured data — no prompt engineering needed to extract JSON from prose, no hallucinated field names.

### Why adaptive thinking?

`thinking: {type: "adaptive"}` lets the model decide how much internal reasoning to allocate based on the complexity of the question. For a clear setup it reasons briefly; for an ambiguous one (which is exactly when you need the LLM most) it spends more compute thinking through the context. This is preferable to a fixed `budget_tokens` value that either wastes compute on easy cases or starves hard ones.

### Cost gate rationale

Anthropic `claude-opus-4-8` is priced at approximately $5/1M input tokens and $25/1M output tokens. A typical analysis prompt is ~1,500 tokens in + ~400 tokens out ≈ $0.017 per call. This is negligible for individual analysis but adds up if you're scanning many tickers. The cost gate eliminates calls where the rule engine already has a definitive answer — if 3 out of 4 conditions fail, no amount of AI narrative changes the REJECT.

---

## 7. Configuration Reference

All settings live in `config/config.yaml`.

```yaml
# ── Broker ────────────────────────────────────────────────────
active_broker: alpaca          # alpaca | ibkr

alpaca:
  api_key: "..."
  secret_key: "..."
  base_url: "https://paper-api.alpaca.markets"  # change for live

ibkr:
  host: "127.0.0.1"
  port: 7497                   # TWS paper=7497, live=7496

# ── Data ──────────────────────────────────────────────────────
polygon:
  api_key: "..."               # free tier sufficient for analysis

finnhub:
  api_key: "..."               # earnings calendar (EarningsSwing only)

# ── AI Analysis ───────────────────────────────────────────────
llm:
  api_key: ""                  # leave blank if using ANTHROPIC_API_KEY env var
  model: "claude-opus-4-8"

# ── Strategy ──────────────────────────────────────────────────
active_strategy: volume_rsi_swing

volume_rsi_swing:
  rsi_period: 14
  rsi_oversold: 40
  rsi_overbought: 65
  rsi_lookback_bars: 10        # key knob — increase to catch earlier oversold touches
  sma_period: 50
  sma_slope_period: 5
  volume_lookback: 20
  volume_avg_period: 20
  volume_dry_up_ratio: 0.80
  distribution_lookback: 10
  distribution_vol_ratio: 1.5
  distribution_price_drop_pct: 0.01
  max_distribution_days: 1

# ── Risk ──────────────────────────────────────────────────────
risk:
  max_position_pct: 0.05
  max_portfolio_heat: 0.20
  stop_loss_pct: 0.02
  max_daily_loss_pct: 0.03

# ── Backtest ──────────────────────────────────────────────────
backtest:
  start_date: "2024-07-01"
  end_date: "2024-12-31"
  initial_capital: 100000.0
  commission_per_trade: 0.0
```

---

## 8. Setup & Installation

### Prerequisites

- Python 3.11+
- A Polygon.io account (free tier works for analysis and backtesting)
- An Anthropic account for AI analysis (optional but recommended)
- An Alpaca account for paper/live trading (optional)

### Step 1 — Clone and create virtual environment

```bash
cd trading_system
python3 -m venv .venv
source .venv/bin/activate
```

### Step 2 — Install dependencies

```bash
pip install -r requirements.txt
```

This installs all packages including `anthropic`, `polygon-api-client`, `streamlit`, `plotly`, `alpaca-py`, and others.

### Step 3 — Configure API keys

Edit `config/config.yaml` and fill in your keys:

```yaml
polygon:
  api_key: "your-polygon-key"

llm:
  api_key: "your-anthropic-key"   # or use env var (see below)
```

For Anthropic, the environment variable approach is preferred (keeps keys out of the config file):

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

### Step 4 — Launch the dashboard

```bash
python main.py --mode dashboard
```

Or directly:

```bash
.venv/bin/streamlit run dashboard/app.py
```

Opens at `http://localhost:8501`.

### Step 5 — (Optional) Run a backtest to verify setup

```bash
python main.py --mode backtest --strategy volume_rsi_swing \
  --start 2024-01-01 --end 2024-12-31
```

---

## 9. Running the Dashboard

```bash
# Via main.py (recommended — uses the .venv automatically)
python main.py --mode dashboard

# Via streamlit directly
.venv/bin/streamlit run dashboard/app.py

# Via the trade shell script (if present)
./trade dashboard
```

The dashboard runs on `localhost:8501` by default. Streamlit hot-reloads on file save — you can edit `config.yaml` and changes take effect on the next Analyze click without restarting.

### Dashboard pages

| Page | Description |
|------|-------------|
| **Live** | Real-time portfolio value, event feed, recent trades (auto-refreshes every 30s) |
| **Analyze** | On-demand entry/exit analysis for any ticker with hybrid rule + AI verdict |
| **Signal Audit** | Batch back-test the rule engine across a date range; measures historical prediction accuracy |
| **Backtests** | History of all backtest runs with equity curves and trade logs |
| **Trades** | Full trade history across paper and live sessions |
| **Settings** | Edit strategy parameters and risk settings from the UI; launch paper/live/backtest runs |

---

## 10. Broker Integrations

### Alpaca

- Free paper trading account available at alpaca.markets
- Paper and live use identical code paths — only the `base_url` changes
- Real-time bar streaming via WebSocket (`alpaca_feed.py`)
- REST API for account, positions, orders (`alpaca_broker.py`)

### Interactive Brokers

- Requires a funded IBKR brokerage account + TWS or IB Gateway running locally
- Connection is a local TCP socket — no cloud API key
- Enable API access in TWS: File → Global Config → API → Settings → Enable ActiveX and Socket Clients
- Set `active_broker: ibkr` in config; ensure TWS is running before starting

### Switching brokers

Change `active_broker` in `config.yaml`. All strategy and risk code is broker-agnostic through the `BrokerBase` interface.

---

## 11. Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| `anthropic` | ≥0.50.0 | Anthropic Python SDK — AI analysis layer |
| `polygon-api-client` | ≥1.14.0 | Polygon.io REST client — historical OHLCV |
| `alpaca-py` | ≥0.20.0 | Alpaca broker + real-time WebSocket data |
| `ib_insync` | ≥0.9.86 | Interactive Brokers TWS/Gateway connection |
| `finnhub-python` | ≥2.4.19 | Earnings calendar |
| `streamlit` | ≥1.35.0 | Dashboard web UI |
| `plotly` | ≥5.20.0 | Interactive candlestick and indicator charts |
| `pandas` | ≥2.0.0 | Data manipulation and indicator computation |
| `numpy` | ≥1.24.0 | Numerical operations |
| `PyYAML` | ≥6.0.1 | Config file parsing |
| `streamlit-autorefresh` | ≥1.0.1 | Auto-refresh for the Live page |
| `tabulate` | ≥0.9.0 | Backtest summary tables |
| `aiohttp` | ≥3.9.0 | Async HTTP (used by alpaca-py internally) |
| `websockets` | ≥12.0 | WebSocket support |
