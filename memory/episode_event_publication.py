"""Publish events captured after an episode's immutable seal through the same memory lane.

A retry never rewrites its sealed EpisodeRecord. Events which occurred afterwards are
ordinary temporal memory envelopes, bound to that exact parent record and carrying the
existing EpisodeEvent by value. Their visibility comes from the requested graph snapshot,
not the intake journal or today's canonical files.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from schema.continuity_json import CanonicalEpisodeUnavailable, CanonicalRef
from schema.episode_event import EpisodeEvent
from schema.episode_record import EpisodeRecord


@dataclass(frozen=True)
class EpisodeEventPublication:
    """An exact parent and the events this terminus must publish beside its seal."""

    record: EpisodeRecord
    parent_ref: CanonicalRef
    supplements: tuple[EpisodeEvent, ...]
    recovered: bool = False


def event_memory_id(event_id: str) -> str:
    return "episode_event." + hashlib.sha256(event_id.encode("utf-8")).hexdigest()


def supplement_envelope(event: EpisodeEvent, parent: CanonicalRef):
    """Build a standard memory envelope; intake/drain remains its only writer."""
    from memory.api import CanonicalEnvelope
    from memory.episode_record_seal import episode_record_id

    if parent.record_id != episode_record_id(event.episode_id):
        raise ValueError("a supplemental event must name its own episode's sealed record")
    memory_id = event_memory_id(event.event_id)
    reason = str(event.payload.get("reason") or event.event_kind)
    return CanonicalEnvelope(
        cycle_id=event.episode_id,
        coord=[9, 1, 0],
        type="episode_event",
        id=memory_id,
        timestamp_created=event.observed_at,
        source="lobe:temporal",
        payload={
            "id": memory_id,
            "title": f"{event.phase}: {event.event_kind}",
            "summary": reason[:240],
            "text": reason,
            "subtopic": "episode_event",
            "tags": ["episode", "deepsleep", "terminus"],
            "enrichment": {
                "canonical": True,
                "episode_id": event.episode_id,
                "event_id": memory_id,
                "event_kind": "deepsleep_summary",
                "event_source": "deepsleep.episode_event",
                "closed_at": event.observed_at,
                "parent_episode_ref": parent.to_dict(),
                "episode_event": event.to_dict(),
                "episode_event_sha256": event.sealed_sha256(),
            },
        },
    )


def decode_supplement(payload: Mapping[str, Any], parent: CanonicalRef) -> EpisodeEvent:
    """Check parent identity and event integrity before exposing its content."""
    actual = CanonicalRef.coerce(payload.get("parent_episode_ref"), where="supplement.parent")
    if actual != parent:
        raise CanonicalEpisodeUnavailable("supplement does not hold the exact parent episode ref")
    event = EpisodeEvent.from_dict(payload.get("episode_event"))
    from memory.episode_record_seal import episode_record_id

    if episode_record_id(event.episode_id) != parent.record_id:
        raise CanonicalEpisodeUnavailable("supplement event belongs to another episode")
    if event.sealed_sha256() != payload.get("episode_event_sha256"):
        raise CanonicalEpisodeUnavailable("supplement event does not match its recorded digest")
    if payload.get("event_id") != event_memory_id(event.event_id):
        raise CanonicalEpisodeUnavailable("supplement canonical identity does not match its event")
    return event


def stage_event_supplements(record: Any, parent: CanonicalRef) -> tuple[EpisodeEvent, ...]:
    """Admit every captured event absent from the frozen record into this episode's SRAM."""
    from memory.api import intake_memory, intake_receipt_accepted
    from memory.episode_events import read_episode_events

    sealed = {event.event_id for event in record.events["by_value"]}
    events = tuple(event for event in read_episode_events(record.episode_id)
                   if event.event_id not in sealed)
    for event in events:
        receipt = intake_memory(
            supplement_envelope(event, parent), propose=False, bypass_guardian=True,
            event_id=f"publish-episode-event:{event.event_id}",
        )
        if not intake_receipt_accepted(receipt):
            raise RuntimeError(f"supplement intake refused for {event.event_id}: {receipt.message}")
    return events


