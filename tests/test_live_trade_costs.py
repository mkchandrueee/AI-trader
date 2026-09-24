"""
Live paper-trade costs in backend/app.py. Run directly or with pytest.

The live book used to charge a flat Rs.40 and no spread at all, so every number
the promotion gate reads was flattered -- measured on the 75 closed trades of
2026-09-24, by about Rs.32 a trade, moving profit factor 0.56 -> 0.52. These tests
pin the cost maths and, just as importantly, that a sample spanning BOTH cost
models is reported as mixed rather than averaged into a figure that describes
neither.

No Flask server, no broker.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SKIP_DB", "1")
from backend import app as backend_app  # noqa: E402
from config import measured_costs  # noqa: E402
from models.evidence_bundle import build_evidence_bundle  # noqa: E402


def test_costs_include_the_flat_commission_and_both_spread_crossings():
    entry, exit_, lot = 100.0, 120.0, 65
    rt = measured_costs.option_round_trip_pct()
    expected = backend_app.COMMISSION + (entry * rt / 2 + exit_ * rt / 2) * lot
    assert abs(backend_app._trade_costs(entry, exit_, lot) - expected) < 1e-9


def test_costs_are_strictly_higher_than_the_old_flat_commission():
    """The whole point: the old model was cheaper than reality."""
    assert backend_app._trade_costs(100.0, 120.0, 65) > backend_app.COMMISSION


def test_costs_scale_with_premium_and_lot_size():
    cheap = backend_app._trade_costs(20.0, 22.0, 65)
    rich = backend_app._trade_costs(200.0, 220.0, 65)
    bigger_lot = backend_app._trade_costs(200.0, 220.0, 130)
    assert cheap < rich < bigger_lot


def test_the_measured_spread_is_actually_being_used():
    """A fallback figure here would silently re-flatter every trade."""
    assert backend_app.SPREAD_ROUND_TRIP == measured_costs.option_round_trip_pct()
    assert backend_app.SPREAD_ROUND_TRIP > 0


def _trade(pid, pnl, entry=100.0, exit_=105.0, cost_model=None):
    t = {"id": pid, "status": "CLOSED", "realised_pnl": pnl, "entry_premium": entry,
         "exit_premium": exit_, "lot_size": 65, "entry_time": f"2026-09-24T10:0{pid}:00"}
    if cost_model:
        t["cost_model"] = cost_model
    return t


def test_untagged_trades_are_reported_as_the_old_cost_model():
    """Everything closed before the change carries no tag; it must not be assumed current."""
    b = build_evidence_bundle([_trade(1, 100.0), _trade(2, -50.0)], basis="paper")
    assert b["cost_models"] == {"flat40_no_spread": 2}
    assert b["cost_model_mixed"] is False


def test_a_sample_spanning_both_models_is_flagged_as_mixed():
    b = build_evidence_bundle(
        [_trade(1, 100.0), _trade(2, -50.0, cost_model="flat40+measured_spread")], basis="paper")
    assert b["cost_model_mixed"] is True
    assert b["cost_models"] == {"flat40_no_spread": 1, "flat40+measured_spread": 1}


def test_a_clean_sample_under_the_new_model_is_not_flagged():
    b = build_evidence_bundle(
        [_trade(1, 100.0, cost_model="flat40+measured_spread"),
         _trade(2, -50.0, cost_model="flat40+measured_spread")], basis="paper")
    assert b["cost_model_mixed"] is False


def test_persisting_a_closed_trade_stamps_the_cost_model():
    import tempfile
    from pathlib import Path
    backend_app._PAPER_TRADES_DIR = Path(tempfile.mkdtemp())
    pos = _trade(9, -1000.0)
    backend_app._persist_closed_trade(pos)
    assert pos["cost_model"] == backend_app.COST_MODEL_ID
    assert pos["costs_rupees"] == round(backend_app._trade_costs(100.0, 105.0, 65), 2)


def test_an_existing_tag_is_never_overwritten():
    """A restored or replayed trade keeps the model that actually booked it."""
    import tempfile
    from pathlib import Path
    backend_app._PAPER_TRADES_DIR = Path(tempfile.mkdtemp())
    pos = _trade(10, -1000.0, cost_model="flat40_no_spread")
    backend_app._persist_closed_trade(pos)
    assert pos["cost_model"] == "flat40_no_spread"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
    print("ALL PASSED")
