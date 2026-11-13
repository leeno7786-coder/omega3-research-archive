"""The episode commit view — a read-only join over existing authorities (WO-07).

CONTRACTS.md section 3: ``memory_commit_status`` is exposed by ``EpisodeCommitView``, never
guessed inside the sealed content, and "required publication proof must check actual record
membership/coverage and receipt identity, not merely a later wall-clock or a greater high-water
mark". This module is that join. It owns no store, writes nothing, and adds no identity:

* the **intake journal** (``memory/intake_journal.py``) says whether the sealed record's
  canonical write is still an intent (``pending``), was refused (``rejected``), was superseded
  by a retry (``consumed``), or committed under its id;
* the **canonical filesystem** is the truth the committed receipt must match: the record is
  read back by its id and its content hash is recomputed, never trusted from the receipt;
* the **served recall projection** proves publication by MEMBERSHIP. Recall is the compiled
  graph (AGENTS.md section 5: "reindexed by DeepSleep from the updated filesystem"), and the
  compiler turns each lobe-9 episode record into the graph event ``episode-<episode id>``
  carrying the full body — so the proof is that event, in the snapshot's graph, holding the
  referenced sealed content. The coord index is NOT that proof: the canonical writer updates it
  at write time, before any reindex has published anything.

Statuses and what earns them:

======================  =====================================================================
``pending``             the sealed record is journalled but the drain has not committed it
``canonical_committed`` committed and on disk with the referenced content, not in the snapshot
``recall_published``    all of the above AND the snapshot's graph holds the record
``failed``              the drain rejected it, or a retry superseded it before it landed
``unknown``             no journal row and no canonical record carries this reference
======================  =====================================================================

**Snapshot identity.** The one projection path retains no history: a snapshot's identity is the
compiled graph's own snapshot hash (``compiled/latest_snapshot_hash.txt``, the identity memory
epochs already pin), so a snapshot taken before the graph was recompiled is
``HistoricalSnapshotUnavailable`` — never a read of today's contents dressed as the past.

**Window release.** :func:`window_members_unpublished` is the readability gate the window lane
applies before offering a staged cohort for reflection. It checks one leased view of the served
projection, so the whole cohort waits if the latest member has not crossed Program 0 yet.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Optional

from schema.continuity_json import (
    CanonicalEpisodeUnavailable,
    CanonicalRef,
    HistoricalSnapshotUnavailable,
    content_hash,
    freeze,
    require_enum,
    unfreeze,
)
from schema.episode_record import MEMORY_COMMIT_STATUSES, EpisodeRecord
from schema.episode_event import EpisodeEvent
from schema.window_evidence import SnapshotRef

_GRAPH_FILE = "graph.sqlite"


@dataclass(frozen=True)
class EpisodeCommitView:
    """What the existing authorities say about one sealed record, under one snapshot."""

    record_ref: CanonicalRef
    snapshot_generation_id: str
    memory_commit_status: str
    reason: str
    record: Optional[EpisodeRecord] = None
    canonical_receipt: Mapping[str, Any] = field(default_factory=dict)
    publication: Mapping[str, Any] = field(default_factory=dict)
    rejected_intake: tuple[Mapping[str, Any], ...] = ()
    supplemental_events: tuple[EpisodeEvent, ...] = ()

    def __post_init__(self) -> None:
        require_enum(self.memory_commit_status, allowed=MEMORY_COMMIT_STATUSES,
                     where="commit_view.memory_commit_status")
        if not str(self.reason or "").strip():
            raise ValueError("a commit view states the reason for its status")
        object.__setattr__(self, "canonical_receipt", freeze(dict(self.canonical_receipt)))
        object.__setattr__(self, "publication", freeze(dict(self.publication)))
        object.__setattr__(self, "rejected_intake",
                           tuple(freeze(dict(row)) for row in self.rejected_intake))
        object.__setattr__(self, "supplemental_events", tuple(self.supplemental_events))

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_ref": self.record_ref.to_dict(),
            "snapshot_generation_id": self.snapshot_generation_id,
            "memory_commit_status": self.memory_commit_status,
            "reason": self.reason,
            "record": self.record.to_dict() if self.record is not None else None,
            "canonical_receipt": unfreeze(self.canonical_receipt),
            "publication": unfreeze(self.publication),
            "rejected_intake": [unfreeze(row) for row in self.rejected_intake],
            "supplemental_events": [event.to_dict() for event in self.supplemental_events],
        }


# ── the intake journal side ──────────────────────────────────────────────────


def _record_rows(record_id: str) -> list[Any]:
    from memory import intake_journal

    return [
        row for row in intake_journal.records()
        if row.kind == "memory" and str((row.payload or {}).get("id") or "") == record_id
    ]


def _row_body(row: Any) -> dict[str, Any]:
    text = ((row.payload or {}).get("payload") or {}).get("text")
    try:
        body = json.loads(text) if isinstance(text, str) else {}
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _row_sha(row: Any) -> str:
    record = _row_body(row).get("episode_record")
    return content_hash(record) if isinstance(record, dict) else ""


def _ref_from_row(row: Any) -> Optional[CanonicalRef]:
    sha = _row_sha(row)
    record_id = str(row.memory_id or (row.payload or {}).get("id") or "")
    if not sha or not record_id:
        return None
    return CanonicalRef(record_id=record_id, record_version=1, content_sha256=sha)


def episode_record_ref(episode_id: str) -> Optional[CanonicalRef]:
    """The exact reference of *episode_id*'s COMMITTED sealed record, or None.

    The committed intake row names it; a record the drain wrote through with no row (intake
    inside a drain) is named by the canonical record itself, read by its deterministic id.
    """
    from memory import intake_journal
    from memory.episode_record_seal import episode_record_id

    record_id = episode_record_id(episode_id)
    committed = [row for row in _record_rows(record_id)
                 if row.status == intake_journal.COMMITTED]
    if committed:
        return _ref_from_row(committed[-1])
    if _record_rows(record_id):
        return None  # journalled but not committed: pending, rejected or superseded
    record, _why = _canonical_record(record_id)
    if record is None:
        return None
    return CanonicalRef(record_id=record_id, record_version=1,
                        content_sha256=record.content_sha256())


def pending_episode_record_ref(episode_id: str, *,
                               include_terminal: bool = False) -> Optional[CanonicalRef]:
    """The reference a sealed record was journalled under that has NOT committed (an intent).

    With ``include_terminal`` the latest row of any status is returned — a rejected or
    superseded seal is still something a reader must be able to name.
    """
    from memory import intake_journal
    from memory.episode_record_seal import episode_record_id

    rows = [row for row in _record_rows(episode_record_id(episode_id))
            if include_terminal
            or row.status in (intake_journal.PENDING, intake_journal.APPLYING)]
    if not rows:
        return None
    sha = _row_sha(rows[-1])
    record_id = str((rows[-1].payload or {}).get("id") or "")
    if not sha or not record_id:
        return None
    return CanonicalRef(record_id=record_id, record_version=1, content_sha256=sha)


def _rejected_intake(episode_id: str) -> list[dict[str, Any]]:
    """This episode's intake that did NOT land: rejected or uncertain, with its reason."""
    from memory import intake_journal

    if not episode_id:
        return []
    out = []
    for row in intake_journal.records():
        if row.status not in (intake_journal.REJECTED, intake_journal.UNCERTAIN):
            continue
        if episode_id not in (row.origin_episode_id, row.owner_episode_id):
            continue
        out.append({
            "record_id": row.record_id,
            "event_id": row.event_id,
            "memory_id": str(row.memory_id or (row.payload or {}).get("id") or ""),
            "kind": row.kind,
            "status": row.status,
            "reason": str(row.note or row.status),
        })
    return out


