"""
Shared live-candle access for the Intraday Engine (strategy/intraday_service.py).

One mStock session and ONE AngelOne session for the whole backend process,
never one per call:
  * mStock enforces a single session per login, so a second MStockMarketData()
    logging in can kick out scripts/collect_ticks.py's own WebSocket session
    (documented in strategy/premarket._get_mstock_client). The mStock client is
    therefore that same singleton.
  * AngelOne's historical endpoint started answering with empty bodies after
    the tick collector was respawned ~28 times before the open (2026-09-21);
    strategy/premarket used to build a fresh MarketDataAdapter() and log in
    on every live_confirmation() call, so it shares this AngelOne singleton too.

Fetch order is mStock first, AngelOne only if mStock fails or returns nothing
(same direction the rest of the app follows), and AngelOne is authenticated
lazily.

Observed 2026-09-21 (live market): mStock served 14 sessions of history for all 51
symbols in ~20s but returned NO bars for the current session, so today's bars
come from AngelOne, whose historical endpoint rate-limits hard (~1 req/s already
draws "exceeding access rate" bursts). `fetch_bars(today_only=True)` therefore
stops asking mStock after a few same-day misses (re-probing every 20 min) and
`_angel_fetch` spaces AngelOne calls adaptively with one backoff-and-retry.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import pandas as pd

from utils.logger import get_logger

logger = get_logger("live_bars")

_lock = threading.Lock()
_angel = None
MIN_SPACING_SECS = 0.4         # mStock: polite spacing
ANGEL_MIN_SPACING = 1.2        # AngelOne answered ~1 req/s with "exceeding access rate" bursts on 2026-09-21 -> slower + adaptive
ANGEL_MAX_SPACING = 3.0
ANGEL_RETRY_WAIT = 3.0
_last_call = 0.0
_angel_last = 0.0
_angel_spacing = ANGEL_MIN_SPACING

# mStock's historical endpoint returned the last 14 sessions but NO bars for the current session (2026-09-21, live
# market). After a few same-day misses in a row, stop asking it for today's bars for a while (each miss also costs a
# throttled call) and probe again later in case that changes.
MSTOCK_TODAY_MISSES_TO_SKIP = 3
MSTOCK_SKIP_SECS = 20 * 60
_mstock_today_misses = 0
_mstock_skip_until = 0.0


@dataclass(frozen=True)
class Instrument:
    """`symbol` is what mStock/our DB call it; `angel_symbol` what AngelOne's master calls it."""
    symbol: str
    exchange: str = "NSE"
    angel_symbol: Optional[str] = None

    @property
    def angel(self) -> str:
        return self.angel_symbol or (f"{self.symbol}-EQ" if self.exchange == "NSE" else self.symbol)


def get_mstock():
    from strategy.premarket import _get_mstock_client
    return _get_mstock_client()


def get_angel():
    """The process-wide AngelOne MarketDataAdapter (constructed once; authenticate() caches the login)."""
    global _angel
    with _lock:
        if _angel is None:
            from data.market_data_adapter import MarketDataAdapter
            _angel = MarketDataAdapter()
        return _angel


def _throttle() -> None:
    global _last_call
    with _lock:
        wait = MIN_SPACING_SECS - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()


def _angel_throttle() -> None:
    global _angel_last
    with _lock:
        wait = _angel_spacing - (time.time() - _angel_last)
        if wait > 0:
            time.sleep(wait)
        _angel_last = time.time()


def _angel_fetch(angel, inst: "Instrument", start: datetime, end: datetime, interval: str):
    """One AngelOne call with adaptive spacing: back off and retry once when it says 'exceeding access rate'."""
    global _angel_spacing
    for attempt in (1, 2):
        _angel_throttle()
        df = angel.fetch_historical_bars(inst.angel, start, end, interval, exchange=inst.exchange)
        err = df.attrs.get("error") if df is not None else None
        if err and "exceeding access rate" in str(err).lower():
            _angel_spacing = min(ANGEL_MAX_SPACING, _angel_spacing * 1.5)
            if attempt == 1:
                time.sleep(ANGEL_RETRY_WAIT)
                continue
        elif df is not None and not df.empty:
            _angel_spacing = max(ANGEL_MIN_SPACING, _angel_spacing * 0.95)   # recover slowly after clean calls
        return df
    return df


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    """Naive-IST timestamps, numeric OHLCV, sorted, de-duplicated."""
    ts = pd.to_datetime(df["timestamp"])
    if getattr(ts.dt, "tz", None) is not None:
        ts = ts.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    out = pd.DataFrame({"timestamp": ts})
    for c in ("open", "high", "low", "close", "volume"):
        out[c] = pd.to_numeric(df[c], errors="coerce")
    out = out.dropna(subset=["open", "high", "low", "close"])
    out["volume"] = out["volume"].fillna(0.0)
    return out.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)


def fetch_bars(inst: Instrument, start: datetime, end: datetime, interval: str = "5min",
               today_only: bool = False) -> tuple[Optional[pd.DataFrame], Optional[str], list[str]]:
    """
    Returns (df, source, problems). df is None only if BOTH sources failed or had no
    candles; `problems` lists each source's refusal so the caller can surface it.
    `today_only` marks a same-session refresh: it lets mStock be skipped while it is known not to serve
    the current session (see MSTOCK_TODAY_MISSES_TO_SKIP).
    """
    global _mstock_today_misses, _mstock_skip_until
    problems: list[str] = []
    skip_ms = today_only and time.time() < _mstock_skip_until
    if skip_ms:
        problems.append("mstock: skipped (no same-day bars recently)")
    else:
        ms = get_mstock()
        try:
            if ms.authenticate():
                _throttle()
                df = ms.fetch_historical_bars(inst.symbol, start, end, interval, exchange=inst.exchange)
                if df is not None and not df.empty:
                    if today_only:
                        _mstock_today_misses = 0
                        _mstock_skip_until = 0.0
                    return _clean(df), "mstock", problems
                problems.append(f"mstock: {df.attrs.get('error') if df is not None and df.attrs.get('error') else 'no candles'}")
                if today_only:
                    _mstock_today_misses += 1
                    if _mstock_today_misses >= MSTOCK_TODAY_MISSES_TO_SKIP:
                        _mstock_skip_until = time.time() + MSTOCK_SKIP_SECS
                        logger.warning(f"mStock returned no same-day bars {_mstock_today_misses}x in a row -- using AngelOne for "
                                       f"today's bars for {MSTOCK_SKIP_SECS // 60} min, then probing mStock again")
            else:
                problems.append("mstock: not authenticated")
        except Exception as e:  # never let one source's failure abort the other
            problems.append(f"mstock: {e}")

    angel = get_angel()
    try:
        if angel.authenticate():
            df = _angel_fetch(angel, inst, start, end, interval)
            if df is not None and not df.empty:
                return _clean(df), "angelone", problems
            problems.append(f"angelone: {df.attrs.get('error') if df is not None and df.attrs.get('error') else 'no candles'}")
        else:
            problems.append("angelone: not authenticated")
    except Exception as e:
        problems.append(f"angelone: {e}")
    return None, None, problems
