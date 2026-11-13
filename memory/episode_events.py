"""Owner-bound episode event capture, through the existing journal and SRAM (WO-03).

WO-01 measured this as the program's second missing capability: phase facts survived only in
the final enriched body and in ``guardian.terminal_feedback.emit_data`` calls wrapped in bare
excepts, so a lost emission and an absent fact were the same observation. This is the one
append/read API every stage uses.

**It adds no new substrate.** Durability is ``memory/intake_journal.py`` — its fsync append,
its record identity, its conflict detection, its release/adopt and reownership paths. Working
state is ``memory/episode_sram.py`` — its buckets, its owner authorization, its locks. There is
no sidecar log, no canonical writer, and no second clock or sequence authority here.

Four properties carry the order:

* **One real operation is one event, forever.** The event id is derived from the episode and the
  operation token, so a retry — in this process or after a restart — RETRIEVES the original
  occurrence before anything allocates a sequence or a timestamp. Two different tokens are two
  events even when their payload bytes are identical. A retry's immutable inputs are compared as
  CANONICAL BYTES, so a JSON type substitution is a conflict rather than a silent replay.
* **Ownership is authorized before mutation.** The CALLER must hold the live owner binding for
  the episode it names — a valid open target is not proof that the caller owns it. The binding is
  rechecked before acknowledgement, so a lost owner leaves recoverable unacknowledged work rather
  than redirecting the event into another bucket.
* **Durable journal acknowledgement precedes successful capture.** The row is proven durably
  accepted before the event reaches the bucket, so a crash in between leaves the event durable
  and rehydratable — never acknowledged-but-lost. Bytes that are merely READABLE are not that
  proof: a failed fsync leaves them behind.
* **A journal-backed event verifies its own integrity claim before use.** One decoder serves
  reads, retries and boot adoption; it recomputes the sealed digest and checks the row's kind,
  identity and source linkage. A recognizable row that cannot be verified fails the read and
  never becomes an empty success.
* **Recovery moves ownership, not history.** ``reown_record`` changes who owes the row; the
  event's own sealed episode, thread, sequence, timestamp and attribution are fixed at capture.

A pending row is NOT recallable. It stays ``PENDING`` until DeepSleep publishes it, which is
WO-07's work, not this order's.
"""

from __future__ import annotations

import threading
import uuid
from contextlib import contextmanager
from typing import Any, Iterator, Mapping

from schema.continuity_json import (
    ContinuityContractError,
    EpisodeCaptureUnavailable,
    EpisodeOwnershipError,
    EventIdentityConflict,
    Observation,
    canonical_bytes,
    content_hash,
    freeze,
    require_enum,
    require_text,
    require_texts,
)
from schema.episode_event import (
    EPISODE_PHASES,
    EVIDENCE_CLASSES,
    MAX_EVENT_KIND,
    MAX_REFS,
    SCHEMA_VERSION as EVENT_SCHEMA_VERSION,
    EpisodeEvent,
)

#: The intake-journal ``kind`` these rows carry. Recovery classifies on it.
EVENT_JOURNAL_KIND = "episode_event"

#: Default producer identity when the host has not bound a more specific one.
CAPTURE_COMPONENT = "memory.episode_events"
CAPTURE_COMPONENT_VERSION = "wo03"

#: This process's identity. NOT a clock and not an ordering authority — it answers "which run
#: observed this", which CONTRACTS.md section 2 requires on every event. Minted once per
#: process; the host may bind its own through :func:`capture_context`.
_BOOT_ID = f"boot-{uuid.uuid4().hex}"

#: Serializes sequence allocation for a single episode. The journal's own file lock makes the
#: append atomic; this makes "read the highest sequence, then append the next" atomic too, so
#: two concurrent captures on one owner cannot choose the same number. Process-local by design:
#: an episode's SRAM bucket is process-local, so its capture is too.
_CAPTURE_LOCK = threading.RLock()

_context = threading.local()

_CONTEXT_FIELDS = (
    "thread_id",
    # HOST IDENTITY (WO-04). The sealed event has had these fields since WO-02; WO-03 left them
    # empty because "no host has supplied one". These are the names the admission seam binds from
    # the leased request, so the occurrence carries the identities the queue admitted rather than
    # duplicating them only in a payload.
    "turn_id",
    "continuation_id",
    "job_id",
    "attempt_id",
    "arrival_seq",
    "component",
    "component_version",
    "boot_id",
    "model",
    "prompt_revision",
    "tool_revision",
    "config_revision",
    "verification_revision",
)

