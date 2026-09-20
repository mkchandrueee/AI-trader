"""
Positional Engine -- market regime, sector leadership and chart-pattern setups
(IPO base, VCP, horizontal break, flag & pole) over the whole NSE EQ board,
computed from the local EOD store (data/eod_store.py).

The pattern rules below are OUR OWN definitions of the well-known setups, not
a copy of any commercial screener; thresholds are module constants so they can
be tuned. Every detector is a pure function of numpy arrays ending at the bar
being evaluated ("as of" that bar), which is what lets `replay_stats` measure
each pattern's real forward hit-rate against the all-stock baseline instead of
assuming it works.

Caveats worth knowing:
  * Bhavcopy prices are NOT split/bonus adjusted. Any symbol whose prior-close
    field disagrees with the previous day's close inside the lookback (a
    corporate action) is skipped rather than risk a fake breakout.
  * History is ~130 sessions, so trend tests use SMA50/SMA100, not SMA200, and
    "IPO base" means listed inside that window.
  * Replay horizons are 10 sessions (not 20/30) for the same reason.
"""
from __future__ import annotations

import json
import math
import re
from datetime import date
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pandas as pd

from utils.logger import get_logger

logger = get_logger("positional_scanner")

# ── universe / hygiene ────────────────────────────────────────────────────────
MIN_SESSIONS = 60
MIN_PRICE = 20.0
MIN_MEDIAN_TURNOVER = 2e7          # Rs.2 crore/day median over the last 20 sessions
CORP_ACTION_GAP = 0.03             # |prev_close / yesterday close - 1| above this = adjusted/unclean

# ── pattern parameters ────────────────────────────────────────────────────────
HB_BOX_BARS = 25
HB_MAX_DEPTH = 0.15
HB_MIN_TOUCHES = 2
HB_NEAR_PCT = 0.03                 # within 3% under the box top = "coiling"
FP_POLE_MIN_GAIN = 0.25
FP_POLE_BARS = 15
FP_FLAG_MIN, FP_FLAG_MAX = 4, 15
FP_MAX_RETRACE = 0.50
VCP_WINDOW = 15                    # three consecutive 15-bar windows
VCP_MAX_FINAL_RANGE = 0.12
VCP_NEAR_HIGH = 0.95
IPO_MAX_SESSIONS = 100
IPO_MIN_SESSIONS = 15
IPO_MAX_RECENT_RANGE = 0.20
IPO_NEAR_HIGH = 0.85
BREAKOUT_VOL_MULT = 1.5

FORWARD_SESSIONS = 10
FORWARD_HIT = 0.10                 # +10% intraday within the forward window
FORWARD_DRAWDOWN = -0.10           # closing 10% under the signal close after it

_RESULT_CACHE = Path(__file__).resolve().parent.parent / "data" / "eod_cache" / "_replay_stats.json"


# ── helpers ───────────────────────────────────────────────────────────────────
def _sma(x: np.ndarray, n: int) -> float:
    return float(x[-n:].mean()) if len(x) >= n else float("nan")


def _vol_ratio(v: np.ndarray, avg_bars: int = 20) -> float:
    base = v[-avg_bars - 1:-1]
    m = float(base.mean()) if len(base) else 0.0
    return float(v[-1]) / m if m > 0 else 0.0


def _clean(prev_close: np.ndarray, c: np.ndarray, bars: int = 60) -> bool:
    """False if a corporate-action price gap sits inside the lookback."""
    pc, cc = prev_close[-bars:], c[-bars - 1:-1] if len(c) > bars else c[:-1]
    m = min(len(pc) - 1, len(cc))
    if m <= 0:
        return True
    a, b = pc[-m:], cc[-m:]
    with np.errstate(divide="ignore", invalid="ignore"):
        gap = np.abs(a / b - 1.0)
    return bool(np.all(np.nan_to_num(gap, nan=0.0) <= CORP_ACTION_GAP))


