"""
Tick Collector
──────────────
Central tick ingestion service. Receives ticks from any source
(AngelOne WebSocket live stream, or mock generator) and:

  1. Writes raw ticks to tick_data table
  2. Notifies the aggregation engine for candle building
  3. Buffers ticks in-memory for micro-feature computation
"""

from datetime import datetime
from typing import Callable, Dict, List, Optional

import pandas as pd

from database.db import upsert_candles
from utils.logger import get_logger

logger = get_logger("tick_collector")


def _sanitize_for_db(df: pd.DataFrame, cols: List[str]) -> pd.DataFrame:
    """
    Replace NaN with real None before a DB write.

    pandas silently upcasts a column to float64 the moment it contains a
    mix of a real number and Python None (e.g. one tick's bid_qty=130,
    another's bid_qty=None) — turning every None in that column into NaN.
    Confirmed live, market-hours, 2026-09-17: once mStock started
    delivering ticks alongside AngelOne (mStock's parser doesn't populate
    bid_qty/ask_qty, so on_tick()'s setdefault fills None for those), any
    buffer mixing a source that sets bid_qty with one that doesn't hit
    this on every flush — upsert_candles() then handed psycopg2 a literal
    float('nan') for a BIGINT column (bid_qty/ask_qty/oi/volume in
    tick_data's schema), which Postgres rejects as
    "NumericValueOutOfRange: bigint out of range". That failed the WHOLE
    batch (up to `buffer_size` ticks), silently, for every flush — no
    ticks were persisted at all from market open until this was fixed.
    `.astype(object)` keeps real values as Python ints/floats (not forced
    back to a uniform dtype) while letting None coexist in the same column.
    """
    return df[cols].astype(object).where(pd.notnull(df[cols]), None)


class TickCollector:
    """Collects ticks and persists them, with optional in-memory buffer."""

    def __init__(self, buffer_size: int = 500):
        self._buffer: List[Dict] = []
        self._buffer_size = buffer_size
        self._listeners: List[Callable] = []

    # ── Public API ────────────────────────────────────────────────────────────

    def on_tick(self, tick: dict):
        """
        Process a single tick. Expected keys:
          timestamp, symbol, price, volume,
          bid_price, ask_price, bid_qty, ask_qty, oi
        """
        tick.setdefault("timestamp", datetime.now())
        tick.setdefault("bid_price", None)
        tick.setdefault("ask_price", None)
        tick.setdefault("bid_qty", None)
        tick.setdefault("ask_qty", None)
        tick.setdefault("oi", None)

        self._buffer.append(tick)

        # Notify listeners (aggregation engine, micro-feature builder, etc.)
        for listener in self._listeners:
            try:
                listener(tick)
            except Exception as e:
                logger.error(f"Tick listener error: {e}")

        # Flush buffer when full
        if len(self._buffer) >= self._buffer_size:
            self.flush()

    def flush(self):
        """Persist buffered ticks to the database."""
        if not self._buffer:
            return

        df = pd.DataFrame(self._buffer)
        cols = [
            "timestamp", "symbol", "price", "volume",
            "bid_price", "ask_price", "bid_qty", "ask_qty", "oi",
        ]
        for c in cols:
            if c not in df.columns:
                df[c] = None

        df = _sanitize_for_db(df, cols)

        try:
            # ON CONFLICT DO NOTHING, not a bare append: two collector
            # processes racing on the same symbol (or a websocket resend)
            # can legitimately produce the same (timestamp, symbol) key.
            # A bare INSERT dies on the first duplicate — this crashed the
            # whole collector process on 2026-09-07 (see _kill_stalled_collector
            # in backend/app.py for the actual root cause: duplicate
            # collector processes piling up because pkill doesn't exist on
            # Windows). One skipped duplicate row is the correct outcome;
            # losing the rest of the trading day to an unhandled crash is not.
            upsert_candles(df[cols], table="tick_data")
            logger.info(f"Flushed {len(self._buffer)} ticks to database.")
        except Exception as e:
            logger.error(f"Failed to flush ticks: {e}")

        self._buffer.clear()

    def add_listener(self, callback: Callable):
        """Register a callback that receives every tick dict."""
        self._listeners.append(callback)

    def get_buffer(self) -> List[Dict]:
        """Return current in-memory buffer (for micro-feature computation)."""
        return list(self._buffer)

    def get_buffer_df(self, symbol: Optional[str] = None) -> pd.DataFrame:
        """Return buffer as DataFrame, optionally filtered by symbol."""
        if not self._buffer:
            return pd.DataFrame()
        df = pd.DataFrame(self._buffer)
        if symbol:
            df = df[df["symbol"] == symbol]
        return df

    # ── Bulk Ingest (for loading historical ticks) ────────────────────────────

    def ingest_historical_ticks(self, df: pd.DataFrame):
        """
        Write a DataFrame of historical ticks directly to the database.
        Used when backfilling historical tick data (see the known gap in
        data/market_data_adapter.py — kept for whichever source can supply it).
        """
        cols = [
            "timestamp", "symbol", "price", "volume",
            "bid_price", "ask_price", "bid_qty", "ask_qty", "oi",
        ]
        for c in cols:
            if c not in df.columns:
                df[c] = None

        df = _sanitize_for_db(df, cols)

        try:
            upsert_candles(df[cols], table="tick_data")
            logger.info(f"Ingested {len(df)} historical ticks.")
        except Exception as e:
            logger.error(f"Failed to ingest historical ticks: {e}")