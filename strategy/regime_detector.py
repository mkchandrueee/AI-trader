"""
Market Regime Detector
──────────────────────
Detects the current market environment from 5-minute candle data.

From the docs (Product Vision §8):
  Possible regimes:
    TRENDING_BULL
    TRENDING_BEAR
    SIDEWAYS
    HIGH_VOLATILITY
    LOW_VOLATILITY

  Model types: RandomForest, HMM, Gradient Boosting
  Strategy selection adapts to regime.

  Example output: 10:15 AM → TRENDING BULL

For now this uses a rule-based detector. Once real data is available,
it can be upgraded to an ML-based classifier (RandomForest / HMM).
"""

from datetime import date as _date
from enum import Enum
from typing import Optional

import numpy as np
import pandas as pd

from utils.logger import get_logger

logger = get_logger("regime_detector")


class MarketRegime(str, Enum):
    TRENDING_BULL = "TRENDING_BULL"
    TRENDING_BEAR = "TRENDING_BEAR"
    SIDEWAYS = "SIDEWAYS"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    LOW_VOLATILITY = "LOW_VOLATILITY"
    UNKNOWN = "UNKNOWN"


class RegimeDetector:
    """
    Detects market regime using a combination of:
      - EMA trend (20 vs 50 on 5m candles)
      - ATR percentile (volatility)
      - ADX-like directional strength
      - Price range compression

    Returns a MarketRegime enum value.
    """

    def __init__(
        self,
        ema_short: int = 20,
        ema_long: int = 50,
        atr_period: int = 14,
        lookback: int = 50,
        vol_high_pct: float = 75,
        vol_low_pct: float = 25,
        trend_threshold: float = 0.002,
    ):
        self.ema_short = ema_short
        self.ema_long = ema_long
        self.atr_period = atr_period
        self.lookback = lookback
        self.vol_high_pct = vol_high_pct
        self.vol_low_pct = vol_low_pct
        self.trend_threshold = trend_threshold

    def detect(self, df: pd.DataFrame) -> MarketRegime:
        """
        Detect regime from a DataFrame of 5-minute candles.
        Requires columns: open, high, low, close, volume.
        Uses the most recent `lookback` candles.
        """
        if df.empty or len(df) < self.ema_long + 5:
            logger.warning("Not enough data for regime detection.")
            return MarketRegime.UNKNOWN

        df = df.tail(max(self.lookback, self.ema_long + 20)).copy()

        # ── EMA Trend ────────────────────────────────────────────────────────
        df["_ema_s"] = df["close"].ewm(span=self.ema_short, adjust=False).mean()
        df["_ema_l"] = df["close"].ewm(span=self.ema_long, adjust=False).mean()

        ema_diff = (df["_ema_s"].iloc[-1] - df["_ema_l"].iloc[-1]) / df["_ema_l"].iloc[-1]
        ema_slope = (df["_ema_s"].iloc[-1] - df["_ema_s"].iloc[-5]) / df["_ema_s"].iloc[-5]

        # ── ATR / Volatility ─────────────────────────────────────────────────
        tr = pd.concat([
            df["high"] - df["low"],
            (df["high"] - df["close"].shift(1)).abs(),
            (df["low"] - df["close"].shift(1)).abs(),
        ], axis=1).max(axis=1)

        atr = tr.rolling(self.atr_period).mean()
        atr_pct = atr / df["close"]  # ATR as % of price

        current_atr_pct = atr_pct.iloc[-1]
        atr_history = atr_pct.dropna()

        vol_high_threshold = np.percentile(atr_history, self.vol_high_pct)
        vol_low_threshold = np.percentile(atr_history, self.vol_low_pct)

        # ── Price Range Compression (sideways detection) ─────────────────────
        recent_high = df["high"].tail(20).max()
        recent_low = df["low"].tail(20).min()
        range_pct = (recent_high - recent_low) / df["close"].iloc[-1]

        # ── Classification Logic ─────────────────────────────────────────────

        # Priority 1: Extreme volatility
        if current_atr_pct > vol_high_threshold:
            regime = MarketRegime.HIGH_VOLATILITY
        elif current_atr_pct < vol_low_threshold:
            regime = MarketRegime.LOW_VOLATILITY
        # Priority 2: Clear trend
        elif ema_diff > self.trend_threshold and ema_slope > 0:
            regime = MarketRegime.TRENDING_BULL
        elif ema_diff < -self.trend_threshold and ema_slope < 0:
            regime = MarketRegime.TRENDING_BEAR
        # Priority 3: Sideways (range-bound)
        elif range_pct < 0.01:
            regime = MarketRegime.SIDEWAYS
        # Default
        elif ema_diff > 0:
            regime = MarketRegime.TRENDING_BULL
        elif ema_diff < 0:
            regime = MarketRegime.TRENDING_BEAR
        else:
            regime = MarketRegime.SIDEWAYS

        logger.info(
            f"Regime: {regime.value} "
            f"(ema_diff={ema_diff:.4f}, atr%={current_atr_pct:.4f}, "
            f"range%={range_pct:.4f})"
        )
        return regime

    def detect_with_details(self, df: pd.DataFrame) -> dict:
        """
        Detect regime and return full diagnostics.
        Useful for logging and dashboard display.
        """
        regime = self.detect(df)

        df = df.tail(max(self.lookback, self.ema_long + 20)).copy()

        df["_ema_s"] = df["close"].ewm(span=self.ema_short, adjust=False).mean()
        df["_ema_l"] = df["close"].ewm(span=self.ema_long, adjust=False).mean()

        return {
            "regime": regime.value,
            "ema_short": round(float(df["_ema_s"].iloc[-1]), 2),
            "ema_long": round(float(df["_ema_l"].iloc[-1]), 2),
            "last_close": round(float(df["close"].iloc[-1]), 2),
            "recent_high": round(float(df["high"].tail(20).max()), 2),
            "recent_low": round(float(df["low"].tail(20).min()), 2),
        }


