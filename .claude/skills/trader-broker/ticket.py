"""
Present an order ticket. Never send one.

This is the last step before a trade, so it is the most dangerous file here, and
it is deliberately the least capable: it builds the ticket, runs the SAME risk
gate the agent runs, and then STOPS. It has no code path that reaches a broker.

Why it works this way. This platform already has a human-in-the-loop design:
in paper mode strategy/intraday_agent.py fires directly into the paper book, and
in live mode it STAGES an approval (models/approval.py) that a human releases
from the dashboard. A skill that placed orders would bypass that, so this one
hands you the exact command or the dashboard step instead.

    python ticket.py --entry 120 --stop 105 --exit 140 --symbol NIFTY --lots 1
    python ticket.py --entry 120 --stop 105 --exit 140 --option-symbol NIFTY04NOV2625900CE
    python ticket.py ... --json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

LOT_SIZES = {"NIFTY": 65, "BANKNIFTY": 35, "FINNIFTY": 65, "SENSEX": 20}


def build(symbol: str, option_symbol: str | None, entry: float, stop: float,
          exit_px: float, lots: int) -> dict:
    units = LOT_SIZES.get(symbol.upper(), 65) * lots
    return {"symbol": symbol.upper(),
            "option_symbol": option_symbol or f"{symbol.upper()}-ATM",
            "entry": entry, "stop": stop, "partial": exit_px, "lot_size": units}


def gate(order: dict):
    from config.risk_profiles import RiskLevel, get_risk_profile
    from config.settings import INITIAL_CAPITAL, MAX_CONCURRENT_POSITIONS, MAX_DAILY_LOSS
    from risk import pretrade
    try:
        profile = get_risk_profile(RiskLevel(os.getenv("RISK_LEVEL", "medium").lower()))
    except (ValueError, KeyError):
        profile = get_risk_profile(RiskLevel.MEDIUM)
    limits = pretrade.Limits.from_config(profile, min_rr=1.0, capital=INITIAL_CAPITAL,
                                         max_concurrent=MAX_CONCURRENT_POSITIONS,
                                         max_daily_loss=MAX_DAILY_LOSS)
    try:
        import urllib.request
        with urllib.request.urlopen("http://localhost:5050/api/paper/positions", timeout=5) as r:
            rows = json.load(r).get("positions", [])
        book, src = pretrade.Book.from_paper_book(rows), f"live backend, {len(rows)} row(s)"
    except Exception as e:
        book, src = pretrade.Book(), f"backend unreachable ({type(e).__name__}); book treated as EMPTY"
    return pretrade.evaluate(order=order, book=book, limits=limits, now=datetime.now()), src


def main() -> None:
    ap = argparse.ArgumentParser(description="Present an order ticket for approval. Sends nothing.")
    ap.add_argument("--entry", type=float, required=True)
    ap.add_argument("--stop", type=float, required=True)
    ap.add_argument("--exit", dest="exit_px", type=float, required=True)
    ap.add_argument("--symbol", default="NIFTY")
    ap.add_argument("--option-symbol")
    ap.add_argument("--lots", type=int, default=1)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    order = build(a.symbol, a.option_symbol, a.entry, a.stop, a.exit_px, a.lots)
    verdict, book_src = gate(order)
    mode = os.getenv("TRADE_MODE", "paper").lower()
    risk_rs = max(0.0, order["entry"] - order["stop"]) * order["lot_size"]
    outlay = order["entry"] * order["lot_size"]

    if a.json:
        print(json.dumps({"ticket": order, "trade_mode": mode, "book": book_src,
                          "risk_rupees": round(risk_rs, 2), "premium_outlay": round(outlay, 2),
                          "verdict": verdict.to_dict(), "sent": False}, indent=2))
        sys.exit(0 if verdict.allowed else 1)

    print("ORDER TICKET  (nothing has been sent)")
    print(f"  instrument : {order['option_symbol']}  BUY  {order['lot_size']} units ({a.lots} lot)")
    print(f"  entry      : {order['entry']}      stop {order['stop']}      exit {order['partial']}")
    print(f"  risk       : Rs.{risk_rs:,.0f}        premium outlay Rs.{outlay:,.0f}")
    print(f"  trade mode : {mode.upper()}")
    print(f"  book       : {book_src}")

    print("\nRISK GATE")
    for c in verdict.checks:
        mark = {"pass": " ok ", "block": "BLOCK", "warn": "warn", "n/a": " -- "}[c.status]
        print(f"  [{mark}] {c.name:20} {c.detail}")

    if not verdict.allowed:
        print("\nBLOCKED. This ticket cannot be placed. Fix the reasons above or abandon it.")
        sys.exit(1)

    print("\nPASSED the risk gate. It is still NOT placed, and this skill will not place it.")
    if mode == "paper":
        print("  To enter it on the paper book, use the dashboard's Live page,")
        print("  or POST /api/paper/enter with this ticket.")
    else:
        print(f"  TRADE_MODE is {mode.upper()}. Live orders go through the approval queue:")
        print("  the agent stages the signal and you release it from the dashboard.")
        print("  Nothing here, and nothing automated, may release it for you.")
    sys.exit(0)


if __name__ == "__main__":
    main()
