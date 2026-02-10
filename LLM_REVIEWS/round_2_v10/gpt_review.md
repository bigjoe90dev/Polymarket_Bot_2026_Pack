

Below is a forensic, production-readiness audit of the repo at `/mnt/data/polymarket_bot_audit/Polymarket_Bot_2026_Pack-main`, treating it as if it will trade real money 24/7 on a **local MacBook** (sleep/wake + Wi-Fi drops + local-only dashboard + no paid infra assumptions).

---

## A) Executive Summary (Verdict + Why)

**Verdict: Needs work (not safe for LIVE as-is).**
Biggest strength: you *try* to model adverse execution (stress sim) and enforce basic portfolio constraints (market caps, exposure caps, TP/SL, winner’s-curse skips).
Biggest risk: **fee + staleness modeling is wrong in money-moving paths**, and **risk state is not persisted/rebuilt**, so a restart can silently exceed exposure limits.
Next: (1) fix fee plumbing + fee math everywhere; (2) fix signal age and execute against the **current order book**; (3) persist/rebuild exposure + daily loss; (4) add circuit breakers + “doing nothing” alerts.

---

## B) Critical Issues (Must Fix Before LIVE)

### 1) **Fee handling is fundamentally broken (wrong defaults + wrong math + never actually fetched)**

* **Severity:** High
* **Location:**

  * `src/paper_engine.py:_get_fee_rate()` lines **672–684**
  * `src/paper_engine.py:execute_copy_trade()` fee calc lines **368–372**
  * `src/paper_engine.py:_auto_sell()` fee calc lines **711–713**
  * `src/paper_engine.py:close_copy_position()` fee calc lines **600–604**
* **Why it matters:**

  * `_get_fee_rate()` calls `market_client.get_fee_rate_bps()`, but your `MarketDataService` has **no such method** (`src/market.py`), so PAPER always falls back to **DEFAULT_FEE_BPS=200**.
  * Worse: if the API returns 0, you treat that as “use conservative default 200” (line **678–680**), which directly contradicts Polymarket’s fee model for most markets (0 bps), and also breaks arb EV math.
  * Your copy-trade fee math is **linear notional × bps**, but Polymarket fee is a **price-curve fee** for fee-enabled markets (varies with price, peaks near 50%). ([docs.polymarket.com][1])
* **Exact suggested fix (implementation-level):**

  1. Add fee-rate support to `MarketDataService`:

     * Implement `get_fee_rate_bps(token_id)` in `src/market.py` that calls the documented endpoint `GET https://clob.polymarket.com/fee-rate?token_id=...` and returns `fee_rate_bps` (0 for fee-free). ([docs.polymarket.com][1])
  2. Change `_get_fee_rate()` behavior:

     * If API returns **0**, return **0** (do not override).
     * Set fallback default to **0**, not 200, unless user explicitly wants pessimistic override.
  3. Replace **all** fee calculations in paper engine with `paper_fees.calculate_trading_fee(price, size, fee_bps)` (you already implemented this correctly for arb in `src/paper_fills.py`).

---

### 2) **Copy-trading staleness is mis-modeled: you pass “time since detection”, not “time since whale trade”**

* **Severity:** High
* **Location:**

  * `src/whale_tracker.py` signal fields include `timestamp` (trade time) lines **555, 620** and `detected_at` lines **556, 621**
  * `src/paper_engine.py:execute_copy_trade()` uses `signal.get("detected_at")` for age lines **425–433**
* **Why it matters:**
  The stress simulator’s staleness and drift are supposed to represent being late to the whale. But you compute age as `now - detected_at`, which is typically milliseconds/seconds. In reality, with round-robin polling of hundreds of wallets, you can easily be **minutes** behind.
* **Exact fix:**
  In `execute_copy_trade()` and `close_copy_position()` replace:

  * `signal_age = time.time() - signal.get("detected_at", time.time())`
    with:
  * `signal_age = time.time() - normalize_ts(signal.get("timestamp"))`
    and implement `normalize_ts()` to handle:
  * seconds vs milliseconds,
  * ISO timestamps if Data API returns strings.

