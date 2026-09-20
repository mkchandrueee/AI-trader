"""
Glue for the Positional Engine API: cached scan per EOD date, sector lookup,
background EOD sync + replay refresh. Kept out of backend/app.py so the routes
stay thin. All state is in-process and resets on a Flask restart.
"""
from __future__ import annotations

import threading
from datetime import date, datetime
from typing import Optional

from data import eod_store
from strategy import positional_scanner as ps
from utils.logger import get_logger

logger = get_logger("positional_service")

_lock = threading.Lock()
_scan_cache: dict = {"key": None, "result": None}
_panel_cache: dict = {"key": None, "panel": None}
_sync: dict = {"running": False, "started": None, "finished": None, "message": "", "error": None}


def _latest_cached_day() -> Optional[str]:
    files = sorted(p.stem.replace(".csv", "") for p in eod_store.CACHE_DIR.glob("2*.csv.gz")) if eod_store.CACHE_DIR.exists() else []
    for f in reversed(files):
        if eod_store.load_day(date.fromisoformat(f), fetch=False) is not None:
            return f
    return None


def get_panel():
    key = _latest_cached_day()
    with _lock:
        if _panel_cache["key"] != key or _panel_cache["panel"] is None:
            _panel_cache.update(key=key, panel=eod_store.load_panel(130, end=date.fromisoformat(key)) if key else None)
        return _panel_cache["panel"]


def get_scan() -> dict:
    panel = get_panel()
    if panel is None or panel.empty:
        return {"error": "No EOD data cached yet — press Sync data."}
    key = str(panel["date"].max())
    with _lock:
        if _scan_cache["key"] == key:
            return _scan_cache["result"]
    try:
        from strategy.market_scanner import fetch_index_industries
        industries = fetch_index_industries("nifty500")
    except Exception as e:  # sectors are optional
        logger.warning(f"industries unavailable: {e}")
        industries = {}
    result = ps.scan(panel, industries)
    result["replay"] = ps.load_replay()
    result["sectors_available"] = bool(industries)
    with _lock:
        _scan_cache.update(key=key, result=result)
    return result


def get_chart(symbol: str, sessions: int = 90) -> Optional[dict]:
    panel = get_panel()
    return None if panel is None else ps.chart_data(panel, symbol.upper(), sessions)


def sync_status() -> dict:
    return dict(_sync)


def start_sync() -> dict:
    """Fetch any missing bhavcopy days, then refresh the replay stats. One run at a time."""
    with _lock:
        if _sync["running"]:
            return {"started": False, **_sync}
        _sync.update(running=True, started=datetime.now().isoformat(timespec="seconds"), finished=None,
                     message="fetching EOD bhavcopy", error=None)

    def work():
        try:
            counts = eod_store.sync()
            _sync["message"] = f"replaying patterns ({counts['fetched']} new days)"
            panel = eod_store.load_panel(130)
            ps.save_replay(ps.replay_stats(panel))
            with _lock:
                _scan_cache.update(key=None, result=None)
                _panel_cache.update(key=None, panel=None)
            _sync["message"] = f"done — {counts['fetched']} new days"
        except Exception as e:
            logger.error(f"positional sync failed: {e}")
            _sync["error"] = str(e)
            _sync["message"] = "failed"
        finally:
            _sync["running"] = False
            _sync["finished"] = datetime.now().isoformat(timespec="seconds")

    threading.Thread(target=work, daemon=True, name="positional-sync").start()
    return {"started": True, **_sync}


# ── intraday: ChartBank 30-minute hammer on NIFTY futures ─────────────────────
INTRADAY_DECLINE = 0.006     # 10 bars (5 hours) of decline into the hammer
INTRADAY_NEAR_SUPPORT = 0.003


def get_intraday_hammer(symbol: str = "NIFTY-I", sessions: int = 10) -> dict:
    """Latest 30-minute hammer signals on the collected index-futures minute candles (read-only)."""
    import pandas as pd
    from database.db import read_sql

    df = read_sql(
        "SELECT timestamp, open, high, low, close, volume FROM minute_candles "
        "WHERE symbol = :s AND timestamp >= now() - interval '30 days' ORDER BY timestamp", {"s": symbol})
    if df.empty:
        return {"symbol": symbol, "error": "no minute candles collected for this symbol yet", "signals": []}
    ts = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    df = df.assign(ts=ts)
    df["bucket"] = (df["ts"] - pd.Timedelta(minutes=15)).dt.floor("30min") + pd.Timedelta(minutes=15)  # 09:15-anchored
    bars = df.groupby("bucket").agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                                    close=("close", "last"), volume=("volume", "sum")).reset_index()
    bars = bars[(bars["bucket"].dt.time >= pd.Timestamp("09:15").time()) & (bars["bucket"].dt.time <= pd.Timestamp("15:00").time())]
    bars = bars.reset_index(drop=True)
    keep_from = bars["bucket"].dt.date.drop_duplicates().iloc[-sessions:].iloc[0]
    arr = {k: bars[k].to_numpy(float) for k in ("open", "high", "low", "close", "volume")}
    signals = []
    for i in range(45, len(bars)):
        if bars["bucket"].iloc[i].date() < keep_from:
            continue
        d = ps.detect_hammer(arr["open"][:i + 1], arr["high"][:i + 1], arr["low"][:i + 1], arr["close"][:i + 1],
                             arr["volume"][:i + 1], decline=INTRADAY_DECLINE, near=INTRADAY_NEAR_SUPPORT, min_target=0.004)
        if d:
            hb = d.pop("bars_back")
            d["hammer_time"] = str(bars["bucket"].iloc[i - hb])
            d["seen_at"] = str(bars["bucket"].iloc[i] + pd.Timedelta(minutes=30))
            signals.append(d)
    # a hammer seen as 'coiling' and then confirmed next bar is the same trade: keep the latest read per hammer bar
    latest = {}
    for d in signals:
        latest[d["hammer_time"]] = d
    out = sorted(latest.values(), key=lambda d: d["hammer_time"], reverse=True)[:12]
    return {"symbol": symbol, "timeframe": "30-min", "bars": len(bars), "last_bar": str(bars["bucket"].iloc[-1]),
            "sessions": sessions, "signals": out,
            "note": "Signals are computed on completed 30-minute bars only. Futures volume/levels, not option premiums."}
