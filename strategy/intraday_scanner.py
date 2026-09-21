"""
Intraday Engine -- opening-range break, VWAP reclaim/loss, previous-day-level
break, consolidation break and the ChartBank 30-minute hammer, plus market
regime and sector leadership, over a liquid universe (NIFTY 50 + NIFTY futures)
from COMPLETED 5-minute bars only.

Same design rules as strategy/positional_scanner.py:
  * every detector is a pure function of numpy arrays "as of" a bar, so
    `replay_stats` can run the exact live code over history and MEASURE each
    pattern's outcomes (target-before-stop, R, hit-rate lift vs a same-universe
    baseline) instead of assuming it works;
  * a candle still forming is never analysed (callers pass completed bars);
  * thresholds are module constants.

Long trigger  -> state "breakout" (confirmed) / "coiling" (just under it);
short trigger -> state "breakdown". No new setups after LAST_SIGNAL_TIME.
The opening-range, VWAP, level and box rules are our own definitions; the
hammer follows the user's ChartBank intraday PDF (T-shaped candle near
support, SL under its low, next resistance as target).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import time as dtime, timedelta
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pandas as pd

from strategy import positional_scanner as ps
from utils.logger import get_logger

logger = get_logger("intraday_scanner")

BAR_MIN = 5
SESSION_OPEN = dtime(9, 15)
OR_BARS = 3                        # 09:15-09:30 opening range
LAST_SIGNAL_TIME = dtime(15, 0)    # a bar closing after this is too late for a fresh setup
OR_MIN_RANGE, OR_MAX_RANGE = 0.0015, 0.025
OR_FRESH_BARS = 12                 # a break older than an hour is history, not a setup
NEAR_TRIGGER = 0.0015              # within 0.15% under the trigger = coiling
BOX_BARS = 12
BOX_MAX_DEPTH = 0.006
LEVEL_FRESH_BARS = 6
VWAP_LOOKBACK = 8
VWAP_MIN_OTHER_SIDE = 5
HAMMER_DECLINE = 0.006
HAMMER_NEAR_SUPPORT = 0.003
HAMMER_MIN_TARGET = 0.004
BREAKOUT_VOL_MULT = 1.5

FORWARD_BARS = 6                   # replay hit-rate window: 30 minutes
FORWARD_HIT = 0.005                # +0.5% within it
SIM_MAX_BARS = 24                  # trade replay: at most 2 hours
# Round-trip friction for NSE intraday equity: brokerage ~0.06% + STT 0.025% (sell side) + exchange/GST/stamp ~0.02%
# = ~0.105%, plus slippage: a breakout is bought as the bar has already moved, so >=0.05% is the floor. The first
# version assumed 0.05% in total, ~3x too low (REVIEW_2026-09-21.md A3).
COST_PCT = 0.00105
SLIPPAGE_PCT = 0.0005

PATTERNS = ("orb", "vwap", "level", "box", "hammer")

_REPLAY_CACHE = Path(__file__).resolve().parent.parent / "data" / "intraday_cache" / "_replay_stats.json"


# ── per-symbol series with per-session precomputation ─────────────────────────
@dataclass
class Series:
    """All bars for one symbol (naive-IST, ascending) + what every session needs, computed once."""
    symbol: str
    t: np.ndarray
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    v: np.ndarray
    starts: list = field(default_factory=list)     # index of each session's first bar
    ends: list = field(default_factory=list)       # one past each session's last bar
    days: list = field(default_factory=list)
    vwap: np.ndarray = None                        # session-anchored VWAP for every bar
    prev: list = field(default_factory=list)       # per session: (pdh, pdl, pdc) or None
    tod: list = field(default_factory=list)        # per session: {minute-of-day: mean volume in EARLIER sessions}
    b30: dict = None                               # completed 30-minute bars (+ the 5-min index each completes on)

    @staticmethod
    def from_df(symbol: str, df: pd.DataFrame) -> Optional["Series"]:
        if df is None or df.empty:
            return None
        df = df.sort_values("timestamp").reset_index(drop=True)
        ts = pd.to_datetime(df["timestamp"])
        s = Series(symbol, ts.to_numpy("datetime64[m]"), *(df[k].to_numpy(float) for k in ("open", "high", "low", "close", "volume")))
        day = ts.dt.date.to_numpy()
        s.vwap = np.zeros(len(df))
        vol_by_tod: dict = {}
        prev = None
        for d in pd.unique(day):
            idx = np.flatnonzero(day == d)
            a, b = int(idx[0]), int(idx[-1]) + 1
            s.starts.append(a)
            s.ends.append(b)
            s.days.append(d)
            tp = (s.h[a:b] + s.l[a:b] + s.c[a:b]) / 3
            cv = np.cumsum(s.v[a:b])
            s.vwap[a:b] = np.where(cv > 0, np.cumsum(tp * s.v[a:b]) / np.where(cv > 0, cv, 1), np.cumsum(tp) / np.arange(1, b - a + 1))
            s.prev.append(prev)
            s.tod.append({m: float(np.mean(x)) for m, x in vol_by_tod.items()})
            prev = (float(s.h[a:b].max()), float(s.l[a:b].min()), float(s.c[b - 1]))
            for j in range(a, b):
                m = int(ts.iloc[j].hour * 60 + ts.iloc[j].minute)
                vol_by_tod.setdefault(m, []).append(float(s.v[j]))
        s._build_30m(ts, day)
        return s

    def _build_30m(self, ts: pd.Series, day) -> None:
        """Completed 30-minute bars anchored at 09:15; `done_at[k]` = 5-min index that completes bar k."""
        anchor = (ts - pd.Timedelta(minutes=15)).dt.floor("30min") + pd.Timedelta(minutes=15)
        key = anchor.to_numpy("datetime64[m]")
        o, h, l, c, v, done = [], [], [], [], [], []
        i = 0
        n = len(key)
        while i < n:
            j = i
            while j + 1 < n and key[j + 1] == key[i]:
                j += 1
            if j - i + 1 == 30 // BAR_MIN:              # only a fully printed bucket counts
                o.append(self.o[i]); h.append(self.h[i:j + 1].max()); l.append(self.l[i:j + 1].min())
                c.append(self.c[j]); v.append(self.v[i:j + 1].sum()); done.append(j)
            i = j + 1
        self.b30 = {"o": np.array(o), "h": np.array(h), "l": np.array(l), "c": np.array(c), "v": np.array(v),
                    "done": np.array(done, dtype=int), "t": key}

    def session_of(self, i: int) -> int:
        for k in range(len(self.starts) - 1, -1, -1):
            if self.starts[k] <= i:
                return k
        return 0


@dataclass
class Ctx:
    s: Series
    k: int          # session index
    i: int          # global index of the evaluated (last completed) bar
    a: int          # session start index

    @property
    def n(self) -> int:
        return self.i - self.a + 1

    def arr(self, name: str) -> np.ndarray:
        return getattr(self.s, name)[self.a:self.i + 1]

    @property
    def price(self) -> float:
        return float(self.s.c[self.i])

    @property
    def vwap(self) -> np.ndarray:
        return self.s.vwap[self.a:self.i + 1]

    @property
    def prev(self):
        return self.s.prev[self.k]

    def bar_end(self) -> dtime:
        ts = pd.Timestamp(self.s.t[self.i]) + timedelta(minutes=BAR_MIN)
        return ts.time()

    def vol_ratio(self, j: Optional[int] = None) -> float:
        """Bar volume vs the SAME time-of-day average of earlier sessions (rolling 20 bars if none)."""
        j = self.i if j is None else j
        ts = pd.Timestamp(self.s.t[j])
        m = ts.hour * 60 + ts.minute
        base = self.s.tod[self.k].get(m)
        if base and base > 0:
            return float(self.s.v[j]) / base
        lo = max(0, j - 20)
        roll = float(self.s.v[lo:j].mean()) if j > lo else 0.0
        return float(self.s.v[j]) / roll if roll > 0 else 0.0


def _too_late(x: Ctx) -> bool:
    return x.bar_end() > LAST_SIGNAL_TIME


def _mk(x: Ctx, kind: str, state: str, score: int, entry: float, stop: float, target: float, *, box=None, **extra) -> dict:
    risk = abs(entry - stop)
    return {
        "pattern": kind, "state": state, "score": score,
        "entry": round(entry, 2), "stop": round(stop, 2), "target": round(target, 2), "close": round(x.price, 2),
        "risk_pct": round(100 * risk / entry, 2) if entry > 0 else None,
        "rr": round(abs(target - entry) / risk, 2) if risk > 0 else None,
        "box": None if box is None else [box[0], round(box[1], 2), round(box[2], 2)],
        "vol_ratio": round(x.vol_ratio(), 2), **extra,
    }


def _day_change(x: Ctx) -> Optional[float]:
    p = x.prev
    return (x.price / p[2] - 1) if p and p[2] else None


# ── detectors ────────────────────────────────────────────────────────────────
def detect_orb(x: Ctx) -> Optional[dict]:
    """Opening-range break: 09:15-09:30 high/low. Target = one opening-range height beyond the break."""
    if x.n <= OR_BARS or _too_late(x):
        return None
    h, l, c = x.arr("h"), x.arr("l"), x.arr("c")
    hi, lo = float(h[:OR_BARS].max()), float(l[:OR_BARS].min())
    rng = hi - lo
    if not (OR_MIN_RANGE <= rng / hi <= OR_MAX_RANGE):
        return None
    last = float(c[-1])
    above = np.flatnonzero(c[OR_BARS:] > hi)
    below = np.flatnonzero(c[OR_BARS:] < lo)
    dchg, vw = _day_change(x), float(x.vwap[-1])
    vr = x.vol_ratio()
    if last > hi and len(above) and x.n - 1 - (OR_BARS + above[0]) <= OR_FRESH_BARS:
        sc = ps._score(3, vr >= BREAKOUT_VOL_MULT, last > vw, bool(dchg and dchg > 0.003), bool(x.prev and last > x.prev[0]),
                       rng / hi <= 0.008, last <= hi * 1.006)
        return _mk(x, "orb", "breakout", sc, hi, lo, hi + rng, box=(x.n - 1, hi, lo), or_high=round(hi, 2), or_low=round(lo, 2),
                   extended_pct=round(100 * (last / hi - 1), 2))
    if last < lo and len(below) and x.n - 1 - (OR_BARS + below[0]) <= OR_FRESH_BARS:
        sc = ps._score(3, vr >= BREAKOUT_VOL_MULT, last < vw, bool(dchg and dchg < -0.003), bool(x.prev and last < x.prev[1]),
                       rng / hi <= 0.008, last >= lo * 0.994)
        return _mk(x, "orb", "breakdown", sc, lo, hi, lo - rng, box=(x.n - 1, hi, lo), or_high=round(hi, 2), or_low=round(lo, 2),
                   extended_pct=round(100 * (last / lo - 1), 2))
    if hi * (1 - NEAR_TRIGGER) <= last <= hi:
        sc = ps._score(3, last > vw, bool(dchg and dchg > 0.003), rng / hi <= 0.008)
        return _mk(x, "orb", "coiling", sc, hi, lo, hi + rng, box=(x.n - 1, hi, lo), or_high=round(hi, 2), or_low=round(lo, 2))
    return None


def detect_vwap(x: Ctx) -> Optional[dict]:
    """VWAP reclaim (long) / loss (short): most of the last 8 bars on the other side, then a decisive cross in the last 3."""
    if x.n < VWAP_LOOKBACK + 3 or _too_late(x):
        return None
    c, h, l, vw = x.arr("c"), x.arr("h"), x.arr("l"), x.vwap
    look = slice(-(VWAP_LOOKBACK + 3), -3)
    below = int(np.sum(c[look] < vw[look]))
    above = int(np.sum(c[look] > vw[look]))
    vr = x.vol_ratio()
    up_cross = bool(c[-1] > vw[-1] and np.any(c[-3:-1] <= vw[-3:-1]))     # was at/below VWAP within the last 2 bars, now above
    dn_cross = bool(c[-1] < vw[-1] and np.any(c[-3:-1] >= vw[-3:-1]))
    if below >= VWAP_MIN_OTHER_SIDE and up_cross:
        stop = float(l[-6:].min())
        entry = float(h[-3:].max())
        if entry <= stop:
            return None
        sc = ps._score(3, vr >= 1.2, vr >= BREAKOUT_VOL_MULT, bool(x.prev and c[-1] > x.prev[2]), float(c[-1]) > float(c[-6:].mean()),
                       below >= 6)
        return _mk(x, "vwap", "breakout", sc, entry, stop, entry + 1.5 * (entry - stop), vwap=round(float(vw[-1]), 2), bars_below=below)
    if above >= VWAP_MIN_OTHER_SIDE and dn_cross:
        stop = float(h[-6:].max())
        entry = float(l[-3:].min())
        if stop <= entry:
            return None
        sc = ps._score(3, vr >= 1.2, vr >= BREAKOUT_VOL_MULT, bool(x.prev and c[-1] < x.prev[2]), float(c[-1]) < float(c[-6:].mean()),
                       above >= 6)
        return _mk(x, "vwap", "breakdown", sc, entry, stop, entry - 1.5 * (stop - entry), vwap=round(float(vw[-1]), 2), bars_above=above)
    return None


def detect_level(x: Ctx) -> Optional[dict]:
    """Break of the previous session's high (long) or low (short). Stop = the 6-bar swing, target = 1.5x risk."""
    if not x.prev or x.n < 3 or _too_late(x):
        return None
    pdh, pdl, _ = x.prev
    c, h, l = x.arr("c"), x.arr("h"), x.arr("l")
    last = float(c[-1])
    vr, vw = x.vol_ratio(), float(x.vwap[-1])
    n = len(c)
    over = np.flatnonzero(c > pdh)
    under = np.flatnonzero(c < pdl)
    if last > pdh and len(over) and n - 1 - over[0] <= LEVEL_FRESH_BARS:
        stop = min(float(l[-6:].min()), pdh * 0.996)
        sc = ps._score(3, vr >= BREAKOUT_VOL_MULT, last > vw, float(c[0]) < pdh, last <= pdh * 1.005, n - 1 - over[0] <= 2)
        return _mk(x, "level", "breakout", sc, pdh, stop, pdh + 1.5 * (pdh - stop), level="PDH", level_price=round(pdh, 2),
                   extended_pct=round(100 * (last / pdh - 1), 2))
    if last < pdl and len(under) and n - 1 - under[0] <= LEVEL_FRESH_BARS:
        stop = max(float(h[-6:].max()), pdl * 1.004)
        sc = ps._score(3, vr >= BREAKOUT_VOL_MULT, last < vw, float(c[0]) > pdl, last >= pdl * 0.995, n - 1 - under[0] <= 2)
        return _mk(x, "level", "breakdown", sc, pdl, stop, pdl - 1.5 * (stop - pdl), level="PDL", level_price=round(pdl, 2),
                   extended_pct=round(100 * (last / pdl - 1), 2))
    if pdh * (1 - NEAR_TRIGGER) <= last <= pdh:
        stop = min(float(l[-6:].min()), pdh * 0.996)
        return _mk(x, "level", "coiling", ps._score(3, last > vw, vr >= 1.0), pdh, stop, pdh + 1.5 * (pdh - stop), level="PDH",
                   level_price=round(pdh, 2))
    return None


