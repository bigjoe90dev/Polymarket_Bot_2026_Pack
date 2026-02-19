#!/usr/bin/env python3
"""
CLOB WebSocket Monitor — 300ms latency whale tracking

Connects directly to Polymarket's off-chain orderbook WebSocket
for real-time order fill notifications (10× faster than blockchain).

Free to use, no API key required for paper trading (public market channel).

Key features:
- Gamma API integration for clobTokenIds (required for WS subscription)
- Public market channel (no auth needed for paper trading)
- Local L2 order book maintenance
- Simulated fill detection for paper trading
"""

import asyncio
import json
import time
import threading
from typing import Set, Dict, Any, Optional
from collections import defaultdict
import requests

# Try to import websockets, install if not available
try:
    import websockets
except ImportError:
    print("[CLOB] websockets package not found, installing...")
    import subprocess
    subprocess.check_call(['pip3', 'install', 'websockets'])
    import websockets


# Gamma API for fetching clobTokenIds
GAMMA_API_URL = "https://gamma-api.polymarket.com/markets"


class LocalOrderBook:
    """
    Thread-safe local L2 order book maintained from WebSocket updates.
    
    Stores bids and asks per token_id with price levels and cumulative sizes.
    """
    
    def __init__(self):
        self._lock = threading.RLock()
        # token_id -> {price -> {size, orders}}
        self._bids = defaultdict(dict)  # Buy orders, sorted desc by price
        self._asks = defaultdict(dict)  # Sell orders, sorted asc by price
        self._last_update = defaultdict(float)
    
    def update_bid(self, token_id: str, price: float, size: float):
        """Update a bid level."""
        with self._lock:
            if size <= 0:
                # Remove the level
                self._bids[token_id].pop(price, None)
            else:
                self._bids[token_id][price] = {"size": size, "side": "bid"}
            self._last_update[token_id] = time.time()
    
    def update_ask(self, token_id: str, price: float, size: float):
        """Update an ask level."""
        with self._lock:
            if size <= 0:
                # Remove the level
                self._asks[token_id].pop(price, None)
            else:
                self._asks[token_id][price] = {"size": size, "side": "ask"}
            self._last_update[token_id] = time.time()
    
    def get_best_bid(self, token_id: str) -> Optional[tuple]:
        """Get best bid (highest price) for a token."""
        with self._lock:
            bids = self._bids.get(token_id, {})
            if not bids:
                return None
            best_price = max(bids.keys())
            return (best_price, bids[best_price]["size"])
    
    def get_best_ask(self, token_id: str) -> Optional[tuple]:
        """Get best ask (lowest price) for a token."""
        with self._lock:
            asks = self._asks[token_id]
            if not asks:
                return None
            best_price = min(asks.keys())
            return (best_price, asks[best_price]["size"])
    
    def get_mid_price(self, token_id: str) -> Optional[float]:
        """Get mid price (average of best bid and ask)."""
        with self._lock:
            best_bid = self.get_best_bid(token_id)
            best_ask = self.get_best_ask(token_id)
            if best_bid and best_ask:
                return (best_bid[0] + best_ask[0]) / 2
            return None
    
    def can_fill(self, token_id: str, side: str, size: float, max_price: float = None) -> bool:
        """
        Check if an order can be filled at current book state.
        
        For BUY: need asks at or below max_price
        For SELL: need bids at or above min_price (if specified)
        """
        with self._lock:
            if side.lower() == "buy" or side.lower() == "yes":
                # Check asks (sell side)
                asks = self._asks.get(token_id, {})
                if not asks:
                    return False
                # Sort asks by price ascending
                sorted_asks = sorted(asks.items(), key=lambda x: x[0])
                available = 0
                for price, level in sorted_asks:
                    if max_price and price > max_price:
                        break
                    available += level["size"]
                return available >= size
            
            else:  # sell or "no"
                # Check bids (buy side)
                bids = self._bids.get(token_id, {})
                if not bids:
                    return False
                available = sum(level["size"] for level in bids.values())
                return available >= size
    
    def get_order_book_snapshot(self, token_id: str, depth: int = 5) -> Dict:
        """Get a snapshot of the order book for a token."""
        with self._lock:
            bids = self._bids.get(token_id, {})
            asks = self._asks.get(token_id, {})
            
            sorted_bids = sorted(bids.items(), key=lambda x: -x[0])[:depth]
            sorted_asks = sorted(asks.items(), key=lambda x: x[0])[:depth]
            
            return {
                "token_id": token_id,
                "bids": [{"price": p, "size": l["size"]} for p, l in sorted_bids],
                "asks": [{"price": p, "size": l["size"]} for p, l in sorted_asks],
                "last_update": self._last_update.get(token_id, 0)
            }


