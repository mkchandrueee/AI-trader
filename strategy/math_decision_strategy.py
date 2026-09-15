"""
Math Decision Strategy — ported from the NIFTY Option Strategy Suite
──────────────────────────────────────────────────────────────────────
Two deterministic, formula-based (not ML) strategies, ported faithfully from
the JS reference implementation in `_vendor-src/Market_Scanner/assets/js/`
(engine.js, analyser.js, nextday.js). Every constant and worked example below
is reproduced from that source so the numbers can be checked against it.

1. **Trade Decision Engine** (`analyse_option_pair`) — the "math strategy":
   given the current 5-minute CE and PE premium candles for the ATM strike,
   picks a side by candle-quality, sets a buffered breakout entry, a partial
   booking level, a full target, and a structure-based stop. Registered as
   the `math_decision_engine` strategy in signal_generator.py, so it trains,
   validates, and backtests exactly like the other three strategies.

2. **NextDay Direction Analyser** (`nextday_pivots` / `nextday_bias`) —
   classic floor pivots on the previous day's index OHLC, giving a
   BULLISH/BEARISH/NO CLEAR DIRECTION lean for the session ahead. Exposed as
   a standalone daily bias function — see `nextday_bias()` — for whatever
   caller wants a once-a-day directional filter (e.g. the dashboard or a
   regime-detector bonus); it is not itself a per-bar entry signal, so it is
   not registered in STRATEGY_MAP.

Both are pure functions of their inputs — no ML, no randomness, no lookback
beyond what's passed in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from utils.logger import get_logger

logger = get_logger("math_decision_strategy")


# ═══════════════════════════════════════════════════════════════════════════════
# Shared candle stats (ported from utils.js candleStats)
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class CandleStats:
    range: float
    body: float
    body_ratio: float
    close_pos: float  # (c - l) / range — 0 = closed at low, 1 = closed at high
    upper_wick: float
    lower_wick: float
    bullish: bool
    mid: float
    pivot: float


def candle_stats(o: float, h: float, l: float, c: float) -> CandleStats:
    rng = h - l
    body = abs(c - o)
    body_top = max(o, c)
    body_bottom = min(o, c)
    return CandleStats(
        range=rng,
        body=body,
        body_ratio=(body / rng) if rng > 0 else 0.0,
        close_pos=((c - l) / rng) if rng > 0 else 0.5,
        upper_wick=((h - body_top) / rng) if rng > 0 else 0.0,
        lower_wick=((body_bottom - l) / rng) if rng > 0 else 0.0,
        bullish=c > o,
        mid=(h + l) / 2,
        pivot=(h + l + c) / 3,
    )


def _clamp(n: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, n))


def _round2(n: float) -> float:
    return round(n, 2)


# ═══════════════════════════════════════════════════════════════════════════════
# Trade Decision Engine (engine.js + analyser.js)
# ═══════════════════════════════════════════════════════════════════════════════

# Defaults match config.js's DEFAULT_CONFIG exactly.
DEFAULT_CFG = {
    "sideMinConfidence": 60,   # winning leg's candle quality must reach this
    "sideMinMargin": 8,        # ...and beat the losing leg by at least this much
    "engineBufferPct": 15,     # breakout buffer = max(15% of leg range, engineBufferMin)
    "engineBufferMin": 2,
    "enginePartialPts": 10,    # partial-book level = entry + 10
    "engineTargetPts": 20,     # full target = entry + 20
}


def directional_strength(stats: CandleStats) -> float:
    """
    The reference app's directional-conviction bar: how decisively the candle
    moved the way it moved. NOT the same as candle quality (bullishness) —
    a hard down-candle scores high here.
    """
    toward = stats.close_pos if stats.bullish else (1 - stats.close_pos)
    return _clamp(100 * toward * (0.5 + stats.body_ratio * 0.5), 0, 100)


@dataclass
class LegAnalysis:
    stats: CandleStats
    entry: float
    target1: float
    target2: float
    target3: float
    stop_loss: float
    risk: float
    confidence: float  # candle-quality score: 20*bullish + 40*body_ratio + 40*close_pos


def analyse_side(o: float, h: float, l: float, c: float, cfg: dict = None) -> LegAnalysis:
    """One leg's candle-quality analysis (analyser.js analyseSide)."""
    cfg = {**DEFAULT_CFG, **(cfg or {})}
    stats = candle_stats(o, h, l, c)
    rng = stats.range

    entry_pct = cfg.get("entryPremiumPct", 0.5)
    step_mult = cfg.get("targetStepMult", 0.8)
    min_step = cfg.get("minTargetStep", 16)
    t2_mult = cfg.get("t2Mult", 2.0)
    t3_mult = cfg.get("t3Mult", 3.5)
    sl_mult = cfg.get("slRangeMult", 0.3)

    entry = _round2(c * (1 + entry_pct / 100))
    step = max(step_mult * rng, min_step)
    target1 = _round2(entry + step)
    target2 = _round2(entry + t2_mult * step)
    target3 = _round2(entry + t3_mult * step)
    stop_loss = _round2(l - sl_mult * rng)

    # Candle quality: direction 20, body 40, close position 40 (0-100).
    confidence = _clamp(
        20 * (1 if stats.bullish else 0) + 40 * stats.body_ratio + 40 * stats.close_pos,
        0, 100,
    )

    return LegAnalysis(
        stats=stats, entry=entry, target1=target1, target2=target2, target3=target3,
        stop_loss=stop_loss, risk=entry - stop_loss, confidence=confidence,
    )


