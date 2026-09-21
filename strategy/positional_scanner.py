"""
Positional Engine -- market regime, sector leadership and chart-pattern setups
(IPO base, VCP, horizontal break, flag & pole) over the whole NSE EQ board,
computed from the local EOD store (data/eod_store.py).

The flag, cup & handle, triangle, RSI-divergence and hammer detectors follow the
rules in the user's ChartBank study PDFs (pole/flag slope, U-shaped cup with a
handle under a third of its height, converging trend lines with volume drying up,
price-vs-RSI disagreement, hammer near support); targets are the PDFs' measured
moves (pole height, cup height, widest triangle height). The horizontal, VCP and
IPO rules are our own. Neither set is a copy of any commercial screener; thresholds are module constants so they can
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
FP_MAX_FLAG_SLOPE = 0.004         # a flag may drift down or sideways; rising faster than 0.4%/bar is not a flag
VCP_WINDOW = 15                    # three consecutive 15-bar windows
VCP_MAX_FINAL_RANGE = 0.12
VCP_NEAR_HIGH = 0.95
IPO_MAX_SESSIONS = 100
IPO_MIN_SESSIONS = 15
IPO_MAX_RECENT_RANGE = 0.20
IPO_NEAR_HIGH = 0.85
BREAKOUT_VOL_MULT = 1.5
CUP_BARS = (35, 50, 65, 80)
CUP_HANDLE_BARS = (6, 10, 14, 18)
CUP_RIM_TOL = 0.08                 # ChartBank: rims roughly level, slight tilt ok
CUP_MIN_DEPTH, CUP_MAX_DEPTH = 0.12, 0.45
TRI_WINDOWS = (30, 40, 55)
TRI_FLAT = 0.0005                  # |slope| per bar (fraction of price) that still counts as a flat line
TRI_SLOPE = 0.001
HAMMER_DECLINE = 0.04              # fell at least 4% over the 10 bars into the hammer (daily)
HAMMER_NEAR_SUPPORT = 0.03         # hammer low within 3% of the 40-bar low

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


def _out(kind: str, state: str, score: int, entry: float, stop: float, c: float, *, box: Optional[tuple] = None,
         target: Optional[float] = None, **extra) -> dict:
    risk = abs(entry - stop)
    out = {
        "pattern": kind, "state": state, "score": score,
        "entry": round(entry, 2), "stop": round(stop, 2), "close": round(c, 2),
        "risk_pct": round(100 * risk / entry, 1) if entry > 0 else None,
        "box": None if box is None else [box[0], round(box[1], 2), round(box[2], 2)],  # [bars_back, top, bottom]
        **extra,
    }
    if target is not None:
        out["target"] = round(target, 2)
        out["rr"] = round(abs(target - entry) / risk, 2) if risk > 0 else None
    return out


def _pivots(x: np.ndarray, k: int, high: bool) -> list[int]:
    """Indexes that are the max (or min) of their +-k neighbourhood. Only bars with k bars after them qualify."""
    idx = []
    for i in range(k, len(x) - k):
        before, after = x[i - k:i], x[i + 1:i + k + 1]
        # strictly beyond the k bars before, at least level with the k after: a flat run yields ONE pivot (its first bar)
        if (x[i] > before.max() and x[i] >= after.max()) if high else (x[i] < before.min() and x[i] <= after.min()):
            idx.append(i)
    return idx


def _rsi(c: np.ndarray, n: int = 14) -> np.ndarray:
    """Wilder RSI, same length as c (NaN until warm)."""
    out = np.full(len(c), np.nan)
    if len(c) <= n:
        return out
    d = np.diff(c)
    up, dn = np.where(d > 0, d, 0.0), np.where(d < 0, -d, 0.0)
    au, ad = up[:n].mean(), dn[:n].mean()
    out[n] = 100.0 if ad == 0 else 100 - 100 / (1 + au / ad)
    for i in range(n, len(d)):
        au, ad = (au * (n - 1) + up[i]) / n, (ad * (n - 1) + dn[i]) / n
        out[i + 1] = 100.0 if ad == 0 else 100 - 100 / (1 + au / ad)
    return out


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
        fc = c[-fl - 1:-1]
        slope = float(np.polyfit(np.arange(len(fc)), fc, 1)[0] / fc.mean())   # per bar; ChartBank: flag slopes AGAINST the pole
        if slope > FP_MAX_FLAG_SLOPE:
            continue
        vr = _vol_ratio(v)
        sc = _score(3, gain >= 0.35, retrace <= 0.30, state == "breakout" and vr >= BREAKOUT_VOL_MULT,
                    vr >= 2.5, last > _sma(c, 50), slope <= 0)
        cand = _out("flag", state, sc, f_hi, f_lo, last, box=(fl, f_hi, f_lo), target=f_hi + (p_hi - p_lo),
                    pole_gain_pct=round(100 * gain, 1), retrace_pct=round(100 * retrace, 1), flag_bars=fl,
                    flag_slope_pct=round(100 * slope, 2), vol_ratio=round(vr, 2))
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


def detect_cup(o, h, l, c, v) -> Optional[dict]:
    """ChartBank cup & handle: U-shaped cup (not V) with roughly level rims, then a short handle that
    retraces no more than a third of the cup's height. Target = cup height added to the breakout."""
    n = len(c)
    if n < 100:
        return None
    last = float(c[-1])
    if last < 0.85 * float(h[-100:].max()):
        return None
    best = None
    for hl in CUP_HANDLE_BARS:
        for L in CUP_BARS:
            if hl > L / 3 or n < hl + 1 + L + 5:
                continue
            seg_h, seg_l, seg_v = h[-hl - 1 - L:-hl - 1], l[-hl - 1 - L:-hl - 1], v[-hl - 1 - L:-hl - 1]
            left, right = float(seg_h[:5].max()), float(seg_h[-5:].max())
            if abs(left / right - 1) > CUP_RIM_TOL:
                continue
            rim, low = max(left, right), float(seg_l.min())
            depth = (rim - low) / rim
            if not (CUP_MIN_DEPTH <= depth <= CUP_MAX_DEPTH):
                continue
            li = int(seg_l.argmin())
            if not (0.25 * L <= li <= 0.75 * L):
                continue
            if np.sum(seg_l <= low + (rim - low) / 4) < 0.4 * L:      # time spent at the bottom: rounded, not a V (a V gives ~25-30%)
                continue
            hand_h, hand_l = h[-hl - 1:-1], l[-hl - 1:-1]
            hi_h, lo_h = float(hand_h.max()), float(hand_l.min())
            if hi_h > rim * 1.03 or (rim - lo_h) > (rim - low) / 3:
                continue
            trig = max(rim, hi_h)
            if last > trig:
                state = "breakout"
            elif last >= trig * (1 - HB_NEAR_PCT):
                state = "coiling"
            else:
                continue
            vr = _vol_ratio(v)
            handle_vol_up = float(v[-hl - 1:-1].mean()) > float(seg_v.mean())
            sc = _score(3, handle_vol_up, state == "breakout" and vr >= BREAKOUT_VOL_MULT, abs(left / right - 1) <= 0.03,
                        0.15 <= depth <= 0.35, last > _sma(c, 50))
            cand = _out("cup", state, sc, trig, lo_h, last, box=(hl + L, trig, low), target=trig + (rim - low),
                        cup_depth_pct=round(100 * depth, 1), cup_bars=L, handle_bars=hl,
                        handle_vol_up=handle_vol_up, vol_ratio=round(vr, 2))
            if best is None or cand["score"] > best["score"]:
                best = cand
    return best


