"""
Pre-trade Risk Gate
───────────────────
One place that answers "may this order be placed?", with every reason, instead of
the answer being spread across modules that mostly do not run.

WHY THIS EXISTS. An audit on 2026-11-03 against the checklist in
github.com/Jakeschincariol/day-trader-skill's trader-risk skill found that this
platform DECLARES far more risk limits than it ENFORCES on the path that actually
opens positions (strategy/intraday_agent.py). At the time of writing the agent
enforced only: a tradable signal, MIN_RR, one position per symbol, the SUSPENDED
registry state, and the live-autofire backstop. Every one of these was declared
somewhere and checked nowhere on that path:

    max_trades_day        read only by scripts/tick_replay_backtest.py
    afternoon_cut         enforced in backend/app.py's suggestion path, not the agent
    skip_first_min        same
    max_premium           not enforced at all (the `max_premium` in backend/app.py
                          is peak-premium tracking for trailing, a different thing)
    max_capital_per_trade not enforced at all
    daily loss            broker/order_manager.py has one, but the paper agent
                          does not go through it
    max concurrent        same

risk/risk_manager.py already tried to be this module and is dead code -- nothing
imports it -- which is how a unit bug survived in it unnoticed (see that file).

DESIGN, borrowed deliberately from trader-risk:

  * Every check runs; the verdict carries ALL reasons, not just the first. A
    trade rejected for three reasons is a different situation from one rejected
    for a near-miss, and the log should say which.
  * The gate NEVER edits an order to make it pass, and never relaxes a limit.
  * EXITS ARE NEVER BLOCKED. A risk check that can trap you in a position is
    worse than no risk check. Only entries are gated.
  * Thresholds come from this platform's OWN config (config/risk_profiles.py,
    config/settings.py). Nothing here invents a number.
  * Pure: the book, the clock and the limits are passed in. No globals, no
    imports of the agent, so it is testable without a broker or a Flask app.

A PASS means "may be placed", not "should be placed" -- it says nothing about
whether the strategy is any good. models/strategy_registry.py answers that.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime
from typing import Any, Optional

PASS = "pass"
BLOCK = "block"
WARN = "warn"
NA = "n/a"

MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)


@dataclass(frozen=True)
class Check:
    name: str
    status: str          # pass | block | warn | n/a
    detail: str


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    checks: list[Check]

    @property
    def blocks(self) -> list[str]:
        return [f"{c.name}: {c.detail}" for c in self.checks if c.status == BLOCK]

    @property
    def warnings(self) -> list[str]:
        return [f"{c.name}: {c.detail}" for c in self.checks if c.status == WARN]

    def reason(self) -> str:
        """One line for the agent log -- every blocking reason, not just the first."""
        return "; ".join(self.blocks) if self.blocks else "all checks passed"

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "blocks": self.blocks,
            "warnings": self.warnings,
            "checks": [{"name": c.name, "status": c.status, "detail": c.detail} for c in self.checks],
        }


@dataclass(frozen=True)
class Limits:
    """Every number here is read from this platform's existing config, never invented."""
    min_rr: float
    max_premium: float            # rupees per unit, profile.max_premium
    max_trades_day: int
    max_concurrent: int
    max_loss_per_trade: float     # rupees, profile.max_capital_per_trade x capital
    max_daily_loss: float         # rupees, NEGATIVE (e.g. -5000)
    skip_first_min: int
    afternoon_cut: int            # minutes from open after which no new entries
    skip_last_min: int

    @classmethod
    def from_config(cls, profile, min_rr: float, capital: float,
                    max_concurrent: int, max_daily_loss: float) -> "Limits":
        return cls(
            min_rr=min_rr,
            max_premium=profile.max_premium,
            max_trades_day=profile.max_trades_day,
            max_concurrent=max_concurrent,
            max_loss_per_trade=profile.max_capital_per_trade * capital,
            max_daily_loss=-abs(max_daily_loss),      # stored negative whatever the caller passes
            skip_first_min=profile.skip_first_min,
            afternoon_cut=profile.afternoon_cut,
            skip_last_min=profile.skip_last_min,
        )


