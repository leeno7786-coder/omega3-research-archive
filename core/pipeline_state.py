"""Durable recovery envelopes for four physical workers.

READY must land before queue acknowledgement. Restore enqueues before clearing
the envelope, so a crash at either boundary retains the exact queue identity.
Failed persistence always raises; memory never advertises an uncommitted write.
"""

from __future__ import annotations

import copy
import json
import os
import threading
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

from core.pipelines import PIPELINE_IDS, validate_pipeline_id

STATES = frozenset({"IDLE", "READY", "INFERENCE", "WAITING_TOOL", "WAITING_IO", "COMMITTING", "COMPLETE", "FAILED"})
RESTORABLE_STATES = frozenset({"READY", "INFERENCE", "WAITING_TOOL", "WAITING_IO"})


class PipelineEnvelopeUnreadable(RuntimeError):
    """Recovery cannot distinguish an unreadable record from owed work."""


@dataclass(frozen=True)
class PipelineEnvelope:
    pipeline_id: int
    queue_id: str = ""
    cycle_id: str = ""
    job_id: str = ""
    work_kind: str = "autonomous"
    # The operator turn this pipeline's work descends from, kept through foldback: a
    # continuation's own ``cycle_id`` is its successor id, and ``mark_acted`` drops the
    # continuation that carried the root lineage, so nothing else names it while the fold runs
    # (cross-lane spec section 11, T12 — the durable stop consults it).
    origin_cycle_id: str = ""
    state: str = "IDLE"
    phase: str = ""
    active_episode_id: str = ""
    continuation: dict | None = None

    def __post_init__(self):
        validate_pipeline_id(self.pipeline_id)
        if self.state not in STATES:
            raise ValueError(f"unknown pipeline state {self.state!r}")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict) -> PipelineEnvelope:
        if isinstance(payload, dict):
            # An envelope written before the stop doctrine records no root lineage (T12).
            payload = {"origin_cycle_id": "", **payload}
        if not isinstance(payload, dict) or set(payload) != {f.name for f in fields(cls)}:
            raise ValueError("pipeline envelope fields incomplete or unknown")
        envelope = cls(**payload)
        if envelope.state == "READY" and envelope.continuation is None:
            raise ValueError("READY pipeline has no recovery continuation")
        if envelope.continuation is not None:
            contributor = contributor_from_continuation(envelope.continuation)
            if contributor.queue_id != envelope.queue_id:
                raise ValueError("pipeline continuation queue identity mismatch")
        return envelope


_LOCKS = {pid: threading.RLock() for pid in PIPELINE_IDS}
_CACHE: dict[int, PipelineEnvelope] = {}


def pipelines_dir() -> Path:
    from core.paths import GUARDIAN_STATE_ROOT

    return GUARDIAN_STATE_ROOT / "pipelines"


def envelope_path(pipeline_id: int) -> Path:
    return pipelines_dir() / f"pipeline_{validate_pipeline_id(pipeline_id)}.json"


def read_envelope(pipeline_id: int) -> PipelineEnvelope:
    pid = validate_pipeline_id(pipeline_id)
    with _LOCKS[pid]:
        if pid not in _CACHE:
            path = envelope_path(pid)
            if not path.exists():
                envelope = PipelineEnvelope(pid)
            else:
                try:
                    envelope = PipelineEnvelope.from_dict(json.loads(path.read_text(encoding="utf-8")))
                    if envelope.pipeline_id != pid:
                        raise ValueError("pipeline file ownership mismatch")
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    raise PipelineEnvelopeUnreadable(f"pipeline envelope unreadable: {path}") from exc
            _CACHE[pid] = envelope
        return copy.deepcopy(_CACHE[pid])


def envelopes() -> tuple[PipelineEnvelope, ...]:
    return tuple(read_envelope(pid) for pid in PIPELINE_IDS)


def active_operator_turns(exclude_cycle_id: str = "") -> tuple[dict[str, object], ...]:
    rows = []
    for envelope in envelopes():
        if (envelope.state in {"IDLE", "COMPLETE", "FAILED"} or not envelope.cycle_id
            or envelope.cycle_id == exclude_cycle_id or envelope.work_kind != "operator_turn"):
            continue
        continuation = envelope.continuation or {}
        metadata = (continuation.get("contributor") or {}).get("metadata") or {}
        rows.append({"cycle_id": envelope.cycle_id, "job_id": envelope.job_id,
                     "work_kind": envelope.work_kind, "status": envelope.state,
                     "objective": str(metadata.get("message") or metadata.get("objective") or "")[:500]})
    return tuple(rows)


def bind_job(pipeline_id: int, cycle_id: str, job_id: str) -> PipelineEnvelope:
    with _LOCKS[validate_pipeline_id(pipeline_id)]:
        envelope = read_envelope(pipeline_id)
        if envelope.cycle_id != cycle_id or envelope.state not in RESTORABLE_STATES:
            raise ValueError("job binding does not own active pipeline")
        continuation = copy.deepcopy(envelope.continuation)
        if continuation is not None:
            continuation["contributor"]["metadata"]["job_id"] = job_id
        return _write(pipeline_id, job_id=job_id, continuation=continuation)


