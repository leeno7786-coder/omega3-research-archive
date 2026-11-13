from dataclasses import replace
import json

import pytest

from pressure_loop import cognition_queue as queue
from pressure_loop.contributors import EpisodeIgnition, make_contributor


@pytest.fixture
def state(tmp_path, monkeypatch):
    from core import pipeline_state
    monkeypatch.setattr("core.paths.GUARDIAN_STATE_ROOT", tmp_path)
    monkeypatch.setattr(queue, "_queue_path", lambda: tmp_path / "cognition_queue.json")
    monkeypatch.setattr(pipeline_state, "_episode_in_journal", lambda episode_id: False)
    queue._reset_cache_for_tests()
    pipeline_state.reset_for_tests()
    yield pipeline_state
    queue._reset_cache_for_tests()
    pipeline_state.reset_for_tests()


def ready(state):
    c = make_contributor("operator", "operator_query", 1, {"cycle_id": "cycle-exact", "job_id": "job-exact", "arrival_seq": 27})
    queue.enqueue_input(c, operator_activity=False)
    c = queue.lease_next_input(0)
    ignition = EpisodeIgnition("contest", 1, c.type, c, (c,), None, pipeline_id=0, work_kind="operator_turn")
    state.mark_ready(0, ignition)
    return c, ignition


def test_ready_restart_preserves_exact_identity_once(state):
    c, _ = ready(state)
    queue.acknowledge_input(c, 0)
    queue._reset_cache_for_tests()
    state.reset_for_tests()
    envelope = state.read_envelope(0)
    assert (envelope.queue_id, envelope.cycle_id, envelope.job_id, envelope.work_kind) == (c.queue_id, "cycle-exact", "job-exact", "operator_turn")
    state.restore_pipelines_at_boot()
    state.restore_pipelines_at_boot()
    restored = queue.lease_next_input(0)
    assert queue.serialize_input(restored) == queue.serialize_input(c)
    queue.acknowledge_input(restored, 0)
    assert queue.lease_next_input(1) is None


def test_ready_failure_propagates_without_caching_success(state, monkeypatch):
    c = make_contributor("operator", "operator_query", 1, {"arrival_seq": 1, "cycle_id": "failure-cycle"})
    queue.enqueue_input(c, operator_activity=False)
    c = queue.lease_next_input(0)
    ignition = EpisodeIgnition("contest", 1, c.type, c, (c,), None, pipeline_id=0)
    def fail(envelope):
        raise OSError("disk full")
    monkeypatch.setattr(state, "_persist", fail)
    with pytest.raises(OSError, match="disk full"):
        state.mark_ready(0, ignition)
    assert state.read_envelope(0).state == "IDLE"


@pytest.mark.parametrize("transition", ["mark_acted", "mark_committing", "mark_failed"])
def test_completed_action_or_failed_work_is_not_replayed(state, transition):
    c, _ = ready(state)
    queue.acknowledge_input(c, 0)
    from pressure_loop.episode import CognitiveEpisode
    ep = CognitiveEpisode(pipeline_id=0, work_kind="operator_turn")
    getattr(state, transition)(0 if transition == "mark_failed" else ep)
    state.restore_pipelines_at_boot()
    assert queue.lease_next_input(0) is None


def test_journal_completion_prevents_replay(state, monkeypatch):
    c, ignition = ready(state)
    queue.acknowledge_input(c, 0)
    from pressure_loop.episode import CognitiveEpisode
    state.mark_inference(CognitiveEpisode(pipeline_id=0), ignition)
    monkeypatch.setattr(state, "_episode_in_journal", lambda episode_id: True)
    state.restore_pipelines_at_boot()
    assert queue.lease_next_input(0) is None


def test_restore_does_not_clear_envelope_when_queue_write_fails(state, monkeypatch):
    ready(state)
    def fail(c):
        raise OSError("queue unavailable")
    monkeypatch.setattr(queue, "enqueue_restore", fail)
    with pytest.raises(OSError, match="queue unavailable"):
        state.restore_pipelines_at_boot()
    assert state.read_envelope(0).state == "READY"


def test_ready_rejects_mismatched_physical_binding(state):
    _, ignition = ready(state)
    with pytest.raises(ValueError):
        state.mark_ready(1, ignition)
    with pytest.raises(ValueError):
        state.mark_ready(0, replace(ignition, pipeline_id=-1))