def detect_box(x: Ctx) -> Optional[dict]:
    """A tight 12-bar (1 hour) consolidation with a flat top touched twice. Target = the box's height beyond the break."""
    if x.n < BOX_BARS + 3 or _too_late(x):
        return None
    h, l, c, v = x.arr("h"), x.arr("l"), x.arr("c"), x.arr("v")
    bh, bl = float(h[-BOX_BARS - 1:-1].max()), float(l[-BOX_BARS - 1:-1].min())
    depth = (bh - bl) / bh
    if depth > BOX_MAX_DEPTH or int(np.sum(h[-BOX_BARS - 1:-1] >= bh * 0.9985)) < 2:
        return None
    last = float(c[-1])
    vw, vr = float(x.vwap[-1]), x.vol_ratio()
    half = BOX_BARS // 2
    dry = float(v[-BOX_BARS - 1:-1][half:].mean()) < float(v[-BOX_BARS - 1:-1][:half].mean())
    height = bh - bl
    if last > bh:
        sc = ps._score(3, dry, vr >= BREAKOUT_VOL_MULT, last > vw, depth <= 0.004, last <= bh * 1.004)
        return _mk(x, "box", "breakout", sc, bh, bl, bh + height, box=(BOX_BARS, bh, bl), depth_pct=round(100 * depth, 2), vol_dry_up=dry)
    if last < bl:
        sc = ps._score(3, dry, vr >= BREAKOUT_VOL_MULT, last < vw, depth <= 0.004, last >= bl * 0.996)
        return _mk(x, "box", "breakdown", sc, bl, bh, bl - height, box=(BOX_BARS, bh, bl), depth_pct=round(100 * depth, 2), vol_dry_up=dry)
    if last >= bh * (1 - NEAR_TRIGGER):
        return _mk(x, "box", "coiling", ps._score(3, dry, last > vw), bh, bl, bh + height, box=(BOX_BARS, bh, bl),
                   depth_pct=round(100 * depth, 2), vol_dry_up=dry)
    return None


