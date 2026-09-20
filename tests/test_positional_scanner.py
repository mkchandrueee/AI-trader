"""Synthetic-series checks for strategy/positional_scanner.py's detectors.
Run directly (`python tests/test_positional_scanner.py`) or with pytest."""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategy.positional_scanner import (
    _clean, _rsi, detect_cup, detect_flag, detect_hammer, detect_horizontal, detect_ipo_base, detect_rsi_div,
    detect_triangle, detect_vcp,
)


def _bars(closes, spread=0.01, vol=1000.0):
    c = np.array(closes, float)
    o = np.concatenate([[c[0]], c[:-1]])
    h = np.maximum(o, c) * (1 + spread)
    l = np.minimum(o, c) * (1 - spread)
    return o, h, l, c, np.full(len(c), vol)


def test_horizontal_breakout_and_coiling():
    base = [100 + 3 * np.sin(i / 2) for i in range(80)] + [100] * 5
    o, h, l, c, v = _bars(base + [111.0])                # closes above the ~104 ceiling
    v[-1] = 3000.0
    d = detect_horizontal(o, h, l, c, v)
    assert d and d["state"] == "breakout" and d["entry"] < 111 and d["stop"] < d["entry"]
    o, h, l, c, v = _bars(base + [max(base)])
    d = detect_horizontal(o, h, l, c, v)
    assert d is None or d["state"] == "coiling"
    o, h, l, c, v = _bars(list(np.linspace(50, 150, 90)))   # a straight trend is not a box
    assert detect_horizontal(o, h, l, c, v) is None


def test_flag_and_pole():
    pre = [100.0] * 60
    pole = list(np.linspace(100, 140, 10))
    flag = [138, 136, 137, 135, 136, 137, 136, 138]
    o, h, l, c, v = _bars(pre + pole + flag + [143.0])
    d = detect_flag(o, h, l, c, v)
    assert d and d["state"] == "breakout" and d["pole_gain_pct"] >= 25 and d["retrace_pct"] <= 50
    o, h, l, c, v = _bars(pre + list(np.linspace(100, 110, 10)) + [109] * 8 + [112.0])  # only a 10% "pole"
    assert detect_flag(o, h, l, c, v) is None


def test_vcp_needs_contracting_ranges():
    up = list(np.linspace(60, 100, 80))
    wide = [100 + 12 * np.sin(i / 1.2) for i in range(15)]
    mid = [100 + 6 * np.sin(i / 1.2) for i in range(15)]
    tight = [100 + 2 * np.sin(i / 1.2) for i in range(14)] + [101.5]
    o, h, l, c, v = _bars(up + wide + mid + tight)
    d = detect_vcp(o, h, l, c, v)
    assert d is None or d["contractions_pct"][0] > d["contractions_pct"][1] > d["contractions_pct"][2]
    flat = [100 + 6 * np.sin(i / 1.2) for i in range(45)]
    o, h, l, c, v = _bars(up + flat)
    assert detect_vcp(o, h, l, c, v) is None             # equal ranges must not pass the r1>r2>r3 test


def test_ipo_window_gates():
    o, h, l, c, v = _bars(list(np.linspace(100, 120, 20)) + [120] * 10 + [123.0])
    assert detect_ipo_base(o, h, l, c, v, sessions_listed=31) is not None
    assert detect_ipo_base(o, h, l, c, v, sessions_listed=10**6) is None   # not a recent listing


def test_corporate_action_gap_is_skipped():
    c = np.array([100.0] * 30 + [50.0] * 30)
    pc = np.concatenate([[100.0], c[:-1]])
    pc[30] = 50.0                      # NSE adjusts prev_close on the ex-date; yesterday's close is still 100
    assert _clean(pc, c) is False
    assert _clean(np.concatenate([[100.0], np.full(59, 100.0)]), np.full(60, 100.0)) is True


