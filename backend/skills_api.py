"""
Toolkit API — the 11 trader skills, served to the dashboard.

Every route calls the SAME module the matching skill in .claude/skills calls.
Nothing here shells out to a skill script and nothing reimplements one, so the
Toolkit page and the `/trader-*` skills can never disagree about a signal, a
limit or a position size. Where the platform already had a route (news, the
intraday scan, the live premarket read) the skill and this blueprint share that
code path too.

Read-only with one deliberate exception: /toolkit/entry makes throttled AngelOne
calls, because that is what looking at a live option candle costs. Nothing in
this file places, modifies or approves an order -- /toolkit/broker builds a
ticket and runs the gate, exactly like the skill, and stops there.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, time as dtime
from pathlib import Path

from flask import Blueprint, jsonify, request

from utils.logger import get_logger

logger = get_logger("toolkit")

toolkit = Blueprint("toolkit", __name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
LIVE_CACHE_CANDIDATES = [r"D:\tmp\td_live_prices.json", "/tmp/td_live_prices.json"]
SQUAREOFF = dtime(15, 25)


def _fail(e: Exception, what: str):
    logger.error(f"toolkit/{what} failed: {e}", exc_info=True)
    return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


def _body() -> dict:
    return request.get_json(silent=True) or {}


# ── limits shared by the risk and broker tools ───────────────────────────────
def _limits():
    from config.risk_profiles import RiskLevel, get_risk_profile
    from config.settings import INITIAL_CAPITAL, MAX_CONCURRENT_POSITIONS, MAX_DAILY_LOSS
    from risk.pretrade import Limits
    try:
        profile = get_risk_profile(RiskLevel(os.getenv("RISK_LEVEL", "medium").lower()))
    except (ValueError, KeyError):
        profile = get_risk_profile(RiskLevel.MEDIUM)
    return Limits.from_config(profile, min_rr=1.0, capital=INITIAL_CAPITAL,
                              max_concurrent=MAX_CONCURRENT_POSITIONS,
                              max_daily_loss=MAX_DAILY_LOSS)


def _book():
    """Today's real book, straight from the live paper store in backend.app."""
    from risk.pretrade import Book
    try:
        from backend.app import paper_positions_by_mode
        rows = list(paper_positions_by_mode.get("test", []))
        return Book.from_paper_book(rows), f"live book, {len(rows)} row(s)"
    except Exception as e:
        return Book(), f"book unavailable ({type(e).__name__}); treated as EMPTY"


# ── 1. scanner ───────────────────────────────────────────────────────────────
@toolkit.route("/api/toolkit/scanner")
def tk_scanner():
    try:
        from data.intraday_bars_store import load_all
        from strategy import intraday_scanner as isc

        frames = load_all()
        if not frames:
            return jsonify({"error": "No cached 5-minute bars. Sync the Intraday Engine first."}), 404

        # The store sanitises symbols into filenames, so the index arrives as
        # NIFTY_I. scan() uses index_symbol to decide which series may NOT set the
        # freshness reference: NIFTY futures print to 15:40 while stocks stop at
        # 15:25, so an unmatched key marks every stock stale.
        index_symbol = next((k for k in frames if k.upper().replace("-", "_") == "NIFTY_I"), "NIFTY-I")
        r = isc.scan(frames, datetime.now(), index_symbol=index_symbol)

        pattern = request.args.get("pattern")
        state = request.args.get("state")
        min_score = int(request.args.get("min_score", 0))
        rows = []
        for p, found in r["setups"].items():
            if pattern and p != pattern:
                continue
            for d in found:
                if state and d.get("state") != state:
                    continue
                if (d.get("score") or 0) < min_score:
                    continue
                rows.append({**d, "pattern": p})
        rows.sort(key=lambda d: (-(d.get("score") or 0), d.get("symbol", "")))

        asof = r["asof_bar"]
        late = bool(asof and asof.time() > isc.LAST_SIGNAL_TIME) if hasattr(asof, "time") else False
        return jsonify({
            "asof_bar": str(asof), "universe": r["universe"], "stale": r["stale"],
            "regime": r["regime"], "counts": r["counts"], "patterns": list(isc.PATTERNS),
            "setups": rows[: int(request.args.get("limit", 40))],
            "past_signal_cutoff": late,
            "cutoff_note": (f"The newest cached bar is past {isc.LAST_SIGNAL_TIME:%H:%M}, the cut-off for "
                            f"opening a fresh setup, so the detectors decline by design."
                            if late else None),
        })
    except Exception as e:
        return _fail(e, "scanner")


