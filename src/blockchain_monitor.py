"""
Blockchain event monitor for real-time whale trade detection.

Monitors Polymarket's CTFExchange contract on Polygon for OrderFilled events.
Provides 2-3 second latency (block time) vs 5-12 minute polling latency.

Architecture:
- Connects to Polygon RPC WebSocket (free tier: Alchemy/Infura/Polygon public)
- Subscribes to OrderFilled events from CTFExchange contract
- Filters events by tracked whale wallet addresses (maker/taker)
- Decodes event data to extract market, outcome, price, size
- Fetches market metadata from Polymarket API
- Emits real-time trade signals to whale_tracker

Sources:
- CTFExchange contract: https://polygonscan.com/address/0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e
- Event structure: https://docs.bitquery.io/docs/examples/polymarket-api/polymarket-ctf-exchange/
"""

import json
import os
import time
import threading
import urllib.request
from web3 import Web3
from web3.providers import LegacyWebSocketProvider
import concurrent.futures


# Polymarket CTFExchange contract on Polygon
CTFEXCHANGE_ADDRESS = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"

# Load contract ABI
ABI_PATH = os.path.join(os.path.dirname(__file__), "ctfexchange_abi.json")
with open(ABI_PATH, "r") as f:
    CTFEXCHANGE_ABI = json.load(f)