#: The bound fields that are NOT text. ``arrival_seq`` is a positive integer in the sealed
#: schema, and stringifying it would make the event's own field disagree with the queue's.
_CONTEXT_INTS = ("arrival_seq",)

#: The immutable inputs a caller supplies. A retry presenting any of these differently is a
#: conflict, never an update — these are exactly the fields compared in :func:`record_event`.
SUPPLIED_IMMUTABLE_INPUTS = (
    "phase", "event_kind", "payload", "evidence_refs", "occurred_at", "evidence_class",
)


@contextmanager
def capture_context(**fields: str) -> Iterator[None]:
    """Bind host metadata for captures on this thread.

    The host binds this at dispatch — the same place ``episode_sram.bind_episode`` is bound —
    so every field below is the HOST's, never a label lifted from a model or external payload.
    An unbound field falls back to a truthful default rather than being inferred from content.
    """
    unknown = sorted(set(fields) - set(_CONTEXT_FIELDS))
    if unknown:
        raise ContinuityContractError(f"unknown capture context field(s): {unknown}")
    previous = getattr(_context, "fields", None)
    supplied = {
        key: (int(value) if key in _CONTEXT_INTS else str(value))
        for key, value in fields.items()
        if value is not None
    }
    _context.fields = {**(previous or {}), **supplied}
    try:
        yield
    finally:
        _context.fields = previous


def _bound() -> dict[str, str]:
    return dict(getattr(_context, "fields", None) or {})


def _event_id_for(episode_id: str, operation_id: str) -> str:
    """One deterministic identity per (episode, operation).

    Deliberately NOT content-derived: two different operations may legitimately produce
    identical payload bytes and must remain two events. Deliberately deterministic: a retry
    after a crash or a restart has to find the original row without carrying state.
    """
    return "evt_" + content_hash({"episode_id": episode_id, "operation_id": operation_id})[:32]


def _record_id_for(event_id: str) -> str:
    from memory import intake_journal

    return intake_journal._record_id(event_id)


def _journal_read_failure(exc: BaseException, *, where: str) -> EpisodeCaptureUnavailable:
    """The public capture boundary for a journal READ that could not be performed.

    WO-03A closure F4: the journal's own read errors reached callers of ``record_event`` and
    ``read_episode_events`` untranslated, so the public contract — capture either produces a
    verified occurrence or raises ``EpisodeCaptureUnavailable`` — held for appends and
    confirmations but not for the lookups in front of them.

    The cause is chained directly, so the parse or OS failure that actually happened survives.
    """
    return EpisodeCaptureUnavailable(
        f"the intake journal could not be read while {where}: {exc}"
    )


def _journal_records(*, where: str) -> tuple[Any, ...]:
    """Every journal row, or an attributable capture failure. Never a shortened list."""
    from memory import intake_journal

    try:
        return intake_journal.records()
    except (intake_journal.IntakeJournalError, OSError) as exc:
        raise _journal_read_failure(exc, where=where) from exc


def _journal_record(record_id: str, *, where: str) -> Any:
    """One journal row by locator, ``None`` if genuinely absent, or an attributable failure.

    "Absent" and "unreadable" are different answers and must not share a return value: an
    unreadable journal that answered ``None`` here would look like a brand-new operation and
    capture would allocate a second occurrence for it.
    """
    from memory import intake_journal

    try:
        return intake_journal.record_by_id(record_id)
    except (intake_journal.IntakeJournalError, OSError) as exc:
        raise _journal_read_failure(exc, where=where) from exc