# ── Strategy Regime Mapping ───────────────────────────────────────────────────

REGIME_STRATEGIES = {
    # Trending regimes: primary directional strategy gets the bonus.
    # math_decision_engine is direction-agnostic (driven by ATM CE/PE candle
    # quality, not the underlying's trend classification), so it rides along
    # in every regime except LOW_VOLATILITY, where option premiums barely
    # move and its 15%-of-range breakout buffer would rarely mean anything.
    MarketRegime.TRENDING_BULL: ["vwap_momentum_breakout", "math_decision_engine"],
    MarketRegime.TRENDING_BEAR: ["bearish_momentum", "math_decision_engine"],
    # Sideways: mean_reversion is primary, but momentum breakouts/breakdowns are valid too
    # (NIFTY can grind down from a sideways range — bearish_momentum is a real sideways setup)
    MarketRegime.SIDEWAYS: ["mean_reversion", "bearish_momentum", "vwap_momentum_breakout", "math_decision_engine"],
    # High-vol: both reversion and momentum are valid (fast moves in both directions)
    MarketRegime.HIGH_VOLATILITY: ["mean_reversion", "bearish_momentum", "vwap_momentum_breakout", "math_decision_engine"],
    MarketRegime.LOW_VOLATILITY: ["vwap_momentum_breakout"],
    MarketRegime.UNKNOWN: ["vwap_momentum_breakout", "bearish_momentum", "mean_reversion", "math_decision_engine"],
}


def get_strategies_for_regime(regime: MarketRegime) -> list:
    """Return list of strategy names appropriate for the given regime."""
    return REGIME_STRATEGIES.get(regime, REGIME_STRATEGIES[MarketRegime.UNKNOWN])


# ── Daily Bias (NextDay Direction Analyser pivots) ─────────────────────────────
#
# The intraday regime above answers "what is NIFTY-I doing right now, on 5m
# candles". nextday_bias() (strategy/math_decision_strategy.py) answers a
# slower, once-a-day question from classic floor pivots on the PREVIOUS
# completed day's H/L/C: which side does the pivot math lean for the session
# ahead. Wired in the same additive way regime_bonus already is — a signal
# whose direction agrees with the prior day's lean gets a small bonus, one
# that fights it gets a small penalty. Neither overrides the other; a signal
# still needs everything else (ML prob, flow, technical strength) to clear
# the score floor.

DAILY_BIAS_BONUS = 0.03    # signal direction agrees with the prior day's pivot lean
DAILY_BIAS_PENALTY = 0.03  # signal direction fights it

