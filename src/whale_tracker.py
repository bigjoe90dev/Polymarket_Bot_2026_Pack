"""Whale tracker: discover and copy mid-tier profitable Polymarket traders.

Mass tracking strategy:
- Paginate the leaderboard to find 300+ traders with $3k-$10k monthly PnL
- Poll their activity round-robin (one wallet per bot cycle)
- Network discovery: when a tracked trader trades a market, find other
  profitable wallets in that same market and add them to the pool

Uses the public Polymarket Data API (no auth required):
- Leaderboard: GET https://data-api.polymarket.com/v1/leaderboard
- Activity:    GET https://data-api.polymarket.com/activity?user=<wallet>
- Trades:      GET https://data-api.polymarket.com/trades?market=<conditionId>

All endpoints are public. No wallet or API key needed.
"""

import json
import time
import os
import requests
import queue
import threading
import hashlib
from src.state_backup import save_state_with_backup, load_state_with_recovery

DATA_API = "https://data-api.polymarket.com"
WHALE_STATE_FILE = "data/whale_state.json"
LEADERBOARD_REFRESH = 3600          # Re-fetch leaderboard every hour
ACTIVITY_POLL_INTERVAL = 1          # Poll each wallet every 1 second (was 2)
MAX_TRACKED_WALLETS = 1000          # Cap to avoid state file bloat
MIN_TRADE_SIZE_USDC = 25            # Copy trades > $25
LEADERBOARD_PAGE_SIZE = 50          # API max per page
PNL_MIN = 3000                      # Min monthly PnL to track ($3k)
PNL_MAX = 999999                    # No ceiling — forensic filters handle quality
MIN_VOLUME = 5000                   # Min monthly volume ($5k = active trader)
MIN_PNL_RATIO = 0.05                # Farmer Test: PnL/Volume > 5% (filters volume farmers)
MAX_INACTIVE_DAYS = 7               # Skip wallets with no trade in 7 days
NETWORK_DISCOVERY_INTERVAL = 1800   # Network scan every 30 min
NETWORK_DISCOVERY_MIN_PNL = 1000    # Min PnL for network-discovered wallets
MIN_AVG_HOLD_HOURS = 0.25            # Swing Test: avg hold > 15min (catches micro-bots, allows 15-min markets)
SEED_HISTORY_LIMIT = 50             # Trades to fetch for forensic analysis
MAX_WASH_RATIO = 0.30               # Wash Test: max % of round-trip trades
MIN_SLOW_RATIO = 0.70                # Skip wallets where >70% of trades are in slow/long-term markets
SIGNAL_DEDUP_TTL = 1800              # 30 minutes - global signal dedup time-to-live


