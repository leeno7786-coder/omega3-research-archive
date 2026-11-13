"""Durable write-ahead identity for canonical episode-SRAM intake.

This journal is ingress/work-control state, not canonical memory and not a
recall source. It records that a canonical envelope was accepted, then records
the terminal outcome produced by the DeepSleep drain. A malformed journal is a
hard unreadable state, never an empty journal.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Sequence

JOURNAL_SCHEMA = 1
JOURNAL_FILENAME = "intake_journal.jsonl"

PENDING = "pending"
APPLYING = "applying"
COMMITTED = "committed"
REJECTED = "rejected"
CONSUMED = "consumed"
UNCERTAIN = "uncertain"
TERMINAL_STATUSES = frozenset({COMMITTED, REJECTED, CONSUMED, UNCERTAIN})
COMMIT_PROOF_KEY = "intake_commit"


class IntakeJournalError(RuntimeError):
    """The journal could not durably accept or acknowledge an intake."""


class IntakeJournalConflict(IntakeJournalError):
    """One source event was presented again with different immutable content."""


class IntakeJournalUnreadable(IntakeJournalError):
    """The journal exists but cannot be trusted as a complete state."""


@dataclass(frozen=True)
class IntakeRecord:
    record_id: str
    event_id: str
    kind: str
    status: str
    owner_episode_id: str
    owner_pipeline_id: int
    fingerprint: str
    payload: dict[str, Any]
    write_options: dict[str, Any]
    origin_episode_id: str = ""
    origin_pipeline_id: int = -1
    adoptable: bool = False
    memory_id: str = ""
    canonical_path: str = ""
    note: str = ""
    updated_at: str = ""


_lock = threading.RLock()
_cache: Optional[tuple[Path, Optional[tuple[int, int]], dict[str, IntakeRecord]]] = None
_transaction = threading.local()


def journal_root() -> Path:
    """Resolve the profile state path at call time for boot and test isolation."""
    from core.paths import GUARDIAN_STATE_ROOT

    return Path(GUARDIAN_STATE_ROOT) / "intake"


def journal_path() -> Path:
    return journal_root() / JOURNAL_FILENAME


def reset_cache_for_tests() -> None:
    global _cache
    with _lock:
        _cache = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jsonable(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True))
    except (TypeError, ValueError) as exc:
        raise IntakeJournalError(f"intake payload is not JSON serializable: {exc}") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _stamp(path: Path) -> Optional[tuple[int, int]]:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise IntakeJournalUnreadable(f"cannot stat intake journal: {path}") from exc
    return stat.st_mtime_ns, stat.st_size


def _record_from_row(row: Mapping[str, Any], line_no: int) -> IntakeRecord:
    try:
        return IntakeRecord(
            record_id=str(row["record_id"]),
            event_id=str(row["event_id"]),
            kind=str(row["kind"]),
            status=str(row["status"]),
            owner_episode_id=str(row["owner_episode_id"]),
            owner_pipeline_id=int(row["owner_pipeline_id"]),
            fingerprint=str(row["fingerprint"]),
            payload=dict(row.get("payload") or {}),
            write_options=dict(row.get("write_options") or {}),
            origin_episode_id=str(row.get("origin_episode_id") or row.get("owner_episode_id") or ""),
            origin_pipeline_id=int(row.get("origin_pipeline_id", row.get("owner_pipeline_id", -1))),
            adoptable=bool(row.get("adoptable")),
            memory_id=str(row.get("memory_id") or ""),
            canonical_path=str(row.get("canonical_path") or ""),
            note=str(row.get("note") or ""),
            updated_at=str(row.get("updated_at") or ""),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise IntakeJournalUnreadable(
            f"intake journal line {line_no} is invalid: {exc}"
        ) from exc


def _read(path: Path) -> dict[str, IntakeRecord]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise IntakeJournalUnreadable(f"cannot read intake journal: {path}") from exc

    records: dict[str, IntakeRecord] = {}
    for line_no, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except ValueError as exc:
            raise IntakeJournalUnreadable(
                f"intake journal line {line_no} is malformed; owed intake is unreadable"
            ) from exc
        if not isinstance(row, dict) or int(row.get("schema_version", -1)) != JOURNAL_SCHEMA:
            raise IntakeJournalUnreadable(
                f"intake journal line {line_no} has an unsupported schema"
            )
        record = _record_from_row(row, line_no)
        records[record.record_id] = record
    return records


def _load(*, force: bool = False) -> dict[str, IntakeRecord]:
    global _cache
    path = journal_path()
    stamp = _stamp(path)
    if (
        not force
        and _cache is not None
        and _cache[0] == path
        and _cache[1] == stamp
    ):
        return _cache[2]
    records = _read(path) if stamp is not None else {}
    _cache = (path, stamp, records)
    return records


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """Serialize journal mutations across processes as well as threads."""
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            acquired = False
            for _ in range(400):
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                    break
                except OSError:
                    time.sleep(0.025)
            if not acquired:
                raise IntakeJournalError(f"timed out acquiring journal lock: {lock_path}")
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _row(record: IntakeRecord, operation: str) -> dict[str, Any]:
    return {
        "schema_version": JOURNAL_SCHEMA,
        "operation": operation,
        "updated_at": record.updated_at,
        "record_id": record.record_id,
        "event_id": record.event_id,
        "kind": record.kind,
        "status": record.status,
        "owner_episode_id": record.owner_episode_id,
        "owner_pipeline_id": record.owner_pipeline_id,
        "fingerprint": record.fingerprint,
        "payload": record.payload,
        "write_options": record.write_options,
        "origin_episode_id": record.origin_episode_id,
        "origin_pipeline_id": record.origin_pipeline_id,
        "adoptable": record.adoptable,
        "memory_id": record.memory_id,
        "canonical_path": record.canonical_path,
        "note": record.note,
    }


def _append_locked(record: IntakeRecord, operation: str) -> IntakeRecord:
    global _cache
    path = journal_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    stamped = replace(record, updated_at=_now_iso())
    line = json.dumps(_row(stamped, operation), ensure_ascii=False, sort_keys=True)
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception as exc:
        _cache = None
        raise IntakeJournalError(
            f"could not durably append intake journal record {record.record_id}: {exc}"
        ) from exc
    records = _read(path)
    _cache = (path, _stamp(path), records)
    return stamped


def confirm_durable(record_id: str) -> IntakeRecord:
    """Prove that an ALREADY-WRITTEN row is durably accepted, or fail attributably.

    ``_append_locked`` writes and flushes before it fsyncs, so a durability failure leaves
    readable bytes behind. Visibility is therefore not a receipt: a caller that retries after a
    failed append can read its own row back and would otherwise acknowledge work the filesystem
    never accepted (WO-03A R4). Nothing here could re-establish or prove durability for an
    existing row, which is why this seam is added rather than a caller-side flag.

    The proof is a filesystem operation on the journal itself, not process state, so it
    reconciles across a process restart: a new process can confirm bytes an older one wrote.
    ``fsync`` on any writable descriptor commits the whole file's dirty data, so this covers
    rows appended through a handle that is already closed.

    It proves the same thing the append's own fsync proves and no more — the data reached the
    device as the platform's ``fsync`` reports it. It is not a power-loss guarantee, and it does
    not fsync the containing directory (neither does the append path).
    """
    wanted = str(record_id or "").strip()
    if not wanted:
        raise IntakeJournalError("confirming durable acceptance requires a record id")
    with _lock:
        path = journal_path()
        with _file_lock(path):
            record = _load(force=True).get(wanted)
            if record is None:
                raise IntakeJournalError(
                    f"intake journal record {wanted!r} is not present on disk; there is nothing "
                    "durable to acknowledge"
                )
            try:
                with open(path, "rb+") as handle:
                    os.fsync(handle.fileno())
            except OSError as exc:
                raise IntakeJournalError(
                    f"could not confirm durable acceptance of intake journal record {wanted}: "
                    f"{exc}"
                ) from exc
            return record


def _save(record: IntakeRecord, operation: str) -> IntakeRecord:
    with _lock:
        path = journal_path()
        with _file_lock(path):
            records = _load(force=True)
            current = records.get(record.record_id)
            if current is not None and current.fingerprint != record.fingerprint:
                raise IntakeJournalConflict(
                    f"source event {record.event_id} was presented with different content"
                )
            return _append_locked(record, operation)


def _envelope_payload(envelope: Any) -> dict[str, Any]:
    return _jsonable(
        {
            "cycle_id": str(getattr(envelope, "cycle_id", "") or ""),
            "id": getattr(envelope, "id", None),
            "coord": list(getattr(envelope, "coord", None) or []),
            "type": str(getattr(envelope, "type", "") or ""),
            "payload": dict(getattr(envelope, "payload", None) or {}),
            "source": str(getattr(envelope, "source", "") or ""),
            "confidence": float(getattr(envelope, "confidence", 0.5) or 0.0),
            "provenance": list(getattr(envelope, "provenance", None) or []),
            "timestamp_created": getattr(envelope, "timestamp_created", None),
            "decay_policy": getattr(envelope, "decay_policy", None),
            "contradiction_state": getattr(envelope, "contradiction_state", None),
            "permissions": dict(getattr(envelope, "permissions", None) or {}),
            "version": int(getattr(envelope, "version", 1) or 1),
            "strength_index": float(getattr(envelope, "strength_index", 0.5) or 0.0),
        }
    )


def _record_id(event_id: str) -> str:
    return "ijr_" + hashlib.sha256(event_id.encode("utf-8")).hexdigest()[:24]


def journal_memory_intake(
    envelope: Any,
    *,
    owner_episode_id: str,
    owner_pipeline_id: int,
    write_options: Mapping[str, Any],
    event_id: Optional[str] = None,
) -> tuple[IntakeRecord, bool]:
    """Durably open one canonical-envelope intake event.

    Returns ``(record, replayed)``. A same-content replay is idempotent; a
    same-event content change is an explicit conflict.
    """
    from core.pipelines import validate_pipeline_id

    episode = str(owner_episode_id or "").strip()
    if not episode:
        raise IntakeJournalError("intake journal requires an owner episode")
    pipeline = validate_pipeline_id(owner_pipeline_id)
    payload = _envelope_payload(envelope)
    options = _jsonable(dict(write_options or {}))
    fingerprint = _digest({"kind": "memory", "payload": payload, "write_options": options})
    explicit = str(event_id or "").strip()
    identity = {
        "cycle_id": payload["cycle_id"],
        "source": payload["source"],
        "id": payload.get("id") or "",
    }
    if not explicit:
        # Without an explicit source event identity, the envelope's immutable
        # content distinguishes a legitimate update to the same canonical
        # record from a replay of the same write. Explicit event IDs retain
        # strict conflict detection below.
        identity["content"] = fingerprint
        explicit = "evt_" + _digest(identity)[:32]
    record_id = _record_id(explicit)
    with _lock:
        path = journal_path()
        with _file_lock(path):
            records = _load(force=True)
            existing = records.get(record_id)
            if existing is not None:
                if existing.fingerprint != fingerprint:
                    raise IntakeJournalConflict(
                        f"source event {explicit} was accepted with different immutable content"
                    )
                return existing, True
            record = IntakeRecord(
                record_id=record_id,
                event_id=explicit,
                kind="memory",
                status=PENDING,
                owner_episode_id=episode,
                owner_pipeline_id=pipeline,
                fingerprint=fingerprint,
                payload=payload,
                write_options=options,
                origin_episode_id=episode,
                origin_pipeline_id=pipeline,
            )
            return _append_locked(record, "accepted"), False


def _journal_generic(
    *,
    kind: str,
    payload: Mapping[str, Any],
    identity: Mapping[str, Any],
    owner_episode_id: str,
    owner_pipeline_id: int,
    event_id: Optional[str] = None,
) -> IntakeRecord:
    from core.pipelines import validate_pipeline_id

    episode = str(owner_episode_id or "").strip()
    if not episode:
        raise IntakeJournalError("intake journal requires an owner episode")
    pipeline = validate_pipeline_id(owner_pipeline_id)
    frozen_payload = _jsonable(dict(payload))
    fingerprint = _digest({"kind": kind, "payload": frozen_payload})
    explicit = str(event_id or "").strip()
    if not explicit:
        explicit = "evt_" + _digest({"kind": kind, **dict(identity), "content": fingerprint})[:32]
    record_id = _record_id(explicit)
    with _lock:
        path = journal_path()
        with _file_lock(path):
            records = _load(force=True)
            existing = records.get(record_id)
            if existing is not None:
                if existing.fingerprint != fingerprint:
                    raise IntakeJournalConflict(
                        f"source event {explicit} was accepted with different immutable content"
                    )
                return existing
            record = IntakeRecord(
                record_id=record_id,
                event_id=explicit,
                kind=kind,
                status=PENDING,
                owner_episode_id=episode,
                owner_pipeline_id=pipeline,
                fingerprint=fingerprint,
                payload=frozen_payload,
                write_options={},
                origin_episode_id=episode,
                origin_pipeline_id=pipeline,
            )
            return _append_locked(record, "accepted")


def journal_perception(
    *,
    owner_episode_id: str,
    owner_pipeline_id: int,
    ordinal: int,
    text: str,
    speaker: str,
    ingress_class: str,
    chat_intent: str,
    source_citation: Mapping[str, Any],
) -> IntakeRecord:
    return _journal_generic(
        kind="perception",
        identity={"episode": owner_episode_id, "ordinal": int(ordinal)},
        payload={
            "text": str(text),
            "speaker": str(speaker),
            "ingress_class": str(ingress_class or ""),
            "chat_intent": str(chat_intent or ""),
            "source_citation": dict(source_citation or {}),
            "processed": False,
        },
        owner_episode_id=owner_episode_id,
        owner_pipeline_id=owner_pipeline_id,
    )


def journal_turn(
    *, owner_episode_id: str, owner_pipeline_id: int, ordinal: int, role: str, text: str
) -> IntakeRecord:
    return _journal_generic(
        kind="turn",
        identity={"episode": owner_episode_id, "ordinal": int(ordinal)},
        payload={"text": str(text), "role": str(role), "graduated": False},
        owner_episode_id=owner_episode_id,
        owner_pipeline_id=owner_pipeline_id,
    )


def journal_recall_maintenance(
    *, owner_episode_id: str, owner_pipeline_id: int, memory_id: str
) -> IntakeRecord:
    return _journal_generic(
        kind="recall",
        identity={"episode": owner_episode_id, "memory_id": str(memory_id)},
        payload={"memory_id": str(memory_id)},
        owner_episode_id=owner_episode_id,
        owner_pipeline_id=owner_pipeline_id,
    )


def record_by_id(record_id: str) -> Optional[IntakeRecord]:
    with _lock:
        return _load().get(str(record_id or ""))


def record_for_memory(memory_id: str) -> Optional[IntakeRecord]:
    """The intake record that BECAME this canonical memory, or None.

    ``memory_id`` is bound to a record when it commits (``mark_committed``). Read backwards it
    resolves a canonical memory to the event that produced it — and so to that event's actor
    and derivation (``memory.cognition_write.derivation_of``).
    """
    wanted = str(memory_id or "")
    if not wanted:
        return None
    with _lock:
        return next((record for record in _load().values() if record.memory_id == wanted), None)


def records() -> tuple[IntakeRecord, ...]:
    with _lock:
        return tuple(_load().values())


def pending_records(
    *, owner_episode_id: Optional[str] = None, owner_pipeline_id: Optional[int] = None
) -> tuple[IntakeRecord, ...]:
    with _lock:
        return tuple(
            record
            for record in _load().values()
            if record.status == PENDING
            and (
                owner_episode_id is None
                or record.owner_episode_id == str(owner_episode_id or "")
            )
            and (
                owner_pipeline_id is None
                or record.owner_pipeline_id == int(owner_pipeline_id)
            )
        )


def reown_record(
    record_id: str,
    *,
    episode_id: str,
    pipeline_id: int,
    reason: str,
) -> IntakeRecord:
    """Move pending work after its old process-local SRAM owner disappeared."""
    from core.pipelines import validate_pipeline_id

    episode = str(episode_id or "").strip()
    if not episode:
        raise IntakeJournalError("reown requires an episode")
    pipeline = validate_pipeline_id(pipeline_id)
    with _lock:
        path = journal_path()
        with _file_lock(path):
            records_by_id = _load(force=True)
            record = records_by_id.get(str(record_id or ""))
            if record is None:
                raise IntakeJournalError(f"unknown intake journal record: {record_id!r}")
            if record.status != PENDING:
                raise IntakeJournalError(
                    f"intake journal record {record_id} is {record.status}, not pending"
                )
            return _append_locked(
                replace(
                    record,
                    owner_episode_id=episode,
                    owner_pipeline_id=pipeline,
                    adoptable=False,
                    note=str(reason or ""),
                ),
                "reowned",
            )


def reconcile_at_boot(
    *, episode_id: str, pipeline_id: int, cascade_replay: bool
) -> dict[str, int | str | bool]:
    """Classify owed intake without opening an episode or writing canonical memory.

    Boot runs before any worker. Reconcile every owed owner in this stopped
    physical lane: admission can journal intake before the pipeline envelope
    acquires its active episode ID. Only the named continuation keeps its
    pending events reserved for replay; untracked owners are released for the
    next episode on the same physical pipeline to adopt.
    """
    from core.pipelines import validate_pipeline_id

    episode = str(episode_id or "").strip()
    pipeline = validate_pipeline_id(pipeline_id)
    report: dict[str, int | str | bool] = {
        "episode_id": episode,
        "pipeline_id": pipeline,
        "cascade_replay": bool(cascade_replay),
        "pending": 0,
        "released": 0,
        "applying": 0,
        "uncertain": 0,
    }
    with _lock:
        path = journal_path()
        with _file_lock(path):
            records_by_id = _load(force=True)
            interrupted = [
                record for record in records_by_id.values()
                if record.owner_pipeline_id == pipeline
                and (record.status == APPLYING or (
                    record.status == UNCERTAIN and not record.payload.get("recovery_record_id")
                ))
            ]
            report["applying"] = sum(record.status == APPLYING for record in interrupted)
            for record in interrupted:
                reason = (f"interrupted {record.kind} effect in episode {record.owner_episode_id}: {record.note}"
                          if record.status == APPLYING else record.note)
                _append_locked(replace(record, status=UNCERTAIN, adoptable=True, note=reason),
                               "recovery_uncertain")
            report["uncertain"] = len(interrupted)
            report["released"] = len(interrupted)
            owed = [
                record
                for record in records_by_id.values()
                if record.status == PENDING and record.owner_pipeline_id == pipeline
            ]
            report["pending"] = len(owed)
            for record in owed:
                if cascade_replay and episode and record.owner_episode_id == episode:
                    continue
                _append_locked(
                    replace(
                        record,
                        owner_pipeline_id=pipeline,
                        adoptable=True,
                        note=f"released at boot from {record.owner_episode_id}",
                    ),
                    "released",
                )
                report["released"] += 1
            return report


def claim_released_records(
    *, episode_id: str, pipeline_id: int
) -> tuple[IntakeRecord, ...]:
    """Adopt only work boot explicitly released for this physical pipeline."""
    from core.pipelines import validate_pipeline_id

    episode = str(episode_id or "").strip()
    pipeline = validate_pipeline_id(pipeline_id)
    if not episode:
        raise IntakeJournalError("claim requires an episode")
    claimed: list[IntakeRecord] = []
    with _lock:
        path = journal_path()
        with _file_lock(path):
            records_by_id = _load(force=True)
            for record in records_by_id.values():
                if (
                    record.status not in {PENDING, UNCERTAIN}
                    or not record.adoptable
                    or record.owner_pipeline_id != pipeline
                    or (record.status == UNCERTAIN and record.payload.get("recovery_record_id"))
                ):
                    continue
                claimed.append(
                    _append_locked(
                        replace(
                            record,
                            owner_episode_id=episode,
                            adoptable=False,
                            note=record.note if record.status == UNCERTAIN else f"adopted by {episode}",
                        ),
                        "adopted",
                    )
                )
    return tuple(claimed)


def verify_canonical_binding(record: IntakeRecord) -> tuple[str, str, str]:
    """Verify a canonical record before retrying a pending journal event.

    Returns ``(verdict, memory_id, canonical_path)``. ``missing`` means the
    write can still be attempted. ``unavailable`` and ``mismatch`` are hard
    diagnostics and must not fall through to another write.
    """
    memory_id = canonical_id_for_record(record)
    if not memory_id:
        return "no_identity", "", ""
    try:
        from memory.engine.walker import load_coord_index, path_for_id

        load_coord_index()
        resolved = path_for_id(memory_id)
    except Exception as exc:
        return "unavailable", memory_id, str(exc)
    if not resolved or not Path(resolved).is_file():
        return "missing", memory_id, ""
    try:
        document = json.loads(Path(resolved).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return "mismatch", memory_id, str(exc)
    if not isinstance(document, dict) or str(document.get("id") or "") != memory_id:
        return "mismatch", memory_id, str(resolved)
    enrichment = document.get("enrichment")
    proof = enrichment.get(COMMIT_PROOF_KEY) if isinstance(enrichment, Mapping) else None
    if proof != commit_proof(record):
        return "mismatch", memory_id, f"{resolved}: intake event/content proof differs"
    from memory.projection_receipt import verify_projection_receipt
    from memory.engine import walker

    verification = verify_projection_receipt(document, root=walker.MEMORY_ROOT)
    if not verification.ok:
        return "mismatch", memory_id, f"{resolved}: {verification.reason}"
    return "ok", memory_id, str(resolved)


def canonical_id_for_record(record: IntakeRecord) -> str:
    """Resolve the accepted identity, including deterministic anonymous intake."""
    payload = dict(record.payload or {})
    # Promotion treats only CanonicalEnvelope.id as canonical authority. An id
    # inside its payload may identify shared source evidence across phase records.
    return str(record.memory_id or payload.get("id")
               or f"intake.{record.record_id}").strip()


def commit_proof(record: IntakeRecord) -> dict[str, str]:
    """The exact accepted event and envelope, persisted inside its canonical claim."""
    return {"record_id": record.record_id, "event_id": record.event_id,
            "fingerprint": record.fingerprint}


def envelope_for_commit(envelope: Any, record: IntakeRecord) -> Any:
    """Stamp a copy at the drain boundary; never mutate the journalled input."""
    payload = dict(envelope.payload or {})
    enrichment = dict(payload.get("enrichment") or {})
    enrichment[COMMIT_PROOF_KEY] = commit_proof(record)
    payload["enrichment"] = enrichment
    return replace(envelope, id=canonical_id_for_record(record), payload=payload)


def release_unreported_uncertainty(*, episode_id: str, pipeline_id: int) -> None:
    """Keep failed evidence admission available to another same-lane owner."""
    with _lock:
        with _file_lock(journal_path()):
            records_by_id = _load(force=True)
            for record in records_by_id.values():
                if (record.owner_episode_id == episode_id
                        and record.owner_pipeline_id == pipeline_id
                        and record.status == UNCERTAIN
                        and not record.payload.get("recovery_record_id")):
                    _append_locked(replace(record, adoptable=True), "recovery_admission_released")


def _terminal_update(
    record_id: str,
    *,
    status: str,
    operation: str,
    memory_id: str = "",
    canonical_path: str = "",
    note: str = "",
) -> IntakeRecord:
    with _lock:
        path = journal_path()
        with _file_lock(path):
            records = _load(force=True)
            record = records.get(str(record_id or ""))
            if record is None:
                raise IntakeJournalError(f"unknown intake journal record: {record_id!r}")
            if record.status == status:
                return record
            if record.status in TERMINAL_STATUSES:
                raise IntakeJournalError(
                    f"intake journal record {record_id} is already {record.status}"
                )
            return _append_locked(
                replace(
                    record,
                    status=status,
                    memory_id=str(memory_id or record.memory_id),
                    canonical_path=str(canonical_path or record.canonical_path),
                    note=str(note or ""),
                ),
                operation,
            )


def _state_update(
    record_id: str,
    *,
    status: Optional[str] = None,
    payload: Optional[Mapping[str, Any]] = None,
    note: str = "",
    operation: str,
) -> IntakeRecord:
    with _lock:
        path = journal_path()
        with _file_lock(path):
            records = _load(force=True)
            record = records.get(str(record_id or ""))
            if record is None:
                raise IntakeJournalError(f"unknown intake journal record: {record_id!r}")
            if status is not None and record.status in TERMINAL_STATUSES:
                if record.status == status:
                    return record
                raise IntakeJournalError(
                    f"intake journal record {record_id} is already {record.status}"
                )
            return _append_locked(
                replace(
                    record,
                    status=record.status if status is None else status,
                    payload=dict(record.payload if payload is None else _jsonable(dict(payload))),
                    note=str(note or record.note),
                ),
                operation,
            )


def mark_applying(record_id: str, *, note: str = "") -> IntakeRecord:
    return _state_update(
        record_id,
        status=APPLYING,
        note=note,
        operation="applying",
    )


def mark_uncertain(record_id: str, *, reason: str) -> IntakeRecord:
    return _state_update(
        record_id,
        status=UNCERTAIN,
        note=reason,
        operation="uncertain",
    )


def mark_recovery_record(record_id: str, *, recovery_record_id: str) -> IntakeRecord:
    """Bind uncertainty to the durable SRAM intake that will teach it to the mind."""
    record = record_by_id(record_id)
    if record is None or record.status != UNCERTAIN or not recovery_record_id:
        raise IntakeJournalError("recovery evidence requires an uncertain row and durable intake")
    payload = dict(record.payload, recovery_record_id=recovery_record_id)
    return _state_update(record_id, payload=payload, operation="recovery_evidence_admitted")


def mark_consumed(record_id: str, *, reason: str) -> IntakeRecord:
    return _state_update(
        record_id,
        status=CONSUMED,
        note=reason,
        operation="consumed",
    )


def mark_graduated(record_id: str) -> IntakeRecord:
    record = record_by_id(record_id)
    if record is None:
        raise IntakeJournalError(f"unknown intake journal record: {record_id!r}")
    payload = dict(record.payload)
    payload["graduated"] = True
    return _state_update(record_id, payload=payload, operation="graduated")


def mark_perception_processed(
    record_id: str, *, records_admitted: Sequence[str]
) -> IntakeRecord:
    record = record_by_id(record_id)
    if record is None:
        raise IntakeJournalError(f"unknown intake journal record: {record_id!r}")
    payload = dict(record.payload)
    payload["processed"] = True
    payload["records"] = [str(item) for item in records_admitted]
    return _state_update(record_id, payload=payload, operation="processed")


def mark_committed(
    record_id: str,
    *,
    memory_id: str,
    canonical_path: str = "",
    note: str = "",
) -> IntakeRecord:
    return _terminal_update(
        record_id,
        status=COMMITTED,
        operation="committed",
        memory_id=memory_id,
        canonical_path=canonical_path,
        note=note,
    )


def mark_rejected(record_id: str, *, reason: str) -> IntakeRecord:
    return _terminal_update(
        record_id,
        status=REJECTED,
        operation="rejected",
        note=reason,
    )


def envelope_from_record(record: IntakeRecord) -> Any:
    from memory.api import CanonicalEnvelope

    payload = dict(record.payload)
    return CanonicalEnvelope(
        cycle_id=str(payload.get("cycle_id") or ""),
        coord=list(payload.get("coord") or []),
        type=str(payload.get("type") or ""),
        payload=dict(payload.get("payload") or {}),
        source=str(payload.get("source") or "memory.api"),
        confidence=float(payload.get("confidence", 0.5)),
        provenance=list(payload.get("provenance") or []),
        id=payload.get("id"),
        timestamp_created=payload.get("timestamp_created"),
        decay_policy=payload.get("decay_policy"),
        contradiction_state=payload.get("contradiction_state"),
        permissions=dict(payload.get("permissions") or {}),
        version=int(payload.get("version", 1)),
        strength_index=float(payload.get("strength_index", 0.5)),
    )


__all__ = [
    "COMMITTED",
    "IntakeJournalConflict",
    "IntakeJournalError",
    "IntakeJournalUnreadable",
    "IntakeRecord",
    "PENDING",
    "REJECTED",
    "TERMINAL_STATUSES",
    "confirm_durable",
    "envelope_from_record",
    "journal_memory_intake",
    "journal_path",
    "journal_root",
    "mark_committed",
    "mark_rejected",
    "record_by_id",
    "records",
    "reset_cache_for_tests",
]
