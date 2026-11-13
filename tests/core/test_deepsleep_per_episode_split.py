"""DeepSleep is one complete terminus PER EPISODE — including the window episode.

The 2026-08-03 collapse made the per-episode terminus the only one, because the
window terminus at the time was a SECOND consolidation path an ordinary episode
could fall into. Phase 4 cut 10 (operator ruling 2026-08-10) brings a window
consolidation back on a different footing: it is the terminus of the window
LANE's own episode, reachable only from a carrier the window lane opened, and it
consolidates the ten reflected episodes rather than re-consolidating the one that
is closing. The invariant the collapse was protecting is unchanged and pinned
below — an ordinary episode has exactly one terminus and it is the per-episode
one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import cognition_chain as cl
from core import paths as core_paths
from core.required_substrate import RequiredSubstrateError
from deepsleep_loop.intake.run import build_deepsleep_input_for_episode
from deepsleep_loop.loop import run_deepsleep_for_episode
from deepsleep_loop.mastery_store import load_mastery
from pressure_loop.episode import EpisodeStore
from tests.core.deepsleep_window_fixtures import (
    episode_entry,
    meta_b_reflection_for,
    patch_window_deepsleep_paths,
)


def test_only_a_window_lane_episode_can_reach_the_window_terminus():
    """The window terminus exists again (cut 10) but is not a second path an
    ordinary episode can take: ``_ignite_episode_cascade`` chooses it only when
    ``window_lane.reflection_committed`` is true, which requires a carried
    ReflectionWindow that this episode's own fold archived and unstaged."""
    from types import SimpleNamespace

    from deepsleep_loop import loop as deepsleep
    from pressure_loop import window_lane

    assert hasattr(deepsleep, "run_deepsleep_for_window")

    ordinary = SimpleNamespace(episode_id="ep-ordinary", meta={}, lane="chat")
    assert window_lane.is_window_episode(ordinary) is False
    assert window_lane.reflection_committed(ordinary) is False
    # Not even a hand-written meta record reaches it: the gate is the ARCHIVED
    # flag that only commit_reflection sets, after the archive file landed.
    ordinary.meta = {window_lane.WINDOW_META_KEY: {"window_id": "w-1"}}
    assert window_lane.reflection_committed(ordinary) is False


def _learn_body(
    episode_id: str = "ep-learn-1",
    *,
    target: str = "Python",
    knowledge_mastery: float = 0.10,
) -> dict:
    return episode_entry(
        episode_id,
        intent="LEARN",
        target=target,
        success=True,
        knowledge_mastery=knowledge_mastery,
    )


def _run_deepsleep(episode_id: str, body: dict):
    from memory.episode_sram import open_bucket

    store = EpisodeStore()
    episode = store.open(
        opened_by=str(body.get("ignition_reason") or "test"),
        pipeline_id=0,
        work_kind=str(body.get("work_kind") or "autonomous"),
    )
    episode.episode_id = episode_id
    episode.meta = {
        "execution_action_pack": dict(
            (body.get("execution") or {}).get("execution_action_pack") or {}
        ),
        **meta_b_reflection_for(body),
    }
    episode.global_ = {
        "intent_decision": {"intent": body.get("intent")},
        "outcome": dict(body.get("outcome") or {}),
        "omega_result": body.get("omega_result"),
    }
    episode.omega = {"trajectory_update": body.get("trajectory_update")}
    episode.auto = {"outputs": dict(body.get("outputs") or {})}
    open_bucket(episode_id, pipeline_id=episode.pipeline_id)
    return run_deepsleep_for_episode(store)


