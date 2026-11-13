"""Pure metric fixtures for WO-11 (`build_window_metrics`).

The order's RED seed is a *pure* fixture: four sealed records whose outcome axes are set by
hand, so every population can be asserted exactly. Nothing here reaches memory, and nothing
encodes a person's schedule, mailbox or machine (AGENTS.md section 0).

`record()` builds one strict :class:`EpisodeRecord` through the production schema — the
assessments are assembled so a PERFORMED verification always carries its attribution and at
least one reference, because the contract refuses a verdict without one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from schema.continuity_json import Observation
from schema.episode_record import EpisodeRecord
from tests.helpers.continuity_records import (
    BOOT_ID,
    make_episode_record,
    make_event,
    make_history_bundle,
)

#: Two boot identities: elapsed measurements from different boots are never comparable.
BOOT_A = BOOT_ID
BOOT_B = "boot-00000000000000000000000000000002"

PRODUCER_A = "wo11-a"
PRODUCER_B = "wo11-b"

_PERFORMED = ("passed", "failed", "partial")


def producer(version: str = PRODUCER_A, component: str = "tests.helpers.continuity_metrics") -> dict:
    return {
        "component": component,
        "component_version": version,
        "authority": "host",
    }


def outcome(
    *,
    cascade: str = "completed",
    action: str = "succeeded",
    task: str = "success",
    verification: str = "passed",
    feedback: str = "not_received",
    criteria: str = "acceptance@1",
    artifact: str | None = "artifact@1",
    task_author: str = "host",
    evidence: Sequence[str] = ("evt-0001",),
) -> dict[str, Any]:
    """Five axes plus the attributions the contract requires for what they assert."""
    assessments: dict[str, Any] = {}
    if task not in ("not_applicable", "unknown"):
        assessments["task_outcome"] = {
            "author": task_author,
            "evidence_refs": list(evidence),
            "criteria_revision": criteria,
        }
    if verification in _PERFORMED:
        row: dict[str, Any] = {
            "author": "host",
            "verifier": "pytest",
            "check_refs": list(evidence),
            "criteria_revision": criteria,
        }
        if artifact:
            row["artifact_revision"] = artifact
        assessments["verification_status"] = row
    return {
        "cascade_status": cascade,
        "action_status": action,
        "task_outcome": task,
        "verification_status": verification,
        "feedback_status": feedback,
        "assessments": assessments,
    }


def record(
    episode_id: str,
    *,
    cascade: str = "completed",
    action: str = "succeeded",
    task: str = "success",
    verification: str = "passed",
    feedback: str = "not_received",
    criteria: str = "acceptance@1",
    artifact: str | None = "artifact@1",
    task_author: str = "host",
    thread: Any = "thread-0001",
    job: str | None = None,
    continuation: str | None = None,
    attempt: str | None = "attempt-0001",
    lane: str = "agent",
    work_kind: str = "operator_turn",
    commit_seq: int = 1,
    operations: Sequence[tuple[str, int | None]] | None = None,
    boot: str = BOOT_A,
    producer_version: str = PRODUCER_A,
    unavailable: Sequence[str] = (),
    unrecorded: Sequence[str] = (),
    inaccessible: Sequence[str] = (),
    gap: str = "",
    model_assessment: Mapping[str, Any] | None = None,
) -> EpisodeRecord:
    """One sealed record whose every metric-bearing field the caller controls.

    ``operations`` is a sequence of ``(operation_id, elapsed_ns_or_None)``: one event each, in
    that order, with ``None`` meaning the producer recorded no elapsed measurement.
    """
    ops = list(operations if operations is not None else [(f"op-{episode_id}", 1_250_000)])
    events = tuple(
        make_event(
            episode_id=episode_id,
            thread_id=thread if isinstance(thread, str) else "thread-unknown",
            event_id=f"evt-{episode_id}-{index}",
            operation_id=operation_id,
            event_seq=index + 1,
            elapsed_ns=(Observation.observed(elapsed) if elapsed is not None
                     else Observation.missing("not_captured", reason="no elapsed was recorded")),
            boot_id=boot,
            producer=producer(producer_version),
            evidence_refs=(f"evt-{episode_id}-{index}",),
        )
        for index, (operation_id, elapsed) in enumerate(ops)
    )
    from schema.continuity_json import CanonicalRef

    events_block: dict[str, Any] = {
        "refs": tuple(
            CanonicalRef(record_id=event.event_id, record_version=1,
                         content_sha256=event.content_sha256())
            for event in events
        ),
        "by_value": events,
        "integrity": {event.event_id: event.content_sha256() for event in events},
        "coverage": {
            "retained": len(events),
            "referenced": len(events) + len(tuple(unavailable)),
            "unavailable_refs": tuple(unavailable),
            "reason": "the payload was not retained" if unavailable else "",
        },
    }

    identity: dict[str, Any] = {
        "episode_id": episode_id,
        "thread_id": thread,
        "turn_id": Observation.observed(f"turn-{episode_id}"),
        "continuation_id": (Observation.observed(continuation) if continuation
                            else Observation.missing("not_applicable",
                                                     reason="not a continuation")),
        "job_id": (Observation.observed(job) if job
                   else Observation.missing("not_applicable", reason="no durable job")),
        "attempt_id": (Observation.observed(attempt) if attempt
                       else Observation.missing("not_captured", reason="no attempt was recorded")),
        "pipeline_id": 0,
        "lane": lane,
        "work_kind": work_kind,
    }

    first_event = events[0].event_id if events else "evt-0001"
    built = make_episode_record(
        episode_id=episode_id,
        thread_id=thread if isinstance(thread, str) else "thread-unknown",
        commit_seq=commit_seq,
        outcome=outcome(
            cascade=cascade, action=action, task=task, verification=verification,
            feedback=feedback, criteria=criteria, artifact=artifact, task_author=task_author,
            evidence=(first_event,),
        ),
        objective={
            "original_request": f"the synthetic objective of {episode_id}",
            "acceptance_criteria": ["the synthetic check reports passed"],
            "criteria_revision": criteria,
            "preconditions": [],
        },
        identity=identity,
        events=events_block,
        coverage_overrides={
            "captured": ("admission", "auto", "omega", "deepsleep"),
            "unrecorded": tuple(unrecorded),
            "inaccessible": tuple(inaccessible),
            "redacted": (),
            "causal_gaps": (gap,) if gap else (),
        },
    )
    if model_assessment is not None:
        data = built.to_dict()
        data["metacognition"]["model_assessment"] = dict(model_assessment)
        built = EpisodeRecord.from_dict(data)
    return built


# ── the order's RED seed ─────────────────────────────────────────────────────


def seed_records() -> tuple[EpisodeRecord, ...]:
    """The order's required reproduction: one completed-but-FAILED task, one verified success,
    one unverified claim, one not-applicable idle episode."""
    return (
        record("ep-0001", action="failed", task="failure", verification="failed", commit_seq=1),
        record("ep-0002", task="success", verification="passed", commit_seq=2),
        # The model claims success; no check was performed. It must never enter a verified numerator.
        record("ep-0003", task="success", verification="not_performed", task_author="model",
               commit_seq=3),
        # An idle episode: its cascade completed, but there was no task to succeed at.
        record("ep-0004", action="not_attempted", task="not_applicable",
               verification="not_applicable", commit_seq=4),
    )


def history(**overrides: Any):
    """A history bundle whose audit is honest about a counterexample search nobody ran."""
    return make_history_bundle(**overrides)


def write_metric_golden(name: str, metrics: Any) -> Path:
    """Emit a golden metric fixture with its canonical bytes.

    Run deliberately, never from a test: a golden the suite rewrites proves nothing. The suite
    only ever COMPARES against the committed bytes, so a change in the computation has to be
    seen and re-approved. The generation command is recorded in WO-11's report.
    """
    from core.paths import OMEGA_ROOT
    from schema.continuity_json import canonical_bytes

    document = {"fixture_kind": "window_metrics", "body": metrics.to_dict()}
    path = Path(OMEGA_ROOT) / "tests" / "continuity" / "fixtures" / "metrics" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_bytes(document) + b"\n")
    return path


def rows_by_id(metrics: Any) -> dict[str, Mapping[str, Any]]:
    return {str(row["metric_id"]): row for row in metrics.rows}


def base_ids(metrics: Any) -> set[str]:
    """Metric ids with their stratum suffix removed."""
    return {str(row["metric_id"]).split("@", 1)[0] for row in metrics.rows}


def only(metrics: Any, base: str) -> Mapping[str, Any]:
    """The single row whose base id is *base*; fails loudly when the stratification split it."""
    found = [row for row in metrics.rows if str(row["metric_id"]).split("@", 1)[0] == base]
    assert len(found) == 1, f"expected exactly one {base} row, got {[r['metric_id'] for r in found]}"
    return found[0]


def ids_of(rows: Iterable[Mapping[str, Any]]) -> list[str]:
    return [str(row["metric_id"]) for row in rows]


__all__ = [
    "BOOT_A",
    "BOOT_B",
    "PRODUCER_A",
    "PRODUCER_B",
    "base_ids",
    "history",
    "ids_of",
    "only",
    "outcome",
    "producer",
    "record",
    "rows_by_id",
    "seed_records",
    "write_metric_golden",
]