def stage_event_publications(
    record: EpisodeRecord, parent: CanonicalRef,
) -> tuple[EpisodeEventPublication, ...]:
    """Stage this owner's events and adopted events under their original proven parents.

    Recovery ownership lives in the intake journal. Consult it even after a failed
    drain emptied SRAM: those pending events remain this owner's obligation.

    An adopted event whose source episode has no committed seal (the source died
    before its terminus, or its seal is journalled but not yet committed) has no
    parent to publish under. Its row stays PENDING, deferred exactly as the SRAM
    drain reports it: never published under another parent, never dropped, and
    never a reason to fail this unrelated terminus. A crash would otherwise fail
    every later terminus on the pipeline that adopts it. A seal that exists but
    is unreadable or changed still fails hard.
    """
    from memory import intake_journal
    from memory.episode_commit_view import canonical_episode_body, episode_record_ref
    from memory.episode_events import _event_from_record, _is_event_row

    sources = set()
    for row in intake_journal.pending_records(owner_episode_id=record.episode_id):
        if _is_event_row(row):
            event = _event_from_record(row)
            if event.episode_id != record.episode_id:
                sources.add(event.episode_id)

    parents = [(record, parent, False)]
    for episode_id in sorted(sources):
        reference = episode_record_ref(episode_id)
        if reference is None:
            continue  # unsealed source: its rows stay pending (see docstring)
        body, why = canonical_episode_body(reference.record_id)
        if body is None:
            raise CanonicalEpisodeUnavailable(
                f"recovered episode event parent {reference.record_id} is unavailable: {why}"
            )
        original = EpisodeRecord.from_dict(body["episode_record"])
        if original.episode_id != episode_id or original.content_sha256() != reference.content_sha256:
            raise CanonicalEpisodeUnavailable(
                f"recovered episode event parent {reference.record_id} has changed content"
            )
        parents.append((original, reference, True))
    return tuple(
        EpisodeEventPublication(original, reference,
                                stage_event_supplements(original, reference), recovered)
        for original, reference, recovered in parents
    )


def verify_committed_supplements(events: tuple[EpisodeEvent, ...], parent: CanonicalRef) -> None:
    """The drain must commit every required supplement with its exact bytes."""
    from memory.engine.walker import load_coord_index, path_for_id

    if not events:
        return
    load_coord_index()
    for event in events:
        memory_id = event_memory_id(event.event_id)
        where = path_for_id(memory_id)
        if not where or not Path(where).is_file():
            raise RuntimeError(f"required episode event {event.event_id} did not commit")
        document = json.loads(Path(where).read_text(encoding="utf-8"))
        if document.get("id") != memory_id or document.get("type") != "episode_event":
            raise RuntimeError(f"canonical supplement {memory_id} has the wrong identity or type")
        actual = decode_supplement(document.get("enrichment") or {}, parent)
        if actual != event:
            raise RuntimeError(f"canonical supplement {memory_id} holds different event content")


def graph_supplements(graph: Path, parent: CanonicalRef) -> tuple[EpisodeEvent, ...]:
    """Read supplements only from the supplied projection snapshot, with exact parent binding."""
    from memory.taxonomy.graph_store import connect_readonly

    connection = connect_readonly(graph)
    try:
        rows = connection.execute(
            "SELECT event_id, payload_json FROM events WHERE event_kind = ?",
            ("deepsleep_summary",),
        ).fetchall()
    finally:
        connection.close()
    found: dict[str, EpisodeEvent] = {}
    for row in rows:
        if not str(row[0]).startswith("episode_event."):
            continue
        try:
            payload = json.loads(row[1])
            reference = payload.get("parent_episode_ref") if isinstance(payload, dict) else None
            if not isinstance(reference, dict):
                raise CanonicalEpisodeUnavailable("published supplement has no parent episode ref")
            if reference.get("record_id") != parent.record_id:
                continue
            event = decode_supplement(payload, parent)
            if str(row[0]) != event_memory_id(event.event_id):
                raise CanonicalEpisodeUnavailable("published supplement has the wrong graph identity")
        except (KeyError, TypeError, ValueError) as exc:
            raise CanonicalEpisodeUnavailable(f"unreadable published episode supplement: {exc}") from exc
        if event.event_id in found:
            raise CanonicalEpisodeUnavailable(f"duplicate published event {event.event_id}")
        found[event.event_id] = event
    return tuple(sorted(found.values(), key=lambda event: event.event_seq))


def verify_published_supplements(
    events: tuple[EpisodeEvent, ...], parent: CanonicalRef, *, episode_id: str,
) -> None:
    """Prove the exact parent and every supplement before acknowledging any episode event."""
    from memory.episode_commit_view import _graph_episode_sha, _served_graph_view

    with _served_graph_view() as (_generation, graph):
        if _graph_episode_sha(graph, episode_id) != parent.content_sha256:
            raise RuntimeError(f"required episode event parent {parent.record_id} was not published")
        published = {event.event_id: event for event in graph_supplements(graph, parent)}
    for event in events:
        if published.get(event.event_id) != event:
            raise RuntimeError(f"required episode event {event.event_id} was not published")