def _row_event_envelope(record: Any) -> Mapping[str, Any] | None:
    """The row's versioned event envelope, or None if this row does not carry one.

    Recognition keyed on the envelope the capture actually writes — a `payload.event` body
    stamped with `schema.episode_event.SCHEMA_VERSION` alongside a `payload.sealed_sha256`
    string — NOT on the outer `kind`, and not on a loose substring or any user text.

    WO-03A closure F1: the outer `kind` and `origin_episode_id` are routing metadata, and a read
    that used them to decide not to validate a row could drop a recognizable event from its own
    episode's history. The five journal kinds in use (`memory`, `perception`, `turn`, `recall`,
    `episode_event`) put no such pair at the root of a record payload — a memory envelope's model
    or user content lives under `payload.payload`, one level down — so this recognizes capture
    rows without claiming any ordinary intake.
    """
    payload = getattr(record, "payload", None)
    if not isinstance(payload, Mapping):
        return None
    body = payload.get("event")
    if not isinstance(body, Mapping):
        return None
    if str(body.get("schema_version") or "") != EVENT_SCHEMA_VERSION:
        return None
    if not isinstance(payload.get("sealed_sha256"), str):
        return None
    return body


def _is_event_row(record: Any) -> bool:
    """Whether this row must be validated as a captured event before it can be dismissed."""
    if str(getattr(record, "kind", "") or "") == EVENT_JOURNAL_KIND:
        return True
    return _row_event_envelope(record) is not None


def _row_source_episode(record: Any) -> str:
    """The episode that CAPTURED this row, from stable row identity, not from its body.

    ``origin_episode_id`` is set once at capture and is never moved by ``reown_record``,
    ``reconcile_at_boot`` or ``claim_released_records`` — only ``owner_*`` moves. Keying reads on
    it rather than on the event body means a row whose body is missing or damaged can still be
    attributed to the episode that owes it, instead of disappearing from that episode's history
    (WO-03A R3). For an intact row it is the same answer as the sealed ``episode_id``, which
    :func:`_event_from_record` asserts.
    """
    return str(getattr(record, "origin_episode_id", "") or "")


def _event_from_record(record: Any) -> EpisodeEvent:
    """Rebuild the sealed event from its journal row, verifying its own integrity claim.

    The ONE decoder for reads, retries and boot adoption, so no caller can present a row this
    has not checked. Structural schema validity is not byte integrity (WO-03A R3): the previous
    version rebuilt the schema and never compared the stored ``sealed_sha256``, so a row whose
    payload or ``event_seq`` had changed under a retained digest decoded cleanly.

    Verified here: the record's kind, that the body is present, that it satisfies the WO-02
    contract, that its sealed digest still matches the bytes, that the row's outer event identity
    is the body's own, and that the immutable source episode/pipeline linkage is intact. The
    recovery OWNER is deliberately not checked — moving ownership is what recovery is for.

    A matching digest detects inconsistency. It does not authenticate history: a writer able to
    replace the body can replace the digest with it.
    """
    record_id = getattr(record, "record_id", "?")
    kind = str(getattr(record, "kind", "") or "")
    if kind != EVENT_JOURNAL_KIND:
        raise EpisodeCaptureUnavailable(
            f"intake journal row {record_id!r} is kind {kind!r}, not {EVENT_JOURNAL_KIND!r}; "
            "it cannot be presented as a captured event"
        )
    payload = dict(getattr(record, "payload", None) or {})
    body = payload.get("event")
    if not isinstance(body, Mapping):
        raise EpisodeCaptureUnavailable(
            f"intake journal row {record_id!r} carries no event body; "
            "it cannot be presented as a captured event"
        )
    try:
        event = EpisodeEvent.from_dict(dict(body))
    except ContinuityContractError as exc:
        raise EpisodeCaptureUnavailable(
            f"intake journal row {record_id!r} holds an event that no "
            f"longer satisfies its contract: {exc}"
        ) from exc

    sealed = payload.get("sealed_sha256")
    if not isinstance(sealed, str) or not sealed:
        raise EpisodeCaptureUnavailable(
            f"intake journal row {record_id!r} carries no sealed digest; its event body cannot "
            "be verified and must not be presented as captured evidence"
        )
    recomputed = event.sealed_sha256()
    if recomputed != sealed:
        raise EpisodeCaptureUnavailable(
            f"intake journal row {record_id!r} holds event {event.event_id!r} whose sealed "
            f"digest is {sealed!r} but whose bytes hash to {recomputed!r}; the recorded "
            "occurrence and its integrity claim disagree"
        )

    outer_event_id = str(getattr(record, "event_id", "") or "")
    if outer_event_id != event.event_id:
        raise EpisodeCaptureUnavailable(
            f"intake journal row {record_id!r} is filed under event {outer_event_id!r} but "
            f"carries event {event.event_id!r}; its identity is inconsistent"
        )
    source_episode = _row_source_episode(record)
    if source_episode != event.episode_id:
        raise EpisodeCaptureUnavailable(
            f"intake journal row {record_id!r} records source episode {source_episode!r} but "
            f"carries an event sealed to {event.episode_id!r}; its source linkage is broken"
        )
    source_pipeline = int(getattr(record, "origin_pipeline_id", -1))
    if source_pipeline != event.pipeline_id:
        raise EpisodeCaptureUnavailable(
            f"intake journal row {record_id!r} records source pipeline {source_pipeline} but "
            f"carries an event sealed to pipeline {event.pipeline_id}"
        )

    # WO-03A closure F2. The locator is how a retry FINDS this occurrence, so a row filed under
    # the wrong key is not a cosmetic inconsistency: the retry misses it, believes the operation
    # is new, and mints a second occurrence at the next sequence under the same event id. Both
    # relations capture actually establishes are checked, so neither the row's key nor the
    # event's own id can be moved independently of the other.
    expected_record_id = _record_id_for(event.event_id)
    if str(record_id) != expected_record_id:
        raise EpisodeCaptureUnavailable(
            f"intake journal row {record_id!r} is filed under a locator that disagrees with its "
            f"event identity: event {event.event_id!r} (episode {event.episode_id!r}, sequence "
            f"{event.event_seq}) belongs at {expected_record_id!r}"
        )
    expected_event_id = _event_id_for(event.episode_id, event.operation_id)
    if event.event_id != expected_event_id:
        raise EpisodeCaptureUnavailable(
            f"intake journal row {record_id!r} carries event {event.event_id!r}, but episode "
            f"{event.episode_id!r} with operation {event.operation_id!r} derives "
            f"{expected_event_id!r}; its identity was not produced by this capture"
        )
    return event


