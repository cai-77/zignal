"""
LLM hybrid analysis layer for the Trade Signal Analyzer.

Enriches the prompt with five context blocks before calling the LLM:
  1. Market context   — SPY / VIXY recent performance (from Polygon)
  2. Earnings risk    — next earnings date and EPS estimate (from Finnhub)
  3. Recent news      — last 14 days of headlines + summaries (from Finnhub)
  4. Insider activity — recent insider buys/sells (from Finnhub)
  5. Position details — cost basis, unrealised P&L, stop distance (exit mode only)

Cost gate: skip LLM when 3+ conditions hard-fail (clear reject, no ambiguity).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

import pandas as pd


# ──────────────────────────────────────────────────────────────────────
# Result type
# ──────────────────────────────────────────────────────────────────────

@dataclass
class LLMAnalysis:
    verdict: str = ""           # entry: ENTER|WAIT|REJECT|CAUTION  exit: HOLD|EXIT|EXIT_PARTIAL|TIGHTEN_STOP
    confidence: str = ""        # high | medium | low
    summary: str = ""
    analysis: str = ""
    key_observations: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    watch_for: str = ""
    model_used: str = ""
    skipped: bool = False
    skip_reason: str = ""
    error: Optional[str] = None
    entry_trigger: str = ""
    invalidation_level: str = ""
    position_guidance: str = ""
    final_action: str = ""


# ──────────────────────────────────────────────────────────────────────
# Cost gate
# ──────────────────────────────────────────────────────────────────────

def should_call_llm(result, analysis_type: str = "entry") -> tuple[bool, str]:
    """
    Skip LLM when 3+ conditions hard-fail on an entry analysis.
    Always call for exit analysis — the trader holds the position,
    so even a weak technical picture needs a reasoned exit recommendation.
    """
    if analysis_type == "exit":
        return True, ""
    hard_fails = sum(1 for c in result.conditions if not c.passed)
    if hard_fails >= 3:
        return (
            False,
            f"{hard_fails}/4 conditions failed — clear reject, skipping LLM to save cost",
        )
    return True, ""


# ──────────────────────────────────────────────────────────────────────
# Context fetchers
# ──────────────────────────────────────────────────────────────────────

def fetch_earnings_context(symbol: str, finnhub_key: str) -> Optional[dict]:
    """Next earnings date + EPS estimate. Returns None on failure."""
    try:
        import finnhub
        client = finnhub.Client(api_key=finnhub_key)
        today = date.today()
        resp = client.earnings_calendar(
            _from=today.isoformat(),
            to=(today + timedelta(days=180)).isoformat(),
            symbol=symbol,
            international=False,
        )
        events = resp.get("earningsCalendar", [])
        future = []
        for ev in events:
            try:
                earnings_date = date.fromisoformat(ev["date"])
            except (KeyError, ValueError):
                continue
            if earnings_date >= today:
                future.append((earnings_date, ev))
        if future:
            # Pick the soonest upcoming date — API order is not guaranteed
            earnings_date, ev = min(future, key=lambda x: x[0])
            return {
                "date": earnings_date.isoformat(),
                "days_away": (earnings_date - today).days,
                "eps_estimate": ev.get("epsEstimate"),
                "hour": ev.get("hour", ""),  # bmo | amc | dmh
                "source": "Finnhub",
                "retrieved": today.isoformat(),
                "confidence": "estimated",
            }
    except Exception:
        pass
    return None


def fetch_news_context(symbol: str, finnhub_key: str, days: int = 14, start_date: Optional[str] = None) -> list[dict]:
    """News headlines + summaries from start_date (or last `days` days) to today. Returns [] on failure."""
    try:
        import finnhub
        client = finnhub.Client(api_key=finnhub_key)
        today = date.today()
        from_date = start_date if start_date else (today - timedelta(days=days)).isoformat()
        articles = client.company_news(symbol, _from=from_date, to=today.isoformat())
        if not articles:
            return []
        results = []
        seen = set()
        for a in sorted(articles, key=lambda x: x.get("datetime", 0), reverse=True)[:8]:
            headline = a.get("headline", "").strip()
            if not headline or headline in seen:
                continue
            seen.add(headline)
            summary = a.get("summary", "").strip()
            # Truncate long summaries to keep token count reasonable
            if len(summary) > 300:
                summary = summary[:297] + "…"
            ts = a.get("datetime", 0)
            try:
                article_date = date.fromtimestamp(ts).isoformat() if ts else ""
            except Exception:
                article_date = ""
            results.append({
                "date": article_date,
                "headline": headline,
                "summary": summary,
                "source": a.get("source", ""),
                "url": a.get("url", ""),
            })
        return results
    except Exception:
        return []


def fetch_insider_context(symbol: str, finnhub_key: str, days: int = 90, start_date: Optional[str] = None) -> list[dict]:
    """Insider transactions from start_date (or last `days` days) to today. Returns [] on failure."""
    try:
        import finnhub
        client = finnhub.Client(api_key=finnhub_key)
        today = date.today()
        from_date = start_date if start_date else (today - timedelta(days=days)).isoformat()
        resp = client.stock_insider_transactions(symbol=symbol, _from=from_date)
        transactions = resp.get("data", []) if isinstance(resp, dict) else []
        if not transactions:
            return []
        # SEC Form 4 transaction codes that indicate a clear directional signal
        _OPEN_BUY  = {"P"}           # open-market purchase
        _OPEN_SELL = {"S"}           # open-market sale
        results = []
        for t in sorted(transactions, key=lambda x: x.get("transactionDate", ""), reverse=True)[:10]:
            change   = t.get("change", 0) or 0
            price    = t.get("transactionPrice") or 0
            value    = abs(change * price) if price else None
            tx_code  = (t.get("transactionCode") or "").strip().upper()
            if tx_code in _OPEN_BUY:
                action_label   = "OPEN-MARKET BUY"
                is_mkt_signal  = True
            elif tx_code in _OPEN_SELL:
                action_label   = "OPEN-MARKET SALE"
                is_mkt_signal  = True
            elif change > 0:
                action_label   = f"ACQUISITION (code: {tx_code or 'unknown'})"
                is_mkt_signal  = False
            elif change < 0:
                action_label   = f"DISPOSITION (code: {tx_code or 'unknown'})"
                is_mkt_signal  = False
            else:
                action_label   = f"TRANSACTION (code: {tx_code or 'unknown'})"
                is_mkt_signal  = False
            results.append({
                "date":             t.get("transactionDate", ""),
                "name":             t.get("name", "Unknown"),
                "action":           action_label,
                "shares":           abs(int(change)),
                "price":            float(price) if price else None,
                "value_usd":        value,
                "filing_date":      t.get("filingDate", ""),
                "transaction_code": tx_code,
                "is_market_signal": is_mkt_signal,
                "source":           "Finnhub / SEC Form 4",
            })
        return results
    except Exception:
        return []


def fetch_market_context(polygon_key: str, as_of_date: Optional[str] = None) -> Optional[dict]:
    """
    Fetch SPY and VIXY recent performance as market regime context.
    Uses Polygon (same key the caller already authenticated with).
    """
    try:
        from dashboard.signal_analyzer import fetch_bars
        today = as_of_date or date.today().isoformat()
        lookback_start = (date.fromisoformat(today) - timedelta(days=40)).isoformat()

        spy_df = fetch_bars("SPY", lookback_start, today, polygon_key)
        vixy_df = fetch_bars("VIXY", lookback_start, today, polygon_key)

        if spy_df is None or len(spy_df) < 5:
            return None

        spy_close = spy_df["close"].astype(float)
        spy_1d  = (spy_close.iloc[-1] / spy_close.iloc[-2] - 1) * 100 if len(spy_close) >= 2 else None
        spy_5d  = (spy_close.iloc[-1] / spy_close.iloc[-6] - 1) * 100 if len(spy_close) >= 6 else None
        spy_20d = (spy_close.iloc[-1] / spy_close.iloc[-21] - 1) * 100 if len(spy_close) >= 21 else None

        # Market regime label
        if spy_20d is not None and spy_5d is not None:
            if spy_20d > 0 and spy_5d > 0:
                regime = "Uptrend — broad market tailwind"
            elif spy_20d > 0 and spy_5d < -2:
                regime = "Pullback within uptrend — potential entry window"
            elif spy_20d < -3:
                regime = "Downtrend — broad market headwind, be cautious"
            else:
                regime = "Choppy / sideways market"
        else:
            regime = "Insufficient data"

        vixy_close = None
        vixy_5d = None
        if vixy_df is not None and len(vixy_df) >= 2:
            vc = vixy_df["close"].astype(float)
            vixy_close = float(vc.iloc[-1])
            if len(vc) >= 6:
                vixy_5d = (vc.iloc[-1] / vc.iloc[-6] - 1) * 100

        return {
            "spy_last": float(spy_close.iloc[-1]),
            "spy_1d_pct": spy_1d,
            "spy_5d_pct": spy_5d,
            "spy_20d_pct": spy_20d,
            "vixy_last": vixy_close,
            "vixy_5d_pct": vixy_5d,
            "regime": regime,
        }
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────────
# Prompt builder
# ──────────────────────────────────────────────────────────────────────

def _serialize_bars(df: pd.DataFrame, rsi_series: pd.Series, sma_series: pd.Series, n: int = 30) -> str:
    recent     = df.tail(n)
    rsi_recent = rsi_series.reindex(recent.index)
    sma_recent = sma_series.reindex(recent.index)

    lines = ["Date         Open    High    Low     Close   Volume        RSI    SMA"]
    lines.append("-" * 76)
    for ts in recent.index:
        r   = recent.loc[ts]
        rsi = rsi_recent.loc[ts]
        sma = sma_recent.loc[ts]
        rsi_str = f"{rsi:5.1f}" if pd.notna(rsi) else "  n/a"
        sma_str = f"{sma:7.2f}" if pd.notna(sma) else "    n/a"
        lines.append(
            f"{str(ts)[:10]}  "
            f"{r['open']:7.2f} {r['high']:7.2f} {r['low']:7.2f} {r['close']:7.2f}  "
            f"{r['volume']:12.0f}  "
            f"{rsi_str}  {sma_str}"
        )
    return "\n".join(lines)


def _fmt_pct(v) -> str:
    if v is None:
        return "n/a"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.2f}%"


def _section(title: str, body: str) -> str:
    bar = "═" * 55
    return f"\n{bar}\n{title}\n{bar}\n{body}\n"


def build_prompt(
    symbol: str,
    df: pd.DataFrame,
    result,
    vrs_cfg: dict,
    *,
    analysis_type: str = "entry",
    cost_basis: Optional[float] = None,
    earnings_ctx: Optional[dict] = None,
    news_ctx: Optional[list] = None,
    insider_ctx: Optional[list] = None,
    market_ctx: Optional[dict] = None,
) -> dict:
    """
    Build system + user messages for the LLM.
    Returns {"system": str, "user": str}.
    """
    is_exit = analysis_type == "exit"
    current_price = float(df["close"].iloc[-1])
    analysis_date = str(df.index[-1])[:10]

    # ── System prompt ─────────────────────────────────────────────────
    if is_exit:
        system = (
            "You are a professional swing trader with 15 years of experience managing "
            "open positions. A trader holds a position and needs to decide whether to "
            "hold, exit, take partial profits, or tighten their stop-loss.\n\n"
            "You have access to: the technical rule-engine results, recent price/indicator "
            "data (30 bars), market context, earnings schedule, recent news, and insider "
            "activity. Use all of it.\n\n"
            "Key questions to answer:\n"
            "  • Is the original thesis still intact, or has something changed?\n"
            "  • Has the trade reached a logical exit point (RSI overbought, resistance)?\n"
            "  • Does news or insider activity change the hold/exit decision?\n"
            "  • Is earnings risk within the hold period a concern?\n"
            "  • What is the remaining upside vs the downside risk from current price?\n\n"
            "Be direct. The trader already has real money in this trade."
        )
    else:
        system = (
            "You are a professional swing trader and quantitative analyst with 15 years "
            "of experience identifying oversold reversals.\n\n"
            "A mechanical rule engine has checked four entry conditions — your job is the "
            "holistic judgment the rules alone cannot make. You have access to: market "
            "context (SPY/VIX), upcoming earnings, recent news, and insider transactions. "
            "Factor all of it into your assessment.\n\n"
            "Key questions:\n"
            "  • Is the stock down due to stock-specific news, or broad market selling?\n"
            "  • Does insider activity confirm or contradict the oversold thesis?\n"
            "  • Does the earnings date create unacceptable risk within the hold period?\n"
            "  • Does the market environment support or work against this setup?\n"
            "  • Are there divergences or support/resistance levels visible in the data?\n\n"
            "Be direct and actionable. The trader decides — your analysis sharpens conviction "
            "or raises the right flags.\n\n"
            "Report quality rules — follow strictly:\n"
            "1. RSI wording: Always distinguish (a) 1-day RSI change from (b) 3-bar RSI trend. "
            "   Never say 'RSI is curling up' or 'momentum confirmed' if the rule engine marked "
            "   the RSI curl condition as FAIL. If RSI is up day-over-day but the 3-bar trend is "
            "   down, state both explicitly.\n"
            "2. Language: Do not use 'sellers exhausted', 'buyers in control', 'classic bottom', "
            "   'this is a bottom', or 'institutional dumping'. Use instead: "
            "   'momentum confirmation incomplete', 'possible capitulation-style low', "
            "   'distribution pressure', 'buyer participation improving but not confirmed', "
            "   'high-volume selling pressure'.\n"
            "3. Insider transactions: Only code 'P' (open-market purchase) is a bullish insider signal. "
            "   Grants, awards, option exercises, tax withholding are not signals. Do not call a "
            "   non-open-market transaction bullish. If type is unclear, say so.\n"
            "4. Earnings: Note whether the date is confirmed or estimated. Do not state an estimated "
            "   date as a hard fact.\n"
            "5. News claims: When you say a selloff is macro/sector-driven rather than company-specific, "
            "   reference specific headlines from the provided news data to support that claim. "
            "   If the news does not clearly support it, soften the statement.\n"
            "6. Internal consistency: Your analysis and key_observations must use the same RSI values "
            "   and price levels shown in the rule engine results. Do not introduce different numbers."
        )

    # ── User message — assemble sections ──────────────────────────────
    parts = []
    parts.append(f"SYMBOL: {symbol}   |   ANALYSIS TYPE: {'EXIT (should I hold or sell?)' if is_exit else 'ENTRY (should I buy?)'}")
    parts.append(f"Current price: ${current_price:.2f}   |   As of: {analysis_date}")

    # Market context
    if market_ctx:
        m = market_ctx
        vixy_str = f"${m['vixy_last']:.2f} ({_fmt_pct(m.get('vixy_5d_pct'))} 5d)" if m.get("vixy_last") else "n/a"
        body = (
            f"S&P 500 (SPY):  {_fmt_pct(m.get('spy_1d_pct'))} today  |  "
            f"{_fmt_pct(m.get('spy_5d_pct'))} 5-day  |  {_fmt_pct(m.get('spy_20d_pct'))} 20-day\n"
            f"Volatility (VIXY): {vixy_str}\n"
            f"Market regime: {m['regime']}"
        )
        parts.append(_section("MARKET CONTEXT", body))
    else:
        parts.append(_section("MARKET CONTEXT", "Unavailable — analyze without market context"))

    # Earnings risk
    if earnings_ctx:
        e = earnings_ctx
        eps_str = f"  |  EPS estimate: ${e['eps_estimate']:.2f}" if e.get("eps_estimate") is not None else ""
        hour_map = {"bmo": "before market open", "amc": "after market close", "dmh": "during market hours"}
        hour_str = f"  ({hour_map.get(e.get('hour', ''), '')})" if e.get("hour") else ""
        warning = ""
        if e["days_away"] <= 7:
            warning = "\n⚠ CRITICAL: Earnings in ≤7 days — extremely high risk to hold through announcement"
        elif e["days_away"] <= 21:
            warning = "\n⚠ WARNING: Earnings within 3 weeks — factor announcement risk into position size and hold duration"
        elif e["days_away"] <= 45:
            warning = "\nNote: Earnings within ~45 days — plan your exit timeline accordingly"
        confidence  = e.get("confidence", "estimated")
        source      = e.get("source", "Finnhub")
        retrieved   = e.get("retrieved", "unknown")
        conf_label  = confidence.upper()
        body = (
            f"Next earnings: {e['date']}{hour_str} — {e['days_away']} days away{eps_str}{warning}\n"
            f"Date confidence: {conf_label} · Source: {source} · Retrieved: {retrieved}\n"
            f"⚠ This is an {confidence} date from {source}. "
            f"Do not treat this as a hard-confirmed date. Verify independently before trading around this event."
        )
        parts.append(_section("EARNINGS RISK", body))
    else:
        parts.append(_section("EARNINGS RISK", "No earnings date found in next 6 months (or data unavailable)"))

    # Recent news
    if news_ctx:
        news_lines = []
        for n in news_ctx:
            news_lines.append(f"[{n['date']}] {n['headline']}  — {n['source']}")
            if n.get("summary"):
                news_lines.append(f"  {n['summary']}")
        parts.append(_section("RECENT NEWS (last 14 days)", "\n".join(news_lines)))
    else:
        parts.append(_section("RECENT NEWS (last 14 days)", "No recent news found"))

    # Insider transactions
    if insider_ctx:
        insider_lines = []
        for t in insider_ctx:
            val_str    = f"  (${t['value_usd']:,.0f})" if t.get("value_usd") else ""
            price_str  = f" @ ${t['price']:.2f}" if t.get("price") else ""
            code_str   = f"  [SEC code: {t['transaction_code']}]" if t.get("transaction_code") else ""
            signal_str = "" if t.get("is_market_signal") else "  ⚠ not an open-market transaction"
            insider_lines.append(
                f"[{t['date']}] {t['name']} — {t['action']}  "
                f"{t['shares']:,} shares{price_str}{val_str}"
                f"  (filed: {t.get('filing_date', 'n/a')}){code_str}{signal_str}"
            )
        insider_lines.append(
            "\nIMPORTANT: Only SEC code 'P' (open-market purchase) or 'S' (open-market sale) are "
            "directional signals. Grants (A), option exercises (M/X), tax withholding (F), and "
            "gifts (G) are administrative transactions — do NOT treat them as bullish or bearish signals. "
            "If transaction type is unclear, say so explicitly rather than implying directional intent."
        )
        parts.append(_section("INSIDER TRANSACTIONS (last 90 days — Finnhub / SEC Form 4)", "\n".join(insider_lines)))
    else:
        parts.append(_section("INSIDER TRANSACTIONS (last 90 days)", "No recent insider transactions found"))

    # Position details (exit mode)
    if is_exit and cost_basis is not None:
        pnl = current_price - cost_basis
        pnl_pct = (pnl / cost_basis) * 100
        stop_price = cost_basis * (1 - vrs_cfg.get("stop_loss_pct", 0.02))
        dist_to_stop = (current_price - stop_price) / current_price * 100
        rsi_now = float(result.rsi_series.iloc[-1])
        rsi_overbought = vrs_cfg.get("rsi_overbought", 65)
        rsi_headroom = rsi_overbought - rsi_now
        sign = "+" if pnl >= 0 else ""
        body = (
            f"Entry price (cost basis):  ${cost_basis:.2f}\n"
            f"Current price:             ${current_price:.2f}\n"
            f"Unrealised P&L:            {sign}${pnl:.2f} ({sign}{pnl_pct:.1f}%)\n"
            f"Stop-loss level (2% rule): ${stop_price:.2f}  "
            f"({dist_to_stop:.1f}% above stop — {'tight' if dist_to_stop < 3 else 'comfortable'})\n"
            f"RSI now: {rsi_now:.1f}  |  Overbought exit at: {rsi_overbought}  "
            f"({'only {:.1f} pts to exit trigger'.format(rsi_headroom) if rsi_headroom < 10 else '{:.1f} pts headroom'.format(rsi_headroom)})"
        )
        parts.append(_section("POSITION DETAILS", body))

    # Rule-based result
    rule_lines = []
    for c in result.conditions:
        status = "PASS" if c.passed else "FAIL"
        rule_lines.append(f"  [{status}] {c.name}\n         {c.detail}")

    rule_body = (
        f"Verdict: {result.verdict}\n"
        f"Reason:  {result.verdict_reason}\n\n"
        + "\n".join(rule_lines)
    )
    parts.append(_section("RULE-BASED TECHNICAL ANALYSIS", rule_body))

    # Strategy thresholds
    thresh_body = (
        f"RSI oversold: {vrs_cfg.get('rsi_oversold', 40)}  |  "
        f"overbought: {vrs_cfg.get('rsi_overbought', 65)}  |  "
        f"lookback: {vrs_cfg.get('rsi_lookback_bars', 10)} bars\n"
        f"SMA period: {vrs_cfg.get('sma_period', 50)}  |  "
        f"volume dry-up ratio: {vrs_cfg.get('volume_dry_up_ratio', 0.80)}"
    )
    parts.append(_section("STRATEGY THRESHOLDS", thresh_body))

    # Price/indicator data
    bars_table = _serialize_bars(df, result.rsi_series, result.sma_series)
    parts.append(_section("LAST 30 BARS — Daily OHLCV + RSI + SMA", bars_table))

    parts.append(
        "\nUsing ALL of the above context, use the trade_analysis tool to provide "
        "your structured assessment."
    )

    return {"system": system, "user": "\n".join(parts)}


# ──────────────────────────────────────────────────────────────────────
# Tool definition (varies by analysis type)
# ──────────────────────────────────────────────────────────────────────

def _build_tool_def(analysis_type: str) -> dict:
    if analysis_type == "exit":
        verdict_enum = ["HOLD", "EXIT", "EXIT_PARTIAL", "TIGHTEN_STOP"]
        verdict_desc = (
            "Your exit verdict. "
            "HOLD = thesis intact, stay in the position. "
            "EXIT = exit the full position now. "
            "EXIT_PARTIAL = take partial profits / reduce size, hold remainder. "
            "TIGHTEN_STOP = raise stop-loss level but don't exit yet."
        )
    else:
        verdict_enum = ["ENTER", "WAIT", "REJECT", "CAUTION"]
        verdict_desc = (
            "Your entry verdict. "
            "ENTER = high-quality setup, act now. "
            "WAIT = setup forming but not complete. "
            "REJECT = clear reasons to stay out. "
            "CAUTION = rules say enter but context raises meaningful risk."
        )

    base_properties = {
        "verdict": {
            "type": "string",
            "enum": verdict_enum,
            "description": verdict_desc,
        },
        "confidence": {
            "type": "string",
            "enum": ["high", "medium", "low"],
            "description": "Overall confidence given data quality and signal clarity.",
        },
        "summary": {
            "type": "string",
            "description": "One sentence headline summarising the setup and your verdict.",
        },
        "analysis": {
            "type": "string",
            "description": (
                "3-5 sentence holistic analysis. Connect the technical picture "
                "with market context, news catalyst, earnings risk, and insider activity. "
                "Explain what is driving your verdict beyond the mechanical rules."
            ),
        },
        "key_observations": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "4-6 specific, concrete observations — from ANY of the context blocks "
                "(news catalyst, insider signal, earnings proximity, market regime, "
                "price-action divergence, volume pattern). Not just technical observations."
            ),
        },
        "risks": {
            "type": "array",
            "items": {"type": "string"},
            "description": "3-5 key risks. Include earnings risk, news overhang, macro risk if relevant.",
        },
        "watch_for": {
            "type": "string",
            "description": (
                "Concrete, actionable trigger for the next 1-5 sessions. "
                "Name specific price levels, RSI levels, or news events to watch."
            ),
        },
    }
    base_required = ["verdict", "confidence", "summary", "analysis", "key_observations", "risks", "watch_for"]

    if analysis_type != "exit":
        base_properties.update({
            "final_action": {
                "type": "string",
                "enum": ["AVOID", "WATCH", "WAIT FOR TRIGGER", "STARTER ONLY", "VALID ENTRY", "ADD", "HOLD", "TRIM", "EXIT"],
                "description": (
                    "Specific final action label. "
                    "VALID ENTRY=rules pass and AI agrees cleanly. "
                    "STARTER ONLY=rules pass but risk elevated (earnings close, weak confirmation). "
                    "WAIT FOR TRIGGER=promising setup but confirmation missing. "
                    "WATCH=rules mostly fail but early signs visible. "
                    "AVOID=hard risk filters fail."
                ),
            },
            "entry_trigger": {
                "type": "string",
                "description": (
                    "Specific price level or technical condition that would confirm entry. "
                    "Name exact price and volume condition. "
                    "If insufficient data to determine, say 'Not available from current data.'"
                ),
            },
            "invalidation_level": {
                "type": "string",
                "description": (
                    "Price or condition that breaks the setup and invalidates the trade thesis. "
                    "Name the support level or price. "
                    "If insufficient data, say 'Not available from current data.'"
                ),
            },
            "position_guidance": {
                "type": "string",
                "description": (
                    "Position sizing and timing guidance. Examples: 'No entry yet.', "
                    "'Starter position only (25-50% of normal size).', "
                    "'Full position with normal risk.', "
                    "'Reduce size — earnings within swing window.', "
                    "'Avoid — distribution pressure elevated.'"
                ),
            },
        })
        base_required += ["final_action", "entry_trigger", "invalidation_level", "position_guidance"]

    return {
        "name": "trade_analysis",
        "description": (
            "Structured trade assessment combining technical signals, "
            "market context, earnings risk, news, and insider activity."
        ),
        "input_schema": {
            "type": "object",
            "properties": base_properties,
            "required": base_required,
        },
    }


# ──────────────────────────────────────────────────────────────────────
# LLM call
# ──────────────────────────────────────────────────────────────────────

def call_llm(prompt_data: dict, api_key: str, model: str, analysis_type: str = "entry") -> LLMAnalysis:
    """
    Call the Anthropic API with tool use for structured output.
    Adaptive thinking lets the model reason through the full context before answering.
    """
    try:
        import anthropic
    except ImportError:
        return LLMAnalysis(
            error="anthropic package not installed — run: pip install anthropic",
            skipped=True,
            skip_reason="package missing",
        )

    tool_def = _build_tool_def(analysis_type)

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=model,
            max_tokens=5000,
            thinking={"type": "adaptive"},
            system=prompt_data["system"],
            messages=[{"role": "user", "content": prompt_data["user"]}],
            tools=[tool_def],
            tool_choice={"type": "tool", "name": "trade_analysis"},
        )
    except Exception as exc:
        return LLMAnalysis(error=f"API call failed: {exc}", model_used=model)

    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "trade_analysis":
            inp = block.input
            return LLMAnalysis(
                verdict=inp.get("verdict", ""),
                confidence=inp.get("confidence", ""),
                summary=inp.get("summary", ""),
                analysis=inp.get("analysis", ""),
                key_observations=inp.get("key_observations", []),
                risks=inp.get("risks", []),
                watch_for=inp.get("watch_for", ""),
                model_used=model,
                entry_trigger=inp.get("entry_trigger", ""),
                invalidation_level=inp.get("invalidation_level", ""),
                position_guidance=inp.get("position_guidance", ""),
                final_action=inp.get("final_action", ""),
            )

    return LLMAnalysis(
        error="LLM response did not include a structured trade_analysis call.",
        model_used=model,
    )