---

### 3) **Risk limits can be bypassed after restart (exposure + daily loss not persisted or rebuilt)**

* **Severity:** High
* **Location:**

  * `src/risk.py:RiskGuard` has in-memory `current_exposure`, `daily_loss` only (no persistence)
  * `src/bot.py` adds exposure after trades (e.g., copy: lines **165–168**, arb: **206–208**)
* **Why it matters:**
  If the process crashes/restarts, `RiskGuard.current_exposure` resets to 0 while paper engine may still have many open positions in `data/paper_state.json`. The bot can then open *new* positions beyond caps.
* **Exact fix:**
  On startup (in `TradingBot.__init__()` or `RiskGuard.__init__()`), rebuild:

  * `current_exposure = sum(pos["total_cost"] for OPEN copy positions) + sum(arb_positions exposure)`
  * `daily_loss` from realized PnL entries by date (or persist `daily_loss` in a separate `data/risk_state.json` with atomic writes).
    Also, update exposure removal to be **idempotent** (e.g., track “exposure_id” per position to avoid double-remove).

---

### 4) **Arb scanner can execute negative-EV trades (strategy ignores fees; execution doesn’t reject negative EV)**

* **Severity:** High
* **Location:**

  * `src/strategy.py:check_opportunity()` lines ~**60–120** (checks `ask_yes + ask_no + buffer < 1.0`, ignores fees)
  * `src/paper_engine.py:execute_paper_trade()` lines **169–177** (accepts fill result without rejecting negative `expected_profit`)
* **Why it matters:**
  Even small taker fees (or fee-enabled markets) can erase thin arbs. Polymarket has fee-free markets broadly, but fee-enabled markets exist (notably 15-min crypto). ([docs.polymarket.com][1])
  In your current PAPER implementation, you can still “take” trades that are net negative after fees because there’s no `if expected_profit <= 0: reject`.
* **Exact fix:**

  * In `check_opportunity()`: compute **net** profit including fee curve (and a slippage buffer), then require `net_profit_per_share >= MIN_PROFIT`.
  * In `execute_paper_trade()`: hard reject if `expected_profit <= 0` (and log `ARB_REJECT_NEG_EV`).

---

### 5) **Fee + stress + winner’s-curse are currently double-counting in a way that collapses trade count**

* **Severity:** Medium–High
* **Location:** `src/stress_sim.py` + `execute_copy_trade()` winner’s curse check lines **263–266**
* **Why it matters:**
  Your stress entry slippage averages ~9–10% in many conditions; then you also enforce **MAX_PRICE_DEVIATION=8%** (default). That combination means **~95% of “successful” stress entries fail the deviation guard** (net accept rate ~5% in Monte Carlo). Result: bot “alive but doing nothing.”
* **Exact fix:**
  Make slippage/price-move penalties:

  * primarily a function of **actual signal age** and **current book move vs whale price**, not large independent random bumps.
  * Then keep MAX_PRICE_DEVIATION as the deterministic “late signal guard.”

---

### 6) **Concurrency hazards: watchdog can save scorer/whale state while main thread mutates dicts**

* **Severity:** Medium
* **Location:**

  * Watchdog thread: `src/bot.py:_watchdog_loop()` lines **275–292**
  * `src/wallet_scorer.py:_save_state()` mutates `flow_events` and `market_types` lines **83–90**
* **Why it matters:**
  You can hit “dict changed size during iteration” or silently corrupt saved JSON snapshots under concurrent mutation.
* **Exact fix:**
  Add a `threading.RLock()` to `WalletScorer` and `WhaleTracker` and take the lock in **all** read/write and `_save_state()` operations. Or: remove watchdog writes and only persist state from the main loop at controlled safe points.

---

### 7) **Fail-open behaviors are unsafe for LIVE**

