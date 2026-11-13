from __future__ import annotations

from types import SimpleNamespace

from memory.taxonomy.meta_recall import recall_skill_context
from memory.taxonomy.schemas import MemoryEvent
from memory.taxonomy.workflow_memory import workflow_topology


def _event(kind: str, payload: dict, created_at: str, event_id: str) -> MemoryEvent:
    return MemoryEvent(
        event_id=event_id,
        event_kind=kind,
        episode_id=payload.get("episode_id", "ep-1"),
        source="test",
        created_at=created_at,
        content_hash=event_id,
        payload=payload,
    )


def _cycle(*, episode_id: str, dispatch_id: str, created_at: str, complete: bool, files: list[str]):
    return [
        _event(
            "skill_execution_pack",
            {
                "episode_id": episode_id,
                "dispatch_id": dispatch_id,
                "skill_name": "python-builder",
                "skill_id": "skill-python-builder",
                "objective": "continue the todo app",
            },
            created_at,
            f"pack-{dispatch_id}",
        ),
        _event(
            "qwen_skill_result",
            {
                "episode_id": episode_id,
                "dispatch_id": dispatch_id,
                "skill_name": "python-builder",
                "skill_id": "skill-python-builder",
                "execution_kind": "WRITE",
                "status": "success",
                "summary": "todo app",
                "files_changed": files,
            },
            created_at,
            f"result-{dispatch_id}",
        ),
        _event(
            "global_b_skill_update",
            {
                "episode_id": episode_id,
                "dispatch_id": dispatch_id,
                "intent_satisfied": complete,
                "objective_complete": complete,
                "more_work_needed": not complete,
                "unresolved_blockers": [] if complete else ["verification_needed"],
            },
            created_at,
            f"global-{dispatch_id}",
        ),
    ]


def test_workflow_memory_requires_authoritative_global_completion():
    events = _cycle(
        episode_id="ep-red",
        dispatch_id="d-red",
        created_at="2026-07-09T23:00:00Z",
        complete=False,
        files=["todo-app/main.py"],
    )

    _, nodes, _ = workflow_topology(events)

    assert len(nodes) == 1
    assert nodes[0]["payload"]["outcome"] == "in_progress"
    assert nodes[0]["payload"]["project_key"] == "todo-app"
    assert nodes[0]["payload"]["objective_complete"] is False


def test_recall_keeps_newer_project_checkpoint_over_old_completion(monkeypatch):
    results = [
        {
            "node_type": "workflow_memory",
            "payload": {
                "outcome": "success",
                "project_key": "todo-app",
                "objective_pattern": "continue the todo app",
                "created_at": "2026-07-09T21:00:00Z",
                "source_event_id": "old-success",
            },
            "evidence_event_ids": ["old-success"],
        },
        {
            "node_type": "workflow_memory",
            "payload": {
                "outcome": "in_progress",
                "project_key": "todo-app",
                "objective_pattern": "continue the todo app",
                "progress_digest": "tests still failing",
                "created_at": "2026-07-09T23:00:00Z",
                "source_event_id": "new-wip",
            },
            "evidence_event_ids": ["new-wip"],
        },
    ]
    monkeypatch.setattr(
        "memory.taxonomy.meta_recall.recall",
        lambda *_args, **_kwargs: SimpleNamespace(
            results=results,
            query_hash="q",
            snapshot_hash="s",
            evidence_event_ids=("old-success", "new-wip"),
        ),
    )

    context = recall_skill_context("continue the todo app")

    assert context["prior_builds"] == []
    assert len(context["build_in_progress"]) == 1
    assert context["build_in_progress"][0]["progress_digest"] == "tests still failing"
