# Zignal — Usage Guide

This document covers how to use the system day-to-day: running an analysis, reading verdicts, interpreting the AI layer, tuning parameters, and understanding what the tool can and cannot tell you.

For architecture, setup, and configuration reference, see [TECHNICAL_GUIDE.md](TECHNICAL_GUIDE.md).

---

## Table of Contents

1. [Philosophy — What This Tool Is For](#1-philosophy--what-this-tool-is-for)
2. [The Analyze Page — Step by Step](#2-the-analyze-page--step-by-step)
3. [Reading the Rule-Based Verdict](#3-reading-the-rule-based-verdict)
4. [The Four Conditions Explained](#4-the-four-conditions-explained)
5. [Reading the AI Analysis](#5-reading-the-ai-analysis)
6. [The Chart Panel](#6-the-chart-panel)
7. [When to Trust the Verdict](#7-when-to-trust-the-verdict)
8. [Tuning Parameters for Your Style](#8-tuning-parameters-for-your-style)
9. [Common Scenarios and What to Do](#9-common-scenarios-and-what-to-do)
10. [Cost and API Usage](#10-cost-and-api-usage)
11. [Limitations](#11-limitations)

---

## 1. Philosophy — What This Tool Is For

This tool is a **decision support system**, not a trading bot. It does not place orders, manage positions, or tell you how much to buy. You make every entry and exit decision.

The tool exists to answer one question: **"Is this a good time to enter this stock based on the Volume-RSI Swing setup?"**

It does this in two layers:

**Layer 1 — Rule Engine:** A precise, deterministic check of four technical conditions derived directly from the VolumeRsiSwing strategy. Every pass/fail has a specific numeric reason. No ambiguity, no interpretation.

**Layer 2 — AI (claude-opus-4-8):** A holistic read of the same data by a model instructed to think like an experienced swing trader. It can see things the rules cannot — divergences, support levels, momentum context, whether the volume story is convincing — and it can disagree with the rules when context warrants it.

The combination means: rules catch what's clear-cut, AI handles the grey area.

---

## 2. The Analyze Page — Step by Step

### Open the page

Navigate to **Analyze** in the left sidebar of the dashboard.

### Fill in the inputs

| Field | What to enter | Notes |
|-------|--------------|-------|
| **Symbol** | Ticker (e.g. `MSFT`, `NVDA`) | Case-insensitive, Polygon-supported US equities |
| **To (end of window)** | The last date you want analyzed | Defaults to today |
| **From (start of window)** | Start of your visible chart window | Rule check always runs on the *last* bar; this only affects what you see in the chart |

The window you set is your **display range** — what appears in the chart. The analysis always evaluates the most recent bar (the `To` date). An extra 150-day warmup window is silently prepended so indicators are fully converged.

### Click Analyze

The system:
1. Fetches daily bars from Polygon.io (one API call)
2. Computes RSI, SMA, and volume stats
3. Runs the four entry conditions on the last bar
4. Displays the rule-based verdict and condition breakdown
5. If the setup is not an obvious reject, calls the AI for a holistic read (a few seconds)
6. Displays the full AI analysis below the charts

---

## 3. Reading the Rule-Based Verdict

A colored badge appears at the top of the results:

### ENTER (green)
All four conditions passed. The setup matches the VolumeRsiSwing entry criteria:
- RSI touched oversold and is now curling up
- SMA50 decline is slowing or price has already reversed
- Buyers are stepping in on up days
- No sign of institutional selling

**This does not mean buy immediately.** It means the setup is valid. Use the AI analysis and your own judgment to decide on entry timing and size.

### WAIT (orange)
The setup is forming but not complete. Common reasons:
- RSI is approaching oversold but hasn't touched it yet
- RSI dipped and recovered but is still trending down
- RSI and SMA conditions are met but volume isn't confirming

**Watch the stock.** Check again in 1-3 days. The verdict reason explains exactly what needs to change.

### REJECT (red)
A hard blocker is present. One of:
- Distribution days detected (institutional selling — smart money is exiting, not a time to buy)
- Stock is in true freefall (SMA accelerating down AND price still falling)
- RSI is overbought — the entry window has already passed

**Don't fight a REJECT.** The stock may still go up, but the specific setup this strategy looks for is not present. Find a different entry point or a different stock.

---

## 4. The Four Conditions Explained

### Condition 1: RSI — Oversold & Curling Up

**What it checks:** Did RSI touch below the oversold threshold (default: 40) within the last `rsi_lookback_bars` (default: 10) bars? And is RSI ticking higher today compared to yesterday?

**Why both parts matter:**
- Oversold alone is not enough — a stock can stay oversold for weeks in a downtrend
- Curling up alone is not enough — RSI at 55 ticking to 56 is not a bottoming signal
- Together they mean: sellers exhausted themselves, and buyers are now taking control

**Reading the detail:**
```
Oversold touch: PASS — RSI dipped to 36.2 (4 bars ago), recovered +8.1 pts to 44.3 now  
[single dip in last 10 bars]
Curling: PASS — curling up (43.1 → 44.3)
```
This is a clean signal — one dip into oversold territory 4 days ago, now recovering.

```
Oversold touch: PASS — RSI dipped to 33.8 (1 bars ago), recovered +2.1 pts to 35.9 now  
[single dip in last 10 bars]
Curling: FAIL — still falling (37.2 → 35.9)
```
RSI touched oversold but hasn't curled yet — wait for it to tick higher.

**Episode labels:**
- `single dip` — one clean oversold touch, classic reversal setup
- `double-bottom` — RSI went oversold, recovered partially, then went oversold again — historically a stronger setup as it shows the level was tested twice

**Key tuning:** If the tool keeps missing oversold touches you can see on the chart, increase `rsi_lookback_bars` in config from 10 to 14 or 20.

---

### Condition 2: SMA50 — Trend Flattening (Not Freefall)

**What it checks:** Is the rate of SMA50's decline slowing down? Or has price already turned up while SMA still lags?

**Why this matters:** You want to enter when a downtrend is exhausting itself, not when it's accelerating. A stock whose SMA is declining faster each week is still in active distribution — not a bottom candidate.

**The bottoming pattern override:** SMA is a lagging indicator. At actual price bottoms, price turns up *before* SMA does — this is by design. The rule recognizes this: if SMA is still declining but price has already risen over the last 3 bars, that's a normal bottoming pattern and the condition passes.

**Reading the detail:**
```
PASS (price leading) — SMA50 slope still accelerating down (-0.06% → -0.21% per bar) 
but price has already turned up +2.34% over last 3 bars. 
SMA lags price by design — this is a normal bottoming pattern
```
This is the most commonly misread condition. The SMA numbers look bad but the condition passes because price is leading the turn.

```
FAIL — SMA50 accelerating downward (-0.06% → -0.21% per bar) 
AND price still falling (-1.8% over last 3 bars) — true freefall
```
Here both SMA and price are declining together — this is an active downtrend, not a bottom.

---

### Condition 3: Volume — Accumulation (Buyers vs Sellers)

**What it checks:** Is average volume on up days at least 80% of average volume on down days over the last 20 bars?

**Why this matters:** At genuine bottoms, buying pressure starts to match or exceed selling pressure. If stocks are bouncing on weak volume but crashing on heavy volume, the rallies are likely short-covering — not real demand.

**Reading the detail:**
```
Buyers active: up-day avg vol = 94% of down-day avg vol 
(threshold ≥ 80%) — accumulation confirmed
```
Buyers are nearly as active as sellers — accumulation underway.

```
Sellers dominant: up-day avg vol = 61% of down-day avg vol 
(below 80% threshold) — no accumulation yet
```
Rallies are on thin volume — sellers still in control.

**Note:** A ratio > 100% means up days are actually seeing more volume than down days — this is very bullish and passes easily.

---

### Condition 4: Institutional Dumping — Distribution Days

**What it checks:** In the last 10 bars, how many days had volume > 1.5× average AND price fell more than 1%? More than 1 such day triggers a REJECT.

**Why this matters:** High volume + down day = institutional selling. One such day can happen for any reason. Two or more in a short window means smart money is actively distributing — they know something, or they're large enough that their own selling is creating the down days. You don't want to buy into that.

**Reading the detail:**
```
0 distribution day(s) found in last 10 bars (max allowed: 1) | 
Criteria: volume > 1.5x avg AND price drops > 1.0%
```
Clean — no evidence of institutional selling.

```
2 distribution days in 10 bars — institutional dumping detected
```
Hard REJECT. The money behind this stock is exiting.

**Distribution day markers** appear as red downward triangles above the candles in the chart.

---

## 5. Reading the AI Analysis

The AI section appears below the charts after the rule-based results. It adds a layer of judgment the rules cannot provide.

### The AI verdict

The AI uses the same ENTER / WAIT / REJECT labels as the rule engine, plus one additional option:

| Verdict | Meaning |
|---------|---------|
| **ENTER** | Holistic picture confirms the setup — high-quality opportunity |
| **WAIT** | Setup is forming; context supports patience |
| **REJECT** | AI sees reasons to stay out beyond what the rules flagged |
| **CAUTION** | Rules technically pass, but AI sees elevated risk — proceed carefully |

**CAUTION is the most important AI-only verdict.** This is what you hire the AI for: the rule engine says "all clear" but a seasoned trader looking at the same chart might say "this works on paper but I don't like the momentum here" or "this volume pattern looks suspicious." CAUTION surfaces that.

### Confidence

`high / medium / low` — reflects signal clarity, not just whether the AI agrees with you. A high-confidence WAIT means the AI is certain you should wait. A low-confidence ENTER means the setup is technically valid but the data is ambiguous.

### Analysis

2-4 sentences of holistic narrative. This is where the AI explains *why* it reached its verdict — connecting price action, volume, momentum, and indicator context in ways the individual rule pass/fail table cannot.

### Key Observations

3-5 specific findings the rules may not have captured:
- Divergences (e.g. price making lower lows but RSI making higher lows)
- Support/resistance levels in the recent price history
- Quality of the RSI oversold touch (clean spike vs gradual drift)
- Volume characteristics beyond the simple up/down day ratio

### Risks

What could make this trade go wrong. The AI specifically looks for:
- Macro/sector weakness patterns visible in the price data
- Whether the current bounce has the same characteristics as previous failed bounces in this chart
- Volume distribution red flags not severe enough to trigger a rule fail but worth noting

### Watch For

Specific price, volume, or RSI levels to monitor over the next 1-3 sessions. This gives you a concrete action trigger rather than a general "looks interesting."

---

## 6. The Chart Panel

Three panels, all sharing the same time axis:

### Panel 1: Price (Candlestick + SMA50)

- Green candles = up day (close ≥ open)
- Red candles = down day (close < open)
- Orange line = SMA50
- **Red downward triangles** = distribution days (from Condition 4)

Use this panel to visually verify the SMA flattening narrative and spot the distribution day markers.

### Panel 2: RSI

- Blue line = RSI(14)
- Green band = oversold zone (below threshold, default 40)
- Red band = overbought zone (above threshold, default 65)
- Green dotted line = oversold threshold
- Red dotted line = overbought threshold

Look for the RSI touching the green zone and then curling up. Deeper touches and sharper curls are stronger signals.

### Panel 3: Volume

- Green bars = up-day volume
- Red bars = down-day volume
- White dotted line = 20-day average volume

Distribution days will show as tall red bars significantly above the dotted average line.

---

## 7. When to Trust the Verdict

### Trust it more when:

- Rule verdict and AI verdict **agree** — both saying ENTER or both saying REJECT
- AI confidence is `high`
- RSI had a sharp, clean dip into oversold (not a slow grind)
- The volume accumulation ratio is clearly above threshold (not borderline at 81%)
- No distribution days at all (not "1, which is the allowed max")
- The AI's CAUTION or REJECT comes with specific, named observations — not vague concern

### Be more cautious when:

- Rule engine says ENTER but AI says CAUTION — read the observations carefully before acting
- Volume accumulation is borderline (80-85% of threshold)
- RSI dipped oversold only briefly or months ago (high `bars_ago` value)
- AI confidence is `low`
- The SMA pass was via the "bottoming pattern" override — true, but a weaker signal than genuine flattening

### Verdicts don't account for:

- Fundamental catalysts (earnings, FDA decisions, management changes)
- Macro environment (Fed rate decisions, sector rotations, broad market direction)
- Liquidity (works best on stocks with average daily volume > 500K shares)
- Gaps (overnight news can invalidate an analysis from the previous close)

Always check if there's an upcoming earnings announcement before entering a swing trade.

---

## 8. Tuning Parameters for Your Style

All parameters live in `config/config.yaml` under `volume_rsi_swing`. Changes take effect on the next Analyze click — no restart needed.

### I keep missing oversold setups I can see on the chart

Increase `rsi_lookback_bars` from 10 to 14 or 20. This tells the rule engine to look further back for an oversold touch.

### Too many false ENTER signals

- Lower `rsi_oversold` from 40 to 35 — requires a deeper oversold before qualifying
- Raise `volume_dry_up_ratio` from 0.80 to 0.90 — requires stronger accumulation confirmation
- Reduce `max_distribution_days` from 1 to 0 — zero tolerance for distribution

### Too many REJECT signals on stocks that look fine

- Raise `rsi_oversold` slightly (e.g. 42) to catch shallower oversold conditions
- Raise `max_distribution_days` to 2 if you're OK with some institutional activity
- Lower `distribution_vol_ratio` from 1.5 to 1.3 makes the distribution day detector less sensitive

### The SMA condition feels too strict/loose

- `sma_slope_period` (default 5) controls how many bars each slope window covers. Increase it for smoother (less reactive) slope comparisons; decrease it to catch trend changes faster.

### Standard parameters to leave alone

- `rsi_period: 14` — the universal Wilder default; changing it makes comparisons with external tools unreliable
- `stop_loss_pct` and `max_position_pct` in the `risk` section — these are execution parameters, not analysis parameters

---

## 9. Common Scenarios and What to Do

### "RSI says FAIL but I see the stock dipped to 35 five days ago"

Your `rsi_lookback_bars` is too small. Go to the Settings page or edit `config.yaml` and set `rsi_lookback_bars: 14` (or higher). Re-analyze. The condition detail will then show the earlier dip.

### "SMA says FAIL — freefall — but the last 3 candles are green"

This can happen if the 3-bar price change is negative (e.g. the stock fell hard on day 1 of the 3-bar window, and the subsequent recovery hasn't fully offset it). Look at the exact numbers in the detail. If the bottoming pattern override isn't kicking in, it means the 3-bar price change is still negative despite recent green days. You may also want to ask the AI — this is exactly the nuance the AI layer is good at.

### "ENTER from rules, CAUTION from AI"

Read the AI's Key Observations carefully. Typical reasons:
- The bounce is on below-average volume (the ratio passes but is borderline)
- There's a nearby resistance level visible in the chart
- The oversold touch was shallow and brief
- RSI double-bottom pattern where the second low was higher than the first (losing momentum)

Treat the AI's concerns as additional due diligence, not a veto. You decide.

### "REJECT — distribution days — but I think those were news-driven"

The rule engine can't distinguish a news-driven volume spike from genuine institutional distribution. The AI can reason about this to some extent. Check: were those high-volume down days clustered (persistent selling = bad) or isolated (news-driven = may be fine)? The AI's analysis section will often flag this distinction.

### "No Anthropic key set"

The AI analysis section shows an info message. The rule-based analysis still works fully — you get the four-condition breakdown, verdict, and charts without needing any Anthropic account.

---

## 10. Cost and API Usage

### Polygon.io

- **Cost:** Free tier supports daily OHLCV (end-of-day). Each Analyze click = 1 API call.
- **Rate limits:** Free tier is generous for single-user analysis use. No throttling needed for one symbol at a time.

### Anthropic (AI analysis)

- **Model:** `claude-opus-4-8` — $5.00 / 1M input tokens, $25.00 / 1M output tokens
- **Per analysis call:** ~1,500 input tokens + ~400 output tokens ≈ **$0.02 per analysis**
- **Cost gate savings:** When 3+ conditions fail, the AI call is skipped. For a 10-ticker scan where 6 are clear rejects, you pay for 4 calls instead of 10 — $0.08 instead of $0.20.
- **Adaptive thinking overhead:** The model may use additional tokens for internal reasoning on complex setups. These are billed as output tokens. For most analyses this adds a small amount to the ~$0.02 estimate.

### Alpaca

- Free for paper trading. Live trading requires a funded account. No per-API-call charges for data or order placement.

---

## 11. Limitations

**Point-in-time analysis only.** The verdict evaluates the last bar of your selected window. It does not forecast what will happen next — it assesses whether the setup conditions are met *right now*.

**Daily bars only.** The strategy and analysis are designed for swing trading (hold days to weeks). Intraday setups, scalping, and overnight gaps are not captured.

**US equities only.** The system uses Polygon.io for historical data and Alpaca for execution — both are US equity focused. International markets and crypto are not supported.

**No fundamental analysis.** The rule engine and AI both work from price, volume, and technical indicators only. Earnings, revenue growth, debt levels, sector tailwinds — none of this is factored in. Always check fundamentals before sizing up.

**Earnings risk.** If there is an upcoming earnings announcement within your expected hold period, the entire technical setup can be invalidated by the announcement. The tool does not warn you about this. Check `finnhub` earnings calendar or a financial site before entering.

**AI analysis is not backtested.** The rule engine maps directly to the VolumeRsiSwing strategy which has been backtested. The AI layer is an expert judgment overlay — it adds holistic context but its specific recommendations are not historically validated.

**The AI can hallucinate.** The model is instructed to reason from the data provided (last 30 bars + rule results). However, it may occasionally reference patterns or levels that aren't clearly visible in the data. Always cross-reference the AI's specific observations against the chart yourself.