def _score(base: int, *bonuses: bool) -> int:
    return int(min(10, base + sum(1 for b in bonuses if b)))


def _out(kind: str, state: str, score: int, entry: float, stop: float, c: float, *, box: Optional[tuple] = None, **extra) -> dict:
    risk = entry - stop
    return {
        "pattern": kind, "state": state, "score": score,
        "entry": round(entry, 2), "stop": round(stop, 2), "close": round(c, 2),
        "risk_pct": round(100 * risk / entry, 1) if entry > 0 else None,
        "box": None if box is None else [box[0], round(box[1], 2), round(box[2], 2)],  # [bars_back, top, bottom]
        **extra,
    }


# ── detectors: arrays end at the evaluated bar; each returns a dict or None ──
def detect_horizontal(o, h, l, c, v) -> Optional[dict]:
    if len(c) < HB_BOX_BARS + 30:
        return None
    hi_box, lo_box = float(h[-HB_BOX_BARS - 1:-1].max()), float(l[-HB_BOX_BARS - 1:-1].min())
    depth = (hi_box - lo_box) / hi_box
    if depth > HB_MAX_DEPTH:
        return None
    touches = int(np.sum(h[-HB_BOX_BARS - 1:-1] >= hi_box * 0.98))
    if touches < HB_MIN_TOUCHES:
        return None
    last = float(c[-1])
    sma50 = _sma(c, 50)
    if last > hi_box:
        state = "breakout"
    elif last >= hi_box * (1 - HB_NEAR_PCT):
        state = "coiling"
    else:
        return None
    vr = _vol_ratio(v)
    sc = _score(3, state == "breakout" and vr >= BREAKOUT_VOL_MULT, vr >= 2.5, last > sma50, depth <= 0.10,
                touches >= 3, sma50 > _sma(c, 100) if len(c) >= 100 else False)
    return _out("horizontal", state, sc, hi_box, lo_box, last, box=(HB_BOX_BARS, hi_box, lo_box),
                depth_pct=round(100 * depth, 1), touches=touches, vol_ratio=round(vr, 2))


def detect_flag(o, h, l, c, v) -> Optional[dict]:
    n = len(c)
    if n < FP_POLE_BARS + FP_FLAG_MAX + 30:
        return None
    last = float(c[-1])
    best = None
    for fl in range(FP_FLAG_MIN, FP_FLAG_MAX + 1):
        flag_h, flag_l = h[-fl - 1:-1], l[-fl - 1:-1]
        pole_h, pole_l = h[-fl - 1 - FP_POLE_BARS:-fl - 1], l[-fl - 1 - FP_POLE_BARS:-fl - 1]
        lo_i, hi_i = int(pole_l.argmin()), int(pole_h.argmax())
        if hi_i <= lo_i:
            continue
        p_lo, p_hi = float(pole_l[lo_i]), float(pole_h[hi_i])
        gain = p_hi / p_lo - 1
        if gain < FP_POLE_MIN_GAIN:
            continue
        f_hi, f_lo = float(flag_h.max()), float(flag_l.min())
        retrace = (p_hi - f_lo) / (p_hi - p_lo)
        if retrace > FP_MAX_RETRACE or f_hi > p_hi * 1.03:
            continue
        if last > f_hi:
            state = "breakout"
        elif last >= f_hi * (1 - HB_NEAR_PCT):
            state = "coiling"
        else:
            continue
        vr = _vol_ratio(v)
        sc = _score(3, gain >= 0.35, retrace <= 0.30, state == "breakout" and vr >= BREAKOUT_VOL_MULT,
                    vr >= 2.5, last > _sma(c, 50), fl >= 6)
        cand = _out("flag", state, sc, f_hi, f_lo, last, box=(fl, f_hi, f_lo), pole_gain_pct=round(100 * gain, 1),
                    retrace_pct=round(100 * retrace, 1), flag_bars=fl, vol_ratio=round(vr, 2))
        if best is None or cand["score"] > best["score"]:
            best = cand
    return best


