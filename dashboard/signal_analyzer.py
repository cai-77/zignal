"""
Signal Analyzer — point-in-time entry analysis for a single ticker.

Fetches daily OHLCV from Polygon and runs the VolumeRsiSwing entry conditions
against the last bar in the requested window.  Returns a structured verdict:
  ENTER  — all conditions satisfied, entry setup confirmed
  WAIT   — setup incomplete but not broken (RSI approaching, accumulation pending)
  REJECT — hard blocker present (freefall, overbought, high-volume selling pressure)
"""

import sys
from pathlib import Path
from typing import Optional

import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from strategies.base_strategy import compute_rsi, compute_sma


# ──────────────────────────────────────────────────────────────────────
# Data fetch
# ──────────────────────────────────────────────────────────────────────

def _fetch_prev_close(symbol: str, api_key: str) -> Optional[pd.DataFrame]:
    """
    Fetch the previous close bar via Polygon's /prev endpoint.
    This data is available on free-tier and finalises sooner than list_aggs,
    so we use it to patch the most-recent-day gap when list_aggs lags behind.
    """
    try:
        import requests
        r = requests.get(
            f"https://api.polygon.io/v2/aggs/ticker/{symbol.upper()}/prev",
            params={"adjusted": "true", "apiKey": api_key},
            timeout=10,
        )
        data = r.json()
        if data.get("status") != "OK" or not data.get("results"):
            return None
        bar = data["results"][0]
        ts = pd.to_datetime(bar["t"], unit="ms", utc=True)
        row = {"open": bar["o"], "high": bar["h"], "low": bar["l"],
               "close": bar["c"], "volume": bar["v"]}
        df = pd.DataFrame([row], index=pd.DatetimeIndex([ts], name="timestamp"))
        for col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        return df
    except Exception:
        return None


def fetch_bars(symbol: str, start: str, end: str, api_key: str) -> Optional[pd.DataFrame]:
    """
    Fetch adjusted daily OHLCV bars from Polygon.
    Returns a UTC-indexed DataFrame or None on failure.
    No rate-limit sleep — caller is responsible for not hammering the API.
    """
    try:
        from polygon import RESTClient
        import datetime as _dt
        client = RESTClient(api_key=api_key)
        # Polygon timestamps daily bars at session open (13:30 UTC). Passing
        # to=end as-is gets interpreted as midnight UTC, which excludes that
        # day's bar. Add one day so today's bar is always included.
        _end_inclusive = (
            _dt.date.fromisoformat(end) + _dt.timedelta(days=1)
        ).isoformat()
        aggs = list(client.list_aggs(
            ticker=symbol.upper(),
            multiplier=1,
            timespan="day",
            from_=start,
            to=_end_inclusive,
            adjusted=True,
            sort="asc",
            limit=50_000,
        ))
    except Exception:
        return None

    if not aggs:
        return None

    rows = []
    for bar in aggs:
        rows.append({
            "timestamp": pd.to_datetime(bar.timestamp, unit="ms", utc=True),
            "open":   bar.open,
            "high":   bar.high,
            "low":    bar.low,
            "close":  bar.close,
            "volume": bar.volume,
        })

    df = pd.DataFrame(rows).set_index("timestamp").sort_index()
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # list_aggs on Polygon free-tier finalises daily bars the morning after.
    # The /prev endpoint has the same data available sooner — use it to fill
    # any gap between the last returned bar and the requested end date.
    import datetime as _dt
    end_d = _dt.date.fromisoformat(end)
    if df.index[-1].date() < end_d:
        prev_df = _fetch_prev_close(symbol, api_key)
        if prev_df is not None:
            existing_dates = {ts.date() for ts in df.index}
            new_rows = prev_df[
                [ts.date() not in existing_dates and ts.date() <= end_d
                 for ts in prev_df.index]
            ]
            if not new_rows.empty:
                df = pd.concat([df, new_rows]).sort_index()

    return df


