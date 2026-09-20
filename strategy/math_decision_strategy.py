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
# Options Analyzer + Pullback Entry + Value Calculator (reference "Trading Toolkit")
# ═══════════════════════════════════════════════════════════════════════════════
#
# Display-only: nothing here changes analyse_option_pair()'s decision, so the
# live agent's trading logic is untouched.
#
# PROVENANCE — what is exact and what is fitted:
#  * Target ladders reuse analyse_side() (entry = close x 1.005, T1/T2/T3 =
#    entry + 1/2/3.5 steps, stop = low - 0.3 x range): exact, matches every
#    published leg to the paisa.
#  * Checklist rules come from the reference source (analyser.js): body
#    >= 60% pass, 45-60% warn (config.js bodyModerate), below 45% fail; close
#    position >= 60%; PCR <= 0.8 bullish / >= 1.2 bearish / else neutral.
#  * The analyzer's side SCORE is not in any local source. It is fitted to the
#    four samples published in the reference release videos and reproduces all
#    of them exactly: direction 20 + body 25 + close position 35 + PCR aligned
#    with the side 20 (call scores 40 / put 55 on sample 1; 80, 75 and 80 on
#    samples 2-4). The half credit for a 45-60% body is an UNOBSERVED
#    assumption (no published sample lands in that band as a scored leg).
#  * The Pullback Entry levels are likewise fitted to two published samples
#    and reproduce both exactly: zones at 25/38/50% of the range above the
#    low, stop = low - 3, targets = zone-2 + max(floor, k x risk) with floors
#    30/50/80 and k 1.5/2.5/4.0 (risk = zone-2 - stop). The reference's own
#    "x/11" setup grade is NOT reproduced.
#  * A flat close == open candle counts as bullish in the checklist, score and
#    strength bars because the reference shows 127->127 as BULLISH (the engine
#    itself still uses strict close > open, unchanged).

ANALYZER_BODY_PASS = 0.60
ANALYZER_BODY_WARN = 0.45
ANALYZER_CLOSE_POS_STRONG = 0.60
ANALYZER_PCR_BULLISH_MAX = 0.8
ANALYZER_PCR_BEARISH_MIN = 1.2
SCORE_W_DIRECTION, SCORE_W_BODY, SCORE_W_CLOSE, SCORE_W_PCR = 20, 25, 35, 20
SCORE_BODY_WARN_CREDIT = 0.5   # unobserved assumption, see above
ANALYZER_MIN_GAP = 20          # the video: "Need 20%+ gap for entry"
ANALYZER_MIN_LEADER = 60       # config.js takeConfidence
BOOK_PLAN = (("BOOK", 40), ("BOOK", 40), ("HOLD", 20))  # config.js bookT1Pct/bookT2Pct/holdT3Pct

PB_ZONES = (0.25, 0.38, 0.50)
PB_SL_POINTS = 3
PB_TARGET_MULTS = (1.5, 2.5, 4.0)
PB_TARGET_FLOORS = (30, 50, 80)


def _r1(x: float) -> float:
    """One decimal, halves up (the reference prints levels this way)."""
    from decimal import Decimal, ROUND_HALF_UP
    return float(Decimal(str(round(x, 10))).quantize(Decimal("0.1"), ROUND_HALF_UP))


def _ladder(leg: LegAnalysis) -> dict:
    targets = (leg.target1, leg.target2, leg.target3)
    return {
        "entry": leg.entry,
        "targets": [
            {"level": lvl, "pts": round(lvl - leg.entry), "action": BOOK_PLAN[i][0], "pct": BOOK_PLAN[i][1]}
            for i, lvl in enumerate(targets)
        ],
        "stop_loss": leg.stop_loss,
        "stop_pts": round(leg.entry - leg.stop_loss),
    }


def _display_strength(o: float, h: float, l: float, c: float) -> tuple[float, bool]:
    """Directional-conviction % with close >= open counted as bullish (see above)."""
    stats = candle_stats(o, h, l, c)
    bullish = c >= o
    toward = stats.close_pos if bullish else (1 - stats.close_pos)
    return _clamp(100 * toward * (0.5 + stats.body_ratio * 0.5), 0, 100), bullish


def _pcr_bias(pcr: Optional[float]) -> str:
    if pcr is None:
        return "unknown"
    if pcr >= ANALYZER_PCR_BEARISH_MIN:
        return "bearish"
    if pcr <= ANALYZER_PCR_BULLISH_MAX:
        return "bullish"
    return "neutral"


def _leg_score(ohlc: tuple, side: str, pcr_bias: str) -> int:
    o, h, l, c = ohlc
    st = candle_stats(o, h, l, c)
    pts = 0.0
    if c >= o:
        pts += SCORE_W_DIRECTION
    if st.body_ratio >= ANALYZER_BODY_PASS:
        pts += SCORE_W_BODY
    elif st.body_ratio >= ANALYZER_BODY_WARN:
        pts += SCORE_W_BODY * SCORE_BODY_WARN_CREDIT
    if st.close_pos >= ANALYZER_CLOSE_POS_STRONG:
        pts += SCORE_W_CLOSE
    if (side == "call" and pcr_bias == "bullish") or (side == "put" and pcr_bias == "bearish"):
        pts += SCORE_W_PCR
    return int(pts + 0.5)


