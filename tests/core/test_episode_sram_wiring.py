"""SRAM bucket wired into the episode cascade + DeepSleep flush (operator 2026-07-16).

The bucket mechanics live in tests/memory/test_episode_sram.py; here we test the seams:
  * episode OPEN extracts the operator perception from the episode and stages it (held);
  * DeepSleep drains the bucket, retaining its owner until the terminus completes;
  * the cognition finalizer can only clear an already-drained slot, never write memory.
"""

from __future__ import annotations

from types import SimpleNamespace

import memory.episode_sram as sram
import pytest


def setup_function(_):
    sram.reset()


def teardown_function(_):
    sram.reset()


def _episode(contribs, episode_id="ep-w1"):
    return SimpleNamespace(
        episode_id=episode_id,
        pipeline_id=0,
        work_kind="operator_turn",
        pressure={"contributors": contribs},
    )


def test_operator_perception_extracted_from_episode():
    import cognition_chain as cc

    ep = _episode(
        [
            {"id": "situational", "detail": {"message": "ignore me"}},
            {"id": "operator_query", "detail": {"message": "I have a sister named Miranda."}},
        ]
    )
    assert cc._operator_perception_from_episode(ep) == "I have a sister named Miranda."


def test_no_operator_contributor_yields_empty():
    import cognition_chain as cc

    ep = _episode([{"id": "curiosity_gap", "detail": {"message": "autonomous drift"}}])
    assert cc._operator_perception_from_episode(ep) == ""


def test_open_stages_perception_and_user_turn_into_bucket():
    import cognition_chain as cc

    citation = {"source_id": "src-ep-w1", "span_hash": "span-ep-w1"}
    ep = _episode(
        [
            {
                "id": "operator_query",
                "detail": {
                    "message": "I study cybersecurity.",
                    "ingress_class": "phatic",
                    "chat_intent": "freeform",
                    "source_citation": citation,
                },
            }
        ]
    )
    cc._open_episode_sram(ep)
    assert sram.is_open("ep-w1")
    staged = {
        (it.kind, it.text, it.speaker if it.kind == "perception" else it.role)
        for it in sram.pending()
    }
    assert ("perception", "I study cybersecurity.", "operator") in staged
    assert ("turn", "I study cybersecurity.", "user") in staged
    perception = next(it for it in sram.pending() if it.kind == "perception")
    assert perception.ingress_class == "phatic"
    assert perception.chat_intent == "freeform"
    assert perception.source_citation == citation


def test_open_for_autonomous_episode_holds_nothing():
    import cognition_chain as cc

    ep = _episode([{"id": "curiosity_gap", "detail": {}}], episode_id="ep-auto")
    cc._open_episode_sram(ep)
    assert sram.is_open("ep-auto")  # uniform lifecycle — a bucket still opens
    assert sram.pending() == []  # ...but nothing is held


def test_open_moves_queued_external_memory_into_the_episode_bucket():
    import cognition_chain as cc

    envelope = {
        "cycle_id": "sensory-calendar-ev-1",
        "coord": [9, 1, 0],
        "type": "situational",
        "payload": {"text": "screened observation"},
        "source": "sensory:calendar",
    }
    ep = _episode(
        [
            {
                "id": "sensory_observation",
                "detail": {
                    "cycle_id": "sensory-calendar-ev-1",
                    "memory_envelopes": [envelope],
                },
            }
        ],
        episode_id="ep-external-observation",
    )

    cc._open_episode_sram(ep)

    [held] = sram.pending()
    assert held.kind == "memory"
    assert held.envelope.cycle_id == "sensory-calendar-ev-1"
    assert held.envelope.payload == {"text": "screened observation"}


