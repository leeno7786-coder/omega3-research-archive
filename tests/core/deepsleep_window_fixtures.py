"""Shared window-level DeepSleepInput builders and EpisodeStore helpers for core tests."""

from __future__ import annotations

from deepsleep_loop._deepsleep_state import DeepSleepInput
from deepsleep_loop.loop import (
    _run_memory_integrity_housekeeping as _real_run_memory_integrity_housekeeping,
)
from pressure_loop.episode import EpisodeStore
from pressure_loop.reflection_window import WINDOW_SIZE


def fixture_omega_result(
    *,
    intent: str,
    target: str,
    success: bool,
    knowledge_mastery: float | None,
    application_mastery: float,
    knowledge_products: list[dict] | None,
) -> dict | None:
    if intent not in ("LEARN", "USE_TOOL") or not success:
        return None
    km = knowledge_mastery if knowledge_mastery is not None else (0.5 if intent == "LEARN" else 0.0)
    return {
        "cognized": True,
        "knowledge_to_ingest": list(knowledge_products or []),
        "mastery_effects": [
            {
                "domain": target,
                "knowledge_mastery": km,
                "application_mastery": application_mastery,
                "rationale": "fixture",
            }
        ],
        "pressure_params": {},
        "response_text": None,
        "synthesis": None,
        "reflection": None,
    }


def producer_outcome_axes(*, success: bool | None, cascade_status: str = "completed") -> dict:
    """The five outcome axes the WO-06 producer records for a Meta B success or failure verdict.

    Read from the producer's own table, so a fixture cannot invent an axis combination the live
    path never writes. No host check ran, and the operator has not spoken. ``success=None`` is
    the producer's answer when Meta B recorded no verdict at all: action and task are unknown.
    """
    from pressure_loop.episode_journal import _RESULT_AXES
    from schema.episode_record import EpisodeOutcome

    if success is None:
        action_status, task_outcome = "unknown", "unknown"
    else:
        action_status, task_outcome = _RESULT_AXES["success" if success else "failure"]
    return EpisodeOutcome(
        cascade_status=cascade_status,
        action_status=action_status,
        task_outcome=task_outcome,
        verification_status="not_performed",
        feedback_status="not_received",
    ).to_dict()


def episode_entry(
    episode_id: str,
    *,
    intent: str = "LEARN",
    target: str = "Python",
    success: bool = True,
    action: str = "web_fetch",
    failure_kind: str | None = None,
    outputs: dict | None = None,
    knowledge_mastery: float | None = None,
    application_mastery: float = 0.0,
    knowledge_products: list[dict] | None = None,
) -> dict:
    compat_status = "completed" if success else "failed"
    exec_status = "success" if success else "failure"
    outcome_inner: dict = {
        "intent": intent,
        "status": compat_status,
        "goal_state": compat_status,
        "execution_outcome": {
            "executed": True,
            "status": exec_status,
            "failure_reason": failure_kind if not success else None,
        },
    }
    if failure_kind:
        outcome_inner["failure_kind"] = failure_kind
    entry: dict = {
        "episode_id": episode_id,
        "ignition_reason": "test",
        "intent": intent,
        "action": action,
        "execution": {
            "execution_action_pack": {
                "action": action,
                "execution_inputs": {"query": target, "domain": target},
            }
        },
        "outcome": {
            "outcome": outcome_inner,
        },
        "outcome_axes": producer_outcome_axes(success=success),
        "trajectory_update": None,
    }
    if outputs is not None:
        entry["outputs"] = dict(outputs)
    omega_result = fixture_omega_result(
        intent=intent,
        target=target,
        success=success,
        knowledge_mastery=knowledge_mastery,
        application_mastery=application_mastery,
        knowledge_products=knowledge_products,
    )
    if omega_result is not None:
        entry["omega_result"] = omega_result
    return entry