def analyzer_breakdown(
    call_ohlc: tuple[float, float, float, float],
    put_ohlc: tuple[float, float, float, float],
    cfg: dict = None,
    candle_closed: bool = True,
) -> dict:
    cfg = {**DEFAULT_CFG, **(cfg or {})}
    ce = analyse_side(*call_ohlc, cfg)
    pe = analyse_side(*put_ohlc, cfg)

    call_strength, call_bull = _display_strength(*call_ohlc)
    put_strength, put_bull = _display_strength(*put_ohlc)

    ce_close, pe_close = call_ohlc[3], put_ohlc[3]
    pcr = (pe_close / ce_close) if ce_close > 0 else None
    pcr_bias = _pcr_bias(pcr)

    call_score = _leg_score(call_ohlc, "call", pcr_bias)
    put_score = _leg_score(put_ohlc, "put", pcr_bias)
    gap = abs(call_score - put_score)
    leader = "call" if call_score >= put_score else "put"
    leader_score = max(call_score, put_score)
    if call_score == put_score:
        decision = "tie"
    elif gap < ANALYZER_MIN_GAP:
        decision = "noEdge"
    elif leader_score < ANALYZER_MIN_LEADER:
        decision = "insufficient"
    else:
        decision = "clear"
    side = leader if decision == "clear" else None

    def row(key, label, state, value):
        return {"key": key, "label": label, "state": state, "value": value}

    def body_state(ratio):
        return "pass" if ratio >= ANALYZER_BODY_PASS else ("warn" if ratio >= ANALYZER_BODY_WARN else "fail")

    checklist = [
        row("closed", "Candle fully closed?", "pass" if candle_closed else "fail",
            "CONFIRMED" if candle_closed else "STILL FORMING"),
    ]
    for name, ohlc, bull in (("Call", call_ohlc, call_bull), ("Put", put_ohlc, put_bull)):
        checklist.append(row(f"{name.lower()}_direction", f"{name} candle direction ({ohlc[0]:.2f}→{ohlc[3]:.2f})",
                             "pass" if bull else "fail", "BULLISH" if bull else "BEARISH"))
    for name, leg in (("Call", ce), ("Put", pe)):
        checklist.append(row(f"{name.lower()}_body", f"{name} body strength",
                             body_state(leg.stats.body_ratio), f"{leg.stats.body_ratio * 100:.2f}%"))
    # The reference lists the CALL leg's close position only.
    checklist.append(row("call_close_pos", "Call close position (near range top?)",
                         "pass" if ce.stats.close_pos >= ANALYZER_CLOSE_POS_STRONG else "fail",
                         f"{ce.stats.close_pos * 100:.2f}%"))
    checklist.append(row(
        "pcr", "PCR ratio (Put ÷ Call close)",
        "skip" if pcr is None else ("warn" if pcr_bias == "neutral" else "pass"),
        "n/a" if pcr is None else f"{pcr:.2f} → {pcr_bias.capitalize()}",
    ))

    # ---- verdict card (leader leg) ----
    lead_ohlc = call_ohlc if leader == "call" else put_ohlc
    lead_leg = ce if leader == "call" else pe
    lead_bull = call_bull if leader == "call" else put_bull
    lead_ladder = _ladder(lead_leg)
    if pcr is not None and ((leader == "call" and pcr < 1) or (leader == "put" and pcr > 1)):
        pcr_text = f"{pcr:.2f} ({'Bullish' if leader == 'call' else 'Bearish'}) ✓"
    else:
        pcr_text = "n/a" if pcr is None else f"{pcr:.2f}"
    reason = (
        f"{leader.capitalize()} candle: {'Bullish ✓' if lead_bull else 'Bearish ✗'} | "
        f"Body: {lead_leg.stats.body_ratio * 100:.2f}% {'✓' if lead_leg.stats.body_ratio >= ANALYZER_BODY_PASS else '⚠'} | "
        f"Close position: {lead_leg.stats.close_pos * 100:.2f}% of range "
        f"{'✓' if lead_leg.stats.close_pos >= ANALYZER_CLOSE_POS_STRONG else '⚠'} | PCR: {pcr_text}"
    )

    if side:
        buy = "BUY CALL" if side == "call" else "BUY PUT"
        t = lead_ladder["targets"]
        rules = [
            f"As soon as the candle close is confirmed → place the {buy} order at ₹{lead_ladder['entry']:.2f}.",
            f"SL ₹{lead_ladder['stop_loss']:.2f} — exit immediately if hit, no delay.",
            f"T1 ₹{t[0]['level']:.2f} hit → sell 40% (+{t[0]['pts']} pts).",
            f"T2 ₹{t[1]['level']:.2f} hit → sell another 40% (+{t[1]['pts']} pts).",
            f"T3 ₹{t[2]['level']:.2f} (+{t[2]['pts']} pts) — hold the last 20%.",
        ]
    else:
        why = (
            "both legs scored the same" if decision == "tie"
            else f"gap is only {gap}% (need {ANALYZER_MIN_GAP}%+)" if decision == "noEdge"
            else f"leader score {leader_score}% is below {ANALYZER_MIN_LEADER}%"
        )
        rules = [
            "No entry on this candle — conditions weak.",
            f"Call score {call_score}% vs Put score {put_score}% — {why}.",
            "Fetch again after the next candle closes.",
        ]

    return {
        "verdict": {
            "decision": decision, "side": side, "leader": leader,
            "signal": ("BULLISH SIGNAL" if side == "call" else "BEARISH SIGNAL" if side == "put" else "NEUTRAL — WAIT"),
            "headline": (f"YES — BUY {side.upper()}" if side else "NO — WAIT"),
            "confidence": leader_score,
            "reason": reason,
            "entry": lead_ladder["entry"] if side else None,
            "entry_note": f"Close {lead_ohlc[3]:.2f} + 0.5% buffer",
            "rules": rules,
        },
        "scores": {
            "call": call_score, "put": put_score, "margin": gap,
            "required_margin": ANALYZER_MIN_GAP, "required_confidence": ANALYZER_MIN_LEADER,
            "leader": leader, "decision": decision, "side": side,
        },
        "strength": {
            "call_pct": round(call_strength), "put_pct": round(put_strength),
            "call_bullish": call_bull, "put_bullish": put_bull,
        },
        "pcr": None if pcr is None else round(pcr, 2), "pcr_bias": pcr_bias,
        "checklist": checklist,
        "call_ladder": _ladder(ce), "put_ladder": _ladder(pe),
    }


