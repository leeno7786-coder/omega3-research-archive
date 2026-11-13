"""Action ledger — durable lineage for every signed plugin operation.

Every plugin operation mints a lineage record BEFORE dispatch and closes it
with the MCP receipt, so

  1. a goal ends at ``confirmed`` (receipt metabolized), not at dispatch — the
     dispatched→confirmed/failed state is the workflow-closure primitive; and
  2. when the future sensory sync observes a provider object Omega itself created,
     ``match_receipt(provider_object_id)`` identifies it as the RECEIPT for an existing
     action (``caused_by_omega=True``) — NOT new external stimulation. That is the
     loop-prevention edge: without it, bidirectional sync self-poisons (Omega observes
     its own writes as fresh input, forever).

Idempotency lives at both boundaries: ``omega_action_id`` de-dupes Omega-side retries,
``provider_request_id`` is the stable token for provider-side retries.

Durable, plaintext-safe (ids + metadata, no tokens/secrets), atomic tmp+replace writes,
RLock-guarded, fail-closed reads. Lives under ``guardian/state/mcp`` (gitignored) and is
shared by signed MCP plugin operations.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from mcp_platform.file_lock import file_lock

log = logging.getLogger("omega.mcp.action_ledger")

_lock = threading.RLock()
_UNREADABLE_MESSAGE = "action ledger state is unreadable"
# Lifecycle: dispatched -> confirmed (provider receipt) | failed (DEFINITIVE pre-commit
# rejection — safe to retry) | outcome_unknown (AMBIGUOUS: timeout/disconnect/5xx after
# the provider MAY have committed — never blind-retried; reconciled by a later receipt
# observation or operator action). A future async receipt lands on `confirmed` via
# match_receipt + record_receipt — same terminal state, same semantics.
STATES = ("dispatched", "confirmed", "failed", "outcome_unknown")


class ActionLedgerAmbiguousMatch(RuntimeError):
    """A provider observation matched more than one Omega write lineage."""


class ActionLedgerUnreadable(RuntimeError):
    """The durable action ledger exists but cannot be read as valid state."""


def _reject_non_json_constant(_value: str) -> None:
    raise ValueError


def _default_path() -> Path:
    from core.paths import MCP_ACTION_LEDGER_PATH

    return MCP_ACTION_LEDGER_PATH


def _load_existing(path: Path) -> Optional[Dict[str, Dict[str, Any]]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError):
        raise ActionLedgerUnreadable(_UNREADABLE_MESSAGE) from None
    try:
        data = json.loads(raw, parse_constant=_reject_non_json_constant)
    except (TypeError, ValueError):
        raise ActionLedgerUnreadable(_UNREADABLE_MESSAGE) from None
    if not isinstance(data, dict) or any(not isinstance(record, dict) for record in data.values()):
        raise ActionLedgerUnreadable(_UNREADABLE_MESSAGE) from None
    return data


def _load(path: Path) -> Dict[str, Dict[str, Any]]:
    return _load_existing(path) or {}


def _sync_directory(directory: Path) -> None:
    """Durably publish directory-entry changes where the platform supports it."""
    if os.name == "nt":
        # CPython cannot open directory handles for fsync on Windows. The file data is
        # flushed above and os.replace uses the platform's atomic rename primitive.
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(directory, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sync_parent_directory(path: Path) -> None:
    _sync_directory(path.parent)


def _save(path: Path, store: Dict[str, Dict[str, Any]]) -> None:
    try:
        serialized = json.dumps(store, indent=2, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, OverflowError):
        raise ActionLedgerUnreadable(_UNREADABLE_MESSAGE) from None
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    # fsync before the atomic swap — lineage is the idempotency/loop-prevention record;
    # a crash between write and flush must not lose a dispatched-state entry.
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(serialized)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)  # atomic swap
        _sync_parent_directory(path)
    except OSError:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise
    # Cross-process safety (phase-2 sensory, 2026-07-18): every MUTATOR holds the
    # sidecar file_lock across its whole read-modify-write, so the runtime and any
    # worker process serialize on the same ledger. Readers stay lock-free — the
    # atomic replace above guarantees they always see a complete document.


@contextmanager
def _action_ledger_process_lock(
    *,
    timeout_s: float | None = None,
) -> Iterator[None]:
    """Acquire the reentrant process lock within the caller's lock budget."""
    try:
        acquired = _lock.acquire() if timeout_s is None else _lock.acquire(timeout=timeout_s)
    except (OSError, OverflowError, ValueError):
        raise ActionLedgerUnreadable(_UNREADABLE_MESSAGE) from None
    if not acquired:
        raise ActionLedgerUnreadable(_UNREADABLE_MESSAGE)
    try:
        yield
    finally:
        _lock.release()


