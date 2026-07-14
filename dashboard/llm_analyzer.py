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
# SEC Form 4 transaction code descriptions
# ──────────────────────────────────────────────────────────────────────

_TX_CODE_MEANINGS = {
    "P": "open-market purchase",
    "S": "open-market sale",
    "A": "grant or award (not a market transaction)",
    "D": "disposition (not necessarily an open-market sale)",
    "F": "tax withholding / payment of exercise price",
    "G": "gift",
    "M": "exercise of derivative security",
    "X": "exercise of in-the-money or at-the-money derivative",
    "J": "other transaction",
    "Z": "deposit or withdrawal from trust",
}


def _tag_news_relevance(headline: str, summary: str = "") -> str:
    """Classify a news article by relevance type using keyword matching."""
    text = (headline + " " + (summary or "")).lower()
    if any(k in text for k in [
        "earnings", " eps ", "revenue", "guidance", "quarterly results",
        "fiscal year", "q1 ", "q2 ", "q3 ", "q4 ", "annual results", "beat", "miss",
    ]):
        return "earnings/fundamentals"
    if any(k in text for k in [
        "analyst", "upgrade", "downgrade", "price target", " pt ", "overweight",
        "underweight", "outperform", "underperform", "buy rating", "sell rating",
        "neutral rating", "initiat",
    ]):
        return "analyst/rating"
    if any(k in text for k in [
        "lawsuit", " sec ", "antitrust", "regulat", "fine ", "penalty",
        "investigation", "subpoena", "doj ", " ftc ", "legal action", "settlement",
    ]):
        return "legal/regulatory"
    if any(k in text for k in [
        "artificial intelligence", " ai ", "machine learning", "data center",
        "cloud computing", "semiconductor", "nvidia", "openai", "chatgpt",
        "generative ai", "large language",
    ]):
        return "AI infrastructure"
    if any(k in text for k in [
        "federal reserve", "fed rate", "interest rate", "inflation", " gdp",
        "recession", "sector ", "s&p 500", "nasdaq", "dow jones",
        "market selloff", "broad market", "macro", "geopolit", "tariff",
    ]):
        return "sector/macro"
    if any(k in text for k in [
        "acqui", "merger", "deal ", "launch", "partnership", "product",
        "ceo ", "executive", "appoint", "hire ", "layoff", "restructur",
    ]):
        return "company-specific"
    return "low relevance"


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
                "date":      article_date,
                "headline":  headline,
                "summary":   summary,
                "source":    a.get("source", ""),
                "url":       a.get("url", ""),
                "relevance": _tag_news_relevance(headline, summary),
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
            "REPORT QUALITY RULES — FOLLOW STRICTLY:\n\n"
            "1. RSI wording: Distinguish 1-day RSI change from 3-bar trend. "
            "   Never say 'curling up' or 'momentum confirmed' if the RSI curl condition is FAIL. "
            "   If RSI is up day-over-day but 3-bar trend is down, state both explicitly.\n\n"
            "2. Banned language — do NOT use any of the following:\n"
            "   • 'institutional dumping' → say 'high-volume selling pressure' or 'distribution pressure'\n"
            "   • 'sellers exhausted' or 'seller exhaustion' → say 'possible capitulation-style low'\n"
            "   • 'buyers in control' or 'buyers are now in control' → say 'buyer participation improving'\n"
            "   • 'classic bottom' or 'textbook bottom' or 'this is a bottom' → say 'constructive oversold-recovery setup'\n"
            "   • 'confirming the reversal' → say 'consistent with the reversal thesis'\n"
            "   • 'textbook oversold reversal' → say 'constructive oversold-recovery setup'\n"
            "   • 'classic seller exhaustion' → say 'possible capitulation-style low'\n\n"
            "3. Insider signals: The data section above is already split into VERIFIED signals and "
            "   NON-MARKET transactions. If the section header says '⛔ NO VERIFIED OPEN-MARKET "
            "   TRANSACTIONS', do NOT mention any insider transaction as bullish evidence. "
            "   Use exactly: 'Reported insider transaction data exists but does not include a "
            "   verified open-market purchase — it is not used as a market signal.' "
            "   Only code P (open-market purchase) or S (open-market sale) are signals.\n\n"
            "4. Earnings: Always label the earnings date as ESTIMATED unless the data section "
            "   explicitly says CONFIRMED. Say 'estimated earnings date: [date]', not 'earnings on [date]'.\n\n"
            "5. News sourcing: To claim a selloff is macro/sector-driven, you must cite a specific "
            "   headline tagged [SECTOR/MACRO] from the news section. If no such article exists, "
            "   say 'available news does not clearly identify a macro/sector catalyst.' "
            "   Do not use [LOW RELEVANCE] articles as evidence for any claim.\n\n"
            "6. STARTER ONLY — timing clarity: When final_action is STARTER ONLY, position_guidance "
            "   MUST explicitly state ONE of:\n"
            "   (a) 'Starter position allowed now — [reason current conditions qualify].'\n"
            "   (b) 'No entry yet — starter only after [specific trigger, e.g. close above $X].'\n"
            "   Never use STARTER ONLY without making clear whether action is immediate or conditional.\n\n"
            "7. Internal consistency: analysis and key_observations must use the exact RSI values "
            "   and price levels from the rule engine results. Do not introduce different numbers."
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

    # Recent news — split by relevance so LLM knows which support macro claims
    if news_ctx:
        high_rel = [n for n in news_ctx if n.get("relevance", "") != "low relevance"]
        low_rel  = [n for n in news_ctx if n.get("relevance", "") == "low relevance"]
        news_lines = []
        for n in high_rel:
            tag = n.get("relevance", "unknown").upper()
            news_lines.append(f"[{n['date']}] [{tag}] {n['headline']}  — {n['source']}")
            if n.get("summary"):
                news_lines.append(f"  {n['summary']}")
        if low_rel:
            news_lines.append("\n--- Low-relevance articles (list only; do not use to support macro/sector claims) ---")
            for n in low_rel:
                news_lines.append(f"[{n['date']}] [LOW RELEVANCE] {n['headline']}  — {n['source']}")
        news_lines.append(
            "\n⚠ News usage rules: Only cite articles tagged SECTOR/MACRO or AI INFRASTRUCTURE "
            "to support broad-market or sector-driven claims. Do not cite LOW RELEVANCE articles "
            "as evidence. If no SECTOR/MACRO articles exist, soften any macro-driven claim."
        )
        parts.append(_section("RECENT NEWS (last 14 days — with relevance tags)", "\n".join(news_lines)))
    else:
        parts.append(_section("RECENT NEWS (last 14 days)", "No recent news found"))

    # Insider transactions — verified signals separated from non-signal transactions
    if insider_ctx:
        signal_txns     = [t for t in insider_ctx if t.get("is_market_signal")]
        non_signal_txns = [t for t in insider_ctx if not t.get("is_market_signal")]
        insider_lines   = []

        if signal_txns:
            insider_lines.append("VERIFIED OPEN-MARKET TRANSACTIONS (SEC code P or S — may be used as signals):")
            for t in signal_txns:
                val_str   = f"  (${t['value_usd']:,.0f})" if t.get("value_usd") else ""
                price_str = f" @ ${t['price']:.2f}" if t.get("price") else ""
                insider_lines.append(
                    f"  ✓ [{t['date']}] {t['name']} — {t['action']} "
                    f"{t['shares']:,} shares{price_str}{val_str}  "
                    f"(filed: {t.get('filing_date','n/a')}, code: {t.get('transaction_code','')})"
                )
        else:
            insider_lines.append(
                "⛔ NO VERIFIED OPEN-MARKET TRANSACTIONS IN THIS PERIOD.\n"
                "   None of the transactions below are open-market purchases or sales.\n"
                "   DO NOT use any transaction below as bullish or bearish evidence.\n"
                "   Required wording if you mention insiders: "
                "   'Reported insider transaction data exists but does not include a "
                "   verified open-market purchase — it is not used as a market signal.'"
            )

        if non_signal_txns:
            insider_lines.append("\nNON-MARKET TRANSACTIONS — DO NOT TREAT AS DIRECTIONAL SIGNALS:")
            for t in non_signal_txns:
                val_str   = f"  (${t['value_usd']:,.0f})" if t.get("value_usd") else ""
                price_str = f" @ ${t['price']:.2f}" if t.get("price") else ""
                code      = t.get("transaction_code", "?")
                meaning   = _TX_CODE_MEANINGS.get(code, "non-open-market transaction")
                insider_lines.append(
                    f"  ✗ [{t['date']}] {t['name']} — {t['action']} "
                    f"{t['shares']:,} shares{price_str}{val_str}  "
                    f"(code: {code} = {meaning}; filed: {t.get('filing_date','n/a')})"
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
                    "VALID ENTRY = rules pass and AI agrees, act now at current price. "
                    "STARTER ONLY = rules pass but risk elevated — MUST pair with position_guidance "
                    "stating clearly whether the starter is allowed NOW or only after a named trigger. "
                    "WAIT FOR TRIGGER = promising but confirmation missing — name the trigger in position_guidance. "
                    "WATCH = rules mostly fail but early signs visible, monitor only. "
                    "AVOID = hard risk filters fail, stay out."
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
                    "Position sizing AND timing — must be unambiguous. "
                    "If final_action is STARTER ONLY, you MUST state explicitly whether entry is "
                    "allowed NOW or only after a specific trigger: "
                    "e.g. 'Starter position (25% size) allowed now — RSI curl forming at current price.' "
                    "OR 'No entry yet — starter only after close above $X on above-average volume.' "
                    "If final_action is WAIT FOR TRIGGER, name the exact trigger price or condition. "
                    "Other examples: 'No entry.', 'Full position with normal risk.', "
                    "'Reduce size — estimated earnings within swing window.', "
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
# Output guardrails — post-process LLM text to enforce preferred phrasing
# ──────────────────────────────────────────────────────────────────────

import re as _re

# (regex pattern, replacement) pairs applied case-insensitively to all text fields.
# Replacements use preferred phrasing; the LLM prompt is kept as guidance only —
# these rules are the hard enforcement layer.
_PHRASE_GUARDRAILS: list[tuple[str, str]] = [
    # "institutional dumping" variants
    (r"\binstitutional\s+dumping\b",                    "high-volume selling pressure"),
    (r"\bdumping\s+(?:by\s+)?institutions?\b",          "high-volume selling pressure"),

    # seller-exhaustion variants
    (r"\bclassic\s+seller\s+exhaustion\b",              "possible capitulation-style low"),
    (r"\bseller\s+exhaustion\b",                        "possible capitulation-style low"),
    (r"\bsellers?\s+(?:are\s+(?:now\s+)?)?exhausted\b", "a possible capitulation-style low may be forming"),
    (r"\bsellers?\s+(?:still\s+)?not\s+exhausted\b",   "momentum confirmation is incomplete"),
    (r"\bsellers?\s+(?:still\s+)?have\s+the\s+edge\b", "buyer participation not yet confirmed"),

    # buyers-in-control variants
    (r"\bbuyers?\s+(?:are\s+(?:now\s+)?)?in\s+control\b", "buyer participation is improving"),
    (r"\bbuying\s+pressure\s+(?:is\s+)?(?:now\s+)?in\s+control\b", "buyer participation is improving"),

    # overconfident reversal / bottom language
    (r"\btextbook\s+oversold\s+reversal\b",             "constructive oversold-recovery setup"),
    (r"\btextbook\s+reversal\b",                        "constructive oversold-recovery setup"),
    (r"\bclassic\s+oversold\s+reversal\b",              "constructive oversold-recovery setup"),
    (r"\bclassic\s+reversal\b",                         "constructive oversold-recovery setup"),
    (r"\bconfirm(?:s|ing|ed)\s+the\s+reversal\s+thesis\b", "is consistent with the reversal thesis"),
    (r"\bconfirm(?:s|ing|ed)\s+the\s+reversal\b",       "is consistent with the reversal thesis"),
    (r"\bconfirm(?:s|ing|ed)\s+(?:a\s+)?reversal\b",   "is consistent with a reversal"),
    (r"\bconfirm(?:s|ing|ed)\s+(?:a\s+)?bottom\b",     "is consistent with a possible low"),
    (r"\bthis\s+is\s+(?:the\s+|a\s+)?bottom\b",        "this may represent a constructive oversold-recovery setup"),
]


def _apply_text_guardrails(text: str) -> str:
    """Apply all phrase guardrails to a single string, preserving start-of-sentence capitalisation."""
    if not text:
        return text
    for pattern, replacement in _PHRASE_GUARDRAILS:
        def _replace(m: "_re.Match") -> str:
            original = m.group(0)
            # Preserve leading capital if the matched phrase starts a sentence
            if original and original[0].isupper():
                return replacement[0].upper() + replacement[1:]
            return replacement
        text = _re.sub(pattern, _replace, text, flags=_re.IGNORECASE)
    return text


def _apply_output_guardrails(result: LLMAnalysis) -> LLMAnalysis:
    """Scrub all free-text fields of an LLMAnalysis result in-place."""
    result.summary           = _apply_text_guardrails(result.summary)
    result.analysis          = _apply_text_guardrails(result.analysis)
    result.watch_for         = _apply_text_guardrails(result.watch_for)
    result.entry_trigger     = _apply_text_guardrails(result.entry_trigger)
    result.invalidation_level = _apply_text_guardrails(result.invalidation_level)
    result.position_guidance = _apply_text_guardrails(result.position_guidance)
    result.key_observations  = [_apply_text_guardrails(o) for o in result.key_observations]
    result.risks             = [_apply_text_guardrails(r) for r in result.risks]
    return result


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
            raw = LLMAnalysis(
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
            return _apply_output_guardrails(raw)

    return LLMAnalysis(
        error="LLM response did not include a structured trade_analysis call.",
        model_used=model,
    )
