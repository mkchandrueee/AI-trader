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


def _live_autofire_blocked() -> Optional[str]:
    """
    Hard safety backstop — independent of, and enforced regardless of, the
    module docstring's "PAPER ONLY" claim above. This agent has no human
    approval step between a validated signal and opening a position (that
    gate is planned but not yet built — see the project's AI-platform
    roadmap, Phase 3: Approval Request object). Until it exists, the ONLY
    thing standing between a signal and a real order is this check: it
    refuses to open anything unless TRADE_MODE is "paper" (the default) OR
    a second, deliberately alarming env var is ALSO set. This means
    flipping TRADE_MODE alone — e.g. to test mStock order execution
    elsewhere in the app — can never make this agent place a real,
    unapproved order by accident; someone would have to deliberately set
    a second flag with "LIVE_AUTOFIRE_CONFIRMED" in its name first.

    Returns a human-readable block reason, or None if firing is allowed.
    """
    trade_mode = os.getenv("TRADE_MODE", "paper").lower()
    if trade_mode == "paper":
        return None
    if os.getenv("INTRADAY_AGENT_LIVE_AUTOFIRE_CONFIRMED", "").strip().lower() in ("1", "true", "yes"):
        return None
    return (
        f"TRADE_MODE={trade_mode!r} but this agent has no approval gate yet — "
        f"refusing to auto-fire a real order. Set INTRADAY_AGENT_LIVE_AUTOFIRE_CONFIRMED=true "
        f"only once an approval step actually exists."
    )

LIVE_CACHE_FILE = "/tmp/td_live_prices.json"

# One entry check per symbol per this many seconds. The engine reads 5-minute
# candles, so anything faster just re-reads the same candle.
ENTRY_INTERVAL_SECS = 60
EXIT_CHECK_INTERVAL_SECS = 10
# One live_confirmation is already several candle fetches (both legs, plus
# the opening cross-check). Spacing the per-symbol calls keeps a full cycle
# under AngelOne's ~3 req/sec ceiling instead of self-throttling.
INTER_CALL_PAUSE_SECS = 1.0
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
        # contract may not even exist any more. Drop rather than carry, but
        # close out its mirrored row first: dropping it silently would leave
        # a phantom OPEN position sitting in the dashboard's paper book
        # forever, with no agent left to ever close it.
        for pos in _state.get("open_positions", {}).values():
            mirror = pos.get("_mirror")
            if mirror is not None and mirror.get("status") == "OPEN":
                mirror.update({
                    "status": "CLOSED",
                    "exit_time": datetime.now().isoformat(),
                    "exit_reason": "ABANDONED_SESSION_ROLLOVER",
                    "exit_premium": mirror.get("current_premium"),
                    "realised_pnl": mirror.get("unrealised_pnl") or 0,
                    "unrealised_pnl": 0,
                })
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


def _public(pos: dict) -> dict:
    """Position minus internals — `_mirror` holds a live reference into the
    shared paper book and has no business being serialised into the API."""
    return {k: v for k, v in pos.items() if not k.startswith("_")}


