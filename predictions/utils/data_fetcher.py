"""
predictions/utils/data_fetcher.py
──────────────────────────────────
Replaces NSE-Neuron's original nselib-based fetcher with jugaad-data (free,
already used elsewhere in this project — see data/jugaad_adapter.py).

Simplified from the original: rather than looking up each symbol's exact
listing date via nselib's equity master (no direct jugaad-data equivalent),
this defaults to a fixed lookback window, which is what every one of the
LSTM/BiLSTM/GRU/CNN-LSTM models actually needs (a rolling window, not the
full listing history).
"""
import os
from datetime import date, datetime, timedelta

import pandas as pd

from data.jugaad_adapter import fetch_stock_history, fetch_index_history
from predictions.utils.preprocessor import preprocess_nse_df

RAW_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "raw")

# How far back to pull daily bars when no explicit range is given. 5 years
# comfortably covers the 200-day SMA regime detector plus long LSTM lookback
# windows with room to spare.
DEFAULT_LOOKBACK_DAYS = 5 * 365

# Indices don't go through jugaad-data's stock_df (that's for equities/EQ
# series) — they need index_df instead. Recognize the common ones by name.
_INDEX_ALIASES = {
    "NIFTY": "NIFTY 50", "NIFTY50": "NIFTY 50", "NIFTY 50": "NIFTY 50",
    "BANKNIFTY": "NIFTY BANK", "NIFTY BANK": "NIFTY BANK",
    "FINNIFTY": "NIFTY FIN SERVICE",
}


def _get_cache_path(symbol: str, today: str) -> str:
    return os.path.join(RAW_DATA_DIR, f"{symbol}_{today}.csv")


def _fetch_index_via_angelone(symbol_key: str, from_d, to_d) -> pd.DataFrame:
    """
    Fallback for when jugaad-data's index endpoint is unavailable (see the
    KNOWN GAP note on fetch_index_history): AngelOne's own EOD candle API
    for the same underlying's continuous futures contract. Slightly
    different instrument (futures, not spot) but the same one the rest of
    this project already treats as "NIFTY" everywhere else (see
    data/market_data_adapter.py), so it's consistent rather than a hack.

    Needs ANGEL_* credentials in .env; returns empty (not an error) if
    they're not set or the login fails, so callers can still report a
    clear combined failure rather than a stack trace.
    """
    try:
        from data.market_data_adapter import MarketDataAdapter
        adapter = MarketDataAdapter()
        if not adapter.authenticate():
            return pd.DataFrame()
        df = adapter.fetch_historical_bars(
            symbol_key,
            datetime.combine(from_d, datetime.min.time()),
            datetime.combine(to_d, datetime.min.time()),
            interval="eod",
            exchange="NFO",
        )
        return df
    except Exception as e:
        from utils.logger import get_logger
        get_logger("predictions.data_fetcher").debug(f"AngelOne index fallback failed for {symbol_key}: {e}")
        return pd.DataFrame()


def _load_or_fetch(symbol: str, from_date: str, to_date: str) -> pd.DataFrame:
    """
    Cache-or-fetch, same contract as the original: (symbol, from_date,
    to_date) strings in '%d-%m-%Y' format in, raw DataFrame out.
    """
    os.makedirs(RAW_DATA_DIR, exist_ok=True)
    cache_path = _get_cache_path(symbol, to_date)

    if os.path.exists(cache_path):
        return pd.read_csv(cache_path)

    from_d = pd.to_datetime(from_date, dayfirst=True).date()
    to_d = pd.to_datetime(to_date, dayfirst=True).date()

    key = symbol.upper().replace("-", "").strip()
    if key in _INDEX_ALIASES:
        df = fetch_index_history(_INDEX_ALIASES[key], from_d, to_d)
        if df.empty:
            df = _fetch_index_via_angelone(key, from_d, to_d)
    else:
        df = fetch_stock_history(symbol, from_d, to_d)

    if not df.empty:
        df.to_csv(cache_path, index=False)
    return df


def getDataFrame(SYMBOL):
    """Decorator matching the original signature — see main.py's usage."""
    def decorate(func):
        def decorated(*args, **kwargs):
            algorithm = args[0] if args else kwargs.get("algorithm")

            to_date = date.today()
            from_date = to_date - timedelta(days=DEFAULT_LOOKBACK_DAYS)
            df = _load_or_fetch(
                SYMBOL,
                from_date.strftime("%d-%m-%Y"),
                to_date.strftime("%d-%m-%Y"),
            )
            df = preprocess_nse_df(df)

            details = {"scheme_name": SYMBOL, "scheme_code": str(SYMBOL)}

            if algorithm is None:
                return func(df, details)
            return func(df, details, algorithm)

        return decorated
    return decorate