@dataclass
class SideSelection:
    side: Optional[str]  # "call" | "put" | None (no clear edge)
    leader: str
    leader_score: float
    call_score: float
    put_score: float
    margin: float
    decision: str  # "clear" | "tie" | "noEdge" | "insufficient"


def select_side(ce: LegAnalysis, pe: LegAnalysis, cfg: dict = None) -> SideSelection:
    """
    The winner must clear a confidence floor AND beat the loser by a margin,
    or the answer is WAIT / NO CLEAR EDGE — an exact tie never defaults to a
    side (analyser.js selectSide).
    """
    cfg = {**DEFAULT_CFG, **(cfg or {})}
    call_score, put_score = ce.confidence, pe.confidence
    margin = abs(call_score - put_score)
    leader = "call" if call_score >= put_score else "put"
    leader_score = max(call_score, put_score)
    required_margin = cfg.get("sideMinMargin", 8)
    required_confidence = cfg.get("sideMinConfidence", 60)

    if call_score == put_score:
        decision = "tie"
    elif margin < required_margin:
        decision = "noEdge"
    elif leader_score < required_confidence:
        decision = "insufficient"
    else:
        decision = "clear"

    return SideSelection(
        side=leader if decision == "clear" else None,
        leader=leader, leader_score=leader_score,
        call_score=call_score, put_score=put_score,
        margin=margin, decision=decision,
    )


