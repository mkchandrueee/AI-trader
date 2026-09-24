"""
Backtest Record Store
─────────────────────
Research in this project has so far been printed to a terminal and then lost.
That is why the same questions kept getting re-asked ("is the intraday edge real
this time?") and why REVIEW_2026-09-21.md had to re-derive numbers that had
already been measured once. This stores each research run as a row, so a result
survives the terminal it was printed in.

A record is deliberately awkward to write incompletely. Every one must state:

  * `split` -- which sessions trained the choice and which tested it. A run with
    no held-out sessions is recorded with `out_of_sample=False` and can never
    satisfy the promotion gate, no matter how good its numbers look.
  * `cost_pct` -- the round-trip cost the net figures are AFTER. A backtest that
    does not say this is not a result, it is a chart.
  * `headline` -- the single number the run is claiming, with its sign convention.

Nothing here decides anything. models/strategy_registry.py still owns state, and
promotion is still a human action -- this just means the human is looking at a
stored measurement rather than remembering one.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from utils.logger import get_logger

logger = get_logger("backtest_record")

_PATH = Path(__file__).resolve().parent / "saved" / "backtest_records.json"

MAX_RECORDS_PER_STRATEGY = 20          # keep the recent history, not every run ever


def _load() -> dict:
    if not _PATH.exists():
        return {}
    try:
        with open(_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        logger.error(f"Backtest record store at {_PATH} is unreadable; treating as empty.")
        return {}


def _save(store: dict) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_PATH, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=2)


def record(
    strategy: str,
    *,
    script: str,
    headline: str,
    headline_value: Optional[float],
    out_of_sample: bool,
    cost_pct: float,
    train_sessions: int,
    test_sessions: int,
    train_signals: int,
    test_signals: int,
    period: str,
    verdict: str,
    detail: Optional[dict] = None,
) -> dict:
    """
    Store one research run. `verdict` is the plain-language conclusion a human
    would write in a commit message -- "no configuration was profitable out of
    sample", not "PF 0.47".
    """
    entry = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "script": script,
        "headline": headline,
        "headline_value": headline_value,
        "out_of_sample": bool(out_of_sample),
        "cost_pct": cost_pct,
        "split": {
            "train_sessions": train_sessions, "test_sessions": test_sessions,
            "train_signals": train_signals, "test_signals": test_signals,
            "period": period,
        },
        "verdict": verdict,
        "detail": detail or {},
    }
    store = _load()
    rows = store.setdefault(strategy, [])
    rows.append(entry)
    del rows[:-MAX_RECORDS_PER_STRATEGY]
    _save(store)
    logger.info(f"Recorded backtest for '{strategy}': {headline}={headline_value} "
                f"(out_of_sample={out_of_sample}) -- {verdict}")
    return entry


def latest(strategy: str) -> Optional[dict]:
    rows = _load().get(strategy, [])
    return rows[-1] if rows else None


def history(strategy: str) -> list[dict]:
    return list(_load().get(strategy, []))


def all_strategies() -> list[str]:
    return sorted(_load().keys())
