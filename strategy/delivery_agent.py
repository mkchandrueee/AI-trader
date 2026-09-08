"""
Delivery / Swing Agent — Scanner picks, double-confirmed by AI Forecast
──────────────────────────────────────────────────────────────────────
Takes the Market Scanner's highest candle-quality rows and keeps only those
where NSE-Neuron's own next-day forecast points the SAME way. Two independent
readings agreeing is the whole point: the scanner reads one completed candle's
shape, the forecast reads a trained sequence model over daily bars. Neither
is a probability of profit, and agreement doesn't make one — it just filters
out the rows where the two disagree.

SUGGEST-ONLY, by design. This never opens a position; it returns picks for a
human to place. That was an explicit choice — the intraday agent auto-enters
because it's a one-lot intraday option with a hard exit, whereas these are
multi-day holdings whose exit isn't defined by the model.

Cost note: forecast_symbol() trains or loads a real per-symbol model. It is
NOT cheap enough to call across a whole scan, so this caps how many rows it
will confirm and caches each result for the trading day — the forecast has a
daily horizon, so recomputing it every poll buys nothing.
"""

from __future__ import annotations

import threading
from datetime import date, datetime
from typing import Optional

from utils.logger import get_logger

logger = get_logger("delivery_agent")

DEFAULT_MIN_CONFIDENCE = 100.0   # "100% confidence" rows, per the spec
DEFAULT_TOP_N = 10               # forecast calls are expensive — see module docstring
DEFAULT_MIN_QTY = 1              # "go with min quantity"

_lock = threading.RLock()
_forecast_cache: dict[tuple[str, str], dict] = {}   # (symbol, iso date) -> forecast summary
_last_run: dict = {"at": None, "picks": [], "checked": 0, "errors": []}


def _forecast_signal(symbol: str, algorithm: str = "lstm") -> Optional[dict]:
    """
    Nearest forecast day's BUY/HOLD/SELL label + confidence for `symbol`,
    cached per trading day. None when the forecast can't be produced (missing
    history, model failure) — the caller drops the row rather than guessing.
    """
    key = (symbol.upper(), date.today().isoformat())
    with _lock:
        if key in _forecast_cache:
            return _forecast_cache[key]

    try:
        from predictions.forecast import forecast_symbol
        result = forecast_symbol(symbol, algorithm=algorithm)
    except Exception as e:
        logger.warning(f"forecast failed for {symbol}: {e}")
        return None

    days = result.get("forecast") or []
    if not days:
        return None
    signal = days[0].get("signal") or {}
    label = (signal.get("label") or "").upper()
    if not label:
        return None

    summary = {
        "label": label,
        "confidence": signal.get("confidence"),
        "regime_direction": signal.get("regime_direction"),
        "forecast_close": days[0].get("close"),
        "algorithm": algorithm,
    }
    with _lock:
        _forecast_cache[key] = summary
    return summary


def _agrees(scanner_is_long: bool, forecast_label: str) -> bool:
    """LONG needs BUY, SHORT needs SELL. HOLD confirms nothing either way."""
    return (scanner_is_long and forecast_label == "BUY") or (not scanner_is_long and forecast_label == "SELL")


def get_confirmed_picks(
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    top_n: int = DEFAULT_TOP_N,
    min_qty: int = DEFAULT_MIN_QTY,
    algorithm: str = "lstm",
) -> dict:
    """
    Scan → forecast → keep only where both agree. Returns every candidate that
    was checked along with why it was kept or dropped, so an empty result is
    distinguishable from a broken one.
    """
    from strategy.market_scanner import scan_market

    scan = scan_market(
        universe="all", min_confidence=min_confidence,
        sort_by="confidence", limit=50,
    )
    if scan.get("message"):
        return {"error": scan["message"], "picks": [], "checked": []}

    # Options rows are intraday instruments with an expiry — not delivery
    # candidates, and NSE-Neuron has no per-contract history to forecast on.
    candidates = [r for r in scan.get("rows", []) if r.get("kind") in ("stock", "index")][:top_n]

    picks, checked, errors = [], [], []
    for row in candidates:
        symbol = row["symbol"]
        fc = _forecast_signal(symbol, algorithm=algorithm)
        if fc is None:
            checked.append({"symbol": symbol, "kept": False, "reason": "no forecast available"})
            errors.append(symbol)
            continue

        side = "LONG" if row["bullish"] else "SHORT"
        if not _agrees(row["bullish"], fc["label"]):
            checked.append({
                "symbol": symbol, "kept": False,
                "reason": f"scanner says {side}, forecast says {fc['label']}",
                "scanner_side": side, "forecast_label": fc["label"],
            })
            continue

        picks.append({
            "symbol": symbol,
            "kind": row["kind"],
            "sector": row.get("industry") or "",
            "side": side,
            "scanner_confidence": row["confidence"],
            "forecast_label": fc["label"],
            "forecast_confidence": fc["confidence"],
            "forecast_close": fc["forecast_close"],
            "close": row["close"],
            "entry": row["entry"],
            "target1": row["target1"],
            "target2": row["target2"],
            "stop_loss": row["stop_loss"],
            "rr": row["rr"],
            "qty": min_qty,
        })
        checked.append({
            "symbol": symbol, "kept": True,
            "reason": f"both agree: {side} / {fc['label']}",
            "scanner_side": side, "forecast_label": fc["label"],
        })

    result = {
        "session_date": scan.get("date"),
        "min_confidence": min_confidence,
        "scanned": scan.get("total_before_filter", 0),
        "qualified": len(scan.get("rows", [])),
        "considered": len(candidates),
        "picks": picks,
        "checked": checked,
        "forecast_errors": errors,
        "generated_at": datetime.now().isoformat(),
    }
    with _lock:
        _last_run.update({"at": result["generated_at"], "picks": picks, "checked": len(checked), "errors": errors})
    logger.info(f"[delivery] {len(picks)} confirmed of {len(candidates)} considered ({len(scan.get('rows', []))} qualified)")
    return result
