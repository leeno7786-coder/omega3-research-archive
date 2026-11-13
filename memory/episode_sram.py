"""The SRAM episode bucket — fresh memory held IN the episode, consolidated by DeepSleep.

Perception before consolidation, done structurally (operator 2026-07-16):

    "build an sram bucket that holds fresh memory in the episode until close and then
     add them to the system memory all at once during deepsleep, then we will never
     have that problem."

The problem: the operator states a fact ("I have a sister named Miranda"), it is written
to system memory on ingress, and then the SAME turn's recall surfaces it as PRIOR
knowledge — Omega "remembers something before it's told." That is ingest racing ahead of
recall inside one turn.

The cure is two separate pipelines made structural, not a post-hoc filter:

  * RECALL pipeline (AUTO) reads SYSTEM memory only — the durable, prior-turn store.
  * INGEST pipeline holds this turn's fresh memory in THIS bucket (volatile, in-episode)
    and flushes it into system memory ALL AT ONCE at DeepSleep/close.

Because a turn's own intake is not in system memory until that turn's DeepSleep, recall
for turn N can never see turn N's intake. Next turn recalls it normally. This is the same
biology the hot buffer already follows ("hot is where memories live BEFORE they're
memories"), extended from conversation turns to every fresh intake and moved to the one
consolidation seam designed for it — DeepSleep.

Process-global bucket map, one bucket per episode and physical pipeline.
Opening binds the episode to the calling thread. Mutating operations require
that live binding or an explicit existing episode; unbound diagnostic lookups
never authorize intake or cleanup. Spawned workers use ``bind_episode`` with
identities captured by their dispatcher.

Only one drain executes a bucket at a time. Other physical pipelines remain
independent. The drain binds its own episode for nested work, retains the
captured batch on failure, and removes only the successfully processed prefix.
Closing and cleanup refuse to discard remaining intake.
"""

from __future__ import annotations

import logging
import re
import threading
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional

log = logging.getLogger("omega.memory.episode_sram")


@dataclass(frozen=True)
class FreshItem:
    """One piece of fresh memory held for an episode.

    kind == "perception": operator-stated text run through persona ingest at flush.
    kind == "perception_processed": AUTO already admitted its extracted records into SRAM.
    kind == "turn":       a conversation turn (role, text) graduated to the hot buffer.
    kind == "recall":     a verified memory id whose lifecycle maintenance waits for DeepSleep.
    kind == "memory":     a canonical envelope accepted for DeepSleep-only persistence.
    kind == "event":      a captured EpisodeEvent (WO-03). Already durable in the intake
                          journal at the moment it is held; its row stays PENDING through the
                          drain, because canonical publication belongs to WO-07.
    """

    kind: str
    text: str
    speaker: str = "operator"  # perception: who stated it
    graduated: bool = False  # turn: already in the hot buffer (graduated at delivery)
    role: str = "user"  # turn: user | assistant
    ingress_class: str = ""
    chat_intent: str = ""
    source_citation: Dict[str, Any] = field(default_factory=dict)
    envelope: Any = None
    write_options: Dict[str, Any] = field(default_factory=dict)
    journal_record_id: str = ""
    event_id: str = ""
    event: Any = None  # kind == "event": the validated EpisodeEvent this item carries


class _Bucket:
    def __init__(self, episode_id: str, pipeline_id: int) -> None:
        self.episode_id = str(episode_id or "")
        self.pipeline_id = pipeline_id
        self.items: List[FreshItem] = []
        self.draining = False
        self._ordinal = 0

    def next_ordinal(self) -> int:
        ordinal = self._ordinal
        self._ordinal += 1
        return ordinal


_lock = threading.RLock()
# episode_id -> bucket; insertion-ordered so the newest open bucket is the
# deterministic fallback for unbound SRAM readers (reported by epoch receipts).
_buckets: Dict[str, _Bucket] = {}
_binding = threading.local()
_drain_state = threading.local()


# ── lifecycle ─────────────────────────────────────────────────────────────────


def open_bucket(
    episode_id: str,
    *,
    pipeline_id: int,
) -> None:
    """Open a fresh bucket for a physical owner and bind it to this thread.

    A pending prior bucket in the SAME pipeline is an unfinished episode, not
    permission to consolidate outside DeepSleep. The new episode is refused and
    the prior intake stays intact. Buckets in other pipelines are live concurrent
    episodes and are untouched."""
    ep_id = str(episode_id or "")
    if not ep_id.strip():
        raise ValueError("episode SRAM requires a nonempty episode id")
    from core.pipelines import validate_pipeline_id

    pid = validate_pipeline_id(pipeline_id)
    with _lock:
        for bucket in _buckets.values():
            if bucket.pipeline_id == pid and (bucket.items or bucket.draining) and bucket.episode_id != ep_id:
                raise RuntimeError(
                    f"episode SRAM bucket {bucket.episode_id} still has pending intake; "
                    f"cannot open {episode_id}"
                )
        existing = _buckets.get(ep_id)
        if existing is not None and existing.pipeline_id != pid:
            raise RuntimeError(
                f"episode SRAM bucket {ep_id} belongs to pipeline {existing.pipeline_id}, not {pid}"
            )
        if existing is not None and (existing.items or existing.draining):
            raise RuntimeError(
                f"episode SRAM bucket {ep_id} still has pending intake; "
                f"cannot open {episode_id}"
            )
        # Same-lane empty leftovers are drained episodes that skipped cleanup;
        # replace them so the map cannot accrete dead slots.
        for stale_id in [
            b.episode_id for b in _buckets.values() if b.pipeline_id == pid and not b.items and not b.draining
        ]:
            _buckets.pop(stale_id, None)
        adopted, uncertain = _adopt_released_intake(ep_id, pid)
        bucket = _Bucket(ep_id, pid)
        bucket.items.extend(adopted)
        _buckets[ep_id] = bucket
        _binding.episode_id = ep_id
        try:
            for record in uncertain:
                _hold_recovery_failure(record)
        except Exception:
            from memory import intake_journal

            intake_journal.release_unreported_uncertainty(episode_id=ep_id, pipeline_id=pid)
            raise