def _persist(envelope: PipelineEnvelope) -> None:
    path = envelope_path(envelope.pipeline_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as stream:
        json.dump(envelope.to_dict(), stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def _write(pipeline_id: int, **changes) -> PipelineEnvelope:
    pid = validate_pipeline_id(pipeline_id)
    with _LOCKS[pid]:
        updated = replace(read_envelope(pid), **changes)
        _publish(updated)
        return updated


def _publish(envelope: PipelineEnvelope) -> None:
    """Publish only after success; uncertain replace outcomes must reread disk."""
    try:
        _persist(envelope)
    except BaseException:
        _CACHE.pop(envelope.pipeline_id, None)
        raise
    _CACHE[envelope.pipeline_id] = copy.deepcopy(envelope)


def contributor_from_continuation(continuation: dict):
    from pressure_loop.cognition_queue import deserialize_input

    row = dict(continuation["contributor"])
    if "work_kind" in continuation:
        row["work_kind"] = continuation["work_kind"]
    return deserialize_input(row)


def _ignition_fields(ignition: object) -> dict:
    from pressure_loop.cognition_queue import deserialize_input, serialize_input

    row = serialize_input(ignition.dominant_contributor)
    # Validate before READY can be claimed as durable recovery evidence.
    contributor = deserialize_input(row)
    metadata = contributor.metadata
    return {
        "queue_id": contributor.queue_id,
        "cycle_id": str(metadata.get("cycle_id") or ""),
        # A successor's cycle_id is its own identity; the turn it continues is the lineage,
        # and that is what an operator's stop names (T12).
        "origin_cycle_id": str(metadata.get("original_cycle_id") or metadata.get("cycle_id") or ""),
        "job_id": str(metadata.get("job_id") or ""),
        "work_kind": str(ignition.work_kind),
        "continuation": {
            "contributor": row,
            "ignition_id": str(ignition.episode_id),
            "opened_by": str(ignition.opened_by),
            "work_kind": str(ignition.work_kind),
        },
    }


def mark_ready(pipeline_id: int, ignition: object) -> PipelineEnvelope:
    pid = validate_pipeline_id(pipeline_id)
    if validate_pipeline_id(ignition.pipeline_id) != pid:
        raise ValueError("ignition pipeline binding mismatch")
    return _write(pid, state="READY", phase="dispatch", active_episode_id="", **_ignition_fields(ignition))


def mark_inference(episode: object, ignition: object = None) -> PipelineEnvelope:
    fields = {}
    if ignition is not None:
        if validate_pipeline_id(ignition.pipeline_id) != validate_pipeline_id(episode.pipeline_id):
            raise ValueError("ignition pipeline binding mismatch")
        fields = _ignition_fields(ignition)
    fields["work_kind"] = str(episode.work_kind)
    return _write(episode.pipeline_id, state="INFERENCE", phase="cascade", active_episode_id=episode.episode_id, **fields)


def mark_waiting_tool(episode: object, *, phase: str = "motor_execution") -> PipelineEnvelope:
    return _write(episode.pipeline_id, state="WAITING_TOOL", phase=phase, active_episode_id=episode.episode_id)


def mark_acted(episode: object) -> PipelineEnvelope:
    """AUTO has executed: replay would repeat side effects, even during foldback.

    Dropping the continuation must not drop WHICH job this episode still folds for. A
    continuation names its job in its contributor (``agent_job_id``), and ``job_id`` is set
    only if the batch went on to own the job; without this a successor that never reopened
    its job looked fold-less for the rest of its foldback, and held operator mail addressed
    to that job could be handed off before the fold bound it (cross-lane T7, review
    2026-09-16). Recovery is unchanged: boot restores only envelopes that keep a
    continuation.

    ``origin_cycle_id`` survives for the same reason and is NOT restated here: ``_write``
    carries every unnamed field forward, so the root lineage this work descends from outlives
    the continuation that brought it (T12). Without it, one unrelated stop could prune the
    root's cancellation while this fold was still running, and the fold would publish again
    (review 2026-09-18).
    """
    pid = validate_pipeline_id(episode.pipeline_id)
    with _LOCKS[pid]:
        envelope = read_envelope(pid)
        carried = dict(dict((envelope.continuation or {}).get("contributor") or {})
                       .get("metadata") or {})
        job_id = envelope.job_id or str(carried.get("job_id") or carried.get("agent_job_id") or "")
        return _write(pid, state="INFERENCE", phase="foldback", active_episode_id=episode.episode_id,
                      continuation=None, job_id=job_id)


def mark_committing(episode: object) -> PipelineEnvelope:
    return _write(episode.pipeline_id, state="COMMITTING", phase="deepsleep_gate", active_episode_id=episode.episode_id)


def mark_idle(pipeline_or_episode: object) -> PipelineEnvelope:
    pid = validate_pipeline_id(getattr(pipeline_or_episode, "pipeline_id", pipeline_or_episode))
    with _LOCKS[pid]:
        idle = PipelineEnvelope(pid)
        _publish(idle)
        return idle


def mark_failed(pipeline_id: int, exc: BaseException | None = None) -> PipelineEnvelope:
    return _write(pipeline_id, state="FAILED", phase=f"failed:{type(exc).__name__}" if exc is not None else "failed")


def _episode_in_journal(episode_id: str) -> bool:
    if not episode_id:
        return False
    from pressure_loop.episode_journal import _resolve_path
    from pressure_loop.policy import episode_journal_path

    path = _resolve_path(episode_journal_path())
    if not path.exists():
        return False
    # Unreadable journals raise; a torn JSONL tail alone is not a completion.
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("episode_id") == episode_id:
            return True
    return False


def restore_pipelines_at_boot() -> dict:
    """Boot-only recovery; call before scheduling any physical worker.

    Intake reconciliation only classifies or releases journal records. It never
    opens an episode or performs canonical memory work during boot.
    """
    from pressure_loop.cognition_queue import enqueue_restore, reconcile_operator_turns_at_boot
    from auto_loop.execute.job_control import reconcile_jobs_at_boot
    from memory.intake_journal import (
        IntakeJournalUnreadable,
        reconcile_at_boot as reconcile_intake_at_boot,
    )

    report = {"pipelines": {}, "migration": _recover_old_envelopes()}
    restorable_jobs = {}
    restorable_cycles = set()
    for pid in PIPELINE_IDS:
        with _LOCKS[pid]:
            envelope = read_envelope(pid)
            queued = False
            if envelope.state in RESTORABLE_STATES and envelope.continuation is not None and not _episode_in_journal(envelope.active_episode_id):
                queued = enqueue_restore(contributor_from_continuation(envelope.continuation))
                restorable_cycles.add(envelope.cycle_id)
                if envelope.job_id:
                    restorable_jobs[envelope.job_id] = envelope.cycle_id
            try:
                intake = reconcile_intake_at_boot(
                    episode_id=envelope.active_episode_id,
                    pipeline_id=pid,
                    cascade_replay=queued,
                )
            except IntakeJournalUnreadable as exc:
                try:
                    from guardian.terminal_feedback import emit_data

                    emit_data(
                        "memory",
                        "intake_journal_unreadable",
                        status="degraded",
                        ok=False,
                        error=str(exc),
                        pipeline_id=pid,
                        episode_id=envelope.active_episode_id,
                    )
                except Exception:
                    pass
                intake = {
                    "episode_id": envelope.active_episode_id,
                    "pipeline_id": pid,
                    "cascade_replay": queued,
                    "pending": 0,
                    "released": 0,
                    "error": str(exc),
                    "unreadable": True,
                }
            report["pipelines"][pid] = {
                "state": envelope.state,
                "queued": queued,
                "queue_id": envelope.queue_id,
                "intake": intake,
            }
            mark_idle(pid)
    report["jobs"] = [job.job_id for job in reconcile_jobs_at_boot(restorable_jobs)]
    from pressure_loop.pressure import recover_continuations

    recover_continuations()
    # Boundary B2 (cross-lane T8): a fold that opened its transaction record and crashed
    # before publishing is completed from the record, exactly once.
    from omega_loop.episode.transaction import reconcile_transactions_at_boot

    report["completed_transactions"] = reconcile_transactions_at_boot()
    report["unrestorable_turns"] = list(reconcile_operator_turns_at_boot(restorable_cycles))
    return report


def _recover_old_envelopes() -> list[dict]:
    """One-time data migration, with no import of the retired scheduler.

    Rename each source only after the shared queue durably owns its owed work.
    Stable normalized identity also deduplicates an old pending queue row.
    """
    from core.paths import GUARDIAN_STATE_ROOT
    from pressure_loop import cognition_queue as queue

    report = []
    for path in sorted((GUARDIAN_STATE_ROOT / "lanes").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("state") not in STATES:
            raise PipelineEnvelopeUnreadable(f"old envelope unreadable: {path}")
        continuation = payload.get("continuation")
        queued = False
        if payload["state"] in RESTORABLE_STATES and continuation is not None and not _episode_in_journal(str(payload.get("active_episode_id") or "")):
            row = continuation["contributor"]
            metadata = row.get("metadata") or {}
            # Synthesized pressure already has its own durable source.
            if metadata.get("line_lane") or row.get("queue_id") or metadata.get("queue_id") or row.get("type") == "plugin_permission_decision":
                queued = queue.enqueue_legacy_restore(row)
        target = path.with_suffix(".json.migrated")
        suffix = 1
        while target.exists():
            target = path.with_suffix(f".json.migrated.{suffix}")
            suffix += 1
        path.rename(target)
        report.append({"source": path.name, "queued": queued})
    return report


def reset_for_tests() -> None:
    for pid in PIPELINE_IDS:
        with _LOCKS[pid]:
            _CACHE.pop(pid, None)
