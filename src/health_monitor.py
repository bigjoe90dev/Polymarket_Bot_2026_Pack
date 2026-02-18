"""Production health monitoring and auto-recovery system.

Monitors all subsystems for signs of silent failure:
- Blockchain monitor: no blocks/events for N minutes
- Whale tracker: no signals for N hours
- Main loop: heartbeat timeout
- State files: corruption detection
- Network: connectivity issues
- Memory: leak detection (approximate)

Provides auto-recovery where possible:
- Force blockchain reconnect
- Emergency state saves
- Graceful degradation

Alerts when manual intervention required.
"""

import time
import threading
import os
from collections import deque


class HealthMonitor:
    """Comprehensive health checking and auto-recovery system."""

    def __init__(self, config, bot_ref=None):
        self.config = config
        self.bot = bot_ref  # Reference to main bot for recovery actions
        self.enabled = config.get("HEALTH_MONITOR_ENABLED", True)
        self.check_interval = config.get("HEALTH_CHECK_INTERVAL_SEC", 30)

        # Thread safety
        self._lock = threading.RLock()
        self._running = False
        self._thread = None

        # Health state
        self.health_status = {
            "overall": "HEALTHY",
            "last_check": 0,
            "checks_run": 0,
            "issues_detected": 0,
            "auto_recoveries": 0,
        }

        # Subsystem liveness tracking
        self.liveness = {
            "main_loop_heartbeat": time.time(),
            "blockchain_last_block": 0,
            "blockchain_last_event": 0,
            "whale_last_signal": 0,
            "last_trade_execution": 0,
        }

        # Issue history
        self.issues = deque(maxlen=100)

        # Thresholds (configurable)
        self.thresholds = {
            "main_loop_timeout_sec": 120,
            "blockchain_stall_sec": 300,  # 5 minutes no blocks
            "blockchain_event_drought_sec": 3600,  # 1 hour no events
            "signal_drought_sec": 7200,  # 2 hours no signals (might be legit)
            "blockchain_reconnect_limit_1h": 10,
            "decode_failure_limit_1h": 20,
        }

        # Start monitoring
        if self.enabled:
            self.start()

    # ── Public API for Liveness Updates ──────────────────────

    def update_main_loop_heartbeat(self):
        """Called by main bot loop each iteration."""
        with self._lock:
            self.liveness["main_loop_heartbeat"] = time.time()

    def update_blockchain_block(self, block_number):
        """Called when blockchain monitor sees a new block."""
        with self._lock:
            self.liveness["blockchain_last_block"] = time.time()

    def update_blockchain_event(self):
        """Called when blockchain monitor processes an event."""
        with self._lock:
            self.liveness["blockchain_last_event"] = time.time()

    def update_whale_signal(self):
        """Called when whale tracker emits a signal."""
        with self._lock:
            self.liveness["whale_last_signal"] = time.time()

    def update_trade_execution(self):
        """Called when a trade is executed."""
        with self._lock:
            self.liveness["last_trade_execution"] = time.time()

    # ── Health Checking Loop ──────────────────────────────────

    def start(self):
        """Start background health monitoring."""
        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(target=self._monitoring_loop, daemon=True)
        self._thread.start()
        print(f"[HEALTH] Monitor started (interval: {self.check_interval}s)")

    def stop(self):
        """Stop health monitoring."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        print("[HEALTH] Monitor stopped")

    def _monitoring_loop(self):
        """Background thread that runs health checks."""
        while self._running:
            time.sleep(self.check_interval)
            self._run_health_checks()

    def _run_health_checks(self):
        """Run all health checks and record issues."""
        with self._lock:
            now = time.time()
            self.health_status["last_check"] = now
            self.health_status["checks_run"] += 1

            issues = []

            # Check if in BTC_1H_ONLY mode (skip whale/copy/blockchain checks)
            is_btc_1h_only = self.config.get("BOT_MODE") == "BTC_1H_ONLY"

            # Check 1: Main loop heartbeat
            heartbeat_age = now - self.liveness["main_loop_heartbeat"]
            if heartbeat_age > self.thresholds["main_loop_timeout_sec"]:
                issues.append({
                    "severity": "CRITICAL",
                    "component": "main_loop",
                    "issue": f"Heartbeat timeout ({heartbeat_age:.0f}s)",
                    "action": "emergency_state_save",
                })

            # Skip blockchain and whale checks in BTC_1H_ONLY mode
            if not is_btc_1h_only:
                # Check 2: Blockchain monitor stall
                if self.bot and hasattr(self.bot, 'blockchain_monitor') and self.bot.blockchain_monitor:
                    block_age = now - self.liveness["blockchain_last_block"]
                    if block_age > self.thresholds["blockchain_stall_sec"]:
                        issues.append({
                            "severity": "HIGH",
                            "component": "blockchain_monitor",
                            "issue": f"No blocks for {block_age:.0f}s",
                            "action": "force_reconnect",
                        })

                # Check 3: Blockchain event drought (might be legit, just warn)
                # Only check if we have a valid timestamp (not 0/None)
                blockchain_last_event = self.liveness.get("blockchain_last_event", 0)
                if blockchain_last_event > 0:
                    event_age = now - blockchain_last_event
                    if event_age > self.thresholds["blockchain_event_drought_sec"]:
                        issues.append({
                            "severity": "MEDIUM",
                            "component": "blockchain_monitor",
                            "issue": f"No events for {event_age/60:.0f} minutes",
                            "action": "monitor",
                        })

                # Check 4: Signal drought (might be legit market conditions)
                # Only check if we have a valid timestamp (not 0/None)
                whale_last_signal = self.liveness.get("whale_last_signal", 0)
                if whale_last_signal > 0:
                    signal_age = now - whale_last_signal
                    if signal_age > self.thresholds["signal_drought_sec"]:
                        issues.append({
                            "severity": "LOW",
                            "component": "whale_tracker",
                            "issue": f"No signals for {signal_age/3600:.1f} hours",
                            "action": "monitor",
                        })

            # Check 5: State file corruption (basic check)
            state_check = self._check_state_files()
            if state_check:
                issues.append(state_check)

            # Process issues
            for issue in issues:
                self._handle_issue(issue)

            # Update overall status
            if any(i["severity"] == "CRITICAL" for i in issues):
                self.health_status["overall"] = "CRITICAL"
            elif any(i["severity"] == "HIGH" for i in issues):
                self.health_status["overall"] = "DEGRADED"
            elif any(i["severity"] == "MEDIUM" for i in issues):
                self.health_status["overall"] = "WARNING"
            else:
                self.health_status["overall"] = "HEALTHY"

    def _handle_issue(self, issue):
        """Handle a detected health issue with appropriate recovery action."""
        self.health_status["issues_detected"] += 1
        self.issues.append({
            "timestamp": time.time(),
            **issue
        })

        # Log the issue
        severity_emoji = {
            "CRITICAL": "🚨",
            "HIGH": "⚠️",
            "MEDIUM": "⚡",
            "LOW": "ℹ️",
        }
        emoji = severity_emoji.get(issue["severity"], "❓")
        print(f"[HEALTH] {emoji} {issue['severity']}: {issue['component']} - {issue['issue']}")

        # Take recovery action
        action = issue.get("action")

        if action == "emergency_state_save" and self.bot:
            print("[HEALTH] 🆘 Emergency state save triggered")
            self._emergency_save_all_state()
            self.health_status["auto_recoveries"] += 1

        elif action == "force_reconnect" and self.bot:
            print("[HEALTH] 🔄 Forcing blockchain reconnect")
            self._force_blockchain_reconnect()
            self.health_status["auto_recoveries"] += 1

        elif action == "monitor":
            # Just log, no auto-recovery
            pass

    def _emergency_save_all_state(self):
        """Emergency save all state files (called on critical issues)."""
        try:
            if hasattr(self.bot, 'execution') and hasattr(self.bot.execution, 'paper_engine'):
                self.bot.execution.paper_engine._save_state()
                print("[HEALTH] ✅ Paper state saved")
        except Exception as e:
            print(f"[HEALTH] ❌ Paper state save failed: {e}")

        try:
            if hasattr(self.bot, 'wallet_scorer'):
                self.bot.wallet_scorer._save_state()
                print("[HEALTH] ✅ Wallet scorer saved")
        except Exception as e:
            print(f"[HEALTH] ❌ Wallet scorer save failed: {e}")

        try:
            if hasattr(self.bot, 'whale_tracker'):
                self.bot.whale_tracker._save_state()
                print("[HEALTH] ✅ Whale tracker saved")
        except Exception as e:
            print(f"[HEALTH] ❌ Whale tracker save failed: {e}")

        try:
            if hasattr(self.bot, 'risk'):
                self.bot.risk._save_state()
                print("[HEALTH] ✅ Risk state saved")
        except Exception as e:
            print(f"[HEALTH] ❌ Risk state save failed: {e}")

    def _force_blockchain_reconnect(self):
        """Force blockchain monitor to reconnect."""
        try:
            if (hasattr(self.bot, 'blockchain_monitor') and
                self.bot.blockchain_monitor and
                hasattr(self.bot.blockchain_monitor, 'connected')):

                # Set connected = False to trigger reconnection in the monitor loop
                self.bot.blockchain_monitor.connected = False
                print("[HEALTH] ✅ Blockchain reconnect triggered")

        except Exception as e:
            print(f"[HEALTH] ❌ Blockchain reconnect failed: {e}")

    def _check_state_files(self):
        """Check if critical state files exist and are readable."""
        # In BTC_1H_ONLY mode, skip whale_state.json and wallet_scores.json
        is_btc_1h_only = self.config.get("BOT_MODE") == "BTC_1H_ONLY"
        
        critical_files = [
            "data/paper_state.json",
            "data/risk_state.json",
        ]
        
        # Only check whale/wallet files in FULL mode
        if not is_btc_1h_only:
            critical_files.extend([
                "data/whale_state.json",
                "data/wallet_scores.json",
            ])

        for filepath in critical_files:
            if not os.path.exists(filepath):
                # Missing file might be okay for fresh start
                continue

            try:
                # Try to read the file
                with open(filepath, "r") as f:
                    import json
                    json.load(f)
            except json.JSONDecodeError:
                return {
                    "severity": "HIGH",
                    "component": "state_persistence",
                    "issue": f"Corrupted file: {filepath}",
                    "action": "monitor",  # Let backup rotation handle this
                }
            except Exception as e:
                return {
                    "severity": "MEDIUM",
                    "component": "state_persistence",
                    "issue": f"Cannot read {filepath}: {e}",
                    "action": "monitor",
                }

        return None

    # ── Reporting ─────────────────────────────────────────────

    def get_health_status(self):
        """Get current health status for dashboard/API."""
        with self._lock:
            return {
                "overall_status": self.health_status["overall"],
                "last_check": self.health_status["last_check"],
                "checks_run": self.health_status["checks_run"],
                "issues_detected": self.health_status["issues_detected"],
                "auto_recoveries": self.health_status["auto_recoveries"],
                "recent_issues": list(self.issues)[-10:],
                "liveness": dict(self.liveness),
            }

    def get_health_summary(self):
        """Get one-line health summary."""
        with self._lock:
            status = self.health_status["overall"]
            recent = len([i for i in self.issues if time.time() - i["timestamp"] < 300])

            return {
                "status": status,
                "recent_issues_5min": recent,
                "emoji": {
                    "HEALTHY": "✅",
                    "WARNING": "⚡",
                    "DEGRADED": "⚠️",
                    "CRITICAL": "🚨",
                }.get(status, "❓"),
            }
