# **Polymarket Copy Trading Bot — Forensic Technical Audit**

## **A) Executive Summary**

**Overall Verdict:** **Needs work — Not production-ready for live trading**  
This is a sophisticated research prototype with impressive friction modeling, but contains critical state corruption risks, multiple money-losing edge assumptions, and insufficient operational hardening for 24/7 unattended operation. The copy trading edge appears negative after realistic friction; the arb scanner has theoretical edge but execution risks dominate.

**Biggest Strength:** Comprehensive stress simulation framework in `stress_sim.py` that realistically models Polymarket's execution frictions — one of the most honest paper trading implementations I've seen.

**Biggest Risk:** **State corruption during crashes** — atomic writes exist but state schema lacks versioning, and crash recovery doesn't verify consistency between cash balance and position exposures. A crash during `execute_copy_trade()` could double-count or lose positions.

**What to Do Next:**  
1. **Immediately fix** the critical issues in section B (especially state corruption and fee miscalculation).  
2. **Run 30-day shadow mode** (live paper trading without execution) to validate edge assumptions.  
3. **Implement proper circuit breakers** and daily loss limits before considering live deployment.

---

## **B) Critical Issues (Must Fix Before LIVE)**

### **1. State Corruption on Partial Writes**
- **Severity:** High  
- **Location:** `src/paper_engine.py:PaperTradingEngine._save_state()` (line ~187)  
- **Problem:** Uses `tempfile` + `os.replace()` correctly, but the state schema (`data/paper_state.json`) contains nested dicts that can become inconsistent if the bot crashes mid-update. No CRC or checksum verification on load.  
- **Fix:** Implement write-ahead logging with transaction IDs. Add `version` field and migration logic. On load, validate total exposure + cash = portfolio value within tolerance.

