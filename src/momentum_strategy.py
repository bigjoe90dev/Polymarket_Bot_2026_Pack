"""
1H Trend-Following Strategy for Polymarket
==========================================
Phase 1: Paper-only, BTC only, 1H timeframe

Signal Logic: 3-Layer Gating
- Layer 0: Data Sanity (30s updates, 15min history)
- Layer 1: Regime Filter (trendiness score > 0.3)
- Layer 2: Entry Trigger (breakout + 5min return + cooldown)

Exit Rules:
- Take Profit: +8 ticks
- Stop Loss: -3 cents
- Trailing MA: 20-period
- Max Hold: 45 minutes

Confidence Scoring: Do nothing when unsure (confidence < 0.5)
"""

import time
import threading
import re
from collections import defaultdict, deque
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field


@dataclass
class PricePoint:
    """Single price update with timestamp."""
    timestamp: float
    price: float
    source: str = "unknown"  # "ws" or "rest"


@dataclass
class Position:
    """Track open position."""
    condition_id: str
    token_id: str
    outcome: str  # "YES" or "NO"
    entry_price: float
    entry_time: float
    size: float
    market_name: str = ""
    

@dataclass
class TradeDecision:
    """Structured trade decision for logging."""
    action: str  # ENTER_YES, ENTER_NO, EXIT, SKIP
    asset: str
    timeframe: str
    market_title: str
    token_id: str
    last_price: float
    trendiness: float
    breakout_status: str
    time_left_minutes: float
    confidence: float
    reason: str
    time_left_source: str = "none"  # "metadata", "title_fallback", or "none"
    tick_size: float = 0.01  # Tick size in dollars (default $0.01 for Polymarket)


