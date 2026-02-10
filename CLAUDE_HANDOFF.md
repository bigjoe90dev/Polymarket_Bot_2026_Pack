# Claude Build Instructions for Polymarket Bot

## Context
You are a Senior Python Engineer. You have received a "Build Pack" containing the specification and the initial codebase scaffolding for a production-ready Polymarket trading bot (CLOB).

## Your Mission
Your goal is to finalize the implementation of the python files in `src/`.
The structural scaffolding is already done. The logic for Risk (Sec 11) and Strategy (Sec 4) is mostly done.

**You must implement the API connections.**

## Constraints (Zero Ambiguity)
1.  **Do NOT change the architecture.** The file structure in `src/` maps 1:1 to the `SPEC.md`. Keep it.
2.  **Library**: Use `py-clob-client` (Polymarket's official python client) for all market data and ordering.
3.  **Safety First**:
    * Never remove the `RiskGuard` checks in `src/risk.py`.
    * Never bypass the `LockedProfit` check in `src/strategy.py`.
4.  **Secrets**: Use `src/config.py` as provided. It handles user prompts for keys. Do not hardcode keys.

## Specific Tasks for You
1.  **`src/market.py`**:
    * Flesh out `fetch_markets` using `py-clob-client` to get active YES/NO markets.
    * Implement `get_order_book` to get the live book depth.
2.  **`src/execution.py`**:
    * Implement `place_orders` using the CLOB client.
    * Implement the **Two-Leg Logic** (Spec Section 9.2): If one fills and the other doesn't, you MUST cancel and hedge.
3.  **`run.py`**:
    * Connect the loop. Currently it mocks data. Connect it to the real `market.py` functions you build.
