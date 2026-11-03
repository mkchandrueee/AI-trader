import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent
MODEL_DIR = BASE_DIR / os.getenv("MODEL_DIR", "models/saved")
LOG_DIR = BASE_DIR / os.getenv("LOG_DIR", "logs")
MODEL_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ── Database (TimescaleDB) ─────────────────────────────────────────────────────
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "trading")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_URL = (
    f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    if DB_PASSWORD
    else f"postgresql://{DB_USER}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
)

# ── AngelOne SmartAPI (broker + market data) ───────────────────────────────────
# Free for retail accounts — no separate API subscription like Kite Connect.
# Auth is fully non-interactive: client code + password/PIN live in .env, and
# the TOTP secret (base32, from enabling TOTP on the Angel One app) is used to
# compute the live 6-digit code at auth time via pyotp — no browser/OAuth step.
# Never run the login yourself from chat — see scripts/angelone_login_setup.py.
ANGEL_API_KEY = os.getenv("ANGEL_API_KEY", "")
ANGEL_CLIENT_CODE = os.getenv("ANGEL_CLIENT_CODE", "")
ANGEL_PASSWORD_OR_PIN = os.getenv("ANGEL_PASSWORD_OR_PIN", "")
ANGEL_TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET", "")

# AngelOne SmartAPI endpoints
ANGEL_INSTRUMENT_MASTER_URL = (
    "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
)
ANGEL_WS_URL = "wss://smartapisocket.angelone.in/smart-stream"

# ── mStock Trading API (Type A — Kite-Connect-shaped) ──────────────────────────
# The broker going forward for live order execution once TRADE_MODE leaves
# "paper" (see broker/mstock_adapter.py). Auth mirrors AngelOne's shape: user
# ID + password live in .env, and the TOTP secret (base32, from enabling TOTP
# at trade.mstock.com -> Hamburger Menu -> Key Products -> Trading APIs ->
# Enable TOTP) computes the live 6-digit code at auth time via pyotp — no
# browser/OAuth step. Never run the login yourself from chat — see
# scripts/mstock_check_auth.py, which the user runs locally.
# Install: pip install mStock-TradingApi-A
MSTOCK_API_KEY = os.getenv("MSTOCK_API_KEY", "")
MSTOCK_USER_ID = os.getenv("MSTOCK_USER_ID", "")
MSTOCK_PASSWORD = os.getenv("MSTOCK_PASSWORD", "")
MSTOCK_TOTP_SECRET = os.getenv("MSTOCK_TOTP_SECRET", "")

MSTOCK_WS_URL = "wss://ws.mstock.trade"

# ── jugaad-data (free NSE/BSE EOD + index history) ─────────────────────────────
# Used for EOD bhavcopy / index OHLC / F&O historical backfill — not a live
# tick source. Live intraday ticks + minute candles come from AngelOne above.
JUGAAD_CACHE_DIR = str(BASE_DIR / "data" / "jugaad_cache")

# ── Symbols ───────────────────────────────────────────────────────────────────
SYMBOLS = ["NIFTY"]
# SYMBOLS = ["NIFTY", "BANKNIFTY"]  # Uncomment when ready to add BANKNIFTY

# AngelOne symbol name mappings (resolved to symboltoken via data/angelone_symbols.py)
# Spot index (for live quotes / real-time tick stream)
ANGEL_INDEX_SPOT_SYMBOLS = {
    "NIFTY": "NIFTY 50",
    "BANKNIFTY": "NIFTY BANK",
    "FINNIFTY": "NIFTY FIN SERVICE",
}
# Continuous futures (for historical data / charting). Kept as the same
# synthetic "-I" symbol the whole DB schema, dashboard, and ML pipeline are
# already keyed on — data/market_data_adapter.py resolves "NIFTY-I" to
# AngelOne's actual current-month contract (e.g. NIFTY26APRFUT) internally,
# so nothing downstream of the adapter had to change.
ANGEL_INDEX_FUTURES_SYMBOLS = {
    "NIFTY": "NIFTY-I",
    "BANKNIFTY": "BANKNIFTY-I",
    "FINNIFTY": "FINNIFTY-I",
}

