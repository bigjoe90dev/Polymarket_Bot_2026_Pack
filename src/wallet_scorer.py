"""Deep wallet performance tracking, flow analysis, and pattern recognition.

Tracks:
- Per-wallet copy results: wins, losses, ROI, best market types
- Market classification: crypto_fast, sports, politics (auto-skip slow ones)
- Flow clusters: when multiple top wallets agree on same market
- Smart sizing: proven wallets get bigger copies, losers get cut

The goal: follow the money. Find which wallets ACTUALLY make us profit,
rank them, connect the dots, and route capital to the best flows.
"""

import json
import os
import re
import time

SCORER_STATE_FILE = "data/wallet_scores.json"

# Market classification patterns
FAST_CRYPTO_PATTERNS = [
    r"Up or Down",          # BTC/ETH up or down (15-30 min windows)
    r"\d{1,2}:\d{2}[AP]M",  # Contains specific time like "8:30AM"
]
FAST_SPORTS_PATTERNS = [
    r"\bvs\.?\b",           # "Team A vs Team B"
    r"\bBO[1-3]\b",         # Best of 1/2/3 (esports)
    r"\bSet \d\b",          # Tennis sets
    r"\bO/U\b",             # Over/under
    r"\bMatch\b.*\bWinner\b",  # Match winner
    r"win on \d{4}-\d{2}-\d{2}",  # "win on 2026-02-06" (same-day)
]
SLOW_PATTERNS = [
    r"World Cup",
    r"win the 2",           # "win the 2025-26 NBA Championship"
    r"by (March|April|May|June|July|August|September|October|November|December) \d",
    r"by \w+ \d{1,2}, 202[6-9]",  # "by June 30, 2026"
    r"Prime Minister",
    r"largest company",
    r"FDV above",
    r"win the most medals",
    r"next President",
    r"next Prime",
    r"price of Bitcoin be above",  # Long-term price targets
    r"price of Ethereum be above",
    r"tweets from",              # Elon tweet counts (weekly)
    r"most medals",
]


