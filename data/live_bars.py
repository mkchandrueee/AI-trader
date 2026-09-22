"""
Shared live-candle access for the Intraday Engine (strategy/intraday_service.py).

AngelOne only, per the user's own architecture: AngelOne powers market data,
mStock is order execution only (broker/mstock_adapter.py) -- see CLAUDE.md's
mStock section. This module used to try mStock first and fall back to
AngelOne, but that was removed 2026-09-22: mStock enforces one session per
login, and running its market-data client (data/mstock_market_data.py)
alongside order execution's own mStock session (same credentials) risked
one kicking the other. It had also turned out to add little value in
practice -- mStock served historical bars for PAST sessions fine but
returned nothing for the CURRENT session, so same-day bars (what the
Intraday Engine actually needs) came from AngelOne anyway.

ONE AngelOne session for the whole backend process, never one per call:
AngelOne's historical endpoint started answering with empty bodies after
the tick collector was respawned ~28 times before the open (2026-09-21);
strategy/premarket used to build a fresh MarketDataAdapter() and log in on
every live_confirmation() call, so it shares this singleton too.
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
ANGEL_MIN_SPACING = 1.2        # AngelOne answered ~1 req/s with "exceeding access rate" bursts on 2026-09-21 -> slower + adaptive
ANGEL_MAX_SPACING = 3.0
ANGEL_RETRY_WAIT = 3.0
_angel_last = 0.0
_angel_spacing = ANGEL_MIN_SPACING


@dataclass(frozen=True)
class Instrument:
    """`symbol` is our DB's alias; `angel_symbol` what AngelOne's master calls it, if different."""
    symbol: str
    exchange: str = "NSE"
    angel_symbol: Optional[str] = None

    @property
    def angel(self) -> str:
        return self.angel_symbol or (f"{self.symbol}-EQ" if self.exchange == "NSE" else self.symbol)


def get_angel():
    """The process-wide AngelOne MarketDataAdapter (constructed once; authenticate() caches the login)."""
    global _angel
    with _lock:
        if _angel is None:
            from data.market_data_adapter import MarketDataAdapter
            _angel = MarketDataAdapter()
        return _angel


def _angel_throttle() -> None:
    global _angel_last
    with _lock:
        wait = _angel_spacing - (time.time() - _angel_last)
        if wait > 0:
            time.sleep(wait)
        _angel_last = time.time()


def _angel_fetch_raw(angel, symbol: str, exchange: str, start: datetime, end: datetime, interval: str):
    """One AngelOne call with adaptive spacing: back off and retry once when it says 'exceeding access rate'."""
    global _angel_spacing
    for attempt in (1, 2):
        _angel_throttle()
        df = angel.fetch_historical_bars(symbol, start, end, interval, exchange=exchange)
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


def fetch_angel_bars(symbol: str, start: datetime, end: datetime, interval: str = "5min", exchange: str = "NFO") -> pd.DataFrame:
    """
    Public entry point for a SINGLE AngelOne historical-candle fetch, through the process-wide adaptive
    throttle above and the shared get_angel() session -- for ANY caller, not just fetch_bars() below.

    Added 2026-09-22: strategy/premarket.py's live agent path used to call MarketDataAdapter.fetch_historical_bars()
    directly, bypassing this throttle entirely. That was fine while mStock split the load, but once market
    data went AngelOne-only (same day), Pre Market and the Intraday Engine both hitting AngelOne's REST
    endpoint at their own uncoordinated paces tripped its "exceeding access rate" limit within minutes of
    the open, making Pre Market's live-confirmation fetches fail intermittently. Centralizing every AngelOne
    historical-candle call through this one function/throttle fixes that.

    Returns the raw (uncleaned) DataFrame, with df.attrs["error"] set on failure -- same contract as
    MarketDataAdapter.fetch_historical_bars() itself, so existing df.attrs.get("error") checks don't change.
    """
    angel = get_angel()
    if not angel.authenticate():
        df = pd.DataFrame()
        df.attrs["error"] = "not authenticated"
        return df
    return _angel_fetch_raw(angel, symbol, exchange, start, end, interval)


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


def fetch_bars(inst: Instrument, start: datetime, end: datetime, interval: str = "5min"
               ) -> tuple[Optional[pd.DataFrame], Optional[str], list[str]]:
    """
    Returns (df, source, problems). df is None if AngelOne failed or had no candles for this window;
    `problems` lists the refusal so the caller can surface it. `source` is always "angelone" on success
    -- kept in the return shape so callers (strategy/intraday_service.py) don't need to change if a second
    source is ever added back.
    """
    problems: list[str] = []
    try:
        df = fetch_angel_bars(inst.angel, start, end, interval, inst.exchange)
        if df is not None and not df.empty:
            return _clean(df), "angelone", problems
        problems.append(f"angelone: {df.attrs.get('error') if df is not None and df.attrs.get('error') else 'no candles'}")
    except Exception as e:
        problems.append(f"angelone: {e}")
    return None, None, problems