# Strike gap per underlying
STRIKE_GAP = {
    "NIFTY": 50,
    "BANKNIFTY": 100,
    "FINNIFTY": 50,
}

# Expiry cadence (NIFTY = weekly Tue, others = monthly last Tue)
# BANKNIFTY weekly options were discontinued by NSE in late 2024.
EXPIRY_CADENCE = {
    "NIFTY": "weekly",
    "BANKNIFTY": "monthly",
    "FINNIFTY": "monthly",
}

# Symbols collected EOD-only via REST (no live websocket subscription).
# Used by scripts/seed_other_symbols.py and scripts/eod_collect_other_symbols.py.
EOD_ONLY_SYMBOLS = ["BANKNIFTY", "FINNIFTY"]

# Number of strikes above/below ATM to track (±3 = 7 strikes per CE/PE)
ATM_RANGE = int(os.getenv("ATM_RANGE", "3"))

# Maximum symbols to subscribe (plan limit)
MAX_SYMBOLS = int(os.getenv("MAX_SYMBOLS", "50"))

# ── Trading Parameters ────────────────────────────────────────────────────────
INITIAL_CAPITAL = float(os.getenv("INITIAL_CAPITAL", "50000"))
RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "0.01"))
MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", "5"))

# Daily loss ceiling as a FRACTION of capital -- risk/risk_manager.py's own rule
# ("max daily loss = 5%", see its docstring). Deliberately a different name, and a
# different env var, from the RUPEE ceiling further down this file: both were
# called MAX_DAILY_LOSS and both read $MAX_DAILY_LOSS, so the rupee definition
# shadowed this one and risk_manager computed `capital * -5000`, making its
# daily-loss rule fire on every single check. Found 2026-11-03; that module is
# not imported anywhere, so nothing live was affected, but the trap is removed
# rather than left for whoever wires it up.
MAX_DAILY_LOSS_PCT = float(os.getenv("MAX_DAILY_LOSS_PCT", "0.05"))
SCORE_THRESHOLD = float(os.getenv("SCORE_THRESHOLD", "0.6"))

# ── Model Paths ───────────────────────────────────────────────────────────────
MACRO_MODEL_PATH = str(BASE_DIR / os.getenv("MACRO_MODEL_PATH", "models/saved/macro_model.pkl"))
MICRO_MODEL_PATH = str(BASE_DIR / os.getenv("MICRO_MODEL_PATH", "models/saved/micro_model.pkl"))

# ── Feature Configuration ─────────────────────────────────────────────────────
FEATURE_COLUMNS_MACRO = [
    # Price / Momentum (core)
    "rsi", "macd", "macd_signal", "macd_hist",
    "ema9", "ema20", "ema50", "sma200",
    "vwap_dist", "bollinger_upper", "bollinger_lower", "bollinger_width",
    "atr", "volume_ratio", "volume_sma20",
    # Momentum derivatives
    "stoch_rsi_k", "stoch_rsi_d", "williams_r",
    "roc_10", "roc_20", "adx", "di_plus", "di_minus", "cci",
    # Trend crossovers
    "ema9_20_cross", "ema20_50_cross", "close_above_sma200",
    # Trend context (added 2026-04-19)
    "close_vs_ema50_pct", "weekly_trend_slope", "pullback_in_uptrend",
    # Volatility
    "atr_pct", "bollinger_pct", "returns_1m",
    "volatility_20", "volatility_60", "vol_regime",
    # Candle patterns
    "candle_body_pct", "upper_shadow_pct", "lower_shadow_pct",
    # Multi-timeframe
    "rsi_5m", "rsi_15m", "ema20_5m", "atr_5m",
    # Session / time
    "minutes_since_open", "session_progress", "day_of_week",
    "is_first_hour", "is_last_hour",
    # Volume signals (expanded)
    "volume_change", "cum_volume_delta_20", "obv_slope", "mfi",
    # Options basics
    "oi_change", "pcr", "iv",
    # Options-aware (relative strike, expiry, cross-strike)
    "relative_strike", "days_to_expiry", "theta_pressure",
    "oi_skew", "pcr_near_atm", "pcr_far",
    "max_oi_call_rel", "max_oi_put_rel", "oi_concentration",
    "call_oi_gradient", "put_oi_gradient", "iv_skew",
]

