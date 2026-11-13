"""MRIL recall for an update routed to an active Coding Agent run.

The caller supplies host-owned identities — the job, the conversation thread and the
instruction revision it is acting on — never the operator's wording as a job reference.
The run anchor is resolved exactly (``memory.run_anchor``); only then does recall run, and
it reads only that run's canonical coding/dev records (``memory.run_scoped_recall``).

The envelope carries canonical memory IDs, bounded excerpts, source citations and source
provenance for each recalled record. Excerpts are bounded so no agent transcript is copied
into Chat. Anchor and retrieval failures propagate as their own errors with causes attached;
a run with nothing to recall returns ``status == "empty"``.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable

from memory.run_anchor import resolve_run_anchor
from memory.run_scoped_recall import MAX_EXCERPT_CHARS, recall_run_scoped

__all__ = ["MAX_EXCERPT_CHARS", "recall_for_active_run"]


def _run_query(operator_update: str, anchor: Any) -> str:
    """The operator's update plus the host-owned run anchors, as one recall query."""
    return "\n".join(
        (
            str(operator_update or "").strip(),
            f"job {anchor.job_id}",
            f"project {anchor.project_root}",
            f"thread {anchor.thread_id}",
            f"instruction revision {anchor.instruction_revision}",
        )
    )


def recall_for_active_run(
    *,
    job_id: Any,
    thread_id: Any,
    instruction_revision: Any,
    operator_update: str,
    allowed_lobes: Iterable[Any],
    limit: int,
) -> Dict[str, Any]:
    """Resolve the exact active run, then recall its own memories with provenance."""
    anchor = resolve_run_anchor(
        job_id=job_id, thread_id=thread_id, instruction_revision=instruction_revision
    )
    lobes = tuple(allowed_lobes) if allowed_lobes is not None else None
    query = _run_query(operator_update, anchor)
    result = recall_run_scoped(anchor, query, allowed_lobes=lobes, limit=limit)
    return {
        "kind": "run_scoped_recall",
        "tool": "remember_active_run",
        "status": result.status,
        "anchor": anchor.as_dict(),
        "query": query,
        "allowed_lobes": list(lobes or ()),
        "evidence": [
            {
                "memory_id": record.memory_id,
                "coord": list(record.coord),
                "title": record.title,
                "excerpt": str(record.excerpt or "")[:MAX_EXCERPT_CHARS],
                "source_citation": dict(record.source_citation),
                "source_provenance": dict(record.source_provenance),
                "run_provenance": dict(record.run_provenance),
                "score": float(record.score),
            }
            for record in result.records
        ],
    }
