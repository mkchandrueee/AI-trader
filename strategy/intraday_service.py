"""
Live-sync layer for the Intraday Engine (strategy/intraday_scanner.py).

Accuracy rules this module enforces:
  * Bars come from the brokers' own candle endpoints, mStock first / AngelOne
    fallback (data/live_bars.py), through ONE shared session per broker.
  * The engine analyses COMPLETED 5-minute bars only. A background thread
    refreshes a few seconds after each bar closes (09:20:08, 09:25:08, ...) so
    the newest closed bar is normally in the scan within ~30 seconds.
  * Every scan carries its own sync report: which bar it is as of, how old that
    is, how many symbols refreshed / are stale / failed, which source served
    them, and a reconciliation of the NIFTY futures bar against our own
    tick-built candles in the database. The UI shows all of it, so a stale or
    disputed read is visible instead of silently trusted.
  * Nothing here starts until the first API request, and it never logs in on
    its own schedule when the market is closed and data is already loaded.

All state is in-process and resets on a Flask restart.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

from data.live_bars import Instrument, fetch_bars
from strategy import intraday_scanner as isc
from utils.logger import get_logger

logger = get_logger("intraday_service")

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "intraday_cache"
INDEX = Instrument("NIFTY-I", "NFO", "NIFTY-I")           # continuous futures alias both brokers' resolvers understand
HISTORY_DAYS = 14                                          # calendar days of 5-min bars for context + replay
REFRESH_DELAY_SECS = 8                                     # after a bar closes, give the brokers a moment to publish it
FAIL_BACKOFF_SECS = 60
MARKET_OPEN, MARKET_CLOSE = (9, 15), (15, 35)
RECON_TOLERANCE_PCT = 0.05

# Used only if NSE's NIFTY 50 constituent list can't be fetched. Reviewed 2026; index membership drifts, so the
# live list always wins when available.
FALLBACK_UNIVERSE = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK", "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BEL", "BHARTIARTL",
    "CIPLA", "COALINDIA", "DRREDDY", "EICHERMOT", "ETERNAL", "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE", "HEROMOTOCO",
    "HINDALCO", "HINDUNILVR", "ICICIBANK", "INDUSINDBK", "INFY", "ITC", "JSWSTEEL", "KOTAKBANK", "LT", "M&M",
    "MARUTI", "NESTLEIND", "NTPC", "ONGC", "POWERGRID", "RELIANCE", "SBILIFE", "SBIN", "SHRIRAMFIN", "SUNPHARMA",
    "TCS", "TATACONSUM", "TATAMOTORS", "TATASTEEL", "TECHM", "TITAN", "TRENT", "ULTRACEMCO", "WIPRO", "JIOFIN",
]

_lock = threading.RLock()
_state: dict = {
    "frames": {},            # symbol -> DataFrame of completed + forming bars (scan trims the forming one)
    "scan": None,
    "industries": {},
    "industries_day": None,
    "universe": [],
    "universe_day": None,
    "thread": None,
    "force": threading.Event(),
    "status": {"running": False, "last_sync": None, "last_boundary": None, "next_due": None, "duration_s": None,
               "ok": 0, "total": 0, "failed": [], "sources": {}, "errors": [], "mode": "idle", "message": "not started"},
    "next_retry": 0.0,
    "replay_running": False,
}


# ── helpers ───────────────────────────────────────────────────────────────────
def market_open(now: Optional[datetime] = None) -> bool:
    now = now or datetime.now()
    return now.weekday() < 5 and MARKET_OPEN <= (now.hour, now.minute) < MARKET_CLOSE


def _boundary(now: datetime) -> datetime:
    """Start of the current 5-minute slot."""
    return now.replace(second=0, microsecond=0) - timedelta(minutes=now.minute % isc.BAR_MIN)


def get_universe() -> list[str]:
    today = date.today()
    with _lock:
        if _state["universe_day"] == today and _state["universe"]:
            return _state["universe"]
    f = CACHE_DIR / f"universe_{today.isoformat()}.json"
    syms: list[str] = []
    try:
        if f.exists():
            syms = json.loads(f.read_text(encoding="utf-8"))
        else:
            from strategy.market_scanner import fetch_index_constituents
            live = sorted(fetch_index_constituents("nifty50"))
            if len(live) >= 40:
                syms = live
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                f.write_text(json.dumps(syms), encoding="utf-8")
    except Exception as e:
        logger.warning(f"NIFTY 50 constituents unavailable: {e}")
    if not syms:
        syms = list(FALLBACK_UNIVERSE)
        logger.warning("Intraday universe: using the built-in fallback list (NSE constituent list unavailable)")
    with _lock:
        _state.update(universe=syms, universe_day=today)
    return syms


def _industries() -> dict:
    today = date.today()
    with _lock:
        if _state["industries_day"] == today:
            return _state["industries"]
    try:
        from strategy.market_scanner import fetch_index_industries
        ind = fetch_index_industries("nifty500")
    except Exception as e:
        logger.warning(f"industries unavailable: {e}")
        ind = {}
    with _lock:
        _state.update(industries=ind, industries_day=today if ind else None)
    return ind


def _reconcile(index_df: Optional[pd.DataFrame], now: datetime) -> Optional[dict]:
    """Compare the broker's last closed NIFTY-futures 5-min bar with the same bar built from OUR OWN ticks in the DB."""
    if index_df is None or index_df.empty:
        return None
    try:
        from database.db import read_sql
        done = isc.completed_only(index_df, now)
        if done.empty:
            return None
        last = done.iloc[-1]
        start = pd.Timestamp(last["timestamp"]).normalize()
        db = read_sql("SELECT timestamp, close FROM minute_candles WHERE symbol = 'NIFTY-I' AND timestamp >= :s ORDER BY timestamp",
                      {"s": f"{start:%Y-%m-%d} 00:00:00+05:30"})
        if db.empty:
            return {"ok": None, "note": "no DB candles for NIFTY-I yet (tick collector may still be starting)"}
        ts = pd.to_datetime(db["timestamp"], utc=True).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
        db = db.assign(ts=ts)
        db["bucket"] = db["ts"] - pd.to_timedelta(db["ts"].dt.minute % isc.BAR_MIN, unit="m")
        bar_end = pd.Timestamp(last["timestamp"]) + pd.Timedelta(minutes=isc.BAR_MIN - 1)
        mine = db[(db["bucket"] == pd.Timestamp(last["timestamp"])) & (db["ts"] <= bar_end)]
        if mine.empty:
            return {"ok": None, "note": f"our tick candles have no rows for the {pd.Timestamp(last['timestamp']):%H:%M} bar"}
        db_close = float(mine.iloc[-1]["close"])
        diff = 100 * (float(last["close"]) / db_close - 1)
        return {"ok": abs(diff) <= RECON_TOLERANCE_PCT, "bar": str(pd.Timestamp(last["timestamp"])), "broker_close": round(float(last["close"]), 2),
                "db_close": round(db_close, 2), "diff_pct": round(diff, 3), "tolerance_pct": RECON_TOLERANCE_PCT}
    except Exception as e:
        return {"ok": None, "note": f"reconciliation unavailable: {e}"}


