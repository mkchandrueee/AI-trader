#!/usr/bin/env python3
"""
Strategy Health Check
────────────────────────
Runs models/strategy_registry.check_health() against every strategy's
current Performance Evidence Bundle (models/evidence_bundle.py), computed
from real closed paper/live trades. Automatically demotes a strategy to
SUSPENDED if it's measurably losing money over a real sample — see
strategy_registry.py's module docstring for exactly what that means and
why promotion is never automatic.

Meant to run periodically (piggyback on the existing post-market retrain
cadence — see CLAUDE.md's "Training Workflow (Post-Market)" section)
rather than continuously; a strategy's real track record doesn't change
meaningfully minute to minute.

Usage:
  python scripts/strategy_health_check.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.console import fix_windows_console_encoding
fix_windows_console_encoding()

from dotenv import load_dotenv
load_dotenv()

from models.evidence_bundle import load_all_closed_trades, build_evidence_bundle
from models.strategy_registry import check_health, get_state


def main():
    project_root = Path(__file__).resolve().parent.parent
    by_strategy = load_all_closed_trades(project_root / "paper_trades")

    print("=" * 70)
    print("  STRATEGY HEALTH CHECK")
    print("=" * 70)

    if not by_strategy:
        print("  No closed trades recorded yet — nothing to check.")
        return 0

    any_demoted = False
    for strategy, trades in sorted(by_strategy.items(), key=lambda kv: -len(kv[1])):
        bundle = build_evidence_bundle(trades, basis="paper")
        before = get_state(strategy)
        result = check_health(strategy, bundle)
        after = get_state(strategy)

        status = "DEMOTED" if result else "unchanged"
        print(f"\n  {strategy}  (n={bundle['n']}, state: {before} -> {after})  [{status}]")
        if not bundle.get("sufficient_sample"):
            print(f"    Insufficient sample (need >={20}) — health check skipped, state left as-is.")
        elif result:
            any_demoted = True
            print(f"    Reason: {result['history'][-1]['reason']}")
        else:
            pf = bundle.get("profit_factor")
            pf_str = f"{pf:.2f}" if pf is not None else "undefined"
            print(f"    profit_factor={pf_str} -- no demotion trigger hit.")

    print()
    if any_demoted:
        print("  One or more strategies were just SUSPENDED — check strategy/intraday_agent.py's")
        print("  status/log to confirm it stops firing that strategy's signals going forward.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
