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
