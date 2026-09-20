"""
Pins strategy/math_decision_strategy.py's Options Analyzer breakdown and Value
Calculator to the numbers shown in the reference app's release video
(mathpro.netlify.app "Trading Toolkit", recorded 2026-09-18). Run directly
(`python tests/test_analyzer_value_calc.py`) or with pytest.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategy.math_decision_strategy import (
    analyse_option_pair, analyzer_breakdown, pullback_entry, value_calculator,
)

# Video sample: CALL O127 H149 L118 C127, PUT O88 H99 L79 C95.
CALL = (127, 149, 118, 127)
PUT = (88, 99, 79, 95)


def _levels(ladder):
    return [t["level"] for t in ladder["targets"]]


def test_ladders_match_video():
    a = analyzer_breakdown(CALL, PUT)
    cl, pl = a["call_ladder"], a["put_ladder"]
    assert (cl["entry"], _levels(cl), cl["stop_loss"]) == (127.63, [152.43, 177.23, 214.43], 108.70)
    assert [t["pts"] for t in cl["targets"]] == [25, 50, 87] and cl["stop_pts"] == 19
    assert (pl["entry"], _levels(pl), pl["stop_loss"]) == (95.47, [111.47, 127.47, 151.47], 73.00)
    assert [t["pts"] for t in pl["targets"]] == [16, 32, 56] and pl["stop_pts"] == 22
    assert [(t["action"], t["pct"]) for t in cl["targets"]] == [("BOOK", 40), ("BOOK", 40), ("HOLD", 20)]


def test_ladders_match_sensex_screenshot():
    # Telegram screenshot: call entry 520.59, T1 604.59, T2 688.59, T3 814.59, SL 466.50
    a = analyzer_breakdown((515, 603, 498, 518), (400, 455, 377, 432))
    cl, pl = a["call_ladder"], a["put_ladder"]
    assert (cl["entry"], _levels(cl), cl["stop_loss"]) == (520.59, [604.59, 688.59, 814.59], 466.50)
    assert (pl["entry"], _levels(pl)) == (434.16, [496.56, 558.96, 652.56])


def test_checklist_and_strength_match_video():
    a = analyzer_breakdown(CALL, PUT)
    states = {r["key"]: (r["state"], r["value"]) for r in a["checklist"]}
    assert states["closed"] == ("pass", "CONFIRMED")
    assert states["call_direction"] == ("pass", "BULLISH")   # flat 127->127 counts as bullish in the reference
    assert states["put_direction"] == ("pass", "BULLISH")
    assert states["call_body"] == ("fail", "0.00%")
    assert states["put_body"] == ("fail", "35.00%")
    assert states["call_close_pos"] == ("fail", "29.03%")
    assert states["pcr"] == ("pass", "0.75 → Bullish")
    assert (a["strength"]["call_pct"], a["strength"]["put_pct"]) == (15, 54)


def test_engine_still_matches_decision_helper_v2():
    d = analyse_option_pair(CALL, PUT)
    assert (d.side, d.entry, d.partial, d.target, d.stop) == ("put", 102.0, 112.0, 122.0, 79)
    assert (d.risk, d.rr, d.confidence, d.tier) == (23.0, 0.87, 77, "strong")


def test_value_calculator_matches_video():
    r = value_calculator([127.63, 152.43, 177.23, 95.47, 111.47, 127.47])
    assert (r["average"], r["sqrt_of_avg"], r["call_level"]) == (131.95, 11.49, 120.46)
    assert (r["call_target"], r["put_level"], r["put_target"]) == (150.58, 54.21, 92.15)


def test_value_calculator_rejects_bad_input():
    for bad in ([1, 2, 3], [1, 2, 3, 4, 5, 0], [1, 2, 3, 4, 5, float("nan")], [1, 2, 3, 4, 5, -1]):
        try:
            value_calculator(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted bad input {bad}")


# Samples 2-4 from the later reference videos: (call OHLC, put OHLC).
S2 = ((95, 113, 49, 51), (19, 64, 19, 57))
S3 = ((232, 258, 202, 249), (90, 102, 69, 72))
S4 = ((70, 77, 48, 52), (30, 49, 30, 46))


def _verdict(sample):
    a = analyzer_breakdown(*sample)
    return a, a["verdict"], a["scores"]


def test_analyzer_scores_and_verdicts_match_videos():
    a, v, sc = _verdict((CALL, PUT))
    assert (sc["call"], sc["put"], sc["margin"]) == (40, 55, 15)
    assert (v["signal"], v["headline"], v["confidence"], v["side"]) == ("NEUTRAL — WAIT", "NO — WAIT", 55, None)
    for name, sample, side, conf, scores in (
        ("S2", S2, "put", 80, (25, 80)), ("S3", S3, "call", 75, (75, 13)), ("S4", S4, "put", 80, (25, 80)),
    ):
        _, v, sc = _verdict(sample)
        assert (sc["call"], sc["put"]) == scores, name
        assert (v["side"], v["confidence"]) == (side, conf), name
        assert v["headline"] == f"YES — BUY {side.upper()}", name


def test_reason_lines_match_videos():
    assert _verdict(S2)[1]["reason"] == "Put candle: Bullish ✓ | Body: 84.44% ✓ | Close position: 84.44% of range ✓ | PCR: 1.12 (Bearish) ✓"
    assert _verdict(S3)[1]["reason"] == "Call candle: Bullish ✓ | Body: 30.36% ⚠ | Close position: 83.93% of range ✓ | PCR: 0.29 (Bullish) ✓"


def test_body_check_is_tri_state():
    states = {r["key"]: r["state"] for r in analyzer_breakdown(*S3)["checklist"]}
    assert states["put_body"] == "warn"      # 54.55% sits in the 45-60% band
    assert states["call_body"] == "fail"


def test_pullback_matches_videos():
    pb = pullback_entry("call", S3[0])
    assert pb["zones"] == {"zone1": 216.0, "zone2": 223.3, "zone3": 230.0}
    assert (pb["stop_loss"], pb["stop_pts"], pb["rr"]) == (199, 24.3, 2.5)
    assert [t["level"] for t in pb["targets"]] == [259.8, 284.1, 320.5]
    assert pb["cautions"] == ["Body weak — momentum is slow"]
    pb = pullback_entry("put", S4[1])
    assert pb["zones"] == {"zone1": 34.8, "zone2": 37.2, "zone3": 39.5}
    assert (pb["stop_loss"], pb["stop_pts"], pb["rr"]) == (27, 10.2, 4.9)
    assert [t["level"] for t in pb["targets"]] == [67.2, 87.2, 117.2]
    assert pullback_entry(None, None)["wait"] is True


def test_value_calculator_sample_4():
    r = value_calculator([52.26, 75.46, 98.66, 46.23, 62.23, 78.23])
    assert (r["average"], r["sqrt_of_avg"], r["call_level"]) == (68.85, 8.30, 60.55)
    assert (r["call_target"], r["put_level"], r["put_target"]) == (75.68, 27.25, 46.32)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
    print("ALL PASSED")
