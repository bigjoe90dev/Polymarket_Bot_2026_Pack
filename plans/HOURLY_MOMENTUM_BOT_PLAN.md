# Hourly BTC Momentum Bot - Comprehensive Implementation Plan

## Executive Summary

The bot currently:
- ✅ Discovers 11 valid 1H BTC Up/Down markets
- ✅ Connects to CLOB WebSocket and receives 45,000+ messages/minute
- ❌ Does NOT execute momentum trades (stuck at "INSUFFICIENT_HISTORY")
- ❌ Has no market rotation logic
- ❌ Has extraneous whale/copy trading code

## Issues Found

### 1. Critical: Price History Not Building
**Symptom:** `[TREND] ⚠️ Unusual tick size: observed=0.0000, expected=0.01`

**Root Cause:** The WebSocket is receiving price data but the momentum strategy's `on_price_update` is not being called with the correct price values. The price updates are coming through but not being stored in the price buffers.

**Fix Required:** 
- Trace the price update flow from WebSocket → momentum_strategy
- Ensure `on_price_update` is called with the correct price
- Verify price buffers are being populated

### 2. Critical: No Market Rotation Brain
**Symptom:** Bot tracks same market forever, doesn't know when to switch

**Root Cause:** No state machine controlling the hourly market lifecycle

**Required Logic:**
```
State Machine:
  WAITING_FOR_NEXT → (market found in window) → TRADING
  TRADING → (15 min before end) → STOP_NEW_ENTRIES  
  TRADING → (market resolved) → FLATTEN_AND_ROTATE
  FLATTEN_AND_ROTATE → (positions closed) → WAITING_FOR_NEXT
```

### 3. High: Extraneous Code
The bot includes:
- Whale tracking (not needed)
- Copy trading (not needed)
- Arbitrage scanning (not needed)
- Blockchain monitoring (not needed)
- Parity checker (causing errors)

**Fix:** Disable or remove these features in BTC_1H_ONLY mode

### 4. Medium: Logging Spam
- "Unusual tick size" warning repeated 21+ times per cycle
- PARITY matching error spam
- CLOB message count spam

**Fix:** Reduce logging verbosity

---

## Implementation Plan

### Phase 1: Fix Price Updates (Day 1)

#### Task 1.1: Trace Price Update Flow
- [ ] Add debug logging in `momentum_strategy.on_price_update()`
- [ ] Verify WebSocket calls `on_price_update` with correct price
- [ ] Check price buffers are populated after 1 minute

#### Task 1.2: Fix Price Parsing
- [ ] Check WebSocket price_change format
- [ ] Ensure price is extracted correctly from JSON
- [ ] Verify tick size detection works

#### Task 1.3: Verify 15-Minute History
- [ ] After 15 min, check `is_data_sane()` returns True
- [ ] Verify trendiness calculation works

### Phase 2: Implement Market Rotation (Day 2)

#### Task 2.1: Create Market Rotation Controller
New file: `src/market_rotation.py`
```python
class MarketRotation:
    def __init__(self, config):
        self.state = "WAITING_FOR_NEXT"  # WAITING_FOR_NEXT, TRADING, STOP_NEW_ENTRIES, FLATTEN_AND_ROTATE
        self.current_market = None
        self.current_window_start = None
        self.current_window_end = None
        
    def get_current_market(self, markets):
        """Find the current active hourly market"""
        # Filter to BTC 1H Up/Down markets that are in window
        # Return the one with most time left
        
    def should_stop_new_entries(self):
        """Check if we should stop entering new positions"""
        # Stop 15 minutes before market ends
        
    def should_flatten(self):
        """Check if we should close all positions"""
        # Close 5 minutes before market ends or when resolved
        
    def should_rotate(self):
        """Check if we should switch to next market"""
        # Rotate when current market is resolved
```

#### Task 2.2: Integrate with Bot
- [ ] Replace current market tracking with MarketRotation
- [ ] Add state transitions to main loop
- [ ] Ensure clean rotation between markets

### Phase 3: Clean Up Extraneous Code (Day 3)

#### Task 3.1: Disable Whale/Copy Trading
In `config/config.json`:
```json
{
  "USE_WHALE_TRACKING": false,
  "USE_BLOCKCHAIN_MONITOR": false,
  "ENABLE_ARB_SCANNER": false,
  "USE_CLOB_WEBSOCKET": true  // Keep for momentum
}
```

#### Task 3.2: Fix Parity Error
- [ ] Check why parity.checker is None
- [ ] Either initialize it properly or disable in BTC_1H_ONLY mode

#### Task 3.3: Reduce Logging
- [ ] Remove "Unusual tick size" spam
- [ ] Remove CLOB message count spam
- [ ] Keep only: market status, trade entries/exits, errors

### Phase 4: Define Momentum Signal (Day 4)

#### Task 4.1: Simple Momentum Signal
Current signal is too complex. Replace with:
```python
def check_momentum_signal(self, token_id, price_history):
    """Simple momentum: price moved X% in Y seconds"""
    
    # Entry: price moved > 1% in last 5 minutes AND
    #        volume > threshold AND
    #        not within 15 min of market close
    
    # Exit: price moved > 2% against position OR
    #       within 5 min of market close
```

#### Task 4.2: Position Sizing
- [ ] Calculate position size based on portfolio
- [ ] Risk no more than 2% per trade
- [ ] Max 5% total exposure per market

### Phase 5: Testing & Production Readiness (Day 5)

#### Task 5.1: Paper Trading Test
- [ ] Run for 1 hour with real markets
- [ ] Verify trades execute
- [ ] Verify P&L tracking works

#### Task 5.2: Dashboard Updates
- [ ] Show current market and state
- [ ] Show time until rotation
- [ ] Show open positions with P&L

#### Task 5.3: Error Handling
- [ ] WebSocket disconnect recovery
- [ ] Market resolution detection
- [ ] Clean shutdown

---

## Files to Modify

| File | Changes |
|------|---------|
| `src/momentum_strategy.py` | Fix price update handling, simplify signal |
| `src/bot.py` | Integrate MarketRotation, disable whale features |
| `src/clob_websocket.py` | Reduce logging spam |
| `src/market.py` | (Already working) |
| `src/web_server.py` | Add rotation state to dashboard |
| `config/config.json` | Disable extraneous features |
| `src/market_rotation.py` | **NEW** - Market rotation brain |

---

## Success Criteria

1. Bot discovers current hourly BTC market within 1 minute of startup
2. Bot builds 15-minute price history and starts trading
3. Bot executes momentum trades during the hour
4. Bot rotates to next market when current resolves
5. Dashboard shows current market, state, and positions
6. Console output is clean and readable
7. No extraneous features running (whale, arb, blockchain)

---

## Timeline

- **Day 1:** Fix price updates, verify history builds
- **Day 2:** Implement market rotation brain
- **Day 3:** Clean up extraneous code
- **Day 4:** Define simple momentum signal
- **Day 5:** Test and productionize

Total: 5 days for full implementation