def _checked_inputs(
    *,
    phase: str,
    event_kind: str,
    payload: Any,
    evidence_refs: Any,
    occurred_at: str | None,
    evidence_class: str = "host_observation",
) -> dict[str, Any]:
    """Validate the caller's inputs BEFORE anything is journalled, compared or allocated.

    A malformed phase must not leave a row behind, so this runs ahead of the journal and ahead
    of sequence allocation — and ahead of BOTH the new-event and the replay branch, so a
    malformed retry is refused rather than acknowledging the original (WO-03A R2).

    ``freeze`` takes the immutable copy: the caller cannot mutate the payload afterwards and
    change either what a replay compares or what a new event writes, and a value the contract
    cannot represent is refused here rather than at hashing time.
    """
    require_enum(phase, allowed=EPISODE_PHASES, where="record_event.phase")
    require_text(event_kind, where="record_event.event_kind", limit=MAX_EVENT_KIND)
    if not isinstance(payload, Mapping):
        raise ContinuityContractError(
            f"record_event.payload must be a JSON object, got {type(payload).__name__}"
        )
    refs = require_texts(
        evidence_refs, where="record_event.evidence_refs", limit=512, maximum=MAX_REFS
    )
    if occurred_at is not None:
        require_text(occurred_at, where="record_event.occurred_at", limit=64)
    require_enum(evidence_class, allowed=EVIDENCE_CLASSES, where="record_event.evidence_class")
    return {
        "phase": phase,
        "event_kind": event_kind,
        "payload": freeze(payload, where="record_event.payload"),
        "evidence_refs": refs,
        "occurred_at": occurred_at,
        "evidence_class": evidence_class,
    }


def _require_same_inputs(original: EpisodeEvent, supplied: Mapping[str, Any]) -> None:
    """A retry of one operation must present the same immutable inputs, or it is a conflict.

    Compared as CANONICAL BYTES, never by Python equality (WO-03A R2). ``True == 1`` and
    ``False == 0`` in Python, so a retry that turned a recorded integer into a boolean — or the
    reverse, at any depth — replayed as if nothing had changed while the bytes the journal holds
    say otherwise. ``canonical_bytes`` is the same encoding the sealed digest is built from:
    boolean is not an integer, object key order is irrelevant, and array order is significant.

    This compares the declared immutable INPUT set only. It is not a semantic-content hash, and
    the original occurrence's own metadata — sequence, timestamps, producing boot — is untouched
    by a retry, including one from a process with a new live boot id.
    """
    actual = {
        "phase": original.phase,
        "event_kind": original.event_kind,
        "payload": original.payload,
        "evidence_refs": tuple(original.evidence_refs),
        "occurred_at": original.occurred_at.value if original.occurred_at.is_observed else None,
        "evidence_class": original.evidence_class,
    }
    moved = [
        name
        for name in SUPPLIED_IMMUTABLE_INPUTS
        if canonical_bytes(actual[name]) != canonical_bytes(supplied[name])
    ]
    if moved:
        raise EventIdentityConflict(
            f"event {original.event_id!r} (episode {original.episode_id!r}, sequence "
            f"{original.event_seq}) was already captured with different immutable content; "
            f"these supplied input(s) differ: {moved}. The original is unchanged."
        )


