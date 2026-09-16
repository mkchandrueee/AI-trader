"""
Broker Execution Adapters
─────────────────────────
Broker-agnostic execution layer for the AI Trader signal pipeline.

Architecture:
  scan_market() → OrderManager → BrokerAdapter.buy/sell/modify_sl

Adapters:
  PaperAdapter     — simulated trades (default, current behavior)
  AngelOneAdapter  — AngelOne SmartAPI (real money)
  MStockAdapter    — mStock Trading API, Type A (real money) — the intended
                     broker for live trading going forward, once TRADE_MODE
                     leaves "paper"

Config:
  TRADE_MODE env var: "paper" (default) | "angelone" | "mstock"
"""
