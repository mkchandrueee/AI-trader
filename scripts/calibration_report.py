#!/usr/bin/env python3
"""
Confidence Calibration Report
──────────────────────────────
Checks whether a displayed confidence score actually behaves like a
probability of a winning trade, using every closed paper/live trade
recorded so far. See models/calibration.py for the full method.

Runs the report separately for each known score field (final_score, the
XGBoost strategy pipeline's blend, and candle_quality_confidence, the
math_decision_engine agent's deterministic candle-shape grade) — these are
structurally different numbers and must never be combined into one
analysis (they used to collide under the same "final_score" key on disk;
see strategy/intraday_agent.py's _mirror_open() for the fix and this
report's git history for what that collision looked like).

Usage:
  python scripts/calibration_report.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.console import fix_windows_console_encoding
fix_windows_console_encoding()

from dotenv import load_dotenv
load_dotenv()

from models.calibration import load_closed_trades, compute_calibration, KNOWN_SCORE_FIELDS


def _print_report(report: dict, label: str):
    print("-" * 60)
    print(f"  {label}  (field: {report['score_field']})")
    print("-" * 60)
    print(f"  Closed trades analyzed: {report['n_total']}")

    if report["n_total"] == 0:
        print("  No closed trades with this score field yet.")
        print()
        return

    print(f"  Overall win rate (base rate): {report['base_rate']*100:.1f}%")
    print(f"  Brier score treating it as a probability: {report['brier_score']}")
    print(f"  Brier score a no-skill model (always predicts the base rate) would get: {report['no_skill_brier']}")
    if report["brier_score"] > report["no_skill_brier"]:
        print("  -> WORSE than just guessing the base rate every time.")
        print("     Must not be displayed as a probability until this changes.")
    else:
        print("  -> Beats a no-skill baseline, but that alone doesn't confirm good")
        print("     calibration -- check the reliability table below.")
    print()

    print(f"  {'Score range':<14}{'n':>6}{'avg score':>12}{'actual win rate':>18}")
    print(f"  {'-'*14:<14}{'-'*6:>6}{'-'*12:>12}{'-'*18:>18}")
    for b in report["buckets"]:
        if b["n"] == 0:
            continue
        print(f"  {b['range']:<14}{b['n']:>6}{b['avg_score']*100:>11.1f}%{b['win_rate']*100:>17.1f}%")
    print()


def main():
    project_root = Path(__file__).resolve().parent.parent
    paper_trades_dir = project_root / "paper_trades"

    print("=" * 60)
    print("  CONFIDENCE CALIBRATION REPORT")
    print("=" * 60)
    print()

    labels = {
        "final_score": "XGBoost strategy pipeline (ml_prob/flow/technical blend)",
        "candle_quality_confidence": "math_decision_engine agent (candle-shape grade, no ML)",
    }

    for field in KNOWN_SCORE_FIELDS:
        trades = load_closed_trades(paper_trades_dir, field)
        report = compute_calibration(trades, field)
        _print_report(report, labels.get(field, field))

    print("Read each table as: if the score were a calibrated probability, 'avg score'")
    print("and 'actual win rate' should be close in every row. A large gap in either")
    print("direction means the number is not a probability, whatever it looks like.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
