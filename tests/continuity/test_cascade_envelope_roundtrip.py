"""Queue-to-recall contracts through real phase owners and the real DeepSleep terminus."""

from __future__ import annotations

import json

import pytest

from tests.helpers.blueprint_driver import BlueprintDriver, browser_action, browser_decision


def test_admitted_turn_survives_every_phase_and_published_memory(monkeypatch, tmp_path):
    import deepsleep_loop.loop as deepsleep
    from memory import episode_commit_view, episode_sram
    from memory.engine import walker
    from pressure_loop import cognition_queue
    from core.pipelines import lane_for_contributor
    from pressure_loop.contributors import make_contributor
    from schema.episode_record import EpisodeRecord
    from tests.helpers.intent_router import stub_router

    driver = BlueprintDriver(monkeypatch=monkeypatch, tmp_path=tmp_path)
    stub_router(monkeypatch, "BROWSER_AGENT")
    address = "https://example.org/cascade-sentinel"
    request = "Open the cascade sentinel page and report its title."
    synthesis = "The cascade sentinel page is open."
    driver.browser_actions.extend([
        browser_action("navigate", url=address),
        browser_action("done", summary="CASCADE-SENTINEL-ACTION"),
    ])
    driver.omega.append(browser_decision("DONE", synthesis=synthesis))

    boundaries = []
    packs = []
    real_terminus = deepsleep.run_deepsleep_for_episode

    def terminus(store, **kwargs):
        episode = store.current()
        assert episode_sram.pending(episode.episode_id)
        assert episode_commit_view.episode_record_ref(episode.episode_id) is None
        boundaries.append("deepsleep")
        pack = real_terminus(store, **kwargs)
        packs.append(pack)
        return pack

    monkeypatch.setattr(deepsleep, "run_deepsleep_for_episode", terminus)
    for module, name, phase in (
        ("global_loop.loop", "run_global_a", "global_a"),
        ("meta_loop.loop", "run_meta_a", "meta_a"),
        ("auto_loop.auto", "run_auto_front", "auto"),
        ("meta_loop.meta_b", "run_meta_b_front", "meta_b"),
        ("global_loop.loop", "run_global_b", "global_b"),
        ("cognition_chain", "_run_omega_episode_cognition", "omega"),
    ):
        import importlib

        owner = importlib.import_module(module)
        real = getattr(owner, name)

        def observed(*args, _real=real, _phase=phase, **kwargs):
            boundaries.append(_phase)
            return _real(*args, **kwargs)

        monkeypatch.setattr(owner, name, observed)

    contributor = make_contributor("operator", "operator_query", 1.0, {
        "message": request,
        "cycle_id": "cascade-sentinel-cycle",
        "turn_id": "cascade-sentinel-turn",
        "thread_id": "cascade-sentinel-thread",
        "source_citation": {"id": "cascade-sentinel-citation"},
        "connection_authority_revision": "cascade-sentinel-authority",
        "chat_bubble_waiting": True,
        "directive_intent": "RESPOND",
    })
    assert cognition_queue.enqueue_input(contributor, operator_activity=False)
    leased = cognition_queue.lease_next_input(0)
    assert leased is not None
    episode = driver._ignite(leased, opened_by=leased.type)

    assert boundaries == ["global_a", "meta_a", "auto", "meta_b", "global_b", "omega", "deepsleep"]
    remaining = cognition_queue.peek_inputs()
    assert len(remaining) == 1, "the browser handoff remains queued after Chat acknowledges"
    assert all(row.metadata.get("queue_id") != leased.metadata["queue_id"] for row in remaining)
    assert lane_for_contributor(remaining[0]) == "browser"
    assert driver.store.current() is None
    assert not episode_sram.is_open(episode.episode_id)
    reply = driver.reply("cascade-sentinel-cycle")["text"]
    assert "Browser lane" in reply and "this conversation" in reply
    assert len(driver.browser_actions) == 2, "Chat routing must not execute Browser work inline"
    assert packs[0].projection_state["episode_record"]["ref"]["record_id"]

    ref = episode_commit_view.episode_record_ref(episode.episode_id)
    assert ref is not None
    walker.load_coord_index()
    assert ref.record_id in walker.COORD_INDEX
    with open(walker.path_for_id(ref.record_id), encoding="utf-8") as handle:
        document = json.load(handle)
    record = EpisodeRecord.from_dict(json.loads(document["text"])["episode_record"])
    assert record.content_sha256() == ref.content_sha256
    assert record.identity["episode_id"] == episode.episode_id
    assert record.identity["thread_id"] == "cascade-sentinel-thread"
    assert record.identity["turn_id"].value == "cascade-sentinel-turn"
    assert record.temporal["queue_arrival_seq"].value == leased.metadata["arrival_seq"]
    assert record.objective["original_request"] == request
    assert record.metacognition["control"].synthesis == synthesis
    assert record.outcome.cascade_status == "completed"
    assert record.outcome.action_status == "succeeded"
    events = record.events["by_value"]
    assert {event.episode_id for event in events} == {episode.episode_id}
    assert {event.thread_id for event in events} == {"cascade-sentinel-thread"}
    assert {event.turn_id for event in events} == {"cascade-sentinel-turn"}
    assert {event.arrival_seq for event in events} == {leased.metadata["arrival_seq"]}
    origin = next(event for event in events if event.event_kind == "admission.origin")
    assert origin.payload["queue_id"] == leased.metadata["queue_id"]
    assert origin.payload["authority_revision"] == "cascade-sentinel-authority"
    # The sealed contract carries compact typed evidence; the canonical body keeps
    # the complete phase payloads beside it, including the browser's action report.
    canonical_body = json.loads(document["text"])
    assert "CASCADE-SENTINEL-ACTION" not in json.dumps(canonical_body["outputs"])
    assert canonical_body["outcome"] == episode.global_["outcome"]