# ── the canonical filesystem side ────────────────────────────────────────────


def canonical_episode_body(record_id: str) -> tuple[Optional[dict[str, Any]], str]:
    """The canonical document body stored under *record_id*, read from disk.

    ``(None, why)`` when no record exists under the id; a record that exists but carries no
    readable sealed record raises. This is the ONE reader of a sealed record's canonical
    document: the commit view reads the record from it, and WO-09's window hydration reads the
    record and its body from it — by exact id, never by scanning for an episode id.
    """
    from memory.engine.walker import load_coord_index, path_for_id

    try:
        load_coord_index()
    except Exception as exc:  # noqa: BLE001 - reported as the cause, never as "absent"
        raise CanonicalEpisodeUnavailable(f"the coord index is unavailable: {exc}") from exc
    where = path_for_id(record_id)
    if not where or not Path(where).is_file():
        return None, "no canonical record exists under this id"
    try:
        document = json.loads(Path(where).read_text(encoding="utf-8"))
        body = json.loads(document.get("text") or "")
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise CanonicalEpisodeUnavailable(
            f"canonical record {record_id!r} does not carry a readable sealed record: {exc}"
        ) from exc
    if not isinstance(body, dict) or not isinstance(body.get("episode_record"), dict):
        raise CanonicalEpisodeUnavailable(
            f"canonical record {record_id!r} does not carry a readable sealed record"
        )
    return body, ""


