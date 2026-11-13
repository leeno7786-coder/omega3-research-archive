"""Real histories for the WO-10 suites: host evidence produced by the real producers.

Every episode here closes through the real DeepSleep terminus, so what history reads back is
what the commit region sealed, drained and published — never a reader-shaped dictionary. The
evidence each relation rests on is produced by the component that owns it in production:

* ``admission.origin`` / ``admission.episode_opened`` — ``cognition_chain._admit_episode``, the
  real admission seam, over a real durable queue item and a real ignition;
* the ``continued`` causal fact — ``cognition_chain._record_continuation_fact``;
* ``auto.input_exposure`` — ``auto_loop.mril_tools.run.run_forced_memory_tools`` (only the
  per-lane recall read is stubbed, exactly as WO-05's exposure suite stubs it);
* artifact and verification receipts — ``memory.auto_evidence``;
* ``window.manifest_admitted`` — ``pressure_loop.window_lane.restore_window``.

The one exception is operator feedback. No production component records
``operator_feedback_received`` yet (the contract fixture defines its shape; ``feedback_status``
producers are later orders'), so :func:`record_feedback` records it through the real
``record_event`` API inside a real episode, which then seals and publishes it like any other
event. Stated here so no test reads as proof that a feedback producer exists.
"""

from __future__ import annotations

import importlib
import threading
from typing import Any, Callable, Sequence

import pytest

from tests.helpers import continuity_terminus as ct
from tests.helpers import continuity_window as cw


def history():
    """The WO-10 module, or a failure that names what is missing."""
    try:
        return importlib.import_module("omega_loop.window.history")
    except ModuleNotFoundError:
        pytest.fail(
            "omega_loop.window.history does not exist: nothing collects pinned history, typed "
            "host relations or a reproducible retrieval audit for a sealed window"
        )


def limits(*, record_cap: int = 32, token_candidate_cap: int = 8192):
    from schema.window_evidence import HistoryLimits

    return HistoryLimits(
        record_cap=record_cap,
        token_candidate_cap=token_candidate_cap,
        retrieval_policy_revision=history().HISTORY_POLICY_REVISION,
    )


def seal_staged():
    """Seal the one staged cohort against the served projection; return (window, manifest, records)."""
    m = cw.membership()
    staged = cw.staged_cohort()
    manifest = m.seal_window(staged, snapshot=m.window_snapshot(staged))
    return staged, manifest, m.hydrate_window(manifest)


def collect(manifest, records, **limit_overrides):
    return history().collect_history(manifest, records, limits=limits(**limit_overrides))


# ── producers ────────────────────────────────────────────────────────────────


def open_carrier(store, *, pipeline_id: int = 0) -> Any:
    """A real carrier whose memory epoch is opened as the ignition seam opens it.

    ``continuity_terminus.open_carrier`` skips ``open_episode_epoch``, so its epoch starts at 0
    and its sealed ``concurrent_commits`` lists every earlier episode. History reads that set
    as host evidence, so these suites open the epoch exactly where production does.
    """
    from memory.epoch import open_episode_epoch

    carrier = ct.open_carrier(store, pipeline_id=pipeline_id)
    open_episode_epoch(carrier)
    return carrier


def close_learn(store, *, pipeline_id: int = 0, domain: str = "quantum_physics") -> Any:
    carrier = ct.dress_learn(open_carrier(store, pipeline_id=pipeline_id), domain=domain)
    ct.close(store)
    return carrier


def close_members(store, count: int) -> list[Any]:
    return [close_learn(store) for _ in range(count)]


def close_with(store, produce: Callable[[Any], None], *, pipeline_id: int = 0,
               kind: str = "success") -> Any:
    """Open a real carrier, let *produce* record host evidence into it, dress and close it."""
    carrier = open_carrier(store, pipeline_id=pipeline_id)
    produce(carrier)
    if kind == "failure":
        ct.dress_failed_build(carrier)
    else:
        ct.dress_learn(carrier)
    ct.close(store)
    return carrier


def record_feedback(carrier, *, about_episode_id: str, feedback: str, statement: str) -> Any:
    """Operator feedback about an earlier episode, recorded through the real event API."""
    from memory.episode_events import record_event

    return record_event(
        carrier.episode_id,
        operation_id=f"feedback:{carrier.episode_id}:{about_episode_id}",
        phase="admission",
        event_kind="operator_feedback_received",
        payload={
            "about_episode_id": about_episode_id,
            "feedback": feedback,
            "received_after_close": True,
            "statement": statement,
        },
        evidence_class="operator_statement",
    )


