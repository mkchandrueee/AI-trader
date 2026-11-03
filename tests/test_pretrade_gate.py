"""
The pre-trade risk gate (risk/pretrade.py). Run directly or with pytest.

This gate is the only thing standing between a signal and an opened position, so
these tests pin the rules that matter: every limit blocks when it should, the
verdict reports ALL failing reasons rather than the first, exits are never
blocked, and a gate that errors blocks rather than waving the trade through.

Thresholds come from config/risk_profiles.py and config/settings.py; the tests
build their own Limits so they pin behaviour, not whatever the profile happens
to say this week.
"""
import os
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from risk import pretrade  # noqa: E402
from risk.pretrade import BLOCK, PASS, Book, Check, Limits, Verdict  # noqa: E402

LIMITS = Limits(
    min_rr=1.0, max_premium=250.0, max_trades_day=5, max_concurrent=1,
    max_loss_per_trade=2000.0, max_daily_loss=-2500.0,
    skip_first_min=15, afternoon_cut=210, skip_last_min=20,
)

NOON = datetime(2026, 11, 3, 11, 30)          # 135min after open: inside the window


def order(entry=100.0, stop=90.0, partial=115.0, lot=65, sym="NIFTY26110423250CE"):
    return {"symbol": "NIFTY", "option_symbol": sym,
            "entry": entry, "stop": stop, "partial": partial, "lot_size": lot}


def verdict(o=None, book=None, limits=LIMITS, now=NOON, **kw) -> Verdict:
    return pretrade.evaluate(order=o or order(), book=book or Book(), limits=limits, now=now, **kw)


def names_blocking(v: Verdict):
    return {c.name for c in v.checks if c.status == BLOCK}


# ── the happy path ────────────────────────────────────────────────────────────

def test_a_clean_order_passes_every_check():
    v = verdict()
    assert v.allowed, v.blocks
    assert not names_blocking(v)
    assert v.reason() == "all checks passed"


# ── exits ─────────────────────────────────────────────────────────────────────

def test_an_exit_is_never_blocked():
    """A gate that can trap you in a position is worse than no gate."""
    v = verdict(o=order(entry=9999.0, stop=10000.0, partial=0.0, lot=99999),
                book=Book(open_positions=[{"symbol": "X"}] * 50), is_exit=True)
    assert v.allowed


# ── individual limits ─────────────────────────────────────────────────────────

def test_stop_above_entry_is_blocked():
    v = verdict(o=order(entry=100.0, stop=110.0))
    assert not v.allowed and "stop_side" in names_blocking(v)


def test_reward_risk_below_min_rr_is_blocked():
    # entry 100, stop 90 -> risk 10; exit at 105 -> R:R 0.5
    v = verdict(o=order(partial=105.0))
    assert not v.allowed and "reward_risk" in names_blocking(v)


def test_loss_per_trade_above_cap_is_blocked():
    # risk 10/unit x 65 = Rs.650 -> fine; 40/unit x 65 = Rs.2600 -> over the 2000 cap
    assert verdict(o=order(stop=90.0)).allowed
    v = verdict(o=order(entry=100.0, stop=60.0, partial=145.0))
    assert not v.allowed and "max_loss_per_trade" in names_blocking(v)


def test_premium_above_the_profile_cap_is_blocked():
    v = verdict(o=order(entry=300.0, stop=270.0, partial=345.0))
    assert "max_premium" in names_blocking(v)


def test_an_open_position_blocks_a_second_one():
    book = Book(open_positions=[{"symbol": "NIFTY26110423200PE", "entry_premium": 50.0,
                                 "sl": 45.0, "lot_size": 65}])
    v = verdict(book=book)
    assert not v.allowed and "max_concurrent" in names_blocking(v)


def test_the_same_option_already_open_is_a_duplicate():
    sym = "NIFTY26110423250CE"
    book = Book(open_positions=[{"symbol": sym, "entry_premium": 50.0, "sl": 45.0, "lot_size": 65}])
    v = verdict(o=order(sym=sym), book=book)
    assert "duplicate" in names_blocking(v)


def test_trades_per_day_limit_blocks():
    book = Book(closed_today=[{"realised_pnl": 10.0} for _ in range(5)])
    v = verdict(book=book)
    assert not v.allowed and "max_trades_day" in names_blocking(v)