def _phase_recorder(monkeypatch) -> dict[str, list[str]]:
    calls: dict[str, list[str]] = {"ran": [], "skipped": []}

    def _track(name: str, original):
        def _wrapper(*args, **kwargs):
            calls["ran"].append(name)
            return original(*args, **kwargs)

        return _wrapper

    import deepsleep_loop.loop as ds_loop

    monkeypatch.setattr(
        ds_loop.mastery_phase,
        "update_mastery",
        _track("mastery", ds_loop.mastery_phase.update_mastery),
    )
    monkeypatch.setattr(
        ds_loop.failure_phase,
        "consolidate_failures",
        _track("failure", ds_loop.failure_phase.consolidate_failures),
    )
    monkeypatch.setattr(
        ds_loop.semantic_phase,
        "promote_semantic",
        _track("semantic", ds_loop.semantic_phase.promote_semantic),
    )
    monkeypatch.setattr(
        ds_loop.consolidate_phase,
        "consolidate",
        _track("consolidate", ds_loop.consolidate_phase.consolidate),
    )

    import deepsleep_loop.consolidate_memory.run as consolidate_memory_run

    monkeypatch.setattr(
        "deepsleep_loop.consolidate_memory.consolidate_to_memory",
        _track(
            "consolidate_memory",
            consolidate_memory_run.consolidate_to_memory,
        ),
    )
    monkeypatch.setattr(
        ds_loop.identity_phase,
        "update_identity",
        _track("identity", ds_loop.identity_phase.update_identity),
    )
    monkeypatch.setattr(
        ds_loop.trajectory_phase,
        "maintain_trajectory",
        _track("trajectory", ds_loop.trajectory_phase.maintain_trajectory),
    )
    monkeypatch.setattr(
        ds_loop.query_lifecycle_phase,
        "run_query_lifecycle",
        _track("query_lifecycle", ds_loop.query_lifecycle_phase.run_query_lifecycle),
    )
    monkeypatch.setattr(
        ds_loop.projection_phase,
        "build_projection",
        _track("projection", ds_loop.projection_phase.build_projection),
    )
    return calls


# --- 1. Hook fires episode-level only ---


def test_per_episode_runner_runs_the_complete_deepsleep_cycle(tmp_path, monkeypatch):
    paths = patch_window_deepsleep_paths(monkeypatch, tmp_path)
    calls = _phase_recorder(monkeypatch)
    body = _learn_body("ep-phase-1")
    _run_deepsleep("ep-phase-1", body)

    assert set(calls["ran"]) == {
        "mastery",
        "identity",
        "failure",
        "semantic",
        "trajectory",
        "query_lifecycle",
        "consolidate",
        "projection",
        "consolidate_memory",
    }
    assert "reseed" not in calls["ran"]
    assert load_mastery(path=paths["mastery_path"])


def test_per_episode_runner_dispatches_program0(tmp_path, monkeypatch):
    patch_window_deepsleep_paths(monkeypatch, tmp_path)
    from memory.projection.dispatch import projection_dispatch

    requests: list[dict] = []
    monkeypatch.setattr(
        "memory.projection.dispatch.projection_dispatch",
        lambda root, *, reason: requests.append({"root": root, "reason": reason})
        or projection_dispatch(root, reason=reason),
    )

    _run_deepsleep("ep-reindex-1", _learn_body("ep-reindex-1"))

    assert len(requests) == 1
    assert requests[0]["root"] == core_paths.MEMORY_ROOT.parent
    assert requests[0]["reason"] == "deepsleep"
    # DeepSleep enters Program 0 once; the dispatcher owns generation recovery,
    # canonical snapshot construction, and publication.


def test_per_episode_integrity_housekeeping_is_due_aware(monkeypatch):
    import deepsleep_loop.loop as ds_loop

    calls: list[dict] = []

    class _Repair:
        def to_dict(self):
            return {
                "after": {"status": "ok", "issues": [], "dme_parity_status": "ok"},
                "repaired_count": 0,
                "skipped_due_interval": True,
            }

    monkeypatch.setattr(
        "memory.integrity.repair.repair_integrity_issues",
        lambda **kwargs: calls.append(dict(kwargs)) or _Repair(),
    )

    projection = ds_loop._run_memory_integrity_housekeeping(
        context_id="ep-integrity-cached", scope="episode"
    )

    assert calls == [{"if_due": True}]
    assert projection["status"] == "ok"


