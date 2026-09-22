"""
Paper Trading Dashboard
───────────────────────
A simple Flask web app that shows live paper trading status.

Features:
  - Current market regime and price
  - Trade suggestions with ML scores
  - Trade log with P&L
  - System status (models loaded, DB connected, etc.)

Run: python backend/app.py
Open: http://localhost:5050
"""

import os
import sys
import json
import threading
import time
from datetime import datetime, date, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Must run before utils.logger is imported (it grabs a reference to
# sys.stdout for its handler) — see utils/console.py for why.
from utils.console import fix_windows_console_encoding
fix_windows_console_encoding()

from dotenv import load_dotenv
load_dotenv()

import numpy as np
import pandas as pd
from flask import Flask, render_template_string, jsonify, request
from flask_cors import CORS

from database.db import read_sql, get_engine
from features.indicators import compute_all_macro_indicators
from strategy.signal_generator import generate_signals
from strategy.regime_detector import (
    RegimeDetector, get_strategies_for_regime, MarketRegime,
    get_daily_bias, daily_bias_adjustment,
)
from models.predict import Predictor
from models.strategy_models import StrategyPredictor
from backtest.option_resolver import get_nearest_expiry, get_days_to_expiry
from config.settings import (
    WEIGHT_ML_PROBABILITY, WEIGHT_OPTIONS_FLOW, WEIGHT_TECHNICAL_STRENGTH,
    SCORE_THRESHOLD,
)
from utils.logger import get_logger

logger = get_logger("dashboard")

app = Flask(__name__)
CORS(app)  # Allow Next.js dev server on :3000

# ── Global State ──────────────────────────────────────────────────────────────
scanner_enabled = True      # Start/stop toggle for the background scanner
auto_trade_enabled = True   # Auto-enter paper trades when signals fire (vs manual)
_sse_client_count = 0       # Number of active SSE connections (frontend clients)

state = {
    "status": "initializing",
    "last_scan": None,
    "last_price": 0,
    "regime": "UNKNOWN",
    "daily_bias": None,
    "models_loaded": False,
    "strategy_models_loaded": [],
    "db_connected": False,
    "trade_suggestions": [],
    "scan_count": 0,
    "signals_checked": 0,
    "trades_today": 0,
    "scanner_enabled": True,
    "auto_trade_enabled": True,
}

# Cooldown tracker: key=(symbol, direction, strategy) → last suggestion datetime
_suggestion_cooldown: dict = {}
SUGGESTION_COOLDOWN_SECS = 300  # 5 minutes

# Paper positions: keyed by mode ("test" / "live")
paper_positions_by_mode: dict = {"test": [], "live": []}
paper_positions: list = paper_positions_by_mode["test"]  # default alias for tick monitor

# Closed live trade history — persisted to disk so it survives Flask restarts
_closed_trades_by_mode: dict = {"test": [], "live": []}
_PAPER_TRADES_DIR = Path("paper_trades")


def _paper_trades_file(mode: str) -> Path:
    return _PAPER_TRADES_DIR / f"trades_{mode}.jsonl"


def _load_paper_trade_history():
    """Load closed paper trade history from JSONL files on startup."""
    global _closed_trades_by_mode
    _PAPER_TRADES_DIR.mkdir(exist_ok=True)
    for mode in ("test", "live"):
        fpath = _paper_trades_file(mode)
        if fpath.exists():
            try:
                trades = []
                for line in fpath.read_text().splitlines():
                    line = line.strip()
                    if line:
                        trades.append(json.loads(line))
                _closed_trades_by_mode[mode] = trades
                logger.info(f"Loaded {len(trades)} closed {mode} paper trades from history")
            except Exception as e:
                logger.warning(f"Failed to load paper trade history ({mode}): {e}")


def _persist_closed_trade(pos: dict):
    """Append a closed trade (with journey) to the JSONL file for its mode."""
    mode = pos.get("mode", "test")
    _closed_trades_by_mode.setdefault(mode, []).append(pos)
    fpath = _paper_trades_file(mode)
    try:
        with open(fpath, "a") as f:
            f.write(json.dumps(pos, default=str) + "\n")
    except Exception as e:
        logger.warning(f"Failed to persist trade {pos.get('id')}: {e}")

    # ── Consecutive SL tracking (ported from backtest 2026-04-15) ────────
    # Count consecutive SL_HIT exits. After 2 in a row, pause new entries
    # for 30 minutes. Any non-SL exit (trailing, target, RL, timeout, manual)
    # resets the counter.
    global _consecutive_sl_hits, _sl_pause_until
    reason = pos.get("exit_reason", "")
    if reason == "SL_HIT":
        _consecutive_sl_hits += 1
        if _consecutive_sl_hits >= 2:
            _sl_pause_until = datetime.now() + timedelta(minutes=30)
            logger.warning(
                f"SL COOLDOWN ACTIVATED: {_consecutive_sl_hits} consecutive SL hits — "
                f"pausing entries until {_sl_pause_until.strftime('%H:%M')}"
            )
    else:
        if _consecutive_sl_hits > 0:
            logger.debug(f"SL streak broken ({reason}): counter reset")
        _consecutive_sl_hits = 0


def _get_mode_positions(mode: str = None) -> list:
    """Return positions list for given mode (from request arg or explicit)."""
    if mode is None:
        mode = request.args.get("mode", "test") if request else "test"
    if mode not in paper_positions_by_mode:
        mode = "test"
    return paper_positions_by_mode[mode]


BREAKEVEN_AFTER_MIN   = 15    # minutes in any profit → move SL to entry
BREAKEVEN_MIN_PROFIT  = 0.02  # must be at least +2% in profit to trigger BE
REGIME_CHECK_INTERVAL = 10    # seconds between regime checks per position (was 30)


def _auto_enter_position(trade: dict, trade_mode: str = "test"):
    """Auto-enter a paper position from a signal suggestion (no HTTP request needed)."""
    positions = paper_positions_by_mode.get(trade_mode, paper_positions_by_mode["test"])

    # Skip if already have an open position for this symbol
    if any(p["symbol"] == trade["symbol"] and p["status"] == "OPEN" for p in positions):
        logger.debug(f"Auto-trade skipped: already have open position for {trade['symbol']}")
        return

    entry_premium = trade.get("entry_premium")
    if not entry_premium or entry_premium <= 0:
        logger.debug(f"Auto-trade skipped: no live premium available for {trade['symbol']}")
        return

    ep = round(float(entry_premium), 2)
    sl_pct = trade.get("sl_pct", INITIAL_SL_PCT)
    target_pct = trade.get("target_pct", TGT_PCT)
    final_score = trade.get("final_score", 0.5)
    initial_sl = round(ep * (1 - sl_pct), 2)
    lots = _lots_for_score(final_score)
    now_dt = datetime.now()
    position = {
        "id": int(now_dt.timestamp() * 1000),
        "entry_time": now_dt.strftime("%H:%M:%S"),
        "entry_time_dt": now_dt.isoformat(),   # full datetime for BE timer
        "symbol": trade["symbol"],
        "direction": trade["direction"],
        "strategy": trade.get("strategy", ""),
        "entry_premium": ep,
        "sl": initial_sl,
        "initial_sl": initial_sl,
        "target": round(ep * (1 + target_pct), 2),
        "max_premium": ep,
        "trailing_active": False,
        "breakeven_locked": False,             # True once SL moved to entry
        "first_profit_time": None,             # iso ts when trade first went green
        "last_regime_check": now_dt.isoformat(),
        "lot_size": LOT_SIZE * lots,
        "ml_prob": trade.get("ml_prob", 0.5),
        "final_score": final_score,
        "index_price": trade.get("index_price", 0),
        "expiry": trade.get("expiry", ""),
        "status": "OPEN",
        "current_premium": ep,
        "unrealised_pnl": 0.0,
        "exit_time": None,
        "exit_premium": None,
        "realised_pnl": None,
        "exit_reason": None,
        "mode": trade_mode,
        "auto_entered": True,
        "journey": [],   # [{ts, option_price, nifty_price}] — populated by tick monitor
    }
    positions.append(position)
    logger.info(
        f"AUTO-TRADE [{trade_mode.upper()}]: {trade['direction']} {trade['symbol']} "
        f"@ ₹{ep} | SL=₹{initial_sl} TGT=₹{position['target']} | {lots} lot(s) (score={final_score:.3f})"
    )
    _ensure_tick_monitor()
    _ensure_collector()

LOT_SIZE = 65
MAX_LOTS = 3                 # Cap lot multiplier at 3 lots

def _lots_for_score(final_score: float) -> int:
    """Dynamic lot sizing based on signal strength."""
    if final_score >= 0.80:
        return 3
    elif final_score >= 0.70:
        return 2
    return 1

INITIAL_SL_PCT = 0.15       # Initial SL at 15% below entry
TRAIL_ACTIVATE_PCT = 0.10   # Start trailing once profit > 10%
TRAIL_FACTOR = 0.50         # Trail SL at 50% of max profit
TGT_PCT = 0.50              # Target at 50% above entry
COMMISSION = 40.0
LIVE_CACHE_FILE = "/tmp/td_live_prices.json"

# Background processes
_tick_monitor_thread = None
_collector_process = None
_collector_spawned_at = 0.0  # time.time() of the last Popen -- see _ensure_collector's startup-grace guard
_tick_monitor_rest_ts: dict = {}  # symbol -> last REST fallback timestamp

predictor = Predictor()
strategy_predictor = StrategyPredictor()
regime_detector = RegimeDetector()

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AI Trader - Paper Trading</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif; background: #0f1117; color: #e1e4e8; }
        .container { max-width: 1200px; margin: 0 auto; padding: 20px; }
        h1 { color: #58a6ff; margin-bottom: 5px; font-size: 24px; }
        .subtitle { color: #8b949e; margin-bottom: 20px; font-size: 14px; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; margin-bottom: 20px; }
        .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; }
        .card h3 { color: #8b949e; font-size: 12px; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 8px; }
        .card .value { font-size: 28px; font-weight: 700; }
        .green { color: #3fb950; }
        .red { color: #f85149; }
        .yellow { color: #d29922; }
        .blue { color: #58a6ff; }
        .badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: 600; }
        .badge-green { background: #0d1117; border: 1px solid #3fb950; color: #3fb950; }
        .badge-red { background: #0d1117; border: 1px solid #f85149; color: #f85149; }
        .badge-yellow { background: #0d1117; border: 1px solid #d29922; color: #d29922; }
        .badge-blue { background: #0d1117; border: 1px solid #58a6ff; color: #58a6ff; }
        table { width: 100%; border-collapse: collapse; margin-top: 10px; }
        th { text-align: left; padding: 8px 12px; border-bottom: 2px solid #30363d; color: #8b949e; font-size: 12px; text-transform: uppercase; }
        td { padding: 8px 12px; border-bottom: 1px solid #21262d; font-size: 13px; }
        tr:hover { background: #1c2128; }
        .status-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 6px; }
        .dot-green { background: #3fb950; }
        .dot-red { background: #f85149; }
        .dot-yellow { background: #d29922; }
        .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
        .paper-badge { background: #d29922; color: #0d1117; padding: 4px 12px; border-radius: 4px; font-weight: 700; font-size: 12px; }
        .refresh-info { color: #484f58; font-size: 12px; }
        .pnl-positive { color: #3fb950; font-weight: 600; }
        .pnl-negative { color: #f85149; font-weight: 600; }
    </style>
    <script>
        function refreshData() {
            fetch('/api/state')
                .then(r => r.json())
                .then(data => {
                    document.getElementById('status').innerHTML = data.status === 'scanning' ?
                        '<span class="dot-green status-dot"></span>Live' :
                        '<span class="dot-yellow status-dot"></span>' + data.status;
                    document.getElementById('price').textContent = data.last_price ? '₹' + data.last_price.toLocaleString('en-IN', {maximumFractionDigits: 1}) : '--';
                    document.getElementById('regime').textContent = data.regime;
                    document.getElementById('regime').className = 'value ' +
                        (data.regime.includes('BULL') ? 'green' : data.regime.includes('BEAR') ? 'red' : 'yellow');
                    document.getElementById('scans').textContent = data.scan_count;
                    document.getElementById('signals').textContent = data.signals_checked;
                    document.getElementById('trades-today').textContent = data.trades_today;
                    document.getElementById('last-scan').textContent = data.last_scan || '--';
                    document.getElementById('models').innerHTML =
                        (data.models_loaded ? '<span class="badge badge-green">ML Loaded</span> ' : '<span class="badge badge-red">No ML</span> ') +
                        data.strategy_models_loaded.map(s => '<span class="badge badge-blue">' + s + '</span>').join(' ');

                    // Trade table
                    let html = '';
                    data.trade_suggestions.slice().reverse().forEach(t => {
                        const pnlClass = t.estimated_pnl >= 0 ? 'pnl-positive' : 'pnl-negative';
                        html += `<tr>
                            <td>${t.time || '--'}</td>
                            <td><strong>${t.symbol}</strong></td>
                            <td><span class="badge ${t.direction === 'CALL' ? 'badge-green' : 'badge-red'}">${t.direction}</span></td>
                            <td>${t.strategy}</td>
                            <td>₹${t.entry_premium || '--'}</td>
                            <td>${t.expiry || '--'} (${t.dte}d)</td>
                            <td>${(t.ml_prob * 100).toFixed(0)}%</td>
                            <td>${(t.strat_prob * 100).toFixed(0)}%</td>
                            <td>${(t.final_score * 100).toFixed(0)}%</td>
                            <td>${t.regime}</td>
                        </tr>`;
                    });
                    document.getElementById('trades-body').innerHTML = html || '<tr><td colspan="10" style="text-align:center;color:#484f58">No trade suggestions yet. Waiting for signals...</td></tr>';
                });
        }
        setInterval(refreshData, 3000);
        refreshData();
    </script>
</head>
<body>
    <div class="container">
        <div class="nav" style="margin-bottom:16px">
            <a href="/" style="color:#58a6ff;text-decoration:none;margin-right:16px;font-size:14px;font-weight:bold">Live Paper Trading</a>
            <a href="/replay" style="color:#58a6ff;text-decoration:none;font-size:14px">Replay Simulation →</a>
        </div>
        <div class="header">
            <div>
                <h1>AI Trader Dashboard</h1>
                <div class="subtitle">NIFTY Options Paper Trading System</div>
            </div>
            <div>
                <span class="paper-badge">PAPER MODE</span>
                <div class="refresh-info" style="margin-top:4px">Auto-refreshes every 3s</div>
            </div>
        </div>

        <div class="grid">
            <div class="card">
                <h3>System Status</h3>
                <div class="value" id="status"><span class="dot-yellow status-dot"></span>Starting...</div>
            </div>
            <div class="card">
                <h3>NIFTY Index Price</h3>
                <div class="value blue" id="price">--</div>
            </div>
            <div class="card">
                <h3>Market Regime</h3>
                <div class="value yellow" id="regime">UNKNOWN</div>
            </div>
            <div class="card">
                <h3>Trades Today</h3>
                <div class="value green" id="trades-today">0</div>
            </div>
        </div>

        <div class="grid">
            <div class="card">
                <h3>Scan Count</h3>
                <div class="value" id="scans">0</div>
            </div>
            <div class="card">
                <h3>Signals Checked</h3>
                <div class="value" id="signals">0</div>
            </div>
            <div class="card">
                <h3>Last Scan</h3>
                <div class="value" style="font-size:16px" id="last-scan">--</div>
            </div>
            <div class="card">
                <h3>Models</h3>
                <div id="models" style="margin-top:4px"></div>
            </div>
        </div>

        <div class="card" style="margin-top:16px">
            <h3>Trade Suggestions</h3>
            <table>
                <thead>
                    <tr>
                        <th>Time</th>
                        <th>Contract</th>
                        <th>Direction</th>
                        <th>Strategy</th>
                        <th>Premium</th>
                        <th>Expiry</th>
                        <th>ML Prob</th>
                        <th>Strat ML</th>
                        <th>Score</th>
                        <th>Regime</th>
                    </tr>
                </thead>
                <tbody id="trades-body">
                    <tr><td colspan="10" style="text-align:center;color:#484f58">No trade suggestions yet. Waiting for signals...</td></tr>
                </tbody>
            </table>
        </div>
    </div>
</body>
</html>
"""


def initialize():
    """Load models and verify DB connection."""
    global state
    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(__import__('sqlalchemy').text("SELECT 1"))
        state["db_connected"] = True
    except Exception as e:
        logger.error(f"DB connection failed: {e}")

    predictor.load()
    state["models_loaded"] = predictor.is_loaded

    strategy_predictor.load()
    state["strategy_models_loaded"] = strategy_predictor.available_strategies

    state["status"] = "ready"
    _load_paper_trade_history()
    logger.info("Dashboard initialized.")

    # ── Startup backfill: fill today's missing candles from 9:15 to now ──────
    # Runs in a background thread so the server starts immediately.
    def _startup_backfill():
        try:
            if not _is_market_hours():
                return
            today = date.today()
            day_start = datetime(today.year, today.month, today.day, 9, 15, 0)
            end_dt = datetime.now()

            # Find the first candle we have today (could be None if totally missing)
            first = read_sql(
                "SELECT MIN(timestamp) as first_bar FROM minute_candles "
                "WHERE symbol = 'NIFTY-I' AND timestamp::date = :d",
                {"d": today.isoformat()}
            )
            first_bar = None
            if not first.empty and first.iloc[0]["first_bar"] is not None:
                first_bar = pd.to_datetime(first.iloc[0]["first_bar"], utc=True)
                first_bar_ist = first_bar.tz_convert("Asia/Kolkata").replace(tzinfo=None)
                # If we already have data from 9:15, skip
                if first_bar_ist.hour == 9 and first_bar_ist.minute <= 16:
                    logger.info("Startup backfill: candles already start from 9:15, skipping.")
                    return

            from data.market_data_adapter import MarketDataAdapter
            from database.db import upsert_candles as _upsert
            from backtest.option_resolver import get_nearest_expiry

            td_bf = MarketDataAdapter()
            if not td_bf.authenticate():
                logger.warning("Startup backfill: AngelOne auth failed, skipping.")
                return

            logger.info(f"Startup backfill: fetching NIFTY-I candles from 09:15 to {end_dt.strftime('%H:%M')}...")
            bars = td_bf.fetch_historical_bars("NIFTY-I", day_start, end_dt, interval="1min")
            if not bars.empty:
                for col in ["vwap", "oi"]:
                    if col not in bars.columns:
                        bars[col] = 0
                bars = bars[["timestamp", "symbol", "open", "high", "low", "close", "volume", "vwap", "oi"]]
                _upsert(bars)
                logger.info(f"Startup backfill: upserted {len(bars)} NIFTY-I candles.")

            # Also backfill ATM option candles for current strike range
            import time as _time
            nifty_close = 0
            if not bars.empty:
                nifty_close = float(bars.iloc[-1]["close"])
            if nifty_close > 0:
                atm = round(nifty_close / 50) * 50
                expiry = get_nearest_expiry(today)
                if expiry:
                    exp_code = expiry.strftime("%y%m%d")
                    for delta in range(-3, 4):
                        strike = atm + delta * 50
                        for opt in ["CE", "PE"]:
                            sym = f"NIFTY{exp_code}{strike}{opt}"
                            chk = read_sql(
                                "SELECT COUNT(*) as cnt FROM minute_candles WHERE symbol=:s AND timestamp::date=:d",
                                {"s": sym, "d": today.isoformat()}
                            )
                            cnt = int(chk.iloc[0]["cnt"]) if not chk.empty else 0
                            # Only fetch if less than 50% expected bars
                            expected = max(1, int((end_dt - day_start).total_seconds() / 60))
                            if cnt < expected * 0.5:
                                try:
                                    ob = td_bf.fetch_historical_bars(sym, day_start, end_dt, interval="1min")
                                    if not ob.empty:
                                        for col in ["vwap", "oi"]:
                                            if col not in ob.columns:
                                                ob[col] = 0
                                        ob = ob[["timestamp", "symbol", "open", "high", "low", "close", "volume", "vwap", "oi"]]
                                        _upsert(ob)
                                        logger.info(f"Startup backfill {sym}: {len(ob)} bars")
                                    _time.sleep(1.1)
                                except Exception as e:
                                    logger.warning(f"Startup backfill {sym}: {e}")
            # ── Tick backfill (NIFTY-I only — 5-day REST window) ──────────────
            # Mirrors the candle backfill: fills tick gaps from 09:15 onwards
            # so the live chart and micro features have continuous data.
            try:
                _backfill_ticks_if_stale()
            except Exception as e:
                logger.warning(f"Startup tick backfill error: {e}")

            logger.info("Startup backfill complete.")
        except Exception as e:
            logger.warning(f"Startup backfill error: {e}")

    import threading as _th
    _th.Thread(target=_startup_backfill, daemon=True, name="startup-backfill").start()


def _is_market_hours() -> bool:
    """Return True if current IST time is within market hours 9:15–15:30."""
    now_ist = datetime.now()
    t = now_ist.time()
    from datetime import time as dtime
    return dtime(9, 15) <= t <= dtime(15, 31)


# How long the tick stream can be silent before we trigger a REST backfill
# (the WebSocket sends ~150-180 ticks/min during a normal session, so >120s
# gap means a real disconnect, not just illiquid quiet)
TICK_STALENESS_THRESHOLD_SECS = 120

# Throttle: never run the tick backfill more than once every N seconds
_last_tick_backfill_run = 0.0
_TICK_BACKFILL_MIN_INTERVAL = 60.0

# Consecutive SL tracking (ported from backtest 2026-04-15). After 2
# consecutive SL hits, pause new entries for 30 minutes. Resets on any
# non-SL exit (trailing, target, RL, timeout).
_consecutive_sl_hits: int = 0
_sl_pause_until: datetime = datetime.min

# Per-(strategy, direction) entry cooldown to prevent ladder entries on the
# same exhausted move. Apr 28 fired 5 bearish_momentum PUTs within 65 min,
# 4 of 5 lost. Block subsequent entries of the same (strategy, direction)
# for STRATEGY_DIRECTION_COOLDOWN_SECS after one fires, regardless of strike.
STRATEGY_DIRECTION_COOLDOWN_SECS = 900  # 15 minutes
_last_entry_by_strat_dir: dict[tuple[str, str], datetime] = {}


def _detect_tick_gaps_today(min_gap_secs: int = TICK_STALENESS_THRESHOLD_SECS) -> list:
    """
    Find time windows in today's NIFTY-I tick stream where no ticks exist
    for >= min_gap_secs. Returns a list of (start_dt, end_dt) tuples in IST
    naive datetimes. Includes the trailing gap (last tick → now) if stale.

    Used by _backfill_ticks_if_stale() so it fixes BOTH leading/internal
    gaps (09:15 → first observed tick) AND any subsequent dropouts.
    """
    today = date.today()
    session_start = datetime(today.year, today.month, today.day, 9, 15, 0)
    now_ist = datetime.now()
    if now_ist < session_start:
        return []

    df = read_sql(
        "SELECT timestamp FROM tick_data WHERE symbol = 'NIFTY-I' "
        "AND timestamp::date = :d ORDER BY timestamp",
        {"d": today.isoformat()},
    )

    if df.empty:
        return [(session_start, now_ist)]

    # Normalize to naive IST (DB column is TIMESTAMPTZ; convert)
    ts_series = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    ticks = ts_series.tolist()

    gaps: list[tuple[datetime, datetime]] = []

    # Leading gap: 09:15 → first tick
    if (ticks[0] - session_start).total_seconds() >= min_gap_secs:
        gaps.append((session_start, ticks[0]))

    # Internal gaps: any consecutive pair >= min_gap_secs apart
    for i in range(1, len(ticks)):
        delta = (ticks[i] - ticks[i - 1]).total_seconds()
        if delta >= min_gap_secs:
            gaps.append((ticks[i - 1], ticks[i]))

    # Trailing gap: last tick → now
    if (now_ist - ticks[-1]).total_seconds() >= min_gap_secs:
        gaps.append((ticks[-1], now_ist))

    return gaps


def _backfill_ticks_if_stale():
    """
    During market hours, scan today's NIFTY-I tick coverage for gaps and
    fill each one via AngelOne REST `getticks`. Idempotent: deletes the
    affected window first to dedupe with anything the websocket has
    streamed in parallel.

    Why mirror this from the candle backfill rather than relying on the
    websocket alone:
      • AngelOne WebSocket drops several times per session (Connection
        timed out → 5-15 minute reconnect gap), and on 2026-04-08 produced
        long sparse intervals in the live tick chart.
      • REST `getticks` covers the last 5 days at full bid/ask/OI fidelity,
        so any gap inside the 5-day window can be silently patched.
      • EOD scripts also patch gaps, but waiting until 16:00 means the
        scoring loop and the live chart see incomplete data all day.

    Throttled to once per minute (the scanner runs every 30s) — gaps don't
    appear that fast.
    """
    global _last_tick_backfill_run
    if not _is_market_hours():
        return
    import time as _t
    now = _t.time()
    if now - _last_tick_backfill_run < _TICK_BACKFILL_MIN_INTERVAL:
        return

    try:
        gaps = _detect_tick_gaps_today()
        if not gaps:
            _last_tick_backfill_run = now
            return

        from data.market_data_adapter import MarketDataAdapter
        from sqlalchemy import text
        from database.db import engine as _engine

        td_bf = MarketDataAdapter()
        if not td_bf.authenticate():
            return

        # Pad each gap by 30s on both sides so the REST window overlaps
        # with whatever the websocket gave us — the DELETE+INSERT is
        # bounded so this is safe and idempotent.
        total_inserted = 0
        for from_dt, to_dt in gaps:
            from_dt_pad = from_dt - timedelta(seconds=30)
            to_dt_pad = to_dt + timedelta(seconds=30)
            logger.info(
                f"Tick backfill: filling gap "
                f"{from_dt.strftime('%H:%M:%S')} → {to_dt.strftime('%H:%M:%S')} "
                f"({(to_dt - from_dt).total_seconds():.0f}s)"
            )
            try:
                ticks = td_bf.fetch_historical_ticks("NIFTY-I", start=from_dt_pad, end=to_dt_pad)
            except Exception as e:
                logger.warning(f"Tick backfill REST failed for gap: {e}")
                continue
            if ticks.empty:
                continue

            ticks = ticks.copy()
            ticks["symbol"] = "NIFTY-I"
            required = ["timestamp", "symbol", "price", "volume", "oi",
                        "bid_price", "ask_price", "bid_qty", "ask_qty"]
            for col in required:
                if col not in ticks.columns:
                    ticks[col] = ticks.get("price", 0) if col in ("bid_price", "ask_price") else 0
            ticks = ticks[required]
            ticks["timestamp"] = pd.to_datetime(ticks["timestamp"])

            with _engine.begin() as conn:
                conn.execute(
                    text("DELETE FROM tick_data "
                         "WHERE symbol = 'NIFTY-I' "
                         "AND timestamp >= :from_ts AND timestamp <= :to_ts"),
                    {"from_ts": from_dt_pad, "to_ts": to_dt_pad},
                )
                ticks.to_sql("tick_data", conn, if_exists="append", index=False,
                             method=None, chunksize=2000)
            total_inserted += len(ticks)
            # Respect AngelOne REST rate limit (1 req/sec)
            _t.sleep(1.1)

        _last_tick_backfill_run = now
        if total_inserted:
            logger.info(f"Tick backfill: inserted {total_inserted} NIFTY-I ticks across {len(gaps)} gap(s).")
    except Exception as e:
        logger.warning(f"Tick backfill failed: {e}")


def _backfill_candles_if_stale():
    """
    During market hours:
    1. If NIFTY-I candles are >2 min old, fetch fresh bars from REST.
    2. If the current ATM option has no candles today, fetch its full-day bars.
       This handles the case where NIFTY moves significantly after market open
       and the new ATM strike was never subscribed by the tick collector.
    """
    if not _is_market_hours():
        return
    try:
        stale = read_sql(
            "SELECT MAX(timestamp) as latest FROM minute_candles WHERE symbol = 'NIFTY-I'"
        )
        if stale.empty or stale.iloc[0]["latest"] is None:
            return
        latest_ts = pd.to_datetime(stale.iloc[0]["latest"], utc=True)
        age_seconds = (pd.Timestamp.now(tz="UTC") - latest_ts).total_seconds()

        from data.market_data_adapter import MarketDataAdapter
        from database.db import upsert_candles
        from backtest.option_resolver import get_nearest_expiry

        need_td = age_seconds >= 120
        # Also check if current ATM options are missing
        nifty_close = state.get("last_price", 0)
        atm_missing_syms = []
        if nifty_close > 0:
            atm = round(nifty_close / 50) * 50
            today = date.today()
            expiry = get_nearest_expiry(today)
            if expiry:
                exp_code = expiry.strftime("%y%m%d")
                for delta in range(-3, 4):
                    strike = atm + delta * 50
                    for opt in ["CE", "PE"]:
                        sym = f"NIFTY{exp_code}{strike}{opt}"
                        chk = read_sql(
                            "SELECT 1 FROM minute_candles WHERE symbol = :s AND timestamp::date = :d LIMIT 1",
                            {"s": sym, "d": today.isoformat()}
                        )
                        if chk.empty:
                            atm_missing_syms.append(sym)

        if not need_td and not atm_missing_syms:
            return

        td = MarketDataAdapter()
        if not td.authenticate():
            return

        today = date.today()
        day_start = datetime(today.year, today.month, today.day, 9, 15, 0)
        end_dt = datetime.now()

        # 1. Top-up NIFTY-I if stale
        if need_td:
            start_dt = latest_ts.tz_convert("Asia/Kolkata").replace(tzinfo=None)
            bars = td.fetch_historical_bars("NIFTY-I", start_dt, end_dt, interval="1min")
            if not bars.empty:
                for col in ["vwap", "oi"]:
                    if col not in bars.columns:
                        bars[col] = 0
                bars = bars[["timestamp", "symbol", "open", "high", "low", "close", "volume", "vwap", "oi"]]
                inserted = upsert_candles(bars)
                if inserted:
                    logger.info(f"Auto-backfill NIFTY-I: {inserted} new candles (was {age_seconds:.0f}s stale)")

        # 2. Fetch missing ATM option candles
        if atm_missing_syms:
            logger.info(f"Auto-backfill: fetching {len(atm_missing_syms)} missing ATM option symbols")
            import time as _time
            for sym in atm_missing_syms:
                try:
                    bars = td.fetch_historical_bars(sym, day_start, end_dt, interval="1min")
                    if not bars.empty:
                        for col in ["vwap", "oi"]:
                            if col not in bars.columns:
                                bars[col] = 0
                        bars = bars[["timestamp", "symbol", "open", "high", "low", "close", "volume", "vwap", "oi"]]
                        upsert_candles(bars)
                        logger.info(f"Auto-backfill {sym}: {len(bars)} bars")
                    _time.sleep(1.1)  # respect rate limit
                except Exception as e:
                    logger.warning(f"Auto-backfill {sym} failed: {e}")

    except Exception as e:
        logger.warning(f"Backfill check failed: {e}")


def scan_market():
    """Run one scan cycle — compute features, check signals, score trades."""
    global state

    # Only scan during market hours; clear suggestions outside market hours
    if not _is_market_hours():
        state["trade_suggestions"] = []
        state["status"] = "idle"
        return

    state["status"] = "scanning"

    try:
        # ── Expire stale suggestions (older than cooldown window) ─────────
        now_ts = datetime.now()
        def _suggestion_age_secs(t):
            try:
                ts = datetime.strptime(t.get("time", "00:00:00"), "%H:%M:%S")
                ts = ts.replace(year=now_ts.year, month=now_ts.month, day=now_ts.day)
                if ts > now_ts:
                    ts -= timedelta(days=1)
                return (now_ts - ts).total_seconds()
            except Exception:
                return 9999
        state["trade_suggestions"] = [
            t for t in state["trade_suggestions"]
            if _suggestion_age_secs(t) < SUGGESTION_COOLDOWN_SECS
        ]

        # Auto-backfill if DB is stale during market hours
        _backfill_candles_if_stale()
        _backfill_ticks_if_stale()

        # Load latest 300 candles
        df = read_sql(
            "SELECT timestamp, symbol, open, high, low, close, volume, vwap, oi "
            "FROM minute_candles WHERE symbol = 'NIFTY-I' "
            "ORDER BY timestamp DESC LIMIT 300"
        )
        if df.empty or len(df) < 250:
            return

        df = df.sort_values("timestamp").reset_index(drop=True)
        df["timestamp"] = pd.to_datetime(df["timestamp"])

        # Compute features
        featured = compute_all_macro_indicators(df)
        if featured.empty:
            return

        latest = featured.iloc[-1].to_dict()

        # ── Compute live micro features from last ~120s of NIFTY-I ticks ──────
        # Used as a 30% blend with the macro model in directional scoring below.
        # Falls back to None if bid/ask coverage is too thin (we don't force a
        # stale value into the model).
        micro_latest = None
        try:
            tick_df = read_sql(
                "SELECT timestamp, symbol, price, volume, bid_price, ask_price, "
                "bid_qty, ask_qty FROM tick_data WHERE symbol = 'NIFTY-I' "
                "AND bid_price > 0 AND ask_price > 0 "
                "ORDER BY timestamp DESC LIMIT 200"
            )
            if not tick_df.empty and len(tick_df) >= 30:
                tick_df = tick_df.sort_values("timestamp").reset_index(drop=True)
                from features.micro_features import compute_micro_features
                micro_df = compute_micro_features(tick_df, window_seconds=30)
                if not micro_df.empty:
                    micro_latest = micro_df.iloc[-1].to_dict()
        except Exception as e:
            logger.debug(f"micro feature compute skipped: {e}")
        state["last_price"] = float(latest.get("close", 0))
        state["last_scan"] = datetime.now().strftime("%H:%M:%S")
        state["scan_count"] += 1

        # Regime
        regime_window = df.tail(100)[["open", "high", "low", "close", "volume"]].copy()
        regime = regime_detector.detect(regime_window)
        state["regime"] = regime.value
        regime_strategies = get_strategies_for_regime(regime)

        # Daily bias — previous day's floor-pivot lean (see regime_detector.py)
        daily_bias = get_daily_bias("NIFTY-I")
        state["daily_bias"] = daily_bias

        # Signals
        signals = generate_signals(latest, "NIFTY-I")
        state["signals_checked"] += len(signals) if signals else 0

        if not signals:
            return

        # ── Time-of-day gate (added 2026-04-15, ported from backtest) ─────
        # Backtest enforces these windows via SKIP_FIRST_MIN / SKIP_LAST_MIN /
        # AFTERNOON_CUT from the active risk profile. Live was missing them
        # entirely, letting afternoon entries slip through that backtest
        # would have rejected. MEDIUM profile: afternoon_cut=210 = 12:45 IST.
        from config.risk_profiles import get_risk_profile as _get_rp_t, RiskLevel as _RL_t
        _prof = _get_rp_t(_RL_t.MEDIUM)
        now_ist = datetime.now()
        minutes_from_open = max(0, now_ist.hour * 60 + now_ist.minute - 555)  # 9:15 IST = 555 min
        if minutes_from_open < _prof.skip_first_min:
            return
        if minutes_from_open > (375 - _prof.skip_last_min):
            return
        if minutes_from_open > _prof.afternoon_cut:
            logger.info(
                f"SKIP all signals: past afternoon_cut "
                f"({minutes_from_open}min from open > {_prof.afternoon_cut}min)"
            )
            return

        # ── Consecutive-SL cooldown (added 2026-04-15, ported from backtest) ─
        # After 2 SL hits in a row, pause new entries for 30 minutes. Resets
        # on any non-SL exit. Prevents the 4-SL-cluster pattern from today.
        global _consecutive_sl_hits, _sl_pause_until
        if datetime.now() < _sl_pause_until:
            remaining = int((_sl_pause_until - datetime.now()).total_seconds() / 60)
            logger.info(f"SKIP all signals: SL cooldown active ({remaining}min remaining)")
            return

        today = date.today()
        expiry = get_nearest_expiry(today)
        dte = get_days_to_expiry(today, expiry) if expiry else 0

        for sig in signals:
            ml_prob = 0.5
            if predictor.is_loaded:
                p = predictor.predict_macro(latest)
                if p is not None:
                    ml_prob = p
                # Micro-model blend DISABLED 2026-04-15 after live-vs-backtest
                # divergence investigation:
                #   - Backtest uses macro-only ml_prob
                #   - Live was blending 70% macro + 30% micro
                #   - micro_model AUC = 0.50 (random) → adds NOISE, not signal
                #   - On 2026-04-15 11:15 trade: macro=0.407 (score 0.74, reject)
                #     but blended=0.358 (score 0.76, execute) — the 0.05 shift
                #     across the 0.75 floor is pure noise firing trades the
                #     backtest correctly rejects. Three consecutive losing days
                #     (Apr 9/10/15) traced to this.
                # The micro_model stays loaded but is no longer blended into
                # directional scoring until its AUC > 0.55.

            strat_prob = strategy_predictor.predict(sig.strategy, latest) or 0.5
            # Strategy model outputs 0.003–0.11 due to 97% negative class imbalance.
            # Threshold 0.02 ≈ top 50% of model outputs — blocks clearly weak setups only.
            if strat_prob < 0.02:
                continue

            # For PUT signals, use inverted ML prob as directional confidence
            # (high ml_prob = bullish → low PUT confidence; low ml_prob = bearish → high PUT confidence)
            directional_prob = ml_prob if sig.direction == "CALL" else (1.0 - ml_prob)

            flow_score = 0.5
            pcr = latest.get("pcr")
            if pcr and not np.isnan(pcr):
                flow_score = min(0.3 * (pcr > 1.2) + 0.2, 1.0)
            else:
                # OBV slope + MFI direction-aware fallback when PCR unavailable
                obv_slope = latest.get("obv_slope", 0) or 0
                mfi = latest.get("mfi", 50) or 50
                if sig.direction == "CALL":
                    obv_contrib = 0.15 if obv_slope > 0 else (-0.10 if obv_slope < 0 else 0.0)
                    mfi_contrib = 0.15 if mfi > 60 else (-0.10 if mfi < 40 else 0.0)
                else:  # PUT
                    obv_contrib = 0.15 if obv_slope < 0 else (-0.10 if obv_slope > 0 else 0.0)
                    mfi_contrib = 0.15 if mfi < 40 else (-0.10 if mfi > 60 else 0.0)
                flow_score = max(0.20, min(1.0, 0.50 + obv_contrib + mfi_contrib))

            regime_bonus = 0.05 if regime_strategies and sig.strategy in regime_strategies else 0.0
            regime_bonus += daily_bias_adjustment(sig.direction, daily_bias)
            final_score = (
                WEIGHT_ML_PROBABILITY * directional_prob
                + WEIGHT_OPTIONS_FLOW * flow_score
                + WEIGHT_TECHNICAL_STRENGTH * sig.technical_strength
                + regime_bonus
            )

            # Quality floor raised 2026-04-08 from 0.70 → 0.75 after backtest analysis:
            #   0.70-0.75 bucket: 25 trades, 28% WR, -₹3,551 (drag on P&L)
            #   0.75-0.80 bucket: 12 trades, 42% WR, +₹1,207
            #   0.80+ bucket:     17 trades, 76% WR, +₹34,718
            # The 0.70-0.75 range adds noise without edge — filter it out.
            from config.risk_profiles import get_risk_profile as _get_rp, RiskLevel as _RL
            from strategy.regime_detector import MarketRegime
            _med_profile = _get_rp(_RL.MEDIUM)
            effective_threshold = max(0.75, SCORE_THRESHOLD, _med_profile.put_score_threshold)

            # Strategy-specific gates (evidence from backtest with real slippage)
            if sig.strategy == "bearish_momentum" and sig.direction == "PUT":
                # ── Gate A: trend-context filter (added 2026-04-19) ─────────
                # bearish_momentum fires PUT signals on 1-bar red candles. In a
                # confirmed uptrend (close > ema50 AND ema20 > ema50), these are
                # pullbacks that almost always revert. Require much higher ML
                # conviction (>=0.85) to take the trade in that context.
                close_px = float(latest.get("close", 0) or 0)
                ema20_v  = float(latest.get("ema20", 0) or 0)
                ema50_v  = float(latest.get("ema50", 0) or 0)
                in_uptrend = (close_px > 0 and ema50_v > 0
                              and close_px > ema50_v and ema20_v > ema50_v)
                if in_uptrend and directional_prob < 0.85:
                    logger.info(
                        f"SKIP bearish_momentum PUT: uptrend context "
                        f"(close={close_px:.2f} ema20={ema20_v:.2f} ema50={ema50_v:.2f}) "
                        f"and directional_prob={directional_prob:.3f} < 0.85"
                    )
                    continue
                # ── Gate B: multi-timeframe RSI divergence (added 2026-04-24,
                # loosened 2026-04-29). Apr 22-23 live losses entered with
                # rsi_1m<40 + rsi_15m>85. Apr 28 added 2 more near-misses
                # (rsi_1m=40.9 and 44.1) that Gate B with strict <40 missed.
                # Loosening to <45 catches those without filtering legitimate
                # reversals — bearish_momentum PUT into a clearly stretched
                # higher-TF (r15m>80) is the wrong setup whether r1m is 38
                # or 44.
                rsi_1m  = float(latest.get("rsi") or 50.0)
                rsi_15m = float(latest.get("rsi_15m") or 50.0)
                if rsi_1m < 45.0 and rsi_15m > 80.0:
                    logger.info(
                        f"SKIP bearish_momentum PUT: multi-TF RSI divergence "
                        f"(rsi_1m={rsi_1m:.1f}<45 rsi_15m={rsi_15m:.1f}>80) — "
                        f"pullback-bottom inside stretched-up higher-TF"
                    )
                    continue
            elif sig.strategy == "mean_reversion":
                # Only fire in SIDEWAYS/LOW_VOLATILITY — in trending markets it fights the trend
                # (scores 0.90-0.94 but still lost on trending days in backtest)
                if regime not in (MarketRegime.SIDEWAYS, MarketRegime.LOW_VOLATILITY):
                    continue
                # ML must confirm direction: directional_prob < 0.40 = model actively opposes signal
                if directional_prob < 0.40:
                    continue
                # Trend-context gate for mean_reversion PUT (added 2026-04-29
                # after Apr 29 PUT mean_rev MAE -₹3,433 / final -₹60). PUT
                # mean_reversion in an established uptrend with stretched
                # higher-TF (r15m>70) is the same anti-pattern as bearish_
                # momentum PUT in uptrend — fading the dominant trend.
                if sig.direction == "PUT":
                    close_px = float(latest.get("close", 0) or 0)
                    ema20_v  = float(latest.get("ema20", 0) or 0)
                    ema50_v  = float(latest.get("ema50", 0) or 0)
                    rsi_15m  = float(latest.get("rsi_15m") or 50.0)
                    in_uptrend = (close_px > 0 and ema50_v > 0
                                  and close_px > ema50_v and ema20_v > ema50_v)
                    if in_uptrend and rsi_15m > 70.0:
                        logger.info(
                            f"SKIP mean_reversion PUT: uptrend+stretched higher-TF "
                            f"(close={close_px:.2f} ema50={ema50_v:.2f} rsi_15m={rsi_15m:.1f}>70)"
                        )
                        continue
                effective_threshold = max(effective_threshold, 0.80)
            elif sig.strategy == "vwap_momentum_breakout":
                # Tightened 2026-04-08: was firing on 2-3 bar micro-spikes that reverted
                # immediately. Now requires sustained breakout (>=2 of last 3 bars green
                # AND last close > 3-bar-ago close) plus 0.78 score floor.
                if regime not in (MarketRegime.TRENDING_BULL, MarketRegime.LOW_VOLATILITY):
                    continue
                if len(featured) >= 4:
                    last3 = featured.iloc[-4:-1]
                    sustained_up = bool(
                        (last3["close"] > last3["open"]).sum() >= 2
                        and last3["close"].iloc[-1] > last3["close"].iloc[0]
                    )
                    if not sustained_up:
                        continue
                effective_threshold = max(effective_threshold, 0.78)

            if final_score < effective_threshold:
                continue

            # ── Same-(strategy, direction) ladder cooldown (added 2026-04-29) ─
            # Prevents firing multiple entries on the same exhausted move.
            # Apr 28 fired 5 bearish_momentum PUTs in 65min, 4 lost. Apr 23
            # fired 3 bearish_momentum PUTs in 8min, 2 lost. Block subsequent
            # entries of the same (strategy, direction) for 15 min after one
            # fires, regardless of strike.
            sd_key = (sig.strategy, sig.direction)
            last_sd = _last_entry_by_strat_dir.get(sd_key)
            if last_sd is not None:
                age = (datetime.now() - last_sd).total_seconds()
                if age < STRATEGY_DIRECTION_COOLDOWN_SECS:
                    remaining = int((STRATEGY_DIRECTION_COOLDOWN_SECS - age) / 60)
                    logger.info(
                        f"SKIP {sig.strategy} {sig.direction}: same-strat/dir cooldown "
                        f"({remaining}min left, last fired {int(age/60)}min ago) — "
                        f"avoid laddering"
                    )
                    continue

            # ── Previous-bar direction confirmation (CONTINUATION ONLY) ──────
            # Rejects trades where the previous 1-min bar moved counter to the
            # signal direction. Only applied to CONTINUATION strategies
            # (vwap_momentum_breakout); skipped for reversal strategies
            # (bearish_momentum, mean_reversion) because a reversal signal
            # *should* fire against the prior bar's direction.
            #
            # Backtest testing 2026-04-08: applying this gate to reversal
            # strategies filtered 8 MEDIUM trades, net -₹13,872 of filtered
            # profit. Making it strategy-aware restored ~₹10k of that.
            CONTINUATION_STRATEGIES = {"vwap_momentum_breakout", "math_decision_engine"}
            if sig.strategy in CONTINUATION_STRATEGIES and len(featured) >= 2:
                prev_bar = featured.iloc[-2]
                prev_open = float(prev_bar.get("open", 0))
                prev_close = float(prev_bar.get("close", 0))
                if prev_open > 0:
                    prev_move_pct = (prev_close - prev_open) / prev_open
                    if sig.direction == "PUT" and prev_move_pct > 0.0010:
                        logger.info(
                            f"SKIP {sig.strategy} {sig.direction}: prev-bar bullish "
                            f"({prev_move_pct*100:+.3f}%) — continuation-only filter"
                        )
                        continue
                    if sig.direction == "CALL" and prev_move_pct < -0.0010:
                        logger.info(
                            f"SKIP {sig.strategy} {sig.direction}: prev-bar bearish "
                            f"({prev_move_pct*100:+.3f}%) — continuation-only filter"
                        )
                        continue

            # ── Micro-level entry confirmation (CONTINUATION ONLY) ──────────
            # Same reasoning as the prev-bar gate above: reversal strategies
            # fire against current momentum by design, so rejecting them for
            # "momentum opposes direction" is self-defeating. Only apply to
            # continuation strategies. Reversal strategies rely on the
            # option-premium confirmation gate below instead.
            if sig.strategy in CONTINUATION_STRATEGIES:
                try:
                    if micro_latest is not None:
                        tick_mom = micro_latest.get("tick_momentum", 0)
                        if tick_mom is not None and not np.isnan(tick_mom):
                            MICRO_THRESHOLD = 0.1
                            if sig.direction == "CALL" and tick_mom < -MICRO_THRESHOLD:
                                logger.info(
                                    f"SKIP {sig.strategy} CALL: tick_momentum {tick_mom:.3f} "
                                    f"strongly selling — micro opposes entry"
                                )
                                continue
                            if sig.direction == "PUT" and tick_mom > MICRO_THRESHOLD:
                                logger.info(
                                    f"SKIP {sig.strategy} PUT: tick_momentum {tick_mom:.3f} "
                                    f"strongly buying — micro opposes entry"
                                )
                                continue
                except Exception as _e:
                    logger.debug(f"micro confirmation check skipped: {_e}")

            # ── Option-premium confirmation gate (Fix D, 2026-04-08) ────────
            # Look at the last 30 SECONDS of ticks for the SPECIFIC option
            # contract we're about to buy. If its premium has been actively
            # falling >0.8% in that window, reject — we'd be catching a
            # falling knife.
            #
            # Strict time window (not tick count) — illiquid strikes with
            # few ticks get skipped entirely (fail-open), which is the
            # correct behavior since microstructure on thin books is noise.
            #
            # This catches today's ₹-7,031 live trade: 24000PE premium fell
            # 233.9→232.7 in ~25 seconds before entry.
            try:
                now_ts = datetime.now()
                window_start = now_ts - timedelta(seconds=30)
                # Build the option symbol (same logic as below)
                _atm_probe = round(latest.get("close", 0) / 50) * 50
                _opt_type_probe = "CE" if sig.direction == "CALL" else "PE"
                _exp_code_probe = expiry.strftime("%y%m%d") if expiry else "000000"
                _opt_sym_probe = f"NIFTY{_exp_code_probe}{_atm_probe}{_opt_type_probe}"

                prem_ticks = read_sql(
                    "SELECT timestamp, price FROM tick_data "
                    "WHERE symbol = :sym "
                    "AND timestamp >= :start "
                    "ORDER BY timestamp",
                    {"sym": _opt_sym_probe, "start": window_start},
                )
                if not prem_ticks.empty and len(prem_ticks) >= 8:
                    first_p = float(prem_ticks.iloc[0]["price"])
                    last_p = float(prem_ticks.iloc[-1]["price"])
                    if first_p > 0:
                        slope_pct = (last_p - first_p) / first_p
                        if slope_pct < -0.008:
                            logger.info(
                                f"SKIP {sig.strategy} {sig.direction}: "
                                f"option premium falling {slope_pct*100:+.2f}% "
                                f"in last 30s ({first_p:.2f}→{last_p:.2f}) — premium gate"
                            )
                            continue
            except Exception as _prem_err:
                logger.debug(f"option premium gate skipped: {_prem_err}")

            atm = round(latest.get("close", 0) / 50) * 50
            opt_type = "CE" if sig.direction == "CALL" else "PE"
            exp_code = expiry.strftime("%y%m%d") if expiry else "000000"
            opt_symbol = f"NIFTY{exp_code}{atm}{opt_type}"

            # Risk label based on score confidence
            if final_score >= 0.70:
                risk_label = "LOW"
            elif final_score >= 0.60:
                risk_label = "MEDIUM"
            else:
                risk_label = "HIGH"

            # Try to get current option price — tick cache first, then DB candle, then REST
            # Use ASK price for entry (we're the buyer, we pay the ask).
            # Fall back to LTP if bid/ask not available.
            opt_ltp = None
            try:
                cache_data = json.loads(open(LIVE_CACHE_FILE).read())
                tick_entry = cache_data.get(opt_symbol, {})
                # Prefer ask price for entry; fall back to last traded price
                ask = float(tick_entry.get("ask", 0) or 0)
                ltp = float(tick_entry.get("price", 0) or 0)
                p = ask if ask > 0 else ltp
                if p > 0:
                    opt_ltp = p
            except Exception:
                pass

            if not opt_ltp:
                try:
                    row = read_sql(
                        "SELECT close, timestamp FROM minute_candles WHERE symbol = :sym ORDER BY timestamp DESC LIMIT 1",
                        {"sym": opt_symbol}
                    )
                    if not row.empty:
                        candle_age = (pd.Timestamp.now(tz="UTC") - pd.to_datetime(row.iloc[0]["timestamp"], utc=True)).total_seconds()
                        if candle_age < 300:  # only use if within 5 minutes
                            opt_ltp = float(row.iloc[0]["close"])
                            logger.debug(f"opt_ltp from DB candle ({candle_age:.0f}s old) for {opt_symbol}: {opt_ltp}")
                        else:
                            logger.debug(f"DB candle too stale ({candle_age:.0f}s) for {opt_symbol}, trying REST")
                except Exception:
                    pass

            if not opt_ltp:
                # REST fallback — fetch latest 1-min bar directly from AngelOne
                try:
                    from data.market_data_adapter import MarketDataAdapter
                    _td = MarketDataAdapter()
                    if _td.authenticate():
                        bars = _td.fetch_last_n_bars(opt_symbol, n=1, interval="1min")
                        if bars is not None and not bars.empty:
                            opt_ltp = float(bars.iloc[-1]["close"])
                            logger.info(f"opt_ltp from REST for {opt_symbol}: {opt_ltp}")
                except Exception as e:
                    logger.debug(f"REST price fallback failed for {opt_symbol}: {e}")

            # Dynamic SL and target based on signal quality (score-tiered)
            # Targets are a ceiling — trailing SL (activates at +10%, ratchets up) is what
            # captures most profit in practice. Hard target only fires on sharp one-way moves.
            if final_score >= 0.75:
                sl_pct, target_pct = 0.15, 0.55   # was 70% — most moves exhaust before that
            elif final_score >= 0.65:
                sl_pct, target_pct = 0.15, 0.45
            else:
                sl_pct, target_pct = 0.12, 0.35

            sl_price = round(opt_ltp * (1 - sl_pct), 2) if opt_ltp else None
            target_price = round(opt_ltp * (1 + target_pct), 2) if opt_ltp else None

            # Deduplicate: if same signal fired within cooldown, refresh its timestamp
            cooldown_key = (opt_symbol, sig.direction, sig.strategy)
            last_fired = _suggestion_cooldown.get(cooldown_key)
            if last_fired and (datetime.now() - last_fired).total_seconds() < SUGGESTION_COOLDOWN_SECS:
                # Refresh existing suggestion with latest scores + price
                for existing in state["trade_suggestions"]:
                    if existing.get("symbol") == opt_symbol and existing.get("strategy") == sig.strategy:
                        existing["time"] = datetime.now().strftime("%H:%M:%S")
                        existing["index_price"] = round(latest.get("close", 0), 1)
                        existing["ml_prob"] = round(ml_prob, 4)
                        existing["final_score"] = round(final_score, 4)
                        existing["strategy_track_record"] = _strategy_track_record(sig.strategy)
                        existing["risk_label"] = risk_label
                        existing["lots"] = _lots_for_score(final_score)
                        if opt_ltp:
                            existing["entry_premium"] = opt_ltp
                            existing["sl_price"] = sl_price
                            existing["target_price"] = target_price
                continue
            _suggestion_cooldown[cooldown_key] = datetime.now()

            lots = _lots_for_score(final_score)
            trade = {
                "time": datetime.now().strftime("%H:%M:%S"),
                "symbol": opt_symbol,
                "direction": sig.direction,
                "strategy": sig.strategy,
                "entry_premium": opt_ltp,
                "sl_price": sl_price,
                "target_price": target_price,
                "sl_pct": sl_pct,
                "target_pct": target_pct,
                "lots": lots,
                "expiry": str(expiry),
                "dte": dte,
                "ml_prob": round(ml_prob, 4),
                "strat_prob": round(strat_prob, 4),
                "flow_score": round(flow_score, 2),
                "final_score": round(final_score, 4),
                # Decomposed confidence (AI-platform roadmap Phase 1): the
                # numbers above (ml_prob/strat_prob/flow_score/final_score)
                # are all THIS BAR's live setup-quality reading. This is the
                # separate, historical "has this strategy actually worked"
                # number — never blend the two into one display value.
                "strategy_track_record": _strategy_track_record(sig.strategy),
                "risk_label": risk_label,
                "regime": regime.value,
                "index_price": round(latest.get("close", 0), 1),
            }

            state["trade_suggestions"].append(trade)
            # Keep only last 50 suggestions to avoid unbounded growth
            if len(state["trade_suggestions"]) > 50:
                state["trade_suggestions"] = state["trade_suggestions"][-50:]
            state["trades_today"] += 1

            # Record this fire for the same-(strategy, direction) ladder cooldown
            _last_entry_by_strat_dir[(sig.strategy, sig.direction)] = datetime.now()

            logger.info(
                f"TRADE SUGGESTION: {sig.direction} {opt_symbol} | "
                f"ML={ml_prob:.2f} Strat={strat_prob:.2f} Score={final_score:.2f}"
            )

            # Server-side audio alert when no browser tab is open (no SSE clients)
            if _sse_client_count == 0:
                try:
                    import subprocess as _sp
                    _sp.Popen(
                        ["afplay", "/System/Library/Sounds/Glass.aiff"],
                        stdout=_sp.DEVNULL, stderr=_sp.DEVNULL
                    )
                except Exception:
                    pass

            # Auto-trade mode: enter the position automatically without waiting for manual action
            if auto_trade_enabled:
                _auto_enter_position(trade, trade_mode="test")

            break

    except Exception as e:
        logger.error(f"Scan error: {e}", exc_info=True)
    finally:
        state["status"] = "idle"


_eod_done_date: str = ""  # tracks which date we already ran EOD for

def _run_end_of_day():
    """
    End-of-day tasks (run once after 15:35 IST):
    1. Backfill any missing NIFTY-I candles for today
    2. Incremental model retrain with last 2 days of data
    """
    global _eod_done_date
    import subprocess as sp

    today = date.today().isoformat()
    if _eod_done_date == today:
        return
    _eod_done_date = today
    logger.info("=== END-OF-DAY AUTOMATION STARTING ===")

    project_root = Path(__file__).resolve().parent.parent
    # sys.executable, not a hardcoded .venv/bin/python (macOS-only path that
    # doesn't exist on Windows, or at all without a venv) -- always the same
    # interpreter this Flask process itself is running under.
    python_bin = sys.executable

    # 1. Backfill missing candles
    try:
        logger.info("EOD: Backfilling any missing candles for today...")
        from data.market_data_adapter import MarketDataAdapter
        from database.db import upsert_candles
        td_eod = MarketDataAdapter()
        if td_eod.authenticate():
            start_dt = datetime(date.today().year, date.today().month, date.today().day, 9, 15, 0)
            end_dt = datetime(date.today().year, date.today().month, date.today().day, 15, 31, 0)
            bars = td_eod.fetch_historical_bars("NIFTY-I", start_dt, end_dt, interval="1min")
            if not bars.empty:
                for col in ["vwap", "oi"]:
                    if col not in bars.columns:
                        bars[col] = 0
                bars = bars[["timestamp", "symbol", "open", "high", "low", "close", "volume", "vwap", "oi"]]
                upsert_candles(bars)
                logger.info(f"EOD: Upserted {len(bars)} candles for today")
    except Exception as e:
        logger.error(f"EOD backfill error: {e}")

    # 2. Incremental model retrain (macro + micro only — strategy models
    # are deliberately skipped here, see incremental_train.py comment for why)
    try:
        retrain_script = str(project_root / "scripts" / "incremental_train.py")
        if os.path.exists(retrain_script):
            logger.info("EOD: Running incremental model retrain (last 2 days)...")
            result = sp.run(
                [python_bin, retrain_script, "--days", "2"],
                cwd=str(project_root), capture_output=True, text=True, timeout=300,
            )
            if result.returncode == 0:
                logger.info(f"EOD: Retrain completed successfully")
            else:
                logger.error(f"EOD: Retrain failed: {result.stderr[:500]}")
    except Exception as e:
        logger.error(f"EOD retrain error: {e}")

    # 3. Outcome-based strategy model retrain (uses real backtest WIN/LOSS
    # outcomes from backtest_results/trades_*.csv). Safe to run nightly even
    # when there's no new data — the script's --min-samples threshold makes
    # it a no-op for strategies with <15 samples.
    try:
        outcome_script = str(project_root / "scripts" / "train_outcome_models.py")
        if os.path.exists(outcome_script):
            logger.info("EOD: Running outcome-based strategy model retrain...")
            result = sp.run(
                [python_bin, outcome_script],
                cwd=str(project_root), capture_output=True, text=True, timeout=180,
            )
            if result.returncode == 0:
                logger.info("EOD: Outcome strategy retrain completed")
            else:
                logger.error(f"EOD: Outcome retrain failed: {result.stderr[:500]}")
    except Exception as e:
        logger.error(f"EOD outcome retrain error: {e}")

    logger.info("=== END-OF-DAY AUTOMATION COMPLETE ===")


def background_scanner():
    """Background thread that scans every 30 seconds. Triggers EOD tasks after market close."""
    global scanner_enabled
    while True:
        now = datetime.now()

        if scanner_enabled:
            try:
                scan_market()
            except Exception as e:
                logger.error(f"Scanner error: {e}")
                state["status"] = "idle"
        else:
            state["status"] = "stopped"

        # End-of-day tasks: run once after 15:35 IST on weekdays
        if now.weekday() < 5 and now.hour == 15 and now.minute >= 35:
            try:
                _run_end_of_day()
            except Exception as e:
                logger.error(f"EOD error: {e}")

        # Auto-start collector during market hours
        if 9 <= now.hour < 16 and now.weekday() < 5:
            _ensure_collector()

        time.sleep(30)


replay_state = {
    "status": "idle",        # idle, running, done
    "date": None,
    "progress": 0,
    "total_minutes": 0,
    "current_time": None,
    "current_price": 0,
    "regime": "UNKNOWN",
    "trades": [],
    "total_pnl": 0,
    "ticks_processed": 0,
}


def run_replay(replay_date: str):
    """Run tick replay for a specific date in background."""
    global replay_state
    from backtest.option_resolver import get_nearest_expiry, get_days_to_expiry, clear_cache

    replay_state = {
        "status": "running", "date": replay_date, "progress": 0,
        "total_minutes": 0, "current_time": None, "current_price": 0,
        "regime": "UNKNOWN", "trades": [], "total_pnl": 0, "ticks_processed": 0,
    }
    clear_cache()

    try:
        # Load ticks for this day
        ticks = read_sql(
            "SELECT timestamp, price, volume, oi, bid_price, ask_price, bid_qty, ask_qty "
            "FROM tick_data WHERE symbol = 'NIFTY-I' AND timestamp::date = :dt "
            "ORDER BY timestamp",
            {"dt": replay_date},
        )
        if ticks.empty:
            replay_state["status"] = "done"
            return

        ticks["timestamp"] = pd.to_datetime(ticks["timestamp"])

        # Load warmup candles
        warmup = read_sql(
            "SELECT timestamp, symbol, open, high, low, close, volume, vwap, oi "
            "FROM minute_candles WHERE symbol = 'NIFTY-I' "
            "AND timestamp < :dt ORDER BY timestamp DESC LIMIT 300",
            {"dt": replay_date},
        )
        warmup["timestamp"] = pd.to_datetime(warmup["timestamp"])
        candle_buffer = warmup.sort_values("timestamp").reset_index(drop=True)

        # Group by minute
        ticks["minute"] = ticks["timestamp"].dt.floor("min")
        minute_groups = ticks.groupby("minute")
        minutes = sorted(minute_groups.groups.keys())
        replay_state["total_minutes"] = len(minutes)

        in_trade = False
        current_trade = None
        option_info = None
        daily_trades = 0
        LOT_SIZE = 65
        SL_PCT = 0.30
        TGT_PCT = 0.50
        COMMISSION = 40.0

        for idx, minute_ts in enumerate(minutes):
            minute_ticks = minute_groups.get_group(minute_ts)
            replay_state["ticks_processed"] += len(minute_ticks)
            replay_state["progress"] = int((idx + 1) / len(minutes) * 100)
            replay_state["current_time"] = str(minute_ts)
            replay_state["current_price"] = float(minute_ticks["price"].iloc[-1])

            # Check open trade against ticks
            if in_trade and current_trade and option_info:
                entry_prem = option_info["entry_premium"]
                prem_df = option_info["premium_df"]
                ts_pd = pd.to_datetime(minute_ts)
                mask = (prem_df["timestamp"] - ts_pd).abs() <= pd.Timedelta(minutes=1)
                prem_row = prem_df[mask]

                if not prem_row.empty:
                    p_high = float(prem_row.iloc[0].get("high", prem_row.iloc[0]["premium"]))
                    p_low = float(prem_row.iloc[0].get("low", prem_row.iloc[0]["premium"]))
                    p_close = float(prem_row.iloc[0]["premium"])
                    bars_held = idx - current_trade.get("entry_idx", 0)

                    exit_prem = None
                    result = None
                    if p_low <= entry_prem * (1 - SL_PCT):
                        exit_prem = entry_prem * (1 - SL_PCT)
                        result = "LOSS"
                    elif p_high >= entry_prem * (1 + TGT_PCT):
                        exit_prem = entry_prem * (1 + TGT_PCT)
                        result = "WIN"
                    elif bars_held >= 20:
                        exit_prem = p_close
                        result = "TIMEOUT"

                    if exit_prem:
                        pnl = round((exit_prem - entry_prem) * LOT_SIZE - COMMISSION, 2)
                        current_trade["exit_time"] = str(minute_ts)
                        current_trade["exit_price"] = round(exit_prem, 2)
                        current_trade["pnl"] = pnl
                        current_trade["result"] = result
                        replay_state["trades"].append(current_trade)
                        replay_state["total_pnl"] = round(
                            sum(t["pnl"] for t in replay_state["trades"]), 2
                        )
                        in_trade = False
                        current_trade = None
                        option_info = None

            # Build candle
            candle = {
                "timestamp": minute_ts,
                "symbol": "NIFTY-I",
                "open": float(minute_ticks["price"].iloc[0]),
                "high": float(minute_ticks["price"].max()),
                "low": float(minute_ticks["price"].min()),
                "close": float(minute_ticks["price"].iloc[-1]),
                "volume": int(minute_ticks["volume"].sum()),
                "vwap": 0, "oi": 0,
            }
            candle_buffer = pd.concat(
                [candle_buffer, pd.DataFrame([candle])], ignore_index=True
            ).tail(500)

            # Signal + ML if not in trade
            if not in_trade and len(candle_buffer) >= 250:  # daily_trades < 5 TEMP: disabled to collect more training data
                try:
                    featured = compute_all_macro_indicators(candle_buffer.tail(300).copy())
                    if featured.empty:
                        continue
                    latest = featured.iloc[-1].to_dict()
                except Exception:
                    continue

                # Time filter
                if hasattr(minute_ts, "hour"):
                    mins = minute_ts.hour * 60 + minute_ts.minute - 555
                    if mins < 5 or mins > 360:
                        continue

                # Regime
                try:
                    rw = candle_buffer.tail(100)[["open","high","low","close","volume"]].copy()
                    regime = regime_detector.detect(rw)
                    replay_state["regime"] = regime.value
                except Exception:
                    pass

                signals = generate_signals(latest, "NIFTY-I")
                if not signals:
                    continue

                sig = signals[0]
                ml_prob = 0.5
                if predictor.is_loaded:
                    p = predictor.predict_macro(latest)
                    if p is not None:
                        ml_prob = p

                strat_prob = strategy_predictor.predict(sig.strategy, latest)
                if strat_prob is None or strat_prob < 0.05:
                    strat_prob = 0.5  # fallback when model unavailable / out-of-distribution

                dp = ml_prob if sig.direction == "CALL" else (1.0 - ml_prob)
                score = 0.5 * dp + 0.3 * strat_prob + 0.2 * sig.technical_strength
                if score < 0.55:
                    continue

                # Resolve option
                from backtest.option_resolver import resolve_option_at_entry
                opt = resolve_option_at_entry(
                    index_price=latest["close"], timestamp=minute_ts,
                    direction=sig.direction,
                )
                if opt is None:
                    continue

                entry_prem = opt["entry_premium"]
                if entry_prem <= 0:
                    continue

                current_trade = {
                    "entry_time": str(minute_ts),
                    "symbol": opt["symbol"],
                    "direction": sig.direction,
                    "strategy": sig.strategy,
                    "entry_price": round(entry_prem, 2),
                    "sl": round(entry_prem * (1 - SL_PCT), 2),
                    "target": round(entry_prem * (1 + TGT_PCT), 2),
                    "ml_prob": round(ml_prob, 3),
                    "strat_prob": round(strat_prob, 3),
                    "score": round(score, 3),
                    "entry_idx": idx,
                    "exit_time": None, "exit_price": None, "pnl": None, "result": "OPEN",
                }
                option_info = opt
                in_trade = True
                daily_trades += 1

            time.sleep(0.20)  # Delay for UI polling (200ms × ~375 min ≈ 75s per day)

        replay_state["status"] = "done"
        replay_state["progress"] = 100

    except Exception as e:
        logger.error(f"Replay error: {e}", exc_info=True)
        replay_state["status"] = "done"


REPLAY_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AI Trader - Replay Simulation</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif; background: #0f1117; color: #e1e4e8; }
        .container { max-width: 1200px; margin: 0 auto; padding: 20px; }
        h1 { color: #58a6ff; margin-bottom: 5px; font-size: 24px; }
        .subtitle { color: #8b949e; margin-bottom: 20px; font-size: 14px; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; margin-bottom: 16px; }
        .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 14px; }
        .card h3 { color: #8b949e; font-size: 11px; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 6px; }
        .card .value { font-size: 24px; font-weight: 700; }
        .green { color: #3fb950; } .red { color: #f85149; } .yellow { color: #d29922; } .blue { color: #58a6ff; }
        .badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: 600; }
        .badge-green { background: #0d1117; border: 1px solid #3fb950; color: #3fb950; }
        .badge-red { background: #0d1117; border: 1px solid #f85149; color: #f85149; }
        .badge-yellow { background: #0d1117; border: 1px solid #d29922; color: #d29922; }
        table { width: 100%; border-collapse: collapse; margin-top: 8px; }
        th { text-align: left; padding: 6px 10px; border-bottom: 2px solid #30363d; color: #8b949e; font-size: 11px; text-transform: uppercase; }
        td { padding: 6px 10px; border-bottom: 1px solid #21262d; font-size: 13px; }
        tr:hover { background: #1c2128; }
        .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
        .sim-badge { background: #a371f7; color: #0d1117; padding: 4px 12px; border-radius: 4px; font-weight: 700; font-size: 12px; }
        select, button { background: #21262d; color: #e1e4e8; border: 1px solid #30363d; padding: 8px 16px; border-radius: 6px; font-size: 14px; cursor: pointer; }
        button:hover { background: #30363d; }
        button:disabled { opacity: 0.5; cursor: not-allowed; }
        .progress-bar { width: 100%; height: 6px; background: #21262d; border-radius: 3px; margin-top: 8px; }
        .progress-fill { height: 100%; background: #58a6ff; border-radius: 3px; transition: width 0.3s; }
        .nav { margin-bottom: 16px; }
        .nav a { color: #58a6ff; text-decoration: none; margin-right: 16px; font-size: 14px; }
        .nav a:hover { text-decoration: underline; }
    </style>
    <script>
        function startReplay() {
            const day = document.getElementById('day-select').value;
            if (!day) return;
            document.getElementById('start-btn').disabled = true;
            fetch('/api/replay/start?date=' + day, {method: 'POST'}).then(() => pollReplay());
        }
        function pollReplay() {
            fetch('/api/replay/state').then(r => r.json()).then(data => {
                document.getElementById('status').textContent = data.status;
                document.getElementById('time').textContent = data.current_time ? data.current_time.split(' ')[1] || data.current_time : '--';
                document.getElementById('price').textContent = data.current_price ? '₹' + data.current_price.toLocaleString('en-IN', {maximumFractionDigits:1}) : '--';
                document.getElementById('regime').textContent = data.regime;
                document.getElementById('regime').className = 'value ' + (data.regime.includes('BULL') ? 'green' : data.regime.includes('BEAR') ? 'red' : 'yellow');
                document.getElementById('ticks').textContent = data.ticks_processed.toLocaleString();
                document.getElementById('n-trades').textContent = data.trades.length;
                document.getElementById('pnl').textContent = '₹' + data.total_pnl.toLocaleString('en-IN');
                document.getElementById('pnl').className = 'value ' + (data.total_pnl >= 0 ? 'green' : 'red');
                document.getElementById('progress-fill').style.width = data.progress + '%';
                document.getElementById('progress-text').textContent = data.progress + '%';

                let wins = data.trades.filter(t => t.pnl > 0).length;
                let total = data.trades.length;
                document.getElementById('wr').textContent = total > 0 ? Math.round(wins/total*100) + '%' : '--';

                let html = '';
                data.trades.slice().reverse().forEach(t => {
                    const cls = t.result === 'WIN' ? 'badge-green' : t.result === 'LOSS' ? 'badge-red' : 'badge-yellow';
                    const pnlCls = t.pnl >= 0 ? 'green' : 'red';
                    html += '<tr>' +
                        '<td>' + (t.entry_time ? t.entry_time.split(' ')[1] || t.entry_time.substring(11,19) : '') + '</td>' +
                        '<td><strong>' + t.symbol + '</strong></td>' +
                        '<td><span class="badge ' + (t.direction==='CALL'?'badge-green':'badge-red') + '">' + t.direction + '</span></td>' +
                        '<td>' + t.strategy + '</td>' +
                        '<td>₹' + t.entry_price + '</td>' +
                        '<td>' + (t.exit_price || '--') + '</td>' +
                        '<td class="' + pnlCls + '">₹' + (t.pnl || 0) + '</td>' +
                        '<td><span class="badge ' + cls + '">' + t.result + '</span></td>' +
                        '<td>' + (t.ml_prob*100).toFixed(0) + '%</td>' +
                        '<td>' + (t.score*100).toFixed(0) + '%</td></tr>';
                });
                document.getElementById('trades-body').innerHTML = html || '<tr><td colspan="10" style="text-align:center;color:#484f58">Waiting for trades...</td></tr>';

                if (data.status === 'running') setTimeout(pollReplay, 500);
                else document.getElementById('start-btn').disabled = false;
            });
        }
    </script>
</head>
<body>
    <div class="container">
        <div class="nav">
            <a href="/">← Live Paper Trading</a>
            <a href="/replay"><strong>Replay Simulation</strong></a>
        </div>
        <div class="header">
            <div>
                <h1>Tick Replay Simulation</h1>
                <div class="subtitle">Watch the AI trade a full historical day in fast-forward</div>
            </div>
            <span class="sim-badge">SIMULATION</span>
        </div>

        <div class="card" style="margin-bottom:16px;display:flex;align-items:center;gap:16px">
            <div>
                <h3>Select Day</h3>
                <select id="day-select">
                    <option value="">-- pick a day --</option>
                    DAYS_OPTIONS
                </select>
            </div>
            <button id="start-btn" onclick="startReplay()">▶ Start Replay</button>
            <div style="flex:1">
                <div style="display:flex;justify-content:space-between;font-size:12px;color:#8b949e">
                    <span id="status">idle</span>
                    <span id="progress-text">0%</span>
                </div>
                <div class="progress-bar"><div class="progress-fill" id="progress-fill" style="width:0%"></div></div>
            </div>
        </div>

        <div class="grid">
            <div class="card"><h3>Time</h3><div class="value blue" id="time">--</div></div>
            <div class="card"><h3>NIFTY Price</h3><div class="value blue" id="price">--</div></div>
            <div class="card"><h3>Regime</h3><div class="value yellow" id="regime">--</div></div>
            <div class="card"><h3>Ticks</h3><div class="value" id="ticks">0</div></div>
            <div class="card"><h3>Trades</h3><div class="value" id="n-trades">0</div></div>
            <div class="card"><h3>Win Rate</h3><div class="value green" id="wr">--</div></div>
            <div class="card"><h3>Day P&L</h3><div class="value green" id="pnl">₹0</div></div>
        </div>

        <div class="card">
            <h3>Trades</h3>
            <table>
                <thead><tr>
                    <th>Time</th><th>Contract</th><th>Dir</th><th>Strategy</th>
                    <th>Entry</th><th>Exit</th><th>P&L</th><th>Result</th><th>ML</th><th>Score</th>
                </tr></thead>
                <tbody id="trades-body">
                    <tr><td colspan="10" style="text-align:center;color:#484f58">Select a day and click Start</td></tr>
                </tbody>
            </table>
        </div>
    </div>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/replay")
def replay_page():
    # Get available days from tick data
    days = read_sql("""
        SELECT timestamp::date as day, COUNT(*) as ticks
        FROM tick_data WHERE symbol = 'NIFTY-I'
        GROUP BY 1 HAVING COUNT(*) > 100
        ORDER BY 1
    """)
    options = ""
    for _, r in days.iterrows():
        options += f'<option value="{r["day"]}">{r["day"]} ({r["ticks"]:,} ticks)</option>\n'
    html = REPLAY_HTML.replace("DAYS_OPTIONS", options)
    return render_template_string(html)


@app.route("/api/state")
def api_state():
    return jsonify(state)


@app.route("/api/replay/state")
def api_replay_state():
    return jsonify(replay_state)


@app.route("/api/replay/start", methods=["POST"])
def api_replay_start():
    from flask import request
    replay_date = request.args.get("date")
    if not replay_date:
        return jsonify({"error": "date required"}), 400
    thread = threading.Thread(target=run_replay, args=(replay_date,), daemon=True)
    thread.start()
    return jsonify({"status": "started", "date": replay_date})


@app.route("/api/scan", methods=["POST"])
def api_scan():
    scan_market()
    return jsonify({"status": "scanned"})


# ── Paper Trading ─────────────────────────────────────────────────────────────


def _update_position_price(pos: dict, live_prem: float):
    """
    Update a single open position with a new live premium.
    Handles: current premium, trailing SL, unrealised P&L, auto-exit on SL/target.
    """
    if pos["status"] != "OPEN":
        return

    ep = pos["entry_premium"]
    pos["current_premium"] = round(live_prem, 2)
    pos["unrealised_pnl"] = round((live_prem - ep) * pos["lot_size"] - COMMISSION, 2)

    # --- Trailing SL logic ---
    # Ensure fields exist (backwards compat with old positions)
    if "max_premium" not in pos:
        pos["max_premium"] = ep
    if "trailing_active" not in pos:
        pos["trailing_active"] = False
    if "initial_sl" not in pos:
        pos["initial_sl"] = pos["sl"]
    if "breakeven_locked" not in pos:
        pos["breakeven_locked"] = False
    if "first_profit_time" not in pos:
        pos["first_profit_time"] = None
    # Stagnation tracking: ISO timestamp of when max_premium was last set
    if "peak_time" not in pos:
        pos["peak_time"] = pos.get("entry_time_dt") or datetime.now().isoformat()

    now = datetime.now()

    # Update max premium seen + reset peak-staleness timer
    if live_prem > pos["max_premium"]:
        pos["max_premium"] = round(live_prem, 2)
        pos["peak_time"] = now.isoformat()

    current_profit_pct = (live_prem - ep) / ep

    # --- Breakeven protection ---
    # Track when trade first goes into profit (≥ BREAKEVEN_MIN_PROFIT)
    if current_profit_pct >= BREAKEVEN_MIN_PROFIT and pos["first_profit_time"] is None:
        pos["first_profit_time"] = now.isoformat()

    # After BREAKEVEN_AFTER_MIN minutes in profit, lock SL at entry (worst case: scratch trade)
    if (not pos["breakeven_locked"]
            and pos["first_profit_time"] is not None
            and current_profit_pct >= 0):   # still in profit when timer fires
        try:
            first_profit_dt = datetime.fromisoformat(pos["first_profit_time"])
            mins_in_profit = (now - first_profit_dt).total_seconds() / 60
            if mins_in_profit >= BREAKEVEN_AFTER_MIN:
                be_sl = round(ep * 1.001, 2)  # entry + tiny buffer for slippage
                if be_sl > pos["sl"]:
                    pos["sl"] = be_sl
                    pos["breakeven_locked"] = True
                    logger.info(f"BE LOCK {pos['symbol']}: SL moved to ₹{be_sl} (entry+0.1%) after {mins_in_profit:.1f}min in profit")
        except Exception:
            pass

    # Activate trailing once profit exceeds threshold
    profit_pct = (pos["max_premium"] - ep) / ep
    if profit_pct >= TRAIL_ACTIVATE_PCT:
        pos["trailing_active"] = True

    # ── Tiered retention (unified with backtest 2026-04-16) ──────────────
    # Base retention by peak gain + stagnation boost when peak stops advancing.
    # Replaces the flat TRAIL_FACTOR=0.50 which was giving back too much
    # profit on +35%+ winners.
    if pos["trailing_active"]:
        max_profit = pos["max_premium"] - ep
        gain_pct = max_profit / ep if ep > 0 else 0

        # Base tier
        if gain_pct >= 0.50:                  # monster winner
            retention = 0.80
        elif gain_pct >= 0.35:                # big winner
            retention = 0.70
        elif gain_pct >= 0.25:                # solid winner
            retention = 0.60
        elif gain_pct >= 0.20:                # normal trail
            retention = 0.55
        elif gain_pct >= 0.12:
            retention = 0.45
        else:
            retention = 0.35

        # Stagnation boost — peak hasn't advanced in N minutes
        if gain_pct >= 0.15:
            try:
                peak_dt = datetime.fromisoformat(pos["peak_time"])
                mins_since_peak = (now - peak_dt).total_seconds() / 60
                if mins_since_peak >= 20:
                    retention = min(0.90, retention + 0.20)
                elif mins_since_peak >= 10:
                    retention = min(0.85, retention + 0.12)
                elif mins_since_peak >= 5:
                    retention = min(0.80, retention + 0.06)
            except Exception:
                pass

        trail_sl = round(ep + max_profit * retention, 2)
        # SL can only move up, never down
        if trail_sl > pos["sl"]:
            pos["sl"] = trail_sl

    # --- Auto-exit checks ---
    if live_prem <= pos["sl"]:
        pnl = round((live_prem - ep) * pos["lot_size"] - COMMISSION, 2)
        pos.update({
            "status": "CLOSED",
            "exit_time": datetime.now().strftime("%H:%M:%S"),
            "exit_premium": round(live_prem, 2),
            "realised_pnl": pnl,
            "unrealised_pnl": 0.0,
            "exit_reason": "TRAILING_SL" if pos["trailing_active"] else "SL_HIT",
        })
        logger.info(f"PAPER AUTO-EXIT {pos['exit_reason']}: {pos['symbol']} @ ₹{live_prem} | SL was ₹{pos['sl']} | PnL=₹{pnl}")
        _persist_closed_trade(pos)
    elif live_prem >= pos["target"]:
        pnl = round((live_prem - ep) * pos["lot_size"] - COMMISSION, 2)
        pos.update({
            "status": "CLOSED",
            "exit_time": datetime.now().strftime("%H:%M:%S"),
            "exit_premium": round(live_prem, 2),
            "realised_pnl": pnl,
            "unrealised_pnl": 0.0,
            "exit_reason": "TARGET_HIT",
        })
        logger.info(f"PAPER AUTO-EXIT TGT: {pos['symbol']} @ ₹{live_prem} | PnL=₹{pnl}")
        _persist_closed_trade(pos)


def _tick_monitor_loop():
    """
    Background thread: reads /tmp/td_live_prices.json every second
    and updates all open paper positions with trailing SL + auto-exit.
    Runs only during market hours.
    """
    logger.info("Tick monitor thread started.")
    while True:
        try:
            # Only run during ~9:00-15:35 IST
            now = datetime.now()
            if not (9 <= now.hour < 16):
                time.sleep(30)
                continue

            all_positions = paper_positions_by_mode.get("test", []) + paper_positions_by_mode.get("live", [])
            # Agent-mirrored positions (source == "agent", e.g.
            # strategy/intraday_agent.py's math_decision_engine) run their
            # OWN exit rule via check_exits() every EXIT_CHECK_INTERVAL_SECS
            # (10s) -- deliberately NOT this loop's trailing-SL/breakeven
            # machinery (see that module's docstring). Before this filter,
            # this 1s loop was racing check_exits() on the very same mirror
            # dict and winning almost every time, so agent trades were
            # actually being exited by trailing-SL/breakeven logic they were
            # never designed for -- then check_exits() ran ~10s later, found
            # its own internal state still said "open" (nothing told it the
            # position had already closed), and closed it AGAIN with a
            # different exit price, writing a second, contradictory row to
            # the trade journal. Found via 35 duplicate-trade rows (52% of
            # all math_decision_engine trades) in paper_trades/trades_test.jsonl.
            open_positions = [p for p in all_positions if p["status"] == "OPEN" and p.get("source") != "agent"]
            if not open_positions:
                time.sleep(1)
                continue

            # Read tick cache
            cache = {}
            try:
                mtime = os.path.getmtime(LIVE_CACHE_FILE)
                if time.time() - mtime <= 30:
                    cache = json.loads(open(LIVE_CACHE_FILE).read())
            except Exception:
                pass

            for pos in open_positions:
                sym = pos["symbol"]
                live_price = None

                # 1. Try tick cache (WebSocket, most real-time)
                if sym in cache and cache[sym].get("price", 0) > 0:
                    ts_str = cache[sym].get("ts", "")
                    try:
                        ts_age = (datetime.now() - datetime.fromisoformat(ts_str)).total_seconds()
                    except Exception:
                        ts_age = 0
                    if ts_age < 120:  # only use if not stale
                        # Use BID price for exit/SL monitoring — we receive the bid when selling
                        bid = float(cache[sym].get("bid", 0) or 0)
                        ltp = float(cache[sym]["price"])
                        live_price = bid if bid > 0 else ltp

                # 2. Fallback: fetch last 1-min bar via AngelOne REST (rate-limited to once per 60s per symbol)
                if live_price is None:
                    last_rest = _tick_monitor_rest_ts.get(sym, 0)
                    if time.time() - last_rest >= 60:
                        try:
                            from data.market_data_adapter import MarketDataAdapter as _TDA
                            _td_fallback = _TDA()
                            bars = _td_fallback.fetch_last_n_bars(sym, n=1, interval="1min")
                            if not bars.empty:
                                live_price = float(bars.iloc[-1]["close"])
                                _tick_monitor_rest_ts[sym] = time.time()
                        except Exception:
                            pass

                if live_price is not None:
                    _update_position_price(pos, live_price)

                # --- Journey tracking: record every 5s ---
                if live_price is not None and pos["status"] == "OPEN":
                    journey = pos.setdefault("journey", [])
                    last_ts = journey[-1]["ts"] if journey else None
                    nifty_price = float(cache.get("NIFTY-I", {}).get("price", 0) or 0)
                    now_ts = datetime.now().isoformat()
                    if last_ts is None or (datetime.now() - datetime.fromisoformat(last_ts)).total_seconds() >= 5:
                        journey.append({
                            "ts": now_ts,
                            "option_price": round(live_price, 2),
                            "nifty_price": round(nifty_price, 2),
                            "sl": round(pos["sl"], 2),
                            "unrealised_pnl": pos.get("unrealised_pnl", 0),
                        })
                        # Cap at 500 points (~40 min at 5s interval) to avoid unbounded memory
                        if len(journey) > 500:
                            journey.pop(0)

                # --- Regime-aware SL tightening + adverse-move early exit ---
                # Tightened 2026-04-08 after the ₹-7,031 NIFTY26041324000PE trade:
                # the old 0.3% threshold (~72pts on NIFTY=24000) triggered AT the
                # moment of exit — too late to help. Now has three tiers:
                #
                #   Tier 1: NIFTY moved >0.10% against us → log warning, no action
                #   Tier 2: NIFTY moved >0.20% against us → tighten SL to entry+2%
                #           (locks a tiny profit if we had any; otherwise caps loss)
                #   Tier 3: NIFTY moved >0.30% against us AND option is underwater
                #           → HARD EXIT at live_price (don't wait for SL hit)
                #
                # Runs every 10s (down from 30s) so fast reversals get caught.
                if live_price is not None and pos["status"] == "OPEN":
                    try:
                        last_check = datetime.fromisoformat(pos.get("last_regime_check", "2000-01-01"))
                        if (datetime.now() - last_check).total_seconds() >= REGIME_CHECK_INTERVAL:
                            pos["last_regime_check"] = datetime.now().isoformat()
                            nifty_bid = float(cache.get("NIFTY-I", {}).get("bid", 0) or
                                              cache.get("NIFTY-I", {}).get("price", 0) or 0)
                            if nifty_bid > 0 and pos.get("index_price", 0) > 0:
                                nifty_move_pct = (nifty_bid - pos["index_price"]) / pos["index_price"]
                                direction = pos["direction"]
                                # Sign adverse = move is AGAINST our direction (+ve for CALL
                                # means bearish move, for PUT means bullish move)
                                adverse = -nifty_move_pct if direction == "CALL" else nifty_move_pct
                                entry_prem = pos.get("entry_premium", 0) or 0

                                if adverse >= 0.0030 and entry_prem > 0 and live_price < entry_prem:
                                    # Tier 3: HARD EXIT. NIFTY moved 0.3% against us and
                                    # the option is already in loss. Don't wait for SL.
                                    logger.warning(
                                        f"REGIME EXIT {pos['symbol']}: NIFTY moved {nifty_move_pct:+.2%} "
                                        f"against {direction}, option underwater "
                                        f"(₹{live_price} < entry ₹{entry_prem}). Closing now."
                                    )
                                    pos["status"] = "CLOSED"
                                    pos["exit_time"] = datetime.now().strftime("%H:%M:%S")
                                    pos["exit_premium"] = live_price
                                    pos["exit_reason"] = "REGIME_EXIT"
                                    lot = pos.get("lot_size", 65)
                                    pos["realised_pnl"] = round((live_price - entry_prem) * lot, 2)
                                    pos["unrealised_pnl"] = 0.0
                                    _persist_closed_trade(pos)
                                elif adverse >= 0.0020 and live_price > pos["sl"]:
                                    # Tier 2: Tighten SL toward entry+2%. Locks a tiny
                                    # profit or caps loss at ~entry.
                                    target_sl = round(entry_prem * 1.02, 2) if entry_prem > 0 else pos["sl"]
                                    # Don't tighten above current live price (would insta-exit)
                                    safe_sl = min(target_sl, round(live_price * 0.98, 2))
                                    if safe_sl > pos["sl"]:
                                        pos["sl"] = safe_sl
                                        logger.info(
                                            f"REGIME TIGHTEN {pos['symbol']}: SL→₹{safe_sl} "
                                            f"(NIFTY moved {nifty_move_pct:+.2%} against {direction})"
                                        )
                                elif adverse >= 0.0010:
                                    # Tier 1: Just log. Don't touch SL yet — small adverse
                                    # moves often mean-revert within a minute.
                                    logger.info(
                                        f"REGIME WATCH {pos['symbol']}: NIFTY moved {nifty_move_pct:+.2%} "
                                        f"against {direction}, live=₹{live_price} sl=₹{pos['sl']}"
                                    )
                    except Exception as _e:
                        logger.debug(f"regime check failed: {_e}")

        except Exception as e:
            logger.error(f"Tick monitor error: {e}")
        time.sleep(1)


def _ensure_tick_monitor():
    """Start the tick monitor thread if not already running."""
    global _tick_monitor_thread
    if _tick_monitor_thread is None or not _tick_monitor_thread.is_alive():
        _tick_monitor_thread = threading.Thread(target=_tick_monitor_loop, daemon=True, name="tick_monitor")
        _tick_monitor_thread.start()


def _cache_prices_are_fresh(max_age_secs: int = 90) -> bool:
    """
    Return True if the tick cache is healthy.
    Three states:
      - File old (>30s)  → False (no collector running)
      - File fresh, cache empty → True (collector just started, give it grace period)
      - File fresh, cache has entries but all stale (>max_age_secs) → False (WebSocket stalled)
      - File fresh, cache has a recent entry → True (healthy)
    """
    try:
        mtime = os.path.getmtime(LIVE_CACHE_FILE)
        if time.time() - mtime >= 30:
            return False  # file itself is old — no process writing it
        cache = json.loads(open(LIVE_CACHE_FILE).read())
        if not cache:
            # Empty cache + fresh file = collector just started and hasn't received ticks yet.
            # Give it up to 120s grace period before declaring it stalled.
            return True
        now_ts = datetime.now()
        for sym_data in cache.values():
            try:
                ts = datetime.fromisoformat(sym_data.get("ts", ""))
                if (now_ts - ts).total_seconds() < max_age_secs:
                    return True
            except Exception:
                pass
    except Exception:
        pass
    return False


def _kill_stalled_collector():
    """
    Kill any running collect_ticks.py process (stalled or otherwise).

    `pkill` does not exist on Windows (this deployment's actual OS — the
    original macOS-era code never accounted for that). subprocess.run(["pkill",
    ...]) raised FileNotFoundError every time, swallowed by the bare except,
    so this function silently did NOTHING on every call: each 30s stale-price
    cycle in _ensure_collector() spawned a BRAND NEW collect_ticks.py on top
    of whatever was already running, without ever killing the old one. Over
    2026-09-07's session that piled up many concurrent collector processes,
    all subscribing to the same AngelOne feed and racing to insert the same
    (timestamp, symbol) ticks — the actual source of the duplicate-key
    crashes that eventually took the whole collector down for the rest of
    the trading day (see data/tick_collector.py's flush() for the other half
    of that fix: duplicate ticks are now skipped, not fatal).

    Primary path here: terminate the specific PID this process spawned
    (no shell-out needed). Fallback: a platform-aware sweep for any OTHER
    collect_ticks.py process we've lost track of (e.g. after a Flask
    restart) — pkill on POSIX, a CIM/WMI process filter on Windows.
    """
    global _collector_process
    if _collector_process is not None and _collector_process.poll() is None:
        try:
            _collector_process.terminate()
            _collector_process.wait(timeout=5)
        except Exception:
            try:
                _collector_process.kill()
            except Exception:
                pass
    _collector_process = None

    try:
        import subprocess as sp
        import platform
        if platform.system() == "Windows":
            filt = "CommandLine LIKE '%collect_ticks.py%'"
            sp.run(
                ["powershell", "-NoProfile", "-Command",
                 f"Get-CimInstance Win32_Process -Filter \"{filt}\" "
                 "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"],
                capture_output=True, timeout=10,
            )
            # Verify the kill actually landed instead of assuming it did (see below): 2026-09-07 through
            # 2026-09-18's trading.log shows hundreds of "stale, killing and restarting" events some days
            # with almost no matching "atexit: disconnecting" lines -- strong evidence this sweep was
            # silently matching zero processes on at least some of those days, so respawned collectors
            # piled up on top of still-alive ones instead of replacing them. Each pile-up forces a FRESH
            # mStock login (mStock enforces one session per login -- see
            # strategy/premarket._get_mstock_client's docstring for the same failure mode elsewhere),
            # which invalidates whichever older process's mStock WebSocket was still connected, while
            # AngelOne's already-open sockets are not torn down the same way -- so a silent kill failure
            # starves mStock of tick coverage specifically, without ever showing up as an AngelOne problem.
            for attempt in range(5):
                time.sleep(1)
                check = sp.run(
                    ["powershell", "-NoProfile", "-Command",
                     f"(Get-CimInstance Win32_Process -Filter \"{filt}\").Count"],
                    capture_output=True, timeout=10, text=True,
                )
                survivors = (check.stdout or "").strip()
                if survivors in ("", "0"):
                    return
            logger.warning(
                f"_kill_stalled_collector: {survivors} collect_ticks.py process(es) still running after the "
                "kill sweep -- a new one will be spawned ON TOP of them. This is the exact condition that "
                "starves mStock of ticks (see comment above); investigate rather than ignore."
            )
        else:
            sp.run(["pkill", "-f", "collect_ticks.py"], capture_output=True, timeout=10)
            time.sleep(1)
    except Exception:
        pass


COLLECTOR_STARTUP_GRACE_SECS = 150  # see the guard below
_collector_lock = threading.Lock()


def _ensure_collector():
    """Auto-start collect_ticks.py if it's market hours and not delivering fresh prices."""
    global _collector_process, _collector_spawned_at
    import subprocess

    # _ensure_collector() is called from multiple threads close together on a cold start (the main thread
    # right before app.run(), and background_scanner's very first loop iteration, which has no initial
    # sleep) -- confirmed live 2026-09-22 09:09:50: both calls saw _collector_process as None (the other's
    # Popen() hadn't been assigned back to the global yet) and each spawned its own collect_ticks.py, two
    # processes 0 seconds apart. Since mStock enforces one session per login, the second process's mStock
    # login very likely kicked the first one's mStock WebSocket immediately -- on EVERY cold start, not
    # just during a stale-price storm. Serializing the whole check-and-spawn removes the race.
    if not _collector_lock.acquire(blocking=False):
        return  # another thread is already inside this function; don't pile another spawn decision on top
    try:
        _ensure_collector_locked()
    finally:
        _collector_lock.release()


def _ensure_collector_locked():
    global _collector_process, _collector_spawned_at
    import subprocess

    now = datetime.now()
    # Only auto-start during market hours (9:00 - 15:35 IST, weekdays)
    if now.weekday() >= 5 or not (9 <= now.hour < 16):
        return

    # Startup grace: a freshly spawned collector must authenticate mStock AND AngelOne, resolve today's
    # ATM options, connect two WebSockets and only THEN start writing the price cache -- confirmed live
    # (2026-09-18 09:22-09:51) that this can take 30-90s under any broker slowness/rate-limiting, well
    # past this function's own 30s call cadence (background_scanner). Judging it "stale" before it has
    # had a real chance to start just kills and respawns it again mid-startup -- that day's log shows 40+
    # full restart banners in 29 minutes, almost none followed by a clean "atexit: disconnecting", meaning
    # the kills mostly failed and processes piled up (see _kill_stalled_collector). Each respawn is a FRESH
    # mStock login, and mStock enforces one session per login, so every pile-up silently kicks whichever
    # older process's mStock WebSocket was still alive -- while AngelOne's already-open sockets are not
    # torn down the same way. That is why mStock's tick coverage looked worse than AngelOne's: not a
    # data-quality problem with mStock itself, but this restart storm starving it specifically.
    # This used to only guard the pre-open window (< 9:16); it now applies at any time of day, because the
    # same slow-startup-into-premature-kill sequence can happen whenever the collector (re)starts, not just
    # at the open. The grace clock starts at whichever is LATER: the actual spawn time, or market open
    # (09:15) -- a collector spawned at 09:00 does no real work (and writes no cache file) until it wakes
    # at 09:14-09:15, so its grace window must cover the wait too, not just the 2 minutes after 09:00.
    if _collector_process is not None and _collector_process.poll() is None:
        from datetime import time as _dtime
        market_open_today = datetime.combine(now.date(), _dtime(9, 15))
        effective_start = max(_collector_spawned_at, market_open_today.timestamp())
        if time.time() - effective_start < COLLECTOR_STARTUP_GRACE_SECS:
            return

    # Price freshness is the single source of truth — check it first.
    # A process can be "running" but have a stalled WebSocket writing stale prices.
    if _cache_prices_are_fresh():
        return  # collector is alive and delivering real ticks

    # Prices are stale — kill whatever is running and restart fresh
    logger.warning("Tick cache prices are stale. Killing any existing collector and restarting.")
    _kill_stalled_collector()
    _collector_process = None

    # Start collector
    project_root = Path(__file__).resolve().parent.parent
    collector_script = project_root / "scripts" / "collect_ticks.py"
    python_bin = sys.executable  # same interpreter this Flask process runs under
    if collector_script.exists():
        logger.info("Auto-starting collect_ticks.py for live tick data...")
        _collector_process = subprocess.Popen(
            [python_bin, str(collector_script)],
            cwd=str(project_root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _collector_spawned_at = time.time()
        logger.info(f"collect_ticks.py started (PID={_collector_process.pid})")


@app.route("/api/paper/enter", methods=["POST"])
def api_paper_enter():
    """Enter a paper trade from a suggestion."""
    body = request.get_json(force=True)
    trade_mode = body.get("mode", "test")
    positions = _get_mode_positions(trade_mode)
    symbol = body.get("symbol")
    direction = body.get("direction")
    strategy = body.get("strategy")
    entry_premium = body.get("entry_premium")
    expiry = body.get("expiry")
    ml_prob = body.get("ml_prob", 0.5)
    final_score = body.get("final_score", 0.5)
    index_price = body.get("index_price", state.get("last_price", 0))

    if not symbol or not direction:
        return jsonify({"error": "symbol and direction required"}), 400

    # If no premium provided, try multiple resolution strategies
    if not entry_premium:
        # 1. Try DB candle — only if fresh (< 5 minutes old); stale prices cause instant SL hits
        try:
            row = read_sql(
                "SELECT close, timestamp FROM minute_candles WHERE symbol = :sym ORDER BY timestamp DESC LIMIT 1",
                {"sym": symbol}
            )
            if not row.empty:
                candle_age = (pd.Timestamp.now(tz="UTC") - pd.to_datetime(row.iloc[0]["timestamp"], utc=True)).total_seconds()
                if candle_age < 300:
                    entry_premium = float(row.iloc[0]["close"])
        except Exception:
            pass

    if not entry_premium:
        # 2. Try AngelOne REST for last 1 bar of the option
        try:
            from data.market_data_adapter import MarketDataAdapter
            td = MarketDataAdapter()
            if td.authenticate():
                bars = td.fetch_last_n_bars(symbol, n=1, interval="1min")
                if bars is not None and not bars.empty:
                    entry_premium = float(bars.iloc[-1]["close"])
        except Exception:
            pass

    if not entry_premium:
        # 3. Estimate from index price: use intrinsic + time value heuristic
        # ATM option ~0.5% of index, OTM decays by ~20% per 50pt distance
        try:
            idx = float(index_price) if index_price else state.get("last_price", 0)
            if idx > 0:
                # Extract strike from symbol (last numeric segment before CE/PE)
                import re
                m = re.search(r'(\d{4,6})(CE|PE)$', symbol)
                if m:
                    strike = int(m.group(1))
                    opt_type = m.group(2)
                    intrinsic = max(0, (idx - strike) if opt_type == "CE" else (strike - idx))
                    otm_dist = abs(idx - strike)
                    time_val = max(10, idx * 0.003 * (1 - otm_dist / (idx * 0.05)))
                    entry_premium = round(intrinsic + time_val, 1)
        except Exception:
            pass

    if not entry_premium or entry_premium <= 0:
        return jsonify({"error": "Could not determine entry premium — no DB candle, no live data, and strike parse failed"}), 400

    ep = round(float(entry_premium), 2)
    # Dynamic SL/target: use values from suggestion if provided, else derive from score
    sl_pct = body.get("sl_pct") or (
        0.15 if final_score >= 0.75 else
        0.15 if final_score >= 0.65 else
        0.12
    )
    target_pct = body.get("target_pct") or (
        0.70 if final_score >= 0.75 else
        0.55 if final_score >= 0.65 else
        0.40
    )
    initial_sl = round(ep * (1 - sl_pct), 2)
    lots = _lots_for_score(final_score)
    position = {
        "id": int(datetime.now().timestamp() * 1000),
        "entry_time": datetime.now().strftime("%H:%M:%S"),
        "symbol": symbol,
        "direction": direction,
        "strategy": strategy or "",
        "entry_premium": ep,
        "sl": initial_sl,
        "initial_sl": initial_sl,
        "target": round(ep * (1 + target_pct), 2),
        "max_premium": ep,       # tracks highest premium seen (for trailing SL)
        "trailing_active": False,  # becomes True once profit > TRAIL_ACTIVATE_PCT
        "lot_size": LOT_SIZE * lots,
        "ml_prob": ml_prob,
        "final_score": final_score,
        "index_price": index_price,
        "expiry": expiry or "",
        "status": "OPEN",
        "current_premium": ep,
        "unrealised_pnl": 0.0,
        "exit_time": None,
        "exit_premium": None,
        "realised_pnl": None,
        "exit_reason": None,
    }
    position["mode"] = trade_mode
    positions.append(position)
    logger.info(f"PAPER ENTER [{trade_mode}]: {direction} {symbol} @ ₹{ep} | SL={position['sl']} TGT={position['target']} | {lots} lot(s) (score={final_score:.3f})")

    # Ensure tick monitor and data collector are running
    _ensure_tick_monitor()
    _ensure_collector()

    return jsonify(position)


@app.route("/api/paper/exit", methods=["POST"])
def api_paper_exit():
    """Manually exit an open paper position."""
    body = request.get_json(force=True)
    trade_mode = body.get("mode", "test")
    positions = _get_mode_positions(trade_mode)
    pos_id = body.get("id")
    pos = next((p for p in positions if p["id"] == pos_id and p["status"] == "OPEN"), None)
    if not pos:
        return jsonify({"error": "Position not found or already closed"}), 404

    exit_premium = pos["current_premium"]
    pnl = round((exit_premium - pos["entry_premium"]) * pos["lot_size"] - COMMISSION, 2)
    pos.update({
        "status": "CLOSED",
        "exit_time": datetime.now().strftime("%H:%M:%S"),
        "exit_premium": round(exit_premium, 2),
        "realised_pnl": pnl,
        "unrealised_pnl": 0.0,
        "exit_reason": "MANUAL",
    })
    logger.info(f"PAPER EXIT (manual): {pos['symbol']} @ ₹{exit_premium} | PnL=₹{pnl}")
    _persist_closed_trade(pos)
    return jsonify(pos)


@app.route("/api/paper/positions")
def api_paper_positions():
    """Return all paper positions with live P&L update for open ones."""
    positions = _get_mode_positions()
    current_price = state.get("last_price", 0)

    # Fetch live prices for all unique open option symbols in one pass
    open_symbols = list({p["symbol"] for p in positions if p["status"] == "OPEN"})
    live_prices: dict = {}

    # 1. Try in-memory cache file written by collect_ticks.py every ~1s
    LIVE_CACHE_FILE = "/tmp/td_live_prices.json"
    cache_age = float("inf")
    try:
        mtime = os.path.getmtime(LIVE_CACHE_FILE)
        cache_age = time.time() - mtime
        if cache_age < 30:  # cache is fresh (< 30s old)
            cache = json.loads(open(LIVE_CACHE_FILE).read())
            for sym in open_symbols:
                if sym in cache and cache[sym].get("price", 0) > 0:
                    live_prices[sym] = float(cache[sym]["price"])
    except Exception:
        pass

    # 2. For symbols not found in cache, fall back to AngelOne REST
    missing = [s for s in open_symbols if s not in live_prices]
    if missing:
        try:
            from data.market_data_adapter import MarketDataAdapter
            _td = MarketDataAdapter()
            if _td.authenticate():
                for sym in missing:
                    try:
                        bars = _td.fetch_last_n_bars(sym, n=1, interval="1min")
                        if bars is not None and not bars.empty:
                            live_prices[sym] = float(bars.iloc[-1]["close"])
                    except Exception:
                        pass
        except Exception:
            pass

    for pos in positions:
        if pos["status"] != "OPEN":
            continue

        # 1. Live AngelOne price
        live_prem = live_prices.get(pos["symbol"])

        # 2. Fallback: DB candle
        if not live_prem:
            try:
                row = read_sql(
                    "SELECT close FROM minute_candles WHERE symbol = :sym ORDER BY timestamp DESC LIMIT 1",
                    {"sym": pos["symbol"]}
                )
                if not row.empty:
                    live_prem = float(row.iloc[0]["close"])
            except Exception:
                pass

        # 3. Last resort: delta ~0.5 estimate from index move
        if not live_prem and current_price and pos.get("index_price"):
            idx_move = current_price - pos["index_price"]
            delta = 0.5 if pos["direction"] == "CALL" else -0.5
            live_prem = max(1.0, pos["entry_premium"] + delta * idx_move)

        if live_prem:
            _update_position_price(pos, live_prem)

    total_open_pnl = sum(p["unrealised_pnl"] for p in positions if p["status"] == "OPEN")
    total_closed_pnl = sum(p["realised_pnl"] for p in positions if p["status"] == "CLOSED" and p["realised_pnl"] is not None)
    return jsonify({
        "positions": positions,
        "total_open_pnl": round(total_open_pnl, 2),
        "total_closed_pnl": round(total_closed_pnl, 2),
        "total_pnl": round(total_open_pnl + total_closed_pnl, 2),
    })


@app.route("/api/paper/journey/<int:position_id>")
def api_paper_journey(position_id):
    """Return the price journey for a specific position (open, closed this session, or historical)."""
    # Search in-memory (open + closed this session)
    all_positions = []
    for mode_positions in paper_positions_by_mode.values():
        all_positions.extend(mode_positions)
    pos = next((p for p in all_positions if p.get("id") == position_id), None)
    # Fall back to persisted closed trade history (survives Flask restarts)
    if pos is None:
        for mode_trades in _closed_trades_by_mode.values():
            pos = next((p for p in mode_trades if p.get("id") == position_id), None)
            if pos:
                break
    if pos is None:
        return jsonify({"error": "Position not found"}), 404
    return jsonify({
        "id": position_id,
        "symbol": pos["symbol"],
        "direction": pos["direction"],
        "entry_premium": pos["entry_premium"],
        "entry_time": pos["entry_time"],
        "sl": pos["sl"],
        "initial_sl": pos.get("initial_sl"),
        "target": pos["target"],
        "status": pos["status"],
        "journey": pos.get("journey", []),
    })


@app.route("/api/paper/trades")
def api_paper_trades_history():
    """Return all closed live paper trades (persisted across restarts).

    Query params:
      mode=test|live  (default: test)
      date=YYYY-MM-DD (optional, filter by entry date)
    """
    mode = request.args.get("mode", "test")
    date_filter = request.args.get("date")
    trades = list(_closed_trades_by_mode.get(mode, []))
    if date_filter:
        trades = [t for t in trades if (t.get("entry_time_dt") or "").startswith(date_filter)]
    # Newest first
    return jsonify(list(reversed(trades)))


@app.route("/api/backtest/journey/<risk>/<int:trade_idx>")
def api_backtest_journey(risk, trade_idx):
    """Return the per-minute journey for a specific backtest trade."""
    _project_root = Path(__file__).resolve().parent.parent
    path = _project_root / "backtest_results" / f"journeys_{risk}_risk.json"
    if not path.exists():
        return jsonify({"error": "No journey data — re-run the backtest to generate it"}), 404
    try:
        journeys = json.loads(path.read_text())
        journey = journeys.get(str(trade_idx))
        if journey is None:
            return jsonify({"error": f"No journey for trade index {trade_idx}"}), 404
        return jsonify({"trade_idx": trade_idx, "journey": journey})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/live/prices")
def api_live_prices():
    """Return the latest tick prices from collect_ticks.py cache file."""
    LIVE_CACHE_FILE = "/tmp/td_live_prices.json"
    try:
        mtime = os.path.getmtime(LIVE_CACHE_FILE)
        age = time.time() - mtime
        cache = json.loads(open(LIVE_CACHE_FILE).read())
        return jsonify({"prices": cache, "age_seconds": round(age, 1), "source": "tick_cache"})
    except FileNotFoundError:
        return jsonify({"prices": {}, "age_seconds": None, "source": "unavailable",
                        "hint": "Start collect_ticks.py to enable live tick prices"})
    except Exception as e:
        return jsonify({"prices": {}, "age_seconds": None, "source": "error", "error": str(e)})


@app.route("/api/stream")
def api_stream():
    """
    Server-Sent Events stream: pushes live prices, positions, and state every ~1s.
    The frontend opens a single EventSource connection instead of multiple HTTP polls.
    """
    from flask import Response
    global _sse_client_count

    def generate():
        global _sse_client_count
        _sse_client_count += 1
        try:
            while True:
                try:
                    # Build payload — read tick cache first so last_price is real-time
                    cache = {}
                    tick_cache_age = None
                    try:
                        mtime = os.path.getmtime(LIVE_CACHE_FILE)
                        age = time.time() - mtime
                        if age < 30:
                            cache = json.loads(open(LIVE_CACHE_FILE).read())
                            tick_cache_age = round(age, 1)
                    except Exception:
                        pass

                    # Use tick cache NIFTY-I price for real-time last_price (falls back to scanner value)
                    last_price = state.get("last_price", 0)
                    nifty_tick = cache.get("NIFTY-I", {})
                    if nifty_tick.get("price", 0):
                        last_price = nifty_tick["price"]

                    # Spot price: NIFTY 50 index (excludes futures basis)
                    spot_tick = cache.get("NIFTY 50", {})
                    spot_price = spot_tick.get("price", 0) or 0

                    payload = {
                        "state": {
                            "last_price": last_price,
                            "spot_price": spot_price,
                            "regime": state.get("regime", "UNKNOWN"),
                            "status": state.get("status", "idle"),
                            "last_scan": state.get("last_scan"),
                            "scan_count": state.get("scan_count", 0),
                            "trade_suggestions": state.get("trade_suggestions", []),
                            "auto_trade_enabled": state.get("auto_trade_enabled", True),
                        },
                        "positions_by_mode": paper_positions_by_mode,
                        "tick_cache": cache,
                        "tick_cache_age": tick_cache_age,
                    }

                    # Compute totals per mode
                    for m in ["test", "live"]:
                        mpos = paper_positions_by_mode.get(m, [])
                        o_pnl = sum(p["unrealised_pnl"] for p in mpos if p["status"] == "OPEN")
                        c_pnl = sum(p.get("realised_pnl", 0) or 0 for p in mpos if p["status"] == "CLOSED")
                        payload[f"total_open_pnl_{m}"] = round(o_pnl, 2)
                        payload[f"total_closed_pnl_{m}"] = round(c_pnl, 2)
                        payload[f"total_pnl_{m}"] = round(o_pnl + c_pnl, 2)

                    # Also include combined for backward compat
                    all_pos = paper_positions_by_mode.get("test", []) + paper_positions_by_mode.get("live", [])
                    open_pnl = sum(p["unrealised_pnl"] for p in all_pos if p["status"] == "OPEN")
                    closed_pnl = sum(p.get("realised_pnl", 0) or 0 for p in all_pos if p["status"] == "CLOSED")
                    payload["total_open_pnl"] = round(open_pnl, 2)
                    payload["total_closed_pnl"] = round(closed_pnl, 2)
                    payload["total_pnl"] = round(open_pnl + closed_pnl, 2)

                    yield f"data: {json.dumps(payload)}\n\n"
                except GeneratorExit:
                    return
                except Exception as e:
                    yield f"data: {json.dumps({'error': str(e)})}\n\n"
                time.sleep(1)
        finally:
            _sse_client_count = max(0, _sse_client_count - 1)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/paper/clear", methods=["POST"])
def api_paper_clear():
    """Clear all closed paper positions for the specified mode."""
    body = request.get_json(force=True) if request.is_json else {}
    trade_mode = body.get("mode", request.args.get("mode", "test"))
    if trade_mode not in paper_positions_by_mode:
        trade_mode = "test"
    paper_positions_by_mode[trade_mode] = [p for p in paper_positions_by_mode[trade_mode] if p["status"] == "OPEN"]
    return jsonify({"cleared": True, "mode": trade_mode})


@app.route("/api/system/start", methods=["POST"])
def api_system_start():
    """Enable the background scanner."""
    global scanner_enabled
    scanner_enabled = True
    state["scanner_enabled"] = True
    state["status"] = "idle"
    logger.info("Scanner STARTED via API")
    return jsonify({"scanner_enabled": True})


@app.route("/api/system/stop", methods=["POST"])
def api_system_stop():
    """Disable the background scanner."""
    global scanner_enabled
    scanner_enabled = False
    state["scanner_enabled"] = False
    state["status"] = "stopped"
    logger.info("Scanner STOPPED via API")
    return jsonify({"scanner_enabled": False})


@app.route("/api/auto_trade", methods=["GET"])
def api_get_auto_trade():
    """Return current auto-trade mode."""
    return jsonify({"auto_trade_enabled": auto_trade_enabled})


@app.route("/api/auto_trade", methods=["POST"])
def api_set_auto_trade():
    """Enable or disable auto-trade mode. Body: {\"enabled\": true|false}"""
    global auto_trade_enabled
    body = request.get_json(force=True)
    auto_trade_enabled = bool(body.get("enabled", True))
    state["auto_trade_enabled"] = auto_trade_enabled
    logger.info(f"Auto-trade {'ENABLED' if auto_trade_enabled else 'DISABLED'}")
    return jsonify({"auto_trade_enabled": auto_trade_enabled})


# ── New API endpoints for Next.js dashboard ────────────────────────────────

@app.route("/api/backtest/results")
def api_backtest_results():
    """Load saved backtest CSV results for all risk profiles."""
    import glob
    _project_root = Path(__file__).resolve().parent.parent
    results = {}
    for risk in ["low", "medium", "high"]:
        path = str(_project_root / "backtest_results" / f"trades_{risk}_risk.csv")
        if os.path.exists(path):
            try:
                df = pd.read_csv(path)
                if df.empty or "pnl" not in df.columns:
                    results[risk] = {
                        "trades": 0, "pnl": 0, "win_rate": 0, "avg_win": 0,
                        "avg_loss": 0, "max_dd": 0, "rr": 0,
                        "equity_curve": [], "trade_list": [],
                    }
                    continue
                pnls = df["pnl"].tolist()
                wins = [p for p in pnls if p > 0]
                losses = [p for p in pnls if p <= 0]
                equity = np.cumsum(pnls)
                peak = np.maximum.accumulate(equity)
                dd = equity - peak
                results[risk] = {
                    "trades":    len(df),
                    "pnl":       round(float(df["pnl"].sum()), 2),
                    "win_rate":  round(len(wins) / max(len(pnls), 1) * 100, 1),
                    "avg_win":   round(float(np.mean(wins)), 2) if wins else 0,
                    "avg_loss":  round(float(np.mean(losses)), 2) if losses else 0,
                    "max_dd":    round(float(dd.min()), 2),
                    "rr":        round(abs(np.mean(wins) / np.mean(losses)), 2) if wins and losses else 0,
                    "equity_curve": [round(float(e), 2) for e in equity],
                    "trade_list": df.to_dict(orient="records"),
                }
            except Exception as e:
                logger.error(f"Error loading {path}: {e}")
    return jsonify(results)


@app.route("/api/trades/history")
def api_trade_history():
    """All completed trades from latest backtest run."""
    risk = request.args.get("risk", "medium")
    _project_root = Path(__file__).resolve().parent.parent
    path = str(_project_root / "backtest_results" / f"trades_{risk}_risk.csv")
    if not os.path.exists(path):
        return jsonify([])
    try:
        df = pd.read_csv(path)
        return jsonify(df.to_dict(orient="records"))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/equity/curve")
def api_equity_curve():
    """Return equity curves for all three risk profiles."""
    _project_root = Path(__file__).resolve().parent.parent
    curves = {}
    for risk in ["low", "medium", "high"]:
        path = str(_project_root / "backtest_results" / f"trades_{risk}_risk.csv")
        if os.path.exists(path):
            try:
                df = pd.read_csv(path)
                df = df.sort_values("entry_time")
                equity = np.cumsum(df["pnl"].values)
                curves[risk] = [
                    {"time": str(t)[:10], "equity": round(float(e), 2)}
                    for t, e in zip(df["entry_time"], equity)
                ]
            except Exception:
                pass
    return jsonify(curves)


@app.route("/api/risk/profiles")
def api_risk_profiles():
    """Return risk profile configs."""
    try:
        from config.risk_profiles import LOW_RISK, MEDIUM_RISK, HIGH_RISK
        import dataclasses
        return jsonify({
            "low":    dataclasses.asdict(LOW_RISK),
            "medium": dataclasses.asdict(MEDIUM_RISK),
            "high":   dataclasses.asdict(HIGH_RISK),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


_strategy_track_record_cache: dict = {}  # strategy_name -> (loaded_at, record)
_TRACK_RECORD_CACHE_TTL = 300  # 5 min — these only change after a manual retrain


def _strategy_track_record(strategy_name: str) -> dict:
    """
    Real historical performance for a strategy, read off its saved model
    file via _describe_model_file() below — the SAME function the AI Models
    page uses, so a trade suggestion's track record can never diverge from
    what that page reports for the same strategy.

    This is the "strategy prior" half of a decomposed confidence reading:
    distinct from ml_prob/strat_prob/flow_score on the trade dict below,
    which are all THIS BAR's live reading, not a historical track record.
    Previously the dashboard only ever showed one blended `final_score` —
    styled as if it were a single win-probability number, which conflates
    "does this strategy generally work" with "does today's setup look
    good", exactly the anti-pattern this field exists to stop.
    """
    now = time.time()
    cached = _strategy_track_record_cache.get(strategy_name)
    if cached and now - cached[0] < _TRACK_RECORD_CACHE_TTL:
        return cached[1]
    path = Path(__file__).resolve().parent.parent / "models" / "saved" / "strategy" / f"{strategy_name}_model.pkl"
    info = _describe_model_file(path)
    metrics = info.get("metrics") or {}
    n_samples = info.get("n_samples")
    record = {
        "auc_roc": metrics.get("auc_roc"),
        "n_samples": n_samples,
        "trained_at": info.get("trained_at") or info.get("modified"),
        # Below this, an AUC/sample-size reading is closer to noise than
        # signal — the UI should say "insufficient sample" rather than
        # render a number that looks precise but isn't reliable.
        "sufficient_sample": bool(n_samples and n_samples >= 200),
    }
    _strategy_track_record_cache[strategy_name] = (now, record)
    return record


def _describe_model_file(path: Path) -> dict:
    """
    Real metadata for one saved model, read off the file itself. The AI Models
    page used to hardcode this ("LightGBM — 46K bars — AUC 0.98") from whatever
    the original developer's machine had, which on any other install is simply
    fiction — and overstating a model's AUC on a trading dashboard is the kind
    of wrong number someone acts on. Everything here comes from disk.
    """
    info = {"path": str(path.relative_to(Path(__file__).resolve().parent.parent)).replace("\\", "/"),
            "exists": path.exists()}
    if not path.exists():
        return info
    info["modified"] = datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
    try:
        import joblib
        blob = joblib.load(path)
    except Exception as e:
        info["error"] = f"unreadable: {e}"
        return info
    if not isinstance(blob, dict):
        info["algorithm"] = type(blob).__name__
        return info
    model = blob.get("model")
    info["algorithm"] = type(model).__name__ if model is not None else None
    feats = blob.get("features") or blob.get("feature_names") or []
    info["n_features"] = len(feats) if hasattr(feats, "__len__") else None
    if blob.get("n_samples") is not None:
        info["n_samples"] = int(blob["n_samples"])
    if blob.get("trained_at"):
        info["trained_at"] = str(blob["trained_at"])
    metrics = blob.get("metrics")
    if isinstance(metrics, dict):
        clean = {}
        for k, v in metrics.items():
            try:
                f = float(v)
            except (TypeError, ValueError):
                clean[k] = v
                continue
            # NaN is not valid JSON — Flask emits a bare NaN token that
            # JSON.parse rejects outright, taking the whole page's fetch down.
            # A degenerate fold (math_decision_engine's AUC, from a 0.7%
            # positive rate) legitimately produces one, so null it here.
            clean[k] = None if f != f else round(f, 4)
        info["metrics"] = clean
    return info


@app.route("/api/models/status")
def api_models_status():
    """
    What is ACTUALLY on disk for this install — algorithm, feature count,
    sample count, walk-forward metrics and file mtime, per model.
    """
    root = Path(__file__).resolve().parent.parent
    saved = root / "models" / "saved"
    strategy_models = {}
    strat_dir = saved / "strategy"
    if strat_dir.exists():
        for f in sorted(strat_dir.glob("*_model.pkl")):
            strategy_models[f.stem.replace("_model", "")] = _describe_model_file(f)
    return jsonify({
        "macro": _describe_model_file(saved / "macro_model.pkl"),
        "micro": _describe_model_file(saved / "micro_model.pkl"),
        "strategy": strategy_models,
        "rl_exit_agent": {"path": "models/saved/rl_exit_agent.pkl",
                          "exists": (saved / "rl_exit_agent.pkl").exists()},
        "dqn_exit_agent": {"path": "models/saved/dqn_exit_agent.pt",
                           "exists": (saved / "dqn_exit_agent.pt").exists()},
    })


@app.route("/api/rl/status")
def api_rl_status():
    """Return RL / DQN agent status."""
    result = {}
    # Tabular agent
    try:
        from models.rl_exit_agent import RLExitAgent
        agent = RLExitAgent()
        if agent.load():
            result["tabular"] = agent.policy_summary()
    except Exception:
        pass
    # DQN agent
    try:
        from models.dqn_exit_agent import DQNExitAgent
        dqn = DQNExitAgent()
        if dqn.load():
            result["dqn"] = dqn.policy_summary()
    except Exception:
        pass
    return jsonify(result)


@app.route("/api/market/candles")
def api_market_candles():
    """Latest N minute candles for chart."""
    n = int(request.args.get("n", 100))
    df = read_sql(
        "SELECT timestamp, open, high, low, close, volume "
        "FROM minute_candles WHERE symbol = 'NIFTY-I' "
        "ORDER BY timestamp DESC LIMIT :n",
        {"n": n},
    )
    if df.empty:
        return jsonify([])
    df = df.sort_values("timestamp")
    df["timestamp"] = df["timestamp"].astype(str)
    return jsonify(df.to_dict(orient="records"))


# ── Data coverage + maintenance jobs (backfill / retrain) ───────────────────
#
# One job at a time, same streaming-subprocess pattern the backtest runner
# uses. These are the actions the AI Models page needs: without candle
# history the macro/strategy models cannot train at all, and the page
# previously gave no way to see coverage or fix it.
maintenance_job: dict = {"running": False, "job": None, "status": "idle", "output_lines": []}


def _utf8_env() -> dict:
    """
    Child-process env with UTF-8 stdio forced.

    Python on Windows defaults stdout to cp1252, so any script that prints a
    →, ₹, — or box-drawing character dies mid-run with UnicodeEncodeError.
    Four scripts call utils.console.fix_windows_console_encoding() for this,
    but 21 others print such characters without it — train_rl_on_journeys.py
    crashed on a literal "Hold→end" after training had already started.
    Setting PYTHONIOENCODING here fixes every script this launcher can start,
    including ones added later, rather than patching them one at a time as
    each new crash surfaces.
    """
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    return env


@app.route("/api/data/coverage")
def api_data_coverage():
    """
    What training data this install actually holds. Read live from the DB —
    the models are only as good as this, so it should never be guessed at.
    """
    def _one(sql, params=None):
        try:
            df = read_sql(sql, params or {})
            if df.empty:
                return {}
            row = df.to_dict(orient="records")[0]
            # jsonify renders a date as an HTTP date string ("Wed, 06 Aug
            # 2025 00:00:00 GMT"); ISO is what the UI actually wants.
            return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in row.items()}
        except Exception as e:
            return {"error": str(e)}

    candles = _one(
        "SELECT COUNT(DISTINCT timestamp::date) AS days, COUNT(*) AS bars, "
        "MIN(timestamp)::date AS first_day, MAX(timestamp)::date AS last_day "
        "FROM minute_candles WHERE symbol = 'NIFTY-I'")
    ticks = _one(
        "SELECT COUNT(DISTINCT timestamp::date) AS days, COUNT(*) AS rows, "
        "MIN(timestamp)::date AS first_day, MAX(timestamp)::date AS last_day "
        "FROM tick_data")
    options = _one(
        "SELECT COUNT(DISTINCT symbol) AS symbols, COUNT(*) AS bars FROM minute_candles "
        "WHERE symbol LIKE 'NIFTY%%CE' OR symbol LIKE 'NIFTY%%PE'")

    return jsonify({
        "candles": candles,
        "ticks": ticks,
        "option_candles": options,
        "collector_running": _cache_prices_are_fresh(),
        "market_hours": _is_market_hours(),
        # Stated here so the UI never offers a button that cannot work:
        # AngelOne's free tier has no historical tick endpoint (see
        # data/market_data_adapter.fetch_historical_ticks), so tick history
        # only ever accumulates forward from the live collector.
        "tick_backfill_supported": False,
    })


def _start_maintenance_job(name: str, cmd: list, label: str):
    global maintenance_job
    import subprocess  # local import, matching the other job routes in this file
    if maintenance_job["running"]:
        return jsonify({"error": f"'{maintenance_job['job']}' is already running"}), 409

    maintenance_job = {"running": True, "job": name, "label": label, "status": "running",
                       "output_lines": [], "started": datetime.now().strftime("%H:%M:%S")}

    def _run():
        global maintenance_job
        import subprocess
        try:
            proc = subprocess.Popen(
                cmd, cwd=str(Path(__file__).resolve().parent.parent),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, encoding="utf-8", errors="replace",
                env=_utf8_env(),
            )
            for line in proc.stdout:
                line = line.strip()
                if line:
                    maintenance_job["output_lines"].append(line)
                    if len(maintenance_job["output_lines"]) > 80:
                        maintenance_job["output_lines"] = maintenance_job["output_lines"][-80:]
            proc.wait()
            maintenance_job["status"] = "done" if proc.returncode == 0 else "error"
            maintenance_job["exit_code"] = proc.returncode
        except Exception as e:
            maintenance_job["status"] = "error"
            maintenance_job["output_lines"].append(f"ERROR: {e}")
        finally:
            maintenance_job["running"] = False
            maintenance_job["finished"] = datetime.now().strftime("%H:%M:%S")

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "started", "job": name})


@app.route("/api/data/backfill", methods=["POST"])
def api_data_backfill():
    """Backfill NIFTY-I + ATM option 1-min candles for the last N days."""
    days = int((request.get_json(silent=True) or {}).get("days", 30))
    days = max(1, min(days, 365))
    return _start_maintenance_job(
        "backfill", [sys.executable, "scripts/backfill_history.py", "--days", str(days)],
        f"Backfilling {days} days of candles")


@app.route("/api/models/train", methods=["POST"])
def api_models_train():
    """
    Retrain a model from the data currently in the DB. `target` picks which:
      macro    — macro + per-strategy models (scripts/retrain_full.py)
      rl       — tabular RL exit agent, needs backtest journeys first
      dqn      — DQN exit agent, same prerequisite
    """
    target = (request.get_json(silent=True) or {}).get("target", "macro")
    jobs = {
        "macro": ([sys.executable, "scripts/retrain_full.py"], "Retraining macro + strategy models"),
        "rl":    ([sys.executable, "scripts/train_rl_on_journeys.py", "--epochs", "30"], "Training RL exit agent"),
        "dqn":   ([sys.executable, "scripts/train_dqn_exit.py", "--epochs", "10"], "Training DQN exit agent"),
    }
    if target not in jobs:
        return jsonify({"error": f"unknown target {target!r}; use one of {list(jobs)}"}), 400
    cmd, label = jobs[target]
    return _start_maintenance_job(f"train:{target}", cmd, label)


@app.route("/api/maintenance/progress")
def api_maintenance_progress():
    """Live output of the running backfill/train job."""
    return jsonify(maintenance_job)


backtest_progress: dict = {"running": False, "risk": None, "status": "idle", "output_lines": []}

@app.route("/api/backtest/run", methods=["POST"])
def api_backtest_run():
    """Trigger a backtest run for a given risk profile and optional date range."""
    global backtest_progress
    data = request.get_json() or {}
    risk = data.get("risk", "medium")
    if risk not in ["low", "medium", "high"]:
        return jsonify({"error": "Invalid risk"}), 400

    start_date = data.get("start_date")  # YYYY-MM-DD or None
    end_date = data.get("end_date")      # YYYY-MM-DD or None

    if backtest_progress["running"]:
        return jsonify({"error": "A backtest is already running", "risk": backtest_progress["risk"]}), 409

    def _run():
        global backtest_progress
        import subprocess
        label = f"{risk}"
        if start_date and end_date:
            label += f" ({start_date} → {end_date})"
        elif start_date:
            label += f" (from {start_date})"
        backtest_progress = {"running": True, "risk": risk, "status": "running", "output_lines": [],
                             "started": datetime.now().strftime("%H:%M:%S"),
                             "start_date": start_date, "end_date": end_date}
        project_root = Path(__file__).resolve().parent.parent
        python_bin = sys.executable  # same interpreter this Flask process runs under

        # Build date list if range specified
        date_args: list = []
        if start_date:
            from datetime import timedelta as _td
            from email.utils import parsedate_to_datetime as _parse_rfc2822

            def _parse_date(s):
                """Parse YYYY-MM-DD or RFC 2822 date string to date object."""
                if not s:
                    return date.today()
                s = s.strip()
                # Try YYYY-MM-DD first (most common)
                if len(s) == 10 and s[4] == '-':
                    return datetime.strptime(s, "%Y-%m-%d").date()
                # Try RFC 2822 (e.g. 'Tue, 10 Mar 2026 00:00:00 GMT')
                try:
                    return _parse_rfc2822(s).date()
                except Exception:
                    pass
                # Try ISO format as fallback
                return datetime.fromisoformat(s.replace("Z", "+00:00")).date()

            sd = _parse_date(start_date)
            ed = _parse_date(end_date) if end_date else date.today()
            d = sd
            while d <= ed:
                if d.weekday() < 5:  # skip weekends
                    date_args.append(d.strftime("%Y-%m-%d"))
                d += _td(days=1)

        cmd = [python_bin, "scripts/tick_replay_backtest.py", "--risk", risk]
        if date_args:
            cmd.extend(date_args)

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(project_root),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, encoding="utf-8", errors="replace",
                env=_utf8_env(),  # see _utf8_env: cp1252 stdout kills scripts mid-run
            )
            for line in proc.stdout:
                line = line.strip()
                if line:
                    backtest_progress["output_lines"].append(line)
                    # Keep last 50 lines
                    if len(backtest_progress["output_lines"]) > 50:
                        backtest_progress["output_lines"] = backtest_progress["output_lines"][-50:]
            proc.wait()
            backtest_progress["status"] = "done" if proc.returncode == 0 else "error"
            backtest_progress["exit_code"] = proc.returncode
        except Exception as e:
            backtest_progress["status"] = "error"
            backtest_progress["output_lines"].append(f"ERROR: {e}")
        finally:
            backtest_progress["running"] = False
            backtest_progress["finished"] = datetime.now().strftime("%H:%M:%S")

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "started", "risk": risk})


@app.route("/api/backtest/progress")
def api_backtest_progress():
    """Return real-time backtest progress."""
    return jsonify(backtest_progress)


@app.route("/api/days")
def api_days():
    """Available trading days in the database."""
    days = read_sql("""
        SELECT timestamp::date as day, COUNT(*) as ticks
        FROM tick_data WHERE symbol = 'NIFTY-I'
        GROUP BY 1 HAVING COUNT(*) > 100
        ORDER BY 1
    """)
    if days.empty:
        return jsonify([])
    days["day"] = days["day"].astype(str)
    return jsonify(days.to_dict(orient="records"))


@app.route("/api/market/candles/date")
def api_market_candles_date():
    """NIFTY 1-min candles for a specific date."""
    dt = request.args.get("date")
    if not dt:
        return jsonify({"error": "date required"}), 400
    df = read_sql(
        "SELECT timestamp, open, high, low, close, volume "
        "FROM minute_candles WHERE symbol = 'NIFTY-I' "
        "AND timestamp::date = :dt ORDER BY timestamp",
        {"dt": dt},
    )
    if df.empty:
        return jsonify([])
    df["timestamp"] = df["timestamp"].astype(str)
    return jsonify(df.to_dict(orient="records"))


@app.route("/api/market/ticks/date")
def api_market_ticks_date():
    """NIFTY raw tick data for a specific date (for tick chart view)."""
    dt = request.args.get("date")
    if not dt:
        return jsonify({"error": "date required"}), 400
    df = read_sql(
        "SELECT timestamp, price, volume, bid_price, ask_price "
        "FROM tick_data WHERE symbol = 'NIFTY-I' "
        "AND timestamp::date = :dt ORDER BY timestamp",
        {"dt": dt},
    )
    if df.empty:
        return jsonify([])
    df["timestamp"] = df["timestamp"].astype(str)
    return jsonify(df.to_dict(orient="records"))


@app.route("/api/candle_dates")
def api_candle_dates():
    """All dates that have NIFTY index candle data, plus tick count per date."""
    candle_df = read_sql("""
        SELECT timestamp::date as day, COUNT(*) as bars
        FROM minute_candles WHERE symbol = 'NIFTY-I'
        GROUP BY 1 HAVING COUNT(*) > 50
        ORDER BY 1
    """)
    tick_df = read_sql("""
        SELECT timestamp::date as day, COUNT(*) as ticks
        FROM tick_data WHERE symbol = 'NIFTY-I'
        GROUP BY 1 HAVING COUNT(*) > 100
        ORDER BY 1
    """)
    tick_map = {}
    if not tick_df.empty:
        for _, r in tick_df.iterrows():
            tick_map[str(r["day"])] = int(r["ticks"])

    if candle_df.empty:
        # Return tick-only dates if no candle data
        if not tick_df.empty:
            return jsonify([{"day": str(r["day"]), "bars": 0, "ticks": int(r["ticks"])} for _, r in tick_df.iterrows()])
        return jsonify([])

    result = []
    seen = set()
    for _, r in candle_df.iterrows():
        day = str(r["day"])
        seen.add(day)
        result.append({"day": day, "bars": int(r["bars"]), "ticks": tick_map.get(day, 0)})
    # Add tick-only dates not already in candle dates
    for _, r in (tick_df.iterrows() if not tick_df.empty else []):
        day = str(r["day"])
        if day not in seen:
            result.append({"day": day, "bars": 0, "ticks": int(r["ticks"])})
    result.sort(key=lambda x: x["day"])
    return jsonify(result)


@app.route("/api/options/expiries")
def api_option_expiries():
    """Distinct expiries available for a given date.

    Sources (merged):
      1. Option symbols in minute_candles for that date
      2. Option symbols in tick_data for that date
      3. All known expiry dates from DB that are >= the selected date (future expiries)
    """
    dt = request.args.get("date")
    if not dt:
        return jsonify([])

    import re
    expiries = set()

    # 1. minute_candles
    df = read_sql(
        "SELECT DISTINCT symbol FROM minute_candles "
        "WHERE (symbol LIKE 'NIFTY%CE' OR symbol LIKE 'NIFTY%PE') "
        "AND timestamp::date = :dt",
        {"dt": dt},
    )
    for sym in (df["symbol"] if not df.empty else []):
        m = re.match(r"NIFTY(\d{6})", sym)
        if m:
            try:
                expiries.add(datetime.strptime(f"20{m.group(1)}", "%Y%m%d").strftime("%Y-%m-%d"))
            except ValueError:
                pass

    # 2. tick_data
    df2 = read_sql(
        "SELECT DISTINCT symbol FROM tick_data "
        "WHERE (symbol LIKE 'NIFTY%CE' OR symbol LIKE 'NIFTY%PE') "
        "AND timestamp::date = :dt",
        {"dt": dt},
    )
    for sym in (df2["symbol"] if not df2.empty else []):
        m = re.match(r"NIFTY(\d{6})", sym)
        if m:
            try:
                expiries.add(datetime.strptime(f"20{m.group(1)}", "%Y%m%d").strftime("%Y-%m-%d"))
            except ValueError:
                pass

    # 3. All known future expiries (from option symbols in DB) >= selected date
    all_exp = read_sql("""
        SELECT DISTINCT SUBSTRING(symbol FROM 6 FOR 6) as exp_code
        FROM minute_candles
        WHERE symbol ~ '^NIFTY[0-9]{6}[0-9]+(CE|PE)$'
    """)
    for _, r in (all_exp.iterrows() if not all_exp.empty else []):
        try:
            d = datetime.strptime(f"20{r['exp_code']}", "%Y%m%d")
            if d.strftime("%Y-%m-%d") >= dt:
                expiries.add(d.strftime("%Y-%m-%d"))
        except ValueError:
            pass

    if not expiries:
        return jsonify([])
    return jsonify(sorted(expiries))


@app.route("/api/options/chain")
def api_option_chain():
    """Option chain for a date + expiry: all strikes with last price, volume, OI."""
    dt = request.args.get("date")
    expiry = request.args.get("expiry")  # YYYY-MM-DD
    if not dt or not expiry:
        return jsonify({"error": "date and expiry required"}), 400

    # Convert expiry to YYMMDD code
    try:
        exp_code = datetime.strptime(expiry, "%Y-%m-%d").strftime("%y%m%d")
    except ValueError:
        return jsonify({"error": "invalid expiry format"}), 400

    pattern = f"NIFTY{exp_code}%"

    # Try minute_candles first (1-min bars aggregated to day-end snapshot)
    df = read_sql(
        "SELECT symbol, "
        "  (array_agg(close ORDER BY timestamp DESC))[1] as last_price, "
        "  SUM(volume) as volume, "
        "  MAX(oi) as oi "
        "FROM minute_candles "
        "WHERE symbol LIKE :pat AND timestamp::date = :dt "
        "GROUP BY symbol",
        {"pat": pattern, "dt": dt},
    )
    if df.empty:
        # Fallback to tick_data
        df = read_sql(
            "SELECT symbol, "
            "  (array_agg(price ORDER BY timestamp DESC))[1] as last_price, "
            "  SUM(volume) as volume, "
            "  MAX(oi) as oi "
            "FROM tick_data "
            "WHERE symbol LIKE :pat AND timestamp::date = :dt "
            "GROUP BY symbol",
            {"pat": pattern, "dt": dt},
        )

    if df.empty:
        return jsonify([])

    import re
    chain = []
    for _, r in df.iterrows():
        sym = r["symbol"]
        m = re.match(r"NIFTY\d{6}(\d+)(CE|PE)", sym)
        if m:
            chain.append({
                "symbol": sym,
                "strike": int(m.group(1)),
                "type": m.group(2),
                "last_price": round(float(r["last_price"] or 0), 2),
                "volume": int(r["volume"] or 0),
                "oi": int(r["oi"] or 0),
            })
    chain.sort(key=lambda x: (x["strike"], x["type"]))
    return jsonify(chain)


@app.route("/api/options/ticks")
def api_option_ticks():
    """Tick-level data for a specific option symbol on a specific date."""
    symbol = request.args.get("symbol")
    dt = request.args.get("date")
    if not symbol or not dt:
        return jsonify({"error": "symbol and date required"}), 400

    # Try tick_data table first
    df = read_sql(
        "SELECT timestamp, price, volume, oi, bid_price, ask_price "
        "FROM tick_data WHERE symbol = :sym AND timestamp::date = :dt "
        "ORDER BY timestamp",
        {"sym": symbol, "dt": dt},
    )
    source = "ticks"

    if df.empty:
        # Fallback to minute_candles
        df = read_sql(
            "SELECT timestamp, open, high, low, close, volume, oi "
            "FROM minute_candles WHERE symbol = :sym AND timestamp::date = :dt "
            "ORDER BY timestamp",
            {"sym": symbol, "dt": dt},
        )
        source = "candles"

    if df.empty:
        return jsonify({"data": [], "source": source})

    df["timestamp"] = df["timestamp"].astype(str)
    return jsonify({"data": df.to_dict(orient="records"), "source": source})


# ── Broker Execution API ────────────────────────────────────────────────────

from broker.order_manager import OrderManager
from broker.angelone_adapter import AngelOneAdapter
from broker.mstock_adapter import MStockAdapter

# One adapter instance per broker, always available to the Connect/status
# routes below regardless of TRADE_MODE. In paper mode these exist purely so
# the dashboard's Connect cards can verify credentials work without needing
# to flip into live trading first. Whichever one matches TRADE_MODE is the
# SAME instance OrderManager executes orders through — connecting via the
# dashboard IS connecting the live trading session, not a second independent
# one. mStock is the intended broker for live trading going forward; AngelOne
# stays available (and still powers all market data — see data/market_data_adapter.py).
angelone_adapter = AngelOneAdapter()
mstock_adapter = MStockAdapter()
_trade_mode = os.getenv("TRADE_MODE", "paper").lower()
_live_adapter = {"angelone": angelone_adapter, "mstock": mstock_adapter}.get(_trade_mode)
order_manager = OrderManager(adapter=_live_adapter)


@app.route("/api/broker/status")
def api_broker_status():
    """Return broker connection state, positions, daily P&L."""
    return jsonify(order_manager.to_dict())


@app.route("/api/broker/connect", methods=["POST"])
def api_broker_connect():
    """Authenticate with the configured broker."""
    ok = order_manager.connect()
    return jsonify({"connected": ok, "broker": order_manager.adapter.broker_name})


@app.route("/api/broker/auth/status")
def api_broker_auth_status():
    """
    Two ways to be connected per broker: the automatic .env path (credentials
    + TOTP secret, computed via pyotp — see OrderManager.connect(), used at
    Flask startup) or each broker's manual dashboard Connect below. Either
    way, this just reports whether it produced a live session — there's no
    OAuth redirect step for either broker. Reports on the shared adapter
    instances regardless of TRADE_MODE, so this works even in paper mode —
    both can be connected independently (connecting is just "verify
    credentials work", not "start live trading").
    """
    return jsonify({
        # Kept at top level for backwards compatibility with the existing
        # AngelOne Connect card.
        "connected": angelone_adapter.is_connected,
        "broker": "AngelOne",
        "client_code": angelone_adapter.client_code if angelone_adapter.is_connected else None,
        "angelone": {
            "connected": angelone_adapter.is_connected,
            "client_code": angelone_adapter.client_code if angelone_adapter.is_connected else None,
        },
        "mstock": {
            "connected": mstock_adapter.is_connected,
            "user_id": mstock_adapter.user_id if mstock_adapter.is_connected else None,
        },
        "trade_mode": _trade_mode,
    })


@app.route("/api/broker/angelone/connect", methods=["POST"])
def api_broker_angelone_connect():
    """
    Manual Connect: client ID, PIN/password, and the current 6-digit TOTP
    code (read off the user's own authenticator app) are typed into the
    dashboard and sent straight here — this request never passes through
    any chat session, and none of these values are logged. Works
    regardless of TRADE_MODE (see `angelone_adapter` above) — connecting
    here in paper mode just verifies your credentials without placing you
    in live trading.
    """
    body = request.get_json(force=True, silent=True) or {}
    client_code = str(body.get("client_code", "")).strip()
    pin = str(body.get("pin", "")).strip()
    totp = str(body.get("totp", "")).strip()

    if not (client_code and pin and totp):
        return jsonify({"connected": False, "error": "client_code, pin, and totp are all required"}), 400

    ok = angelone_adapter.connect_manual(client_code, pin, totp)
    return jsonify({
        "connected": ok,
        "client_code": angelone_adapter.client_code if ok else None,
        # AngelOne's own rejection reason (e.g. "Invalid Password", "AB1050:
        # Invalid Totp") — never a credential value, safe to send back.
        "error": None if ok else angelone_adapter.last_error,
    })


@app.route("/api/broker/angelone/disconnect", methods=["POST"])
def api_broker_angelone_disconnect():
    """Drop the current AngelOne session — dashboard 'Disconnect' action."""
    angelone_adapter.disconnect()
    return jsonify({"connected": False})


@app.route("/api/broker/mstock/connect", methods=["POST"])
def api_broker_mstock_connect():
    """
    Manual Connect: user ID, password, and the current 6-digit TOTP code
    (read off the user's own authenticator app) are typed into the
    dashboard and sent straight here — this request never passes through
    any chat session, and none of these values are logged. Works
    regardless of TRADE_MODE (see `mstock_adapter` above) — connecting
    here in paper mode just verifies your credentials without placing you
    in live trading.
    """
    body = request.get_json(force=True, silent=True) or {}
    user_id = str(body.get("user_id", "")).strip()
    password = str(body.get("password", "")).strip()
    totp = str(body.get("totp", "")).strip()

    if not (user_id and password and totp):
        return jsonify({"connected": False, "error": "user_id, password, and totp are all required"}), 400

    ok = mstock_adapter.connect_manual(user_id, password, totp)
    return jsonify({
        "connected": ok,
        "user_id": mstock_adapter.user_id if ok else None,
        # mStock's own rejection reason — never a credential value, safe to
        # send back.
        "error": None if ok else mstock_adapter.last_error,
    })


@app.route("/api/broker/mstock/disconnect", methods=["POST"])
def api_broker_mstock_disconnect():
    """Drop the current mStock session — dashboard 'Disconnect' action."""
    mstock_adapter.disconnect()
    return jsonify({"connected": False})


@app.route("/api/broker/mstock/positions")
def api_broker_mstock_positions():
    """
    Real open positions from the connected mStock account — read-only,
    queries the SAME session the Connect card established (mstock_adapter
    above), regardless of TRADE_MODE. Separate from /api/broker/status
    (which reports on order_manager's active adapter — Paper while
    TRADE_MODE=paper, even if mStock is connected here).
    """
    if not mstock_adapter.is_connected:
        return jsonify({"error": "mStock not connected"}), 400
    positions = mstock_adapter.get_positions()
    return jsonify([p.__dict__ for p in positions])


@app.route("/api/broker/mstock/margins")
def api_broker_mstock_margins():
    """Real available funds/margin from the connected mStock account — read-only."""
    if not mstock_adapter.is_connected:
        return jsonify({"error": "mStock not connected"}), 400
    return jsonify(mstock_adapter.get_margins())


@app.route("/api/broker/kill", methods=["POST"])
def api_broker_kill():
    """EMERGENCY: Kill switch — close all positions, halt trading."""
    result = order_manager.kill_switch()
    return jsonify(result)


@app.route("/api/broker/resume", methods=["POST"])
def api_broker_resume():
    """Resume trading after a halt."""
    result = order_manager.resume()
    return jsonify(result)


@app.route("/api/broker/reconcile")
def api_broker_reconcile():
    """Compare internal positions vs broker positions."""
    return jsonify(order_manager.reconcile())


@app.route("/api/broker/confirm", methods=["POST"])
def api_broker_confirm_signal():
    """Confirm a pending signal (manual confirmation mode)."""
    body = request.get_json(force=True) if request.is_json else {}
    index = body.get("index", 0)
    result = order_manager.confirm_signal(index)
    return jsonify(result)


@app.route("/api/broker/reject", methods=["POST"])
def api_broker_reject_signal():
    """Reject a pending signal (manual confirmation mode)."""
    body = request.get_json(force=True) if request.is_json else {}
    index = body.get("index", 0)
    result = order_manager.reject_signal(index)
    return jsonify(result)


@app.route("/api/broker/exit", methods=["POST"])
def api_broker_exit():
    """Manually exit a specific position by order_id."""
    body = request.get_json(force=True) if request.is_json else {}
    order_id = body.get("order_id", "")
    price = float(body.get("price", 0))
    if not order_id:
        return jsonify({"error": "order_id required"}), 400
    result = order_manager.exit_position(order_id, price=price, reason="MANUAL_EXIT")
    return jsonify(result)


# ── Predictions API (NSE-Neuron: LSTM/BiLSTM/GRU/CNN-LSTM forecasting) ──────
# Runs training/inference on request, not in a background thread — a forecast
# call can take a while (model train or warm-start), so the dashboard shows
# a loading state rather than this blocking the scanner loop.


def _missing_dependency_message(e: ModuleNotFoundError) -> str:
    """
    predictions/ needs tensorflow + scikit-learn (heavy, ~1GB+ combined) on
    top of the base requirements.txt — a fresh install commonly hasn't run
    `pip install -r requirements.txt` to completion yet. Surface that
    directly instead of a bare "No module named 'x'".
    """
    return (
        f"Missing Python package: {e.name}. Run `pip install -r requirements.txt` "
        f"(the AI Forecast tab needs tensorflow + scikit-learn on top of the base "
        f"install — that's a large download, give it a few minutes)."
    )


@app.route("/api/predictions/forecast")
def api_predictions_forecast():
    """
    GET /api/predictions/forecast?symbol=NIFTY&algorithm=lstm&force_retrain=false
    algorithm: lstm | bilstm | gru | cnn_lstm | all
    """
    symbol = request.args.get("symbol", "NIFTY").strip().upper()
    algorithm = request.args.get("algorithm", "lstm").strip().lower()
    force_retrain = request.args.get("force_retrain", "false").lower() == "true"

    try:
        from predictions.forecast import forecast_symbol, forecast_all
        if algorithm == "all":
            result = forecast_all(symbol, force_retrain=force_retrain)
        else:
            result = forecast_symbol(symbol, algorithm=algorithm, force_retrain=force_retrain)
        return jsonify(result)
    except ModuleNotFoundError as e:
        logger.error(f"Prediction failed for {symbol}: {e}")
        return jsonify({"error": _missing_dependency_message(e)}), 500
    except Exception as e:
        logger.error(f"Prediction failed for {symbol}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/predictions/regime")
def api_predictions_regime():
    """GET /api/predictions/regime?symbol=NIFTY — regime + candlestick patterns, no model training."""
    symbol = request.args.get("symbol", "NIFTY").strip().upper()
    try:
        from predictions.forecast import regime_analysis
        return jsonify(regime_analysis(symbol))
    except ModuleNotFoundError as e:
        logger.error(f"Regime analysis failed for {symbol}: {e}")
        return jsonify({"error": _missing_dependency_message(e)}), 500
    except Exception as e:
        logger.error(f"Regime analysis failed for {symbol}: {e}")
        return jsonify({"error": str(e)}), 500


# ── Market Scanner API (free NSE EOD bhavcopy via jugaad-data) ──────────────


@app.route("/api/scanner/scan")
def api_scanner_scan():
    """
    GET /api/scanner/scan?universe=all&direction=all&min_confidence=0&search=&limit=100&index_list=&option_type=&nearest_expiry_only=true&sector=&index_type=&sort_by=confidence&include_option_legs=false
    Same candle-quality scoring as math_decision_engine's analyse_side,
    applied across the whole published EOD board. See strategy/market_scanner.py.
    index_list: "" | nifty50 | nifty100 | nifty200 | nifty500 — restricts
    stock rows to that index's current constituents (free NSE archive list).
    universe="options" scores individual CE/PE contracts (strike/expiry/OI/
    volume included) instead of the underlying's own candle; option_type
    ("CE"/"PE") and nearest_expiry_only further narrow that universe.
    sector: exact Industry match (from the same constituent list) — stocks only.
    index_type: "" | "fno" | "traded" — restricts index rows to F&O-tradable ones.
    sort_by: "confidence" | "rr" | "change" | "turnover".
    include_option_legs: when true, resolves the ATM CE/PE for the top-ranked
    index/stock rows and runs them through the Trade Decision Engine.
    """
    try:
        from strategy.market_scanner import scan_market
        result = scan_market(
            universe=request.args.get("universe", "all"),
            direction=request.args.get("direction", "all"),
            min_confidence=float(request.args.get("min_confidence", 0)),
            search=request.args.get("search", ""),
            limit=int(request.args.get("limit", 100)),
            index_list=request.args.get("index_list", ""),
            option_type=request.args.get("option_type", ""),
            nearest_expiry_only=request.args.get("nearest_expiry_only", "true").lower() != "false",
            sector=request.args.get("sector", ""),
            index_type=request.args.get("index_type", ""),
            sort_by=request.args.get("sort_by", "confidence"),
            include_option_legs=request.args.get("include_option_legs", "false").lower() == "true",
            top_n_option_legs=int(request.args.get("top_n_option_legs", 10)),
        )
        return jsonify(result)
    except Exception as e:
        logger.error(f"Market scan failed: {e}")
        return jsonify({"error": str(e)}), 500


# ── Pre-Market API (NextDay Direction Analyser + Option Trade Decision Engine — Live) ──


@app.route("/api/premarket/nextday")
def api_premarket_nextday():
    """
    GET /api/premarket/nextday?symbol=NIFTY
    NextDay Direction Analyser: classic floor pivots on the last completed
    session's H/L/C, read before the bell. See strategy/premarket.py.
    """
    try:
        from strategy.premarket import nextday_reading
        result = nextday_reading(request.args.get("symbol", "NIFTY").upper())
        status = 200 if "error" not in result else 404
        return jsonify(result), status
    except Exception as e:
        logger.error(f"NextDay reading failed: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/premarket/live")
def api_premarket_live():
    """
    GET /api/premarket/live?symbol=NIFTY&timeframe=5min&mode=opening|latest
    Option Trade Decision Engine — Live: runs analyse_option_pair on the
    fixed 09:15-09:20 opening ATM CE/PE candle (mode=opening) or the latest
    fully closed timeframe candle (mode=latest, default), and reports
    whether that side still agrees with the NextDay bias and the day's
    opening reading. Needs an active AngelOne session (Connect in the
    sidebar) — this reads live option candles, not free EOD data.
    """
    try:
        from strategy.premarket import live_confirmation
        result = live_confirmation(
            symbol=request.args.get("symbol", "NIFTY").upper(),
            timeframe=request.args.get("timeframe", "5min"),
            mode=request.args.get("mode", "latest"),
            # A user-facing button click, not the agent's tight poll loop -- can afford one
            # extra wait-and-retry on a transient AngelOne refusal. See live_confirmation's
            # own docstring on why the agent's automated call must NOT set this.
            retry_on_refusal=True,
        )
        status = 200 if "error" not in result else 409
        return jsonify(result), status
    except Exception as e:
        logger.error(f"Live confirmation failed: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/premarket/value-calc")
def api_premarket_value_calc():
    """
    GET /api/premarket/value-calc?v1=..&v2=..&v3=..&v4=..&v5=..&v6=..
    Value Calculator from the reference app's Trading Toolkit: six premiums
    (call entry/T1/T2, put entry/T1/T2). /api/premarket/live already returns
    this auto-fed from the fetched candles; this route recalculates from
    values the user edited on the Pre Market page.
    """
    try:
        from strategy.math_decision_strategy import value_calculator
        missing = [f"v{i}" for i in range(1, 7) if not request.args.get(f"v{i}", "").strip()]
        if missing:
            return jsonify({"error": f"Missing value(s): {', '.join(missing)} — need six positive numbers v1..v6"}), 400
        values = [float(request.args[f"v{i}"]) for i in range(1, 7)]
        return jsonify(value_calculator(values))
    except ValueError as e:
        return jsonify({"error": f"Need six positive numbers v1..v6 ({e})"}), 400
    except Exception as e:
        logger.error(f"Value calculator failed: {e}")
        return jsonify({"error": str(e)}), 500


# ── Intraday Math-Engine Agent (PAPER only — see strategy/intraday_agent.py) ──


@app.route("/api/agent/intraday/status")
def api_agent_intraday_status():
    """Armed flag, open/closed agent positions, and the rolling decision log."""
    from strategy.intraday_agent import status as agent_status
    return jsonify(agent_status())


@app.route("/api/agent/intraday/arm", methods=["POST"])
def api_agent_intraday_arm():
    """
    POST {"armed": true|false} — arm or stop the agent.

    The agent starts DISARMED on every backend start and must be armed
    explicitly: it opens paper positions with no per-trade confirmation, so a
    restart silently resuming that is exactly the behaviour to avoid.
    """
    from strategy.intraday_agent import arm
    body = request.get_json(silent=True) or {}
    return jsonify(arm(bool(body.get("armed", True))))


@app.route("/api/agent/intraday/approvals")
def api_agent_intraday_approvals():
    """
    Pending approval requests (AI-platform roadmap Phase 3) — only ever
    populated once TRADE_MODE leaves "paper"; in paper mode the agent
    auto-fires directly and this is always empty.
    """
    from strategy.intraday_agent import list_pending_approvals
    return jsonify(list_pending_approvals())


@app.route("/api/agent/intraday/approvals/<approval_id>/approve", methods=["POST"])
def api_agent_intraday_approve(approval_id):
    """
    Re-validates (price tolerance, strategy health, market hours) and, only
    if all of that still holds, opens the position for real. See
    models/approval.py for exactly what can void an approval between
    staging and this call.
    """
    from strategy.intraday_agent import approve_request
    result = approve_request(approval_id)
    return jsonify(result), (200 if result.get("ok") else 400)


@app.route("/api/agent/intraday/approvals/<approval_id>/reject", methods=["POST"])
def api_agent_intraday_reject(approval_id):
    """POST {"reason": "..."} (optional) — reject a pending approval request."""
    from strategy.intraday_agent import reject_request
    body = request.get_json(silent=True) or {}
    result = reject_request(approval_id, body.get("reason", ""))
    return jsonify(result), (200 if result.get("ok") else 400)


def _intraday_agent_loop():
    """
    Background thread: entry pass every ENTRY_INTERVAL_SECS, exit check every
    EXIT_CHECK_INTERVAL_SECS. Both are no-ops while disarmed or out of hours,
    so this can run from startup without doing anything until armed.
    """
    from strategy.intraday_agent import (
        run_cycle, check_exits, ENTRY_INTERVAL_SECS, EXIT_CHECK_INTERVAL_SECS,
    )
    logger.info("Intraday agent thread started (disarmed).")
    last_entry = 0.0
    while True:
        try:
            check_exits()
            if time.time() - last_entry >= ENTRY_INTERVAL_SECS:
                last_entry = time.time()
                run_cycle()
        except Exception as e:
            logger.error(f"Intraday agent loop error: {e}")
        time.sleep(EXIT_CHECK_INTERVAL_SECS)


RECONCILIATION_INTERVAL_SECS = 120


def _reconciliation_loop():
    """
    Background thread (AI-platform roadmap Phase 4): periodically compares
    order_manager's internal position state against the broker's actual
    positions, and HALTS new signal submission on any discrepancy —
    "do not silently overwrite data; an unreconciled state halts
    automation." See broker/order_manager.py's reconcile(auto_halt=True)
    for exactly what counts as a discrepancy (PHANTOM/ORPHAN/QTY_MISMATCH)
    and how the halt reuses the existing kill-switch/max-daily-loss
    mechanism rather than a parallel one.

    Only runs meaningfully once TRADE_MODE leaves "paper" — PaperAdapter's
    positions are order_manager's own internal book by construction, so
    reconciling paper-mode against itself can never find anything and
    would just be a periodic no-op. Checked on every loop (not just at
    thread start) so flipping TRADE_MODE via a backend restart is picked
    up without needing this thread's own restart logic.
    """
    logger.info("Reconciliation thread started.")
    while True:
        try:
            if os.getenv("TRADE_MODE", "paper").lower() != "paper":
                order_manager.reconcile(auto_halt=True)
        except Exception as e:
            logger.error(f"Reconciliation loop error: {e}")
        time.sleep(RECONCILIATION_INTERVAL_SECS)


# ── Delivery / Swing Agent (suggest-only; PAPER positions) ──────────────────

# Kept separate from paper_positions_by_mode on purpose: that store is
# options-shaped (entry_premium, expiry, lots, premium decay, forced EOD
# close). A delivery holding has none of those — it's plain quantity × price,
# held across days.
delivery_positions: list = []


@app.route("/api/agent/delivery/picks")
def api_agent_delivery_picks():
    """
    GET /api/agent/delivery/picks?min_confidence=100&top_n=10

    Scanner rows at/above min_confidence, kept only where AI Forecast's
    nearest-day call agrees with the scanner's side. Returns the dropped
    candidates too (with the reason), so "no picks" is distinguishable from
    "something broke".

    Slow by nature — each confirmation trains/loads a per-symbol model, hence
    the top_n cap and the per-day cache in strategy/delivery_agent.py.
    """
    try:
        from strategy.delivery_agent import get_confirmed_picks
        result = get_confirmed_picks(
            min_confidence=float(request.args.get("min_confidence", 100)),
            top_n=int(request.args.get("top_n", 10)),
            min_qty=int(request.args.get("min_qty", 1)),
            algorithm=request.args.get("algorithm", "lstm"),
        )
        return jsonify(result), 200 if "error" not in result else 404
    except Exception as e:
        logger.error(f"Delivery picks failed: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/positional/scan")
def api_positional_scan():
    """Regime + sector leaders + IPO/VCP/horizontal/flag setups from the local EOD store."""
    try:
        from strategy.positional_service import get_scan
        result = get_scan()
        return jsonify(result), 200 if "error" not in result else 404
    except Exception as e:
        logger.error(f"positional scan failed: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/positional/chart")
def api_positional_chart():
    try:
        from strategy.positional_service import get_chart
        sym = (request.args.get("symbol") or "").strip()
        if not sym:
            return jsonify({"error": "symbol is required"}), 400
        data = get_chart(sym, int(request.args.get("sessions", 90)))
        return (jsonify(data), 200) if data else (jsonify({"error": f"no data for {sym}"}), 404)
    except Exception as e:
        logger.error(f"positional chart failed: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/positional/intraday-hammer")
def api_positional_intraday_hammer():
    """ChartBank 30-minute hammer signals on the collected NIFTY-I minute candles (read-only)."""
    try:
        from strategy.positional_service import get_intraday_hammer
        return jsonify(get_intraday_hammer(request.args.get("symbol", "NIFTY-I")))
    except Exception as e:
        logger.error(f"intraday hammer failed: {e}")
        return jsonify({"error": str(e), "signals": []}), 500


@app.route("/api/intraday/scan")
def api_intraday_scan():
    """Intraday Engine: regime, sectors and 5-minute setups, with the live-sync report attached."""
    try:
        from strategy.intraday_service import get_scan
        return jsonify(get_scan())
    except Exception as e:
        logger.error(f"intraday scan failed: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/intraday/chart")
def api_intraday_chart():
    try:
        from strategy.intraday_service import get_chart
        sym = (request.args.get("symbol") or "").strip()
        if not sym:
            return jsonify({"error": "symbol is required"}), 400
        data = get_chart(sym, int(request.args.get("sessions", 2)))
        return (jsonify(data), 200) if data else (jsonify({"error": f"no bars loaded for {sym}"}), 404)
    except Exception as e:
        logger.error(f"intraday chart failed: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/intraday/sync", methods=["POST"])
def api_intraday_sync():
    """Refresh now instead of waiting for the next 5-minute bar close."""
    from strategy.intraday_service import sync_now
    return jsonify(sync_now())


@app.route("/api/positional/sync", methods=["GET", "POST"])
def api_positional_sync():
    """POST starts the EOD bhavcopy sync + replay refresh in the background; GET reports progress."""
    from strategy.positional_service import start_sync, sync_status
    return jsonify(start_sync() if request.method == "POST" else sync_status())


@app.route("/api/agent/delivery/positions")
def api_agent_delivery_positions():
    """Open + closed delivery paper positions, with live P&L where priced."""
    return jsonify({"positions": delivery_positions})


@app.route("/api/agent/delivery/enter", methods=["POST"])
def api_agent_delivery_enter():
    """
    POST {symbol, side, qty, entry_price, ...} — record a delivery paper
    position. Human-initiated: the delivery agent only suggests, it never
    calls this itself.
    """
    body = request.get_json(force=True) or {}
    symbol = body.get("symbol")
    side = (body.get("side") or "LONG").upper()
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    if side not in ("LONG", "SHORT"):
        return jsonify({"error": "side must be LONG or SHORT"}), 400
    try:
        qty = int(body.get("qty", 1))
        entry_price = float(body.get("entry_price") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "qty and entry_price must be numeric"}), 400
    if qty <= 0 or entry_price <= 0:
        return jsonify({"error": "qty and entry_price must be positive"}), 400

    pos = {
        "id": f"{symbol}-{int(time.time())}",
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "entry_price": round(entry_price, 2),
        # Same tagging convention as the intraday agent: which automation
        # surfaced it, and which models actually made the call. Delivery
        # picks are a two-model agreement, so both are named.
        "agent": "delivery_agent",
        "model": "market_scanner+nse_neuron_forecast",
        "model_label": "Candle-quality scan, confirmed by AI Forecast",
        "source": "agent-suggested, human-placed",
        "target1": body.get("target1"),
        "target2": body.get("target2"),
        "stop_loss": body.get("stop_loss"),
        "scanner_confidence": body.get("scanner_confidence"),
        "forecast_label": body.get("forecast_label"),
        "product": "CNC",          # delivery
        "status": "OPEN",
        "entry_time": datetime.now().isoformat(),
        "current_price": None,
        "unrealised_pnl": None,
    }
    delivery_positions.append(pos)
    logger.info(f"DELIVERY PAPER ENTRY: {side} {qty} x {symbol} @ {pos['entry_price']}")
    return jsonify(pos)


@app.route("/api/agent/delivery/exit", methods=["POST"])
def api_agent_delivery_exit():
    """POST {id, exit_price} — close a delivery paper position."""
    body = request.get_json(force=True) or {}
    pos_id = body.get("id")
    pos = next((p for p in delivery_positions if p["id"] == pos_id and p["status"] == "OPEN"), None)
    if pos is None:
        return jsonify({"error": f"No open delivery position with id {pos_id!r}"}), 404
    try:
        exit_price = float(body.get("exit_price") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "exit_price must be numeric"}), 400
    if exit_price <= 0:
        return jsonify({"error": "exit_price must be positive"}), 400

    sign = 1 if pos["side"] == "LONG" else -1
    pos["exit_price"] = round(exit_price, 2)
    pos["exit_time"] = datetime.now().isoformat()
    pos["status"] = "CLOSED"
    pos["pnl"] = round((exit_price - pos["entry_price"]) * pos["qty"] * sign, 2)
    logger.info(f"DELIVERY PAPER EXIT: {pos['symbol']} @ {pos['exit_price']} P&L {pos['pnl']}")

    # File it into the same closed-trade history the Trades page reads, so
    # delivery trades sit alongside intraday ones instead of only living on
    # the Delivery tab. Open delivery holdings deliberately stay off the Live
    # board: that view is intraday-option shaped (lots, premium, expiry) and a
    # multi-day equity holding would render as nonsense there.
    _closed_trades_by_mode.setdefault("test", []).append({
        "id": pos["id"], "mode": "test",
        "symbol": pos["symbol"], "direction": pos["side"],
        "strategy": f"{pos.get('model', 'delivery')} ({pos.get('agent', 'delivery_agent')})",
        "agent": pos.get("agent"), "model": pos.get("model"),
        "model_label": pos.get("model_label"), "source": pos.get("source"),
        "entry_premium": pos["entry_price"], "exit_premium": pos["exit_price"],
        "lot_size": pos["qty"], "lots": 1,
        "status": "CLOSED", "entry_time": pos["entry_time"], "exit_time": pos["exit_time"],
        "exit_reason": "MANUAL", "pnl": pos["pnl"], "realised_pnl": pos["pnl"],
        "unrealised_pnl": 0,
    })
    return jsonify(pos)


# ── News Brief API (free RSS: ET, LiveMint, RBI, SEBI, Google News) ─────────


@app.route("/api/news/brief")
def api_news_brief():
    """
    GET /api/news/brief?hours=24 — latest market news with keyword-based
    sentiment scoring. No API key required for any of it.
    """
    hours = int(request.args.get("hours", 24))
    try:
        from data.news_sentiment import NewsSentimentEngine

        engine = NewsSentimentEngine()
        articles = engine.fetch_all(max_age_hours=hours)

        try:
            engine.store(articles)
            sentiment = engine.get_market_sentiment(lookback_hours=hours)
        except Exception as e:
            logger.warning(f"News sentiment DB read/write skipped: {e}")
            sentiment = None

        return jsonify({
            "articles": [
                {
                    "title": a.title, "url": a.url, "source": a.source,
                    "published_at": a.published_at.isoformat(),
                    "summary": a.summary, "symbols": a.symbols,
                    "sentiment_label": a.sentiment_label, "sentiment_score": round(a.sentiment_score, 2),
                    "impact_level": a.impact_level,
                }
                for a in articles
            ],
            "sentiment": sentiment,
        })
    except Exception as e:
        logger.error(f"News brief failed: {e}")
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    initialize()

    # Connect broker adapter (paper by default)
    order_manager.connect()

    # Start background scanner
    scanner_thread = threading.Thread(target=background_scanner, daemon=True)
    scanner_thread.start()

    # Hand the agent THIS process's real stores. It must not import
    # backend.app to find them: started as `python backend/app.py` this file
    # is __main__, so that import builds a second copy of the module with its
    # own empty paper book, and every mirrored agent trade lands in a phantom
    # list no route serves. Registration keeps one source of truth.
    #
    # The closed-trade sink deliberately does NOT go through
    # _persist_closed_trade: that also drives the consecutive-SL cooldown
    # which pauses the MAIN scanner's entries, and an agent stop has no
    # business halting a separate system's trading.
    def _persist_agent_trade(trade: dict):
        _closed_trades_by_mode.setdefault("test", []).append(trade)
        try:
            with open(_paper_trades_file("test"), "a") as f:
                f.write(json.dumps(trade, default=str) + "\n")
        except Exception as e:
            logger.warning(f"Failed to persist agent trade: {e}")

    from strategy.intraday_agent import set_paper_book as _agent_bind
    _agent_bind(paper_positions_by_mode, _persist_agent_trade)

    # Intraday math-engine agent — runs disarmed until explicitly armed via
    # POST /api/agent/intraday/arm, so this thread is inert on a fresh start.
    threading.Thread(target=_intraday_agent_loop, daemon=True, name="intraday-agent").start()

    # Reconciliation — inert (checks TRADE_MODE every cycle) while paper.
    threading.Thread(target=_reconciliation_loop, daemon=True, name="reconciliation").start()

    # Auto-start tick monitor and data collector during market hours
    _ensure_tick_monitor()
    _ensure_collector()

    _default_port = 5050
    port = int(os.environ.get("PORT") or _default_port)

    trade_mode = os.getenv("TRADE_MODE", "paper")
    print("\n" + "=" * 50)
    print("  AI Trader Dashboard")
    print(f"  Mode:       {trade_mode.upper()}")
    print(f"  Broker:     {order_manager.adapter.broker_name}")
    print(f"  Flask API:  http://localhost:{port}")
    print("  Next.js UI: http://localhost:3000")
    print(f"  SSE Stream: http://localhost:{port}/api/stream")
    print(f"  Kill switch: POST http://localhost:{port}/api/broker/kill")
    print("  Press Ctrl+C to stop")
    print("=" * 50 + "\n")

    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