def meta_b_reflection_for(body: dict) -> dict:
    """The Meta B reflection a carrier needs so the real producer records the body's task axis.

    The terminus rebuilds the canonical body from the carrier, deriving ``outcome_axes`` from
    Meta B's execution verdict (WO-06). A carrier with no verdict yields ``task_outcome=unknown``,
    which earns no credit, so a carrier dressed from a hand-built body must carry the verdict
    that body's axes describe.
    """
    task = str((body.get("outcome_axes") or {}).get("task_outcome") or "")
    # Meta B's own (result, status, disposition) per verdict: meta_loop/reflect/run.py with the
    # cycle_kind_map in config/meta_b.yaml.
    verdict = {"success": ("success", "success", "completed"),
               "failure": ("failure", "failed", "failed"),
               "partial": ("partial", "partial", "degraded")}.get(task)
    if verdict is None:
        return {}
    result, status, disposition = verdict
    return {"reflection": {"execution": {"action": str(body.get("action") or ""),
                                         "executed": True, "result": result, "status": status,
                                         "disposition": disposition}}}


def active_store_from_episode_body(body: dict) -> tuple[EpisodeStore, object]:
    """Build the active carrier required by the real DeepSleep terminus."""
    episode_id = str(body.get("episode_id") or "test-episode")
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
    return store, episode


def window_input(
    window_id: str = "window-test",
    *,
    episodes: list[dict] | None = None,
    reflection: dict | None = None,
    trajectory_update: dict | None = None,
    query_resolutions: list[dict] | None = None,
    trajectory_snapshot: dict | None = None,
) -> DeepSleepInput:
    eps = list(episodes or [])
    domain = ""
    if eps:
        execution = eps[0].get("execution") or {}
        pack = execution.get("execution_action_pack") or {}
        inputs = pack.get("execution_inputs") or {}
        if isinstance(inputs, dict):
            domain = str(inputs.get("domain") or inputs.get("query") or "")
    window_reflection: dict = {}
    if reflection is not None:
        window_reflection["reflection"] = dict(reflection)
    if trajectory_update is not None:
        window_reflection["trajectory_update"] = dict(trajectory_update)
    if query_resolutions is not None:
        window_reflection["query_resolutions"] = list(query_resolutions)
    snapshot = dict(trajectory_snapshot or {})
    if domain and "target_domain" not in snapshot:
        snapshot["target_domain"] = domain
    reasoning = dict((reflection or {}).get("reasoning") or {})
    return DeepSleepInput(
        window_id=window_id,
        window_reflection=window_reflection,
        episode_window=eps,
        trajectory_snapshot=snapshot,
        omega_reasoning=reasoning,
    )


def learn_input(target: str = "Python", *, success: bool = True) -> DeepSleepInput:
    return window_input(
        window_id="ep-learn",
        episodes=[episode_entry("ep-learn", intent="LEARN", target=target, success=success)],
        reflection={"trajectory": {"target_domain": target}},
    )


def use_tool_input(target: str = "Python", *, success: bool = True) -> DeepSleepInput:
    return window_input(
        window_id="ep-use",
        episodes=[
            episode_entry(
                "ep-use",
                intent="USE_TOOL",
                target=target,
                success=success,
                action="run_code",
            )
        ],
        reflection={"trajectory": {"target_domain": target}},
    )


def trajectory_input(
    episode_id: str,
    *,
    domain: str,
    capability: str = "foundational understanding",
    confidence: float = 0.8,
) -> DeepSleepInput:
    return window_input(
        window_id=episode_id,
        episodes=[episode_entry(episode_id, intent="LEARN", target=domain)],
        trajectory_update={
            "target_domain": domain,
            "target_capability": capability,
            "confidence": confidence,
            "rationale": "test",
            "supporting_episode": episode_id,
        },
    )


def query_input(
    window_id: str,
    *,
    questions: list[dict] | None = None,
    query_resolutions: list[dict] | None = None,
) -> DeepSleepInput:
    reflection: dict = {"open_questions": questions or []}
    return window_input(
        window_id=window_id,
        episodes=[episode_entry(window_id, intent="LEARN", target="quantum_physics")],
        reflection=reflection,
        query_resolutions=query_resolutions,
    )


