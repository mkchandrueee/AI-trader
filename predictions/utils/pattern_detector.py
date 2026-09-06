"""
predictions/utils/pattern_detector.py
────────────────────────────────────────
Adapted from NSE-Neuron's original, which used TA-Lib — a C-extension
library with real install friction on Windows (no clean `pip install`,
needs a prebuilt wheel or a compiler + the C library). Replaced with plain
pandas implementations of the same five standard candlestick patterns, so
the whole predictions stack stays pip-installable. Same +100/-100/0 scoring
convention as TA-Lib, same PATTERN_COLS names, same call signature.
"""
import numpy as np
import pandas as pd

from predictions.nn_config import PATTERN_COLS


def _body_range(o, h, l, c):
    rng = (h - l).replace(0, np.nan)
    body = (c - o).abs()
    upper_wick = h - np.maximum(o, c)
    lower_wick = np.minimum(o, c) - l
    return rng, body, upper_wick, lower_wick


def _cdl_doji(o, h, l, c) -> pd.Series:
    """Body is a tiny fraction of the day's range — indecision."""
    rng, body, _, _ = _body_range(o, h, l, c)
    return np.where(body <= 0.1 * rng, 100, 0)


def _cdl_hammer(o, h, l, c) -> pd.Series:
    """Small body near the top, long lower wick, little/no upper wick."""
    rng, body, upper_wick, lower_wick = _body_range(o, h, l, c)
    is_hammer = (
        (body <= 0.3 * rng)
        & (lower_wick >= 2 * body)
        & (upper_wick <= 0.1 * rng)
    )
    return np.where(is_hammer, 100, 0)


def _cdl_shooting_star(o, h, l, c) -> pd.Series:
    """Mirror of the hammer: small body near the bottom, long upper wick."""
    rng, body, upper_wick, lower_wick = _body_range(o, h, l, c)
    is_star = (
        (body <= 0.3 * rng)
        & (upper_wick >= 2 * body)
        & (lower_wick <= 0.1 * rng)
    )
    return np.where(is_star, -100, 0)


def _cdl_engulfing(o, h, l, c) -> pd.Series:
    """Current body fully engulfs the previous body, opposite direction."""
    prev_o, prev_c = o.shift(1), c.shift(1)
    bullish = (c > o) & (prev_c < prev_o) & (c >= prev_o) & (o <= prev_c)
    bearish = (c < o) & (prev_c > prev_o) & (o >= prev_c) & (c <= prev_o)
    out = np.where(bullish, 100, np.where(bearish, -100, 0))
    out[0] = 0  # no previous candle for the first row
    return out


def _cdl_morning_star(o, h, l, c) -> pd.Series:
    """
    3-candle reversal: long bearish, small-bodied middle gapping down,
    long bullish closing above the midpoint of the first candle.
    """
    body = (c - o).abs()
    day1_bearish = (c.shift(2) < o.shift(2)) & (body.shift(2) > body.rolling(10).mean())
    day2_small = body.shift(1) <= 0.3 * body.shift(2)
    day3_bullish = (c > o) & (c >= (o.shift(2) + c.shift(2)) / 2)
    is_star = day1_bearish & day2_small & day3_bullish
    out = np.where(is_star.fillna(False), 100, 0)
    return out


def detect_patterns(df):
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]

    df["HAMMER"] = _cdl_hammer(o, h, l, c)
    df["ENGULFING"] = _cdl_engulfing(o, h, l, c)
    df["DOJI"] = _cdl_doji(o, h, l, c)
    df["SHOOTING_STAR"] = _cdl_shooting_star(o, h, l, c)
    df["MORNING_STAR"] = _cdl_morning_star(o, h, l, c)

    # +100 for bullish, -100 for bearish — simply sum, no manual sign flipping.
    df["Pattern_Score"] = (
        df["HAMMER"] + df["ENGULFING"] + df["DOJI"] +
        df["SHOOTING_STAR"] + df["MORNING_STAR"]
    ) / 100

    # Scan last 10 trading days — these patterns are rare on any single day.
    LOOKBACK = 10
    recent = df.tail(LOOKBACK)
    active_pats = []
    for _, row in recent.iterrows():
        for col in PATTERN_COLS:
            if row[col] != 0:
                active_pats.append((
                    col, int(row[col]),
                    row["Date"].strftime("%d-%b-%Y") if hasattr(row["Date"], "strftime") else str(row["Date"]),
                ))

    # Remove duplicates keeping most recent.
    seen = set()
    unique_pats = []
    for pat, val, date_str in reversed(active_pats):
        if pat not in seen:
            seen.add(pat)
            unique_pats.append((pat, val, date_str))
    active_pats = list(reversed(unique_pats))
    return df, active_pats


def combine_regime_patterns(regime, active_pats):
    if not (regime["sufficient_data"] and active_pats):
        return None
    bullish_pats = [p for p, v, d in active_pats if v > 0]
    bearish_pats = [p for p, v, d in active_pats if v < 0]
    reg = regime["regime"]
    if reg == "BEAR" and bullish_pats:
        return "Bullish pattern inside a BEAR trend — possible short-term reversal or dead-cat bounce. Wait for confirmation."
    if reg == "BULL" and bearish_pats:
        return "Bearish pattern inside a BULL trend — possible short-term pullback. Trend is still up."
    if reg == "BULL" and bullish_pats:
        return "Bullish pattern confirms the BULL regime — trend and candles are aligned."
    if reg == "BEAR" and bearish_pats:
        return "Bearish pattern confirms the BEAR regime — trend and candles are aligned."
    if reg == "SIDEWAYS" and bullish_pats:
        return "Bullish pattern in a SIDEWAYS market — possible breakout upward, watch volume."
    if reg == "SIDEWAYS" and bearish_pats:
        return "Bearish pattern in a SIDEWAYS market — possible breakout downward, watch volume."
    return None