def test_integrity_failure_keeps_the_episode_open_for_retry(tmp_path, monkeypatch):
    patch_window_deepsleep_paths(monkeypatch, tmp_path)
    import deepsleep_loop.loop as ds_loop
    from memory.episode_sram import open_bucket

    store = EpisodeStore()
    episode = store.open(opened_by="test", pipeline_id=0, work_kind="autonomous")
    open_bucket(episode.episode_id, pipeline_id=episode.pipeline_id)

    monkeypatch.setattr(
        "memory.integrity.repair.repair_integrity_issues",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("repair failed")),
    )

    def integrity_only(*_args, **_kwargs):
        return ds_loop._run_memory_integrity_housekeeping(
            context_id=episode.episode_id,
            scope="episode",
        )

    monkeypatch.setattr(ds_loop, "_run_deepsleep_episode_core", integrity_only)

    with pytest.raises(RequiredSubstrateError) as raised:
        ds_loop.run_deepsleep_for_episode(store)

    assert raised.value.component == "memory_integrity"
    assert raised.value.operation == "repair"
    assert store.current() is episode
    assert episode.state == "open"


def test_per_episode_runner_publishes_after_maintenance(tmp_path, monkeypatch):
    patch_window_deepsleep_paths(monkeypatch, tmp_path)
    import deepsleep_loop.loop as ds_loop
    from memory.taxonomy.canonical_capture import capture_episode_to_canonical

    order: list[str] = []
    captured: list[dict] = []

    def _capture(body, **_kwargs):
        order.append("canonical_projection")
        captured.append(dict(body))
        return capture_episode_to_canonical(body, **_kwargs)

    monkeypatch.setattr(
        "memory.taxonomy.canonical_capture.capture_episode_to_canonical",
        _capture,
    )
    monkeypatch.setattr(
        ds_loop,
        "_run_memory_integrity_housekeeping",
        lambda **_kwargs: order.append("integrity") or {},
    )
    monkeypatch.setattr(
        ds_loop,
        "_run_conflict_reconciliation",
        lambda **_kwargs: order.append("conflict") or {},
    )
    from memory.projection.dispatch import projection_dispatch

    def _dispatch(root, *, reason):
        order.append("program0_publish")
        assert reason == "deepsleep"
        return projection_dispatch(root, reason=reason)

    monkeypatch.setattr("memory.projection.dispatch.projection_dispatch", _dispatch)
    monkeypatch.setattr(
        "memory.pathway_performance.rebuild_pathway_projection",
        lambda: order.append("pathway_projection"),
    )
    original_checkpoint = EpisodeStore.commit_reflection_window_append

    def _checkpoint(self, *args, **kwargs):
        order.append("reflection_checkpoint")
        return original_checkpoint(self, *args, **kwargs)

    monkeypatch.setattr(EpisodeStore, "commit_reflection_window_append", _checkpoint)

    # Keep the real canonical and publication proof while observing their order.
    real_receipt = ds_loop._bind_sealed_record

    def _receipt(context_id, record, **_kwargs):
        order.append("canonical_receipt")
        return real_receipt(context_id, record, **_kwargs)

    monkeypatch.setattr(ds_loop, "_bind_sealed_record", _receipt)

    _run_deepsleep("ep-owned-maintenance", _learn_body("ep-owned-maintenance"))

    assert captured[0]["episode_id"] == "ep-owned-maintenance"
    assert captured[0]["intent"] == "LEARN"
    assert captured[0]["mastery_effects"]
    assert order == [
        "canonical_projection",
        "canonical_receipt",
        "conflict",
        "reflection_checkpoint",
        "pathway_projection",
        "integrity",
        "program0_publish",
    ]


