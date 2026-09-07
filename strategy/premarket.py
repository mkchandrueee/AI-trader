"""
Pre-Market — NextDay Direction Analyser + Option Trade Decision Engine (Live)
──────────────────────────────────────────────────────────────────────────
Two pieces, both already ported into math_decision_strategy.py, wired
together here into the before-the-bell / during-the-session workflow the
reference tool describes:

1. **NextDay Direction Analyser** (`nextday_reading`) — classic floor
   pivots on the previous session's completed NIFTY candle, read before the
   bell: BULLISH / BEARISH / no clear direction for the session ahead.

2. **Option Trade Decision Engine — Live** (`live_confirmation`) — the same
   engine the Options tab uses (`analyse_option_pair`), run on demand
   against either the fixed 09:15–09:20 opening ATM CE/PE candle or the
   latest *fully closed* 5/15/30/60-minute one, so a reading taken mid-session
   can be checked against what the open and the pre-market pivots said.
   "Confirms" means the engine's own side pick (CALL/PUT) still matches;
   nothing here re-derives a new formula.

NIFTY-only, matching `backtest/option_resolver.py` (the ATM/expiry
resolution this reuses is itself NIFTY-only).
"""

from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timedelta
from typing import Optional

from strategy.math_decision_strategy import analyse_option_pair, nextday_bias
from utils.logger import get_logger

logger = get_logger("premarket")

LIVE_CACHE_FILE = "/tmp/td_live_prices.json"
OPENING_WINDOW = ("09:15", "09:20")  # fixed first 5-minute candle, matches the reference SOP
TIMEFRAME_MINUTES = {"5min": 5, "15min": 15, "30min": 30, "60min": 60}


def _fetch_with_fallback(fetch_fn, start_date: date, max_days: int = 7) -> tuple[Optional[dict], Optional[date]]:
    """Walk back from start_date to find the latest published index session."""
    d = start_date
    for _ in range(max_days):
        row = fetch_fn(d)
        if row is not None:
            return row, d
        d = d - timedelta(days=1)
    return None, None


def _previous_session_ohlc(symbol: str = "NIFTY") -> tuple[Optional[dict], Optional[date]]:
    """
    High/Low/Close of the last COMPLETED session — what the NextDay
    Direction Analyser reads to call tomorrow's lean. Sourced from the free
    NSE index EOD bhavcopy (same source as market_scanner.py).

    Walk-back starts at TODAY, not yesterday: NSE only publishes a day's
    bhavcopy after that session closes, so a same-day request before
    publish correctly comes back empty and falls through to the prior day —
    there's no risk of reading an incomplete, still-forming session. Read
    in the evening (after today's close and NSE's publish), today itself
    IS the last completed session and must be used, not skipped — a fixed
    "start at yesterday" previously always excluded it, so a reading taken
    after the close was silently stale by a full session (confirmed against
    the reference tool: 2026-09-07 22:01 IST correctly read TODAY's own
    H 23890 / L 23737.9 / C 23779.15, this code was returning Sep 4's).
    """
    from data.jugaad_adapter import fetch_index_bhavcopy
    from strategy.market_scanner import INDEX_TRADING_SYMBOL, _find_col, _safe_float

    index_name = next((k for k, v in INDEX_TRADING_SYMBOL.items() if v == symbol.upper()), None)
    if index_name is None:
        return None, None

    def _fetch_one(d: date) -> Optional[dict]:
        df = fetch_index_bhavcopy(d)
        if df.empty:
            return None
        name_col = _find_col(df, ["index name", "index_name", "indexname"])
        h_col = _find_col(df, ["high index value", "high"])
        l_col = _find_col(df, ["low index value", "low"])
        c_col = _find_col(df, ["closing index value", "close index value", "close"])
        if not all([name_col, h_col, l_col, c_col]):
            return None
        rows = df[df[name_col].str.strip().str.lower() == index_name]
        if rows.empty:
            return None
        r = rows.iloc[0]
        h, l, c = _safe_float(r[h_col]), _safe_float(r[l_col]), _safe_float(r[c_col])
        if not all(v == v and v > 0 for v in (h, l, c)):
            return None
        return {"high": h, "low": l, "close": c}

    return _fetch_with_fallback(_fetch_one, date.today())


def nextday_reading(symbol: str = "NIFTY") -> dict:
    """NextDay Direction Analyser result for `symbol`, or an error dict."""
    ohlc, session_date = _previous_session_ohlc(symbol)
    if ohlc is None:
        return {"error": f"No published index bhavcopy found for {symbol} in the last 7 days."}

    bias = nextday_bias(ohlc["high"], ohlc["low"], ohlc["close"])
    return {
        "symbol": symbol, "session_date": session_date.isoformat(),
        "prev_high": ohlc["high"], "prev_low": ohlc["low"], "prev_close": ohlc["close"],
        "direction": bias.direction, "entry": bias.entry, "stop": bias.stop,
        "target1": bias.target1, "target2": bias.target2,
        "risk": bias.risk, "reward": bias.reward, "rr": bias.rr,
    }


