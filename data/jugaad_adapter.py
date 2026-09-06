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


def _fetch_fo_bhavcopy_udiff(dt: date) -> pd.DataFrame:
    """
    jugaad-data's own bhavcopy_fo_raw() hits an old NSE zip endpoint that
    404s (confirmed: BadZipFile, the "zip" is actually an HTML error page).
    Unlike bhavcopy_raw() (equity), it was never updated with a UDIFF
    fallback. This calls NSE's modern UDiFF daily-reports API directly —
    the same one bhavcopy_raw() itself falls back to internally — which
    works (verified: real strike/expiry/OHLC/OI data). Covers only the
    current and previous trading day (NSE's own limit on this API); older
    dates fall through to the caller's fallback.
    """
    import io
    import zipfile
    from jugaad_data.nse.archives import NSEDailyReports

    content = NSEDailyReports().download_file("FO-UDIFF-BHAVCOPY-CSV", trading_date=dt, segment="FO")
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        fname = zf.namelist()[0]
        with zf.open(fname) as f:
            return f.read().decode("utf-8")


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

    raw = None
    if segment.upper() == "FO":
        try:
            raw = _fetch_fo_bhavcopy_udiff(dt)
        except Exception as e:
            logger.debug(f"F&O UDiFF fetch failed for {dt}, trying legacy endpoint: {e}")
            try:
                raw = bhavcopy_fo_raw(dt)
            except Exception as e2:
                logger.warning(f"jugaad-data bhavcopy fetch failed for {dt} (FO): {e2}")
                return pd.DataFrame()
    else:
        try:
            raw = bhavcopy_raw(dt)
        except Exception as e:
            logger.warning(f"jugaad-data bhavcopy fetch failed for {dt} ({segment}): {e}")
            return pd.DataFrame()

    try:
        df = pd.read_csv(StringIO(raw))
        df.columns = [c.strip().lower() for c in df.columns]
        return df
    except Exception as e:
        logger.warning(f"Could not parse bhavcopy CSV for {dt} ({segment}): {e}")
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

    # jugaad-data's on-disk cache directory (~/AppData/Local/nsehistory-stock
    # on Windows) throws WinError 183 ("cannot create a file that already
    # exists") the very first time it's created on a machine — a race in the
    # library's own os.makedirs() call, not anything wrong with the request.
    # Every call after the first succeeds once the directory exists, so one
    # retry clears it rather than failing the whole first run.
    for attempt in range(2):
        try:
            df = stock_df(symbol=symbol, from_date=from_date, to_date=to_date, series=series)
            df.columns = [c.strip().upper() for c in df.columns]
            return df
        except Exception as e:
            if attempt == 0 and "WinError 183" in str(e):
                logger.info(f"jugaad-data cache dir race for {symbol}, retrying once...")
                continue
            logger.warning(f"jugaad-data stock history fetch failed for {symbol}: {e}")
            return pd.DataFrame()
    return pd.DataFrame()


def fetch_index_history(index_name: str, from_date: date, to_date: date) -> pd.DataFrame:
    """
    Daily OHLC history for an NSE index (e.g. 'NIFTY 50', 'NIFTY BANK').

    KNOWN GAP: as of jugaad-data 0.33.1, the NSE endpoint this hits
    (index_df / index_raw) returns a non-JSON response for at least some
    indices/date ranges — an upstream library/NSE-side issue, not something
    fixable from here. Returns empty on any failure; callers (see
    predictions/utils/data_fetcher.py) fall back to AngelOne's own
    historical candles for index symbols when this comes back empty.
    """
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