def _next_sequence(episode_id: str) -> int:
    """The next per-episode sequence, from DURABLE journal state. Call under ``_CAPTURE_LOCK``.

    Derived, not counted: there is no new global sequence authority, and a restart continues
    from what is on disk rather than from process memory. Keyed on the event's own SEALED
    episode id, so a reowned row still belongs to the episode that captured it.
    """
    highest = 0
    for event in read_episode_events(episode_id):
        highest = max(highest, event.event_seq)
    return highest + 1


def _require_owner_binding(episode_id: str) -> tuple[str, int]:
    """Authorize the CALLER, not the target. Naming a live episode is not owning it.

    WO-03A R1: capture used ``owner_for_intake(episode_id)`` alone. That helper's explicit-id
    route resolves whichever bucket is named and — correctly, for its own broader contract —
    never asks whether the caller belongs to it, so a worker bound to A could write into an open
    B, and an unbound worker could write into any open episode just by naming it.

    The binding is the authority. A dispatched worker acquires one through
    ``episode_sram.bind_episode``; ``open_bucket`` binds its opener. ``bound_episode_id``
    already answers None for an unbound thread AND for a thread whose bucket has since been
    drained, so a stale binding is not a binding here either.

    ``owner_for_intake`` still runs, so the bucket lifecycle stays the one authority on which
    physical pipeline owns the episode — this adds a caller check in front of it rather than
    replacing it, and no unrelated SRAM API changes.
    """
    from memory import episode_sram

    wanted = str(episode_id or "").strip()
    if not wanted:
        raise EpisodeOwnershipError("event capture requires an episode id")
    bound_episode = episode_sram.bound_episode_id()
    if bound_episode is None or bound_episode != wanted:
        raise EpisodeOwnershipError(
            "event capture requires the matching live owner binding; this thread is bound to "
            f"{bound_episode!r} and presented {wanted!r}"
        )
    return episode_sram.owner_for_intake(wanted)


def _confirmed_occurrence(record: Any, *, expected: EpisodeEvent) -> tuple[Any, EpisodeEvent]:
    """Confirm durability, then VALIDATE and BIND the row the confirmation actually read.

    WO-03A closure F3. ``confirm_durable`` performs ``_load(force=True)``, so the row it returns
    comes from disk while the row already validated may have come from the cache. The previous
    code handed the OLD validated event to the bucket alongside the NEWLY READ row: an on-disk
    body that changed under a preserved size, mtime and sealed digest was fsynced, reported
    durable, and acknowledged as the original occurrence. An fsync proves persistence; it cannot
    say anything about content.

    So the confirmed row is decoded through the one decoder — the same integrity, identity,
    locator and source checks — and then compared to *expected* by SEALED OCCURRENCE, not by
    semantic content. What goes into SRAM is the event that was read from the row whose
    durability was just proven.
    """
    confirmed = _confirmed_durable(record, event_id=expected.event_id)
    decoded = _event_from_record(confirmed)
    try:
        expected.require_same_occurrence(decoded)
    except ContinuityContractError as exc:
        raise EpisodeCaptureUnavailable(
            f"event {expected.event_id!r} was confirmed durable, but the row that confirmation "
            f"read is a different occurrence than the one validated: {exc}. Nothing is "
            "acknowledged; the durable row stays recoverable."
        ) from exc
    return confirmed, decoded


