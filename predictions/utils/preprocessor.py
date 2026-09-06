"""
predictions/utils/preprocessor.py
──────────────────────────────────
Adapted from NSE-Neuron's original (which expected nselib's exact column
names). jugaad-data's `stock_df`/`index_df` return slightly different column
names depending on version and whether the source was a stock or an index,
so columns are resolved case-insensitively by candidate name rather than
hard-coded to one exact casing.
"""
import pandas as pd

import predictions.nn_config as config

PRICE_COLUMNS = ["open", "close", "prev_close", "high", "low"]

# candidate raw column names (case-insensitive) -> our standard lowercase name
_COLUMN_CANDIDATES = {
    "date": ["date", "historicaldate", "timestamp"],
    "close": ["close", "closeprice", "ltp", "last"],
    "prev_close": ["prev_close", "prevclose", "prev. close", "prev.close"],
    "high": ["high", "highprice"],
    "low": ["low", "lowprice"],
    "open": ["open", "openprice"],
}


def _find_column(df: pd.DataFrame, candidates: list[str]) -> str | None:
    lower_map = {c.lower().strip(): c for c in df.columns}
    for cand in candidates:
        if cand in lower_map:
            return lower_map[cand]
    return None


def select_and_rename_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Resolve jugaad-data's column names to the standard lowercase set."""
    resolved = {}
    for std_name, candidates in _COLUMN_CANDIDATES.items():
        col = _find_column(df, candidates)
        if col is not None:
            resolved[std_name] = col

    missing = [k for k in ("date", "close", "high", "low") if k not in resolved]
    if missing:
        raise ValueError(
            f"jugaad-data response is missing required column(s) {missing}. "
            f"Available columns: {list(df.columns)}"
        )

    out = pd.DataFrame({std: df[col] for std, col in resolved.items()})
    out.rename(columns={"date": "Date"}, inplace=True)

    if "prev_close" not in out.columns:
        # Not every jugaad-data response includes it (e.g. some index feeds) —
        # derive it from the previous row's close, which is what "prev close"
        # means anyway.
        out["prev_close"] = out["close"].shift(1)

    if "open" not in out.columns:
        out["open"] = out["close"]

    return out[["Date", "close", "prev_close", "high", "low", "open"]]


def convert_price_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Strip commas and convert price columns to numeric."""
    for col in PRICE_COLUMNS:
        df[col] = pd.to_numeric(
            df[col].astype(str).str.replace(",", "", regex=False),
            errors="coerce",
        )
    return df


def parse_and_sort_dates(df: pd.DataFrame) -> pd.DataFrame:
    """Parse Date column, dedupe, sort ascending (oldest -> newest)."""
    df = df.copy()
    df.reset_index(inplace=True, drop=True)
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df.dropna(subset=["Date"])
    df = df.sort_values(by="Date", ascending=True)
    df = df.drop_duplicates(subset=["Date"], keep="last").reset_index(drop=True)
    df["date"] = df["Date"].dt.strftime("%Y-%m-%d")
    return df


def removed_open_coulmn(df: pd.DataFrame) -> pd.DataFrame:
    if "open" in df.columns:
        df = df.drop(columns=["open"])
    return df


def preprocess_nse_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    Full preprocessing pipeline for daily equity/index price data.
    Steps: resolve+select columns -> numeric conversion -> parse/sort dates.
    """
    if df.empty:
        raise ValueError("jugaad-data returned no rows for this symbol/date range.")

    df = select_and_rename_columns(df)
    df = convert_price_columns(df)
    df = parse_and_sort_dates(df)
    # Save AFTER renaming/parsing but BEFORE removing 'open', so
    # pattern_detector gets correct column names including 'open'.
    config.HISTORIC_DATA = df.copy()
    df = removed_open_coulmn(df)
    return df
