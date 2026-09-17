"""
Strategy Certification Registry
──────────────────────────────────
A lightweight (3-state, not the platform spec's full 6-level ladder)
version of "a strategy carries a lifecycle state that controls what the
platform is permitted to do with it" (spec §59). Deliberately small:

  PAPER_TESTING          — default for every strategy. Signals fire in
                           paper mode; never eligible for live execution.
  LIVE_ASSISTED_ELIGIBLE — a human deliberately promoted this strategy
                           after reviewing its Performance Evidence Bundle
                           (models/evidence_bundle.py). Promotion is NEVER
                           automatic — see check_health() below.
  SUSPENDED              — automatically demoted on a health-check
                           failure (see check_health()). Blocks ALL new
                           signals from this strategy, including paper —
                           if a strategy is measurably losing money over a
                           real sample, letting it keep firing paper
                           trades doesn't teach anything new, and leaving
                           it live-eligible risks it getting promoted (or
                           re-promoted) on stale, better-looking evidence.

Backed by a small JSON file rather than a DB table — this project doesn't
need more than a few dozen strategy+version rows, and a file is
trivially inspectable/editable by a human, which matters since promotion
is meant to be a deliberate, reviewable action.

State changes are logged (with a reason) rather than silently overwritten,
since "why is this strategy suspended" is exactly the question a human
will ask when they notice their agent stopped firing.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from utils.logger import get_logger

logger = get_logger("strategy_registry")

PAPER_TESTING = "PAPER_TESTING"
LIVE_ASSISTED_ELIGIBLE = "LIVE_ASSISTED_ELIGIBLE"
SUSPENDED = "SUSPENDED"

_VALID_STATES = (PAPER_TESTING, LIVE_ASSISTED_ELIGIBLE, SUSPENDED)

_REGISTRY_PATH = Path(__file__).resolve().parent / "saved" / "strategy_registry.json"

# A demotion needs real evidence behind it — same threshold
# models/evidence_bundle.py already uses to decide whether a win rate is
# even worth showing. Below this, health checks leave the state alone.
MIN_SAMPLE_FOR_HEALTH_CHECK = 20

# A strategy loses money below profit_factor 1.0 by definition (gross
# losses exceed gross gains) — the simplest, most defensible automatic
# demotion trigger available from the evidence bundle alone. More
# sophisticated triggers (live-vs-backtest divergence, drawdown-vs-
# backtest-max breach — spec §63) are a later refinement once there's a
# backtest baseline recorded per strategy version to compare against;
# this is the honest MVP version.
MIN_PROFIT_FACTOR = 1.0


def _load() -> dict:
    if not _REGISTRY_PATH.exists():
        return {}
    try:
        with open(_REGISTRY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        logger.error(f"Strategy registry at {_REGISTRY_PATH} is unreadable; treating as empty.")
        return {}


def _save(registry: dict):
    _REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_REGISTRY_PATH, "w", encoding="utf-8") as f:
        json.dump(registry, f, indent=2)


def get_state(strategy_name: str) -> str:
    """Every strategy defaults to PAPER_TESTING — the most restrictive
    state — until a human or a health check says otherwise."""
    return _load().get(strategy_name, {}).get("state", PAPER_TESTING)


def get_entry(strategy_name: str) -> dict:
    return _load().get(strategy_name, {"state": PAPER_TESTING, "history": []})


def _set_state(strategy_name: str, new_state: str, reason: str, actor: str):
    if new_state not in _VALID_STATES:
        raise ValueError(f"Unknown strategy state: {new_state!r}")
    registry = _load()
    entry = registry.setdefault(strategy_name, {"state": PAPER_TESTING, "history": []})
    old_state = entry.get("state", PAPER_TESTING)
    entry["state"] = new_state
    entry["updated_at"] = datetime.now().isoformat()
    entry.setdefault("history", []).append({
        "from": old_state,
        "to": new_state,
        "reason": reason,
        "actor": actor,
        "at": entry["updated_at"],
    })
    _save(registry)
    logger.info(f"Strategy '{strategy_name}': {old_state} -> {new_state} ({actor}): {reason}")
    return entry


def promote(strategy_name: str, reason: str) -> dict:
    """
    A human deliberately promoting a strategy to LIVE_ASSISTED_ELIGIBLE
    after reviewing its Performance Evidence Bundle. Never called
    automatically — see module docstring.
    """
    return _set_state(strategy_name, LIVE_ASSISTED_ELIGIBLE, reason, actor="human")


def suspend(strategy_name: str, reason: str, actor: str = "human") -> dict:
    return _set_state(strategy_name, SUSPENDED, reason, actor=actor)


def reset_to_paper_testing(strategy_name: str, reason: str) -> dict:
    """
    Manually reinstating a SUSPENDED strategy back to PAPER_TESTING (not
    directly back to LIVE_ASSISTED_ELIGIBLE — that still requires a fresh
    promote() with a fresh evidence review, matching the spec's "promotion
    always requires deliberate approval" rule even for a strategy that was
    previously approved).
    """
    return _set_state(strategy_name, PAPER_TESTING, reason, actor="human")


def check_health(strategy_name: str, evidence_bundle: dict) -> Optional[dict]:
    """
    Automatic demotion check — the only state transition this module ever
    makes without a human calling promote()/suspend() directly. Returns
    the updated registry entry if a demotion happened, None otherwise
    (including "not enough evidence to judge" and "already SUSPENDED").

    MVP trigger: profit_factor < MIN_PROFIT_FACTOR (losing money) over a
    real sample (n >= MIN_SAMPLE_FOR_HEALTH_CHECK). Intentionally simple —
    see MIN_PROFIT_FACTOR's docstring for what a fuller version would add.
    """
    if not evidence_bundle.get("sufficient_sample"):
        return None
    if evidence_bundle.get("n", 0) < MIN_SAMPLE_FOR_HEALTH_CHECK:
        return None

    current_state = get_state(strategy_name)
    if current_state == SUSPENDED:
        return None  # already suspended, nothing to do

    profit_factor = evidence_bundle.get("profit_factor")
    if profit_factor is not None and profit_factor < MIN_PROFIT_FACTOR:
        reason = (
            f"profit_factor={profit_factor:.2f} < {MIN_PROFIT_FACTOR} over n={evidence_bundle['n']} "
            f"trades (win_rate={evidence_bundle.get('win_rate', 0)*100:.1f}%, "
            f"expectancy={evidence_bundle.get('expectancy_pct', 0)*100:+.2f}%/trade) — losing money"
        )
        return _set_state(strategy_name, SUSPENDED, reason, actor="health_check")

    return None