def _confirmed_durable(record: Any, *, event_id: str) -> Any:
    """Return the row only once the journal has PROVEN it durably accepted (WO-03A R4).

    ``_append_locked`` writes and flushes before it fsyncs, so a failed durability step leaves
    readable bytes. The previous code treated the existence of those bytes as proof, so a retry
    with fsync still broken read its own unsynced row and acknowledged it. Visibility is not a
    receipt.

    Every acknowledgement passes through here, including the one that directly follows a
    successful append: ``_journal_generic`` can also RETURN an existing row without appending
    (another process won the race), which fsyncs nothing. One rule — no acknowledgement without a
    confirmation that just succeeded — cannot be got wrong at one of two call sites.

    The confirmation is a filesystem operation on the journal, so it reconciles across a restart
    where a process-local success flag could not. A failure is recoverable, never lost: the row
    stays durable-or-not on disk, unacknowledged, and the next retry confirms it.
    """
    from memory import intake_journal

    record_id = getattr(record, "record_id", None) or _record_id_for(event_id)
    try:
        return intake_journal.confirm_durable(record_id)
    except (intake_journal.IntakeJournalError, OSError) as exc:
        raise EpisodeCaptureUnavailable(
            f"event {event_id!r} has a journal row that is not proven durably accepted, so it "
            f"cannot be acknowledged: {exc}"
        ) from exc


def _hold_in_owner_bucket(event: EpisodeEvent, record: Any, *, episode_id: str) -> None:
    """Place the validated event in its owner's bucket, idempotently.

    A separate function because it is the acknowledgement half of the capture: the crash
    boundary the order requires is exactly "journalled, but this had not run yet".

    Ownership is rechecked here (WO-03A R1). Capture can outlive its owner — a bucket can drain
    between authorization and acknowledgement — and a lost owner must leave recoverable,
    unacknowledged durable work rather than redirecting the event into whichever bucket is
    newest or sole. ``hold_event`` is called with the explicit episode, and its own row-owner
    check stays the last word.
    """
    from memory import episode_sram

    bound_episode = episode_sram.bound_episode_id()
    if bound_episode is None or bound_episode != episode_id:
        raise EpisodeCaptureUnavailable(
            f"event {getattr(event, 'event_id', '?')!r} is journalled and durable but its owner "
            f"binding moved from {episode_id!r} to {bound_episode!r} before acknowledgement; it "
            "stays recoverable rather than being held by another episode"
        ) from EpisodeOwnershipError(
            "acknowledgement requires the same live owner binding that authorized the capture"
        )
    try:
        episode_sram.hold_event(event, journal_record=record, episode_id=episode_id)
    except RuntimeError as exc:
        raise EpisodeCaptureUnavailable(
            f"event {getattr(event, 'event_id', '?')!r} is journalled and durable but its owner "
            f"bucket would not accept it: {exc}"
        ) from exc