class CLOBWebSocketMonitor:
    """
    Real-time CLOB order fill monitor.
    
    Latency: 100-300ms (vs 2-3s for blockchain)
    Cost: FREE (no API key needed for public market channel)
    
    Flow: Gamma → clobTokenIds → WS market channel → Local L2 book → Simulate fills
    """

    def __init__(self, config, whale_tracker, price_callback=None):
        """
        Initialize CLOB WebSocket monitor.

        Args:
            config: Bot config dict
            whale_tracker: WhaleTracker instance to emit signals to
            price_callback: Optional callback for price updates (for momentum strategy)
        """
        self.config = config
        self.whale_tracker = whale_tracker
        self.price_callback = price_callback  # For momentum strategy

        # CLOB WebSocket endpoint - public market channel (no auth needed)
        self.ws_url = config.get(
            "CLOB_WS_URL",
            "wss://ws-subscriptions-clob.polymarket.com/ws/market"
        )

        # Tracked wallets (lowercase for comparison)
        self.tracked_wallets: Set[str] = set()

        # Market cache (condition_id -> market info with clobTokenIds)
        self._market_cache = {}
        self._cache_lock = threading.Lock()
        
        # Local L2 order book
        self.order_book = LocalOrderBook()

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
        self.last_message_time = None
        self.messages_received = 0
        self.errors = 0
        self.reconnect_count = 0
        
        # For paper fill simulation
        self._last_book_update = defaultdict(float)

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

        print("[CLOB] Stopped")

    def is_connected(self) -> bool:
        """Return connection status."""
        return self.connected

    def update_tracked_wallets(self, wallets: list):
        """Update list of wallets to monitor."""
        self.tracked_wallets = set(w.lower() for w in wallets)
        print(f"[CLOB] Tracking {len(self.tracked_wallets)} wallets")

    def update_market_cache(self, markets):
        """
        Update market metadata cache.
        
        Uses token IDs from market.py (yes_token_id, no_token_id) for WebSocket subscription.
        These are the CLOB token IDs that work with the WebSocket.
        """
        # Skip Gamma API call - use existing token IDs from market.py
        # The market.py already has the correct yes_token_id and no_token_id
        
        print(f"[CLOB] DEBUG update_market_cache: received {len(markets)} markets")
        
        with self._cache_lock:
            self._market_cache = {}
            for i, m in enumerate(markets):
                cid = m.get("condition_id", "")
                yes_token = m.get("yes_token_id")
                no_token = m.get("no_token_id")
                
                if i < 3:
                    print(f"[CLOB] DEBUG market {i}: cid={cid[:20] if cid else 'NONE'}... yes_token={yes_token[:20] if yes_token else 'NONE'}...")
                
                # Use condition_id if available, otherwise use yes_token as key
                cache_key = cid if cid else yes_token
                
                if cache_key and yes_token and no_token:
                    self._market_cache[cache_key] = {
                        "condition_id": cid if cid else cache_key,
                        "title": m.get("title", ""),
                        "yes_token_id": yes_token,
                        "no_token_id": no_token,
                        # Use these as fallback for WebSocket subscription
                        "yes_clob_token_id": yes_token,
                        "no_clob_token_id": no_token,
                    }
        
        print(f"[CLOB] Market cache updated: {len(self._market_cache)} markets (using token IDs from market.py)")

    def _fetch_clob_token_ids_from_gamma(self, markets):
        """
        Fetch clobTokenIds from Gamma API.
        
        Gamma returns clobTokenIds which are the correct token IDs
        for WebSocket subscription (not the same as token_id from CLOB client).
        
        NOTE: Gamma API expects 'condition_ids' (array), not 'condition_id'.
        """
        print("[CLOB] Fetching clobTokenIds from Gamma API...")
        
        # Get unique condition IDs
        condition_ids = [m.get("condition_id") for m in markets if m.get("condition_id")]
        
        if not condition_ids:
            return
        
        # Gamma API accepts condition_ids as array parameter
        # Batch requests to avoid too many at once
        batch_size = 50  # Gamma can handle more per request
        
        for i in range(0, len(condition_ids), batch_size):
            batch = condition_ids[i:i+batch_size]
            
            try:
                # FIXED: Use 'condition_ids' (array) not 'condition_id' (string)
                params = {
                    "condition_ids": batch  # Array of condition IDs
                }
                    
                resp = requests.get(GAMMA_API_URL, params=params, timeout=10)
                resp.raise_for_status()
                gamma_markets = resp.json()
                
                if not isinstance(gamma_markets, list):
                    gamma_markets = [gamma_markets]
                
                # Map clobTokenIds to our markets
                for gm in gamma_markets:
                    if not gm:
                        continue
                    
                    cid = gm.get("conditionId") or gm.get("condition_id")
                    if not cid:
                        continue
                    
                    # clobTokenIds is a list like ["token_id_yes", "token_id_no"]
                    clob_token_ids = gm.get("clobTokenIds", [])
                    
                    # Find matching market and update
                    for m in markets:
                        if m.get("condition_id") == cid:
                            if len(clob_token_ids) >= 2:
                                m["yes_clob_token_id"] = clob_token_ids[0]
                                m["no_clob_token_id"] = clob_token_ids[1]
                                print(f"[CLOB] Got clobTokenIds for {cid[:20]}...: YES={clob_token_ids[0][:20]}..., NO={clob_token_ids[1][:20]}...")
                            break
                            
            except Exception as e:
                print(f"[CLOB] Error fetching from Gamma: {e}")
                continue
        
        # Count how many markets have clobTokenIds
        with_clob = sum(1 for m in markets if m.get("yes_clob_token_id") or m.get("no_clob_token_id"))
        print(f"[CLOB] Enriched {with_clob}/{len(markets)} markets with clobTokenIds")

    def _run_async_loop(self):
        """Run async event loop in thread."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            loop.run_until_complete(self._connect_and_listen())
        except Exception as e:
            print(f"[CLOB] Event loop error: {e}")
            self.errors += 1
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

                    # Subscribe to market channel with clobTokenIds
                    await self._subscribe_to_markets(ws)

                    # Listen for messages
                    async for message in ws:
                        if not self.running:
                            break

                        await self._handle_message(message)

            except websockets.exceptions.ConnectionClosed as e:
                self.connected = False
                print(f"[CLOB] Connection closed: {e}, reconnecting in {reconnect_delay}s...")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, self.max_reconnect_delay)

            except Exception as e:
                self.connected = False
                self.errors += 1
                print(f"[CLOB] Error: {e}, reconnecting in {reconnect_delay}s...")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, self.max_reconnect_delay)

    async def _subscribe_to_markets(self, ws):
        """
        Subscribe to market channel with clobTokenIds.
        
        Per Polymarket docs:
        - Use 'market' channel (public, no auth needed)
        - Must provide assets_ids (clobTokenIds from Gamma)
        - Example: ws.send({ type: "market", assets_ids: [tokenId] })
        """
        # Get asset IDs - prefer clobTokenIds from Gamma, fallback to token_ids
        asset_ids = []
        
        with self._cache_lock:
            for cid, market in self._market_cache.items():
                # First try clobTokenIds from Gamma
                yes_token = market.get("yes_clob_token_id") or market.get("yes_token_id")
                no_token = market.get("no_clob_token_id") or market.get("no_token_id")
                
                if yes_token:
                    asset_ids.append(yes_token)
                if no_token:
                    asset_ids.append(no_token)
        
        # Deduplicate
        asset_ids = list(dict.fromkeys(asset_ids))
        
        # Limit to first 50 token IDs to avoid overwhelming the connection
        asset_ids = asset_ids[:50]
        
        if not asset_ids:
            print("[CLOB] No asset IDs available for subscription")
            return
        
        # Subscribe to market channel with asset IDs
        subscription = {
            "type": "market",
            "assets_ids": asset_ids
        }
        
        try:
            await ws.send(json.dumps(subscription))
            print(f"[CLOB] Subscribed to market channel with {len(asset_ids)} assets")
            print(f"[CLOB] First asset ID: {asset_ids[0][:30]}...")
        except Exception as e:
            print(f"[CLOB] Market subscription failed: {e}")
        
        print(f"[CLOB] Subscriptions sent to CLOB WebSocket")

    async def _handle_message(self, message: str):
        """Process incoming WebSocket message and update local order book."""
        self.messages_received += 1
        self.last_message_time = time.time()
        
        try:
            data = json.loads(message)
            
            # Handle array of messages (some WS responses are arrays)
            if isinstance(data, list):
                for item in data:
                    await self._handle_message_item(item)
                return
            
            await self._handle_message_item(data)

        except json.JSONDecodeError:
            print(f"[CLOB] Invalid JSON: {message[:100]}")
            self.errors += 1
        except Exception as e:
            print(f"[CLOB] Message handling error: {e}")
            self.errors += 1

    async def _handle_message_item(self, data: Dict):
        """Process a single message item."""
        if not isinstance(data, dict):
            return

        # DIAGNOSTIC: Print raw message once to understand format
        if hasattr(self, '_raw_msg_printed'):
            self._raw_msg_printed += 1
        else:
            self._raw_msg_printed = 1
        
        if self._raw_msg_printed == 1:
            print(f"\n[CLOB RAW] First message payload:")
            import json
            print(json.dumps(data, indent=2)[:2000])
            print(f"[CLOB RAW] Keys found: {list(data.keys())}")
            print("[CLOB RAW] End payload\n")

        # FIXED: Route by event_type as per Polymarket docs
        # - "book" = order book snapshot (bids/asks as objects)
        # - "price_change" = price level updates
        # - "last_trade_price" = actual trade/fill (for whale detection)
        et = data.get("event_type")

        if et == "book":
            await self._handle_book_snapshot(data)
        elif et == "price_change":
            await self._handle_price_change(data)
        elif et == "last_trade_price":
            await self._handle_last_trade_price(data)
        # Legacy support for other formats
        elif "bids" in data or "asks" in data:
            await self._handle_book_snapshot(data)
        elif "asset_id" in data:
            await self._handle_price_update(data)
        elif "price" in data and "size" in data:
            await self._handle_trade(data)
        else:
            # Log unknown format occasionally
            if self.messages_received % 500 == 0:
                print(f"[CLOB] Unknown format: {list(data.keys())[:5]}")

        # Log every 100 messages
        if self.messages_received % 100 == 0:
            print(f"[CLOB] Messages processed: {self.messages_received}")

    async def _handle_book_snapshot(self, data: Dict):
        """Handle full order book snapshot.
        
        Polymarket format: bids/asks are arrays of objects with price/size,
        not arrays of tuples like [[price, size], ...]
        Example: {"bids": [{"price": "0.55", "size": "100"}, ...], ...}
        
        FIXED: Use asset_id as primary key, derive mid-price, call callback.
        """
        # CRITICAL: Use asset_id (token_id) as primary key, NOT market
        token_id = data.get("asset_id", "")
        if not token_id:
            token_id = data.get("market", "")
        
        if not token_id:
            return
            
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        
        # Parse as objects - Polymarket sends: [{"price": "0.55", "size": "100"}, ...]
        # Use None for missing sides (not 0.0)
        best_bid = None
        best_ask = None
        
        for lvl in bids:
            if isinstance(lvl, dict):
                price = float(lvl.get("price", 0))
                size = float(lvl.get("size", 0))
                if price and size:
                    self.order_book.update_bid(token_id, price, size)
                    if best_bid is None or price > best_bid:
                        best_bid = price
            elif isinstance(lvl, list):
                price, size = lvl
                try:
                    price = float(price)
                    size = float(size)
                except (ValueError, TypeError):
                    continue
                if price and size:
                    self.order_book.update_bid(token_id, price, size)
                    if best_bid is None or price > best_bid:
                        best_bid = price
        
        for lvl in asks:
            if isinstance(lvl, dict):
                try:
                    price = float(lvl.get("price", 0))
                    size = float(lvl.get("size", 0))
                except (ValueError, TypeError):
                    continue
                if price and size:
                    self.order_book.update_ask(token_id, price, size)
                    if best_ask is None or price < best_ask:
                        best_ask = price
            elif isinstance(lvl, list):
                price, size = lvl
                try:
                    price = float(price)
                    size = float(size)
                except (ValueError, TypeError):
                    continue
                if price and size:
                    self.order_book.update_ask(token_id, price, size)
                    if best_ask is None or price < best_ask:
                        best_ask = price
        
        self._last_book_update[token_id] = time.time()
        
        # DERIVE MID-PRICE: Only if both best_bid and best_ask exist
        mid_price = None
        if best_bid is not None and best_ask is not None:
            mid_price = (best_bid + best_ask) / 2
            # Sanity check: price should be between 0.01 and 0.99
            if 0.01 <= mid_price <= 0.99:
                # Call momentum callback with derived mid-price
                if self.price_callback:
                    try:
                        self.price_callback(token_id, mid_price)
                    except Exception:
                        pass  # Silent fail for callback
            else:
                # Price out of range, skip
                mid_price = None
        
        # Controlled debug logging (once per 5 seconds per token max)
        now = time.time()
        last_debug = getattr(self, '_last_snapshot_debug', {})
        last_ts = last_debug.get(token_id, 0)
        if now - last_ts >= 5:
            self._last_snapshot_debug = last_debug
            self._last_snapshot_debug[token_id] = now
            # Show None for missing sides
            bid_str = f"{best_bid:.4f}" if best_bid is not None else "None"
            ask_str = f"{best_ask:.4f}" if best_ask is not None else "None"
            mid_str = f"{mid_price:.4f}" if mid_price is not None else "None"
            print(f"[PRICE DEBUG] token={token_id[:20]}... mid={mid_str} best_bid={bid_str} best_ask={ask_str}")

    async def _handle_price_change(self, data: Dict):
        """Handle price_change event.
        
        Polymarket format: {"event_type": "price_change", "market": "...",
        "price_changes": [{"asset_id": "...", "price": "0.55", "size": "100",
        "side": "BUY", "best_bid": "0.9", "best_ask": "0.95"}, ...]}
        
        Price changes come as an ARRAY of updates, not a single object.
        """
        # FIXED: Handle array format - Polymarket sends price_changes as array
        price_changes = data.get("price_changes", [])
        
        if not isinstance(price_changes, list):
            price_changes = [price_changes]
        
        for change in price_changes:
            asset_id = change.get("asset_id", "")
            
            # Use None for missing values (not 0)
            try:
                price_val = change.get("price")
                price = float(price_val) if price_val not in (None, "", 0) else None
            except (ValueError, TypeError):
                price = None
            
            try:
                size_val = change.get("size")
                size = float(size_val) if size_val not in (None, "", 0) else 0
            except (ValueError, TypeError):
                size = 0
            
            side = change.get("side", "")
            
            # Use None for missing best_bid/best_ask
            try:
                bid_val = change.get("best_bid")
                best_bid = float(bid_val) if bid_val not in (None, "", 0) else None
            except (ValueError, TypeError):
                best_bid = None
            
            try:
                ask_val = change.get("best_ask")
                best_ask = float(ask_val) if ask_val not in (None, "", 0) else None
            except (ValueError, TypeError):
                best_ask = None
            
            if not asset_id:
                continue
            
            # FALLBACK: If no price in message, try best_bid/best_ask mid
            if price is None and best_bid is not None and best_ask is not None:
                price = (best_bid + best_ask) / 2
                # Also print diagnostic for this case
                if hasattr(self, '_mid_price_fallback_count'):
                    self._mid_price_fallback_count += 1
                else:
                    self._mid_price_fallback_count = 1
                if self._mid_price_fallback_count <= 3:
                    print(f"[CLOB FALLBACK] Using mid price: best_bid={best_bid}, best_ask={best_ask} -> price={price}")
            
            if price is None:
                continue
            
            # Update order book with best bid/ask (only if not None)
            if best_bid is not None:
                self.order_book.update_bid(asset_id, best_bid, size if side == "BUY" else 0)
            if best_ask is not None:
                self.order_book.update_ask(asset_id, best_ask, size if side == "SELL" else 0)
            
            # Also update at the traded price
            if side.upper() == "BUY":
                self.order_book.update_bid(asset_id, price, size)
            elif side.upper() == "SELL":
                self.order_book.update_ask(asset_id, price, size)
            
            self._last_book_update[asset_id] = time.time()
            
            # Call momentum callback if registered
            if self.price_callback:
                try:
                    # DEBUG: Print first few price updates to diagnose
                    if hasattr(self, '_price_callback_count'):
                        self._price_callback_count += 1
                    else:
                        self._price_callback_count = 1
                    
                    if self._price_callback_count <= 3:
                        print(f"[CLOB CALLBACK] asset_id={asset_id[:30]}... price={price}")
                    
                    self.price_callback(asset_id, price)
                except Exception as e:
                    print(f"[CLOB] Callback error: {e}")
        
        # Log occasionally
        if self.messages_received % 500 == 0:
            print(f"[CLOB] Price updates: {len(price_changes)} changes in batch")

    async def _handle_last_trade_price(self, data: Dict):
        """Handle last_trade_price event - actual trades/fills.
        
        This is how we detect whale trades for paper trading signals.
        Format: {"event_type": "last_trade_price", "asset_id": "...",
        "price": "0.55", "size": "500", "side": "buy"}
        
        Size is the notional (dollar value), not number of shares.
        """
        asset_id = data.get("asset_id", "")
        price = float(data.get("price", 0))
        size = float(data.get("size", 0))
        side = data.get("side", "")
        
        if not asset_id or not price or not size:
            return
        
        # Calculate notional (dollar value)
        # For Polymarket, size is typically the dollar notional
        notional = price * size
        
        # Only signal on significant trades (e.g., >= $50)
        if notional >= 50:
            await self._on_potential_whale_trade(asset_id, price, size, side)
        
        # Update order book - trades consume liquidity
        if side.lower() in ["buy", "bid"]:
            self.order_book.update_bid(asset_id, price, 0)  # Remove bid
        else:
            self.order_book.update_ask(asset_id, price, 0)  # Remove ask
        
        self._last_book_update[asset_id] = time.time()

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
                # Skip invalid price
                return
        
        if isinstance(size, str):
            try:
                size = float(size)
            except (ValueError, TypeError):
                # Skip invalid size
                return
        
        # Ensure they're numbers
        try:
            price = float(price) if price else 0
            size = float(size) if size else 0
        except (ValueError, TypeError, AttributeError):
            return
        
        if not price or not size:
            return
        
        if side.lower() == "bid":
            self.order_book.update_bid(asset_id, price, size)
        elif side.lower() == "ask":
            self.order_book.update_ask(asset_id, price, size)
        
        self._last_book_update[asset_id] = time.time()

    async def _handle_trade(self, data: Dict):
        """
        Handle trade/fill event.
        
        This is where we detect whale orders for paper trading.
        Since we're on the public market channel, we see all trades.
        """
        asset_id = data.get("asset_id", "")
        price = data.get("price", 0)
        size = data.get("size", 0)
        side = data.get("side", "")
        
        if not asset_id:
            return
        
        try:
            price = float(price)
            size = float(size)
        except (ValueError, TypeError):
            return
        
        # The public market channel sends ORDER BOOK updates, not actual trades.
        # To get trade signals, we need to detect significant price changes or use order flow.
        # For now: emit signals for ANY significant book movement (price changes).
        
        # Check for significant price update (could indicate whale activity)
        if price > 0 and size > 0:
            trade_value = price * size
            # Lower threshold to catch more opportunities
            if trade_value >= 10:  # $10 minimum
                await self._on_potential_whale_trade(asset_id, price, size, side)

    async def _on_potential_whale_trade(self, asset_id: str, price: float, size: float, side: str):
        """
        Handle potential whale trade.
        
        For paper trading: emit signal to whale tracker for copy trading.
        The whale_tracker will handle whether to copy or not based on settings.
        
        Key change: Emit signals for ANY large trade, not just tracked whales.
        The public market channel doesn't provide wallet identity, so we treat
        large trades as potential opportunities (anonymous whale copying).
        """
        # Find condition_id from our market cache (try both clobTokenIds and token_ids)
        condition_id = None
        market_title = "Unknown"
        outcome = "YES"  # Default
        
        # Calculate trade value in dollars
        trade_value = price * size
        
        # Only emit signals for significant trades (>$50) to avoid noise
        if trade_value < 50:
            return
        
        with self._cache_lock:
            for cid, market in self._market_cache.items():
                # Try clobTokenIds first (from Gamma)
                if market.get("yes_clob_token_id") == asset_id:
                    condition_id = cid
                    market_title = market.get("title", "")
                    outcome = "YES"
                    break
                elif market.get("no_clob_token_id") == asset_id:
                    condition_id = cid
                    market_title = market.get("title", "")
                    outcome = "NO"
                    break
                # Fallback to token_ids from py_clob_client
                elif market.get("yes_token_id") == asset_id:
                    condition_id = cid
                    market_title = market.get("title", "")
                    outcome = "YES"
                    break
                elif market.get("no_token_id") == asset_id:
                    condition_id = cid
                    market_title = market.get("title", "")
                    outcome = "NO"
                    break
        
        # If we can't find a matching market, create a synthetic signal anyway
        # This allows us to copy ANY large trade on Polymarket
        if not condition_id:
            # Use asset_id as a pseudo condition_id for tracking
            condition_id = f"clob_{asset_id[:40]}"
            market_title = f"Unknown Market ({asset_id[:20]}...)"
        
        timestamp = time.time()
        
        # Build signal for whale tracker
        signal = {
            "source_wallet": "clob_anonymous",  # Anonymous whale from public channel
            "condition_id": condition_id,
            "outcome": outcome,
            "whale_price": price,
            "size": size,
            "trade_value": trade_value,
            "timestamp": timestamp,
            "source": "clob_websocket",
            "market_title": market_title,
            "latency_ms": 0,  # Real-time from WebSocket
            "asset_id": asset_id,
            "side": side,
            "is_anonymous": True,  # Flag for anonymous whale
            "raw_data": {}
        }
        
        # Emit to whale_tracker
        try:
            if hasattr(self.whale_tracker, 'add_clob_signal'):
                self.whale_tracker.add_clob_signal(signal)
            elif hasattr(self.whale_tracker, 'add_signal'):
                self.whale_tracker.add_signal(signal)
        except Exception as e:
            print(f"[CLOB] Failed to emit signal: {e}")
            return

        self.signals_emitted += 1
        self.last_signal_time = time.time()
        
        print(f"[CLOB] 🔥 LARGE TRADE: {side} ${trade_value:.0f} @ ${price:.3f} "
              f"(market: {market_title[:25]}, asset: {asset_id[:15]}...)")

    def get_stats(self) -> Dict[str, Any]:
        """Get monitor statistics."""
        return {
            "connected": self.connected,
            "tracked_wallets": len(self.tracked_wallets),
            "signals_emitted": self.signals_emitted,
            "messages_received": self.messages_received,
            "errors": self.errors,
            "last_signal_time": self.last_signal_time,
            "last_message_time": self.last_message_time,
            "latency_target": "100-300ms",
            "ws_url": self.ws_url,
            "markets_tracked": len(self._market_cache)
        }
    
    def get_order_book(self, token_id: str) -> Dict:
        """Get current order book snapshot for a token."""
        return self.order_book.get_order_book_snapshot(token_id)
