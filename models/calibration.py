"""
Confidence Calibration Tracking
─────────────────────────────────
Answers the question the dashboard's score displays implicitly ask: if
`final_score` were treated as a probability of a winning trade, would it
actually BE one? An uncalibrated score must be labelled a score, never a
probability — this module is what proves calibration (or the lack of it)
with real numbers instead of assuming either way.

Reads every closed paper/live trade (paper_trades/trades_*.jsonl — each
row already carries `final_score` and `realised_pnl`, see
backend/app.py's `_persist_closed_trade`/`_persist_agent_trade`), buckets
them by `final_score`, and compares each bucket's average score against
its ACTUAL observed win rate (a reliability curve), plus the overall
Brier score treating `final_score` as a predicted win probability.

Shared by scripts/calibration_report.py (human-run) and a future
/api/calibration/report route — one computation, not two that could
diverge.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

# Buckets chosen around the score thresholds backend/app.py already gates
# on (0.60/0.65/0.70/0.75/0.80 — see effective_threshold and the score-tiered
# SL/target logic in scan_market()), so this report can be read directly
# against those same decision boundaries.
BUCKET_EDGES = [0.0, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 1.01]


def _bucket_label(lo: float, hi: float) -> str:
    return f"{lo:.2f}-{min(hi, 1.0):.2f}"


def load_closed_trades(paper_trades_dir: Path) -> list[dict]:
    """
    Every CLOSED trade with both a recorded final_score and a realised
    P&L, across all mode files (trades_test.jsonl, trades_live.jsonl if
    it exists). Malformed lines are skipped, not fatal — this report
    should degrade gracefully on a partially-written file, not crash.
    """
    trades = []
    if not paper_trades_dir.exists():
        return trades
    for f in sorted(paper_trades_dir.glob("trades_*.jsonl")):
        try:
            with open(f, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if (row.get("status") == "CLOSED"
                            and row.get("final_score") is not None
                            and row.get("realised_pnl") is not None):
                        trades.append(row)
        except OSError:
            continue
    return trades


def compute_calibration(trades: list[dict]) -> dict:
    """
    Buckets closed trades by final_score and compares each bucket's
    average score (what a "probability of profit" reading implies) against
    the ACTUAL win rate observed in that bucket. Also computes the overall
    Brier score, treating final_score as if it were a predicted probability
    of a winning trade — exactly the assumption a bare-percentage display
    invites, and exactly what real trade outcomes can prove or disprove.
    """
    buckets = []
    for lo, hi in zip(BUCKET_EDGES[:-1], BUCKET_EDGES[1:]):
        in_bucket = [t for t in trades if lo <= t["final_score"] < hi]
        n = len(in_bucket)
        if n == 0:
            buckets.append({"range": _bucket_label(lo, hi), "n": 0, "avg_score": None, "win_rate": None})
            continue
        avg_score = sum(t["final_score"] for t in in_bucket) / n
        wins = sum(1 for t in in_bucket if (t.get("realised_pnl") or 0) > 0)
        buckets.append({
            "range": _bucket_label(lo, hi),
            "n": n,
            "avg_score": round(avg_score, 4),
            "win_rate": round(wins / n, 4),
        })

    n_total = len(trades)
    if n_total == 0:
        return {"n_total": 0, "brier_score": None, "base_rate": None, "buckets": buckets}

    brier = sum(
        (t["final_score"] - (1.0 if (t.get("realised_pnl") or 0) > 0 else 0.0)) ** 2
        for t in trades
    ) / n_total
    base_rate = sum(1 for t in trades if (t.get("realised_pnl") or 0) > 0) / n_total

    return {
        "n_total": n_total,
        "brier_score": round(brier, 4),
        "base_rate": round(base_rate, 4),
        # What a model with ZERO discrimination — always predicting the
        # base rate — would score. final_score doing worse than this means
        # it's actively misleading, not just imprecise.
        "no_skill_brier": round(base_rate * (1 - base_rate), 4),
        "buckets": buckets,
    }