def record_event(
    episode_id: str,
    *,
    operation_id: str,
    phase: str,
    event_kind: str,
    payload: dict,
    evidence_refs: tuple[str, ...] = (),
    occurred_at: str | None = None,
    evidence_class: str = "host_observation",
) -> EpisodeEvent:
    """Capture one owner-bound episode event. Idempotent per (episode, operation).

    ``evidence_class`` is the producer's statement of what KIND of evidence this is (WO-05):
    ``verification_result`` for a host check, ``external_content`` for something an external
    source said. The producer authority is always the host here; the schema refuses a host-only
    class from any other authority.

    Raises ``EpisodeOwnershipError`` when the caller does not hold the live owner binding for
    *episode_id*, ``EventIdentityConflict`` when one operation is presented again with different
    immutable content, and ``EpisodeCaptureUnavailable`` when capture could not run or could not
    be proven durable — never an empty success.
    """
    from memory import intake_journal

    # 1. AUTHORIZE THE CALLER before any mutation. A valid open target is not proof that this
    #    thread owns it: an unbound worker, and a worker bound to a different open episode, are
    #    both refused, and the sole or newest bucket is never authority.
    owner_episode, owner_pipeline = _require_owner_binding(episode_id)

    # 2. Validate the caller's inputs before a row or a sequence can exist.
    require_text(operation_id, where="record_event.operation_id", limit=256)
    supplied = _checked_inputs(
        phase=phase,
        event_kind=event_kind,
        payload=payload,
        evidence_refs=evidence_refs,
        occurred_at=occurred_at,
        evidence_class=evidence_class,
    )

    event_id = _event_id_for(owner_episode, operation_id)
    record_id = _record_id_for(event_id)

    def _replay(record: Any) -> EpisodeEvent:
        """Retrieve the ORIGINAL occurrence; allocate nothing.

        Order is the contract: verify the row's integrity, then the supplied inputs, then prove
        durability, and only then acknowledge. A conflicting or malformed retry is refused before
        anything is held, and it consumes no sequence because nothing here allocates one.
        """
        original = _event_from_record(record)
        _require_same_inputs(original, supplied)
        # A crash may have journalled without acknowledging, and a failed fsync may have left
        # visible bytes. Confirm durability, validate the row the confirmation read, then
        # rehydrate that occurrence once, idempotently.
        confirmed, acknowledged = _confirmed_occurrence(record, expected=original)
        _hold_in_owner_bucket(acknowledged, confirmed, episode_id=owner_episode)
        return acknowledged

    existing = _journal_record(record_id, where=f"looking up event {event_id!r}")
    if existing is not None:
        return _replay(existing)

    with _CAPTURE_LOCK:
        # Re-check under the lock: a concurrent caller may have won the race.
        existing = _journal_record(record_id, where=f"looking up event {event_id!r}")
        if existing is not None:
            return _replay(existing)

        # AUTO producers often hold only an episode id (for example browser tool
        # attempts and inference receipts). Their lineage is the origin this episode
        # already recorded, not a new thread rooted at the episode id. Read the durable
        # origin by owner so another physical pipeline's context cannot leak here.
        from schema.episode_event import ADMISSION_ORIGIN

        admitted = next(
            (event for event in read_episode_events(owner_episode)
             if event.event_kind == ADMISSION_ORIGIN),
            None,
        )
        bound = {
            name: getattr(admitted, name)
            for name in ("thread_id", "turn_id", "continuation_id", "job_id", "arrival_seq")
            if admitted is not None and getattr(admitted, name)
        }
        bound.update({name: value for name, value in _bound().items() if value not in (None, "", 0)})
        producer = {
            "component": bound.get("component") or CAPTURE_COMPONENT,
            "component_version": bound.get("component_version") or CAPTURE_COMPONENT_VERSION,
            "authority": "host",
        }
        for name in ("model", "prompt_revision", "tool_revision", "config_revision", "verification_revision"):
            if bound.get(name):
                producer[name] = bound[name]

        from core.symphony_time import now_iso

        event = EpisodeEvent(
            episode_id=owner_episode,
            # No bound thread means this episode roots its own — the same rule
            # EpisodeStore.open applies, rather than inventing a relationship.
            thread_id=bound.get("thread_id") or owner_episode,
            event_seq=_next_sequence(owner_episode),
            event_id=event_id,
            operation_id=operation_id,
            phase_attempt=1,
            phase=supplied["phase"],
            event_kind=supplied["event_kind"],
            observed_at=now_iso(),
            occurred_at=(
                Observation.observed(supplied["occurred_at"])
                if supplied["occurred_at"] is not None
                else Observation.missing(
                    "not_captured", reason="the producer did not record an occurrence time"
                )
            ),
            boot_id=bound.get("boot_id") or _BOOT_ID,
            elapsed_ns=Observation.missing(
                "not_captured", reason="this capture seam does not measure elapsed time"
            ),
            producer=producer,
            evidence_class=supplied["evidence_class"],
            status="succeeded",
            payload=supplied["payload"],
            evidence_refs=supplied["evidence_refs"],
            pipeline_id=owner_pipeline,
            # WO-04: the host's own identities for this occurrence, bound at dispatch. Absent
            # stays absent — an unbound field keeps the schema's own sentinel rather than being
            # guessed from the episode.
            turn_id=bound.get("turn_id") or "",
            continuation_id=bound.get("continuation_id") or "",
            job_id=bound.get("job_id") or "",
            attempt_id=bound.get("attempt_id") or "",
            arrival_seq=int(bound.get("arrival_seq") or 0),
        )

        # 3. DURABLE FIRST. The row is fsynced before the event is acknowledged, so a crash
        #    between the two leaves it recoverable rather than lost. A failure here propagates
        #    with its cause and nothing is acknowledged; it is never re-journalled through the
        #    same broken path.
        try:
            record = intake_journal._journal_generic(
                kind=EVENT_JOURNAL_KIND,
                payload={"event": event.to_dict(), "sealed_sha256": event.sealed_sha256()},
                identity={"episode": owner_episode, "operation": operation_id},
                owner_episode_id=owner_episode,
                owner_pipeline_id=owner_pipeline,
                event_id=event_id,
            )
        except intake_journal.IntakeJournalConflict as exc:
            # Another caller journalled this identity first. Its row is authoritative.
            raced = _journal_record(record_id, where=f"reading back raced event {event_id!r}")
            if raced is None:
                raise EpisodeCaptureUnavailable(
                    f"event {event_id!r} conflicted but its row cannot be read back"
                ) from exc
            return _replay(raced)
        except (intake_journal.IntakeJournalError, OSError) as exc:
            # The journal could not accept the row. Named at the capture boundary with its cause
            # chained, never re-journalled through the path that just failed. Two named classes,
            # not a blanket catch: the journal's own error, and an OS error that reached here
            # without being wrapped. `KeyboardInterrupt`, `SystemExit` and every contract error
            # still propagate untouched.
            raise EpisodeCaptureUnavailable(
                f"event {event_id!r} could not be durably journalled for episode "
                f"{owner_episode!r}: {exc}"
            ) from exc

        # 4. Prove durable acceptance, validate the confirmed row, then acknowledge it.
        confirmed, acknowledged = _confirmed_occurrence(record, expected=event)
        _hold_in_owner_bucket(acknowledged, confirmed, episode_id=owner_episode)
        return acknowledged


