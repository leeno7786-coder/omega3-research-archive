"""One durable ingress gate routing to four lane-local FIFO lines.

Permission ownership and reservations share the same atomic publication as rows.
A failed write reloads disk, including when replacement committed before raising.
Global arrival identity orders each semantic lane; process-local leases and
acknowledgements remain bound to that lane's permanent physical pipeline.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import os
import re
import threading
import time
import uuid
from pathlib import Path

from core.pipelines import (
    AGENT_CONTRIBUTOR_TYPES,
    CAUSAL_PARENT_KEY,
    lane_for_contributor,
    lane_for_pipeline,
    pipeline_for_lane,
    validate_lane,
    validate_pipeline_id,
    work_kind_for_contributor,
)
from pressure_loop.contributors import Contributor, make_contributor

log = logging.getLogger(__name__)


class CognitionQueueUnreadable(RuntimeError):
    """Durable state cannot be read; this is never an empty queue."""


class _QueueState:
    def __init__(self):
        self.lock = threading.RLock()
        self.queue: list[Contributor] = []
        self.loaded = False
        self.uninterpreted: dict[str, str] = {}
        self.interpretation_outcomes: list[dict] = []
        # One-time migration receipts, never ordinary-turn history. Keep them
        # after interpretation so interrupted envelope archival cannot reopen it.
        self.legacy_registered_turns: dict[str, dict] = {}
        # Host handoffs admitted, keyed by their deterministic queue identity. Written in
        # the SAME publication as the admitted row and retained until both the source mail
        # and the lane row are settled, so a retry cannot repin or execute the same update.
        self.handoff_receipts: dict[str, dict] = {}


_STATE = _QueueState()
_LEASES: dict[int, str] = {}
_QUEUE_STATE_KEY = "_operator_queue_state"
_QUEUE_STATE_SCHEMA = 3
_MAX_PLUGIN_PERMISSION_OWNERS = 256
_MAX_ARRIVAL_RESERVATIONS = 256
_MAX_LOCAL_ABANDONED_RESERVATIONS = 256
_MAX_UNINTERPRETED = 4096
_MAX_HANDOFF_RECEIPTS = 4096
_ARRIVAL_RESERVATION_TTL_SECONDS = 60.0
_OPAQUE = re.compile(r"[0-9a-f]{32,128}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_wall_time = time.time
_PROCESS_EPOCH = uuid.uuid4().hex
_PLUGIN_PERMISSION_OWNERS: dict[str, dict[str, object]] = {}
_ARRIVAL_RESERVATIONS: dict[int, dict[str, object]] = {}
_LOCAL_ABANDONED_RESERVATIONS: dict[int, dict[str, object]] = {}
_NEXT_ARRIVAL_SEQ = 1


def _queue_path() -> Path:
    from core.paths import GUARDIAN_STATE_ROOT

    return GUARDIAN_STATE_ROOT / "cognition_queue.json"


def _legacy_queue_path() -> Path:
    return _queue_path().parent / "operator_input_queue.json"


def _legacy_lines_dir() -> Path:
    return _queue_path().parent / "operator_lines"


def _legacy_state_path() -> Path:
    return _queue_path().parent / "operator_queue_state.json"


def _arrival_seq(c: Contributor) -> int | None:
    value = (c.metadata or {}).get("arrival_seq")
    return value if type(value) is int and value > 0 else None


def _box_identity(c: Contributor) -> str:
    return str(getattr(c, "queue_id", "") or (c.metadata or {}).get("queue_id") or "")


def serialize_input(c: Contributor) -> dict:
    return {
        "source": c.source,
        "type": c.type,
        "weight": c.weight,
        "metadata": copy.deepcopy(dict(c.metadata or {})),
        "queue_id": _box_identity(c),
        "work_kind": work_kind_for_contributor(c),
        "lane": lane_for_contributor(c),
    }


def deserialize_input(row: dict) -> Contributor:
    metadata = copy.deepcopy(dict(row.get("metadata") or {}))
    queue_id = row.get("queue_id") or metadata.get("queue_id")
    if type(queue_id) is not str or not queue_id:
        raise ValueError("cognition_queue_identity_invalid")
    if row.get("type") == "plugin_permission_decision":
        if row.get("source") != "operator" or row.get("weight") != 1.0:
            raise ValueError("plugin_permission_continuation_invalid")
        from pressure_loop.plugin_continuations import plugin_permission_contributor_from_metadata

        original = plugin_permission_contributor_from_metadata(metadata)
    else:
        metadata["queue_id"] = queue_id
        original = make_contributor(
            str(row["source"]), str(row["type"]), float(row["weight"]), metadata
        )
    if _arrival_seq(original) is None:
        raise ValueError("cognition_queue_arrival_invalid")
    # Keep the exact Contributor type required by permission validation. The
    # transport identity is outside its closed permission metadata schema.
    object.__setattr__(original, "queue_id", queue_id)
    if "work_kind" in row:
        object.__setattr__(original, "work_kind", row["work_kind"])
        work_kind_for_contributor(original)
    resolved_lane = lane_for_contributor(original)
    row_lane = row.get("lane", resolved_lane)
    if validate_lane(row_lane) != resolved_lane:
        raise ValueError("cognition_queue_lane_identity_mismatch")
    object.__setattr__(original, "lane", resolved_lane)
    return original


def normalize_legacy_input(row: dict) -> dict:
    """Share stable migration identity between pending rows and active work.

    Hash before assigning any missing arrival sequence, exactly as the original
    queue migration did. This is migration-only; live intake mints new identities.
    """
    metadata = copy.deepcopy(dict(row.get("metadata") or {}))
    metadata.pop("line_lane", None)
    normalized = dict(row, metadata=metadata)
    normalized["queue_id"] = (
        row.get("queue_id")
        or metadata.get("queue_id")
        or hashlib.sha256(
            json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    return normalized


def _state_row() -> dict:
    return {
        _QUEUE_STATE_KEY: {
            "schema_version": 3,
            "plugin_permission_owners": list(_PLUGIN_PERMISSION_OWNERS.values()),
            "arrival_reservations": list(_ARRIVAL_RESERVATIONS.values()),
            "next_arrival_seq": _NEXT_ARRIVAL_SEQ,
        }
    }


def _encoded_rows() -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "queue": [serialize_input(c) for c in _STATE.queue],
            "permission_state": _state_row(),
            "uninterpreted": _STATE.uninterpreted,
            "interpretation_outcomes": _STATE.interpretation_outcomes,
            "legacy_registered_turns": _STATE.legacy_registered_turns,
            "handoff_receipts": _STATE.handoff_receipts,
        },
        ensure_ascii=False,
        indent=2,
    )


def _write_encoded(path: Path, encoded: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _install(payload: dict) -> None:
    global _NEXT_ARRIVAL_SEQ
    if type(payload) is not dict or payload.get("schema_version") != 1:
        raise ValueError("cognition_queue_schema_invalid")
    rows = payload["queue"]
    ledger = payload["uninterpreted"]
    outcomes = payload.get("interpretation_outcomes", [])
    if (type(outcomes) is not list or len(outcomes) > _MAX_UNINTERPRETED or any(
        type(row) is not dict or set(row) != {"arrival_seq", "cycle_id", "outcome"}
        or type(row["arrival_seq"]) is not int or row["arrival_seq"] <= 0
        or type(row["cycle_id"]) is not str or not row["cycle_id"]
        or type(row["outcome"]) is not str or not row["outcome"]
        for row in outcomes
    )):
        raise ValueError("cognition_queue_outcome_receipts_invalid")
    registered = payload.get("legacy_registered_turns", {})
    if (
        type(registered) is not dict
        or any(
            type(identity) is not str or not identity
            or type(receipt) is not dict
            or set(receipt) != {"arrival_seq", "cycle_id"}
            or type(receipt["arrival_seq"]) is not int or receipt["arrival_seq"] <= 0
            or type(receipt["cycle_id"]) is not str or not receipt["cycle_id"]
            for identity, receipt in registered.items()
        )
    ):
        raise ValueError("cognition_queue_migration_receipts_invalid")
    # Absent in files written before handoffs existed: an empty set, never corruption.
    handoffs = payload.get("handoff_receipts", {})
    if (
        type(handoffs) is not dict
        or len(handoffs) > _MAX_HANDOFF_RECEIPTS
        or any(
            type(identity) is not str or not _OPAQUE.fullmatch(identity)
            or type(receipt) is not dict
            or set(receipt) not in (
                {"arrival_seq", "cycle_id", "origin_ref"},
                {"arrival_seq", "cycle_id", "origin_ref", "mail_acknowledged"},
            )
            or type(receipt["arrival_seq"]) is not int or receipt["arrival_seq"] <= 0
            or type(receipt["cycle_id"]) is not str or not receipt["cycle_id"]
            or type(receipt["origin_ref"]) is not str or not receipt["origin_ref"]
            or ("mail_acknowledged" in receipt
                and type(receipt["mail_acknowledged"]) is not bool)
            for identity, receipt in handoffs.items()
        )
    ):
        raise ValueError("cognition_queue_handoff_receipts_invalid")
    if type(rows) is not list or type(ledger) is not dict or len(ledger) > _MAX_UNINTERPRETED:
        raise ValueError("cognition_queue_state_invalid")
    if any(
        not key.isdigit() or int(key) <= 0 or type(value) is not str or not value
        for key, value in ledger.items()
    ):
        raise ValueError("cognition_queue_ledger_invalid")
    queue = [deserialize_input(row) for row in rows]
    if len({_box_identity(c) for c in queue}) != len(queue):
        raise ValueError("cognition_queue_duplicate_identity")
    _NEXT_ARRIVAL_SEQ = 1
    _ingest_state_row(payload["permission_state"])
    _STATE.queue[:] = sorted(queue, key=_arrival_seq)
    _STATE.uninterpreted = dict(ledger)
    _STATE.interpretation_outcomes = copy.deepcopy(outcomes)
    _STATE.legacy_registered_turns = copy.deepcopy(registered)
    _STATE.handoff_receipts = {
        identity: {**copy.deepcopy(receipt),
                   "mail_acknowledged": bool(receipt.get("mail_acknowledged", False))}
        for identity, receipt in handoffs.items()
    }
    _NEXT_ARRIVAL_SEQ = max(
        [_NEXT_ARRIVAL_SEQ]
        + [int(c.metadata["arrival_seq"]) + 1 for c in queue]
        + [int(key) + 1 for key in ledger]
        + [receipt["arrival_seq"] + 1 for receipt in registered.values()]
    )
    _suppress_exact_local_abandonments_locked()
    identities = {_box_identity(c) for c in queue}
    for pid, identity in tuple(_LEASES.items()):
        if identity not in identities:
            del _LEASES[pid]
    _STATE.loaded = True


def _read_disk_snapshot_locked() -> dict:
    return json.loads(_queue_path().read_text(encoding="utf-8"))


def _persist_mutation() -> None:
    expected = None
    try:
        expected = _encoded_rows()
        _write_encoded(_queue_path(), expected)
        _LOCAL_ABANDONED_RESERVATIONS.clear()
    except BaseException as exc:
        for attempt in range(2):
            try:
                payload = _read_disk_snapshot_locked()
                _install(payload)
                committed = expected is not None and json.loads(expected) == payload
                exc.add_note(
                    "operator_queue_persistence=" + ("committed" if committed else "not_committed")
                )
                break
            except FileNotFoundError:
                _clear_cached_state()
                _STATE.loaded = True
                exc.add_note("operator_queue_persistence=not_committed")
                break
            except BaseException as reconciliation:
                if attempt == 1:
                    _STATE.loaded = False
                    exc.add_note(f"operator_queue_reconciliation_failed={reconciliation!r}")
                    exc.add_note("operator_queue_persistence=unknown")
        raise


def _clear_cached_state() -> None:
    global _NEXT_ARRIVAL_SEQ
    _STATE.queue.clear()
    _STATE.uninterpreted.clear()
    _STATE.interpretation_outcomes.clear()
    _STATE.legacy_registered_turns.clear()
    _STATE.handoff_receipts.clear()
    _PLUGIN_PERMISSION_OWNERS.clear()
    _ARRIVAL_RESERVATIONS.clear()
    _NEXT_ARRIVAL_SEQ = 1


def _ensure_loaded() -> None:
    if _STATE.loaded:
        return
    try:
        if _queue_path().exists():
            _install(_read_disk_snapshot_locked())
            _finish_migration()
            return
        _clear_cached_state()
        sources = _migration_sources()
        raw_rows = []
        for path in sources:
            rows = json.loads(path.read_text(encoding="utf-8"))
            if type(rows) is not list or any(type(row) is not dict for row in rows):
                raise ValueError("legacy_queue_invalid")
            for row in rows:
                if _QUEUE_STATE_KEY in row:
                    _ingest_state_row(row)
                else:
                    raw_rows.append(row)
        # Stable ids also collapse the same row present in a pre-split queue and line.
        for row in raw_rows:
            metadata = dict(row.get("metadata") or {})
            seq = metadata.get("arrival_seq")
            if type(seq) is int and seq > 0:
                global _NEXT_ARRIVAL_SEQ
                _NEXT_ARRIVAL_SEQ = max(_NEXT_ARRIVAL_SEQ, seq + 1)
        for row in raw_rows:
            normalized = normalize_legacy_input(row)
            metadata = normalized["metadata"]
            if (
                _arrival_seq(
                    make_contributor(
                        str(row["source"]), str(row["type"]), float(row["weight"]), metadata
                    )
                )
                is None
            ):
                metadata["arrival_seq"] = _NEXT_ARRIVAL_SEQ
                _NEXT_ARRIVAL_SEQ += 1
            item = deserialize_input(normalized)
            if not any(_box_identity(c) == _box_identity(item) for c in _STATE.queue):
                _STATE.queue.append(item)
                _register_turn(item)
                if item.source == "operator" and item.type != "plugin_permission_decision":
                    _record_legacy_registration(item)
        _STATE.queue.sort(key=_arrival_seq)
        _STATE.loaded = True
        if sources:
            _persist_mutation()
            _finish_migration()
    except BaseException as exc:
        _STATE.loaded = False
        from guardian.terminal_feedback import emit_data
        emit_data("pressure", "cognition_queue_load", status="degraded",
                   summary="Cognition queue unreadable; admission stopped",
                   reason="cognition_queue_load_failed", error=str(exc))
        raise CognitionQueueUnreadable(f"cognition queue unreadable: {exc}") from exc


def _migration_sources() -> list[Path]:
    return [
        p
        for p in [
            _legacy_queue_path(),
            *sorted(_legacy_lines_dir().glob("*.json")),
            _legacy_state_path(),
        ]
        if p.is_file()
    ]


def _finish_migration() -> None:
    for path in _migration_sources():
        path.rename(_migrated_target(path))


def _register_turn(c: Contributor) -> None:
    if c.source != "operator" or c.type == "plugin_permission_decision":
        return
    seq = str(c.metadata["arrival_seq"])
    cycle = str(c.metadata.get("cycle_id") or "")
    if not cycle:
        raise ValueError("operator_turn_cycle_required")
    existing = _STATE.uninterpreted.get(seq)
    if existing is not None and existing != cycle:
        raise ValueError("operator_turn_arrival_conflict")
    if existing is None and len(_STATE.uninterpreted) >= _MAX_UNINTERPRETED:
        raise ValueError("operator_interpretation_ledger_full")
    _STATE.uninterpreted[seq] = cycle


def _record_legacy_registration(c: Contributor) -> None:
    _STATE.legacy_registered_turns[_box_identity(c)] = {
        "arrival_seq": c.metadata["arrival_seq"],
        "cycle_id": c.metadata["cycle_id"],
    }


def next_arrival_seq() -> int:
    global _NEXT_ARRIVAL_SEQ
    with _STATE.lock:
        _ensure_loaded()
        value = _NEXT_ARRIVAL_SEQ
        _NEXT_ARRIVAL_SEQ += 1
        _persist_mutation()
        return value


def enqueue_input(contributor: Contributor, *, operator_activity: bool, admission_cycle_id: str | None = None) -> bool:
    if contributor.type in {"operator_command", "operator_query", "operator_response"} and contributor.source != "operator":
        return False
    return _enqueue(contributor, operator_activity=operator_activity, restore=False, admission_cycle_id=admission_cycle_id)


def enqueue_handoff(contributor: Contributor, *, handoff_id: str, origin_ref: str) -> bool:
    """Admit work the HOST hands off from another durable store, exactly once.

    Returns True when admitted now and False when this handoff was ALREADY accepted — even
    if that first copy has since been leased and consumed. Presence in the queue cannot
    answer that, so the receipt is written in the same atomic publication as the row. Mail
    acknowledgement is recorded on the receipt; it is retired when the exact lane row is
    acknowledged, or immediately if the lane row was already gone. An unrelated queued turn
    that collides with the handoff's identity raises rather than reading as "already accepted".
    """
    if type(handoff_id) is not str or not _OPAQUE.fullmatch(handoff_id):
        raise ValueError("handoff_identity_invalid")
    if type(origin_ref) is not str or not origin_ref or len(origin_ref) > 256:
        raise ValueError("handoff_origin_invalid")
    metadata = getattr(contributor, "metadata", None)
    cycle_id = str(metadata.get("cycle_id") or "").strip() if isinstance(metadata, dict) else ""
    if not cycle_id:
        raise ValueError("handoff_contributor_cycle_required")
    return _enqueue(contributor, operator_activity=False, restore=False,
                    handoff=(handoff_id, origin_ref))


def handoff_accepted(handoff_id: str) -> bool:
    """Whether this handoff still has a retained idempotency receipt."""
    with _STATE.lock:
        _ensure_loaded()
        return str(handoff_id) in _STATE.handoff_receipts


def handoff_receipts() -> dict[str, dict]:
    """Every retained handoff receipt, keyed by handoff identity (a snapshot)."""
    with _STATE.lock:
        _ensure_loaded()
        return copy.deepcopy(_STATE.handoff_receipts)


def forget_handoff_receipts(handoff_ids) -> int:
    """Drop receipts whose source has been retired. Unknown identities are ignored."""
    wanted = {str(one) for one in handoff_ids or ()}
    with _STATE.lock:
        _ensure_loaded()
        present = wanted & set(_STATE.handoff_receipts)
        if not present:
            return 0
        for identity in present:
            del _STATE.handoff_receipts[identity]
        _persist_mutation()
        return len(present)


def prepare_handoff_mail_absorption(handoff_ids) -> tuple[str, ...]:
    """Persist that a running lane fold owns a specialist mail update.

    The matching lane-work row remains durable until mail acknowledgement commits. A row
    already leased as the current lane episode belongs to that episode and is left alone.
    This prepares the queue side before the separate mail store deletes its row, closing the
    duplicate-execution window across the two stores.
    """
    wanted = {str(identity) for identity in handoff_ids or () if str(identity)}
    if not wanted:
        return ()
    prepared: list[str] = []
    with _STATE.lock:
        _ensure_loaded()
        for item in _STATE.queue:
            identity = str(item.metadata.get("handoff_id") or "")
            if (identity not in wanted or item.type != "lane_work_dispatch"
                    or not item.metadata.get("lane_work_handoff")):
                continue
            pipeline_id = pipeline_for_lane(lane_for_contributor(item))
            if _LEASES.get(pipeline_id) == _box_identity(item):
                continue
            if item.metadata.get("mail_absorbed") or item.metadata.get("mail_absorption_pending"):
                prepared.append(identity)
                continue
            item.metadata["mail_absorption_pending"] = True
            prepared.append(identity)
        if prepared:
            _persist_mutation()
    return tuple(dict.fromkeys(prepared))


def complete_handoff_mail_absorption(handoff_ids) -> tuple[str, ...]:
    """Commit queue suppression after specialist mail was durably acknowledged."""
    wanted = {str(identity) for identity in handoff_ids or () if str(identity)}
    if not wanted:
        return ()
    completed: list[str] = []
    with _STATE.lock:
        _ensure_loaded()
        for item in _STATE.queue:
            identity = str(item.metadata.get("handoff_id") or "")
            if identity not in wanted or not item.metadata.get("mail_absorption_pending"):
                continue
            pipeline_id = pipeline_for_lane(lane_for_contributor(item))
            if _LEASES.get(pipeline_id) == _box_identity(item):
                # The target dispatch itself began while the previous fold was settling;
                # it now owns this input and must not be retired as absorbed.
                continue
            item.metadata.pop("mail_absorption_pending", None)
            item.metadata["mail_absorbed"] = True
            completed.append(identity)
        if completed:
            _persist_mutation()
    return tuple(dict.fromkeys(completed))


def mark_handoff_mail_acknowledged(handoff_ids) -> tuple[str, ...]:
    """Retain idempotency until the corresponding queued handoff is also settled."""
    wanted = {str(identity) for identity in handoff_ids or () if str(identity)}
    if not wanted:
        return ()
    acknowledged: list[str] = []
    changed = False
    with _STATE.lock:
        _ensure_loaded()
        queued_ids = {_box_identity(item) for item in _STATE.queue}
        for identity in wanted:
            receipt = _STATE.handoff_receipts.get(identity)
            if receipt is None:
                continue
            if identity not in queued_ids:
                del _STATE.handoff_receipts[identity]
            else:
                receipt["mail_acknowledged"] = True
            acknowledged.append(identity)
            changed = True
        if changed:
            _persist_mutation()
    return tuple(acknowledged)


def enqueue_restore(contributor: Contributor) -> bool:
    if not _box_identity(contributor):
        raise ValueError("restore_requires_queue_id")
    return _enqueue(contributor, operator_activity=False, restore=True)


def enqueue_legacy_restore(row: dict) -> bool:
    """Recover pre-cutover work and register its first-decision barrier once.

    Accept the serialized historical row because it may have no arrival yet.
    The queue assigns that sequence under its lock, in the SAME publication as
    the registration receipt, barrier, and row. Normal restore is unchanged.
    """
    normalized = normalize_legacy_input(row)
    metadata = normalized["metadata"]
    seq = metadata.get("arrival_seq")
    if type(seq) is not int or seq <= 0:
        metadata.pop("arrival_seq", None)
    contributor = make_contributor(
        normalized["source"], normalized["type"], normalized["weight"], metadata
    )
    object.__setattr__(contributor, "queue_id", normalized["queue_id"])
    return _enqueue(contributor, operator_activity=False, restore=True, legacy_migration=True)


def _enqueue(contributor: Contributor, *, operator_activity: bool, restore: bool, legacy_migration: bool = False, admission_cycle_id: str | None = None,
             handoff: tuple[str, str] | None = None) -> bool:
    global _NEXT_ARRIVAL_SEQ
    with _STATE.lock:
        _ensure_loaded()
        try:
            before = json.loads(_encoded_rows())
        except BaseException as exc:
            exc.add_note("operator_queue_persistence=not_committed")
            raise
        persistence_attempted = False
        try:
            row = serialize_input(contributor)
            if handoff is not None:
                if handoff[0] in _STATE.handoff_receipts:
                    return False
                queued_handoff = next(
                    (candidate for candidate in _STATE.queue
                     if _box_identity(candidate) == handoff[0]),
                    None,
                )
                if queued_handoff is not None:
                    existing_metadata = queued_handoff.metadata
                    proposed_metadata = row["metadata"]
                    same_specialist_handoff = (
                        queued_handoff.type == "lane_work_dispatch"
                        and existing_metadata.get("lane_work_handoff") is True
                        and all(existing_metadata.get(key) == proposed_metadata.get(key)
                                for key in (
                                    "handoff_id", "lane", "original_cycle_id", "thread_id",
                                    "message", "operator_route_context",
                                ))
                    )
                    if same_specialist_handoff:
                        return False
                    raise ValueError("handoff_identity_collision")
                if len(_STATE.handoff_receipts) >= _MAX_HANDOFF_RECEIPTS:
                    raise ValueError("handoff_receipts_full")
                row["queue_id"] = handoff[0]
            row["queue_id"] = row["queue_id"] or uuid.uuid4().hex
            if "arrival_seq" not in row["metadata"]:
                row["metadata"]["arrival_seq"] = _NEXT_ARRIVAL_SEQ
            item = deserialize_input(row)
            receipt = _STATE.legacy_registered_turns.get(_box_identity(item)) if legacy_migration else None
            if receipt is not None:
                if receipt["cycle_id"] != item.metadata.get("cycle_id"):
                    raise ValueError("legacy_registration_cycle_conflict")
                # Missing historical arrival was assigned on the FIRST recovery.
                # An acknowledged row may now exist only in this receipt.
                row["metadata"]["arrival_seq"] = receipt["arrival_seq"]
                item = deserialize_input(row)
            register_legacy = (
                legacy_migration
                and item.source == "operator"
                and item.type != "plugin_permission_decision"
                and receipt is None
            )
            if handoff is not None and (
                any(_box_identity(c) == _box_identity(item) for c in _STATE.queue)
                or any(contributor_dedup_key(c) == contributor_dedup_key(item) for c in _STATE.queue)
            ):
                # No receipt, yet a queued turn claims this identity: never report that as
                # an accepted handoff, or the caller would retire words nothing will read.
                raise ValueError("handoff_identity_collision")
            if any(_box_identity(c) == _box_identity(item) for c in _STATE.queue):
                if register_legacy:
                    # An existing shared-queue row already passed registration;
                    # its absent barrier may be a completed first decision.
                    existing = next(c for c in _STATE.queue if _box_identity(c) == _box_identity(item))
                    _record_legacy_registration(existing)
                    persistence_attempted = True
                    _persist_mutation()
                return False
            permission = item.type == "plugin_permission_decision"
            if not restore:
                if admission_cycle_id is not None:
                    reservation = _ARRIVAL_RESERVATIONS.get(_arrival_seq(item))
                    if (reservation is None or reservation.get("cycle_id") != admission_cycle_id
                            or item.metadata.get("cycle_id") != admission_cycle_id
                            or reservation["process_epoch"] != _PROCESS_EPOCH):
                        raise ValueError("operator_arrival_unreserved")
                consumed = _resolve_arrival_reservation(item)
                if permission:
                    owner = _permission_owner(item)
                    request = str(owner["request_id"])
                    existing_owner = _PLUGIN_PERMISSION_OWNERS.get(request)
                    if existing_owner:
                        if existing_owner["state"] != "queued" or int(
                            existing_owner["arrival_seq"]
                        ) <= int(owner["arrival_seq"]):
                            if consumed:
                                persistence_attempted = True
                                _persist_mutation()
                            return False
                        _STATE.queue[:] = [
                            c
                            for c in _STATE.queue
                            if contributor_dedup_key(c) != contributor_dedup_key(item)
                        ]
                    else:
                        while len(_PLUGIN_PERMISSION_OWNERS) >= _MAX_PLUGIN_PERMISSION_OWNERS:
                            if not _prune_settled_owner_for_capacity():
                                raise ValueError("plugin_permission_continuation_limit_exceeded")
                    _PLUGIN_PERMISSION_OWNERS[request] = owner
                elif any(
                    contributor_dedup_key(c) == contributor_dedup_key(item) for c in _STATE.queue
                ):
                    if consumed:
                        persistence_attempted = True
                        _persist_mutation()
                    return False
            elif permission:
                owner = _permission_owner(item)
                request = str(owner["request_id"])
                existing_owner = _PLUGIN_PERMISSION_OWNERS.get(request)
                if existing_owner is not None:
                    if (
                        existing_owner["expected_revision"] != owner["expected_revision"]
                        or existing_owner["arrival_seq"] != owner["arrival_seq"]
                    ):
                        raise ValueError("plugin_permission_restore_owner_conflict")
                    if existing_owner["state"] == "settled":
                        return False
                    if any(
                        contributor_dedup_key(c) == contributor_dedup_key(item)
                        for c in _STATE.queue
                    ):
                        return False
                else:
                    while len(_PLUGIN_PERMISSION_OWNERS) >= _MAX_PLUGIN_PERMISSION_OWNERS:
                        if not _prune_settled_owner_for_capacity():
                            raise ValueError("plugin_permission_continuation_limit_exceeded")
                # Only the same unresolved decision can return from an active
                # carrier. A terminal owner or another revision is never reset.
                _PLUGIN_PERMISSION_OWNERS[request] = owner
            if not restore or register_legacy:
                _register_turn(item)
            if register_legacy:
                _record_legacy_registration(item)
            _NEXT_ARRIVAL_SEQ = max(_NEXT_ARRIVAL_SEQ, int(item.metadata["arrival_seq"]) + 1)
            if handoff is not None:
                _STATE.handoff_receipts[handoff[0]] = {
                    "arrival_seq": int(item.metadata["arrival_seq"]),
                    "cycle_id": str(item.metadata.get("cycle_id") or ""),
                    "origin_ref": handoff[1],
                    "mail_acknowledged": False,
                }
            _STATE.queue.append(item)
            _STATE.queue.sort(key=_arrival_seq)
        except BaseException:
            if not persistence_attempted:
                _install(before)
            raise
        _persist_mutation()
    # The operator's whole-turn budget starts HERE, at acceptance, and nowhere later: the
    # time this turn now spends in line is time they spend in silence, so it belongs to the
    # same budget as the inference stages that follow (pressure_loop/turn_deadline.py).
    # Machine-minted work is deliberately ungoverned — no bubble waits on it, and Omega's own
    # dispatch is not a person sitting in front of a spinner.
    if item.source == "operator" and str(item.metadata.get("cycle_id") or "").strip():
        from pressure_loop.turn_deadline import start as start_turn_deadline

        start_turn_deadline(str(item.metadata["cycle_id"]))
    try:
        # Measurement only (demo bar B2): when the ONE gate stamped this turn. A bounded
        # in-memory mark — it gates nothing, persists nothing, and losing it loses a number
        # rather than a turn.
        from pressure_loop.turn_latency import mark

        mark(str(item.metadata.get("cycle_id") or ""), "arrival", work_kind=item.type)
    except Exception:
        log.debug("turn latency arrival mark not recorded (non-fatal)", exc_info=True)
    if operator_activity:
        try:
            from pressure_loop import idle_wake

            idle_wake.record_activity("operator")
        except Exception:
            log.exception("Durable admission succeeded; operator activity notification failed")
    try:
        from pressure_loop.loop import notify_work

        notify_work()
    except Exception:
        log.exception("Durable admission succeeded; pressure wake notification failed")
    return True


def lease_next_input(pipeline_id: int) -> Contributor | None:
    """Lease the oldest unleased item addressed to this pipeline's fixed lane.

    FIFO order and the incomplete-ingress barrier apply within that semantic
    lane. A blocked Chat admission cannot stop Browser, Agent, or Window from
    leasing their own oldest item.
    """
    pid = validate_pipeline_id(pipeline_id)
    lane = lane_for_pipeline(pid)
    with _STATE.lock:
        _ensure_loaded()
        if _prune_expired_reservations_locked():
            _persist_mutation()
        if pid in _LEASES:
            return None
        for item in _STATE.queue:
            if lane_for_contributor(item) != lane:
                continue
            if _head_blocked_by_reservation(item):
                # Correct (§4.0) but no longer silent: name the barrier, time it, and report it
                # once it outlives its budget.
                _note_barrier_locked(True, pipeline_id=pid)
                _report_stall_if_over_budget_locked()
                return None
            if _box_identity(item) not in _LEASES.values():
                _LEASES[pid] = _box_identity(item)
                return item
        return None


def owns_input_lease(contributor: Contributor, pipeline_id: int) -> bool:
    pid = validate_pipeline_id(pipeline_id)
    with _STATE.lock:
        _ensure_loaded()
        return bool(_box_identity(contributor)) and _LEASES.get(pid) == _box_identity(contributor)


def release_input_lease(pipeline_id: int) -> bool:
    pid = validate_pipeline_id(pipeline_id)
    with _STATE.lock:
        _ensure_loaded()
        return _LEASES.pop(pid, None) is not None


def acknowledge_input(contributor: Contributor, pipeline_id: int) -> bool:
    pid = validate_pipeline_id(pipeline_id)
    with _STATE.lock:
        _ensure_loaded()
        if not owns_input_lease(contributor, pid):
            return False
        item = next((c for c in _STATE.queue if _box_identity(c) == _LEASES[pid]), None)
        if item is None:
            return False
        if item.type == "plugin_permission_decision":
            owner = _PLUGIN_PERMISSION_OWNERS.get(str(item.metadata["request_id"]))
            if owner is None or owner["state"] != "queued":
                raise ValueError("plugin_permission_continuation_invalid")
            owner["state"] = "inflight"
        _STATE.queue.remove(item)
        if item.type == "lane_work_dispatch":
            handoff_id = str(item.metadata.get("handoff_id") or "")
            receipt = _STATE.handoff_receipts.get(handoff_id)
            if receipt is not None and receipt.get("mail_acknowledged") is True:
                del _STATE.handoff_receipts[handoff_id]
        _persist_mutation()
        _LEASES.pop(pid, None)
        return True


def settle_plugin_permission_decision(request_id: str, *, expected_revision: str) -> bool:
    with _STATE.lock:
        _ensure_loaded()
        owner = _PLUGIN_PERMISSION_OWNERS.get(request_id)
        if (
            owner is None
            or owner["expected_revision"] != expected_revision
            or owner["state"] != "inflight"
        ):
            return False
        owner["state"] = "settled"
        _persist_mutation()
        return True


def peek_inputs() -> tuple[Contributor, ...]:
    with _STATE.lock:
        _ensure_loaded()
        return tuple(_STATE.queue)


def queue_depth() -> int:
    return len(peek_inputs())


def has_waiting_input(*, operator_only: bool = False) -> Contributor | None:
    """A non-claiming OBSERVATION of the line. Never selection, never a lease.

    ``operator_only`` here answers "is an operator-authored item waiting?", which is what the
    ``operator_message`` pressure component reads (``pressure_field.read_operator_message``):
    §4.0's "the line IS the pressure". It reorders nothing, consumes nothing and grants no
    queue position — unlike the retired ``lease_next_input(operator_only=...)`` filter, which
    was selection and is gone. Keep the two apart: an observation of who is waiting is not a
    priority class.
    """
    return next((c for c in peek_inputs() if not operator_only or c.source == "operator"), None)


def clear_inputs() -> None:
    with _STATE.lock:
        _ensure_loaded()
        _clear_cached_state()
        _persist_mutation()
        _LEASES.clear()


def contributor_dedup_key(contributor: Contributor) -> tuple[str, str]:
    return _box_dedup_key(contributor)


def mark_operator_turn_interpreted(arrival_seq: int, cycle_id: str, *, outcome: str) -> bool:
    if type(arrival_seq) is not int or arrival_seq <= 0 or not outcome:
        raise ValueError("operator_interpretation_invalid")
    with _STATE.lock:
        _ensure_loaded()
        if _STATE.uninterpreted.get(str(arrival_seq)) != cycle_id:
            return False
        del _STATE.uninterpreted[str(arrival_seq)]
        _STATE.interpretation_outcomes = (_STATE.interpretation_outcomes + [{
            "arrival_seq": arrival_seq, "cycle_id": cycle_id, "outcome": outcome,
        }])[-_MAX_UNINTERPRETED:]
        _persist_mutation()
    from pressure_loop.loop import notify_work
    notify_work()
    return True


def reconcile_operator_turns_at_boot(recoverable_cycle_ids) -> tuple[dict, ...]:
    """Boot only: record why non-replayable first decisions cannot arrive."""
    with _STATE.lock:
        _ensure_loaded()
        recoverable = set(recoverable_cycle_ids) | {
            str(c.metadata.get("cycle_id") or "") for c in _STATE.queue
        }
        receipts = []
        for seq, cycle in tuple(_STATE.uninterpreted.items()):
            if cycle in recoverable:
                continue
            receipt = {"arrival_seq": int(seq), "cycle_id": cycle, "outcome": "boot_unrestorable_no_carrier"}
            receipts.append(receipt)
            del _STATE.uninterpreted[seq]
        if receipts:
            _STATE.interpretation_outcomes = (_STATE.interpretation_outcomes + receipts)[-_MAX_UNINTERPRETED:]
            _persist_mutation()
        return tuple(copy.deepcopy(receipts))


def earlier_operator_arrivals_admitted(arrival_seq: int) -> bool:
    """No operator turn sent before ``arrival_seq`` is still inside intake.

    Narrower than :func:`earlier_operator_turns_interpreted`, deliberately. It waits only
    on LIVE arrival reservations — turns still being screened between the transport and
    their durable admission — and never on the interpretation ledger, which only the agent
    tool loop settles: an image, document, learn or plugin turn stays uninterpreted until
    the next boot, and waiting on it would hold every later message back indefinitely.

    A reservation past its expiry no longer holds anything back, so an intake that never
    returned delays later words by at most the reservation ttl. Read-only: nothing is
    pruned here.
    """
    if type(arrival_seq) is not int or arrival_seq <= 0:
        raise ValueError("operator_arrival_invalid")
    now = float(_wall_time())
    with _STATE.lock:
        _ensure_loaded()
        return not any(
            seq < arrival_seq and "cycle_id" in reservation
            and float(reservation["expires_at"]) > now
            for seq, reservation in _ARRIVAL_RESERVATIONS.items()
        )


def earlier_operator_turns_interpreted(arrival_seq: int) -> bool:
    if type(arrival_seq) is not int or arrival_seq <= 0:
        raise ValueError("operator_interpretation_invalid")
    with _STATE.lock:
        _ensure_loaded()
        return not any(int(seq) < arrival_seq for seq in _STATE.uninterpreted) and not any(
            seq < arrival_seq and "cycle_id" in reservation
            for seq, reservation in _ARRIVAL_RESERVATIONS.items()
        )


def _reset_cache_for_tests() -> None:
    global _PROCESS_EPOCH
    with _STATE.lock:
        _clear_cached_state()
        _LEASES.clear()
        _LOCAL_ABANDONED_RESERVATIONS.clear()
        _BARRIER.clear()
        _PROCESS_EPOCH = uuid.uuid4().hex
        _STATE.loaded = False


def _box_dedup_key(c: Contributor) -> tuple[str, str]:
    meta = c.metadata or {}
    if c.type in AGENT_CONTRIBUTOR_TYPES:
        # Machine-minted agent work is identified by the episode that CAUSED it
        # (cut 9). The handoff writes the durable row BEFORE it folds the
        # dispatch back through the chat episode's cascade, so a crash between
        # those two steps re-runs the whole plan-and-hand-off on the next
        # ignition of the same ask — and this key is what makes that re-run
        # merge with the row already waiting instead of building twice. The
        # cycle_id fallback keeps a payload minted without a parent (a boot
        # restore of pre-cut-9 state) identifiable rather than keyless.
        ident = str(meta.get(CAUSAL_PARENT_KEY) or meta.get("cycle_id") or "")
        return (c.type, ident)
    if c.type == "plugin_permission_decision":
        ident = str(meta.get("request_id") or "")
        return (c.type, ident)
    if c.type == "operator_response":
        ident = str(
            meta.get("system_query_id")
            or meta.get("cycle_id")
            or meta.get("message")
            or meta.get("text")
            or ""
        )
    else:
        ident = str(meta.get("cycle_id") or meta.get("message") or meta.get("text") or "")
    return (c.type, ident)


def _parse_owner(value: object) -> dict[str, object]:
    if (
        type(value) is not dict
        or set(value) != {"request_id", "expected_revision", "arrival_seq", "state"}
        or type(value["request_id"]) is not str
        or _OPAQUE.fullmatch(value["request_id"]) is None
        or type(value["expected_revision"]) is not str
        or _DIGEST.fullmatch(value["expected_revision"]) is None
        or type(value["arrival_seq"]) is not int
        or value["arrival_seq"] <= 0
        or value["state"] not in {"queued", "inflight", "settled"}
    ):
        raise ValueError("operator_queue_state_invalid")
    return dict(value)


def _parse_reservation(value: object, *, legacy: bool = False) -> dict[str, object] | None:
    expected_keys = {"arrival_seq", "request_id", "expected_revision", "expires_at"}
    ordinary = type(value) is dict and "cycle_id" in value
    if ordinary:
        expected_keys.add("cycle_id")
    if not legacy:
        expected_keys.add("process_epoch")
    if (
        type(value) is not dict
        or set(value) != expected_keys
        or type(value["arrival_seq"]) is not int
        or value["arrival_seq"] <= 0
        or type(value["expires_at"]) not in {int, float}
        or not math.isfinite(float(value["expires_at"]))
        or float(value["expires_at"]) <= 0.0
    ):
        raise ValueError("operator_queue_state_invalid")
    request_id = value["request_id"]
    revision = value["expected_revision"]
    if ordinary:
        if (legacy or request_id is not None or revision is not None
                or type(value["cycle_id"]) is not str or not value["cycle_id"]
                or type(value["process_epoch"]) is not str
                or _OPAQUE.fullmatch(value["process_epoch"]) is None):
            raise ValueError("operator_queue_state_invalid")
        return dict(value)
    if legacy and request_id is None and revision is None:
        return None
    if request_id is None or revision is None:
        raise ValueError("operator_queue_state_invalid")
    if (
        type(request_id) is not str
        or _OPAQUE.fullmatch(request_id) is None
        or type(revision) is not str
        or _DIGEST.fullmatch(revision) is None
    ):
        raise ValueError("operator_queue_state_invalid")
    reservation = dict(value)
    if legacy:
        reservation["process_epoch"] = "0" * 32
    elif (
        type(value["process_epoch"]) is not str or _OPAQUE.fullmatch(value["process_epoch"]) is None
    ):
        raise ValueError("operator_queue_state_invalid")
    return reservation


def _ingest_state_row(row: dict[str, object]) -> None:
    global _NEXT_ARRIVAL_SEQ
    state = row.get(_QUEUE_STATE_KEY)
    if type(state) is not dict:
        raise ValueError("operator_queue_state_invalid")
    schema = state.get("schema_version")
    if type(schema) is not int:
        raise ValueError("operator_queue_state_invalid")
    expected_keys = {"schema_version", "plugin_permission_owners"}
    stored_next: int | None = None
    if schema == _QUEUE_STATE_SCHEMA:
        expected_keys.update({"arrival_reservations", "next_arrival_seq"})
        stored_next = state.get("next_arrival_seq")
        if type(stored_next) is not int or stored_next <= 0:
            raise ValueError("operator_queue_state_invalid")
    elif schema == 2:
        expected_keys.add("arrival_reservations")
    elif schema != 1:
        raise ValueError("operator_queue_state_invalid")
    if (
        set(state) != expected_keys
        or type(state["plugin_permission_owners"]) is not list
        or len(state["plugin_permission_owners"]) > _MAX_PLUGIN_PERMISSION_OWNERS
    ):
        raise ValueError("operator_queue_state_invalid")
    owners: dict[str, dict[str, object]] = {}
    for raw_owner in state["plugin_permission_owners"]:
        owner = _parse_owner(raw_owner)
        request_id = str(owner["request_id"])
        if request_id in owners:
            raise ValueError("operator_queue_state_invalid")
        owners[request_id] = owner
    raw_reservations = state.get("arrival_reservations", [])
    if type(raw_reservations) is not list or len(raw_reservations) > _MAX_ARRIVAL_RESERVATIONS:
        raise ValueError("operator_queue_state_invalid")
    reservations: dict[int, dict[str, object]] = {}
    for raw_reservation in raw_reservations:
        reservation = _parse_reservation(raw_reservation, legacy=schema == 2)
        _NEXT_ARRIVAL_SEQ = max(_NEXT_ARRIVAL_SEQ, int(raw_reservation["arrival_seq"]) + 1)
        if reservation is None:
            continue
        arrival_seq = int(reservation["arrival_seq"])
        if arrival_seq in reservations:
            raise ValueError("operator_queue_state_invalid")
        reservations[arrival_seq] = reservation
    _PLUGIN_PERMISSION_OWNERS.clear()
    _PLUGIN_PERMISSION_OWNERS.update(owners)
    for owner in owners.values():
        _NEXT_ARRIVAL_SEQ = max(_NEXT_ARRIVAL_SEQ, int(owner["arrival_seq"]) + 1)
    _ARRIVAL_RESERVATIONS.clear()
    _ARRIVAL_RESERVATIONS.update(reservations)
    if stored_next is not None:
        _NEXT_ARRIVAL_SEQ = max(_NEXT_ARRIVAL_SEQ, stored_next)
    _STATE.loaded = True


def _reservation_identity(reservation: dict[str, object]) -> tuple[object, ...]:
    return (
        reservation["arrival_seq"],
        reservation["request_id"],
        reservation["expected_revision"],
        reservation["expires_at"],
        reservation["process_epoch"],
        reservation.get("cycle_id"),
    )


def _suppress_exact_local_abandonments_locked() -> None:
    for sequence, marker in tuple(_LOCAL_ABANDONED_RESERVATIONS.items()):
        reservation = _ARRIVAL_RESERVATIONS.get(sequence)
        if (
            marker["process_epoch"] != _PROCESS_EPOCH
            or reservation is None
            or _reservation_identity(reservation) != _reservation_identity(marker)
        ):
            _LOCAL_ABANDONED_RESERVATIONS.pop(sequence, None)
            continue
        _ARRIVAL_RESERVATIONS.pop(sequence)


def _register_local_abandonment_locked(reservation: dict[str, object]) -> bool:
    if reservation["process_epoch"] != _PROCESS_EPOCH:
        return False
    sequence = int(reservation["arrival_seq"])
    current = _ARRIVAL_RESERVATIONS.get(sequence)
    if current is None or _reservation_identity(current) != _reservation_identity(reservation):
        if sequence not in _ARRIVAL_RESERVATIONS:
            return False
    existing = _LOCAL_ABANDONED_RESERVATIONS.get(sequence)
    if existing is not None:
        if _reservation_identity(existing) != _reservation_identity(reservation):
            return False
    elif len(_LOCAL_ABANDONED_RESERVATIONS) >= _MAX_LOCAL_ABANDONED_RESERVATIONS:
        raise ValueError("operator_local_abandonment_limit_exceeded")
    else:
        _LOCAL_ABANDONED_RESERVATIONS[sequence] = dict(reservation)
    _ARRIVAL_RESERVATIONS.pop(sequence, None)
    return True


def _prune_expired_reservations_locked() -> bool:
    now = float(_wall_time())
    expired = [
        sequence
        for sequence, reservation in _ARRIVAL_RESERVATIONS.items()
        if reservation["process_epoch"] != _PROCESS_EPOCH
        and float(reservation["expires_at"]) <= now
    ]
    for sequence in expired:
        log.error("Interrupted admission reservation retired: arrival_seq=%s cycle_id=%s",
                  sequence, _ARRIVAL_RESERVATIONS[sequence].get("cycle_id", ""))
        _ARRIVAL_RESERVATIONS.pop(sequence, None)
    return bool(expired)


def _new_reservation_locked(
    *,
    request_id: str,
    expected_revision: str,
    arrival_seq: int,
) -> int:
    global _NEXT_ARRIVAL_SEQ
    pruned = _prune_expired_reservations_locked()
    if len(_ARRIVAL_RESERVATIONS) >= _MAX_ARRIVAL_RESERVATIONS:
        raise ValueError("operator_arrival_reservation_limit_exceeded")
    sequence = arrival_seq
    if type(sequence) is not int or sequence <= 0:
        raise ValueError("operator_arrival_reservation_invalid")
    existing = _ARRIVAL_RESERVATIONS.get(sequence)
    if existing is not None:
        if (
            existing["request_id"] == request_id
            and existing["expected_revision"] == expected_revision
        ):
            if pruned:
                _persist_mutation()
            return sequence
        raise ValueError("operator_arrival_reservation_conflict")
    _ARRIVAL_RESERVATIONS[sequence] = {
        "arrival_seq": sequence,
        "request_id": request_id,
        "expected_revision": expected_revision,
        "expires_at": float(_wall_time()) + _ARRIVAL_RESERVATION_TTL_SECONDS,
        "process_epoch": _PROCESS_EPOCH,
    }
    _NEXT_ARRIVAL_SEQ = max(_NEXT_ARRIVAL_SEQ, sequence + 1)
    _persist_mutation()
    return sequence


def reserve_plugin_permission_arrival(
    request_id: str,
    expected_revision: str,
    arrival_seq: int,
) -> int:
    """Durably stamp an exact permission click before its transport thread hop."""
    if (
        type(request_id) is not str
        or _OPAQUE.fullmatch(request_id) is None
        or type(expected_revision) is not str
        or _DIGEST.fullmatch(expected_revision) is None
    ):
        raise ValueError("plugin_permission_decision_invalid")
    line = _STATE
    with line.lock:
        _ensure_loaded()
        return _new_reservation_locked(
            request_id=request_id,
            expected_revision=expected_revision,
            arrival_seq=arrival_seq,
        )


def reserve_operator_arrival(cycle_id: str, arrival_seq: int | None = None, *, existing_only: bool = False) -> int:
    """Reserve arrival order before screening or a transport thread hop."""
    global _NEXT_ARRIVAL_SEQ
    if type(cycle_id) is not str or not cycle_id.strip():
        raise ValueError("operator_cycle_invalid")
    with _STATE.lock:
        _ensure_loaded()
        sequence = _NEXT_ARRIVAL_SEQ if arrival_seq is None else arrival_seq
        if type(sequence) is not int or sequence <= 0:
            raise ValueError("operator_arrival_reservation_invalid")
        existing = _ARRIVAL_RESERVATIONS.get(sequence)
        if existing is not None:
            if existing.get("cycle_id") != cycle_id or existing["process_epoch"] != _PROCESS_EPOCH:
                raise ValueError("operator_arrival_reservation_conflict")
            return sequence
        if existing_only:
            raise ValueError("operator_arrival_unreserved")
        if len(_ARRIVAL_RESERVATIONS) >= _MAX_ARRIVAL_RESERVATIONS:
            raise ValueError("operator_arrival_reservation_limit_exceeded")
        _ARRIVAL_RESERVATIONS[sequence] = {
            "arrival_seq": sequence, "cycle_id": cycle_id, "request_id": None,
            "expected_revision": None, "process_epoch": _PROCESS_EPOCH,
            "expires_at": float(_wall_time()) + _ARRIVAL_RESERVATION_TTL_SECONDS,
        }
        _NEXT_ARRIVAL_SEQ = max(_NEXT_ARRIVAL_SEQ, sequence + 1)
        try:
            _persist_mutation()
        except BaseException as primary:
            try:
                abandon_arrival_reservation(sequence, cycle_id=cycle_id)
            except BaseException as cleanup:
                primary.add_note(f"Operator arrival cleanup failed: {cleanup!r}")
            raise
        return sequence


def settle_interrupted_operator_arrivals() -> tuple[dict, ...]:
    """Boot-only settlement: pre-screen text was never admitted and cannot replay."""
    with _STATE.lock:
        _ensure_loaded()
        interrupted = tuple(dict(r) for r in _ARRIVAL_RESERVATIONS.values()
                            if "cycle_id" in r and r["process_epoch"] != _PROCESS_EPOCH)
        for reservation in interrupted:
            _ARRIVAL_RESERVATIONS.pop(reservation["arrival_seq"])
        if interrupted:
            _persist_mutation()
        for reservation in interrupted:
            log.error("Operator intake interrupted before admission; resubmit cycle_id=%s arrival_seq=%s",
                      reservation["cycle_id"], reservation["arrival_seq"])
        return interrupted


def abandon_arrival_reservation(
    arrival_seq: int,
    *,
    request_id: str | None = None,
    expected_revision: str | None = None,
    cycle_id: str | None = None,
) -> bool:
    """Release a failed/cancelled intake stamp without touching permission state."""
    line = _STATE
    with line.lock:
        removal_attempted = False
        for attempt in range(2):
            _ensure_loaded()
            reservation = _ARRIVAL_RESERVATIONS.get(arrival_seq)
            if reservation is None:
                return removal_attempted
            if cycle_id is not None and reservation.get("cycle_id") != cycle_id:
                return False
            if request_id is not None and (
                reservation["request_id"] != request_id
                or reservation["expected_revision"] != expected_revision
            ):
                return False
            if reservation["process_epoch"] != _PROCESS_EPOCH:
                return False
            _ARRIVAL_RESERVATIONS.pop(arrival_seq)
            removal_attempted = True
            try:
                _persist_mutation()
            except BaseException:
                if attempt == 1:
                    _register_local_abandonment_locked(reservation)
                    raise
                continue
            return True
        return removal_attempted


def _permission_owner(contributor: Contributor) -> dict[str, object]:
    metadata = contributor.metadata or {}
    return {
        "request_id": str(metadata["request_id"]),
        "expected_revision": str(metadata["expected_revision"]),
        "arrival_seq": int(metadata["arrival_seq"]),
        "state": "queued",
    }


def _prune_settled_owner_for_capacity() -> bool:
    settled = [owner for owner in _PLUGIN_PERMISSION_OWNERS.values() if owner["state"] == "settled"]
    if not settled:
        return False
    oldest = min(settled, key=lambda owner: (int(owner["arrival_seq"]), str(owner["request_id"])))
    _PLUGIN_PERMISSION_OWNERS.pop(str(oldest["request_id"]), None)
    return True


def _resolve_arrival_reservation(contributor: Contributor) -> bool:
    sequence = _arrival_seq(contributor)
    if sequence is None:
        if contributor.type == "plugin_permission_decision":
            raise ValueError("plugin_permission_arrival_unreserved")
        return False
    reservation = _ARRIVAL_RESERVATIONS.get(sequence)
    if reservation is None:
        if contributor.type == "plugin_permission_decision":
            raise ValueError("plugin_permission_arrival_unreserved")
        return False
    metadata = contributor.metadata or {}
    if contributor.type == "plugin_permission_decision":
        if reservation["request_id"] != metadata.get("request_id") or reservation[
            "expected_revision"
        ] != metadata.get("expected_revision"):
            raise ValueError("plugin_permission_arrival_conflict")
    elif reservation["request_id"] is not None:
        raise ValueError("operator_arrival_reservation_conflict")
    elif reservation.get("cycle_id") != metadata.get("cycle_id"):
        raise ValueError("operator_arrival_reservation_conflict")
    _ARRIVAL_RESERVATIONS.pop(sequence)
    return True


def _head_blocked_by_reservation(head: Contributor) -> bool:
    # Reservations are made for operator-authored ingress and are therefore
    # barriers only for Chat. Other lanes preserve their own FIFO independently.
    if lane_for_contributor(head) != "chat":
        return False
    sequence = _arrival_seq(head)
    return sequence is not None and any(
        reserved_sequence < sequence for reserved_sequence in _ARRIVAL_RESERVATIONS
    )


# ── the ingress barrier, made visible (demo track H4) ────────────────────────
#
# The barrier above is correct: it holds the line for an input that already has its arrival_seq but
# has not finished ingress, so a later arrival cannot overtake it (AGENTS.md §4.0). What it was not
# is OBSERVABLE. `_prune_expired_reservations_locked` retires only reservations from a DIFFERENT
# process epoch, so the 60 s TTL cleans up after a restart and never rescues a running process: a
# reservation whose ingress hangs or whose thread dies stops leasing on all four pipelines for as
# long as the process lives, and nothing recorded that it was happening.
#
# A stalled queue and a thinking one looked identical from outside. They no longer do.

#: How long a barrier may hold the line before it is reported as a stall. Not an expiry — the
#: barrier still holds, because forfeiting a turn's place is a ruling this code may not make on its
#: own. It is the point at which silence becomes a named, attributed event.
INGRESS_STALL_BUDGET_S = 5.0

_BARRIER: dict[str, object] = {}
_STALL_REPORTER: object = None


def set_ingress_stall_reporter(reporter: object) -> None:
    """Install the sink an over-budget barrier is reported to (tests, telemetry). ``None`` clears."""
    global _STALL_REPORTER
    _STALL_REPORTER = reporter


def _force_ingress_barrier_age_for_tests(seconds: float) -> None:
    """Age the current barrier, so a budget can be exercised without waiting for it."""
    with _STATE.lock:
        _note_barrier_locked(True)
        if _BARRIER:
            _BARRIER["since"] = float(_wall_time()) - float(seconds)


def _blocking_reservation_locked() -> tuple[int, dict[str, object]] | None:
    """The earliest outstanding reservation that is actually holding queued work back.

    An outstanding reservation with nothing behind it blocks nobody: the barrier exists only when
    a queued item has a LATER arrival than an ingress that has not finished.
    """
    if not _ARRIVAL_RESERVATIONS:
        return None
    sequence = min(_ARRIVAL_RESERVATIONS)
    if not any(
        lane_for_contributor(item) == "chat"
        and (seq := _arrival_seq(item)) is not None
        and seq > sequence
        for item in _STATE.queue
    ):
        return None
    return sequence, dict(_ARRIVAL_RESERVATIONS[sequence])


def _note_barrier_locked(blocked: bool, *, pipeline_id: int | None = None) -> None:
    """Track when the current barrier began, and which pipelines it turned away."""
    found = _blocking_reservation_locked() if blocked else None
    if found is None:
        _BARRIER.clear()
        return
    sequence, reservation = found
    if _BARRIER.get("arrival_seq") != sequence:
        _BARRIER.clear()
        _BARRIER.update({
            "arrival_seq": sequence,
            "cycle_id": str(reservation.get("cycle_id") or ""),
            "process_local": reservation.get("process_epoch") == _PROCESS_EPOCH,
            "since": float(_wall_time()),
            "pipelines": set(),
            "reported": False,
        })
    if pipeline_id is not None:
        pipelines = _BARRIER.get("pipelines")
        if isinstance(pipelines, set):
            pipelines.add(int(pipeline_id))


def _render_barrier_locked() -> dict[str, object]:
    if not _BARRIER:
        return {"blocked": False, "arrival_seq": None, "cycle_id": None, "held_for_s": None,
                "process_local": None, "pipelines_blocked": 0, "over_budget": False}
    pipelines = _BARRIER.get("pipelines")
    held = max(0.0, float(_wall_time()) - float(_BARRIER["since"]))  # type: ignore[arg-type]
    return {
        "blocked": True,
        "arrival_seq": _BARRIER.get("arrival_seq"),
        "cycle_id": _BARRIER.get("cycle_id"),
        "held_for_s": held,
        "process_local": _BARRIER.get("process_local"),
        "pipelines_blocked": len(pipelines) if isinstance(pipelines, set) else 0,
        "over_budget": held >= INGRESS_STALL_BUDGET_S,
    }


def ingress_barrier_state() -> dict[str, object]:
    """Whether an earlier ingress is holding the line, which one, and for how long.

    It never leases, releases or consumes anything — the only thing it writes is the clock the
    barrier is timed from, which starts on first observation whether that observation came from a
    lease attempt or from this read. An unblocked queue reports no duration at all rather than
    zero: "nothing is held" and "held for no time" are different statements.
    """
    with _STATE.lock:
        _ensure_loaded()
        _note_barrier_locked(True)
        return _render_barrier_locked()


def _report_stall_if_over_budget_locked() -> None:
    """Emit an over-budget barrier once, with its cause. Never raises: a telemetry channel is not
    a dependency of admission (§2.2)."""
    if not _BARRIER or _BARRIER.get("reported"):
        return
    state = _render_barrier_locked()
    if not state["over_budget"]:
        return
    _BARRIER["reported"] = True
    payload = dict(state)
    payload["reason"] = (
        "ingress barrier held the queue: an earlier arrival has its sequence but has not "
        "completed ingress, so no pipeline may lease. A same-process reservation does not expire."
    )
    log.error(
        "Queue held by ingress barrier for %.1fs: arrival_seq=%s cycle_id=%s pipelines=%s",
        state["held_for_s"], state["arrival_seq"], state["cycle_id"], state["pipelines_blocked"],
    )
    reporter = _STALL_REPORTER
    if reporter is not None:
        try:
            reporter(payload)  # type: ignore[operator]
        except Exception:
            log.debug("ingress stall reporter failed (non-fatal)", exc_info=True)
        return
    try:
        from guardian.terminal_feedback import emit_data

        emit_data("pressure", "ingress_barrier_stall", status="degraded", ok=False,
                  summary=f"queue held {state['held_for_s']:.1f}s by arrival "
                          f"{state['arrival_seq']}",
                  **{key: payload[key] for key in
                     ("arrival_seq", "cycle_id", "held_for_s", "process_local",
                      "pipelines_blocked")})
    except Exception:
        log.debug("ingress barrier stall not emitted (non-fatal)", exc_info=True)


def _migrated_target(legacy: Path) -> Path:
    """A free ``.migrated`` name — an earlier migration's copy is NEVER lost."""
    target = legacy.with_name(legacy.name + ".migrated")
    n = 2
    while target.exists():
        target = legacy.with_name(f"{legacy.name}.migrated.{n}")
        n += 1
    return target
