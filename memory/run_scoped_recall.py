"""Recall of one active Coding Agent run's own coding/dev memories.

Candidates are selected from canonical records (the coordinate index and the files it names
— memory as it stood before this episode; episode SRAM is never read) whose
``enrichment.run_provenance`` matches the resolved :class:`memory.run_anchor.RunAnchor`
exactly. The run scope is applied to the whole candidate set BEFORE ranking and top-k, and
every admitted record is re-read and re-verified against the anchor after the lobes are
merged. Its SourceLedger citation and immutable canonical claim receipt must both verify.
There is no unscoped fallback: a record without matching run provenance is not a memory
of this run.

A run with no matching records is a legitimate ``empty`` result. Anything that prevents the
scan or the verification — an unavailable index, an unreadable canonical file, malformed
provenance, a record that changed between scan and verification, a lobe outside the coding
and dev lanes — raises a :class:`RunScopedRecallError` with its cause attached.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from memory.run_anchor import RUN_PROVENANCE_KEY, RunAnchor, validate_run_provenance

#: Coding (8) and dev (11) are the only lobes run-scoped recall may read.
RUN_SCOPED_LOBES = frozenset({8, 11})
MAX_EXCERPT_CHARS = 1600

_SOURCE_CITATION_FIELDS = (
    "source_id",
    "source_type",
    "source_session_id",
    "source_session_timestamp",
    "span_start",
    "span_end",
    "span_hash",
    "source_content_hash",
)


class RunScopedRecallError(RuntimeError):
    """Run-scoped recall could not run; never a legitimate empty result."""


class RunScopedLobeDenied(RunScopedRecallError):
    """The requested lobes are not an explicit subset of the coding/dev lanes."""


class RunScopedRecallUnavailable(RunScopedRecallError):
    """The canonical index or a canonical record could not be read."""


class RunScopedProvenanceInvalid(RunScopedRecallError):
    """A canonical record carries run provenance that is not well formed."""


class RunScopedVerificationFailed(RunScopedRecallError):
    """An admitted record failed run scope, source, or canonical claim verification."""


@dataclass(frozen=True)
class RunScopedRecord:
    memory_id: str
    coord: Tuple[int, ...]
    title: str
    excerpt: str
    source_citation: Mapping[str, Any] = field(default_factory=dict)
    source_provenance: Mapping[str, Any] = field(default_factory=dict)
    run_provenance: Mapping[str, Any] = field(default_factory=dict)
    score: float = 0.0


@dataclass(frozen=True)
class RunScopedRecall:
    anchor: RunAnchor
    status: str  # "verified" | "empty"
    records: Tuple[RunScopedRecord, ...] = ()


def _allowed_lobes(allowed_lobes: Optional[Iterable[Any]]) -> Tuple[int, ...]:
    from memory.dewey_map import NON_RECALLABLE_LOBES

    if allowed_lobes is None:
        raise RunScopedLobeDenied("run-scoped recall requires explicitly permitted lobes")
    try:
        lobes = tuple(dict.fromkeys(int(lobe) for lobe in allowed_lobes))
    except (TypeError, ValueError) as exc:
        raise RunScopedLobeDenied(f"invalid lobe list {allowed_lobes!r}") from exc
    if not lobes:
        raise RunScopedLobeDenied("run-scoped recall requires at least one permitted lobe")
    outside = [lobe for lobe in lobes if lobe not in RUN_SCOPED_LOBES or lobe in NON_RECALLABLE_LOBES]
    if outside:
        raise RunScopedLobeDenied(f"lobes {outside} are outside the coding/dev run scope")
    return lobes


def _read_canonical(memory_id: str, path: Path) -> Dict[str, Any]:
    """Read one canonical record; any unreadable state is a retrieval failure."""
    try:
        record = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RunScopedRecallUnavailable(
            f"canonical record {memory_id} at {path} is unreadable") from exc
    if not isinstance(record, dict):
        raise RunScopedRecallUnavailable(f"canonical record {memory_id} is not an object")
    if str(record.get("id") or "") != memory_id:
        raise RunScopedRecallUnavailable(
            f"canonical record at {path} is {record.get('id')!r}, index names {memory_id}")
    return record


def _record_run_provenance(record: Mapping[str, Any], memory_id: str) -> Optional[Dict[str, Any]]:
    enrichment = record.get("enrichment")
    if enrichment is None:
        return None
    if not isinstance(enrichment, Mapping):
        raise RunScopedProvenanceInvalid(f"canonical record {memory_id} enrichment is not an object")
    if RUN_PROVENANCE_KEY not in enrichment:
        return None
    try:
        return validate_run_provenance(enrichment[RUN_PROVENANCE_KEY])
    except ValueError as exc:
        raise RunScopedProvenanceInvalid(
            f"canonical record {memory_id} has malformed run provenance: {exc}") from exc


def _matches(provenance: Optional[Mapping[str, Any]], anchor: RunAnchor) -> bool:
    return (
        provenance is not None
        and provenance["job_id"] == anchor.job_id
        and provenance["project_root"] == anchor.project_root
        and provenance["thread_id"] == anchor.thread_id
        and provenance["instruction_revision"] <= anchor.instruction_revision
    )


def _record_lobe(record: Mapping[str, Any], memory_id: str) -> int:
    coord = record.get("coord")
    if not isinstance(coord, list) or not coord:
        raise RunScopedRecallUnavailable(f"canonical record {memory_id} has no coordinate")
    try:
        return int(coord[0])
    except (TypeError, ValueError) as exc:
        raise RunScopedRecallUnavailable(f"canonical record {memory_id} coordinate is invalid") from exc


def _score(query_tokens: set, record: Mapping[str, Any]) -> float:
    from memory.taxonomy.canonicalize import content_tokens

    body = " ".join(str(record.get(key) or "") for key in ("title", "summary", "text"))
    return float(len(query_tokens & content_tokens(body)))


def _verify_canonical_proof(memory_id: str, record: Mapping[str, Any]) -> Mapping[str, Any]:
    """Verify the exact reread record that will supply the handoff excerpt."""
    try:
        from memory.engine import walker
        from memory.projection_receipt import verify_projection_receipt
        from memory.recall_verifier import _verify_source_proof

        source = _verify_source_proof(record, (), root=walker.MEMORY_ROOT)
        if source.get("status") != "verified":
            raise RunScopedVerificationFailed(
                f"canonical record {memory_id} source proof failed: {source.get('reason')}"
            )
        receipt = verify_projection_receipt(record, root=walker.MEMORY_ROOT)
        if not receipt.ok:
            raise RunScopedVerificationFailed(
                f"canonical record {memory_id} claim proof failed: {receipt.reason}"
            )
    except RunScopedRecallError:
        raise
    except Exception as exc:
        raise RunScopedRecallUnavailable(
            f"canonical record {memory_id} proof unavailable: {exc}"
        ) from exc
    return source["proof"]


def _shape(memory_id: str, record: Mapping[str, Any], provenance: Mapping[str, Any], score: float,
           source_proof: Mapping[str, Any]) -> RunScopedRecord:
    source = record.get("provenance") if isinstance(record.get("provenance"), Mapping) else {}
    return RunScopedRecord(
        memory_id=memory_id,
        coord=tuple(int(value) for value in record.get("coord") or ()),
        title=str(record.get("title") or ""),
        excerpt=str(record.get("text") or record.get("summary") or "")[:MAX_EXCERPT_CHARS],
        source_citation={key: source_proof[key] for key in _SOURCE_CITATION_FIELDS if key in source_proof},
        source_provenance=dict(source),
        run_provenance=dict(provenance),
        score=score,
    )


def recall_run_scoped(
    anchor: RunAnchor,
    query: str,
    *,
    allowed_lobes: Optional[Iterable[Any]],
    limit: int,
) -> RunScopedRecall:
    """Recall the anchored run's canonical coding/dev records, scoped before top-k."""
    if not isinstance(anchor, RunAnchor):
        raise TypeError("run-scoped recall requires a resolved RunAnchor")
    lobes = _allowed_lobes(allowed_lobes)
    if type(limit) is not int or limit <= 0:
        raise ValueError("run-scoped recall limit must be a positive integer")

    from memory.engine import walker
    from memory.taxonomy.canonicalize import content_tokens

    try:
        walker.load_coord_index()
    except walker.CoordIndexUnavailable as exc:
        raise RunScopedRecallUnavailable(f"coordinate index unavailable: {exc}") from exc
    snapshot = walker.coord_index_snapshot()

    query_tokens = content_tokens(str(query or ""))
    scoped: List[Tuple[float, str, Path]] = []
    for memory_id in sorted(snapshot):
        entry = snapshot[memory_id]
        try:
            lobe = int(entry["coord"][0])
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise RunScopedRecallUnavailable(f"index entry {memory_id} has no coordinate") from exc
        if lobe not in lobes:
            continue
        path = walker.resolve_coord_index_path(entry.get("path"))
        if path is None:
            raise RunScopedRecallUnavailable(f"index entry {memory_id} names no readable path")
        record = _read_canonical(memory_id, path)
        if not _matches(_record_run_provenance(record, memory_id), anchor):
            continue
        scoped.append((_score(query_tokens, record), memory_id, path))

    # Scope first, then rank and cut: top-k can never admit another run's record.
    scoped.sort(key=lambda row: (-row[0], row[1]))
    admitted: List[RunScopedRecord] = []
    for score, memory_id, path in scoped[:limit]:
        record = _read_canonical(memory_id, path)
        provenance = _record_run_provenance(record, memory_id)
        lobe = _record_lobe(record, memory_id)
        if lobe not in lobes or not _matches(provenance, anchor):
            raise RunScopedVerificationFailed(
                f"canonical record {memory_id} no longer matches run {anchor.job_id}")
        source_proof = _verify_canonical_proof(memory_id, record)
        admitted.append(_shape(memory_id, record, provenance, score, source_proof))

    return RunScopedRecall(
        anchor=anchor,
        status="verified" if admitted else "empty",
        records=tuple(admitted),
    )
