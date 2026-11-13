"""Pressure observability -> guardian/logs/pressure.jsonl."""

from __future__ import annotations

import json

import pytest

from guardian import pressure_observability as po


@pytest.fixture
def log_dir(tmp_path, monkeypatch):
    import guardian.data_logger as dl

    monkeypatch.setattr(dl, "_LOG_DIR", tmp_path)
    dl._file_locks.clear()
    return tmp_path


def test_neuromod_apply_before_after_logged(log_dir):
    class C:
        def __init__(self, t, w):
            self.source = "autonomous"
            self.type = t
            self.weight = w
            self.metadata = {}

    nm = {"dopamine": 0.2, "serotonin": 0.1, "stress": 0.0, "spike": 0.0}
    po.log_neuromod_apply_before(nm, [C("curiosity_gap", 0.5)])
    po.log_neuromod_apply_after(nm, [C("curiosity_gap", 0.55)], gamma=1.1, patience=0.9)
    rows = [
        json.loads(line)
        for line in (log_dir / "pressure.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert rows[0]["event"] == "neuromod_apply_before"
    assert rows[0]["data"]["weights_before"]["curiosity_gap"] == 0.5
    assert rows[1]["event"] == "neuromod_apply_after"
    assert rows[1]["data"]["gamma"] == 1.1


def test_tonic_transition_logged(log_dir):
    before = {"dopamine": 0.5, "serotonin": 0.0, "stress": 0.0, "spike": 0.0}
    after = {"dopamine": 0.25, "serotonin": 0.0, "stress": 0.0, "spike": 0.0}
    po.log_neuromod_tonic_transition("idle_tick_decay", before, after)
    row = json.loads((log_dir / "pressure.jsonl").read_text(encoding="utf-8").strip())
    assert row["event"] == "neuromod_tonic_transition"
    assert row["data"]["hook"] == "idle_tick_decay"
    assert row["data"]["delta"]["dopamine"] == -0.25


def test_decay_neuromod_tonic_writes_guardian_log(log_dir, monkeypatch):
    from pressure_loop import pressure_field as pf

    pf._NEUROMOD_TONIC.update({"dopamine": 1.0, "serotonin": 0.0, "stress": 0.0})
    monkeypatch.setattr(
        "pressure_loop.policy.pressure_neuromod_tonic_decay",
        lambda: {"enabled": True, "decay_rate": 0.5, "baseline": 0.0},
    )
    pf.decay_neuromod_tonic()
    rows = [
        json.loads(line)
        for line in (log_dir / "pressure.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert rows[-1]["event"] == "neuromod_tonic_transition"
    assert rows[-1]["data"]["hook"] == "idle_tick_decay"