def detect_hammer30(x: Ctx) -> Optional[dict]:
    """ChartBank 30-minute hammer (coiling = just completed, breakout = next 30-min bar closed above it)."""
    b = x.s.b30
    m = int(np.searchsorted(b["done"], x.i, side="right"))     # 30-min bars completed by this 5-min bar
    if m < 45 or _too_late(x):
        return None
    d = ps.detect_hammer(b["o"][:m], b["h"][:m], b["l"][:m], b["c"][:m], b["v"][:m],
                         decline=HAMMER_DECLINE, near=HAMMER_NEAR_SUPPORT, min_target=HAMMER_MIN_TARGET)
    if not d or d["state"] not in ("coiling", "breakout"):
        return None
    if (x.i - int(b["done"][m - 1])) > 3:                      # hammer read is stale once 20+ minutes past its bucket
        return None
    return {**{k: d[k] for k in ("pattern", "state", "score", "entry", "stop", "target", "risk_pct", "rr")}, "pattern": "hammer",
            "close": round(x.price, 2), "box": None, "vol_ratio": d.get("vol_ratio"), "rsi": d.get("rsi"),
            "near_support_pct": d.get("near_support_pct"), "timeframe": "30-min"}


DETECTORS: dict[str, Callable[[Ctx], Optional[dict]]] = {
    "orb": detect_orb, "vwap": detect_vwap, "level": detect_level, "box": detect_box, "hammer": detect_hammer30,
}