def test_open_projects_queued_onboarding_only_after_the_episode_bucket_exists(monkeypatch):
    import cognition_chain as cc

    profile = {"name": "Ada", "occupation": "software_engineer"}
    citation = {"source_id": "src-onboarding", "source_type": "operator_onboarding"}
    calls = []

    def ingest(received, **kwargs):
        assert sram.is_open("ep-onboarding")
        calls.append(("persona", dict(received), kwargs))
        return SimpleNamespace(records=["name", "occupation"])

    def seed(occupation, topics, **kwargs):
        assert sram.is_open("ep-onboarding")
        calls.append(("seed", occupation, list(topics), kwargs))
        return {"seeded": topics}

    monkeypatch.setattr("memory.persona_ingest.ingest_operator_profile", ingest)
    monkeypatch.setattr("hud.operator_onboarding.seed_first_wake", seed)
    ep = _episode(
        [
            {
                "id": "sensory_observation",
                "detail": {
                    "cycle_id": "operator-onboarding",
                    "onboarding_profile": {
                        "profile": profile,
                        "subcategories": ["Python"],
                        "source_citation": citation,
                    },
                },
            }
        ],
        episode_id="ep-onboarding",
    )

    cc._open_episode_sram(ep)

    assert calls == [
        ("persona", profile, {"source_citation": citation}),
        (
            "seed",
            "software_engineer",
            ["Python"],
            {"source_citation": citation, "operator_id": "operator"},
        ),
    ]


def test_episode_sram_open_failure_propagates(monkeypatch):
    import cognition_chain as cc

    monkeypatch.setattr(
        sram,
        "open_bucket",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("SRAM unavailable")),
    )

    with pytest.raises(RuntimeError, match="SRAM unavailable"):
        cc._open_episode_sram(_episode([], episode_id="ep-sram-open-failure"))


def test_post_deepsleep_cleanup_delegates_to_write_free_clear(monkeypatch):
    import cognition_chain as cc

    seen = {}
    monkeypatch.setattr(sram, "is_open", lambda ep_id=None: ep_id == "ep-w1")
    monkeypatch.setattr(sram, "pending", lambda ep_id=None: [])
    monkeypatch.setattr(sram, "clear_bucket", lambda ep_id: seen.setdefault("ep", ep_id))
    cc._close_episode_sram("ep-w1")
    assert seen["ep"] == "ep-w1"


def test_post_deepsleep_cleanup_refuses_to_erase_unflushed_intake():
    import cognition_chain as cc

    sram.open_bucket("ep-failed-drain", pipeline_id=0)
    sram.hold_turn("user", "retain me")

    with pytest.raises(RuntimeError, match="still has 1 pending item"):
        cc._close_episode_sram("ep-failed-drain")

    assert [item.text for item in sram.pending()] == ["retain me"]


def test_deepsleep_flush_helper_flushes_the_named_episode(monkeypatch):
    import deepsleep_loop.loop as loop

    called = {}

    def _spy(cid):
        called["cid"] = cid
        return {"ingested": ["x"], "turns": [], "errors": []}

    monkeypatch.setattr("memory.episode_sram.flush", _spy)
    result = loop._flush_episode_sram("ep-ds")
    assert called["cid"] == "ep-ds"
    assert result["ingested"] == ["x"]


def test_deepsleep_flush_helper_propagates_the_flush_cause(monkeypatch):
    import deepsleep_loop.loop as loop

    def _boom(cid):
        raise RuntimeError("flush boom")

    monkeypatch.setattr("memory.episode_sram.flush", _boom)
    with pytest.raises(RuntimeError, match="flush boom"):
        loop._flush_episode_sram("ep-ds")


def test_deepsleep_rebuilds_source_validity_before_program0_publication(monkeypatch, tmp_path):
    import deepsleep_loop.loop as loop
    from memory.projection.dispatch import DispatchReport

    counts = {
        "records_scanned": 2,
        "workspace_records": 2,
        "objects": 1,
        "revisions": 2,
        "available": 1,
        "unavailable": 0,
        "tombstoned": 0,
    }
    order: list[str] = []
    monkeypatch.setattr("core.paths.MEMORY_ROOT", tmp_path / "memory", raising=False)
    monkeypatch.setattr(
        "memory.source_validity.rebuild_from_canonical",
        lambda *_a, **_k: order.append("source_validity") or counts,
    )
    monkeypatch.setattr(
        "memory.projection.dispatch.projection_dispatch",
        lambda _root, *, reason: order.append("program0")
        or DispatchReport(
            owner="program0",
            action="published",
            publication=None,
            drain_status="complete",
            generation_id="generation-1",
        ),
    )

    result = loop._maintain_projection_inputs_after_deepsleep(
        context_id="ep-source-validity",
        scope="episode",
        before_projection=lambda _root: order.append("canonical_mutations"),
    )
    assert result == counts
    assert order == ["canonical_mutations", "source_validity"]

    report = loop._publish_projection_after_deepsleep(
        context_id="ep-source-validity",
        scope="episode",
    )
    assert report.owner == "program0"
    assert order == ["canonical_mutations", "source_validity", "program0"]