def _adopt_released_intake(episode_id: str, pipeline_id: int) -> tuple[List[FreshItem], list[Any]]:
    from memory import intake_journal

    records = intake_journal.claim_released_records(
        episode_id=episode_id,
        pipeline_id=pipeline_id,
    )
    return (
        [_item_from_journal_record(record) for record in records if record.status != intake_journal.UNCERTAIN],
        [record for record in records if record.status == intake_journal.UNCERTAIN],
    )


def _hold_recovery_failure(record: Any) -> None:
    """An interrupted effect is learned through ordinary SRAM, never re-executed."""
    from memory import intake_journal
    from memory.api import CanonicalEnvelope, intake_memory

    memory_id = f"intake.failure.{record.record_id}"
    reason = str(record.note or "effect completion could not be established")
    envelope = CanonicalEnvelope(
        cycle_id=record.origin_episode_id or record.owner_episode_id,
        id=memory_id, coord=[9, 0, 0], type="episode",
        payload={
            "id": memory_id,
            "title": "Interrupted memory intake effect",
            "text": f"Memory intake recovery could not establish completion of {record.kind}: {reason}",
            "enrichment": {"intake_recovery": {
                "record_id": record.record_id, "event_id": record.event_id,
                "origin_episode_id": record.origin_episode_id,
                "origin_pipeline_id": record.origin_pipeline_id,
                "kind": record.kind, "disposition": "uncertain", "cause": reason,
            }},
        },
        source="memory.intake_recovery",
    )
    receipt = intake_memory(envelope, event_id=f"intake-recovery:{record.record_id}")
    if receipt.validation_status not in {"held", "committed"} or not receipt.journal_record_id:
        raise intake_journal.IntakeJournalError("uncertain effect evidence was not durably admitted")
    intake_journal.mark_recovery_record(record.record_id, recovery_record_id=receipt.journal_record_id)


def _item_from_journal_record(record: Any) -> FreshItem:
    from memory import intake_journal

    payload = dict(record.payload or {})
    ids = {"journal_record_id": record.record_id, "event_id": record.event_id}
    if record.kind == "memory":
        return FreshItem(
            kind="memory",
            text=str(payload.get("type") or "memory"),
            envelope=intake_journal.envelope_from_record(record),
            write_options=dict(record.write_options or {}),
            **ids,
        )
    if record.kind == "perception":
        admitted = [str(item) for item in (payload.get("records") or ())]
        return FreshItem(
            kind="perception_processed" if payload.get("processed") else "perception",
            text=str(payload.get("text") or ""),
            speaker=str(payload.get("speaker") or "operator"),
            ingress_class=str(payload.get("ingress_class") or ""),
            chat_intent=str(payload.get("chat_intent") or ""),
            source_citation=deepcopy(payload.get("source_citation") or {}),
            write_options=(
                {"record_count": len(admitted), "records": admitted}
                if payload.get("processed")
                else {}
            ),
            **ids,
        )
    if record.kind == "turn":
        return FreshItem(
            kind="turn",
            text=str(payload.get("text") or ""),
            role=str(payload.get("role") or "user"),
            graduated=bool(payload.get("graduated")),
            **ids,
        )
    if record.kind == "recall":
        return FreshItem(kind="recall", text=str(payload.get("memory_id") or ""), **ids)
    if record.kind == "episode_event":
        # WO-03: an adopted event row rebuilds its SEALED event. Adoption changes who owes the
        # row, never what the event says — the recovery owner is separate metadata.
        from memory.episode_events import _event_from_record

        event = _event_from_record(record)
        return FreshItem(kind="event", text=event.event_id, event=event, **ids)
    raise RuntimeError(f"unsupported released intake kind: {record.kind}")



def _resolve_bucket() -> Optional[_Bucket]:
    """The calling thread's bucket. Call holding ``_lock``.

    Cascade-bound thread → its bucket (while still open). Unbound thread →
    the sole open bucket, else the most recently opened."""
    bound_id = getattr(_binding, "episode_id", None)
    if bound_id is not None:
        bucket = _buckets.get(bound_id)
        if bucket is not None:
            return bucket
    if not _buckets:
        return None
    return next(reversed(_buckets.values()))


def is_open(episode_id: Optional[str] = None) -> bool:
    with _lock:
        if episode_id is None:
            return bool(_buckets)
        return str(episode_id or "") in _buckets


def current_episode_id() -> Optional[str]:
    with _lock:
        bucket = _resolve_bucket()
        return bucket.episode_id if bucket is not None else None


