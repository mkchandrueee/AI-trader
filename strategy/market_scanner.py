"""
Market Scanner — ported from the NIFTY Option Strategy Suite's Market Scanner tab
────────────────────────────────────────────────────────────────────────────────
The reference tool's own description of this tab (scanner.js) is the design
spec here too: "every underlying row goes through APP.swing.readCandle,
exactly as the Candle Quality Scan does, which is APP.analyser.analyseSide
PLUS ITS MIRROR... nothing here invents a formula, re-weights a score, or
moves a threshold. What it adds is breadth and filters."

That "plus its mirror" is load-bearing and was missed in an earlier version
of this port: an underlying (index/stock) candle has no CE/PE counterpart
to compare against the way an option pair does, so scanner.js's swing.js
reads the SAME candle two ways — once with analyseSide's own bullish-only
formula (confidence = 20*bullish + 40*bodyRatio + 40*closePos), and once
with its bearish mirror (confidence = 20*bearish + 40*bodyRatio +
40*(1-closePos)) — and keeps whichever side scores higher. Without the
mirror, a textbook bearish marubozu (opened at the high, closed at the
low, all body) scores a middling ~40 under the bullish-only formula
instead of the 100 it deserves as a short setup — confirmed live against
the reference tool: BAJAJHLDNG's 2026-09-07 candle (O=H=11200, L=C=10956)
read 100% SHORT there and 40% here before this fix. _score_row below now
does both readings for stock/index rows (see analyse_underlying) and picks
the higher-scoring side, exactly like readCandle. Option rows are
unaffected — an option's own premium candle already has a real opposite
number (the other leg, CE vs PE), so mirroring one candle in isolation
isn't the reference's model there; analyse_side's bullish-only reading
applied directly to that contract's own candle is correct as-is, matching
how the Trade Decision Engine already scores CE/PE.

So this reuses the exact same candle-quality scoring already ported in
strategy/math_decision_strategy.py (analyse_side — entry/targets/stop/risk
AND confidence come from that one call, not a separate inline formula),
applied across a wide symbol universe instead of a single ATM CE/PE pair —
the INPUT is different (whole-market EOD bhavcopy vs. two option-premium
candles), the SCORING MATH is identical (for options; underlyings get the
long-vs-short mirror above).

Also ports scanner.js's own filter/breadth feature set, not just its core
formula: sector filter, F&O-tradable-only index filter, sort by
quality/R:R/change/turnover, a funnel count of what got filtered at each
step, and — the one genuinely different feature, not just another filter —
option legs on the top-ranked underlyings, which runs the Trade Decision
Engine (math_decision_strategy.analyse_option_pair) on the ATM contract of
whichever direction each top underlying is already leaning, exactly the
way scanner.js's optionRows() connects breadth to depth.

Data source: free NSE EOD bhavcopy via jugaad-data (data/jugaad_adapter.py)
— completed candles from the last published session, not a live intraday
read. Same trade-off the reference tool states on screen: NSE's live
board endpoints for a list this size are bot-protected / retired, but the
bhavcopy archive isn't.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict, field
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd

from strategy.math_decision_strategy import (
    analyse_side, analyse_option_pair, candle_stats, DEFAULT_CFG, LegAnalysis, _clamp, _round2,
)
from utils.logger import get_logger

logger = get_logger("market_scanner")

# Indices with listed F&O — the only ones a directional "which side" reading
# is actually tradable on. Maps the bhavcopy's title-case index name to its
# option-chain trading symbol (same mapping scanner.js uses).
INDEX_TRADING_SYMBOL = {
    "nifty 50": "NIFTY",
    "nifty bank": "BANKNIFTY",
    "nifty financial services": "FINNIFTY",
    "nifty midcap select": "MIDCPNIFTY",
    "nifty next 50": "NIFTYNXT50",
}

# Free, public, no-auth NSE archive constituent lists — same endpoint the
# Market Scanner reference tool uses for its NIFTY 50/100/200/500 filters
# (see _vendor-src/Market_Scanner/netlify/functions/market.mjs STOCK_LISTS).
# Not available via jugaad-data itself, so fetched directly here. Also the
# source of sector/industry data (an "Industry" column NSE already publishes
# in these same files) — scanner.js's own comment on this: "sectors offered
# by the filter come from the DATA, not from a table kept here that would
# drift out of date the next time NSE reclassifies."
NSE_ARCHIVES = "https://nsearchives.nseindia.com"
INDEX_LISTS = {
    "nifty50": ("ind_nifty50list", "NIFTY 50"),
    "nifty100": ("ind_nifty100list", "NIFTY 100"),
    "nifty200": ("ind_nifty200list", "NIFTY 200"),
    "nifty500": ("ind_nifty500list", "NIFTY 500"),
}
# Widest free list available — used as the sector lookup when the caller
# hasn't already picked a narrower index_list (so `sector` still works for
# an unfiltered "all stocks" scan, at the cost of stocks outside NIFTY 500
# showing no sector).
DEFAULT_SECTOR_SOURCE = "nifty500"

# SENSEX/BSE: jugaad-data has no free BSE bhavcopy (BSELive only exposes
# live quotes, not historical/EOD), so it can't join the bulk NSE-bhavcopy
# path every other index/stock row comes from. AngelOne's own instrument
# master DOES carry it, though — a real index token on exchange "BSE"
# (symbol "SENSEX") with real listed options on exchange "BFO" — so its
# daily candle is fetched directly via MarketDataAdapter and added to the
# board as a single extra row, same shape as everything else. This needs
# an authenticated AngelOne session (unlike the rest of the scan, which is
# free/no-auth NSE bhavcopy); if that's unavailable, SENSEX is silently
# skipped rather than failing the whole scan — see _fetch_sensex_row.
#
# Not extended to option legs / a stock constituent list: BSE has no free
# equity bhavcopy either, so there's no way to build a "SENSEX 50"-style
# constituent scan the same way NIFTY 50/100/200/500 work, and SENSEX's
# own ATM-strike resolution on BFO (different strike gaps, different
# expiry calendar) is a separate piece of work left for later.
_SENSEX_FETCH_DAYS_BACK = 7  # walk back this many calendar days for weekends/holidays


def fetch_sensex_ohlc(scan_date: date) -> Optional[dict]:
    """
    SENSEX's last completed daily candle at/before `scan_date`, straight from
    AngelOne (exchange "BSE") — BSE publishes no free bhavcopy, so this is
    the only source wired up. Returns open/high/low/close/prev_close/date, or
    None when AngelOne isn't connected or has nothing in the window.

    Shared with strategy/premarket.py, which needs the same raw candle for
    SENSEX's NextDay reading and ATM-strike spot — everything else in that
    module reads NSE bhavcopy, which has no SENSEX row at all.
    """
    from data.market_data_adapter import MarketDataAdapter

    adapter = MarketDataAdapter()
    if not adapter.authenticate():
        logger.info("SENSEX skipped — AngelOne not connected (free NSE data doesn't need this, BSE has no free bhavcopy).")
        return None

    end = datetime.combine(scan_date, datetime.min.time()) + timedelta(days=1)
    start = end - timedelta(days=_SENSEX_FETCH_DAYS_BACK + 1)
    try:
        df = adapter.fetch_historical_bars("SENSEX", start, end, "eod", exchange="BSE")
    except Exception as e:
        logger.warning(f"SENSEX fetch failed: {e}")
        return None
    if df.empty:
        return None

    df = df.sort_values("timestamp")
    last = df.iloc[-1]
    ts = last["timestamp"]
    return {
        "open": _safe_float(last["open"]), "high": _safe_float(last["high"]),
        "low": _safe_float(last["low"]), "close": _safe_float(last["close"]),
        "prev_close": float(df.iloc[-2]["close"]) if len(df) >= 2 else float("nan"),
        "date": ts.date() if hasattr(ts, "date") else scan_date,
    }


def _fetch_sensex_row(scan_date: date) -> Optional["ScanRow"]:
    ohlc = fetch_sensex_ohlc(scan_date)
    if ohlc is None:
        return None
    return _score_row(
        "SENSEX", "index",
        ohlc["open"], ohlc["high"], ohlc["low"], ohlc["close"],
        ohlc["prev_close"], tradable_fno=True,
    )

_index_list_cache: dict[str, tuple[datetime, pd.DataFrame]] = {}
_CONSTITUENTS_TTL = timedelta(hours=20)  # these lists change rarely (quarterly rebalance)


def _fetch_index_list_csv(index_list: str) -> pd.DataFrame:
    """
    Raw fetch+cache of one NSE constituent CSV (Symbol + Company Name +
    Industry columns), cached for a day. Empty DataFrame on any failure —
    callers treat that as "don't filter" rather than "return nothing".
    """
    if index_list not in INDEX_LISTS:
        return pd.DataFrame()

    cached = _index_list_cache.get(index_list)
    if cached and datetime.now() - cached[0] < _CONSTITUENTS_TTL:
        return cached[1]

    file_key, _ = INDEX_LISTS[index_list]
    try:
        # A bare `requests.get` here reliably times out — NSE blocks
        # requests without a browser-like User-Agent. Reuse jugaad-data's
        # own session (same headers it already uses successfully against
        # this exact host for bhavcopy) rather than duplicating that setup.
        from jugaad_data.nse.archives import NSEArchives
        session = NSEArchives().s
        resp = session.get(f"{NSE_ARCHIVES}/content/indices/{file_key}.csv", timeout=15)
        resp.raise_for_status()
        from io import StringIO
        df = pd.read_csv(StringIO(resp.text))
        _index_list_cache[index_list] = (datetime.now(), df)
        return df
    except Exception as e:
        logger.warning(f"Failed to fetch {file_key}.csv: {e}")
        return pd.DataFrame()


def fetch_index_constituents(index_list: str) -> set[str]:
    """Stock symbols currently in the given NSE index (nifty50/100/200/500)."""
    df = _fetch_index_list_csv(index_list)
    if df.empty:
        return set()
    sym_col = _find_col(df, ["symbol"])
    if not sym_col:
        return set()
    return {str(s).strip().upper() for s in df[sym_col] if str(s).strip()}


def fetch_index_industries(index_list: str) -> dict[str, str]:
    """{symbol: industry} for the given NSE index list — empty dict on failure."""
    df = _fetch_index_list_csv(index_list)
    if df.empty:
        return {}
    sym_col = _find_col(df, ["symbol"])
    ind_col = _find_col(df, ["industry"])
    if not sym_col or not ind_col:
        return {}
    return {
        str(r[sym_col]).strip().upper(): str(r[ind_col]).strip()
        for _, r in df.iterrows() if str(r[sym_col]).strip()
    }


@dataclass
class ScanRow:
    symbol: str
    kind: str  # "index" | "stock" | "option"
    open: float
    high: float
    low: float
    close: float
    prev_close: float
    change_pct: float
    confidence: float  # candle-quality score, 0-100 (see analyse_side)
    bullish: bool
    body_ratio: float
    close_pos: float
    # From analyse_side — a full entry/target/stop reading of this row's own
    # candle, same formula the Options Analyser/Trade Decision Engine use.
    entry: float = 0.0
    target1: float = 0.0
    target2: float = 0.0
    target3: float = 0.0
    stop_loss: float = 0.0
    risk: float = 0.0
    rr: Optional[float] = None  # None serializes to JSON null; NaN does not
    # Stocks only — turnover in ₹ lakhs, delivery % of traded volume, sector.
    turnover: float = 0.0
    delivery_pct: float = 0.0
    industry: str = ""
    # Indices only — has listed F&O (see INDEX_TRADING_SYMBOL).
    tradable_fno: bool = False
    # Options only (kind="option") — empty/zero for index and stock rows.
    underlying: str = ""
    strike: float = 0.0
    option_type: str = ""   # "CE" | "PE"
    expiry: str = ""        # YYYY-MM-DD
    oi: int = 0
    volume: int = 0


def _find_col(df: pd.DataFrame, candidates: list[str]) -> Optional[str]:
    lower = {c.lower().strip(): c for c in df.columns}
    for cand in candidates:
        if cand in lower:
            return lower[cand]
    return None


def _safe_float(val) -> float:
    """
    NSE bhavcopy files use '-' (and sometimes blank) as a not-traded/no-data
    placeholder in OHLC columns — a plain float() on that raises ValueError
    and, uncaught, took the whole scan down. Returns NaN for anything
    unparseable; _score_row's NaN guard (`v == v`) already skips those rows.
    """
    if val is None or (isinstance(val, float) and val != val):  # NaN
        return float("nan")
    try:
        return float(str(val).replace(",", "").strip())
    except (ValueError, TypeError):
        return float("nan")


# swing.js's priceCfg(): minTargetStep is 16 RUPEES OF OPTION PREMIUM, which
# is meaningless on a share price — on a ₹12 stock it demands a ₹16 target
# step (producing a NEGATIVE target), and on a ₹3000 one it never binds at
# all. The reference substitutes a percentage of price for underlying
# candles; config.js's own default is 0.25%.
SWING_MIN_STEP_PCT = 0.25


def _underlying_cfg(close: float) -> dict:
    """DEFAULT_CFG with the premium-scale target-step floor swapped for the
    price-scale one — for index/stock rows only, never for option premiums."""
    return {**DEFAULT_CFG, "minTargetStep": abs(close) * SWING_MIN_STEP_PCT / 100}


def _mirror_side(o: float, h: float, l: float, c: float, cfg: dict) -> "LegAnalysis":
    """
    swing.js's shortRead: the SAME three-term formula as analyse_side, read
    backwards — entry below close, targets below entry, stop above the
    high, confidence rewarding a bearish/full-body/low-close candle instead
    of a bullish/full-body/high-close one. Only exists for underlying
    (index/stock) rows, which have no CE/PE counterpart to compare against
    the way an option pair does — see the module docstring.
    """
    stats = candle_stats(o, h, l, c)
    rng = stats.range
    entry_pct = cfg.get("entryPremiumPct", 0.5)
    step_mult = cfg.get("targetStepMult", 0.8)
    min_step = cfg.get("minTargetStep", 16)
    t2_mult = cfg.get("t2Mult", 2.0)
    t3_mult = cfg.get("t3Mult", 3.5)
    sl_mult = cfg.get("slRangeMult", 0.3)

    entry = _round2(c * (1 - entry_pct / 100))
    step = max(step_mult * rng, min_step)
    target1 = _round2(entry - step)
    target2 = _round2(entry - t2_mult * step)
    target3 = _round2(entry - t3_mult * step)
    stop_loss = _round2(h + sl_mult * rng)

    confidence = _clamp(
        20 * (0 if stats.bullish else 1) + 40 * stats.body_ratio + 40 * (1 - stats.close_pos), 0, 100,
    )

    return LegAnalysis(
        stats=stats, entry=entry, target1=target1, target2=target2, target3=target3,
        stop_loss=stop_loss, risk=stop_loss - entry, confidence=confidence,
    )


def _score_row(symbol: str, kind: str, o: float, h: float, l: float, c: float, prev_close: float, **extra) -> Optional[ScanRow]:
    if not all(v == v and v > 0 for v in (o, h, l, c)):  # NaN/zero guard
        return None

    if kind == "option":
        # Option premiums keep the ₹16 premium-scale target-step floor AND
        # get no mirror — an option's own candle already has a real opposite
        # number (the other leg), so analyse_side's bullish-only reading
        # applied to that contract's own candle is correct (module docstring).
        leg, is_long = analyse_side(o, h, l, c, DEFAULT_CFG), True
    else:
        cfg = _underlying_cfg(c)  # price-scale floor, per swing.js's priceCfg
        long_leg = analyse_side(o, h, l, c, cfg)
        short_leg = _mirror_side(o, h, l, c, cfg)
        is_long = long_leg.confidence >= short_leg.confidence
        leg = long_leg if is_long else short_leg

    change_pct = ((c - prev_close) / prev_close * 100) if prev_close and prev_close == prev_close else 0.0
    rr = round(abs(leg.target2 - leg.entry) / leg.risk, 2) if leg.risk > 0 else None
    return ScanRow(
        symbol=symbol, kind=kind, open=o, high=h, low=l, close=c, prev_close=prev_close,
        change_pct=round(change_pct, 2), confidence=round(leg.confidence, 1),
        bullish=is_long, body_ratio=round(leg.stats.body_ratio, 3), close_pos=round(leg.stats.close_pos, 3),
        entry=leg.entry, target1=leg.target1, target2=leg.target2, target3=leg.target3,
        stop_loss=leg.stop_loss, risk=round(leg.risk, 2), rr=rr,
        **extra,
    )


def _score_equity_bhavcopy(df: pd.DataFrame) -> list[ScanRow]:
    sym_col = _find_col(df, ["symbol", "tckrsymb"])
    series_col = _find_col(df, ["series", "sctysrs"])
    # NSE has since moved the CM (equity) bhavcopy to the same abbreviated
    # UDiFF column layout F&O already used (tckrsymb/sctysrs/opnpric/
    # hghpric/lwpric/clspric/prvsclsgpric) — confirmed live 2026-09-07:
    # the openprice-style names below stopped matching anything, so every
    # stock row silently dropped out and universe="all"/"stocks" scans
    # returned indices only. Accept both layouts since NSE has changed
    # this format before and may again.
    o_col = _find_col(df, ["open_price", "openprice", "opnpric", "open"])
    h_col = _find_col(df, ["high_price", "highprice", "hghpric", "high"])
    l_col = _find_col(df, ["low_price", "lowprice", "lwpric", "low"])
    c_col = _find_col(df, ["close_price", "closeprice", "clspric", "close"])
    prev_col = _find_col(df, ["prev_close", "prevclose", "prvsclsgpric", "prev. close"])
    turnover_col = _find_col(df, ["turnover_lacs", "turnover"])
    # The UDiFF layout has no turnover-in-lacs column at all; ttltrfval
    # (total traded value) is the closest equivalent but in raw rupees, so
    # it's scaled to lacs below to keep the same unit the field always had.
    turnover_raw_col = None if turnover_col else _find_col(df, ["ttltrfval"])
    deliv_col = _find_col(df, ["deliv_per", "delivery_pct", "%dly qt to traded qty"])
    # UDiFF also dropped delivery % entirely — no replacement column exists
    # in this bhavcopy, so it stays 0 for every row (see ScanRow default)
    # until NSE publishes it again or a separate free source is found.
    if not all([sym_col, o_col, h_col, l_col, c_col]):
        logger.warning(f"Equity bhavcopy missing expected columns: {list(df.columns)}")
        return []

    rows = []
    for _, r in df.iterrows():
        if series_col and str(r[series_col]).strip() != "EQ":
            continue
        turnover = (
            _safe_float(r[turnover_col]) if turnover_col
            else _safe_float(r[turnover_raw_col]) / 100_000 if turnover_raw_col
            else 0.0
        )
        row = _score_row(
            str(r[sym_col]).strip(), "stock",
            _safe_float(r[o_col]), _safe_float(r[h_col]), _safe_float(r[l_col]), _safe_float(r[c_col]),
            _safe_float(r[prev_col]) if prev_col else float("nan"),
            turnover=turnover,
            delivery_pct=_safe_float(r[deliv_col]) if deliv_col else 0.0,
        )
        if row:
            if row.turnover != row.turnover:
                row.turnover = 0.0
            if row.delivery_pct != row.delivery_pct:
                row.delivery_pct = 0.0
            rows.append(row)
    return rows


def _score_index_bhavcopy(df: pd.DataFrame) -> list[ScanRow]:
    name_col = _find_col(df, ["index name", "index_name", "indexname"])
    o_col = _find_col(df, ["open index value", "open"])
    h_col = _find_col(df, ["high index value", "high"])
    l_col = _find_col(df, ["low index value", "low"])
    c_col = _find_col(df, ["closing index value", "close index value", "close"])
    prev_col = _find_col(df, ["prev close", "prev. close", "prevclose"])
    if not all([name_col, o_col, h_col, l_col, c_col]):
        logger.warning(f"Index bhavcopy missing expected columns: {list(df.columns)}")
        return []

    rows = []
    for _, r in df.iterrows():
        raw_name = str(r[name_col]).strip()
        trading_symbol = INDEX_TRADING_SYMBOL.get(raw_name.lower())
        symbol = trading_symbol or raw_name
        prev_close = _safe_float(r[prev_col]) if prev_col else _safe_float(r[c_col])  # best effort
        row = _score_row(
            symbol, "index",
            _safe_float(r[o_col]), _safe_float(r[h_col]), _safe_float(r[l_col]), _safe_float(r[c_col]), prev_close,
            tradable_fno=trading_symbol is not None,
        )
        if row:
            rows.append(row)
    return rows


def _score_fo_bhavcopy(
    df: pd.DataFrame, option_type: str = "", nearest_expiry_only: bool = True,
) -> list[ScanRow]:
    """
    Score option contracts from the F&O UDiFF bhavcopy — the reference
    tool's "index options / stock options" universe. Each contract's own
    premium candle goes through the exact same candle-quality formula as
    everything else here; nothing option-specific is invented.

    Defaults to the nearest expiry per underlying, since scoring every
    listed expiry for every strike (tens of thousands of rows, most with a
    single stale trade or none at all) isn't useful — this mirrors why
    math_decision_strategy only ever looks at the current ATM contract.
    """
    tkr_col = _find_col(df, ["tckrsymb", "symbol"])
    opt_col = _find_col(df, ["optntp", "option_type"])
    strike_col = _find_col(df, ["strkpric", "strike_price", "strike"])
    xpry_col = _find_col(df, ["xprydt", "expiry_dt", "expiry"])
    o_col = _find_col(df, ["opnpric", "open_price", "open"])
    h_col = _find_col(df, ["hghpric", "high_price", "high"])
    l_col = _find_col(df, ["lwpric", "low_price", "low"])
    c_col = _find_col(df, ["clspric", "close_price", "close"])
    prev_col = _find_col(df, ["prvsclsgpric", "prev_close", "prevclose"])
    oi_col = _find_col(df, ["opnintrst", "open_interest", "oi"])
    vol_col = _find_col(df, ["ttltradgvol", "volume", "contracts"])
    if not all([tkr_col, opt_col, strike_col, xpry_col, o_col, h_col, l_col, c_col]):
        logger.warning(f"F&O bhavcopy missing expected columns: {list(df.columns)}")
        return []

    work = df[df[opt_col].isin(["CE", "PE"])].copy()  # drop futures rows (OptnTp is blank/NaN there)
    if option_type in ("CE", "PE"):
        work = work[work[opt_col] == option_type]

    if nearest_expiry_only:
        work["_xpry_parsed"] = pd.to_datetime(work[xpry_col], errors="coerce")
        nearest = work.groupby(tkr_col)["_xpry_parsed"].transform("min")
        work = work[work["_xpry_parsed"] == nearest]

    rows = []
    for _, r in work.iterrows():
        underlying = str(r[tkr_col]).strip()
        strike = _safe_float(r[strike_col])
        opt_type = str(r[opt_col]).strip()
        expiry = str(r[xpry_col]).strip()
        symbol = f"{underlying} {strike:.0f}{opt_type} {expiry}"
        row = _score_row(
            symbol, "option",
            _safe_float(r[o_col]), _safe_float(r[h_col]), _safe_float(r[l_col]), _safe_float(r[c_col]),
            _safe_float(r[prev_col]) if prev_col else float("nan"),
            underlying=underlying, strike=strike, option_type=opt_type, expiry=expiry,
            oi=int(_safe_float(r[oi_col])) if oi_col and r[oi_col] == r[oi_col] else 0,
            volume=int(_safe_float(r[vol_col])) if vol_col and r[vol_col] == r[vol_col] else 0,
        )
        if row:
            rows.append(row)
    return rows


def _find_atm_pair(fo_df: pd.DataFrame, underlying: str, spot: float) -> Optional[tuple[tuple, tuple]]:
    """
    For one underlying's nearest expiry, find the CE and PE rows whose
    strike is closest to `spot` (ATM). Returns ((o,h,l,c) for CE, (o,h,l,c)
    for PE) or None if either leg is missing (e.g. an illiquid strike with
    no trade that day — matches _score_row's own NaN/zero guard rather than
    inventing a synthetic candle for a leg that didn't trade).
    """
    tkr_col = _find_col(fo_df, ["tckrsymb", "symbol"])
    opt_col = _find_col(fo_df, ["optntp", "option_type"])
    strike_col = _find_col(fo_df, ["strkpric", "strike_price", "strike"])
    xpry_col = _find_col(fo_df, ["xprydt", "expiry_dt", "expiry"])
    o_col = _find_col(fo_df, ["opnpric", "open_price", "open"])
    h_col = _find_col(fo_df, ["hghpric", "high_price", "high"])
    l_col = _find_col(fo_df, ["lwpric", "low_price", "low"])
    c_col = _find_col(fo_df, ["clspric", "close_price", "close"])
    if not all([tkr_col, opt_col, strike_col, xpry_col, o_col, h_col, l_col, c_col]):
        return None

    under_df = fo_df[(fo_df[tkr_col] == underlying) & (fo_df[opt_col].isin(["CE", "PE"]))].copy()
    if under_df.empty:
        return None
    under_df["_xpry_parsed"] = pd.to_datetime(under_df[xpry_col], errors="coerce")
    nearest_expiry = under_df["_xpry_parsed"].min()
    under_df = under_df[under_df["_xpry_parsed"] == nearest_expiry]

    def _leg(opt_type: str) -> Optional[tuple]:
        leg_df = under_df[under_df[opt_col] == opt_type].copy()
        if leg_df.empty:
            return None
        leg_df["_dist"] = (leg_df[strike_col].apply(_safe_float) - spot).abs()
        leg_df = leg_df.sort_values("_dist")
        best = leg_df.iloc[0]
        ohlc = (_safe_float(best[o_col]), _safe_float(best[h_col]), _safe_float(best[l_col]), _safe_float(best[c_col]))
        if not all(v == v and v > 0 for v in ohlc):
            return None
        return ohlc

    ce, pe = _leg("CE"), _leg("PE")
    if ce is None or pe is None:
        return None
    return ce, pe


def scan_market(
    scan_date: Optional[date] = None,
    universe: str = "all",          # "all" | "indices" | "stocks" | "options"
    direction: str = "all",         # "all" | "bullish" | "bearish"
    min_confidence: float = 0.0,
    search: str = "",
    limit: int = 100,
    index_list: str = "",           # "" | "nifty50" | "nifty100" | "nifty200" | "nifty500"
    option_type: str = "",          # "" | "CE" | "PE" — only used when universe="options"
    nearest_expiry_only: bool = True,  # only used when universe="options"
    sector: str = "",                # exact match against a stock's Industry (from the constituent list)
    index_type: str = "",            # "" | "fno" | "traded" — index-only, see below
    sort_by: str = "confidence",     # "confidence" | "rr" | "change" | "turnover"
    include_option_legs: bool = False,
    top_n_option_legs: int = 10,
) -> dict:
    """
    Score every row of the latest published EOD bhavcopy with the same
    candle-quality formula the rest of the app uses (analyse_side), then
    filter and rank. Falls back one trading day at a time if a bhavcopy
    isn't published yet for the requested date (weekends/holidays/today
    before EOD).

    `index_list`, when set, restricts STOCK rows to that index's current
    constituents (e.g. "nifty50" -> only NIFTY 50 members) — index rows
    are never affected by it.

    `sector`, when set, restricts STOCK rows to that Industry classification
    (from the same constituent list — nifty500 by default, or index_list's
    own list if one was given, so `sector` narrows whatever universe
    `index_list` already picked rather than a second independent fetch).

    `index_type` restricts INDEX rows: "fno" keeps only indices with listed
    options (NIFTY/BANKNIFTY/FINNIFTY/MIDCPNIFTY/NIFTYNXT50 — the same set
    INDEX_TRADING_SYMBOL maps); "traded" is a stricter version reserved for
    when the bhavcopy carries real per-index turnover (most NSE index
    bhavcopy exports don't), so it currently behaves the same as "fno".
    Without this, "all" 130+ published indices show up, including untradeable
    bond/sector indices with no F&O market at all.

    `universe="options"` is a separate, explicit choice (not folded into
    "all") — it scores every CE/PE contract's own premium candle instead of
    the underlying's cash-market candle. Not bundled into the default scan
    because the F&O bhavcopy is a much larger, differently-shaped fetch
    (~32k rows across every underlying/strike/expiry) that most scans don't
    need. Use `search` (e.g. "NIFTY") to narrow to one underlying's options.

    `include_option_legs`: after everything else is filtered/sorted, take
    the top `top_n_option_legs` INDEX/STOCK rows with a clear bullish/bearish
    read and, for each, find its ATM CE (if bullish) or PE (if bearish) at
    the nearest expiry and run it through the Trade Decision Engine
    (analyse_option_pair). Only kept if the engine's own side pick agrees
    with the underlying's direction — same rule scanner.js's optionRows()
    uses, so a top-ranked bullish stock whose actual option chain reads
    bearish doesn't get a misleading recommendation attached. Costs one
    extra F&O bhavcopy fetch (~32k rows), so it's opt-in.
    """
    from data.jugaad_adapter import fetch_bhavcopy, fetch_index_bhavcopy

    scan_date = scan_date or date.today()
    rows: list[ScanRow] = []
    fo_df_for_legs = pd.DataFrame()

    def _fetch_with_fallback(fetch_fn, max_days: int = 5) -> tuple[pd.DataFrame, Optional[date]]:
        """
        Walk back up to `max_days` from scan_date to find a published
        session. Segments are tried independently (not a single shared
        walk-back) because they don't fail in lockstep — e.g. equity
        bhavcopy has been observed to return the latest published session
        regardless of the exact date requested (including weekends),
        while index/F&O bhavcopy correctly error on non-trading days. A
        shared loop that breaks on the FIRST segment to succeed would
        silently drop the other segments from a universe="all" scan.
        """
        d = scan_date
        for _ in range(max_days):
            df = fetch_fn(d)
            if not df.empty:
                return df, d
            d = d - timedelta(days=1)
        return pd.DataFrame(), None

    eq_df, eq_date = _fetch_with_fallback(lambda d: fetch_bhavcopy(d, segment="EQ")) if universe in ("all", "stocks") else (pd.DataFrame(), None)
    idx_df, idx_date = _fetch_with_fallback(fetch_index_bhavcopy) if universe in ("all", "indices") else (pd.DataFrame(), None)
    need_fo = universe == "options" or include_option_legs
    fo_df, fo_date = _fetch_with_fallback(lambda d: fetch_bhavcopy(d, segment="FO")) if need_fo else (pd.DataFrame(), None)
    sensex_row = _fetch_sensex_row(scan_date) if universe in ("all", "indices") else None

    if eq_df.empty and idx_df.empty and fo_df.empty and sensex_row is None:
        return {"date": None, "rows": [], "total_before_filter": 0, "message": "No published bhavcopy found in the last 5 days."}

    if not eq_df.empty:
        rows.extend(_score_equity_bhavcopy(eq_df))
    if not idx_df.empty:
        rows.extend(_score_index_bhavcopy(idx_df))
    if sensex_row is not None:
        rows.append(sensex_row)
    if universe == "options" and not fo_df.empty:
        rows.extend(_score_fo_bhavcopy(fo_df, option_type=option_type, nearest_expiry_only=nearest_expiry_only))
    if include_option_legs:
        fo_df_for_legs = fo_df

    # idx_date is the most trustworthy "actual session" indicator (index
    # bhavcopy reliably errors on non-trading days; equity's does not — see
    # above), so prefer it for the date reported back to the caller. Falls
    # back to scan_date itself in the (rare) case only SENSEX came back —
    # AngelOne's own candle fetch doesn't hand back which date it landed on.
    used_date = idx_date or eq_date or fo_date or (scan_date if sensex_row is not None else None)

    total_before_filter = len(rows)
    counts = {"published": total_before_filter}

    if index_type in ("fno", "traded"):
        rows = [r for r in rows if r.kind != "index" or r.tradable_fno]
    counts["after_index_type"] = len(rows)

    if index_list:
        constituents = fetch_index_constituents(index_list)
        if constituents:
            rows = [r for r in rows if r.kind != "stock" or r.symbol.upper() in constituents]
        else:
            logger.warning(f"Could not fetch constituents for {index_list!r} — index_list filter not applied")
    counts["after_index_list"] = len(rows)

    if sector:
        industries = fetch_index_industries(index_list or DEFAULT_SECTOR_SOURCE)
        if industries:
            for r in rows:
                if r.kind == "stock":
                    r.industry = industries.get(r.symbol.upper(), r.industry)
            rows = [r for r in rows if r.kind != "stock" or r.industry.lower() == sector.lower()]
        else:
            logger.warning("Could not fetch sector data — sector filter not applied")
    elif index_list or universe in ("all", "stocks"):
        # Populate industry even without a sector filter, so the UI can
        # still show/offer it — cheap, this list is already cached.
        industries = fetch_index_industries(index_list or DEFAULT_SECTOR_SOURCE)
        if industries:
            for r in rows:
                if r.kind == "stock" and r.symbol.upper() in industries:
                    r.industry = industries[r.symbol.upper()]
    counts["after_sector"] = len(rows)

    if direction == "bullish":
        rows = [r for r in rows if r.bullish]
    elif direction == "bearish":
        rows = [r for r in rows if not r.bullish]
    counts["after_direction"] = len(rows)

    if min_confidence > 0:
        rows = [r for r in rows if r.confidence >= min_confidence]
    counts["qualified"] = len(rows)

    if search:
        needle = search.strip().upper()
        rows = [r for r in rows if needle in r.symbol.upper()]
    counts["after_search"] = len(rows)

    sectors_available = sorted({r.industry for r in rows if r.kind == "stock" and r.industry})

    sort_key = {
        "rr": lambda r: r.rr if r.rr is not None else -1,
        "change": lambda r: abs(r.change_pct),
        "turnover": lambda r: r.turnover,
    }.get(sort_by, lambda r: r.confidence)
    rows.sort(key=sort_key, reverse=True)

    option_legs: list[dict] = []
    if include_option_legs:
        if fo_df_for_legs.empty:
            logger.warning("include_option_legs requested but F&O bhavcopy fetch failed — no legs computed")
        else:
            # Only ~180 stocks (plus the 5 indices in INDEX_TRADING_SYMBOL)
            # actually have listed F&O contracts. Ranking by pure candle
            # quality/turnover/etc. surfaces mostly non-F&O micro-caps, so
            # restrict to the F&O underlying universe *before* taking the
            # top N — otherwise top_n_option_legs candidates would mostly
            # resolve to no contract and the feature would look broken.
            tkr_col = _find_col(fo_df_for_legs, ["tckrsymb", "symbol"])
            fno_underlyings = set(fo_df_for_legs[tkr_col].unique()) if tkr_col else set()
            candidates = [r for r in rows if r.kind in ("index", "stock") and r.symbol in fno_underlyings][:top_n_option_legs]
            for r in candidates:
                pair = _find_atm_pair(fo_df_for_legs, r.symbol, r.close)
                if pair is None:
                    continue
                ce_ohlc, pe_ohlc = pair
                decision = analyse_option_pair(ce_ohlc, pe_ohlc)
                expected = "call" if r.bullish else "put"
                actual = "call" if decision.side == "call" else ("put" if decision.side == "put" else None)
                if actual != expected or not decision.tradable:
                    continue
                option_legs.append({
                    "underlying": r.symbol, "underlying_confidence": r.confidence,
                    "side": decision.side, "entry": decision.entry, "partial": decision.partial,
                    "target": decision.target, "stop": decision.stop, "rr": decision.rr,
                    "confidence": decision.confidence, "tier": decision.tier,
                })

    rows = rows[:limit]

    return {
        "date": used_date.isoformat(),
        "rows": [asdict(r) for r in rows],
        "total_before_filter": total_before_filter,
        "returned": len(rows),
        "counts": counts,
        "sectors_available": sectors_available,
        "option_legs": option_legs,
    }
