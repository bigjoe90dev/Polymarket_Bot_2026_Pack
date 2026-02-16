"""
CLOB Momentum Strategy
======================
Monitors CLOB price streams for momentum patterns and executes trades.

Strategy: Price going UP → Buy YES (bet on continuation)
          Price going DOWN → Buy NO (bet on reversal)

Focus: Bitcoin/Ethereum "Up or Down" markets (high frequency, short duration)
"""

import time
import asyncio
from collections import defaultdict
from typing import Dict, List, Optional, Any


class MomentumTracker:
    """Track price momentum for each token."""
    
    def __init__(self, lookback_seconds: int = 60, min_momentum: float = 0.02):
        """
        Args:
            lookback_seconds: How far back to track price history
            min_momentum: Minimum price change to trigger trade (2% default)
        """
        self.lookback_seconds = lookback_seconds
        self.min_momentum = min_momentum
        
        # Price history: token_id -> [(timestamp, price), ...]
        self.price_history: Dict[str, List[tuple]] = defaultdict(list)
        
        # Last triggered trade per token (prevent over-trading)
        self.last_trade_time: Dict[str, float] = {}
        self.trade_cooldown = 30  # seconds between trades on same token
        
    def update_price(self, token_id: str, price: float, timestamp: float = None):
        """Update price history for a token."""
        if timestamp is None:
            timestamp = time.time()
            
        self.price_history[token_id].append((timestamp, price))
        
        # Clean old prices
        cutoff = timestamp - self.lookback_seconds
        self.price_history[token_id] = [
            (t, p) for t, p in self.price_history[token_id] if t > cutoff
        ]
        
    def get_momentum(self, token_id: str) -> Optional[float]:
        """Calculate momentum as percentage change from earliest to latest price."""
        history = self.price_history.get(token_id, [])
        if len(history) < 2:
            return None
            
        # Get price range
        prices = [p for _, p in history]
        earliest = prices[0]
        latest = prices[-1]
        
        if earliest == 0:
            return None
            
        momentum = (latest - earliest) / earliest
        return momentum
    
    def should_trade(self, token_id: str) -> bool:
        """Check if we should trade this token (cooldown)."""
        now = time.time()
        last_trade = self.last_trade_time.get(token_id, 0)
        return (now - last_trade) > self.trade_cooldown
    
    def record_trade(self, token_id: str):
        """Record that we traded this token."""
        self.last_trade_time[token_id] = time.time()