def bound_episode_id() -> Optional[str]:
    """The episode THIS THREAD is bound to — no most-recently-opened fallback.

    ``current_episode_id`` stays the one resolver: this does not answer "which
    episode is this read for", it answers "was that answer reached by the
    thread's own binding, or by the fallback". Phase 4 risk 11 needs the
    difference because the fallback is only exact while one bucket is open, and
    four lanes now run at once — a worker thread a cascade spawned inherits no
    binding and lands on whichever episode opened LAST (see
    ``memory.epoch.resolve_active_pin``).

    Returns None for an unbound thread, and also for a thread whose bucket has
    since been drained — a stale binding is not a binding.
    """
    with _lock:
        return _bound_episode_id_locked()


def open_bucket_count() -> int:
    """How many episodes hold an open bucket right now.

    One means the fallback above cannot be wrong, however the thread got there.
    """
    with _lock:
        return len(_buckets)


def resolve_binding() -> tuple[Optional[str], Optional[str], int]:
    """``(resolved, bound, open_buckets)`` — all three under ONE acquisition.

    The three questions have to be asked at one instant or the answer can
    over-claim: read separately, a bucket closing between the resolve and the
    count turns "the fallback chose among two" into "there was only one, so it
    cannot be wrong". That is exactly the silent rounding risk 11 exists to
    stop, reintroduced by the code that reports it.
    """
    with _lock:
        bucket = _resolve_bucket()
        return (
            bucket.episode_id if bucket is not None else None,
            _bound_episode_id_locked(),
            len(_buckets),
        )


def _bound_episode_id_locked() -> Optional[str]:
    """This thread's own binding, if its bucket is still open. Call under ``_lock``."""
    bound = getattr(_binding, "episode_id", None)
    if bound is None:
        return None
    return bound if str(bound) in _buckets else None


# ── holding ─────────────────────────────────────────────────────────────────────


def _ensure_bucket(episode_id: Optional[str] = None) -> _Bucket:
    """Resolve mutation ownership; diagnostic fallback is never write authority.

    Call holding ``_lock``. An unbound worker must supply an explicit episode.
    A sole open bucket is not proof that the worker belongs to that episode.
    """
    if episode_id is not None:
        bucket = _buckets.get(str(episode_id or ""))
        if bucket is None:
            raise RuntimeError(
                f"no episode SRAM bucket is open for episode {episode_id}; "
                "intake must enter through pressure"
            )
        return bucket
    bound = _bound_episode_id_locked()
    if bound is not None:
        return _buckets[bound]
    if not _buckets:
        raise RuntimeError("no episode SRAM bucket is open; intake must enter through pressure")
    raise RuntimeError(
        "no live episode SRAM binding for this thread; "
        "intake requires a bound owner or an explicit episode_id"
    )


@contextmanager
def bind_episode(episode_id: str, *, pipeline_id: int) -> Iterator[None]:
    """Temporarily bind a worker to an existing physical episode owner.

    This never opens an episode or drains memory. Capture both identities at
    dispatch, rather than deriving them from ``current_episode_id`` in a worker.
    """
    from core.pipelines import validate_pipeline_id

    pid = validate_pipeline_id(pipeline_id)
    with _lock:
        bucket = _ensure_bucket(episode_id)
        if bucket.pipeline_id != pid:
            raise RuntimeError(
                f"episode SRAM bucket {episode_id} belongs to pipeline {bucket.pipeline_id}, not {pid}"
            )
        if is_draining() and _bound_episode_id_locked() != bucket.episode_id:
            raise RuntimeError("cannot change episode ownership during an SRAM drain")
        previous = getattr(_binding, "episode_id", None)
        _binding.episode_id = bucket.episode_id
    try:
        yield
    finally:
        _binding.episode_id = previous


def owner_for_intake(episode_id: Optional[str] = None) -> tuple[str, int]:
    """Return the physical owner an intake would use under current binding rules."""
    with _lock:
        bucket = _ensure_bucket(episode_id)
        return bucket.episode_id, bucket.pipeline_id


def remove_journal_record(record_id: str) -> int:
    """Remove one terminal journalled item without touching other intake."""
    wanted = str(record_id or "").strip()
    if not wanted:
        return 0
    with _lock:
        removed = 0
        for bucket in _buckets.values():
            if bucket.draining:
                continue
            before = len(bucket.items)
            bucket.items = [item for item in bucket.items if item.journal_record_id != wanted]
            removed += before - len(bucket.items)
        return removed


def hold_perception(
    text: str,
    *,
    speaker: str = "operator",
    ingress_class: str = "",
    chat_intent: str = "",
    source_citation: Optional[Dict[str, Any]] = None,
) -> bool:
    """Hold a structured operator perception for close-time consolidation."""
    clean = str(text or "").strip()
    if not clean:
        return False
    from memory import intake_journal

    with _lock:
        bucket = _ensure_bucket()
        citation = deepcopy(dict(source_citation or {}))
        record = intake_journal.journal_perception(
            owner_episode_id=bucket.episode_id,
            owner_pipeline_id=bucket.pipeline_id,
            ordinal=bucket.next_ordinal(),
            text=clean,
            speaker=str(speaker or "operator"),
            ingress_class=str(ingress_class or ""),
            chat_intent=str(chat_intent or ""),
            source_citation=citation,
        )
        bucket.items.append(
            FreshItem(
                kind="perception",
                text=clean,
                speaker=str(speaker or "operator"),
                ingress_class=str(ingress_class or ""),
                chat_intent=str(chat_intent or ""),
                source_citation=citation,
                journal_record_id=record.record_id,
                event_id=record.event_id,
            )
        )
    return True


