"""
Open-position snapshot/restore in backend/app.py. Run directly or with pytest.

For most of this project's life only CLOSED trades were persisted, so restarting
the backend silently erased any running position -- it never recorded an exit and
left a hole in the very evidence models/strategy_registry.py's health check reads.
That is what makes the dashboard's restart button safe to press, so it is what
these tests pin:

  * an open position survives a snapshot/restore round trip
  * a closed one is not duplicated back into the live book
  * a snapshot from a PREVIOUS session is never adopted -- stale positions must
    not be resurrected into the tick monitor as if they were live
  * restoring twice does not double a position

Nothing here starts Flask or touches a broker; it drives the two functions
directly against a temporary paper_trades directory.
"""
import json
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SKIP_DB", "1")
from backend import app as backend_app  # noqa: E402


def _isolate():
    """Point the snapshot at a temp dir and give the book a clean slate."""
    tmp = Path(tempfile.mkdtemp())
    backend_app._PAPER_TRADES_DIR = tmp
    backend_app._OPEN_POSITIONS_FILE = tmp / "open_positions.json"
    for mode in backend_app.paper_positions_by_mode:
        backend_app.paper_positions_by_mode[mode] = []
    return tmp


def _position(pid, status="OPEN", symbol="NIFTY26092923250PE"):
    return {"id": pid, "symbol": symbol, "direction": "PUT", "status": status,
            "entry_premium": 121.35, "current_premium": 130.0, "unrealised_pnl": 1124.5,
            "entry_time": "09:53:31", "lot_size": 130, "mode": "test"}


def test_an_open_position_survives_a_restart():
    _isolate()
    backend_app.paper_positions_by_mode["test"].append(_position(1))
    assert backend_app._snapshot_open_positions() == 1

    backend_app.paper_positions_by_mode["test"] = []          # the restart
    backend_app._restore_open_positions()

    got = backend_app.paper_positions_by_mode["test"]
    assert len(got) == 1, "the open position did not survive the restart"
    assert got[0]["id"] == 1 and got[0]["entry_premium"] == 121.35


def test_closed_positions_are_not_snapshotted():
    """Closed trades already live in the JSONL history; re-adding them would double-count."""
    _isolate()
    backend_app.paper_positions_by_mode["test"] += [_position(1), _position(2, status="CLOSED")]
    assert backend_app._snapshot_open_positions() == 1

    backend_app.paper_positions_by_mode["test"] = []
    backend_app._restore_open_positions()
    assert [p["id"] for p in backend_app.paper_positions_by_mode["test"]] == [1]


def test_a_snapshot_from_a_previous_session_is_not_adopted():
    """A stale position must not be resurrected into the live book by a later restart."""
    tmp = _isolate()
    backend_app.paper_positions_by_mode["test"].append(_position(1))
    backend_app._snapshot_open_positions()

    stale = json.loads(backend_app._OPEN_POSITIONS_FILE.read_text(encoding="utf-8"))
    stale["session_date"] = (date.today() - timedelta(days=1)).isoformat()
    backend_app._OPEN_POSITIONS_FILE.write_text(json.dumps(stale), encoding="utf-8")

    backend_app.paper_positions_by_mode["test"] = []
    backend_app._restore_open_positions()
    assert backend_app.paper_positions_by_mode["test"] == [], "a stale position was resurrected"
    assert backend_app._OPEN_POSITIONS_FILE.exists(), "the stale file should be kept for inspection"


def test_restoring_twice_does_not_duplicate():
    _isolate()
    backend_app.paper_positions_by_mode["test"].append(_position(1))
    backend_app._snapshot_open_positions()
    backend_app.paper_positions_by_mode["test"] = []

    backend_app._restore_open_positions()
    backend_app._restore_open_positions()          # the file is consumed, so this is a no-op
    assert len(backend_app.paper_positions_by_mode["test"]) == 1


def test_restore_is_a_no_op_with_nothing_saved():
    _isolate()
    backend_app._restore_open_positions()
    assert backend_app.paper_positions_by_mode["test"] == []


def test_snapshot_file_is_consumed_after_a_successful_restore():
    """Leaving it behind would re-add the position on the NEXT restart too."""
    _isolate()
    backend_app.paper_positions_by_mode["test"].append(_position(1))
    backend_app._snapshot_open_positions()
    backend_app.paper_positions_by_mode["test"] = []
    backend_app._restore_open_positions()
    assert not backend_app._OPEN_POSITIONS_FILE.exists()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
    print("ALL PASSED")