def detect_all(x: Ctx) -> dict[str, Optional[dict]]:
    return {p: fn(x) for p, fn in DETECTORS.items()}


# ── live scan ────────────────────────────────────────────────────────────────
def completed_only(df: pd.DataFrame, now) -> pd.DataFrame:
    """Drop the bar still forming: a bar counts only once its 5 minutes are over."""
    if df is None or df.empty:
        return df
    return df[df["timestamp"] + pd.Timedelta(minutes=BAR_MIN) <= pd.Timestamp(now)].reset_index(drop=True)


def _row_summary(x: Ctx) -> dict:
    c, vw = x.arr("c"), x.vwap
    dchg = _day_change(x)
    return {
        "close": round(x.price, 2), "vwap": round(float(vw[-1]), 2), "above_vwap": bool(c[-1] > vw[-1]),
        "day_change_pct": None if dchg is None else round(100 * dchg, 2),
        "bar_time": str(pd.Timestamp(x.s.t[x.i])),
    }


def compute_regime(rows: dict[str, dict], index_row: Optional[dict], setups: dict[str, list]) -> dict:
    vals = list(rows.values())
    if not vals:
        return {"label": "UNKNOWN", "breadth": {}}
    above = float(np.mean([r["above_vwap"] for r in vals]))
    chg = [r["day_change_pct"] for r in vals if r["day_change_pct"] is not None]
    adv = sum(1 for x in chg if x > 0)
    dec = sum(1 for x in chg if x < 0)
    med = float(np.median(chg)) if chg else 0.0
    up_setups = sum(1 for s in setups.get("orb", []) if s["state"] == "breakout")
    dn_setups = sum(1 for s in setups.get("orb", []) if s["state"] == "breakdown")
    idx_up = index_row["above_vwap"] if index_row else None
    bull = above >= 0.6 and (idx_up is None or idx_up) and med >= 0
    bear = above <= 0.4 and (idx_up is None or not idx_up) and med <= 0
    label = "BULLISH" if bull else "BEARISH" if bear else "CHOPPY"
    posture = {
        "BULLISH": "Most of the universe is holding above VWAP with the index — favour long breaks, keep stops at the trigger's other side.",
        "BEARISH": "Most of the universe is under VWAP with the index — favour short breaks and VWAP-loss setups; long breaks are more likely to fail.",
        "CHOPPY": "Mixed: the index and the universe disagree. Breaks are more likely to reverse — size down, wait for volume-confirmed breaks only.",
    }[label]
    return {
        "label": label, "posture": posture,
        "index": index_row,
        "breadth": {"universe": len(vals), "pct_above_vwap": round(100 * above, 1), "advancers": adv, "decliners": dec,
                    "median_day_change_pct": round(med, 2), "orb_breakouts": up_setups, "orb_breakdowns": dn_setups},
    }