def hold_turn(role: str, text: str) -> bool:
    """Hold a conversation turn to be graduated into the hot buffer at flush."""
    clean = str(text or "").strip()
    if not clean:
        return False
    from memory import intake_journal

    with _lock:
        bucket = _ensure_bucket()
        record = intake_journal.journal_turn(
            owner_episode_id=bucket.episode_id,
            owner_pipeline_id=bucket.pipeline_id,
            ordinal=bucket.next_ordinal(),
            role=str(role or "user"),
            text=clean,
        )
        bucket.items.append(
            FreshItem(
                kind="turn",
                text=clean,
                role=str(role or "user"),
                journal_record_id=record.record_id,
                event_id=record.event_id,
            )
        )
    return True


def hold_turn_for_episode(episode_id: str, role: str, text: str) -> bool:
    """Hold a turn only when the named episode has an open bucket."""
    clean = str(text or "").strip()
    if not clean:
        return False
    from memory import intake_journal

    with _lock:
        bucket = _buckets.get(str(episode_id or ""))
        if bucket is None:
            return False
        record = intake_journal.journal_turn(
            owner_episode_id=bucket.episode_id,
            owner_pipeline_id=bucket.pipeline_id,
            ordinal=bucket.next_ordinal(),
            role=str(role or "user"),
            text=clean,
        )
        bucket.items.append(
            FreshItem(
                kind="turn",
                text=clean,
                role=str(role or "user"),
                journal_record_id=record.record_id,
                event_id=record.event_id,
            )
        )
    return True


def graduate_turns_now(episode_id: str) -> int:
    """Move the named episode's held conversation turns into the hot buffer NOW,
    in order, so the next turn's prompt already carries this exchange.

    Called at delivery: this turn's recall is done, so the "ingress echo" the
    hold guards against cannot happen any more. Waiting for DeepSleep left a
    fast follow-up without the turn just answered — live 2026-09-06, "what did
    I ask you about earlier?" sent seconds after the reply saw a conversation
    block that ended one exchange too early. Returns the number graduated."""
    from memory import intake_journal

    with _lock:
        bucket = _buckets.get(str(episode_id or ""))
        if bucket is None:
            return 0
        turns = [item for item in bucket.items if item.kind == "turn" and not item.graduated]
        if not turns:
            return 0
        from dataclasses import replace as _replace

        for item in turns:
            intake_journal.mark_applying(
                item.journal_record_id,
                note="graduating conversation turn into hot buffer",
            )

        # frozen items: rebuild the list; the turns stay as the episode transcript
        bucket.items = [
            _replace(item, graduated=True) if (item.kind == "turn" and not item.graduated) else item
            for item in bucket.items
        ]
    from memory.hot_buffer import append_turn

    try:
        for item in turns:
            append_turn(item.role, item.text)
    except Exception as exc:
        for item in turns:
            intake_journal.mark_uncertain(item.journal_record_id, reason=str(exc))
        raise
    for item in turns:
        intake_journal.mark_consumed(item.journal_record_id, reason="graduated to hot buffer")
    return len(turns)


def hold_verified_recall_for_episode(episode_id: str, memory_ids: List[str]) -> int:
    """Defer verified-recall maintenance into the owning episode's DeepSleep.

    Recall itself stays read-only.  Repeated tools may surface the same record,
    so one episode schedules each memory id at most once.

    A miss names the OPEN buckets as well as the requested episode. Before the
    per-lane map (Phase 3 cut 2, 86f385af) there was a single ``_current``
    bucket and two warnings — "no bucket open" and "mismatch: requested=%s
    open=%s". Collapsing them onto ``_buckets.get`` dropped the second half,
    and with it the only way to tell a dropped maintenance apart from a lane
    carrier pointing at the wrong episode: both then logged the same line. The
    open set is the multi-bucket form of that ``open=`` field.
    """
    clean_ids = tuple(dict.fromkeys(str(mid or "").strip() for mid in memory_ids if str(mid or "").strip()))
    if not clean_ids:
        return 0
    from memory import intake_journal

    with _lock:
        requested_episode_id = str(episode_id or "")
        bucket = _buckets.get(requested_episode_id)
        if bucket is None:
            log.warning(
                "verified recall maintenance could not be scheduled: no SRAM bucket open "
                "for episode=%s (open=%s)",
                requested_episode_id,
                ",".join(b.episode_id for b in _buckets.values()) or "none",
            )
            return 0
        existing = {item.text for item in bucket.items if item.kind == "recall"}
        scheduled = 0
        for memory_id in clean_ids:
            if memory_id in existing:
                continue
            record = intake_journal.journal_recall_maintenance(
                owner_episode_id=bucket.episode_id,
                owner_pipeline_id=bucket.pipeline_id,
                memory_id=memory_id,
            )
            bucket.items.append(
                FreshItem(
                    kind="recall",
                    text=memory_id,
                    journal_record_id=record.record_id,
                    event_id=record.event_id,
                )
            )
            existing.add(memory_id)
            scheduled += 1
        return scheduled


