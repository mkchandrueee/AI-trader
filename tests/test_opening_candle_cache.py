"""
The opening-candle cache in strategy/premarket.py. Run directly or with pytest.

Measured live on 2026-09-24: AngelOne refused 51% of this process's historical
calls with "exceeding access rate", and the live agent lost real decisions to it.
Six of roughly fourteen calls per 60-second cycle were re-fetching the SAME
immutable 09:15-09:20 candle. These tests pin the three properties that make
caching it safe: it is only cached once the window has closed, it never leaks
across session dates, and the cached candle is identical to the fetched one.

No broker is touched -- the fetch underneath is stubbed and counted.
"""
import os
import sys
from datetime import date, datetime, timedelta

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategy import premarket as pm


class _Counter:
    """Stands in for the AngelOne fetch, counting how often it is actually called."""

    def __init__(self):
        self.calls = 0

    def __call__(self, adapter, opt_symbol, start, end, timeframe, exchange):
        self.calls += 1
        row = {"timestamp": pd.Timestamp(start), "open": 100.0, "high": 110.0, "low": 95.0, "close": 105.0}
        return pd.DataFrame([row]), False, None


def _install(monkey_now: datetime):
    """Point premarket at a stub fetch and a fixed 'now'. Returns the call counter."""
    counter = _Counter()
    pm._fetch_candle_df = counter
    pm.clear_opening_candle_cache()

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return monkey_now

    pm.datetime = _FixedDatetime
    return counter


def _restore():
    pm.datetime = datetime


def test_closed_opening_candle_is_fetched_once_then_served_from_cache():
    today = date(2026, 9, 24)
    counter = _install(datetime.combine(today, datetime.min.time()) + timedelta(hours=10))  # 10:00, window closed
    try:
        first = pm._fetch_option_candle(None, "NIFTY29SEP2623250CE", "5min", "opening", session_date=today)
        assert counter.calls == 1
        for _ in range(9):
            again = pm._fetch_option_candle(None, "NIFTY29SEP2623250CE", "5min", "opening", session_date=today)
            assert again == first, "cached candle differs from the fetched one"
        assert counter.calls == 1, f"cache missed: {counter.calls} broker calls for one immutable candle"
    finally:
        _restore()


def test_a_still_forming_opening_candle_is_never_cached():
    """Before 09:20 the candle is still moving; caching it would freeze a partial bar."""
    today = date(2026, 9, 24)
    counter = _install(datetime.combine(today, datetime.min.time()) + timedelta(hours=9, minutes=17))
    try:
        for _ in range(3):
            pm._fetch_option_candle(None, "NIFTY29SEP2623250CE", "5min", "opening", session_date=today)
        assert counter.calls == 3, "a forming opening candle must be re-fetched every time"
    finally:
        _restore()


def test_cache_does_not_leak_across_sessions_or_symbols():
    today = date(2026, 9, 24)
    counter = _install(datetime.combine(today, datetime.min.time()) + timedelta(hours=10))
    try:
        pm._fetch_option_candle(None, "NIFTY29SEP2623250CE", "5min", "opening", session_date=today)
        pm._fetch_option_candle(None, "NIFTY29SEP2623250CE", "5min", "opening", session_date=today - timedelta(days=1))
        pm._fetch_option_candle(None, "NIFTY29SEP2623250PE", "5min", "opening", session_date=today)
        assert counter.calls == 3, "different date or symbol must not hit another key's cache entry"
        pm._fetch_option_candle(None, "NIFTY29SEP2623250CE", "5min", "opening", session_date=today)
        assert counter.calls == 3
    finally:
        _restore()


def test_latest_mode_is_never_cached():
    """Only the fixed opening candle is immutable -- 'latest' changes every bar."""
    today = date(2026, 9, 24)
    counter = _install(datetime.combine(today, datetime.min.time()) + timedelta(hours=14))
    try:
        for _ in range(3):
            pm._fetch_option_candle(None, "NIFTY29SEP2623250CE", "5min", "latest", session_date=today)
        assert counter.calls == 3, "'latest' must never be served from the opening cache"
    finally:
        _restore()


def test_clear_reports_what_it_dropped():
    today = date(2026, 9, 24)
    _install(datetime.combine(today, datetime.min.time()) + timedelta(hours=10))
    try:
        pm._fetch_option_candle(None, "NIFTY29SEP2623250CE", "5min", "opening", session_date=today)
        pm._fetch_option_candle(None, "NIFTY29SEP2623250PE", "5min", "opening", session_date=today)
        assert pm.clear_opening_candle_cache() == 2
        assert pm.clear_opening_candle_cache() == 0
    finally:
        _restore()


# ── the previous-session TTL cache (the other half of the per-cycle waste) ──────
# _current_spot() falls through to this on every call for any index with no tick
# stream, which for SENSEX means a rate-limited AngelOne call each time.

def _stub_sensex(result):
    import strategy.market_scanner as ms
    calls = {"n": 0}

    def fake(d):
        calls["n"] += 1
        return result(d) if callable(result) else result

    ms.fetch_sensex_ohlc = fake
    pm._prev_session_cache.clear()
    return calls


def test_previous_session_is_fetched_once_per_ttl_window():
    calls = _stub_sensex(lambda d: {"high": 74500.0, "low": 74000.0, "close": 74200.0, "date": d})
    for _ in range(6):
        ohlc, _d = pm._previous_session_ohlc("SENSEX")
    assert ohlc["close"] == 74200.0
    assert calls["n"] == 1, f"{calls['n']} broker calls for six reads inside one TTL window"


def test_a_failed_previous_session_read_is_not_cached():
    """'No price right now' must be retried, not remembered for the rest of the day."""
    calls = _stub_sensex(None)
    for _ in range(3):
        assert pm._previous_session_ohlc("SENSEX") == (None, None)
    assert calls["n"] == 3, "a failed read was cached"


def test_previous_session_cache_expires():
    # (TTL is forced to expire by rewriting the stamp, not by sleeping)
    calls = _stub_sensex(lambda d: {"high": 74500.0, "low": 74000.0, "close": 74200.0, "date": d})
    pm._previous_session_ohlc("SENSEX")
    stamp, value = pm._prev_session_cache["SENSEX"]
    pm._prev_session_cache["SENSEX"] = (stamp - pm._PREV_SESSION_TTL_SECS - 1, value)
    pm._previous_session_ohlc("SENSEX")
    assert calls["n"] == 2, "cache did not expire"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
    print("ALL PASSED")