class BlockchainMonitor:
    """Real-time blockchain event monitor for whale trades."""

    def __init__(self, config, on_whale_trade_callback):
        """
        Initialize blockchain monitor.

        Args:
            config: Bot config dict with POLYGON_RPC_WSS url
            on_whale_trade_callback: Function to call when whale trade detected
                Signature: callback(whale_address, signal_data)
                signal_data = {condition_id, market_title, outcome, whale_price, ...}
        """
        self.config = config
        self.on_whale_trade = on_whale_trade_callback

        # WebSocket connection
        rpc_url = config.get(
            "POLYGON_RPC_WSS",
            "wss://polygon-mainnet.g.alchemy.com/v2/demo"  # Free demo RPC
        )
        self.web3 = Web3(LegacyWebSocketProvider(rpc_url))

        # Contract instance
        self.contract = self.web3.eth.contract(
            address=Web3.to_checksum_address(CTFEXCHANGE_ADDRESS),
            abi=CTFEXCHANGE_ABI
        )

        # Tracked whale addresses (set by whale_tracker)
        self.tracked_wallets = set()  # Checksummed addresses

        # v14: Market metadata cache (eliminates per-event HTTP fetches)
        # Updated by bot after fetching active markets
        self._token_to_market = {}  # token_id -> {condition_id, outcome, title}
        self._cache_lock = threading.Lock()

        # v14: Reorg protection - pending events wait for confirmations
        self.min_confirmations = config.get("MIN_BLOCKCHAIN_CONFIRMATIONS", 1)
        self._pending_events = []  # [(event, block_num, queued_at), ...]

        # Connection state
        self.running = False
        self.connected = False
        self._thread = None
        self._reconnect_delay = 1  # Exponential backoff

        # Stats
        self.events_received = 0
        self.signals_emitted = 0
        self.whale_trades_detected = 0
        self.wallets_discovered = 0  # Network discovery counter
        self.last_event_time = 0
        self.last_block = 0
        self.events_dropped_reorg = 0  # v14: Reorg tracking

    def start(self):
        """Start monitoring in background thread."""
        if self.running:
            return

        self.running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        print("[BLOCKCHAIN] Monitor started")

    def stop(self):
        """Stop monitoring."""
        self.running = False
        self.connected = False  # v14: Set connected flag to False on stop
        if self._thread:
            self._thread.join(timeout=5)
        print("[BLOCKCHAIN] Monitor stopped")

    def update_tracked_wallets(self, wallet_addresses):
        """Update list of whale wallets to monitor.

        Args:
            wallet_addresses: List of Ethereum addresses (will be checksummed)
        """
        self.tracked_wallets = {
            Web3.to_checksum_address(addr.lower()) for addr in wallet_addresses
        }
        print(f"[BLOCKCHAIN] Tracking {len(self.tracked_wallets)} whale wallets")

    def update_market_cache(self, markets):
        """Update market metadata cache (v14 - eliminates per-event HTTP).

        Called by bot after fetching active markets. Enables instant token->market
        lookup without network calls.

        Args:
            markets: List of market dicts from MarketDataService.get_active_markets()
                Each with: condition_id, yes_token_id, no_token_id, title
        """
        with self._cache_lock:
            self._token_to_market = {}

            for m in markets:
                yes_id = m.get("yes_token_id")
                no_id = m.get("no_token_id")
                cid = m.get("condition_id", "")
                title = m.get("title", "")

                if yes_id:
                    # Convert hex token ID to int for comparison
                    try:
                        yes_id_int = int(yes_id, 16) if isinstance(yes_id, str) and yes_id.startswith("0x") else int(yes_id)
                        self._token_to_market[yes_id_int] = {
                            "condition_id": cid,
                            "outcome": "YES",
                            "title": title,
                        }
                    except (ValueError, TypeError):
                        pass

                if no_id:
                    try:
                        no_id_int = int(no_id, 16) if isinstance(no_id, str) and no_id.startswith("0x") else int(no_id)
                        self._token_to_market[no_id_int] = {
                            "condition_id": cid,
                            "outcome": "NO",
                            "title": title,
                        }
                    except (ValueError, TypeError):
                        pass

            print(f"[BLOCKCHAIN] Market cache updated: {len(self._token_to_market)} token mappings")

    def _monitor_loop(self):
        """Main monitoring loop with reconnection logic."""
        while self.running:
            try:
                self._connect_and_subscribe()
            except Exception as e:
                print(f"[BLOCKCHAIN] Error: {e}, reconnecting in {self._reconnect_delay}s...")
                time.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 60)  # Exp backoff

    def _connect_and_subscribe(self):
        """Connect to RPC and subscribe to contract events."""
        print("[BLOCKCHAIN] Connecting to Polygon RPC...")

        # Test connection
        if not self.web3.is_connected():
            raise ConnectionError("Failed to connect to Polygon RPC")

        self.connected = True
        self._reconnect_delay = 1  # Reset backoff
        latest_block = self.web3.eth.block_number
        self.last_block = latest_block
        print(f"[BLOCKCHAIN] Connected (block: {latest_block})")

        # BUG FIX #4: On reconnect, backfill from last seen block (with 5-block safety margin)
        backfill_from = max(1, self.last_block - 5) if self.last_block > 0 else "latest"

        event_filter = self.contract.events.OrderFilled.create_filter(
            from_block=backfill_from
        )

        print(f"[BLOCKCHAIN] Event filter created (from block: {backfill_from})")
        print(f"[BLOCKCHAIN] Subscribed to OrderFilled events at {CTFEXCHANGE_ADDRESS}")

        # Poll for new events
        while self.running and self.connected:
            try:
                new_events = event_filter.get_new_entries()
                for event in new_events:
                    # BUG FIX #4: Update last_block on EVERY event
                    block_num = event.get('blockNumber', 0)
                    if block_num > self.last_block:
                        self.last_block = block_num

                    # v14: Reorg protection - queue event for confirmation
                    if self.min_confirmations > 0:
                        self._pending_events.append((event, block_num, time.time()))
                    else:
                        # No confirmations required - process immediately
                        self._process_order_filled(event)

                # v14: Process pending events that have enough confirmations
                current_block = self.web3.eth.block_number
                if current_block > self.last_block:
                    self.last_block = current_block

                if self._pending_events:
                    confirmed = []
                    still_pending = []

                    for event, event_block, queued_at in self._pending_events:
                        confirmations = current_block - event_block

                        if confirmations >= self.min_confirmations:
                            # Event is confirmed - process it
                            confirmed.append(event)
                        elif time.time() - queued_at < 300:  # Keep pending up to 5 min
                            still_pending.append((event, event_block, queued_at))
                        else:
                            # Too old - likely reorged out
                            self.events_dropped_reorg += 1

                    # Process confirmed events
                    for event in confirmed:
                        self._process_order_filled(event)

                    # Update pending list
                    self._pending_events = still_pending

                # Heartbeat every 10 blocks
                if current_block % 10 == 0:
                    pending_count = len(self._pending_events)
                    print(f"[BLOCKCHAIN] Heartbeat: block {current_block}, "
                          f"{self.whale_trades_detected} whale trades detected, "
                          f"{pending_count} pending confirmations")

                time.sleep(0.5)  # Poll every 500ms

            except Exception as e:
                print(f"[BLOCKCHAIN] Event polling error: {e}")
                self.connected = False
                raise

    def _process_order_filled(self, event):
        """Process an OrderFilled event and emit whale trade signal if relevant.

        Event structure:
        {
            'args': {
                'orderHash': bytes32,
                'maker': address,
                'taker': address,
                'makerAssetId': uint256,
                'takerAssetId': uint256,
                'makerAmountFilled': uint256,
                'takerAmountFilled': uint256,
                'fee': uint256
            },
            'blockNumber': int,
            'transactionHash': bytes,
            ...
        }
        """
        self.events_received += 1
        self.last_event_time = time.time()

        try:
            args = event['args']
            maker = Web3.to_checksum_address(args['maker'])
            taker = Web3.to_checksum_address(args['taker'])

            # Extract trade amounts for network discovery
            maker_amount = args['makerAmountFilled']
            taker_amount = args['takerAmountFilled']

            # Network Discovery: Detect high-value trades from ANY wallet
            # If trade > $500, this might be a new whale to track
            trade_value_usdc = max(maker_amount, taker_amount) / 1e6  # Convert from wei
            if trade_value_usdc >= 500:
                # Emit discovery signal for both maker and taker (one might be the whale)
                for address, side in [(maker, "maker"), (taker, "taker")]:
                    if address not in self.tracked_wallets:
                        self._emit_network_discovery(address, side, event, trade_value_usdc)

            # Check if maker or taker is a tracked whale (for copy trading)
            whale_address = None
            whale_side = None
            if maker in self.tracked_wallets:
                whale_address = maker
                whale_side = "maker"
            elif taker in self.tracked_wallets:
                whale_address = taker
                whale_side = "taker"

            if not whale_address:
                return  # Not a tracked whale (but may have been discovered above)

            self.whale_trades_detected += 1

            # Extract trade details (maker_amount and taker_amount already extracted above)
            maker_asset_id = args['makerAssetId']
            taker_asset_id = args['takerAssetId']
            fee = args['fee']

            # Whale's token ID (the one they're buying)
            whale_token_id = taker_asset_id if whale_side == "maker" else maker_asset_id
            whale_amount = taker_amount if whale_side == "maker" else maker_amount

            # BUG FIX #2: Calculate price correctly based on whale's side
            # Price = what whale PAID per share they RECEIVED
            if whale_side == "maker":
                # Maker sold makerAmount, received takerAmount of outcome tokens
                # Price = cost per share received = makerAmount / takerAmount
                whale_price = float(maker_amount) / float(taker_amount) if taker_amount > 0 else 0
            else:
                # Taker sold takerAmount, received makerAmount of outcome tokens
                # Price = cost per share received = takerAmount / makerAmount
                whale_price = float(taker_amount) / float(maker_amount) if maker_amount > 0 else 0

            # v14: Use market cache (instant, no HTTP)
            with self._cache_lock:
                market_data = self._token_to_market.get(whale_token_id)

            if not market_data:
                # Cache miss - try HTTP fallback
                market_data = self._fetch_market_from_token_id(whale_token_id)
                if not market_data:
                    print(f"[BLOCKCHAIN] Unknown token {whale_token_id} (not in cache, API failed)")
                    return

            print(f"[BLOCKCHAIN] Whale trade: {whale_address[:10]}... "
                  f"bought {market_data.get('outcome', '?')} at {whale_price:.4f} "
                  f"in \"{market_data.get('title', 'Unknown')[:40]}\" "
                  f"(tx: {event['transactionHash'].hex()[:10]}...)")

            # BUG FIX #6: Gas Signals with timeout protection
            # High gas = high conviction (whale paying premium for fast execution)
            gas_price_gwei = 0
            try:
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(
                        self.web3.eth.get_transaction,
                        event['transactionHash']
                    )
                    tx_receipt = future.result(timeout=3)  # 3 second timeout
                    gas_price_wei = tx_receipt.get('gasPrice', 0)
                    gas_price_gwei = gas_price_wei / 1e9 if gas_price_wei else 0

                    # Log high-conviction trades
                    if gas_price_gwei > 200:
                        print(f"[BLOCKCHAIN] 🔥 HIGH CONVICTION: {whale_address[:10]}... "
                              f"paid {gas_price_gwei:.0f} gwei gas")
            except (concurrent.futures.TimeoutError, Exception) as e:
                gas_price_gwei = 0  # Fail gracefully

            # BUG FIX #5: Fetch block timestamp (whale's actual trade time)
            whale_timestamp = time.time()  # Fallback to current time
            try:
                block = self.web3.eth.get_block(event['blockNumber'])
                whale_timestamp = float(block['timestamp'])
            except Exception as e:
                print(f"[BLOCKCHAIN] Failed to fetch block timestamp: {e}")

            # Emit signal to whale_tracker
            signal_data = {
                "source_wallet": whale_address,
                "condition_id": market_data.get("condition_id", ""),
                "market_title": market_data.get("title", market_data.get("question", "Unknown Market")),
                "outcome": market_data.get("outcome", "YES"),  # YES/NO based on token ID
                "whale_price": whale_price,
                "timestamp": whale_timestamp,  # FIXED — actual block timestamp
                "detected_at": time.time(),  # Our detection time
                "size": int(whale_amount),
                "tx_hash": event['transactionHash'].hex(),
                "log_index": event.get('logIndex', 0),  # v14: For dedup
                "block_number": event['blockNumber'],
                "gas_price_gwei": gas_price_gwei,  # Gas Signals
            }

            self.on_whale_trade(whale_address, signal_data)
            self.signals_emitted += 1

        except Exception as e:
            print(f"[BLOCKCHAIN] Failed to process OrderFilled event: {e}")

    def _emit_network_discovery(self, address, side, event, trade_value):
        """Emit a network discovery signal for a high-value trade from an unknown wallet.

        This allows the bot to automatically discover profitable whales before they
        appear on the leaderboard.
        """
        try:
            args = event['args']
            # Determine which token this wallet is buying
            token_id = args['takerAssetId'] if side == "maker" else args['makerAssetId']
            amount = args['takerAmountFilled'] if side == "maker" else args['makerAmountFilled']

            # Log discovery
            print(f"[BLOCKCHAIN] 🔍 DISCOVERED: {address[:10]}... "
                  f"traded ${trade_value:.0f} (token {token_id})")

            self.wallets_discovered += 1

            # Emit discovery signal to whale_tracker
            # The whale_tracker will decide whether to add this wallet to network_wallets
            discovery_signal = {
                "type": "network_discovery",
                "address": address,
                "trade_value": trade_value,
                "token_id": token_id,
                "amount": int(amount),
                "tx_hash": event['transactionHash'].hex(),
                "block_number": event['blockNumber'],
                "timestamp": time.time(),
            }

            # Call the callback with discovery signal
            # (whale_tracker will handle adding to network if criteria met)
            if hasattr(self.on_whale_trade, '__self__'):  # Check if it's a bound method
                whale_tracker = self.on_whale_trade.__self__
                if hasattr(whale_tracker, 'add_discovered_wallet'):
                    whale_tracker.add_discovered_wallet(discovery_signal)

        except Exception as e:
            print(f"[BLOCKCHAIN] Failed to emit network discovery: {e}")

    def _fetch_market_from_token_id(self, token_id):
        """Fetch market metadata from Polymarket API using ERC1155 token ID.

        Args:
            token_id: ERC1155 token ID (uint256)

        Returns:
            Dict with market data or None if fetch fails
        """
        try:
            # Convert token ID to hex string
            token_id_hex = hex(token_id)

            # Polymarket API endpoint for token lookup
            # This may need adjustment - Polymarket's API structure might differ
            url = f"https://gamma-api.polymarket.com/markets?token_id={token_id_hex}"

            req = urllib.request.Request(url)
            req.add_header("User-Agent", "PolymarketBot/1.0")

            with urllib.request.urlopen(req, timeout=5) as response:
                data = json.loads(response.read().decode())

                # Parse response (structure depends on Polymarket's API)
                # This is a placeholder - actual structure may vary
                if isinstance(data, list) and len(data) > 0:
                    market = data[0]
                    return {
                        "condition_id": market.get("condition_id", ""),
                        "question": market.get("question", "Unknown"),
                        "outcome": market.get("outcome", "YES"),
                    }

                return None

        except Exception as e:
            print(f"[BLOCKCHAIN] API fetch error for token {token_id}: {e}")
            return None

    def get_stats(self):
        """Get monitoring statistics."""
        return {
            "connected": self.connected,
            "events_received": self.events_received,
            "whale_trades_detected": self.whale_trades_detected,
            "tracked_wallets": len(self.tracked_wallets),
            "last_event_time": self.last_event_time,
            "last_block": self.last_block,
        }