def pullback_entry(side: Optional[str], ohlc: Optional[tuple]) -> dict:
    """
    The reference's Pullback Entry tab for the chosen leg: don't buy the
    already-pumped candle close — wait for a dip into 25/38/50% of the candle's
    range above its low. `side`/`ohlc` are the analyzer's leader; with no clear
    leader there is nothing to enter (WAIT). See the provenance note above.
    """
    if side is None or ohlc is None:
        return {"side": None, "wait": True}
    o, h, l, c = ohlc
    rng = h - l
    z1, z2, z3 = (_r1(l + f * rng) for f in PB_ZONES)
    stop = l - PB_SL_POINTS
    risk = _r1(z2 - stop)
    steps = [max(floor, _r1(mult * risk)) for floor, mult in zip(PB_TARGET_FLOORS, PB_TARGET_MULTS)]
    body = abs(c - o) / rng if rng > 0 else 0.0
    return {
        "side": side, "wait": False,
        "zones": {"zone1": z1, "zone2": z2, "zone3": z3},
        "stop_loss": stop, "stop_pts": risk,
        "targets": [{"level": _r1(z2 + s), "pts": s} for s in steps],
        "rr": _r1(steps[1] / risk) if risk > 0 else None,
        "cautions": ["Body weak — momentum is slow"] if body < ANALYZER_BODY_WARN else [],
        "rules": [
            "Candle closed → analysed.",
            "Wait for the next candle to open — do not chase the close.",
            f"Price touches Zone 1 (₹{z1:g}) → enter.",
            f"Price runs past Zone 3 (₹{z3:g}) without dipping → skip the trade.",
            f"SL ₹{stop:g} — only on a break of the candle's low structure.",
            "T1 hit → move SL to entry → risk-free.",
        ],
    }


def value_calculator(values: list[float]) -> dict:
    """
    The reference app's Value Calculator: six premiums (call entry, call T1,
    call T2, put entry, put T1, put T2) -> average -> its square root ->
    CALL level = avg - sqrt(avg) -> CALL target +25% -> PUT level = CALL - 55%
    -> PUT target +70%. Intermediates are NOT rounded between steps (the
    reference's own 131.95 / 11.49 / 120.46 / 150.58 / 54.21 / 92.15 only
    reproduces that way).
    """
    if len(values) != 6:
        raise ValueError("value_calculator needs exactly 6 values")
    if any(not (v == v) or v <= 0 for v in values):
        raise ValueError("all six values must be positive numbers")
    def _hu(n: float) -> float:
        # Half-up on the decimal value (sample 4's 413.07/6 = 68.845 must show
        # 68.85 like the reference; float round() gives 68.84). Display-only.
        from decimal import Decimal, ROUND_HALF_UP
        return float(Decimal(repr(round(n, 9))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    avg = sum(values) / 6
    root = avg ** 0.5
    call_level = avg - root
    put_level = call_level * (1 - 0.55)
    return {
        "inputs": [round(v, 2) for v in values],
        "average": _hu(avg),
        "sqrt_of_avg": _hu(root),
        "call_level": _hu(call_level),
        "call_target": _hu(call_level * 1.25),
        "put_level": _hu(put_level),
        "put_target": _hu(put_level * 1.70),
    }


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
