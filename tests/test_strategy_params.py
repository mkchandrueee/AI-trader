"""
Keeps config/strategy_params.py honest. Run directly or with pytest.

The schema deliberately does not own any value -- it points at the module
attribute that does. That only works if the pointers stay correct, so the tests
that matter here are: every declared pointer still resolves, every live value is
inside the bounds we claimed for it, and the risk controls are still categorised
as risk controls (nobody quietly reclassified MIN_RR as tunable).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import strategy_params as sp


def test_every_declared_param_resolves_to_a_real_attribute():
    """A renamed or deleted constant must fail here, not silently vanish from the UI."""
    unreadable = [r for r in sp.snapshot() if r["violation"] and "unreadable" in r["violation"]]
    assert not unreadable, "schema points at attributes that no longer exist: " + "; ".join(
        f"{r['key']} -> {r['module']}.{r['attr']}" for r in unreadable)


def test_live_values_are_within_declared_bounds():
    assert sp.validate() == [], "live constants outside their declared bounds: " + "; ".join(sp.validate())


def test_keys_are_unique():
    keys = [p.key for p in sp.PARAMS]
    assert len(keys) == len(set(keys)), "duplicate param keys: " + str(
        sorted({k for k in keys if keys.count(k) > 1}))


def test_bounds_are_sane():
    for p in sp.PARAMS:
        if p.lo is not None and p.hi is not None:
            assert p.lo < p.hi, f"{p.key}: lo {p.lo} is not below hi {p.hi}"


def test_every_param_is_documented_and_categorised():
    for p in sp.PARAMS:
        assert p.category in (sp.TACTICAL, sp.LOCKED, sp.MEASURED), f"{p.key}: bad category {p.category!r}"
        assert p.group, f"{p.key}: no UI group"
        assert len(p.desc) > 20, f"{p.key}: description too thin to be useful"
        assert p.unit, f"{p.key}: no unit"


def test_risk_controls_stay_locked():
    """
    The whole point of the category split. If any of these ever shows up as
    TACTICAL, something is about to auto-tune a safety net.
    """
    must_be_locked = {
        "agent.min_rr",                 # the reward:risk floor
        "agent.eod_squareoff",          # no overnight carry
        "agent.inter_call_pause_secs",  # broker rate limit
        "engine.partial_pts",           # defines the R:R that min_rr is checked against
        "positional.min_price",         # liquidity / spread sanity
        "positional.min_median_turnover",
        "positional.corp_action_gap",   # split/bonus detection, not a preference
    }
    by_key = {p.key: p for p in sp.PARAMS}
    for key in must_be_locked:
        assert key in by_key, f"{key} disappeared from the schema"
        assert by_key[key].category == sp.LOCKED, f"{key} is {by_key[key].category}, must be LOCKED"


def test_costs_are_measured_not_tactical():
    """Costs come from the charge schedule and our own recorded spreads -- never tuned."""
    for p in sp.PARAMS:
        if "cost" in p.key or "slippage" in p.key:
            assert p.category == sp.MEASURED, f"{p.key} is {p.category}, must be MEASURED"


def test_snapshot_is_json_serialisable():
    """It is served over HTTP; a datetime.time in there would 500 the route."""
    import json
    json.dumps(sp.snapshot())
    json.dumps(sp.groups())
    json.dumps(sp.counts())


def test_groups_cover_every_param_exactly_once():
    flat = [r["key"] for g in sp.groups() for r in g["params"]]
    assert sorted(flat) == sorted(p.key for p in sp.PARAMS)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
    print("ALL PASSED")