class TrendTracker:
    """Track price data and compute indicators for trend-following."""
    
    def __init__(self, config: Dict):
        self.config = config
        
        # Thresholds from config
        self.min_data_seconds = config.get("TREND_MIN_DATA_SECONDS", 30)
        self.min_history_minutes = config.get("TREND_MIN_HISTORY_MINUTES", 15)
        self.trendiness_threshold = config.get("TREND_TRENDINESS_THRESHOLD", 0.3)
        self.breakout_ticks = config.get("TREND_BREAKOUT_TICKS", 1)
        self.return_threshold = config.get("TREND_RETURN_THRESHOLD", 0.005)
        self.cooldown_minutes = config.get("TREND_COOLDOWN_MINUTES", 30)
        self.time_left_threshold = config.get("TREND_TIME_LEFT_THRESHOLD", 12)
        
        # Exit thresholds
        self.tp_ticks = config.get("TREND_TP_TICKS", 8)
        self.sl_cents = config.get("TREND_SL_CENTS", 3)
        self.trailing_ma_periods = config.get("TREND_TRAILING_MA_PERIODS", 20)
        self.max_hold_minutes = config.get("TREND_MAX_HOLD_MINUTES", 45)
        
        # Confidence
        self.confidence_threshold = config.get("TREND_CONFIDENCE_THRESHOLD", 0.5)
        
        # Price buffer: token_id -> deque of PricePoint
        self.price_buffers: Dict[str, deque] = defaultdict(lambda: deque(maxlen=1000))
        
        # Last price: token_id -> (price, timestamp)
        self.last_prices: Dict[str, tuple] = {}
        
        # Cooldowns: token_id -> last_trade_time
        self.cooldowns: Dict[str, float] = {}
        
        # Open positions: condition_id -> Position
        self.positions: Dict[str, Position] = {}
        
        # MA buffer for trailing exit: token_id -> deque of prices
        self.ma_buffers: Dict[str, deque] = defaultdict(lambda: deque(maxlen=100))
        
        # Statistics
        self.decisions_log: List[TradeDecision] = []
        self._lock = threading.Lock()
    
    def update_price(self, token_id: str, price: float, source: str = "unknown"):
        """Update price data for a token."""
        now = time.time()
        
        with self._lock:
            # Add to price buffer
            self.price_buffers[token_id].append(PricePoint(now, price, source))
            
            # Update last price
            self.last_prices[token_id] = (price, now)
            
            # Add to MA buffer
            self.ma_buffers[token_id].append(price)
    
    def is_data_sane(self, token_id: str) -> tuple:
        """Layer 0: Check if data is fresh enough.
        Returns (is_sane, reason)"""
        with self._lock:
            buffer = self.price_buffers.get(token_id)
            if not buffer or len(buffer) < 2:
                return False, "INSUFFICIENT_DATA"
            
            # Check last update is recent (< 10 seconds)
            last_price, last_time = self.last_prices.get(token_id, (None, 0))
            if last_price is None:
                return False, "NO_LAST_PRICE"
            
            if time.time() - last_time > 10:
                return False, "STALE_DATA"
            
            # Check we have enough history (15 minutes)
            first_point = buffer[0]
            if time.time() - first_point.timestamp < self.min_history_minutes * 60:
                return False, "INSUFFICIENT_HISTORY"
            
            return True, "OK"
    
    def compute_trendiness(self, token_id: str) -> float:
        """Layer 1: Compute trendiness score.
        trendiness = |return_10min| / sum(|step_changes|)"""
        with self._lock:
            buffer = list(self.price_buffers.get(token_id, []))
            if len(buffer) < 10:
                return 0.0
            
            # Get 10-minute window
            cutoff = time.time() - 600  # 10 minutes
            recent = [p for p in buffer if p.timestamp >= cutoff]
            if len(recent) < 10:
                return 0.0
            
            prices = [p.price for p in recent]
            
            # Calculate return
            if prices[0] == 0:
                return 0.0
            return_10min = (prices[-1] - prices[0]) / prices[0]
            
            # Calculate sum of absolute changes
            steps = [abs(prices[i] - prices[i-1]) for i in range(1, len(prices))]
            total_steps = sum(steps)
            
            if total_steps == 0:
                return 0.0
            
            trendiness = abs(return_10min) / (total_steps / prices[0])
            return trendiness
    
    def compute_return_5min(self, token_id: str) -> float:
        """Compute 5-minute return."""
        with self._lock:
            buffer = list(self.price_buffers.get(token_id, []))
            if len(buffer) < 2:
                return 0.0
            
            # Get 5-minute window
            cutoff = time.time() - 300  # 5 minutes
            recent = [p for p in buffer if p.timestamp >= cutoff]
            if len(recent) < 2:
                return 0.0
            
            prices = [p.price for p in recent]
            if prices[0] == 0:
                return 0.0
            
            return (prices[-1] - prices[0]) / prices[0]
    
    def get_rolling_high_low(self, token_id: str, minutes: int = 10) -> tuple:
        """Get rolling high and low over N minutes."""
        with self._lock:
            buffer = list(self.price_buffers.get(token_id, []))
            cutoff = time.time() - (minutes * 60)
            recent = [p for p in buffer if p.timestamp >= cutoff]
            
            if not recent:
                return None, None
            
            prices = [p.price for p in recent]
            return max(prices), min(prices)
    
    def get_ma(self, token_id: str, periods: int = None) -> Optional[float]:
        """Get moving average."""
        if periods is None:
            periods = self.trailing_ma_periods
        
        with self._lock:
            buffer = list(self.ma_buffers.get(token_id, []))
            if len(buffer) < periods:
                return None
            
            return sum(buffer[-periods:]) / periods
    
    def check_cooldown(self, token_id: str) -> bool:
        """Check if cooldown has expired."""
        now = time.time()
        last_trade = self.cooldowns.get(token_id, 0)
        return (now - last_trade) > (self.cooldown_minutes * 60)
    
    def record_trade(self, token_id: str):
        """Record that we traded this token."""
        self.cooldowns[token_id] = time.time()
    
    def parse_time_left(self, market_title: str, end_date: str = None) -> tuple:
        """Parse time remaining from market title AND end_date metadata.
        
        Priority:
        1. end_date metadata (ISO 8601 timestamp) - most reliable
        2. Title parsing ("in 1 hour", etc)
        
        Returns (minutes_remaining, source) where:
        - minutes_remaining: float or None
        - source: "metadata" or "title_fallback" or "none"
        """
        now = time.time()
        
        # Priority 1: Try end_date from market metadata (MOST RELIABLE)
        if end_date:
            try:
                from datetime import datetime
                if isinstance(end_date, str):
                    end_date = end_date.replace('Z', '+00:00')
                    dt = datetime.fromisoformat(end_date)
                    resolves_at = dt.timestamp()
                    minutes_left = (resolves_at - now) / 60
                    if minutes_left > 0:
                        return minutes_left, "metadata"
            except Exception:
                pass
        
        # Priority 2: Parse from market title
        if not market_title:
            return None, "none"
        
        # Pattern 1: "in X hour" or "in X hours"
        match = re.search(r'in\s+(\d+)\s+hour', market_title, re.IGNORECASE)
        if match:
            return float(match.group(1)) * 60, "title_fallback"
        
        # Pattern 2: "in X min" or "in X minute"
        match = re.search(r'in\s+(\d+)\s+min', market_title, re.IGNORECASE)
        if match:
            return float(match.group(1)), "title_fallback"
        
        return None, "none"
    
    def is_entry_allowed(self, condition_id: str) -> tuple:
        """Check if entry is allowed based on time remaining.
        Returns (allowed: bool, minutes_left: float, cutoff: int)"""
        # Use config value for cutoff, default to 10 minutes
        cutoff = self.config.get("NO_TRADE_LAST_MINUTES", 10)
        
        metadata = self.market_metadata.get(condition_id, {})
        end_date = metadata.get("end_date")
        title = metadata.get("title", "")
        
        minutes_left, source = self.parse_time_left(title, end_date)
        
        if minutes_left is None:
            return False, 0, cutoff
        
        return minutes_left > cutoff, minutes_left, cutoff
    
    def compute_confidence(
        self,
        trendiness: float,
        breakout_magnitude: float,
        time_left_minutes: Optional[float]
    ) -> float:
        """Compute confidence score (0-1).
        confidence = trendiness * breakout_factor * time_factor"""
        # Trendiness factor (0-1)
        trend_factor = min(1.0, trendiness / self.trendiness_threshold)
        
        # Breakout magnitude factor (0-1)
        breakout_factor = min(1.0, breakout_magnitude / (self.breakout_ticks * 2))
        
        # Time remaining factor
        if time_left_minutes is None or time_left_minutes > 30:
            time_factor = 1.0
        else:
            time_factor = max(0.0, time_left_minutes / self.time_left_threshold)
        
        confidence = trend_factor * breakout_factor * time_factor
        return confidence
    
    def check_exit_conditions(
        self,
        position: Position,
        current_price: float
    ) -> tuple:
        """Check if we should exit a position.
        Returns (should_exit, reason, pnl_ticks)"""
        now = time.time()
        entry_price = position.entry_price
        
        # Calculate PnL in cents (assuming $1 token = 100 cents)
        pnl_cents = (current_price - entry_price) * 100
        pnl_ticks = pnl_cents  # 1 cent = 1 tick on $1 token
        
        # Check take profit (+8 ticks)
        if pnl_ticks >= self.tp_ticks:
            return True, f"TP:+{pnl_ticks:.1f}Ticks", pnl_ticks
        
        # Check stop loss (-3 cents)
        if pnl_ticks <= -self.sl_cents:
            return True, f"SL:{pnl_ticks:.1f}Ticks", pnl_ticks
        
        # Check trailing MA
        ma = self.get_ma(position.token_id)
        if ma is not None:
            if position.outcome == "YES" and current_price < ma:
                return True, f"TRAIL_MA:Price<MA", pnl_ticks
            elif position.outcome == "NO" and current_price > ma:
                return True, f"TRAIL_MA:Price>MA", pnl_ticks
        
        # Check max hold (45 minutes)
        hold_minutes = (now - position.entry_time) / 60
        if hold_minutes >= self.max_hold_minutes:
            return True, f"MAX_HOLD:{hold_minutes:.0f}min", pnl_ticks
        
        return False, "", pnl_ticks