def test_daily_loss_limit_counts_open_risk_too():
    """
    Three open losers must not look like a flat book. The limit is measured on
    what the day could still cost, not only on what has been booked.
    """
    roomy = Limits(**{**LIMITS.__dict__, "max_concurrent": 5})
    # -1000 booked plus this trade's own Rs.650 risk = -1650, inside the -2500 limit
    v = verdict(book=Book(closed_today=[{"realised_pnl": -1000.0}]), limits=roomy)
    assert "max_daily_loss" not in names_blocking(v)

    # -2000 booked plus the same Rs.650 = -2650, past it
    v = verdict(book=Book(closed_today=[{"realised_pnl": -2000.0}]), limits=roomy)
    assert "max_daily_loss" in names_blocking(v)


def test_open_risk_is_included_in_the_daily_loss_calculation():
    book = Book(open_positions=[{"symbol": "OTHER", "entry_premium": 100.0, "sl": 70.0, "lot_size": 65}])
    # open risk = 30 x 65 = Rs.1950, plus this trade's Rs.650 = Rs.2600 > Rs.2500
    v = verdict(book=book, limits=Limits(**{**LIMITS.__dict__, "max_concurrent": 5}))
    assert "max_daily_loss" in names_blocking(v)


# ── entry window ──────────────────────────────────────────────────────────────

def test_entry_before_the_opening_skip_is_blocked():
    v = verdict(now=datetime(2026, 11, 3, 9, 20))        # 5min after open, skip is 15
    assert "entry_window" in names_blocking(v)


def test_entry_after_the_afternoon_cut_is_blocked():
    v = verdict(now=datetime(2026, 11, 3, 13, 30))       # 255min after open, cut at 210
    assert "entry_window" in names_blocking(v)


def test_entry_inside_the_closing_skip_is_blocked():
    v = verdict(now=datetime(2026, 11, 3, 15, 20))       # 10min to close, skip is 20
    assert "entry_window" in names_blocking(v)


def test_entry_outside_market_hours_is_blocked():
    assert "entry_window" in names_blocking(verdict(now=datetime(2026, 11, 3, 21, 0)))


# ── folded-in agent gates ─────────────────────────────────────────────────────

def test_a_suspended_strategy_blocks():
    v = verdict(strategy_blocked="strategy is SUSPENDED")
    assert not v.allowed and "strategy_state" in names_blocking(v)


def test_the_live_autofire_backstop_blocks():
    v = verdict(autofire_blocked="TRADE_MODE='mstock' with no approval gate")
    assert not v.allowed and "live_autofire" in names_blocking(v)


# ── reporting ─────────────────────────────────────────────────────────────────

def test_every_failing_reason_is_reported_not_just_the_first():
    """'Blocked for three reasons' and 'missed by a hair' must not look alike."""
    v = verdict(o=order(entry=300.0, stop=310.0, partial=305.0))
    assert len(names_blocking(v)) >= 3, v.blocks
    assert len(v.blocks) == len(names_blocking(v))
    assert "; " in v.reason()


def test_a_malformed_order_is_blocked_without_a_cascade():
    v = verdict(o={"symbol": "NIFTY", "option_symbol": None, "entry": None,
                   "stop": None, "partial": None, "lot_size": None})
    assert not v.allowed
    assert names_blocking(v) == {"shape"}, "a shapeless order should not produce secondary failures"


def test_verdict_is_json_serialisable():
    import json
    json.dumps(verdict().to_dict())


# ── Book construction ─────────────────────────────────────────────────────────

def test_book_separates_open_from_closed_and_ignores_other_days():
    today, yesterday = date(2026, 11, 3), date(2026, 11, 2)
    rows = [
        {"status": "OPEN", "symbol": "A"},
        {"status": "CLOSED", "realised_pnl": -100.0, "entry_time_dt": f"{today}T10:00:00"},
        {"status": "CLOSED", "realised_pnl": -900.0, "entry_time_dt": f"{yesterday}T10:00:00"},
    ]
    b = Book.from_paper_book(rows, today=today)
    assert len(b.open_positions) == 1
    assert len(b.closed_today) == 1
    assert b.realised_today() == -100.0


def test_an_unreadable_entry_date_counts_as_today():
    """Under-counting today's trades would quietly loosen the limits."""
    b = Book.from_paper_book([{"status": "CLOSED", "realised_pnl": -50.0, "entry_time_dt": "garbage"}],
                             today=date(2026, 11, 3))
    assert len(b.closed_today) == 1


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
    print("ALL PASSED")