# --- The flush result must survive the seam (AGENTS.md §10 #15, §2.1, §5.0) ---
#
# The single auditable lane collects per-item failures into `errors` and rejected
# items into `skipped`, then hands both back.  Every one of those is a fact that
# did NOT reach memory.  If the dict dies at the seam, an ingest failure is
# indistinguishable from a clean episode — §2.1's exact shape, at the one place
# that is supposed to make memory auditable.


def _flush_result(**over) -> dict:
    """A flush return in its full production shape."""
    base = {
        "enabled": True,
        "ingested": [],
        "turns": [],
        "recalls": [],
        "memories": [],
        "errors": [],
        "skipped": [],
    }
    base.update(over)
    return base


def test_flush_telemetry_reports_skipped_alongside_errors(monkeypatch):
    """A rejected item is as reportable as a raised one.  `no_committed_persona_records`
    is the measured empty-stub case: the operator was told the fact was remembered."""
    import deepsleep_loop.loop as loop

    dropped = [{"text": "I am a dermatologist.", "reason": "no_committed_persona_records"}]
    emitted: list[dict] = []
    monkeypatch.setattr(loop, "emit_data", lambda *a, **kw: emitted.append(dict(kw)))
    monkeypatch.setattr(
        "memory.episode_sram.flush",
        lambda cid: _flush_result(ingested=["kept"], skipped=list(dropped)),
    )

    loop._flush_episode_sram("ep-skip")

    assert emitted, "the flush must emit"
    payload = emitted[0]
    assert payload["skipped"] == dropped
    assert "skipped=1" in payload["summary"]


def test_every_flush_return_path_supplies_skipped():
    """Contract test between memory.episode_sram and its DeepSleep consumer.
    The consumer reads result['skipped'] unconditionally, so every producer path
    must supply it — drift on either side breaks a test, not a production run."""
    sram.reset()
    assert "skipped" in sram.flush("ep-no-bucket")

    sram.open_bucket("ep-empty", pipeline_id=0)
    assert "skipped" in sram.flush("ep-empty")

    sram.reset()
    sram.open_bucket("ep-a", pipeline_id=0)
    assert "skipped" in sram.flush("ep-b")  # episode mismatch


def test_episode_pack_carries_the_sram_flush_result(tmp_path, monkeypatch):
    """#15 proper: the dict reached no episode, no telemetry, no operator.
    DeepSleep is the terminus (§3), so the flush outcome closes with the episode."""
    from deepsleep_loop.loop import run_deepsleep_for_episode
    from tests.core.deepsleep_window_fixtures import (
        active_store_from_episode_body,
        episode_entry,
        patch_window_deepsleep_paths,
    )

    patch_window_deepsleep_paths(monkeypatch, tmp_path)
    from memory.episode_sram import open_bucket

    body = episode_entry(
        "ep-sram-pack", intent="LEARN", target="Python", knowledge_mastery=0.10
    )
    store, episode = active_store_from_episode_body(body)
    open_bucket(episode.episode_id, pipeline_id=episode.pipeline_id)
    real_flush = sram.flush

    def reported_flush(cid):
        # Keep the real ownership/drain contract while controlling the reported
        # outcomes this pack-and-telemetry test is about.
        real_flush(cid)
        return _flush_result(
            ingested=["kept"],
            errors=["perception:RuntimeError('ingest blew up')"],
            skipped=[{"text": "dropped.", "reason": "no_committed_persona_records"}],
        )

    monkeypatch.setattr("memory.episode_sram.flush", reported_flush)

    outcome = run_deepsleep_for_episode(store)

    assert outcome is not None
    flushed = outcome.projection_state.get("episode_sram_flush") or {}
    assert flushed["ingested"] == ["kept"]
    assert flushed["errors"] == ["perception:RuntimeError('ingest blew up')"]
    assert flushed["skipped"] == [{"text": "dropped.", "reason": "no_committed_persona_records"}]


