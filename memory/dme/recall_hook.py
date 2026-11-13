"""Recall-path DME hook: load packs once, run merge, retain the hot seat.

DME SRAM / hop planes stay resident by default after the first recall.
Every top-level recall acquires a refcounted session and runs candidate merge
against the loaded packs. Nested acquire (merge → recall_candidates) shares
the same resident state.

Set ``OMEGA_MEMORY_DME_RESIDENT_TTL_SECONDS`` to a finite value or zero to
limit/disable idle residency. Fingerprint drift, rebuild, and
``force_unload_all_sessions`` still invalidate safely.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterator

from memory.dme.geometry import DME_SCORING_GEOMETRY

log = logging.getLogger("omega.memory.dme.recall_hook")


class DmeLoadError(RuntimeError):
    """DME packs missing/corrupt and could not be rebuilt.

    Raised by the load path; ``read_memory`` / MCP recall catch this and
    fail-open (continue without the DME lane), matching Omega soft-degrade.
    """


@dataclass
class DmeSessionState:
    """Resident DME packs for the duration of one (possibly nested) recall."""

    root_key: str
    index: Any
    planes: Any
    snapshot_hash: str = ""
    artifact_fingerprint: tuple = ()
    geometry_compatible: bool = True
    query_dimension: int = DME_SCORING_GEOMETRY.query_dimension
    weight_dimension: int = DME_SCORING_GEOMETRY.weight_dimension
    uses_embeddings: bool = DME_SCORING_GEOMETRY.uses_embeddings
    onnx_session_ready: bool = False
    session_providers: tuple[str, ...] = ()
    generation_id: str | None = None   # Cut 13: the ONE generation this session's packs came from (None = legacy)
    learned_planes: Any | None = None
    learned_records: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def seat_count(self) -> int:
        return int(getattr(self.index, "seat_count", 0) or 0)

    @property
    def backend(self) -> str:
        return str(getattr(self.index, "backend", "") or "")


@dataclass
class _SessionEntry:
    refcount: int = 0
    state: DmeSessionState | None = None
    stale: bool = False
    # Soft-resident idle deadline (monotonic seconds). None = no TTL hold.
    expires_at: float | None = None


@dataclass
class DmeRecallHookStats:
    """Diagnostics from the most recent apply_dme_recall_hook call on this thread."""

    dme_ran: bool = False
    dme_seat_count: int = 0
    dme_backend: str = ""
    dme_snapshot_hash: str = ""
    dme_loaded: bool = False
    dme_unloaded: bool = False
    dme_resident_after: bool = False
    dme_hit_count: int = 0
    dme_cache_hit: bool = False
    dme_geometry_compatible: bool = False
    dme_query_dimension: int = DME_SCORING_GEOMETRY.query_dimension
    dme_weight_dimension: int = DME_SCORING_GEOMETRY.weight_dimension
    dme_uses_embeddings: bool = DME_SCORING_GEOMETRY.uses_embeddings
    dme_onnx_session_ready: bool = False
    dme_session_providers: tuple[str, ...] = ()
    error: str = ""
    # Cut 1 (memory/dme/activation_trace.py): observation-only receipt of the
    # DME execution behind this merge. Metadata only — never a candidate input.
    dme_activation_trace: dict[str, Any] | None = None
    # Cut 3 (memory/recall_basis.py): RecallBasis records derived inside the
    # exact DME generation for this merge. Routing proofs the verifier will
    # independently revalidate — never trusted, never source proof.
    dme_recall_bases: tuple[dict[str, Any], ...] = ()
    dme_truncation_receipts: tuple[dict[str, Any], ...] = ()
    # Cut 3 rework 2: ephemeral binding of what THIS run actually executed
    # (query hash, cue receipt id, coordinates passed to search_thought,
    # generation). Always emitted when DME ran — independent of the
    # diagnostic trace flag. Not canonical; authorizes no credit.
    dme_execution_binding: dict[str, Any] | None = None

    def to_meta(self) -> dict[str, Any]:
        return {
            "dme_ran": self.dme_ran,
            "dme_seat_count": self.dme_seat_count,
            "dme_backend": self.dme_backend,
            "dme_snapshot_hash": self.dme_snapshot_hash,
            "dme_loaded": self.dme_loaded,
            "dme_unloaded": self.dme_unloaded,
            "dme_resident_after": self.dme_resident_after,
            "dme_hit_count": self.dme_hit_count,
            "dme_cache_hit": self.dme_cache_hit,
            "dme_geometry_compatible": self.dme_geometry_compatible,
            "dme_query_dimension": self.dme_query_dimension,
            "dme_weight_dimension": self.dme_weight_dimension,
            "dme_uses_embeddings": self.dme_uses_embeddings,
            "dme_onnx_session_ready": self.dme_onnx_session_ready,
            "dme_session_providers": list(self.dme_session_providers),
            **({"dme_error": self.error} if self.error else {}),
            **(
                {"dme_activation_trace": self.dme_activation_trace}
                if self.dme_activation_trace is not None
                else {}
            ),
            **({"recall_bases": list(self.dme_recall_bases)} if self.dme_recall_bases else {}),
            **(
                {"dme_truncation_receipts": list(self.dme_truncation_receipts)}
                if self.dme_truncation_receipts
                else {}
            ),
            **(
                {"dme_execution_binding": self.dme_execution_binding}
                if self.dme_execution_binding is not None
                else {}
            ),
        }


_lock = threading.RLock()
_sessions: dict[str, _SessionEntry] = {}
_tls = threading.local()


def _root_key(root: Path | str | None) -> str:
    """Canonicalize so ``profile`` and ``profile/memory`` share one session."""
    if root is None:
        return ""
    from memory.dme.adapter import _memory_root

    base = _memory_root(root)
    try:
        return str(base.resolve())
    except OSError:
        return str(base)


def _learned_artifact_fingerprint() -> tuple[str, ...]:
    from memory.dme.learned_planes import learned_planes_enabled

    if not learned_planes_enabled():
        return ()
    from memory.association_strength import association_generation

    return (association_generation(),)


def resident_ttl_seconds() -> float:
    """Idle residency TTL. Default ``inf`` keeps MatMul seats hot until rebuild."""
    raw = os.environ.get(
        "OMEGA_MEMORY_DME_RESIDENT_TTL_SECONDS", "inf"
    ).strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return float("inf")


def is_dme_resident(root: Path | str | None = None) -> bool:
    """True when packs for ``root`` are held (active session or soft TTL cache)."""
    key = _root_key(root)
    now = time.monotonic()
    with _lock:
        entry = _sessions.get(key)
        if not entry or entry.state is None:
            return False
        if entry.refcount > 0:
            return True
        return (
            entry.expires_at is not None
            and now < entry.expires_at
            and not entry.stale
        )


def dme_session_refcount(root: Path | str | None = None) -> int:
    key = _root_key(root)
    with _lock:
        entry = _sessions.get(key)
        return int(entry.refcount) if entry else 0


def get_active_dme_session(root: Path | str | None = None) -> DmeSessionState | None:
    key = _root_key(root)
    with _lock:
        entry = _sessions.get(key)
        if entry and entry.refcount > 0:
            return entry.state
        return None


def resident_snapshot_hash(root: Path | str | None = None) -> str:
    """The snapshot hash of the packs currently held for ``root``, or "".

    Phase 4 cut 12: an episode pins its DME read surface at admission. It must
    NOT load packs to do so — capturing an epoch is bookkeeping, and forcing a
    pack load at every episode open would turn an observation into a cost. So
    the pin reads what is already resident (active session or soft-TTL hold)
    and reports an unpinnable DME surface honestly when nothing is.

    Deliberately not ``get_active_dme_session``: that one answers "is a recall
    in flight", which is never true at admission. A soft-resident hold is the
    pack set the episode's first recall will actually be served, so it is the
    truthful thing to pin against.
    """
    key = _root_key(root)
    with _lock:
        entry = _sessions.get(key)
        if not entry or entry.state is None or entry.stale:
            return ""
        return str(entry.state.snapshot_hash or "")


def last_recall_hook_stats() -> DmeRecallHookStats:
    stats = getattr(_tls, "last_stats", None)
    return stats if isinstance(stats, DmeRecallHookStats) else DmeRecallHookStats()


def record_recall_hook_stats(stats: DmeRecallHookStats) -> None:
    """Publish hook diagnostics for the current thread (fail-open skip path)."""
    _tls.last_stats = stats


def force_unload_all_sessions() -> None:
    """Invalidate resident sessions after an on-disk rebuild.

    Idle sessions (refcount==0), including soft TTL holds, are dropped
    immediately. In-flight sessions are marked ``stale`` but keep their
    current packs until the outermost holder releases — callers must not
    observe ``index is None`` mid-yield. The next acquire reloads from disk.
    """
    with _lock:
        for key, entry in list(_sessions.items()):
            if entry.refcount > 0:
                entry.stale = True
                entry.expires_at = None
                continue
            _unload_entry(entry)
            _sessions.pop(key, None)


def _load_state(root: Path | str | None) -> DmeSessionState:
    from memory.dme.adapter import (
        _artifact_fingerprint,
        _read_snapshot_hash,
        load_sram,
    )
    from memory.dme.hop_engine import (
        ensure_backend_loaded,
        load_or_build_hop_planes,
    )

    from memory.projection.generation import GenerationIntegrityError

    learned_planes = None
    learned_records: dict[str, dict[str, Any]] = {}
    learned_generation = ""
    try:
        index = load_sram(root)
        planes = load_or_build_hop_planes(root, index.sram, index.row_index)
        from memory.dme.learned_planes import build_learned_planes, learned_planes_enabled

        if learned_planes_enabled():
            from memory.association_strength import AssociationStrengthIndex, association_generation

            learned_index = AssociationStrengthIndex()
            learned_records = learned_index.snapshot_records()
            learned_generation = association_generation()
            from memory.projection.generation import current_view

            view = current_view()
            expected_generation = (
                str(view.manifest.content.association_generation or "") if view is not None else ""
            )
            if expected_generation and expected_generation != learned_generation:
                raise GenerationIntegrityError(
                    "learned association generation differs from the active projection"
                )
            learned_planes = build_learned_planes(
                index.sram,
                index.row_index,
                learned_records,
                association_generation=learned_generation,
            )
        backend_receipt = ensure_backend_loaded()
        index = replace(index, backend=str(backend_receipt["backend"]))
    except GenerationIntegrityError:
        raise  # Cut 13 (D-13-3): an unavailable generation is loud — never wrapped into a soft load error
    except Exception as exc:
        raise DmeLoadError(
            f"DME packs failed to load for root={root!r}: {type(exc).__name__}: {exc}"
        ) from exc

    if index.seat_count == 0:
        # Empty graph is valid; empty seats after a non-empty graph would
        # already have been rebuilt by load_sram. Stay fail-open on empty.
        log.info("dme.recall_hook: loaded empty DME index (seat_count=0) root=%s", root)

    from memory.projection.generation import current_view

    view = current_view()
    return DmeSessionState(
        root_key=_root_key(root),
        index=index,
        planes=planes,
        snapshot_hash=str(index.snapshot_hash or _read_snapshot_hash(root)),
        artifact_fingerprint=tuple(_artifact_fingerprint(root)) + _learned_artifact_fingerprint(),
        generation_id=(view.generation_id if view is not None else None),
        geometry_compatible=bool(backend_receipt["compatible"]),
        query_dimension=int(backend_receipt["query_dimension"]),
        weight_dimension=int(backend_receipt["weight_dimension"]),
        uses_embeddings=bool(backend_receipt["uses_embeddings"]),
        onnx_session_ready=bool(backend_receipt["onnx_session_ready"]),
        session_providers=tuple(
            str(provider) for provider in backend_receipt["providers"]
        ),
        learned_planes=learned_planes,
        learned_records=learned_records,
    )


def _unload_entry(entry: _SessionEntry) -> None:
    if entry.state is not None:
        # Drop large ndarray references so GC can reclaim promptly.
        entry.state.index = None
        entry.state.planes = None
    entry.state = None
    entry.refcount = 0
    entry.expires_at = None
    entry.stale = False


def _soft_resident_usable(entry: _SessionEntry, root: Path | str | None) -> bool:
    """Whether an idle TTL-held entry can be reused without reloading."""
    if entry.state is None or entry.stale or entry.refcount > 0:
        return False
    if entry.expires_at is None or time.monotonic() >= entry.expires_at:
        return False
    from memory.dme.adapter import _artifact_fingerprint

    try:
        current = tuple(_artifact_fingerprint(root)) + _learned_artifact_fingerprint()
    except Exception:
        return False
    return current == entry.state.artifact_fingerprint


@contextmanager
def dme_recall_session(root: Path | str | None = None) -> Iterator[DmeSessionState]:
    """Acquire resident DME packs; unload (or soft-hold) when outermost exits."""
    key = _root_key(root)
    cache_hit = False
    with _lock:
        entry = _sessions.get(key)
        if entry is None:
            entry = _SessionEntry()
            _sessions[key] = entry
        elif entry.refcount == 0 and entry.state is not None:
            if _soft_resident_usable(entry, root):
                cache_hit = True
                entry.expires_at = None
            else:
                # Expired, fingerprint drift, or stale — drop before reload.
                _unload_entry(entry)
        if entry.state is None or entry.stale:
            # Reload when empty or marked stale by an on-disk rebuild.
            # In-flight outer holders keep their prior state object; this
            # acquire gets a fresh one for nested/new users.
            entry.state = _load_state(root)
            entry.stale = False
            entry.expires_at = None
            log.info(
                "dme.recall_hook: loaded seats=%s backend=%s root=%s cache_hit=%s",
                entry.state.seat_count,
                entry.state.backend,
                key or "<default>",
                cache_hit,
            )
        elif cache_hit:
            log.debug(
                "dme.recall_hook: soft-resident hit seats=%s root=%s",
                entry.state.seat_count,
                key or "<default>",
            )
        entry.refcount += 1
        state = entry.state
    assert state is not None
    # Expose cache-hit on TLS for apply_dme_recall_hook diagnostics.
    _tls.last_cache_hit = cache_hit
    # Phase 4 cut 12 — the DME half of the episode's snapshot-read pin. The
    # branch above may have RELOADED packs mid-episode (stale flag, fingerprint
    # drift, expired TTL), which silently moves the DME surface under an
    # episode that pinned the previous packs. Recorded here, outside the module
    # lock (the epoch registry has its own; never nest the two), so the
    # divergence reaches the recall receipt instead of nowhere.
    try:
        from memory import epoch as memory_epoch

        memory_epoch.observe(memory_epoch.SURFACE_DME, str(state.snapshot_hash or ""))
    except Exception:  # noqa: BLE001 — bookkeeping never breaks a recall
        log.debug("dme.recall_hook: memory-epoch observation skipped", exc_info=True)
    try:
        yield state
    finally:
        with _lock:
            entry = _sessions.get(key)
            if entry is None:
                pass
            else:
                entry.refcount = max(0, entry.refcount - 1)
                if entry.refcount == 0:
                    ttl = resident_ttl_seconds()
                    if ttl > 0 and entry.state is not None and not entry.stale:
                        entry.expires_at = time.monotonic() + ttl
                        log.info(
                            "dme.recall_hook: soft-resident hold ttl=%.3fs seats=%s root=%s",
                            ttl,
                            getattr(state, "seat_count", 0),
                            key or "<default>",
                        )
                    else:
                        log.info(
                            "dme.recall_hook: unloaded seats_were=%s root=%s",
                            getattr(state, "seat_count", 0),
                            key or "<default>",
                        )
                        _unload_entry(entry)
                        _sessions.pop(key, None)


def _activated_candidates_from_execution(
    execution: Any, basis_ids: dict[str, str] | None = None
) -> list[Any]:
    """Shape the scorer's hits into ActivatedCandidates — only those that
    survived the lobe/subdomain filters into ``execution.candidates``. Built
    from ThoughtResult.hits (what DME actually scored), NOT from the merged
    rows: a baseline lane can outrank a DME hit while the DME hit still
    occurred, and the trace must say so. ``basis_ids`` (Cut 3) maps
    memory_id → recall_basis_id for hits that received a RecallBasis."""
    from memory.dme.activation_trace import candidate_from_thought_hit

    thought = execution.thought_result
    if thought is None:
        return []
    surviving = {
        str(row.get("memory_id") or "") for row in execution.candidates if row.get("memory_id")
    }
    basis_ids = basis_ids or {}
    return [
        candidate_from_thought_hit(
            hit, recall_basis_id=basis_ids.get(str(getattr(hit, "memory_id", "") or ""))
        )
        for hit in thought.hits
        if str(getattr(hit, "memory_id", "") or "") in surviving
    ]


def _learned_candidate_rows(
    session: Any,
    execution: Any,
    query: Any,
    *,
    truncation_receipts: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[Any]]:
    """Run the opt-in learned projection without changing structural scoring."""
    planes = getattr(session, "learned_planes", None)
    thought = getattr(execution, "thought_result", None)
    if planes is None or thought is None:
        return [], []
    from memory.dme.adapter import _coord_tuple, _passes_filters
    from memory.dme.hop_engine import ThoughtHit
    from memory.dme.learned_planes import search_learned_planes
    from memory.dewey_map import NON_RECALLABLE_LOBES

    allowed = getattr(query, "allowed_lobes", None)
    allowed_set = set(allowed) if allowed is not None else None
    denied_set = set(getattr(query, "denied_lobes", None) or ()) | set(NON_RECALLABLE_LOBES)
    allowed_subdomains = getattr(query, "allowed_subdomains", None)
    allowed_subs = set(allowed_subdomains) if allowed_subdomains is not None else None
    hits = search_learned_planes(
        session.index.sram,
        session.index.row_index,
        planes,
        list(getattr(thought, "query_coordinates", None) or ()),
        max_results=40,
        truncation_receipts=truncation_receipts,
    )
    rows: list[dict[str, Any]] = []
    thought_hits: list[Any] = []
    for hit in hits:
        meta = session.index.row_index[hit.row_index]
        coord = _coord_tuple(meta.get("coord"))
        if not _passes_filters(coord, allowed_set, denied_set, allowed_subs):
            continue
        thought_hits.append(
            ThoughtHit(
                row_index=hit.row_index,
                memory_id=hit.memory_id,
                score=hit.score,
                hop=1,
                match_count=hit.match_count,
                query_count=hit.query_count,
                title=str(meta.get("title") or ""),
                summary=str(meta.get("summary") or ""),
                edge_class=hit.edge_class,
            )
        )
        rows.append(
            {
                "id": hit.memory_id,
                "memory_id": hit.memory_id,
                "coord": list(coord),
                "score": hit.score,
                "title": str(meta.get("title") or ""),
                "summary": str(meta.get("summary") or ""),
                "domain": str(meta.get("domain") or ""),
                "subtopic": str(meta.get("subtopic") or ""),
                "tags": list(meta.get("tags") or ()),
                "provenance": {
                    "candidate_source": "dme_learned",
                    "dme_snapshot_hash": session.snapshot_hash,
                    "dme_edge_class": hit.edge_class,
                },
            }
        )
    return rows, thought_hits


def _derive_recall_bases(session: Any, execution: Any, cue_receipt: Any, root: Any = None) -> dict[str, Any]:
    """Cut 3: build RecallBasis records for surviving DME hits INSIDE the exact
    generation that scored them (session index/planes/snapshot). direct/union
    → canonical_coordinate; strong_hop1 → structural_path (Task 4). Returns
    memory_id → RecallBasis. Failure-soft: any error → {} (recall proceeds
    without routing proof; the verifier then applies pure lexical topicality)."""
    try:
        from memory.recall_basis import (
            build_coordinate_basis,
            build_learned_path_basis,
            build_structural_path_basis,
        )

        thought = execution.thought_result
        index = getattr(session, "index", None)
        if thought is None or index is None:
            return {}
        surviving = {
            str(row.get("memory_id") or "") for row in execution.candidates if row.get("memory_id")
        }
        generation = str(getattr(session, "snapshot_hash", "") or index.snapshot_hash or "")
        query_coords = list(getattr(thought, "query_coordinates", None) or ())
        bases: dict[str, Any] = {}
        for hit in thought.hits:
            mid = str(getattr(hit, "memory_id", "") or "")
            if not mid or mid in bases or mid not in surviving:
                continue
            cls = str(getattr(hit, "edge_class", "") or "")
            row_index = int(getattr(hit, "row_index", -1))
            if row_index < 0 or row_index >= len(index.sram):
                continue
            basis = None
            if cls in ("direct", "union"):
                basis = build_coordinate_basis(
                    candidate_id=mid,
                    candidate_row=index.sram[row_index],
                    query_coordinates=query_coords,
                    cue_receipt=cue_receipt,
                    projection_generation=generation,
                    graph_snapshot_hash=generation,
                )
            elif cls == "strong_hop1":
                basis = build_structural_path_basis(
                    candidate_id=mid,
                    evidence_class=cls,
                    sram=index.sram,
                    row_index=index.row_index,
                    query_coordinates=query_coords,
                    root=root,
                    projection_generation=generation,
                    graph_snapshot_hash=generation,
                    cue_receipt=cue_receipt,
                )
            elif cls.startswith("learned_"):
                basis = build_learned_path_basis(
                    candidate_id=mid,
                    sram=index.sram,
                    row_index=index.row_index,
                    query_coordinates=query_coords,
                    projection_generation=generation,
                    graph_snapshot_hash=generation,
                    cue_receipt=cue_receipt,
                )
            if basis is not None:
                bases[mid] = basis
        return bases
    except Exception as exc:  # noqa: BLE001 — routing proof is optional; recall is not
        log.warning("recall basis derivation skipped: %s", exc)
        return {}


def _annotate_recall_bases(merged: list[dict[str, Any]], bases: dict[str, Any]) -> None:
    """Attach recall_basis_id to the merged row's provenance. No score/order change."""
    if not bases:
        return
    for item in merged:
        mid = str(item.get("id") or "")
        basis = bases.get(mid)
        if basis is None:
            continue
        prov = item.get("provenance")
        if not isinstance(prov, dict):
            prov = dict(prov or {})
            item["provenance"] = prov
        prov["recall_basis_id"] = basis.basis_id