# ── the sync ──────────────────────────────────────────────────────────────────
def _sync_once(reason: str) -> None:
    now = datetime.now()
    insts = [Instrument(s, "NSE") for s in get_universe()] + [INDEX]
    start_full = (now - timedelta(days=HISTORY_DAYS)).replace(hour=9, minute=0, second=0, microsecond=0)
    start_today = now.replace(hour=9, minute=0, second=0, microsecond=0)
    t0 = time.time()
    sources: dict[str, int] = {}
    problems: list[str] = []
    failed: list[str] = []
    with _lock:
        _state["status"].update(running=True, message=f"syncing ({reason})", mode="live" if market_open(now) else "after_hours")
    ok = 0
    for inst in insts:
        with _lock:
            old = _state["frames"].get(inst.symbol)
        have_history = old is not None and not old.empty and old["timestamp"].dt.date.nunique() >= 3
        df, src, probs = fetch_bars(inst, start_today if have_history else start_full, now, "5min")
        if df is None:
            failed.append(inst.symbol)
            problems.extend(f"{inst.symbol} {p}" for p in probs[:2])
            continue
        if have_history:                        # keep prior sessions, replace everything from today's session start on
            day0 = pd.Timestamp(df["timestamp"].iloc[0]).normalize()
            df = pd.concat([old[old["timestamp"] < day0], df], ignore_index=True).drop_duplicates("timestamp").sort_values("timestamp")
        with _lock:
            _state["frames"][inst.symbol] = df.reset_index(drop=True)
        sources[src] = sources.get(src, 0) + 1
        ok += 1

    with _lock:
        frames = dict(_state["frames"])
    scan = isc.scan(frames, now, _industries(), INDEX.symbol) if frames else None

    stale: list[str] = []
    if scan and scan.get("asof_bar") and market_open(now):
        expected = _boundary(now) - timedelta(minutes=isc.BAR_MIN)         # newest bar that must have closed by now
        for s, df in frames.items():
            if s == INDEX.symbol:
                continue
            done = isc.completed_only(df, now)
            if done.empty or pd.Timestamp(done["timestamp"].iloc[-1]) < expected:
                stale.append(s)
    recon = _reconcile(frames.get(INDEX.symbol), now)
    dur = round(time.time() - t0, 1)
    boundary = _boundary(now)
    with _lock:
        st = _state["status"]
        st.update(running=False, last_sync=now.isoformat(timespec="seconds"), last_boundary=boundary.isoformat(timespec="seconds"),
                  duration_s=dur, ok=ok, total=len(insts), failed=failed, sources=sources, errors=problems[:6],
                  stale=stale, reconcile=recon, message="ok" if ok else "no symbol refreshed — see errors")
        _state["scan"] = scan
        _state["next_retry"] = 0.0 if ok else time.time() + FAIL_BACKOFF_SECS
    logger.info(f"[intraday] sync ({reason}): {ok}/{len(insts)} in {dur}s sources={sources} stale={len(stale)} failed={len(failed)}")
    if ok and scan:
        _maybe_replay(frames, scan.get("asof_bar"))