def status() -> dict:
    with _lock:
        return {
            "armed": _state["armed"],
            "agent": AGENT_NAME,
            "model": MODEL_NAME,
            "model_label": MODEL_LABEL,
            "session_date": _state["session_date"],
            "open_positions": [_public(p) for p in _state["open_positions"].values()],
            "closed_today": [_public(p) for p in _state["closed_today"]],
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

def _open_position(symbol: str, mode: str, decision: dict) -> Optional[dict]:
    """
    Record a paper entry. `exit_target` is the engine's PARTIAL level, not its
    full target — the agent was specified to take one lot and close the whole
    thing there.

    Re-checks _live_autofire_blocked() even though run_cycle() already did —
    this is the actual position-mutating function, and defense-in-depth here
    means any other/future caller can't bypass the safety backstop by
    skipping run_cycle()'s check. Returns None (opens nothing) if blocked.
    """
    block_reason = _live_autofire_blocked()
    if block_reason:
        _log(symbol, "BLOCKED", block_reason)
        logger.error(f"[SAFETY] {symbol}: {block_reason}")
        return None

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
    _mirror_open(pos)
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

    block_reason = _live_autofire_blocked()
    if block_reason:
        # Checked before even calling live_confirmation() — no point
        # burning a broker API call on a signal this agent isn't allowed
        # to act on anyway.
        _log("-", "BLOCKED", block_reason)
        return status()

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
                # today_only: the agent acts only on is_live data, so a
                # walked-back candle would be fetched and then discarded —
                # and enough of those trip the broker's rate limit, which
                # then corrupts reads for the UI too.
                decision = live_confirmation(symbol, "5min", mode, today_only=True)
            except Exception as e:
                _log(symbol, "ERROR", f"{mode}: {e}")
                continue
            time.sleep(INTER_CALL_PAUSE_SECS)  # stay under AngelOne's ~3 req/sec

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
    _mirror_close(pos)


# Tags carried on every mirrored position so the dashboard can say WHAT
# produced a trade, not just that one exists. AGENT is the automation;
# MODEL is the thing that actually made the call.
AGENT_NAME = "intraday_agent"
MODEL_NAME = "math_decision_engine"
MODEL_LABEL = "Option Trade Decision Engine — Live"

# The shared paper book, handed over by backend/app.py at startup.
#
# This used to be `import backend.app as app_mod` inside the mirror. That is
# broken whenever the backend is started the documented way — `python
# backend/app.py` makes that file `__main__`, so importing `backend.app`
# builds a SECOND, independent copy of the module with its own empty
# paper_positions_by_mode (and its own OrderManager, scanner state, ...).
# Every mirrored trade was appended to that phantom copy, which no route
# serves, so agent trades executed correctly and then appeared nowhere.
# Registration removes the module-identity guess entirely.
_paper_book: Optional[dict] = None
# Called with a finished trade so it reaches /api/paper/trades, which is what
# the Trades page reads. Kept separate from the book above because the Live
# page (open positions) and the Trades page (closed history) are fed by two
# different stores.
_persist_closed = None


def set_paper_book(book: dict, persist_closed=None):
    """Register the live paper-position store and closed-trade sink."""
    global _paper_book, _persist_closed
    _paper_book = book
    _persist_closed = persist_closed
    logger.info("Intraday agent bound to the live paper book%s.",
                " + closed-trade history" if persist_closed else "")


def _mirror_open(pos: dict):
    """
    Publish the position to the shared paper book AS SOON AS IT OPENS, so it
    is visible on the Live page while it is running — mirroring only on close
    meant an in-flight agent trade appeared nowhere in the dashboard.

    Keeps a reference on the agent position so the close updates this same
    row in place instead of appending a duplicate. Best-effort throughout: a
    mirroring failure must never affect the agent's own book, which is the
    source of truth.
    """
    if _paper_book is None:
        logger.warning("No paper book registered — agent trade will not appear in the dashboard.")
        return
    try:
        mirror = {
            "id": int(datetime.now().timestamp() * 1000),
            "mode": "test",
            "symbol": pos["option_symbol"],
            "direction": "CALL" if pos["side"] == "call" else "PUT",
            # `strategy` is what the existing Live/Trades tables already
            # render, so the tag has to live there to be visible at all.
            "strategy": f"{MODEL_NAME} ({AGENT_NAME})",
            "agent": AGENT_NAME,
            "model": MODEL_NAME,
            "model_label": MODEL_LABEL,
            "source": "agent",
            "underlying": pos["symbol"],
            "trigger_mode": pos["mode"],
            "entry_premium": pos["entry"],
            "current_premium": pos["entry"],
            "lot_size": pos["qty"],
            "lots": 1,
            "sl": pos["stop"],
            "initial_sl": pos["stop"],
            "target": pos["exit_target"],
            "final_score": (pos.get("confidence") or 0) / 100,
            "status": "OPEN",
            "entry_time": pos["entry_time"],
            "unrealised_pnl": 0,
            "exit_time": None,
            "exit_premium": None,
            "realised_pnl": None,
            "exit_reason": None,
            # Same shape backend/app.py's _tick_monitor_loop uses for its own
            # positions, so the Trades page's JourneyChart renders either kind
            # identically. Seeded with the entry point so a trade that opens
            # and closes within one check_exits() pass still has 2 points.
            "journey": [{
                "ts": pos["entry_time"],
                "option_price": pos["entry"],
                "nifty_price": 0,
                "sl": pos["stop"],
                "unrealised_pnl": 0,
            }],
        }
        _paper_book.setdefault("test", []).append(mirror)
        pos["_mirror"] = mirror
    except Exception as e:
        logger.debug(f"paper-trade open mirror skipped: {e}")


def _mirror_close(pos: dict):
    """Update the already-published row in place with the exit."""
    mirror = pos.get("_mirror")
    if mirror is None:
        return
    try:
        mirror.update({
            "status": "CLOSED",
            "current_premium": pos["exit_price"],
            "exit_premium": pos["exit_price"],
            "exit_time": pos["exit_time"],
            "exit_reason": pos["exit_reason"],
            "pnl": pos["pnl"],
            "realised_pnl": pos["pnl"],
            "unrealised_pnl": 0,
        })
        # Final journey point at the actual exit price/time, so the chart's
        # last point always lands exactly on where the position closed —
        # even a same-cycle open+close still ends up with entry + exit.
        journey = mirror.setdefault("journey", [])
        journey.append({
            "ts": pos["exit_time"],
            "option_price": pos["exit_price"],
            "nifty_price": 0,
            "sl": pos["stop"],
            "unrealised_pnl": 0,
        })
        # Also file it into the closed-trade history the Trades page reads.
        if _persist_closed is not None:
            _persist_closed(dict(mirror))
    except Exception as e:
        logger.debug(f"paper-trade close mirror skipped: {e}")


def _mirror_price(pos: dict):
    """
    Keep the mirrored row's live price/P&L in step while it's open, and
    append a journey point (same shape/cadence contract as backend/app.py's
    _tick_monitor_loop: one point roughly every EXIT_CHECK_INTERVAL_SECS,
    capped at 500) so the Trades page has a real series to chart instead of
    just an entry/exit pair.
    """
    mirror = pos.get("_mirror")
    if mirror is None:
        return
    mirror["current_premium"] = pos.get("current_premium")
    mirror["unrealised_pnl"] = pos.get("unrealised_pnl")
    journey = mirror.setdefault("journey", [])
    journey.append({
        "ts": datetime.now().isoformat(),
        "option_price": pos.get("current_premium"),
        "nifty_price": 0,
        "sl": pos.get("stop"),
        "unrealised_pnl": pos.get("unrealised_pnl", 0),
    })
    if len(journey) > 500:
        del journey[:-500]


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
            _mirror_price(pos)
            if premium >= pos["exit_target"]:
                _close_position(pos, pos["exit_target"], "TARGET")
            elif premium <= pos["stop"]:
                _close_position(pos, pos["stop"], "STOP")

    return status()