# ── 2. news ──────────────────────────────────────────────────────────────────
@toolkit.route("/api/toolkit/news")
def tk_news():
    try:
        from data.news_sentiment import NewsSentimentEngine
        hours = int(request.args.get("hours", 24))
        symbol = (request.args.get("symbol") or "").upper() or None
        arts = NewsSentimentEngine().fetch_all(max_age_hours=hours)
        if symbol:
            arts = [a for a in arts if symbol in (a.symbols or []) or symbol in (a.title or "").upper()]
        if request.args.get("negative_only") == "1":
            arts = [a for a in arts if (a.sentiment_score or 0) < 0]
        arts = sorted(arts, key=lambda x: x.published_at, reverse=True)[: int(request.args.get("limit", 20))]
        scores = [a.sentiment_score for a in arts if a.sentiment_score is not None]
        return jsonify({
            "hours": hours, "symbol": symbol,
            "average_tone": round(sum(scores) / len(scores), 3) if scores else None,
            "articles": [{"title": a.title, "url": a.url, "source": a.source,
                          "published_at": a.published_at.isoformat(), "symbols": a.symbols,
                          "sentiment_label": a.sentiment_label,
                          "sentiment_score": round(a.sentiment_score, 2)} for a in arts],
        })
    except Exception as e:
        return _fail(e, "news")


# ── 3. entry (the only route that spends broker calls) ───────────────────────
@toolkit.route("/api/toolkit/entry", methods=["POST"])
def tk_entry():
    try:
        from strategy.premarket import live_confirmation
        b = _body()
        symbol = (b.get("symbol") or "NIFTY").upper()
        mode = b.get("mode") or "latest"
        # retry_on_refusal stays False, as in the agent's own loop: an extra retry
        # doubles API spend during exactly the rate-limit spike it tries to survive.
        d = live_confirmation(symbol=symbol, timeframe="5min", mode=mode,
                              today_only=True, retry_on_refusal=False)
        if d.get("error"):
            state, detail = "ERROR", d["error"]
        elif not d.get("is_live"):
            state, detail = "STALE", f"candle is from {d.get('session_date')}, not today"
        elif not d.get("tradable"):
            state, detail = "WAIT", ", ".join(d.get("blockers") or []) or "no clear side"
        else:
            state = "SIGNAL"
            detail = (f"{str(d.get('side')).upper()} entry {d.get('entry')} stop {d.get('stop')} "
                      f"exit {d.get('partial')} (R:R taken {d.get('rr_partial')})")
        return jsonify({"state": state, "detail": detail, "decision": d})
    except Exception as e:
        return _fail(e, "entry")


# ── 4. exit ──────────────────────────────────────────────────────────────────
@toolkit.route("/api/toolkit/exit", methods=["POST"])
def tk_exit():
    try:
        from strategy.math_decision_strategy import analyse_option_pair
        b = _body()
        ce, pe = b.get("ce"), b.get("pe")
        if not (isinstance(ce, list) and isinstance(pe, list) and len(ce) == 4 and len(pe) == 4):
            return jsonify({"error": "give ce and pe as [open, high, low, close]"}), 400
        d = analyse_option_pair(tuple(float(x) for x in ce), tuple(float(x) for x in pe))
        return jsonify(d if isinstance(d, dict) else dict(d.__dict__))
    except Exception as e:
        return _fail(e, "exit")


# ── 5. time machine ──────────────────────────────────────────────────────────
@toolkit.route("/api/toolkit/timemachine", methods=["POST"])
def tk_timemachine():
    try:
        from backtest.plan_replay import replay_plan
        b = _body()
        r = replay_plan(str(b.get("symbol", "")).upper(), float(b["entry"]), float(b["stop"]),
                        float(b["exit"]), int(b.get("sessions", 12)))
        return jsonify(r), (404 if r.get("error") else 200)
    except (KeyError, TypeError, ValueError) as e:
        return jsonify({"error": f"give symbol, entry, stop and exit ({e})"}), 400
    except Exception as e:
        return _fail(e, "timemachine")


# ── 6. risk ──────────────────────────────────────────────────────────────────
@toolkit.route("/api/toolkit/risk", methods=["POST"])
def tk_risk():
    try:
        from risk import pretrade
        from risk.sizing import LOT_SIZES
        b = _body()
        symbol = (b.get("symbol") or "NIFTY").upper()
        units = b.get("lot_size") or LOT_SIZES.get(symbol, 65) * int(b.get("lots", 1))
        order = {"symbol": symbol,
                 "option_symbol": b.get("option_symbol") or f"{symbol}-ATM",
                 "entry": b.get("entry"), "stop": b.get("stop"),
                 "partial": b.get("exit"), "lot_size": units}
        book, book_src = _book()
        v = pretrade.evaluate(order=order, book=book, limits=_limits(), now=datetime.now(),
                              is_exit=bool(b.get("is_exit")))
        return jsonify({"order": order, "book": book_src, "verdict": v.to_dict()})
    except Exception as e:
        return _fail(e, "risk")