class TrendStrategy:
    """
    1H Trend-Following Strategy for Polymarket.
    
    Monitors real-time price changes and trades in the direction of momentum.
    """
    
    def __init__(self, paper_engine=None, config: Dict = None, is_btc_1h_only: bool = False):
        self.paper_engine = paper_engine
        self.config = config or {}
        self.is_btc_1h_only = is_btc_1h_only
        
        # Initialize trend tracker
        self.tracker = TrendTracker(self.config)
        
        # Track markets by token
        self.token_to_market: Dict[str, Dict] = {}
        
        # Market metadata (for time-left parsing)
        self.market_metadata: Dict[str, Dict] = {}
        
        # REST polling state
        self._last_poll_time = 0
        self._poll_interval = config.get("TREND_POLL_INTERVAL", 10) if config else 10
        
        # Signal decision logging
        self._last_signal_log_time = 0
        
        # Market watch logging (throttled)
        self._last_watch_log_time = 0
        
        # WS health tracking - updated whenever WS sends price
        self._last_ws_update_ts = 0
        
        # Statistics
        self.signals_generated = 0
        self.trades_executed = 0
        self.decisions_log: List[TradeDecision] = []
        
        # Lock for thread safety
        self._lock = threading.Lock()
    
    def _is_1h_crypto_up_down(self, market_name: str) -> bool:
        """Check if market is BTC Up/Down crypto market.
        
        NOTE: Duration filtering (1H) is already done by market.py via start/end times.
        This method only checks for BTC + Up/Down format - trusting market.py's
        duration-based filtering (50-70 min) instead of guessing from title words.
        
        Filters for:
        - Crypto (BTC)
        - Up or Down format
        """
        if not market_name:
            return False
        
        name_lower = market_name.lower()
        
        # Must be crypto (BTC) - trust config for allowed assets
        allowed_assets = self.config.get("TREND_ASSETS", ["BTC"])
        is_crypto = any(asset.lower() in name_lower for asset in allowed_assets)
        if not is_crypto:
            return False
        
        # Must be Up or Down format
        return "up or down" in name_lower or "up/down" in name_lower
    
    def register_market(
        self,
        condition_id: str,
        yes_token_id: str,
        no_token_id: str,
        market_name: str = "",
        end_date: str = None,
        timeframe: str = "",
        yes_price: float = 0.5,
        no_price: float = 0.5
    ) -> bool:
        """Register a market with its token IDs and prices.
        Returns True if market was registered, False if filtered out."""
        
        # BTC_1H_ONLY mode: Trust market.py's filtering - accept all markets passed in
        # market.py already filters by BTC + Up/Down + duration 50-70 min
        if not self.is_btc_1h_only:
            # FULL mode: Apply additional filter
            if not self._is_1h_crypto_up_down(market_name):
                return False
        
        # Store metadata for time-left parsing (includes end_date!)
        self.market_metadata[condition_id] = {
            "title": market_name,
            "end_date": end_date,  # THIS IS CRITICAL FOR TIME-LEFT GATE
            "timeframe": timeframe or "1h",
            "yes_price": yes_price,
            "no_price": no_price,
            "entry_allowed": True,  # Will be computed dynamically at trade time
        }
        
        # Register both YES and NO tokens
        self.token_to_market[yes_token_id] = {
            "condition_id": condition_id,
            "yes_token_id": yes_token_id,
            "no_token_id": no_token_id,
            "outcome": "YES",
            "market_name": market_name,
            "price": yes_price,
        }
        self.token_to_market[no_token_id] = {
            "condition_id": condition_id,
            "yes_token_id": yes_token_id,
            "no_token_id": no_token_id,
            "outcome": "NO",
            "market_name": market_name,
            "price": no_price,
        }
        
        # Initialize price in tracker if valid
        if yes_price and yes_price > 0:
            self.tracker.update_price(yes_token_id, yes_price, "init")
        if no_price and no_price > 0:
            self.tracker.update_price(no_token_id, no_price, "init")
        
        return True
    
    def on_price_update(self, token_id: str, price: float, source: str = "ws"):
        """Handle incoming price update (from WebSocket).
        
        Note: WS price is the last_trade_price from CLOB, which is a trade-weighted
        price. This is appropriate for momentum signals.
        """
        # DEBUG: Heartbeat to diagnose price update flow
        # Print no more than once every 5 seconds
        now = time.time()
        last_debug = getattr(self, '_last_price_debug', 0)
        if now - last_debug > 5:
            buffer_len = len(self.tracker.price_buffers.get(token_id, []))
            print(f"[PRICE DEBUG] token={token_id[:20]}... price={price} buffer_len={buffer_len}")
            self._last_price_debug = now
        
        # CRITICAL: Track WS updates for health check
        if source == "ws":
            self._last_ws_update_ts = now
        
        self.tracker.update_price(token_id, price, source)
        self._process_signals(token_id)
    
    def poll_prices(self, market_service, source: str = "rest"):
        """Poll prices from REST API (fallback mode).
        
        For BTC_1H_ONLY mode: Uses prices from Gamma API (already in market_metadata).
        For FULL mode: Uses CLOB REST to get current prices.
        
        This ensures we have valid prices for trend following.
        """
        now = time.time()
        
        # Throttle polling
        if now - self._last_poll_time < self._poll_interval:
            return
        
        self._last_poll_time = now
        
        # Get prices for registered markets
        for token_id, market_info in self.token_to_market.items():
            try:
                # First, try to use stored price from Gamma API (already fetched at startup)
                outcome = market_info.get("outcome")
                stored_price = market_info.get("price", 0)
                
                price = None
                
                # Use stored Gamma API price if valid
                if stored_price and stored_price > 0:
                    price = stored_price
                
                # If BTC_1H_ONLY mode, prefer Gamma API prices (already fetched)
                # If FULL mode or Gamma price is 0, try CLOB REST
                if not price or price == 0:
                    if hasattr(market_service, 'client'):
                        try:
                            # Try direct token price fetch from CLOB
                            resp = market_service.client.get_markets(token=token_id)
                            if resp and len(resp) > 0:
                                price = float(resp[0].get("price", 0))
                        except:
                            pass
                
                # Update tracker if we have a valid price
                if price and price > 0:
                    self.tracker.update_price(token_id, price, source)
            except Exception as e:
                # Log but continue
                pass  # Suppress noisy logging
        
        # Get market status for BTC_1H_ONLY mode
        market_status = None
        now = time.time()
        if self.is_btc_1h_only and hasattr(market_service, '_hourly_markets'):
            hourly_markets = market_service._hourly_markets
            if hourly_markets:
                # CRITICAL: Prefer IN_WINDOW markets, then by hours_until
                # First get all active markets
                active = [m for m in hourly_markets if m.get('hours_until', -1) >= 0]
                # Then prioritize in_window
                in_window_markets = [m for m in active if m.get('in_window', False)]
                if in_window_markets:
                    # Use in_window market (already sorted by hours_until)
                    m = in_window_markets[0]
                    # Throttle log: only print every 30 seconds
                    if now - self._last_watch_log_time > 30:
                        print(f"[WATCH] Using IN_WINDOW market: {m.get('title', '')[:50]}... minutes_left={m.get('minutes_left')}")
                        self._last_watch_log_time = now
                elif active:
                    # Fall back to upcoming market
                    m = active[0]
                    # Throttle log: only print every 30 seconds
                    if now - self._last_watch_log_time > 30:
                        print(f"[WATCH] Using UPCOMING market: {m.get('title', '')[:50]}... minutes_to_start={m.get('minutes_to_start')}")
                        self._last_watch_log_time = now
                else:
                    m = None
                
                if m:
                    market_status = {
                        'in_window': m.get('in_window', False),
                        'accepting_orders': m.get('accepting_orders', False),
                        'minutes_left': m.get('minutes_left'),
                        'minutes_to_start': m.get('minutes_to_start'),
                    }
        
        # Process signals for all tokens
        for token_id in self.token_to_market:
            self._process_signals(token_id, market_status=market_status)
    
    def _process_signals(self, token_id: str, market_status: dict = None):
        """Process signals for a token. Called after price update.
        
        Args:
            token_id: The token to process
            market_status: Optional dict with in_window, accepting_orders, minutes_left
        """
        import time as time_module
        
        with self._lock:
            market = self.token_to_market.get(token_id)
            if not market:
                return
            
            # BTC_1H_ONLY: Check market window status first
            # Only evaluate when in_window=true AND accepting_orders=true AND minutes_left>=ENTRY_CUTOFF
            is_in_window = False
            if self.is_btc_1h_only and market_status:
                in_window = market_status.get('in_window', False)
                accepting_orders = market_status.get('accepting_orders', False)
                minutes_left = market_status.get('minutes_left')
                entry_cutoff = self.config.get('TREND_TIME_LEFT_THRESHOLD', 5)  # Default 5 min
                is_in_window = in_window and accepting_orders
                
                if not in_window:
                    # Market not in trading window - skip silently (reduce log spam)
                    return
                
                if not accepting_orders:
                    # Market not accepting orders - skip silently
                    return
                
                if minutes_left is not None and minutes_left < entry_cutoff:
                    # Too close to expiry - skip
                    return
            
            # Get current price for SIGNAL log
            current_price, price_time = self.tracker.last_prices.get(token_id, (None, None))
            
            # Get market info for SIGNAL log
            market_title = market.get("market_name", "")
            outcome = market.get("outcome", "")
            condition_id = market.get("condition_id", "")
            
            # Compute signal metrics for logging (even if we skip)
            trendiness = self.tracker.compute_trendiness(token_id)
            return_5min = self.tracker.compute_return_5min(token_id)
            rolling_high, rolling_low = self.tracker.get_rolling_high_low(token_id, minutes=10)
            
            # Get spread and liquidity if possible
            spread_pct = "N/A"
            liquidity = "N/A"
            
            # Get end_date for time left
            end_date = None
            if condition_id and condition_id in self.market_metadata:
                end_date = self.market_metadata[condition_id].get("end_date")
            time_left, time_source = self.tracker.parse_time_left(market_title, end_date)
            
            # Throttled SIGNAL log for in-window market (every 10 seconds)
            now = time_module.time()
            last_signal_log = getattr(self, '_last_signal_log_time', 0)
            if is_in_window and (now - last_signal_log) >= 10:
                self._last_signal_log_time = now
                
                # Determine decision and reason
                decision = "HOLD"
                reason = "unknown"
                
                # Check layers
                is_sane, sanity_reason = self.tracker.is_data_sane(token_id)
                if not is_sane:
                    decision = "HOLD"
                    reason = f"Layer0:{sanity_reason}"
                elif trendiness < self.tracker.trendiness_threshold:
                    decision = "HOLD"
                    reason = f"Layer1:trend={trendiness:.2f}<{self.tracker.trendiness_threshold}"
                elif rolling_high is None:
                    decision = "HOLD"
                    reason = "no_rolling_data"
                elif outcome == "YES" and current_price and rolling_high and current_price > rolling_high:
                    ticks_above = (current_price - rolling_high) * 100
                    if ticks_above >= self.tracker.breakout_ticks:
                        decision = "ENTER_LONG"
                        reason = f"breakout_yes"
                elif outcome == "NO" and current_price and rolling_low and current_price < rolling_low:
                    ticks_below = (rolling_low - current_price) * 100
                    if ticks_below >= self.tracker.breakout_ticks:
                        decision = "ENTER_SHORT"
                        reason = f"breakout_no"
                else:
                    # Check time left gate
                    if time_left is not None and time_left < self.tracker.time_left_threshold:
                        reason = f"time_left:{time_left:.1f}min"
                    else:
                        reason = f"no_breakout"
                
                # Format the log
                token_short = token_id[:12] + "..." if len(token_id) > 12 else token_id
                print(f"[SIGNAL] market={market_title[:30]}... token={token_short} mid={current_price:.4f if current_price else 0:.4f} "
                      f"lookback=600s move={return_5min*100:.2f}% trend={trendiness:.2f} spread={spread_pct} liquidity={liquidity} "
                      f"decision={decision} reason={reason}")
            
            # Layer 0: Data sanity check
            is_sane, sanity_reason = self.tracker.is_data_sane(token_id)
            if not is_sane:
                # In BTC_1H_ONLY mode, suppress LAYER0 logs when UPCOMING to reduce spam
                if not (self.is_btc_1h_only and market_status and not market_status.get('in_window', False)):
                    self._log_decision(
                        action="SKIP",
                        token_id=token_id,
                        market=market,
                        reason=f"LAYER0:{sanity_reason}",
                        trendiness=0.0,
                        breakout="N/A",
                        time_left=None,
                        confidence=0.0
                    )
                return
            
            # Get current price and time left
            current_price, _ = self.tracker.last_prices.get(token_id, (None, None))
            if current_price is None:
                return
            
            market_title = market.get("market_name", "")
            condition_id = market.get("condition_id", "")
            
            # Get end_date from stored market metadata (if available)
            end_date = None
            if condition_id and condition_id in self.market_metadata:
                end_date = self.market_metadata[condition_id].get("end_date")
            
            # Parse time left with end_date from metadata
            time_left, time_left_source = self.tracker.parse_time_left(market_title, end_date)
            
            # Time-left gate: don't enter if < 12 minutes remain
            if time_left is not None and time_left < self.tracker.time_left_threshold:
                self._log_decision(
                    action="SKIP",
                    token_id=token_id,
                    market=market,
                    reason=f"TIME_LEFT:{time_left:.1f}min[{time_left_source}]",
                    trendiness=0.0,
                    breakout="N/A",
                    time_left=time_left,
                    confidence=0.0,
                    time_left_source=time_left_source
                )
                return
            
            # Layer 1: Regime filter (trendiness)
            trendiness = self.tracker.compute_trendiness(token_id)
            if trendiness < self.tracker.trendiness_threshold:
                # None-safe formatting
                trend_str = f"{trendiness:.2f}" if trendiness is not None else "N/A"
                self._log_decision(
                    action="SKIP",
                    token_id=token_id,
                    market=market,
                    reason=f"LAYER1:Trendiness={trend_str}<{self.tracker.trendiness_threshold}",
                    trendiness=trendiness,
                    breakout="N/A",
                    time_left=time_left,
                    confidence=0.0
                )
                return
            
            # Layer 2: Entry trigger
            return_5min = self.tracker.compute_return_5min(token_id)
            rolling_high, rolling_low = self.tracker.get_rolling_high_low(token_id, minutes=10)
            
            if rolling_high is None or rolling_low is None:
                return
            
            outcome = market.get("outcome")
            is_breakout = False
            breakout_direction = "NONE"
            
            # Determine breakout status
            if outcome == "YES":
                # LONG: price breaks above 10-min high
                if current_price > rolling_high:
                    ticks_above = (current_price - rolling_high) * 100
                    if ticks_above >= self.tracker.breakout_ticks:
                        is_breakout = True
                        breakout_direction = "LONG"
            else:
                # SHORT: price breaks below 10-min low
                if current_price < rolling_low:
                    ticks_below = (rolling_low - current_price) * 100
                    if ticks_below >= self.tracker.breakout_ticks:
                        is_breakout = True
                        breakout_direction = "SHORT"
            
            # Check entry conditions
            can_enter = False
            entry_action = None
            
            if is_breakout:
                if outcome == "YES" and return_5min > self.tracker.return_threshold:
                    can_enter = True
                    entry_action = "ENTER_YES"
                elif outcome == "NO" and return_5min < -self.tracker.return_threshold:
                    can_enter = True
                    entry_action = "ENTER_NO"
            
            # Check cooldown
            if can_enter and not self.tracker.check_cooldown(token_id):
                can_enter = False
                self._log_decision(
                    action="SKIP",
                    token_id=token_id,
                    market=market,
                    reason="COOLDOWN",
                    trendiness=trendiness,
                    breakout=breakout_direction,
                    time_left=time_left,
                    confidence=0.0
                )
            
            if not can_enter:
                if is_breakout:
                    # Breakout but wrong direction
                    self._log_decision(
                        action="SKIP",
                        token_id=token_id,
                        market=market,
                        reason=f"RETURN_5min={return_5min:.3f}",
                        trendiness=trendiness,
                        breakout=breakout_direction,
                        time_left=time_left,
                        confidence=0.0
                    )
                return
            
            # Compute confidence
            breakout_magnitude = abs(current_price - (rolling_high if outcome == "YES" else rolling_low)) * 100
            confidence = self.tracker.compute_confidence(
                trendiness, breakout_magnitude, time_left
            )
            
            # Confidence threshold: do nothing when unsure
            if confidence < self.tracker.confidence_threshold:
                # None-safe formatting
                conf_str = f"{confidence:.2f}" if confidence is not None else "N/A"
                self._log_decision(
                    action="SKIP",
                    token_id=token_id,
                    market=market,
                    reason=f"CONFIDENCE={conf_str}<{self.tracker.confidence_threshold}",
                    trendiness=trendiness,
                    breakout=breakout_direction,
                    time_left=time_left,
                    confidence=confidence
                )
                return
            
            # Execute entry
            self._execute_entry(token_id, current_price, market, entry_action, confidence)
    
    def _execute_entry(
        self,
        token_id: str,
        price: float,
        market: Dict,
        action: str,
        confidence: float
    ):
        """Execute a paper trade entry."""
        outcome = market.get("outcome")
        condition_id = market.get("condition_id")
        market_name = market.get("market_name", "Unknown")
        
        # Get asset from market name
        asset = "BTC"
        if "ethereum" in market_name.lower() or "eth" in market_name.lower():
            asset = "ETH"
        elif "solana" in market_name.lower() or "sol" in market_name.lower():
            asset = "SOL"
        elif "xrp" in market_name.lower():
            asset = "XRP"
        
        # Get time_left with metadata
        end_date = None
        if condition_id and condition_id in self.market_metadata:
            end_date = self.market_metadata[condition_id].get("end_date")
        time_left, time_left_source = self.tracker.parse_time_left(market_name, end_date)
        
        # Log the decision (None-safe)
        conf_str = f"{confidence:.2f}" if confidence is not None else "N/A"
        self._log_decision(
            action=action,
            token_id=token_id,
            market=market,
            reason=f"ENTRY:Conf={conf_str}",
            trendiness=self.tracker.compute_trendiness(token_id),
            breakout="LONG" if outcome == "YES" else "SHORT",
            time_left=time_left,
            confidence=confidence,
            time_left_source=time_left_source
        )
        
        # SIGNAL CHAIN: [SIGNAL] -> [RISK] -> [EXEC] -> [PAPER]
        print(f"[SIGNAL] -> {action} confidence={confidence:.2f} price={price:.4f} market={condition_id[:16]}...")
        
        if self.paper_engine:
            try:
                # Build signal dict in the format execute_copy_trade expects
                signal = {
                    "condition_id": condition_id,
                    "token_id": token_id,
                    "outcome": outcome,
                    "whale_price": price,  # Our entry price
                    "market_title": market_name,
                    "score": confidence,  # Use confidence as score
                    "source_wallet": "TREND_BOT",  # Mark as trend signal
                    "source_username": "trend_strategy",
                    "usdc_value": self.config.get("MOMENTUM_SIZE", 5.0),
                }
                
                # RISK: Entry cutoff check - verify we can still enter
                entry_allowed, mins_left, cutoff = self.is_entry_allowed(condition_id)
                if not entry_allowed:
                    print(f"[RISK] BLOCKED entry_allowed=False minutes_left={mins_left:.0f} cutoff={cutoff}")
                    return None  # Skip this signal
                print(f"[RISK] OK entry_allowed=True minutes_left={mins_left:.0f} cutoff={cutoff}")
                
                # EXEC: Submit to paper engine
                print(f"[EXEC] submitting trade to paper_engine...")
                result = self.paper_engine.execute_copy_trade(
                    signal,
                    current_exposure=0.0
                )
                
                if not result or not result.get("success"):
                    reason = result.get("reason", "unknown") if result else "null_result"
                    print(f"[EXEC] BLOCKED reason={reason} market={condition_id[:20]}...")
                elif result and result.get("success"):
                    self.tracker.record_trade(token_id)
                    self.trades_executed += 1
                    
                    # Track open position for exit monitoring
                    position = Position(
                        condition_id=condition_id,
                        token_id=token_id,
                        outcome=outcome,
                        entry_price=price,
                        entry_time=time.time(),
                        size=self.config.get("MOMENTUM_SIZE", 5.0),
                        market_name=market_name
                    )
                    self.tracker.positions[condition_id] = position
                    
                    # PAPER: Confirm trade recorded
                    print(f"[PAPER] SUCCESS trade_id={result.get('trade_id', 'N/A')} market={condition_id[:16]}...")
                    
                    # None-safe formatting
                    price_str = f"${price:.2f}" if price is not None else "$0.00"
                    conf_str = f"{confidence:.2f}" if confidence is not None else "0.00"
                    print(f"[TREND] 🎯 {action} @ {price_str} "
                          f"confidence={conf_str} on {market_name[:40]}...")
                else:
                    reason = result.get("reason", "unknown") if result else "null_result"
                    print(f"[TREND] ⚠️ Trade rejected: {reason} - {market_name[:40]}...")
            except Exception as e:
                print(f"[TREND] Trade error: {e}")
    
    def _execute_paper_trade(self, market: Dict, outcome: str, price: float) -> Dict:
        """Execute a paper trade through the PaperTradingEngine.
        
        This properly applies:
        - Spread: Uses order book to determine fill price (not signal price)
        - Fees: Applies fee rates based on market type (crypto vs sports/politics)
        - Missed fills: simulate_fill returns filled=False if insufficient liquidity
        
        Returns dict with:
        - success: bool
        - reason: str (if failed)
        - fill_price: float (actual price including spread)
        - slippage: float
        - fee: float
        """
        if not self.paper_engine:
            print("[TREND] No paper engine - skipping trade")
            return None
        
        try:
            token_id = market.get("yes_token_id") if outcome == "YES" else market.get("no_token_id")
            condition_id = market.get("condition_id")
            market_name = market.get("market_name", "Unknown")
            
            # Get order book for this market to simulate realistic fill
            # This is critical for spread/slippage simulation
            book = None
            if hasattr(self, '_clob_orderbook'):
                # Try to get from CLOB WebSocket cache first
                book_snapshot = self._clob_orderbook.get_order_book_snapshot(token_id, depth=10)
                if book_snapshot and book_snapshot.get("asks") and book_snapshot.get("bids"):
                    book = {
                        "asks_yes": book_snapshot.get("asks", []),
                        "bids_yes": book_snapshot.get("bids", []),
                    }
            
            # If no CLOB book, we can't properly simulate - log warning
            if not book:
                print(f"[TREND] ⚠️ No order book for {token_id[:12]}... - cannot simulate fill accurately")
                # Return None to skip - we need book for realistic fills
                return None
            
            # Get fee rate based on market type
            yes_fee_bps = 100 if "bitcoin" in market_name.lower() or "ethereum" in market_name.lower() else 0
            
            # Use the paper engine's simulation
            from src.paper_fills import simulate_fill
            
            size = self.config.get("MOMENTUM_SIZE", 5.0)
            
            # Get the appropriate order book side for our outcome
            if outcome == "YES":
                order_book_side = book.get("asks_yes", [])
            else:
                order_book_side = book.get("asks_no", [])
            
            if not order_book_side:
                print(f"[TREND] ⚠️ No liquidity for {outcome} on {token_id[:12]}...")
                return {"success": False, "reason": "NO_LIQUIDITY"}
            
            # Simulate fill against order book (this applies spread/slippage)
            fill_result = simulate_fill(order_book_side, size)
            
            if not fill_result["filled"]:
                print(f"[TREND] ⚠️ Missed fill for {token_id[:12]}... - insufficient order book depth")
                return {"success": False, "reason": "MISSED_FILL", "slippage": 0}
            
            # Calculate fee
            from src.paper_fees import calculate_trading_fee
            fee = calculate_trading_fee(fill_result["fill_price"], fill_result["fill_size"], yes_fee_bps)
            
            # None-safe formatting
            price_str = f"${price:.3f}" if price is not None else "$0.000"
            print(f"[TREND] 📝 Paper Trade: {outcome} @ signal={price_str} -> fill=${fill_result['fill_price']:.3f} "
                  f"(spread=${abs(fill_result['fill_price']-(price or 0)):.4f}) fee=${fee:.4f}")
            
            # Return success - actual position recording happens in paper_engine
            return {
                "success": True,
                "outcome": outcome,
                "price": price,
                "fill_price": fill_result["fill_price"],
                "slippage": fill_result["slippage"],
                "fee": fee,
                "size": fill_result["fill_size"],
            }
            
        except Exception as e:
            print(f"[TREND] Paper trade error: {e}")
            return None
    
    def check_exits(self):
        """Check exit conditions for all open positions.
        Called periodically from main loop."""
        for condition_id, position in list(self.tracker.positions.items()):
            current_price, _ = self.tracker.last_prices.get(position.token_id, (None, None))
            if current_price is None:
                continue
            
            should_exit, reason, pnl = self.tracker.check_exit_conditions(position, current_price)
            
            if should_exit:
                self._execute_exit(position, current_price, reason, pnl)
    
    def _execute_exit(
        self,
        position: Position,
        current_price: float,
        reason: str,
        pnl_ticks: float
    ):
        """Execute a paper trade exit through paper_engine."""
        
        # Use paper_engine to close position (records in trade_history)
        if self.paper_engine:
            try:
                signal = {
                    "condition_id": position.condition_id,
                    "token_id": position.token_id,
                    "outcome": position.outcome,
                    "whale_price": current_price,  # Exit price
                    "market_title": position.market_name,
                    "source_username": "trend_strategy",
                    "exit_reason": reason,
                }
                
                result = self.paper_engine.close_copy_position(signal, risk_guard=None)
                
                if result and result.get("success"):
                    print(f"[TREND] ✅ Exit recorded in paper_engine")
                else:
                    print(f"[TREND] ⚠️ Exit not recorded: {result.get('reason', 'unknown') if result else 'null'}")
            except Exception as e:
                print(f"[TREND] Exit error: {e}")
        
        # Log the exit
        self._log_decision(
            action="EXIT",
            token_id=position.token_id,
            market={
                "condition_id": position.condition_id,
                "market_name": position.market_name,
                "outcome": position.outcome
            },
            reason=f"{reason}:PnL={pnl_ticks:.1f}Ticks" if pnl_ticks is not None else f"{reason}:PnL=N/A",
            trendiness=0.0,
            breakout="N/A",
            time_left=None,
            confidence=1.0
        )
        
        # Remove position from tracker
        del self.tracker.positions[position.condition_id]
        
        # None-safe formatting
        price_str = f"${current_price:.2f}" if current_price is not None else "$0.00"
        pnl_str = f"{pnl_ticks:.1f}" if pnl_ticks is not None else "N/A"
        print(f"[TREND] 🏁 EXIT {position.outcome} @ {price_str} "
              f"{reason} PnL={pnl_str} ticks on {position.market_name[:40]}...")
    
    def _log_decision(
        self,
        action: str,
        token_id: str,
        market: Dict,
        reason: str,
        trendiness: float,
        breakout: str,
        time_left: Optional[float],
        confidence: float,
        time_left_source: str = "none"
    ):
        """Log a trade decision for observability."""
        # Get asset
        market_name = market.get("market_name", "")
        asset = "BTC"
        if "ethereum" in market_name.lower() or "eth" in market_name.lower():
            asset = "ETH"
        elif "solana" in market_name.lower() or "sol" in market_name.lower():
            asset = "SOL"
        elif "xrp" in market_name.lower():
            asset = "XRP"
        
        # Get price
        price, _ = self.tracker.last_prices.get(token_id, (0.0, 0.0))
        
        # Verify tick size from price history (inferred from smallest non-zero delta)
        tick_size = self._verify_tick_size(token_id)
        
        decision = TradeDecision(
            action=action,
            asset=asset,
            timeframe="1H",
            market_title=market_name,
            token_id=token_id,
            last_price=price,
            trendiness=trendiness,
            breakout_status=breakout,
            time_left_minutes=time_left or -1.0,
            confidence=confidence,
            reason=reason,
            time_left_source=time_left_source,
            tick_size=tick_size
        )
        
        self.decisions_log.append(decision)
        
        # Only log to console for actual trade decisions, not for every SKIP
        # This reduces spam significantly
        if action in ("ENTER_YES", "ENTER_NO", "EXIT"):
            time_left_str = f"{time_left:.1f}" if time_left is not None else "N/A"
            price_str = f"${price:.2f}" if price is not None else "$0.00"
            trend_str = f"{trendiness:.2f}" if trendiness is not None else "0.00"
            conf_str = f"{confidence:.2f}" if confidence is not None else "0.00"
            
            print(f"[TREND] {action} | {asset} | 1H | {market_name[:30]}... | "
                  f"price={price_str} | trend={trend_str} | breakout={breakout} | "
                  f"time_left={time_left_str}min[{time_left_source}] | conf={conf_str} | "
                  f"tick=${tick_size:.4f} | {reason}")
    
    def _verify_tick_size(self, token_id: str) -> float:
        """Verify tick size from observed price deltas.
        
        Polymarket 1H Up/Down crypto markets typically trade in $0.01 (1 cent) ticks.
        This method infers tick size from the smallest observed price delta.
        
        Returns: tick_size in dollars (default 0.01)
        """
        DEFAULT_TICK = 0.01
        
        buffer = list(self.tracker.price_buffers.get(token_id, []))
        if len(buffer) < 10:
            return DEFAULT_TICK
        
        # Get prices in order
        prices = [p.price for p in buffer]
        if len(prices) < 2:
            return DEFAULT_TICK
        
        # Calculate all deltas
        deltas = sorted(set(abs(prices[i] - prices[i-1]) for i in range(1, len(prices))))
        
        if not deltas:
            return DEFAULT_TICK
        
        # Smallest non-zero delta is our inferred tick size
        smallest_delta = deltas[0]
        
        # Verify it's close to expected tick size ($0.01)
        if abs(smallest_delta - DEFAULT_TICK) > 0.005:
            # Tick size is NOT $0.01 - log warning but use observed
            print(f"[TREND] ⚠️ Unusual tick size: observed={smallest_delta:.4f}, expected={DEFAULT_TICK}")
            return smallest_delta
        
        return DEFAULT_TICK
    
    def get_stats(self) -> Dict:
        """Get strategy statistics."""
        return {
            "signals_generated": self.signals_generated,
            "trades_executed": self.trades_executed,
            "decisions_logged": len(self.decisions_log),
            "open_positions": len(self.tracker.positions),
        }


# Backward compatibility alias
MomentumStrategy = TrendStrategy