def test_flag_target_is_pole_height_and_rising_flag_rejected():
    pre = [100.0] * 60
    pole = list(np.linspace(100, 140, 10))
    flag = [138, 136, 137, 135, 136, 137, 136, 138]
    d = detect_flag(*_bars(pre + pole + flag + [143.0]))
    assert d and abs(d["target"] - (d["entry"] + 40 * 1.02)) < 6      # pole ~ low 99 -> high ~141.4
    rising = list(np.linspace(130, 139, 8))                            # a flag climbing on, not against, the pole
    assert detect_flag(*_bars(pre + pole + rising + [143.0])) is None


def test_cup_and_handle():
    pre = [80.0] * 45
    cup = [100 - 25 * np.sin(np.pi * i / 59) ** 0.5 * 1 for i in range(60)]   # rounded bowl, rims ~100, low ~75
    handle = [98, 96.5, 95.5, 96, 97, 96.5, 97.5, 98]
    d = detect_cup(*_bars(pre + cup + handle + [103.0], vol=1000.0))
    assert d and d["state"] == "breakout" and d["target"] > d["entry"] and d["stop"] < d["entry"]
    vee = [100 - 25 * (1 - abs(i - 30) / 30) for i in range(60)]            # V-shaped: rejected
    assert detect_cup(*_bars(pre + vee + handle + [103.0])) is None


def test_triangle_ascending_breakout_and_target():
    prior = list(np.linspace(70, 100, 60))
    tri = []
    for i in range(40):                                   # flat 110 ceiling, floor rising 95 -> 101
        ph, floor = i % 10, 95 + 0.15 * i
        tri.append(110.0 if ph == 0 else floor if ph == 5 else (110.0 + floor) / 2)
    d = detect_triangle(*_bars(prior + tri + [114.0], spread=0.002))
    assert d and d["variant"] == "ascending" and d["state"] == "breakout"
    assert d["target"] > d["entry"] > d["stop"]
    down = list(np.linspace(140, 100, 60))                # same shape after a DOWNtrend is not a bullish setup
    d2 = detect_triangle(*_bars(down + tri + [114.0], spread=0.002))
    assert d2 is None or d2["state"] != "breakout"


def test_rsi_is_bounded_and_hammer_needs_decline():
    r = _rsi(np.cumsum(np.random.default_rng(1).normal(0, 1, 200)) + 100)
    assert np.nanmin(r) >= 0 and np.nanmax(r) <= 100
    down = list(np.linspace(130, 100, 50))
    o, h, l, c, v = _bars(down + [100.0])
    o[-1], c[-1], h[-1], l[-1] = 99.6, 100.0, 100.05, 96.0                # long lower wick, small body at the top
    d = detect_hammer(o, h, l, c, v)
    assert d and d["state"] == "coiling" and d["stop"] < 96.0 and d["target"] >= d["entry"] * 1.03
    flat = [100.0 + 0.1 * (i % 2) for i in range(50)]
    o, h, l, c, v = _bars(flat + [100.0])
    o[-1], c[-1], h[-1], l[-1] = 99.6, 100.0, 100.05, 96.0
    assert detect_hammer(o, h, l, c, v) is None                            # no decline -> not a reversal hammer


def test_bullish_rsi_divergence():
    seg = (list(np.linspace(150, 130, 30)) + list(np.linspace(130, 100, 10)) + list(np.linspace(100, 112, 10))
           + list(np.linspace(112, 99.0, 22)) + list(np.linspace(99, 100.5, 6)) + [100.0] * 3)
    d = detect_rsi_div(*_bars(seg, spread=0.002))
    assert d and d["variant"] == "bullish" and d["rsi_low2"] > d["rsi_low1"] and d["stop"] < d["entry"]
    sideways = [100 + 2 * np.sin(i / 3) for i in range(90)]  # no clear trend -> no divergence signal
    assert detect_rsi_div(*_bars(sideways)) is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
    print("ALL PASSED")
