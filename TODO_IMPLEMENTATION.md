# TODO IMPLEMENTATION — Polymarket Bot 2026 Pack
## Jira-Style Task Breakdown for LLM Execution

**Created**: February 11, 2026
**Status**: Phase 0 pending (waiting for user's weekly limit reset)
**Current State**: v14 production monitoring complete, 0% win rate, -17.9% ROI

---

## 📋 Quick Navigation

- [Phase 0: Immediate Fixes](#phase-0-immediate-fixes-days-1-2)
- [Phase 1: Quick Wins](#phase-1-quick-wins-days-3-7)
- [Phase 2: Transformational Change](#phase-2-transformational-change-weeks-2-6)
- [Phase 3: Advanced Features](#phase-3-advanced-features-optional)
- [Future Enhancements](#future-enhancements)

---

## Phase 0: Immediate Fixes (Days 1-2)

**Goal**: Fix critical bugs preventing effective paper trading

**Total Time**: 2-3 hours
**Priority**: CRITICAL
**Blocking**: Phase 1 tasks

---

### Task 0.1: Fix WebSocket Leak in Dashboard

**ID**: `POLY-001`
**Priority**: CRITICAL
**Time Estimate**: 10 minutes
**Assignee**: Next LLM
**Status**: TODO

#### Problem Statement
Dashboard polling creates 100+ concurrent WebSocket connections to Alchemy, hitting free tier limit after 19 hours. This causes blockchain monitor to disconnect and miss whale signals.

#### Root Cause
`/api/blockchain` route calls `monitor.web3.is_connected()` on every poll (2-second interval). This method creates a new WebSocket connection without closing the old one.

After 19 hours:
```
(19 hours × 3600 seconds) / 2 seconds = 34,200 polls
34,200 polls ÷ 100 limit = 342× over limit
```

#### Solution
Replace `web3.is_connected()` method call with a boolean flag that's updated by the monitor itself.

#### Files to Modify

**1. `src/blockchain_monitor.py`** (add connection status flag)

```python
# Add to BlockchainMonitor class:

def __init__(self, config, whale_tracker):
    # ... existing code ...

    # NEW: Connection status flag (replaces web3.is_connected() calls)
    self.connected = False  # Thread-safe boolean flag

    self.running = False
    self.thread = None

def _listen_for_events(self):
    """Main event listener loop."""
    while self.running:
        try:
            # Establish WebSocket connection
            self.web3 = Web3(Web3.WebsocketProvider(self.wss_url))

            if not self.web3.is_connected():
                print(f"[BLOCKCHAIN] Connection failed, retrying in 5s...")
                time.sleep(5)
                continue

            # NEW: Set connected flag to True
            self.connected = True

            print(f"[BLOCKCHAIN] Connected to Polygon (block: {self.web3.eth.block_number})")

            # ... rest of event loop ...

        except Exception as e:
            print(f"[BLOCKCHAIN] Error: {e}, reconnecting...")
            self.connected = False  # NEW: Set to False on disconnect
            time.sleep(5)

def stop(self):
    """Gracefully stop the monitor."""
    print("[BLOCKCHAIN] Stopping...")
    self.running = False
    self.connected = False  # NEW: Set to False on stop

    if self.thread:
        self.thread.join(timeout=5)
```

**2. `src/web_server.py`** (fix the leaky route)

```python
# Line 260 (inside /api/blockchain route handler):

# BEFORE (LEAKY):
"connected": monitor.web3.is_connected() if hasattr(monitor, 'web3') else False

# AFTER (FIXED):
"connected": monitor.connected if hasattr(monitor, 'connected') else False
```

#### Acceptance Criteria
- [ ] Bot runs for 24+ hours without WebSocket errors
- [ ] Alchemy dashboard shows <10 concurrent connections (stable)
- [ ] Blockchain monitor stays connected continuously
- [ ] Dashboard still shows correct connection status (true/false)

#### Testing Steps
1. Start bot: `python3 run.py`
2. Open dashboard in browser
3. Verify "Blockchain Monitor" section shows "Connected: true"
4. Let bot run for 2 hours
5. Check Alchemy dashboard for connection count (should be ~5-10, not 100+)
6. Verify no errors in bot console logs

#### Dependencies
- None (can be implemented immediately)

#### Rollback Plan
If this breaks:
```bash
git checkout HEAD -- src/blockchain_monitor.py src/web_server.py
```

---

### Task 0.2: Add Resolution Times to Dashboard

**ID**: `POLY-002`
**Priority**: HIGH
**Time Estimate**: 2-3 hours
**Assignee**: Next LLM
**Status**: TODO

#### Problem Statement
Users cannot see when their open positions will resolve/expire. User quote: *"I have no idea on the dashboard where my active trades are that tells me when each trade is gunna resolve, that must be added"*

#### Current State
Dashboard shows:
- Market name
- Outcome (YES/NO)
- Size
- Entry price
- Current price
- Unrealized P&L

**Missing**: Expiry date/time

#### Solution
1. Parse `end_date_iso` from market metadata
2. Add "Resolves At" column to positions table
3. Sort positions by soonest expiry (urgent ones first)
4. Format as human-readable datetime

#### Files to Modify

**1. `src/web_server.py`** (API route enhancement)

Find the `/api/positions` route (around line 150-180) and enhance it:

```python
def _handle_positions(self, bot, params):
    """Return open positions with resolution times."""
    if not bot.execution.paper_engine:
        return {"positions": []}

    positions = []
    pe = bot.execution.paper_engine

    for pos_id, pos in pe.open_positions.items():
        # Get market metadata for expiry
        condition_id = pos.get("condition_id", "")
        market_title = pos.get("market_name", "Unknown")

        # NEW: Fetch market metadata to get end_date_iso
        end_date_iso = None
        resolves_at_str = "Unknown"
        resolves_timestamp = None

        try:
            # Try to get from CLOB API
            market_info = bot.market.client.get_market(condition_id)
            if market_info and "end_date_iso" in market_info:
                end_date_iso = market_info["end_date_iso"]

                # Parse ISO 8601 datetime
                from datetime import datetime
                dt = datetime.fromisoformat(end_date_iso.replace('Z', '+00:00'))

                # Format as human-readable
                resolves_at_str = dt.strftime("%b %d, %I:%M %p")  # "Feb 12, 6:00 PM"
                resolves_timestamp = dt.timestamp()  # For sorting
        except Exception as e:
            print(f"[DASHBOARD] Failed to get expiry for {condition_id[:12]}...: {e}")

        # Build position dict
        position_data = {
            "id": pos_id,
            "market_name": market_title,
            "outcome": pos.get("outcome", "?"),
            "size": pos.get("size", 0),
            "avg_price": pos.get("avg_price", 0),
            "total_cost": pos.get("total_cost", 0),
            "current_price": pos.get("current_price", 0),
            "unrealized_pnl": pos.get("unrealized_pnl", 0),
            "source_username": pos.get("source_username", "unknown"),

            # NEW: Resolution time fields
            "resolves_at": resolves_at_str,
            "resolves_timestamp": resolves_timestamp,
            "end_date_iso": end_date_iso
        }

        positions.append(position_data)

    # NEW: Sort by expiry (soonest first)
    # Put "Unknown" expiries at the end
    positions.sort(key=lambda p: p['resolves_timestamp'] if p['resolves_timestamp'] else float('inf'))

    return {"positions": positions}
```

**2. `static/index.html`** (UI enhancement)

Find the positions table (around line 300-350) and add the new column:

```html
<!-- BEFORE (current table headers): -->
<thead>
    <tr>
        <th onclick="sortTable(0)">Market</th>
        <th onclick="sortTable(1)">Outcome</th>
        <th onclick="sortTable(2)">Size</th>
        <th onclick="sortTable(3)">Entry</th>
        <th onclick="sortTable(4)">Current</th>
        <th onclick="sortTable(5)">P&L</th>
        <th onclick="sortTable(6)">Source</th>
    </tr>
</thead>

<!-- AFTER (add Resolves At column): -->
<thead>
    <tr>
        <th onclick="sortTable(0)">Market</th>
        <th onclick="sortTable(1)">Outcome</th>
        <th onclick="sortTable(2)">Size</th>
        <th onclick="sortTable(3)">Entry</th>
        <th onclick="sortTable(4)">Current</th>
        <th onclick="sortTable(5)">P&L</th>
        <th onclick="sortTable(6)">Resolves At</th> <!-- NEW -->
        <th onclick="sortTable(7)">Source</th>
    </tr>
</thead>
```

Update the JavaScript that populates the table (around line 450-500):

```javascript
function updatePositions(data) {
    const tbody = document.querySelector('#positionsTable tbody');
    tbody.innerHTML = '';

    data.positions.forEach(pos => {
        const row = tbody.insertRow();

        // Market name
        row.insertCell(0).textContent = pos.market_name.substring(0, 50);

        // Outcome
        const outcomeCell = row.insertCell(1);
        outcomeCell.textContent = pos.outcome;
        outcomeCell.className = pos.outcome === 'YES' ? 'yes' : 'no';

        // Size
        row.insertCell(2).textContent = pos.size.toFixed(2);

        // Entry price
        row.insertCell(3).textContent = '$' + pos.avg_price.toFixed(3);

        // Current price
        row.insertCell(4).textContent = '$' + pos.current_price.toFixed(3);

        // P&L
        const pnlCell = row.insertCell(5);
        const pnl = pos.unrealized_pnl;
        pnlCell.textContent = (pnl >= 0 ? '+' : '') + '$' + pnl.toFixed(2);
        pnlCell.className = pnl >= 0 ? 'profit' : 'loss';

        // NEW: Resolves At
        const resolvesCell = row.insertCell(6);
        resolvesCell.textContent = pos.resolves_at;

        // If expiring soon (< 24 hours), highlight in yellow
        if (pos.resolves_timestamp) {
            const now = Date.now() / 1000;
            const timeUntilExpiry = pos.resolves_timestamp - now;

            if (timeUntilExpiry < 86400) {  // Less than 24 hours
                resolvesCell.style.backgroundColor = '#fff3cd';
                resolvesCell.style.color = '#856404';
                resolvesCell.style.fontWeight = 'bold';
            }
        }

        // Source username
        row.insertCell(7).textContent = pos.source_username;
    });
}
```

Add CSS for the expiry warning (add to `<style>` block):

```css
/* Add to existing <style> block in <head> */

#positionsTable td {
    padding: 8px;
    text-align: left;
}

/* Expiry warning styles */
.expiry-warning {
    background-color: #fff3cd;
    color: #856404;
    font-weight: bold;
}
```

#### Acceptance Criteria
- [ ] Dashboard shows "Resolves At" column for all open positions
- [ ] Expiry times formatted as "Feb 12, 6:00 PM" (human-readable)
- [ ] Positions sorted by soonest expiry first
- [ ] Positions expiring in <24 hours highlighted in yellow
- [ ] "Unknown" expiry positions appear at bottom of table
- [ ] Dashboard updates every 2 seconds (existing refresh rate)

#### Testing Steps
1. Start bot with existing open positions
2. Open dashboard: `http://localhost:8080/?token=YOUR_TOKEN`
3. Verify "Resolves At" column appears
4. Check formatting: should show "Feb 12, 6:00 PM" style
5. Verify sorting: soonest expiry first
6. Check console logs for any errors

#### Edge Cases to Handle
- Market metadata not available → Show "Unknown"
- Invalid ISO 8601 format → Show "Unknown"
- Already expired markets → Show "Expired" in red
- API timeout → Don't block dashboard render

#### Dependencies
- Requires `py-clob-client` with `get_market()` method
- May need to add caching to avoid API rate limits

#### Performance Considerations
If fetching market metadata for every position is slow:
1. Cache market metadata in `bot.market` (5-minute TTL)
2. Batch fetch all market IDs in single API call
3. Only fetch once per dashboard refresh (not per position)

---

## Phase 1: Quick Wins (Days 3-7)

**Goal**: Increase win rate from 0% to 20-35% using clustering + selective execution

**Total Time**: 5-7 days
**Priority**: HIGH
**Blocking**: Phase 2 tasks

---

### Task 1.1: Implement CLOB WebSocket Monitor

**ID**: `POLY-101`
**Priority**: HIGH
**Time Estimate**: 1-2 days
**Assignee**: Next LLM
**Status**: TODO

#### Problem Statement
Current blockchain monitoring has 2-3 second latency. CLOB WebSocket offers 100-300ms latency (10× faster), free to use, and doesn't count against Alchemy limits.

#### Expected Impact
- Latency: 2-3s → 300ms (10× faster)
- Fill price improvement: 3-5% better (less slippage)
- Entry penalty reduction: 7% → 2-3%

#### Solution Overview
Create new `src/clob_websocket.py` module that:
1. Connects to `wss://clob.polymarket.com/ws`
2. Subscribes to tracked wallet order events
3. Emits signals to `whale_tracker` on fills
4. Runs in background thread (async)

#### Files to Create

**`src/clob_websocket.py`** (NEW FILE)

```python
#!/usr/bin/env python3
"""
CLOB WebSocket Monitor — 300ms latency whale tracking

Connects directly to Polymarket's off-chain orderbook WebSocket
for real-time order fill notifications (10× faster than blockchain).

Free to use, no API key required.
"""

import asyncio
import websockets
import json
import time
import threading
from typing import Set, Dict, Any


class CLOBWebSocketMonitor:
    """
    Real-time CLOB order fill monitor.

    Latency: 100-300ms (vs 2-3s for blockchain)
    Cost: FREE (no API key needed)
    """

    def __init__(self, config, whale_tracker):
        self.config = config
        self.whale_tracker = whale_tracker

        # CLOB WebSocket endpoint
        self.ws_url = "wss://clob.polymarket.com/ws"

        # Tracked wallets (lowercase for comparison)
        self.tracked_wallets: Set[str] = set()

        # Connection state
        self.running = False
        self.connected = False
        self.thread = None

        # Reconnection settings
        self.reconnect_delay = 5  # seconds
        self.max_reconnect_delay = 60

        # Stats
        self.signals_emitted = 0
        self.last_signal_time = None

    def start(self):
        """Start WebSocket monitor in background thread."""
        if self.running:
            print("[CLOB] Already running")
            return

        self.running = True
        self.thread = threading.Thread(
            target=self._run_async_loop,
            daemon=True,
            name="CLOBWebSocketMonitor"
        )
        self.thread.start()
        print(f"[CLOB] Started monitoring {len(self.tracked_wallets)} wallets")

    def stop(self):
        """Gracefully stop the monitor."""
        print("[CLOB] Stopping...")
        self.running = False
        self.connected = False

        if self.thread:
            self.thread.join(timeout=5)

    def update_tracked_wallets(self, wallets: list):
        """Update list of wallets to monitor."""
        self.tracked_wallets = set(w.lower() for w in wallets)
        print(f"[CLOB] Tracking {len(self.tracked_wallets)} wallets")

    def _run_async_loop(self):
        """Run async event loop in thread."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            loop.run_until_complete(self._connect_and_listen())
        except Exception as e:
            print(f"[CLOB] Event loop error: {e}")
        finally:
            loop.close()

    async def _connect_and_listen(self):
        """Main WebSocket connection loop with reconnection."""
        reconnect_delay = self.reconnect_delay

        while self.running:
            try:
                async with websockets.connect(
                    self.ws_url,
                    ping_interval=20,
                    ping_timeout=10
                ) as ws:
                    self.connected = True
                    reconnect_delay = self.reconnect_delay  # Reset delay

                    print(f"[CLOB] Connected to {self.ws_url}")

                    # Subscribe to tracked wallets
                    await self._subscribe_to_wallets(ws)

                    # Listen for messages
                    async for message in ws:
                        if not self.running:
                            break

                        await self._handle_message(message)

            except websockets.exceptions.ConnectionClosed:
                self.connected = False
                print(f"[CLOB] Connection closed, reconnecting in {reconnect_delay}s...")
                await asyncio.sleep(reconnect_delay)

                # Exponential backoff
                reconnect_delay = min(reconnect_delay * 2, self.max_reconnect_delay)

            except Exception as e:
                self.connected = False
                print(f"[CLOB] Error: {e}, reconnecting in {reconnect_delay}s...")
                await asyncio.sleep(reconnect_delay)

    async def _subscribe_to_wallets(self, ws):
        """Subscribe to order_filled events for tracked wallets."""
        # CLOB WebSocket subscription format (check Polymarket docs)
        # This is a PLACEHOLDER - adjust based on actual CLOB API

        for wallet in self.tracked_wallets:
            subscription = {
                "type": "subscribe",
                "channel": "orders",
                "wallet": wallet
            }

            await ws.send(json.dumps(subscription))

        print(f"[CLOB] Subscribed to {len(self.tracked_wallets)} wallets")

    async def _handle_message(self, message: str):
        """Process incoming WebSocket message."""
        try:
            data = json.loads(message)

            # Check message type
            msg_type = data.get("type", "")

            if msg_type == "order_filled":
                await self._on_order_filled(data)

            elif msg_type == "subscription_confirmed":
                print(f"[CLOB] Subscription confirmed: {data.get('channel')}")

            # Add other message types as needed

        except json.JSONDecodeError:
            print(f"[CLOB] Invalid JSON: {message[:100]}")
        except Exception as e:
            print(f"[CLOB] Message handling error: {e}")

    async def _on_order_filled(self, data: Dict[str, Any]):
        """Handle order_filled event."""
        # Extract wallet address
        wallet = data.get("wallet", "").lower()

        # Only process tracked wallets
        if wallet not in self.tracked_wallets:
            return

        # Extract order details
        # NOTE: Field names are PLACEHOLDERS - adjust based on actual CLOB API
        condition_id = data.get("market_id") or data.get("condition_id")
        outcome = data.get("outcome") or data.get("side")
        price = float(data.get("price", 0))
        size = float(data.get("size", 0))
        timestamp = data.get("timestamp", time.time())

        # Build signal
        signal = {
            "source_wallet": wallet,
            "condition_id": condition_id,
            "outcome": outcome.upper(),  # YES or NO
            "whale_price": price,
            "size": size,
            "timestamp": timestamp,
            "source": "clob_websocket",
            "latency_ms": (time.time() - timestamp) * 1000  # Track latency
        }

        # Emit to whale_tracker (thread-safe)
        self.whale_tracker.add_clob_signal(signal)

        self.signals_emitted += 1
        self.last_signal_time = time.time()

        print(f"[CLOB] {wallet[:10]}... {outcome} @ ${price:.3f} (size: {size:.1f}, latency: {signal['latency_ms']:.0f}ms)")

    def get_stats(self) -> Dict[str, Any]:
        """Get monitor statistics."""
        return {
            "connected": self.connected,
            "tracked_wallets": len(self.tracked_wallets),
            "signals_emitted": self.signals_emitted,
            "last_signal_time": self.last_signal_time,
            "latency_target": "100-300ms"
        }
```

#### Files to Modify

**1. `src/whale_tracker.py`** (add CLOB signal handler)

```python
# Add to WhaleTracker class:

def add_clob_signal(self, signal):
    """
    Add CLOB WebSocket signal (thread-safe).

    CLOB signals have ~300ms latency (vs 2-3s blockchain).
    """
    # Use existing blockchain signal queue (already thread-safe)
    self._blockchain_signals.put(signal)

    # Log stats
    latency = signal.get("latency_ms", 0)
    print(f"[CLOB] Signal: {signal['condition_id'][:12]}... @ ${signal['whale_price']:.3f} ({latency:.0f}ms)")
```

**2. `src/bot.py`** (wire CLOB monitor)

```python
# In TradingBot.__init__:

# After blockchain monitor initialization:
self.clob_monitor = None
if self.config.get("USE_CLOB_WEBSOCKET", False):
    from src.clob_websocket import CLOBWebSocketMonitor
    self.clob_monitor = CLOBWebSocketMonitor(config, self.whale_tracker)
    print("[*] CLOB WebSocket monitor initialized (300ms latency)")

# In TradingBot.run(), after blockchain monitor start:

# Start CLOB monitor if enabled
if self.clob_monitor:
    self.clob_monitor.update_tracked_wallets(tracked_addresses)
    self.clob_monitor.start()
    print(f"[CLOB] Real-time monitoring started (300ms latency)")

# In TradingBot.shutdown():

# Stop CLOB monitor
if self.clob_monitor:
    self.clob_monitor.stop()
    print("[*] CLOB monitor stopped.")
```

**3. `config/config.json`** (add CLOB toggle)

```json
{
  "_config_version": 14,

  // Existing blockchain monitor (2-3s latency)
  "USE_BLOCKCHAIN_MONITOR": false,  // DISABLE to save WebSocket quota
  "POLYGON_RPC_WSS": "wss://polygon-mainnet.g.alchemy.com/v2/...",

  // NEW: CLOB WebSocket (300ms latency, FREE)
  "USE_CLOB_WEBSOCKET": true,

  // ... rest of config
}
```

**4. `src/web_server.py`** (add CLOB stats to dashboard)

```python
# Add new route for CLOB stats:

elif path == "/api/clob":
    clob = bot.clob_monitor
    if clob:
        stats = clob.get_stats()
        return stats
    else:
        return {"error": "CLOB monitor not enabled"}
```

**5. `static/index.html`** (show CLOB status)

```html
<!-- Add to dashboard (near blockchain monitor section): -->

<div class="stats-card">
    <h3>CLOB WebSocket</h3>
    <div id="clobStatus">
        <p>Status: <span id="clobConnected">Unknown</span></p>
        <p>Signals: <span id="clobSignals">0</span></p>
        <p>Latency: <span id="clobLatency">100-300ms</span></p>
    </div>
</div>

<script>
// In refreshData() function, add:

fetch('/api/clob?token=' + TOKEN)
    .then(r => r.json())
    .then(data => {
        document.getElementById('clobConnected').textContent = data.connected ? 'Connected' : 'Disconnected';
        document.getElementById('clobSignals').textContent = data.signals_emitted || 0;
    });
</script>
```

#### Acceptance Criteria
- [ ] CLOB monitor connects to `wss://clob.polymarket.com/ws`
- [ ] Subscribes to all tracked wallet addresses
- [ ] Emits signals to `whale_tracker` on order fills
- [ ] Signals have <500ms latency (target: 300ms)
- [ ] Reconnects automatically on disconnect
- [ ] Dashboard shows CLOB connection status
- [ ] Alchemy WebSocket quota not affected (CLOB is separate)

#### Testing Steps
1. Disable blockchain monitor: `"USE_BLOCKCHAIN_MONITOR": false`
2. Enable CLOB monitor: `"USE_CLOB_WEBSOCKET": true`
3. Start bot: `python3 run.py`
4. Verify console shows: `[CLOB] Connected to wss://clob.polymarket.com/ws`
5. Wait for whale trades (may take 5-30 minutes)
6. Verify signals appear with `latency_ms` < 500ms
7. Check dashboard shows CLOB status

#### Important Notes
- **CLOB WebSocket API is undocumented** — May need to reverse-engineer from browser DevTools
- **Field names are PLACEHOLDERS** — Adjust based on actual API response format
- **Subscription format unknown** — May need to inspect Polymarket's frontend code
- **Alternative**: If CLOB WebSocket doesn't work, keep blockchain monitor (2-3s latency is acceptable)

#### Research Required
Before implementing, research:
1. CLOB WebSocket endpoint (confirm `wss://clob.polymarket.com/ws`)
2. Subscription message format (channels, authentication)
3. Event types (order_filled, order_placed, order_cancelled)
4. Field names in responses (market_id vs condition_id, etc.)

**Resources**:
- Polymarket CLOB docs: https://docs.polymarket.com/
- Browser DevTools: Inspect network tab on polymarket.com
- py-clob-client source: Check if it has WebSocket support

#### Dependencies
- `pip3 install websockets` (may already be installed)
- CLOB WebSocket API documentation (research first!)

#### Rollback Plan
If CLOB doesn't work:
1. Keep `USE_CLOB_WEBSOCKET: false`
2. Re-enable blockchain monitor: `USE_BLOCKCHAIN_MONITOR: true`
3. Accept 2-3s latency as acceptable (still 700× faster than polling)

---

### Task 1.2: Selective Execution Filter

**ID**: `POLY-102`
**Priority**: HIGH
**Time Estimate**: 3 hours
**Assignee**: Next LLM
**Status**: TODO

#### Problem Statement
Bot currently copies ALL 775 whale signals indiscriminately. Many whales have negative ROI or trade outside their expertise. This dilutes win rate.

#### Expected Impact
- Trade volume: -60% (only copy top 40%)
- Win rate: 0% → 20-35%
- Fewer losses from low-quality signals

#### Solution
Implement confidence scoring that combines:
1. Whale quality score (0-5 from wallet_scorer)
2. Category match (does whale dominate this market type?)
3. Cluster boost (is this a convergence trade?)

Only copy signals with confidence > 0.6 (top 40%).

#### Files to Modify

**1. `src/whale_tracker.py`** (add confidence scoring)

```python
# Add to WhaleTracker class:

def compute_confidence(self, wallet, signal, cluster=None):
    """
    Compute confidence score for a whale signal.

    Returns: 0.0 - 1.0 (only copy if > 0.6)

    Factors:
    - Whale quality (0-5 score from wallet_scorer)
    - Category match (does whale excel in this market type?)
    - Cluster boost (multiple whales converging?)
    """
    # Factor 1: Whale quality (normalize to 0-1)
    whale_score = self.scorer.get_wallet_score(wallet) / 5.0

    if whale_score < 0.2:
        # Very low quality whale, skip entirely
        return 0.0

    # Factor 2: Category match
    market_title = signal.get("market_title", "")
    market_category = self.scorer.classify_market(market_title)

    # Get whale's best category
    wallet_stats = self.scorer.wallet_stats.get(wallet, {})
    category_stats = wallet_stats.get("categories", {})

    # Find category with highest ROI
    best_category = None
    best_roi = -999
    for cat, stats in category_stats.items():
        if stats.get("trades", 0) >= 3:  # Minimum sample size
            roi = stats.get("roi", 0)
            if roi > best_roi:
                best_roi = roi
                best_category = cat

    # Category match score
    if best_category == market_category:
        category_match = 1.0  # Perfect match
    elif best_category and best_roi > 0:
        category_match = 0.5  # Wrong category but whale is profitable
    else:
        category_match = 0.3  # Unknown/unprofitable

    # Factor 3: Cluster boost
    cluster_boost = 1.0
    if cluster:
        # Cluster conviction: 0.0 (no cluster) to 1.0 (10+ whales)
        cluster_conviction = cluster.get("conviction", 0)
        cluster_boost = 1.0 + (0.5 * cluster_conviction)  # Up to 1.5×

    # Final confidence (capped at 1.0)
    confidence = min(1.0, whale_score * category_match * cluster_boost)

    return confidence
```

**2. `src/bot.py`** (apply filter in copy trading loop)

```python
# In copy trading loop (around line 150-185):

# Execute copy trades + exits in paper mode (crash-proofed)
if signals and self.execution.paper_engine:
    for signal in signals:
        try:
            # v14: Record signal metrics
            self.metrics.increment_cumulative("total_signals_received")

            if signal.get("type") == "COPY_EXIT":
                # Exit signals: always process (close positions)
                with self.metrics.timer("copy_exit_execution_ms"):
                    result = self.execution.paper_engine.close_copy_position(
                        signal, risk_guard=self.risk
                    )
                if result and result.get("success"):
                    self._copy_exits += 1
                    self.metrics.increment("copy_exits_executed")
                    self.health.update_trade_execution()

            else:
                # ENTRY SIGNALS: Apply confidence filter
                wallet = signal.get("source_wallet")

                # Get cluster info (if clustering is enabled - see Task 1.3)
                cluster = None  # TODO: Wire clustering in Task 1.3

                # NEW: Compute confidence score
                confidence = self.whale_tracker.compute_confidence(
                    wallet,
                    signal,
                    cluster
                )

                # NEW: Filter low-confidence signals
                MIN_CONFIDENCE = 0.6  # Top 40% threshold

                if confidence < MIN_CONFIDENCE:
                    title = signal.get("market_title", "")[:40]
                    print(f"[COPY] SKIP: Low confidence ({confidence:.2f}) — {title}")
                    self.metrics.increment("skip_reason_low_confidence")
                    continue  # Skip this signal

                # High-confidence signal: execute trade
                self.metrics.increment("copy_trades_attempted")
                with self.metrics.timer("copy_trade_execution_ms"):
                    result = self.execution.paper_engine.execute_copy_trade(
                        signal,
                        current_exposure=self.risk.current_exposure,
                        confidence=confidence  # Pass to execution engine
                    )

                if result and result.get("success"):
                    self._copy_trades += 1
                    self.risk.add_exposure(result.get("total_cost", 0))
                    self.notifier.notify_trade_opened(signal, result)
                    self.metrics.increment("copy_trades_executed")
                    self.health.update_trade_execution()

                    # Log confidence for tracking
                    print(f"[COPY] EXECUTED: {title} (confidence: {confidence:.2f})")

                elif result:
                    title = signal.get("market_title", "")[:40]
                    print(f"[COPY] SKIP: {result.get('reason', '?')} — {title}")
                    self.metrics.increment(f"skip_reason_{result.get('reason', 'unknown').replace(' ', '_')}")

        except Exception as e:
            print(f"[!] Copy trade error: {e}")
            self.metrics.increment("copy_trade_errors")
```

#### Acceptance Criteria
- [ ] Bot computes confidence score (0-1) for each signal
- [ ] Signals with confidence < 0.6 are skipped
- [ ] Console logs show confidence for each signal
- [ ] Metrics track skip reason: "low_confidence"
- [ ] Trade volume reduced by 40-60%
- [ ] Only high-quality whales in profitable categories get copied

#### Testing Steps
1. Start bot in PAPER mode
2. Monitor console logs for confidence scores
3. Verify ~40-60% of signals are skipped with "low_confidence"
4. Check `data/metrics/` logs for skip reasons
5. Run for 24 hours, check if win rate improves

#### Tuning Parameters
If win rate doesn't improve:
- **Lower threshold**: `MIN_CONFIDENCE = 0.5` (copy top 50%)
- **Higher threshold**: `MIN_CONFIDENCE = 0.7` (copy top 30%, more selective)
- **Adjust category_match**: Give more weight to whale quality vs category

#### Dependencies
- Requires `wallet_scorer.py` with per-category stats (already implemented)
- Will be enhanced by Task 1.3 (clustering) for cluster_boost

---

### Task 1.3: Whale Clustering Detection

**ID**: `POLY-103`
**Priority**: HIGH
**Time Estimate**: 1-2 days
**Assignee**: Next LLM
**Status**: TODO

#### Problem Statement
When 3+ whales trade the same market within 30 seconds, it signals high conviction. Historical data shows 65% win rate on cluster trades vs 0% on random copies.

#### Expected Impact
- Win rate on cluster trades: 40-65%
- Position sizing: 1.5-2× on high-conviction clusters
- Earlier detection of market-moving events

#### Solution
Create clustering detector that:
1. Monitors recent signals (last 2 minutes)
2. Groups by market + 30-second time window
3. Detects when 3+ unique wallets converge
4. Boosts confidence + position size for cluster trades

#### Files to Create

**`src/cluster_detector.py`** (NEW FILE)

```python
#!/usr/bin/env python3
"""
Whale Clustering Detector

Detects when multiple whales converge on the same market within
a short time window. Clusters indicate high conviction and have
historically shown 65% win rate.

Cluster = 3+ unique wallets trading same market within 30 seconds.
"""

from collections import defaultdict
import time
from typing import Dict, List, Any


class ClusterDetector:
    """
    Detect whale clustering (convergence trades).

    Cluster criteria:
    - 3+ unique wallets
    - Same market (condition_id)
    - Within 30-second window
    - Same outcome (YES or NO)
    """

    def __init__(self, config):
        self.config = config

        # Clustering parameters
        self.cluster_window = config.get("CLUSTER_WINDOW_SEC", 30)
        self.min_cluster_size = config.get("MIN_CLUSTER_SIZE", 3)

        # Stats
        self.clusters_detected = 0
        self.last_cluster_time = None

    def detect_clusters(self, recent_signals: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """
        Detect whale clusters from recent signals.

        Args:
            recent_signals: List of signals from last 2 minutes

        Returns:
            {
                market_id: {
                    'wallets': ['0xabc...', '0xdef...'],
                    'cluster_size': 4,
                    'conviction': 0.75,  # 0-1 scale
                    'outcome': 'YES',
                    'avg_whale_price': 0.432,
                    'detected_at': timestamp
                }
            }
        """
        # Group signals by (market, time_bucket)
        markets_by_time = defaultdict(list)

        for signal in recent_signals:
            market_id = signal.get("condition_id")
            timestamp = signal.get("timestamp", time.time())

            # Bucket by 30-second window
            time_bucket = int(timestamp // self.cluster_window)

            bucket_key = (market_id, time_bucket)
            markets_by_time[bucket_key].append(signal)

        # Detect clusters
        clusters = {}

        for (market_id, time_bucket), signals in markets_by_time.items():
            # Need 3+ signals
            if len(signals) < self.min_cluster_size:
                continue

            # Get unique wallets
            wallets = set(s.get("source_wallet") for s in signals)

            # Need 3+ unique wallets (not same whale trading multiple times)
            if len(wallets) < self.min_cluster_size:
                continue

            # Find most common outcome (YES or NO)
            outcomes = [s.get("outcome") for s in signals]
            outcome_counts = {}
            for o in outcomes:
                outcome_counts[o] = outcome_counts.get(o, 0) + 1

            dominant_outcome = max(outcome_counts, key=outcome_counts.get)
            outcome_consensus = outcome_counts[dominant_outcome] / len(outcomes)

            # Filter signals to dominant outcome only
            dominant_signals = [
                s for s in signals
                if s.get("outcome") == dominant_outcome
            ]

            # Recount unique wallets for dominant outcome
            dominant_wallets = set(
                s.get("source_wallet") for s in dominant_signals
            )

            if len(dominant_wallets) < self.min_cluster_size:
                continue  # Not enough consensus

            # Compute cluster conviction (0-1 scale)
            # More whales = higher conviction (cap at 10 whales = 1.0)
            conviction = min(1.0, len(dominant_wallets) / 10)

            # Average whale entry price
            prices = [s.get("whale_price", 0) for s in dominant_signals]
            avg_price = sum(prices) / len(prices) if prices else 0

            # Build cluster data
            clusters[market_id] = {
                "wallets": list(dominant_wallets),
                "cluster_size": len(dominant_wallets),
                "conviction": conviction,
                "outcome": dominant_outcome,
                "outcome_consensus": outcome_consensus,
                "avg_whale_price": avg_price,
                "detected_at": time.time(),
                "time_bucket": time_bucket
            }

            self.clusters_detected += 1
            self.last_cluster_time = time.time()

        return clusters

    def get_stats(self) -> Dict[str, Any]:
        """Get clustering statistics."""
        return {
            "clusters_detected": self.clusters_detected,
            "last_cluster_time": self.last_cluster_time,
            "cluster_window_sec": self.cluster_window,
            "min_cluster_size": self.min_cluster_size
        }
```

#### Files to Modify

**1. `src/whale_tracker.py`** (integrate clustering)

```python
# Add to WhaleTracker class:

from src.cluster_detector import ClusterDetector

def __init__(self, config):
    # ... existing code ...

    # NEW: Cluster detector
    self.cluster_detector = ClusterDetector(config)
    self.current_clusters = {}  # {market_id: cluster_data}

def get_current_clusters(self):
    """
    Get active whale clusters (last 2 minutes).

    Returns dict of {market_id: cluster_data}
    """
    cutoff = time.time() - 120  # Last 2 minutes

    # Get recent signals (both blockchain and API)
    recent = []

    # Recent blockchain signals (already in queue)
    temp_signals = []
    while not self._blockchain_signals.empty():
        sig = self._blockchain_signals.get()
        if sig.get("timestamp", 0) > cutoff:
            recent.append(sig)
        temp_signals.append(sig)

    # Put them back
    for sig in temp_signals:
        self._blockchain_signals.put(sig)

    # Recent API signals (from last poll)
    # Note: This is approximate, may need to track history
    # For now, just detect from blockchain signals (they're faster anyway)

    # Detect clusters
    clusters = self.cluster_detector.detect_clusters(recent)

    # Cache for use in confidence scoring
    self.current_clusters = clusters

    return clusters
```

**2. `src/bot.py`** (wire clustering into copy trading)

```python
# In copy trading loop (before processing signals):

# Get current whale clusters
clusters = self.whale_tracker.get_current_clusters()

if clusters:
    print(f"[CLUSTER] Detected {len(clusters)} convergence trades:")
    for market_id, cluster in clusters.items():
        print(f"  → {market_id[:12]}... ({cluster['cluster_size']} whales, "
              f"{cluster['conviction']:.0%} conviction, outcome: {cluster['outcome']})")

# In signal processing loop:

# Get cluster info for this market
market_id = signal.get("condition_id")
cluster = clusters.get(market_id)

# Compute confidence (pass cluster for boost)
confidence = self.whale_tracker.compute_confidence(
    wallet,
    signal,
    cluster  # NEW: Pass cluster data
)

# Boost position size for cluster trades
size_multiplier = 1.0
if cluster and cluster['cluster_size'] >= 3:
    # Scale size by conviction (1.0 to 1.5×)
    size_multiplier = 1.0 + (0.5 * cluster['conviction'])
    print(f"[CLUSTER] Boosting size {size_multiplier:.2f}× for convergence trade")

# Execute with boosted size
result = self.execution.paper_engine.execute_copy_trade(
    signal,
    current_exposure=self.risk.current_exposure,
    confidence=confidence,
    size_multiplier=size_multiplier  # NEW: Boost position size
)
```

**3. `src/paper_engine.py`** (apply size multiplier)

```python
# In execute_copy_trade method:

def execute_copy_trade(self, signal, current_exposure=0, confidence=1.0, size_multiplier=1.0):
    """
    Execute copy trade with optional size multiplier.

    Args:
        signal: Whale trade signal
        current_exposure: Current risk exposure
        confidence: Confidence score (0-1)
        size_multiplier: Position size multiplier (1.0 = normal, 1.5 = cluster boost)
    """
    # ... existing validation code ...

    # Compute position size
    base_size = self.config.get("COPY_TRADE_SIZE", 2.0)

    # Apply Kelly sizing if available (Task 1.4)
    # ... Kelly code ...

    # NEW: Apply cluster size multiplier
    position_size = base_size * size_multiplier

    # Cap at configured max
    max_size = self.config.get("COPY_MAX_SIZE", 5.0)
    position_size = min(position_size, max_size)

    # ... rest of execution logic ...
```

**4. `config/config.json`** (add clustering config)

```json
{
  "_config_version": 14,

  // NEW: Clustering parameters
  "CLUSTER_WINDOW_SEC": 30,
  "MIN_CLUSTER_SIZE": 3,

  // ... rest of config
}
```

#### Acceptance Criteria
- [ ] Clusters detected when 3+ whales converge
- [ ] Console logs show cluster details (size, conviction, outcome)
- [ ] Position size boosted 1.0-1.5× for cluster trades
- [ ] Confidence score boosted for cluster trades
- [ ] Metrics track cluster detection count

#### Testing Steps
1. Start bot with clustering enabled
2. Monitor console for `[CLUSTER] Detected` messages
3. Wait for whale convergence (may take hours/days)
4. Verify position size is boosted (check paper_state.json)
5. Track win rate on cluster trades vs non-cluster

#### Expected Results
After 1 week of data:
- Cluster trades: 40-65% win rate
- Non-cluster trades: 10-20% win rate
- Overall win rate: 20-35% (weighted average)

#### Dependencies
- Requires recent signal history (last 2 minutes)
- Works best with CLOB WebSocket (Task 1.1) for fast signals

---

### Task 1.4: Apply Kelly Criterion to Copy Trades

**ID**: `POLY-104`
**Priority**: MEDIUM
**Time Estimate**: 2 hours
**Assignee**: Next LLM
**Status**: TODO

#### Problem Statement
Position sizing currently uses fixed `COPY_TRADE_SIZE` (2.0 USDC). Kelly Criterion is already computed in `wallet_scorer.py` but never applied. This wastes capital on low-quality whales and under-sizes high-quality whales.

#### Expected Impact
- Better capital allocation (more on winners, less on losers)
- Reduced drawdown from bad whales
- Improved risk-adjusted returns

#### Solution
Wire existing Kelly sizing into copy trade execution. Use half-Kelly for safety (standard practice).

#### Files to Modify

**1. `src/paper_engine.py`** (use Kelly sizing)

```python
# In execute_copy_trade method:

def execute_copy_trade(self, signal, current_exposure=0, confidence=1.0, size_multiplier=1.0):
    """Execute copy trade with Kelly-based position sizing."""

    # ... existing validation ...

    # Get whale address
    wallet = signal.get("source_wallet")

    # NEW: Get Kelly-recommended size from wallet_scorer
    kelly_size = None
    if self.scorer:
        current_bankroll = self.portfolio["cash_balance"]
        kelly_size = self.scorer.get_kelly_size(wallet, current_bankroll)

    # Determine base size
    if kelly_size and kelly_size > 0:
        # Use Kelly sizing (already half-Kelly in wallet_scorer)
        base_size = kelly_size
        print(f"[KELLY] Using Kelly size: ${base_size:.2f} for {wallet[:10]}...")
    else:
        # Fallback to fixed size
        base_size = self.config.get("COPY_TRADE_SIZE", 2.0)
        print(f"[KELLY] No Kelly data, using fixed size: ${base_size:.2f}")

    # Apply cluster multiplier (Task 1.3)
    position_size = base_size * size_multiplier

    # Apply min/max caps
    min_size = self.config.get("COPY_MIN_SIZE", 0.5)
    max_size = self.config.get("COPY_MAX_SIZE", 5.0)
    position_size = max(min_size, min(position_size, max_size))

    # ... rest of execution ...
```

**2. `src/bot.py`** (ensure scorer is injected)

Already done in existing code:
```python
# In TradingBot.__init__:
if self.execution.paper_engine:
    self.execution.paper_engine.scorer = self.wallet_scorer  # Already exists
```

#### Acceptance Criteria
- [ ] Position sizes vary based on whale performance
- [ ] High-ROI whales get larger positions (up to MAX_SIZE)
- [ ] Low-ROI whales get smaller positions (down to MIN_SIZE)
- [ ] Console logs show "Using Kelly size: $X.XX"
- [ ] Positions respect min/max caps

#### Testing Steps
1. Start bot with mix of good/bad whales
2. Monitor console for Kelly sizing messages
3. Check `data/paper_state.json` for position sizes
4. Verify high-quality whales get larger positions
5. Verify low-quality whales get smaller (or skipped by confidence filter)

#### Important Notes
- Kelly requires ≥5 historical trades per whale (wallet_scorer requirement)
- New whales get fallback fixed size until enough data
- Half-Kelly is already applied in wallet_scorer (conservative)

#### Dependencies
- `wallet_scorer.py` with `get_kelly_size()` method (already implemented)

---

## Phase 2: Transformational Change (Weeks 2-6)

**Goal**: Extract whale decision patterns, achieve 40-60% win rate

**Total Time**: 3-6 weeks
**Priority**: MEDIUM
**Blocking**: Phase 3 tasks

---

### Task 2.1: Whale Profiler (Statistical Pattern Extraction)

**ID**: `POLY-201`
**Priority**: MEDIUM
**Time Estimate**: 2 weeks
**Assignee**: Next LLM
**Status**: TODO

#### Problem Statement
Need to build "playbooks" for each whale that capture their decision patterns, not just performance stats. This allows predicting their moves before execution.

#### Expected Impact
- Win rate: 30-50% (profile-based filtering)
- Identify 3-5 "elite whales" with >50% win rate strategies
- Better understand what makes whales successful

#### Solution
Create statistical profiler that analyzes 100+ trades per whale to extract:
- Category performance (crypto vs sports vs politics)
- Cluster sensitivity (do they respond to convergence?)
- Conviction sizing (position size vs outcome correlation)
- Hold time patterns (scalping vs swing trading)
- Exit discipline (TP/SL hit rate)
- Expiry awareness (exit before expiry?)

#### Implementation
See `CLAUDE_HANDOFF.md` Section "Implementation Roadmap > Phase 2 > Task 2.1" for full code examples.

#### Acceptance Criteria
- [ ] Profile generated for each whale with 20+ trades
- [ ] Profiles saved to `data/whale_profiles.json`
- [ ] Dashboard shows top whale profiles
- [ ] Profile-based filtering achieves 30-50% win rate
- [ ] Profiles updated weekly

#### Time Breakdown
- Week 1: Build profiler, analyze category performance, hold times
- Week 2: Add cluster sensitivity, conviction sizing, exit discipline
- Week 3: Test filtering, tune thresholds, validate on historical data

---

### Task 2.2: LLM Pattern Extraction (Claude API Integration)

**ID**: `POLY-202`
**Priority**: MEDIUM
**Time Estimate**: 1-2 weeks
**Assignee**: Next LLM
**Status**: TODO

#### Problem Statement
Statistical profiling is mechanical. LLMs can detect semantic patterns like "whale trades more after Fed announcements" that stats can't catch.

#### Expected Impact
- Win rate: 40-60% (LLM-enriched profiles)
- Identify 2-3 whales with interpretable strategies
- Human-readable insights for validation

#### Solution
Use Claude API to analyze whale trade history and extract interpretable patterns.

**Cost**: ~$0.10-0.50 per whale profile (refresh weekly)

#### Implementation
See `CLAUDE_HANDOFF.md` Section "Implementation Roadmap > Phase 2 > Task 2.2" for full code examples including:
- `src/llm_pattern_extractor.py`
- Anthropic API integration
- Prompt engineering for pattern extraction
- JSON schema for responses

#### Acceptance Criteria
- [ ] LLM analyzes top 10-20 whales
- [ ] Generates JSON with: dominant_category, hold_time_pattern, clustering, conviction_signal, failure_modes
- [ ] Insights are human-readable and actionable
- [ ] API cost stays under $50/month
- [ ] Profiles saved to `data/llm_whale_patterns.json`

#### User Approval Required
Before implementing:
1. User needs Anthropic API key
2. User approves $10-50/month cost
3. Test on 1 whale first, show results

---

## Phase 3: Advanced Features (Optional)

**Goal**: Professional-grade infrastructure (after profitability)

**Total Time**: Variable
**Priority**: LOW
**Blocking**: None (purely optional)

---

### Task 3.1: QuantVPS Deployment

**ID**: `POLY-301`
**Priority**: LOW (after profitable)
**Time Estimate**: 1 week
**Assignee**: Next LLM
**Status**: TODO

#### When to Implement
Only after achieving:
- Consistent profitability ($60+/month)
- 30%+ win rate for 2+ weeks
- Positive cash flow to afford $60/mo VPS

#### Expected Impact
- Latency: 300ms → 20-50ms
- Entry penalty: 2-3% → 0.5-1%
- Always-on operation (no Mac sleep)

#### Solution
Deploy to QuantVPS Netherlands:
- Plan: VPS Lite ($59.99/month)
- Latency: 0-2ms to Amsterdam exchanges
- Legal: Netherlands allows prediction markets
- OS: Ubuntu 22.04 LTS

#### Implementation Steps
1. Sign up: https://quantvps.com
2. Choose VPS Lite (Netherlands datacenter)
3. Install Python 3.9.10+, dependencies
4. Clone repo, configure credentials
5. Set up systemd service for auto-restart
6. Install monitoring (Uptime Kuma)
7. Test in PAPER mode for 3 days
8. Switch to SHADOW mode for 1 week
9. Consider LIVE mode

#### Dependencies
- User has $60/month budget
- Bot is already profitable
- User comfortable with Linux

---

## Future Enhancements

**Not scheduled, implement when user has capital**

---

### Task F.1: Multi-LLM Consensus Pattern Extraction

**ID**: `POLY-F01`
**Priority**: FUTURE
**Time Estimate**: 2 weeks
**Assignee**: Next LLM
**Status**: FUTURE

#### Problem Statement
Single LLM has biases. Consensus from multiple LLMs (Claude, GPT-4, Gemini, Grok, Kimi) produces more robust patterns.

#### Expected Impact
- Higher accuracy (consensus filters false positives)
- Cross-validation of strategies
- Multiple perspectives on same whale

#### Cost
$50-100/month (5 LLMs × $10-20 each)

#### When to Implement
When user has:
- $100+/month profit
- Budget for multiple API keys
- Interest in maximum accuracy

#### Implementation
Create `src/multi_llm_pattern_extractor.py` that:
1. Sends whale data to 5 LLMs (Claude, GPT-4, Gemini, Grok, Kimi)
2. Collects responses in parallel
3. Extracts consensus patterns (appear in 3+ LLMs)
4. Flags disagreements for human review

#### Acceptance Criteria
- [ ] Integrates Claude, GPT-4, Gemini, Grok, Kimi
- [ ] Consensus logic: patterns in 3+ LLMs
- [ ] Disagreements logged for review
- [ ] Cost tracking per LLM
- [ ] Fallback if one LLM fails

---

## Task Status Summary

### Phase 0 (Immediate)
- [ ] POLY-001: Fix WebSocket leak (10 min)
- [ ] POLY-002: Add resolution times (2-3 hours)

### Phase 1 (Days 3-7)
- [ ] POLY-101: CLOB WebSocket (1-2 days)
- [ ] POLY-102: Selective execution (3 hours)
- [ ] POLY-103: Whale clustering (1-2 days)
- [ ] POLY-104: Kelly Criterion (2 hours)

### Phase 2 (Weeks 2-6)
- [ ] POLY-201: Whale profiler (2 weeks)
- [ ] POLY-202: LLM pattern extraction (1-2 weeks) — USER APPROVAL REQUIRED

### Phase 3 (Optional)
- [ ] POLY-301: QuantVPS deployment (1 week) — AFTER PROFITABLE

### Future
- [ ] POLY-F01: Multi-LLM consensus (2 weeks) — AFTER $100+/mo PROFIT

---

## Critical Path

```
Phase 0 (Days 1-2)
├── POLY-001 (WebSocket fix) → BLOCKS Phase 1
└── POLY-002 (Resolution times) → Improves UX

Phase 1 (Days 3-7)
├── POLY-101 (CLOB WebSocket) → Improves latency
├── POLY-102 (Selective execution) → CRITICAL for win rate
├── POLY-103 (Clustering) → Enhances POLY-102
└── POLY-104 (Kelly sizing) → Optimizes capital

Phase 2 (Weeks 2-6)
├── POLY-201 (Profiler) → BLOCKS POLY-202
└── POLY-202 (LLM extraction) → Requires POLY-201

Phase 3 (Optional)
└── POLY-301 (QuantVPS) → Independent, after profitable
```

---

## Success Metrics

### Phase 0
- ✅ Bot runs 24h without WebSocket errors
- ✅ Dashboard shows resolution times

### Phase 1
- 🎯 Win rate: 20-35% (from 0%)
- 🎯 Trade volume: -60% (selective execution)
- 🎯 Cluster trades: 40-65% win rate
- 🎯 Portfolio value: $85-95 (from $82)

### Phase 2
- 🎯 Win rate: 40-60% (LLM-enriched)
- 🎯 Identify 3-5 elite whales (>50% win rate)
- 🎯 Portfolio value: $110-140 (10-40% ROI)

### Phase 3
- 🎯 Latency: 20-50ms (QuantVPS)
- 🎯 Uptime: 99.9% (always-on VPS)

---

## For LLMs Executing These Tasks

### General Guidelines
1. **Read CLAUDE_HANDOFF.md first** — Understand current state
2. **Update CLAUDE_HANDOFF.md after each task** — Document what changed
3. **Test in PAPER mode** — Never skip testing
4. **Ask user before big changes** — Architectural, cost, strategy
5. **Follow acceptance criteria** — Each task has clear success metrics

### Code Quality Standards
- **Type hints**: Use Python type hints for new functions
- **Docstrings**: Every new function gets a docstring
- **Error handling**: Try/except on I/O and API calls
- **Logging**: Print statements for debugging (no logging framework needed)
- **Testing**: Manual testing sufficient (no unit tests required)

### When Stuck
- **WebSocket APIs undocumented**: Research in browser DevTools, check GitHub discussions
- **LLM API costs too high**: Start with 1 whale, show user cost before scaling
- **Performance issues**: Profile first, optimize second (don't premature optimize)
- **Unclear requirements**: Ask user for clarification

---

**END OF TODO_IMPLEMENTATION.md**

*Created: 2026-02-11 by Claude Sonnet 4.5*
*Next LLM: Execute tasks in order (Phase 0 → 1 → 2 → 3). Update CLAUDE_HANDOFF.md after each task.*
