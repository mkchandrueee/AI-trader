"""
Strategy Lab assembler
──────────────────────
Serves the five stages a strategy passes through before it is allowed anywhere
near real money, for each strategy the platform runs:

  1 SPEC       what it claims to do, in words, including what it does NOT do
  2 PARAMS     its tunables from config/strategy_params.py, with bounds
  3 BACKTEST   the stored research run from models/backtest_record.py
  4 EVIDENCE   the live paper record from models/evidence_bundle.py
  5 REGISTRY   its lifecycle state from models/strategy_registry.py

The point is that stages 3-5 already existed and were invisible: the registry was
a JSON file nobody opened, the evidence bundle was only built transiently inside
an approval request, and research results were printed and lost. A strategy
sitting in PAPER_TESTING looked identical to one nobody had ever evaluated.

The gate (`_gate`) is descriptive, not an actuator -- it reports which conditions
a human reviewer would need satisfied before calling
strategy_registry.promote(). It never promotes anything. The last condition is
always outstanding by design: promotion is a deliberate human act, per
models/strategy_registry.py's own contract.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from config import strategy_params as sp
from models import backtest_record, strategy_registry as reg
from models.evidence_bundle import build_evidence_bundle, load_all_closed_trades

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# The live paper agent's registry key, kept identical to strategy/intraday_agent.py's
# _strategy_key() -- imported rather than retyped so a rename cannot desync them.
def _agent_key() -> str:
    from strategy.intraday_agent import AGENT_NAME, MODEL_NAME
    return f"{MODEL_NAME} ({AGENT_NAME})"


# ── Specs ────────────────────────────────────────────────────────────────────
# `trades` is the honest answer to "does this thing place orders?" -- two of the
# three do not, and conflating a research scanner with a live agent is exactly
# the confusion this page exists to stop.
_SPECS = [
    {
        "id": "math_decision_engine",
        "title": "Option Engine (live paper agent)",
        "trades": True,
        "instrument": "NIFTY weekly ATM CE/PE",
        "what": "Scores the two ATM legs on 5-minute candle quality, takes the stronger side on a "
                "breakout above that candle's high plus a buffer, exits the whole lot at a fixed "
                "premium gain or at the candle low.",
        "not": "predict direction from any model -- the ML macro/micro models are not in this path, "
               "and it never holds overnight.",
        "param_prefixes": ("engine.", "agent."),
        "cost_keys": ("costs.option_spread_round_trip",),
        "registry_key_fn": _agent_key,
        "backtest_key": "math_decision_engine",
        "evidence_key_fn": _agent_key,
    },
    {
        "id": "intraday_scanner",
        "title": "Intraday Engine (research only)",
        "trades": False,
        "instrument": "NSE equities, 5-minute bars",
        "what": "Detects opening-range breaks, VWAP reclaims, previous-day level breaks, box breaks "
                "and 30-minute hammers as-of each bar, and replays what happened next.",
        "not": "place orders or feed any live decision. Its output is a scorecard, not a signal.",
        "param_prefixes": ("intraday.",),
        "cost_keys": ("intraday.cost_pct", "intraday.slippage_pct"),
        "registry_key_fn": lambda: "intraday_scanner",
        "backtest_key": "intraday_scanner",
        "evidence_key_fn": lambda: "intraday_scanner",
    },
    {
        "id": "positional_scanner",
        "title": "Positional Engine (research only)",
        "trades": False,
        "instrument": "NSE equities, daily bhavcopy",
        "what": "Screens the EOD board for horizontal bases, flags, VCP contractions, cups and "
                "triangles, with a forward scorecard over the following sessions.",
        "not": "place orders. Bhavcopy is unadjusted, so symbols with a split or bonus in the window "
               "are skipped rather than read as breakouts.",
        "param_prefixes": ("positional.",),
        "cost_keys": ("costs.equity_slippage_assumed",),
        "registry_key_fn": lambda: "positional_scanner",
        "backtest_key": "positional_scanner",
        "evidence_key_fn": lambda: "positional_scanner",
    },
]


def _params_for(prefixes: tuple[str, ...]) -> list[dict]:
    return [r for r in sp.snapshot() if r["key"].startswith(prefixes)]


def _costs_for(keys: tuple[str, ...]) -> list[dict]:
    """The cost parameters this strategy's P&L is net of. Shown separately from its
    own tunables because a reader needs to know whether a result is after a measured
    cost or after an assumed one before the result means anything."""
    wanted = set(keys)
    return [r for r in sp.snapshot() if r["key"] in wanted]


def _evidence_for(key: str) -> dict:
    try:
        by_strategy = load_all_closed_trades(_PROJECT_ROOT / "paper_trades")
        return build_evidence_bundle(by_strategy.get(key, []), basis="paper")
    except Exception as e:                       # a broken evidence read must not blank the page
        return {"basis": "paper", "n": 0, "sufficient_sample": False,
                "error": f"{type(e).__name__}: {e}"}


def _gate(spec: dict, bt: Optional[dict], ev: dict, state: str) -> list[dict]:
    """
    The conditions between a strategy and LIVE_ASSISTED_ELIGIBLE, each with the
    measurement that settles it. `status` is pass | fail | pending, where pending
    means "not measured yet" -- distinct from fail, because "we have not looked"
    and "we looked and it lost money" are different situations.
    """
    checks: list[dict] = []

    # 1. a stored, out-of-sample backtest
    if bt is None:
        checks.append({"label": "Out-of-sample backtest on record", "status": "pending",
                       "detail": "No run has been recorded for this strategy yet."})
    elif not bt.get("out_of_sample"):
        checks.append({"label": "Out-of-sample backtest on record", "status": "fail",
                       "detail": "The recorded run has no held-out sessions, so its result is in-sample only."})
    else:
        s = bt["split"]
        checks.append({"label": "Out-of-sample backtest on record", "status": "pass",
                       "detail": f"{s['test_sessions']} held-out sessions, {s['test_signals']} test signals "
                                 f"({s['period']})."})

    # 2. that backtest has to be positive after costs
    if bt is None:
        checks.append({"label": "Backtest profitable after costs", "status": "pending",
                       "detail": "Nothing measured yet."})
    else:
        v = bt.get("headline_value")
        if v is None:
            checks.append({"label": "Backtest profitable after costs", "status": "pending",
                           "detail": bt.get("verdict", "No headline figure in the recorded run.")})
        elif v > 0:
            checks.append({"label": "Backtest profitable after costs", "status": "pass",
                           "detail": f"{bt['headline']}: {v:+.3f}%, after {100 * bt['cost_pct']:.3f}% round-trip costs."})
        else:
            checks.append({"label": "Backtest profitable after costs", "status": "fail",
                           "detail": f"{bt['headline']}: {v:+.3f}%, against {100 * bt['cost_pct']:.3f}% "
                                     f"round-trip costs. {bt.get('verdict', '')}".strip()})

    # 3+4. a real live paper sample, and it has to make money
    n = ev.get("n", 0)
    if n >= reg.MIN_SAMPLE_FOR_HEALTH_CHECK:
        checks.append({"label": f"At least {reg.MIN_SAMPLE_FOR_HEALTH_CHECK} closed paper trades",
                       "status": "pass", "detail": f"{n} closed trades on record."})
    else:
        checks.append({"label": f"At least {reg.MIN_SAMPLE_FOR_HEALTH_CHECK} closed paper trades",
                       "status": "pending",
                       "detail": f"{n} closed so far -- {reg.MIN_SAMPLE_FOR_HEALTH_CHECK - n} more needed "
                                 f"before a win rate means anything."
                                 if spec["trades"] else "This strategy places no orders, so it has no paper record."})

    pf = ev.get("profit_factor")
    if n < reg.MIN_SAMPLE_FOR_HEALTH_CHECK or pf is None:
        checks.append({"label": f"Paper profit factor at or above {reg.MIN_PROFIT_FACTOR}", "status": "pending",
                       "detail": "Not enough trades to compute a meaningful profit factor."})
    elif pf >= reg.MIN_PROFIT_FACTOR:
        checks.append({"label": f"Paper profit factor at or above {reg.MIN_PROFIT_FACTOR}", "status": "pass",
                       "detail": f"profit factor {pf:.2f} over {n} trades."})
    else:
        checks.append({"label": f"Paper profit factor at or above {reg.MIN_PROFIT_FACTOR}", "status": "fail",
                       "detail": f"profit factor {pf:.2f} over {n} trades -- below 1.0 means gross losses exceed "
                                 f"gross gains. This is also the automatic suspension trigger."})

    # 5. not currently suspended
    if state == reg.SUSPENDED:
        checks.append({"label": "Not suspended", "status": "fail",
                       "detail": "A suspended strategy must be reset to PAPER_TESTING and collect fresh evidence "
                                 "before it can be considered again."})
    else:
        checks.append({"label": "Not suspended", "status": "pass", "detail": f"Currently {state}."})

    # 6. never automatic
    if state == reg.LIVE_ASSISTED_ELIGIBLE:
        checks.append({"label": "Deliberate human promotion", "status": "pass",
                       "detail": "A human called promote() after reviewing the evidence."})
    else:
        checks.append({"label": "Deliberate human promotion", "status": "pending",
                       "detail": "Promotion is never automatic. Even with every check above passed, a human has to "
                                 "call strategy_registry.promote() with a written reason."})
    return checks


def pipeline() -> list[dict]:
    """Everything the Strategy Lab page renders."""
    out = []
    for spec in _SPECS:
        registry_key = spec["registry_key_fn"]()
        state = reg.get_state(registry_key)
        entry = reg.get_entry(registry_key)
        bt = backtest_record.latest(spec["backtest_key"])
        ev = _evidence_for(spec["evidence_key_fn"]())
        checks = _gate(spec, bt, ev, state)
        out.append({
            "id": spec["id"],
            "title": spec["title"],
            "trades": spec["trades"],
            "instrument": spec["instrument"],
            "what": spec["what"],
            "not": spec["not"],
            "params": _params_for(spec["param_prefixes"]),
            "costs": _costs_for(spec.get("cost_keys", ())),
            "backtest": bt,
            "backtest_history": backtest_record.history(spec["backtest_key"]),
            "evidence": ev,
            "registry": {
                "key": registry_key,
                "state": state,
                "updated_at": entry.get("updated_at"),
                "history": entry.get("history", [])[-6:],
            },
            "gate": checks,
            "gate_summary": {
                "passed": sum(1 for c in checks if c["status"] == "pass"),
                "failed": sum(1 for c in checks if c["status"] == "fail"),
                "pending": sum(1 for c in checks if c["status"] == "pending"),
                "total": len(checks),
            },
        })
    return out


def params_view() -> dict:
    """The parameter schema on its own, grouped, with the category legend."""
    return {
        "counts": sp.counts(),
        "groups": sp.groups(),
        "violations": sp.validate(),
        "legend": {
            sp.TACTICAL: "Defines when a setup fires. Tunable, but only against sessions the choice never saw.",
            sp.LOCKED: "A risk control or a structural fact. Changing it changes what the strategy is; never auto-tuned.",
            sp.MEASURED: "Comes from measurement, not preference. Costs and slippage belong here.",
        },
    }
