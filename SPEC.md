# Polymarket Bot — Production Spec (CLOB / order book) — v0.1

## 0) What this is
This document describes a **real, production-ready Polymarket trading bot** that trades on the **central order book** (CLOB).

## 1) Goals
1. **Find only trades that are actually fillable** at the current order book.
2. **Only trade when the bot expects net profit after costs.**
3. **Never “hang” with one-sided exposure** without an automatic fix plan.
4. Run 24/7 with safe defaults.

## 4) Core idea (what we trade)
### 4.1 Locked-profit check (primary strategy)
For a YES/NO market:
If **you can buy both** and: `ask_yes + ask_no + all_costs < 1.00`, then buying both sides locks profit at settlement.

## 6) System shape (modules)
- **Config**: Reads settings, hides secrets.
- **Market Catalog**: Pulls active markets.
- **Live Order Book**: Maintains latest depth.
- **Opportunity Engine**: Calculates locked profit.
- **Execution Engine**: Places orders, handles "Two-Leg" risk.
- **Risk Guard**: Enforces max exposure and hard stops.

## 9) Execution rules
### 9.2 Default fill policy (safe)
- Place both legs immediately as limit orders.
- If one leg fills but the other does not:
  - cancel the unfilled leg quickly
  - hedge the filled leg using the current book, even if that reduces profit
