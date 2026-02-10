import os
import time

class RiskGuard:
    def __init__(self, config):
        self.config = config
        self.max_exposure = config.get("MAX_EXPOSURE", 25.0)
        self.current_exposure = 0.0
        self.daily_loss = 0.0
        self.max_daily_loss = config.get("MAX_DAILY_LOSS", 15.0)
        self.kill_switch = False
        self._day_start = time.time()

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

    def remove_exposure(self, amount):
        """Remove exposure when a position settles."""
        self.current_exposure = max(0.0, self.current_exposure - amount)

    def record_loss(self, amount):
        """Record a realized loss (positive number = loss)."""
        if amount > 0:
            self.daily_loss += amount

    def _check_day_reset(self):
        """Reset daily loss counter at midnight."""
        now = time.time()
        if now - self._day_start >= 86400:
            self.daily_loss = 0.0
            self._day_start = now