@contextmanager
def _action_ledger_file_lock(
    path: Path,
    *,
    timeout_s: float | None = None,
) -> Iterator[None]:
    """Map sidecar acquisition and locked storage failures to the public error."""
    try:
        lock = file_lock(path) if timeout_s is None else file_lock(path, timeout=timeout_s)
        with lock:
            yield
    except OSError:
        raise ActionLedgerUnreadable(_UNREADABLE_MESSAGE) from None


@contextmanager
def _default_store(
    *,
    lock_timeout_s: float | None = None,
) -> Iterator[tuple[Path, Dict[str, Dict[str, Any]]]]:
    canonical_path = _default_path()
    with (
        _action_ledger_process_lock(timeout_s=lock_timeout_s),
        _action_ledger_file_lock(canonical_path, timeout_s=lock_timeout_s),
    ):
        yield canonical_path, _load(canonical_path)


@contextmanager
def _mutation_store(
    path: Optional[Path],
    *,
    lock_timeout_s: float | None = None,
) -> Iterator[tuple[Path, Dict[str, Dict[str, Any]]]]:
    if path is None:
        with _default_store(lock_timeout_s=lock_timeout_s) as resolved:
            yield resolved
        return
    with (
        _action_ledger_process_lock(timeout_s=lock_timeout_s),
        _action_ledger_file_lock(path, timeout_s=lock_timeout_s),
    ):
        yield path, _load(path)


def _read_store(path: Optional[Path]) -> Dict[str, Dict[str, Any]]:
    if path is None:
        with _default_store() as (_resolved_path, store):
            return store
    with _action_ledger_process_lock():
        return _load(path)


def _now_iso() -> str:
    from core.symphony_time import now_iso

    return now_iso()


def mint_action_id() -> str:
    return f"act_{uuid.uuid4().hex[:16]}"


def _build_dispatch_record(
    *,
    omega_action_id: str,
    episode_id: str,
    capability: str,
    operation: str,
    is_write: bool,
    request_id: str = "",
    server_id: str = "",
    server_version: str = "",
    artifact_sha256: str = "",
    provider: str = "google",
    account: str = "",
    connection_id: str = "",
    tool_schema_sha256: str = "",
    risk_class: str = "",
    authority_kind: str = "",
    authority_id: str = "",
    authority_revision: str = "",
    proposal_digest: str = "",
    source_constraints_sha256: str = "",
    destination_constraints_sha256: str = "",
    provider_request_id: Optional[str] = None,
    provider_correlation_id: str = "",
    idempotency_key: str = "",
    connection_revision: str = "",
    job_revision: str = "",
    grant_id: str = "",
    grant_approval_digest: str = "",
    expected_revision: str = "",
) -> Dict[str, Any]:
    provider_request_token = str(provider_request_id or uuid.uuid4())
    correlation_id = str(provider_correlation_id or "")
    omega_idempotency = str(idempotency_key or "")
    return {
        "omega_action_id": str(omega_action_id),
        "originating_cycle_id": str(episode_id),
        "request_id": str(request_id or ""),
        "server_id": str(server_id or ""),
        "server_version": str(server_version or ""),
        "artifact_sha256": str(artifact_sha256 or ""),
        "provider": str(provider),
        "account": str(account or ""),
        "connection_id": str(connection_id or ""),
        "capability": str(capability),
        "operation": str(operation),
        "tool_schema_sha256": str(tool_schema_sha256 or ""),
        "risk_class": str(risk_class or ""),
        "authority_kind": str(authority_kind or ""),
        "authority_id": str(authority_id or ""),
        "authority_revision": str(authority_revision or ""),
        "proposal_digest": str(proposal_digest or ""),
        "source_constraints_sha256": str(source_constraints_sha256 or ""),
        "destination_constraints_sha256": str(destination_constraints_sha256 or ""),
        "is_write": bool(is_write),
        "caused_by_omega": True,
        "provider_request_id": provider_request_token,
        "provider_correlation_id": correlation_id,
        "idempotency_key": omega_idempotency,
        "provider_idempotency": {
            "provider_request_id": provider_request_token,
            "provider_correlation_id": correlation_id,
            "idempotency_key": omega_idempotency,
        },
        "connection_revision": str(connection_revision or ""),
        "job_revision": str(job_revision or ""),
        "grant_id": str(grant_id or ""),
        "grant_approval_digest": str(grant_approval_digest or ""),
        "expected_revision": str(expected_revision or ""),
        "provider_object_id": "",
        "provider_revision": "",
        "state": "dispatched",
        "dispatched_at": _now_iso(),
        "closed_at": "",
        "receipt": {},
        "failure_reason": "",
    }