def _canonical_record(record_id: str) -> tuple[Optional[EpisodeRecord], str]:
    """The sealed record stored under *record_id*, read from disk; ``(None, why)`` if absent."""
    body, why = canonical_episode_body(record_id)
    if body is None:
        return None, why
    try:
        return EpisodeRecord.from_dict(body["episode_record"]), ""
    except (ValueError, KeyError, TypeError) as exc:
        raise CanonicalEpisodeUnavailable(
            f"canonical record {record_id!r} does not carry a readable sealed record: {exc}"
        ) from exc


# ── the served recall projection side ────────────────────────────────────────


@contextmanager
def _served_graph_view() -> Iterator[tuple[str, Path]]:
    """Yield one leased Program 0 graph, or the fixed graph for an unbootstrapped profile."""
    from core import paths as core_paths
    from memory.projection.generation import projection_read_transaction
    from memory.taxonomy import paths as taxonomy_paths

    memory_root = Path(core_paths.MEMORY_ROOT)
    with projection_read_transaction(memory_root) as projection:
        if projection.mode == "unavailable":
            raise HistoricalSnapshotUnavailable(
                f"the served recall projection is unavailable: {projection.reason or 'unknown'}"
            )
        if projection.mode == "generation":
            if projection.view is None:
                raise HistoricalSnapshotUnavailable(
                    "the served recall projection has no readable generation view"
                )
            try:
                graph = projection.view.verified_path(_GRAPH_FILE)
            except Exception as exc:  # noqa: BLE001 — unavailable projection cannot prove membership
                raise HistoricalSnapshotUnavailable(
                    f"the served recall graph cannot be verified: {type(exc).__name__}: {exc}"
                ) from exc
            yield projection.view.generation_id, graph
            return

        compiled = taxonomy_paths.compiled_root(memory_root)
        marker = compiled / "latest_snapshot_hash.txt"
        graph = compiled / _GRAPH_FILE
        if not marker.is_file() or not graph.is_file():
            raise HistoricalSnapshotUnavailable("no recall graph has been compiled")
        yield marker.read_text(encoding="utf-8").strip(), graph


def _unleased_served_graph() -> tuple[str, Path]:
    """Return the current graph without creating reader lease files.

    Commit-view resolution is an observational API. The active generation is retained across
    one publication, so a short read can bind its graph from the active manifest and verify the
    bytes without registering a durable-looking lease as a side effect.
    """
    from core import paths as core_paths
    from memory.projection.generation import manifest_state, projection_root, sha256_file
    from memory.taxonomy import paths as taxonomy_paths

    memory_root = Path(core_paths.MEMORY_ROOT)
    projection = projection_root(memory_root)
    state = manifest_state(projection)
    if state.mode == "unavailable":
        raise HistoricalSnapshotUnavailable(
            f"the served recall projection is unavailable: {state.reason or 'unknown'}"
        )
    if state.mode == "generation":
        manifest = state.manifest
        if manifest is None:
            raise HistoricalSnapshotUnavailable("the served recall projection has no manifest")
        graph = projection / manifest.generation_dir / _GRAPH_FILE
        try:
            if sha256_file(graph) != manifest.content.graph_sqlite_sha256:
                raise HistoricalSnapshotUnavailable(
                    f"generation {manifest.generation_id} graph does not match its manifest"
                )
        except OSError as exc:
            raise HistoricalSnapshotUnavailable(
                f"the served recall graph cannot be read: {type(exc).__name__}: {exc}"
            ) from exc
        return manifest.generation_id, graph

    compiled = taxonomy_paths.compiled_root(memory_root)
    marker = compiled / "latest_snapshot_hash.txt"
    graph = compiled / _GRAPH_FILE
    if not marker.is_file() or not graph.is_file():
        raise HistoricalSnapshotUnavailable("no recall graph has been compiled")
    return marker.read_text(encoding="utf-8").strip(), graph