* **Severity:** Medium
* **Location:** `src/market.py:check_book_health()` lines **161–176** (treats most errors as healthy)
* **Why it matters:**
  In LIVE, “API error, proceeding” is how you place orders into unknown conditions.
* **Exact fix:**
  In LIVE: fail-closed (block trade) on book fetch errors, rate-limit responses, and parsing errors. Keep fail-open only for PAPER experimentation.

---

## C) Edge Analysis

### Copy trading breakeven math (your system’s economics)

For a binary share, ignoring early exits, expected profit per share is:

[
E[\pi] = w - C
]

Where:

* (w) = true probability your copied side wins (empirical whale win rate for that class of trades)
* (C) = your **all-in cost per share** (entry price after slippage + fees per share + gas-per-share)

**Breakeven condition:** (w > C).
So the “minimum whale win rate needed” is simply your effective cost per share.

#### Scenario 1 — Optimistic friction (fee-free markets, mild slippage)

Assume:

* fee = 0 bps (most markets) ([docs.polymarket.com][2])
* slippage = 1%

Then (C \approx p \times 1.01)

| Entry price p | Breakeven win rate |
| ------------: | -----------------: |
|          0.45 |             0.4545 |
|          0.55 |             0.5555 |
|          0.70 |             0.7070 |

This is *plausible* if whales truly have edge **and** you’re not systematically late.

#### Scenario 2 — Fee-enabled markets (15-min crypto), mild slippage

Polymarket documents fee-enabled markets (currently 15-minute crypto) and shows a fee curve that peaks around mid prices. ([docs.polymarket.com][1])
Using the documented curve behavior (max effective ~1.56% around 0.50), the breakeven cost rises modestly:

| Entry price p | Breakeven win rate (≈1% slip + fee curve) |
| ------------: | ----------------------------------------: |
|          0.45 |                                    ~0.462 |
|          0.55 |                                    ~0.563 |
|          0.70 |                                    ~0.713 |

Still plausible—**if** you’re copying quickly and getting close to whale entry.

#### Scenario 3 — “As-coded stress” + “as-coded fees” (what PAPER is roughly doing today)

With your current code path (fee fallback to 200 bps + heavy random slippage), Monte Carlo using `stress_sim.py` yields mean cost per share roughly:

| Whale price p | Mean cost/share | Breakeven win rate | Approx fill success | *Pass winner’s-curse* |
| ------------: | --------------: | -----------------: | ------------------: | --------------------: |
|          0.45 |          ~0.504 |             ~50.4% |                ~49% |                   ~5% |
|          0.55 |          ~0.616 |             ~61.6% |                ~49% |                   ~5% |
|          0.70 |          ~0.784 |             ~78.4% |                ~50% |                   ~5% |

**Interpretation:** under as-coded stress, copy trading is **not viable** except maybe at low prices and only when you get unusually favorable execution. It also means PAPER results are extremely sensitive to simulator parameters and the broken fee/staleness plumbing.

#### Sanity check: are simulator constants too pessimistic or not pessimistic enough?

* **Too pessimistic on slippage randomness:** your mean slippage is ~10% in many cases, which is far larger than what you should expect in liquid markets when you submit promptly; that effectively forces near-zero trade count once combined with the 8% deviation guard.
* **Not pessimistic enough on “being late”:** you currently don’t pass whale-trade timestamp into stress; you often treat “minutes late” as “seconds late,” which is the *actual* killer for copy trading.

**Net:** the simulator is simultaneously *too harsh in random slippage* and *too soft on real staleness*. That’s the worst combination for trusting paper PnL.

---

### Arb strategy: fillability + net EV

**Core condition:** buy YES at best ask and NO at best ask; profit/share at settlement:

[
\pi = 1 - (a_{yes} + a_{no}) - fees - slippage - leg\ risk
]

**Your detection (`strategy.py`) ignores fees** and just checks `ask_yes + ask_no + buffer < 1`. That only makes sense in a world of zero fees and “both legs fill instantly at top-of-book”.

**Realistic issues:**

