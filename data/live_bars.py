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
lazily -- a healthy mStock day never logs into AngelOne at all.
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
MIN_SPACING_SECS = 0.4  # ~2.5 req/s, inside AngelOne's ~3/s limit; polite to mStock too
_last_call = 0.0


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


def fetch_bars(inst: Instrument, start: datetime, end: datetime, interval: str = "5min") -> tuple[Optional[pd.DataFrame], Optional[str], list[str]]:
    """
    Returns (df, source, problems). df is None only if BOTH sources failed or had no
    candles; `problems` lists each source's refusal so the caller can surface it.
    """
    problems: list[str] = []
    ms = get_mstock()
    try:
        if ms.authenticate():
            _throttle()
            df = ms.fetch_historical_bars(inst.symbol, start, end, interval, exchange=inst.exchange)
            if df is not None and not df.empty:
                return _clean(df), "mstock", problems
            problems.append(f"mstock: {df.attrs.get('error') if df is not None and df.attrs.get('error') else 'no candles'}")
        else:
            problems.append("mstock: not authenticated")
    except Exception as e:  # never let one source's failure abort the other
        problems.append(f"mstock: {e}")

    angel = get_angel()
    try:
        if angel.authenticate():
            _throttle()
            df = angel.fetch_historical_bars(inst.angel, start, end, interval, exchange=inst.exchange)
            if df is not None and not df.empty:
                return _clean(df), "angelone", problems
            problems.append(f"angelone: {df.attrs.get('error') if df is not None and df.attrs.get('error') else 'no candles'}")
        else:
            problems.append("angelone: not authenticated")
    except Exception as e:
        problems.append(f"angelone: {e}")
    return None, None, problems