_daily_bias_cache: dict = {}  # {(symbol, ref_date): bias_dict_or_None} — changes once/day


def compute_daily_bias(prev_high: float, prev_low: float, prev_close: float,
                        min_distance_pct: float = 0.0) -> Optional[dict]:
    """
    Thin wrapper around math_decision_strategy.nextday_bias(), returning a
    JSON-serializable dict (or None if inputs are missing/invalid).
    """
    if not (prev_high and prev_low and prev_close):
        return None
    try:
        from strategy.math_decision_strategy import nextday_bias
        bias = nextday_bias(prev_high, prev_low, prev_close, min_distance_pct)
    except Exception as e:
        logger.debug(f"daily bias compute failed: {e}")
        return None

    return {
        "direction": bias.direction, "entry": bias.entry, "stop": bias.stop,
        "target1": bias.target1, "target2": bias.target2,
        "risk": bias.risk, "reward": bias.reward,
        "rr": bias.rr if bias.rr == bias.rr else None,  # NaN -> None for JSON
    }


def get_daily_bias(symbol: str = "NIFTY-I", ref_date: Optional[_date] = None) -> Optional[dict]:
    """
    The previous completed trading day's floor-pivot lean for `symbol`,
    cached per (symbol, ref_date) — this only changes once a day, so callers
    (the live scanner, the backtest replay loop) can call it every cycle
    without hitting the DB every time.

    `ref_date` is the day being scanned/replayed — the "previous day" is
    resolved relative to it (today's date live, the current backtest bar's
    date in a backtest), not `date.today()`, so backtests get the correct
    historical lean rather than whatever happened to be true when the
    backtest was run.

    Returns None if there's no prior-day candle data yet (e.g. day one of
    collection) or the DB is unavailable — callers should treat that as "no
    adjustment", not an error.
    """
    ref_date = ref_date or _date.today()
    cache_key = (symbol, ref_date)
    if cache_key in _daily_bias_cache:
        return _daily_bias_cache[cache_key]

    bias = None
    try:
        from database.db import read_sql

        prev_day_row = read_sql(
            "SELECT DISTINCT timestamp::date as d FROM minute_candles "
            "WHERE symbol = :sym AND timestamp::date < :ref "
            "ORDER BY d DESC LIMIT 1",
            {"sym": symbol, "ref": ref_date},
        )
        if not prev_day_row.empty:
            prev_day = prev_day_row.iloc[0]["d"]
            agg = read_sql(
                "SELECT MAX(high) as h, MIN(low) as l FROM minute_candles "
                "WHERE symbol = :sym AND timestamp::date = :d",
                {"sym": symbol, "d": prev_day},
            )
            close_row = read_sql(
                "SELECT close FROM minute_candles WHERE symbol = :sym "
                "AND timestamp::date = :d ORDER BY timestamp DESC LIMIT 1",
                {"sym": symbol, "d": prev_day},
            )
            if not agg.empty and not close_row.empty and agg.iloc[0]["h"] is not None:
                bias = compute_daily_bias(
                    float(agg.iloc[0]["h"]), float(agg.iloc[0]["l"]), float(close_row.iloc[0]["close"]),
                )
    except Exception as e:
        logger.debug(f"daily bias fetch skipped: {e}")

    _daily_bias_cache[cache_key] = bias
    if len(_daily_bias_cache) > 8:  # keep the cache small, it's only ever a few days
        oldest_key = min(_daily_bias_cache, key=lambda k: k[1])
        _daily_bias_cache.pop(oldest_key, None)
    return bias


def daily_bias_adjustment(direction: str, daily_bias: Optional[dict]) -> float:
    """
    +DAILY_BIAS_BONUS if `direction` ("CALL"/"PUT") agrees with the prior
    day's pivot lean, -DAILY_BIAS_PENALTY if it disagrees, 0.0 if there's no
    clear lean (tie / too-close-to-pivot) or no bias was available at all.
    """
    if not daily_bias or not daily_bias.get("direction"):
        return 0.0
    bullish_call = direction == "CALL" and daily_bias["direction"] == "bullish"
    bearish_put = direction == "PUT" and daily_bias["direction"] == "bearish"
    if bullish_call or bearish_put:
        return DAILY_BIAS_BONUS
    return -DAILY_BIAS_PENALTY