def _current_spot(symbol: str = "NIFTY") -> Optional[float]:
    """
    Best-effort current price for ATM-strike rounding: the live tick cache
    (same file/key every other live route reads — see backend/app.py's
    LIVE_CACHE_FILE) if fresh, else the previous session's close (the right
    fallback before the bell, when no cache has been written yet).
    """
    try:
        if time.time() - os.path.getmtime(LIVE_CACHE_FILE) < 90:
            cache = json.loads(open(LIVE_CACHE_FILE).read())
            price = float(cache.get(f"{symbol}-I", {}).get("price", 0) or 0)
            if price > 0:
                return price
    except Exception:
        pass

    ohlc, _ = _previous_session_ohlc(symbol)
    return ohlc["close"] if ohlc else None


def _resolve_atm_symbols(symbol: str, spot: float) -> Optional[dict]:
    """
    Resolve this week's ATM CE/PE to AngelOne's real tradingsymbol via the
    instrument master (data.angelone_symbols.resolver.option_symbol_for) —
    NOT backtest.option_resolver.build_option_symbol's DB-internal alias
    format, which never matches a real AngelOne symbol (its
    "{underlying}{yymmdd}{strike}{type}" guess vs. AngelOne's actual
    "{underlying}{DD}{MMM}{YY}{strike}{type}", e.g. "NIFTY08SEP2622100PE").
    That mismatch is why live option candle/websocket resolution silently
    returns nothing wherever the DB-alias format is used for a live call.
    """
    from backtest.option_resolver import get_nearest_expiry, get_atm_strike
    from data.angelone_symbols import resolver as symbol_resolver

    if symbol != "NIFTY":
        return None  # ATM/expiry resolution below is NIFTY-only, see module docstring
    expiry = get_nearest_expiry(date.today())
    if expiry is None:
        return None
    atm = get_atm_strike(spot)
    ce_row = symbol_resolver.option_symbol_for("NIFTY", expiry, atm, "CE")
    pe_row = symbol_resolver.option_symbol_for("NIFTY", expiry, atm, "PE")
    if ce_row is None or pe_row is None:
        return None
    return {
        "expiry": expiry, "atm": atm,
        "ce_symbol": ce_row["symbol"], "pe_symbol": pe_row["symbol"],
    }


def _fetch_option_candle(adapter, opt_symbol: str, timeframe: str, mode: str, session_date: Optional[date] = None) -> Optional[dict]:
    """
    One OHLC candle for `opt_symbol` on `session_date` (default today): the
    fixed 09:15-09:20 opening candle (mode="opening", always 5-minute), or
    the latest *fully closed* `timeframe` candle (mode="latest") — a candle
    still forming is never returned, matching the reference tool's own rule
    (see math_decision_strategy). For a past session_date the whole day has
    already closed, so "latest" simply means that day's last candle, with
    no now-based cutoff.
    """
    now = datetime.now()
    session_date = session_date or now.date()
    is_today = session_date == now.date()

    if mode == "opening":
        start = datetime.combine(session_date, datetime.strptime(OPENING_WINDOW[0], "%H:%M").time())
        end = datetime.combine(session_date, datetime.strptime(OPENING_WINDOW[1], "%H:%M").time())
        df = adapter.fetch_historical_bars(opt_symbol, start, end, "5min", exchange="NFO")
        if df.empty:
            return None
        row = df.iloc[0]
    else:
        tf_minutes = TIMEFRAME_MINUTES.get(timeframe, 5)
        market_open = datetime.combine(session_date, datetime.strptime("09:15", "%H:%M").time())
        market_close = datetime.combine(session_date, datetime.strptime("15:30", "%H:%M").time())
        end_bound = now if is_today else market_close
        df = adapter.fetch_historical_bars(opt_symbol, market_open, end_bound, timeframe, exchange="NFO")
        if df.empty:
            return None
        if is_today:
            df = df[df["timestamp"] + timedelta(minutes=tf_minutes) <= now]
            if df.empty:
                return None  # only the still-forming candle exists so far
        row = df.iloc[-1]

    return {
        "timestamp": row["timestamp"].isoformat() if hasattr(row["timestamp"], "isoformat") else str(row["timestamp"]),
        "open": float(row["open"]), "high": float(row["high"]), "low": float(row["low"]), "close": float(row["close"]),
    }


def _fetch_option_candle_with_fallback(
    adapter, opt_symbol: str, timeframe: str, mode: str, max_days: int = 7,
) -> tuple[Optional[dict], Optional[date], bool]:
    """
    Try today's session first; if the market is closed today (weekend,
    holiday, or simply before/just after the requested window has printed),
    walk back to the last session that has this candle. Returns
    (candle, session_date, is_live) — is_live is True only when the candle
    actually came from today's session.
    """
    d = date.today()
    for i in range(max_days):
        if i > 0:
            time.sleep(0.35)  # AngelOne historical REST: ~3 req/sec, same spacing as market_data_adapter.py
        candle = _fetch_option_candle(adapter, opt_symbol, timeframe, mode, session_date=d)
        if candle is not None:
            return candle, d, d == date.today()
        d = d - timedelta(days=1)
    return None, None, False