def minimal_input() -> DeepSleepInput:
    return window_input(
        window_id="ep-ds-1",
        episodes=[
            episode_entry(
                "ep-ds-1",
                intent="LEARN",
                target="quantum",
                action="web_fetch",
            )
        ],
        reflection={
            "reasoning": {"what": "done"},
            "trajectory": {"target_domain": "quantum"},
        },
    )


def patch_window_deepsleep_paths(monkeypatch, tmp_path):
    """Patch reflection window + DeepSleep store paths for integration tests."""
    window_path = tmp_path / "reflection_window.json"
    traj_path = tmp_path / "current_trajectory.json"
    journal_path = tmp_path / "episode_journal.jsonl"
    deepsleep_journal = tmp_path / "deepsleep_journal.jsonl"
    mastery_path = tmp_path / "mastery.json"
    query_path = tmp_path / "open_queries.json"
    semantic_path = tmp_path / "semantic.json"
    identity_path = tmp_path / "identity.json"

    monkeypatch.setattr(
        "pressure_loop.reflection_window.reflection_window_path",
        lambda: window_path,
    )
    monkeypatch.setattr(
        "pressure_loop.reflection_window.current_trajectory_path",
        lambda: traj_path,
    )
    monkeypatch.setattr(
        "pressure_loop.policy.episode_journal_path",
        lambda policy=None: journal_path,
    )
    monkeypatch.setattr(
        "pressure_loop.policy.deepsleep_journal_path",
        lambda policy=None: deepsleep_journal,
    )
    monkeypatch.setattr(
        "pressure_loop.policy.deepsleep_mastery_path",
        lambda policy=None: mastery_path,
    )
    monkeypatch.setattr(
        "pressure_loop.policy.deepsleep_query_path",
        lambda policy=None: query_path,
    )
    monkeypatch.setattr(
        "pressure_loop.policy.deepsleep_semantic_path",
        lambda policy=None: semantic_path,
    )
    monkeypatch.setattr(
        "pressure_loop.policy.deepsleep_identity_path",
        lambda policy=None: identity_path,
    )
    projection_path = tmp_path / "projection.json"
    monkeypatch.setattr(
        "pressure_loop.policy.deepsleep_projection_path",
        lambda policy=None: projection_path,
    )
    failure_path = tmp_path / "failure.json"
    monkeypatch.setattr(
        "pressure_loop.policy.deepsleep_failure_path",
        lambda policy=None: failure_path,
    )
    monkeypatch.setattr(
        "pressure_loop.policy.deepsleep_trajectory_path",
        lambda policy=None: tmp_path / "trajectory.json",
    )
    trajectory_path = tmp_path / "trajectory.json"
    # Isolate the DeepSleep contradiction-candidate queue: the conflict_reconciliation phase drains
    # it, and redirect_memory_to_tmp does NOT cover this path. Without this, a core test would drain
    # the real queue + touch real memory.
    monkeypatch.setattr(
        "memory.queues.DEEPSLEEP_QUEUE_PATH",
        tmp_path / "deepsleep_candidates.jsonl",
        raising=False,
    )
    monkeypatch.setattr(
        "deepsleep_loop.loop._run_memory_integrity_housekeeping",
        lambda context_id="window", scope="window", **_kwargs: (
            _real_run_memory_integrity_housekeeping(
                context_id=str(context_id),
                scope=str(scope),
            )
        ),
    )
    return {
        "window_path": window_path,
        "traj_path": traj_path,
        "journal_path": journal_path,
        "deepsleep_journal": deepsleep_journal,
        "mastery_path": mastery_path,
        "query_path": query_path,
        "semantic_path": semantic_path,
        "identity_path": identity_path,
        "projection_path": projection_path,
        "failure_path": failure_path,
        "trajectory_path": trajectory_path,
    }


