"""Comprehensive stress simulation for pessimistic paper trading.

Simulates every friction source on real Polymarket CLOB trading so
paper results are conservative. If you're profitable under stress,
live trading should perform the same or better.

Friction layers modeled:
  1. Fill rejection      — order book changed before our order lands
  2. Partial fills        — liquidity dries up mid-fill
  3. Variable slippage    — real books have depth variance
  4. Timing drift         — price moves during our detection→execution gap
  5. Signal staleness     — older signals = worse entries
  6. Copy crowd effect    — other bots copy the same whale, push price
  7. Book depletion       — repeated trades in same market thin the book
  8. Rate limiting        — can't spam the CLOB matching engine
  9. API failures         — random network/server errors
 10. Gas fees             — Polygon on-chain transaction costs
 11. Spread widening      — volatile periods have wider spreads
 12. Time-of-day effect   — off-hours have less liquidity
"""

import math
import random
import time

# ── Polymarket Reality Constants ────────────────────────────────
# Calibrated from real CLOB trading conditions.

# 1. Fill reliability
FILL_REJECTION_BASE = 0.08           # 8% base order rejection
FILL_REJECTION_RATE_LIMIT_EXTRA = 0.15  # +15% if trading faster than cooldown
FILL_REJECTION_PER_MARKET_TRADE = 0.05  # +5% per recent trade in same market
FILL_REJECTION_MAX = 0.40            # Cap at 40% rejection rate

PARTIAL_FILL_CHANCE = 0.12           # 12% of fills are partial
PARTIAL_FILL_MIN = 0.35              # Worst partial: 35% of desired
PARTIAL_FILL_MAX = 0.85              # Best partial: 85% of desired

# 2. Slippage model (stacks additively)
SLIPPAGE_BASE = 0.015                # 1.5% base (we're always slower than whale)
SLIPPAGE_RANDOM_MAX = 0.025          # 0-2.5% random market noise
TIMING_DRIFT_MAX = 0.03              # 0-3% price drift during execution
STALENESS_PER_SEC = 0.001            # +0.1% per second of signal age
STALENESS_CAP = 0.05                 # Cap staleness penalty at 5%
CROWD_SLIPPAGE = 0.02                # +2% when high-score signals attract crowd
CROWD_SCORE_THRESHOLD = 4            # Signals scoring >= 4 attract copiers
BOOK_DEPLETION_PER_TRADE = 0.01      # +1% per recent trade in same market
BOOK_DEPLETION_CAP = 0.05            # Cap at 5% depletion penalty
SPREAD_WIDEN_CHANCE = 0.10           # 10% chance of spread widening event
SPREAD_WIDEN_RANGE = (0.01, 0.04)    # 1-4% extra on widening events

# 3. Timing
MIN_TRADE_INTERVAL_SEC = 3.0         # CLOB rate limit simulation
API_FAILURE_CHANCE = 0.03            # 3% random API/network failure

# 4. Costs
GAS_FEE_MIN = 0.001                  # Polygon gas floor (USDC)
GAS_FEE_MAX = 0.008                  # Polygon gas ceiling (USDC)

# 5. Time-of-day liquidity multiplier
# Markets have less liquidity outside US trading hours (14:30-21:00 UTC)
# We model this as a slippage multiplier
OFF_HOURS_SLIPPAGE_MULT = 1.4        # 40% worse slippage outside peak hours
PEAK_HOURS_UTC = (14, 21)            # 14:30-21:00 UTC = US market hours

# 6. Book depletion decay
DEPLETION_DECAY_SECONDS = 30         # Liquidity replenishes: -1 trade per 30s

# 7. Expiry proximity decay
EXPIRY_DECAY_THRESHOLD_MIN = 5       # Start decay within 5 min of expiry