def test_cognition_chain_hands_active_episode_to_deepsleep_terminus(tmp_path, monkeypatch):
    patch_window_deepsleep_paths(monkeypatch, tmp_path)
    order: list[str] = []

    monkeypatch.setattr(cl, "_start_global_a", lambda *_a, **_k: True)

    receipts: dict[str, object] = {}

    def _journal(store, episode, *, cascade_status):
        """Stand in for DeepSleep — including the SRAM drain it owns.

        WO-04 records this episode's admission events into its bucket, so a terminus that only
        closed the store would leave undrained intake behind and `_close_episode_sram` would
        rightly refuse it. The real terminus flushes and clears the bucket as ONE DeepSleep-owned
        operation (`deepsleep_loop.loop._flush_episode_sram` -> `episode_sram.close_bucket`), so
        this simulation performs exactly that, on this episode's own id.
        """
        from memory import episode_sram

        assert store.current() is episode
        order.append("deepsleep")
        receipts["flush"] = episode_sram.close_bucket(episode.episode_id)
        store.close(cascade_status=cascade_status)
        return None

    original_close = EpisodeStore.close

    def _close(self, *, cascade_status="completed"):
        order.append("close")
        return original_close(self, cascade_status=cascade_status)

    monkeypatch.setattr(cl, "_run_per_episode_terminus", _journal)
    monkeypatch.setattr(EpisodeStore, "close", _close)

    store = EpisodeStore(
        reflection_window_path=tmp_path / "reflection_window.json",
        trajectory_path=tmp_path / "current_trajectory.json",
    )

    from pressure_loop.cognition_queue import deserialize_input
    from pressure_loop.contributors import EpisodeIgnition

    c = deserialize_input(
        {
            "source": "autonomous",
            "type": "epistemic_emptiness",
            "weight": 1.0,
            "metadata": {"lane": "agent", "arrival_seq": 1, "cycle_id": "ign"},
            "queue_id": "test-deepsleep-terminus",
            "work_kind": "autonomous",
            "lane": "agent",
        }
    )
    ignition = EpisodeIgnition(
        episode_id="ign",
        aggregate_pressure=1.0,
        opened_by="epistemic_emptiness",
        dominant_contributor=c,
        contributors=(c,),
        trajectory_snapshot={},
        pipeline_id=2,
        work_kind="autonomous",
    )
    cl.ignite_episode(store, ignition)
    assert order == ["deepsleep", "close"]

    # The drain reported an EVENT RECEIPT for every admission event WO-04 captured, and left
    # each journal row PENDING: capture is WO-04's, canonical publication is WO-07's.
    from memory import intake_journal

    flush = receipts["flush"]
    events = list(flush.get("events") or ())
    assert events, f"the terminus drain reported no event receipts: {flush}"
    assert not flush.get("errors"), flush["errors"]
    for receipt in events:
        assert receipt["status"] == intake_journal.PENDING, receipt
        assert receipt["publication"] == "deferred_to_canonical_publication", receipt
        row = intake_journal.record_by_id(receipt["record_id"])
        assert row is not None, receipt
        assert row.status == intake_journal.PENDING, "the journal obligation remains open"
        assert row.memory_id == "" and row.canonical_path == "", (
            "the drain must not claim a captured event was published"
        )


def test_deepsleep_terminus_owns_episode_close(tmp_path, monkeypatch):
    patch_window_deepsleep_paths(monkeypatch, tmp_path)
    import deepsleep_loop.loop as ds_loop
    from memory.episode_sram import open_bucket

    store = EpisodeStore(
        reflection_window_path=tmp_path / "reflection_window.json",
        trajectory_path=tmp_path / "current_trajectory.json",
    )
    episode = store.open(
        pressure={"aggregate_pressure": 1.0},
        opened_by="test",
        pipeline_id=0,
        work_kind="autonomous",
    )
    open_bucket(episode.episode_id, pipeline_id=episode.pipeline_id)
    marker = object()

    def _core(_store, context_id, intake_builder, **kwargs):
        assert _store.current() is episode
        assert episode.state == "closing"
        assert context_id == episode.episode_id
        assert intake_builder().window_id == episode.episode_id
        # Cut 13: the canonical view is no longer built at the call site — it is
        # read INSIDE the commit region, after the stamp, so it can carry one.
        # What the core receives is the carrier itself.
        assert kwargs["closing_episode"] is episode
        assert kwargs["closing_episode"].closed_at
        return marker

    monkeypatch.setattr(ds_loop, "_run_deepsleep_episode_core", _core)

    result = ds_loop.run_deepsleep_for_episode(store, cascade_status="completed")

    assert result is marker
    assert store.current() is None
    assert episode.state == "complete"


