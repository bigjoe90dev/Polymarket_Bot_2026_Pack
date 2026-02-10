# LLM Review Tracking

This folder contains external LLM audits of the Polymarket bot codebase.

## Review Rounds

### Round 1 (v9) - Initial Reviews
- **Date**: February 2026
- **Config Version**: 9
- **Key Issues Identified**:
  - TP/SL asymmetry (15% TP / 25% SL - backwards risk/reward)
  - Polling latency concerns
  - Correlation risk (multiple whales same market)
  - Survivorship bias in whale selection
  - Category-specific scoring needed

**Consensus**: System had potential but needed TP/SL fix and better risk controls.

---

### Round 2 (v10) - Post-Fix Validation
- **Date**: February 2026
- **Config Version**: 10
- **Starting Balance**: $50
- **Changes Made**:
  - ✅ Dynamic TP/SL: Fast 20%/12%, Slow 30%/15% (reward > risk)
  - ✅ Category-specific wallet scoring
  - ✅ Per-market exposure cap (6% max)
  - ✅ Fresh start with corrected risk/reward ratios

**Goal**: Validate that v10 changes address previous concerns and assess production readiness.

---

## LLMs Consulted
1. Grok (xAI)
2. Gemini (Google)
3. GPT-4 (OpenAI)
4. Kimi K2.5 (Moonshot AI)
5. DeepSeek

## Review Format
Each review should include:
- Executive summary (production ready / needs work / not viable)
- Critical issues (must fix before live)
- Edge analysis (breakeven whale win rate required)
- Scaling assessment (what breaks at $1k, $5k, $10k)
- Architecture review (code quality grade)
- Honest take (would you deploy this with real money?)
