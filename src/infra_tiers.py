"""Infrastructure tier definitions for paper trading simulation.

Models the real-world cost and performance impact of different
server setups on a Polymarket arbitrage bot.
"""

import random

# ── TIER DEFINITIONS ─────────────────────────────────────────
# Each tier models: monthly cost, execution quality, and reliability.
#
# slippage_mult:  Multiplier on simulated slippage (1.0 = baseline)
# fill_rate:      Probability a valid opportunity actually fills (0-1)
# uptime:         Fraction of time the bot is actually running (0-1)
# scan_speed:     Markets scanned per cycle (more = more chances)
# monthly_cost:   USD deducted from paper balance each month

TIERS = {
    1: {
        "name": "Local Machine",
        "tag": "FREE",
        "description": "Your laptop. Free but unreliable — sleeps, restarts, home internet latency.",
        "monthly_cost": 0.0,
        "slippage_mult": 2.0,
        "fill_rate": 0.80,
        "uptime": 0.90,
        "scan_speed": 5,
        "latency_ms": 120,
    },
    2: {
        "name": "Basic Cloud VPS",
        "tag": "$12/mo",
        "description": "DigitalOcean/Vultr basic droplet. 24/7 uptime, decent latency.",
        "monthly_cost": 12.0,
        "slippage_mult": 1.4,
        "fill_rate": 0.90,
        "uptime": 0.99,
        "scan_speed": 5,
        "latency_ms": 45,
    },
    3: {
        "name": "Performance Cloud",
        "tag": "$48/mo",
        "description": "Dedicated vCPUs, NVMe, low-latency region. Serious setup.",
        "monthly_cost": 48.0,
        "slippage_mult": 1.1,
        "fill_rate": 0.96,
        "uptime": 0.999,
        "scan_speed": 10,
        "latency_ms": 12,
    },
    4: {
        "name": "Co-located Premium",
        "tag": "$150/mo",
        "description": "Bare-metal near exchange infrastructure. Minimal latency, max fill rate.",
        "monthly_cost": 150.0,
        "slippage_mult": 1.0,
        "fill_rate": 0.99,
        "uptime": 0.9999,
        "scan_speed": 15,
        "latency_ms": 2,
    },
}


def get_tier(tier_id):
    """Get tier config by ID. Defaults to tier 1 (free)."""
    return TIERS.get(tier_id, TIERS[1])


def get_all_tiers():
    """Return all tier definitions for the UI."""
    return {k: dict(v) for k, v in TIERS.items()}


# ── SIMULATION EFFECTS ───────────────────────────────────────

def apply_uptime_check(tier):
    """Simulate whether the bot is 'online' this cycle.
    Returns True if the bot is up, False if simulating downtime."""
    return random.random() < tier["uptime"]


def apply_fill_rate(tier):
    """Simulate whether a valid opportunity actually gets filled.
    Lower tiers miss more opportunities due to latency."""
    return random.random() < tier["fill_rate"]


def apply_slippage(base_slippage, tier):
    """Scale slippage based on infrastructure quality."""
    return base_slippage * tier["slippage_mult"]


def calculate_daily_infra_cost(tier):
    """Convert monthly cost to daily rate."""
    return tier["monthly_cost"] / 30.0


def calculate_hourly_infra_cost(tier):
    """Convert monthly cost to hourly rate (for per-cycle deduction)."""
    return tier["monthly_cost"] / (30.0 * 24.0)


def tier_comparison_table(total_profit, days_running):
    """Generate a comparison of how each tier would have performed.
    Useful for the UI to show 'what-if' across all tiers."""
    results = []
    for tid, tier in TIERS.items():
        infra_cost = tier["monthly_cost"] * (days_running / 30.0)
        # Scale profit by fill_rate and uptime relative to the best tier
        best = TIERS[4]
        relative_efficiency = (tier["fill_rate"] * tier["uptime"]) / (best["fill_rate"] * best["uptime"])
        estimated_gross = total_profit * relative_efficiency
        net = estimated_gross - infra_cost

        results.append({
            "tier_id": tid,
            "name": tier["name"],
            "tag": tier["tag"],
            "monthly_cost": tier["monthly_cost"],
            "total_infra_cost": round(infra_cost, 2),
            "estimated_gross_profit": round(estimated_gross, 2),
            "estimated_net_profit": round(net, 2),
            "fill_rate": tier["fill_rate"],
            "uptime": tier["uptime"],
            "latency_ms": tier["latency_ms"],
            "roi_positive": net > 0,
        })
    return results