def detect_vcp(o, h, l, c, v) -> Optional[dict]:
    W = VCP_WINDOW
    if len(c) < 3 * W + 55:
        return None
    last = float(c[-1])
    sma50, sma100 = _sma(c, 50), _sma(c, 100)
    if not (last > sma50 and sma50 > sma100):
        return None
    ranges = []
    for seg_h, seg_l in ((h[-3 * W:-2 * W], l[-3 * W:-2 * W]), (h[-2 * W:-W], l[-2 * W:-W]), (h[-W:], l[-W:])):
        ranges.append(float((seg_h.max() - seg_l.min()) / seg_h.max()))
    r1, r2, r3 = ranges
    if not (r1 > r2 > r3) or r3 > VCP_MAX_FINAL_RANGE:
        return None
    if last < VCP_NEAR_HIGH * float(h[-3 * W:].max()):
        return None
    pivot = float(h[-W - 1:-1].max())
    stop = float(l[-10:].min())
    dry = float(v[-10:].mean()) < float(v[-50:].mean())
    vr = _vol_ratio(v)
    state = "breakout" if (last > pivot and vr >= 1.2) else "coiling"
    sc = _score(3, dry, r3 <= 0.08, state == "breakout" and vr >= BREAKOUT_VOL_MULT, r2 < 0.8 * r1, sma50 > 1.05 * sma100,
                last >= 0.98 * pivot)
    return _out("vcp", state, sc, pivot, stop, last, box=(W, pivot, stop),
                contractions_pct=[round(100 * x, 1) for x in (r1, r2, r3)], vol_dry_up=dry, vol_ratio=round(vr, 2))


def detect_ipo_base(o, h, l, c, v, sessions_listed: int) -> Optional[dict]:
    if not (IPO_MIN_SESSIONS <= sessions_listed <= IPO_MAX_SESSIONS) or len(c) < IPO_MIN_SESSIONS:
        return None
    last = float(c[-1])
    top = float(h[:-1].max())
    if last < IPO_NEAR_HIGH * top:
        return None
    recent_h, recent_l = h[-10:], l[-10:]
    rng = float((recent_h.max() - recent_l.min()) / recent_h.max())
    if rng > IPO_MAX_RECENT_RANGE:
        return None
    state = "breakout" if last > top else "coiling"
    vr = _vol_ratio(v, min(20, len(v) - 1))
    sc = _score(3, rng <= 0.10, state == "breakout" and vr >= BREAKOUT_VOL_MULT, last > float(o[0]), sessions_listed >= 25,
                last >= 0.95 * top)
    return _out("ipo", state, sc, top, float(recent_l.min()), last, box=(min(10, len(c)), top, float(recent_l.min())),
                sessions_listed=sessions_listed, vol_ratio=round(vr, 2))


PATTERNS = ("ipo", "vcp", "horizontal", "flag")


def _detect_all(sym_arrays: dict, sessions_listed: int) -> dict[str, Optional[dict]]:
    o, h, l, c, v = (sym_arrays[k] for k in ("o", "h", "l", "c", "v"))
    return {
        "ipo": detect_ipo_base(o, h, l, c, v, sessions_listed),
        "vcp": detect_vcp(o, h, l, c, v),
        "horizontal": detect_horizontal(o, h, l, c, v),
        "flag": detect_flag(o, h, l, c, v),
    }


