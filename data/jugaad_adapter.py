"""
jugaad-data Adapter
────────────────────
Thin wrapper around the free `jugaad-data` library (NSE/BSE EOD data, no
auth, no rate-limit key). Used for:

  - EOD F&O / cash-market bhavcopy (`fetch_bhavcopy`) — backs
    `MarketDataAdapter.fetch_bhavcopy` in data/market_data_adapter.py.
  - Daily stock/index OHLC history (`fetch_stock_history`,
    `fetch_index_history`) — backs the NSE-Neuron forecasting module
    (predictions/) and the Market Scanner tab, both of which need
    multi-year *daily* bars rather than intraday minute candles.

Install: pip install jugaad-data
"""

from __future__ import annotations

from datetime import date, datetime
from io import StringIO
from typing import Optional

import pandas as pd

from utils.logger import get_logger

logger = get_logger("jugaad_adapter")


def fetch_bhavcopy(dt: date, segment: str = "FO") -> pd.DataFrame:
    """
    Fetch EOD bhavcopy for a single date without writing a file to disk.

    segment: "FO" (futures & options) or "EQ" (equity/index cash market).
    """
    try:
        from jugaad_data.nse import bhavcopy_fo_raw, bhavcopy_raw
    except ImportError:
        logger.error("jugaad-data not installed. Run: pip install jugaad-data")
        return pd.DataFrame()

    try:
        raw = bhavcopy_fo_raw(dt) if segment.upper() == "FO" else bhavcopy_raw(dt)
        df = pd.read_csv(StringIO(raw))
        df.columns = [c.strip().lower() for c in df.columns]
        return df
    except Exception as e:
        logger.warning(f"jugaad-data bhavcopy fetch failed for {dt} ({segment}): {e}")
        return pd.DataFrame()


def fetch_index_bhavcopy(dt: date) -> pd.DataFrame:
    """
    EOD bhavcopy for all published NSE indices (NIFTY 50, NIFTY BANK, sector
    indices, ...) for a single date — the free, non-bot-protected source
    the Market Scanner tab uses for a wide, ranked scan (as opposed to a
    live intraday read, which NSE has largely locked down).
    """
    try:
        from jugaad_data.nse import bhavcopy_index_raw
    except ImportError:
        logger.error("jugaad-data not installed. Run: pip install jugaad-data")
        return pd.DataFrame()

    try:
        raw = bhavcopy_index_raw(dt)
        df = pd.read_csv(StringIO(raw))
        df.columns = [c.strip().lower() for c in df.columns]
        return df
    except Exception as e:
        logger.warning(f"jugaad-data index bhavcopy fetch failed for {dt}: {e}")
        return pd.DataFrame()


def fetch_stock_history(symbol: str, from_date: date, to_date: date, series: str = "EQ") -> pd.DataFrame:
    """Daily OHLC history for an NSE equity symbol."""
    try:
        from jugaad_data.nse import stock_df
    except ImportError:
        logger.error("jugaad-data not installed. Run: pip install jugaad-data")
        return pd.DataFrame()

    try:
        df = stock_df(symbol=symbol, from_date=from_date, to_date=to_date, series=series)
        df.columns = [c.strip().upper() for c in df.columns]
        return df
    except Exception as e:
        logger.warning(f"jugaad-data stock history fetch failed for {symbol}: {e}")
        return pd.DataFrame()


def fetch_index_history(index_name: str, from_date: date, to_date: date) -> pd.DataFrame:
    """Daily OHLC history for an NSE index (e.g. 'NIFTY 50', 'NIFTY BANK')."""
    try:
        from jugaad_data.nse import index_df
    except ImportError:
        logger.error("jugaad-data not installed. Run: pip install jugaad-data")
        return pd.DataFrame()

    try:
        df = index_df(symbol=index_name, from_date=from_date, to_date=to_date)
        return df
    except Exception as e:
        logger.warning(f"jugaad-data index history fetch failed for {index_name}: {e}")
        return pd.DataFrame()


def fetch_live_quote(symbol: str) -> Optional[dict]:
    """Free live quote (NSE's own delayed feed, via jugaad-data's NSELive)."""
    try:
        from jugaad_data.nse import NSELive
    except ImportError:
        logger.error("jugaad-data not installed. Run: pip install jugaad-data")
        return None

    try:
        return NSELive().stock_quote(symbol)
    except Exception as e:
        logger.warning(f"jugaad-data live quote failed for {symbol}: {e}")
        return None