def hold_memory(
    envelope: Any,
    *,
    propose: bool,
    bypass_guardian: bool,
    skip_embed: bool,
    episode_id: Optional[str] = None,
    event_id: Optional[str] = None,
    journal_record: Any = None,
) -> str:
    """Accept one canonical envelope into SRAM without touching the filesystem.

    ``episode_id`` targets a specific open bucket (callers holding intake for a
    named episode from outside its cascade thread, e.g. builder-ledger artifact
    snapshots); when omitted, the calling thread's bucket resolves."""
    if envelope is None:
        raise ValueError("memory envelope required")
    from memory import intake_journal

    write_options = {
        "propose": bool(propose),
        "bypass_guardian": bool(bypass_guardian),
        "skip_embed": bool(skip_embed),
    }
    with _lock:
        bucket = _ensure_bucket(episode_id)
        record = journal_record
        if record is None:
            record, _replayed = intake_journal.journal_memory_intake(
                envelope,
                owner_episode_id=bucket.episode_id,
                owner_pipeline_id=bucket.pipeline_id,
                write_options=write_options,
                event_id=event_id,
            )
        if record.owner_episode_id != bucket.episode_id or record.owner_pipeline_id != bucket.pipeline_id:
            raise RuntimeError(
                f"intake event {record.event_id} belongs to "
                f"{record.owner_episode_id}/{record.owner_pipeline_id}, not "
                f"{bucket.episode_id}/{bucket.pipeline_id}"
            )
        for index, item in enumerate(bucket.items):
            if item.kind == "memory" and item.journal_record_id == record.record_id:
                return f"sram:{bucket.episode_id}:{index}"
        intake_index = len(bucket.items)
        bucket.items.append(
            FreshItem(
                kind="memory",
                text=str(getattr(envelope, "type", "") or "memory"),
                envelope=deepcopy(envelope),
                write_options=write_options,
                journal_record_id=record.record_id,
                event_id=record.event_id,
            )
        )
        return f"sram:{bucket.episode_id}:{intake_index}"


def hold_event(event: Any, *, journal_record: Any, episode_id: Optional[str] = None) -> str:
    """Hold one already-journalled EpisodeEvent for its owner. Idempotent by journal row.

    WO-03. The event reaches here only AFTER its intake-journal row is durable, so this call is
    the acknowledgement half of a capture: a crash between the two leaves the event recoverable
    rather than acknowledged-and-lost. Idempotent because a replay after that crash must
    rehydrate the event exactly once.

    Ownership is verified, never inferred: the row's owner must be the bucket being written.
    """
    if event is None or journal_record is None:
        raise ValueError("holding an event requires the event and its journal record")
    with _lock:
        bucket = _ensure_bucket(episode_id)
        if (
            journal_record.owner_episode_id != bucket.episode_id
            or journal_record.owner_pipeline_id != bucket.pipeline_id
        ):
            raise RuntimeError(
                f"event row {journal_record.event_id} belongs to "
                f"{journal_record.owner_episode_id}/{journal_record.owner_pipeline_id}, not "
                f"{bucket.episode_id}/{bucket.pipeline_id}"
            )
        for index, item in enumerate(bucket.items):
            if item.kind == "event" and item.journal_record_id == journal_record.record_id:
                return f"sram:{bucket.episode_id}:{index}"
        index = len(bucket.items)
        bucket.items.append(
            FreshItem(
                kind="event",
                text=str(getattr(event, "event_id", "") or ""),
                event=event,
                journal_record_id=journal_record.record_id,
                event_id=journal_record.event_id,
            )
        )
        return f"sram:{bucket.episode_id}:{index}"


def is_draining() -> bool:
    """Whether the current thread is inside the DeepSleep SRAM drain."""
    return bool(getattr(_drain_state, "active", False))


def pending(episode_id: Optional[str] = None) -> List[FreshItem]:
    """Held items — for the named episode, or the calling thread's bucket."""
    with _lock:
        if episode_id is not None:
            bucket = _buckets.get(str(episode_id or ""))
        else:
            bucket = _resolve_bucket()
        return list(bucket.items) if bucket is not None else []


def prepare_persona_intake_for_action(text: str) -> List[str]:
    """Run a remember action against its held perception and return admitted facts.

    AUTO calls this before emitting the acknowledgement. Persona records enter the
    same episode bucket as canonical envelopes; the raw perception is then marked as
    processed so DeepSleep reports the result without extracting it a second time.
    Any extraction or intake failure propagates and no acknowledgement is emitted.
    """
    clean = str(text or "").strip()
    if not clean:
        raise ValueError("remember action requires text")
    with _lock:
        bucket = _ensure_bucket()
        matches = [item for item in bucket.items if item.kind == "perception" and item.text == clean]
        if len(matches) != 1:
            raise RuntimeError(
                f"remember action requires exactly one held perception; found {len(matches)}"
            )
        perception = matches[0]

    from memory.persona_ingest import ingest_persona_statement

    result = ingest_persona_statement(
        perception.text,
        speaker=perception.speaker,
        source_citation=perception.source_citation,
    )
    records = [
        f"{str(record.facet or record.kind)}={str(record.value)}"
        for record in result.records
        if str(record.value or "").strip()
    ]
    from memory import intake_journal

    with _lock:
        bucket = _ensure_bucket()
        try:
            index = next(i for i, item in enumerate(bucket.items) if item is perception)
        except StopIteration as exc:
            raise RuntimeError("held remember perception changed during AUTO intake") from exc
        if perception.journal_record_id:
            intake_journal.mark_perception_processed(
                perception.journal_record_id,
                records_admitted=records,
            )
        bucket.items[index] = FreshItem(
            kind="perception_processed",
            text=perception.text,
            speaker=perception.speaker,
            ingress_class=perception.ingress_class,
            chat_intent=perception.chat_intent,
            source_citation=perception.source_citation,
            write_options={"record_count": len(records), "records": list(records)},
            journal_record_id=perception.journal_record_id,
            event_id=perception.event_id,
        )
    return records


