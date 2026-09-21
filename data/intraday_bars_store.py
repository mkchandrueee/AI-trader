"""
On-disk cache of 5-minute bars, one gzip CSV per symbol under data/intraday_cache/bars/.

Why it exists (REVIEW_2026-09-21.md C2): the intraday engine used to keep its bars in memory only, so the
scorecard could be produced only by a live backend with broker sessions and was lost on every restart. Persisting
them makes research repeatable offline and lets a restarted backend skip the 14-day history fetch (which is the
part that costs ~50 broker calls) and fetch only the current session.

Format: timestamp (naive IST), open, high, low, close, volume. Merging keeps the LATEST write for a timestamp,
so a re-fetched current session replaces its earlier partial copy.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import pandas as pd

BARS_DIR = Path(__file__).resolve().parent / "intraday_cache" / "bars"
COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


def _path(symbol: str) -> Path:
    safe = "".join(c if c.isalnum() else "_" for c in symbol)
    return BARS_DIR / f"{safe}.csv.gz"


def load_bars(symbol: str) -> Optional[pd.DataFrame]:
    p = _path(symbol)
    if not p.exists():
        return None
    try:
        df = pd.read_csv(p, parse_dates=["timestamp"])
    except Exception:
        return None
    return df if not df.empty else None


def save_bars(symbol: str, df: pd.DataFrame, merge: bool = True) -> int:
    """Write `df` (optionally merged into what is already stored). Returns the number of bars now on disk."""
    df = df[COLUMNS].copy()
    if merge:
        old = load_bars(symbol)
        if old is not None:
            df = pd.concat([old, df], ignore_index=True)
    df = df.drop_duplicates("timestamp", keep="last").sort_values("timestamp").reset_index(drop=True)
    BARS_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(_path(symbol), index=False, compression="gzip")
    return len(df)


def load_all() -> dict[str, pd.DataFrame]:
    """symbol-file-stem -> bars. Stems are the sanitised names (NIFTY_I, BAJAJ_AUTO, M_M)."""
    out = {}
    if BARS_DIR.exists():
        for p in sorted(BARS_DIR.glob("*.csv.gz")):
            try:
                df = pd.read_csv(p, parse_dates=["timestamp"])
            except Exception:
                continue
            if not df.empty:
                out[p.name[:-len(".csv.gz")]] = df
    return out
