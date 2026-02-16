"""Shadow parity validation for blockchain event decoding.

Validates that on-chain OrderFilled events are decoded correctly by matching
them against the Polymarket Data API trade feed. This is CRITICAL before going
LIVE - you need proof that your blockchain signals map to the correct:
- Market (condition_id)
- Outcome (YES/NO)
- Price (what the whale actually paid)
- Side (BUY vs SELL)
- Size

Without parity validation, you could be systematically trading the wrong side
or wrong markets, which would be catastrophic in LIVE mode.

Usage:
1. Run in paper mode with parity checking enabled for 48-72 hours
2. Review the daily parity report
3. Target: >95% match rate, <1% side mismatch rate
4. Fix any systematic decode errors before LIVE
"""

import json
import os
import time
import threading
from collections import defaultdict
from datetime import datetime


PARITY_STATE_FILE = "data/parity_state.json"
PARITY_REPORT_DIR = "data/parity_reports"


class ParityChecker:
    """Validates blockchain event decoding against API trade feed."""

    def __init__(self, config):
        self.config = config
        self.enabled = config.get("PARITY_CHECK_ENABLED", True)

        # Thread safety
        self._lock = threading.RLock()

        # Event storage (for matching)
        self.blockchain_events = {}  # tx_hash -> event_data
        self.api_trades = {}  # composite_key -> trade_data

        # Match results
        self.matched = []  # Successfully matched events
        self.mismatched_side = []  # Matched but wrong side
        self.mismatched_price = []  # Matched but price error > threshold
        self.unmatched_blockchain = []  # Blockchain event with no API match
        self.unmatched_api = []  # API trade with no blockchain match

        # Stats
        self.stats = {
            "total_blockchain_events": 0,
            "total_api_trades": 0,
            "total_matched": 0,
            "total_mismatched_side": 0,
            "total_mismatched_price": 0,
            "total_unmatched_blockchain": 0,
            "total_unmatched_api": 0,
            "last_report_time": 0,
        }

        # Initialize directories
        os.makedirs(PARITY_REPORT_DIR, exist_ok=True)
        self._load_state()

        print(f"[PARITY] Checker initialized (enabled: {self.enabled})")

    # ── Event Recording ───────────────────────────────────────

    def record_blockchain_event(self, event_data):
        """Record a blockchain OrderFilled event for later matching.

        Args:
            event_data: Dict with decoded blockchain event:
                {
                    "tx_hash": "0x...",
                    "block_number": 12345,
                    "log_index": 0,
                    "condition_id": "0x...",
                    "outcome": "YES" or "NO",
                    "whale_address": "0x...",
                    "whale_price": 0.55,
                    "whale_side": "BUY" or "SELL",
                    "size": 100.0,
                    "timestamp": 1234567890,
                }
        """
        if not self.enabled:
            return

        with self._lock:
            tx_hash = event_data.get("tx_hash", "")
            if not tx_hash:
                return

            self.blockchain_events[tx_hash] = event_data
            self.stats["total_blockchain_events"] += 1

            # Trim old events (keep last 5000)
            if len(self.blockchain_events) > 5000:
                oldest_keys = sorted(
                    self.blockchain_events.keys(),
                    key=lambda k: self.blockchain_events[k].get("timestamp", 0)
                )[:1000]
                for k in oldest_keys:
                    del self.blockchain_events[k]

    def record_api_trade(self, trade_data):
        """Record an API trade for matching against blockchain events.

        Args:
            trade_data: Dict with API trade data:
                {
                    "tx_hash": "0x..." (if available),
                    "condition_id": "0x...",
                    "outcome": "YES" or "NO",
                    "wallet": "0x...",
                    "price": 0.55,
                    "side": "BUY" or "SELL",
                    "size": 100.0,
                    "timestamp": 1234567890,
                }
        """
        if not self.enabled:
            return

        with self._lock:
            # Create composite key for matching
            # (tx_hash is preferred, but not always available from API)
            tx_hash = trade_data.get("tx_hash", "")

            if tx_hash:
                key = tx_hash
            else:
                # Fallback: hash wallet + condition + timestamp
                key = "{}_{}_{:.0f}".format(
                    trade_data.get("wallet", "")[:10],
                    trade_data.get("condition_id", "")[:10],
                    trade_data.get("timestamp", 0)
                )

            self.api_trades[key] = trade_data
            self.stats["total_api_trades"] += 1

            # Trim old trades
            if len(self.api_trades) > 5000:
                oldest_keys = sorted(
                    self.api_trades.keys(),
                    key=lambda k: self.api_trades[k].get("timestamp", 0)
                )[:1000]
                for k in oldest_keys:
                    del self.api_trades[k]

    # ── Matching Logic ────────────────────────────────────────

    def run_matching(self):
        """Match blockchain events to API trades and record results.

        Should be called periodically (e.g., every 5 minutes) to match recent
        events and generate parity reports.
        """
        if not self.enabled:
            return

        with self._lock:
            # Match by tx_hash (most reliable)
            for tx_hash, bc_event in list(self.blockchain_events.items()):
                if tx_hash in self.api_trades:
                    api_trade = self.api_trades[tx_hash]
                    self._compare_and_record(bc_event, api_trade, tx_hash)
                    # Remove from pools
                    del self.blockchain_events[tx_hash]
                    del self.api_trades[tx_hash]

            # Fuzzy match: same wallet + condition + similar timestamp
            # (within 30 seconds)
            for tx_hash, bc_event in list(self.blockchain_events.items()):
                bc_wallet = bc_event.get("whale_address", "").lower()
                bc_cid = bc_event.get("condition_id", "")
                bc_ts = bc_event.get("timestamp", 0)

                matched = False
                for api_key, api_trade in list(self.api_trades.items()):
                    api_wallet = api_trade.get("wallet", "").lower()
                    api_cid = api_trade.get("condition_id", "")
                    api_ts = api_trade.get("timestamp", 0)

                    if (bc_wallet == api_wallet and
                        bc_cid == api_cid and
                        abs(bc_ts - api_ts) < 30):
                        # Fuzzy match found
                        self._compare_and_record(bc_event, api_trade, tx_hash)
                        del self.blockchain_events[tx_hash]
                        del self.api_trades[api_key]
                        matched = True
                        break

                if matched:
                    continue

            # Mark remaining as unmatched (after 5 min)
            now = time.time()
            stale_threshold = 300  # 5 minutes

            for tx_hash, bc_event in list(self.blockchain_events.items()):
                if now - bc_event.get("timestamp", now) > stale_threshold:
                    self.unmatched_blockchain.append(bc_event)
                    self.stats["total_unmatched_blockchain"] += 1
                    del self.blockchain_events[tx_hash]

            for api_key, api_trade in list(self.api_trades.items()):
                if now - api_trade.get("timestamp", now) > stale_threshold:
                    self.unmatched_api.append(api_trade)
                    self.stats["total_unmatched_api"] += 1
                    del self.api_trades[api_key]

    def _compare_and_record(self, bc_event, api_trade, match_key):
        """Compare blockchain event to API trade and record result."""
        # Check side match
        bc_side = bc_event.get("whale_side", "").upper()
        api_side = api_trade.get("side", "").upper()

        # Check outcome match
        bc_outcome = bc_event.get("outcome", "").upper()
        api_outcome = api_trade.get("outcome", "").upper()

        # Check price match (within 1%)
        bc_price = bc_event.get("whale_price", 0)
        api_price = api_trade.get("price", 0)
        price_error = abs(bc_price - api_price) / max(api_price, 0.001)

        match_result = {
            "match_key": match_key,
            "timestamp": time.time(),
            "bc_event": bc_event,
            "api_trade": api_trade,
            "side_match": bc_side == api_side,
            "outcome_match": bc_outcome == api_outcome,
            "price_error_pct": round(price_error * 100, 2),
        }

        # Classify match
        if bc_side != api_side or bc_outcome != api_outcome:
            self.mismatched_side.append(match_result)
            self.stats["total_mismatched_side"] += 1
        elif price_error > 0.01:  # >1% price error
            self.mismatched_price.append(match_result)
            self.stats["total_mismatched_price"] += 1
        else:
            self.matched.append(match_result)
            self.stats["total_matched"] += 1

        # Trim result arrays (keep last 1000 of each type)
        self.matched = self.matched[-1000:]
        self.mismatched_side = self.mismatched_side[-1000:]
        self.mismatched_price = self.mismatched_price[-1000:]
        self.unmatched_blockchain = self.unmatched_blockchain[-1000:]
        self.unmatched_api = self.unmatched_api[-1000:]

    # ── Reporting ─────────────────────────────────────────────

    def generate_daily_report(self):
        """Generate and save a daily parity report.

        Returns report dict with match rates, error distributions, examples.
        """
        if not self.enabled:
            return {"enabled": False}

        with self._lock:
            total_attempts = (
                self.stats["total_matched"] +
                self.stats["total_mismatched_side"] +
                self.stats["total_mismatched_price"]
            )

            if total_attempts == 0:
                match_rate = 0.0
                side_error_rate = 0.0
                price_error_rate = 0.0
            else:
                match_rate = self.stats["total_matched"] / total_attempts * 100
                side_error_rate = self.stats["total_mismatched_side"] / total_attempts * 100
                price_error_rate = self.stats["total_mismatched_price"] / total_attempts * 100

            # Price error distribution
            price_errors = [m["price_error_pct"] for m in self.matched + self.mismatched_price]
            if price_errors:
                price_errors_sorted = sorted(price_errors)
                n = len(price_errors_sorted)
                price_dist = {
                    "min": round(price_errors_sorted[0], 2),
                    "p50": round(price_errors_sorted[n // 2], 2),
                    "p95": round(price_errors_sorted[int(n * 0.95)], 2) if n > 1 else 0,
                    "max": round(price_errors_sorted[-1], 2),
                }
            else:
                price_dist = {}

            report = {
                "report_date": datetime.now().isoformat(),
                "enabled": self.enabled,
                "stats": dict(self.stats),
                "match_rate_pct": round(match_rate, 1),
                "side_error_rate_pct": round(side_error_rate, 1),
                "price_error_rate_pct": round(price_error_rate, 1),
                "price_error_distribution": price_dist,
                "sample_mismatched_side": self.mismatched_side[-5:],  # Last 5 examples
                "sample_unmatched_blockchain": self.unmatched_blockchain[-5:],
                "recommendation": self._get_recommendation(match_rate, side_error_rate),
            }

            # Save to file
            self._save_report(report)
            self.stats["last_report_time"] = time.time()

            return report

    def _get_recommendation(self, match_rate, side_error_rate):
        """Generate recommendation based on parity results."""
        if match_rate >= 95 and side_error_rate <= 1:
            return "✅ EXCELLENT - Blockchain decoding is accurate. Safe to proceed to shadow mode."
        elif match_rate >= 90 and side_error_rate <= 2:
            return "✅ GOOD - Minor discrepancies, acceptable for shadow mode. Monitor closely."
        elif match_rate >= 80 and side_error_rate <= 5:
            return "⚠️ MARGINAL - Significant decode errors. Fix before shadow mode."
        else:
            return "🚫 POOR - Critical decode errors. DO NOT proceed to LIVE. Debug blockchain_monitor.py."

    def _save_report(self, report):
        """Save parity report to disk."""
        try:
            date_str = datetime.now().strftime("%Y-%m-%d")
            report_path = os.path.join(PARITY_REPORT_DIR, f"parity_{date_str}.json")

            with open(report_path, "w") as f:
                json.dump(report, f, indent=2)

            print(f"[PARITY] Report saved: {report_path}")
            print(f"[PARITY] Match rate: {report['match_rate_pct']}% | "
                  f"Side errors: {report['side_error_rate_pct']}% | "
                  f"Recommendation: {report['recommendation']}")

        except Exception as e:
            print(f"[PARITY] Report save error: {e}")

    # ── State Persistence ─────────────────────────────────────

    def _save_state(self):
        """Save parity checker state to disk."""
        try:
            state = {
                "stats": self.stats,
                "matched_count": len(self.matched),
                "mismatched_side_count": len(self.mismatched_side),
                "last_updated": time.time(),
            }

            tmp = PARITY_STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp, PARITY_STATE_FILE)

        except Exception as e:
            print(f"[PARITY] State save error: {e}")

    def _load_state(self):
        """Load parity checker state from disk."""
        if not os.path.exists(PARITY_STATE_FILE):
            return

        try:
            with open(PARITY_STATE_FILE, "r") as f:
                state = json.load(f)

            self.stats.update(state.get("stats", {}))
            print(f"[PARITY] Loaded state: {state['matched_count']} matched events")

        except Exception as e:
            print(f"[PARITY] State load error: {e}")

    def get_summary(self):
        """Get current parity summary for dashboard."""
        with self._lock:
            total = (
                self.stats["total_matched"] +
                self.stats["total_mismatched_side"] +
                self.stats["total_mismatched_price"]
            )

            return {
                "enabled": self.enabled,
                "total_events_matched": total,
                "match_rate_pct": round(
                    self.stats["total_matched"] / max(total, 1) * 100, 1
                ),
                "side_error_rate_pct": round(
                    self.stats["total_mismatched_side"] / max(total, 1) * 100, 1
                ),
                "pending_blockchain": len(self.blockchain_events),
                "pending_api": len(self.api_trades),
            }