def compute_sectors(rows: dict[str, dict], industries: dict[str, str], per_symbol_setups: dict[str, int]) -> list[dict]:
    groups: dict[str, list] = {}
    for s, r in rows.items():
        ind = industries.get(s)
        if ind and r["day_change_pct"] is not None:
            groups.setdefault(ind, []).append((s, r))
    out = []
    for ind, items in groups.items():
        if len(items) < 2:
            continue
        out.append({
            "sector": ind, "stocks": len(items),
            "day_change_pct": round(float(np.median([r["day_change_pct"] for _, r in items])), 2),
            "pct_above_vwap": round(100 * float(np.mean([r["above_vwap"] for _, r in items])), 1),
            "setups": sum(per_symbol_setups.get(s, 0) for s, _ in items),
        })
    if not out:
        return []
    for key in ("day_change_pct", "pct_above_vwap"):
        pr = pd.Series([o[key] for o in out]).rank(pct=True).tolist()
        for o, p in zip(out, pr):
            o.setdefault("_p", []).append(p)
    for o in out:
        o["strength"] = round(100 * float(np.mean(o.pop("_p"))))
    return sorted(out, key=lambda o: -o["strength"])


def scan(frames: dict[str, pd.DataFrame], now, industries: Optional[dict[str, str]] = None, index_symbol: str = "NIFTY-I") -> dict:
    """
    `frames`: symbol -> ALL bars (previous sessions + today's, completed or not). The last session in each frame is
    the "current" session; incomplete bars are dropped here so no caller can leak one in.
    """
    setups: dict[str, list] = {p: [] for p in PATTERNS}
    rows: dict[str, dict] = {}
    per_sym: dict[str, int] = {}
    index_row = None
    last_bars = []
    built = []
    for sym, df in frames.items():
        s = Series.from_df(sym, completed_only(df, now))
        if s is not None:
            built.append((sym, s))
    # A symbol whose newest bar is behind the freshest one (a previous session, or more than one bar back) is STALE:
    # analysing it as "current" would mix yesterday's setups into today's scan. Report it, never scan it.
    # The reference is the freshest STOCK bar. The index series must not set it: NIFTY futures bars run to 15:40 while
    # stocks end at 15:25, which made every stock look 3 bars stale after the close (found on the first restart).
    freshest = max((pd.Timestamp(s.t[-1]) for sym, s in built if sym != index_symbol), default=None)
    stale: list[str] = []
    for sym, s in built:
        if freshest is not None and pd.Timestamp(s.t[-1]) < freshest - pd.Timedelta(minutes=BAR_MIN):
            stale.append(sym)
            continue
        k = len(s.starts) - 1
        x = Ctx(s, k, len(s.c) - 1, s.starts[k])
        row = _row_summary(x)
        if sym == index_symbol:
            index_row = row
            continue
        rows[sym] = row
        last_bars.append(row["bar_time"])
        for p, d in detect_all(x).items():
            if d is None:
                continue
            setups[p].append({**d, "symbol": sym, "day_change_pct": row["day_change_pct"], "above_vwap": row["above_vwap"]})
            per_sym[sym] = per_sym.get(sym, 0) + 1
    order = {"breakout": 0, "breakdown": 1, "coiling": 2}
    for p in PATTERNS:
        setups[p].sort(key=lambda d: (-d["score"], order[d["state"]], d["symbol"]))
    asof = max(last_bars) if last_bars else None
    return {
        "asof_bar": asof, "universe": len(rows), "stale": sorted(stale),
        "regime": compute_regime(rows, index_row, setups),
        "sectors": compute_sectors(rows, industries or {}, per_sym),
        "setups": setups,
        "counts": {p: {"total": len(v), "breakout": sum(1 for d in v if d["state"] == "breakout"),
                       "breakdown": sum(1 for d in v if d["state"] == "breakdown")} for p, v in setups.items()},
        "movers": sorted(({"symbol": s, **r} for s, r in rows.items() if r["day_change_pct"] is not None),
                         key=lambda r: -abs(r["day_change_pct"]))[:10],
    }