def read_episode_events(episode_id: str) -> tuple[EpisodeEvent, ...]:
    """The episode's captured events, in sequence order. Working state, never canonical recall.

    Keyed on each event's own SEALED ``episode_id``, not on the journal row's current owner, so
    a row moved by recovery still reads as belonging to the episode that captured it.

    A row is a candidate when :func:`_is_event_row` recognizes it, and it belongs to *episode_id*
    when EITHER its outer ``origin_episode_id`` or its own sealed ``episode_id`` says so. Both
    halves matter (WO-03A closure F1): the outer fields are routing metadata a single-field
    corruption can move, and the earlier version consulted only them, so changing
    ``origin_episode_id`` — or the outer ``kind``, while the versioned event envelope stayed put —
    silently dropped a recognizable event from its own episode's history. A disagreement between
    the two is now an attributable integrity failure, which is what
    :func:`_event_from_record` reports; it is never a legitimate empty result.

    Every candidate goes through :func:`_event_from_record`, so an unverifiable row fails this
    read instead of shrinking its result. Duplicate identity or sequence inside one episode is
    refused rather than tie-sorted into an apparently valid history.

    This is an owner/recovery read. It deliberately does not consult canonical memory and does
    not make a pending event recallable: publication is DeepSleep's, at WO-07.
    """
    wanted = str(episode_id or "")
    if not wanted:
        return ()
    found: list[EpisodeEvent] = []
    for record in _journal_records(where=f"reading episode {wanted!r}"):
        if not _is_event_row(record):
            continue
        envelope = _row_event_envelope(record) or {}
        if wanted not in (_row_source_episode(record), str(envelope.get("episode_id") or "")):
            continue
        found.append(_event_from_record(record))

    by_id: dict[str, EpisodeEvent] = {}
    by_seq: dict[int, EpisodeEvent] = {}
    for event in found:
        clash = by_id.get(event.event_id)
        if clash is not None:
            raise EpisodeCaptureUnavailable(
                f"episode {wanted!r} holds two rows for event {event.event_id!r}; its history is "
                "inconsistent and must not be presented as ordered"
            )
        seq_clash = by_seq.get(event.event_seq)
        if seq_clash is not None:
            raise EpisodeCaptureUnavailable(
                f"episode {wanted!r} holds events {seq_clash.event_id!r} and "
                f"{event.event_id!r} at the same sequence {event.event_seq}; the sequence is "
                "not a valid order and is never resolved by a tiebreak"
            )
        by_id[event.event_id] = event
        by_seq[event.event_seq] = event
    return tuple(sorted(found, key=lambda event: event.event_seq))


__all__ = [
    "CAPTURE_COMPONENT",
    "CAPTURE_COMPONENT_VERSION",
    "EVENT_JOURNAL_KIND",
    "SUPPLIED_IMMUTABLE_INPUTS",
    "capture_context",
    "read_episode_events",
    "record_event",
]