@dataclass
class Book:
    """Today's trading so far, as the gate needs to see it."""
    open_positions: list[dict] = field(default_factory=list)
    closed_today: list[dict] = field(default_factory=list)

    @classmethod
    def from_paper_book(cls, positions: list[dict], today: Optional[date] = None) -> "Book":
        """
        Build from backend/app.py's paper_positions_by_mode[mode] list, which holds
        both OPEN and CLOSED rows for the session.

        A row whose entry date cannot be read is counted as TODAY rather than
        skipped: under-counting today's trades would quietly loosen the
        per-day and daily-loss limits, which is the wrong way to be wrong.
        """
        today = today or date.today()
        open_, closed = [], []
        for p in positions:
            if p.get("status") == "OPEN":
                open_.append(p)
                continue
            stamp = p.get("entry_time_dt") or ""
            try:
                same_day = datetime.fromisoformat(str(stamp)).date() == today
            except (ValueError, TypeError):
                same_day = True
            if same_day:
                closed.append(p)
        return cls(open_positions=open_, closed_today=closed)

    def realised_today(self) -> float:
        return sum(float(p.get("realised_pnl") or 0) for p in self.closed_today)

    def open_risk(self) -> float:
        """
        Worst case still on the table: every open position taken to its stop.
        Counted against the daily loss limit so the limit describes the day's
        exposure, not just what has already been booked -- otherwise three open
        losers look identical to a flat book right up until they close.
        """
        total = 0.0
        for p in self.open_positions:
            entry, stop, lot = p.get("entry_premium"), p.get("sl"), p.get("lot_size")
            if entry and stop and lot:
                total += max(0.0, (float(entry) - float(stop)) * int(lot))
        return total