def detect_triangle(o, h, l, c, v) -> Optional[dict]:
    """ChartBank triangles: >=2 pivots on each converging line. Ascending (flat top, rising lows) and symmetric-in-an-
    uptrend break UP; descending and symmetric-in-a-downtrend break DOWN (reported as 'breakdown', awareness only).
    Target = widest height of the triangle from the breakout point."""
    n = len(c)
    if n < 100:
        return None
    last = float(c[-1])
    best = None
    for W in TRI_WINDOWS:
        hh, ll = h[-W - 1:-1], l[-W - 1:-1]
        ph, pl = _pivots(hh, 2, True), _pivots(ll, 2, False)
        if len(ph) < 2 or len(pl) < 2 or ph[-1] - ph[0] < W * 0.4 or pl[-1] - pl[0] < W * 0.4:
            continue
        sh, bh = np.polyfit(ph, hh[ph], 1)
        sl, bl = np.polyfit(pl, ll[pl], 1)
        mid = float((hh.mean() + ll.mean()) / 2)
        shp, slp = sh / mid, sl / mid
        w0, wT = bh - bl, (sh * W + bh) - (sl * W + bl)
        if w0 <= 0 or wT < 0.25 * w0 or wT > 0.75 * w0:      # converging, but not already past the apex
            continue
        if abs(shp) <= TRI_FLAT and slp >= TRI_SLOPE:
            kind = "ascending"
        elif abs(slp) <= TRI_FLAT and shp <= -TRI_SLOPE:
            kind = "descending"
        elif shp <= -TRI_FLAT and slp >= TRI_FLAT:
            kind = "symmetric"
        else:
            continue
        upper, lower = sh * W + bh, sl * W + bl
        prior = float(c[-W - 1] / c[-W - 41] - 1)
        bullish = kind in ("ascending", "symmetric") and prior >= 0.05
        bearish = kind == "descending" or (kind == "symmetric" and prior <= -0.05)
        half = W // 2
        dry = float(v[-W - 1:-1][half:].mean()) < float(v[-W - 1:-1][:half].mean())
        vr = _vol_ratio(v)
        touches = len(ph) + len(pl)
        if bullish:
            if last > upper:
                state = "breakout"
            elif last >= upper * (1 - HB_NEAR_PCT):
                state = "coiling"
            else:
                continue
            sc = _score(3, dry, state == "breakout" and vr >= BREAKOUT_VOL_MULT, prior >= 0.20, touches >= 5, W >= 40)
            cand = _out("triangle", state, sc, upper, lower, last, box=(W, upper, lower), target=upper + w0,
                        variant=kind, prior_trend_pct=round(100 * prior, 1), touches=touches, vol_dry_up=dry, vol_ratio=round(vr, 2))
        elif bearish and last < lower:
            sc = _score(3, dry, vr >= BREAKOUT_VOL_MULT, touches >= 5, W >= 40)
            cand = _out("triangle", "breakdown", sc, lower, upper, last, box=(W, upper, lower), target=lower - w0,
                        variant=kind, prior_trend_pct=round(100 * prior, 1), touches=touches, vol_dry_up=dry, vol_ratio=round(vr, 2))
        else:
            continue
        if best is None or cand["score"] > best["score"]:
            best = cand
    return best


