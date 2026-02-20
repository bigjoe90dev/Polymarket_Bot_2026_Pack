import time
import threading
from datetime import datetime, timezone
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
from src.clob_websocket import CLOBWebSocketMonitor
from src.momentum_strategy import MomentumStrategy
from src.metrics_logger import MetricsLogger
from src.parity_checker import ParityChecker
from src.health_monitor import HealthMonitor

# Free-infra speed constants (overridable via config)
DEFAULT_MARKETS_PER_CYCLE = 20   # Order books fetched per cycle
CYCLE_SLEEP = 0.5                # Seconds between cycles (was 1.0)
MARKET_REFRESH_SECONDS = 120     # Re-fetch full market list every 2 min


class TradingBot:
    def __init__(self, config):
        self.config = config
        self.bot_mode = config.get("BOT_MODE", "FULL")
        
        # BTC_1H_ONLY mode: Clean mode for 1H BTC trend following only
        self.is_btc_1h_only = (self.bot_mode == "BTC_1H_ONLY")
        
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

        # Initialize whale systems ONLY in FULL mode
        self.wallet_scorer = None
        self.whale_tracker = None
        self.blockchain_monitor = None
        self.clob_websocket = None
        
        if not self.is_btc_1h_only:
            # FULL mode: Initialize all whale/copy trading systems
            self.wallet_scorer = WalletScorer(config)
            self.whale_tracker = WhaleTracker(config, wallet_scorer=self.wallet_scorer)

            # Blockchain monitor for real-time whale trades
            if config.get("USE_BLOCKCHAIN_MONITOR", False):
                def on_whale_trade(whale_address, signal_data):
                    self.whale_tracker.add_blockchain_signal(whale_address, signal_data)

                self.blockchain_monitor = BlockchainMonitor(config, on_whale_trade)
                print("[*] Blockchain monitor initialized (will start after whale discovery)")

            # CLOB WebSocket monitor for whale trades
            if config.get("USE_CLOB_WEBSOCKET", False):
                def on_clob_trade(signal_data):
                    self.whale_tracker.add_clob_signal(signal_data)

                self.clob_websocket = CLOBWebSocketMonitor(config, on_clob_trade)
                print("[*] CLOB WebSocket monitor initialized")
        
        # WS health tracking (for momentum strategy fallback)
        self._ws_healthy = False
        self._ws_last_healthy_time = 0
        
        # Always create momentum strategy for BTC_1H_ONLY mode
        self.momentum_strategy = MomentumStrategy(
            paper_engine=self.execution.paper_engine,
            config=config,
            is_btc_1h_only=self.is_btc_1h_only,
        )
        
        # BTC_1H_ONLY mode: Enable CLOB WebSocket for REAL-TIME prices (momentum needs sub-second updates!)
        if self.is_btc_1h_only and config.get("USE_CLOB_WEBSOCKET", True):
            # Create a simple callback that feeds prices to momentum strategy
            def on_price_update(token_id, price):
                if self.momentum_strategy:
                    self.momentum_strategy.on_price_update(token_id, price, source="ws")
            
            try:
                self.clob_websocket = CLOBWebSocketMonitor(config, on_price_update)
                print("[*] CLOB WebSocket initialized for REAL-TIME prices (momentum strategy)")
            except Exception as e:
                print(f"[!] CLOB WebSocket failed to initialize: {e}")
                self.clob_websocket = None
        
        if self.is_btc_1h_only:
            print("[*] MODE: BTC_1H_ONLY - disabled whale/copy/blockchain/arb/parity")
            print("[*] Momentum strategy initialized (BTC_1H_ONLY mode)")
        else:
            print("[*] Momentum strategy initialized (1H trend-following, WS + REST fallback)")

        # Telegram notifications
        self.notifier = TelegramNotifier(config)

        # Inject scorer + notifier into paper engine (only in FULL mode)
        if self.execution.paper_engine and not self.is_btc_1h_only:
            self.execution.paper_engine.scorer = self.wallet_scorer
            self.execution.paper_engine.notifier = self.notifier

        # Production monitoring systems (simplified for BTC_1H_ONLY)
        self.metrics = MetricsLogger(config)
        
        if not self.is_btc_1h_only:
            self.parity = ParityChecker(config)
        else:
            self.parity = None
            
        self.health = HealthMonitor(config, bot_ref=self)

        # Inject market service into paper engine for fee lookups
        if self.execution.paper_engine:
            self.execution.paper_engine.market_service = self.market

        # Heartbeat watchdog
        self._last_heartbeat = time.time()
        self._heartbeat_thread = threading.Thread(
            target=self._watchdog, daemon=True
        )
        self._heartbeat_thread.start()

    def run(self):
        print("[*] Bot warming up...")
        
        # A3: Prove persistence + file paths at startup
        import os
        data_dir = "data"
        os.makedirs(data_dir, exist_ok=True)
        
        paper_state_path = os.path.join(data_dir, "paper_state.json")
        paper_trades_path = os.path.join(data_dir, "paper_trades.jsonl")
        snapshots_dir = os.path.join(data_dir, "snapshots")
        os.makedirs(snapshots_dir, exist_ok=True)
        
        print(f"[FILE] paper_state path: {paper_state_path}")
        print(f"[FILE] paper_trades path: {paper_trades_path}")
        print(f"[FILE] snapshots dir: {snapshots_dir}")
        
        # Verify paper engine writable
        if hasattr(self, 'execution') and hasattr(self.execution, 'paper_engine') and self.execution.paper_engine:
            pe = self.execution.paper_engine
            print(f"[FILE] paper_engine initialized: cash_balance=${pe.portfolio.get('cash_balance', 0):.2f}")
            
            # Verify paper_trades.jsonl is writable
            try:
                trades_path = os.path.join(data_dir, "paper_trades.jsonl")
                # Test write access
                with open(trades_path, "a") as f:
                    pass
                print(f"[FILE] paper_trades.jsonl: writable")
            except Exception as e:
                print(f"[FILE] WARNING: paper_trades.jsonl not writable: {e}")
        
        # Log paper safety multiplier
        safety_mult = self.config.get("PAPER_SAFETY_MULTIPLIER", 1.0)
        if safety_mult > 1.0:
            print(f"[PAPER] Safety multiplier: {safety_mult}x (fees/slippage/spread assumptions inflated)")
        
        # Tracking for SELECT AUDIT throttling (A4)
        self._last_audit_market_id = None
        self._last_audit_entry_allowed = None
        self._last_audit_is_live = None
        
        # A4: Tracking for DECISION trace throttling (shows strategy evaluation)
        self._last_decision_log_time = 0
        self._last_decision_market = None
        self._last_decision_signal = None
        
        # Activity watchdog for forced trades
        force_mode = self.config.get("PAPER_FORCE_MODE", "OFF")
        if force_mode != "OFF":
            print(f"[FORCE] {force_mode} mode enabled - will force trades if no signals")
        self._force_mode = force_mode
        self._last_trade_time = 0
        self._force_trade_after_seconds = self.config.get("PAPER_FORCE_TRADE_AFTER_MINUTES", 5) * 60
        self._force_trade_size = self.config.get("PAPER_FORCE_TRADE_SIZE_USD", 5.0)
        self._force_max_spread = self.config.get("PAPER_FORCE_TRADE_MAX_SPREAD", 0.03)
        
        markets = self.market.get_active_markets()
        self._current_markets = markets
        self._last_market_refresh = time.time()
        print(f"[*] Found {len(markets)} active markets")
        print(f"[*] Speed: {self._markets_per_cycle} markets/cycle, sequential, {CYCLE_SLEEP}s sleep")
        
        # Register markets with momentum strategy
        if hasattr(self, 'momentum_strategy'):
            registered_count = 0
            for m in markets:
                yes_token = m.get("yes_token_id")
                no_token = m.get("no_token_id")
                condition_id = m.get("condition_id", "")
                title = m.get("title", "")
                # Get end_date from market metadata if available
                end_date = m.get("end_date") or m.get("endDate") or m.get("end_date_iso")
                # Get prices from Gamma API
                yes_price = m.get("yes_price", 0.5)
                no_price = m.get("no_price", 0.5)
                
                if yes_token and no_token:
                    was_registered = self.momentum_strategy.register_market(
                        condition_id, yes_token, no_token, title,
                        end_date=end_date, yes_price=yes_price, no_price=no_price
                    )
                    if was_registered:
                        registered_count += 1
            if self.is_btc_1h_only:
                print(f"[*] Registered {registered_count}/{len(markets)} markets with momentum strategy (BTC_1H_ONLY mode)")
            else:
                print(f"[*] Registered {registered_count}/{len(markets)} markets with momentum strategy (filtered by 1H Up/Down crypto)")

        # BTC_1H_ONLY mode: Skip whale/copy systems but START CLOB WebSocket for real-time prices
        if self.is_btc_1h_only:
            # Start CLOB WebSocket for real-time prices (momentum needs sub-second updates!)
            if self.clob_websocket:
                # CRITICAL: Only pass the SELECTED market (first in_window, else first upcoming)
                # This limits subscriptions to 2 assets (YES + NO tokens)
                if markets:
                    selected = markets[0]  # Already sorted by in_window priority
                    selected_market = [selected]  # Wrap in list for update_market_cache
                    yes_token = selected.get('yes_token_id', '')
                    no_token = selected.get('no_token_id', '')
                    print(f"[*] SELECTED MARKET: {selected.get('title', '')[:50]}...")
                    print(f"[*] Subscribing to YES={yes_token[:20]}... NO={no_token[:20]}...")
                    self.clob_websocket.update_market_cache(selected_market)
                    # Initialize tracking for first selected market
                    self._current_condition_id = selected.get('condition_id', '')
                    self._current_yes_token = yes_token
                    self._current_no_token = no_token
                else:
                    self.clob_websocket.update_market_cache(markets)
                self.clob_websocket.start()
                print(f"[*] CLOB WebSocket started for REAL-TIME prices (momentum strategy)")
            else:
                print("[*] BTC_1H_ONLY: Using REST polling for prices (CLOB WebSocket not available)")
        else:
            # FULL mode: Initialize whale systems
            print("[*] Discovering profitable traders from leaderboard...")
            self.whale_tracker.discover_whales()

            # Start blockchain monitor if enabled
            if self.blockchain_monitor:
                tracked_addresses = list(self.whale_tracker.tracked_wallets.keys())
                self.blockchain_monitor.update_tracked_wallets(tracked_addresses)
                self.blockchain_monitor.update_market_cache(markets)
                self.blockchain_monitor.start()
                print(f"[BLOCKCHAIN] Real-time monitoring started for {len(tracked_addresses)} whales")

            # Start CLOB WebSocket monitor if enabled
            if self.clob_websocket:
                tracked_addresses = list(self.whale_tracker.tracked_wallets.keys())
                self.clob_websocket.update_tracked_wallets(tracked_addresses)
                self.clob_websocket.update_market_cache(markets)
                self.clob_websocket.start()
                print(f"[CLOB] WebSocket monitoring started for {len(tracked_addresses)} whales")

            # Startup notification
            self.notifier.notify_startup(
                len(self.whale_tracker.tracked_wallets), len(markets)
            )

        if not markets:
            print("[!] No active markets found - HARD FAIL")
            raise SystemExit(1)
        
        # A5: One-shot self-check at startup
        if self.is_btc_1h_only:
            print("\n[STARTUP CHECK]")
            # Check prices flowing
            if hasattr(self, 'clob_websocket') and self.clob_websocket:
                ws_age = 0
                if hasattr(self.clob_websocket, '_last_message_time'):
                    ws_age = time.time() - getattr(self.clob_websocket, '_last_message_time', 0)
                print(f"  prices_flowing: last_ws_update_age={ws_age:.1f}s")
            
            # Check strategy ready
            if hasattr(self, 'momentum_strategy'):
                ms = self.momentum_strategy
                has_history = hasattr(ms.tracker, 'price_buffers') and len(ms.tracker.price_buffers) > 0
                warmup = "N/A"
                print(f"  strategy_ready: has_price_history={has_history}, warmup_remaining={warmup}")
            
            # Check execution ready
            pe_ready = hasattr(self, 'execution') and hasattr(self.execution, 'paper_engine') and self.execution.paper_engine
            print(f"  execution_ready: paper_engine={pe_ready}")
            print()

        # Track previous window state for transition detection
        self._was_in_window = False
        
        # Track current selected market for rollover detection
        self._current_condition_id = None
        self._current_yes_token = None
        self._current_no_token = None
        
        # Config values for rollover
        self._rollover_buffer_seconds = self.config.get("ROLLOVER_BUFFER_SECONDS", 20)
        
        # Heartbeat tracker
        self._last_heartbeat_log = 0

        while self.running:
            self._last_heartbeat = time.time()

            # v14: Update health monitor heartbeat
            self.health.update_main_loop_heartbeat()

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
                        
                        # AUDIT: Log selection status after refresh
                        if self.is_btc_1h_only and markets:
                            in_window_count = sum(1 for m in markets if m.get('in_window', False))
                            cutoff = self.config.get("NO_TRADE_LAST_MINUTES", 10)
                            in_window_eligible = sum(1 for m in markets
                                if m.get('in_window', False) and
                                m.get('accepting_orders', True) and
                                m.get('minutes_left', 0) is not None and
                                m.get('minutes_left', 0) > cutoff)
                            print(f"\n[AUDIT] Selection status:")
                            print(f"  total_valid={len(markets)}, in_window={in_window_count}, in_window_eligible={in_window_eligible}")
                            first = markets[0]
                            if first.get('in_window'):
                                print(f"  SELECTED: in_window, minutes_left={first.get('minutes_left')}, cutoff={cutoff}")
                            else:
                                print(f"  SELECTED: upcoming, minutes_to_start={first.get('minutes_to_start')}")
                            print()
                        
                        # v14: Update blockchain monitor market cache
                        if self.blockchain_monitor:
                            self.blockchain_monitor.update_market_cache(markets)
                except Exception as e:
                    print(f"[!] Market refresh failed: {e}")

            # ── BTC_1H_ONLY: Check market window and log transitions ──
            if self.is_btc_1h_only and markets:
                # Get the first market (selected by market.py priority)
                first_market = markets[0]
                in_window = first_market.get('in_window', False)
                minutes_left = first_market.get('minutes_left')
                minutes_to_start = first_market.get('minutes_to_start')
                current_condition_id = first_market.get('condition_id', '')
                current_yes_token = first_market.get('yes_token_id', '')
                current_no_token = first_market.get('no_token_id', '')
                entry_allowed = first_market.get('entry_allowed', False)
                entry_reason = first_market.get('entry_reason', 'unknown')
                cutoff = self.config.get("NO_TRADE_LAST_MINUTES", 10)
                
                # A4: Throttle SELECT AUDIT - only print when state changes
                now_utc = datetime.now(timezone.utc).isoformat()
                start_iso = first_market.get('start_time', '')[:19]
                end_iso = first_market.get('end_date', '')[:19]
                
                # Check if anything changed
                market_changed = (current_condition_id != self._last_audit_market_id)
                entry_changed = (entry_allowed != self._last_audit_entry_allowed)
                live_changed = (in_window != self._last_audit_is_live)
                
                if market_changed or entry_changed or live_changed:
                    print(f"\n[SELECT AUDIT]")
                    print(f"  now_utc={now_utc}")
                    print(f"  selected={first_market.get('title', '')[:50]}...")
                    print(f"  is_live={in_window}")
                    print(f"  accepting_orders={first_market.get('accepting_orders', True)}")
                    print(f"  start={start_iso} end={end_iso}")
                    print(f"  minutes_to_start={minutes_to_start} minutes_left={minutes_left}")
                    print(f"  cutoff={cutoff}")
                    print(f"  entry_allowed={entry_allowed} with reason: {entry_reason}")
                    print()
                    
                    # Update tracking
                    self._last_audit_market_id = current_condition_id
                    self._last_audit_entry_allowed = entry_allowed
                    self._last_audit_is_live = in_window
                
                # ── ROLLOVER DETECTION: Check if market changed or expired ──
                # Rollover triggers: condition_id changed OR market ended (minutes_left <= 0)
                market_ended = minutes_left is not None and minutes_left <= 0
                market_changed = current_condition_id != self._current_condition_id
                
                if market_changed or market_ended:
                    old_title = self._current_condition_id[:30] if self._current_condition_id else "None"
                    new_title = first_market.get('title', current_condition_id)[:50]
                    reason = "market_changed" if market_changed else "market_ended"
                    print(f"[ROLLOVER] {reason}: {old_title} -> {new_title}")
                    
                    # Update tracking
                    self._current_condition_id = current_condition_id
                    self._current_yes_token = current_yes_token
                    self._current_no_token = current_no_token
                    
                    # Resubscribe to new market
                    if self.clob_websocket and self.clob_websocket.running:
                        selected_market = [first_market]
                        self.clob_websocket.update_market_cache(selected_market)
                        # Clear and resubscribe
                        self.clob_websocket._market_condition_ids = {current_condition_id}
                        self.clob_websocket._yes_token_id = current_yes_token
                        self.clob_websocket._no_token_id = current_no_token
                        print(f"[ROLLOVER] Resubscribed to new market: YES={current_yes_token[:20]}... NO={current_no_token[:20]}...")
                
                # Check for UPCOMING -> IN_WINDOW transition
                if in_window and not self._was_in_window:
                    print(f"[*] 🚀 ENTERING WINDOW: {first_market.get('title', '')[:60]}")
                    print(f"[*] Status: IN_WINDOW - {minutes_left} min left")
                    print(f"[*] Entry rule: minutes_left={minutes_left} cutoff={cutoff} -> entry_allowed={entry_allowed} ({entry_reason})")
                
                # Update window state
                self._was_in_window = in_window
                
                # Heartbeat log every 60 seconds when not in window
                if not in_window and (now - self._last_heartbeat_log >= 60):
                    yes_p = first_market.get('yes_price', 0)
                    no_p = first_market.get('no_price', 0)
                    last_update = first_market.get('last_update_time', '')[:19]
                    print(f"[*] 💤 Waiting: {first_market.get('title', '')[:40]}... YES:${yes_p:.2f} NO:${no_p:.2f} ({last_update})")
                    self._last_heartbeat_log = now
                
                # Live price update every 15 seconds
                if not hasattr(self, '_last_price_update'):
                    self._last_price_update = 0
                if now - self._last_price_update >= 15:
                    # Refresh prices from CLOB
                    try:
                        self.market.refresh_hourly_prices()
                    except Exception as e:
                        pass  # Suppress errors
                    self._last_price_update = now
                    
                    # Print current market prices
                    yes_p = first_market.get('yes_price', 0)
                    no_p = first_market.get('no_price', 0)
                    last_update = first_market.get('last_update_time', '')[:19]
                    print(f"[*] 📊 Live: {first_market.get('title', '')[:40]}... YES:${yes_p:.2f} NO:${no_p:.2f} @ {last_update}")
            elif not markets and (now - self._last_heartbeat_log >= 60):
                # No markets at all - heartbeat log
                print(f"[*] 💤 Waiting: no active markets, retrying...")
                self._last_heartbeat_log = now

            # ── Dynamic risk limits: scale with account balance ──
            if self.execution.paper_engine:
                pe = self.execution.paper_engine
                cash = pe.portfolio.get("cash_balance", pe.starting_balance)
                self.risk.update_limits(cash, pe.starting_balance)

            # ── BTC_1H_ONLY mode: Skip all whale tracking ─────
            if not self.is_btc_1h_only:
                # FULL mode: Process whale tracking
                self.whale_tracker.discover_whales()    # No-op if fetched recently
                self.whale_tracker.discover_network()   # No-op if scanned recently

                # Process real-time blockchain signals
                blockchain_signals = self.whale_tracker.drain_blockchain_signals()
                
                # Process CLOB WebSocket signals
                clob_signals = self.whale_tracker.drain_clob_signals()
                
                polled_signals = self.whale_tracker.poll_whale_activity()

                # Combine all signals
                signals = clob_signals + blockchain_signals + polled_signals
            else:
                # BTC_1H_ONLY mode: No whale signals
                signals = []
                blockchain_signals = []
                clob_signals = []
                polled_signals = []

            # A4: DECISION trace - deterministic chain when evaluating momentum strategy
            if self.is_btc_1h_only and markets and now - self._last_decision_log_time >= self.config.get("MOMENTUM_DECISION_LOG_INTERVAL", 30):
                # Get current selected market info
                first_market = markets[0]
                current_condition_id = first_market.get('condition_id', '')
                
                # Get price status
                yes_token = first_market.get('yes_token_id', '')
                no_token = first_market.get('no_token_id', '')
                yes_price = first_market.get('yes_price', 0)
                no_price = first_market.get('no_price', 0)
                
                # Get strategy evaluation status
                strategy_status = "UNKNOWN"
                last_signal = "NONE"
                if hasattr(self, 'momentum_strategy'):
                    ms = self.momentum_strategy
                    # Check if prices are flowing
                    yes_ts = ms.tracker.last_prices.get(yes_token, (None, None))[1]
                    no_ts = ms.tracker.last_prices.get(no_token, (None, None))[1]
                    price_age = max(
                        now - yes_ts if yes_ts else 999,
                        now - no_ts if no_ts else 999
                    )
                    
                    # Get latest decision from log
                    if ms.tracker.decisions_log:
                        last_decision = ms.tracker.decisions_log[-1]
                        last_signal = f"{last_decision.action}:{last_decision.reason[:20]}"
                    
                    strategy_status = f"price_age={price_age:.1f}s"
                
                # Check if market is tradable
                minutes_left = first_market.get('minutes_left')
                in_window = minutes_left is not None and minutes_left > self.config.get("NO_TRADE_LAST_MINUTES", 10)
                accepting = first_market.get('accepting_orders', False)
                entry_allowed = in_window and accepting
                
                # Only log when something significant changes or on interval
                market_changed = (current_condition_id != self._last_decision_market)
                signal_changed = (last_signal != self._last_decision_signal)
                
                if market_changed or signal_changed or (now - self._last_decision_log_time) >= 60:
                    print(f"\n[DECISION]")
                    print(f"  market={first_market.get('title', '')[:40]}...")
                    print(f"  in_window={in_window} accepting={accepting} entry_allowed={entry_allowed}")
                    print(f"  prices: YES={yes_price:.4f} NO={no_price:.4f}")
                    print(f"  strategy: {strategy_status}")
                    print(f"  last_signal: {last_signal}")
                    print(f"  force_mode: {self._force_mode}")
                    print()
                    
                    self._last_decision_market = current_condition_id
                    self._last_decision_signal = last_signal
                self._last_decision_log_time = now
            
            # Activity watchdog: Force trade if no trades for too long
            if self.is_btc_1h_only and self._force_mode != "OFF" and self.execution.paper_engine:
                time_since_trade = now - self._last_trade_time
                if time_since_trade > self._force_trade_after_seconds:
                    print(f"[FORCE] No trades for {time_since_trade/60:.1f} min - attempting forced entry")
                    # Force a trade with current market conditions
                    if markets:
                        selected = markets[0]
                        # Check spread is acceptable
                        yes_p = selected.get('yes_price', 0.5)
                        no_p = selected.get('no_price', 0.5)
                        spread = abs(yes_p + no_p - 1.0)
                        if spread <= self._force_max_spread:
                            # Force entry in random direction
                            import random
                            side = random.choice(["YES", "NO"])
                            token_id = selected.get('yes_token_id') if side == "YES" else selected.get('no_token_id')
                            # Execute through momentum strategy's paper engine
                            if hasattr(self, 'momentum_strategy') and self.momentum_strategy.paper_engine:
                                self.momentum_strategy._execute_entry(
                                    token_id=token_id,
                                    price=yes_p if side == "YES" else no_p,
                                    market={
                                        "condition_id": selected.get('condition_id'),
                                        "outcome": side,
                                        "market_name": selected.get('title', ''),
                                    },
                                    action=f"ENTER_{side}",
                                    confidence=0.8
                                )
                                self._last_trade_time = now
                                print(f"[FORCE] Forced entry: {side} @ {yes_p if side == 'YES' else no_p}")
                        else:
                            print(f"[FORCE] Skipped - spread {spread:.4f} > max {self._force_max_spread}")

            # ── Momentum Strategy: WS-first + REST fallback ─────
            # Check WebSocket health and switch to REST polling if needed
            if hasattr(self, 'momentum_strategy') and self.momentum_strategy:
                ws_healthy = False
                if self.clob_websocket and hasattr(self.clob_websocket, 'connected'):
                    ws_healthy = self.clob_websocket.connected
                
                # Track WS health state
                if ws_healthy:
                    self._ws_healthy = True
                    self._ws_last_healthy_time = now
                else:
                    # Check if we should switch back to WS (if it was healthy for 60s)
                    if self._ws_healthy and (now - self._ws_last_healthy_time) > 60:
                        self._ws_healthy = False
                        print("[*] WS unhealthy for 60s, using REST polling fallback")
                
                # FIXED: Only poll REST when WS is stale (>10 seconds since last price update)
                # But don't report stale until WS has connected and received at least one update
                ws_stale_threshold = 10  # seconds
                
                # Use the dedicated WS timestamp (updated whenever WS sends price)
                last_ws = getattr(self.momentum_strategy, '_last_ws_update_ts', 0)
                
                # Only check staleness if we've received at least one WS update (last_ws > 0)
                if last_ws > 0:
                    ws_is_stale = (now - last_ws) > ws_stale_threshold
                else:
                    # First WS update not yet received - don't declare stale yet
                    ws_is_stale = False
                
                # Also log token-level debug for troubleshooting
                if not ws_is_stale:
                    # Count only the currently subscribed assets (should be 2 for selected market)
                    # Get from the selected market's tokens
                    token_count = 0
                    if markets:
                        selected = markets[0]
                        if selected.get('yes_token_id') and selected.get('no_token_id'):
                            token_count = 2  # YES + NO for selected market
                    
                    # Get most recent token update time
                    most_recent = 0
                    for _, ts in self.momentum_strategy.tracker.last_prices.values():
                        if ts > most_recent:
                            most_recent = ts
                    print(f"[DATA] WS healthy ({token_count} assets, last update {now - most_recent:.1f}s ago)")
                
                if ws_is_stale:
                    # Log first time we switch to REST
                    if not getattr(self, '_rest_poll_logged', False):
                        print(f"[DATA] WS stale (>10s no updates), polling REST fallback")
                        self._rest_poll_logged = True
                    self.momentum_strategy.poll_prices(self.market, source="rest")
                else:
                    # WS is healthy - skip REST polling
                    if not getattr(self, '_ws_healthy_logged', False):
                        print("[DATA] WS healthy, skipping REST")
                        self._ws_healthy_logged = True
                
                # Check exit conditions for open positions
                self.momentum_strategy.check_exits()

            # v14: Record signal metrics
            if clob_signals:
                self.metrics.increment("clob_signals_received", len(clob_signals))
                self.health.update_whale_signal()
            if blockchain_signals:
                self.metrics.increment("blockchain_signals_received", len(blockchain_signals))
                self.health.update_whale_signal()
            if polled_signals:
                self.metrics.increment("api_signals_received", len(polled_signals))

            # Execute copy trades + exits in paper mode (crash-proofed)
            # BTC_1H_ONLY mode: Skip all copy trading
            if not self.is_btc_1h_only and signals and self.execution.paper_engine:
                for signal in signals:
                    try:
                        # v14: Record signal metrics
                        self.metrics.increment_cumulative("total_signals_received")

                        if signal.get("type") == "COPY_EXIT":
                            # Whale is selling — close our matching position
                            with self.metrics.timer("copy_exit_execution_ms"):
                                result = self.execution.paper_engine.close_copy_position(
                                    signal, risk_guard=self.risk
                                )
                            if result and result.get("success"):
                                self._copy_exits += 1
                                self.metrics.increment("copy_exits_executed")
                                self.metrics.increment_cumulative("total_trades_executed")
                                self.health.update_trade_execution()
                        else:
                            # Whale is buying — open a copy position
                            self.metrics.increment("copy_trades_attempted")
                            with self.metrics.timer("copy_trade_execution_ms"):
                                result = self.execution.paper_engine.execute_copy_trade(
                                    signal, current_exposure=self.risk.current_exposure
                                )
                            if result and result.get("success"):
                                self._copy_trades += 1
                                self.risk.add_exposure(result.get("total_cost", 0))
                                self.notifier.notify_trade_opened(signal, result)
                                self.metrics.increment("copy_trades_executed")
                                self.metrics.increment_cumulative("total_trades_executed")
                                self.health.update_trade_execution()
                            elif result:
                                title = signal.get("market_title", "")[:40]
                                print(f"[COPY] SKIP: {result.get('reason', '?')} — {title}")
                                self.metrics.increment(f"skip_reason_{result.get('reason', 'unknown').replace(' ', '_')}")
                    except Exception as e:
                        print(f"[!] Copy trade error: {e}")
                        self.metrics.increment("copy_trade_errors")

            # ── Arb scanning: rotate through markets ──────────
            # DISABLED by default: negative EV per 4 LLM reviews
            # (HFT competition + two-leg execution risk = losses)
            if self.config.get("ENABLE_ARB_SCANNER", False):
                batch = self._get_next_batch(markets)

                for m in batch:
                    try:
                        # FAST PATH: Try to get order book from CLOB WebSocket (instant)
                        book = None
                        if self.clob_websocket and hasattr(self.clob_websocket, 'order_book'):
                            yes_token = m.get("yes_token_id") or m.get("yes_clob_token_id")
                            no_token = m.get("no_token_id") or m.get("no_clob_token_id")
                            
                            if yes_token and no_token:
                                # Get from WebSocket cache (no API call)
                                yes_snapshot = self.clob_websocket.order_book.get_order_book_snapshot(yes_token, depth=10)
                                no_snapshot = self.clob_websocket.order_book.get_order_book_snapshot(no_token, depth=10)
                                
                                if yes_snapshot.get("asks") and no_snapshot.get("asks"):  # Have recent data
                                    book = {
                                        "condition_id": m.get("condition_id"),
                                        "yes_token_id": yes_token,
                                        "no_token_id": no_token,
                                        "bids_yes": yes_snapshot.get("bids", []),
                                        "asks_yes": yes_snapshot.get("asks", []),
                                        "bids_no": no_snapshot.get("bids", []),
                                        "asks_no": no_snapshot.get("asks", []),
                                        "_from_clob_ws": True  # Mark as fast
                                    }
                                    print(f"[CLOB] ✅ FAST: Got order book from WS for {m.get('condition_id', '')[:12]}... yes_asks={len(yes_snapshot.get('asks', []))}, no_asks={len(no_snapshot.get('asks', []))}")
                                else:
                                    print(f"[CLOB] ⚠️  EMPTY: No order book in cache for {m.get('condition_id', '')[:12]}... yes={yes_token[:8] if yes_token else 'None'}... no={no_token[:8] if no_token else 'None'}")
                        
                        # FALLBACK: Fetch from REST API if no WebSocket data
                        if not book:
                            book = self.market.get_order_book(m)
                            if book:
                                print(f"[CLOB] 🔄 SLOW: Got order book from REST API for {m.get('condition_id', '')[:12]}...")
                        
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
                    # v14: Generate daily parity report
                    if self.parity:
                        self.parity.generate_daily_report()
                except Exception:
                    pass

            # v14: Periodic parity matching (every 5 minutes)
            if self._cycle_count % 600 == 0:  # 600 cycles * 0.5s = 5 min
                try:
                    self.parity.run_matching()
                except Exception as e:
                    print(f"[PARITY] Matching error: {e}")

            # v14: Update metrics gauges
            if self._cycle_count % 60 == 0:  # Every 30 seconds (60 cycles * 0.5s)
                try:
                    self.metrics.set_gauge("tracked_wallets", len(self.whale_tracker.tracked_wallets))
                    self.metrics.set_gauge("open_positions", len(self.execution.paper_engine.open_positions) if self.execution.paper_engine else 0)
                    self.metrics.set_gauge("current_exposure", self.risk.current_exposure)
                    if self.blockchain_monitor:
                        self.metrics.set_gauge("blockchain_connected", 1 if self.blockchain_monitor.connected else 0)
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

        # v14: Stop monitoring systems
        if self.health:
            self.health.stop()
            print("[*] Health monitor stopped.")
        if self.metrics:
            self.metrics.stop()
            print("[*] Metrics logger stopped.")
        if self.parity:
            self.parity._save_state()
            print("[*] Parity state saved.")

        # Stop blockchain monitor
        if self.blockchain_monitor:
            self.blockchain_monitor.stop()
            print("[*] Blockchain monitor stopped.")

        # Stop CLOB WebSocket monitor
        if self.clob_websocket:
            self.clob_websocket.stop()
            print("[*] CLOB WebSocket monitor stopped.")

        # Flush all state files to prevent data loss
        try:
            if self.execution and hasattr(self.execution, 'paper_engine') and self.execution.paper_engine:
                self.execution.paper_engine._save_state()
                print("[*] Paper state saved.")
        except Exception as e:
            print(f"[!] Paper state flush failed: {e}")
        
        # Only save wallet_scorer and whale_tracker in FULL mode (not BTC_1H_ONLY)
        if not self.is_btc_1h_only:
            try:
                if self.wallet_scorer:
                    self.wallet_scorer._save_state()
                    print("[*] Wallet scorer saved.")
            except Exception as e:
                print(f"[!] Wallet scorer flush failed: {e}")
            try:
                if self.whale_tracker:
                    self.whale_tracker._save_state()
                    print("[*] Whale state saved.")
            except Exception as e:
                print(f"[!] Whale state flush failed: {e}")
        else:
            print("[*] Skipping whale/wallet state save (BTC_1H_ONLY mode)")

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