@pytest.mark.parametrize("has_arrival", [True, False])
def test_old_pending_row_and_active_envelope_converge(state, tmp_path, has_arrival):
    row = {"source": "operator", "type": "operator_query", "weight": 1.0,
           "metadata": {"cycle_id": "old-cycle", "line_lane": "chat"}}
    if has_arrival:
        row["metadata"]["arrival_seq"] = 7
    old_queue = queue._legacy_queue_path()
    old_queue.parent.mkdir(parents=True, exist_ok=True)
    old_queue.write_text(json.dumps([row]), encoding="utf-8")
    old = tmp_path / "lanes" / "chat.json"
    old.parent.mkdir()
    old.write_text(json.dumps({"state": "READY", "continuation": {"contributor": row}}), encoding="utf-8")
    state.restore_pipelines_at_boot()
    assert queue.queue_depth() == 1
    assert not old.exists()
    assert old.with_suffix(".json.migrated").is_file()
    c = queue.peek_inputs()[0]
    assert c.metadata["cycle_id"] == "old-cycle"
    if has_arrival:
        assert c.metadata["arrival_seq"] == 7
    state.restore_pipelines_at_boot()
    assert queue.peek_inputs()[0].queue_id == c.queue_id


def test_old_envelope_preserved_on_failed_restore(state, tmp_path, monkeypatch):
    old = tmp_path / "lanes" / "chat.json"
    old.parent.mkdir()
    old.write_text(json.dumps({"state": "READY", "continuation": {"contributor": {
        "source": "operator", "type": "operator_query", "weight": 1.0,
        "metadata": {"line_lane": "chat", "arrival_seq": 9, "cycle_id": "old-failed"}}}}), encoding="utf-8")
    def fail(c):
        raise OSError("restore failed")
    monkeypatch.setattr(queue, "enqueue_legacy_restore", fail)
    with pytest.raises(OSError):
        state.restore_pipelines_at_boot()
    assert old.is_file()


def test_crash_after_enqueue_before_envelope_clear_restores_once(state, monkeypatch):
    c, _ = ready(state)
    queue.acknowledge_input(c, 0)
    original = state.mark_idle
    def fail(pid):
        if pid == 0:
            raise OSError("clear failed")
        return original(pid)
    monkeypatch.setattr(state, "mark_idle", fail)
    with pytest.raises(OSError, match="clear failed"):
        state.restore_pipelines_at_boot()
    assert queue.queue_depth() == 1
    assert state.read_envelope(0).state == "READY"
    monkeypatch.setattr(state, "mark_idle", original)
    state.reset_for_tests()
    queue._reset_cache_for_tests()
    state.restore_pipelines_at_boot()
    assert queue.queue_depth() == 1
    assert queue.peek_inputs()[0].queue_id == c.queue_id


def test_four_same_kind_envelopes_restore_in_arrival_order(state):
    lanes = ("chat", "browser", "agent", "window")
    for seq, lane in enumerate(lanes, start=1):
        c = make_contributor("omega", "lane_work_dispatch", 1.0,
                             {"arrival_seq": seq, "cycle_id": f"turn-{seq}", "lane": lane})
        queue.enqueue_input(c, operator_activity=False)
    for pid in (3, 2, 1, 0):
        c = queue.lease_next_input(pid)
        ignition = EpisodeIgnition("contest", 1, c.type, c, (c,), None,
                                   pipeline_id=pid, work_kind="autonomous")
        state.mark_ready(pid, ignition)
        queue.acknowledge_input(c, pid)
    state.reset_for_tests()
    queue._reset_cache_for_tests()
    state.restore_pipelines_at_boot()
    restored = {queue.serialize_input(c)["lane"]: c.metadata["arrival_seq"]
                for c in queue.peek_inputs()}
    assert restored == dict(zip(lanes, range(1, 5), strict=True))