def _is_hammer(o, h, l, c, i: int) -> bool:
    rng = h[i] - l[i]
    if rng <= 0:
        return False
    body = abs(c[i] - o[i])
    lower, upper = min(o[i], c[i]) - l[i], h[i] - max(o[i], c[i])
    return lower >= 0.6 * rng and upper <= 0.15 * rng and lower >= 2 * body


def detect_hammer(o, h, l, c, v, decline: float = HAMMER_DECLINE, near: float = HAMMER_NEAR_SUPPORT,
                  min_target: float = 0.03) -> Optional[dict]:
    """ChartBank hammer: a T-shaped candle after a decline, near a support zone. 'coiling' = the hammer is the latest
    (completed) bar - aggressive entry; 'breakout' = the next bar closed above the hammer's high (confirmed).
    SL just under the hammer's low; target = the nearest resistance (recent swing high), at least `min_target` (+3% on daily bars)."""
    n = len(c)
    if n < 45:
        return None
    if _is_hammer(o, h, l, c, n - 1):
        i, state = n - 1, "coiling"
    elif _is_hammer(o, h, l, c, n - 2) and c[-1] > h[n - 2]:
        i, state = n - 2, "breakout"
    else:
        return None
    if c[i - 10] / c[i] - 1 < decline or c[i] > float(c[i - 19:i + 1].mean()):     # must follow a real decline
        return None
    support = float(l[i - 40:i].min())
    if l[i] > support * (1 + near):
        return None
    last = float(c[-1])
    r = _rsi(c[-60:])
    rsi_i = float(r[len(r) - 1 - (n - 1 - i)])
    vavg = float(v[i - 20:i].mean())
    vr = float(v[i]) / vavg if vavg > 0 else 0.0
    entry, stop = float(h[i]), float(l[i]) * 0.998
    resist = float(h[-30:].max())
    target = resist if resist > entry * (1 + min_target) else entry * (1 + min_target)
    sc = _score(3, rsi_i < 30, rsi_i < 40, vr >= BREAKOUT_VOL_MULT, l[i] <= support * 1.005, state == "breakout")
    return _out("hammer", state, sc, entry, stop, last, target=target, rsi=round(rsi_i, 1), vol_ratio=round(vr, 2),
                near_support_pct=round(100 * (l[i] / support - 1), 2), bars_back=n - 1 - i)


