"""
Market Scanner — ported from the NIFTY Option Strategy Suite's Market Scanner tab
────────────────────────────────────────────────────────────────────────────────
The reference tool's own description of this tab (scanner.js) is the design
spec here too: "every underlying row goes through analyseSide, exactly as
the Candle Quality Scan does... nothing here invents a formula, re-weights a
score, or moves a threshold. What it adds is breadth and filters."

So this reuses the exact same candle-quality scoring already ported in
strategy/math_decision_strategy.py (candle_stats + analyse_side), applied
across a wide symbol universe instead of a single ATM CE/PE pair — the
INPUT is different (whole-market EOD bhavcopy vs. two option-premium
candles), the SCORING MATH is identical.

Data source: free NSE EOD bhavcopy via jugaad-data (data/jugaad_adapter.py)
— completed candles from the last published session, not a live intraday
read. Same trade-off the reference tool states on screen: NSE's live
board endpoints for a list this size are bot-protected / retired, but the
bhavcopy archive isn't.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd

from strategy.math_decision_strategy import candle_stats, DEFAULT_CFG, _clamp
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
# Not available via jugaad-data itself, so fetched directly here.
NSE_ARCHIVES = "https://nsearchives.nseindia.com"
INDEX_LISTS = {
    "nifty50": ("ind_nifty50list", "NIFTY 50"),
    "nifty100": ("ind_nifty100list", "NIFTY 100"),
    "nifty200": ("ind_nifty200list", "NIFTY 200"),
    "nifty500": ("ind_nifty500list", "NIFTY 500"),
}

# SENSEX/BSE: no free bhavcopy or constituent-list source is currently wired
# into this project. jugaad-data's BSE module only exposes live quotes
# (BSELive), not historical/EOD data, and BSE's own bhavcopy endpoints
# aren't verified here — rather than guess at an unstable URL, this is
# left as a known gap. NSE-listed BANKNIFTY does NOT have this problem —
# it's already a normal row in the "indices" universe (see
# INDEX_TRADING_SYMBOL above); use `search=BANKNIFTY` to find it directly.

_constituents_cache: dict[str, tuple[datetime, set[str]]] = {}
_CONSTITUENTS_TTL = timedelta(hours=20)  # these lists change rarely (quarterly rebalance)


def fetch_index_constituents(index_list: str) -> set[str]:
    """
    Stock symbols currently in the given NSE index (nifty50/100/200/500),
    cached for a day. Returns an empty set (not an error) on any failure —
    scan_market() treats that as "don't filter" rather than "return nothing".
    """
    if index_list not in INDEX_LISTS:
        return set()

    cached = _constituents_cache.get(index_list)
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
        sym_col = _find_col(df, ["symbol"])
        if not sym_col:
            logger.warning(f"{file_key}.csv missing a Symbol column: {list(df.columns)}")
            return set()
        symbols = {str(s).strip().upper() for s in df[sym_col] if str(s).strip()}
        _constituents_cache[index_list] = (datetime.now(), symbols)
        return symbols
    except Exception as e:
        logger.warning(f"Failed to fetch {file_key}.csv: {e}")
        return set()


@dataclass
class ScanRow:
    symbol: str
    kind: str  # "index" | "stock"
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


def _score_row(symbol: str, kind: str, o: float, h: float, l: float, c: float, prev_close: float) -> Optional[ScanRow]:
    if not all(v == v and v > 0 for v in (o, h, l, c)):  # NaN/zero guard
        return None
    stats = candle_stats(o, h, l, c)
    confidence = _clamp(
        20 * (1 if stats.bullish else 0) + 40 * stats.body_ratio + 40 * stats.close_pos, 0, 100,
    )
    change_pct = ((c - prev_close) / prev_close * 100) if prev_close and prev_close == prev_close else 0.0
    return ScanRow(
        symbol=symbol, kind=kind, open=o, high=h, low=l, close=c, prev_close=prev_close,
        change_pct=round(change_pct, 2), confidence=round(confidence, 1),
        bullish=stats.bullish, body_ratio=round(stats.body_ratio, 3), close_pos=round(stats.close_pos, 3),
    )


def _score_equity_bhavcopy(df: pd.DataFrame) -> list[ScanRow]:
    sym_col = _find_col(df, ["symbol"])
    series_col = _find_col(df, ["series"])
    # jugaad-data's current (UDIFF) bhavcopy format uses open_price/high_price/
    # low_price/close_price, not the classic openprice-style names — accept both
    # since NSE has changed this format before and may again.
    o_col = _find_col(df, ["open_price", "openprice", "open"])
    h_col = _find_col(df, ["high_price", "highprice", "high"])
    l_col = _find_col(df, ["low_price", "lowprice", "low"])
    c_col = _find_col(df, ["close_price", "closeprice", "close"])
    prev_col = _find_col(df, ["prev_close", "prevclose", "prev. close"])
    if not all([sym_col, o_col, h_col, l_col, c_col]):
        logger.warning(f"Equity bhavcopy missing expected columns: {list(df.columns)}")
        return []

    rows = []
    for _, r in df.iterrows():
        if series_col and str(r[series_col]).strip() != "EQ":
            continue
        row = _score_row(
            str(r[sym_col]).strip(), "stock",
            _safe_float(r[o_col]), _safe_float(r[h_col]), _safe_float(r[l_col]), _safe_float(r[c_col]),
            _safe_float(r[prev_col]) if prev_col else float("nan"),
        )
        if row:
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
        )
        if row:
            rows.append(row)
    return rows


def scan_market(
    scan_date: Optional[date] = None,
    universe: str = "all",          # "all" | "indices" | "stocks"
    direction: str = "all",         # "all" | "bullish" | "bearish"
    min_confidence: float = 0.0,
    search: str = "",
    limit: int = 100,
    index_list: str = "",           # "" | "nifty50" | "nifty100" | "nifty200" | "nifty500"
) -> dict:
    """
    Score every row of the latest published EOD bhavcopy with the same
    candle-quality formula the rest of the app uses (analyse_side), then
    filter and rank. Falls back one trading day at a time if a bhavcopy
    isn't published yet for the requested date (weekends/holidays/today
    before EOD).

    `index_list`, when set, restricts STOCK rows to that index's current
    constituents (e.g. "nifty50" -> only NIFTY 50 members) — index rows
    (kind="index") are never affected by it, there's no equivalent concept
    for them.
    """
    from data.jugaad_adapter import fetch_bhavcopy, fetch_index_bhavcopy

    scan_date = scan_date or date.today()
    rows: list[ScanRow] = []
    used_date = scan_date

    for attempt in range(5):  # walk back up to 5 days to find a published session
        eq_df = fetch_bhavcopy(used_date, segment="EQ") if universe in ("all", "stocks") else pd.DataFrame()
        idx_df = fetch_index_bhavcopy(used_date) if universe in ("all", "indices") else pd.DataFrame()

        if not eq_df.empty or not idx_df.empty:
            if not eq_df.empty:
                rows.extend(_score_equity_bhavcopy(eq_df))
            if not idx_df.empty:
                rows.extend(_score_index_bhavcopy(idx_df))
            break

        used_date = used_date - timedelta(days=1)
    else:
        return {"date": None, "rows": [], "total_before_filter": 0, "message": "No published bhavcopy found in the last 5 days."}

    total_before_filter = len(rows)

    if index_list:
        constituents = fetch_index_constituents(index_list)
        if constituents:
            rows = [r for r in rows if r.kind != "stock" or r.symbol.upper() in constituents]
        else:
            logger.warning(f"Could not fetch constituents for {index_list!r} — index_list filter not applied")

    if direction == "bullish":
        rows = [r for r in rows if r.bullish]
    elif direction == "bearish":
        rows = [r for r in rows if not r.bullish]

    if min_confidence > 0:
        rows = [r for r in rows if r.confidence >= min_confidence]

    if search:
        needle = search.strip().upper()
        rows = [r for r in rows if needle in r.symbol.upper()]

    rows.sort(key=lambda r: r.confidence, reverse=True)
    rows = rows[:limit]

    return {
        "date": used_date.isoformat(),
        "rows": [asdict(r) for r in rows],
        "total_before_filter": total_before_filter,
        "returned": len(rows),
    }