# --- 2. Mastery current per-episode ---


def test_mastery_bumps_immediately_after_one_episode(tmp_path, monkeypatch):
    paths = patch_window_deepsleep_paths(monkeypatch, tmp_path)
    body = _learn_body("ep-mastery-1")

    _run_deepsleep("ep-mastery-1", body)

    mastery = load_mastery(path=paths["mastery_path"])
    assert "python" in mastery
    assert mastery["python"].knowledge_mastery == pytest.approx(0.10)
    assert mastery["python"].evidence_count == 1


def test_the_real_terminus_keeps_mastery_only_in_canonical_episode(tmp_path, monkeypatch):
    """The checkpoint carries only the episode id; canonical lobe 9 carries mastery."""
    from pressure_loop.reflection_window import load_reflection_window

    paths = patch_window_deepsleep_paths(monkeypatch, tmp_path)

    pack = _run_deepsleep("ep-delta-region", _learn_body("ep-delta-region"))

    effects = pack.journal_entry["mastery_effects"]
    assert effects  # the phase really did produce one
    window = load_reflection_window(paths["window_path"])
    assert window.episodes == ["ep-delta-region"]
    assert window.episode_mastery_deltas == []

    # WO-09: the checkpoint pins the exact sealed record the member joined as; the mastery
    # delta lives in that canonical record, read by its id (never by scanning for the episode).
    from memory.episode_commit_view import canonical_episode_body

    body, why = canonical_episode_body(window.member_refs["ep-delta-region"]["record_id"])
    assert body is not None, why
    assert body["mastery_effects"] == effects


def test_repository_deepsleep_publishes_through_program0_dispatch(monkeypatch):
    import deepsleep_loop.loop as ds_loop
    from memory.projection.dispatch import DispatchReport

    requests: list[dict] = []
    monkeypatch.setattr(core_paths, "MEMORY_ROOT", core_paths.OMEGA_ROOT / "memory")
    monkeypatch.setattr(
        "memory.projection.dispatch.projection_dispatch",
        lambda root, *, reason: requests.append({"root": root, "reason": reason})
        or DispatchReport(
            owner="program0",
            action="published",
            publication=None,
            drain_status="complete",
            generation_id="generation-1",
        ),
    )

    report = ds_loop._publish_projection_after_deepsleep(
        context_id="ep-default-projection",
        scope="episode",
    )

    assert requests == [{"root": None, "reason": "deepsleep"}]
    assert report.owner == "program0"
    assert report.generation_id == "generation-1"


def test_repository_deepsleep_retries_a_moved_program0_snapshot(monkeypatch):
    import deepsleep_loop.loop as ds_loop
    from memory.projection.dispatch import DispatchReport

    reports = [
        DispatchReport(
            owner="program0", action="failed", publication=None,
            drain_status="snapshot_moved", generation_id=None,
            detail={"reason": "snapshot_moved"},
        ),
        DispatchReport(
            owner="program0", action="published", publication=None,
            drain_status="complete", generation_id="generation-2",
        ),
    ]
    calls = []
    monkeypatch.setattr(
        "memory.projection.dispatch.projection_dispatch",
        lambda root, *, reason: calls.append((root, reason)) or reports.pop(0),
    )
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    report = ds_loop._publish_projection_after_deepsleep(
        context_id="ep-moved-projection", scope="episode"
    )

    assert len(calls) == 2
    assert report.action == "published"
    assert report.generation_id == "generation-2"