def record_dispatch(
    *,
    omega_action_id: str,
    episode_id: str,
    capability: str,
    operation: str,
    is_write: bool,
    request_id: str = "",
    server_id: str = "",
    server_version: str = "",
    artifact_sha256: str = "",
    provider: str = "google",
    account: str = "",
    connection_id: str = "",
    tool_schema_sha256: str = "",
    risk_class: str = "",
    authority_kind: str = "",
    authority_id: str = "",
    authority_revision: str = "",
    proposal_digest: str = "",
    source_constraints_sha256: str = "",
    destination_constraints_sha256: str = "",
    provider_request_id: Optional[str] = None,
    provider_correlation_id: str = "",
    idempotency_key: str = "",
    connection_revision: str = "",
    job_revision: str = "",
    grant_id: str = "",
    grant_approval_digest: str = "",
    expected_revision: str = "",
    path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Mint the lineage record BEFORE the provider call. Returns the record.

    ``idempotency_key`` is the deterministic Omega-side dedup key (digest of episode +
    spec); ``provider_request_id`` is the provider-side token (for Calendar it doubles as
    the caller-supplied event id, so a provider retry cannot double-create).
    ``provider_correlation_id`` is a marker that survives provider-side object lifecycle
    changes — for Gmail sends, the draft's thread id (the draft id dies at send while the
    sent message remains in the same thread)."""
    record = _build_dispatch_record(
        omega_action_id=omega_action_id,
        episode_id=episode_id,
        capability=capability,
        operation=operation,
        is_write=is_write,
        request_id=request_id,
        server_id=server_id,
        server_version=server_version,
        artifact_sha256=artifact_sha256,
        provider=provider,
        account=account,
        connection_id=connection_id,
        tool_schema_sha256=tool_schema_sha256,
        risk_class=risk_class,
        authority_kind=authority_kind,
        authority_id=authority_id,
        authority_revision=authority_revision,
        proposal_digest=proposal_digest,
        source_constraints_sha256=source_constraints_sha256,
        destination_constraints_sha256=destination_constraints_sha256,
        provider_request_id=provider_request_id,
        provider_correlation_id=provider_correlation_id,
        idempotency_key=idempotency_key,
        connection_revision=connection_revision,
        job_revision=job_revision,
        grant_id=grant_id,
        grant_approval_digest=grant_approval_digest,
        expected_revision=expected_revision,
    )
    with _mutation_store(path) as (ledger_path, store):
        store[record["omega_action_id"]] = record
        _save(ledger_path, store)
    return dict(record)


def find_or_record_dispatch(
    *,
    omega_action_id: str,
    episode_id: str,
    capability: str,
    operation: str,
    is_write: bool,
    request_id: str = "",
    server_id: str = "",
    server_version: str = "",
    artifact_sha256: str = "",
    provider: str = "google",
    account: str = "",
    connection_id: str = "",
    tool_schema_sha256: str = "",
    risk_class: str = "",
    authority_kind: str = "",
    authority_id: str = "",
    authority_revision: str = "",
    proposal_digest: str = "",
    source_constraints_sha256: str = "",
    destination_constraints_sha256: str = "",
    provider_request_id: Optional[str] = None,
    provider_correlation_id: str = "",
    idempotency_key: str = "",
    connection_revision: str = "",
    job_revision: str = "",
    grant_id: str = "",
    grant_approval_digest: str = "",
    expected_revision: str = "",
    path: Optional[Path] = None,
) -> tuple[Dict[str, Any], bool]:
    """Atomically return a prior nonfailed dispatch or publish a new one."""
    record = _build_dispatch_record(
        omega_action_id=omega_action_id,
        episode_id=episode_id,
        capability=capability,
        operation=operation,
        is_write=is_write,
        request_id=request_id,
        server_id=server_id,
        server_version=server_version,
        artifact_sha256=artifact_sha256,
        provider=provider,
        account=account,
        connection_id=connection_id,
        tool_schema_sha256=tool_schema_sha256,
        risk_class=risk_class,
        authority_kind=authority_kind,
        authority_id=authority_id,
        authority_revision=authority_revision,
        proposal_digest=proposal_digest,
        source_constraints_sha256=source_constraints_sha256,
        destination_constraints_sha256=destination_constraints_sha256,
        provider_request_id=provider_request_id,
        provider_correlation_id=provider_correlation_id,
        idempotency_key=idempotency_key,
        connection_revision=connection_revision,
        job_revision=job_revision,
        grant_id=grant_id,
        grant_approval_digest=grant_approval_digest,
        expected_revision=expected_revision,
    )
    key = record["idempotency_key"]
    digest = record["proposal_digest"]
    with _mutation_store(path) as (ledger_path, store):
        matches = [
            prior
            for prior in store.values()
            if (key and prior.get("idempotency_key") == key)
            or (digest and prior.get("proposal_digest") == digest)
        ]
        if matches:
            matches.sort(key=lambda item: str(item.get("dispatched_at") or ""))
            for prior in reversed(matches):
                if (
                    key
                    and prior.get("idempotency_key") == key
                    and prior.get("proposal_digest") != digest
                ):
                    return dict(prior), False
            for prior in reversed(matches):
                if prior.get("state") != "failed":
                    return dict(prior), False
            prior = matches[-1]
            if prior.get("idempotency_key") != key or prior.get("proposal_digest") != digest:
                return dict(prior), False
        store[record["omega_action_id"]] = record
        _save(ledger_path, store)
    return dict(record), True


def find_by_idempotency(
    idempotency_key: str, *, path: Optional[Path] = None
) -> Optional[Dict[str, Any]]:
    """Omega-side dedup: the most recent record for this key, or None.

    Callers must NOT blind-retry a write whose prior record is still ``dispatched``
    (ambiguous — the provider call may have landed); ``confirmed`` = replay the receipt;
    ``failed`` = safe to retry with a fresh action id (same key)."""
    key = str(idempotency_key or "").strip()
    if not key:
        return None
    matches = [
        record for record in _read_store(path).values() if record.get("idempotency_key") == key
    ]
    if not matches:
        return None
    matches.sort(key=lambda r: str(r.get("dispatched_at") or ""))
    return dict(matches[-1])


# Legal state transitions (round-6: compare-and-set — a terminal state can never be
# clobbered; the probe that turned confirmed→failed is structurally impossible now).
_RECEIPT_FROM = ("dispatched", "outcome_unknown")
_FAILURE_FROM = ("dispatched", "outcome_unknown", "failed")
_STRUCTURAL_RECEIPT_KEYS = frozenset(
    {
        "kind",
        "id",
        "provider_id",
        "provider_object_id",
        "count",
        "etag",
        "updated",
        "status",
        "http_status",
        "revision_id",
        "provider_revision",
        "thread_id",
        "label_ids",
        "deduped",
        "via",
        "observed_at",
        "event_kind",
        "capability",
        "omega_action_id",
    }
)


def _structural_receipt(receipt: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(receipt, dict):
        return {}
    return {
        str(key): value
        for key, value in receipt.items()
        if key in _STRUCTURAL_RECEIPT_KEYS and value is not None
    }


def record_receipt(
    omega_action_id: str,
    *,
    provider_object_id: str = "",
    provider_revision: str = "",
    receipt: Optional[Dict[str, Any]] = None,
    path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Close the action with the provider receipt -> ``confirmed`` (workflow closure).

    Compare-and-set: legal only from ``dispatched``/``outcome_unknown``. An already
    ``confirmed`` action returns itself unchanged (idempotent re-receipt); a ``failed``
    action REFUSES (None) — a definitive failure never silently becomes a success."""
    with _mutation_store(path) as (ledger_path, store):
        record = store.get(str(omega_action_id))
        if record is None:
            log.warning("action ledger: receipt for unknown action %s", omega_action_id)
            return None
        state = str(record.get("state") or "")
        if state == "confirmed":
            return dict(record)  # idempotent — the receipt already landed
        if state not in _RECEIPT_FROM:
            log.warning(
                "action ledger: ILLEGAL transition %s→confirmed refused for %s",
                state,
                omega_action_id,
            )
            return None
        record["state"] = "confirmed"
        record["provider_object_id"] = str(provider_object_id or "")
        record["provider_revision"] = str(provider_revision or "")
        record["receipt"] = _structural_receipt(receipt)
        record["closed_at"] = _now_iso()
        _save(ledger_path, store)
        return dict(record)