@dataclass
class TradeDecision:
    side: Optional[str]           # "call" | "put" | None
    tradable: bool
    entry: float
    partial: float
    target: float
    stop: float
    risk: float
    reward: float
    rr: float
    confidence: int                # setup grade 0-100, NOT win probability
    tier: str                      # "strong" | "moderate" | "avoid"
    verdict: str                   # "take" | "caution" | "wait"
    blockers: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def analyse_option_pair(
    call_ohlc: tuple[float, float, float, float],
    put_ohlc: tuple[float, float, float, float],
    cfg: dict = None,
) -> TradeDecision:
    """
    The Trade Decision Engine (engine.js `analyse`). Inputs are (o, h, l, c)
    tuples for the current 5-minute ATM CE and PE premium candles.

    Verified against the reference tool's published examples:
      CALL 165/180/138/140, PUT 48/52/38/51
        -> BUY PUT, buffer 2.10, entry 54.10, partial 64.10, target 74.10, stop 38.00
      CALL 129.7/129.7/107.75/109.25, PUT 58.35/68.3/58.25/67.45
        -> BUY PUT, buffer 2.00, entry 70.30, partial 80.30, target 90.30,
           stop 58.25, risk 12.05, R:R 1.66
    """
    cfg = {**DEFAULT_CFG, **(cfg or {})}
    ce = analyse_side(*call_ohlc, cfg)
    pe = analyse_side(*put_ohlc, cfg)
    selection = select_side(ce, pe, cfg)

    leading = "call" if selection.leader == "call" else "put"
    leg_ohlc = call_ohlc if leading == "call" else put_ohlc
    leg_stats = candle_stats(*leg_ohlc)
    _, leg_h, leg_l, _ = leg_ohlc

    buffer_pct = cfg.get("engineBufferPct", 15)
    buffer_min = cfg.get("engineBufferMin", 2)
    partial_pts = cfg.get("enginePartialPts", 10)
    target_pts = cfg.get("engineTargetPts", 20)

    buffer = _round2(max(leg_stats.range * buffer_pct / 100, buffer_min))
    entry = _round2(leg_h + buffer)
    partial = _round2(entry + partial_pts)
    target = _round2(entry + target_pts)
    stop = _round2(leg_l)  # structure-based: the low of the breakout candle

    risk = _round2(entry - stop)
    rr = _round2(target_pts / risk) if risk > 0 else float("nan")

    call_bull = directional_strength(ce.stats) / 100 if ce.stats.bullish else 0.0
    put_bull = directional_strength(pe.stats) / 100 if pe.stats.bullish else 0.0
    edge = abs(call_bull - put_bull)
    rng = leg_stats.range
    range_quality = (rng / 12) if rng < 12 else (max(0.4, 60 / rng) if rng > 60 else 1.0)
    confidence = round(
        (directional_strength(leg_stats) / 100 * 0.5 + min(1, edge / 0.3) * 0.25 + range_quality * 0.25) * 100
    )
    tier = "strong" if confidence >= 72 else ("moderate" if confidence >= 50 else "avoid")

    blockers = []
    warnings = []
    if selection.side is None:
        blockers.append("NO_CLEAR_SIDE")
    if not leg_stats.bullish:
        blockers.append("CHOSEN_NOT_BULLISH")
    if tier == "avoid":
        blockers.append("CONFIDENCE_LOW")
    if not (risk > 0):
        blockers.append("RISK_INVALID")
    if rr == rr and rr < 1:  # rr == rr is a NaN-safe check
        warnings.append("RR_BELOW_ONE")

    verdict = "wait" if selection.side is None else ("take" if not blockers else "caution")

    return TradeDecision(
        side=selection.side, tradable=(selection.side is not None and not blockers),
        entry=entry, partial=partial, target=target, stop=stop,
        risk=risk, reward=float(target_pts), rr=rr,
        confidence=confidence, tier=tier, verdict=verdict,
        blockers=blockers, warnings=warnings,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# NextDay Direction Analyser (nextday.js) — classic floor pivots
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class PivotLevels:
    p: float
    r1: float
    s1: float
    r2: float
    s2: float


def nextday_pivots(h: float, l: float, c: float) -> PivotLevels:
    rng = h - l
    p = (h + l + c) / 3
    return PivotLevels(p=p, r1=2 * p - l, s1=2 * p - h, r2=p + rng, s2=p - rng)


@dataclass
class NextDayBias:
    direction: Optional[str]  # "bullish" | "bearish" | None (tie/no clear direction)
    entry: float
    stop: float
    target1: float
    target2: float
    risk: float
    reward: float
    rr: float


def nextday_bias(prev_h: float, prev_l: float, prev_c: float, min_distance_pct: float = 0.0) -> NextDayBias:
    """
    Classic floor pivots on the PREVIOUS day's H/L/C (the Open is never used —
    P/R1/S1/R2/S2 are algebraic functions of H, L, C only).

    Verified against the reference tool's examples:
      H 24270 L 24155 C 24155 -> P 24193.33, BEARISH, R:R 2.00
      H 24360 L 24227 C 24288 -> P 24291.67, BEARISH, R:R 1.06
      H 22230 L 22100 C 22180 -> P 22170.00, BULLISH, R:R 1.17
    """
    pv = nextday_pivots(prev_h, prev_l, prev_c)
    rng = prev_h - prev_l
    distance_pct = (abs(prev_c - pv.p) / rng * 100) if rng > 0 else 0.0

    eps = max(abs(pv.p), 1) * 1e-9
    if abs(prev_c - pv.p) <= eps or distance_pct < min_distance_pct:
        direction = None
    else:
        direction = "bearish" if prev_c < pv.p else "bullish"

    bearish = direction == "bearish" if direction else (prev_c <= pv.p)
    raw_stop = pv.r1 if bearish else pv.s1
    raw_t1 = pv.s1 if bearish else pv.r1
    raw_t2 = pv.s2 if bearish else pv.r2
    risk = abs(raw_stop - pv.p)
    reward = abs(raw_t1 - pv.p)

    return NextDayBias(
        direction=direction,
        entry=_round2(pv.p), stop=_round2(raw_stop),
        target1=_round2(raw_t1), target2=_round2(raw_t2),
        risk=_round2(risk), reward=_round2(reward),
        rr=_round2(reward / risk) if risk > 0 else float("nan"),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# STRATEGY_MAP integration — signal_generator.py wrapper
# ═══════════════════════════════════════════════════════════════════════════════


def build_candle_cache(symbol_pairs) -> dict:
    """
    Batch-fetch the latest candle for every (ce_symbol, pe_symbol) pair a
    bulk caller needs, in one query instead of one-per-row.

    generate_signal() normally resolves its own CE/PE candles with a DB
    round trip per call — completely fine for its live use (one Pre Market
    decision at a time). Strategy-model training calls it once per row of a
    macro feature set (tens of thousands of rows, e.g.
    models/strategy_models.generate_strategy_labels), and every row resolves
    to a *distinct option symbol pair* drawn from a much smaller set — same
    ATM strike + expiry recur across many rows of the same day/week. Looping
    naively turned a 95k-row training pass into 95k sequential DB round
    trips (tens of minutes, looked hung). Precompute the small set of pairs
    actually needed and fetch them all here; pass the result to
    generate_signal(..., candle_cache=...) to skip its own DB lookup.
    """
    from database.db import read_sql

    symbols = sorted({s for pair in symbol_pairs for s in pair})
    if not symbols:
        return {}

    symbol_list = ", ".join(f"'{s}'" for s in symbols)
    candles = read_sql(f"""
        SELECT DISTINCT ON (symbol) symbol, open, high, low, close
        FROM minute_candles
        WHERE symbol IN ({symbol_list})
        ORDER BY symbol, timestamp DESC
    """)
    by_symbol = {
        row["symbol"]: (float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"]))
        for row in candles.to_dict(orient="records")
    }

    cache = {}
    for ce_symbol, pe_symbol in symbol_pairs:
        if ce_symbol in by_symbol and pe_symbol in by_symbol:
            cache[(ce_symbol, pe_symbol)] = (by_symbol[ce_symbol], by_symbol[pe_symbol])
    return cache


def generate_signal(row: dict, symbol: str = "", candle_cache: Optional[dict] = None):
    """
    Wraps analyse_option_pair() to match the (row, symbol) -> Signal|None
    interface every other strategy in signal_generator.STRATEGY_MAP uses.

    Unlike the other three strategies (pure functions of the underlying's
    macro feature row), the Trade Decision Engine needs the current 5-minute
    ATM CE/PE *premium* candles — this reads them from the DB, resolving the
    ATM strike from `row["close"]` (the underlying's current price).

    `candle_cache`, when given (see build_candle_cache() above), is consulted
    instead of hitting the DB — for bulk/training callers only. Live callers
    never pass it and get the exact same per-call DB lookup as before.
    """
    from strategy.signal_generator import Signal  # local import avoids a cycle
    from backtest.option_resolver import get_nearest_expiry, get_atm_strike, build_option_symbol

    close = row.get("close")
    if close is None:
        return None

    ts = row.get("timestamp") or datetime.now()
    ref_date = ts.date() if isinstance(ts, datetime) else date.today()

    expiry = get_nearest_expiry(ref_date)
    if expiry is None:
        return None

    atm = get_atm_strike(close)
    ce_symbol = build_option_symbol(expiry, atm, "CE")
    pe_symbol = build_option_symbol(expiry, atm, "PE")

    if candle_cache is not None:
        cached = candle_cache.get((ce_symbol, pe_symbol))
        if cached is None:
            return None
        call_ohlc, put_ohlc = cached
    else:
        from database.db import read_sql

        candles = read_sql(
            """
            SELECT symbol, open, high, low, close
            FROM minute_candles
            WHERE symbol IN (:ce, :pe)
            ORDER BY timestamp DESC
            LIMIT 2
            """,
            {"ce": ce_symbol, "pe": pe_symbol},
        )
        if candles.empty or set(candles["symbol"]) != {ce_symbol, pe_symbol}:
            return None

        ce_row = candles[candles["symbol"] == ce_symbol].iloc[0]
        pe_row = candles[candles["symbol"] == pe_symbol].iloc[0]
        call_ohlc = (float(ce_row.open), float(ce_row.high), float(ce_row.low), float(ce_row.close))
        put_ohlc = (float(pe_row.open), float(pe_row.high), float(pe_row.low), float(pe_row.close))

    decision = analyse_option_pair(call_ohlc, put_ohlc)
    if not decision.tradable:
        return None

    return Signal(
        strategy="math_decision_engine",
        direction="CALL" if decision.side == "call" else "PUT",
        symbol=ce_symbol if decision.side == "call" else pe_symbol,
        entry_price=decision.entry,
        technical_strength=round(decision.confidence / 100, 2),
        details={
            "tier": decision.tier, "rr": decision.rr,
            "partial": decision.partial, "target": decision.target, "stop": decision.stop,
            "warnings": decision.warnings,
        },
    )