# ── 7. budget ────────────────────────────────────────────────────────────────
@toolkit.route("/api/toolkit/budget", methods=["POST"])
def tk_budget():
    try:
        from risk.sizing import size_position
        b = _body()
        r = size_position(float(b["entry"]), float(b["stop"]), b.get("symbol") or "NIFTY",
                          b.get("max_loss"), b.get("capital"))
        return jsonify(r)
    except (KeyError, TypeError, ValueError) as e:
        return jsonify({"error": f"give entry and stop ({e})"}), 400
    except Exception as e:
        return _fail(e, "budget")


# ── 8. broker — builds a ticket, runs the gate, and STOPS ───────────────────
@toolkit.route("/api/toolkit/broker", methods=["POST"])
def tk_broker():
    """
    Deliberately incapable: there is no code path in here that reaches a broker.
    The platform's own design is paper-fires / live-stages-an-approval
    (models/approval.py), and a toolkit button that placed orders would bypass it.
    """
    try:
        from risk import pretrade
        from risk.sizing import LOT_SIZES
        b = _body()
        symbol = (b.get("symbol") or "NIFTY").upper()
        lots = int(b.get("lots", 1))
        units = LOT_SIZES.get(symbol, 65) * lots
        order = {"symbol": symbol,
                 "option_symbol": b.get("option_symbol") or f"{symbol}-ATM",
                 "entry": float(b["entry"]), "stop": float(b["stop"]),
                 "partial": float(b["exit"]), "lot_size": units}
        book, book_src = _book()
        v = pretrade.evaluate(order=order, book=book, limits=_limits(), now=datetime.now())
        mode = os.getenv("TRADE_MODE", "paper").lower()
        risk_rs = max(0.0, order["entry"] - order["stop"]) * units
        return jsonify({
            "ticket": order, "lots": lots, "trade_mode": mode, "book": book_src,
            "risk_rupees": round(risk_rs, 2),
            "premium_outlay": round(order["entry"] * units, 2),
            "verdict": v.to_dict(),
            "sent": False,
            "next_step": ("Enter it from the Live page, or POST /api/paper/enter."
                          if mode == "paper" else
                          "Live orders go through the approval queue: the agent stages the signal and "
                          "you release it from the dashboard. Nothing automated may release it."),
        })
    except (KeyError, TypeError, ValueError) as e:
        return jsonify({"error": f"give entry, stop and exit ({e})"}), 400
    except Exception as e:
        return _fail(e, "broker")


# ── 9. alerts ────────────────────────────────────────────────────────────────
@toolkit.route("/api/toolkit/alerts")
def tk_alerts():
    try:
        cache, src, age = {}, "no tick cache found (the collector is not running)", None
        for p in LIVE_CACHE_CANDIDATES:
            if os.path.exists(p):
                age = time.time() - os.path.getmtime(p)
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        cache = json.load(f)
                    src = p
                except (OSError, json.JSONDecodeError) as e:
                    src = f"{p} unreadable: {e}"
                break

        fired, notes = [], []
        if age is not None and age > 90:
            notes.append(f"Tick cache is STALE ({age:.0f}s old). Prices may be old; the collector may be down.")

        watch = request.args.get("watch")
        above = request.args.get("above", type=float)
        below = request.args.get("below", type=float)
        watched = None
        if watch:
            row = cache.get(watch) or cache.get(watch.upper())
            if row:
                px = float(row.get("price") or 0)
                watched = {"symbol": watch, "price": px, "ts": row.get("ts")}
                if above is not None and px >= above:
                    fired.append(f"{watch} {px} is AT OR ABOVE {above}")
                if below is not None and px <= below:
                    fired.append(f"{watch} {px} is AT OR BELOW {below}")
            else:
                notes.append(f"{watch} is not in the tick cache.")

        positions = []
        if request.args.get("positions") == "1":
            from backend.app import paper_positions_by_mode
            for p in paper_positions_by_mode.get("test", []):
                if p.get("status") != "OPEN":
                    continue
                cur, stop = p.get("current_premium"), p.get("sl")
                to_stop = ((cur - stop) / cur * 100) if (cur and stop) else None
                positions.append({"symbol": p.get("symbol"), "direction": p.get("direction"),
                                  "entry": p.get("entry_premium"), "current": cur,
                                  "stop": stop, "target": p.get("target"),
                                  "pct_to_stop": round(to_stop, 2) if to_stop is not None else None})
                if to_stop is not None and to_stop <= 2.0:
                    fired.append(f"{p.get('symbol')} is within {to_stop:.1f}% of its stop")

        warn_min = request.args.get("close_warning", type=int)
        if warn_min:
            now = datetime.now()
            mins = (SQUAREOFF.hour * 60 + SQUAREOFF.minute) - (now.hour * 60 + now.minute)
            if 0 < mins <= warn_min:
                fired.append(f"{mins} minute(s) to square-off at {SQUAREOFF:%H:%M}")
            elif mins <= 0:
                notes.append(f"Square-off time {SQUAREOFF:%H:%M} has passed.")

        return jsonify({"source": src, "age_secs": round(age) if age is not None else None,
                        "stale": bool(age is not None and age > 90),
                        "symbols": sorted(cache.keys()), "watched": watched,
                        "positions": positions, "alerts": fired, "notes": notes})
    except Exception as e:
        return _fail(e, "alerts")