def record_failure(
    omega_action_id: str,
    reason: str,
    *,
    ambiguous: bool = False,
    path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Close (or park) a failed action.

    ``ambiguous=True`` → state ``outcome_unknown`` (timeout/disconnect/5xx AFTER the
    provider may have committed): NOT retryable until reconciled. ``ambiguous=False`` →
    state ``failed`` (definitive pre-commit rejection): safe to retry under the same
    idempotency key. Compare-and-set: a ``confirmed`` action REFUSES (None) — the
    receipt is truth; a late failure report can never un-confirm it."""
    with _mutation_store(path) as (ledger_path, store):
        record = store.get(str(omega_action_id))
        if record is None:
            return None
        state = str(record.get("state") or "")
        if state not in _FAILURE_FROM:
            log.warning(
                "action ledger: ILLEGAL transition %s→%s refused for %s",
                state,
                "outcome_unknown" if ambiguous else "failed",
                omega_action_id,
            )
            return None
        record["state"] = "outcome_unknown" if ambiguous else "failed"
        record["failure_reason"] = str(reason or "")
        record["closed_at"] = "" if ambiguous else _now_iso()  # unknown = still open
        _save(ledger_path, store)
        return dict(record)


def record_switch_deadline_batch(
    actions: Mapping[str, bool],
    *,
    failure_reason: str,
    path: Optional[Path] = None,
    lock_timeout_s: float = 0.0,
) -> Dict[str, str]:
    """Atomically finalize every durable action unresolved at a control deadline.

    The mapping value is whether provider dispatch made the action ambiguous.
    The whole batch is verified before one durable publication; any missing or
    CAS-incompatible action rejects the batch without a partial write.
    """
    if (
        not isinstance(actions, Mapping)
        or any(type(action_id) is not str or not action_id for action_id in actions)
        or any(type(ambiguous) is not bool for ambiguous in actions.values())
        or type(failure_reason) is not str
        or failure_reason not in {"external_mcp_switched_off", "package_disabled"}
    ):
        raise ActionLedgerUnreadable(_UNREADABLE_MESSAGE)
    if not actions:
        return {}
    timeout = float(lock_timeout_s)
    if timeout < 0:
        raise ActionLedgerUnreadable(_UNREADABLE_MESSAGE)
    finalized: Dict[str, str] = {}
    with _mutation_store(path, lock_timeout_s=timeout) as (ledger_path, store):
        changed = False
        closed_at = _now_iso()
        for action_id, ambiguous in actions.items():
            record = store.get(action_id)
            if record is None:
                raise ActionLedgerUnreadable(_UNREADABLE_MESSAGE)
            target = "outcome_unknown" if ambiguous else "failed"
            state = str(record.get("state") or "")
            if state == target:
                if record.get("failure_reason") != failure_reason:
                    raise ActionLedgerUnreadable(_UNREADABLE_MESSAGE)
                finalized[action_id] = target
                continue
            if state not in _FAILURE_FROM:
                raise ActionLedgerUnreadable(_UNREADABLE_MESSAGE)
            record["state"] = target
            record["failure_reason"] = failure_reason
            record["closed_at"] = "" if ambiguous else closed_at
            finalized[action_id] = target
            changed = True
        if changed:
            _save(ledger_path, store)
    return finalized


def get_action(omega_action_id: str, *, path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    record = _read_store(path).get(str(omega_action_id))
    return dict(record) if record else None


def find_latest_confirmed(
    *,
    capability: str,
    operation: str,
    account: Optional[str] = None,
    path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Newest CONFIRMED action for (capability, operation) — deterministic reference
    resolution ("send THE draft" = the most recent draft Omega itself created, by the
    ledger, never by model guessing)."""
    matches = [
        r
        for r in _read_store(path).values()
        if r.get("state") == "confirmed"
        and r.get("capability") == str(capability)
        and r.get("operation") == str(operation)
        and (account is None or str(r.get("account") or "") == str(account))
    ]
    if not matches:
        return None
    matches.sort(key=lambda r: str(r.get("closed_at") or r.get("dispatched_at") or ""))
    return dict(matches[-1])


def _find_open(
    field: str,
    value: str,
    *,
    provider: Optional[str],
    account: Optional[str],
    path: Optional[Path],
) -> Optional[Dict[str, Any]]:
    """Newest OPEN (dispatched / outcome_unknown) action where ``field`` == ``value``,
    ATOMICALLY scoped (round-6): provider/account filter INSIDE the query — never
    select-newest-then-filter (which missed an older correct-tenant record or closed
    the wrong tenant). When ``account`` is omitted and the matches span more than one
    account, the lookup REFUSES (None) — an ambiguous closure is worse than none."""
    needle = str(value or "").strip()
    if not needle:
        return None
    matches = [
        r
        for r in _read_store(path).values()
        if str(r.get(field) or "") == needle
        and r.get("state") in ("dispatched", "outcome_unknown")
        and (provider is None or str(r.get("provider") or "") == str(provider))
        and (account is None or str(r.get("account") or "") == str(account))
    ]
    if not matches:
        return None
    if account is None and len({str(r.get("account") or "") for r in matches}) > 1:
        log.warning(
            "action ledger: open-%s lookup for %r spans multiple accounts — refusing "
            "the ambiguous match",
            field,
            needle,
        )
        return None
    matches.sort(key=lambda r: str(r.get("dispatched_at") or ""))
    return dict(matches[-1])


def find_open_by_request_id(
    provider_request_id: str,
    *,
    provider: Optional[str] = "google",
    account: Optional[str] = None,
    path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Newest OPEN action carrying this provider-side request token, scoped to
    provider/account (for gmail sends the token is the draft id, so provider state can
    prove/deny the commit later)."""
    return _find_open(
        "provider_request_id", provider_request_id, provider=provider, account=account, path=path
    )


def find_open_by_correlation(
    provider_correlation_id: str,
    *,
    provider: Optional[str] = "google",
    account: Optional[str] = None,
    path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Newest OPEN action carrying this lifecycle-stable correlation marker.

    For Gmail sends this is the draft's thread id, which remains stable when the draft
    becomes a sent message. Gmail rewrites the RFC Message-ID during that lifecycle.
    """
    return _find_open(
        "provider_correlation_id",
        provider_correlation_id,
        provider=provider,
        account=account,
        path=path,
    )


def match_receipt(
    provider_object_id: str,
    *,
    provider: str = "google",
    account: Optional[str] = None,
    observed_revision: Optional[str] = None,
    path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Loop-prevention lookup: is this observed provider object Omega's OWN write?

    Suppression is scoped to the EXACT KNOWN REVISION: when the caller supplies
    ``observed_revision`` and the record captured one at receipt time, they must be
    equal — a LATER revision (a human edited the Omega-created object) is a legitimate
    new change and must NOT be suppressed (returns None). When either side lacks a
    revision the object-id match stands (best available evidence; the sensory layer
    should then fall back to content-hash comparison). ``provider``/``account`` scope
    the match so multi-tenant sync can never cross-suppress.
    """
    poid = str(provider_object_id or "").strip()
    if not poid:
        return None
    for record in _read_store(path).values():
        if not record.get("caused_by_omega"):
            continue
        if record.get("provider_object_id") != poid:
            continue
        if str(record.get("provider") or "") != str(provider or ""):
            continue
        if account is not None and str(record.get("account") or "") != str(account):
            continue
        known_rev = str(record.get("provider_revision") or "")
        if observed_revision is not None:
            # SUPPRESSION REQUIRES EVIDENCE (round-3 P1): when the observer supplies
            # a revision, suppress ONLY on an exact match with a KNOWN stored
            # revision. A stored-empty record cannot prove the echo — fail OPEN
            # toward Guardian intake, never toward suppression. A mismatch keeps
            # SEARCHING (Omega may have written the same object at several
            # revisions) instead of returning None at the first miss.
            if not known_rev or str(observed_revision) != known_rev:
                continue
        # observed_revision=None is CORRELATION mode (internal lookups) — an
        # object-id match returns the record but is NOT sufficient evidence for
        # sensory suppression; the sync worker must always pass the revision.
        return dict(record)
    return None


def find_confirmed_write_for_provider_object(
    provider_object_id: str,
    *,
    provider: str = "google",
    account: Optional[str] = None,
    omega_write_lineage_id: str = "",
    idempotency_key: str = "",
    path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Return the single confirmed Omega write proven by object id plus lineage.

    Content never participates in this lookup. A provider echo is suppressible only
    when the observed object id matches the ledger receipt and the observation also
    carries ledger evidence: the action id or the idempotency key. Multiple matches
    are not a confidence signal; they are ambiguous and must proceed as external
    ingress.
    """
    poid = str(provider_object_id or "").strip()
    lineage = str(omega_write_lineage_id or "").strip()
    idem = str(idempotency_key or "").strip()
    if not poid or not (lineage or idem):
        return None
    matches = [
        record
        for record in _read_store(path).values()
        if record.get("caused_by_omega")
        and record.get("is_write")
        and record.get("state") == "confirmed"
        and str(record.get("provider_object_id") or "") == poid
        and str(record.get("provider") or "") == str(provider or "")
        and (account is None or str(record.get("account") or "") == str(account))
        and (
            (lineage and str(record.get("omega_action_id") or "") == lineage)
            or (lineage and str(record.get("idempotency_key") or "") == lineage)
            or (idem and str(record.get("idempotency_key") or "") == idem)
        )
    ]
    if not matches:
        return None
    action_ids = {str(record.get("omega_action_id") or "") for record in matches}
    if len(action_ids) > 1:
        raise ActionLedgerAmbiguousMatch(
            f"provider object {poid!r} matched multiple Omega write lineages"
        )
    matches.sort(key=lambda r: str(r.get("closed_at") or r.get("dispatched_at") or ""))
    return dict(matches[-1])


def record_provider_observation_confirmation(
    omega_action_id: str,
    *,
    provider_object_id: str,
    provider_revision: str = "",
    receipt: Optional[Dict[str, Any]] = None,
    path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Confirm an observed provider echo and advance its revision metadata.

    This is separate from ``record_receipt`` so re-receipts remain idempotent for
    normal action closure, while a later plugin observation can record provider
    revision evidence for the same Omega-authored object without relearning its
    payload.
    """
    poid = str(provider_object_id or "").strip()
    with _mutation_store(path) as (ledger_path, store):
        record = store.get(str(omega_action_id))
        if record is None:
            log.warning(
                "action ledger: provider observation for unknown action %s",
                omega_action_id,
            )
            return None
        state = str(record.get("state") or "")
        known_object = str(record.get("provider_object_id") or "")
        if known_object and poid and known_object != poid:
            log.warning(
                "action ledger: provider observation object mismatch for %s (%s != %s)",
                omega_action_id,
                known_object,
                poid,
            )
            return None
        if state in _RECEIPT_FROM:
            record["state"] = "confirmed"
            record["closed_at"] = _now_iso()
        elif state != "confirmed":
            log.warning(
                "action ledger: ILLEGAL transition %s→confirmed refused for %s",
                state,
                omega_action_id,
            )
            return None
        if poid:
            record["provider_object_id"] = poid
        if provider_revision:
            record["provider_revision"] = str(provider_revision)
        if receipt is not None:
            record["receipt"] = _structural_receipt(dict(receipt))
        if not str(record.get("closed_at") or ""):
            record["closed_at"] = _now_iso()
        _save(ledger_path, store)
        return dict(record)