def overlapping_pair(store, *, first_pipeline: int = 0, second_pipeline: int = 1):
    """A opens first, B opens second and COMMITS FIRST, then A commits: reverse commit order."""
    carriers: dict[str, Any] = {}
    a_open, b_done = threading.Event(), threading.Event()
    errors: list[BaseException] = []

    def lane_a() -> None:
        try:
            carriers["a"] = ct.dress_learn(open_carrier(store, pipeline_id=first_pipeline))
            a_open.set()
            assert b_done.wait(timeout=120), "B never committed"
            ct.close(store)
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)
            a_open.set()

    def lane_b() -> None:
        try:
            assert a_open.wait(timeout=120), "A never opened"
            carriers["b"] = ct.dress_learn(open_carrier(store, pipeline_id=second_pipeline))
            ct.close(store)
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)
        finally:
            b_done.set()

    threads = [threading.Thread(target=lane_a), threading.Thread(target=lane_b)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=240)
    assert not errors, errors
    return carriers["a"], carriers["b"]


def admit(store, *, metadata: dict, trajectory: dict, pipeline_id: int = 0,
          contributor_source: str = "operator", contributor_type: str = "operator_query",
          work_kind: str = "operator_turn") -> Any:
    """Open a carrier through the REAL admission seam over a real durable queue item."""
    import cognition_chain as chain
    from core.cognition_lock import pipeline_cognition_lock
    from pressure_loop import cognition_queue as cq
    from pressure_loop.contributors import EpisodeIgnition, make_contributor

    contributor = make_contributor(contributor_source, contributor_type, 1.0, dict(metadata))
    assert cq.enqueue_input(contributor, operator_activity=False)
    leased = cq.lease_next_input(pipeline_id)
    assert leased is not None, "the fixture's own row must be leasable"
    ignition = EpisodeIgnition(
        episode_id=f"ig-{leased.metadata.get('cycle_id')}",
        aggregate_pressure=1.0,
        opened_by=leased.type,
        dominant_contributor=leased,
        contributors=(leased,),
        trajectory_snapshot=dict(trajectory),
        pipeline_id=pipeline_id,
        work_kind=work_kind,
    )
    lock = pipeline_cognition_lock(pipeline_id)
    lock.acquire()
    try:
        episode, _ignition = chain._admit_episode(store, ignition)
    finally:
        lock.release()
    return episode


def admit_queued(store, *, pipeline_id: int = 0, work_kind: str = "operator_turn") -> Any:
    """Admit the contributor ALREADY waiting in the durable queue, through the real seam.

    Used where a production component enqueued the turn itself (an operator reply to a system
    message, for instance), so the test admits what the host queued rather than minting its own.
    """
    import cognition_chain as chain
    from core.cognition_lock import pipeline_cognition_lock
    from pressure_loop import cognition_queue as cq
    from pressure_loop.contributors import EpisodeIgnition

    leased = cq.lease_next_input(pipeline_id)
    assert leased is not None, "nothing is queued to admit"
    ignition = EpisodeIgnition(
        episode_id=f"ig-{leased.metadata.get('cycle_id')}",
        aggregate_pressure=1.0,
        opened_by=leased.type,
        dominant_contributor=leased,
        contributors=(leased,),
        trajectory_snapshot={},
        pipeline_id=pipeline_id,
        work_kind=work_kind,
    )
    lock = pipeline_cognition_lock(pipeline_id)
    lock.acquire()
    try:
        episode, _ignition = chain._admit_episode(store, ignition)
    finally:
        lock.release()
    return episode


def admit_and_close(store, *, trajectory: dict, cycle: str, arrival: int,
                    thread: str = "thread-wo10-history", pipeline_id: int = 0) -> Any:
    carrier = admit(
        store,
        metadata={"message": f"history request {cycle}", "cycle_id": cycle,
                  "arrival_seq": arrival, "thread_id": thread},
        trajectory=trajectory,
        pipeline_id=pipeline_id,
    )
    ct.dress_learn(carrier)
    ct.close(store)
    return carrier


def trajectory(tag: str, *, operator: bool) -> dict:
    """A trajectory snapshot as an ignition carries it. Operator-set ones come from onboarding."""
    return {"trajectory_id": f"traj-{tag}", "cycle_id": "onboarding" if operator else f"window-{tag}",
            "target_domain": "quantum_physics", "target_capability": tag}


def recall_with_exposure(carrier, lane_results: Sequence[dict], monkeypatch) -> Any:
    """Run the real forced-recall runner with the per-lane reads stubbed."""
    import auto_loop.mril_tools.run as mril_run

    queue = list(lane_results)
    monkeypatch.setattr(mril_run, "invoke_remember_tool", lambda spec, episode_id, **_k: queue.pop(0))
    pack = {
        "memory_required": True,
        "memory_reason": "prior work on the parser",
        "memory_query": "parser acceptance test",
        "memory_tools": [{"tool": "remember_knowledge"} for _ in lane_results],
    }
    return mril_run.run_forced_memory_tools(pack, carrier.episode_id)


def lane_result(*evidence: tuple[str, str, float]) -> dict:
    return {
        "tool": "remember_knowledge",
        "lobe": 2,
        "verdict": "verified",
        "evidence": [{"memory_id": memory_id, "remembered_as": text, "confidence": confidence}
                     for memory_id, text, confidence in evidence],
    }


def links_of(bundle, kind: str) -> list[dict]:
    return [dict(row) for row in bundle.links if row["relation_kind"] == kind]