def detect_rsi_div(o, h, l, c, v) -> Optional[dict]:
    """ChartBank RSI divergence in a clear trend: price makes a lower low while RSI makes a higher low (bullish), or a
    higher high on a lower RSI high (bearish, reported as 'breakdown' - awareness only). Bullish is 'breakout' once
    price closes above the swing high between the two lows (the trend-line-break proxy), else 'coiling'."""
    if len(c) < 70:
        return None
    oo, cc, hh, ll = o[-100:], c[-100:], h[-100:], l[-100:]
    n = len(cc)
    r = _rsi(cc)
    last = float(cc[-1])
    lows = [i for i in _pivots(ll, 3, False) if i >= n - 60 and not np.isnan(r[i])]
    if len(lows) >= 2:
        i1, i2 = lows[-2], lows[-1]
        if (i2 - i1 >= 5 and n - 1 - i2 <= 15 and ll[i2] < ll[i1] * 0.995 and r[i2] > r[i1] + 2
                and i1 >= 25 and cc[i1 - 25] > cc[i1] * 1.06):
            entry, stop = float(hh[i1:i2 + 1].max()), float(ll[i2])
            above = hh[max(0, n - 60):i1]
            above = above[above > entry * 1.01]
            target = float(above.min()) if len(above) else entry + 2 * (entry - stop)
            vr = _vol_ratio(v)
            state = "breakout" if last > entry else "coiling"
            rng = hh[i2] - ll[i2]
            hammer_like = rng > 0 and (min(oo[i2], cc[i2]) - ll[i2]) >= 0.5 * rng
            sc = _score(3, min(r[i1], r[i2]) < 35, r[i2] - r[i1] >= 8, state == "breakout" and vr >= BREAKOUT_VOL_MULT,
                        hammer_like, i2 - i1 >= 10)
            return _out("rsi", state, sc, entry, stop, last, box=(n - 1 - i1, entry, stop), target=target, variant="bullish",
                        rsi_low1=round(float(r[i1]), 1), rsi_low2=round(float(r[i2]), 1), vol_ratio=round(vr, 2))
    highs = [i for i in _pivots(hh, 3, True) if i >= n - 60 and not np.isnan(r[i])]
    if len(highs) >= 2:
        i1, i2 = highs[-2], highs[-1]
        if (i2 - i1 >= 5 and n - 1 - i2 <= 15 and hh[i2] > hh[i1] * 1.005 and r[i2] < r[i1] - 2
                and i1 >= 25 and cc[i1 - 25] * 1.06 < cc[i1]):
            entry, stop = float(ll[i1:i2 + 1].min()), float(hh[i2])
            sc = _score(3, max(r[i1], r[i2]) > 65, r[i1] - r[i2] >= 8, last < entry, i2 - i1 >= 10)
            return _out("rsi", "breakdown", sc, entry, stop, last, box=(n - 1 - i1, stop, entry), target=entry - 2 * (stop - entry),
                        variant="bearish", rsi_high1=round(float(r[i1]), 1), rsi_high2=round(float(r[i2]), 1),
                        vol_ratio=round(_vol_ratio(v), 2))
    return None