class WhaleTracker:
    """Discovers and monitors profitable Polymarket traders for copy trading."""

    def __init__(self, config, wallet_scorer=None):
        self.config = config
        self.scorer = wallet_scorer
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})
        self.tracked_wallets = {}       # proxy_wallet -> wallet_info
        self.network_wallets = {}       # wallets discovered via network (separate pool)
        self.recent_signals = []        # Copy signals for display/history
        self._last_leaderboard_fetch = 0
        self._last_network_scan = 0
        self._seen_tx_hashes = set()
        self._hot_markets = {}          # condition_id -> set of wallets trading it
        self._poll_errors = 0
        self._slow_skips = 0            # Markets skipped for being slow
        self._discovery_stats = {"leaderboard": 0, "network": 0, "pages_fetched": 0}

        # BUG FIX #1: Thread-safe blockchain signal queue
        self._blockchain_queue = queue.Queue()  # For real-time blockchain signals
        self._blockchain_lock = threading.Lock()  # Protects _seen_tx_hashes

        # POLY-101: CLOB WebSocket signal queue (300ms latency)
        self._clob_queue = queue.Queue()  # For CLOB WebSocket signals

        # v14 ENHANCEMENT: Global signal deduplication across all sources
        # Prevents duplicate trades when same signal arrives via:
        # - Blockchain + API polling
        # - Reconnect backfill
        # - Provider duplicates
        self._signal_dedup_cache = {}  # signal_id -> timestamp
        self._signal_dedup_lock = threading.Lock()

        self._load_state()

    # ── State Persistence ─────────────────────────────────────

    def _load_state(self):
        state = load_state_with_recovery(
            WHALE_STATE_FILE,
            required_keys=["tracked_wallets", "network_wallets"]
        )

        if not state:
            print("[WHALE] No valid state found — starting fresh")
            return

        self.tracked_wallets = state.get("tracked_wallets", {})
        self.network_wallets = state.get("network_wallets", {})
        self._seen_tx_hashes = set(state.get("seen_tx_hashes", []))
        self._hot_markets = {
            k: set(v) for k, v in state.get("hot_markets", {}).items()
        }

        print(f"[WHALE] Loaded {len(self.tracked_wallets)} leaderboard + "
              f"{len(self.network_wallets)} network wallets, "
              f"{len(self._seen_tx_hashes)} seen txs")

    def _save_state(self):
        try:
            # Snapshot dicts to avoid "dictionary changed size" during iteration
            # (watchdog thread may call _save_state while main thread modifies dicts)
            tracked_snap = dict(self.tracked_wallets)
            network_snap = dict(self.network_wallets)

            # Trim seen hashes for state FILE only — never touch the in-memory set
            # (destroying the in-memory set causes historical trades to re-fire as signals)
            seen_list = list(self._seen_tx_hashes)
            if len(seen_list) > 50000:
                seen_list = seen_list[-50000:]

            # Trim hot_markets to last 100 markets
            hot_snap = dict(list(self._hot_markets.items())[-100:])

            state = {
                "version": 1,
                "tracked_wallets": tracked_snap,
                "network_wallets": network_snap,
                "seen_tx_hashes": seen_list,
                "hot_markets": {k: list(v) for k, v in hot_snap.items()},
                "last_updated": time.time(),
            }

            save_state_with_backup(WHALE_STATE_FILE, state, generations=5)

        except Exception as e:
            print(f"[!] Whale state save error: {e}")

    # ── Leaderboard Discovery (paginated) ────────────────────

    def discover_whales(self):
        """Paginate leaderboard to find all traders with $3k-$10k monthly PnL."""
        now = time.time()
        if now - self._last_leaderboard_fetch < LEADERBOARD_REFRESH:
            return

        print("[WHALE] Scanning leaderboard for $3k-$10k/month traders...")

        all_traders = []
        offset = 0
        pages = 0
        empty_streak = 0

        while True:
            try:
                resp = self._session.get(
                    f"{DATA_API}/v1/leaderboard",
                    params={
                        "timePeriod": "MONTH",
                        "orderBy": "PNL",
                        "limit": LEADERBOARD_PAGE_SIZE,
                        "offset": offset,
                    },
                    timeout=15,
                )
                resp.raise_for_status()
                traders = resp.json()
            except Exception as e:
                print(f"[!] Leaderboard page {pages} failed: {e}")
                break

            if not isinstance(traders, list) or len(traders) == 0:
                break

            pages += 1

            # Filter by PnL range + activity metrics
            qualified = []
            below_min = 0
            filtered_out = 0
            for t in traders:
                pnl = float(t.get("pnl", 0))
                vol = float(t.get("vol", 0))

                if pnl < PNL_MIN:
                    below_min += 1
                    continue
                if pnl > PNL_MAX:
                    continue

                # Activity filters
                if vol < MIN_VOLUME:
                    filtered_out += 1
                    continue
                if vol > 0 and (pnl / vol) < MIN_PNL_RATIO:
                    filtered_out += 1
                    continue

                qualified.append(t)

            all_traders.extend(qualified)

            # If most traders on this page are below our min, we're done
            if below_min > LEADERBOARD_PAGE_SIZE * 0.8:
                empty_streak += 1
                if empty_streak >= 2:
                    break
            else:
                empty_streak = 0

            # Safety cap
            if len(all_traders) >= MAX_TRACKED_WALLETS or pages >= 50:
                break

            offset += LEADERBOARD_PAGE_SIZE
            time.sleep(0.3)  # Gentle rate limiting

        self._discovery_stats["pages_fetched"] = pages

        # Register discovered traders
        new_wallets = []
        for trader in all_traders:
            # BUG FIX #7: Normalize wallet address to lowercase
            wallet = trader.get("proxyWallet", "").lower()
            if not wallet:
                continue

            is_new = wallet not in self.tracked_wallets
            self.tracked_wallets[wallet] = {
                "proxy_wallet": wallet,
                "username": trader.get("userName", ""),
                "pnl": trader.get("pnl", 0),
                "volume": trader.get("vol", 0),
                "rank": trader.get("rank", "?"),
                "source": "leaderboard",
                "last_poll": self.tracked_wallets.get(wallet, {}).get("last_poll", 0),
                "trades_copied": self.tracked_wallets.get(wallet, {}).get("trades_copied", 0),
            }

            if is_new:
                new_wallets.append(wallet)

        self._last_leaderboard_fetch = now
        self._discovery_stats["leaderboard"] = len(self.tracked_wallets)

        # Seed history for new wallets and apply forensic filters
        seeded = 0
        filter_counts = {"inactive": 0, "hft": 0, "wash": 0, "farmer": 0, "slow_market": 0}
        for wallet in new_wallets:
            result = self._seed_history(wallet)
            seeded += 1
            if result != "active":
                self.tracked_wallets.pop(wallet, None)
                if result in filter_counts:
                    filter_counts[result] += 1
            if seeded % 20 == 0:
                time.sleep(0.5)  # Pace the seeding

        self._save_state()
        self._discovery_stats["filters"] = filter_counts

        removed = sum(filter_counts.values())
        print(f"[WHALE] Leaderboard: {len(all_traders)} qualified, "
              f"{len(self.tracked_wallets)} active wallets tracked ({pages} pages)")
        if removed > 0:
            print(f"[WHALE] Filters: {filter_counts['inactive']} inactive, "
                  f"{filter_counts['hft']} HFT/scalpers, "
                  f"{filter_counts['wash']} wash traders, "
                  f"{filter_counts['farmer']} farmers, "
                  f"{filter_counts['slow_market']} slow-market removed")

    def _seed_history(self, wallet):
        """Fetch recent trades, mark as seen, and apply forensic filters.

        Returns 'active' if wallet passes all checks, or a filter reason string
        ('inactive', 'hft', 'wash') if it fails.
        """
        trades = self._fetch_recent_activity(wallet)
        if not trades:
            return "inactive"

        now = time.time()
        most_recent = 0
        for trade in trades:
            tx_hash = trade.get("transactionHash", "")
            if tx_hash:
                self._seen_tx_hashes.add(tx_hash)
            ts = float(trade.get("timestamp", 0))
            if ts > most_recent:
                most_recent = ts

        # Check recency
        if most_recent > 0:
            days_ago = (now - most_recent) / 86400
            if days_ago > MAX_INACTIVE_DAYS:
                return "inactive"
        else:
            return "inactive"

        # ── Swing Test: filter out HFT/scalpers ──
        # Calculate avg hold time from BUY→SELL pairs on same market
        by_market = {}
        timestamps = []
        for t in trades:
            cid = t.get("conditionId", "")
            if cid:
                by_market.setdefault(cid, []).append(t)
            ts = float(t.get("timestamp", 0))
            if ts > 0:
                timestamps.append(ts)

        hold_hours = []
        for cid, market_trades in by_market.items():
            market_trades.sort(key=lambda x: float(x.get("timestamp", 0)))
            last_buy_ts = None
            for t in market_trades:
                side = t.get("side", "").upper()
                ts = float(t.get("timestamp", 0))
                if side == "BUY":
                    last_buy_ts = ts
                elif side == "SELL" and last_buy_ts is not None:
                    hold_h = (ts - last_buy_ts) / 3600.0
                    if hold_h > 0:
                        hold_hours.append(hold_h)
                    last_buy_ts = None

        if len(hold_hours) >= 3:
            avg_hold = sum(hold_hours) / len(hold_hours)
            if avg_hold < MIN_AVG_HOLD_HOURS:
                return "hft"
        elif len(timestamps) >= 20:
            # Fallback: if 20+ trades packed into < 2 hours, it's a bot
            timestamps.sort()
            span_hours = (timestamps[-1] - timestamps[0]) / 3600.0
            if span_hours > 0 and span_hours < 2:
                return "hft"

        # ── Wash Test: detect round-trip wash trading ──
        # BUY + SELL same market within 30 min at similar price = suspicious
        wash_count = 0
        total_pairs = 0
        for cid, market_trades in by_market.items():
            market_trades.sort(key=lambda x: float(x.get("timestamp", 0)))
            for i, t1 in enumerate(market_trades):
                if t1.get("side", "").upper() != "BUY":
                    continue
                buy_price = float(t1.get("price", 0))
                buy_ts = float(t1.get("timestamp", 0))
                if buy_price <= 0:
                    continue
                for t2 in market_trades[i + 1:]:
                    if t2.get("side", "").upper() != "SELL":
                        continue
                    sell_price = float(t2.get("price", 0))
                    sell_ts = float(t2.get("timestamp", 0))
                    if sell_price <= 0:
                        continue
                    total_pairs += 1
                    time_gap_min = (sell_ts - buy_ts) / 60.0
                    price_diff_pct = abs(sell_price - buy_price) / buy_price
                    if time_gap_min <= 30 and price_diff_pct < 0.03:
                        wash_count += 1
                    break  # Only match first SELL after each BUY

        if total_pairs >= 3 and (wash_count / total_pairs) > MAX_WASH_RATIO:
            return "wash"

        # ── Slow Market Filter: skip wallets dominated by long-term markets ──
        # We want wallets that trade markets settling within 24h (crypto, sports, etc.)
        # Wallets that mostly trade multi-month markets (elections, prices by June) are
        # useless for copy trading — positions take months to resolve
        if self.scorer and len(trades) >= 3:
            slow_count = sum(1 for t in trades
                             if self.scorer.classify_market(t.get("title", "")) == "slow")
            slow_ratio = slow_count / len(trades)
            if slow_ratio > MIN_SLOW_RATIO:
                return "slow_market"

        return "active"

    # ── Network Discovery ("copy who they copy") ────────────

    def discover_network(self):
        """Find profitable traders in the same markets as our tracked traders.

        When a tracked trader makes a trade on a market, we look at who else
        is trading that same market. If they're profitable, we add them too.
        This is the 'copy whoever they are copying' feature.
        """
        now = time.time()
        if now - self._last_network_scan < NETWORK_DISCOVERY_INTERVAL:
            return

        if not self._hot_markets:
            self._last_network_scan = now
            return

        # Pick up to 3 hot markets to scan
        markets_to_scan = list(self._hot_markets.keys())[-3:]

        discovered = 0
        for condition_id in markets_to_scan:
            try:
                resp = self._session.get(
                    f"{DATA_API}/trades",
                    params={
                        "market": condition_id,
                        "limit": 50,
                    },
                    timeout=15,
                )
                resp.raise_for_status()
                trades = resp.json()
            except Exception:
                continue

            if not isinstance(trades, list):
                continue

            # Collect unique wallets from these trades
            candidate_wallets = set()
            for trade in trades:
                wallet = trade.get("maker", "") or trade.get("taker", "")
                if wallet and wallet not in self.tracked_wallets and wallet not in self.network_wallets:
                    candidate_wallets.add(wallet)

            # Check each candidate's profile on leaderboard
            for wallet in list(candidate_wallets)[:10]:  # Cap per market
                profile = self._check_wallet_pnl(wallet)
                if not profile:
                    time.sleep(0.3)
                    continue

                pnl = float(profile.get("pnl", 0))
                vol = float(profile.get("vol", 0))

                # Must be profitable + active volume + good efficiency
                if pnl < NETWORK_DISCOVERY_MIN_PNL:
                    time.sleep(0.3)
                    continue
                if vol < MIN_VOLUME:
                    time.sleep(0.3)
                    continue

                # Recency check
                seed_result = self._seed_history(wallet)
                if seed_result != "active":
                    time.sleep(0.3)
                    continue

                self.network_wallets[wallet] = {
                    "proxy_wallet": wallet,
                    "username": profile.get("userName", ""),
                    "pnl": pnl,
                    "volume": vol,
                    "rank": profile.get("rank", "?"),
                    "source": "network",
                    "discovered_via": condition_id[:12],
                    "last_poll": 0,
                    "trades_copied": 0,
                }
                discovered += 1

                if len(self.network_wallets) >= 200:
                    break

                time.sleep(0.3)

            if len(self.network_wallets) >= 200:
                break

        self._last_network_scan = now
        self._discovery_stats["network"] = len(self.network_wallets)

        if discovered:
            self._save_state()
            print(f"[WHALE] Network discovery: found {discovered} new traders "
                  f"({len(self.network_wallets)} network wallets total)")

    def _check_wallet_pnl(self, wallet):
        """Check a wallet's PnL via the leaderboard search."""
        try:
            resp = self._session.get(
                f"{DATA_API}/v1/leaderboard",
                params={
                    "timePeriod": "MONTH",
                    "proxyWallet": wallet,
                    "limit": 1,
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list) and data:
                return data[0]
        except Exception:
            pass
        return None

    # ── Activity Polling ──────────────────────────────────────

    def poll_whale_activity(self):
        """Check one tracked wallet for new trades. Returns copy signals.

        Polls one wallet per call (round-robin) across both leaderboard
        and network pools. With 300+ wallets at 2s intervals, full
        rotation takes ~10 minutes.
        """
        signals = []
        now = time.time()

        # Merge both pools for polling
        all_wallets = {}
        all_wallets.update(self.network_wallets)
        all_wallets.update(self.tracked_wallets)  # Leaderboard gets priority

        # Find the wallet most overdue for polling
        wallet_to_poll = None
        oldest_poll = now
        for wallet, info in all_wallets.items():
            last = info.get("last_poll", 0)
            if now - last >= ACTIVITY_POLL_INTERVAL and last < oldest_poll:
                oldest_poll = last
                wallet_to_poll = wallet

        if not wallet_to_poll:
            return signals

        # Determine which pool this wallet belongs to
        if wallet_to_poll in self.tracked_wallets:
            info = self.tracked_wallets[wallet_to_poll]
        else:
            info = self.network_wallets[wallet_to_poll]

        try:
            trades = self._fetch_recent_activity(wallet_to_poll)
            info["last_poll"] = now

            for trade in trades:
                tx_hash = trade.get("transactionHash", "")
                if not tx_hash or tx_hash in self._seen_tx_hashes:
                    continue

                self._seen_tx_hashes.add(tx_hash)

                # Track which markets our traders are active in (for network discovery)
                cid = trade.get("conditionId", "")
                if cid:
                    if cid not in self._hot_markets:
                        self._hot_markets[cid] = set()
                    self._hot_markets[cid].add(wallet_to_poll)

                side = trade.get("side", "").upper()
                size = float(trade.get("size", 0))
                price = float(trade.get("price", 0))
                usdc_size = float(trade.get("usdcSize", 0)) or (size * price)

                # SELL signals → copy exit (close our position too)
                if side == "SELL":
                    if usdc_size >= MIN_TRADE_SIZE_USDC:
                        src_tag = "NET" if info.get("source") == "network" else "LB"
                        print(f"[WHALE] EXIT [{src_tag}]: {info.get('username', wallet_to_poll[:8])} "
                              f"SELL {trade.get('outcome', '?')} "
                              f"${usdc_size:.0f} on \"{trade.get('title', '?')[:40]}\"")

                        exit_signal = {
                            "type": "COPY_EXIT",
                            "source_wallet": wallet_to_poll,
                            "source_username": info.get("username", "?"),
                            "source_rank": info.get("rank", "?"),
                            "source_pool": info.get("source", "leaderboard"),
                            "condition_id": cid,
                            "token_id": trade.get("asset", ""),
                            "outcome": trade.get("outcome", ""),
                            "whale_price": price,
                            "whale_size": size,
                            "usdc_value": round(usdc_size, 2),
                            "market_title": trade.get("title", ""),
                            "tx_hash": tx_hash,
                            "timestamp": trade.get("timestamp", now),
                            "detected_at": now,
                        }
                        signals.append(exit_signal)
                    continue

                if side != "BUY":
                    continue

                # Filter by trade size
                if usdc_size < MIN_TRADE_SIZE_USDC:
                    continue

                # Price quality filter: skip entries with terrible risk/reward
                # Buying at $0.92 means risking $0.92 to make $0.08 (11:1 against)
                if price > 0.90:
                    continue

                # Market filter: skip slow/long-term markets (elections, multi-month)
                market_title = trade.get("title", "")
                if self.scorer and self.scorer.classify_market(market_title) == "slow":
                    self._slow_skips += 1
                    continue

                # Consensus boost: count how many tracked wallets trade this market
                market_traders = len(self._hot_markets.get(cid, set()))

                # ── Signal scoring ──
                # Higher score = higher confidence = bigger copy size
                score = 0
                # Base: every signal starts at 1
                score += 1
                # Consensus: +1 per additional wallet trading same market
                score += min(market_traders - 1, 3)  # Cap at +3
                # Price quality: lower entry = better risk/reward
                if price <= 0.30:
                    score += 2    # Great odds (risk $0.30 to win $0.70)
                elif price <= 0.50:
                    score += 1    # Good odds
                # Whale PnL ranking: top-ranked wallets get a boost
                try:
                    rank = int(info.get("rank", 999))
                    if rank <= 50:
                        score += 1
                except (ValueError, TypeError):
                    pass

                signal = {
                    "type": "COPY_TRADE",
                    "source": "api",  # v14: Mark source for dedup
                    "source_wallet": wallet_to_poll,
                    "source_username": info.get("username", "?"),
                    "source_rank": info.get("rank", "?"),
                    "source_pool": info.get("source", "leaderboard"),
                    "source_pnl": info.get("pnl", 0),
                    "condition_id": cid,
                    "token_id": trade.get("asset", ""),
                    "side": side,
                    "outcome": trade.get("outcome", ""),
                    "outcome_index": trade.get("outcomeIndex", 0),
                    "whale_price": price,
                    "whale_size": size,
                    "usdc_value": round(usdc_size, 2),
                    "market_title": trade.get("title", ""),
                    "market_slug": trade.get("slug", ""),
                    "tx_hash": tx_hash,
                    "timestamp": trade.get("timestamp", now),
                    "detected_at": now,
                    "consensus": market_traders,
                    "score": score,
                }

                # v14: Global deduplication check
                if self.is_duplicate_signal(signal):
                    continue  # Skip duplicate (already processed via blockchain or earlier poll)

                signals.append(signal)
                info["trades_copied"] = info.get("trades_copied", 0) + 1

                src_tag = "NET" if info.get("source") == "network" else "LB"
                print(f"[WHALE] SIGNAL [{src_tag}]: {info.get('username', wallet_to_poll[:8])} "
                      f"BUY {trade.get('outcome', '?')} @{price:.2f} "
                      f"${usdc_size:.0f} on \"{trade.get('title', '?')[:40]}\" "
                      f"(score={score}, consensus={market_traders})")

        except Exception:
            self._poll_errors += 1

        if signals:
            self.recent_signals.extend(signals)
            self.recent_signals = self.recent_signals[-500:]
            self._save_state()

        return signals

    def _fetch_recent_activity(self, wallet, limit=None):
        """Fetch recent trade activity for a wallet via Data API."""
        if limit is None:
            limit = SEED_HISTORY_LIMIT
        try:
            resp = self._session.get(
                f"{DATA_API}/activity",
                params={
                    "user": wallet,
                    "type": "TRADE",
                    "limit": limit,
                    "sortBy": "TIMESTAMP",
                    "sortDirection": "DESC",
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else []
        except Exception:
            self._poll_errors += 1
            return []

    # ── Queries (for web UI and status) ───────────────────────

    def get_tracked_wallets(self):
        """Return all tracked wallets (both leaderboard and network)."""
        wallets = []

        # BUG FIX: Create snapshots to avoid "dictionary changed size during iteration"
        tracked_snapshot = dict(self.tracked_wallets)
        network_snapshot = dict(self.network_wallets)

        for wallet, info in tracked_snapshot.items():
            wallets.append({
                "wallet": wallet[:8] + "..." + wallet[-4:],
                "full_wallet": wallet,
                "username": info.get("username", ""),
                "rank": info.get("rank", "?"),
                "pnl": info.get("pnl", 0),
                "volume": info.get("volume", 0),
                "trades_copied": info.get("trades_copied", 0),
                "source": "leaderboard",
            })

        for wallet, info in network_snapshot.items():
            wallets.append({
                "wallet": wallet[:8] + "..." + wallet[-4:],
                "full_wallet": wallet,
                "username": info.get("username", ""),
                "rank": info.get("rank", "?"),
                "pnl": info.get("pnl", 0),
                "volume": info.get("volume", 0),
                "trades_copied": info.get("trades_copied", 0),
                "source": "network",
            })

        wallets.sort(key=lambda w: float(w.get("pnl", 0)), reverse=True)
        return wallets

    def get_recent_signals(self, limit=20):
        return list(reversed(self.recent_signals[-limit:]))

    def add_blockchain_signal(self, whale_address, signal_data):
        """Add a whale trade signal detected via blockchain monitoring.

        Called by blockchain_monitor when a tracked whale trades on-chain.
        Signal is added to blockchain queue for immediate copy trading execution.

        BUG FIX #1: Now uses thread-safe queue + deduplication to prevent
        duplicate trades when same signal arrives via blockchain AND polling.

        Args:
            whale_address: Checksummed Ethereum address of whale
            signal_data: Dict with trade details from blockchain event
                {condition_id, market_title, outcome, whale_price, timestamp, size, ...}
        """
        # Normalize address to match our tracked wallets format (lowercase)
        wallet = whale_address.lower()

        # Only process if we're tracking this wallet
        if wallet not in self.tracked_wallets and wallet not in self.network_wallets:
            return

        # BUG FIX #1: Check deduplication FIRST (prevent duplicate trades)
        tx_hash = signal_data.get("tx_hash", "")
        if tx_hash:
            with self._blockchain_lock:
                if tx_hash in self._seen_tx_hashes:
                    return  # Already processed via polling or earlier blockchain event
                self._seen_tx_hashes.add(tx_hash)

        # Build signal dict for copy trading execution
        signal = {
            "source": "blockchain",  # Mark as blockchain-sourced (for dedup)
            "source_wallet": wallet,
            "source_username": self.tracked_wallets.get(wallet, {}).get("username", "Blockchain Whale"),
            "condition_id": signal_data.get("condition_id", ""),
            "market_title": signal_data.get("market_title", "Unknown"),
            "outcome": signal_data.get("outcome", "YES"),
            "whale_price": signal_data.get("whale_price", 0),
            "timestamp": signal_data.get("timestamp", time.time()),  # Whale's trade time
            "detected_at": time.time(),  # Our detection time
            "size": signal_data.get("size", 0),
            "tx_hash": tx_hash,
            "log_index": signal_data.get("log_index", 0),  # For dedup
            "gas_price_gwei": signal_data.get("gas_price_gwei", 0),  # Gas Signals
        }

        # v14: Global deduplication check (prevents double-trading)
        if self.is_duplicate_signal(signal):
            return  # Already processed or queued

        # BUG FIX #1: Add to blockchain queue for execution (thread-safe)
        self._blockchain_queue.put(signal)

        # Also add to recent_signals for dashboard display
        self.recent_signals.append(signal)
        if len(self.recent_signals) > 100:
            self.recent_signals = self.recent_signals[-100:]

        print(f"[BLOCKCHAIN] Signal queued for execution: {signal['source_username']} → "
              f"{signal['market_title'][:50]} ({signal['outcome']}) @ ${signal['whale_price']:.3f}")

    def drain_blockchain_signals(self, max_count=50):
        """Drain blockchain signals for execution (thread-safe).

        BUG FIX #1: New method to consume queued blockchain signals.
        Called by bot's main loop to process real-time whale trades.

        Args:
            max_count: Maximum number of signals to drain per call

        Returns:
            List of signal dicts ready for copy trading execution
        """
        signals = []
        try:
            while not self._blockchain_queue.empty() and len(signals) < max_count:
                signals.append(self._blockchain_queue.get_nowait())
        except queue.Empty:
            pass
        return signals

    def add_clob_signal(self, signal):
        """
        Add CLOB WebSocket signal (thread-safe).
        
        POLY-101: CLOB signals have ~300ms latency (vs 2-3s blockchain).
        
        Args:
            signal: Dict with signal details from CLOBWebSocketMonitor
        """
        # Generate unique signal ID for deduplication
        signal_id = f"clob_{signal.get('condition_id', '')}_{signal.get('source_wallet', '')}_{signal.get('timestamp', 0)}"
        
        with self._signal_dedup_lock:
            # Check for duplicates
            if signal_id in self._signal_dedup_cache:
                return  # Already processed
            
            # Add to dedup cache
            self._signal_dedup_cache[signal_id] = time.time()
            
            # Clean old entries (keep last 5 minutes)
            cutoff = time.time() - 300
            self._signal_dedup_cache = {
                k: v for k, v in self._signal_dedup_cache.items()
                if v > cutoff
            }
        
        # Add to CLOB queue for execution
        self._clob_queue.put(signal)
        
        # Also add to recent_signals for dashboard display
        self.recent_signals.append(signal)
        if len(self.recent_signals) > 100:
            self.recent_signals = self.recent_signals[-100:]
        
        latency = signal.get('latency_ms', 0)
        print(f"[CLOB] Signal queued: {signal.get('source_wallet', 'unknown')[:10]}... → "
              f"{signal.get('market_title', 'Unknown')[:30]} ({signal.get('outcome')}) @ "
              f"${signal.get('whale_price', 0):.3f} (latency: {latency:.0f}ms)")

    def drain_clob_signals(self, max_count=50):
        """Drain CLOB signals for execution (thread-safe).
        
        POLY-101: New method to consume queued CLOB signals.
        Called by bot's main loop to process real-time whale trades.
        
        Args:
            max_count: Maximum number of signals to drain per call
            
        Returns:
            List of signal dicts ready for copy trading execution
        """
        signals = []
        try:
            while not self._clob_queue.empty() and len(signals) < max_count:
                signals.append(self._clob_queue.get_nowait())
        except queue.Empty:
            pass
        return signals

    def add_discovered_wallet(self, discovery_signal):
        """Add a newly discovered wallet from network discovery.

        Network Discovery: Automatically finds profitable wallets by monitoring
        high-value trades ($500+) from unknown addresses on-chain.

        Args:
            discovery_signal: Dict with discovery details
                {address, trade_value, token_id, amount, tx_hash, block_number, timestamp}
        """
        address = discovery_signal["address"].lower()

        # Skip if already tracking
        if address in self.tracked_wallets or address in self.network_wallets:
            return

        # Add to network_wallets for monitoring (not copied yet, needs performance validation)
        self.network_wallets[address] = {
            "discovered_at": time.time(),
            "discovery_trade_value": discovery_signal["trade_value"],
            "discovery_tx": discovery_signal["tx_hash"],
            "total_volume": discovery_signal["trade_value"],
            "trades_seen": 1,
        }

        self._save_state()

        print(f"[WHALE] 🔍 NETWORK DISCOVERY: Added {address[:10]}... "
              f"(${discovery_signal['trade_value']:.0f} trade)")

    # ── Global Signal Deduplication (v14) ────────────────────

    def _get_signal_id(self, signal):
        """Generate canonical signal ID for deduplication.

        Uses different strategies depending on signal source:
        - Blockchain: (chain_id, tx_hash, log_index) - most reliable
        - API: hash(wallet, condition_id, outcome, price, timestamp)

        Returns:
            str: Unique signal identifier
        """
        source = signal.get("source", "api")

        if source == "blockchain":
            # On-chain signals: use tx_hash as primary key
            tx_hash = signal.get("tx_hash", "")
            log_index = signal.get("log_index", 0)
            return f"chain:{tx_hash}:{log_index}"
        else:
            # API signals: hash key fields
            # Normalize timestamp to 5-second buckets to catch near-duplicates
            ts_bucket = int(signal.get("timestamp", 0) / 5) * 5

            key_str = "{}:{}:{}:{:.3f}:{}".format(
                signal.get("source_wallet", "")[:20],
                signal.get("condition_id", "")[:20],
                signal.get("outcome", ""),
                signal.get("whale_price", 0),
                ts_bucket
            )

            return "api:" + hashlib.md5(key_str.encode()).hexdigest()

    def is_duplicate_signal(self, signal):
        """Check if signal is a duplicate across all sources.

        Args:
            signal: Signal dict

        Returns:
            bool: True if duplicate, False if new signal
        """
        sig_id = self._get_signal_id(signal)
        now = time.time()

        with self._signal_dedup_lock:
            # Clean expired entries (older than TTL)
            expired = [k for k, v in self._signal_dedup_cache.items()
                      if now - v > SIGNAL_DEDUP_TTL]
            for k in expired:
                del self._signal_dedup_cache[k]

            # Check if we've seen this signal
            if sig_id in self._signal_dedup_cache:
                return True

            # Record this signal
            self._signal_dedup_cache[sig_id] = now
            return False

    def get_stats(self):
        total = len(self.tracked_wallets) + len(self.network_wallets)
        with self._signal_dedup_lock:
            dedup_cache_size = len(self._signal_dedup_cache)

        return {
            "tracked_wallets": total,
            "leaderboard_wallets": len(self.tracked_wallets),
            "network_wallets": len(self.network_wallets),
            "total_signals": len(self.recent_signals),
            "seen_transactions": len(self._seen_tx_hashes),
            "hot_markets": len(self._hot_markets),
            "poll_errors": self._poll_errors,
            "slow_markets_skipped": self._slow_skips,
            "discovery": self._discovery_stats,
            "dedup_cache_size": dedup_cache_size,
        }