1. **Two-leg partial fill risk**: in LIVE, the first leg can fill and the second can drift away; your 10-second polling hedge can be late in fast markets.
2. **Depth realism**: you check top-of-book size, but not whether size will still be there after your first order hits.
3. **Fee-enabled markets**: in 15-min crypto, taker fees exist and are specifically intended to reduce latency arb. ([docs.polymarket.com][1])
4. **Your PAPER can take negative EV trades** (see Critical Issue #4).

**Assessment:** arb scanning can be profitable *only* if:

* you restrict to fee-free markets (or switch to maker behavior),
* you reject net-negative EV after fee curve,
* you treat “two-leg risk” as a cost and require wider edge.

---

### “Probability of profitability over 6 months”

Given current code behavior:

* copy trading: **low** because (a) selection bias (leaderboard), (b) staleness mismatch, (c) fee and execution modeling wrong in multiple places, (d) risk resets on restart.
* arb: **low-to-moderate** if fixed, but currently can trade negative EV.

**My estimate:** **10–25%** chance of being net profitable over 6 months *even in PAPER→LIVE transition*, unless you fix the critical issues and validate fill realism with a shadow/watch-only period.

---

## D) Risk & Failure Mode Review

### Top 10 ways this can fail (and whether mitigated)

1. **Restart resets exposure/daily loss** → bot over-trades into correlation blow-up

   * Mitigation: **No** (must rebuild/persist).
2. **Fee model mismatch** → systematic bleed (copy & arb)

   * Mitigation: **No** (broken today).
3. **Copy staleness** (minutes late) → buy tops, lose to drift

   * Mitigation: **Partial** (winner’s curse exists) but staleness isn’t computed correctly.
4. **Whales herd into same market** → correlated loss wipes small account

   * Mitigation: **Partial** (per-market cap exists) but correlation across related markets not handled.
5. **LIVE two-leg arb: one fills, other doesn’t** → unhedged exposure

   * Mitigation: **Partial** (hedge/cancel exists) but polling-based and may be late.
6. **Wi-Fi drop / captive portal** → missed exits / stale TP triggers

   * Mitigation: **No** robust “network degraded → stop trading” circuit breaker.
7. **Mac sleep/lid close** → process paused, clocks drift vs expiry logic

   * Mitigation: **No** (ops checklist needed).
8. **State corruption / schema drift** → cannot restart safely

   * Mitigation: **Partial** (atomic writes exist) but no schema versioning or migrations.
9. **Silent “alive but idle”** due to over-strict gates or API changes

   * Mitigation: **Partial** (heartbeat exists) but no “no trades in X hours” alert.
10. **Disk bloat** (collector snapshots, histories) → crashes, write failures

* Mitigation: **Partial** (some trimming) but collector can run unbounded.

### Kill-switch / circuit breaker improvements (concrete)

* Add `CircuitBreaker` in `src/bot.py`:

  * trigger if consecutive fetch errors > N, or if API returns throttling, or if time drift > threshold
  * action: stop trading, send Telegram, keep dashboard alive
* Add **“no trades in X hours”** Telegram alert with last trade reason histogram.

---

## E) Paper-to-Live Gap

### Where paper is faithful

* Atomic state writing pattern is good in principle (tmp + replace) across whale/paper/scorer.
* Modeling of partial fills and failures exists (stress sim), and arb paper uses correct fee formula (`paper_fills.py`)—conceptually good.

### Where paper is misleading (biggest gaps)

1. **Fees:** wrong default, wrong fetching, wrong application in copy paths. ([docs.polymarket.com][1])
2. **Staleness:** you’re not measuring real whale lag; this is the central determinant of copy-trading edge.
3. **Copy execution not anchored to order book:** you don’t look at current best ask/bid/depth when copying; live fills will be depth-limited and drift-sensitive.
4. **Arb negative EV bug:** paper can take trades that are net negative after fees/slip.

### Validation steps required before trusting paper PnL

* Implement **SHADOW mode parity**: same signals, same fee/staleness/book lookup, but no orders; log “expected vs actual book after X seconds”.
* Record for each copy signal:

  * whale timestamp, detected timestamp, execution timestamp
  * best ask/bid at execution
  * price move since whale trade (book midpoint change)
* Only after you can quantify “average lateness” and “average adverse move” should you trust any EV claims.

---

## F) Scaling Assessment

### What changes at $500 / $2k / $5k+

**$500**

* your 1–3% sizing becomes $5–15. Still okay, but thin markets already punish you.

**$2,000**

* per trade $20–60. Liquidity becomes a real constraint: book depth at your price matters, and slippage becomes non-linear.

**$5,000+**

* per trade $50–150.
* Copy trading breaks first due to:

  * clustered whale behavior (correlation),
  * limited depth on long-tail markets,
  * being late amplifies slippage dramatically.
* Arb breaks because:

  * top-of-book arbs are tiny and disappear,
  * two-leg risk grows with size.

### Recommended capital deployment strategy (and when to stop)

* **Stop scaling** when your median trade size exceeds ~1–2% of top-of-book size in your target markets (you’ll become the slippage).
* Scale by:

  1. tightening market universe (only deep books),
  2. switching to maker/limits where possible,
  3. adding correlation caps (per theme/event cluster).

---

## G) Architecture & Ops Grade

### Code quality grade: **C**

* Good intentions and modularity, but too many correctness issues in money paths (fees, staleness, restart-risk).

### Production readiness score: **38 / 100**

* You have the skeleton (state, watchdog, dashboard), but correctness + ops hardening aren’t there yet.

### MacBook 24/7 operational checklist (concrete)

1. **Prevent sleep:** run via `caffeinate -dimsu -w <pid>` or create a LaunchAgent that runs `caffeinate` alongside the bot.
2. **Auto-restart:** use `launchd` (LaunchAgent) with `KeepAlive=true` and `StandardOutPath/StandardErrorPath` set.
3. **Network health gate:** before trading each cycle:

   * check DNS + a fast “OK endpoint” and if failing → pause trading + alert.
4. **Log rotation:** write logs to file and rotate daily; cap size.
5. **Disk guard:** refuse to write snapshots if free disk < X GB; alert.
6. **Time sync drift:** periodically compare local time to a trusted HTTP Date header and alert if drift > 2–3 seconds (expiry logic).
7. **Backup state:** copy `data/*.json` daily with a retention window (7–30 days).
8. **“Doing nothing” alert:** if no trades in 6–12 hours, push Telegram with top skip reasons.

---

## H) Brutal Honesty Take

* **Is this likely to make money, or mostly pay fees/slippage?**
  As written, it’s much more likely to be **self-deceptive paper PnL** and/or **idle**, and in LIVE it risks “death by a thousand cuts” (or one restart-caused overexposure event).

* **If this were my own $5,000, would I run it? Under what strict conditions?**
  Not live. I would only run it if:

  1. fee plumbing + fee math fixed everywhere,
  2. staleness is correctly measured from whale trade timestamp,
  3. copy execution references current book and rejects thin liquidity,
  4. risk exposure/daily loss rebuilt on startup,
  5. shadow-mode logging proves that copied trades aren’t systematically worse than whales by more than your edge can tolerate.

* **Single biggest flaw that could sink it:**
  **You don’t actually know (and the code doesn’t correctly measure) how late you are relative to the whale**, and lateness is the entire ballgame in copy trading. Until that is measured and modeled against real book movement, the system has no reliable claim to edge.

---

If you want, I can also produce a patch-style “must-fix” change list (exact diffs by file/function) keyed to the items above, but the core verdict won’t change until **fees + staleness + restart-risk** are corrected.

[1]: https://docs.polymarket.com/developers/market-makers/maker-rebates-program "Maker Rebates Program - Polymarket Documentation"
[2]: https://docs.polymarket.com/developers/market-makers/trading "Trading - Polymarket Documentation"