class WalletScorer:
    """Tracks per-wallet copy trading performance and identifies money flow."""

    def __init__(self, config):
        self.config = config
        self.wallet_stats = {}      # wallet -> performance dict
        self.market_types = {}      # condition_id -> classification
        self.flow_events = []       # Recent flow events for cluster detection
        self.cluster_scores = {}    # condition_id -> flow strength
        self._load_state()

    # ── State Persistence ─────────────────────────────────────

    def _load_state(self):
        if os.path.exists(SCORER_STATE_FILE):
            try:
                with open(SCORER_STATE_FILE, "r") as f:
                    state = json.load(f)
                self.wallet_stats = state.get("wallet_stats", {})
                self.market_types = state.get("market_types", {})
                self.flow_events = state.get("flow_events", [])
                total = len(self.wallet_stats)
                scored = sum(1 for w in self.wallet_stats.values() if w.get("total_copies", 0) >= 3)
                print(f"[SCORER] Loaded {total} wallets ({scored} with 3+ copies scored)")
                return
            except Exception:
                pass
        print("[SCORER] Starting fresh — will build wallet scores from copy results")

    def _save_state(self):
        os.makedirs(os.path.dirname(SCORER_STATE_FILE), exist_ok=True)
        try:
            # Trim flow events to last 2000
            if len(self.flow_events) > 2000:
                self.flow_events = self.flow_events[-2000:]
            # Trim market types cache to last 500
            if len(self.market_types) > 500:
                keys = list(self.market_types.keys())
                self.market_types = {k: self.market_types[k] for k in keys[-500:]}

            state = {
                "wallet_stats": self.wallet_stats,
                "market_types": self.market_types,
                "flow_events": self.flow_events,
                "last_updated": time.time(),
            }
            tmp = SCORER_STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f)
            os.replace(tmp, SCORER_STATE_FILE)
        except Exception as e:
            print(f"[!] Scorer state save error: {e}")

    # ── Market Classification ─────────────────────────────────

    def classify_market(self, title, condition_id=None):
        """Classify a market by settlement speed and type.

        Returns: 'crypto_fast', 'sports_fast', 'slow', or 'unknown'
        """
        if condition_id and condition_id in self.market_types:
            return self.market_types[condition_id]

        title_upper = title.upper() if title else ""
        classification = "unknown"

        # Check slow patterns first (these override fast)
        for pattern in SLOW_PATTERNS:
            if re.search(pattern, title, re.IGNORECASE):
                classification = "slow"
                break

        if classification != "slow":
            # Check fast crypto
            for pattern in FAST_CRYPTO_PATTERNS:
                if re.search(pattern, title, re.IGNORECASE):
                    classification = "crypto_fast"
                    break

            # Check fast sports
            if classification == "unknown":
                for pattern in FAST_SPORTS_PATTERNS:
                    if re.search(pattern, title, re.IGNORECASE):
                        classification = "sports_fast"
                        break

        if condition_id:
            self.market_types[condition_id] = classification

        return classification

    def is_fast_market(self, title, condition_id=None):
        """Returns True if market likely settles within 24h."""
        mtype = self.classify_market(title, condition_id)
        return mtype in ("crypto_fast", "sports_fast")

    def is_crypto_market(self, title):
        """Returns True for any BTC/ETH/SOL/XRP crypto market."""
        if not title:
            return False
        t = title.lower()
        return any(kw in t for kw in (
            "bitcoin", "btc",
            "ethereum", "eth",
            "solana", "sol",
            "xrp", "ripple",
        ))

    # ── Entry/Exit Recording ──────────────────────────────────

    def record_entry(self, wallet, condition_id, cost, market_title, score):
        """Called when we open a copy position."""
        stats = self._get_or_create(wallet)
        stats["total_copies"] += 1
        stats["total_invested"] += cost
        stats["last_copy_at"] = time.time()

        # Track market type affinity
        mtype = self.classify_market(market_title, condition_id)
        if mtype not in stats["market_types"]:
            stats["market_types"][mtype] = {"copies": 0, "wins": 0, "losses": 0, "pnl": 0.0}
        stats["market_types"][mtype]["copies"] += 1

        # Record flow event (for cluster detection)
        self.flow_events.append({
            "timestamp": time.time(),
            "wallet": wallet,
            "condition_id": condition_id,
            "market_title": market_title[:50],
            "market_type": mtype,
            "direction": "BUY",
            "cost": round(cost, 4),
        })

        # Update cluster scores (how many wallets are buying this market)
        self._update_clusters(condition_id)

    def record_result(self, wallet, condition_id, realized_pnl, hold_seconds, market_title=""):
        """Called when a copy position settles or is exit-copied."""
        stats = self._get_or_create(wallet)

        if realized_pnl >= 0:
            stats["wins"] += 1
            stats["total_profit"] += realized_pnl
            stats["streak"] = max(0, stats.get("streak", 0)) + 1
            stats["bayes_alpha"] = stats.get("bayes_alpha", 2.0) + 1
        else:
            stats["losses"] += 1
            stats["total_loss"] += abs(realized_pnl)
            stats["streak"] = min(0, stats.get("streak", 0)) - 1
            stats["bayes_beta"] = stats.get("bayes_beta", 2.0) + 1

        stats["net_pnl"] += realized_pnl

        # Track best and worst result
        if realized_pnl > stats.get("best_trade_pnl", 0):
            stats["best_trade_pnl"] = round(realized_pnl, 4)
        if realized_pnl < stats.get("worst_trade_pnl", 0):
            stats["worst_trade_pnl"] = round(realized_pnl, 4)

        # Track hold time
        total_holds = stats.get("total_hold_seconds", 0) + hold_seconds
        hold_count = stats["wins"] + stats["losses"]
        stats["total_hold_seconds"] = total_holds
        stats["avg_hold_minutes"] = round(total_holds / max(hold_count, 1) / 60, 1)

        # Update market type stats
        mtype = self.classify_market(market_title, condition_id)
        if mtype in stats["market_types"]:
            type_stats = stats["market_types"][mtype]
            if realized_pnl >= 0:
                type_stats["wins"] += 1
            else:
                type_stats["losses"] += 1
            type_stats["pnl"] += realized_pnl

        # Recalculate composite score
        stats["score"] = self._calculate_score(stats)

        self._save_state()

    def _get_or_create(self, wallet):
        if wallet not in self.wallet_stats:
            self.wallet_stats[wallet] = {
                "wallet": wallet,
                "total_copies": 0,
                "total_invested": 0.0,
                "wins": 0,
                "losses": 0,
                "total_profit": 0.0,
                "total_loss": 0.0,
                "net_pnl": 0.0,
                "best_trade_pnl": 0.0,
                "worst_trade_pnl": 0.0,
                "streak": 0,
                "total_hold_seconds": 0,
                "avg_hold_minutes": 0.0,
                "market_types": {},
                "score": 1.0,
                "bayes_alpha": 2.0,  # Beta prior: mild 50% bias
                "bayes_beta": 2.0,
                "first_copy_at": time.time(),
                "last_copy_at": 0,
            }
        return self.wallet_stats[wallet]

    # ── Scoring Engine ────────────────────────────────────────

    def _calculate_score(self, stats):
        """Calculate wallet score from 0.0 (cut off) to 3.0 (max allocation).

        Uses Bayesian Beta-Binomial model for win rate estimation:
        - Prior: Beta(2, 2) — mild 50% assumption
        - Updates with each win (alpha++) or loss (beta++)
        - Posterior mean = alpha / (alpha + beta)

        Combined with ROI and volume confidence for final score.
        """
        copies = stats["total_copies"]
        if copies < 3:
            return 1.0  # Not enough data, use default

        wins = stats["wins"]
        losses = stats["losses"]
        settled = wins + losses
        if settled == 0:
            return 1.0  # No results yet

        # Bayesian win rate (regularized by prior)
        alpha = stats.get("bayes_alpha", 2.0 + wins)
        beta = stats.get("bayes_beta", 2.0 + losses)
        posterior_wr = alpha / (alpha + beta)

        # ROI metric
        invested = max(stats["total_invested"], 0.01)
        roi = stats["net_pnl"] / invested

        # Volume confidence: 0→1 over 20 trades
        confidence = min(settled / 20.0, 1.0)

        # Component scores (each in a useful range)
        wr_score = posterior_wr * 2.0            # 0-2 range
        roi_score = max(-1, min(1, roi * 5))     # -1 to +1 range
        vol_score = confidence * 0.5             # 0-0.5 range

        # Weighted combination: 50% win rate, 35% ROI, 15% volume
        raw = 1.0 + (wr_score - 1.0) * 0.50 + roi_score * 0.35 + vol_score * 0.15

        # Clamp
        return round(max(0.0, min(3.0, raw)), 2)

    def get_category_score(self, wallet, market_type):
        """Get wallet's score for a specific market category.

        If the wallet has 3+ results in this category, returns a
        category-specific score. Otherwise falls back to overall score.
        Prevents a Sports whale from getting high score on Crypto trades.
        """
        stats = self.wallet_stats.get(wallet)
        if not stats:
            return 1.0

        cat_stats = stats.get("market_types", {}).get(market_type, {})
        cat_wins = cat_stats.get("wins", 0)
        cat_losses = cat_stats.get("losses", 0)
        cat_settled = cat_wins + cat_losses

        if cat_settled < 3:
            # Not enough category data — use overall score
            return stats.get("score", 1.0)

        # Category-specific Bayesian win rate
        cat_alpha = 2.0 + cat_wins
        cat_beta = 2.0 + cat_losses
        cat_wr = cat_alpha / (cat_alpha + cat_beta)

        # Category ROI
        cat_invested = cat_stats.get("copies", 0) * max(stats["total_invested"] / max(stats["total_copies"], 1), 0.01)
        cat_roi = cat_stats.get("pnl", 0) / max(cat_invested, 0.01)

        # Same scoring formula as _calculate_score but with category data
        confidence = min(cat_settled / 10.0, 1.0)  # Lower threshold for categories
        wr_score = cat_wr * 2.0
        roi_score = max(-1, min(1, cat_roi * 5))
        vol_score = confidence * 0.5
        raw = 1.0 + (wr_score - 1.0) * 0.50 + roi_score * 0.35 + vol_score * 0.15

        return round(max(0.0, min(3.0, raw)), 2)

    def get_wallet_multiplier(self, wallet, market_type=None):
        """Returns sizing multiplier for a wallet.

        0.0 = stop copying (proven loser)
        0.5 = reduce size (underperforming)
        1.0 = normal (default / unproven)
        1.5 = increase (good performer)
        2.0 = max allocation (proven winner)
        3.0 = ultra allocation (exceptional, 70%+ win rate with volume)

        If market_type provided, uses category-specific score when available.
        """
        stats = self.wallet_stats.get(wallet)
        if not stats:
            return 1.0

        # Use category-specific score if market type is known
        if market_type:
            score = self.get_category_score(wallet, market_type)
        else:
            score = stats.get("score", 1.0)

        # Hard cutoff: if wallet has 5+ results and score below 0.3, stop copying
        settled = stats["wins"] + stats["losses"]
        if settled >= 5 and score < 0.3:
            return 0.0

        return score

    # ── Anti-Hedge Detection ──────────────────────────────────

    def check_anti_hedge(self, condition_id, outcome, open_positions):
        """Check if we already have the opposite side of this market.

        Returns True if safe to proceed, False if it would create a hedge.
        """
        outcome_upper = outcome.upper()

        for pos_key, pos in open_positions.items():
            if pos.get("status") != "OPEN":
                continue
            if pos.get("trade_type") != "COPY":
                continue
            if pos.get("condition_id") != condition_id:
                continue

            existing_outcome = pos.get("outcome", "").upper()

            # Same market, different outcome = hedge
            if existing_outcome != outcome_upper:
                return False

        return True

    # ── Flow & Cluster Analysis ───────────────────────────────

    def _update_clusters(self, condition_id):
        """Update flow cluster scores based on recent activity."""
        now = time.time()
        window = 600  # 10-minute window for cluster detection

        # Count unique wallets buying this market in the last 10 minutes
        recent_wallets = set()
        for event in reversed(self.flow_events):
            if now - event["timestamp"] > window:
                break
            if event["condition_id"] == condition_id and event["direction"] == "BUY":
                recent_wallets.add(event["wallet"])

        self.cluster_scores[condition_id] = {
            "wallets": len(recent_wallets),
            "last_updated": now,
        }

    def get_flow_strength(self, condition_id):
        """How many wallets are flowing into this market right now?

        Returns: number of unique wallets that bought in last 10 min.
        Higher = stronger consensus = bigger copy.
        """
        cluster = self.cluster_scores.get(condition_id, {})
        # Stale check (older than 15 min = 0)
        if time.time() - cluster.get("last_updated", 0) > 900:
            return 0
        return cluster.get("wallets", 0)

    def get_hot_flows(self, min_wallets=2, top_n=10):
        """Find markets with the most smart money flowing in right now.

        These are the markets where multiple tracked wallets are buying
        within a short time window — strongest copy signals.
        """
        now = time.time()
        flows = []

        for cid, cluster in self.cluster_scores.items():
            if now - cluster.get("last_updated", 0) > 900:
                continue
            wallet_count = cluster.get("wallets", 0)
            if wallet_count >= min_wallets:
                # Find the market title from flow events
                title = ""
                for event in reversed(self.flow_events):
                    if event["condition_id"] == cid:
                        title = event.get("market_title", "")
                        break

                # Calculate average wallet score for this flow
                flow_wallets = set()
                for event in reversed(self.flow_events):
                    if now - event["timestamp"] > 600:
                        break
                    if event["condition_id"] == cid:
                        flow_wallets.add(event["wallet"])

                avg_score = 0.0
                if flow_wallets:
                    scores = [self.wallet_stats.get(w, {}).get("score", 1.0)
                              for w in flow_wallets]
                    avg_score = sum(scores) / len(scores)

                flows.append({
                    "condition_id": cid,
                    "market_title": title,
                    "wallets_in": wallet_count,
                    "avg_wallet_score": round(avg_score, 2),
                    "flow_strength": round(wallet_count * avg_score, 2),
                })

        flows.sort(key=lambda f: f["flow_strength"], reverse=True)
        return flows[:top_n]

    # ── Rankings & Reports ────────────────────────────────────

    def get_rankings(self, top_n=50):
        """Return wallets ranked by our ROI from copying them.

        This is THE key metric: which wallets make US money?
        """
        ranked = []
        for wallet, stats in self.wallet_stats.items():
            settled = stats["wins"] + stats["losses"]
            invested = max(stats["total_invested"], 0.01)

            ranked.append({
                "wallet": wallet[:10] + "...",
                "full_wallet": wallet,
                "total_copies": stats["total_copies"],
                "settled": settled,
                "wins": stats["wins"],
                "losses": stats["losses"],
                "win_rate": round(stats["wins"] / max(settled, 1) * 100, 1),
                "net_pnl": round(stats["net_pnl"], 4),
                "roi": round(stats["net_pnl"] / invested * 100, 1),
                "total_invested": round(stats["total_invested"], 2),
                "score": stats.get("score", 1.0),
                "streak": stats.get("streak", 0),
                "avg_hold_min": stats.get("avg_hold_minutes", 0),
                "best_market_type": self._best_market_type(stats),
            })

        # Sort by net PnL (money talks)
        ranked.sort(key=lambda w: w["net_pnl"], reverse=True)
        return ranked[:top_n]

    def _best_market_type(self, stats):
        """Find which market type this wallet performs best in."""
        best_type = "unknown"
        best_pnl = -999
        for mtype, type_stats in stats.get("market_types", {}).items():
            if type_stats.get("pnl", 0) > best_pnl and type_stats.get("copies", 0) >= 2:
                best_pnl = type_stats["pnl"]
                best_type = mtype
        return best_type

    def get_market_type_stats(self):
        """Aggregate stats by market type across all wallets."""
        type_totals = {}

        for wallet, stats in self.wallet_stats.items():
            for mtype, type_stats in stats.get("market_types", {}).items():
                if mtype not in type_totals:
                    type_totals[mtype] = {
                        "copies": 0, "wins": 0, "losses": 0,
                        "pnl": 0.0, "wallets": 0,
                    }
                t = type_totals[mtype]
                t["copies"] += type_stats.get("copies", 0)
                t["wins"] += type_stats.get("wins", 0)
                t["losses"] += type_stats.get("losses", 0)
                t["pnl"] += type_stats.get("pnl", 0)
                if type_stats.get("copies", 0) > 0:
                    t["wallets"] += 1

        for mtype, t in type_totals.items():
            settled = t["wins"] + t["losses"]
            t["win_rate"] = round(t["wins"] / max(settled, 1) * 100, 1)
            t["pnl"] = round(t["pnl"], 4)

        return type_totals

    def get_summary(self):
        """High-level scorer summary for status display."""
        total = len(self.wallet_stats)
        scored = sum(1 for w in self.wallet_stats.values()
                     if (w["wins"] + w["losses"]) >= 3)
        cutoff = sum(1 for w in self.wallet_stats.values()
                     if w.get("score", 1.0) < 0.3 and (w["wins"] + w["losses"]) >= 5)
        hot = sum(1 for w in self.wallet_stats.values()
                  if w.get("score", 1.0) >= 2.0)
        total_pnl = sum(w["net_pnl"] for w in self.wallet_stats.values())

        return {
            "total_tracked": total,
            "scored_wallets": scored,
            "hot_wallets": hot,
            "cutoff_wallets": cutoff,
            "total_copy_pnl": round(total_pnl, 4),
            "active_flows": len([c for c in self.cluster_scores.values()
                                 if time.time() - c.get("last_updated", 0) < 900]),
            "market_type_stats": self.get_market_type_stats(),
        }