def chart_data(frame: pd.DataFrame, now, sessions: int = 2) -> Optional[dict]:
    """Completed 5-min bars of the last `sessions` sessions + session VWAP, for the setup charts."""
    s = Series.from_df("x", completed_only(frame, now))
    if s is None:
        return None
    first = s.starts[max(0, len(s.starts) - sessions)]
    sl = slice(first, None)
    ts = pd.to_datetime(s.t[sl])
    return {"times": [t.strftime("%d %b %H:%M") for t in ts], "open": s.o[sl].round(2).tolist(), "high": s.h[sl].round(2).tolist(),
            "low": s.l[sl].round(2).tolist(), "close": s.c[sl].round(2).tolist(), "volume": s.v[sl].astype(int).tolist(),
            "vwap": s.vwap[sl].round(2).tolist(),
            "session_starts": [int(a - first) for a in s.starts if a >= first]}


# ── replay: does each pattern actually work? ────────────────────────────────
def _simulate(s: Series, i: int, d: dict, end: int) -> Optional[tuple]:
    """Trade the setup from the NEXT bar's open. Returns (pnl_pct, r_multiple, exit_reason). Same-bar tie -> stop first."""
    j0 = i + 1
    if j0 >= end:
        return None
    long = d["state"] in ("breakout", "coiling")
    fill, stop, target = float(s.o[j0]), d["stop"], d["target"]
    # A gap through the target or the stop at the fill bar is a real outcome, not a non-event: you would have entered
    # at the open and paid costs for ~0 gross. Dropping these (the first version did) removed the gap-through-stop
    # losers and so flattered every pattern (REVIEW C4). They are traded at the open, exit at the open, and counted.
    if (long and fill >= target) or (not long and fill <= target):
        return -(COST_PCT + SLIPPAGE_PCT), 0.0, "gap_target"
    if (long and fill <= stop) or (not long and fill >= stop):
        return -(COST_PCT + SLIPPAGE_PCT), 0.0, "gap_stop"
    risk = abs(fill - stop)
    exit_px, reason = float(s.c[min(end - 1, i + SIM_MAX_BARS)]), "timeout"
    for j in range(j0, min(end, i + 1 + SIM_MAX_BARS)):
        lo, hi = float(s.l[j]), float(s.h[j])
        if long:
            if lo <= stop:
                exit_px, reason = min(stop, float(s.o[j])), "stop"
                break
            if hi >= target:
                exit_px, reason = target, "target"
                break
        else:
            if hi >= stop:
                exit_px, reason = max(stop, float(s.o[j])), "stop"
                break
            if lo <= target:
                exit_px, reason = target, "target"
                break
    sign = 1.0 if long else -1.0
    return sign * (exit_px - fill) / fill - (COST_PCT + SLIPPAGE_PCT), sign * (exit_px - fill) / risk, reason


