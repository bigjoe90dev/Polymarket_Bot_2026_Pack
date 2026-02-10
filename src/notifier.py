"""Telegram notification system for trade alerts and daily summaries.

Uses raw Telegram Bot API via urllib — zero dependencies.
Setup: Create a bot via @BotFather, get token + chat_id.
Config keys: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

import json
import time
import threading
import urllib.request
import urllib.error

# Rate limit: max 1 message per N seconds per type (prevents spam)
RATE_LIMITS = {
    "trade": 5,        # 1 trade alert per 5 seconds
    "exit": 5,         # 1 exit alert per 5 seconds
    "tp_sl": 0,        # TP/SL always sends immediately
    "daily": 0,        # Daily summary always sends
    "alert": 30,       # System alerts every 30 seconds max
}


class TelegramNotifier:
    """Sends trade alerts and summaries to Telegram."""

    def __init__(self, config):
        self.token = config.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = config.get("TELEGRAM_CHAT_ID", "")
        self.enabled = bool(self.token and self.chat_id)
        self._last_sent = {}  # type -> timestamp (rate limiting)
        self._queue = []
        self._lock = threading.Lock()

        if self.enabled:
            print(f"[TG] Telegram alerts enabled (chat {self.chat_id[:6]}...)")
        else:
            print("[TG] Telegram not configured — set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID in config")

    def _can_send(self, msg_type):
        """Rate limit check."""
        limit = RATE_LIMITS.get(msg_type, 5)
        if limit == 0:
            return True
        last = self._last_sent.get(msg_type, 0)
        return time.time() - last >= limit

    def _send(self, text, msg_type="trade"):
        """Send message to Telegram (non-blocking)."""
        if not self.enabled:
            return
        if not self._can_send(msg_type):
            return

        self._last_sent[msg_type] = time.time()

        # Fire and forget in background thread
        t = threading.Thread(target=self._do_send, args=(text,), daemon=True)
        t.start()

    def _do_send(self, text):
        """Actually send the message via Telegram Bot API."""
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = json.dumps({
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }).encode("utf-8")

        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            print(f"[TG] Send failed: {e}")

    # ── Public API ────────────────────────────────────────────

    def notify_trade_opened(self, signal, result):
        """Alert when a copy trade is opened."""
        if not result or not result.get("success"):
            return

        market = signal.get("market_title", "?")
        outcome = signal.get("outcome", "?")
        whale = signal.get("source_wallet", "?")[:10]
        whale_price = signal.get("whale_price", 0)
        our_price = result.get("avg_price", 0)
        cost = result.get("total_cost", 0)
        size = result.get("size", 0)
        score = signal.get("score", 0)

        text = (
            f"<b>📈 COPY TRADE OPENED</b>\n"
            f"<b>{market}</b>\n"
            f"Side: <b>{outcome}</b>\n"
            f"Whale: <code>{whale}...</code> @ ${whale_price:.3f}\n"
            f"Our entry: ${our_price:.3f} | {size:.1f} shares\n"
            f"Cost: ${cost:.2f} | Score: {score}\n"
        )
        self._send(text, "trade")

    def notify_trade_closed(self, pos, reason, pnl):
        """Alert when a position is closed (TP/SL/exit/settlement)."""
        market = pos.get("market_name", "?")
        outcome = pos.get("outcome", "?")
        cost = pos.get("total_cost", 0)
        pnl_pct = (pnl / cost * 100) if cost > 0 else 0

        emoji = "✅" if pnl >= 0 else "❌"
        reason_label = {
            "TAKE_PROFIT": "Take Profit 🎯",
            "STOP_LOSS": "Stop Loss 🛑",
            "EXIT": "Whale Exit 🐋",
        }.get(reason, reason)

        text = (
            f"<b>{emoji} POSITION CLOSED</b>\n"
            f"<b>{market}</b>\n"
            f"Side: {outcome} | Reason: {reason_label}\n"
            f"Cost: ${cost:.2f} → P&L: <b>{'+' if pnl >= 0 else ''}{pnl:.2f}</b> ({pnl_pct:+.1f}%)\n"
        )
        self._send(text, "tp_sl" if reason in ("TAKE_PROFIT", "STOP_LOSS") else "exit")

    def notify_settlement(self, pos, pnl, winner):
        """Alert when a market settles."""
        market = pos.get("market_name", "?")
        outcome = pos.get("outcome", "?")
        cost = pos.get("total_cost", 0)
        won = outcome == winner

        emoji = "🏆" if won else "💀"
        text = (
            f"<b>{emoji} MARKET SETTLED</b>\n"
            f"<b>{market}</b>\n"
            f"Winner: <b>{winner}</b> | Our side: {outcome}\n"
            f"Cost: ${cost:.2f} → P&L: <b>{'+' if pnl >= 0 else ''}{pnl:.2f}</b>\n"
        )
        self._send(text, "tp_sl")

    def notify_daily_summary(self, portfolio_data):
        """Send daily P&L summary."""
        bal = portfolio_data.get("cash_balance", 0)
        start = portfolio_data.get("starting_balance", 0)
        realized = portfolio_data.get("realized_pnl", 0)
        trades = portfolio_data.get("total_trades", 0)
        wins = portfolio_data.get("winning_trades", 0)
        losses = portfolio_data.get("losing_trades", 0)
        positions = portfolio_data.get("open_positions", 0)
        wr = portfolio_data.get("win_rate", 0)

        emoji = "📊"
        text = (
            f"<b>{emoji} DAILY SUMMARY</b>\n"
            f"Balance: ${bal:.2f} (started ${start:.2f})\n"
            f"Realized P&L: <b>{'+' if realized >= 0 else ''}{realized:.2f}</b>\n"
            f"Trades: {trades} | W/L: {wins}/{losses} | WR: {wr:.0f}%\n"
            f"Open positions: {positions}\n"
        )
        self._send(text, "daily")

    def notify_alert(self, message):
        """Send system alert (kill switch, heartbeat, etc.)."""
        text = f"<b>⚠️ ALERT</b>\n{message}"
        self._send(text, "alert")

    def notify_startup(self, wallets, markets):
        """Send startup notification."""
        text = (
            f"<b>🚀 BOT STARTED</b>\n"
            f"Tracking: {wallets} wallets\n"
            f"Markets: {markets}\n"
            f"Mode: PAPER\n"
        )
        self._send(text, "daily")
