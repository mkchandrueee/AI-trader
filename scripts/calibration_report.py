#!/usr/bin/env python3
"""
Confidence Calibration Report
──────────────────────────────
Checks whether `final_score` (the number shown on the Live/Trades pages as
a bare percentage) actually behaves like a probability of a winning trade,
using every closed paper/live trade recorded so far. See
models/calibration.py for the full method.

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

from models.calibration import load_closed_trades, compute_calibration


def main():
    project_root = Path(__file__).resolve().parent.parent
    trades = load_closed_trades(project_root / "paper_trades")
    report = compute_calibration(trades)

    print("=" * 60)
    print("  CONFIDENCE CALIBRATION REPORT")
    print("=" * 60)
    print(f"  Closed trades analyzed: {report['n_total']}")

    if report["n_total"] == 0:
        print("  No closed trades with a recorded final_score yet.")
        return 0

    print(f"  Overall win rate (base rate): {report['base_rate']*100:.1f}%")
    print(f"  Brier score treating final_score as a probability: {report['brier_score']}")
    print(f"  Brier score a no-skill model (always predicts the base rate) would get: {report['no_skill_brier']}")
    if report["brier_score"] > report["no_skill_brier"]:
        print("  -> final_score is currently WORSE than just guessing the base rate every time.")
        print("     It must not be displayed as a probability until this changes.")
    else:
        print("  -> final_score beats a no-skill baseline, but Brier score alone doesn't")
        print("     confirm good calibration — check the reliability table below.")
    print()

    print(f"  {'Score range':<14}{'n':>6}{'avg score':>12}{'actual win rate':>18}")
    print(f"  {'-'*14:<14}{'-'*6:>6}{'-'*12:>12}{'-'*18:>18}")
    for b in report["buckets"]:
        if b["n"] == 0:
            continue
        print(f"  {b['range']:<14}{b['n']:>6}{b['avg_score']*100:>11.1f}%{b['win_rate']*100:>17.1f}%")

    print()
    print("  Read this as: if final_score were a calibrated probability, 'avg score'")
    print("  and 'actual win rate' should be close in every row. A large gap in either")
    print("  direction means the number is not a probability, whatever it looks like.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
