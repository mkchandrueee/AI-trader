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

Supports NIFTY, BANKNIFTY and SENSEX — see `_INDEX_CONFIG`. Each has its
own exchange, strike gap, lot size and expiry calendar, all read from
AngelOne's instrument master rather than hardcoded, since NSE/BSE keep
changing them (BANKNIFTY lost its weekly expiries; SENSEX trades on BFO,
not NFO).
"""

from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timedelta
from typing import Optional

from strategy.math_decision_strategy import (
    analyse_option_pair, analyzer_breakdown, nextday_bias, pullback_entry, value_calculator,
)
from utils.logger import get_logger

logger = get_logger("premarket")

LIVE_CACHE_FILE = "/tmp/td_live_prices.json"
OPENING_WINDOW = ("09:15", "09:20")  # fixed first 5-minute candle, matches the reference SOP
TIMEFRAME_MINUTES = {"5min": 5, "15min": 15, "30min": 30, "60min": 60}

# Per-index contract facts, all verified against AngelOne's live instrument
# master (2026-09-08). `lot_size` is only a fallback for display/sizing —
# the real lotsize comes back on the resolved contract row itself, since
# exchanges revise it and the master is republished daily.
#   NIFTY     NFO, 50-pt strikes, weekly expiries
#   BANKNIFTY NFO, 100-pt strikes, MONTHLY expiries only (weeklies withdrawn)
#   SENSEX    BFO (not NFO), 100-pt strikes, weekly expiries
_INDEX_CONFIG = {
    "NIFTY":     {"exchange": "NFO", "strike_gap": 50,  "lot_size": 65},
    "BANKNIFTY": {"exchange": "NFO", "strike_gap": 100, "lot_size": 30},
    "SENSEX":    {"exchange": "BFO", "strike_gap": 100, "lot_size": 20},
}
SUPPORTED_SYMBOLS = tuple(_INDEX_CONFIG)


def _nearest_expiry(symbol: str) -> Optional[date]:
    """
    The nearest still-open expiry for `symbol`, straight from AngelOne's
    instrument master. Deliberately NOT backtest.option_resolver's
    get_nearest_expiry(), which is NIFTY-only and additionally consults a
    NIFTY-shaped regex over minute_candles for historical dates — this
    only ever needs today's live contract, which is exactly the branch
    that module resolves via resolver.expiries_for() anyway.
    """
    from data.angelone_symbols import resolver as symbol_resolver

    today = date.today()
    out = []
    for raw in symbol_resolver.expiries_for(symbol, instrument_type="OPTIDX"):
        try:
            d = datetime.strptime(raw, "%d%b%Y").date()
        except ValueError:
            continue
        if d >= today:
            out.append(d)
    return min(out) if out else None


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
    from strategy.market_scanner import INDEX_TRADING_SYMBOL, _find_col, _safe_float, fetch_sensex_ohlc

    # SENSEX is a BSE index — it appears nowhere in NSE's index bhavcopy, so
    # it takes the AngelOne path the scanner already uses for it.
    if symbol.upper() == "SENSEX":
        ohlc = fetch_sensex_ohlc(date.today())
        if ohlc is None:
            return None, None
        return {"high": ohlc["high"], "low": ohlc["low"], "close": ohlc["close"]}, ohlc["date"]

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
    from data.angelone_symbols import resolver as symbol_resolver

    cfg = _INDEX_CONFIG.get(symbol.upper())
    if cfg is None:
        return None
    expiry = _nearest_expiry(symbol)
    if expiry is None:
        return None

    gap = cfg["strike_gap"]
    atm = int(round(spot / gap) * gap)
    ce_row = symbol_resolver.option_symbol_for(symbol, expiry, atm, "CE", exchange=cfg["exchange"])
    pe_row = symbol_resolver.option_symbol_for(symbol, expiry, atm, "PE", exchange=cfg["exchange"])
    if ce_row is None or pe_row is None:
        return None
    # lotsize comes off the resolved contract itself — exchanges revise it
    # and the master is republished daily, so prefer it over the static map.
    try:
        lot_size = int(float(ce_row.get("lotsize") or cfg["lot_size"]))
    except (TypeError, ValueError):
        lot_size = cfg["lot_size"]
    return {
        "expiry": expiry, "atm": atm, "exchange": cfg["exchange"], "lot_size": lot_size,
        "ce_symbol": ce_row["symbol"], "pe_symbol": pe_row["symbol"],
    }


class _FetchRefused(Exception):
    """
    The broker refused the request (rate limit, auth, upstream error) — as
    opposed to answering "no candle in that window". The difference matters
    to the walk-back below: stepping back a day on a refusal is both wrong
    (the day may well have data) and actively harmful (another request,
    deeper into the rate limit).
    """


def _fetch_candle_df(adapter, symbol: str, start: datetime, end: datetime, timeframe: str, exchange: str):
    """One source's raw attempt -- returns (df, refused: bool, reason). Never
    raises; the caller decides how a refusal from ONE source (of possibly
    two, see _fetch_option_candle) should affect the overall result."""
    try:
        df = adapter.fetch_historical_bars(symbol, start, end, timeframe, exchange=exchange)
    except Exception as e:
        return None, True, str(e)
    if df.empty and df.attrs.get("error"):
        return None, True, df.attrs["error"]
    return df, False, None


def _fetch_option_candle(
    adapter, opt_symbol: str, timeframe: str, mode: str, session_date: Optional[date] = None,
    exchange: str = "NFO", mstock=None, mstock_symbol: Optional[str] = None,
) -> Optional[dict]:
    """
    One OHLC candle for `opt_symbol` on `session_date` (default today): the
    fixed 09:15-09:20 opening candle (mode="opening", always 5-minute), or
    the latest *fully closed* `timeframe` candle (mode="latest") — a candle
    still forming is never returned, matching the reference tool's own rule
    (see math_decision_strategy). For a past session_date the whole day has
    already closed, so "latest" simply means that day's last candle, with
    no now-based cutoff.

    `mstock`/`mstock_symbol`: when both are given, mStock is tried FIRST
    (per the user's "mstock primary" direction) via `mstock_symbol` — the
    DB-internal alias format (backtest.option_resolver.build_option_symbol),
    since mStock's real tradingsymbol convention differs from AngelOne's
    resolved `opt_symbol` and its own _resolve() already parses that alias
    format directly. Falls back to `adapter` (AngelOne, using `opt_symbol`)
    on any mStock failure or empty result — AngelOne's historical REST path
    is the proven one; mStock's has not been exercised as long. Either
    source refusing alone doesn't abort the walk-back in
    _fetch_option_candle_with_fallback -- only both refusing does (a single
    source's rate limit isn't a reason to give up on the other).
    """
    now = datetime.now()
    session_date = session_date or now.date()
    is_today = session_date == now.date()

    if mode == "opening":
        start = datetime.combine(session_date, datetime.strptime(OPENING_WINDOW[0], "%H:%M").time())
        end = datetime.combine(session_date, datetime.strptime(OPENING_WINDOW[1], "%H:%M").time())
    else:
        start = datetime.combine(session_date, datetime.strptime("09:15", "%H:%M").time())
        end = now if is_today else datetime.combine(session_date, datetime.strptime("15:30", "%H:%M").time())
    fetch_tf = "5min" if mode == "opening" else timeframe

    refusals = []
    df = None
    source = None
    if mstock is not None and mstock_symbol is not None:
        df, refused, reason = _fetch_candle_df(mstock, mstock_symbol, start, end, fetch_tf, exchange)
        if refused:
            refusals.append(f"mstock: {reason}")
        elif df is not None and not df.empty:
            source = "mstock"
    if df is None or df.empty:
        df2, refused, reason = _fetch_candle_df(adapter, opt_symbol, start, end, fetch_tf, exchange)
        if refused:
            refusals.append(f"angelone: {reason}")
        elif df2 is not None and not df2.empty:
            df, source = df2, "angelone"

    if df is None or df.empty:
        if refusals:
            raise _FetchRefused("; ".join(refusals))
        return None  # genuinely no candle in this window on either source

    if mode == "opening":
        row = df.iloc[0]
    else:
        if is_today:
            tf_minutes = TIMEFRAME_MINUTES.get(timeframe, 5)
            df = df[df["timestamp"] + timedelta(minutes=tf_minutes) <= now]
            if df.empty:
                return None  # only the still-forming candle exists so far
        row = df.iloc[-1]

    return {
        "timestamp": row["timestamp"].isoformat() if hasattr(row["timestamp"], "isoformat") else str(row["timestamp"]),
        "open": float(row["open"]), "high": float(row["high"]), "low": float(row["low"]), "close": float(row["close"]),
        "source": source,
    }


def _fetch_option_candle_with_fallback(
    adapter, opt_symbol: str, timeframe: str, mode: str, max_days: int = 7, exchange: str = "NFO",
    today_only: bool = False, mstock=None, mstock_symbol: Optional[str] = None,
) -> tuple[Optional[dict], Optional[date], bool]:
    """
    Try today's session first; if the market is closed today (weekend,
    holiday, or simply before/just after the requested window has printed),
    walk back to the last session that has this candle. Returns
    (candle, session_date, is_live) — is_live is True only when the candle
    actually came from today's session.

    `today_only` skips the walk-back entirely. Automated callers want this:
    they act only on is_live data, so every fallback result they fetch is
    discarded anyway — pure API spend, and enough of it to trip the rate
    limit that then corrupts everyone else's reads.

    A broker refusal aborts rather than walking back — see _FetchRefused.
    """
    d = date.today()
    for i in range(1 if today_only else max_days):
        if i > 0:
            time.sleep(0.35)  # AngelOne historical REST: ~3 req/sec, same spacing as market_data_adapter.py
        try:
            candle = _fetch_option_candle(adapter, opt_symbol, timeframe, mode, session_date=d, exchange=exchange,
                                           mstock=mstock, mstock_symbol=mstock_symbol)
        except _FetchRefused as e:
            logger.warning(f"{opt_symbol}: broker refused ({e}) — not walking back, the day may well have data")
            return None, None, False
        if candle is not None:
            return candle, d, d == date.today()
        d = d - timedelta(days=1)
    return None, None, False


_mstock_singleton = None  # see _get_mstock_client()


def _get_mstock_client():
    """
    A cached, module-level MStockMarketData instance, reused across every
    live_confirmation() call. Confirmed live 2026-09-18: constructing a
    fresh MStockMarketData() per call (as this used to) forces a brand-new
    TOTP login every ~30-60s (the agent's own cycle interval) since
    authenticate()'s "already logged in" check lives on the instance, not
    anywhere shared -- a new object always starts unauthenticated. That
    was very likely kicking out scripts/collect_ticks.py's own already-
    working mStock WebSocket session (mStock's docs/this project's own
    check-script warnings already flag single-session-per-login
    enforcement), producing a live tick-collector restart-thrashing loop
    (every ~30s instead of running continuously for the whole session) and
    611 mStock re-authentications in about 10 minutes. Reusing one
    instance means authenticate() only actually logs in once, then reuses
    that session on every subsequent call.
    """
    global _mstock_singleton
    if _mstock_singleton is None:
        from data.mstock_market_data import MStockMarketData
        _mstock_singleton = MStockMarketData()
    return _mstock_singleton


def live_confirmation(symbol: str = "NIFTY", timeframe: str = "5min", mode: str = "latest",
                      today_only: bool = False) -> dict:
    """
    Option Trade Decision Engine — Live. Runs analyse_option_pair on the
    requested ATM CE/PE candle, then checks whether its side agrees with
    (a) the day's NextDay Direction Analyser bias and (b) the fixed
    09:15-09:20 opening reading (skipped when this call itself IS the
    opening reading, or when the opening candle hasn't printed yet).
    """
    from data.market_data_adapter import MarketDataAdapter
    from backtest.option_resolver import build_option_symbol

    symbol = symbol.upper()
    if symbol not in _INDEX_CONFIG:
        return {"error": f"Live confirmation supports {', '.join(SUPPORTED_SYMBOLS)} — not {symbol}."}
    if timeframe not in TIMEFRAME_MINUTES:
        return {"error": f"Unknown timeframe {timeframe!r}. Use one of {list(TIMEFRAME_MINUTES)}."}

    # mStock tried first (per user direction), AngelOne is the proven
    # fallback -- only fail the whole call if BOTH sessions are down, same
    # "only one needs to connect" principle scripts/collect_ticks.py
    # already applies to live ticks. mstock is a cached, reused instance
    # (see _get_mstock_client()) -- NOT constructed fresh per call.
    mstock = _get_mstock_client()
    mstock_ok = mstock.authenticate()
    # One shared AngelOne session for the whole process (data/live_bars.get_angel):
    # a fresh MarketDataAdapter() here logged into AngelOne on EVERY call.
    from data.live_bars import get_angel
    adapter = get_angel()
    angelone_ok = adapter.authenticate()
    if not mstock_ok and not angelone_ok:
        return {"error": "Neither mStock nor AngelOne is connected — connect via the sidebar (needs a live session for option candles)."}
    if not mstock_ok:
        logger.warning(f"{symbol}: mStock session unavailable this call, relying on AngelOne alone")
        mstock = None

    spot = _current_spot(symbol)
    if spot is None:
        return {"error": f"Could not resolve a current or previous-close price for {symbol}."}

    resolved = _resolve_atm_symbols(symbol, spot)
    if resolved is None:
        return {"error": f"Could not resolve this week's ATM option contracts for {symbol}."}

    exch = resolved["exchange"]
    # mStock's real tradingsymbol format differs from AngelOne's resolved
    # ce_symbol/pe_symbol above -- its own _resolve() already parses the
    # DB-internal alias format directly (backtest/option_resolver.py's
    # build_option_symbol), so that's what gets passed for the mStock leg.
    # build_option_symbol() hardcodes an "NFO NIFTY" tradingsymbol prefix
    # (it's NIFTY-only, same as get_nearest_expiry() -- see that module's
    # own docstring), so calling it for BANKNIFTY/SENSEX produces a
    # wrong-underlying symbol like "NIFTY26092956100CE" carrying
    # BANKNIFTY's strike -- confirmed live 2026-09-18 as a stream of
    # "Could not resolve mStock instrument token" errors. Only attempt the
    # mStock leg for NIFTY; BANKNIFTY/SENSEX fall straight to AngelOne,
    # unchanged from before this dual-source work.
    if symbol == "NIFTY":
        mstock_ce = build_option_symbol(resolved["expiry"], resolved["atm"], "CE")
        mstock_pe = build_option_symbol(resolved["expiry"], resolved["atm"], "PE")
    else:
        mstock, mstock_ce, mstock_pe = None, None, None
    ce_candle, ce_date, ce_live = _fetch_option_candle_with_fallback(
        adapter, resolved["ce_symbol"], timeframe, mode, exchange=exch, today_only=today_only,
        mstock=mstock, mstock_symbol=mstock_ce)
    pe_candle, pe_date, pe_live = _fetch_option_candle_with_fallback(
        adapter, resolved["pe_symbol"], timeframe, mode, exchange=exch, today_only=today_only,
        mstock=mstock, mstock_symbol=mstock_pe)
    if ce_candle is None or pe_candle is None:
        window = "09:15-09:20" if mode == "opening" else f"latest closed {timeframe}"
        scope = "today" if today_only else "the last 7 sessions"
        return {"error": f"No {window} candle available for {resolved['ce_symbol']}/{resolved['pe_symbol']} in {scope}."}
    logger.info(f"{symbol} {mode}: CE from {ce_candle.get('source')}, PE from {pe_candle.get('source')}")

    # CE and PE fall back independently (see _fetch_option_candle_with_fallback);
    # in practice they land on the same session, but if they ever disagree,
    # the pair is only genuinely live when BOTH legs are today's session —
    # a mixed pair is reported as the earlier (non-live) of the two dates.
    is_live = ce_live and pe_live
    candle_session_date = min(ce_date, pe_date) if not is_live else ce_date

    ce_ohlc = (ce_candle["open"], ce_candle["high"], ce_candle["low"], ce_candle["close"])
    pe_ohlc = (pe_candle["open"], pe_candle["high"], pe_candle["low"], pe_candle["close"])
    decision = analyse_option_pair(ce_ohlc, pe_ohlc)

    # Reference-app "Options Analyzer" breakdown + "Value Calculator", auto-fed
    # from the candles fetched above instead of typed in by hand. Display-only:
    # nothing here feeds back into `decision` or the live agent.
    analyzer = analyzer_breakdown(ce_ohlc, pe_ohlc)
    cl, pl = analyzer["call_ladder"], analyzer["put_ladder"]
    value_calc = value_calculator([
        cl["entry"], cl["targets"][0]["level"], cl["targets"][1]["level"],
        pl["entry"], pl["targets"][0]["level"], pl["targets"][1]["level"],
    ])

    # Pullback Entry on the analyzer's leading leg (nothing to enter on WAIT).
    lead_side = analyzer["verdict"]["side"]
    pullback = pullback_entry(lead_side, ce_ohlc if lead_side == "call" else pe_ohlc)

    nextday = nextday_reading(symbol)
    nextday_side = {"bullish": "call", "bearish": "put"}.get(nextday.get("direction"))
    agrees_with_nextday = (decision.side == nextday_side) if (decision.side and nextday_side) else None

    agrees_with_opening = None
    opening_side = None
    if mode != "opening":
        # Compare against the SAME session's opening candle — if the live
        # fetch above fell back to a previous day, the opening reading must
        # come from that same day, not today's (possibly nonexistent) open.
        try:
            opening_ce = _fetch_option_candle(adapter, resolved["ce_symbol"], "5min", "opening", session_date=candle_session_date, exchange=exch,
                                               mstock=mstock, mstock_symbol=mstock_ce)
            opening_pe = _fetch_option_candle(adapter, resolved["pe_symbol"], "5min", "opening", session_date=candle_session_date, exchange=exch,
                                               mstock=mstock, mstock_symbol=mstock_pe)
        except _FetchRefused as e:
            # Only the cross-check against the open — the primary reading
            # above stands, so report it with the comparison unavailable
            # rather than failing the whole call.
            logger.warning(f"opening comparison skipped for {symbol}: {e}")
            opening_ce = opening_pe = None
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
        "exchange": resolved["exchange"], "lot_size": resolved["lot_size"],
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
        "analyzer": analyzer,
        "value_calc": value_calc,
        "pullback": pullback,
    }
