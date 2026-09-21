"""Synthetic-session checks for strategy/intraday_scanner.py. Run directly or with pytest."""
import os
import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategy import intraday_scanner as it


def session(day, closes, spread=0.0008, vol=1000.0, vols=None, opens=None):
    """75 five-minute bars from 09:15; `closes` may be shorter (a session still in progress)."""
    t0 = datetime.combine(day, datetime.strptime("09:15", "%H:%M").time())
    c = np.array(closes, float)
    o = np.array(opens, float) if opens is not None else np.concatenate([[c[0]], c[:-1]])
    h, l = np.maximum(o, c) * (1 + spread), np.minimum(o, c) * (1 - spread)
    v = np.array(vols, float) if vols is not None else np.full(len(c), vol)
    return pd.DataFrame({"timestamp": [t0 + timedelta(minutes=5 * i) for i in range(len(c))],
                         "open": o, "high": h, "low": l, "close": c, "volume": v})


def frame(*sessions):
    return pd.concat(sessions, ignore_index=True)


def ctx_for(df, upto=None):
    s = it.Series.from_df("T", df)
    k = len(s.starts) - 1
    i = len(s.c) - 1 if upto is None else s.starts[k] + upto
    return it.Ctx(s, k, i, s.starts[k])


D0, D1 = datetime(2026, 9, 17).date(), datetime(2026, 9, 18).date()
PREV = session(D0, list(np.linspace(100, 102, 75)))            # PDH ~102.2, PDL ~99.9, close 102


def test_orb_breakout_target_and_stop():
    # opening range ~100..101, then a close above with a volume spike
    closes = [100.4, 100.8, 100.6, 100.7, 100.9, 100.8, 101.6]
    vols = [1000] * 6 + [4000]
    x = ctx_for(frame(PREV, session(D1, closes, vols=vols)))
    d = it.detect_orb(x)
    assert d and d["state"] == "breakout" and d["stop"] < d["entry"] < d["target"]
    assert abs((d["target"] - d["entry"]) - (d["entry"] - d["stop"])) < 1e-6      # one opening-range height beyond
    assert d["vol_ratio"] > 1.5


def test_orb_breakdown_and_no_signal_inside_range():
    down = [100.6, 100.3, 100.5, 100.4, 100.2, 100.1, 99.3]
    d = it.detect_orb(ctx_for(frame(PREV, session(D1, down))))
    assert d and d["state"] == "breakdown" and d["target"] < d["entry"] < d["stop"]
    inside = [100.4, 100.8, 100.6, 100.7, 100.5, 100.6, 100.4]
    assert it.detect_orb(ctx_for(frame(PREV, session(D1, inside)))) is None


def test_prev_day_high_break():
    closes = [101.5, 101.8, 102.0, 102.1, 102.0, 102.3, 102.9]      # crosses PDH (~102.2) on the last bar
    d = it.detect_level(ctx_for(frame(PREV, session(D1, closes))))
    assert d and d["state"] == "breakout" and d["level"] == "PDH" and d["stop"] < d["entry"]


def test_vwap_reclaim():
    closes = [101.0, 100.6, 100.4, 100.3, 100.2, 100.15, 100.2, 100.3, 100.25, 100.4, 100.9, 101.1]
    d = it.detect_vwap(ctx_for(frame(PREV, session(D1, closes))))
    assert d and d["state"] == "breakout" and d["stop"] < d["entry"] < d["target"]


def test_box_break():
    box = [100.0 + 0.15 * ((i * 7) % 5) / 5 for i in range(18)]
    d = it.detect_box(ctx_for(frame(PREV, session(D1, [100.2] * 3 + box + [101.5]))))
    assert d and d["state"] == "breakout" and d["target"] > d["entry"] > d["stop"]


def test_forming_bar_is_never_analysed():
    df = frame(PREV, session(D1, [100.4, 100.8, 100.6, 100.7, 100.9, 100.8, 101.6]))
    now = df["timestamp"].iloc[-1] + timedelta(minutes=2)            # last bar still has 3 minutes to run
    assert len(it.completed_only(df, now)) == len(df) - 1
    assert len(it.completed_only(df, now + timedelta(minutes=3))) == len(df)


