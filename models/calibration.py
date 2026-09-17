"""
Confidence Calibration Tracking
─────────────────────────────────
Answers the question a bare-percentage score display implicitly asks: if
this number were treated as a probability of a winning trade, would it
actually BE one? An uncalibrated score must be labelled a score, never a
probability — this module is what proves calibration (or the lack of it)
with real numbers instead of assuming either way.

Reads every closed paper/live trade (paper_trades/trades_*.jsonl) and
buckets them by a chosen score field, comparing each bucket's average
score against its ACTUAL observed win rate (a reliability curve), plus
the overall Brier score treating that field as a predicted win
probability.

**Two structurally different scores exist in this codebase and must never
be analyzed as one series**: `final_score` (strategy/trade_scorer.py's
0.5*ml_prob + 0.3*flow + 0.2*technical XGBoost-driven blend) and
`candle_quality_confidence` (strategy/intraday_agent.py's
math_decision_engine — a deterministic candle-shape grade, no ML, no
historical calibration). They used to share the `final_score` key on disk
until that collision was found by this module's own first report giving
nonsensical numbers — see strategy/intraday_agent.py's `_mirror_open()`
for the fix. `score_field` below exists specifically so this can never
happen again silently: every caller must say which score it means.

Shared by scripts/calibration_report.py (human-run) and a future
/api/calibration/report route — one computation, not two that could
diverge.
"""

from __future__ import annotations

import json
from pathlib import Path

# Buckets chosen around the score thresholds backend/app.py already gates
# on for final_score (0.60/0.65/0.70/0.75/0.80 — see effective_threshold and
# the score-tiered SL/target logic in scan_market()); reused as-is for
# candle_quality_confidence too since that field is also a 0-1 reading and
# comparable bucket widths make the two reports easy to read side by side.
BUCKET_EDGES = [0.0, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 1.01]

KNOWN_SCORE_FIELDS = ("final_score", "candle_quality_confidence")

# Below this many closed trades, a "beats/misses no-skill" verdict is not
# trustworthy — a handful of trades can flip it either way on pure luck.
# Matches the platform spec's own minimum-evidence-threshold principle
# (§58): don't report a verdict the sample can't actually support.
MIN_SAMPLE_FOR_VERDICT = 30


def _bucket_label(lo: float, hi: float) -> str:
    return f"{lo:.2f}-{min(hi, 1.0):.2f}"


def load_closed_trades(paper_trades_dir: Path, score_field: str) -> list[dict]:
    """
    Every CLOSED trade carrying `score_field` (a number) and a realised
    P&L, across all mode files (trades_test.jsonl, trades_live.jsonl if it
    exists). Malformed lines are skipped, not fatal — this report should
    degrade gracefully on a partially-written file, not crash.

    `score_field` is required, not defaulted — see module docstring for
    why: silently assuming "final_score" is exactly the bug that produced
    a nonsensical calibration report the first time this was written.
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
                            and row.get(score_field) is not None
                            and row.get("realised_pnl") is not None):
                        trades.append(row)
        except OSError:
            continue
    return trades


def compute_calibration(trades: list[dict], score_field: str) -> dict:
    """
    Buckets closed trades by `score_field` and compares each bucket's
    average score (what a "probability of profit" reading implies) against
    the ACTUAL win rate observed in that bucket. Also computes the overall
    Brier score, treating `score_field` as if it were a predicted
    probability of a winning trade — exactly the assumption a bare-
    percentage display invites, and exactly what real trade outcomes can
    prove or disprove.
    """
    buckets = []
    for lo, hi in zip(BUCKET_EDGES[:-1], BUCKET_EDGES[1:]):
        in_bucket = [t for t in trades if lo <= t[score_field] < hi]
        n = len(in_bucket)
        if n == 0:
            buckets.append({"range": _bucket_label(lo, hi), "n": 0, "avg_score": None, "win_rate": None})
            continue
        avg_score = sum(t[score_field] for t in in_bucket) / n
        wins = sum(1 for t in in_bucket if (t.get("realised_pnl") or 0) > 0)
        buckets.append({
            "range": _bucket_label(lo, hi),
            "n": n,
            "avg_score": round(avg_score, 4),
            "win_rate": round(wins / n, 4),
        })

    n_total = len(trades)
    if n_total == 0:
        return {"score_field": score_field, "n_total": 0, "brier_score": None, "base_rate": None, "buckets": buckets}

    brier = sum(
        (t[score_field] - (1.0 if (t.get("realised_pnl") or 0) > 0 else 0.0)) ** 2
        for t in trades
    ) / n_total
    base_rate = sum(1 for t in trades if (t.get("realised_pnl") or 0) > 0) / n_total
    no_skill_brier = base_rate * (1 - base_rate)

    return {
        "score_field": score_field,
        "n_total": n_total,
        "brier_score": round(brier, 4),
        "base_rate": round(base_rate, 4),
        # What a model with ZERO discrimination — always predicting the
        # base rate — would score. The real score doing worse than this
        # means it's actively misleading, not just imprecise.
        "no_skill_brier": round(no_skill_brier, 4),
        # A "beats/misses no-skill" verdict isn't trustworthy below
        # MIN_SAMPLE_FOR_VERDICT trades, and is meaningless outright when
        # no_skill_brier is exactly 0 -- that only happens when every
        # single trade in the sample won (or every one lost), which makes
        # a "predict the base rate" baseline artificially perfect by
        # definition, not because it's actually a strong baseline. Caught
        # live: 5 brand-new candle_quality_confidence trades that all
        # happened to win produced a "WORSE than no-skill" verdict that
        # looked damning but was really just n=5 and a lucky streak.
        "verdict_reliable": n_total >= MIN_SAMPLE_FOR_VERDICT and no_skill_brier > 0,
        "buckets": buckets,
    }


def compute_direction_breakdown(trades: list[dict]) -> dict:
    """
    Win rate and average P&L% split by trade direction (CALL/PUT).

    Added after digging into why candle_quality_confidence calibrated so
    poorly on its first real sample: direction turned out to be a much
    bigger driver of outcome than the score itself (CALL 33.3% vs PUT
    59.2% win rate, avg P&L -2.4% vs +2.2% on the first 94 math_decision_engine
    trades) — unsurprising once you consider the score only grades candle
    *shape*, with no concept of market direction or regime at all. That
    gap is invisible unless tracked separately, which is what this is for.

    avg_pnl_pct is (exit_premium - entry_premium) / entry_premium, not raw
    rupee P&L — necessary because different underlyings (NIFTY/BANKNIFTY/
    SENSEX) have different lot sizes, so rupee P&L isn't comparable across
    them but a percentage move in the option premium is.
    """
    breakdown = {}
    for direction in ("CALL", "PUT"):
        sub = [t for t in trades if t.get("direction") == direction]
        n = len(sub)
        if n == 0:
            breakdown[direction] = {"n": 0, "win_rate": None, "avg_pnl_pct": None}
            continue
        wins = sum(1 for t in sub if (t.get("realised_pnl") or 0) > 0)
        pnl_pcts = [
            (t["exit_premium"] - t["entry_premium"]) / t["entry_premium"]
            for t in sub
            if t.get("entry_premium") and t.get("exit_premium") is not None
        ]
        breakdown[direction] = {
            "n": n,
            "win_rate": round(wins / n, 4),
            "avg_pnl_pct": round(sum(pnl_pcts) / len(pnl_pcts), 4) if pnl_pcts else None,
        }
    return breakdown