PATTERNS = ("ipo", "vcp", "horizontal", "flag", "cup", "triangle", "rsi", "hammer")


def _detect_all(sym_arrays: dict, sessions_listed: int) -> dict[str, Optional[dict]]:
    o, h, l, c, v = (sym_arrays[k] for k in ("o", "h", "l", "c", "v"))
    return {
        "ipo": detect_ipo_base(o, h, l, c, v, sessions_listed),
        "vcp": detect_vcp(o, h, l, c, v),
        "horizontal": detect_horizontal(o, h, l, c, v),
        "flag": detect_flag(o, h, l, c, v),
        "cup": detect_cup(o, h, l, c, v),
        "triangle": detect_triangle(o, h, l, c, v),
        "rsi": detect_rsi_div(o, h, l, c, v),
        "hammer": detect_hammer(o, h, l, c, v),
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
        "counts": {p: {"total": len(v), "breakout": sum(1 for d in v if d["state"] == "breakout"),
                       "breakdown": sum(1 for d in v if d["state"] == "breakdown")} for p, v in setups.items()},
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
    # NaN (a short history has no SMA50 yet) is not valid JSON -> null
    sma = lambda n: [None if x != x else x for x in pd.Series(c).rolling(n).mean().round(2).tolist()]  # noqa: E731
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
            base["rets"].append(fwd_close / c0 - 1)
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
                "median_fwd_return_pct": round(100 * float(np.median(r)), 2) if r else None,
                "mean_fwd_return_pct": round(100 * float(np.mean(r)), 3) if r else None,
                "mean_abs_move_pct": round(100 * float(np.mean(np.abs(r))), 2) if r else None}

    b = fin(base)
    base_r = np.array(base["rets"]) if base["rets"] else np.array([0.0])
    out = {"horizon_sessions": FORWARD_SESSIONS, "hit_threshold_pct": 100 * FORWARD_HIT,
           "drawdown_threshold_pct": 100 * FORWARD_DRAWDOWN, "baseline": b,
           "patterns": {}, "asof": str(panel["date"].max()), "sessions": int(panel["date"].nunique())}
    for p in PATTERNS:
        f = fin(stats[p])
        f["hit_lift"] = round(f["hit_rate_pct"] / b["hit_rate_pct"], 2) if f["hit_rate_pct"] and b["hit_rate_pct"] else None
        # DIRECTIONAL test (the hit-rate lift above is NOT one: it counts a +10% move, so it rises with volatility even if
        # the stock is as likely to fall -- e.g. flag: lift 1.6x but mean |move| 6.96% vs 5.27% and a return BELOW baseline).
        r = np.array(stats[p]["rets"]) if stats[p]["rets"] else None
        if r is not None and len(r) >= 30:
            edge = float(r.mean() - base_r.mean())
            f["vs_baseline_pct"] = round(100 * edge, 3)
            f["t_stat"] = round(edge / (float(r.std(ddof=1)) / np.sqrt(len(r))), 2)
            f["move_ratio"] = round(f["mean_abs_move_pct"] / b["mean_abs_move_pct"], 2) if b["mean_abs_move_pct"] else None
        else:
            f["vs_baseline_pct"] = f["t_stat"] = f["move_ratio"] = None
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