def _proven_association_rows(bases: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    """Expose only sidecar associations named by a validated recall basis.

    A structural path without association IDs remains diagnostic. Missing or
    inactive sidecar records are omitted, never invented from edge adjacency.
    """
    association_ids = {
        str(association_id or "")
        for basis in bases.values()
        if getattr(basis, "kind", "") != "learned_path"
        for association_id in getattr(basis, "association_ids", ())
        if str(association_id or "")
    }
    if not association_ids:
        return ()
    from memory.association_strength import AssociationStrengthIndex, STATUS_ACTIVE

    index = AssociationStrengthIndex()
    rows: list[dict[str, Any]] = []
    for association_id in sorted(association_ids):
        record = index.get_record(association_id)
        if record is None or record.get("status") != STATUS_ACTIVE:
            continue
        rows.append(
            {
                "association_id": association_id,
                "source_id": str(record.get("source_id") or ""),
                "target_id": str(record.get("target_id") or ""),
                "relation_type": str(record.get("relation_type") or ""),
                "provenance_class": str(record.get("provenance_class") or ""),
                "activation_strength": float(index.get_effective_strength(association_id)),
                "structural_strength": float(record.get("structural_strength") or 0.0),
                "experiential_strength": float(record.get("experiential_strength") or 0.0),
                "attribution_status": "proven",
            }
        )
    return tuple(rows)


def _attach_activation_trace(
    stats: DmeRecallHookStats,
    query: Any,
    execution: Any,
    cue_receipt: Any | None = None,
    basis_ids: dict[str, str] | None = None,
    basis_records: dict[str, Any] | None = None,
) -> None:
    """Build the Cut-1 DMEActivationTrace and hang it on ``stats``.

    Failure-soft by contract: trace construction is diagnostic degradation
    only. It never fails recall, alters merged candidates, alters
    ``stats.dme_ran`` or switches backend. Cut 2: the trace names the cue
    receipt by ``receipt_id`` only — cue lists are never duplicated into it."""
    try:
        from memory.dme.activation_trace import build_activation_trace, trace_enabled

        if not trace_enabled() or execution.thought_result is None:
            return
        cue_id = str(getattr(cue_receipt, "receipt_id", "") or "") or None
        stats.dme_activation_trace = build_activation_trace(
            cycle_id=str(getattr(query, "cycle_id", "") or ""),
            episode_id=str(getattr(query, "episode_id", "") or ""),
            query_text=str(getattr(query, "text", "") or ""),
            projection_generation=execution.snapshot_hash,
            backend=execution.backend,
            query_coordinates=list(execution.thought_result.query_coordinates),
            candidates=_activated_candidates_from_execution(execution, basis_ids),
            cue_receipt_id=cue_id,
            associations=_proven_association_rows(basis_records or {}),
        ).to_dict()
    except Exception as exc:
        log.warning("dme activation trace skipped: %s", exc)


def apply_dme_recall_hook(
    query: Any,
    candidates: list[dict[str, Any]],
    *,
    root: Path | str | None = None,
    cue_receipt: Any | None = None,
) -> tuple[list[dict[str, Any]], DmeRecallHookStats]:
    """Recall plugin: load → merge DME candidates → unload (or soft-hold).

    This is the primary attachment point for ``memory_recall`` /
    ``read_memory``. Raises ``DmeLoadError`` on load/rebuild failure; callers
    (``read_memory``) fail-open and continue without the DME lane.

    ``cue_receipt`` (Cut 2, memory.routing_cues.CueReceipt) is passed through
    to the adapter untouched; the query object is never mutated to carry it.
    """
    from memory.dme.adapter import merge_dme_candidate_dicts_observed

    stats = DmeRecallHookStats()
    before_resident = is_dme_resident(root)
    try:
        with dme_recall_session(root) as session:
            stats.dme_cache_hit = bool(getattr(_tls, "last_cache_hit", False))
            stats.dme_loaded = True
            stats.dme_seat_count = session.seat_count
            stats.dme_backend = session.backend
            stats.dme_snapshot_hash = session.snapshot_hash
            stats.dme_geometry_compatible = session.geometry_compatible
            stats.dme_query_dimension = session.query_dimension
            stats.dme_weight_dimension = session.weight_dimension
            stats.dme_uses_embeddings = session.uses_embeddings
            stats.dme_onnx_session_ready = session.onnx_session_ready
            stats.dme_session_providers = session.session_providers
            before_ids = {
                str(item.get("id") or "")
                for item in candidates
                if str(item.get("id") or "")
            }
            merged, execution = merge_dme_candidate_dicts_observed(
                query, candidates, root=root, cue_receipt=cue_receipt
            )
            learned_truncation_receipts: list[dict[str, Any]] = []
            learned_rows, learned_hits = _learned_candidate_rows(
                session,
                execution,
                query,
                truncation_receipts=learned_truncation_receipts,
            )
            if learned_rows:
                from memory.dme.adapter import DmeRecallExecution

                existing_ids = {str(item.get("id") or "") for item in merged}
                new_rows: list[dict[str, Any]] = []
                learned_hits_for_execution: list[Any] = []
                for row, hit in zip(learned_rows, learned_hits):
                    memory_id = str(row.get("id") or "")
                    if memory_id in existing_ids:
                        existing = next(item for item in merged if str(item.get("id") or "") == memory_id)
                        provenance = existing.get("provenance")
                        if not isinstance(provenance, dict):
                            provenance = {}
                            existing["provenance"] = provenance
                        provenance.update(row.get("provenance") or {})
                    else:
                        new_rows.append(row)
                        existing_ids.add(memory_id)
                    learned_hits_for_execution.append(hit)
                if new_rows:
                    merged.extend(new_rows)
                execution.thought_result.hits.extend(learned_hits_for_execution)
            from memory.dme.adapter import DmeRecallExecution

            execution = DmeRecallExecution(
                candidates=(
                    tuple(execution.candidates) + tuple(new_rows)
                    if learned_rows and new_rows
                    else tuple(execution.candidates)
                ),
                thought_result=execution.thought_result,
                snapshot_hash=execution.snapshot_hash,
                backend=execution.backend,
                truncation_receipts=(
                    *tuple(execution.truncation_receipts),
                    *tuple(learned_truncation_receipts),
                ),
            )
            stats.dme_truncation_receipts = execution.truncation_receipts
            stats.dme_ran = True
            # Cut 3 rework 2: execution binding FROM THE RUN, before anything else
            try:
                from memory.recall_basis import build_execution_binding

                stats.dme_execution_binding = build_execution_binding(
                    execution, session, cue_receipt, str(getattr(query, "text", "") or "")
                ).to_dict()
            except Exception as exc:  # noqa: BLE001 — no binding → no basis validates (fail closed)
                log.warning("dme execution binding skipped: %s", exc)
            # Cut 3: routing proofs derived in THIS generation, before the trace
            bases = _derive_recall_bases(session, execution, cue_receipt, root)
            _annotate_recall_bases(merged, bases)
            stats.dme_recall_bases = tuple(b.to_dict() for b in bases.values())
            _attach_activation_trace(
                stats, query, execution, cue_receipt,
                {mid: b.basis_id for mid, b in bases.items()},
                bases,
            )
            stats.dme_hit_count = sum(
                1
                for item in merged
                if str((item.get("provenance") or {}).get("candidate_source") or "")
                == "dme"
                or (
                    str(item.get("id") or "") not in before_ids
                    and str(item.get("id") or "")
                )
            )
            # Also count grafted DME provenance on existing ids.
            if stats.dme_hit_count == 0:
                stats.dme_hit_count = sum(
                    1
                    for item in merged
                    if "dme" in str((item.get("provenance") or {}).get("candidate_source") or "")
                    or str((item.get("provenance") or {}).get("dme_backend") or "")
                )
    except DmeLoadError as exc:
        stats.error = str(exc)
        _tls.last_stats = stats
        raise
    stats.dme_unloaded = not is_dme_resident(root)
    stats.dme_resident_after = is_dme_resident(root)
    # If we nested into an already-open outer session, resident_after may stay True.
    if before_resident and stats.dme_resident_after and resident_ttl_seconds() <= 0:
        stats.dme_unloaded = False
    _tls.last_stats = stats
    return merged, stats
