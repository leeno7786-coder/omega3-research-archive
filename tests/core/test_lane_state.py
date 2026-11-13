"""Physical envelope transitions and recovery integrity (migrated lane tests)."""

import json
from dataclasses import replace

import pytest

from core import pipeline_state as state
from pressure_loop import cognition_queue as queue
from pressure_loop.contributors import Contributor, EpisodeIgnition, make_contributor
from pressure_loop.episode import CognitiveEpisode


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr("core.paths.GUARDIAN_STATE_ROOT", tmp_path)
    monkeypatch.setattr(queue, "_queue_path", lambda: tmp_path / "cognition_queue.json")
    monkeypatch.setattr("pressure_loop.policy.episode_journal_path", lambda policy=None: tmp_path / "journal.jsonl")
    state.reset_for_tests()
    queue._reset_cache_for_tests()
    yield
    state.reset_for_tests()
    queue._reset_cache_for_tests()


def ignition(pid=2):
    lane = ("chat", "browser", "agent", "window")[pid]
    if pid == 0:
        c = make_contributor("operator", "operator_query", 1.0, {
            "arrival_seq": 8, "cycle_id": "cycle", "job_id": "job",
        })
        work_kind = "operator_turn"
    else:
        c = make_contributor("omega", "lane_work_dispatch", 1.0, {
            "arrival_seq": 8, "cycle_id": "cycle", "job_id": "job", "lane": lane,
        })
        work_kind = "operator_lane_work"
    queue.enqueue_input(c, operator_activity=False)
    c = queue.lease_next_input(pid)
    return EpisodeIgnition("contest", 1.0, c.type, c, (c,), None, pipeline_id=pid, work_kind=work_kind)


def test_four_envelopes_keep_their_permanent_lane_bindings():
    from core.pipelines import PIPELINE_TO_LANE

    state.restore_pipelines_at_boot()
    assert [e.pipeline_id for e in state.envelopes()] == [0, 1, 2, 3]
    assert PIPELINE_TO_LANE == {0: "chat", 1: "browser", 2: "agent", 3: "window"}
    assert {p.name for p in state.pipelines_dir().glob("*.json")} == {f"pipeline_{pid}.json" for pid in range(4)}
    assert set(state.read_envelope(0).to_dict()) == {
        "pipeline_id", "queue_id", "cycle_id", "job_id", "work_kind",
        # The operator turn this work descends from, kept through foldback so a durable stop
        # can still name it (cross-lane spec section 11, T12).
        "origin_cycle_id",
        "state", "phase", "active_episode_id", "continuation",
    }


def test_transitions_keep_lease_until_action_then_release_at_idle():
    armed = ignition()
    state.mark_ready(2, armed)
    ep = CognitiveEpisode(episode_id="carrier", pipeline_id=2, work_kind="operator_lane_work")
    for transition in (state.mark_inference, state.mark_waiting_tool, state.mark_committing):
        transition(ep)
        e = state.read_envelope(2)
        assert e.queue_id == armed.dominant_contributor.queue_id
        assert e.active_episode_id == "carrier"
        assert (e.cycle_id, e.job_id, e.work_kind) == ("cycle", "job", "operator_lane_work")
    state.mark_acted(ep)
    assert state.read_envelope(2).continuation is None
    state.mark_idle(ep)
    assert state.read_envelope(2) == state.PipelineEnvelope(2)


def test_snapshot_mutation_cannot_modify_durable_cache():
    state.mark_ready(2, ignition())
    snapshot = state.read_envelope(2)
    snapshot.continuation["contributor"]["metadata"]["cycle_id"] = "changed"
    assert state.read_envelope(2).continuation["contributor"]["metadata"]["cycle_id"] == "cycle"


@pytest.mark.parametrize("payload", ["broken", "[]", '{"pipeline_id": 2, "state": "UNKNOWN"}', '{"pipeline_id": 1}', '{"pipeline_id": 2, "state": "READY"}'])
def test_unreadable_envelopes_fail_on_read_and_write(payload):
    path = state.envelope_path(2)
    path.parent.mkdir(parents=True)
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(state.PipelineEnvelopeUnreadable):
        state.read_envelope(2)
    with pytest.raises(state.PipelineEnvelopeUnreadable):
        state.mark_failed(2)
    assert path.read_text(encoding="utf-8") == payload


def test_completion_journal_is_authoritative(tmp_path):
    armed = ignition()
    state.mark_ready(2, armed)
    ep = CognitiveEpisode(episode_id="carrier", pipeline_id=2, work_kind="operator_lane_work")
    state.mark_inference(ep)
    queue.acknowledge_input(armed.dominant_contributor, 2)
    (tmp_path / "journal.jsonl").write_text('{"episode_id":"carrier"}\n{torn', encoding="utf-8")
    state.restore_pipelines_at_boot()
    assert queue.queue_depth() == 0


def test_unreadable_journal_does_not_claim_no_completed_episode(tmp_path, monkeypatch):
    armed = ignition()
    state.mark_ready(2, armed)
    state.mark_inference(CognitiveEpisode(episode_id="carrier", pipeline_id=2))
    journal = tmp_path / "journal.jsonl"
    journal.mkdir()
    with pytest.raises(OSError):
        state.restore_pipelines_at_boot()
    assert state.read_envelope(2).state == "INFERENCE"


def test_exact_permission_type_and_closed_metadata_survive_restart():
    from tests.core.test_cognition_queue import permission
    from pressure_loop.plugin_continuations import queued_plugin_permission_continuation_from_contributor

    queue.reserve_plugin_permission_arrival("a" * 48, "b" * 64, 1)
    queue.enqueue_input(permission(), operator_activity=False)
    c = queue.lease_next_input(0)
    armed = EpisodeIgnition("contest", 1, c.type, c, (c,), None, pipeline_id=0, work_kind="operator_turn")
    state.mark_ready(0, armed)
    queue.acknowledge_input(c, 0)
    queue._reset_cache_for_tests()
    state.reset_for_tests()
    state.restore_pipelines_at_boot()
    restored = queue.lease_next_input(0)
    assert type(restored) is Contributor
    assert restored.metadata == c.metadata
    assert restored.queue_id == c.queue_id
    assert "queue_id" not in restored.metadata
    assert queued_plugin_permission_continuation_from_contributor(restored).arrival_seq == 1


@pytest.mark.parametrize("after_replace", [False, True])
def test_replace_failure_is_observable_and_restart_reads_disk(monkeypatch, after_replace):
    armed = ignition()
    original = state.os.replace
    def fail(source, target):
        if after_replace:
            original(source, target)
        raise OSError("injected replace boundary")
    monkeypatch.setattr(state.os, "replace", fail)
    with pytest.raises(OSError, match="replace boundary"):
        state.mark_ready(2, armed)
    assert state.read_envelope(2).state == ("READY" if after_replace else "IDLE")
    state.reset_for_tests()
    assert state.read_envelope(2).state == ("READY" if after_replace else "IDLE")


def test_mismatched_continuation_identity_is_unreadable():
    armed = ignition()
    envelope = state.mark_ready(2, armed)
    path = state.envelope_path(2)
    path.write_text(json.dumps(replace(envelope, queue_id="wrong").to_dict()), encoding="utf-8")
    state.reset_for_tests()
    with pytest.raises(state.PipelineEnvelopeUnreadable):
        state.read_envelope(2)