def _maybe_replay(frames: dict, asof: Optional[str]) -> None:
    """Refresh the pattern scorecard at most once per session date, in the background."""
    cached = isc.load_replay()
    day = (asof or "")[:10]
    if (cached and str(cached.get("asof", ""))[:10] == day) or _state["replay_running"]:
        return
    if not frames or len(next(iter(frames.values())).index) < 200:
        return
    _state["replay_running"] = True

    def work():
        try:
            snap = {s: d.copy() for s, d in frames.items()}
            # replay only earlier, fully printed sessions: drop today's forming session
            last_day = day
            for s, d in snap.items():
                snap[s] = d[d["timestamp"].dt.strftime("%Y-%m-%d") < last_day] if last_day == date.today().isoformat() else d
            stats = isc.replay_stats(snap)
            isc.save_replay(stats, day)
            logger.info("[intraday] replay scorecard refreshed")
        except Exception as e:
            logger.error(f"[intraday] replay failed: {e}")
        finally:
            _state["replay_running"] = False

    threading.Thread(target=work, daemon=True, name="intraday-replay").start()


def _loop() -> None:
    logger.info("[intraday] sync thread started")
    last_done_boundary: Optional[datetime] = None
    while True:
        try:
            now = datetime.now()
            forced = _state["force"].is_set()
            if forced:
                _state["force"].clear()
            do, reason = False, ""
            if forced:
                do, reason = True, "manual"
            elif _state["scan"] is None and time.time() >= _state["next_retry"]:
                do, reason = True, "initial load"
            elif market_open(now) and time.time() >= _state["next_retry"]:
                b = _boundary(now)
                if last_done_boundary != b and (now - b).total_seconds() >= REFRESH_DELAY_SECS and (now.hour, now.minute) >= (9, 20):
                    do, reason, last_done_boundary = True, f"bar {b:%H:%M}", b
            if do:
                _sync_once(reason)
            with _lock:
                b = _boundary(datetime.now()) + timedelta(minutes=isc.BAR_MIN, seconds=REFRESH_DELAY_SECS)
                _state["status"]["next_due"] = b.isoformat(timespec="seconds") if market_open() else None
        except Exception as e:  # the loop must survive anything
            logger.error(f"[intraday] sync loop error: {e}")
            with _lock:
                _state["status"].update(running=False, message=f"error: {e}")
            time.sleep(FAIL_BACKOFF_SECS)
        _state["force"].wait(2.0)


def ensure_started() -> None:
    with _lock:
        t = _state["thread"]
        if t is None or not t.is_alive():
            t = threading.Thread(target=_loop, daemon=True, name="intraday-sync")
            _state["thread"] = t
            t.start()


# ── API surface ───────────────────────────────────────────────────────────────
def get_scan() -> dict:
    ensure_started()
    with _lock:
        scan = _state["scan"]
        status = dict(_state["status"])
    out = dict(scan) if scan else {"error": "First sync in progress — the engine is loading the last few sessions of 5-minute bars."}
    last = status.get("last_sync")
    status["age_s"] = int((datetime.now() - datetime.fromisoformat(last)).total_seconds()) if last else None
    status["market_open"] = market_open()
    out["sync"] = status
    out["replay"] = isc.load_replay()
    return out


def sync_now() -> dict:
    ensure_started()
    with _lock:
        running = _state["status"].get("running")
    if not running:
        _state["force"].set()
    return {"started": not running}


def get_chart(symbol: str, sessions: int = 2) -> Optional[dict]:
    with _lock:
        df = _state["frames"].get(symbol.upper())
    return None if df is None else isc.chart_data(df, datetime.now(), sessions)

