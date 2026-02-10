import time
from src.records import log_decision

CLOB_HOST = "https://clob.polymarket.com"


class ExecutionEngine:
    def __init__(self, config, risk_guard, market_client=None):
        self.config = config
        self.risk = risk_guard
        self.mode = config["MODE"]
        self.client = None
        self.paper_engine = None

        # Initialize paper trading engine for PAPER mode
        if self.mode == "PAPER":
            from src.paper_engine import PaperTradingEngine
            self.paper_engine = PaperTradingEngine(config, market_client)

        # Initialize trading client only for LIVE mode (requires L2 auth)
        if self.mode == "LIVE":
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds
            from py_clob_client.constants import POLYGON

            creds = ApiCreds(
                api_key=config["POLY_API_KEY"],
                api_secret=config["POLY_SECRET"],
                api_passphrase=config["POLY_PASSPHRASE"],
            )
            self.client = ClobClient(
                CLOB_HOST,
                key=config.get("POLY_PRIVATE_KEY", ""),
                chain_id=POLYGON,
                creds=creds,
            )

    def execute_plan(self, plan, book=None, market_info=None):
        # PAPER mode: route to paper trading engine
        if self.mode == "PAPER" and self.paper_engine:
            result = self.paper_engine.execute_paper_trade(plan, book, market_info or {})
            if result.get("success"):
                print(f"[PAPER] Filled: YES@{result['yes_price']:.3f} + "
                      f"NO@{result['no_price']:.3f} x{result['size']} "
                      f"profit=${result['expected_profit']:.4f}")
            return result

        if self.mode != "LIVE":
            print(f"[{self.mode}] Would execute: {plan}")
            return

        # 1. Re-check Risk (Spec 9.3)
        if not self.risk.can_trade(plan):
            print("[!] Trade rejected by Risk Guard")
            log_decision("REJECTED", "Risk Guard blocked trade")
            return

        print(f"[EXEC] Placing Orders: YES @ {plan['buy_yes']}, NO @ {plan['buy_no']}")

        # Spec 9.2: Place both legs as limit orders
        yes_order_id = self._place_order(
            plan["yes_token_id"], "BUY", plan["buy_yes"], plan["size"]
        )
        no_order_id = self._place_order(
            plan["no_token_id"], "BUY", plan["buy_no"], plan["size"]
        )

        if not yes_order_id and not no_order_id:
            log_decision("FAILED", "Both orders failed to place")
            return

        self.monitor_fills(plan, yes_order_id, no_order_id)

    def _place_order(self, token_id, side, price, size):
        """Place a single limit order. Returns order ID or None."""
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL

        order_side = BUY if side == "BUY" else SELL

        try:
            order_args = OrderArgs(
                price=price,
                size=size,
                side=order_side,
                token_id=token_id,
            )
            signed_order = self.client.create_order(order_args)
            resp = self.client.post_order(signed_order, OrderType.GTC)

            if resp.get("success"):
                order_id = resp.get("orderID")
                print(f"[EXEC] Order placed: {order_id}")
                return order_id
            else:
                print(f"[!] Order failed: {resp.get('errorMsg', 'Unknown error')}")
                return None
        except Exception as e:
            print(f"[!] Order placement error: {e}")
            return None

    def monitor_fills(self, plan, yes_order_id, no_order_id):
        """Spec 9.2: Two-Leg fill policy — hedge if one leg fails."""
        if not yes_order_id and not no_order_id:
            return

        # If only one order was placed, cancel/hedge immediately
        if not yes_order_id or not no_order_id:
            placed_id = yes_order_id or no_order_id
            filled_side = "YES" if yes_order_id else "NO"
            self._cancel_and_hedge(placed_id, plan, filled_side=filled_side)
            return

        # Poll for fills (check every 0.5s, up to 10s)
        max_checks = 20
        for i in range(max_checks):
            time.sleep(0.5)

            try:
                yes_order = self.client.get_order(yes_order_id)
                no_order = self.client.get_order(no_order_id)
            except Exception as e:
                print(f"[!] Error checking order status: {e}")
                continue

            yes_matched = float(yes_order.get("size_matched", "0"))
            no_matched = float(no_order.get("size_matched", "0"))

            # Both fully filled — locked profit achieved
            if yes_matched >= plan["size"] and no_matched >= plan["size"]:
                profit = plan["expected_profit"] * plan["size"]
                log_decision("FILLED", f"Both legs filled. Locked profit: ${profit:.4f}")
                self.risk.current_exposure += (plan["buy_yes"] + plan["buy_no"]) * plan["size"]
                print(f"[EXEC] Both legs filled! Locked profit: ${profit:.4f}")
                return

            # After 5 seconds, check for stalled legs
            if i >= 10:
                yes_filled = yes_matched >= plan["size"]
                no_filled = no_matched >= plan["size"]

                if yes_filled and not no_filled:
                    print("[!] YES filled but NO stalled — canceling and hedging")
                    self._cancel_and_hedge(no_order_id, plan, filled_side="YES")
                    return
                elif no_filled and not yes_filled:
                    print("[!] NO filled but YES stalled — canceling and hedging")
                    self._cancel_and_hedge(yes_order_id, plan, filled_side="NO")
                    return

        # Timeout: cancel both unfilled orders
        print("[!] Fill timeout — canceling remaining orders")
        self._cancel_order(yes_order_id)
        self._cancel_order(no_order_id)
        log_decision("TIMEOUT", "Fill monitoring timeout, orders cancelled")

    def _cancel_order(self, order_id):
        """Cancel a single order."""
        if not order_id:
            return
        try:
            self.client.cancel(order_id)
            print(f"[EXEC] Cancelled order: {order_id}")
        except Exception as e:
            print(f"[!] Cancel error for {order_id}: {e}")

    def _cancel_and_hedge(self, unfilled_order_id, plan, filled_side=None):
        """Spec 9.2: Cancel unfilled order and hedge the filled side."""
        self._cancel_order(unfilled_order_id)

        if not filled_side:
            log_decision("HEDGE", "One leg failed to place, cancelled remaining")
            return

        # Hedge: sell the filled position back at current best bid
        if filled_side == "YES":
            hedge_token = plan["yes_token_id"]
        else:
            hedge_token = plan["no_token_id"]

        try:
            book = self.client.get_order_book(hedge_token)
            if book.bids:
                hedge_price = float(book.bids[0].price)
                self._place_order(hedge_token, "SELL", hedge_price, plan["size"])
                log_decision("HEDGE", f"Sold {filled_side} side at {hedge_price} to hedge")
                print(f"[EXEC] Hedge order placed for {filled_side} side at {hedge_price}")
            else:
                log_decision("HEDGE_FAILED", f"No bids available to hedge {filled_side} side")
                print(f"[!] No bids available to hedge {filled_side} side")
        except Exception as e:
            log_decision("HEDGE_FAILED", f"Hedge error: {e}")
            print(f"[!] Hedge error: {e}")
