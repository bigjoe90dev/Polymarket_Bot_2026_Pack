# Polymarket Copy Trading Bot — Technical Audit Request

## Your Role
You are an expert quantitative trading systems auditor with deep knowledge of:
- High-frequency trading systems and market microstructure
- Python trading bot architecture and performance optimization
- Polymarket CLOB API and prediction market mechanics
- Risk management, position sizing, and statistical edge analysis
- Real-time data processing, latency optimization, and concurrency

**Your task:** Conduct a comprehensive technical audit of this Polymarket copy trading bot codebase. This is NOT a casual code review — this is a production readiness assessment for a system that will trade real money.

---

## What This System Does

### Core Strategy: Forensic Whale Copy Trading
This bot identifies profitable Polymarket traders ("whales") by analyzing the public blockchain and copies their trades in real-time. Unlike naive copy trading, it uses:

1. **Forensic wallet discovery**: Scans Polymarket's leaderboard for $3k-$10k/month traders, discovers network clusters
2. **Bayesian performance scoring**: Beta-Binomial model (Beta(2,2) prior) that learns which wallets actually make money
3. **Category-specific scoring**: A whale crushing NBA bets shouldn't get high confidence for crypto trades — scores are segmented by market type
4. **Kelly Criterion position sizing**: Half-Kelly with empirical win rates from wallet track records (minimum 5 settled trades required)
5. **13-layer stress simulation**: Models real Polymarket friction (slippage, gas, liquidity depletion, rate limits, crowd effects, expiry decay, etc.)
6. **Dynamic TP/SL**: Fast markets (crypto/sports) use 20% TP / 12% SL. Slow markets use 30% TP / 15% SL. Reward always exceeds risk.
7. **Time intelligence**: Won't enter trades within 3 minutes of market expiry. Exponential decay factor for near-expiry positions.
8. **Winner's curse protection**: Max 8% price deviation from whale entry — if price moved too much, skip the trade
9. **Anti-hedge logic**: Won't bet both YES and NO on the same market simultaneously
10. **Account growth scaling**: Position sizes scale 0.5x-2.0x based on balance performance vs. starting capital

### Modes
- **PAPER**: Simulates trading with realistic friction (current mode — validation phase)
- **SHADOW**: Watches live markets, logs what trades would be made, but doesn't execute (not implemented yet)
- **LIVE**: Real money trading via Polymarket CLOB API (execution engine exists, not battle-tested)

### Current Status
- **CONFIG_VERSION 10**: Fresh start with improved TP/SL (see below)
- $50 starting balance (paper trading validation phase)
- 700+ wallets tracked (filtered from 1000+ leaderboard candidates)
- Bayesian scoring active on wallets with 3+ historical copies
- Tracking all market categories: crypto, sports, esports, politics (no restrictions)
- **Recent fix**: TP/SL asymmetry corrected based on initial LLM review consensus (reward now exceeds risk)

### v10 Upgrade: Dynamic TP/SL Fix
**Problem identified by 5 previous LLM audits**: Old system had Take-Profit at +15%, Stop-Loss at -25%. This is backwards — we let losses run further than wins (0.6:1 risk/reward ratio).

**Solution implemented (CONFIG_VERSION 10)**:
- **Fast markets** (crypto 15-min, sports): TP=+20%, SL=-12% → 1.67:1 reward/risk ratio ✅
- **Slow markets** (all others): TP=+30%, SL=-15% → 2.0:1 reward/risk ratio ✅

Market classification uses regex patterns in `wallet_scorer.py:classify_market()` returning: `crypto_fast`, `sports_fast`, `slow`, or `unknown`.

**Why this matters**: With 50% win rate, old system averaged -5% per 2 trades. New system averages +4% (fast) or +7.5% (slow) per 2 trades. This is the difference between guaranteed loss and potential profitability.

