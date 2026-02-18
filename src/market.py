import json
import os
from datetime import datetime, timezone, timedelta
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BookParams
import requests

CLOB_HOST = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

# Month names for slug generation
MONTHS = ['january', 'february', 'march', 'april', 'may', 'june',
          'july', 'august', 'september', 'october', 'november', 'december']


def fmt(value, ndp=2):
    """Safe formatting - returns 'N/A' if value is None."""
    if value is None:
        return "N/A"
    try:
        return f"{value:.{ndp}f}"
    except (TypeError, ValueError):
        return "N/A"


class MarketDataService:
    def __init__(self, config):
        self.config = config
        # L0 client: no auth needed for reading markets and order books
        self.client = ClobClient(CLOB_HOST)
        
        # For 1H discovery
        self._hourly_markets = []
        self._hourly_discovered = False
    
    def _discover_hourly_markets(self):
        """Dynamically discover 1H BTC Up/Down markets from Gamma API using slug generation."""
        if self._hourly_discovered:
            return
        
        print("[*] Discovering 1H BTC Up/Down markets from Gamma API...")
        
        # Generate candidate slugs for next 7 days
        slugs = []
        today = datetime.now(timezone.utc)
        
        for day_offset in range(0, 7):
            day = today + timedelta(days=day_offset)
            month_name = MONTHS[day.month - 1]
            
            # Generate hours 8AM-11PM ET (1PM-4AM UTC next day)
            for hour in range(8, 24):
                slug = f'bitcoin-up-or-down-{month_name}-{day.day}-{hour}pm-et'
                slugs.append(slug)
        
        print(f"[*] Testing {len(slugs)} candidate slugs...")
        
        # Fetch markets by slug (parallel)
        valid_markets = []
        
        def fetch_slug(slug):
            try:
                resp = requests.get(f'{GAMMA_API}/markets?slug={slug}', timeout=5)
                if resp.status_code == 200:
                    data = resp.json()
                    if data and isinstance(data, list) and len(data) > 0:
                        return data[0]
            except:
                pass
            return None
        
        # Use ThreadPoolExecutor for speed
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
            futures = {executor.submit(fetch_slug, slug): slug for slug in slugs}
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                if not result:
                    continue
                
                # Check if market is active
                if not result.get("active", False):
                    continue
                # accepting_orders can be None or True - treat None as True (open for trading)
                accepting = result.get("accepting_orders")
                if accepting is False:  # Only skip if explicitly False
                    continue
                if result.get("closed", False) or result.get("archived", False):
                    continue
                
                # Check question
                question = result.get("question", "").lower()
                if "bitcoin" not in question and "btc" not in question:
                    continue
                if "up or down" not in question and "up/down" not in question:
                    continue
                
                # Get times - eventStartTime is the actual 1H window start
                # startDate is when the market was created, not the window start
                start_time = result.get('eventStartTime', result.get('startTime', ''))
                end_date = result.get('endDate', '')
                
                if not start_time or not end_date:
                    continue
                
                # Parse duration
                try:
                    start_time = start_time.replace('Z', '+00:00')
                    if '.' in start_time:
                        start_time = start_time.split('.')[0] + '+00:00'
                    
                    end_date = end_date.replace('Z', '+00:00')
                    if '.' in end_date:
                        end_date = end_date.split('.')[0] + '+00:00'
                    
                    start_dt = datetime.fromisoformat(start_time)
                    end_dt = datetime.fromisoformat(end_date)
                    
                    duration_min = (end_dt - start_dt).total_seconds() / 60
                    
                    # Must be ~60 minutes (1 hour)
                    if not (50 <= duration_min <= 70):
                        continue
                    
                    # Check if resolves within reasonable time (not past, not too far)
                    now = datetime.now(timezone.utc)
                    hours_until = (end_dt - now).total_seconds() / 3600
                    
                    # Accept markets that resolve in next 12 hours (for trading)
                    if hours_until < -1:  # Already resolved
                        continue
                    
                    # Get token info from Gamma response
                    # Gamma returns clobTokenIds as JSON string
                    token_ids = json.loads(result.get('clobTokenIds', '[]'))
                    
                    if len(token_ids) != 2:
                        continue
                    
                    yes_token_id = token_ids[0]
                    no_token_id = token_ids[1]
                    
                    # Get current prices - determine source
                    # Try Gamma API first (outcomePrices)
                    outcome_prices = json.loads(result.get('outcomePrices', '[]'))
                    price_source = "gamma"
                    
                    if outcome_prices and len(outcome_prices) >= 2:
                        yes_price = float(outcome_prices[0])
                        no_price = float(outcome_prices[1])
                    else:
                        # Gamma didn't provide prices - need CLOB REST fallback
                        # This will be done at runtime by momentum_strategy
                        yes_price = 0.0
                        no_price = 0.0
                        price_source = "clob_fallback"
                    
                    # Track price source and timestamp
                    last_update_time = datetime.now(timezone.utc).isoformat()
                    
                    # Compute market status fields
                    accepting = result.get("accepting_orders")
                    accepting_orders = accepting is not False  # True if None or True
                    
                    # Compute in_window and time remaining
                    now = datetime.now(timezone.utc)
                    minutes_left = None
                    minutes_to_start = None
                    in_window = False
                    
                    if start_dt and end_dt:
                        if start_dt <= now <= end_dt:
                            # Currently in the 1-hour window
                            in_window = True
                            minutes_left = int((end_dt - now).total_seconds() / 60)
                        elif now < start_dt:
                            # Market hasn't started yet
                            minutes_to_start = int((start_dt - now).total_seconds() / 60)
                    
                    valid_markets.append({
                        "condition_id": result.get("condition_id"),
                        "yes_token_id": yes_token_id,
                        "no_token_id": no_token_id,
                        "yes_price": yes_price,
                        "no_price": no_price,
                        "price_source": price_source,
                        "last_update_time": last_update_time,
                        "title": result.get("question", ""),
                        "end_date": end_date,
                        "start_time": start_time,
                        "duration_min": duration_min,
                        "hours_until": hours_until,
                        "accepting_orders": accepting_orders,
                        "in_window": in_window,
                        "minutes_left": minutes_left,
                        "minutes_to_start": minutes_to_start,
                    })
                    
                except Exception as e:
                    continue
        
        # Sort by hours until resolution
        valid_markets.sort(key=lambda x: x.get('hours_until', 999))
        
        self._hourly_markets = valid_markets
        self._hourly_discovered = True
        
        # Print summary
        print(f"\n{'='*60}")
        print(f"FOUND {len(self._hourly_markets)} VALID 1H BTC UP/DOWN MARKETS")
        print(f"{'='*60}")
        
        if len(self._hourly_markets) == 0:
            print("[!] ERROR: No 1H BTC Up/Down markets found!")
            print("[!] HARD FAIL: Cannot trade anything else. Exiting.")
            raise SystemExit(1)
        
        # Print 3 examples with prices and source (startup proof)
        print("\nStartup prices (first 3 markets):")
        for i, market in enumerate(self._hourly_markets[:3]):
            yes_p = market.get('yes_price', 0)
            no_p = market.get('no_price', 0)
            source = market.get('price_source', 'unknown')
            last_update = market.get('last_update_time', '')[:19]
            print(f"  {i+1}. {market['title'][:50]}")
            print(f"     YES: ${yes_p:.2f} | NO: ${no_p:.2f} | source={source}")
            print(f"     Updated: {last_update}")
            print(f"     Start: {market['start_time'][:19]}")
            print(f"     End: {market['end_date'][:19]}")
            print(f"     Duration: {market['duration_min']:.0f} min")
            print()
        
        print(f"{'='*60}\n")
        
        # Get active (not resolved) markets only
        active_markets = [m for m in self._hourly_markets if m.get('hours_until', -1) >= 0]
        
        # Print market status (first active market sorted by hours_until)
        if active_markets:
            first_market = active_markets[0]
            in_window = first_market.get('in_window', False)
            minutes_left = first_market.get('minutes_left')
            minutes_to_start = first_market.get('minutes_to_start')
            
            if in_window:
                print(f"[*] WATCHING: {first_market['title'][:60]}")
                print(f"[*] Status: IN_WINDOW - {minutes_left} min left")
                print(f"[*] Entry allowed: YES" if minutes_left and minutes_left >= 5 else f"[*] Entry allowed: NO (cutoff)")
            else:
                print(f"[*] NEXT MARKET: {first_market['title'][:60]}")
                print(f"[*] Status: UPCOMING - starts in {minutes_to_start} min")
                print(f"[*] Entry allowed: NO (waiting for window)")
        elif self._hourly_markets:
            # All markets resolved
            print(f"[*] No active markets - waiting for next hourly market")
        print()

    def get_active_markets(self):
        """Spec 6.2: Pulls active markets (YES/NO only).
        Uses dynamic 1H discovery if enabled in config.
        
        Returns only markets that are not yet resolved (hours_until >= 0)."""
        
        use_hourly = self.config.get("USE_HOURLY_MARKETS", False)
        
        if use_hourly:
            # Dynamically discover 1H markets
            self._discover_hourly_markets()
            
            if self._hourly_markets:
                # Filter out resolved markets - only return active ones
                active_markets = [m for m in self._hourly_markets if m.get('hours_until', -1) >= 0]
                return active_markets
            else:
                # If no hourly markets found, hard fail
                print("[!] No hourly markets available - HARD FAIL")
                raise SystemExit(1)
        
        # Default: get all active markets from CLOB
        all_markets = []

        try:
            resp = self.client.get_sampling_simplified_markets()
            data = resp if isinstance(resp, list) else resp.get("data", [])
        except Exception as e:
            print(f"[!] Error fetching sampling markets: {e}")
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
                    "title": m.get("question", ""),
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
                        "condition_id": m.get("condition_id"),
                        "yes_token_id": yes_token.get("token_id"),
                        "no_token_id": no_token.get("token_id"),
                        "yes_price": yes_token.get("price", 0),
                        "no_price": no_token.get("price", 0),
                        "title": m.get("question", ""),
                        "end_date": m.get("endDate"),
                    })

            next_cursor = resp.get("next_cursor")
            if not next_cursor:
                break

        return all_markets

    def fetch_clob_price(self, token_id: str) -> float:
        """Fetch current price from CLOB REST API.
        Returns float price or None if failed."""
        try:
            # Try get_midpoint first
            try:
                price = self.client.get_midpoint(token_id)
                if price is not None and price > 0:
                    return float(price)
            except:
                pass
            
            # Try get_last_trade_price
            try:
                price = self.client.get_last_trade_price(token_id)
                if price is not None and price > 0:
                    return float(price)
            except:
                pass
            
            # Try get_price
            try:
                price = self.client.get_price(token_id, side="BUY")
                if price is not None and price > 0:
                    return float(price)
            except:
                pass
                
        except Exception as e:
            pass  # Suppress errors - don't spam logs
        
        return None

    def refresh_hourly_prices(self):
        """Refresh prices for hourly markets using CLOB REST fallback.
        Called periodically to get fresh prices."""
        if not self._hourly_markets:
            return
        
        now = datetime.now(timezone.utc)
        updated_count = 0
        no_price_count = 0
        clob_errors = 0
        
        for market in self._hourly_markets:
            yes_token = market.get("yes_token_id")
            no_token = market.get("no_token_id")
            
            if not yes_token or not no_token:
                continue
            
            # Check if we already have valid Gamma prices
            existing_yes = market.get("yes_price", 0)
            existing_no = market.get("no_price", 0)
            existing_source = market.get("price_source", "unknown")
            
            # Only try CLOB if Gamma prices are missing/zero
            if existing_yes <= 0 or existing_no <= 0:
                # Fetch fresh prices from CLOB
                yes_price = self.fetch_clob_price(yes_token)
                no_price = self.fetch_clob_price(no_token)
                
                if yes_price is not None and no_price is not None:
                    market["yes_price"] = yes_price
                    market["no_price"] = no_price
                    market["price_source"] = "clob_rest"
                    market["last_update_time"] = now.isoformat()
                    updated_count += 1
                else:
                    no_price_count += 1
                    clob_errors += 1
                    # Keep existing prices if available, mark as no_data only if both fail
                    if existing_yes <= 0 and existing_no <= 0:
                        market["price_source"] = "no_data"
            else:
                # Gamma prices are valid - keep them and update timestamp
                market["last_update_time"] = now.isoformat()
                if existing_source == "clob_fallback":
                    market["price_source"] = "gamma"  # Now we have Gamma prices
        
        # Only print error summary once per cycle, not per token
        if clob_errors > 0 and updated_count == 0:
            print(f"[!] NO PRICE DATA - cannot trade (Gamma failed, CLOB failed)")
        
        return updated_count
