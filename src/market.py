from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BookParams

CLOB_HOST = "https://clob.polymarket.com"


class MarketDataService:
    def __init__(self, config):
        self.config = config
        # L0 client: no auth needed for reading markets and order books
        self.client = ClobClient(CLOB_HOST)

    def get_active_markets(self):
        """Spec 6.2: Pulls active markets (YES/NO only).
        Uses get_sampling_simplified_markets for pre-filtered active markets."""
        all_markets = []

        try:
            resp = self.client.get_sampling_simplified_markets()
            data = resp if isinstance(resp, list) else resp.get("data", [])
        except Exception as e:
            print(f"[!] Error fetching sampling markets: {e}")
            # Fallback to paginated scan
            return self._get_active_markets_fallback()

        for m in data:
            if not m.get("active", False):
                continue
            if not m.get("accepting_orders", False):
                continue
            if m.get("closed", False) or m.get("archived", False):
                continue

            tokens = m.get("tokens", [])
            if len(tokens) != 2:
                continue

            yes_token = None
            no_token = None
            for t in tokens:
                outcome = t.get("outcome", "").lower()
                if outcome == "yes":
                    yes_token = t
                elif outcome == "no":
                    no_token = t

            if yes_token and no_token:
                all_markets.append({
                    "condition_id": m["condition_id"],
                    "yes_token_id": yes_token["token_id"],
                    "no_token_id": no_token["token_id"],
                    "yes_price": yes_token.get("price", 0),
                    "no_price": no_token.get("price", 0),
                    "title": m.get("question", ""),  # For fee classification
                    "end_date": m.get("endDate") or m.get("end_date"),
                })

        return all_markets

    def _get_active_markets_fallback(self):
        """Fallback: paginate get_simplified_markets if sampling endpoint fails."""
        all_markets = []
        next_cursor = "MA=="
        max_pages = 15

        for _ in range(max_pages):
            try:
                resp = self.client.get_simplified_markets(next_cursor=next_cursor)
            except Exception as e:
                print(f"[!] Error fetching markets: {e}")
                break

            for m in resp.get("data", []):
                if not m.get("active", False):
                    continue
                if not m.get("accepting_orders", False):
                    continue
                if m.get("closed", False) or m.get("archived", False):
                    continue

                tokens = m.get("tokens", [])
                if len(tokens) != 2:
                    continue

                yes_token = None
                no_token = None
                for t in tokens:
                    outcome = t.get("outcome", "").lower()
                    if outcome == "yes":
                        yes_token = t
                    elif outcome == "no":
                        no_token = t

                if yes_token and no_token:
                    all_markets.append({
                        "condition_id": m["condition_id"],
                        "yes_token_id": yes_token["token_id"],
                        "no_token_id": no_token["token_id"],
                        "yes_price": yes_token.get("price", 0),
                        "no_price": no_token.get("price", 0),
                        "title": m.get("question", ""),  # For fee classification
                    })

            next_cursor = resp.get("next_cursor", "LTE=")
            if next_cursor == "LTE=":
                break

        return all_markets

    def get_order_book(self, market, client=None):
        """Fetch live order book depth for both YES and NO sides.

        Pass client= for thread-safe parallel use (each thread
        should create its own ClobClient to avoid shared session issues).
        """
        if not market:
            return None

        yes_id = market.get("yes_token_id")
        no_id = market.get("no_token_id")

        if not yes_id or not no_id:
            return None

        c = client or self.client

        try:
            yes_book = c.get_order_book(yes_id)
            no_book = c.get_order_book(no_id)
        except Exception:
            return None

        # Transform to format expected by strategy.py: [[price, size], ...]
        asks_yes = [[ask.price, ask.size] for ask in yes_book.asks] if yes_book.asks else []
        asks_no = [[ask.price, ask.size] for ask in no_book.asks] if no_book.asks else []
        bids_yes = [[bid.price, bid.size] for bid in yes_book.bids] if yes_book.bids else []
        bids_no = [[bid.price, bid.size] for bid in no_book.bids] if no_book.bids else []

        if not asks_yes or not asks_no:
            return None

        return {
            "condition_id": market["condition_id"],
            "yes_token_id": yes_id,
            "no_token_id": no_id,
            "asks_yes": asks_yes,
            "asks_no": asks_no,
            "bids_yes": bids_yes,
            "bids_no": bids_no,
        }

    # ── Book Health Check (Defensive Execution) ──────────────
    #
    # Polymarket 15-min binary markets move fast: the winning side hits
    # $0.99 within minutes. The whale enters at $0.50-$0.55 and by the
    # time we poll, the book shows $0.99 asks even on LIVE markets.
    #
    # Old approach (spread check) blocked EVERYTHING — zero trades in 11h.
    #
    # New approach for paper mode: only block RESOLVED markets (API 404).
    # For live markets, the 12-layer stress simulator handles slippage,
    # rejection, and fill probability realistically.

    def check_book_health(self, token_id):
        """Check if a market's order book still exists (not resolved).

        A 404 means the market resolved and no book exists — block trade.
        Any valid book response means the market is live — allow trade.
        Non-404 errors (network/rate limit): fail-open for paper mode.
        """
        try:
            self.client.get_order_book(token_id)
        except Exception as e:
            err_str = str(e)
            if "404" in err_str or "No orderbook" in err_str:
                return {"healthy": False,
                        "reason": "Market resolved (no orderbook)"}
            return {"healthy": True, "reason": "API error, proceeding"}

        return {"healthy": True, "reason": "Book exists"}

    # ── Fee Rate Lookup (CRITICAL for profitability) ─────────
    #
    # v14 ENHANCEMENT: Fee tier accuracy is critical for:
    # 1. Realistic paper trading PnL
    # 2. Correct profitability analysis before LIVE
    # 3. Position sizing decisions
    #
    # Polymarket fee structure (as of 2026):
    # - Crypto fast markets (BTC/ETH up or down): 1000 bps (10%)
    # - Sports/politics markets: 0 bps (0%)
    # - Unknown/other: 200 bps (2% conservative fallback)
    #
    # Curved fee formula: fee = (bps/10000) * p * (1-p)
    # where p is the share price

    def get_fee_rate_bps(self, token_id, market_title="", condition_id=""):
        """Get fee rate in basis points for a token.

        Tries multiple approaches in order:
        1. py-clob-client API (if available)
        2. Market title classification
        3. Conservative fallback (200 bps)

        Args:
            token_id: ERC1155 token ID
            market_title: Market question/title (for classification)
            condition_id: Condition ID (for caching)

        Returns:
            int: Fee rate in basis points (bps)
        """
        # Approach 1: Try to get from CLOB API
        # NOTE: py-clob-client may not expose this directly
        # Check the library docs for the correct method
        try:
            # Placeholder: this may not exist in current py-clob-client
            # Check if get_market or get_simplified_market returns fee_bps
            # If it does, uncomment and adjust this:
            #
            # market_info = self.client.get_market(condition_id)
            # if market_info and "fee_bps" in market_info:
            #     return int(market_info["fee_bps"])
            pass
        except Exception:
            pass

        # Approach 2: Classify by market title
        if market_title:
            fee_bps = self._classify_fee_tier(market_title)
            if fee_bps is not None:
                return fee_bps

        # Approach 3: Conservative fallback
        return 200  # 2% when unknown (pessimistic but safe)

    def _classify_fee_tier(self, market_title):
        """Classify fee tier based on market title patterns.

        Returns:
            int or None: Fee rate in bps, or None if cannot classify
        """
        if not market_title:
            return None

        title = market_title.upper()

        # Crypto fast markets (high fee tier)
        crypto_patterns = [
            "UP OR DOWN",
            "BITCOIN",
            "BTC",
            "ETHEREUM",
            "ETH",
            "SOLANA",
            "SOL",
            ":00AM",
            ":30AM",
            ":00PM",
            ":30PM",
        ]

        for pattern in crypto_patterns:
            if pattern in title:
                return 1000  # 10% fee for crypto fast markets

        # Sports markets (zero fee tier)
        sports_patterns = [
            " VS ",
            " VS. ",
            "MATCH WINNER",
            "WIN ON 202",  # "win on 2026-02-10"
            "O/U",
            "OVER/UNDER",
            "BO1",
            "BO2",
            "BO3",
            "SET 1",
            "SET 2",
        ]

        for pattern in sports_patterns:
            if pattern in title:
                return 0  # 0% fee for sports markets

        # Politics/long-term markets (zero fee tier)
        politics_patterns = [
            "PRESIDENT",
            "PRIME MINISTER",
            "ELECTION",
            "WIN THE 202",  # "win the 2026 World Cup"
            "BY MARCH",
            "BY APRIL",
            "BY MAY",
            "BY JUNE",
        ]

        for pattern in politics_patterns:
            if pattern in title:
                return 0  # 0% fee for politics markets

        # Cannot classify
        return None