# ──────────────────────────────────────────────────────────────────────
# Result types
# ──────────────────────────────────────────────────────────────────────

class Condition:
    def __init__(self, name: str, passed: bool, detail: str, value=None):
        self.name = name
        self.passed = passed
        self.detail = detail
        self.value = value


class AnalysisResult:
    def __init__(self):
        self.symbol: str = ""
        self.verdict: str = "WAIT"          # ENTER | WAIT | REJECT
        self.verdict_reason: str = ""
        self.conditions: list[Condition] = []
        self.rsi_series: Optional[pd.Series] = None
        self.sma_series: Optional[pd.Series] = None
        self.df: Optional[pd.DataFrame] = None
        self.dist_day_flags: Optional[pd.Series] = None  # bool series, True = distribution day
        self.error: Optional[str] = None


# ──────────────────────────────────────────────────────────────────────
# Analyzer
# ──────────────────────────────────────────────────────────────────────

def analyze(df: pd.DataFrame, symbol: str, vrs_cfg: dict) -> AnalysisResult:
    """
    Run VolumeRsiSwing entry conditions on the last bar of *df*.
    *df* should include warmup bars before the display range so indicators are stable.
    """
    result = AnalysisResult()
    result.symbol = symbol
    result.df = df

    closes  = df["close"].astype(float)
    volumes = df["volume"].astype(float)

    # Config params
    rsi_period      = int(vrs_cfg.get("rsi_period", 14))
    rsi_oversold    = float(vrs_cfg.get("rsi_oversold", 40))
    rsi_overbought  = float(vrs_cfg.get("rsi_overbought", 65))
    sma_period      = int(vrs_cfg.get("sma_period", 50))
    sma_slope_n     = int(vrs_cfg.get("sma_slope_period", 5))
    vol_lookback    = int(vrs_cfg.get("volume_lookback", 20))
    vol_avg_period  = int(vrs_cfg.get("volume_avg_period", 20))
    dry_up_ratio    = float(vrs_cfg.get("volume_dry_up_ratio", 0.80))
    dist_lookback   = int(vrs_cfg.get("distribution_lookback", 10))
    dist_vol_ratio  = float(vrs_cfg.get("distribution_vol_ratio", 1.5))
    dist_price_drop = float(vrs_cfg.get("distribution_price_drop_pct", 0.01))
    max_dist_days   = int(vrs_cfg.get("max_distribution_days", 1))

    rsi = compute_rsi(closes, rsi_period)
    sma = compute_sma(closes, sma_period)
    result.rsi_series = rsi
    result.sma_series = sma

    min_bars = max(sma_period, rsi_period, vol_avg_period) + sma_slope_n * 2 + 10
    if len(df) < min_bars:
        result.verdict = "WAIT"
        result.verdict_reason = f"Not enough data to compute indicators ({len(df)} bars, need ~{min_bars})"
        result.error = result.verdict_reason
        return result

    rsi_lookback_bars = int(vrs_cfg.get("rsi_lookback_bars", 10))
    rsi_now   = float(rsi.iloc[-1])
    rsi_prev  = float(rsi.iloc[-2])
    # Compare current RSI against 3 bars ago so a single-bar dip doesn't kill a valid curl
    rsi_3ago  = float(rsi.iloc[-4]) if len(rsi) >= 4 else rsi_prev
    curling   = rsi_now > rsi_3ago

    # Look at the full configurable window, not just last 3 bars
    rsi_window = rsi.iloc[-rsi_lookback_bars:].dropna()
    oversold_mask  = rsi_window < rsi_oversold
    touched_oversold = bool(oversold_mask.any())

    # Find the lowest RSI point and how long ago it was
    rsi_min_val  = float(rsi_window.min())
    rsi_min_iloc = int(rsi_window.values.argmin())
    bars_ago     = len(rsi_window) - 1 - rsi_min_iloc
    recovery     = rsi_now - rsi_min_val

    # Count distinct oversold episodes (consecutive runs below threshold = 1 episode)
    episodes, in_ep = 0, False
    for v in oversold_mask:
        if v and not in_ep:
            episodes += 1
            in_ep = True
        elif not v:
            in_ep = False

    rsi_passed = touched_oversold and curling

    # ── Condition 1: RSI oversold + curling up ────────────────────────
    if touched_oversold:
        ep_label = {1: "single dip", 2: "double-bottom"}.get(episodes, f"{episodes} oversold touches")
        oversold_sub = (
            f"PASS — RSI dipped to {rsi_min_val:.1f} ({bars_ago} bar{'s' if bars_ago != 1 else ''} ago), "
            f"recovered +{recovery:.1f} pts to {rsi_now:.1f} now  [{ep_label} in last {rsi_lookback_bars} bars]"
        )
    else:
        gap = rsi_min_val - rsi_oversold
        oversold_sub = (
            f"FAIL — lowest RSI in last {rsi_lookback_bars} bars was {rsi_min_val:.1f}, "
            f"needs to drop below {rsi_oversold:.0f} (missed by {gap:.1f} pts)"
        )

    curling_sub = (
        f"PASS — curling up ({rsi_3ago:.1f} → {rsi_now:.1f} over 3 bars)"
        if curling
        else f"FAIL — still falling ({rsi_3ago:.1f} → {rsi_now:.1f} over 3 bars) — wait for RSI to tick higher"
    )

    rsi_detail = f"Oversold touch: {oversold_sub}  |  Curling: {curling_sub}"
    result.conditions.append(Condition("RSI — Oversold & Curling Up", rsi_passed, rsi_detail, rsi_now))

    # ── Condition 2: SMA flattening / not in freefall ─────────────────
    n    = sma_slope_n
    need = sma_period + 2 * n
    if len(sma.dropna()) < need:
        sma_passed = False
        sma_detail = f"Insufficient bars for SMA{sma_period} slope comparison"
    else:
        sma_now  = float(sma.iloc[-1])
        sma_mid  = float(sma.iloc[-(n + 1)])
        sma_old  = float(sma.iloc[-(2 * n + 1)])
        price    = float(closes.iloc[-1])
        recent_slope = (sma_now - sma_mid) / n
        prior_slope  = (sma_mid - sma_old) / n
        recent_pct   = recent_slope / price * 100
        prior_pct    = prior_slope  / price * 100

        # Check whether price itself has already turned up (SMA lags by design)
        price_3bar_chg = (float(closes.iloc[-1]) - float(closes.iloc[-4])) / float(closes.iloc[-4]) * 100
        price_turning  = price_3bar_chg > 0

        if prior_slope < 0 and recent_slope < prior_slope:
            if price_turning:
                # Price already recovering — SMA50 will follow. Classic bottoming pattern.
                sma_passed = True
                sma_detail = (
                    f"PASS (price leading) — SMA{sma_period} slope still accelerating down "
                    f"({prior_pct:+.2f}% → {recent_pct:+.2f}% per bar) but price has already turned up "
                    f"+{price_3bar_chg:.2f}% over last 3 bars. "
                    f"SMA lags price by design — this is a normal bottoming pattern"
                )
            else:
                sma_passed = False
                sma_detail = (
                    f"FAIL — SMA{sma_period} accelerating downward "
                    f"({prior_pct:+.2f}% → {recent_pct:+.2f}% per bar) "
                    f"AND price still falling ({price_3bar_chg:+.2f}% over last 3 bars) — true freefall"
                )
        elif prior_slope >= 0:
            sma_passed = True
            sma_detail = (
                f"SMA{sma_period} in uptrend: "
                f"slope {prior_pct:+.2f}% → {recent_pct:+.2f}% per bar"
            )
        else:
            sma_passed = True
            sma_detail = (
                f"SMA{sma_period} decline slowing (flattening): "
                f"slope {prior_pct:+.2f}% → {recent_pct:+.2f}% per bar"
            )

    result.conditions.append(Condition(f"SMA{sma_period} — Trend Flattening (Not Freefall)", sma_passed, sma_detail))

    # ── Condition 3: Volume accumulation ──────────────────────────────
    recent    = df.iloc[-vol_lookback:]
    prev_c    = closes.shift(1)
    up_mask   = closes >= prev_c
    down_mask = closes <  prev_c
    recent_up   = recent[up_mask.reindex(recent.index, fill_value=False)]
    recent_down = recent[down_mask.reindex(recent.index, fill_value=False)]

    if recent_down.empty:
        vol_passed = True
        vol_ratio  = float("inf")
        vol_detail = "No down days in lookback window — very bullish"
    elif recent_up.empty:
        vol_passed = False
        vol_ratio  = 0.0
        vol_detail = "No up days in lookback window — relentless selling"
    else:
        up_vol_avg   = float(recent_up["volume"].astype(float).mean())
        down_vol_avg = float(recent_down["volume"].astype(float).mean())
        vol_ratio    = up_vol_avg / down_vol_avg
        if vol_ratio >= dry_up_ratio:
            vol_passed = True
            vol_detail = (
                f"Buyers active: up-day avg vol = {vol_ratio:.1%} of down-day avg vol "
                f"(threshold ≥ {dry_up_ratio:.1%}) — accumulation confirmed"
            )
        else:
            vol_passed = False
            vol_detail = (
                f"Sellers dominant: up-day avg vol = {vol_ratio:.1%} of down-day avg vol "
                f"(below {dry_up_ratio:.1%} threshold) — no accumulation yet"
            )

    result.conditions.append(Condition(
        "Volume — Accumulation (Buyers vs Sellers)", vol_passed, vol_detail,
        vol_ratio if vol_ratio != float("inf") else None,
    ))

    # ── Condition 4: Distribution days (high-volume selling pressure) ───
    recent_dist = df.iloc[-dist_lookback:]
    avg_vol     = float(volumes.iloc[-vol_avg_period:].mean())
    vol_thresh  = avg_vol * dist_vol_ratio if avg_vol > 0 else float("inf")
    prev_c_dist = closes.shift(1)

    dist_flags = pd.Series(False, index=df.index)
    dist_days  = 0
    for ts in recent_dist.index:
        vol = float(recent_dist.loc[ts, "volume"])
        c   = float(recent_dist.loc[ts, "close"])
        pc  = float(prev_c_dist.loc[ts]) if ts in prev_c_dist.index else c
        if pc > 0 and vol > vol_thresh and (pc - c) / pc > dist_price_drop:
            dist_days += 1
            dist_flags.loc[ts] = True

    result.dist_day_flags = dist_flags

    dist_passed = dist_days <= max_dist_days
    dist_detail = (
        f"{dist_days} distribution day(s) found in last {dist_lookback} bars "
        f"(max allowed: {max_dist_days}) | "
        f"Criteria: volume > {dist_vol_ratio}x avg AND price drops > {dist_price_drop:.1%}"
    )
    result.conditions.append(Condition(
        "High-Volume Selling Pressure — Distribution Days", dist_passed, dist_detail, dist_days,
    ))

    # ── Verdict ───────────────────────────────────────────────────────
    if not dist_passed:
        result.verdict = "REJECT"
        result.verdict_reason = (
            f"Distribution pressure detected — {dist_days} distribution day(s) "
            f"in last {dist_lookback} bars (limit: {max_dist_days})"
        )
    elif not sma_passed:
        result.verdict = "REJECT"
        result.verdict_reason = (
            f"Stock in freefall — SMA{sma_period} decline is accelerating, not flattening"
        )
    elif rsi_now >= rsi_overbought:
        result.verdict = "REJECT"
        result.verdict_reason = (
            f"RSI={rsi_now:.1f} is overbought (≥ {rsi_overbought:.0f}) — missed the entry, too extended"
        )
    elif not rsi_passed:
        result.verdict = "WAIT"
        if not touched_oversold:
            gap = rsi_min_val - rsi_oversold
            if gap <= 3:
                result.verdict_reason = (
                    f"RSI nearly oversold — lowest reading in last {rsi_lookback_bars} bars was {rsi_min_val:.1f}, "
                    f"just {gap:.1f} pts above the {rsi_oversold:.0f} threshold. "
                    f"{'RSI curling up — ' if curling else ''}"
                    f"Watch closely, one more small pullback could set up an entry"
                )
            else:
                result.verdict_reason = (
                    f"RSI={rsi_now:.1f}, lowest in last {rsi_lookback_bars} bars was {rsi_min_val:.1f} — "
                    f"needs to dip below {rsi_oversold:.0f} to qualify as an oversold setup"
                )
        else:
            if rsi_now > rsi_prev and rsi_now <= rsi_3ago:
                _rsi_curl_msg = (
                    f"RSI ticked up day-over-day ({rsi_prev:.1f} → {rsi_now:.1f}), "
                    f"but the 3-bar trend is still declining ({rsi_3ago:.1f} → {rsi_now:.1f}). "
                    f"Momentum confirmation remains incomplete — wait for the 3-bar trend to confirm the curl"
                )
            else:
                _rsi_curl_msg = (
                    f"RSI is declining day-over-day ({rsi_prev:.1f} → {rsi_now:.1f}) "
                    f"and the 3-bar trend also points down ({rsi_3ago:.1f} → {rsi_now:.1f}). "
                    f"Momentum confirmation is incomplete"
                )
            result.verdict_reason = (
                f"RSI dipped to {rsi_min_val:.1f} ({bars_ago} bar{'s' if bars_ago != 1 else ''} ago) "
                f"and recovered to {rsi_now:.1f}. {_rsi_curl_msg}"
            )
    elif not vol_passed:
        result.verdict = "WAIT"
        result.verdict_reason = (
            f"RSI and SMA conditions met but volume accumulation not confirmed "
            f"(up-day vol = {vol_ratio:.1%} of down-day vol, need ≥ {dry_up_ratio:.1%}). "
            f"Buyer participation not yet confirmed — wait for up-day volume to improve"
        )
    else:
        result.verdict = "ENTER"
        result.verdict_reason = (
            "All entry conditions satisfied: RSI oversold and curling, "
            f"SMA{sma_period} flattening, volume accumulation confirmed, "
            "no distribution pressure detected"
        )

    return result