# ── panel plumbing ────────────────────────────────────────────────────────────
def _by_symbol(panel: pd.DataFrame) -> dict[str, dict]:
    """symbol -> arrays (+ dates, first-seen index within the panel)."""
    all_dates = sorted(panel["date"].unique())
    out = {}
    for sym, g in panel.groupby("symbol", sort=False):
        g = g.sort_values("date")
        out[sym] = {
            "dates": g["date"].to_numpy(), "o": g["open"].to_numpy(float), "h": g["high"].to_numpy(float),
            "l": g["low"].to_numpy(float), "c": g["close"].to_numpy(float), "v": g["volume"].to_numpy(float),
            "pc": g["prev_close"].to_numpy(float), "t": g["turnover"].to_numpy(float),
            "listed_in_window": g["date"].iloc[0] != all_dates[0] and len(g) < len(all_dates) - 5,
            "contiguous": len(g) >= len(all_dates) - 3,
        }
    return out


_FUND_NAME = re.compile(r"(ETF|BEES|LIQUID|GILT|^SGB|^NIFTY|^SENSEX|^GOLD1|^GOLDC|^SILVER1|^SILVERC)", re.I)
MIN_DAILY_VOL = 0.008                # funds/ETFs barely move; a breakout in one is meaningless


def _tradeable(a: dict, name: str = "") -> bool:
    if len(a["c"]) < IPO_MIN_SESSIONS:
        return False
    if name and _FUND_NAME.search(name):
        return False
    r = np.diff(a["c"][-61:]) / a["c"][-61:-1]
    if len(r) >= 10 and float(np.std(r)) < MIN_DAILY_VOL:
        return False
    if a["c"][-1] < MIN_PRICE:
        return False
    return float(np.median(a["t"][-20:])) >= MIN_MEDIAN_TURNOVER


def _ret(c: np.ndarray, n: int) -> float:
    return float(c[-1] / c[-n - 1] - 1) if len(c) > n and c[-n - 1] > 0 else float("nan")


# ── regime ────────────────────────────────────────────────────────────────────
def compute_regime(sym: dict[str, dict]) -> dict:
    ok = [a for s_, a in sym.items() if _tradeable(a, s_) and len(a["c"]) >= 60]
    if not ok:
        return {"label": "UNKNOWN", "breadth": {}}
    above50 = np.mean([a["c"][-1] > _sma(a["c"], 50) for a in ok])
    above100 = np.mean([a["c"][-1] > _sma(a["c"], 100) for a in ok if len(a["c"]) >= 100] or [np.nan])
    adv = sum(1 for a in ok if a["c"][-1] > a["pc"][-1])
    dec = sum(1 for a in ok if a["c"][-1] < a["pc"][-1])
    hi = sum(1 for a in ok if a["c"][-1] >= a["h"][-60:].max() * 0.999)
    lo = sum(1 for a in ok if a["c"][-1] <= a["l"][-60:].min() * 1.001)
    med20 = float(np.nanmedian([_ret(a["c"], 20) for a in ok]))
    bull = above50 >= 0.55 and hi >= lo
    bear = above50 <= 0.35 and lo > hi
    label = "BULLISH" if bull else "BEARISH" if bear else "NEUTRAL"
    posture = {
        "BULLISH": "Broad participation — breakouts have follow-through more often. Normal size, respect stops.",
        "NEUTRAL": "Mixed breadth — be selective, favour the highest-scoring setups in leading sectors, keep size small.",
        "BEARISH": "Weak breadth — most breakouts fail. Stay mostly in cash; only the strongest leaders.",
    }[label]
    return {
        "label": label, "posture": posture,
        "breadth": {
            "universe": len(ok), "pct_above_sma50": round(100 * float(above50), 1),
            "pct_above_sma100": None if math.isnan(above100) else round(100 * float(above100), 1),
            "advancers": adv, "decliners": dec, "new_60d_highs": hi, "new_60d_lows": lo,
            "median_20d_return_pct": round(100 * med20, 2),
        },
    }