def live_confirmation(symbol: str = "NIFTY", timeframe: str = "5min", mode: str = "latest") -> dict:
    """
    Option Trade Decision Engine — Live. Runs analyse_option_pair on the
    requested ATM CE/PE candle, then checks whether its side agrees with
    (a) the day's NextDay Direction Analyser bias and (b) the fixed
    09:15-09:20 opening reading (skipped when this call itself IS the
    opening reading, or when the opening candle hasn't printed yet).
    """
    from data.market_data_adapter import MarketDataAdapter

    if symbol != "NIFTY":
        return {"error": "Live confirmation currently supports NIFTY only."}
    if timeframe not in TIMEFRAME_MINUTES:
        return {"error": f"Unknown timeframe {timeframe!r}. Use one of {list(TIMEFRAME_MINUTES)}."}

    adapter = MarketDataAdapter()
    if not adapter.authenticate():
        return {"error": "AngelOne is not connected — connect via the sidebar (needs a live session for option candles)."}

    spot = _current_spot(symbol)
    if spot is None:
        return {"error": f"Could not resolve a current or previous-close price for {symbol}."}

    resolved = _resolve_atm_symbols(symbol, spot)
    if resolved is None:
        return {"error": f"Could not resolve this week's ATM option contracts for {symbol}."}

    ce_candle, ce_date, ce_live = _fetch_option_candle_with_fallback(adapter, resolved["ce_symbol"], timeframe, mode)
    pe_candle, pe_date, pe_live = _fetch_option_candle_with_fallback(adapter, resolved["pe_symbol"], timeframe, mode)
    if ce_candle is None or pe_candle is None:
        window = "09:15-09:20" if mode == "opening" else f"latest closed {timeframe}"
        return {"error": f"No {window} candle available for {resolved['ce_symbol']}/{resolved['pe_symbol']} in the last 7 sessions."}

    # CE and PE fall back independently (see _fetch_option_candle_with_fallback);
    # in practice they land on the same session, but if they ever disagree,
    # the pair is only genuinely live when BOTH legs are today's session —
    # a mixed pair is reported as the earlier (non-live) of the two dates.
    is_live = ce_live and pe_live
    candle_session_date = min(ce_date, pe_date) if not is_live else ce_date

    decision = analyse_option_pair(
        (ce_candle["open"], ce_candle["high"], ce_candle["low"], ce_candle["close"]),
        (pe_candle["open"], pe_candle["high"], pe_candle["low"], pe_candle["close"]),
    )

    nextday = nextday_reading(symbol)
    nextday_side = {"bullish": "call", "bearish": "put"}.get(nextday.get("direction"))
    agrees_with_nextday = (decision.side == nextday_side) if (decision.side and nextday_side) else None

    agrees_with_opening = None
    opening_side = None
    if mode != "opening":
        # Compare against the SAME session's opening candle — if the live
        # fetch above fell back to a previous day, the opening reading must
        # come from that same day, not today's (possibly nonexistent) open.
        opening_ce = _fetch_option_candle(adapter, resolved["ce_symbol"], "5min", "opening", session_date=candle_session_date)
        opening_pe = _fetch_option_candle(adapter, resolved["pe_symbol"], "5min", "opening", session_date=candle_session_date)
        if opening_ce and opening_pe:
            opening_decision = analyse_option_pair(
                (opening_ce["open"], opening_ce["high"], opening_ce["low"], opening_ce["close"]),
                (opening_pe["open"], opening_pe["high"], opening_pe["low"], opening_pe["close"]),
            )
            opening_side = opening_decision.side
            if decision.side and opening_side:
                agrees_with_opening = decision.side == opening_side

    return {
        "symbol": symbol, "timeframe": timeframe, "mode": mode,
        "session_date": candle_session_date.isoformat(), "is_live": is_live,
        "spot": spot, "atm": resolved["atm"], "expiry": resolved["expiry"].isoformat(),
        "ce_symbol": resolved["ce_symbol"], "pe_symbol": resolved["pe_symbol"],
        "ce_candle": ce_candle, "pe_candle": pe_candle,
        "side": decision.side, "tradable": decision.tradable,
        "entry": decision.entry, "partial": decision.partial, "target": decision.target, "stop": decision.stop,
        "risk": decision.risk, "rr": decision.rr, "confidence": decision.confidence, "tier": decision.tier,
        "verdict": decision.verdict, "blockers": decision.blockers, "warnings": decision.warnings,
        "nextday_direction": nextday.get("direction"),
        "opening_side": opening_side,
        "agrees_with_nextday": agrees_with_nextday,
        "agrees_with_opening": agrees_with_opening,
    }