# ── consolidation ───────────────────────────────────────────────────────────────


_SELF_ASSERTION_RE = re.compile(
    r"^(?:i\b|i['’](?:m|ve|d|ll)\b|my\b|we\b|we['’](?:re|ve|d|ll)\b|our\b)",
    re.IGNORECASE,
)
_EXPLICIT_MEMORY_COMMAND_RE = re.compile(
    r"^(?:please\s+)?(?:remember|note)(?:\s+that)?\s+",
    re.IGNORECASE,
)
_NON_FACTUAL_RE = re.compile(
    r"^(?:if\b|hypothetically\b|imagine\b)|"
    r"\b(?:i|we)\s+(?:do\s+not|don't|did\s+not|didn't|never|might|may|could|wonder|wish|imagine)\b",
    re.IGNORECASE,
)


def _is_persona_assertion(item: FreshItem) -> bool:
    """Whether a perception can safely yield durable persona facts.

    Questions, acknowledgements, reasoning, temporal recall, and ordinary
    commands remain conversational evidence only. Explicit remember/note
    commands are admitted as factual intake whether they describe the operator
    or another named person; persona ingest decides whether the payload yields
    any committable records.
    """
    text = item.text.strip()
    if not text or text.endswith("?"):
        return False
    explicit_memory = _EXPLICIT_MEMORY_COMMAND_RE.match(text)
    assertion_text = text[explicit_memory.end() :].strip() if explicit_memory else text
    if _NON_FACTUAL_RE.search(assertion_text):
        return False
    return bool(_SELF_ASSERTION_RE.search(assertion_text) or explicit_memory)


def complete_required_memory_commit(envelope: Any, canonical_ref: str) -> None:
    """Finish required canonical links and derived face state before acknowledgement."""
    if str(getattr(envelope, "type", "") or "") == "emergent_trait":
        from memory.post_commit import ensure_trait_revision

        ensure_trait_revision(canonical_ref)
    _refresh_committed_projection(envelope)


def _refresh_committed_projection(envelope: Any) -> None:
    """Refresh the face cache from canonical truth before acknowledging intake."""
    if getattr(envelope, "type", None) == "face_memory":
        from core.face_recall_warmup import warm_anchor_store

        warm_anchor_store()


