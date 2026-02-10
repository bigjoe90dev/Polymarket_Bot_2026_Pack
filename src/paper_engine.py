"""Core paper trading engine with thread-safe state and JSON persistence."""

import threading
import json
import time
import os
import re
import math
import uuid
from datetime import datetime, timedelta

from src.paper_fees import calculate_trading_fee, calculate_withdrawal_haircut
from src.paper_fills import simulate_two_leg_fill
from src.records import log_decision
from src.stress_sim import StressSimulator

STATE_FILE = "data/paper_state.json"
SNAPSHOT_INTERVAL = 60  # seconds between PnL snapshots
MAX_TRADE_HISTORY = 1000
MAX_SNAPSHOTS = 10000
DEFAULT_FEE_BPS = 200               # Conservative 2% fee when API returns 0
EXPIRY_BLOCK_MINUTES = 3             # Don't enter trades within 3 min of expiry
MAX_PRICE_DEVIATION = 0.08           # Winner's Curse: max 8% worse entry than whale


class PaperTradingEngine:
    """Thread-safe paper trading engine with persistent state."""

    def __init__(self, config, market_client=None, wallet_scorer=None):
        self._lock = threading.RLock()
        self.config = config
        self.market_client = market_client  # L0 ClobClient for fee lookups
        self.scorer = wallet_scorer
        self.notifier = None  # Set by bot.py after construction
        self.starting_balance = config.get("PAPER_BALANCE", 1000.0)
        self._last_snapshot_time = 0
        self._hedge_blocks = 0  # Counter for anti-hedge blocks
        self.stress = StressSimulator()  # Comprehensive friction simulation
        self._load_or_create_state()

    # ── State Persistence ────────────────────────────────────────

    def _load_or_create_state(self):
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)

        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r") as f:
                    state = json.load(f)
                self.portfolio = state
                print(f"[PAPER] Loaded state: balance=${state['cash_balance']:.2f}, "
                      f"{len(state.get('positions', {}))} positions, "
                      f"{state.get('total_trades', 0)} trades")
                return
            except Exception as e:
                print(f"[!] Error loading paper state, creating fresh: {e}")

        self.portfolio = {
            "version": 3,
            "starting_balance": self.starting_balance,
            "cash_balance": self.starting_balance,
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "total_fees_paid": 0.0,
            "total_realized_pnl": 0.0,
            "total_opportunities_seen": 0,
            "total_scans": 0,
            "created_at": time.time(),
            "last_updated": time.time(),
            "positions": {},
            "trade_history": [],
            "pnl_snapshots": [],
        }
        self._save_state()
        print(f"[PAPER] New portfolio: starting balance=${self.starting_balance:.2f}")

    def _save_state(self):
        self.portfolio["last_updated"] = time.time()
        try:
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.portfolio, f, indent=2)
            os.replace(tmp, STATE_FILE)
        except Exception as e:
            print(f"[!] Error saving paper state: {e}")

    # ── Time Intelligence ──────────────────────────────────────────

    def _minutes_to_expiry(self, market_title):
        """Parse expiry time from market title and return minutes remaining.

        Titles look like: "Bitcoin Up or Down - February 8, 1:30PM-"
        Returns None if can't parse (fail-open: trade proceeds).
        """
        if not market_title:
            return None
        try:
            # Match patterns like "February 8, 1:30PM" or "February 8, 12:00PM"
            m = re.search(
                r'(\w+)\s+(\d{1,2}),?\s+(\d{1,2}):(\d{2})\s*(AM|PM)',
                market_title, re.IGNORECASE
            )
            if not m:
                return None

            month_str, day_str, hour_str, min_str, ampm = m.groups()
            hour = int(hour_str)
            minute = int(min_str)

            # Convert 12-hour to 24-hour
            if ampm.upper() == "PM" and hour != 12:
                hour += 12
            elif ampm.upper() == "AM" and hour == 12:
                hour = 0

            # Parse month name
            now = datetime.now()
            try:
                month_num = datetime.strptime(month_str, "%B").month
            except ValueError:
                return None

            day = int(day_str)
            # Build the expiry datetime (assume current year)
            expiry = now.replace(month=month_num, day=day, hour=hour,
                                 minute=minute, second=0, microsecond=0)

            # If expiry is more than 12 hours in the past, it might be next year
            if (now - expiry).total_seconds() > 43200:
                return None  # Stale market, let other checks handle it

            diff_minutes = (expiry - now).total_seconds() / 60.0
            return diff_minutes
        except Exception:
            return None  # Fail-open: can't parse = trade proceeds

    # ── Core Trading ─────────────────────────────────────────────

    def execute_paper_trade(self, plan, book, market_info):
        """Execute a paper trade using live order book for fill simulation."""
        with self._lock:
            if not book or not plan:
                return {"success": False, "reason": "Missing book or plan"}

            condition_id = plan.get("condition_id", "")
            yes_token = plan.get("yes_token_id", "")
            no_token = plan.get("no_token_id", "")
            size = plan.get("size", 10.0)

            self.portfolio["total_opportunities_seen"] = \
                self.portfolio.get("total_opportunities_seen", 0) + 1

            # Look up fee rates
            yes_fee_bps = self._get_fee_rate(yes_token)
            no_fee_bps = self._get_fee_rate(no_token)

            # Simulate fills against live order book
            result = simulate_two_leg_fill(
                book.get("asks_yes", []),
                book.get("asks_no", []),
                size,
                yes_fee_bps,
                no_fee_bps,
            )

            if not result["both_filled"]:
                log_decision("PAPER_SKIP", f"Insufficient liquidity for {condition_id[:12]}")
                return {"success": False, "reason": "Insufficient liquidity"}

            total_cost = result["total_cost"]

            # Check balance
            if total_cost > self.portfolio["cash_balance"]:
                log_decision("PAPER_SKIP", f"Insufficient balance: need ${total_cost:.2f}")
                return {"success": False, "reason": "Insufficient balance"}

            # Deduct cost
            self.portfolio["cash_balance"] -= total_cost
            self.portfolio["total_fees_paid"] += result["yes_fee"] + result["no_fee"]

            # Record fills
            now = time.time()
            market_name = market_info.get("condition_id", condition_id)[:30]

            yes_fill_entry = {
                "fill_id": str(uuid.uuid4())[:8],
                "timestamp": now,
                "condition_id": condition_id,
                "market_name": market_name,
                "token_id": yes_token,
                "side": "YES",
                "direction": "BUY",
                "price": result["yes_fill"]["fill_price"],
                "size": result["yes_fill"]["fill_size"],
                "fee": result["yes_fee"],
                "slippage": result["yes_fill"]["slippage"],
                "fee_rate_bps": yes_fee_bps,
            }

            no_fill_entry = {
                "fill_id": str(uuid.uuid4())[:8],
                "timestamp": now,
                "condition_id": condition_id,
                "market_name": market_name,
                "token_id": no_token,
                "side": "NO",
                "direction": "BUY",
                "price": result["no_fill"]["fill_price"],
                "size": result["no_fill"]["fill_size"],
                "fee": result["no_fee"],
                "slippage": result["no_fill"]["slippage"],
                "fee_rate_bps": no_fee_bps,
            }

            self.portfolio["trade_history"].append(yes_fill_entry)
            self.portfolio["trade_history"].append(no_fill_entry)

            # Trim history
            if len(self.portfolio["trade_history"]) > MAX_TRADE_HISTORY:
                self.portfolio["trade_history"] = self.portfolio["trade_history"][-MAX_TRADE_HISTORY:]

            self.portfolio["total_trades"] += 1

            # Create or update position
            pos = self.portfolio["positions"].get(condition_id)
            if pos:
                # Average in
                old_yes_cost = pos["yes_avg_price"] * pos["yes_size"]
                old_no_cost = pos["no_avg_price"] * pos["no_size"]
                pos["yes_size"] += result["yes_fill"]["fill_size"]
                pos["no_size"] += result["no_fill"]["fill_size"]
                new_yes_cost = old_yes_cost + result["yes_fill"]["fill_price"] * result["yes_fill"]["fill_size"]
                new_no_cost = old_no_cost + result["no_fill"]["fill_price"] * result["no_fill"]["fill_size"]
                pos["yes_avg_price"] = new_yes_cost / pos["yes_size"] if pos["yes_size"] > 0 else 0
                pos["no_avg_price"] = new_no_cost / pos["no_size"] if pos["no_size"] > 0 else 0
                pos["total_cost"] += total_cost
                pos["total_fees"] += result["yes_fee"] + result["no_fee"]
            else:
                self.portfolio["positions"][condition_id] = {
                    "position_id": str(uuid.uuid4())[:8],
                    "condition_id": condition_id,
                    "market_name": market_name,
                    "yes_token_id": yes_token,
                    "no_token_id": no_token,
                    "yes_size": result["yes_fill"]["fill_size"],
                    "no_size": result["no_fill"]["fill_size"],
                    "yes_avg_price": result["yes_fill"]["fill_price"],
                    "no_avg_price": result["no_fill"]["fill_price"],
                    "total_cost": total_cost,
                    "total_fees": result["yes_fee"] + result["no_fee"],
                    "opened_at": now,
                    "status": "OPEN",
                    "settlement_value": 0.0,
                }

            self._save_state()

            profit_str = f"${result['expected_profit']:.4f}"
            log_decision("PAPER_FILL",
                         f"Bought YES@{result['yes_fill']['fill_price']:.3f} + "
                         f"NO@{result['no_fill']['fill_price']:.3f} x{result['matched_size']} "
                         f"profit={profit_str} fees=${result['yes_fee']+result['no_fee']:.4f}")

            return {
                "success": True,
                "total_cost": total_cost,
                "expected_profit": result["expected_profit"],
                "yes_price": result["yes_fill"]["fill_price"],
                "no_price": result["no_fill"]["fill_price"],
                "size": result["matched_size"],
                "fees": result["yes_fee"] + result["no_fee"],
            }

    def execute_copy_trade(self, signal, current_exposure=0.0):
        """Execute a single-side copy trade (directional, not arb).

        Buys one side (YES or NO) based on what the whale bought.
        Profit/loss depends on market outcome at settlement.
        Signal score controls copy size (higher score = bigger bet).
        """
        with self._lock:
            condition_id = signal.get("condition_id", "")
            token_id = signal.get("token_id", "")
            outcome = signal.get("outcome", "YES").upper()
            whale_price = signal.get("whale_price", 0)
            market_title = signal.get("market_title", "")
            score = signal.get("score", 1)

            if whale_price <= 0 or whale_price >= 1.0:
                return {"success": False, "reason": "Invalid price"}

            # ── Anti-hedge: don't bet opposite side of same market ──
            if self.scorer:
                safe = self.scorer.check_anti_hedge(
                    condition_id, outcome, self.portfolio["positions"]
                )
                if not safe:
                    self._hedge_blocks += 1
                    return {"success": False, "reason": "Anti-hedge: opposite side open"}

            # ── Wallet performance multiplier (category-aware) ──
            # Uses category-specific score when available (e.g. sports vs crypto)
            wallet_mult = 1.0
            source_wallet = signal.get("source_wallet", "")
            market_type = None
            if self.scorer:
                market_type = self.scorer.classify_market(market_title, condition_id)
            if self.scorer and source_wallet:
                wallet_mult = self.scorer.get_wallet_multiplier(
                    source_wallet, market_type=market_type
                )
                if wallet_mult <= 0.0:
                    return {"success": False, "reason": "Wallet cut off (proven loser)"}

            # ── Dynamic percentage-based risk ──
            # All limits scale with current balance. As account grows,
            # risk percentages increase progressively (compound growth).
            balance = self.portfolio["cash_balance"]
            growth = balance / max(self.starting_balance, 1)

            # Progressive growth multiplier: risk more as account grows
            if growth >= 3.0:
                growth_mult = 2.0     # 3x+ growth: double risk %
            elif growth >= 2.0:
                growth_mult = 1.5     # 2x growth: 50% more risk %
            elif growth >= 1.5:
                growth_mult = 1.25    # 50% growth: 25% more risk %
            else:
                growth_mult = 1.0     # Still building

            # Calculate dynamic limits from balance percentages
            exposure_pct = self.config.get("RISK_MAX_EXPOSURE_PCT", 0.50)
            market_pct = self.config.get("RISK_MAX_MARKET_PCT", 0.06)
            min_trade_pct = self.config.get("RISK_PER_TRADE_PCT", 0.01)
            max_trade_pct = self.config.get("RISK_MAX_TRADE_PCT", 0.03)

            max_exposure = balance * exposure_pct * growth_mult
            max_per_market = balance * market_pct * growth_mult
            copy_min = max(0.25, balance * min_trade_pct * growth_mult)
            copy_max = max(0.50, balance * max_trade_pct * growth_mult)

            # ── Exposure guard ──
            if current_exposure >= max_exposure:
                return {"success": False, "reason": f"Max exposure ${max_exposure:.2f} reached"}

            # ── Position concentration limit ──
            existing_cost = 0.0
            for pk, pos in self.portfolio["positions"].items():
                if pos["status"] == "OPEN" and pos.get("condition_id") == condition_id:
                    existing_cost += pos.get("total_cost", 0)
            if existing_cost >= max_per_market:
                return {"success": False, "reason": "Market concentration limit"}

            # ── Book stability / liquidity check ──
            if self.market_client and token_id:
                book_health = self.market_client.check_book_health(token_id)
                if not book_health.get("healthy", True):
                    return {"success": False,
                            "reason": f"Book: {book_health.get('reason', 'unhealthy')}"}

            # ── Position sizing: Kelly Criterion or score-based fallback ──
            whale_usdc = signal.get("usdc_value", 0)
            use_kelly = False
            kelly_fraction = 0

            # Try Kelly if we have enough wallet performance data
            if self.scorer and source_wallet and whale_price > 0:
                stats = self.scorer.wallet_stats.get(source_wallet, {})
                settled = stats.get("wins", 0) + stats.get("losses", 0)
                if settled >= 5:
                    p = stats["wins"] / settled  # Empirical win rate
                    b = (1.0 / whale_price) - 1.0  # Odds: profit per dollar risked
                    q = 1.0 - p
                    if b > 0:
                        kelly = (p * b - q) / b
                        half_kelly = max(0, kelly / 2.0)  # Half-Kelly: conservative
                        if half_kelly > 0:
                            use_kelly = True
                            kelly_fraction = half_kelly

            if use_kelly:
                # Kelly fraction of current balance, clamped to min/max
                kelly_budget = balance * kelly_fraction * wallet_mult
                copy_budget = max(copy_min, min(kelly_budget, copy_max))
            else:
                # Fallback: score-based proportional sizing
                copy_ratio = self.config.get("COPY_RATIO", 0.01)
                score_multiplier = 1.0 + min(score - 1, 4) * 0.25  # 1.0x to 2.0x
                if whale_usdc > 0:
                    copy_budget = whale_usdc * copy_ratio * score_multiplier * wallet_mult
                    copy_budget = max(copy_min, min(copy_budget, copy_max))
                else:
                    copy_budget = copy_min

            # Don't exceed remaining exposure room
            room = max_exposure - current_exposure
            if copy_budget > room:
                copy_budget = room
            if copy_budget < copy_min:
                return {"success": False, "reason": "Not enough exposure room"}

            # Don't exceed market concentration limit
            market_room = max_per_market - existing_cost
            if copy_budget > market_room:
                copy_budget = market_room
            if copy_budget < copy_min:
                return {"success": False, "reason": "Market limit reached"}

            # ── Time Intelligence: expiry block ──
            minutes_left = self._minutes_to_expiry(market_title)
            if minutes_left is not None:
                if minutes_left <= 0:
                    return {"success": False, "reason": "Market past expiry"}
                expiry_block = self.config.get("EXPIRY_BLOCK_MINUTES", EXPIRY_BLOCK_MINUTES)
                if minutes_left <= expiry_block:
                    return {"success": False,
                            "reason": f"Too close to expiry ({minutes_left:.0f}m left)"}

            # ── STRESS: Full Polymarket friction simulation ──
            # Covers: fill rejection, partial fills, variable slippage,
            # timing drift, signal staleness, copy crowd, book depletion,
            # rate limiting, API failures, spread widening, off-hours penalty,
            # exponential decay near expiry
            # CRITICAL: Use whale's actual trade timestamp, not our detection time
            whale_trade_time = signal.get("timestamp", signal.get("detected_at", time.time()))
            signal_age = time.time() - whale_trade_time
            stress_result = self.stress.stress_entry(
                whale_price=whale_price,
                copy_budget=copy_budget,
                condition_id=condition_id,
                signal_score=score,
                signal_age_sec=signal_age,
                min_size=copy_min,
                minutes_to_expiry=minutes_left,
            )

            if not stress_result["success"]:
                return {"success": False, "reason": stress_result["reason"]}

            our_price = stress_result["adjusted_price"]
            copy_budget = stress_result["adjusted_budget"]
            gas_fee = stress_result["gas_fee"]
            stress_tags = stress_result["stress_tags"]

            # ── Gas Signals: Adjust size based on whale's conviction ──
            # High gas = whale paying premium for fast execution = high conviction
            gas_price_gwei = signal.get("gas_price_gwei", 0)
            gas_multiplier = 1.0
            if gas_price_gwei > 200:  # High conviction
                gas_multiplier = 1.5
                stress_tags.append("HIGH_GAS_CONVICTION")
            elif gas_price_gwei > 0 and gas_price_gwei < 50:  # Low conviction
                gas_multiplier = 0.75
                stress_tags.append("LOW_GAS_CONVICTION")

            copy_budget *= gas_multiplier

            # ── Winner's Curse Protection ──
            # If our entry is too much worse than the whale's, skip
            max_dev = self.config.get("WINNER_CURSE_MAX_PCT", MAX_PRICE_DEVIATION)
            if whale_price > 0:
                deviation = (our_price - whale_price) / whale_price
                if deviation > max_dev:
                    return {"success": False,
                            "reason": f"Winner's curse: {deviation:.1%} deviation "
                                      f"(cap {max_dev:.0%})"}

            shares = copy_budget / our_price

            # Fee lookup (use curved Polymarket formula)
            fee_bps = self._get_fee_rate(token_id)
            fee = calculate_trading_fee(our_price, shares, fee_bps)
            total_cost = copy_budget + fee + gas_fee

            # Balance check
            if total_cost > self.portfolio["cash_balance"]:
                return {"success": False, "reason": "Insufficient balance"}

            # Deduct
            self.portfolio["cash_balance"] -= total_cost
            self.portfolio["total_fees_paid"] += fee + gas_fee
            self.portfolio["total_trades"] += 1

            # Record fill
            now = time.time()
            fill_entry = {
                "fill_id": str(uuid.uuid4())[:8],
                "timestamp": now,
                "condition_id": condition_id,
                "market_name": market_title,
                "token_id": token_id,
                "side": outcome,
                "direction": "BUY",
                "price": round(our_price, 4),
                "size": round(shares, 2),
                "fee": round(fee, 4),
                "fee_rate_bps": fee_bps,
                "slippage": round(our_price - whale_price, 4),
                "trade_type": "COPY",
                "source_username": signal.get("source_username", ""),
                "score": score,
            }

            self.portfolio["trade_history"].append(fill_entry)
            if len(self.portfolio["trade_history"]) > MAX_TRADE_HISTORY:
                self.portfolio["trade_history"] = self.portfolio["trade_history"][-MAX_TRADE_HISTORY:]

            # Position key: separate from arb positions
            pos_key = f"copy_{condition_id}_{outcome}"
            pos = self.portfolio["positions"].get(pos_key)

            if pos and pos.get("status") == "OPEN":
                # Stack onto existing OPEN position (average in)
                old_cost = pos.get("avg_price", 0) * pos.get("size", 0)
                pos["size"] = pos.get("size", 0) + shares
                new_cost = old_cost + our_price * shares
                pos["avg_price"] = round(new_cost / pos["size"], 4) if pos["size"] > 0 else 0
                pos["total_cost"] += total_cost
                pos["total_fees"] += fee
            else:
                self.portfolio["positions"][pos_key] = {
                    "position_id": str(uuid.uuid4())[:8],
                    "condition_id": condition_id,
                    "market_name": market_title,
                    "token_id": token_id,
                    "outcome": outcome,
                    "size": round(shares, 2),
                    "avg_price": round(our_price, 4),
                    "total_cost": round(total_cost, 4),
                    "total_fees": round(fee, 4),
                    "opened_at": now,
                    "status": "OPEN",
                    "settlement_value": 0.0,
                    "trade_type": "COPY",
                    "source_username": signal.get("source_username", ""),
                    "source_wallet": source_wallet,
                }

            self._save_state()

            # Record entry in wallet scorer for performance tracking
            if self.scorer and source_wallet:
                self.scorer.record_entry(source_wallet, condition_id,
                                         total_cost, market_title, score)

            slip_pct = stress_result["slippage_pct"]
            tags_str = " ".join(stress_tags)
            if tags_str:
                tags_str = " [" + tags_str + "]"
            log_decision("COPY_FILL",
                         f"Copied {signal.get('source_username', '?')}: "
                         f"BUY {outcome}@{our_price:.3f} x{shares:.1f} "
                         f"${total_cost:.2f} score={score} wm={wallet_mult:.1f} "
                         f"slip={slip_pct:.1f}% gas=${gas_fee:.3f}"
                         f"{tags_str} on \"{market_title}\"")

            return {
                "success": True,
                "total_cost": round(total_cost, 4),
                "price": round(our_price, 4),
                "size": round(shares, 2),
                "fees": round(fee, 4),
                "whale_price": whale_price,
                "slippage_pct": round((our_price - whale_price) / whale_price * 100, 2),
                "score": score,
                "score_multiplier": round(score_multiplier, 2),
            }

    def close_copy_position(self, signal, risk_guard=None):
        """Close a copy position when the whale exits.

        Simulates selling our shares at the whale's sell price minus slippage.
        Credits proceeds to cash balance and records realized PnL.
        """
        with self._lock:
            condition_id = signal.get("condition_id", "")
            outcome = signal.get("outcome", "").upper()
            whale_price = signal.get("whale_price", 0)
            market_title = signal.get("market_title", "")

            # Find matching open copy position
            pos_key = f"copy_{condition_id}_{outcome}"
            pos = self.portfolio["positions"].get(pos_key)

            if not pos or pos["status"] != "OPEN":
                return {"success": False, "reason": "No open position to close"}

            if pos.get("trade_type") != "COPY":
                return {"success": False, "reason": "Not a copy position"}

            # ── STRESS: Full friction on sell side ──
            signal_age = time.time() - signal.get("detected_at", time.time())
            exit_stress = self.stress.stress_exit(
                whale_price=whale_price,
                condition_id=condition_id,
                signal_age_sec=signal_age,
            )

            if not exit_stress["success"]:
                return {"success": False, "reason": exit_stress["reason"]}

            our_sell_price = exit_stress["adjusted_price"]
            gas_fee = exit_stress["gas_fee"]
            shares = pos["size"]
            gross_proceeds = shares * our_sell_price

            # Fee on the sell side (use curved Polymarket formula)
            fee_bps = self._get_fee_rate(pos.get("token_id", ""))
            fee = calculate_trading_fee(our_sell_price, shares, fee_bps)
            net_proceeds = gross_proceeds - fee - gas_fee

            # Realized PnL
            realized_pnl = net_proceeds - pos["total_cost"]

            # Credit cash
            self.portfolio["cash_balance"] += net_proceeds
            self.portfolio["total_fees_paid"] += fee + gas_fee
            self.portfolio["total_realized_pnl"] += realized_pnl

            if realized_pnl >= 0:
                self.portfolio["winning_trades"] += 1
            else:
                self.portfolio["losing_trades"] += 1
                if risk_guard:
                    risk_guard.record_loss(abs(realized_pnl))

            if risk_guard:
                risk_guard.remove_exposure(pos["total_cost"])

            # Mark position closed
            close_time = time.time()
            pos["status"] = "CLOSED_EXIT"
            pos["closed_at"] = close_time
            pos["sell_price"] = round(our_sell_price, 4)
            pos["realized_pnl"] = round(realized_pnl, 4)
            pos["settlement_value"] = round(net_proceeds, 4)

            # Record result in wallet scorer
            if self.scorer and pos.get("source_wallet"):
                hold_time = close_time - pos.get("opened_at", close_time)
                self.scorer.record_result(
                    pos["source_wallet"], condition_id,
                    realized_pnl, hold_time, market_title
                )

            # Record the sell in trade history
            now = time.time()
            fill_entry = {
                "fill_id": str(uuid.uuid4())[:8],
                "timestamp": now,
                "condition_id": condition_id,
                "market_name": market_title,
                "token_id": pos.get("token_id", ""),
                "side": outcome,
                "direction": "SELL",
                "price": round(our_sell_price, 4),
                "size": round(shares, 2),
                "fee": round(fee, 4),
                "fee_rate_bps": fee_bps,
                "slippage": round(whale_price - our_sell_price, 4),
                "trade_type": "COPY_EXIT",
                "source_username": signal.get("source_username", ""),
                "realized_pnl": round(realized_pnl, 4),
            }
            self.portfolio["trade_history"].append(fill_entry)
            if len(self.portfolio["trade_history"]) > MAX_TRADE_HISTORY:
                self.portfolio["trade_history"] = self.portfolio["trade_history"][-MAX_TRADE_HISTORY:]

            self._save_state()

            pnl_label = f"+${realized_pnl:.4f}" if realized_pnl >= 0 else f"-${abs(realized_pnl):.4f}"
            log_decision("COPY_EXIT",
                         f"Exited with {signal.get('source_username', '?')}: "
                         f"SELL {outcome}@{our_sell_price:.3f} x{shares:.1f} "
                         f"PnL={pnl_label} on \"{market_title}\"")

            return {
                "success": True,
                "net_proceeds": round(net_proceeds, 4),
                "realized_pnl": round(realized_pnl, 4),
                "sell_price": round(our_sell_price, 4),
                "shares_sold": round(shares, 2),
                "fees": round(fee, 4),
            }

    def _get_fee_rate(self, token_id):
        """Get fee rate in bps for a token. Returns conservative default on failure."""
        if not self.market_client or not token_id:
            return DEFAULT_FEE_BPS  # Conservative fallback
        try:
            rate = self.market_client.get_fee_rate_bps(token_id)
            if rate <= 0:
                return DEFAULT_FEE_BPS  # API returned 0 — use conservative default
            return rate
        except Exception as e:
            log_decision("FEE_WARN", f"Fee lookup failed for {token_id[:12]}: {e}")
            return DEFAULT_FEE_BPS  # Conservative fallback

    # ── Auto Sell (Take-Profit / Stop-Loss) ─────────────────────

    def _auto_sell(self, pos, pos_key, current_price, reason, risk_guard=None):
        """Sell a copy position automatically (take-profit or stop-loss).

        Called during settlement checks when price target is hit.
        Uses stress simulation for realistic sell execution.
        """
        condition_id = pos.get("condition_id", "")
        outcome = pos.get("outcome", "")
        market_title = pos.get("market_name", "")

        # Stress on the sell
        exit_stress = self.stress.stress_exit(
            whale_price=current_price,
            condition_id=condition_id,
            signal_age_sec=0,
        )
        if not exit_stress["success"]:
            return  # Stress blocked the sell — try again next cycle

        our_sell_price = exit_stress["adjusted_price"]
        gas_fee = exit_stress["gas_fee"]
        shares = pos["size"]
        gross_proceeds = shares * our_sell_price

        # Use curved Polymarket fee formula
        fee_bps = self._get_fee_rate(pos.get("token_id", ""))
        fee = calculate_trading_fee(our_sell_price, shares, fee_bps)
        net_proceeds = gross_proceeds - fee - gas_fee

        realized_pnl = net_proceeds - pos["total_cost"]

        # Credit cash
        self.portfolio["cash_balance"] += net_proceeds
        self.portfolio["total_fees_paid"] += fee + gas_fee
        self.portfolio["total_realized_pnl"] += realized_pnl

        if realized_pnl >= 0:
            self.portfolio["winning_trades"] += 1
        else:
            self.portfolio["losing_trades"] += 1
            if risk_guard:
                risk_guard.record_loss(abs(realized_pnl))

        if risk_guard:
            risk_guard.remove_exposure(pos["total_cost"])

        # Mark position
        close_time = time.time()
        pos["status"] = f"CLOSED_{reason}"
        pos["closed_at"] = close_time
        pos["sell_price"] = round(our_sell_price, 4)
        pos["realized_pnl"] = round(realized_pnl, 4)
        pos["settlement_value"] = round(net_proceeds, 4)

        # Scorer tracking
        if self.scorer and pos.get("source_wallet"):
            hold_time = close_time - pos.get("opened_at", close_time)
            self.scorer.record_result(
                pos["source_wallet"], condition_id,
                realized_pnl, hold_time, market_title
            )

        # Trade history
        fill_entry = {
            "fill_id": str(uuid.uuid4())[:8],
            "timestamp": close_time,
            "condition_id": condition_id,
            "market_name": market_title,
            "token_id": pos.get("token_id", ""),
            "side": outcome,
            "direction": "SELL",
            "price": round(our_sell_price, 4),
            "size": round(shares, 2),
            "fee": round(fee, 4),
            "fee_rate_bps": fee_bps,
            "trade_type": reason,
            "realized_pnl": round(realized_pnl, 4),
        }
        self.portfolio["trade_history"].append(fill_entry)

        # Telegram notification for TP/SL
        if hasattr(self, 'notifier') and self.notifier:
            self.notifier.notify_trade_closed(pos, reason, realized_pnl)

        if len(self.portfolio["trade_history"]) > MAX_TRADE_HISTORY:
            self.portfolio["trade_history"] = self.portfolio["trade_history"][-MAX_TRADE_HISTORY:]

        pnl_sign = "+" if realized_pnl >= 0 else ""
        pnl_pct = round((realized_pnl / pos["total_cost"]) * 100, 1) if pos["total_cost"] > 0 else 0
        log_decision(reason,
                     f"{outcome}@{our_sell_price:.3f} x{shares:.1f} "
                     f"PnL={pnl_sign}${realized_pnl:.2f} ({pnl_sign}{pnl_pct}%) "
                     f"on \"{market_title}\"")

    # ── Settlement ───────────────────────────────────────────────

    def check_and_settle_positions(self, market_service, risk_guard=None):
        """Check if any open positions' markets have resolved.
        Updates risk_guard exposure on settlement."""
        with self._lock:
            if not market_service:
                return

            for pos_key, pos in list(self.portfolio["positions"].items()):
                if pos["status"] != "OPEN":
                    continue

                condition_id = pos.get("condition_id", pos_key)
                is_copy = pos.get("trade_type") == "COPY"

                try:
                    market = market_service.client.get_market(condition_id)
                except Exception:
                    continue

                if not market:
                    continue

                # Check for resolution
                tokens = market.get("tokens", [])
                winner = None
                for t in tokens:
                    if t.get("winner", False):
                        outcome = t.get("outcome", "").upper()
                        winner = outcome
                        break

                if not winner:
                    # ── Take-profit / Stop-loss for unsettled copy positions ──
                    if is_copy:
                        our_outcome = pos.get("outcome", "").upper()
                        current_price = None
                        for t in tokens:
                            if t.get("outcome", "").upper() == our_outcome:
                                current_price = float(t.get("price", 0))
                                break

                        if current_price and current_price > 0:
                            pos["current_price"] = current_price
                            pos["price_updated_at"] = time.time()

                        if current_price and current_price > 0 and pos.get("total_cost", 0) > 0:
                            sell_value = current_price * pos["size"]
                            pnl_pct = (sell_value - pos["total_cost"]) / pos["total_cost"]

                            # Dynamic TP/SL based on market type
                            # Fast markets: tighter bands (resolve quickly)
                            # Other markets: let winners run, cut losers early
                            market_name = pos.get("market_name", "")
                            cid = pos.get("condition_id", "")
                            is_fast = (self.scorer.is_fast_market(market_name, cid)
                                       if self.scorer else False)

                            if is_fast:
                                tp_pct = self.config.get("TP_FAST_PCT", 0.20)
                                sl_pct = self.config.get("SL_FAST_PCT", 0.12)
                            else:
                                tp_pct = self.config.get("TP_SLOW_PCT", 0.30)
                                sl_pct = self.config.get("SL_SLOW_PCT", 0.15)

                            if pnl_pct >= tp_pct:
                                self._auto_sell(pos, pos_key, current_price,
                                                "TAKE_PROFIT", risk_guard)
                            elif pnl_pct <= -sl_pct:
                                self._auto_sell(pos, pos_key, current_price,
                                                "STOP_LOSS", risk_guard)
                    continue

                # Settle based on position type
                if is_copy:
                    # Copy trade: single side — win if our outcome matches
                    our_outcome = pos.get("outcome", "").upper()
                    if winner == our_outcome:
                        payout = pos["size"] * 1.0
                    else:
                        payout = 0.0
                    pos["status"] = f"SETTLED_{winner}"
                else:
                    # Arb trade: both sides — winner side pays $1/share
                    if winner == "YES":
                        payout = pos["yes_size"] * 1.0
                    elif winner == "NO":
                        payout = pos["no_size"] * 1.0
                    else:
                        continue
                    pos["status"] = f"SETTLED_{winner}"

                pos["settlement_value"] = payout
                realized_pnl = payout - pos["total_cost"]
                self.portfolio["cash_balance"] += payout
                self.portfolio["total_realized_pnl"] += realized_pnl

                if realized_pnl >= 0:
                    self.portfolio["winning_trades"] += 1
                else:
                    self.portfolio["losing_trades"] += 1
                    if risk_guard:
                        risk_guard.record_loss(abs(realized_pnl))

                if risk_guard:
                    risk_guard.remove_exposure(pos["total_cost"])

                # Record result in wallet scorer for copy trades
                if is_copy and self.scorer and pos.get("source_wallet"):
                    settle_time = time.time()
                    hold_time = settle_time - pos.get("opened_at", settle_time)
                    self.scorer.record_result(
                        pos["source_wallet"], condition_id,
                        realized_pnl, hold_time,
                        pos.get("market_name", "")
                    )

                trade_type = "COPY" if is_copy else "ARB"
                log_decision("PAPER_SETTLED",
                             f"[{trade_type}] {condition_id[:12]} resolved {winner}. "
                             f"Payout=${payout:.2f} PnL=${realized_pnl:.4f}")

                # Telegram notification for settlement
                if hasattr(self, 'notifier') and self.notifier:
                    self.notifier.notify_settlement(pos, realized_pnl, winner)

            self._save_state()

    # ── PnL Snapshots ────────────────────────────────────────────

    def record_pnl_snapshot(self, current_prices=None):
        """Record portfolio value snapshot (max once per minute)."""
        now = time.time()
        if now - self._last_snapshot_time < SNAPSHOT_INTERVAL:
            return

        with self._lock:
            self._last_snapshot_time = now

            unrealized = self._calculate_unrealized_pnl(current_prices)
            total_value = self.portfolio["cash_balance"] + unrealized

            snapshot = {
                "timestamp": now,
                "cash_balance": round(self.portfolio["cash_balance"], 2),
                "unrealized_pnl": round(unrealized, 2),
                "total_value": round(total_value, 2),
                "positions_count": len([p for p in self.portfolio["positions"].values()
                                        if p["status"] == "OPEN"]),
            }

            self.portfolio["pnl_snapshots"].append(snapshot)

            if len(self.portfolio["pnl_snapshots"]) > MAX_SNAPSHOTS:
                self.portfolio["pnl_snapshots"] = self.portfolio["pnl_snapshots"][-MAX_SNAPSHOTS:]

            self._save_state()

    def _calculate_unrealized_pnl(self, current_prices=None):
        """Estimate unrealized PnL from open positions."""
        unrealized = 0.0
        for cid, pos in self.portfolio["positions"].items():
            if pos["status"] != "OPEN":
                continue

            if pos.get("trade_type") == "COPY":
                # Copy trade: best case = size * $1 - cost (if our side wins)
                # Use 50% expected value as conservative estimate
                unrealized += (pos["size"] * 0.5) - pos["total_cost"]
            else:
                # Arb: deterministic — both sides settle to $1
                settlement = min(pos["yes_size"], pos["no_size"]) * 1.0
                unrealized += settlement - pos["total_cost"]
        return unrealized

    # ── Portfolio Queries (thread-safe, called by web UI) ────────

    def get_portfolio_summary(self):
        with self._lock:
            p = self.portfolio
            unrealized = self._calculate_unrealized_pnl()
            total_value = p["cash_balance"] + unrealized
            net_profit = total_value - p["starting_balance"]
            withdrawal_fee = calculate_withdrawal_haircut(net_profit)

            # ── Confidence Report ──
            # Pessimistic: all open copy positions lose (pay $0)
            # Optimistic: all open copy positions win (pay $1/share)
            # Realistic: use current 50% estimate (already in unrealized)
            pessimistic_unrealized = 0.0
            optimistic_unrealized = 0.0
            open_copy_count = 0
            open_arb_count = 0

            for cid, pos in p["positions"].items():
                if pos["status"] != "OPEN":
                    continue
                if pos.get("trade_type") == "COPY":
                    open_copy_count += 1
                    # Pessimistic: we lose everything
                    pessimistic_unrealized += 0.0 - pos["total_cost"]
                    # Optimistic: our side wins, payout = shares * $1
                    optimistic_unrealized += pos["size"] * 1.0 - pos["total_cost"]
                else:
                    open_arb_count += 1
                    # Arb is deterministic
                    arb_val = min(pos["yes_size"], pos["no_size"]) * 1.0 - pos["total_cost"]
                    pessimistic_unrealized += arb_val
                    optimistic_unrealized += arb_val

            pessimistic_total = p["cash_balance"] + pessimistic_unrealized
            optimistic_total = p["cash_balance"] + optimistic_unrealized

            return {
                "starting_balance": p["starting_balance"],
                "cash_balance": round(p["cash_balance"], 2),
                "unrealized_pnl": round(unrealized, 2),
                "realized_pnl": round(p["total_realized_pnl"], 2),
                "total_value": round(total_value, 2),
                "net_profit": round(net_profit, 2),
                "withdrawal_haircut": round(withdrawal_fee, 2),
                "net_after_haircut": round(net_profit - withdrawal_fee, 2),
                "total_trades": p["total_trades"],
                "winning_trades": p["winning_trades"],
                "losing_trades": p["losing_trades"],
                "win_rate": round(p["winning_trades"] / max(p["winning_trades"] + p["losing_trades"], 1) * 100, 1),
                "total_fees_paid": round(p["total_fees_paid"], 4),
                "total_opportunities_seen": p.get("total_opportunities_seen", 0),
                "open_positions": len([pos for pos in p["positions"].values()
                                       if pos["status"] == "OPEN"]),
                "uptime_since": p["created_at"],
                # Confidence report
                "confidence": {
                    "pessimistic_value": round(pessimistic_total, 2),
                    "realistic_value": round(total_value, 2),
                    "optimistic_value": round(optimistic_total, 2),
                    "pessimistic_pnl": round(pessimistic_total - p["starting_balance"], 2),
                    "realistic_pnl": round(net_profit, 2),
                    "optimistic_pnl": round(optimistic_total - p["starting_balance"], 2),
                    "open_copy_positions": open_copy_count,
                    "open_arb_positions": open_arb_count,
                    "hedge_blocks": self._hedge_blocks,
                    "stress": self.stress.get_stats(),
                },
            }

    def get_positions(self):
        with self._lock:
            positions = []
            for pos_key, pos in self.portfolio["positions"].items():
                entry = dict(pos)
                is_copy = pos.get("trade_type") == "COPY"

                if pos["status"] == "OPEN":
                    if is_copy:
                        cur_p = pos.get("current_price")
                        if cur_p and cur_p > 0:
                            sell_val = cur_p * pos["size"]
                            entry["unrealized_pnl"] = round(sell_val - pos["total_cost"], 4)
                            entry["current_price"] = round(cur_p, 4)
                            entry["unrealized_label"] = "live"
                        else:
                            entry["unrealized_pnl"] = round(pos["size"] * 1.0 - pos["total_cost"], 4)
                            entry["unrealized_label"] = "if_win"
                    else:
                        # Arb trade: deterministic (both sides settle to $1)
                        settlement = min(pos["yes_size"], pos["no_size"]) * 1.0
                        entry["unrealized_pnl"] = round(settlement - pos["total_cost"], 4)
                        entry["unrealized_label"] = "locked"
                else:
                    entry["unrealized_pnl"] = 0.0
                    entry["unrealized_label"] = "settled"

                positions.append(entry)
            positions.sort(key=lambda x: (0 if x["status"] == "OPEN" else 1, -x["opened_at"]))
            return positions

    def get_trade_history(self, limit=50):
        with self._lock:
            trades = list(self.portfolio["trade_history"])
            trades.reverse()  # newest first
            return trades[:limit]

    def get_pnl_chart_data(self):
        with self._lock:
            return list(self.portfolio["pnl_snapshots"])

    def get_metrics(self):
        with self._lock:
            p = self.portfolio
            total = p["total_trades"]
            snapshots = p["pnl_snapshots"]

            # Basic metrics
            avg_profit = p["total_realized_pnl"] / max(total, 1)

            # Max drawdown from snapshots
            max_dd = 0.0
            peak = 0.0
            for s in snapshots:
                val = s["total_value"]
                if val > peak:
                    peak = val
                dd = (peak - val) / peak if peak > 0 else 0
                if dd > max_dd:
                    max_dd = dd

            # Best/worst trades (by individual fill)
            fills = p["trade_history"]
            best_fill = max(fills, key=lambda f: f.get("size", 0) * (1.0 - f.get("price", 1.0)), default=None)
            worst_fill = min(fills, key=lambda f: f.get("size", 0) * (1.0 - f.get("price", 1.0)), default=None)

            return {
                "total_trades": total,
                "win_rate": round(p["winning_trades"] / max(p["winning_trades"] + p["losing_trades"], 1) * 100, 1),
                "avg_profit_per_trade": round(avg_profit, 4),
                "total_fees_paid": round(p["total_fees_paid"], 4),
                "total_opportunities_seen": p.get("total_opportunities_seen", 0),
                "max_drawdown_pct": round(max_dd * 100, 2),
                "best_fill": best_fill,
                "worst_fill": worst_fill,
                "snapshots_count": len(snapshots),
            }

    def export_full_state(self):
        """Export complete portfolio state for LLM analysis."""
        with self._lock:
            export = {
                "export_timestamp": time.time(),
                "portfolio": dict(self.portfolio),
                "summary": self.get_portfolio_summary(),
                "metrics": self.get_metrics(),
                "config": {
                    "mode": self.config.get("MODE"),
                    "paper_balance": self.config.get("PAPER_BALANCE"),
                    "min_profit": self.config.get("MIN_PROFIT"),
                    "max_exposure": self.config.get("MAX_EXPOSURE"),
                    "cost_buffer": self.config.get("COST_BUFFER"),
                    "min_liquidity": self.config.get("MIN_LIQUIDITY"),
                    "max_order_size": self.config.get("MAX_ORDER_SIZE"),
                },
            }
            return export