@pytest.mark.parametrize("context", ["temporal", "learning_state"])
def test_global_b_context_conversion_preserves_the_failure(monkeypatch, context):
    from global_loop.loop import run_global_b
    from tests.core.test_global_b_pack import _episode

    episode = _episode({"cycle_kind": "success", "outputs": {}, "telemetry": {}})
    setattr(episode, context, {"source": "cascade-context-sentinel"})
    fault = ValueError(f"{context} envelope cannot be compacted")

    def fail(_envelope):
        raise fault

    target = (
        "core.episode_temporal.compact_temporal_summary" if context == "temporal"
        else "pressure_loop.symphony_learning_state.compact_learning_state_summary"
    )
    monkeypatch.setattr(target, fail)
    with pytest.raises(ValueError) as caught:
        run_global_b(episode, {"execution": {"executed": True, "result": "success"}})
    assert caught.value is fault
    assert "outcome" not in episode.global_


@pytest.mark.parametrize("context", ["temporal", "learning_state"])
def test_admission_snapshot_failure_preserves_the_unconsumed_request(monkeypatch, context):
    import cognition_chain
    from pressure_loop import cognition_queue
    from pressure_loop.episode import EpisodeStore
    from tests.continuity.test_admission_capture import _ignition, _lease, _operator_contributor

    leased = _lease(_operator_contributor())
    store = EpisodeStore()
    fault = OSError(f"{context} snapshot source unavailable")

    def fail(*_args, **_kwargs):
        raise fault

    target = (
        "core.episode_temporal.capture_temporal_context" if context == "temporal"
        else "pressure_loop.symphony_learning_state.build_episode_learning_state"
    )
    monkeypatch.setattr(target, fail)
    with pytest.raises(OSError) as caught:
        cognition_chain._admit_episode(store, _ignition(leased))
    assert caught.value is fault
    assert store.current() is None
    assert cognition_queue.queue_depth() == 1
    assert cognition_queue.owns_input_lease(leased, 0)


def test_temporal_envelope_carries_the_admitted_cycle(monkeypatch):
    import cognition_chain
    from memory import episode_sram
    from pressure_loop.episode import EpisodeStore
    from tests.continuity.test_admission_capture import _ignition, _lease, _operator_contributor

    leased = _lease(_operator_contributor(cycle_id="temporal-cycle-sentinel"))
    store = EpisodeStore()
    try:
        episode, _ = cognition_chain._admit_episode(store, _ignition(leased))
        assert episode.temporal["cycle_id"] == "temporal-cycle-sentinel"
    finally:
        episode_sram.reset()


def test_seal_preserves_the_origin_references_captured_at_admission():
    import cognition_chain
    from core.commit_region import serialized_commit
    from memory import episode_sram
    from memory.episode_events import read_episode_events
    from memory.episode_record_seal import seal_episode_record
    from pressure_loop.episode import EpisodeStore
    from tests.continuity.test_admission_capture import _ignition, _lease, _operator_contributor

    leased = _lease(_operator_contributor())
    ignition = _ignition(leased)
    store = EpisodeStore()
    try:
        episode, _ = cognition_chain._admit_episode(store, ignition)
        with serialized_commit(episode):
            record = seal_episode_record(
                episode, events=read_episode_events(episode.episode_id), effects=()
            )
        assert record.origin["authority_snapshot_ref"].value == leased.metadata["connection_authority_revision"]
        assert dict(record.origin["initial_trajectory_ref"].value) == ignition.trajectory_snapshot
    finally:
        episode_sram.reset()