def published_projection_id() -> str:
    """The identity of the recall projection being served now (see the module docstring)."""
    generation_id, _graph = _unleased_served_graph()
    return generation_id


def _graph_episode_sha(graph: Path, episode_id: str) -> Optional[str]:
    """The sealed content hash the graph's ``episode-<id>`` event carries; None when absent.

    An event present WITHOUT a sealed record is an episode that predates sealing: it is in the
    graph, and its hash is the empty string.
    """
    from memory.taxonomy.graph_store import connect_readonly, get_event

    connection = connect_readonly(graph)
    try:
        row = get_event(connection, f"episode-{episode_id}")
    finally:
        connection.close()
    if row is None:
        return None
    try:
        payload = json.loads(row.get("payload_json") or "{}")
    except ValueError:
        return ""
    record = payload.get("episode_record") if isinstance(payload, dict) else None
    return content_hash(record) if isinstance(record, dict) else ""


def snapshot_graph(snapshot: SnapshotRef) -> Path:
    """The recall graph *snapshot* names, read as of then — or ``HistoricalSnapshotUnavailable``.

    The one projection path retains nothing: a snapshot is readable only while the served graph
    is still the one it names, and after a recompile it is unavailable — never today's graph
    dressed as the past. WO-10's history reads its corpus through this, the same authority
    publication is proven by.
    """
    snap = SnapshotRef.coerce(snapshot)
    served, graph = _unleased_served_graph()
    if served != snap.generation_id:
        raise HistoricalSnapshotUnavailable(
            "the recall graph this snapshot names has been recompiled, and the projection "
            "retains no history to read it as of then"
        )
    return graph


@contextmanager
def leased_snapshot_graph(snapshot: SnapshotRef) -> Iterator[Path]:
    """:func:`snapshot_graph`, holding the active generation lease through the read."""
    snap = SnapshotRef.coerce(snapshot)
    with _served_graph_view() as (served, graph):
        if served != snap.generation_id:
            raise HistoricalSnapshotUnavailable(
                "the recall graph this snapshot names has been recompiled, and the projection "
                "retains no history to read it as of then"
            )
        yield graph


# ── the join ─────────────────────────────────────────────────────────────────


def _receipt_of(row: Any, sha: str) -> dict[str, Any]:
    return {
        "journal_record_id": row.record_id,
        "event_id": row.event_id,
        "memory_id": str(row.memory_id or (row.payload or {}).get("id") or ""),
        "status": row.status,
        "content_sha256": sha,
    }


def resolve_episode_commit(record_ref: CanonicalRef, *, snapshot: SnapshotRef) -> EpisodeCommitView:
    """Join the intake, canonical and projection authorities for one exact sealed record."""
    from memory import intake_journal

    ref = CanonicalRef.coerce(record_ref, where="resolve_episode_commit.record_ref")
    snap = SnapshotRef.coerce(snapshot)
    rows = [row for row in _record_rows(ref.record_id) if _row_sha(row) == ref.content_sha256]
    row = rows[-1] if rows else None
    episode_id = str((row.payload or {}).get("cycle_id") or "") if row is not None else ""

    def view(status: str, reason: str, **extra: Any) -> EpisodeCommitView:
        return EpisodeCommitView(
            record_ref=ref,
            snapshot_generation_id=snap.generation_id,
            memory_commit_status=status,
            reason=reason,
            rejected_intake=tuple(_rejected_intake(extra.pop("episode_id", episode_id))),
            **extra,
        )

    if row is not None and row.status in (intake_journal.PENDING, intake_journal.APPLYING):
        return view("pending", "the sealed record is journalled and held; the drain has not "
                    "committed it — a recorded intent is not a committed effect",
                    canonical_receipt=_receipt_of(row, ref.content_sha256))
    if row is not None and row.status == intake_journal.REJECTED:
        return view("failed", f"the drain rejected the sealed record: {row.note or 'no reason'}",
                    canonical_receipt=_receipt_of(row, ref.content_sha256))
    if row is not None and row.status == intake_journal.CONSUMED:
        return view("failed", f"the sealed record never landed: {row.note or 'superseded'}",
                    canonical_receipt=_receipt_of(row, ref.content_sha256))
    if row is not None and row.status == intake_journal.UNCERTAIN:
        return view("unknown", f"the drain's outcome is uncertain: {row.note or 'no reason'}",
                    canonical_receipt=_receipt_of(row, ref.content_sha256))

    record, why = _canonical_record(ref.record_id)
    if record is None:
        if row is not None:  # committed receipt, no record: an integrity failure, not "unknown"
            raise CanonicalEpisodeUnavailable(
                f"sealed record {ref.record_id!r} has a committed receipt but {why}"
            )
        return view("unknown", "no journal row and no canonical record carry this reference")
    if record.content_sha256() != ref.content_sha256:
        if row is not None:
            raise CanonicalEpisodeUnavailable(
                f"canonical record {ref.record_id!r} does not hold the committed content"
            )
        return view("unknown", "the canonical record under this id holds different content")
    receipt = (_receipt_of(row, ref.content_sha256) if row is not None
               else {"status": "no_intake_row", "memory_id": ref.record_id,
                     "content_sha256": ref.content_sha256})
    from memory.episode_event_publication import graph_supplements

    # This observational join must not create lease artifacts in canonical memory.
    # Resolve one exact graph path for both reads, preserving the existing read-only API.
    graph = snapshot_graph(snap)
    member = _graph_episode_sha(graph, record.episode_id) == record.content_sha256()
    supplements = graph_supplements(graph, ref) if member else ()
    publication = {
        "generation_id": snap.generation_id,
        "member": member,
        "coverage_clock": snap.coverage_clock.to_dict(),
    }
    if publication["member"]:
        return view("recall_published", "committed, and the snapshot's recall graph holds it",
                    record=record, canonical_receipt=receipt, publication=publication,
                    episode_id=record.episode_id, supplemental_events=supplements)
    return view("canonical_committed", "committed canonically; the snapshot's recall graph does "
                "not hold it, whatever its coverage clock says",
                record=record, canonical_receipt=receipt, publication=publication,
                episode_id=record.episode_id)


