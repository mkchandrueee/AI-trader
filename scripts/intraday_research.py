"""
Offline intraday research on the cached 5-minute bars (data/intraday_cache/bars/), implementing REVIEW_2026-09-21.md
items B3 (size stops/targets from measured excursions), B4 (pullback entry vs chasing the break) and C1 (judge
everything on sessions the choice never saw). It also re-checks B1 (does score / volume rank outcomes) on intraday
signals.

Method
  * Every CONFIRMED setup (breakout long / breakdown short) is taken once per pattern+direction per symbol-session,
    using the exact live detectors as-of the signal bar (strategy/intraday_scanner.py).
  * For each signal two price paths are recorded, for up to SIM_MAX_BARS bars:
      chase     fill at the next bar's open
      pullback  limit at the retest level (broken trigger, or VWAP for the vwap pattern), waited for up to
                RETEST_WAIT bars; no touch = no trade (the miss is counted, not hidden)
  * Any (stop %, target %) pair can then be replayed bar by bar with the stop winning a same-bar tie.
  * Sessions are split by DATE: the best pair is chosen on the earlier sessions only and reported on the later ones.
    The number of configurations tried is printed, because the best of N is optimistic by construction.

    python scripts/intraday_research.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.intraday_bars_store import load_all  # noqa: E402
from strategy import intraday_scanner as isc  # noqa: E402

COST = isc.COST_PCT + isc.SLIPPAGE_PCT
MAXB = isc.SIM_MAX_BARS
RETEST_WAIT = 6
TOUCH_TOL = 0.0003
STOPS = (0.002, 0.003, 0.004, 0.005, 0.007, 0.010)
TARGETS = (0.003, 0.005, 0.007, 0.010, 0.015)
DEFAULT_CFG = (0.005, 0.007)          # a plausible untuned pair, to compare the tuned pick against
TRAIN_FRACTION = 0.6
MIN_TRAIN_TRADES = 20


def _path(s: isc.Series, j0: int, end: int, fill: float, long: bool, fill_bar_conservative: bool = False):
    """Favourable / adverse excursion per bar (fractions of `fill`) from bar j0, plus the final close excursion."""
    j1 = min(end, j0 + MAXB)
    h, l, c = s.h[j0:j1], s.l[j0:j1], s.c[j0:j1]
    if long:
        fav, adv, close = (h - fill) / fill, (l - fill) / fill, (c - fill) / fill
    else:
        fav, adv, close = (fill - l) / fill, (fill - h) / fill, (fill - c) / fill
    if fill_bar_conservative and len(fav):
        # A limit fills at an unknown moment inside its bar: count the bar's full adverse range but only the part
        # of the favourable move still visible at the close.
        fav = fav.copy()
        fav[0] = max(0.0, float(close[0]))
    return fav, adv, float(close[-1]) if len(close) else 0.0


def _retest_fill(s: isc.Series, i: int, end: int, level: float, long: bool):
    for j in range(i + 1, min(end, i + 1 + RETEST_WAIT)):
        if long and s.l[j] <= level * (1 + TOUCH_TOL):
            return j, min(level, float(s.o[j])) if s.o[j] > level else float(s.o[j])
        if not long and s.h[j] >= level * (1 - TOUCH_TOL):
            return j, max(level, float(s.o[j])) if s.o[j] < level else float(s.o[j])
    return None


_SERIES: dict[str, isc.Series] = {}


def collect() -> list[dict]:
    frames = {k: v for k, v in load_all().items() if k != "NIFTY_I"}
    recs: list[dict] = []
    for sym, df in frames.items():
        day = df["timestamp"].dt.date
        df = df[df.groupby(day)["timestamp"].transform("count") >= 70]      # complete sessions only
        s = isc.Series.from_df(sym, df)
        if s is None:
            continue
        _SERIES[sym] = s
        for k in range(len(s.starts)):
            a, b = s.starts[k], s.ends[k]
            seen: set = set()
            for i in range(a + isc.OR_BARS, b - 1):
                x = isc.Ctx(s, k, i, a)
                for p, d in isc.detect_all(x).items():
                    if d is None or d["state"] == "coiling" or (p, d["state"]) in seen:
                        continue
                    seen.add((p, d["state"]))
                    long = d["state"] == "breakout"
                    fill = float(s.o[i + 1])
                    rec = {"sym": sym, "day": s.days[k], "pattern": p, "long": long, "score": d["score"],
                           "vol": d.get("vol_ratio") or 0.0, "chase": _path(s, i + 1, b, fill, long), "pullback": None}
                    level = float(s.vwap[i]) if p == "vwap" else float(d["entry"])
                    hit = _retest_fill(s, i, b, level, long)
                    if hit is not None:
                        j, pf = hit
                        rec["pullback"] = _path(s, j, b, pf, long, fill_bar_conservative=True)
                    recs.append(rec)
    return recs


def sim(path, stop: float, target: float) -> float:
    """Net return of one trade on a recorded path, as a fraction of the fill."""
    fav, adv, close = path
    for f, a in zip(fav, adv):
        if a <= -stop:
            return -stop - COST                  # the stop wins a same-bar tie
        if f >= target:
            return target - COST
    return close - COST


def evaluate(recs: list[dict], kind: str, cfg) -> tuple[int, float, float]:
    """(trades, mean net %, win %) for one entry kind and one (stop, target) pair."""
    r = [sim(x[kind], *cfg) for x in recs if x[kind] is not None]
    if not r:
        return 0, float("nan"), float("nan")
    a = np.array(r)
    return len(a), 100 * a.mean(), 100 * (a > 0).mean()


def _random_bar_drift(sym: str, long: bool, rng: np.random.Generator) -> float:
    """Forward drift of a RANDOM bar of a random session of the same stock, traded in the same direction."""
    s = _SERIES[sym]
    k = int(rng.integers(len(s.starts)))
    a, b = s.starts[k], s.ends[k]
    hi = b - MAXB - 2
    i = int(rng.integers(a + isc.OR_BARS, hi)) if hi > a + isc.OR_BARS else a + isc.OR_BARS
    return _path(s, i + 1, b, float(s.o[i + 1]), long)[2]


def directional_check(recs: list[dict]) -> None:
    """
    Do the signals point anywhere BEFORE costs?  Compares each pattern's mean forward drift (in the signal's own
    direction) with a same-stock random-bar baseline. This is the test the hit-rate 'lift' cannot do: a lift of
    1.6-2.4x on '+0.5% within 30 min' appeared while the drift was zero or negative, because breakouts simply
    happen when a stock is moving and moves are frequent in BOTH directions.
    NB signals are clustered by day (a whole market up/down day hits many stocks), so |t| overstates significance.
    """
    rng = np.random.default_rng(7)
    print("=== DIRECTIONAL CHECK  (gross of costs, chase entry, drift over the whole hold window) ===")
    print(f"{'pattern':8} {'dir':6} {'n':>5} {'drift%':>8} {'t':>6} {'random-bar%':>12}")
    for p in isc.PATTERNS:
        for lg, lbl in ((True, "long"), (False, "short")):
            sub = [r for r in recs if r["pattern"] == p and r["long"] == lg]
            if len(sub) < 30:
                continue
            d = np.array([r["chase"][2] for r in sub]) * 100
            base = np.array([_random_bar_drift(r["sym"], lg, rng) for r in sub]) * 100
            t = d.mean() / (d.std(ddof=1) / np.sqrt(len(d)))
            print(f"{p:8} {lbl:6} {len(d):>5} {d.mean():>+8.3f} {t:>+6.2f} {base.mean():>+12.3f}")
    print("  A pattern has directional edge only if drift is clearly ABOVE the random-bar column AND above the"
          f" {100 * COST:.3f}% cost.\n")


def main() -> None:
    recs = collect()
    if not recs:
        print("No cached bars -- run scripts/export_intraday_bars.py or let the backend sync first.")
        return
    days = sorted({r["day"] for r in recs})
    cut = days[int(len(days) * TRAIN_FRACTION) - 1]
    train = [r for r in recs if r["day"] <= cut]
    test = [r for r in recs if r["day"] > cut]
    n_cfg = len(STOPS) * len(TARGETS)
    print(f"{len(recs)} signals over {len(days)} sessions ({days[0]} .. {days[-1]}); cost {100 * COST:.3f}% round trip")
    print(f"TRAIN sessions <= {cut}: {len(train)} signals   TEST sessions after: {len(test)} signals")
    print(f"configurations tried per pattern/entry: {n_cfg}  (the best of {n_cfg} on train is optimistic by construction)\n")

    directional_check(recs)

    # ---- B3: measured excursions on the chase path ----------------------------------------------------------------
    print("=== B3  how far do these signals actually run?  (chase entry, all sessions, % of fill) ===")
    print(f"{'pattern':8} {'n':>5} {'MFE p50':>8} {'MFE p60':>8} {'MFE p75':>8} {'MAE p50':>8} {'MAE p75':>8}")
    for p in isc.PATTERNS:
        sub = [r for r in recs if r["pattern"] == p]
        if len(sub) < 15:
            print(f"{p:8} {len(sub):>5}  (too few)")
            continue
        mfe = np.array([100 * r["chase"][0].max() for r in sub if len(r["chase"][0])])
        mae = np.array([-100 * r["chase"][1].min() for r in sub if len(r["chase"][1])])
        print(f"{p:8} {len(sub):>5} {np.percentile(mfe, 50):>8.2f} {np.percentile(mfe, 60):>8.2f} {np.percentile(mfe, 75):>8.2f} "
              f"{np.percentile(mae, 50):>8.2f} {np.percentile(mae, 75):>8.2f}")
    print("  (MFE = best favourable move within the window; MAE = worst adverse move)\n")

    # ---- B3 + B4 + C1: tuned exits, chase vs pullback, out of sample -----------------------------------------------
    print("=== B3/B4/C1  stop & target chosen on TRAIN, reported on TEST  (net of costs) ===")
    print(f"{'pattern':8} {'entry':9} {'train pick (stop/tgt)':>22} {'train net%':>10} {'TEST n':>7} {'TEST net%':>10} {'TEST win%':>10} "
          f"{'default TEST net%':>18} {'missed':>7}")
    summary = []
    for p in isc.PATTERNS:
        tr = [r for r in train if r["pattern"] == p]
        te = [r for r in test if r["pattern"] == p]
        for kind in ("chase", "pullback"):
            best = None
            for cfg in ((s, t) for s in STOPS for t in TARGETS):
                n, m, _ = evaluate(tr, kind, cfg)
                if n >= MIN_TRAIN_TRADES and (best is None or m > best[1]):
                    best = (cfg, m, n)
            if best is None:
                print(f"{p:8} {kind:9} {'(< ' + str(MIN_TRAIN_TRADES) + ' train trades)':>22}")
                continue
            cfg, tm, _ = best
            n, m, w = evaluate(te, kind, cfg)
            dn, dm, _ = evaluate(te, kind, DEFAULT_CFG)
            missed = "" if kind == "chase" else f"{100 * sum(1 for r in te if r['pullback'] is None) / max(1, len(te)):.0f}%"
            print(f"{p:8} {kind:9} {f'{100 * cfg[0]:.1f}% / {100 * cfg[1]:.1f}%':>22} {tm:>10.3f} {n:>7} {m:>10.3f} {w:>10.1f} "
                  f"{dm:>18.3f} {missed:>7}")
            summary.append((p, kind, tm, m, n))
    print("\n  A pick 'works' only if TEST net% is positive and close to train net%. A large gap is overfitting.\n")

    # ---- overfit check across ALL configurations --------------------------------------------------------------------
    tr_m, te_m = [], []
    for p in isc.PATTERNS:
        for kind in ("chase", "pullback"):
            for cfg in ((s, t) for s in STOPS for t in TARGETS):
                a = evaluate([r for r in train if r["pattern"] == p], kind, cfg)
                b = evaluate([r for r in test if r["pattern"] == p], kind, cfg)
                if a[0] >= MIN_TRAIN_TRADES and b[0] >= 10:
                    tr_m.append(a[1]); te_m.append(b[1])
    if len(tr_m) > 10:
        corr = float(np.corrcoef(tr_m, te_m)[0, 1])
        print(f"train->test correlation of net% across {len(tr_m)} pattern/entry/config cells: {corr:+.2f} "
              f"(near 0 = what looked good on train did not carry over)")
        print(f"cells profitable on train: {100 * np.mean(np.array(tr_m) > 0):.0f}%   on test: {100 * np.mean(np.array(te_m) > 0):.0f}%\n")

    # ---- B1: do score and volume rank outcomes? ---------------------------------------------------------------------
    print("=== B1  do score / volume rank outcomes?  (chase entry, +-0.5% first touch, all sessions) ===")
    print(f"{'bucket':16} {'n':>5} {'win%':>6} {'net%':>7}")

    def first_touch(r):
        fav, adv, close = r["chase"]
        for f, a in zip(fav, adv):
            if a <= -0.005:
                return -0.005 - COST
            if f >= 0.005:
                return 0.005 - COST
        return close - COST

    for lbl, pred in (("score <= 5", lambda r: r["score"] <= 5), ("score 6-7", lambda r: 6 <= r["score"] <= 7),
                      ("score >= 8", lambda r: r["score"] >= 8),
                      ("volume < 1.0x", lambda r: r["vol"] < 1.0), ("volume 1.0-1.5x", lambda r: 1.0 <= r["vol"] < 1.5),
                      ("volume >= 1.5x", lambda r: r["vol"] >= 1.5)):
        sub = [first_touch(r) for r in recs if pred(r)]
        if len(sub) >= 15:
            a = np.array(sub)
            print(f"{lbl:16} {len(a):>5} {100 * (a > 0).mean():>6.1f} {100 * a.mean():>7.3f}")
        else:
            print(f"{lbl:16} {len(sub):>5}  (too few)")


if __name__ == "__main__":
    main()
