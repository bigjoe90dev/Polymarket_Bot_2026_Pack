import os
import time
import json
from src.state_backup import save_state_with_backup, load_state_with_recovery

RISK_STATE_FILE = "data/risk_state.json"

class RiskGuard:
    def __init__(self, config):
        self.config = config
        self.max_exposure = config.get("MAX_EXPOSURE", 25.0)
        self.current_exposure = 0.0
        self.daily_loss = 0.0
        self.max_daily_loss = config.get("MAX_DAILY_LOSS", 15.0)
        self.kill_switch = False
        self._day_start = time.time()

        # Load persisted state (exposure + daily loss)
        self._load_state()

    def update_limits(self, balance, starting_balance):
        """Dynamically update limits based on current account balance."""
        growth = balance / max(starting_balance, 1)
        if growth >= 3.0:
            g = 2.0
        elif growth >= 2.0:
            g = 1.5
        elif growth >= 1.5:
            g = 1.25
        else:
            g = 1.0
        self.max_exposure = balance * self.config.get("RISK_MAX_EXPOSURE_PCT", 0.50) * g
        self.max_daily_loss = balance * self.config.get("RISK_MAX_DAILY_LOSS_PCT", 0.30) * g

    def check_kill_switch(self):
        """Spec 11: Simple file toggle."""
        if os.path.exists("STOP_TRADING"):
            self.kill_switch = True
            return True
        return False

    def can_trade(self, plan):
        if self.check_kill_switch(): return False
        self._check_day_reset()
        if self.daily_loss >= self.max_daily_loss: return False

        estimated_cost = (plan['buy_yes'] + plan['buy_no']) * plan['size']
        if (self.current_exposure + estimated_cost) > self.max_exposure:
            return False

        return True

    def add_exposure(self, amount):
        """Add exposure when a trade fills."""
        self.current_exposure += amount
        self._save_state()

    def remove_exposure(self, amount):
        """Remove exposure when a position settles."""
        self.current_exposure = max(0.0, self.current_exposure - amount)
        self._save_state()

    def record_loss(self, amount):
        """Record a realized loss (positive number = loss)."""
        if amount > 0:
            self.daily_loss += amount
            self._save_state()

    def _check_day_reset(self):
        """Reset daily loss counter at midnight."""
        now = time.time()
        if now - self._day_start >= 86400:
            self.daily_loss = 0.0
            self._day_start = now
            self._save_state()  # Persist the reset

    def _save_state(self):
        """Persist exposure and daily loss to disk (atomic write with backup)."""
        state = {
            "version": 1,
            "current_exposure": self.current_exposure,
            "daily_loss": self.daily_loss,
            "day_start": self._day_start,
            "last_updated": time.time(),
        }
        save_state_with_backup(RISK_STATE_FILE, state, generations=5)

    def _load_state(self):
        """Load persisted exposure and daily loss from disk (with auto-recovery)."""
        state = load_state_with_recovery(
            RISK_STATE_FILE,
            required_keys=["current_exposure", "daily_loss", "day_start"]
        )

        if not state:
            print("[RISK] No valid state found — starting fresh")
            return

        self.current_exposure = state.get("current_exposure", 0.0)
        self.daily_loss = state.get("daily_loss", 0.0)
        self._day_start = state.get("day_start", time.time())

        print(f"[RISK] Loaded state: exposure=${self.current_exposure:.2f}, "
              f"daily_loss=${self.daily_loss:.2f}")