def replay_stats(frames: dict[str, pd.DataFrame], max_sessions: int = 12, progress: Optional[Callable[[int, int], None]] = None) -> dict:
    """
    Run every detector "as of" each completed bar of each earlier session and trade each CONFIRMED setup
    (breakout long / breakdown short) from the next bar with its own stop and target. Reports per pattern:
    trades, win %, avg R, target-before-stop %, average net P&L %, and the 30-minute +0.5% hit-rate lift versus
    the same-universe baseline for that direction. Read-only.
    """
    acc = {p: {"pnl": [], "r": [], "tgt": 0, "stp": 0, "gap_t": 0, "gap_s": 0, "hit": 0, "n": 0, "n_long": 0, "days": set()} for p in PATTERNS}
    base = {"long": {"n": 0, "hit": 0}, "short": {"n": 0, "hit": 0}}
    names = [s for s in frames if frames[s] is not None and not frames[s].empty]
    for si, sym in enumerate(names):
        s = Series.from_df(sym, frames[sym])
        if s is None or len(s.starts) < 2:
            continue
        ks = range(max(1, len(s.starts) - max_sessions), len(s.starts))
        for k in ks:
            a, b = s.starts[k], s.ends[k]
            if b - a < OR_BARS + FORWARD_BARS + 3:
                continue
            seen: set = set()          # a setup stays "fresh" for several bars -- trade it once, on the first bar it confirms
            for i in range(a + OR_BARS, b - 1):
                fwd_hi = float(s.h[i + 1:min(b, i + 1 + FORWARD_BARS)].max())
                fwd_lo = float(s.l[i + 1:min(b, i + 1 + FORWARD_BARS)].min())
                c0 = float(s.c[i])
                base["long"]["n"] += 1
                base["long"]["hit"] += fwd_hi / c0 - 1 >= FORWARD_HIT
                base["short"]["n"] += 1
                base["short"]["hit"] += 1 - fwd_lo / c0 >= FORWARD_HIT
                x = Ctx(s, k, i, a)
                for p, d in detect_all(x).items():
                    if d is None or d["state"] == "coiling":
                        continue
                    # ONE trade per pattern+direction per symbol-session. Keying on the entry price only worked for
                    # orb/level (fixed trigger); box/vwap entries drift every bar so they re-traded the same setup
                    # ~4-5x a day and inflated their samples ~5x (REVIEW A4).
                    key = (p, d["state"])
                    if key in seen:
                        continue
                    seen.add(key)
                    sim = _simulate(s, i, d, b)
                    if sim is None:
                        continue
                    pnl, r, why = sim
                    st = acc[p]
                    st["n"] += 1
                    st["pnl"].append(pnl)
                    st["r"].append(r)
                    st["tgt"] += why == "target"
                    st["stp"] += why == "stop"
                    st["gap_t"] += why == "gap_target"
                    st["gap_s"] += why == "gap_stop"
                    st["days"].add(str(s.days[k]))
                    lg = d["state"] == "breakout"
                    st["n_long"] += lg
                    st["hit"] += (fwd_hi / c0 - 1 >= FORWARD_HIT) if lg else (1 - fwd_lo / c0 >= FORWARD_HIT)
        if progress and si % 10 == 0:
            progress(si, len(names))

    def pct(x, n):
        return round(100 * x / n, 1) if n else None

    out = {"sessions": max_sessions, "forward_bars": FORWARD_BARS, "hit_threshold_pct": 100 * FORWARD_HIT, "cost_pct": round(100 * (COST_PCT + SLIPPAGE_PCT), 3),
           "sim_max_bars": SIM_MAX_BARS,
           "baseline": {"long_hit_rate_pct": pct(base["long"]["hit"], base["long"]["n"]), "short_hit_rate_pct": pct(base["short"]["hit"], base["short"]["n"]),
                        "bars": base["long"]["n"]},
           "patterns": {}}
    bl, bs = out["baseline"]["long_hit_rate_pct"], out["baseline"]["short_hit_rate_pct"]
    for p in PATTERNS:
        st = acc[p]
        n = st["n"]
        pnl = np.array(st["pnl"]) if n else np.array([0.0])
        rr = np.array(st["r"]) if n else np.array([0.0])
        hit = pct(st["hit"], n)
        ref = ((st["n_long"] * bl + (n - st["n_long"]) * bs) / n) if n and bl is not None and bs is not None else None
        out["patterns"][p] = {
            "n": n, "days": len(st["days"]), "win_rate_pct": pct(int((pnl > 0).sum()), n) if n else None,
            "avg_r": round(float(rr.mean()), 2) if n else None, "avg_pnl_pct": round(100 * float(pnl.mean()), 3) if n else None,
            # before costs: separates "no directional edge" (this is ~0) from "an edge that costs eat" (this is >0).
            # 2026-09-21 research: every pattern's gross drift was zero or slightly negative.
            "avg_gross_pct": round(100 * (float(pnl.mean()) + COST_PCT + SLIPPAGE_PCT), 3) if n else None,
            "target_first_pct": pct(st["tgt"], n), "stop_first_pct": pct(st["stp"], n),
            "gap_past_target": st["gap_t"], "gap_past_stop": st["gap_s"],
            "hit_rate_pct": hit, "hit_lift": round(hit / ref, 2) if hit and ref else None,
        }
    return out


def save_replay(stats: dict, asof: str) -> None:
    _REPLAY_CACHE.parent.mkdir(parents=True, exist_ok=True)
    _REPLAY_CACHE.write_text(json.dumps({**stats, "asof": asof}), encoding="utf-8")


def load_replay() -> Optional[dict]:
    try:
        return json.loads(_REPLAY_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return None