# ── sectors ───────────────────────────────────────────────────────────────────
def compute_sectors(sym: dict[str, dict], industries: dict[str, str], setup_symbols: dict[str, int]) -> list[dict]:
    rows: dict[str, list] = {}
    for s, a in sym.items():
        ind = industries.get(s)
        if not ind or not _tradeable(a, s) or len(a["c"]) < 64:
            continue
        rows.setdefault(ind, []).append((s, _ret(a["c"], 21), _ret(a["c"], 63), a["c"][-1] > _sma(a["c"], 50)))
    out = []
    for ind, r in rows.items():
        if len(r) < 3:
            continue
        out.append({
            "sector": ind, "stocks": len(r),
            "ret_1m_pct": round(100 * float(np.nanmedian([x[1] for x in r])), 2),
            "ret_3m_pct": round(100 * float(np.nanmedian([x[2] for x in r])), 2),
            "pct_above_sma50": round(100 * float(np.mean([x[3] for x in r])), 1),
            "setups": sum(setup_symbols.get(x[0], 0) for x in r),
        })
    if not out:
        return []
    for key in ("ret_1m_pct", "ret_3m_pct", "pct_above_sma50"):
        vals = pd.Series([x[key] for x in out]).rank(pct=True).tolist()
        for x, p in zip(out, vals):
            x.setdefault("_p", []).append(p)
    for x in out:
        x["strength"] = round(100 * float(np.mean(x.pop("_p"))))
    return sorted(out, key=lambda x: -x["strength"])


# ── the scan ──────────────────────────────────────────────────────────────────
def scan(panel: pd.DataFrame, industries: Optional[dict[str, str]] = None) -> dict:
    if panel.empty:
        return {"error": "No EOD data cached — run the EOD sync first."}
    sym = _by_symbol(panel)
    asof = str(panel["date"].max())
    total_sessions = panel["date"].nunique()

    ok = {s: a for s, a in sym.items() if _tradeable(a, s) and a["dates"][-1] == asof}
    rs_raw = {s: _ret(a["c"], 63) for s, a in ok.items() if len(a["c"]) > 63}
    rs_pct = pd.Series(rs_raw).rank(pct=True).to_dict() if rs_raw else {}

    setups: dict[str, list] = {p: [] for p in PATTERNS}
    per_symbol_count: dict[str, int] = {}
    for s, a in ok.items():
        if not _clean(a["pc"], a["c"]):
            continue
        listed = len(a["c"]) if a["listed_in_window"] else 10**6
        found = _detect_all({k: a[k] for k in ("o", "h", "l", "c", "v")}, listed)
        for p, d in found.items():
            if d is None:
                continue
            d = {**d, "symbol": s, "rs_pct": round(100 * rs_pct.get(s, float("nan")), 0) if s in rs_pct else None,
                 "change_pct": round(100 * float(a["c"][-1] / a["pc"][-1] - 1), 2) if a["pc"][-1] else None,
                 "open": round(float(a["o"][-1]), 2), "high": round(float(a["h"][-1]), 2), "low": round(float(a["l"][-1]), 2)}
            setups[p].append(d)
            per_symbol_count[s] = per_symbol_count.get(s, 0) + 1
    for p in PATTERNS:
        setups[p].sort(key=lambda d: (-d["score"], d["state"] != "breakout", d["symbol"]))

    return {
        "asof": asof, "sessions": total_sessions, "universe": len(ok),
        "regime": compute_regime(sym),
        "sectors": compute_sectors(sym, industries or {}, per_symbol_count),
        "setups": setups,
        "counts": {p: {"total": len(v), "breakout": sum(1 for d in v if d["state"] == "breakout")} for p, v in setups.items()},
        "notes": [
            "Prices are unadjusted bhavcopy closes; symbols with a corporate-action gap in the lookback are skipped.",
            f"History is {total_sessions} sessions, so trend tests use SMA50/SMA100 and IPO bases mean listed inside this window.",
        ],
    }