# ── the window release gate ─────────────────────────────────────────────────


def window_members_unpublished(
    episode_ids: Iterable[str],
    *,
    member_refs: Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    """Members whose exact seal or later captured events are absent from the served graph.

    DeepSleep appends a new member inside the serialized canonical commit and runs Program 0
    after releasing that lock. Read all ids against one leased projection view: before that
    publication the new id is absent, and on the next check it is present. If the projection
    cannot be read, conservatively hold the complete cohort. When the checkpoint supplies pinned
    references, matching the episode id alone is not enough: a later body under the same graph
    event must not release a cohort pinned to an older canonical hash.
    """
    members = tuple(dict.fromkeys(str(item) for item in episode_ids if str(item)))
    if not members:
        return ()
    try:
        from memory.episode_event_publication import graph_supplements
        from memory.episode_events import read_episode_events
        from memory.episode_record_seal import episode_record_id

        expected_hashes = {}
        if member_refs is not None:
            for episode_id in members:
                raw = member_refs.get(episode_id)
                if raw is None:
                    return members
                expected_hashes[episode_id] = CanonicalRef.coerce(
                    raw, where=f"window.member_refs[{episode_id}]"
                ).content_sha256
        with _served_graph_view() as (_generation_id, graph):
            unpublished = []
            for episode_id in members:
                actual = _graph_episode_sha(graph, episode_id)
                if actual is None or (episode_id in expected_hashes
                                      and actual != expected_hashes[episode_id]):
                    unpublished.append(episode_id)
                    continue
                record, _why = _canonical_record(episode_record_id(episode_id))
                if record is None or record.content_sha256() != actual:
                    unpublished.append(episode_id)
                    continue
                ref = CanonicalRef(record_id=episode_record_id(episode_id), record_version=1,
                                   content_sha256=actual)
                published = {
                    event.event_id: event
                    for event in (*record.events["by_value"], *graph_supplements(graph, ref))
                }
                # Intake is consulted only as an obligation: pending content is never
                # exposed as canonical evidence. A post-publication failure holds its
                # cohort until a retry publishes that exact occurrence beside the seal.
                if any(published.get(event.event_id) != event
                       for event in read_episode_events(episode_id)):
                    unpublished.append(episode_id)
            return tuple(unpublished)
    except Exception:  # noqa: BLE001 — an unprovable member must never release its cohort
        return members


__all__ = [
    "EpisodeCommitView",
    "canonical_episode_body",
    "episode_record_ref",
    "leased_snapshot_graph",
    "pending_episode_record_ref",
    "published_projection_id",
    "resolve_episode_commit",
    "snapshot_graph",
    "window_members_unpublished",
]
