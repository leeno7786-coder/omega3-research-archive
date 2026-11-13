"""Release regression: the shared state snapshot is structurally independent."""
from core.shared_state import SharedState


def test_snapshot_is_deeply_independent():
    state = SharedState()
    state.write("_report", {"events": [{"ok": True}]})
    report = state.snapshot()
    report["_report"]["events"][0]["ok"] = False
    report["_report"]["events"].append({"id": 2})
    assert state.read("_report") == {"events": [{"ok": True}]}


def test_snapshot_preserves_value_type():
    state = SharedState()
    state.write("_a", ("x", {"value": [1, 2]}))
    assert state.snapshot() == {"_a": ("x", {"value": [1, 2]})}