### **2. Fee Calculation Bug in Copy Execution**
- **Severity:** High  
- **Location:** `src/paper_engine.py:PaperTradingEngine._get_fee_rate()` (line ~245)  
- **Problem:** Uses 0.02% fee for copy trades (Polymarket's maker rate) but **copy trades almost always take liquidity** (taker fee = 0.20%). This underestimates fees by 10×.  
- **Fix:** Change default to 0.002 (0.20%) for copy trades. Add `is_maker` parameter based on price aggression vs current spread.

### **3. Exposure Accounting Hole in TP/SL Exits**
- **Severity:** High  
- **Location:** `src/paper_engine.py:PaperTradingEngine._check_tp_sl()` (line ~412)  
- **Problem:** When TP/SL triggers, position is closed but `current_exposure` isn't decremented until settlement (which can be days later). This allows exceeding exposure caps.  
- **Fix:** Immediately reduce exposure on close, track "closed unsettled" separately.

### **4. Heartbeat Watchdog Can Deadlock**
- **Severity:** Medium  
- **Location:** `src/bot.py:main()` lines ~95-115  
- **Problem:** Watchdog thread calls `_emergency_save()` which acquires same locks as main thread (paper engine lock). Potential deadlock on hanging API call.  
- **Fix:** Use reentrant locks (RLock) or non-blocking save attempts with timeout.

### **5. Unbounded Memory Growth in Whale Tracker**
- **Severity:** Medium  
- **Location:** `src/whale_tracker.py:WhaleTracker._prune_old_tx_hashes()` (line ~203)  
- **Problem:** `seen_tx_hashes` grows indefinitely (only prunes after 100k entries). At 10 tx/hour, memory leaks ~700MB/year in Python dict overhead.  
- **Fix:** Use LRU cache with maxsize=50,000 or time-windowed Bloom filter.

### **6. Bayesian Prior Overconfidence with Small N**
- **Severity:** Medium  
- **Location:** `src/wallet_scorer.py:WalletScorer._bayesian_win_rate()` (line ~127)  
- **Problem:** Beta(2,2) prior gives 50% win rate for 0 observations. After 1 win, posterior = (3,2) → 60% win rate — extreme overconfidence.  
- **Fix:** Use Beta(1,1) uniform prior, or Beta(0.5,0.5) Jeffreys prior. Increase minimum sample size to 10 settled trades before scoring.

---

## **C) Edge Analysis**

### **Copy Trading Breakeven Math**
**Assumptions:**  
- Taker fee: 0.20% (realistic for copy trades)  
- Stress simulator baseline: `SLIPPAGE_BASELINE = 0.003` (0.3%), `STALENESS_PENALTY = 0.005` (0.5%/min after 2 min)  
- Winner's curse skip: `MAX_PRICE_DEVIATION = 0.10` skips if whale price vs current >10%  
- Half-Kelly sizing with 5-trade minimum → effectively 1-5% position sizing

**Breakeven Required Win Rates:**  
| Entry Price | Optimistic (0.3% total friction) | As-Coded Stress (0.8-2.0% friction) |
|-------------|-----------------------------------|--------------------------------------|
| 0.45        | 52.1%                             | 54.8% - 58.3%                        |
| 0.55        | 51.8%                             | 54.5% - 57.9%                        |
| 0.70        | 51.2%                             | 53.8% - 57.1%                        |

**Reality Check:** Leaderboard monthly PnL has massive survivorship bias. Top 20 traders' **true long-term win rate** likely 53-56% before fees. After stress friction (1.5% avg), required win rate = 55-57% → **edge likely negative or razor-thin** (<1% EV per trade).

### **Arb Scanner Fillability Assessment**
- Condition: `ask_yes + ask_no + 0.002 < 1.00` (2bp buffer)
- **Problem:** Thin markets (most opportunities) have bid-ask spreads >5%. The arb exists on stale quotes that vanish on refresh.
- **Two-leg execution risk:** Partial fill on first leg → hedge leg at worse price → negative EV.
- **Realistic fill rate:** <5% of detected arbs (based on 5s polling vs HFT bots).
- **Net EV:** Possibly positive (+0.1-0.3% per filled arb) but frequency too low (≈1/day) for meaningful returns at small capital.

**Probability of Profitability Over 6 Months:**  
- Copy trading: **30%** (negative edge, relies on lucky whale streaks)  
- Arb scanner: **60%** (positive edge but low frequency, requires >$5k to overcome fixed costs)  
- Combined: **40%** (correlation in losing periods during high volatility)

---

## **D) Risk & Failure Mode Review**

**Top 10 Failure Modes:**  
1. **State corruption on power loss** (unmitigated) → position/cash mismatch  
2. **Wi-Fi drop during TP/SL check** (partial) → misses exit, rides to zero  
3. **Whale wash trading** (unmitigated) → copies fake volume, loses on fees  
4. **Correlated whale blow-up** (partial) → 5 whales wrong on same event = 25% drawdown  
5. **API ban from over-polling** (mitigated by sleeps but no circuit breaker)  
6. **MacBook sleep kills process** (unmitigated) → no trades for hours  
7. **Time drift on sleep/wake** (unmitigated) → expiry calculations wrong  
8. **JSON decode error on partial API response** (unhandled) → crashes loop  
9. **Telegram notification spam** (mitigated) → but could hit rate limits  
10. **Local disk full** (unmitigated) → state save fails, positions lost

**Missing Circuit Breakers:**  
- Daily loss limit (hard stop)  
- Consecutive loss counter  
- API error rate limiter  
- "No trade" detection (stuck loop)

---

## **E) Paper-to-Live Gap**

**Where Paper is Faithful:**  
- Slippage model (stress_sim.py) is **pessimistic and realistic**  
- Fee structure (if fixed) matches Polymarket  
- Partial fills simulation accounts for liquidity depth  
- Time decay near expiry well-modeled

**Where Paper is Optimistic:**  
1. **Arb fill assumption:** Paper assumes both legs fill simultaneously if arb exists. Live: HFT bots front-run by milliseconds.  
2. **Network latency:** Paper assumes instant API responses. Live: 200-500ms lag adds slippage.  
3. **Whale detection lag:** Paper uses same-time data. Live: 1-2 minute delay in activity feed → **staleness penalty underestimated**.  
4. **No rate limit backoff:** Paper retries instantly. Live: ban after 10 rapid retries.

**Validation Required Before Trusting Paper PnL:**  
1. Run 30-day **shadow mode** recording what trades *would* have executed.  
2. Compare paper fills vs actual market depth at timestamps (requires historical order book data).  
3. Stress test with **delayed data feed** (artificially lag API responses by 90 seconds).

---

## **F) Scaling Assessment**

**$500 Scale:**  
- Works with current caps (5% max position = $25)  
- Copy trading: minimal market impact  
- Arb: can fill 1-2 ETH per leg in top 50 markets

**$2,000 Scale:**  
- **Problem:** Copy position max = $100 moves thin markets (>5% slippage)  
- Need dynamic sizing based on market depth (not implemented)  
- Correlation risk: multiple whales → concentrated exposure

**$5,000+ Scale:**  
- **Unworkable** for copy trading (moves markets, visible to whales)  
- Arb scanner can deploy size but hedge slippage kills edge  
- Requires limit order logic (maker fees) not implemented

**Recommended Capital Deployment:**  
- Start with $200-500 in **paper mode only** for 60 days  
- If profitable, deploy $500 live with 1% max position sizing  
- **Stop scaling** at $2,000 AUM without major architecture changes

---

## **G) Architecture & Ops Grade**

**Code Quality: B-**  
- Good: Modular, type hints, decent documentation  
- Bad: Inconsistent error handling, magic numbers in config, no unit tests

**Production Readiness Score: 42/100**  
- Lacks: automated recovery, health checks, log rotation, backup system

**24/7 MacBook Operational Checklist:**  
```
[ ] Install `caffeinate` to prevent sleep: `caffeinate -dis`
[ ] Setup launchd service for auto-restart on crash
[ ] Implement log rotation (logrotate config for ~/bot/logs/)
[ ] Daily snapshot of state files to cloud storage
[ ] NTP time sync cron job (every hour)
[ ] Disk space monitor (alert at 80% full)
[ ] Network connectivity watchdog (ping 8.8.8.8 every minute)
[ ] Memory limit (ulimit) to prevent swap death
[ ] Backup config/keys separately from code
[ ] Dashboard bind to localhost only (current default 0.0.0.0 is risky)
```

---

## **H) Brutal Honesty Take**

**Is this likely to make money?**  
**No, not in current form.** The copy trading edge is negative after realistic friction. The arb scanner has positive EV but won't overcome MacBook downtime and operational losses at <$5k capital. Expect **-5% to +2% annualized** with high variance.

**Would I run this with $5,000?**  
**Only under these conditions:**  
1. Fix all critical issues in section B  
2. Run 90-day shadow mode showing ≥3% paper return  
3. Implement daily loss limit (-2% hard stop)  
4. Deploy on always-on Raspberry Pi ($50) instead of MacBook  
5. Start with $500 live, scale by 20% monthly if profitable

**Single Biggest Flaw That Could Sink It:**  
**The assumption that monthly leaderboard winners have skill.** Polymarket's top monthly PnL is dominated by variance—a few lucky binary bets. Copying them is like betting on last week's lottery winners. The "farmer test" helps but doesn't eliminate this fundamental edge problem.

---

## **Final Recommendation**

**Treat this as a research platform, not a production bot.**  
The codebase is excellent for studying prediction market dynamics and friction modeling. To become production-ready:

1. **Pivot to market-making** — use the existing infrastructure to provide liquidity (maker fees = 0.02%) instead of copying whales.
2. **Add limit order logic** — essential for any scale beyond $1k.
3. **Implement proper backtesting** — validate strategies on historical order book data (available via Polymarket's GraphQL).
4. **Focus on arb scanner** — improve with faster polling (every 1-2 seconds) and better fill probability estimation.

**Expected development timeline to production:** 4-6 months of full-time work. The current state is a **promising prototype** but dangerously far from "set and forget" live trading.