@pytest.mark.parametrize("interpret_before_second_boot", [False, True])
@pytest.mark.parametrize("has_arrival", [False, True])
def test_envelope_only_legacy_turn_registers_before_later_work(state, tmp_path, monkeypatch, interpret_before_second_boot, has_arrival):
    from pathlib import Path

    # Historical missing arrivals receive the next authority sequence at boot.
    # Explicit older arrivals must also block an already queued later turn.
    if has_arrival:
        later = make_contributor("operator", "operator_query", 1.0, {"arrival_seq": 10, "cycle_id": "later"})
        queue.enqueue_input(later, operator_activity=False)
    old = tmp_path / "lanes" / "chat.json"
    old.parent.mkdir()
    payload = {"state": "READY", "continuation": {"contributor": {
        "source": "operator", "type": "operator_query", "weight": 1.0,
        "metadata": {"line_lane": "chat", "cycle_id": "older"}}}}
    if has_arrival:
        payload["continuation"]["contributor"]["metadata"]["arrival_seq"] = 7
    old.write_text(json.dumps(payload), encoding="utf-8")
    rename = Path.rename
    def fail_archive(path, target):
        if path == old:
            raise OSError("archive interrupted")
        return rename(path, target)
    monkeypatch.setattr(Path, "rename", fail_archive)
    with pytest.raises(OSError, match="archive interrupted"):
        state.restore_pipelines_at_boot()
    assert old.is_file()
    assert not queue.earlier_operator_turns_interpreted(10)
    recovered = queue.lease_next_input(0)
    assert recovered.metadata["cycle_id"] == "older"
    seq = recovered.metadata["arrival_seq"]
    if interpret_before_second_boot:
        assert queue.mark_operator_turn_interpreted(seq, "older", outcome="answered")
        queue.acknowledge_input(recovered, 0)
    monkeypatch.setattr(Path, "rename", rename)
    state.reset_for_tests()
    queue._reset_cache_for_tests()
    state.restore_pipelines_at_boot()
    assert queue.earlier_operator_turns_interpreted(10) is interpret_before_second_boot
    assert len([c for c in queue.peek_inputs() if c.queue_id == recovered.queue_id]) == 1
    assert next(c for c in queue.peek_inputs() if c.queue_id == recovered.queue_id).metadata == recovered.metadata
    state.restore_pipelines_at_boot()
    assert queue.earlier_operator_turns_interpreted(10) is interpret_before_second_boot


@pytest.mark.parametrize("after_replace", [False, True])
def test_missing_legacy_arrival_assignment_is_one_atomic_publication(state, tmp_path, monkeypatch, after_replace):
    queue.enqueue_input(make_contributor("operator", "operator_query", 1.0, {"arrival_seq": 5, "cycle_id": "existing"}), operator_activity=False)
    old = tmp_path / "lanes" / "chat.json"
    old.parent.mkdir()
    old.write_text(json.dumps({"state": "READY", "continuation": {"contributor": {
        "source": "operator", "type": "operator_query", "weight": 1.0,
        "metadata": {"line_lane": "chat", "cycle_id": "missing-arrival"}}}}), encoding="utf-8")
    original = queue._write_encoded
    writes = []
    def fail(path, encoded):
        payload = json.loads(encoded)
        writes.append(payload)
        recovering = any(row["metadata"].get("cycle_id") == "missing-arrival" for row in payload["queue"])
        if recovering:
            if after_replace:
                original(path, encoded)
            raise OSError("queue publication interrupted")
        original(path, encoded)
    monkeypatch.setattr(queue, "_write_encoded", fail)
    with pytest.raises(OSError, match="queue publication interrupted"):
        state.restore_pipelines_at_boot()
    assert old.exists()
    assert len(writes) == 1  # no separately committed sequence reservation
    queue._reset_cache_for_tests()
    snapshot = json.loads(queue._queue_path().read_text(encoding="utf-8"))
    assert snapshot["permission_state"]["_operator_queue_state"]["next_arrival_seq"] == (7 if after_replace else 6)
    assert (snapshot["uninterpreted"].get("6") == "missing-arrival") is after_replace
    monkeypatch.setattr(queue, "_write_encoded", original)
    state.reset_for_tests()
    state.restore_pipelines_at_boot()
    restored = next(c for c in queue.peek_inputs() if c.metadata["cycle_id"] == "missing-arrival")
    assert restored.metadata["arrival_seq"] == 6
    assert not queue.earlier_operator_turns_interpreted(7)