class MomentumStrategy:
    """
    CLOB-based momentum trading strategy.
    
    Monitors real-time price changes and trades in the direction of momentum.
    """
    
    def __init__(
        self,
        paper_engine=None,
        lookback_seconds: int = 60,
        min_momentum: float = 0.02,
        position_size: float = 5.0,
    ):
        self.paper_engine = paper_engine
        self.tracker = MomentumTracker(lookback_seconds, min_momentum)
        self.position_size = position_size
        
        # Track markets by token (need to know YES vs NO for each market)
        self.token_to_market: Dict[str, Dict] = {}
        
        # Statistics
        self.trades_executed = 0
        self.signals_generated = 0
        
    def _is_crypto_up_down(self, market_name: str) -> bool:
        """Check if market is Bitcoin/Ethereum Up or Down 5min/15min (ACTIVE markets only)."""
        if not market_name:
            return False
        name_lower = market_name.lower()
        
        # Must be crypto (Bitcoin or Ethereum)
        is_crypto = "bitcoin" in name_lower or "btc" in name_lower or "ethereum" in name_lower or "eth" in name_lower
        if not is_crypto:
            return False
            
        # Must be Up or Down format
        is_up_down = "up or down" in name_lower
        if not is_up_down:
            return False
            
        # MUST have 5 min or 15 min timeframe (only active short-duration markets)
        has_short_timeframe = "5 min" in name_lower or "15 min" in name_lower or "5min" in name_lower or "15min" in name_lower
        if not has_short_timeframe:
            return False
        
        return True
        
    def register_market(self, condition_id: str, yes_token_id: str, no_token_id: str,
                        market_name: str = "") -> bool:
        """Register a market with its token IDs - ONLY crypto Up/Down markets.
        Returns True if market was registered, False if filtered out."""
        # Filter: Only register Bitcoin/Ethereum 5min/15min Up or Down markets
        if not self._is_crypto_up_down(market_name):
            return False  # Skip non-crypto or non-Up/Down markets
            
        self.token_to_market[yes_token_id] = {
            "condition_id": condition_id,
            "yes_token_id": yes_token_id,
            "no_token_id": no_token_id,
            "outcome": "YES",
            "market_name": market_name,
        }
        self.token_to_market[no_token_id] = {
            "condition_id": condition_id,
            "yes_token_id": yes_token_id,
            "no_token_id": no_token_id,
            "outcome": "NO",
            "market_name": market_name,
        }
        
        return True
        
    def on_price_update(self, token_id: str, price: float):
        """Handle incoming price update from CLOB."""
        self.tracker.update_price(token_id, price)
        
        # Check for momentum signal
        momentum = self.tracker.get_momentum(token_id)
        if momentum is None:
            return None
            
        self.signals_generated += 1
        
        # Check if momentum is strong enough
        if abs(momentum) < self.tracker.min_momentum:
            return None
            
        # Check cooldown
        if not self.tracker.should_trade(token_id):
            return None
            
        # Get market info
        market = self.token_to_market.get(token_id)
        if not market:
            return None
            
        # Determine trade direction
        if momentum > 0 and market.get("outcome") == "YES":
            # Price going up, buy YES
            direction = "BUY"
            outcome = "YES"
            signal_price = price
        elif momentum < 0 and market.get("outcome") == "NO":
            # Price going down, buy NO
            direction = "BUY"
            outcome = "NO"
            signal_price = price
        else:
            return None
            
        # Execute paper trade
        if self.paper_engine:
            result = self._execute_paper_trade(market, outcome, signal_price)
            if result:
                self.tracker.record_trade(token_id)
                self.trades_executed += 1
                return {
                    "direction": direction,
                    "outcome": outcome,
                    "price": signal_price,
                    "momentum": momentum,
                    "market": market.get("market_name", ""),
                }
                
        return None
        
    def _execute_paper_trade(self, market: Dict, outcome: str, price: float) -> Dict:
        """Execute a paper trade through the paper engine."""
        try:
            # Get market info
            token_id = market.get("yes_token_id") if outcome == "YES" else market.get("no_token_id")
            condition_id = market.get("condition_id")
            market_name = market.get("market_name", "Unknown")
            momentum = self.tracker.get_momentum(token_id)
            
            if self.paper_engine:
                # Try to use paper engine's method
                try:
                    # Get current order book from CLOB
                    if hasattr(self, '_clob_orderbook'):
                        book = self._clob_orderbook.get_order_book_snapshot(token_id)
                        if book:
                            # Try to fill
                            print(f"[MOMENTUM] 🎯 Signal: {outcome} @ ${price:.2f} "
                                  f"momentum={momentum:.1%} on {market_name[:30]}... (book available)")
                            # For now, just log - full execution needs more integration
                            return {"signal": True, "outcome": outcome, "price": price}
                except Exception as e:
                    pass
            
            # Log the signal (main functionality working)
            print(f"[MOMENTUM] 🎯 Signal: {outcome} @ ${price:.2f} "
                  f"momentum={momentum:.1%} on {market_name[:30]}...")
            
            return {"signal": True, "outcome": outcome, "price": price}
            
        except Exception as e:
            print(f"[MOMENTUM] Trade error: {e}")
            return None
            
    def get_stats(self) -> Dict:
        """Get strategy statistics."""
        return {
            "signals_generated": self.signals_generated,
            "trades_executed": self.trades_executed,
            "conversion_rate": self.trades_executed / max(1, self.signals_generated),
        }


# Standalone test
if __name__ == "__main__":
    strategy = MomentumStrategy(
        lookback_seconds=30,
        min_momentum=0.03,
        position_size=5.0,
    )
    
    # Register test market
    strategy.register_market(
        "test_btc_up",
        "0x123...yes",
        "0x123...no",
        "Bitcoin Up or Down - Test"
    )
    
    # Simulate price updates
    import random
    price = 0.50
    
    for i in range(20):
        # Simulate upward momentum
        price += random.uniform(-0.01, 0.02)
        price = max(0.01, min(0.99, price))
        
        result = strategy.on_price_update("0x123...yes", price)
        if result:
            print(f"Trade executed: {result}")
            
        time.sleep(0.5)
        
    print(f"\nStats: {strategy.get_stats()}")
