import time
import threading
from src.market import MarketDataService
from src.strategy import check_opportunity
from src.execution import ExecutionEngine
from src.risk import RiskGuard
from src.records import log_decision
from src.health import report_status
from src.data_collector import DataCollector
from src.whale_tracker import WhaleTracker
from src.wallet_scorer import WalletScorer
from src.notifier import TelegramNotifier
from src.blockchain_monitor import BlockchainMonitor

# Free-infra speed constants (overridable via config)
DEFAULT_MARKETS_PER_CYCLE = 20   # Order books fetched per cycle
CYCLE_SLEEP = 0.5                # Seconds between cycles (was 1.0)
MARKET_REFRESH_SECONDS = 120     # Re-fetch full market list every 2 min


class TradingBot:
    def __init__(self, config):
        self.config = config
        self.market = MarketDataService(config)
        self.risk = RiskGuard(config)
        self.execution = ExecutionEngine(config, self.risk, self.market)
        self.running = True
        self._start_time = time.time()
        self._current_markets = []
        self._cycle_count = 0
        self._market_offset = 0  # Rotation pointer into full market list
        self._last_market_refresh = 0
        self._market_heat = {}   # cid -> overround (lower = closer to arb)
        self._fetch_errors = 0   # Silent error counter
        self._copy_trades = 0    # Copy trades executed
        self._copy_exits = 0     # Copy exits executed
        self._last_daily_summary = 0  # Daily TG summary timer

        # Configurable speed params
        self._markets_per_cycle = config.get("MARKETS_PER_CYCLE", DEFAULT_MARKETS_PER_CYCLE)

        # Data collection for backtesting
        self.collector = DataCollector(enabled=config.get("COLLECT_DATA", True))

        # Wallet scoring: deep performance tracking + flow analysis
        self.wallet_scorer = WalletScorer(config)

        # Whale tracking for copy trading (with scorer for market filtering)
        self.whale_tracker = WhaleTracker(config, wallet_scorer=self.wallet_scorer)

        # Blockchain monitor for real-time whale trades (2-3s latency vs 5-12min polling)
        self.blockchain_monitor = None
        if config.get("USE_BLOCKCHAIN_MONITOR", False):
            def on_whale_trade(whale_address, signal_data):
                """Callback when blockchain monitor detects whale trade."""
                self.whale_tracker.add_blockchain_signal(whale_address, signal_data)

            self.blockchain_monitor = BlockchainMonitor(config, on_whale_trade)
            print("[*] Blockchain monitor initialized (will start after whale discovery)")

        # Telegram notifications
        self.notifier = TelegramNotifier(config)

        # Inject scorer + notifier into paper engine if it exists
        if self.execution.paper_engine:
            self.execution.paper_engine.scorer = self.wallet_scorer
            self.execution.paper_engine.notifier = self.notifier

        # Heartbeat watchdog: detects if main loop hangs
        self._last_heartbeat = time.time()
        self._heartbeat_thread = threading.Thread(
            target=self._watchdog, daemon=True
        )
        self._heartbeat_thread.start()

    def run(self):
        print("[*] Bot warming up...")
        markets = self.market.get_active_markets()
        self._current_markets = markets
        self._last_market_refresh = time.time()
        print(f"[*] Found {len(markets)} active markets")
        print(f"[*] Speed: {self._markets_per_cycle} markets/cycle, sequential, {CYCLE_SLEEP}s sleep")

        # Discover profitable traders from leaderboard ($3k-$10k/month)
        print("[*] Discovering profitable traders from leaderboard...")
        self.whale_tracker.discover_whales()

        # Start blockchain monitor if enabled (after whale discovery so we have wallets to track)
        if self.blockchain_monitor:
            tracked_addresses = list(self.whale_tracker.tracked_wallets.keys())
            self.blockchain_monitor.update_tracked_wallets(tracked_addresses)
            self.blockchain_monitor.start()
            print(f"[BLOCKCHAIN] Real-time monitoring started for {len(tracked_addresses)} whales")

        # Startup notification
        self.notifier.notify_startup(
            len(self.whale_tracker.tracked_wallets), len(markets)
        )

        if not markets:
            print("[!] No active markets found. Exiting.")
            return

        while self.running:
            self._last_heartbeat = time.time()

            if self.risk.check_kill_switch():
                self.shutdown()
                break

            self._cycle_count += 1

            # Refresh market list every 2 minutes
            now = time.time()
            if now - self._last_market_refresh >= MARKET_REFRESH_SECONDS:
                try:
                    fresh = self.market.get_active_markets()
                    if fresh:
                        markets = fresh
                        self._current_markets = markets
                        self._market_offset = 0
                        self._last_market_refresh = now
                        print(f"[*] Market refresh: {len(markets)} active markets")
                except Exception as e:
                    print(f"[!] Market refresh failed: {e}")

            # ── Dynamic risk limits: scale with account balance ──
            if self.execution.paper_engine:
                pe = self.execution.paper_engine
                self.risk.update_limits(pe.portfolio["cash_balance"], pe.starting_balance)

            # ── Whale tracking: poll one wallet per cycle ─────
            self.whale_tracker.discover_whales()    # No-op if fetched recently
            self.whale_tracker.discover_network()   # No-op if scanned recently
            signals = self.whale_tracker.poll_whale_activity()

            # Execute copy trades + exits in paper mode (crash-proofed)
            if signals and self.execution.paper_engine:
                for signal in signals:
                    try:
                        if signal.get("type") == "COPY_EXIT":
                            # Whale is selling — close our matching position
                            result = self.execution.paper_engine.close_copy_position(
                                signal, risk_guard=self.risk
                            )
                            if result and result.get("success"):
                                self._copy_exits += 1
                        else:
                            # Whale is buying — open a copy position
                            result = self.execution.paper_engine.execute_copy_trade(
                                signal, current_exposure=self.risk.current_exposure
                            )
                            if result and result.get("success"):
                                self._copy_trades += 1
                                self.risk.add_exposure(result.get("total_cost", 0))
                                self.notifier.notify_trade_opened(signal, result)
                            elif result:
                                title = signal.get("market_title", "")[:40]
                                print(f"[COPY] SKIP: {result.get('reason', '?')} — {title}")
                    except Exception as e:
                        print(f"[!] Copy trade error: {e}")

            # ── Arb scanning: rotate through markets ──────────
            # DISABLED by default: negative EV per 4 LLM reviews
            # (HFT competition + two-leg execution risk = losses)
            if self.config.get("ENABLE_ARB_SCANNER", False):
                batch = self._get_next_batch(markets)

                for m in batch:
                    try:
                        book = self.market.get_order_book(m)
                        if not book:
                            self._fetch_errors += 1
                            continue

                        plan = check_opportunity(book, self.config)
                        self.collector.record(m, book, plan)
                        self._update_heat(m, book)

                        if plan:
                            log_decision(
                                "OPPORTUNITY",
                                f"[{plan['type']}] profit=${plan['expected_profit']:.4f}/unit "
                                f"in {m['condition_id'][:12]}..."
                            )
                            result = self.execution.execute_plan(
                                plan, book=book, market_info=m
                            )
                            if result and result.get("success"):
                                self.risk.add_exposure(result.get("total_cost", 0))

                    except Exception:
                        self._fetch_errors += 1
                        continue

            # Paper trading: settlement check and PnL snapshot (crash-proofed)
            try:
                if self.execution.paper_engine:
                    self.execution.paper_engine.check_and_settle_positions(
                        self.market, self.risk
                    )
                    self.execution.paper_engine.record_pnl_snapshot()
            except Exception as e:
                print(f"[!] Settlement/snapshot error: {e}")

            # Daily summary (every 24h)
            if time.time() - self._last_daily_summary >= 86400:
                try:
                    if self.execution.paper_engine:
                        data = self.execution.paper_engine.get_portfolio_data()
                        self.notifier.notify_daily_summary(data)
                    self._last_daily_summary = time.time()
                except Exception:
                    pass

            try:
                report_status(self)
            except Exception:
                pass

            # Flush collected data periodically
            if self._cycle_count % 60 == 0:
                self.collector.flush()

            time.sleep(self.config.get("CYCLE_SLEEP", CYCLE_SLEEP))

    def _get_next_batch(self, markets):
        """Get next batch of markets using rotation + heat priority."""
        total = len(markets)
        if total == 0:
            return []

        mpc = self._markets_per_cycle

        # Every 4th cycle: prioritize "hot" markets (lowest overround)
        if self._cycle_count % 4 == 0 and self._market_heat:
            hot = sorted(self._market_heat.items(), key=lambda x: x[1])
            hot_cids = {cid for cid, _ in hot[:mpc]}
            batch = [m for m in markets if m["condition_id"] in hot_cids]
            if len(batch) >= mpc // 2:
                return batch[:mpc]

        # Normal rotation: sliding window through ALL markets
        start = self._market_offset
        end = start + mpc

        if end <= total:
            batch = markets[start:end]
            self._market_offset = end
        else:
            batch = markets[start:] + markets[:end - total]
            self._market_offset = end - total

        return batch

    def _update_heat(self, market, book):
        """Track how close each market is to arbitrage (lower = hotter)."""
        asks_yes = book.get('asks_yes', [])
        asks_no = book.get('asks_no', [])
        if asks_yes and asks_no:
            overround = float(asks_yes[0][0]) + float(asks_no[0][0]) - 1.0
            self._market_heat[market["condition_id"]] = overround

    def shutdown(self):
        print("[!] Shutting down...")
        self.running = False
        self.collector.flush()
        # Stop blockchain monitor
        if self.blockchain_monitor:
            self.blockchain_monitor.stop()
            print("[*] Blockchain monitor stopped.")
        # Flush all state files to prevent data loss
        try:
            if self.execution.paper_engine:
                self.execution.paper_engine._save_state()
                print("[*] Paper state saved.")
        except Exception as e:
            print(f"[!] Paper state flush failed: {e}")
        try:
            self.wallet_scorer._save_state()
            print("[*] Wallet scorer saved.")
        except Exception as e:
            print(f"[!] Wallet scorer flush failed: {e}")
        try:
            self.whale_tracker._save_state()
            print("[*] Whale state saved.")
        except Exception as e:
            print(f"[!] Whale state flush failed: {e}")

    def _watchdog(self):
        """Background watchdog: detects hung main loop, emergency-saves state."""
        TIMEOUT = 120  # seconds before alarm (leaderboard scan takes ~90s)
        while self.running:
            time.sleep(10)
            if time.time() - self._last_heartbeat > TIMEOUT:
                print("[!!!] HEARTBEAT TIMEOUT — main loop hung for 120s+")
                print("[!!!] Emergency state save...")
                self.notifier.notify_alert("Heartbeat timeout — main loop hung for 120s+. Emergency state save triggered.")
                try:
                    if self.execution.paper_engine:
                        self.execution.paper_engine._save_state()
                    self.wallet_scorer._save_state()
                    self.whale_tracker._save_state()
                    print("[!!!] Emergency save complete.")
                except Exception as e:
                    print(f"[!!!] Emergency save failed: {e}")
                self._last_heartbeat = time.time()  # Reset to avoid spam