def test_no_new_setups_late_in_the_session():
    closes = [100.4, 100.8, 100.6, 100.7, 100.9, 100.8] + [100.8] * 63 + [101.9]      # last bar closes 15:05
    x = ctx_for(frame(PREV, session(D1, closes)))
    assert x.bar_end() > it.LAST_SIGNAL_TIME and it.detect_orb(x) is None


def test_scan_shapes_and_replay_counts_each_setup_once():
    days = [datetime(2026, 9, 10 + i).date() for i in range(4)]
    rng = np.random.default_rng(3)
    frames = {}
    for sym in ("AAA", "BBB", "NIFTY-I"):
        sess = []
        for d in days:
            walk = 100 + np.cumsum(rng.normal(0, 0.15, 75))
            sess.append(session(d, walk, vols=rng.integers(500, 3000, 75)))
        frames[sym] = frame(*sess)
    now = frames["AAA"]["timestamp"].iloc[-1] + timedelta(minutes=5)
    res = it.scan(frames, now, {"AAA": "X", "BBB": "X"})
    assert res["universe"] == 2 and set(res["setups"]) == set(it.PATTERNS) and res["regime"]["label"] in ("BULLISH", "BEARISH", "CHOPPY")
    rp = it.replay_stats(frames)
    for p, st in rp["patterns"].items():
        # ONE trade per pattern+direction per symbol-session, for EVERY pattern (box/vwap used to re-trade
        # ~4-5x a day because their entry price drifts; REVIEW A4)
        assert st["n"] <= 3 * 3 * 2, (p, st)  # 3 symbols x 3 replayable sessions x 2 directions
        assert st["target_first_pct"] is None or 0 <= st["target_first_pct"] <= 100


def _series_for_sim(next_open, next_high, next_low, next_close):
    """Two-session series whose LAST bar is the fill bar; the signal bar is the one before it."""
    base = [100.0] * 8 + [100.4, 100.6]
    s2 = session(D1, base + [next_close])
    s2.loc[len(s2) - 1, ["open", "high", "low", "close"]] = [next_open, next_high, next_low, next_close]
    s = it.Series.from_df("T", frame(PREV, s2))
    return s, len(s.c) - 2, len(s.c)


def test_costs_are_realistic_and_gaps_are_counted_not_dropped():
    assert it.COST_PCT + it.SLIPPAGE_PCT >= 0.0015          # ~0.15%: brokerage+STT+charges+slippage
    tot = it.COST_PCT + it.SLIPPAGE_PCT
    long_setup = {"state": "breakout", "stop": 99.5, "target": 101.5}
    s, i, end = _series_for_sim(next_open=98.0, next_high=98.5, next_low=97.5, next_close=98.0)   # gaps THROUGH the stop
    pnl, r, why = it._simulate(s, i, long_setup, end)
    assert why == "gap_stop" and r == 0.0 and abs(pnl + tot) < 1e-12
    s, i, end = _series_for_sim(next_open=102.0, next_high=102.5, next_low=101.8, next_close=102.2)  # gaps PAST the target
    pnl, r, why = it._simulate(s, i, long_setup, end)
    assert why == "gap_target" and r == 0.0 and abs(pnl + tot) < 1e-12
    s, i, end = _series_for_sim(next_open=100.6, next_high=101.6, next_low=100.5, next_close=101.5)  # a clean target hit
    pnl, r, why = it._simulate(s, i, long_setup, end)
    assert why == "target" and abs(pnl - ((101.5 - 100.6) / 100.6 - tot)) < 1e-9


def test_engine_rr_partial_is_the_rr_the_agent_takes():
    from strategy.math_decision_strategy import analyse_option_pair
    d = analyse_option_pair((127, 149, 118, 127), (88, 99, 79, 95))
    assert d.risk == 23.0 and d.rr == 0.87                   # reference-tool parity figure, unchanged
    assert d.rr_partial == round(10 / 23.0, 2) == 0.43       # exits at entry+10, so this is what is actually risked


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
    print("ALL PASSED")