FEATURE_COLUMNS_MICRO = [
    "bid_ask_spread", "order_imbalance", "trade_size_spike",
    "volume_burst", "tick_momentum",
]

# ── Trade Scoring Weights ─────────────────────────────────────────────────────
WEIGHT_ML_PROBABILITY = 0.50
WEIGHT_OPTIONS_FLOW = 0.30
WEIGHT_TECHNICAL_STRENGTH = 0.20
# WEIGHT_STRATEGY_PROB = 0.25  # reserved — strat_prob is used as gate only until models improve
STRAT_PROB_SCALE = 0.06             # normalize strat_prob raw output to [0,1] when needed

# ── Broker Execution ─────────────────────────────────────────────────────────
TRADE_MODE = os.getenv("TRADE_MODE", "paper")              # "paper" or "angelone"
ORDER_CONFIRMATION = os.getenv("ORDER_CONFIRMATION", "auto")  # "auto" or "manual"
def _daily_loss_rupees() -> float:
    """
    Daily loss ceiling in RUPEES, always negative.

    $MAX_DAILY_LOSS has been ambiguous: this file used to define it twice, once as
    a fraction (0.05) and once in rupees (-5000), and the .env on this machine held
    0.05 -- plainly meaning "5%". broker/order_manager.py reads it as rupees, so it
    was running with a ceiling of Rs.0.05 and its own startup banner said so
    ("max_loss=Rs.0.05"). Its check is `daily_pnl <= max_daily_loss`, and
    daily_pnl starts at 0.0, so 0.0 <= 0.05 is true on the FIRST order: it would
    reject that order and halt trading permanently with "Max daily loss breached
    (Rs.0)". Nothing noticed because the paper agent does not route through
    OrderManager -- but live mStock execution is meant to.

    Rather than silently honour a value that cannot mean what it says, the units
    are resolved explicitly and the interpretation is logged, so a misconfigured
    ceiling is visible instead of being either obeyed or ignored.
    """
    raw = float(os.getenv("MAX_DAILY_LOSS", "-5000"))
    if raw <= -1:
        return raw                                   # already rupees, negative
    if 0 < abs(raw) < 1:                             # 0.05 -> 5% of capital
        resolved = -abs(raw) * INITIAL_CAPITAL
        print(f"[settings] MAX_DAILY_LOSS={raw} looks like a FRACTION, not rupees. "
              f"Reading it as {abs(raw) * 100:.1f}% of Rs.{INITIAL_CAPITAL:,.0f} = Rs.{resolved:,.0f}. "
              f"Set MAX_DAILY_LOSS to a negative rupee amount (e.g. -2500) to be unambiguous.")
        return resolved
    resolved = -abs(raw)                             # positive rupees -> negative
    if raw != resolved:
        print(f"[settings] MAX_DAILY_LOSS={raw} is positive; a loss ceiling must be negative. "
              f"Using Rs.{resolved:,.0f}.")
    return resolved


# Daily loss ceiling in RUPEES (negative), used by broker/order_manager.py and
# risk/pretrade.py. The fraction-of-capital version is MAX_DAILY_LOSS_PCT above.
MAX_DAILY_LOSS = _daily_loss_rupees()
MAX_CONCURRENT_POSITIONS = int(os.getenv("MAX_CONCURRENT_POSITIONS", "1"))

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# ── Market Hours (IST) ───────────────────────────────────────────────────────
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MINUTE = 15
MARKET_CLOSE_HOUR = 15
MARKET_CLOSE_MINUTE = 30

# ── Scan Cycle ────────────────────────────────────────────────────────────────
SCAN_INTERVAL_SECONDS = 60