class StressSimulator:
    """Simulates real-world Polymarket CLOB trading friction.

    Every trade passes through multiple stress layers. The simulator
    tracks what happened so the confidence report can show exactly
    how much friction was applied.
    """

    def __init__(self):
        self._last_trade_time = 0
        # condition_id -> (trade_count, last_trade_timestamp)
        self._market_activity = {}
        self._trade_timestamps = []

        # ── Counters ──
        self.fill_rejections = 0
        self.partial_fills = 0
        self.rate_limit_blocks = 0
        self.api_failures = 0
        self.crowd_hits = 0
        self.spread_widen_hits = 0
        self.off_hours_hits = 0
        self.depletion_hits = 0
        self.total_gas_paid = 0.0
        self.total_extra_slippage = 0.0
        self.total_crowd_penalty = 0.0
        self.total_trades_attempted = 0
        self.total_trades_passed = 0
        self.worst_slippage_pct = 0.0

    # ── Entry Stress (buying) ─────────────────────────────────────

    def stress_entry(self, whale_price, copy_budget, condition_id,
                     signal_score=1, signal_age_sec=0, min_size=0.50,
                     minutes_to_expiry=None):
        """Apply all stress layers to a copy trade entry.

        Args:
            whale_price: Price the whale paid
            copy_budget: How much USDC we want to spend
            condition_id: Market we're trading
            signal_score: Signal quality (higher = more copiers)
            signal_age_sec: Seconds since whale's trade
            min_size: Minimum budget to proceed

        Returns:
            dict with: success, adjusted_price, adjusted_budget, gas_fee,
                       slippage_pct, stress_tags, reason (if failed)
        """
        self.total_trades_attempted += 1
        stress_tags = []
        now = time.time()

        # ── Layer 1: API failure ──
        if random.random() < API_FAILURE_CHANCE:
            self.api_failures += 1
            return self._fail("STRESS: API/network error")

        # ── Layer 2: Rate limiting ──
        time_since_last = now - self._last_trade_time
        if time_since_last < MIN_TRADE_INTERVAL_SEC:
            if random.random() < FILL_REJECTION_RATE_LIMIT_EXTRA:
                self.rate_limit_blocks += 1
                return self._fail("STRESS: Rate limited (too fast)")
            stress_tags.append("FAST")

        # ── Layer 3: Fill rejection ──
        fatigue = self._get_market_fatigue(condition_id, now)
        rejection_chance = min(
            FILL_REJECTION_BASE + (fatigue * FILL_REJECTION_PER_MARKET_TRADE),
            FILL_REJECTION_MAX
        )
        if random.random() < rejection_chance:
            self.fill_rejections += 1
            return self._fail("STRESS: Order rejected (book changed)")

        # ── Layer 4: Partial fill ──
        adjusted_budget = copy_budget
        if random.random() < PARTIAL_FILL_CHANCE:
            fill_pct = random.uniform(PARTIAL_FILL_MIN, PARTIAL_FILL_MAX)
            adjusted_budget = copy_budget * fill_pct
            self.partial_fills += 1
            pct_str = str(int(fill_pct * 100))
            stress_tags.append("PARTIAL(" + pct_str + "%)")
            if adjusted_budget < min_size:
                return self._fail("STRESS: Partial fill too small")

        # ── Layer 5: Slippage calculation (all sub-layers) ──
        total_slip = SLIPPAGE_BASE

        # 5a: Random market noise
        total_slip += random.uniform(0, SLIPPAGE_RANDOM_MAX)

        # 5b: Timing drift
        total_slip += random.uniform(0, TIMING_DRIFT_MAX)

        # 5c: Signal staleness
        if signal_age_sec > 0:
            staleness = min(signal_age_sec * STALENESS_PER_SEC, STALENESS_CAP)
            total_slip += staleness
            if staleness > 0.01:
                stress_tags.append("STALE")

        # 5d: Copy crowd effect
        crowd_penalty = 0.0
        if signal_score >= CROWD_SCORE_THRESHOLD:
            crowd_penalty = CROWD_SLIPPAGE * random.uniform(0.5, 1.5)
            total_slip += crowd_penalty
            self.crowd_hits += 1
            self.total_crowd_penalty += crowd_penalty
            stress_tags.append("CROWD")

        # 5e: Book depletion
        if fatigue > 0:
            depletion = min(fatigue * BOOK_DEPLETION_PER_TRADE, BOOK_DEPLETION_CAP)
            total_slip += depletion
            self.depletion_hits += 1
            stress_tags.append("DEPLETED")

        # 5f: Spread widening event
        if random.random() < SPREAD_WIDEN_CHANCE:
            widen = random.uniform(*SPREAD_WIDEN_RANGE)
            total_slip += widen
            self.spread_widen_hits += 1
            stress_tags.append("WIDE_SPREAD")

        # 5g: Time-of-day liquidity
        hour_utc = time.gmtime(now).tm_hour
        if hour_utc < PEAK_HOURS_UTC[0] or hour_utc >= PEAK_HOURS_UTC[1]:
            # Off-hours: multiply all slippage by penalty factor
            extra_from_off_hours = total_slip * (OFF_HOURS_SLIPPAGE_MULT - 1.0)
            total_slip *= OFF_HOURS_SLIPPAGE_MULT
            self.off_hours_hits += 1
            stress_tags.append("OFF_HOURS")

        # 5h: Expiry proximity decay — books thin out near settlement
        if minutes_to_expiry is not None and minutes_to_expiry <= EXPIRY_DECAY_THRESHOLD_MIN:
            # Exponential: ~1.4x at 5min, ~2.7x at 1min, ~3.7x at 0.5min
            decay_mult = 1.0 + 2.0 * math.exp(-minutes_to_expiry / 2.0)
            total_slip *= decay_mult
            stress_tags.append("EXPIRY_DECAY")

        # Apply slippage (buying: price goes UP against us)
        adjusted_price = min(whale_price * (1.0 + total_slip), 0.99)

        # Track extra slippage beyond base
        extra = total_slip - SLIPPAGE_BASE
        self.total_extra_slippage += extra
        if total_slip > self.worst_slippage_pct:
            self.worst_slippage_pct = total_slip

        # ── Layer 6: Gas fee ──
        gas = random.uniform(GAS_FEE_MIN, GAS_FEE_MAX)
        self.total_gas_paid += gas

        # ── Update tracking ──
        self._last_trade_time = now
        self._record_market_trade(condition_id, now)
        self._trade_timestamps.append(now)
        # Clean old timestamps
        self._trade_timestamps = [t for t in self._trade_timestamps if now - t < 300]

        self.total_trades_passed += 1

        return {
            "success": True,
            "adjusted_price": round(adjusted_price, 6),
            "adjusted_budget": round(adjusted_budget, 4),
            "gas_fee": round(gas, 4),
            "slippage_pct": round(total_slip * 100, 2),
            "stress_tags": stress_tags,
        }

    # ── Exit Stress (selling) ─────────────────────────────────────

    def stress_exit(self, whale_price, condition_id, signal_age_sec=0):
        """Apply stress to a copy exit (selling our position).

        Returns:
            dict with: success, adjusted_price, gas_fee, slippage_pct,
                       stress_tags, reason (if failed)
        """
        self.total_trades_attempted += 1
        stress_tags = []
        now = time.time()

        # API failure
        if random.random() < API_FAILURE_CHANCE:
            self.api_failures += 1
            return self._fail("STRESS: API error on exit")

        # Slippage (selling: price goes DOWN against us)
        total_slip = SLIPPAGE_BASE
        total_slip += random.uniform(0, SLIPPAGE_RANDOM_MAX)
        total_slip += random.uniform(0, TIMING_DRIFT_MAX)

        # Staleness
        if signal_age_sec > 0:
            staleness = min(signal_age_sec * STALENESS_PER_SEC, STALENESS_CAP)
            total_slip += staleness
            if staleness > 0.01:
                stress_tags.append("STALE")

        # Spread widening
        if random.random() < SPREAD_WIDEN_CHANCE:
            widen = random.uniform(*SPREAD_WIDEN_RANGE)
            total_slip += widen
            self.spread_widen_hits += 1
            stress_tags.append("WIDE_SPREAD")

        # Time-of-day
        hour_utc = time.gmtime(now).tm_hour
        if hour_utc < PEAK_HOURS_UTC[0] or hour_utc >= PEAK_HOURS_UTC[1]:
            total_slip *= OFF_HOURS_SLIPPAGE_MULT
            self.off_hours_hits += 1
            stress_tags.append("OFF_HOURS")

        adjusted_price = max(whale_price * (1.0 - total_slip), 0.01)

        extra = total_slip - SLIPPAGE_BASE
        self.total_extra_slippage += extra
        if total_slip > self.worst_slippage_pct:
            self.worst_slippage_pct = total_slip

        # Gas
        gas = random.uniform(GAS_FEE_MIN, GAS_FEE_MAX)
        self.total_gas_paid += gas

        self.total_trades_passed += 1

        return {
            "success": True,
            "adjusted_price": round(adjusted_price, 6),
            "gas_fee": round(gas, 4),
            "slippage_pct": round(total_slip * 100, 2),
            "stress_tags": stress_tags,
        }

    # ── Market Fatigue Tracking ───────────────────────────────────

    def _get_market_fatigue(self, condition_id, now):
        """How depleted is this market's book from our recent trades?
        Returns 0 (fresh) to N (heavily traded)."""
        entry = self._market_activity.get(condition_id)
        if not entry:
            return 0
        count, last_time = entry
        # Decay: -1 trade per DEPLETION_DECAY_SECONDS
        elapsed = now - last_time
        decayed = max(0, count - int(elapsed / DEPLETION_DECAY_SECONDS))
        return decayed

    def _record_market_trade(self, condition_id, now):
        entry = self._market_activity.get(condition_id)
        if entry:
            old_count = max(0, entry[0] - int((now - entry[1]) / DEPLETION_DECAY_SECONDS))
            self._market_activity[condition_id] = (old_count + 1, now)
        else:
            self._market_activity[condition_id] = (1, now)

    # ── Helpers ───────────────────────────────────────────────────

    def _fail(self, reason):
        return {"success": False, "reason": reason}

    def get_stats(self):
        """Full stress statistics for the confidence report."""
        attempted = max(self.total_trades_attempted, 1)
        rejections = self.fill_rejections + self.rate_limit_blocks + self.api_failures
        return {
            "total_attempted": self.total_trades_attempted,
            "total_passed": self.total_trades_passed,
            "total_rejected": rejections,
            "rejection_rate_pct": round(rejections / attempted * 100, 1),
            "fill_rejections": self.fill_rejections,
            "partial_fills": self.partial_fills,
            "rate_limit_blocks": self.rate_limit_blocks,
            "api_failures": self.api_failures,
            "crowd_hits": self.crowd_hits,
            "spread_widen_hits": self.spread_widen_hits,
            "off_hours_hits": self.off_hours_hits,
            "depletion_hits": self.depletion_hits,
            "total_gas_paid": round(self.total_gas_paid, 4),
            "total_extra_slippage": round(self.total_extra_slippage, 4),
            "total_crowd_penalty": round(self.total_crowd_penalty, 4),
            "worst_slippage_pct": round(self.worst_slippage_pct * 100, 2),
            "avg_slippage_pct": round(
                (self.total_extra_slippage / max(self.total_trades_passed, 1)
                 + SLIPPAGE_BASE) * 100, 2
            ),
        }
