#!/usr/bin/env python3
"""
Standalone CLOB Price Streamer

Usage:
    python clob_price_stream.py
    
Or with specific token IDs:
    python clob_price_stream.py 0xACA5AEDF274BE4876EED3441D1CD4C9F5F3908EE2D5DFF4C838BA89CD69365D8

Press Ctrl+C to stop.
"""

import asyncio
import json
import sys
import websockets
import requests
from datetime import datetime

CLOB_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA_API_URL = "https://gamma-api.polymarket.com/markets"

# Default: Fetch active markets if no tokens provided
DEFAULT_TOKEN = None

def get_active_token_ids(limit=10):
    """Fetch active token IDs from Gamma API."""
    try:
        # Get active markets (closed=false)
        params = {
            "closed": "false",
            "limit": limit,
            "active": "true"
        }
        resp = requests.get(GAMMA_API_URL, params=params, timeout=10)
        markets = resp.json()
        
        token_ids = []
        for m in markets:
            # clobTokenIds is a JSON string, not a list - need to parse it
            yes_token_str = m.get("clobTokenIds", "[]")
            no_token_str = m.get("noClobTokenIds", "[]")
            
            # Parse JSON strings
            try:
                yes_tokens = json.loads(yes_token_str) if yes_token_str else []
                no_tokens = json.loads(no_token_str) if no_token_str else []
            except:
                yes_tokens = []
                no_tokens = []
            
            if yes_tokens and len(yes_tokens) > 0:
                token_ids.append(yes_tokens[0])
            if no_tokens and len(no_tokens) > 0:
                token_ids.append(no_tokens[0])
        
        return token_ids[:20]  # Limit to 20 tokens
    except Exception as e:
        print(f"⚠️  Failed to fetch markets: {e}")
        # Fallback to a known active token
        return ["29737896427505913579373080975459102667589461703616780283220419284622020368836"]

async def stream_prices(token_ids=None):
    """Connect to CLOB WebSocket and stream price updates."""
    
    if not token_ids:
        print("📡 Fetching active markets from Gamma...")
        token_ids = get_active_token_ids(10)
        print(f"   Found {len(token_ids)} active tokens")
    
    print(f"📡 Connecting to Polymarket CLOB...")
    print(f"   URL: {CLOB_WS_URL}")
    print(f"   Tokens: {len(token_ids)}")
    print(f"   Press Ctrl+C to stop\n")
    print("-" * 80)
    
    try:
        async with websockets.connect(CLOB_WS_URL) as ws:
            # Subscribe to market channel
            subscribe_msg = {
                "type": "market",
                "assets_ids": token_ids
            }
            await ws.send(json.dumps(subscribe_msg))
            print(f"✅ Subscribed to market channel\n")
            
            msg_count = 0
            async for message in ws:
                msg_count += 1
                try:
                    data = json.loads(message)
                    
                    # Handle array of messages
                    if isinstance(data, list):
                        for item in data:
                            print_message(item, msg_count)
                    else:
                        print_message(data, msg_count)
                        
                except json.JSONDecodeError:
                    print(f"❌ Invalid JSON: {message[:100]}")
                    
    except websockets.exceptions.ConnectionClosed as e:
        print(f"🔌 Connection closed: {e}")
    except Exception as e:
        print(f"❌ Error: {e}")

def print_message(data, msg_num):
    """Format and print a price message."""
    if not isinstance(data, dict):
        return
    
    event_type = data.get("event_type", "")
    market = data.get("market", "")[:20] + "..."
    
    if event_type == "price_change":
        changes = data.get("price_changes", [])
        for change in changes[:3]:  # Show max 3
            asset = change.get("asset_id", "")[:12] + "..."
            price = change.get("price", "N/A")
            size = change.get("size", "0")
            side = change.get("side", "")
            best_bid = change.get("best_bid", "N/A")
            best_ask = change.get("best_ask", "N/A")
            
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"[{ts}] 📊 {asset} | {side:4} | price=${price:>6} | size=${size:>8} | bid=${best_bid:>5} ask=${best_ask:>5}")
    
    elif event_type == "book":
        bids = len(data.get("bids", []))
        asks = len(data.get("asks", []))
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 📖 Book snapshot: {market} | bids={bids} asks={asks}")
    
    elif event_type == "last_trade_price":
        asset = data.get("asset_id", "")[:12] + "..."
        price = data.get("price", "N/A")
        size = data.get("size", "0")
        side = data.get("side", "")
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] 💰 TRADE: {asset} | {side:4} | ${price} x {size}")

if __name__ == "__main__":
    tokens = sys.argv[1:] if len(sys.argv) > 1 else None
    print("🟢 Polymarket CLOB Price Streamer")
    print("=" * 80)
    
    try:
        asyncio.run(stream_prices(tokens))
    except KeyboardInterrupt:
        print("\n\n👋 Stopped by user")