def chart_data(panel: pd.DataFrame, symbol: str, sessions: int = 90) -> Optional[dict]:
    g = panel[panel["symbol"] == symbol].sort_values("date")
    if g.empty:
        return None
    c = g["close"].to_numpy(float)
    sma = lambda n: pd.Series(c).rolling(n).mean().round(2).tolist()  # noqa: E731
    tail = slice(-sessions, None)
    return {
        "symbol": symbol,
        "dates": g["date"].tolist()[tail], "open": g["open"].round(2).tolist()[tail], "high": g["high"].round(2).tolist()[tail],
        "low": g["low"].round(2).tolist()[tail], "close": g["close"].round(2).tolist()[tail],
        "volume": g["volume"].astype(int).tolist()[tail], "sma21": sma(21)[tail], "sma50": sma(50)[tail],
    }


# ── replay: does each pattern actually work? ─────────────────────────────────
def replay_stats(panel: pd.DataFrame, progress: Optional[Callable[[int, int], None]] = None) -> dict:
    """
    Run every detector "as of" each historical session, and measure the next
    FORWARD_SESSIONS sessions after each BREAKOUT: hit-rate of a +10% move,
    hit-rate of a -10% close, and the median forward return -- against the
    same numbers for every liquid stock-day (the baseline). Read-only.
    """
    sym = _by_symbol(panel)
    names = [s for s, a in sym.items() if _tradeable(a, s)]
    stats = {p: {"n": 0, "hit": 0, "dd": 0, "rets": []} for p in PATTERNS}
    base = {"n": 0, "hit": 0, "dd": 0, "rets": []}
    total = len(names)
    for i, s in enumerate(names):
        a = sym[s]
        n = len(a["c"])
        for t in range(MIN_SESSIONS, n - FORWARD_SESSIONS):
            end = t + 1
            c0 = a["c"][t]
            fwd_hi = float(a["h"][end:end + FORWARD_SESSIONS].max())
            fwd_close = float(a["c"][end + FORWARD_SESSIONS - 1])
            if not _clean(a["pc"][:end], a["c"][:end]):
                continue
            hit = fwd_hi / c0 - 1 >= FORWARD_HIT
            dd = fwd_close / c0 - 1 <= FORWARD_DRAWDOWN
            base["n"] += 1
            base["hit"] += hit
            base["dd"] += dd
            arrs = {k: a[k][:end] for k in ("o", "h", "l", "c", "v")}
            listed = end if a["listed_in_window"] else 10**6
            for p, d in _detect_all(arrs, listed).items():
                if d is not None and d["state"] == "breakout":
                    st = stats[p]
                    st["n"] += 1
                    st["hit"] += hit
                    st["dd"] += dd
                    st["rets"].append(fwd_close / c0 - 1)
        if progress and i % 100 == 0:
            progress(i, total)

    def fin(x):
        n = x["n"]
        r = x["rets"]
        return {"n": n, "hit_rate_pct": round(100 * x["hit"] / n, 1) if n else None,
                "drawdown_rate_pct": round(100 * x["dd"] / n, 1) if n else None,
                "median_fwd_return_pct": round(100 * float(np.median(r)), 2) if r else None}

    b = fin({**base, "rets": []})
    out = {"horizon_sessions": FORWARD_SESSIONS, "hit_threshold_pct": 100 * FORWARD_HIT,
           "drawdown_threshold_pct": 100 * FORWARD_DRAWDOWN, "baseline": b,
           "patterns": {}, "asof": str(panel["date"].max()), "sessions": int(panel["date"].nunique())}
    for p in PATTERNS:
        f = fin(stats[p])
        f["hit_lift"] = round(f["hit_rate_pct"] / b["hit_rate_pct"], 2) if f["hit_rate_pct"] and b["hit_rate_pct"] else None
        out["patterns"][p] = f
    return out


def save_replay(stats: dict) -> None:
    _RESULT_CACHE.parent.mkdir(parents=True, exist_ok=True)
    _RESULT_CACHE.write_text(json.dumps(stats), encoding="utf-8")


def load_replay() -> Optional[dict]:
    try:
        return json.loads(_RESULT_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return None
