#!/usr/bin/env python3
"""
Performance Evidence Bundle Report
─────────────────────────────────────
Per-strategy win rate (with a proper 95% Wilson CI, never shown alone),
expectancy, profit factor, and max drawdown — computed from every closed
paper trade recorded so far. See models/evidence_bundle.py for the method.

Usage:
  python scripts/evidence_bundle_report.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.console import fix_windows_console_encoding
fix_windows_console_encoding()

from dotenv import load_dotenv
load_dotenv()

from models.evidence_bundle import load_all_closed_trades, build_evidence_bundle, MIN_SAMPLE_FOR_ESTIMATE


def main():
    project_root = Path(__file__).resolve().parent.parent
    by_strategy = load_all_closed_trades(project_root / "paper_trades")

    print("=" * 70)
    print("  PERFORMANCE EVIDENCE BUNDLE  (basis: paper)")
    print("=" * 70)

    if not by_strategy:
        print("  No closed trades recorded yet.")
        return 0

    for strategy, trades in sorted(by_strategy.items(), key=lambda kv: -len(kv[1])):
        bundle = build_evidence_bundle(trades, basis="paper")
        print()
        print(f"  {strategy}  (n={bundle['n']})")
        print("  " + "-" * 66)
        if not bundle["sufficient_sample"]:
            print(f"  INSUFFICIENT SAMPLE (n={bundle['n']}, need >={MIN_SAMPLE_FOR_ESTIMATE}) — "
                  f"no point estimate shown.")
            continue
        lo, hi = bundle["win_rate_ci95"]
        print(f"  Win rate:        {bundle['win_rate']*100:.1f}%  (95% CI: {lo*100:.1f}%-{hi*100:.1f}%)")
        exp = bundle["expectancy_pct"]
        print(f"  Expectancy:      {exp*100:+.2f}% per trade" if exp is not None else "  Expectancy:      --")
        aw, al = bundle["avg_win_pct"], bundle["avg_loss_pct"]
        print(f"  Avg win / loss:  {aw*100:+.1f}% / {al*100:+.1f}%" if aw is not None and al is not None else "  Avg win / loss:  --")
        pf = bundle["profit_factor"]
        print(f"  Profit factor:   {pf:.2f}" if pf is not None else "  Profit factor:   undefined (no losses in sample)")
        print(f"  Max drawdown:    Rs.{bundle['max_drawdown_rupees']:+,.0f}  "
              f"(cumulative P&L, not a % of capital -- no fixed bankroll is tracked)")

    print()
    print("  A wide CI (small n) means this strategy's true win rate could be far")
    print("  from the point estimate shown — treat it as a range, not a fact.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