# ── 10. analyst ──────────────────────────────────────────────────────────────
@toolkit.route("/api/toolkit/analyst")
def tk_analyst():
    """
    Results HEADLINES plus the measured price reaction -- not a filings summary.
    There is no structured Indian earnings source this platform can reach: NSE's
    corporate endpoints are Akamai-gated and BSE's announcements API returns 403
    (both measured 2026-11-03), so no revenue or EPS figure is available to quote.
    """
    try:
        from data.news_sentiment import NewsSentimentEngine
        symbol = (request.args.get("symbol") or "").upper() or None
        hours = int(request.args.get("hours", 72))
        days = int(request.args.get("days", 7))

        kw = ("result", "results", "earnings", "profit", "revenue", "quarter",
              "q1", "q2", "q3", "q4", "ebitda", "margin")
        arts = []
        for a in NewsSentimentEngine().fetch_all(max_age_hours=hours):
            text = (a.title or "").lower()
            if not any(k in text for k in kw):
                continue
            if symbol and symbol not in (a.symbols or []) and symbol not in text.upper():
                continue
            arts.append(a)
        arts = sorted(arts, key=lambda x: x.published_at, reverse=True)[: int(request.args.get("limit", 12))]

        reaction = None
        if symbol:
            try:
                from data.eod_store import load_panel
                panel = load_panel(sessions=days + 2)
                sub = panel[panel["symbol"] == symbol].sort_values("date").tail(days + 1)
                if len(sub) >= 2:
                    first, last = float(sub["close"].iloc[0]), float(sub["close"].iloc[-1])
                    reaction = {"sessions": len(sub) - 1, "from": first, "to": last,
                                "change_pct": round(100 * (last - first) / first, 2),
                                "high": float(sub["close"].max()), "low": float(sub["close"].min())}
                else:
                    reaction = {"error": f"{symbol} is not in the cached EOD panel, or has too few sessions."}
            except Exception as e:
                reaction = {"error": f"{type(e).__name__}: {e}"}

        return jsonify({
            "symbol": symbol, "window_hours": hours, "price_reaction": reaction,
            "headlines": [{"title": a.title, "url": a.url, "source": a.source,
                           "published_at": a.published_at.isoformat(),
                           "sentiment_label": a.sentiment_label} for a in arts],
            "limitation": ("No structured filings: NSE corporate endpoints are Akamai-gated and BSE's "
                           "announcements API returns 403, so no revenue or EPS figure is available."),
        })
    except Exception as e:
        return _fail(e, "analyst")


# ── 11. sebi ─────────────────────────────────────────────────────────────────
@toolkit.route("/api/toolkit/sebi")
def tk_sebi():
    try:
        from data.sebi_feed import KINDS, SEBI_RSS, fetch_actions, filter_actions
        items = fetch_actions()
        rows = filter_actions(items, request.args.get("kind"), request.args.get("q"),
                              request.args.get("named_only") == "1",
                              int(request.args.get("limit", 15)))
        return jsonify({"source": SEBI_RSS, "kinds": sorted(KINDS),
                        "total_in_feed": len(items), "items": rows,
                        "limitation": ("Regulatory actions, NOT insider-trade disclosures. India "
                                       "publishes no congressional-style feed, and NSE/BSE insider "
                                       "endpoints are not reachable from a script.")})
    except Exception as e:
        return _fail(e, "sebi")
