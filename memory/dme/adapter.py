"""ThoughtEngineAdapter / DME SRAM projection v2.

MatMul Hamming engine over Dewey-lobe coordinate bitmaps. The engine has one
deterministic NumPy path. Not authority — all hits pass RecallVerifier.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np

from core.symphony_time import now_iso
from memory.dme.coordinate_registry import load_registry
from memory.dme.geometry import (
    DmeGeometryError,
    scoring_geometry_receipt,
    validate_geometry_receipt,
    validate_weight_entries,
)
from memory.dme.lobe_layout import ENTRY_WORDS, coordinate_tuple
from memory.dme.matmul_engine import active_backend
from memory.dme.packer import pack_entry
from memory.taxonomy import graph_store as G
from memory.taxonomy import paths as taxonomy_paths
from memory.taxonomy.normalize import canonical_json, normalize_id_field
from memory.temporal_authority import (
    authoritative_event_timestamp,
    timestamp_epoch_or_zero,
)

log = logging.getLogger("omega.memory.dme.adapter")

# v7 makes binary timestamp rank observed-or-unknown; v6 encoded content-hash
# pseudo-dates. v6 publishes the exact fixed-width scoring geometry. This prevents an older
# Omega Runtime pack (3904 payload bits) or another embedding/vector geometry
# from being mistaken for the standalone 261952-bit coordinate scorer.
# v5 introduced the stemmed coordinate vocabulary and widened entries.
DME_SCHEMA_VERSION = "omega.dme_sram.v7"
DME_PARITY_SCHEMA_VERSION = "omega.dme_parity.v1"

# Task 1 instrumentation (projection-speed plan): realized-work counter --
# counts actual attempts to open+read a canonical record in
# ``_load_canonical_for_index_entry``, run once per active coord_index entry
# by both ``_seats_for_rebuild`` and ``parity_report``.
_canonical_open_count = 0


def canonical_open_count() -> int:
    """Cumulative process-wide count of canonical-file open attempts."""
    return _canonical_open_count


@dataclass(frozen=True)
class DMESeat:
    memory_id: str
    coord: tuple[int, ...]
    title: str = ""
    summary: str = ""
    domain: str = ""
    subtopic: str = ""
    tags: tuple[str, ...] = ()
    sense_ids: tuple[str, ...] = ()
    status: str = "active"
    source_id: str = ""
    source_content_hash: str = ""
    source_session_id: str = ""
    source_session_timestamp: str = ""
    temporal_timestamp: str = ""
    # Build-only source for the semantic retina. It is deliberately excluded
    # from ``to_dict`` so the DME row-index schema and scoring identity do not
    # absorb canonical prose that belongs only to perception.
    retina_text: str = field(default="", repr=False, compare=False)
    row_index: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "coord": list(self.coord),
            "title": self.title,
            "summary": self.summary,
            "domain": self.domain,
            "subtopic": self.subtopic,
            "tags": list(self.tags),
            "sense_ids": list(self.sense_ids),
            "status": self.status,
            "source_id": self.source_id,
            "source_content_hash": self.source_content_hash,
            "source_session_id": self.source_session_id,
            "source_session_timestamp": self.source_session_timestamp,
            "temporal_timestamp": self.temporal_timestamp,
            "row_index": self.row_index,
        }


@dataclass(frozen=True)
class DMEIndex:
    snapshot_hash: str
    seats: tuple[DMESeat, ...] = ()
    sram: np.ndarray = field(default_factory=lambda: np.zeros((0, ENTRY_WORDS), dtype=np.uint64))
    row_index: list[dict[str, Any]] = field(default_factory=list)
    built_at: str = ""
    schema_version: str = DME_SCHEMA_VERSION
    backend: str = "numpy"

    @property
    def seat_count(self) -> int:
        return len(self.seats)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "dme_sram_meta",
            "snapshot_hash": self.snapshot_hash,
            "seat_count": self.seat_count,
            "bits": ENTRY_WORDS * 64,
            "built_at": self.built_at,
            "schema_version": self.schema_version,
            "backend": self.backend,
            "geometry": scoring_geometry_receipt(),
        }


@dataclass(frozen=True)
class DMEParityReport:
    status: str
    graph_active_leaf_count: int
    dme_seat_count: int
    missing_in_dme: tuple[str, ...] = ()
    orphan_in_dme: tuple[str, ...] = ()
    invalid_status_ids: tuple[str, ...] = ()
    snapshot_hash: str = ""
    schema_version: str = DME_PARITY_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "dme_parity_report",
            "status": self.status,
            "graph_active_leaf_count": self.graph_active_leaf_count,
            "dme_seat_count": self.dme_seat_count,
            "missing_in_dme": list(self.missing_in_dme),
            "orphan_in_dme": list(self.orphan_in_dme),
            "invalid_status_ids": list(self.invalid_status_ids),
            "snapshot_hash": self.snapshot_hash,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class DmeRecallExecution:
    """One DME recall execution, retained long enough to build an activation
    trace (memory/dme/activation_trace.py, Cut 1). ``candidates`` is exactly
    what ``recall_candidates`` returns; ``thought_result`` is the scorer's
    already-computed ThoughtResult (None when the store is empty / no query
    coordinates resolved). Never persisted — it exists so the observation the
    scorer already made is not discarded before the trace is built."""

    candidates: tuple[dict[str, Any], ...]
    thought_result: Any | None
    snapshot_hash: str
    backend: str
    truncation_receipts: tuple[dict[str, Any], ...] = ()


def dme_root(root: Path | str | None = None) -> Path:
    return _memory_root(root) / "dme"


def _generation_view(root: Path | str | None):
    """Cut 13 (D-13-2/19): the ambient GenerationView when a manifest exists, None in
    legacy mode. A manifest without an ambient view raises GenerationViewRequired —
    no unleased derived Path is ever returned."""
    from memory.projection.generation import resolve_view_or_legacy

    mode, view = resolve_view_or_legacy(root)
    return view if mode == "generation" else None


def dme_sram_path(root: Path | str | None = None) -> Path:
    view = _generation_view(root)
    return view.sram if view is not None else dme_root(root) / "sram.bin"


def dme_meta_path(root: Path | str | None = None) -> Path:
    view = _generation_view(root)
    return view.sram_meta if view is not None else dme_root(root) / "sram_meta.json"


def dme_row_index_path(root: Path | str | None = None) -> Path:
    view = _generation_view(root)
    return view.sram_index if view is not None else dme_root(root) / "sram_index.json"


def dme_manifest_path(root: Path | str | None = None) -> Path:
    view = _generation_view(root)
    return view.dme_manifest if view is not None else dme_root(root) / "manifest.json"


def validate_index_geometry(index: DMEIndex) -> None:
    """Validate a complete DME generation before it reaches disk."""
    validate_weight_entries(index.sram, label="weight")
    row_count = int(index.sram.shape[0])
    if len(index.row_index) != row_count:
        raise DmeGeometryError(
            "DME row index count mismatch: "
            f"expected_rows={row_count} actual_rows={len(index.row_index)}"
        )
    if index.seat_count != row_count:
        raise DmeGeometryError(
            "DME seat count mismatch: "
            f"expected_rows={row_count} actual_seats={index.seat_count}"
        )


def rebuild_sram_from_graph(
    *,
    root: Path | str | None = None,
    snapshot_hash: str | None = None,
    source_root: Path | str | None = None,
    output_dir: Path | str | None = None,
    registry_input: Path | str | None = None,
    retina_prior=None,
) -> DMEIndex:
    """Rebuild DME SRAM seats from filesystem truth (coord_index), graph fallback.

    Memory-organ Cut 13 (D-13-21/26/27/36/40): ``source_root`` + ``output_dir`` +
    ``registry_input`` select the GENERATION build — seats are read under the captured
    profile-form ``source_root``, the captured registry is extended in DETERMINISTIC
    order (sorted tokens, sorted coordinate tuples; existing assignments never move),
    every artifact is written ONLY into ``output_dir`` with the deterministic schema (no
    wall clock, no backend, no absolute path), hop planes come from the candidate graph
    at ``output_dir/graph.sqlite``, and page>0 coordinates are counted as unscorable.
    ``retina_prior`` is the previous generation's verified retina, whose codes the
    candidate reuses for unchanged texts. The in-place ``root=`` mode below is the
    legacy path.

    Doctrine: Dewey/FS is truth; the graph is a disposable scoring projection.
    Prefer active leaves from ``coord_index.json`` so a restart reseats DME even
    when graph.sqlite payloads drift or are mid-rebuild. Fall back to graph
    ``memory_record`` nodes when the index is missing/empty.
    """
    if source_root is not None or output_dir is not None or registry_input is not None:
        if not (source_root and output_dir and registry_input):
            raise ValueError("rebuild_sram_from_graph: source_root, output_dir and registry_input must be given together")
        return _rebuild_generation(Path(source_root), Path(output_dir), Path(registry_input), snapshot_hash=snapshot_hash,
                                   retina_prior=retina_prior)
    # AGENTS.md §2: the in-place rebuild below writes through ``dme_sram_path`` and friends,
    # which resolve INTO the published generation when a manifest survives — an immutable
    # directory this path has no business rewriting. Refuse before the registry load, the
    # seat scan and every write. The candidate mode above is the publication builder's own
    # seam and is never gated: it writes only inside ``output_dir``.
    from memory.projection.dispatch import assert_single_projection_path

    assert_single_projection_path(root)
    registry = load_registry(root)
    leaves = sorted(_seats_for_rebuild(root=root), key=lambda s: s.memory_id)
    snap_hash = snapshot_hash or _read_snapshot_hash(root)
    rows: list[np.ndarray] = []
    row_index: list[dict[str, Any]] = []
    seats: list[DMESeat] = []

    # _coordinates_for_seat mints coordinates, and CoordinateRegistry.register
    # rewrites the whole registry JSON per newly registered key unless saves
    # are deferred. Measured on one 867-seat LongMemEval case: 7,376 _save()
    # calls, 97% of this function's runtime (192.3s of a 271.6s rebuild).
    # bulk_registration() was written for exactly this and had NO callers
    # anywhere in the tree -- a fix that shipped and never fired. One save at
    # exit; lookups inside still resolve from the in-memory map, so seat
    # geometry is unchanged. registry_saves in the rebuild's realized_work is
    # the firing proof: it drops from O(keys) to 1.
    with registry.bulk_registration():
        for row_idx, seat in enumerate(leaves):
            coords = _coordinates_for_seat(seat, registry)
            fingerprint = _fingerprint_for_id(seat.memory_id)
            row = pack_entry(
                fingerprint,
                timestamp_epoch_or_zero(seat.temporal_timestamp),
                seat.source_id or "gate:memory",
                coords,
            )
            rows.append(row)
            meta = seat.to_dict()
            meta["row_index"] = row_idx
            row_index.append(meta)
            seats.append(
                DMESeat(
                    memory_id=seat.memory_id,
                    coord=seat.coord,
                    title=seat.title,
                    summary=seat.summary,
                    domain=seat.domain,
                    subtopic=seat.subtopic,
                    tags=seat.tags,
                    sense_ids=seat.sense_ids,
                    status=seat.status,
                    source_id=seat.source_id,
                    source_content_hash=seat.source_content_hash,
                    source_session_id=seat.source_session_id,
                    source_session_timestamp=seat.source_session_timestamp,
                    temporal_timestamp=seat.temporal_timestamp,
                    retina_text=seat.retina_text,
                    row_index=row_idx,
                )
            )

    sram = np.vstack(rows) if rows else np.zeros((0, ENTRY_WORDS), dtype=np.uint64)
    backend = active_backend()
    index = DMEIndex(
        snapshot_hash=snap_hash,
        seats=tuple(seats),
        sram=sram,
        row_index=row_index,
        built_at=now_iso(),
        backend=backend,
    )
    _write_index(index, root=root)
    if root is None:
        # NPU plane registration is for the LIVE tree only — a test root
        # registering here could bake a foreign plane under live queries.
        from memory.dme.matmul_engine import register_plane

        register_plane(dme_sram_path(root), index.seat_count)
    log.info(
        "dme.rebuild: seats=%s source=%s snapshot=%s",
        index.seat_count,
        "coord_index" if _coord_index_path(root).exists() else "graph",
        (snap_hash or "")[:12],
    )
    return index


def load_sram(root: Path | str | None = None) -> DMEIndex:
    view = _generation_view(root)
    if view is not None:
        return _load_sram_from_view(root, view)
    meta_path = dme_meta_path(root)
    sram_path = dme_sram_path(root)
    current_hash = _read_snapshot_hash(root)
    if not meta_path.exists() or not sram_path.exists():
        return rebuild_sram_from_graph(root=root, snapshot_hash=current_hash)

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    stored_hash = str(meta.get("snapshot_hash") or "")
    if current_hash != "uncompiled" and stored_hash != current_hash:
        return rebuild_sram_from_graph(root=root, snapshot_hash=current_hash)
    if str(meta.get("schema_version") or "") != DME_SCHEMA_VERSION:
        # projection-config change (e.g. tokenizer) — the graph hash can't
        # see it, the schema version must
        return rebuild_sram_from_graph(root=root, snapshot_hash=current_hash)
    try:
        validate_geometry_receipt(meta.get("geometry"))
    except DmeGeometryError as exc:
        log.warning("dme.load: incompatible scoring geometry; rebuilding: %s", exc)
        return rebuild_sram_from_graph(root=root, snapshot_hash=current_hash)

    raw = sram_path.read_bytes()
    if len(raw) % (ENTRY_WORDS * 8) != 0:
        log.warning("dme.load: malformed SRAM byte width; rebuilding")
        return rebuild_sram_from_graph(root=root, snapshot_hash=current_hash)
    row_count = len(raw) // (ENTRY_WORDS * 8)
    if int(meta.get("seat_count") or 0) != row_count:
        log.warning("dme.load: SRAM/meta row count mismatch; rebuilding")
        return rebuild_sram_from_graph(root=root, snapshot_hash=current_hash)
    sram = np.frombuffer(raw, dtype=np.uint64).reshape(row_count, ENTRY_WORDS).copy()

    row_index: list[dict[str, Any]] = []
    index_path = dme_row_index_path(root)
    if index_path.exists():
        raw_index = json.loads(index_path.read_text(encoding="utf-8"))
        if isinstance(raw_index, list):
            row_index = raw_index

    seats = tuple(_seat_from_dict(row) for row in row_index)
    index = DMEIndex(
        snapshot_hash=stored_hash,
        seats=seats,
        sram=sram,
        row_index=row_index,
        built_at=str(meta.get("built_at") or ""),
        schema_version=str(meta.get("schema_version") or DME_SCHEMA_VERSION),
        backend=active_backend(),
    )
    try:
        validate_index_geometry(index)
    except DmeGeometryError as exc:
        log.warning("dme.load: incompatible index generation; rebuilding: %s", exc)
        return rebuild_sram_from_graph(root=root, snapshot_hash=current_hash)
    if root is None:
        # Live tree only (see rebuild_sram_from_graph): the NPU scores the
        # exact file this array was just read from.
        from memory.dme.matmul_engine import register_plane

        register_plane(sram_path, row_count)
    return index


def _load_sram_from_view(root: Path | str | None, view) -> DMEIndex:
    """Cut 13 generation mode (D-13-3/24): read ONE generation's verified artifacts and
    NEVER rebuild, repair or write. Any missing / corrupt / mismatched artifact raises
    GenerationIntegrityError (the lane is unavailable; the projector publishes a NEW
    generation). Registration with the scorer is generation-pinned + leased."""
    from memory.projection.generation import GenerationIntegrityError

    meta = json.loads(view.read_bytes("sram_meta.json").decode("utf-8"))
    stored_hash = str(meta.get("snapshot_hash") or "")
    if stored_hash != view.manifest.content.graph_snapshot_hash:
        raise GenerationIntegrityError(
            f"generation {view.generation_id[:12]}…: sram_meta snapshot_hash {stored_hash[:12]}… != manifest graph_snapshot_hash")
    if str(meta.get("schema_version") or "") != DME_SCHEMA_VERSION:
        raise GenerationIntegrityError(f"generation {view.generation_id[:12]}…: DME schema {meta.get('schema_version')!r} != {DME_SCHEMA_VERSION}")
    try:
        validate_geometry_receipt(meta.get("geometry"))
    except DmeGeometryError as exc:
        raise GenerationIntegrityError(f"generation {view.generation_id[:12]}…: incompatible scoring geometry: {exc}") from exc
    raw = view.read_bytes("sram.bin")
    if len(raw) % (ENTRY_WORDS * 8) != 0:
        raise GenerationIntegrityError(f"generation {view.generation_id[:12]}…: malformed SRAM byte width")
    row_count = len(raw) // (ENTRY_WORDS * 8)
    if int(meta.get("seat_count") or 0) != row_count:
        raise GenerationIntegrityError(f"generation {view.generation_id[:12]}…: SRAM/meta row count mismatch")
    sram = np.frombuffer(raw, dtype=np.uint64).reshape(row_count, ENTRY_WORDS).copy()
    raw_index = json.loads(view.read_bytes("sram_index.json").decode("utf-8"))
    row_index: list[dict[str, Any]] = raw_index if isinstance(raw_index, list) else []
    seats = tuple(_seat_from_dict(row) for row in row_index)
    index = DMEIndex(
        snapshot_hash=stored_hash, seats=seats, sram=sram, row_index=row_index,
        built_at=str(meta.get("built_at") or ""), schema_version=str(meta.get("schema_version") or DME_SCHEMA_VERSION),
        backend=active_backend(),
    )
    try:
        validate_index_geometry(index)
    except DmeGeometryError as exc:
        raise GenerationIntegrityError(f"generation {view.generation_id[:12]}…: incompatible index generation: {exc}") from exc
    if _is_live_root(root):
        from memory.dme.matmul_engine import register_plane

        register_plane(view.sram, row_count, generation_id=view.generation_id)
    return index


def _is_live_root(root: Path | str | None) -> bool:
    if root is None:
        return True
    from core.paths import MEMORY_ROOT
    from memory.projection.generation import resolve_memory_root

    return resolve_memory_root(root) == Path(MEMORY_ROOT)


def _artifact_fingerprint(root: Path | str | None) -> tuple:
    from memory.dme.hop_engine import hop_plane_strong_path, hop_plane_weak_path

    view = _generation_view(root)
    if view is not None:
        # Cut 13: the fingerprint IS the generation identity (no mtime heuristics)
        return ("generation", view.generation_id)
    stamps = []
    for path in (
        dme_sram_path(root),
        dme_meta_path(root),
        dme_row_index_path(root),
        hop_plane_strong_path(root),
        hop_plane_weak_path(root),
    ):
        try:
            st = path.stat()
            stamps.append((st.st_mtime_ns, st.st_size))
        except OSError:
            stamps.append(None)
    return tuple(stamps)


def _session_index_and_planes(root: Path | str | None):
    """Return resident packs from the active recall session, or load ephemerally.

    Prefer an outer ``dme_recall_session`` (the recall hook). When called
    standalone, open a one-shot session so packs still load → use → unload.
    """
    from memory.dme.recall_hook import dme_recall_session, get_active_dme_session

    active = get_active_dme_session(root)
    if active is not None and active.index is not None:
        return active.index, active.planes

    # Standalone callers (tests / direct recall_candidates): load for this call.
    with dme_recall_session(root) as session:
        return session.index, session.planes


def recall_candidates(
    query: str,
    *,
    allowed_lobes: Iterable[int] | None = None,
    denied_lobes: Iterable[int] | None = None,
    allowed_subdomains: Iterable[int] | None = None,
    limit: int = 10,
    root: Path | str | None = None,
    cue_receipt: Any | None = None,
) -> list[dict[str, Any]]:
    return list(
        recall_candidates_observed(
            query,
            allowed_lobes=allowed_lobes,
            denied_lobes=denied_lobes,
            allowed_subdomains=allowed_subdomains,
            limit=limit,
            root=root,
            cue_receipt=cue_receipt,
        ).candidates
    )


def recall_candidates_observed(
    query: str,
    *,
    allowed_lobes: Iterable[int] | None = None,
    denied_lobes: Iterable[int] | None = None,
    allowed_subdomains: Iterable[int] | None = None,
    limit: int = 10,
    root: Path | str | None = None,
    cue_receipt: Any | None = None,
) -> DmeRecallExecution:
    """Same session logic and candidate output as ``recall_candidates``; also
    returns the scorer's ThoughtResult so the recall hook can build a
    DMEActivationTrace from the execution that actually happened.

    ``cue_receipt`` (memory.routing_cues.CueReceipt, Cut 2) contributes
    already-registered addresses additively — see ``_query_coordinates``."""
    from memory.dme.recall_hook import dme_recall_session, get_active_dme_session
    from memory.projection.generation import projection_read_transaction

    # Cut 13 (D-13-28): one reentrant projection-read transaction around the whole
    # execution — session packs, registry and hop planes come from ONE generation view
    # (reused when an outer read_memory/recall_verified transaction is already open).
    with projection_read_transaction(root):
        # Nested under apply_dme_recall_hook / another session: reuse resident packs.
        # Otherwise open a one-shot session so every recall loads → runs → unloads.
        if get_active_dme_session(root) is None:
            with dme_recall_session(root):
                return _recall_execution_loaded(
                    query,
                    allowed_lobes=allowed_lobes,
                    denied_lobes=denied_lobes,
                    allowed_subdomains=allowed_subdomains,
                    limit=limit,
                    root=root,
                    cue_receipt=cue_receipt,
                )
        return _recall_execution_loaded(
            query,
            allowed_lobes=allowed_lobes,
            denied_lobes=denied_lobes,
            allowed_subdomains=allowed_subdomains,
            limit=limit,
            root=root,
            cue_receipt=cue_receipt,
    )


def _recall_candidates_loaded(
    query: str,
    *,
    allowed_lobes: Iterable[int] | None = None,
    denied_lobes: Iterable[int] | None = None,
    allowed_subdomains: Iterable[int] | None = None,
    limit: int = 10,
    root: Path | str | None = None,
    cue_receipt: Any | None = None,
) -> list[dict[str, Any]]:
    return list(
        _recall_execution_loaded(
            query,
            allowed_lobes=allowed_lobes,
            denied_lobes=denied_lobes,
            allowed_subdomains=allowed_subdomains,
            limit=limit,
            root=root,
            cue_receipt=cue_receipt,
        ).candidates
    )


def _recall_execution_loaded(
    query: str,
    *,
    allowed_lobes: Iterable[int] | None = None,
    denied_lobes: Iterable[int] | None = None,
    allowed_subdomains: Iterable[int] | None = None,
    limit: int = 10,
    root: Path | str | None = None,
    cue_receipt: Any | None = None,
) -> DmeRecallExecution:
    index, hop_planes = _session_index_and_planes(root)
    if index.seat_count == 0:
        return DmeRecallExecution((), None, index.snapshot_hash, index.backend)

    registry = load_registry(root)
    # the structural recall-lane boundary (dewey_map.NON_RECALLABLE_LOBES:
    # 6 journal, 7 vision) — no caller argument reaches those lobes
    from memory.dewey_map import NON_RECALLABLE_LOBES

    denied_all = set(denied_lobes or ()) | set(NON_RECALLABLE_LOBES)
    query_coords = _query_coordinates(
        query,
        registry,
        allowed_lobes=allowed_lobes,
        denied_lobes=denied_all,
        cue_receipt=cue_receipt,
    )
    if not query_coords:
        return DmeRecallExecution((), None, index.snapshot_hash, index.backend)

    allowed_set = set(allowed_lobes) if allowed_lobes is not None else None
    denied_set = denied_all
    allowed_subs = set(allowed_subdomains) if allowed_subdomains is not None else None

    # Thought Engine v2 (memory/dme/hop_engine.py): weighted edge-class hop
    # planes (strong = compiler-proven/entity/session, weak = placement/
    # similarity) + the ratified score table + adaptive hop ladder replace the
    # strict-intersection search. Verifier authority unchanged; hits remain
    # candidates only.
    from memory.dme.hop_engine import search_thought

    target_lobe = None
    if allowed_set is not None and len(allowed_set) == 1:
        target_lobe = next(iter(allowed_set))
    result = search_thought(
        index.sram,
        hop_planes,
        index.row_index,
        query_coords,
        target_lobe=target_lobe,
        max_results=max(1, int(limit or 10) * 3),
    )

    hits: list[dict[str, Any]] = []
    scored_ids = [str(hit.memory_id or "") for hit in result.hits]
    for hit in result.hits:
        meta = index.row_index[hit.row_index] if hit.row_index < len(index.row_index) else {}
        coord = _coord_tuple(meta.get("coord"))
        if not _passes_filters(coord, allowed_set, denied_set, allowed_subs):
            continue
        row = hit.to_candidate_dict(snapshot_hash=index.snapshot_hash, backend=index.backend)
        row["coord"] = list(coord)
        row["domain"] = str(meta.get("domain") or "")
        row["subtopic"] = str(meta.get("subtopic") or "")
        row["tags"] = list(meta.get("tags") or [])
        row.setdefault("provenance", {})
        row["provenance"].update(
            {
                key: str(meta.get(key) or "")
                for key in (
                    "source_id",
                    "source_content_hash",
                    "source_session_id",
                    "source_session_timestamp",
                )
                if str(meta.get(key) or "")
            }
        )
        hits.append(row)
        if len(hits) >= max(0, int(limit or 0)):
            break
    from memory.recall_truncation import build_truncation_receipt

    truncation_receipts = list(getattr(result, "truncation_receipts", ()) or ())
    truncation_receipts.append(
        build_truncation_receipt(
            "dme_adapter:filters_and_candidate_limit",
            scored_ids,
            (str(hit.get("memory_id") or "") for hit in hits),
            details={
                "candidate_limit": max(0, int(limit or 0)),
                "lobe_and_subdomain_filters_applied": True,
            },
        )
    )
    return DmeRecallExecution(
        tuple(hits), result, index.snapshot_hash, index.backend, tuple(truncation_receipts)
    )


def recall_verified(query: Any, *, root: Path | str | None = None) -> Any:
    from memory.recall_verifier import append_verdict_log, verify_candidates
    from memory.projection.generation import projection_read_transaction

    with projection_read_transaction(root):  # Cut 13: candidates + verification on ONE generation
        candidates = recall_candidates(
            str(getattr(query, "text", "") or ""),
            allowed_lobes=getattr(query, "allowed_lobes", None),
            denied_lobes=getattr(query, "denied_lobes", None),
            allowed_subdomains=getattr(query, "allowed_subdomains", None),
            limit=int(getattr(query, "limit", 10) or 10),
            root=root,
        )
        verdict = verify_candidates(
            str(getattr(query, "text", "") or ""),
            candidates,
            meta={"candidate_source": "dme"},
            root=root,
            allowed_lobes=getattr(query, "allowed_lobes", None),
        )
    try:
        append_verdict_log(verdict, cycle_id=str(getattr(query, "cycle_id", "") or "dme-recall"))
    except Exception:
        pass
    return verdict


def merge_dme_candidate_dicts(
    query: Any,
    candidates: list[dict[str, Any]],
    *,
    root: Path | str | None = None,
    cue_receipt: Any | None = None,
) -> list[dict[str, Any]]:
    merged, _execution = merge_dme_candidate_dicts_observed(
        query, candidates, root=root, cue_receipt=cue_receipt
    )
    return merged


def merge_dme_candidate_dicts_observed(
    query: Any,
    candidates: list[dict[str, Any]],
    *,
    root: Path | str | None = None,
    cue_receipt: Any | None = None,
) -> tuple[list[dict[str, Any]], DmeRecallExecution]:
    """``merge_dme_candidate_dicts`` plus the DmeRecallExecution it merged from."""
    execution = recall_candidates_observed(
        str(getattr(query, "text", "") or ""),
        allowed_lobes=getattr(query, "allowed_lobes", None),
        denied_lobes=getattr(query, "denied_lobes", None),
        allowed_subdomains=getattr(query, "allowed_subdomains", None),
        limit=int(getattr(query, "limit", 10) or 10),
        root=root,
        cue_receipt=cue_receipt,
    )
    merged = _merge_candidate_rows(candidates, list(execution.candidates))
    return merged, execution


def _merge_candidate_rows(
    candidates: list[dict[str, Any]],
    dme_hits: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {
        str(item.get("id") or ""): dict(item) for item in candidates if str(item.get("id") or "")
    }
    for hit in dme_hits:
        memory_id = str(hit.get("memory_id") or "")
        if not memory_id:
            continue
        row = {
            "id": memory_id,
            "coord": hit.get("coord"),
            "score": float(hit.get("score") or 0.0),
            "title": hit.get("title"),
            "summary": hit.get("summary"),
            "domain": hit.get("domain"),
            "subtopic": hit.get("subtopic"),
            "tags": list(hit.get("tags") or []),
            "provenance": {
                "candidate_source": "dme",
                **dict(hit.get("provenance") or {}),
            },
        }
        existing = by_id.get(memory_id)
        if existing is None or float(row["score"]) > float(existing.get("score") or 0.0):
            if existing is not None:
                # a winning score must not erase the other lane's provenance
                row["provenance"] = {
                    **dict(existing.get("provenance") or {}),
                    **row["provenance"],
                }
            by_id[memory_id] = row
        else:
            # DME lost on score: graft its connectivity provenance anyway —
            # the relevance anchor (dme_edge_class) must not depend on which
            # lane happened to rank higher
            existing["provenance"] = {
                **row["provenance"],
                **dict(existing.get("provenance") or {}),
            }
    merged = sorted(
        by_id.values(),
        key=lambda item: (-float(item.get("score") or 0.0), str(item.get("id") or "")),
    )
    return merged


def parity_report(*, root: Path | str | None = None) -> DMEParityReport:
    # Truth for drift = filesystem active leaves ∪ graph active leaves.
    # Seating prefers FS, but parity still flags graph-only / DME-only skew.
    truth_ids = {
        seat.memory_id for seat in _active_filesystem_seats(root=root)
    } | {seat.memory_id for seat in _active_graph_seats(root=root)}
    index = load_sram(root)
    dme_ids = {seat.memory_id for seat in index.seats}
    invalid = tuple(sorted(seat.memory_id for seat in index.seats if seat.status != "active"))
    missing = tuple(sorted(truth_ids - dme_ids))
    orphan = tuple(sorted(dme_ids - truth_ids))
    status = "ok" if not missing and not orphan and not invalid else "drift"
    return DMEParityReport(
        status=status,
        graph_active_leaf_count=len(truth_ids),
        dme_seat_count=len(dme_ids),
        missing_in_dme=missing,
        orphan_in_dme=orphan,
        invalid_status_ids=invalid,
        snapshot_hash=index.snapshot_hash,
    )


def _coordinates_for_seat(seat: DMESeat, registry) -> list[tuple[int, int, int]]:
    lobe = seat.coord[0] if seat.coord else 0
    coords: list[tuple[int, int, int]] = []
    if seat.coord:
        triple = coordinate_tuple(seat.coord)
        if triple is not None:
            coords.append(registry.register_or_lookup(_coord_key(triple), lobe))
    tokens = _tokens(
        " ".join(
            (
                seat.memory_id,
                seat.title,
                seat.summary,
                seat.domain,
                seat.subtopic,
                " ".join(seat.tags),
                " ".join(seat.sense_ids),
            )
        )
    )
    for token in tokens:
        coords.append(registry.register_or_lookup(token, lobe))
    deduped: dict[tuple[int, int, int], None] = {}
    for coord in coords:
        deduped[coord] = None
    return list(deduped.keys())


def _query_coordinates(
    query: str,
    registry,
    *,
    allowed_lobes: Iterable[int] | None,
    denied_lobes: Iterable[int] | None,
    cue_receipt: Any | None = None,
) -> list[tuple[int, int, int]]:
    """Resolve the query to already-registered addresses. Never mints.

    Order (Cut 2, additive): (1) the pre-existing token lookup + near-match
    pass, unchanged; (2) deterministic/entity cue addresses from the
    CueReceipt; (3) generated-cue addresses ONLY when
    OMEGA_DME_LANGUAGE_CUES_APPLY is on (shadow-only until Cut 3). Every
    address passes the same allow/deny lobe filter; stable dedupe by first
    occurrence. Registration APIs are never called here."""
    tokens = _tokens(query)
    allowed = set(allowed_lobes) if allowed_lobes is not None else None
    denied = set(denied_lobes or ())
    coords: list[tuple[int, int, int]] = []
    seen: set[tuple[int, int, int]] = set()

    def _admit(address: tuple[int, int, int] | None) -> None:
        if address is None or address in seen:
            return
        lobe = address[0]
        if lobe in denied:
            return
        if allowed is not None and lobe not in allowed:
            return
        seen.add(address)
        coords.append(address)

    for token in tokens:
        _admit(registry.lookup(token))
        # A source typo mints its OWN coordinate, so a correctly spelled query
        # token resolves to a different address than the record that should
        # answer it — the exact lookup succeeds and the record still never
        # scores. Widening is additive and query-side only: no coordinate is
        # minted, so seat density and the stopword fabrication guard hold.
        for near in registry.near_matches(token):
            _admit(near)

    if cue_receipt is not None:
        for address in _cue_addresses(cue_receipt):
            _admit(tuple(int(v) for v in address))
    return coords


def _cue_addresses(cue_receipt: Any) -> list[tuple[int, int, int]]:
    """Addresses a CueReceipt contributes: deterministic + entity always;
    generated only behind the apply flag. Reads the receipt's source map so
    generated-origin addresses stay independently switchable."""
    from memory.routing_cues import (
        SOURCE_DETERMINISTIC,
        SOURCE_ENTITY,
        SOURCE_GENERATED,
        generated_cues_apply_enabled,
    )

    sources = [SOURCE_DETERMINISTIC, SOURCE_ENTITY]
    if generated_cues_apply_enabled():
        sources.append(SOURCE_GENERATED)
    addresses_for = getattr(cue_receipt, "addresses_for", None)
    if callable(addresses_for):
        return list(addresses_for(*sources))
    # duck-typed receipt without a source map: nothing generated can be
    # distinguished, so only the explicit non-generated view is honoured
    return []


def _coord_key(coord: tuple[int, int, int]) -> str:
    return f"coord:{coord[0]}:{coord[1]}:{coord[2]}"


def _fingerprint_for_id(memory_id: str) -> int:
    digest = hashlib.sha256(memory_id.encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


def _coord_index_path(root: Path | str | None = None) -> Path:
    return _memory_root(root) / "indexes" / "coord_index.json"


def _seats_for_rebuild(*, root: Path | str | None = None) -> list[DMESeat]:
    """Prefer filesystem coord_index (truth); fall back to graph leaves."""
    fs_seats = _active_filesystem_seats(root=root)
    if fs_seats:
        return fs_seats
    return _active_graph_seats(root=root)


def _active_filesystem_seats(*, root: Path | str | None = None) -> list[DMESeat]:
    """Active canonical leaves from ``coord_index.json`` (Dewey/FS truth).

    Minimal index rows (id/coord/path only) resolve status/title/summary from
    the canonical JSON so quarantine/supersede still exclude seats.
    """
    path = _coord_index_path(root)
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict):
        return []

    profile_root = _memory_root(root)
    if profile_root.name == "memory":
        profile_root = profile_root.parent

    seats: list[DMESeat] = []
    from memory.retina import render_record

    for mid, entry in payload.items():
        if not isinstance(entry, dict):
            continue
        canon = _load_canonical_for_index_entry(entry, profile_root=profile_root)
        status = str(
            (canon or {}).get("status")
            or ((canon or {}).get("enrichment") or {}).get("status")
            or entry.get("status")
            or "active"
        ).strip().lower()
        superseded = (canon or {}).get("superseded_by") or entry.get("superseded_by")
        quarantined = bool(
            entry.get("quarantined")
            or ((canon or {}).get("flags") or {}).get("quarantined")
            or status == "quarantined"
        )
        if status != "active" or superseded or quarantined:
            continue
        memory_id = str(
            (canon or {}).get("id") or entry.get("id") or mid or ""
        ).strip()
        coord = _coord_tuple(entry.get("coord") or (canon or {}).get("coord"))
        if not memory_id or len(coord) < 3:
            continue
        tags_raw = entry.get("tags")
        if not isinstance(tags_raw, list):
            tags_raw = (canon or {}).get("tags") if isinstance((canon or {}).get("tags"), list) else []
        seats.append(
            DMESeat(
                memory_id=memory_id,
                coord=coord,
                title=str(
                    entry.get("title")
                    or (canon or {}).get("title")
                    or ""
                ),
                summary=str(
                    entry.get("summary")
                    or entry.get("text_prefix")
                    or (canon or {}).get("summary")
                    or ""
                ),
                domain=str(entry.get("domain") or (canon or {}).get("domain") or ""),
                subtopic=str(
                    entry.get("subtopic")
                    or (canon or {}).get("subtopic")
                    or (canon or {}).get("topic")
                    or ""
                ),
                tags=tuple(str(t) for t in tags_raw if str(t or "").strip()),
                sense_ids=(),
                status="active",
                source_id=str(
                    entry.get("source_id")
                    or ((canon or {}).get("provenance") or {}).get("source_id")
                    or ""
                ),
                source_content_hash=str(
                    entry.get("source_content_hash")
                    or ((canon or {}).get("provenance") or {}).get("source_content_hash")
                    or ""
                ),
                source_session_id=str(
                    entry.get("source_session_id")
                    or ((canon or {}).get("provenance") or {}).get("source_session_id")
                    or ""
                ),
                source_session_timestamp=str(
                    entry.get("source_session_timestamp")
                    or ((canon or {}).get("provenance") or {}).get(
                        "source_session_timestamp"
                    )
                    or ""
                ),
                temporal_timestamp=authoritative_event_timestamp(canon or {}) or "",
                retina_text=render_record(canon or entry),
            )
        )
    return seats


def _load_canonical_for_index_entry(
    entry: Mapping[str, Any],
    *,
    profile_root: Path,
) -> dict[str, Any] | None:
    rel = str(entry.get("path") or "").replace("\\", "/").strip()
    if not rel:
        return None
    candidate = Path(rel)
    if not candidate.is_absolute():
        candidate = profile_root / rel
    if not candidate.is_file():
        return None
    global _canonical_open_count
    _canonical_open_count += 1
    try:
        payload = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _active_graph_seats(*, root: Path | str | None = None) -> list[DMESeat]:
    conn = G.connect(root)
    try:
        rows = conn.execute(
            "SELECT node_id, label, payload_json FROM nodes WHERE node_type = ?",
            ("memory_record",),
        ).fetchall()
    finally:
        conn.close()

    seats: list[DMESeat] = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except Exception:
            continue
        if str(payload.get("type") or "") != "memory_leaf_v2":
            continue
        if str(payload.get("status") or "active").strip().lower() != "active":
            continue
        memory_id = str(payload.get("memory_id") or row["node_id"] or "")
        coord = _coord_tuple(payload.get("coord"))
        if not memory_id or len(coord) < 3:
            continue
        seats.append(_seat_from_leaf(memory_id, payload, label=str(row["label"] or "")))
    return seats


def _seat_from_leaf(memory_id: str, leaf: Mapping[str, Any], *, label: str = "") -> DMESeat:
    from memory.retina import render_record

    coord = _coord_tuple(leaf.get("coord"))
    return DMESeat(
        memory_id=memory_id,
        coord=coord,
        title=str(leaf.get("title") or label or ""),
        summary=str(leaf.get("summary") or ""),
        domain=str(leaf.get("domain") or ""),
        subtopic=str(leaf.get("subtopic") or ""),
        tags=tuple(str(item) for item in leaf.get("tags") or () if str(item or "").strip()),
        sense_ids=tuple(
            str(item) for item in leaf.get("sense_ids") or () if str(item or "").strip()
        ),
        status=str(leaf.get("status") or "active"),
        source_id=str(leaf.get("source_id") or ""),
        source_content_hash=str(leaf.get("source_content_hash") or ""),
        source_session_id=str(leaf.get("source_session_id") or ""),
        source_session_timestamp=str(leaf.get("source_session_timestamp") or ""),
        temporal_timestamp=authoritative_event_timestamp(
            {
                "provenance": {
                    "source_session_timestamp": leaf.get("source_session_timestamp")
                }
            }
        )
        or "",
        retina_text=render_record(leaf),
    )


def _seat_from_dict(row: Mapping[str, Any]) -> DMESeat:
    coord = _coord_tuple(row.get("coord"))
    return DMESeat(
        memory_id=str(row.get("memory_id") or ""),
        coord=coord,
        title=str(row.get("title") or ""),
        summary=str(row.get("summary") or ""),
        domain=str(row.get("domain") or ""),
        subtopic=str(row.get("subtopic") or ""),
        tags=tuple(str(item) for item in row.get("tags") or () if str(item or "").strip()),
        sense_ids=tuple(
            str(item) for item in row.get("sense_ids") or () if str(item or "").strip()
        ),
        status=str(row.get("status") or "active"),
        source_id=str(row.get("source_id") or ""),
        source_content_hash=str(row.get("source_content_hash") or ""),
        source_session_id=str(row.get("source_session_id") or ""),
        source_session_timestamp=str(row.get("source_session_timestamp") or ""),
        temporal_timestamp=str(
            row.get("temporal_timestamp") or row.get("source_session_timestamp") or ""
        ),
        row_index=int(row.get("row_index") or 0),
    )


def _atomic_replace(tmp: Path, dst: Path, *, attempts: int = 6, base_delay: float = 0.04) -> None:
    """``tmp.replace(dst)`` retried on a transient Windows PermissionError.

    A concurrent recall load, the Windows Search indexer, or an AV scanner can
    briefly hold ``dst`` (or the just-written ``tmp``); Windows then refuses the
    rename with WinError 5/32 where POSIX would succeed over the open handle. The
    lock clears in tens of ms, so retry with a short linear backoff (~0.6s worst
    case, off the query hot path). Re-raise the last error if it never frees — the
    caller treats a persistent failure as non-fatal and rebuilds the artifact lazily."""
    for attempt in range(attempts):
        try:
            tmp.replace(dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(base_delay * (attempt + 1))


# ── Cut 13 generation build (D-13-21/26/27/36/40) ────────────────────────────

GENERATION_PLANE_PATHS = {"hop_plane_strong": "planes/hop_plane_strong.bin", "hop_plane_weak": "planes/hop_plane_weak.bin"}


@dataclass(frozen=True)
class GenerationRebuildReport:
    index: DMEIndex
    page0_capacity_by_lobe: dict
    page0_used_by_lobe: dict
    page0_utilization_by_lobe: dict
    page0_overflow_attempts: int
    unscorable_coordinate_count: int
    unscorable: tuple   # (memory_id, coord) for every page>0 coordinate a seat carries
    registry_sha256: str
    semantic_retina: dict   # the generation component pinning the retina built beside the planes


def _coordinates_for_seat_deterministic(seat: DMESeat, registry) -> list[tuple[int, int, int]]:
    """D-13-27: the same coordinates as ``_coordinates_for_seat`` but minted and returned
    in a HASH-SEED-INDEPENDENT order — sorted tokens, then the sorted coordinate tuple
    list (D-13-40). Existing assignments are looked up, never moved."""
    lobe = seat.coord[0] if seat.coord else 0
    coords: list[tuple[int, int, int]] = []
    if seat.coord:
        triple = coordinate_tuple(seat.coord)
        if triple is not None:
            coords.append(registry.register_or_lookup(_coord_key(triple), lobe))
    tokens = sorted(_tokens(" ".join((seat.memory_id, seat.title, seat.summary, seat.domain, seat.subtopic,
                                       " ".join(seat.tags), " ".join(seat.sense_ids)))))
    for token in tokens:
        coords.append(registry.register_or_lookup(token, lobe))
    return sorted(set(tuple(int(v) for v in c) for c in coords))


def _build_generation_retina(seats, output_dir: Path, *, prior=None, activate: bool = False) -> dict:
    """Encode this generation's seats into the semantic retina, beside the planes.

    Perception is built where the other projections are built — in DeepSleep,
    at publication — so an Ettin forward pass per record is paid during
    consolidation instead of on somebody's recall. ``prior`` is the previous
    generation's verified retina; unchanged texts reuse its codes.

    Returns the ``semantic_retina`` generation component that pins what was
    built. A seat set with nothing to render is an explicitly EMPTY plane. Any
    other failure raises with its cause (audit 2026-09-28 M05): the retina holds
    no authority, but a generation that silently lacks it recalls without
    perception while every health surface reports it healthy.
    """
    from memory import retina

    rows = []
    for seat in seats:
        text = str(getattr(seat, "retina_text", "") or "").strip()
        if not text:
            text = retina.render_record(
                {
                    "title": getattr(seat, "title", ""),
                    "summary": getattr(seat, "summary", ""),
                    "text": getattr(seat, "text", ""),
                }
            )
        if text:
            rows.append((seat.memory_id, text))
    if not rows:
        if activate:
            # The retired fixed-name directory is rewritten in place: codes from an
            # earlier corpus must not survive a rebuild that has nothing to perceive.
            for name in (retina.CODES_FILE, retina.INDEX_FILE):
                (Path(output_dir) / name).unlink(missing_ok=True)
            retina.unload()
        return retina.empty_component()
    report = retina.build(rows, output_dir, prior=prior)
    log.info("dme.generation: retina built (%s)", {k: v for k, v in report.items() if k != "component"})
    if activate:
        retina.load(output_dir)
    return report["component"]


def _rebuild_generation(source_root: Path, output_dir: Path, registry_input: Path, *, snapshot_hash: str | None,
                        retina_prior=None) -> GenerationRebuildReport:
    import shutil

    from memory.atomic_replace import atomic_replace_bytes
    from memory.dme.coordinate_registry import CoordinateRegistry
    from memory.dme import coordinate_registry as _CR
    from memory.dme.hop_engine import build_hop_planes
    from memory.projection.generation import sha256_bytes

    PAGE0_SLOTS = _CR.PAGE0_SLOTS   # the allocator's page-0 capacity (the same constant lobe_layout exports)

    output_dir.mkdir(parents=True, exist_ok=True)
    registry_out = output_dir / "coordinate_registry.json"
    shutil.copyfile(registry_input, registry_out)          # the captured authority snapshot, extended in place
    registry = CoordinateRegistry(registry_out, strict=True)
    leaves = sorted(_seats_for_rebuild(root=source_root), key=lambda s: s.memory_id)
    snap_hash = snapshot_hash or _read_snapshot_hash(source_root)
    rows: list[np.ndarray] = []
    row_index: list[dict[str, Any]] = []
    seats: list[DMESeat] = []
    unscorable: list[tuple[str, tuple[int, int, int]]] = []
    with registry.bulk_registration():
        for row_idx, seat in enumerate(leaves):
            coords = _coordinates_for_seat_deterministic(seat, registry)
            for c in coords:
                if c[1] != 0:
                    unscorable.append((seat.memory_id, c))
            fingerprint = _fingerprint_for_id(seat.memory_id)
            row = pack_entry(fingerprint, timestamp_epoch_or_zero(seat.temporal_timestamp),
                             seat.source_id or "gate:memory", coords)
            rows.append(row)
            meta = seat.to_dict()
            meta["row_index"] = row_idx
            row_index.append(meta)
            seats.append(DMESeat(memory_id=seat.memory_id, coord=seat.coord, title=seat.title, summary=seat.summary, domain=seat.domain,
                                 subtopic=seat.subtopic, tags=seat.tags, sense_ids=seat.sense_ids, status=seat.status, source_id=seat.source_id,
                                 source_content_hash=seat.source_content_hash, source_session_id=seat.source_session_id,
                                 source_session_timestamp=seat.source_session_timestamp,
                                 temporal_timestamp=seat.temporal_timestamp, retina_text=seat.retina_text, row_index=row_idx))
    sram = np.vstack(rows) if rows else np.zeros((0, ENTRY_WORDS), dtype=np.uint64)
    index = DMEIndex(snapshot_hash=snap_hash, seats=tuple(seats), sram=sram, row_index=row_index, built_at="", backend="")
    validate_index_geometry(index)
    planes = build_hop_planes(None, index.sram, index.row_index, graph_path=output_dir / "graph.sqlite")
    if planes.strong is None or planes.weak is None:
        raise DmeGeometryError("DME generation requires both hop weight planes")
    validate_weight_entries(planes.strong, label="strong hop weight", expected_rows=index.seat_count)
    validate_weight_entries(planes.weak, label="weak hop weight", expected_rows=index.seat_count)
    # deterministic artifacts (D-13-21): no built_at, no backend, generation-relative paths only
    atomic_replace_bytes(output_dir / "sram.bin", index.sram.tobytes())
    (output_dir / "planes").mkdir(exist_ok=True)
    atomic_replace_bytes(output_dir / GENERATION_PLANE_PATHS["hop_plane_strong"], planes.strong.tobytes())
    atomic_replace_bytes(output_dir / GENERATION_PLANE_PATHS["hop_plane_weak"], planes.weak.tobytes())
    semantic_retina = _build_generation_retina(seats, output_dir, prior=retina_prior)
    meta_doc = {"type": "dme_sram_meta", "snapshot_hash": snap_hash, "seat_count": index.seat_count, "bits": ENTRY_WORDS * 64,
                "schema_version": DME_SCHEMA_VERSION, "geometry": scoring_geometry_receipt()}
    atomic_replace_bytes(output_dir / "sram_meta.json", canonical_json(meta_doc).encode("utf-8"))
    atomic_replace_bytes(output_dir / "sram_index.json", canonical_json(index.row_index).encode("utf-8"))
    atomic_replace_bytes(output_dir / "dme_manifest.json", canonical_json({
        "type": "dme_manifest", "snapshot_hash": snap_hash, "seat_count": index.seat_count, "sram_path": "sram.bin",
        "schema_version": DME_SCHEMA_VERSION, "geometry": scoring_geometry_receipt(),
        "planes": {k: v for k, v in sorted(GENERATION_PLANE_PATHS.items())},
    }).encode("utf-8"))
    # page-0 telemetry recomputed from the finalized registry (14 §7.3)
    counters = dict(getattr(registry, "_counters", {}) or {})
    used = {str(lobe): min(int(counters.get(lobe, 0)), PAGE0_SLOTS) for lobe in range(12)}
    capacity = {str(lobe): int(PAGE0_SLOTS) for lobe in range(12)}
    utilization = {str(lobe): (used[str(lobe)] / PAGE0_SLOTS if PAGE0_SLOTS else 0.0) for lobe in range(12)}
    overflow = sum(max(0, int(counters.get(lobe, 0)) - PAGE0_SLOTS) for lobe in range(12))
    return GenerationRebuildReport(index=index, page0_capacity_by_lobe=capacity, page0_used_by_lobe=used,
                                   page0_utilization_by_lobe=utilization, page0_overflow_attempts=overflow,
                                   unscorable_coordinate_count=len(unscorable), unscorable=tuple(unscorable),
                                   registry_sha256=sha256_bytes(registry_out.read_bytes()), semantic_retina=semantic_retina)


def _write_index(index: DMEIndex, *, root: Path | str | None = None) -> None:
    from memory.dme.recall_hook import force_unload_all_sessions

    # Validate and fully construct scoring weights before disturbing the
    # currently resident generation. A malformed publication fails loudly and
    # leaves both the hot seat and its on-disk pack untouched.
    validate_index_geometry(index)
    from memory.dme.hop_engine import (
        build_hop_planes,
        hop_plane_strong_path,
        hop_plane_weak_path,
    )

    planes = build_hop_planes(root, index.sram, index.row_index)
    if planes.strong is None or planes.weak is None:
        raise DmeGeometryError("DME publication requires both hop weight planes")
    validate_weight_entries(
        planes.strong,
        label="strong hop weight",
        expected_rows=index.seat_count,
    )
    validate_weight_entries(
        planes.weak,
        label="weak hop weight",
        expected_rows=index.seat_count,
    )

    # On-disk rebuild must never leave a stale resident session.
    force_unload_all_sessions()
    out_dir = dme_root(root)
    out_dir.mkdir(parents=True, exist_ok=True)

    sram_path = dme_sram_path(root)
    tmp = sram_path.with_suffix(".tmp")
    tmp.write_bytes(index.sram.tobytes())
    _atomic_replace(tmp, sram_path)

    # Hop planes share the validated generation and exact row/bit dimensions.
    for path, plane in (
        (hop_plane_strong_path(root), planes.strong),
        (hop_plane_weak_path(root), planes.weak),
    ):
        tmp_plane = path.with_suffix(".tmp")
        tmp_plane.write_bytes(plane.tobytes())
        _atomic_replace(tmp_plane, path)

    # ``root=`` is the shipping legacy projection owner while the Cut 13 flags
    # are disabled.  It must publish the same perception artifact as the
    # generation builder; otherwise boot can seat Ettin successfully but every
    # live recall still sees a permanently cold retina.
    from memory import retina

    mapped = retina.legacy_view()
    prior = mapped if mapped is not None and mapped.directory == out_dir else None
    _build_generation_retina(index.seats, out_dir, prior=prior, activate=True)

    dme_meta_path(root).write_text(canonical_json(index.to_dict()), encoding="utf-8")
    dme_row_index_path(root).write_text(
        canonical_json(index.row_index),
        encoding="utf-8",
    )
    dme_manifest_path(root).write_text(
        canonical_json(
            {
                "type": "dme_manifest",
                "snapshot_hash": index.snapshot_hash,
                "seat_count": index.seat_count,
                "sram_path": "memory/dme/sram.bin",
                "schema_version": DME_SCHEMA_VERSION,
                "backend": index.backend,
                "geometry": scoring_geometry_receipt(),
            }
        ),
        encoding="utf-8",
    )


def _tokens(text: str) -> set[str]:
    # stopwords never mint coordinates: a "the" bit carried by every memory
    # makes the whole lobe union-match any query (Gate-1 fabrication vector).
    # Coordinates are STEMMED (ratified verb/action normalization) so
    # inflection never halves a match ratio — "graduate" in a question and
    # "graduated" in a memory are the same coordinate. Symmetric on seats
    # and queries — one vocabulary.
    from memory.taxonomy.canonicalize import CONTENT_STOPWORDS, _stem

    out: set[str] = set()
    for token in normalize_id_field(str(text or "")).split():
        if len(token) < 2 or token in CONTENT_STOPWORDS:
            continue
        token = _stem(token)
        if len(token) >= 2:
            out.add(token)
    return out


def _coord_tuple(raw: Any) -> tuple[int, ...]:
    out: list[int] = []
    for item in raw or ():
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            return ()
    return tuple(out)


def _passes_filters(
    coord: tuple[int, ...],
    allowed_lobes: set[int] | None,
    denied_lobes: set[int],
    allowed_subdomains: set[int] | None,
) -> bool:
    if len(coord) < 2:
        return False
    x = coord[0]
    y = coord[1]
    if x in denied_lobes:
        return False
    if allowed_lobes is not None and x not in allowed_lobes:
        return False
    return allowed_subdomains is None or y in allowed_subdomains


def _read_snapshot_hash(root: Path | str | None) -> str:
    # Cut 13 (D-13-2/19): under generation mode the hash is the (ambient) manifest's
    # graph_snapshot_hash — the fixed-name marker is legacy only.
    from memory.projection.generation import active_graph_snapshot_hash

    published = active_graph_snapshot_hash(root)
    if published is not None:
        return published
    path = taxonomy_paths.compiled_root(root) / "latest_snapshot_hash.txt"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return "uncompiled"


def _memory_root(root: Path | str | None = None) -> Path:
    if root is not None:
        base = Path(root)
        return base if base.name == "memory" else base / "memory"
    from core.paths import MEMORY_ROOT

    return MEMORY_ROOT