def _default_consolidate(items: List[FreshItem]) -> Dict[str, Any]:
    """Production flush: perceptions → persona ingest (system memory); turns → hot buffer.

    Any failed item aborts the drain. ``_flush_bucket`` retains the complete batch so the
    caller can retry with the original cause instead of reporting partial success."""
    from memory import intake_journal

    ingested: List[str] = []
    turns: List[str] = []
    recalls: List[str] = []
    memories: List[str] = []
    events: List[Dict[str, str]] = []
    errors: List[str] = []
    skipped: List[Dict[str, str]] = []
    for item in items:
        journal_record = (
            intake_journal.record_by_id(item.journal_record_id)
            if item.journal_record_id
            else None
        )
        done = journal_record.status if journal_record is not None else ""
        if item.kind == "perception":
            if done in {intake_journal.COMMITTED, intake_journal.CONSUMED}:
                ingested.append(item.text)
                continue
            if done == intake_journal.REJECTED:
                skipped.append({"text": item.text, "reason": journal_record.note or "rejected"})
                continue
            if done in {intake_journal.APPLYING, intake_journal.UNCERTAIN}:
                skipped.append({"text": item.text, "reason": "perception_uncertain"})
                continue
            if not _is_persona_assertion(item):
                if journal_record is not None:
                    intake_journal.mark_rejected(
                        journal_record.record_id,
                        reason="non_assertive_speech_act",
                    )
                skipped.append({"text": item.text, "reason": "non_assertive_speech_act"})
                continue
            from memory.persona_ingest import ingest_persona_statement

            result = ingest_persona_statement(
                item.text,
                speaker=item.speaker,
                source_citation=item.source_citation,
            )
            if any(getattr(record, "id", None) for record in result.records):
                if journal_record is not None:
                    intake_journal.mark_consumed(
                        journal_record.record_id,
                        reason="persona records admitted",
                    )
                ingested.append(item.text)
            else:
                if journal_record is not None:
                    intake_journal.mark_rejected(
                        journal_record.record_id,
                        reason="no_committed_persona_records",
                    )
                skipped.append({"text": item.text, "reason": "no_committed_persona_records"})
        elif item.kind == "perception_processed":
            if done in {intake_journal.COMMITTED, intake_journal.CONSUMED}:
                ingested.append(item.text)
                continue
            if int(item.write_options.get("record_count") or 0) > 0:
                if journal_record is not None:
                    intake_journal.mark_consumed(
                        journal_record.record_id,
                        reason="processed persona records admitted",
                    )
                ingested.append(item.text)
            else:
                if journal_record is not None:
                    intake_journal.mark_rejected(
                        journal_record.record_id,
                        reason="no_persona_records_admitted",
                    )
                skipped.append({"text": item.text, "reason": "no_persona_records_admitted"})
        elif item.kind == "turn":
            if done in {intake_journal.COMMITTED, intake_journal.CONSUMED}:
                turns.append(item.text)
                continue
            if done in {intake_journal.APPLYING, intake_journal.UNCERTAIN}:
                skipped.append({"text": item.text, "reason": "turn_uncertain"})
                continue
            if not item.graduated:
                from memory.hot_buffer import append_turn

                if journal_record is not None:
                    intake_journal.mark_applying(
                        journal_record.record_id,
                        note="graduating conversation turn during DeepSleep",
                    )
                try:
                    append_turn(item.role, item.text)
                except Exception as exc:
                    if journal_record is not None:
                        intake_journal.mark_uncertain(journal_record.record_id, reason=str(exc))
                    raise
                if journal_record is not None:
                    intake_journal.mark_consumed(
                        journal_record.record_id,
                        reason="graduated to hot buffer",
                    )
            turns.append(item.text)
        elif item.kind == "recall":
            if done in {intake_journal.COMMITTED, intake_journal.CONSUMED}:
                recalls.append(item.text)
                continue
            if done in {intake_journal.REJECTED, intake_journal.UNCERTAIN}:
                skipped.append({"text": item.text, "reason": f"recall_maintenance_{done}"})
                continue
            from memory.confidence_decay import apply_recall_confidence_decay
            from memory.demotion import apply_recall_sustained_floor_demotion
            from memory.strength_index import ensure_strength_index

            if journal_record is not None:
                intake_journal.mark_applying(
                    journal_record.record_id,
                    note="recall lifecycle maintenance",
                )
            try:
                apply_recall_confidence_decay(item.text)
                apply_recall_sustained_floor_demotion(item.text)
                ensure_strength_index().touch(item.text)
            except Exception as exc:
                if journal_record is not None:
                    intake_journal.mark_uncertain(journal_record.record_id, reason=str(exc))
                raise
            if journal_record is not None:
                intake_journal.mark_committed(
                    journal_record.record_id,
                    memory_id=item.text,
                    canonical_path=item.text,
                )
            recalls.append(item.text)
        elif item.kind == "memory":
            if journal_record is not None and journal_record.status == intake_journal.COMMITTED:
                verdict, memory_id, detail = intake_journal.verify_canonical_binding(journal_record)
                if verdict != "ok":
                    raise intake_journal.IntakeJournalError(
                        f"committed intake {journal_record.record_id} canonical binding is {verdict}: {detail}"
                    )
                complete_required_memory_commit(item.envelope, memory_id)
                memories.append(journal_record.canonical_path or journal_record.memory_id)
                continue
            if journal_record is not None and journal_record.status == intake_journal.REJECTED:
                skipped.append(
                    {
                        "text": item.text,
                        "reason": journal_record.note or "memory_rejected",
                    }
                )
                continue
            if journal_record is not None:
                verdict, memory_id, canonical_path = intake_journal.verify_canonical_binding(
                    journal_record
                )
                if verdict == "ok":
                    complete_required_memory_commit(item.envelope, memory_id)
                    intake_journal.mark_committed(
                        journal_record.record_id,
                        memory_id=memory_id,
                        canonical_path=memory_id,
                        note="canonical write was durable before journal acknowledgement",
                    )
                    memories.append(memory_id)
                    continue
                if verdict in {"unavailable", "mismatch"}:
                    raise intake_journal.IntakeJournalError(
                        f"cannot safely retry intake {journal_record.record_id}: "
                        f"canonical binding is {verdict} ({canonical_path})"
                    )
            from memory.api import write_memory

            envelope = (
                intake_journal.envelope_for_commit(item.envelope, journal_record)
                if journal_record is not None else item.envelope
            )
            receipt = write_memory(envelope, **item.write_options)
            status = str(getattr(receipt, "validation_status", "") or "")
            if status == "committed" and getattr(receipt, "path", None):
                complete_required_memory_commit(envelope, str(receipt.path))
                if journal_record is not None:
                    intake_journal.mark_committed(
                        journal_record.record_id,
                        memory_id=str(receipt.path),
                        canonical_path=str(receipt.path),
                    )
                memories.append(str(receipt.path))
            elif status == "rejected":
                if journal_record is not None:
                    intake_journal.mark_rejected(
                        journal_record.record_id,
                        reason=str(getattr(receipt, "message", "") or "memory_rejected"),
                    )
                skipped.append(
                    {
                        "text": item.text,
                        "reason": str(getattr(receipt, "message", "") or "memory_rejected"),
                    }
                )
            else:
                raise RuntimeError(
                    f"memory commit returned non-terminal status {status or '<empty>'}: "
                    f"{getattr(receipt, 'message', '')}"
                )
        elif item.kind == "event":
            # WO-03 captures; WO-07 publishes. The row is already durable, so the drain neither
            # writes canonical memory nor marks it terminal: it leaves the row PENDING and
            # reports it, so a deferred event is never mistaken for a published one and never
            # silently dropped either. `read_episode_events` still finds it after the drain.
            events.append(
                {
                    "event_id": item.event_id,
                    "record_id": item.journal_record_id,
                    "status": done or intake_journal.PENDING,
                    "publication": "deferred_to_canonical_publication",
                }
            )
        else:
            raise RuntimeError(f"unsupported episode SRAM item kind: {item.kind}")
    return {
        "enabled": True,
        "ingested": ingested,
        "turns": turns,
        "recalls": recalls,
        "memories": memories,
        "events": events,
        "errors": errors,
        "skipped": skipped,
    }


