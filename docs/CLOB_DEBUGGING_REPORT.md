# Polymarket Bot CLOB Debugging Session Report

## Executive Summary

This document covers the debugging session for the Polymarket CLOB WebSocket implementation, focusing on the parsing errors in the message handler and the fundamental differences between public and authenticated WebSocket channels.

---

## 1. Original Issue

### Problem Description
The CLOB WebSocket was connecting successfully to Polymarket's WebSocket endpoint but experiencing parsing errors when handling incoming messages:

```
[CLOB] Message handling error: could not convert string to float: 'price'
```

### Location
- **File**: [`src/clob_websocket.py`](src/clob_websocket.py)
- **Method**: [`_handle_message()`](src/clob_websocket.py:433) (originally line 219)
- **Error**: The code was attempting to convert a string value to float without proper validation

---

## 2. Error Analysis

### Root Cause
The Polymarket WebSocket sends messages in a format that differs from what the original code expected. The error occurred in the [`_handle_price_update()`](src/clob_websocket.py:503) method when trying to parse price and size fields.

### Original Problematic Code
```python
async def _handle_price_update(self, data: Dict):
    """Handle price level update."""
    asset_id = data.get("asset_id", "")
    price = data.get("price", 0)
    size = data.get("size", 0)
    side = data.get("side", "")
    
    if not asset_id:
        return
    
    # This fails when price/size are strings
    try:
        price = float(price)
        size = float(size)
    except (ValueError, TypeError):
        return
```

### Fields Attempted
The code tried multiple field name variations:
- `price`, `filled_price`, `execution_price`, `avg_price`
- `size`, `amount`, `quantity`, `filled_amount`
- `asset_id`, `assetId`, `market`

---

## 3. Debug Logging Approach

### Implementation
Added debug logging to capture the first few raw messages to understand the actual Polymarket format:

```python
# In _handle_message() at line ~435
async def _handle_message(self, message: str):
    """Process incoming WebSocket message and update local order book."""
    self.messages_received += 1
    self.last_message_time = time.time()
    
    # Debug: log first few messages to understand format
    if self.messages_received <= 3:
        print(f"[CLOB] DEBUG raw message: {message[:300]}")
    
    try:
        data = json.loads(message)
        
        # Debug: log first few parsed messages
        if self.messages_received <= 3:
            print(f"[CLOB] DEBUG parsed: {data}")
```

### What We Learned
- The WebSocket IS receiving messages (300+ processed)
- The format differs from expected
- Field values may be strings instead of numbers

---

## 4. Key Observations

### Observation 1: Messages Being Processed
```
[CLOB] Messages processed: 100
[CLOB] Messages processed: 200
[CLOB] Messages processed: 300
```

### Observation 2: Leaderboard Signals vs CLOB Signals

The bot was receiving whale signals from two sources:

1. **Leaderboard** (working):
```
[WHALE] SIGNAL [LB]: dawdawdvf BUY Up @0.50 $6514 on "Bitcoin Up or Down"
```

2. **Blockchain** (working):
```
[BLOCKCHAIN] Whale trade: 0xb72665Ae... bought YES at 2.5000 in "Unknown"
```

3. **CLOB** (not emitting signals): No `[CLOB] 🔥 LARGE TRADE` messages appearing

### Observation 3: Gamma API Enrichment Failure

```
[CLOB] Enriched 0/1000 markets with clobTokenIds
```

The Gamma API integration wasn't returning the expected `clobTokenIds`, preventing proper market matching.

---

## 5. Channel Distinction (Critical Finding)

### Public Market Channel (`market`)
- **Endpoint**: `wss://ws-subscriptions-clob.polymarket.com/ws/market`
- **Authentication**: NOT required
- **Data**: Order book updates (bids/asks changes)
- **Trade Detection**: ❌ NO - only book changes, not actual fills

### Authenticated User Channel (`user`)
- **Authentication**: REQUIRED (apiKey, secret, passphrase)
- **Data**: Actual trade executions/fills
- **Trade Detection**: ✅ YES - real-time trade data

### Documentation Reference
Per the Polymarket documentation provided:
- "The Market WebSocket is public"
- "`market` — orderbook and price updates (public)"
- "`user` — user/order updates (auth required)"
- "Only the `user` WS channel requires authentication"

---

## 6. Solution Attempts

### Attempt 1: Fix Price Parsing
Updated [`_handle_price_update()`](src/clob_websocket.py:503) to handle string-to-float conversion:

