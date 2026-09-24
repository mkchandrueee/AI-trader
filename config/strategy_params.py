"""
Strategy Parameter Schema
─────────────────────────
One registry of every tunable in the trading logic, with bounds and a category.
Today those constants live scattered across strategy/math_decision_strategy.py's
DEFAULT_CFG, strategy/intraday_scanner.py, strategy/positional_scanner.py and
strategy/intraday_agent.py, which makes three things impossible: seeing what is
actually tunable, knowing what a safe range is, and validating a proposed change
before it ships.

DESIGN: this schema does NOT own the values. Each Param points at the module
attribute that is still the single source of truth, and `current()` reads it live
via importlib. Nothing here can silently drift out of sync with the code, and
importing this module changes no behaviour anywhere. (The alternative -- making
every strategy module read its constants from a config file -- is a large refactor
of live trading code for no immediate gain; it can happen later, and this schema is
the prerequisite for it either way.)

CATEGORIES -- the important part, and where this deliberately differs from the
"tactical vs personality" split in the Moss reference (REVIEW_2026-09-21.md):

  tactical  Pattern/threshold values that define WHEN a setup fires. Legitimately
            tunable, but only against held-out sessions -- 2026-09-21's research
            found 0 of 240 pattern x entry x stop/target cells profitable on either
            train or test, so tuning these without a holdout manufactures false
            edges rather than finding real ones.

  locked    Risk controls and structural facts (MIN_RR, the square-off time, market
            hours, lot size). Changing one changes what the strategy IS or removes a
            safety net. Never auto-tuned; a human edits the module deliberately.

  measured  Values that must come from MEASUREMENT, not choice -- costs and
            slippage. Picking these optimistically is the single easiest way to
            invent an edge on paper. The intraday cost assumption was 3x too low
            until it was corrected against real charges, and the option spread is
            now measurable from our own recorded bid/ask ticks (median 0.241% round
            trip on the ATM strikes the agent trades).
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from datetime import time as dtime
from typing import Any, Optional

TACTICAL = "tactical"
LOCKED = "locked"
MEASURED = "measured"


@dataclass(frozen=True)
class Param:
    key: str                      # stable id for APIs/UI, e.g. "intraday.or_bars"
    module: str                   # module that owns the real value
    attr: str                     # attribute name inside that module (or inside `container`)
    category: str                 # tactical | locked | measured
    unit: str                     # "bars", "fraction", "%", "points", "time", ...
    desc: str
    lo: Optional[float] = None    # inclusive bound; None = not numerically bounded
    hi: Optional[float] = None
    group: str = ""               # UI grouping
    container: Optional[str] = None  # module-level dict holding the value, e.g. "DEFAULT_CFG"

    def current(self) -> Any:
        """Live value, read from the owning module. Never cached -- that is the point."""
        mod = importlib.import_module(self.module)
        if self.container:
            return getattr(mod, self.container)[self.attr]
        return getattr(mod, self.attr)

    def violation(self) -> Optional[str]:
        """Why the live value is unreadable or outside its declared bounds, else None."""
        try:
            v = self.current()
        except Exception as e:            # a rename or removal must be loud, not silent
            return f"unreadable: {type(e).__name__}: {e}"
        return self.check(v)

    def check(self, v: Any) -> Optional[str]:
        if isinstance(v, bool) or isinstance(v, (dtime, tuple, list, str)):
            return None                                   # not numerically bounded
        if not isinstance(v, (int, float)):
            return f"unexpected type {type(v).__name__}"
        if self.lo is not None and v < self.lo:
            return f"{v} is below the floor {self.lo}"
        if self.hi is not None and v > self.hi:
            return f"{v} is above the ceiling {self.hi}"
        return None


_IA = "strategy.intraday_agent"
_IS = "strategy.intraday_scanner"
_PS = "strategy.positional_scanner"
_MD = "strategy.math_decision_strategy"

PARAMS: list[Param] = [
    # ── Option engine: the live paper agent's own entry/exit geometry ────────────
    # These live inside math_decision_strategy.DEFAULT_CFG rather than as module
    # constants, hence container="DEFAULT_CFG".
    Param("engine.side_min_confidence", _MD, "sideMinConfidence", TACTICAL, "score 0-100",
          "Winning leg's candle quality must reach this before a side is taken.", 40, 90,
          "Option engine", "DEFAULT_CFG"),
    Param("engine.side_min_margin", _MD, "sideMinMargin", TACTICAL, "score points",
          "...and must beat the losing leg by at least this much.", 0, 40,
          "Option engine", "DEFAULT_CFG"),
    Param("engine.buffer_pct", _MD, "engineBufferPct", TACTICAL, "% of leg range",
          "Breakout buffer above the candle high, as a share of that candle's range.", 0, 50,
          "Option engine", "DEFAULT_CFG"),
    Param("engine.buffer_min", _MD, "engineBufferMin", TACTICAL, "premium points",
          "Floor under the breakout buffer, so a tiny candle still needs a real move.", 0, 20,
          "Option engine", "DEFAULT_CFG"),
    Param("engine.partial_pts", _MD, "enginePartialPts", LOCKED, "premium points",
          "Where the agent exits the WHOLE lot. Locked: it defines the trade's actual R:R "
          "(rr_partial), which the MIN_RR gate is measured against.", 1, 100,
          "Option engine", "DEFAULT_CFG"),
    Param("engine.target_pts", _MD, "engineTargetPts", LOCKED, "premium points",
          "The reference tool's far target. Locked for parity with the published tool; the "
          "agent does not exit here.", 1, 200,
          "Option engine", "DEFAULT_CFG"),

    # ── Live agent: risk controls ───────────────────────────────────────────────
    Param("agent.min_rr", _IA, "MIN_RR", LOCKED, "ratio",
          "Minimum reward:risk (on rr_partial, the exit actually used) to take a trade. Risk control.",
          0.5, 5.0, "Agent risk"),
    Param("agent.entry_interval_secs", _IA, "ENTRY_INTERVAL_SECS", LOCKED, "seconds",
          "One entry check per symbol per this interval. Lower = more broker calls into a shared rate limit.",
          15, 600, "Agent risk"),
    Param("agent.exit_check_interval_secs", _IA, "EXIT_CHECK_INTERVAL_SECS", LOCKED, "seconds",
          "How often open positions are checked for stop/target.", 1, 120, "Agent risk"),
    Param("agent.inter_call_pause_secs", _IA, "INTER_CALL_PAUSE_SECS", LOCKED, "seconds",
          "Spacing between per-symbol calls, to stay under AngelOne's ~3 req/sec ceiling.", 0, 10, "Agent risk"),
    Param("agent.eod_squareoff", _IA, "EOD_SQUAREOFF", LOCKED, "time",
          "Everything still open is squared off here rather than carried overnight.", None, None, "Agent risk"),

    # ── Intraday Engine: costs (MEASURED, not chosen) ───────────────────────────
    Param("intraday.cost_pct", _IS, "COST_PCT", MEASURED, "fraction, round trip",
          "Brokerage + STT + exchange/GST/stamp for NSE intraday equity. Measured from the real "
          "charge schedule, not picked to make a backtest look good.", 0.0005, 0.005, "Costs"),
    Param("intraday.slippage_pct", _IS, "SLIPPAGE_PCT", MEASURED, "fraction, round trip",
          "Assumed slippage on a breakout entry. Replaceable with the measured half-spread from our "
          "own recorded bid/ask ticks.", 0.0002, 0.005, "Costs"),

    # ── Intraday Engine: replay/evaluation settings ─────────────────────────────
    Param("intraday.forward_bars", _IS, "FORWARD_BARS", LOCKED, "5-min bars",
          "Hit-rate measurement window (6 bars = 30 min). Changing it changes what the scorecard means.",
          1, 78, "Intraday evaluation"),
    Param("intraday.forward_hit", _IS, "FORWARD_HIT", LOCKED, "fraction",
          "Move size counted as a 'hit' in the scorecard.", 0.001, 0.05, "Intraday evaluation"),
    Param("intraday.sim_max_bars", _IS, "SIM_MAX_BARS", LOCKED, "5-min bars",
          "Maximum hold in the trade replay before a timeout exit.", 2, 78, "Intraday evaluation"),

    # ── Intraday Engine: pattern thresholds ─────────────────────────────────────
    Param("intraday.or_bars", _IS, "OR_BARS", TACTICAL, "5-min bars",
          "Opening range length (3 bars = 09:15-09:30).", 1, 12, "Intraday patterns"),
    Param("intraday.or_min_range", _IS, "OR_MIN_RANGE", TACTICAL, "fraction",
          "Opening range narrower than this is noise, not a range.", 0.0005, 0.01, "Intraday patterns"),
    Param("intraday.or_max_range", _IS, "OR_MAX_RANGE", TACTICAL, "fraction",
          "Wider than this and the break is already exhausted.", 0.005, 0.10, "Intraday patterns"),
    Param("intraday.or_fresh_bars", _IS, "OR_FRESH_BARS", TACTICAL, "5-min bars",
          "A break older than this is history, not a setup.", 1, 39, "Intraday patterns"),
    Param("intraday.near_trigger", _IS, "NEAR_TRIGGER", TACTICAL, "fraction",
          "How close under the trigger still counts as 'coiling'.", 0.0005, 0.01, "Intraday patterns"),
    Param("intraday.box_bars", _IS, "BOX_BARS", TACTICAL, "5-min bars",
          "Consolidation window for the box-break pattern (12 bars = 1 hour).", 4, 39, "Intraday patterns"),
    Param("intraday.box_max_depth", _IS, "BOX_MAX_DEPTH", TACTICAL, "fraction",
          "A box deeper than this is a trend, not a consolidation.", 0.002, 0.03, "Intraday patterns"),
    Param("intraday.level_fresh_bars", _IS, "LEVEL_FRESH_BARS", TACTICAL, "5-min bars",
          "How recently the previous-day level must have broken.", 1, 39, "Intraday patterns"),
    Param("intraday.vwap_lookback", _IS, "VWAP_LOOKBACK", TACTICAL, "5-min bars",
          "Bars examined for the VWAP reclaim/loss setup.", 3, 39, "Intraday patterns"),
    Param("intraday.vwap_min_other_side", _IS, "VWAP_MIN_OTHER_SIDE", TACTICAL, "bars",
          "How many of those bars must sit on the far side of VWAP first.", 1, 39, "Intraday patterns"),
    Param("intraday.breakout_vol_mult", _IS, "BREAKOUT_VOL_MULT", TACTICAL, "x time-of-day average",
          "Volume multiple that counts as a confirming surge.", 1.0, 5.0, "Intraday patterns"),
    Param("intraday.hammer_decline", _IS, "HAMMER_DECLINE", TACTICAL, "fraction",
          "Decline required into a 30-minute hammer for it to be a reversal.", 0.001, 0.05, "Intraday patterns"),
    Param("intraday.hammer_near_support", _IS, "HAMMER_NEAR_SUPPORT", TACTICAL, "fraction",
          "How close to support the hammer's low must sit.", 0.0005, 0.02, "Intraday patterns"),

    # ── Positional Engine: universe hygiene ─────────────────────────────────────
    Param("positional.min_price", _PS, "MIN_PRICE", LOCKED, "rupees",
          "Penny stocks are excluded: spreads swamp any edge.", 1, 500, "Positional universe"),
    Param("positional.min_median_turnover", _PS, "MIN_MEDIAN_TURNOVER", LOCKED, "rupees/day",
          "Median 20-session turnover floor -- liquidity, so a fill is realistic.", 1e6, 1e9,
          "Positional universe"),
    Param("positional.corp_action_gap", _PS, "CORP_ACTION_GAP", LOCKED, "fraction",
          "A prior-close mismatch above this means a split/bonus; bhavcopy is unadjusted so the symbol "
          "is skipped rather than read as a fake breakout.", 0.01, 0.20, "Positional universe"),
    Param("positional.min_sessions", _PS, "MIN_SESSIONS", LOCKED, "sessions",
          "History required before a symbol can be scored at all.", 20, 200, "Positional universe"),

    # ── Positional Engine: evaluation ───────────────────────────────────────────
    Param("positional.forward_sessions", _PS, "FORWARD_SESSIONS", LOCKED, "sessions",
          "Forward window the pattern scorecard measures over.", 1, 60, "Positional evaluation"),
    Param("positional.forward_hit", _PS, "FORWARD_HIT", LOCKED, "fraction",
          "Move counted as a 'hit'. Note this counts BIG moves, so it rises with volatility -- the "
          "directional test (mean return vs baseline, t-stat) is the real one.", 0.02, 0.50,
          "Positional evaluation"),
    Param("positional.forward_drawdown", _PS, "FORWARD_DRAWDOWN", LOCKED, "fraction (negative)",
          "Adverse close counted against the setup.", -0.50, -0.02, "Positional evaluation"),

    # ── Positional Engine: pattern thresholds ───────────────────────────────────
    Param("positional.hb_box_bars", _PS, "HB_BOX_BARS", TACTICAL, "sessions",
          "Horizontal-break box length.", 5, 120, "Positional patterns"),
    Param("positional.hb_max_depth", _PS, "HB_MAX_DEPTH", TACTICAL, "fraction",
          "Deeper than this is not a tight box.", 0.02, 0.40, "Positional patterns"),
    Param("positional.hb_min_touches", _PS, "HB_MIN_TOUCHES", TACTICAL, "touches",
          "Times the ceiling must be tested to count as a level.", 2, 10, "Positional patterns"),
    Param("positional.fp_pole_min_gain", _PS, "FP_POLE_MIN_GAIN", TACTICAL, "fraction",
          "Run-up required to call something a flag pole.", 0.05, 1.00, "Positional patterns"),
    Param("positional.fp_max_retrace", _PS, "FP_MAX_RETRACE", TACTICAL, "fraction",
          "Retracement above this and the pole is broken, not consolidating.", 0.10, 0.80,
          "Positional patterns"),
    Param("positional.vcp_max_final_range", _PS, "VCP_MAX_FINAL_RANGE", TACTICAL, "fraction",
          "Final contraction must be at least this tight.", 0.02, 0.30, "Positional patterns"),
    Param("positional.cup_rim_tol", _PS, "CUP_RIM_TOL", TACTICAL, "fraction",
          "How unequal the two cup rims may be.", 0.01, 0.25, "Positional patterns"),
    Param("positional.cup_min_depth", _PS, "CUP_MIN_DEPTH", TACTICAL, "fraction",
          "Shallower than this is not a cup.", 0.03, 0.30, "Positional patterns"),
    Param("positional.cup_max_depth", _PS, "CUP_MAX_DEPTH", TACTICAL, "fraction",
          "Deeper than this is a crash, not a base.", 0.20, 0.80, "Positional patterns"),
    Param("positional.breakout_vol_mult", _PS, "BREAKOUT_VOL_MULT", TACTICAL, "x 20-day average",
          "Volume multiple that confirms a daily breakout. The one feature measured as monotonic on "
          "hit-rate -- though NOT on forward return.", 1.0, 5.0, "Positional patterns"),
]

def _jsonable(v: Any) -> Any:
    if isinstance(v, dtime):
        return v.strftime("%H:%M")
    if isinstance(v, tuple):
        return list(v)
    return v


def snapshot() -> list[dict]:
    """Every parameter with its live value and any bound violation -- what the API serves."""
    out = []
    for p in PARAMS:
        try:
            value, err = p.current(), None
        except Exception as e:            # a rename or removal must be loud, not silent
            value, err = None, f"unreadable: {type(e).__name__}: {e}"
        else:
            err = p.check(value)
        out.append({
            "key": p.key, "group": p.group, "category": p.category, "unit": p.unit, "desc": p.desc,
            "module": p.module, "attr": p.attr, "value": _jsonable(value),
            "lo": p.lo, "hi": p.hi, "violation": err,
        })
    return out


def validate() -> list[str]:
    """Human-readable problems: unreadable attributes, or live values outside their bounds."""
    return [f"{r['key']} ({r['module']}.{r['attr']}): {r['violation']}"
            for r in snapshot() if r["violation"]]


def counts() -> dict:
    snap = snapshot()
    return {
        "total": len(snap),
        "tactical": sum(1 for r in snap if r["category"] == TACTICAL),
        "locked": sum(1 for r in snap if r["category"] == LOCKED),
        "measured": sum(1 for r in snap if r["category"] == MEASURED),
        "violations": sum(1 for r in snap if r["violation"]),
    }


def groups() -> list[dict]:
    """Snapshot bucketed by UI group, in declaration order."""
    order, buckets = [], {}
    for row in snapshot():
        if row["group"] not in buckets:
            order.append(row["group"])
            buckets[row["group"]] = []
        buckets[row["group"]].append(row)
    return [{"group": g, "params": buckets[g]} for g in order]


if __name__ == "__main__":
    import json
    print(json.dumps(counts(), indent=2))
    problems = validate()
    for problem in problems:
        print("  VIOLATION:", problem)
    if not problems:
        print("  all parameters readable and within bounds")