**Implementation**: [src/paper_engine.py:822-843](src/paper_engine.py#L822-L843), [src/config.py:35-38](src/config.py#L35-L38)

### Technical Stack
- **Language**: Python 3.9.6
- **API Client**: `py-clob-client` for Polymarket CLOB
- **State**: JSON persistence with atomic writes (os.replace pattern)
- **Web UI**: stdlib http.server with token auth (15 API routes)
- **Concurrency**: Threading with RLock for state safety
- **Deployment**: Designed for 24/7 VPS (Amsterdam for low latency to CLOB)

---

## What You Must Audit

### 1. **Statistical Edge Analysis** (CRITICAL)
- **Question**: Does this system have a realistic edge after friction?
- Calculate the required whale edge for breakeven after:
  - 200bps trading fees (2% per trade)
  - Simulated slippage (1-3% depending on conditions)
  - Half-Kelly position sizing (50% of optimal)
  - 0.5-2s latency behind whale signal
- **Ask**: What's the minimum whale win rate needed to be profitable? Is 55% enough? 60%? 65%?
- Review the Bayesian scoring and Kelly sizing — are these mathematically sound? Any bugs in the implementation?

### 2. **Latency & Execution Speed**
- **Context**: This is NOT a latency-sensitive HFT system. Polymarket is a prediction market, not a stock exchange. Markets don't move in microseconds.
- Whale signals come from analyzing blockchain transactions (public, delayed data) — we're inherently 5-30 seconds behind.
- Our Winner's Curse cap (8% max price deviation) protects against stale signals.
- **Ask**: Given this context, is our polling architecture (0.5s cycle, 1s whale poll) reasonable? Or are we over-engineering for latency that doesn't matter?
- Would WebSocket subscriptions provide material benefit, or is HTTP polling adequate?

### 3. **Risk Management Robustness**
- Review [src/risk.py](src/risk.py) — RiskGuard module with exposure limits, daily loss tracking, kill-switch
- Check [src/paper_engine.py](src/paper_engine.py) — position concentration limits, anti-hedge, TP/SL logic
- **Ask**: What scenarios could cause a catastrophic loss? Flash crash? All whales on same side of a bad trade? API rate limit cascade?
- Are there hidden correlations not accounted for? (e.g., 90% of whales buy same outcome during major news events)

### 4. **Code Quality & Production Readiness**
- **Concurrency**: Is the threading safe? Any race conditions in state writes?
- **Error handling**: What happens on network failures, API changes, malformed data?
- **State corruption**: Atomic writes via os.replace — is this sufficient? Need write-ahead logging?
- **Memory leaks**: 105 trades in 30 min = ~200 trades/hour. At this rate, could we hit memory issues after 24h? 7 days?
- **Monitoring**: If the bot silently stops trading (bug, API ban, network issue), would we know? Heartbeat watchdog exists — is it enough?

### 5. **Scaling Limitations**
- **Current**: $13 paper balance, <$1 position sizes
- **Target**: $5,000+ balance, $50-150 position sizes
- **Ask**: What breaks at scale?
  - Order book impact: Will $150 trades get terrible fills on thin markets?
  - Whale threshold: Are $3k-$10k/month whales too small? Should we track $50k+ whales?
  - Market capacity: Many Polymarket markets have <$50k liquidity. Can we even deploy capital efficiently?
  - Fee optimization: Should we use limit orders (maker) instead of market orders (taker) to save 0.5-1%?

### 6. **Data Integrity & Wallet Scoring**
- Review [src/wallet_scorer.py](src/wallet_scorer.py) — Bayesian Beta-Binomial scoring, category segmentation
- **Ask**: Is there survivorship bias? (Leaderboard only shows today's winners, not yesterday's losers)
- Are we overfitting to small sample sizes? (Kelly requires 5+ trades — is that enough?)
- Category classification uses regex patterns — could we misclassify markets?
- Flow analysis and cluster detection — any value, or just complexity for no edge?

### 7. **Polymarket-Specific Risks**
- **Settlement latency**: Outcomes are manually resolved by Polymarket. Can take hours/days. Capital sits locked — opportunity cost?
- **Market creator manipulation**: Creators can dispute settlements. How do we protect against this?
- **Ambiguous questions**: Political markets often have disputed wording. Does our system flag these?
- **Volume farming**: Polymarket runs trading competitions. During these, wash trading increases. Does this pollute our whale scoring?
- **Geographic restrictions**: We're targeting Netherlands VPS to avoid UK/US blocks. Any legal/API risks?

### 8. **Architecture & Code Structure**
- Review the module split: [config.py](src/config.py), [market.py](src/market.py), [strategy.py](src/strategy.py), [execution.py](src/execution.py), [risk.py](src/risk.py), [whale_tracker.py](src/whale_tracker.py), [wallet_scorer.py](src/wallet_scorer.py), [paper_engine.py](src/paper_engine.py), [bot.py](src/bot.py)
- **Ask**: Is the separation clean? Any circular dependencies? Is the paper engine too tightly coupled to the real execution engine?
- Could we plug in alternative strategies (e.g., arb, market making) without rewriting everything?
- Dashboard UI ([static/index.html](static/index.html)) — any security holes beyond token auth?

### 9. **Missing Features / Blind Spots**
- What are we NOT doing that we should be?
- Examples from other audits: Settlement automation, cross-exchange arb, news sentiment signals, technical analysis, limit order strategies, portfolio rebalancing
- **Ask**: Which of these would provide actual edge vs. just complexity?

### 10. **Brutal Honesty Assessment**
- Forget about being polite. This is real money.
- **Ask**: Is this system likely to make money, or is it a sophisticated way to pay fees to market makers?
- If you were allocating $5,000 of your own money, would you trust this bot? Why or why not?
- What's the single biggest flaw that could cause this to fail?

---

## Audit Instructions

1. **Read the entire codebase** — All files in `src/`, the main scripts `run.py` and `backtest.py`, the dashboard UI, the config structure
2. **Trace the execution flow** — From bot startup → whale discovery → signal generation → copy decision → position management → TP/SL/settlement
3. **Check the math** — Bayesian scoring, Kelly Criterion, fee calculations, PnL tracking, stress simulation
4. **Think adversarially** — What could go wrong? Where are the bugs? What assumptions are fragile?
5. **Compare to production standards** — Not academic toy project standards — this needs to run 24/7 with real money

### Output Format

Organize your audit as:

**A. Executive Summary (3-5 sentences)**
- Overall assessment: Production-ready / Needs work / Not viable
- Biggest strength
- Biggest risk
- Recommended next step

**B. Critical Issues (Must Fix Before Live Trading)**
- List each issue with: Severity (High/Medium/Low), Location (file:line), Explanation, Suggested fix

**C. Edge Analysis**
- Calculate breakeven whale edge required
- Assess whether Bayesian + Kelly + category scoring provides sufficient selection advantage
- Probability this makes money over 6 months: X%

**D. Scaling Assessment**
- What breaks at $1k, $5k, $10k, $50k?
- Recommended capital deployment strategy

**E. Architecture Review**
- Code quality grade: A/B/C/D/F
- Production readiness: 0-100%
- Specific improvements

**F. Missing Features (Priority-Ranked)**
- List features we should add, ranked by impact on edge

**G. Honest Take**
- If you were me, would you deploy this with real money? Under what conditions?

---

## Context Files

The codebase includes a few key documentation files you should read first:
- `LLM_PROMPT.md` — High-level system overview (may be in the repo you received)
- `MEMORY.md` — Development history and build status
- Module docstrings in each `.py` file

---

## Final Note

We've already had 5 LLM reviews (Grok, Gemini, GPT-4, Kimi K2.5, DeepSeek). Common themes:
- TP/SL asymmetry concerns (we fixed this — now 20%/12% fast, 30%/15% slow)
- Polling latency (may be overblown for our use case)
- Correlation risk (we have 6% per-market exposure cap)
- Survivorship bias in whale selection (partially mitigated by Bayesian priors)
- "Sophisticated way to pay fees" criticism (this is the key question we need you to settle)

Your audit should acknowledge these previous critiques but go deeper. We need specific, actionable technical findings — not generic "copy trading doesn't work" philosophy.

**Be harsh. Be honest. This is production trading, not a school project.**
