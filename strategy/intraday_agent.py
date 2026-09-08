"""
Intraday Math-Engine Agent — auto-enters the Trade Decision Engine's own call
────────────────────────────────────────────────────────────────────────────
Reads the Pre Market tab's Option Trade Decision Engine (strategy/premarket.py's
`live_confirmation`) on a schedule for NIFTY / BANKNIFTY / SENSEX, and when it
returns a tradable side, opens ONE lot on that leg and exits the whole position
at the engine's own partial-book level.

Deliberately NOT layered onto backend/app.py's generic paper-trading machinery.
That system is built for the XGBoost signal pipeline and carries trailing-SL
activation, breakeven-lock-after-N-minutes and regime-based SL tightening — all
of which would override the flat "exit the full lot at partial" rule this agent
was specified with, and none of which can be selectively disabled per-position
without reworking shared code every other strategy depends on. So this keeps its
own small position book and its own exit check. Closed trades are mirrored into
the shared paper-trade list (tagged `math_decision_engine_agent`) purely so they
appear in the existing Trades / P&L views.

PAPER ONLY. Nothing here calls broker/order_manager — `_open_position` records
an intent, it does not route an order. Wiring this to a live broker is a
separate, explicit decision.

Safety: the agent starts DISARMED. `backend/app.py` exposes start/stop; a
backend restart never silently resumes placing trades.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import date, datetime, time as dtime
from typing import Optional

from strategy.premarket import SUPPORTED_SYMBOLS, live_confirmation
from utils.logger import get_logger

logger = get_logger("intraday_agent")

LIVE_CACHE_FILE = "/tmp/td_live_prices.json"

# One entry check per symbol per this many seconds. The engine reads 5-minute
# candles, so anything faster just re-reads the same candle.
ENTRY_INTERVAL_SECS = 60
EXIT_CHECK_INTERVAL_SECS = 10
# Square off anything still open before the close rather than carrying an
# intraday option overnight.
EOD_SQUAREOFF = dtime(15, 25)
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)

MAX_LOG_ENTRIES = 200

# Reentrant: the public functions call each other (run_cycle/check_exits both
# end by returning status(), and _close_position runs inside the exit lock), so
# a plain Lock self-deadlocks the moment one holds it while calling another.
_lock = threading.RLock()
_state = {
    "armed": False,
    "open_positions": {},   # symbol -> position dict
    "closed_today": [],     # position dicts, this session
    "log": [],              # rolling decision log (fired AND skipped, with reason)
    "last_cycle": None,
    "session_date": None,
}


def _log(symbol: str, action: str, detail: str, extra: Optional[dict] = None):
    entry = {
        "time": datetime.now().strftime("%H:%M:%S"),
        "symbol": symbol, "action": action, "detail": detail,
    }
    if extra:
        entry.update(extra)
    _state["log"].append(entry)
    if len(_state["log"]) > MAX_LOG_ENTRIES:
        del _state["log"][:-MAX_LOG_ENTRIES]
    logger.info(f"[agent] {symbol} {action}: {detail}")


def _reset_if_new_session():
    """Positions and the opening-mode once-per-day flag are per-session."""
    today = date.today().isoformat()
    if _state["session_date"] != today:
        _state["session_date"] = today
        _state["closed_today"] = []
        _state["log"] = []
        _state["opening_fired"] = set()
        # Any position still open across a date boundary is stale — the
        # contract may not even exist any more. Drop rather than carry.
        _state["open_positions"] = {}


def is_market_hours(now: Optional[datetime] = None) -> bool:
    now = now or datetime.now()
    return now.weekday() < 5 and MARKET_OPEN <= now.time() <= MARKET_CLOSE


def arm(on: bool = True):
    with _lock:
        _reset_if_new_session()
        _state["armed"] = bool(on)
        _log("-", "ARMED" if on else "DISARMED", "agent enabled" if on else "agent stopped")
    return status()


def status() -> dict:
    with _lock:
        return {
            "armed": _state["armed"],
            "session_date": _state["session_date"],
            "open_positions": list(_state["open_positions"].values()),
            "closed_today": list(_state["closed_today"]),
            "log": list(reversed(_state["log"][-50:])),
            "last_cycle": _state["last_cycle"],
            "symbols": list(SUPPORTED_SYMBOLS),
        }


# ── Live premium lookup ──────────────────────────────────────────────────────

_rest_throttle: dict[str, float] = {}


def _live_premium(opt_symbol: str, exchange: str) -> Optional[float]:
    """
    Latest traded premium for one option leg: the websocket tick cache first
    (same file/freshness rule backend/app.py's _tick_monitor_loop uses), then
    a rate-limited REST fallback. None when neither can answer.
    """
    try:
        if time.time() - os.path.getmtime(LIVE_CACHE_FILE) < 30:
            cache = json.loads(open(LIVE_CACHE_FILE).read())
            row = cache.get(opt_symbol) or {}
            price = float(row.get("price", 0) or 0)
            if price > 0:
                ts = row.get("ts", "")
                try:
                    if (datetime.now() - datetime.fromisoformat(ts)).total_seconds() < 120:
                        return price
                except Exception:
                    return price
    except Exception:
        pass

    # REST fallback, at most once a minute per symbol (AngelOne ~3 req/sec cap)
    if time.time() - _rest_throttle.get(opt_symbol, 0) < 60:
        return None
    try:
        from data.market_data_adapter import MarketDataAdapter
        adapter = MarketDataAdapter()
        if not adapter.authenticate():
            return None
        bars = adapter.fetch_last_n_bars(opt_symbol, n=1, interval="1min", exchange=exchange)
        _rest_throttle[opt_symbol] = time.time()
        if bars is not None and not bars.empty:
            return float(bars.iloc[-1]["close"])
    except Exception as e:
        logger.debug(f"live premium fetch failed for {opt_symbol}: {e}")
    return None


# ── Entry ────────────────────────────────────────────────────────────────────

def _open_position(symbol: str, mode: str, decision: dict) -> dict:
    """
    Record a paper entry. `exit_target` is the engine's PARTIAL level, not its
    full target — the agent was specified to take one lot and close the whole
    thing there.
    """
    side = decision["side"]
    opt_symbol = decision["ce_symbol"] if side == "call" else decision["pe_symbol"]
    pos = {
        "symbol": symbol,
        "option_symbol": opt_symbol,
        "exchange": decision.get("exchange", "NFO"),
        "side": side,
        "mode": mode,
        "lots": 1,
        "qty": decision.get("lot_size", 0),
        "entry": decision["entry"],
        "exit_target": decision["partial"],
        "stop": decision["stop"],
        "confidence": decision.get("confidence"),
        "tier": decision.get("tier"),
        "entry_time": datetime.now().isoformat(),
        "status": "OPEN",
    }
    _state["open_positions"][symbol] = pos
    _log(symbol, "ENTER", f"{side.upper()} {opt_symbol} @ {pos['entry']} -> exit {pos['exit_target']} / stop {pos['stop']}",
         {"mode": mode, "confidence": pos["confidence"]})
    return pos


def run_cycle() -> dict:
    """
    One entry pass across all supported symbols. Safe to call when disarmed or
    out of hours — it simply records why it did nothing.
    """
    with _lock:
        _reset_if_new_session()
        _state["last_cycle"] = datetime.now().isoformat()
        if not _state["armed"]:
            return status()
        armed = True
        open_syms = set(_state["open_positions"])
        opening_fired = set(_state.get("opening_fired", set()))

    if not is_market_hours():
        return status()

    for symbol in SUPPORTED_SYMBOLS:
        if symbol in open_syms:
            continue  # one live position per underlying at a time

        # "opening" is the fixed 09:15-09:20 candle — it can only ever fire
        # once per session. "latest" re-reads each newly closed 5-min candle.
        modes = ["latest"] if symbol in opening_fired else ["opening", "latest"]
        for mode in modes:
            try:
                decision = live_confirmation(symbol, "5min", mode)
            except Exception as e:
                _log(symbol, "ERROR", f"{mode}: {e}")
                continue

            if decision.get("error"):
                _log(symbol, "SKIP", f"{mode}: {decision['error']}")
                continue
            if not decision.get("is_live"):
                _log(symbol, "SKIP", f"{mode}: candle is from {decision.get('session_date')}, not today")
                continue
            if not decision.get("tradable"):
                blockers = ", ".join(decision.get("blockers") or []) or "no clear side"
                _log(symbol, "SKIP", f"{mode}: not tradable ({blockers})")
                continue

            with _lock:
                if symbol in _state["open_positions"]:
                    break  # opened by the other mode in this same pass
                _open_position(symbol, mode, decision)
                if mode == "opening":
                    _state.setdefault("opening_fired", set()).add(symbol)
            break  # one entry per symbol per cycle

    return status()


# ── Exit ─────────────────────────────────────────────────────────────────────

def _close_position(pos: dict, exit_price: float, reason: str):
    pos["exit_price"] = round(exit_price, 2)
    pos["exit_time"] = datetime.now().isoformat()
    pos["exit_reason"] = reason
    pos["status"] = "CLOSED"
    pos["pnl"] = round((exit_price - pos["entry"]) * pos["qty"], 2)
    _state["open_positions"].pop(pos["symbol"], None)
    _state["closed_today"].append(pos)
    # Plain ASCII arrow on purpose: this module is also driven from standalone
    # scripts, which don't run utils.console.fix_windows_console_encoding().
    _log(pos["symbol"], "EXIT", f"{reason} @ {pos['exit_price']} -> P&L {pos['pnl']:+,.0f}",
         {"pnl": pos["pnl"]})
    _mirror_to_paper_trades(pos)


def _mirror_to_paper_trades(pos: dict):
    """
    Copy the finished trade into the shared paper-trade list so it shows up in
    the existing Trades / P&L views. Best-effort: a failure here must never
    affect the agent's own book, which is the source of truth.
    """
    try:
        import backend.app as app_mod
        app_mod.paper_positions_by_mode.setdefault("test", []).append({
            "symbol": pos["option_symbol"],
            "direction": "CALL" if pos["side"] == "call" else "PUT",
            "strategy": "math_decision_engine_agent",
            "entry_premium": pos["entry"],
            "current_premium": pos["exit_price"],
            "exit_premium": pos["exit_price"],
            "lot_size": pos["qty"],
            "lots": 1,
            "sl": pos["stop"],
            "target": pos["exit_target"],
            "status": "CLOSED",
            "entry_time": pos["entry_time"],
            "exit_time": pos["exit_time"],
            "exit_reason": pos["exit_reason"],
            "pnl": pos["pnl"],
            "realised_pnl": pos["pnl"],
        })
    except Exception as e:
        logger.debug(f"paper-trade mirror skipped: {e}")


def check_exits() -> dict:
    """
    Exit rule, in full: close the whole lot at the partial level, or at the
    stop, or at EOD. No trailing, no breakeven lock — see module docstring.
    """
    with _lock:
        _reset_if_new_session()
        positions = list(_state["open_positions"].values())

    if not positions:
        return status()

    now = datetime.now()
    for pos in positions:
        premium = _live_premium(pos["option_symbol"], pos.get("exchange", "NFO"))

        if now.time() >= EOD_SQUAREOFF:
            with _lock:
                _close_position(pos, premium if premium is not None else pos["entry"], "EOD")
            continue

        if premium is None:
            continue
        with _lock:
            if pos["symbol"] not in _state["open_positions"]:
                continue  # closed concurrently
            pos["current_premium"] = round(premium, 2)
            pos["unrealised_pnl"] = round((premium - pos["entry"]) * pos["qty"], 2)
            if premium >= pos["exit_target"]:
                _close_position(pos, pos["exit_target"], "TARGET")
            elif premium <= pos["stop"]:
                _close_position(pos, pos["stop"], "STOP")

    return status()