```python
async def _handle_price_update(self, data: Dict):
    """Handle price level update."""
    # Handle various Polymarket message formats
    asset_id = data.get("asset_id", "") or data.get("assetId", "") or data.get("market", "")
    price = data.get("price", 0)
    size = data.get("size", 0) or data.get("qty", 0)
    side = data.get("side", "")
    
    if not asset_id:
        return
    
    # Validate price and size are actually numeric before converting
    if isinstance(price, str):
        try:
            price = float(price)
        except (ValueError, TypeError):
            return
    
    if isinstance(size, str):
        try:
            size = float(size)
        except (ValueError, TypeError):
            return
    
    if not price or not size:
        return
```

### Attempt 2: Lower Trade Threshold
Changed from $50 to $10 minimum trade value:

```python
# In _handle_trade() at line ~555
# Check if this is a significant trade (potential whale)
# For paper trading with public channel: detect large trades ($50+) as opportunities
# This allows copying ANY large trader, not just tracked whales
trade_value = price * size if price > 0 and size > 0 else 0

if trade_value >= 50:  # Minimum $50 trade value threshold
    await self._on_potential_whale_trade(asset_id, price, size, side)
```

Changed to:
```python
if trade_value >= 10:  # $10 minimum
    await self._on_potential_whale_trade(asset_id, price, size, side)
```

### Attempt 3: Emit Signals for Any Market Update
Modified to emit signals for ANY market update rather than just large trades:

```python
# The public market channel sends ORDER BOOK updates, not actual trades.
# To get trade signals, we need to detect significant price changes or use order flow.
# For now: emit signals for ANY significant book movement (price changes).

# Check for significant price update (could indicate whale activity)
if price > 0 and size > 0:
    trade_value = price * size
    # Lower threshold to catch more opportunities
    if trade_value >= 10:  # $10 minimum
        await self._on_potential_whale_trade(asset_id, price, size, side)
```

---

## 7. Remaining Issues

### Issue 1: Gamma API Not Returning clobTokenIds
```
[CLOB] Enriched 0/1000 markets with clobTokenIds
```

**Impact**: Can't properly match trades to specific markets

**Code Location**: [`_fetch_clob_token_ids_from_gamma()`](src/clob_websocket.py:265)

### Issue 2: Public Channel Limitation
The public `market` channel doesn't provide actual trade executions - only order book updates.

**Impact**: Cannot detect real-time whale trades with 100-300ms latency using public channel alone

**Solution**: Would need authenticated `user` channel (requires API credentials)

---

## 8. Current Implementation Status

### Working Components:
- ✅ CLOB WebSocket connection
- ✅ Message reception (300+ messages)
- ✅ Local L2 order book maintenance
- ✅ Leaderboard whale detection
- ✅ Blockchain whale detection

### Non-Working Components:
- ❌ CLOB trade signal emission (public channel issue)
- ❌ Gamma API clobTokenIds enrichment

### Files Modified:
- [`src/clob_websocket.py`](src/clob_websocket.py) - Complete rewrite with:
  - [`LocalOrderBook`](src/clob_websocket.py:42) class (lines 42-158)
  - [`_fetch_clob_token_ids_from_gamma()`](src/clob_websocket.py:265) method
  - [`_subscribe_to_markets()`](src/clob_websocket.py:385) - proper market channel subscription
  - [`_handle_price_update()`](src/clob_websocket.py:503) - robust message parsing

---

## 9. Next Steps

### Option A: Continue with Current Setup
- Use leaderboard + blockchain for whale detection
- Accept 2-3 second latency from blockchain
- No API keys required

### Option B: Implement Authenticated Channel
- Obtain CLOB API credentials (apiKey, secret, passphrase)
- Subscribe to `user` channel instead of `market`
- Achieve true 100-300ms trade detection
- Would require real trading (not paper)

---

## 10. Relevant Code References

### Configuration
- **File**: [`config/config.json`](config/config.json)
- **Key Settings**:
  ```json
  {
    "USE_CLOB_WEBSOCKET": true,
    "CLOB_WS_URL": "wss://ws-subscriptions-clob.polymarket.com/ws/market",
    "MODE": "PAPER"
  }
  ```

### Bot Integration
- **File**: [`src/bot.py`](src/bot.py:72)
- **Line**: 72 - CLOB WebSocket initialization
- **Line**: 183-194 - Signal processing

### Whale Tracker Integration
- **File**: [`src/whale_tracker.py`](src/whale_tracker.py:816)
- **Method**: [`add_clob_signal()`](src/whale_tracker.py:816) - receives CLOB signals

---

## Conclusion

The debugging session revealed that the CLOB WebSocket is functional but limited by the public channel's inability to provide actual trade executions. The parsing errors were addressed through more robust type conversion, but the fundamental limitation requires either:

1. Accepting the current 2-3 second latency from blockchain detection, OR
2. Implementing authenticated channel access for true 100-300ms latency

The bot is functional for paper trading with leaderboard and blockchain whale detection, but the "holy grail" of 100ms latency trade copying requires authenticated API access.