def episode_on_store(
    store: EpisodeStore,
    *,
    intent: str,
    target: str,
    success: bool = True,
    action: str = "web_fetch",
    failure_kind: str | None = None,
    knowledge_mastery: float | None = None,
    application_mastery: float = 0.0,
    knowledge_products: list[dict] | None = None,
) -> str:
    compat_status = "completed" if success else "failed"
    exec_status = "success" if success else "failure"
    outcome_inner: dict = {
        "intent": intent,
        "status": compat_status,
        "goal_state": compat_status,
        "execution_outcome": {
            "executed": True,
            "status": exec_status,
            "failure_reason": failure_kind if not success else None,
        },
    }
    if failure_kind:
        outcome_inner["failure_kind"] = failure_kind
    ep = store.open(
        opened_by="curiosity_gap", pressure={}, pipeline_id=0, work_kind="autonomous"
    )
    assert ep is not None
    global_: dict = {
        "intent_decision": {
            "episode_id": ep.episode_id,
            "intent": intent,
            "confidence": 0.9,
            "reason": "test",
        },
        "outcome": {
            "outcome": outcome_inner,
        },
    }
    omega_result = fixture_omega_result(
        intent=intent,
        target=target,
        success=success,
        knowledge_mastery=knowledge_mastery,
        application_mastery=application_mastery,
        knowledge_products=knowledge_products,
    )
    if omega_result is not None:
        global_["omega_result"] = omega_result
    ep.global_ = global_
    ep.meta = {
        "execution_action_pack": {
            "action": action,
            "execution_inputs": {"query": target, "domain": target},
        }
    }
    auto: dict = {"status": exec_status, "cycle_kind": "failure" if not success else exec_status}
    if failure_kind:
        auto["failure_kind"] = failure_kind
    ep.auto = auto
    ep.omega = {"reflection": {}, "trajectory_update": {}}
    episode_id = ep.episode_id
    if store.is_tenth_episode_window_reflection():
        store.stage_episode_for_window_reflection(ep, cascade_status="completed")
    store.close()
    return episode_id


def fill_locked_window(
    store: EpisodeStore,
    *,
    lead_intent: str = "IDLE",
    lead_target: str = "Python",
    lead_count: int = 0,
    intents: list[tuple[str, str]] | None = None,
    knowledge_mastery: float | None = None,
    application_mastery: float = 0.0,
    knowledge_products: list[dict] | None = None,
):
    mastery_kwargs = {
        "knowledge_mastery": knowledge_mastery,
        "application_mastery": application_mastery,
        "knowledge_products": knowledge_products,
    }
    if intents is not None:
        assert len(intents) == WINDOW_SIZE
        for intent, target in intents:
            episode_on_store(store, intent=intent, target=target, **mastery_kwargs)
    else:
        for i in range(WINDOW_SIZE):
            if i < lead_count:
                episode_on_store(
                    store,
                    intent=lead_intent,
                    target=lead_target,
                    **mastery_kwargs,
                )
            else:
                episode_on_store(store, intent="IDLE", target=lead_target)
    window = store.get_locked_window()
    assert window is not None
    return window


def lock_window_with_idle_padding(
    store: EpisodeStore,
    *,
    target: str = "Python",
) -> object:
    """Pad an in-progress window to WINDOW_SIZE with IDLE episodes."""
    for _ in range(WINDOW_SIZE - 1):
        episode_on_store(store, intent="IDLE", target=target)
    window = store.get_locked_window()
    assert window is not None
    return window


def omega_for_window(window, *, target: str = "Python", **reflection_extra) -> dict:
    reflection = {"trajectory": {"target_domain": target}}
    reflection.update(reflection_extra)
    return {
        "window_id": window.window_id,
        "reflection": reflection,
        "trajectory_update": {"target_domain": target},
        "trajectory_diff": "PARTIAL_FOLLOWING",
    }
