"""
Performance Evidence Bundle
─────────────────────────────
The platform-spec's answer to "never show a win rate alone": every stat
here comes bundled with sample size, a proper confidence interval, and a
few of the numbers that actually matter more than win rate (expectancy,
profit factor, drawdown) — modeled on the spec's own field list (basis,
sample, win rate + 95% CI, expectancy, profit factor, avg win/loss, max
drawdown).

Reuses models/calibration.py's trade-loading convention (paper_trades/
trades_*.jsonl), but is not about a displayed confidence SCORE — it's
about a STRATEGY's own realized track record, independent of whatever
score it assigns each signal. Two different questions:
  calibration.py:      "is this strategy's confidence score trustworthy?"
  evidence_bundle.py:  "how has this strategy actually performed?"
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Optional

# Below this many closed trades, a point estimate (win rate, expectancy...)
# is more noise than signal — suppress it rather than show a number
# precise-looking enough to be mistaken for reliable. Same threshold used
# elsewhere in this project's honesty-over-precision fixes this session
# (models/calibration.py's MIN_SAMPLE_FOR_VERDICT uses 30 for a stronger
# claim — "beats/misses no-skill"; 20 here is for a softer one — "here is
# an estimate at all").
MIN_SAMPLE_FOR_ESTIMATE = 20


def wilson_interval(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """
    95% Wilson score interval for a binomial proportion (win rate). Chosen
    over the naive p +/- 1.96*sqrt(p(1-p)/n) interval specifically because
    the naive form produces nonsensical bounds (a negative lower bound, or
    an upper bound above 1) exactly in the small-n / p-near-0-or-1 regime
    this project's paper-trading samples usually sit in — Wilson stays
    well-behaved there by construction.
    """
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    denom = 1 + z ** 2 / n
    center = (p + z ** 2 / (2 * n)) / denom
    margin = (z * math.sqrt(p * (1 - p) / n + z ** 2 / (4 * n ** 2))) / denom
    return (round(max(0.0, center - margin), 4), round(min(1.0, center + margin), 4))


def load_all_closed_trades(paper_trades_dir: Path) -> dict[str, list[dict]]:
    """
    Every CLOSED trade with a realised P&L, grouped by `strategy` — unlike
    models/calibration.py's load_closed_trades(), this doesn't require a
    specific score field, since an evidence bundle is about outcomes, not
    about validating a score. Deliberately a separate loader rather than
    loosening calibration.py's required score_field param — that
    strictness exists specifically to stop two structurally different
    scores being silently analyzed as one (see that module's docstring for
    what happened the one time this project didn't enforce it).
    """
    by_strategy: dict[str, list[dict]] = {}
    if not paper_trades_dir.exists():
        return by_strategy
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
                    if row.get("status") == "CLOSED" and row.get("realised_pnl") is not None:
                        by_strategy.setdefault(row.get("strategy", "?"), []).append(row)
        except OSError:
            continue
    return by_strategy


def _pnl_pct(t: dict) -> Optional[float]:
    entry = t.get("entry_premium")
    exit_ = t.get("exit_premium")
    if entry and exit_ is not None:
        return (exit_ - entry) / entry
    return None


def build_evidence_bundle(trades: list[dict], basis: str = "paper") -> dict:
    """
    `trades` should already be filtered to one strategy (and one basis —
    don't mix backtest/paper/live rows, they're different kinds of
    evidence and the spec is explicit that a backtested number must never
    render styled the same as a live one).
    """
    n = len(trades)
    if n == 0:
        return {"basis": basis, "n": 0, "sufficient_sample": False}

    wins = sum(1 for t in trades if (t.get("realised_pnl") or 0) > 0)
    win_rate = wins / n
    ci_lo, ci_hi = wilson_interval(wins, n)

    # Expectancy/avg-win/avg-loss in % of premium — dimensionless, so
    # comparable across trades on differently-priced options/underlyings.
    pnl_pcts = [p for t in trades if (p := _pnl_pct(t)) is not None]
    gains_pct = [p for p in pnl_pcts if p > 0]
    losses_pct = [p for p in pnl_pcts if p < 0]
    expectancy = sum(pnl_pcts) / len(pnl_pcts) if pnl_pcts else None
    avg_win = sum(gains_pct) / len(gains_pct) if gains_pct else None
    avg_loss = sum(losses_pct) / len(losses_pct) if losses_pct else None

    # Profit factor and max drawdown, by contrast, are only meaningful in
    # real currency — their standard definitions ARE currency ratios/curves.
    # Computing them from summed percentage returns instead (an earlier
    # version of this function did) produced a nonsensical "-242% drawdown"
    # on the very first real run: percentages don't sum across trades with
    # fixed lot sizes the way they would under fixed capital allocation.
    # realised_pnl (rupees) is a real, dimensionally consistent quantity
    # even though position sizes vary trade to trade.
    rupee_pnls = [t["realised_pnl"] for t in trades if t.get("realised_pnl") is not None]
    gains_rs = [p for p in rupee_pnls if p > 0]
    losses_rs = [p for p in rupee_pnls if p < 0]
    gain_sum, loss_sum = sum(gains_rs), abs(sum(losses_rs))
    if loss_sum > 0:
        profit_factor = round(gain_sum / loss_sum, 4)
    elif gain_sum > 0:
        profit_factor = None  # undefined (no losses to divide by) rather than a fake "infinity"
    else:
        profit_factor = 0.0

    # Cumulative realised_pnl (rupees) peak-to-trough, ordered by entry
    # time. NOT a percentage of capital — no fixed starting bankroll is
    # tracked for these paper trades, so a "% drawdown" would imply a
    # capital base that doesn't exist. Report the rupee figure plainly
    # instead of fabricating a percentage.
    ordered = sorted(trades, key=lambda t: t.get("entry_time") or "")
    cum = peak = max_dd = 0.0
    for t in ordered:
        pnl = t.get("realised_pnl")
        if pnl is None:
            continue
        cum += pnl
        peak = max(peak, cum)
        max_dd = min(max_dd, cum - peak)

    # Which cost model booked these trades. Everything closed before 2026-09-24
    # was booked on a flat Rs.40 commission with NO spread at all; after that the
    # measured bid-ask (config/measured_costs.py) is charged too. Averaging the
    # two produces a profit factor that describes neither sample, so the mix is
    # reported rather than hidden -- the reader can then decide whether the
    # number is comparable, and a mixed sample is a reason to wait for a clean
    # one, not to squint at this one.
    models: dict[str, int] = {}
    for t in trades:
        models[t.get("cost_model") or "flat40_no_spread"] = \
            models.get(t.get("cost_model") or "flat40_no_spread", 0) + 1

    return {
        "basis": basis,
        "n": n,
        "sufficient_sample": n >= MIN_SAMPLE_FOR_ESTIMATE,
        "cost_models": models,
        "cost_model_mixed": len(models) > 1,
        "win_rate": round(win_rate, 4),
        "win_rate_ci95": (ci_lo, ci_hi),
        "expectancy_pct": round(expectancy, 4) if expectancy is not None else None,
        "avg_win_pct": round(avg_win, 4) if avg_win is not None else None,
        "avg_loss_pct": round(avg_loss, 4) if avg_loss is not None else None,
        "profit_factor": profit_factor,
        "max_drawdown_rupees": round(max_dd, 2),
    }