def evaluate(
    *,
    order: dict,
    book: Book,
    limits: Limits,
    now: datetime,
    strategy_blocked: Optional[str] = None,
    autofire_blocked: Optional[str] = None,
    is_exit: bool = False,
) -> Verdict:
    """
    `order` needs: symbol, option_symbol, entry, stop, partial, lot_size.
    `strategy_blocked` / `autofire_blocked` are the agent's existing gates, folded
    in so one verdict explains the whole decision rather than two of the reasons
    living in the agent's own log and the rest here.
    """
    checks: list[Check] = []

    # ── exits are never blocked ────────────────────────────────────────────────
    # A risk gate that can refuse to let you out is worse than no gate.
    if is_exit:
        checks.append(Check("exit", PASS, "Exits are never blocked by the risk gate."))
        return Verdict(allowed=True, checks=checks)

    entry = _num(order.get("entry"))
    stop = _num(order.get("stop"))
    partial = _num(order.get("partial"))
    lot = order.get("lot_size")
    lot = int(lot) if isinstance(lot, (int, float)) and lot else None

    # ── 1. shape ──────────────────────────────────────────────────────────────
    missing = [k for k, v in (("entry", entry), ("stop", stop), ("partial", partial), ("lot_size", lot))
               if v is None]
    if missing or not order.get("option_symbol"):
        missing = missing + (["option_symbol"] if not order.get("option_symbol") else [])
        checks.append(Check("shape", BLOCK, f"Order is missing {', '.join(missing)}."))
        # Everything below reads these fields, so stop here rather than
        # producing a cascade of confusing secondary failures.
        return Verdict(allowed=False, checks=checks)
    if entry <= 0 or lot <= 0:
        checks.append(Check("shape", BLOCK, f"entry={entry} lot_size={lot}: both must be positive."))
        return Verdict(allowed=False, checks=checks)
    checks.append(Check("shape", PASS, f"{order['option_symbol']} entry {entry} stop {stop} exit {partial} x{lot}."))

    # ── 2. stop on the correct side ───────────────────────────────────────────
    # Only long option buys exist in this platform; the stop must sit BELOW entry.
    if stop >= entry:
        checks.append(Check("stop_side", BLOCK,
                            f"stop {stop} is not below entry {entry} — this is a long option buy."))
    else:
        checks.append(Check("stop_side", PASS, f"stop {stop} is below entry {entry}."))

    risk_per_unit = entry - stop

    # ── 3. reward:risk ────────────────────────────────────────────────────────
    if risk_per_unit <= 0:
        checks.append(Check("reward_risk", BLOCK, "risk per unit is zero or negative; R:R is undefined."))
    else:
        rr = (partial - entry) / risk_per_unit
        if rr < limits.min_rr:
            checks.append(Check("reward_risk", BLOCK,
                                f"R:R {rr:.2f} on the exit actually taken is below MIN_RR {limits.min_rr:g}."))
        else:
            checks.append(Check("reward_risk", PASS, f"R:R {rr:.2f} meets MIN_RR {limits.min_rr:g}."))

    # ── 4. loss per trade ─────────────────────────────────────────────────────
    trade_risk = max(0.0, risk_per_unit) * lot
    if trade_risk > limits.max_loss_per_trade:
        # One NIFTY lot is 65 units and cannot be split, so a per-trade rupee cap
        # can be structurally unreachable rather than merely tight: at a 1% cap on
        # Rs.50,000 the budget is Rs.500, which one lot breaches on any stop wider
        # than about Rs.7.7 a unit. Say so, because "raise the cap or the capital"
        # is a different action from "take a smaller position" -- and here there is
        # no smaller position to take.
        per_unit_budget = limits.max_loss_per_trade / lot if lot else 0.0
        extra = (f" One lot is {lot} units and cannot be split, so this cap only admits a stop "
                 f"within Rs.{per_unit_budget:.1f} a unit -- raise the per-trade cap or the capital "
                 f"it is a fraction of, since no smaller position exists."
                 if per_unit_budget < risk_per_unit else "")
        checks.append(Check("max_loss_per_trade", BLOCK,
                            f"Rs.{trade_risk:,.0f} at risk exceeds the Rs.{limits.max_loss_per_trade:,.0f} "
                            f"cap for this risk profile.{extra}"))
    else:
        checks.append(Check("max_loss_per_trade", PASS,
                            f"Rs.{trade_risk:,.0f} at risk, cap Rs.{limits.max_loss_per_trade:,.0f}."))

    # ── 5. premium cap ────────────────────────────────────────────────────────
    if entry > limits.max_premium:
        checks.append(Check("max_premium", BLOCK,
                            f"premium {entry} is above the profile cap {limits.max_premium:g}."))
    else:
        checks.append(Check("max_premium", PASS, f"premium {entry}, cap {limits.max_premium:g}."))

    # ── 6. concurrent positions ───────────────────────────────────────────────
    n_open = len(book.open_positions)
    if n_open >= limits.max_concurrent:
        checks.append(Check("max_concurrent", BLOCK,
                            f"{n_open} position(s) already open, limit {limits.max_concurrent}."))
    else:
        checks.append(Check("max_concurrent", PASS, f"{n_open} open, limit {limits.max_concurrent}."))

    # ── 7. duplicate ──────────────────────────────────────────────────────────
    opt = order["option_symbol"]
    if any(p.get("symbol") == opt for p in book.open_positions):
        checks.append(Check("duplicate", BLOCK, f"{opt} is already open."))
    else:
        checks.append(Check("duplicate", PASS, f"{opt} is not already open."))

    # ── 8. trades per day ─────────────────────────────────────────────────────
    taken = len(book.closed_today) + n_open
    if taken >= limits.max_trades_day:
        checks.append(Check("max_trades_day", BLOCK,
                            f"{taken} trade(s) taken today, limit {limits.max_trades_day}."))
    else:
        checks.append(Check("max_trades_day", PASS, f"{taken} taken today, limit {limits.max_trades_day}."))

    # ── 9. daily loss ─────────────────────────────────────────────────────────
    realised = book.realised_today()
    worst_case = realised - book.open_risk() - trade_risk
    if worst_case < limits.max_daily_loss:
        checks.append(Check("max_daily_loss", BLOCK,
                            f"booked Rs.{realised:,.0f} plus Rs.{book.open_risk():,.0f} open risk plus "
                            f"Rs.{trade_risk:,.0f} on this trade reaches Rs.{worst_case:,.0f}, past the "
                            f"Rs.{limits.max_daily_loss:,.0f} daily limit."))
    else:
        checks.append(Check("max_daily_loss", PASS,
                            f"worst case today Rs.{worst_case:,.0f}, limit Rs.{limits.max_daily_loss:,.0f}."))

    # ── 10. entry window ──────────────────────────────────────────────────────
    checks.append(_entry_window(now, limits))

    # ── 11/12. the agent's existing gates, reported in the same place ─────────
    checks.append(Check("strategy_state", BLOCK, strategy_blocked) if strategy_blocked
                  else Check("strategy_state", PASS, "Strategy is not suspended."))
    checks.append(Check("live_autofire", BLOCK, autofire_blocked) if autofire_blocked
                  else Check("live_autofire", PASS, "Paper mode, or live auto-fire explicitly confirmed."))

    return Verdict(allowed=not any(c.status == BLOCK for c in checks), checks=checks)


def _entry_window(now: datetime, limits: Limits) -> Check:
    t = now.time()
    if t < MARKET_OPEN or t >= MARKET_CLOSE:
        return Check("entry_window", BLOCK, f"{t:%H:%M} is outside market hours.")
    minutes_in = (now.hour * 60 + now.minute) - (MARKET_OPEN.hour * 60 + MARKET_OPEN.minute)
    close_min = MARKET_CLOSE.hour * 60 + MARKET_CLOSE.minute
    minutes_left = close_min - (now.hour * 60 + now.minute)
    if minutes_in < limits.skip_first_min:
        return Check("entry_window", BLOCK,
                     f"{minutes_in}min after open, inside the {limits.skip_first_min}min opening skip.")
    if minutes_in > limits.afternoon_cut:
        return Check("entry_window", BLOCK,
                     f"{minutes_in}min after open, past the {limits.afternoon_cut}min afternoon cut-off.")
    if minutes_left < limits.skip_last_min:
        return Check("entry_window", BLOCK,
                     f"{minutes_left}min to close, inside the {limits.skip_last_min}min closing skip.")
    return Check("entry_window", PASS, f"{minutes_in}min after open, within the entry window.")


def _num(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f            # NaN is not a number for our purposes