# ──────────────────────────────────────────────────────────────────────
# Exit analyzer
# ──────────────────────────────────────────────────────────────────────

def analyze_exit(
    df: pd.DataFrame,
    symbol: str,
    vrs_cfg: dict,
    cost_basis: Optional[float] = None,
) -> AnalysisResult:
    """
    Run exit-specific conditions on the last bar of *df*.
    Verdict mapping (reuses AnalysisResult fields):
      ENTER  → HOLD    — thesis intact, no exit signal
      WAIT   → CAUTION — one warning sign, consider tightening stop
      REJECT → EXIT    — strong exit signal (stop hit or multiple bearish flags)
    """
    result = AnalysisResult()
    result.symbol = symbol
    result.df = df

    closes  = df["close"].astype(float)
    volumes = df["volume"].astype(float)

    rsi_period      = int(vrs_cfg.get("rsi_period", 14))
    rsi_overbought  = float(vrs_cfg.get("rsi_overbought", 65))
    sma_period      = int(vrs_cfg.get("sma_period", 50))
    sma_slope_n     = int(vrs_cfg.get("sma_slope_period", 5))
    vol_avg_period  = int(vrs_cfg.get("volume_avg_period", 20))
    dist_lookback   = int(vrs_cfg.get("distribution_lookback", 10))
    dist_vol_ratio  = float(vrs_cfg.get("distribution_vol_ratio", 1.5))
    dist_price_drop = float(vrs_cfg.get("distribution_price_drop_pct", 0.01))
    max_dist_days   = int(vrs_cfg.get("max_distribution_days", 1))
    stop_loss_pct   = float(vrs_cfg.get("stop_loss_pct", 0.02))

    rsi = compute_rsi(closes, rsi_period)
    sma = compute_sma(closes, sma_period)
    result.rsi_series = rsi
    result.sma_series = sma

    min_bars = max(sma_period, rsi_period, vol_avg_period) + sma_slope_n * 2 + 10
    if len(df) < min_bars:
        result.verdict = "WAIT"
        result.verdict_reason = f"Not enough data ({len(df)} bars, need ~{min_bars})"
        result.error = result.verdict_reason
        return result

    price   = float(closes.iloc[-1])
    rsi_now = float(rsi.iloc[-1])

    exit_signals = 0

    # ── Condition 1: Stop-loss check ──────────────────────────────────
    if cost_basis and cost_basis > 0:
        stop_level = cost_basis * (1 - stop_loss_pct)
        pnl_pct    = (price - cost_basis) / cost_basis * 100
        sign       = "+" if pnl_pct >= 0 else ""
        if price < stop_level:
            stop_passed = False
            stop_detail = (
                f"STOP HIT — price ${price:.2f} is below stop-loss ${stop_level:.2f} "
                f"({stop_loss_pct:.0%} rule from ${cost_basis:.2f} entry). "
                f"P&L: {sign}{pnl_pct:.1f}%. Exit to protect capital."
            )
            exit_signals += 2  # hard exit, count double
        else:
            stop_passed = True
            cushion = (price - stop_level) / price * 100
            stop_detail = (
                f"Stop-loss intact — price ${price:.2f} is {cushion:.1f}% above stop ${stop_level:.2f} "
                f"({stop_loss_pct:.0%} rule from ${cost_basis:.2f} entry). "
                f"P&L: {sign}{pnl_pct:.1f}%."
            )
    else:
        stop_passed = True
        stop_detail = "No cost basis provided — stop-loss check skipped"
    result.conditions.append(Condition("Stop-Loss Level", stop_passed, stop_detail, price))

    # ── Condition 2: RSI overbought / momentum ────────────────────────
    rsi_prev = float(rsi.iloc[-2]) if len(rsi) >= 2 else rsi_now
    rsi_3ago = float(rsi.iloc[-4]) if len(rsi) >= 4 else rsi_prev
    falling  = rsi_now < rsi_3ago

    if rsi_now >= rsi_overbought:
        rsi_passed = False
        rsi_detail = (
            f"OVERBOUGHT — RSI={rsi_now:.1f} has reached the {rsi_overbought:.0f} exit threshold"
            + (f" and is now falling ({rsi_3ago:.1f}→{rsi_now:.1f}) — momentum fading" if falling else "")
            + ". Consider taking profits or tightening stop."
        )
        exit_signals += 1
    elif rsi_now >= rsi_overbought - 7:
        rsi_passed = True
        rsi_detail = (
            f"RSI={rsi_now:.1f} — approaching overbought ({rsi_overbought:.0f}), "
            f"{rsi_overbought - rsi_now:.1f} pts of headroom. "
            f"Momentum {'fading' if falling else 'still rising'} — watch closely."
        )
    else:
        rsi_passed = True
        rsi_detail = (
            f"RSI={rsi_now:.1f} — {rsi_overbought - rsi_now:.1f} pts below overbought ({rsi_overbought:.0f}). "
            f"Momentum {'fading (3-bar decline)' if falling else 'still constructive'}."
        )
    result.conditions.append(Condition("RSI — Overbought / Momentum", rsi_passed, rsi_detail, rsi_now))

    # ── Condition 3: SMA uptrend still intact ─────────────────────────
    n    = sma_slope_n
    need = sma_period + 2 * n
    if len(sma.dropna()) < need:
        sma_passed = True
        sma_detail = f"Insufficient bars for SMA{sma_period} slope"
    else:
        sma_now      = float(sma.iloc[-1])
        sma_mid      = float(sma.iloc[-(n + 1)])
        sma_old      = float(sma.iloc[-(2 * n + 1)])
        recent_slope = (sma_now - sma_mid) / n
        prior_slope  = (sma_mid - sma_old) / n
        recent_pct   = recent_slope / price * 100
        prior_pct    = prior_slope  / price * 100
        above_sma    = price > sma_now

        if recent_slope < 0 and prior_slope < 0 and recent_slope < prior_slope:
            sma_passed = False
            sma_detail = (
                f"TREND BREAKING DOWN — SMA{sma_period} slope accelerating downward "
                f"({prior_pct:+.2f}% → {recent_pct:+.2f}% per bar). "
                f"Price {'above' if above_sma else 'below'} SMA at ${sma_now:.2f}."
            )
            exit_signals += 1
        elif recent_slope < 0:
            sma_passed = True
            sma_detail = (
                f"SMA{sma_period} starting to flatten ({prior_pct:+.2f}% → {recent_pct:+.2f}% per bar). "
                f"Price {'above' if above_sma else 'below'} SMA at ${sma_now:.2f}. Monitor closely."
            )
        else:
            sma_passed = True
            sma_detail = (
                f"SMA{sma_period} rising ({prior_pct:+.2f}% → {recent_pct:+.2f}% per bar). "
                f"Price {'above' if above_sma else 'below'} SMA at ${sma_now:.2f}. Uptrend intact."
            )
    result.conditions.append(Condition(f"SMA{sma_period} — Uptrend Intact", sma_passed, sma_detail))

    # ── Condition 4: Distribution days ───────────────────────────────
    recent_dist = df.iloc[-dist_lookback:]
    avg_vol     = float(volumes.iloc[-vol_avg_period:].mean())
    vol_thresh  = avg_vol * dist_vol_ratio if avg_vol > 0 else float("inf")
    prev_c      = closes.shift(1)

    dist_flags = pd.Series(False, index=df.index)
    dist_days  = 0
    for ts in recent_dist.index:
        vol = float(recent_dist.loc[ts, "volume"])
        c   = float(recent_dist.loc[ts, "close"])
        pc  = float(prev_c.loc[ts]) if ts in prev_c.index else c
        if pc > 0 and vol > vol_thresh and (pc - c) / pc > dist_price_drop:
            dist_days += 1
            dist_flags.loc[ts] = True

    result.dist_day_flags = dist_flags

    if dist_days > max_dist_days:
        dist_passed = False
        dist_detail = (
            f"DISTRIBUTION — {dist_days} heavy-volume down day(s) in last {dist_lookback} bars "
            f"(threshold >{max_dist_days}). Institutions may be reducing exposure."
        )
        exit_signals += 1
    else:
        dist_passed = True
        dist_detail = (
            f"{dist_days} distribution day(s) in last {dist_lookback} bars "
            f"(threshold >{max_dist_days}). No abnormal institutional selling."
        )
    result.conditions.append(Condition("High-Volume Selling Pressure — Distribution Days", dist_passed, dist_detail, dist_days))

    # ── Exit verdict ──────────────────────────────────────────────────
    if exit_signals >= 2 or (cost_basis and price < cost_basis * (1 - stop_loss_pct)):
        result.verdict = "REJECT"
        fails = [c.name for c in result.conditions if not c.passed]
        result.verdict_reason = (
            f"Multiple exit signals: {', '.join(fails)}. "
            f"Strong case to exit or significantly reduce position."
        )
    elif exit_signals == 1:
        result.verdict = "WAIT"
        fails = [c.name for c in result.conditions if not c.passed]
        result.verdict_reason = (
            f"Exit signal flagged: {fails[0]}. "
            f"Consider tightening stop-loss or taking partial profits."
        )
    else:
        result.verdict = "ENTER"
        result.verdict_reason = (
            "No exit signals: stop intact, RSI has room, "
            f"SMA{sma_period} uptrend holding, no distribution pressure. "
            "Original thesis still valid — hold the position."
        )

    return result