def test_deepsleep_commit_failure_is_captured_and_still_propagates(tmp_path, monkeypatch):
    from deepsleep_loop.loop import run_deepsleep_for_episode
    from memory.episode_events import read_episode_events
    from tests.core.deepsleep_window_fixtures import (
        active_store_from_episode_body,
        episode_entry,
        patch_window_deepsleep_paths,
    )

    patch_window_deepsleep_paths(monkeypatch, tmp_path)
    body = episode_entry("ep-deepsleep-failure", intent="LEARN", target="Python")
    store, episode = active_store_from_episode_body(body)
    from memory.episode_sram import open_bucket

    open_bucket(episode.episode_id, pipeline_id=episode.pipeline_id)
    failure = RuntimeError("deepsleep transaction failed")

    def fail_core(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr("deepsleep_loop.loop._run_deepsleep_episode_core", fail_core)
    with pytest.raises(RuntimeError) as caught:
        run_deepsleep_for_episode(store)

    assert caught.value is failure
    failures = [
        event for event in read_episode_events(episode.episode_id)
        if event.event_kind == "deepsleep.phase_failed"
    ]
    assert len(failures) == 1
    assert failures[0].payload["phase"] == "deepsleep"
    assert "transaction failed" in failures[0].payload["reason"]
    from deepsleep_loop.failure_store import failure_key, load_failures

    learned = load_failures(path=tmp_path / "failure.json")
    assert failure_key("response", "Python") in learned


def test_deepsleep_failure_learning_error_does_not_replace_the_phase_error(tmp_path, monkeypatch):
    from deepsleep_loop.loop import run_deepsleep_for_episode
    from tests.core.deepsleep_window_fixtures import (
        active_store_from_episode_body,
        episode_entry,
        patch_window_deepsleep_paths,
    )

    patch_window_deepsleep_paths(monkeypatch, tmp_path)
    body = episode_entry("ep-deepsleep-learning-failure", intent="LEARN", target="Python")
    store, episode = active_store_from_episode_body(body)
    from memory.episode_sram import open_bucket

    open_bucket(episode.episode_id, pipeline_id=episode.pipeline_id)
    producer_failure = RuntimeError("deepsleep transaction failed")
    learning_failure = OSError("failure-pattern store unavailable")

    def fail_core(*_args, **_kwargs):
        raise producer_failure

    def fail_learning(_ds_input):
        raise learning_failure

    monkeypatch.setattr("deepsleep_loop.loop._run_deepsleep_episode_core", fail_core)
    monkeypatch.setattr("deepsleep_loop.loop.failure_phase.consolidate_failures", fail_learning)
    with pytest.raises(RuntimeError) as caught:
        run_deepsleep_for_episode(store)

    assert caught.value is producer_failure
    assert any(
        "failure-pattern recording also failed" in note and "store unavailable" in note
        for note in getattr(producer_failure, "__notes__", ())
    )


def test_deepsleep_commits_all_projections_then_publishes_after_maintenance(tmp_path, monkeypatch):
    from deepsleep_loop.loop import run_deepsleep_for_episode
    from memory.projection.dispatch import projection_dispatch
    from tests.core.deepsleep_window_fixtures import (
        active_store_from_episode_body,
        episode_entry,
        patch_window_deepsleep_paths,
    )

    patch_window_deepsleep_paths(monkeypatch, tmp_path)
    from memory.episode_sram import open_bucket

    body = episode_entry("ep-ordered-memory-cycle", intent="LEARN", target="Python")
    store, episode = active_store_from_episode_body(body)
    open_bucket(episode.episode_id, pipeline_id=episode.pipeline_id)
    order: list[str] = []
    from deepsleep_loop.loop import _capture_closed_episode_to_taxonomy, _bind_sealed_record

    def ordered_capture(*args, **kwargs):
        order.append("canonical_projection")
        return _capture_closed_episode_to_taxonomy(*args, **kwargs)

    monkeypatch.setattr(
        "deepsleep_loop.loop._capture_closed_episode_to_taxonomy",
        ordered_capture,
    )
    from deepsleep_loop.loop import _flush_episode_sram

    def ordered_flush(context_id):
        order.append("sram_flush")
        return _flush_episode_sram(context_id)

    monkeypatch.setattr(
        "deepsleep_loop.loop._flush_episode_sram",
        ordered_flush,
    )
    def ordered_receipt(*args, **kwargs):
        order.append("canonical_receipt")
        return _bind_sealed_record(*args, **kwargs)

    monkeypatch.setattr("deepsleep_loop.loop._bind_sealed_record", ordered_receipt)
    monkeypatch.setattr(
        "deepsleep_loop.loop._consolidate_associations",
        lambda *_a, **_k: order.append("association_consolidation") or {},
    )
    monkeypatch.setattr(
        "memory.source_validity.rebuild_from_canonical",
        lambda *_a, **_k: order.append("source_validity") or {},
    )
    monkeypatch.setattr(
        "memory.pathway_performance.rebuild_pathway_projection",
        lambda: order.append("pathway_projection"),
    )
    monkeypatch.setattr(
        "deepsleep_loop.loop._run_memory_integrity_housekeeping",
        lambda **_k: order.append("maintenance") or {},
    )
    monkeypatch.setattr(
        "deepsleep_loop.loop._run_conflict_reconciliation",
        lambda **_k: order.append("conflict_reconciliation") or {},
    )

    def _dispatch(_root, *, reason):
        assert reason == "deepsleep"
        order.append("program0_publish")
        return projection_dispatch(_root, reason=reason)

    monkeypatch.setattr("memory.projection.dispatch.projection_dispatch", _dispatch)

    run_deepsleep_for_episode(store)

    assert order == [
        "canonical_projection",
        "sram_flush",
        "canonical_receipt",
        "conflict_reconciliation",
        "association_consolidation",
        "source_validity",
        "pathway_projection",
        "maintenance",
        "program0_publish",
    ]


def test_flush_telemetry_reports_every_key_of_its_own_contract(monkeypatch):
    """A flush that did real work must not report zeros (measured 2026-09-22).

    The return contract carries eight keys — enabled, ingested, turns, recalls, memories, events,
    errors, skipped — and the emission carried four. So a flush that consolidated memories or events
    emitted `ingested=0 turns=0 errors=0 skipped=0`, and 50 live flushes totalling 325 seconds
    reported 14 records between them: one of them spent 17.9s and reported nothing at all. Real work
    was indistinguishable from an idle pass, which made "where did the terminus spend its time?"
    unanswerable at the one seam that exists to make the memory lane auditable (§2.1, §5.0).

    Row #15's closure added `skipped` and left three keys behind. This asserts the whole contract, so
    the next key added to the result cannot go unreported either.
    """
    import deepsleep_loop.loop as loop
    from memory import episode_sram as sram

    result = _flush_result(
        ingested=["fact-1"], turns=["turn-1"], recalls=["recall-1"],
        memories=["mem-1", "mem-2"], events=["evt-1"],
    )
    result.setdefault("events", ["evt-1"])
    emitted: list[dict] = []
    monkeypatch.setattr(loop, "emit_data", lambda *a, **kw: emitted.append(dict(kw)))
    monkeypatch.setattr("memory.episode_sram.flush", lambda cid: dict(result))

    loop._flush_episode_sram("ep-full")

    payload = emitted[0]
    contract = set(sram._empty_result()) - {"enabled"}
    missing = sorted(key for key in contract if key not in payload)
    assert not missing, f"the flush did work these keys describe and never reported it: {missing}"
    assert payload["memories"] == ["mem-1", "mem-2"]
    assert payload["events"] == ["evt-1"]
    assert payload["recalls"] == ["recall-1"]
    summary = payload["summary"]
    for token in ("memories=2", "events=1", "recalls=1"):
        assert token in summary, f"{token!r} missing from {summary!r}"
