"""
Local daily-OHLCV store for the NSE cash market, built from the free EOD
bhavcopy (data/jugaad_adapter.fetch_bhavcopy(dt, "EQ")) -- one file per
trading day under data/eod_cache/, so pattern scans (strategy/positional_scanner.py)
run offline over ~130 sessions of the whole EQ board.

A holiday/weekend/unpublished day is cached as an empty marker so it is
never re-fetched; TODAY is never marked empty (the bhavcopy is published
after the close, so an early fetch must be retried later).
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

from data.jugaad_adapter import fetch_bhavcopy
from utils.logger import get_logger

logger = get_logger("eod_store")

CACHE_DIR = Path(__file__).resolve().parent / "eod_cache"
COLUMNS = ["symbol", "isin", "open", "high", "low", "close", "prev_close", "volume", "turnover"]
FETCH_SPACING_SECS = 0.6  # be polite to NSE's archive


def _path(d: date) -> Path:
    return CACHE_DIR / f"{d.isoformat()}.csv.gz"


def _normalise(raw: pd.DataFrame) -> pd.DataFrame:
    """UDiFF (or legacy) EQ bhavcopy -> the columns we keep, EQ series only."""
    if raw.empty:
        return pd.DataFrame(columns=COLUMNS)
    df = raw
    if "sctysrs" in df.columns:          # UDiFF format
        df = df[(df["sctysrs"].astype(str).str.strip() == "EQ")]
        out = pd.DataFrame({
            "symbol": df["tckrsymb"].astype(str).str.strip(),
            "isin": df.get("isin"),
            "open": df["opnpric"], "high": df["hghpric"], "low": df["lwpric"], "close": df["clspric"],
            "prev_close": df["prvsclsgpric"], "volume": df["ttltradgvol"], "turnover": df["ttltrfval"],
        })
    elif "open_price" in df.columns:      # sec_bhavdata_full format
        df = df[df["series"].astype(str).str.strip() == "EQ"]
        out = pd.DataFrame({
            "symbol": df["symbol"].astype(str).str.strip(),
            "isin": None,
            "open": df["open_price"], "high": df["high_price"], "low": df["low_price"], "close": df["close_price"],
            "prev_close": df["prev_close"], "volume": df["ttl_trd_qnty"],
            "turnover": pd.to_numeric(df["turnover_lacs"], errors="coerce") * 1e5,
        })
    else:                                 # classic bhavcopy format
        df = df[df["series"].astype(str).str.strip() == "EQ"]
        out = pd.DataFrame({
            "symbol": df["symbol"].astype(str).str.strip(),
            "isin": df.get("isin"),
            "open": df["open"], "high": df["high"], "low": df["low"], "close": df["close"],
            "prev_close": df["prevclose"], "volume": df["tottrdqty"], "turnover": df["tottrdval"],
        })
    for c in ("open", "high", "low", "close", "prev_close", "volume", "turnover"):
        out[c] = pd.to_numeric(out[c], errors="coerce")
    return out.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)


def load_day(d: date, fetch: bool = True) -> Optional[pd.DataFrame]:
    """One day's EQ rows. None = not available (holiday / not yet published)."""
    p = _path(d)
    if p.exists():
        df = pd.read_csv(p)
        return df if not df.empty else None
    if not fetch or d.weekday() >= 5:
        return None
    df = _normalise(fetch_bhavcopy(d, "EQ"))
    if df.empty:
        if d < date.today():  # a past day with no file is a holiday -- remember that
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(columns=COLUMNS).to_csv(p, index=False, compression="gzip")
        return None
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(p, index=False, compression="gzip")
    return df


def sync(days_back: int = 190, end: Optional[date] = None, progress=None) -> dict:
    """Fill the cache for the last `days_back` calendar days. Returns counts."""
    end = end or date.today()
    fetched = cached = missing = 0
    d = end - timedelta(days=days_back)
    while d <= end:
        if d.weekday() < 5:
            had = _path(d).exists()
            df = load_day(d)
            if df is None:
                missing += 1
            elif had:
                cached += 1
            else:
                fetched += 1
                time.sleep(FETCH_SPACING_SECS)
            if progress:
                progress(d, fetched, cached, missing)
        d += timedelta(days=1)
    return {"fetched": fetched, "cached": cached, "missing": missing}


def load_panel(sessions: int = 130, end: Optional[date] = None) -> pd.DataFrame:
    """Long panel (date, symbol, OHLCV) of the last `sessions` cached trading days."""
    end = end or date.today()
    frames = []
    d = end
    got = 0
    while got < sessions and d > end - timedelta(days=400):
        df = load_day(d, fetch=False)
        if df is not None:
            df = df.copy()
            df.insert(0, "date", d.isoformat())
            frames.append(df)
            got += 1
        d -= timedelta(days=1)
    if not frames:
        return pd.DataFrame(columns=["date"] + COLUMNS)
    panel = pd.concat(frames, ignore_index=True)
    return panel.sort_values(["symbol", "date"]).reset_index(drop=True)


if __name__ == "__main__":
    def _p(d, f, c, m):
        if (f + c + m) % 20 == 0:
            print(f"  {d}  fetched={f} cached={c} missing/holiday={m}", flush=True)
    print(sync(progress=_p))