def test_global_b_does_not_reconstruct_a_failed_meta_b_input(monkeypatch):
    from global_loop.assemble.run import global_b_input_from_episode
    from tests.core.test_global_b_pack import _episode

    fault = ValueError("Meta B execution envelope is corrupt")

    def fail(_episode):
        raise fault

    monkeypatch.setattr("meta_loop.meta_b.meta_b_input_from_episode", fail)
    episode = _episode({"cycle_kind": "success", "outputs": {}, "telemetry": {}})
    with pytest.raises(ValueError) as caught:
        global_b_input_from_episode(episode, {})
    assert caught.value is fault


@pytest.mark.parametrize("view,reader", [
    ("_execution_experience_view", "memory.execution_experience.experience_summary"),
    ("build_learning_map", "omega_loop.episode.packet.load_mastery"),
    ("_trajectory_influencer_block", "pressure_loop.temporal_store.read_trajectory"),
    ("_open_questions_view", "pressure_loop.open_questions.list_open"),
])
def test_omega_packet_does_not_misrepresent_failed_context_as_empty(monkeypatch, view, reader):
    from omega_loop.episode import packet

    fault = OSError(f"{view}: sentinel context source unavailable")

    def fail(*_args, **_kwargs):
        raise fault

    monkeypatch.setattr(reader, fail)
    with pytest.raises(OSError) as caught:
        getattr(packet, view)()
    assert caught.value is fault


def test_omega_packet_does_not_silently_drop_pressure_context(monkeypatch):
    from types import SimpleNamespace

    from omega_loop.episode.packet import _assemble_foldback_packet

    fault = OSError("sentinel pressure snapshot read failed")

    def fail(*_args, **_kwargs):
        raise fault

    monkeypatch.setattr("pressure_loop.pressure_surface.build_pressure_surface", fail)
    gbi = SimpleNamespace(pressure={})
    with pytest.raises(OSError) as caught:
        _assemble_foldback_packet(gbi, {}, {}, {}, "")
    assert caught.value is fault


def test_omega_mastery_delta_preserves_a_failed_history_read(monkeypatch):
    from omega_loop.episode import packet
    from omega_loop.episode.types import MasteryView

    fault = OSError("sentinel mastery history read failed")

    def fail():
        raise fault

    monkeypatch.setattr(packet, "_prior_mastery_for_domain", lambda _: MasteryView(
        domain="test-domain", knowledge_mastery=0.4, application_mastery=0.2,
        target_mastery=0.9,
    ))
    monkeypatch.setattr("deepsleep_loop.mastery_store.load_mastery", fail)
    with pytest.raises(OSError) as caught:
        packet._mastery_delta_block("test-domain")
    assert caught.value is fault


@pytest.mark.parametrize("reader", [
    "omega_loop.episode.workspace_tests.collect_test_report",
    "core.build_verification.verify_build_against_request",
])
def test_omega_build_packet_preserves_failed_verification_reads(monkeypatch, reader):
    from global_loop.assemble.run import global_b_input_from_episode
    from omega_loop.episode.packet import build_omega_episode_input
    from tests.core.test_global_b_pack import _episode

    fault = OSError("sentinel build verification unavailable")

    def fail(*_args, **_kwargs):
        raise fault

    monkeypatch.setattr("omega_loop.episode.workspace_tests.collect_test_report",
                        lambda _: {"ran": False})
    monkeypatch.setattr(reader, fail)
    episode = _episode({"outputs": {"skill_result": {
        "status": "partial", "files_changed": [], "summary": "build remains incomplete",
    }}}, intent="EXECUTE", action="USE_SKILL")
    gbi = global_b_input_from_episode(episode, {})
    with pytest.raises(OSError) as caught:
        build_omega_episode_input(gbi, {})
    assert caught.value is fault


def test_dev_recall_failure_is_attributed_to_its_actual_phase(monkeypatch):
    from pressure_loop.agent_lane import recall_dev_context
    from pressure_loop.episode_journal import phase_trail
    from tests.continuity.test_front_envelope_handoffs import _resume_episode, owned

    fault = OSError("sentinel admission dev recall failed")

    def fail(_spec):
        raise fault

    monkeypatch.setattr("memory.api.read_memory", fail)
    episode = _resume_episode()
    with owned(episode):
        with pytest.raises(OSError):
            recall_dev_context(episode)
        phases = {row["phase"]: row for row in phase_trail(episode)}
        assert phases["admission"]["status"] == "failed"
        assert str(fault) in phases["admission"]["cause"]
        assert phases["admission"]["evidence"]
        assert phases["global_a"]["status"] == "not_run"