def _empty_result(**over: Any) -> Dict[str, Any]:
    """The flush return contract, in one place.

    Every producer path supplies every key, so the DeepSleep consumer can read
    ``errors`` and ``skipped`` without a shape check.  Both carry work that did
    NOT reach memory: ``errors`` is "it raised", ``skipped`` is "it was
    rejected".  Neither may be inferable only from a missing key — an outage and
    a clean episode must never share a return value."""
    base: Dict[str, Any] = {
        "enabled": True,
        "ingested": [],
        "turns": [],
        "recalls": [],
        "memories": [],
        "events": [],
        "errors": [],
        "skipped": [],
    }
    base.update(over)
    return base


def _flush_bucket(
    bucket: _Bucket,
    *,
    consolidate: Optional[Callable[[List[FreshItem]], Any]] = None,
) -> Dict[str, Any]:
    """Drain one captured prefix with exclusive execution and explicit ownership.

    Retain the batch on failure. New items appended during the drain remain held.
    The consolidator and its nested intake run bound to the named episode, not
    whichever episode the caller happened to be serving before this operation.
    """
    with _lock:
        if _buckets.get(bucket.episode_id) is not bucket:
            raise RuntimeError("episode SRAM owner changed before drain")
        if bucket.draining:
            raise RuntimeError(f"episode SRAM bucket {bucket.episode_id} is already draining")
        items = list(bucket.items)
        if not items:
            return _empty_result()
        bucket.draining = True
    prior_draining = is_draining()
    prior_binding = getattr(_binding, "episode_id", None)
    _drain_state.active = True
    _binding.episode_id = bucket.episode_id
    try:
        result = (consolidate or _default_consolidate)(items)
        with _lock:
            if _buckets.get(bucket.episode_id) is not bucket:
                raise RuntimeError("episode SRAM owner changed during drain")
            if bucket.items[: len(items)] != items:
                raise RuntimeError("episode SRAM changed before its consolidated prefix drained")
            del bucket.items[: len(items)]
        return result if isinstance(result, dict) else _empty_result(consolidated=len(items))
    finally:
        _drain_state.active = prior_draining
        _binding.episode_id = prior_binding
        with _lock:
            bucket.draining = False


def flush(
    episode_id: Optional[str] = None,
    *,
    consolidate: Optional[Callable[[List[FreshItem]], Any]] = None,
) -> Dict[str, Any]:
    """Drain a named or thread-owned episode; never guess an unbound owner."""
    with _lock:
        if not _buckets:
            return _empty_result()
        if episode_id is not None:
            bucket = _buckets.get(str(episode_id or ""))
            if bucket is None:
                return _empty_result(skipped=[{"text": "", "reason": "episode_mismatch"}])
        else:
            bucket = _ensure_bucket()
    return _flush_bucket(bucket, consolidate=consolidate)


def close_bucket(
    episode_id: Optional[str] = None,
    *,
    consolidate: Optional[Callable[[List[FreshItem]], Any]] = None,
) -> Dict[str, Any]:
    """Drain one owner, then remove only that exact, empty, inactive bucket.

    Intake arriving after the captured prefix is preserved and reported as
    pending, rather than discarded by a successful close of the earlier batch.
    """
    with _lock:
        if not _buckets:
            return _empty_result()
        if episode_id is not None:
            bucket = _buckets.get(str(episode_id or ""))
            if bucket is None:
                return _empty_result(skipped=[{"text": "", "reason": "episode_mismatch"}])
        else:
            bucket = _ensure_bucket()
    result = _flush_bucket(bucket, consolidate=consolidate)
    with _lock:
        if _buckets.get(bucket.episode_id) is bucket:
            _clear_drained_bucket(bucket)
        elif bucket.episode_id in _buckets:
            raise RuntimeError("episode SRAM owner changed before close")
    return result


def _clear_drained_bucket(bucket: _Bucket) -> None:
    """Remove an already resolved bucket. The caller holds ``_lock``."""
    if bucket.draining:
        raise RuntimeError(f"episode SRAM bucket {bucket.episode_id} is still draining")
    if bucket.items:
        raise RuntimeError(
            f"episode SRAM bucket {bucket.episode_id} still has {len(bucket.items)} pending items"
        )
    _buckets.pop(bucket.episode_id, None)
    if getattr(_binding, "episode_id", None) == bucket.episode_id:
        _binding.episode_id = None


def clear_bucket(episode_id: Optional[str] = None) -> bool:
    """Remove only drained state; pending intake and active drains cannot be cleared."""
    with _lock:
        if episode_id is not None:
            bucket = _buckets.get(str(episode_id or ""))
        else:
            bucket = _ensure_bucket() if _buckets else None
        if bucket is None:
            return False
        _clear_drained_bucket(bucket)
        return True


def reset() -> None:
    """Drop every open bucket without flushing — test helper only."""
    with _lock:
        _buckets.clear()
        _binding.episode_id = None