def test_repository_deepsleep_waits_for_a_concurrent_publication_authority(monkeypatch):
    import deepsleep_loop.loop as ds_loop
    from memory.projection.dispatch import DispatchReport
    from memory.projection.generation import PublicationAuthorityBusy

    class Clock:
        now = 0.0

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.now += seconds

    clock = Clock()
    attempts = 0

    def dispatch(_root, *, reason):
        nonlocal attempts
        assert reason == "deepsleep"
        attempts += 1
        if attempts == 1:
            clock.now += 11.0
            raise PublicationAuthorityBusy("another lane is publishing")
        if attempts == 2:
            return DispatchReport(
                owner="program0", action="failed", publication=None,
                drain_status="snapshot_moved", generation_id="",
            )
        return DispatchReport(
            owner="program0", action="published", publication=None,
            drain_status="complete", generation_id="generation-after-contention",
        )

    with monkeypatch.context() as patch:
        patch.setattr("time.monotonic", clock.monotonic)
        patch.setattr("time.sleep", clock.sleep)
        patch.setattr("memory.projection.dispatch.projection_dispatch", dispatch)
        report = ds_loop._publish_projection_after_deepsleep(
            context_id="ep-concurrent-projection", scope="episode"
        )

    assert attempts == 3
    assert report.generation_id == "generation-after-contention"


def test_repository_deepsleep_times_out_on_a_stuck_publication_authority(monkeypatch):
    import deepsleep_loop.loop as ds_loop
    from core.required_substrate import RequiredSubstrateError
    from memory.projection.generation import PublicationAuthorityBusy

    class Clock:
        now = 0.0
        sleeps = 0

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.sleeps += 1
            self.now += seconds
            if self.sleeps > 3:
                raise AssertionError("test guard: publication wait did not time out")

    clock = Clock()
    monkeypatch.setattr(ds_loop, "_PUBLICATION_AUTHORITY_WAIT_S", 0.05, raising=False)
    monkeypatch.setattr("time.monotonic", clock.monotonic)
    monkeypatch.setattr("time.sleep", clock.sleep)
    monkeypatch.setattr(
        "memory.projection.dispatch.projection_dispatch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            PublicationAuthorityBusy("another lane is publishing")
        ),
    )

    with pytest.raises(RequiredSubstrateError, match="publication authority wait timed out"):
        ds_loop._publish_projection_after_deepsleep(
            context_id="ep-publication-timeout", scope="episode"
        )


def test_program0_dispatch_failure_propagates(monkeypatch):
    import deepsleep_loop.loop as ds_loop

    def _fail_dispatch(_root, *, reason):
        assert reason == "deepsleep"
        raise RuntimeError("graph rebuild failed")

    monkeypatch.setattr(
        "memory.projection.dispatch.projection_dispatch",
        _fail_dispatch,
    )

    with pytest.raises(RuntimeError, match="graph rebuild failed"):
        ds_loop._publish_projection_after_deepsleep(
            context_id="ep-reindex-failure",
            scope="episode",
        )


def test_deepsleep_canonical_capture_failure_propagates(monkeypatch):
    import deepsleep_loop.loop as ds_loop

    monkeypatch.setattr(
        "memory.taxonomy.canonical_capture.capture_episode_to_canonical",
        lambda _body: (_ for _ in ()).throw(RuntimeError("capture failed")),
    )

    with pytest.raises(RuntimeError, match="capture failed"):
        ds_loop._capture_closed_episode_to_taxonomy("ep-capture-failure", {})


# --- 3. Isolation ---


def test_session_does_not_use_real_deepsleep_mastery_path():
    from pressure_loop.policy import deepsleep_mastery_path

    resolved = deepsleep_mastery_path()
    real = Path("deepsleep_loop/state/mastery.json").resolve()
    assert resolved.resolve() != real


# --- intake builder ---


def test_build_deepsleep_input_for_episode_one_element_window():
    body = _learn_body("ep-intake-1")
    ds_input = build_deepsleep_input_for_episode(body, "ep-intake-1")

    assert ds_input.window_id == "ep-intake-1"
    assert len(ds_input.episode_window) == 1
    assert ds_input.episode_window[0]["episode_id"] == "ep-intake-1"
    assert ds_input.window_reflection == {}
    assert ds_input.trajectory_snapshot == {}
    assert ds_input.omega_reasoning == {}
