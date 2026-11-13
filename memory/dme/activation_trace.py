"""DMEActivationTrace — observation-only receipt of what the DME scorer activated.

Cut 1 of the memory-organ package (docs/engineering/memory-organ/). The trace
records candidate-level DME evidence for one recall execution: which rows the
Thought Engine surfaced, through which plane / evidence class, at what score,
against which projection generation. It exists so later cuts (RecallBasis,
verified-use credit, exact attribution) have a truthful execution receipt to
consume — it is NOT canonical memory, NOT a graph, and NOT a credit ledger.

Two contracts are frozen here and must not drift (14 §3, §10):

  * OR hop planes destroy contributor identity. A hop hit can name its row,
    plane and score but never which neighbour/edge produced it. Cut 1 therefore
    marks every hop candidate ``attribution_status="diagnostic"`` and emits
    ``associations=()``. Nothing here may be read as association evidence.
  * The trace carries ``cue_receipt_id`` (Cut 2) and each candidate carries a
    nullable ``recall_basis_id`` (Cut 3). Both are ``None`` in Cut 1.

The trace stores a query HASH, never the raw query text. ``trace_id`` is
derived from the stable trace fields only — no wall clock — so identical
executions yield identical ids.

No file I/O lives in this module.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from memory.taxonomy.normalize import canonical_json

DME_ACTIVATION_TRACE_SCHEMA_VERSION = "omega.dme_activation_trace.v1"
DME_ACTIVATION_TRACE_ASSOCIATION_SCHEMA_VERSION = "omega.dme_activation_trace.v2"

# 14 §3.4 frozen enum. Only "observed" / "proven" may ever receive durable
# association credit; Cut 1 emits nothing but "diagnostic" (hops) or None.
ATTRIBUTION_STATUSES = frozenset({"observed", "proven", "ambiguous", "diagnostic"})

# Cut-1 frozen evidence-class -> plane mapping. Direct/union score against the
# leaf's own bitmap (plane0); hop classes score against the OR'd neighbour
# planes built by hop_engine.build_hop_plane. Unknown classes stay observable
# under plane "unknown" but are never credit-worthy.
EVIDENCE_PLANE = {
    "direct": "plane0",
    "union": "plane0",
    "strong_hop1": "structural_strong",
    "strong_hop2": "structural_strong",
    "weak_hop1": "structural_weak",
    "weak_hop2": "structural_weak",
    "learned_strong": "learned_strong",
    "learned_medium": "learned_medium",
    "learned_weak": "learned_weak",
}
UNKNOWN_PLANE = "unknown"


def plane_for_evidence_class(evidence_class: str) -> str:
    return EVIDENCE_PLANE.get(str(evidence_class or ""), UNKNOWN_PLANE)


def attribution_for_evidence_class(evidence_class: str) -> str | None:
    """Direct/union → None (association attribution not applicable); any hop
    class → "diagnostic" (OR plane, contributor unknown); unknown → None."""
    cls = str(evidence_class or "")
    if cls in ("direct", "union"):
        return None
    if cls in EVIDENCE_PLANE:
        return "diagnostic"
    return None


def trace_enabled() -> bool:
    return os.environ.get("OMEGA_DME_ACTIVATION_TRACE", "1").strip().lower() not in {
        "0",
        "false",
        "off",
        "no",
    }


def query_hash(query_text: str) -> str:
    return hashlib.sha256(str(query_text or "").encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ActivatedCandidate:
    memory_id: str
    row_index: int
    score: float
    evidence_class: str
    plane_id: str
    hop_depth: int
    match_count: int
    query_count: int
    recall_basis_id: str | None = None
    attribution_status: str | None = None

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.memory_id:
            errors.append("missing memory_id")
        if self.attribution_status is not None and self.attribution_status not in ATTRIBUTION_STATUSES:
            errors.append(f"invalid attribution_status={self.attribution_status!r}")
        if self.hop_depth > 0 and self.attribution_status != "diagnostic":
            errors.append("Cut 1 hop candidates must be diagnostic-only")
        return errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "row_index": self.row_index,
            "score": self.score,
            "evidence_class": self.evidence_class,
            "plane_id": self.plane_id,
            "hop_depth": self.hop_depth,
            "match_count": self.match_count,
            "query_count": self.query_count,
            "recall_basis_id": self.recall_basis_id,
            "attribution_status": self.attribution_status,
        }


@dataclass(frozen=True)
class DMEActivationTrace:
    trace_id: str
    cycle_id: str
    episode_id: str
    query_hash: str
    projection_generation: str
    backend: str
    cue_receipt_id: str | None
    query_coordinates: tuple[tuple[int, int, int], ...]
    candidates: tuple[ActivatedCandidate, ...]
    associations: tuple[dict[str, Any], ...] = ()
    schema_version: str = DME_ACTIVATION_TRACE_SCHEMA_VERSION

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.query_hash:
            errors.append("missing query_hash")
        if self.associations and self.schema_version != DME_ACTIVATION_TRACE_ASSOCIATION_SCHEMA_VERSION:
            errors.append("association-bearing traces require schema v2")
        for association in self.associations:
            if not isinstance(association, dict):
                errors.append("association is not an object")
                continue
            for key in ("association_id", "source_id", "target_id", "relation_type"):
                if not str(association.get(key) or "").strip():
                    errors.append(f"association missing {key}")
            if association.get("attribution_status") != "proven":
                errors.append("only proven associations may enter a v2 trace")
        for cand in self.candidates:
            errors.extend(f"{cand.memory_id}: {e}" for e in cand.validate())
        return errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "dme_activation_trace",
            "trace_id": self.trace_id,
            "cycle_id": self.cycle_id,
            "episode_id": self.episode_id,
            "query_hash": self.query_hash,
            "projection_generation": self.projection_generation,
            "backend": self.backend,
            "cue_receipt_id": self.cue_receipt_id,
            "query_coordinates": [list(c) for c in self.query_coordinates],
            "candidates": [c.to_dict() for c in self.candidates],
            "associations": list(self.associations),
            "schema_version": self.schema_version,
        }


def _identity_payload(
    *,
    cycle_id: str,
    episode_id: str,
    query_hash_value: str,
    projection_generation: str,
    backend: str,
    cue_receipt_id: str | None,
    query_coordinates: tuple[tuple[int, int, int], ...],
    candidates: tuple[ActivatedCandidate, ...],
    associations: tuple[dict[str, Any], ...],
    schema_version: str = DME_ACTIVATION_TRACE_SCHEMA_VERSION,
) -> dict[str, Any]:
    # every stable trace field except trace_id itself; canonical_json sorts keys
    return {
        "schema_version": schema_version,
        "cycle_id": cycle_id,
        "episode_id": episode_id,
        "query_hash": query_hash_value,
        "projection_generation": projection_generation,
        "backend": backend,
        "cue_receipt_id": cue_receipt_id,
        "query_coordinates": [list(c) for c in query_coordinates],
        "candidates": [c.to_dict() for c in candidates],
        "associations": list(associations),
    }


def build_activation_trace(
    *,
    cycle_id: str,
    episode_id: str,
    query_text: str,
    projection_generation: str,
    backend: str,
    query_coordinates: Iterable[Iterable[int]],
    candidates: Iterable[ActivatedCandidate],
    cue_receipt_id: str | None = None,
    associations: Iterable[Mapping[str, Any]] = (),
) -> DMEActivationTrace:
    coords = tuple(tuple(int(v) for v in c) for c in query_coordinates)
    cands = tuple(candidates)
    association_rows = tuple(dict(row) for row in associations)
    schema_version = (
        DME_ACTIVATION_TRACE_ASSOCIATION_SCHEMA_VERSION
        if association_rows
        else DME_ACTIVATION_TRACE_SCHEMA_VERSION
    )
    qh = query_hash(query_text)
    payload = _identity_payload(
        cycle_id=str(cycle_id or ""),
        episode_id=str(episode_id or ""),
        query_hash_value=qh,
        projection_generation=str(projection_generation or ""),
        backend=str(backend or ""),
        cue_receipt_id=cue_receipt_id,
        query_coordinates=coords,
        candidates=cands,
        associations=association_rows,
        schema_version=schema_version,
    )
    trace_id = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return DMEActivationTrace(
        trace_id=trace_id,
        cycle_id=str(cycle_id or ""),
        episode_id=str(episode_id or ""),
        query_hash=qh,
        projection_generation=str(projection_generation or ""),
        backend=str(backend or ""),
        cue_receipt_id=cue_receipt_id,
        query_coordinates=coords,
        candidates=cands,
        associations=association_rows,
        schema_version=schema_version,
    )


def candidate_from_thought_hit(hit: Any, *, recall_basis_id: str | None = None) -> ActivatedCandidate:
    """Shape one hop_engine.ThoughtHit into an ActivatedCandidate.

    Reads only what the scorer already emitted (row, class, hop, counts, score);
    plane and attribution follow from the frozen Cut-1 mapping. Cut 3 supplies
    ``recall_basis_id`` when a RecallBasis was derived for the hit — routing
    proof, which is compatible with ``attribution_status="diagnostic"``: a
    route exists; causal edge attribution for plasticity does not."""
    evidence_class = str(getattr(hit, "edge_class", "") or "")
    return ActivatedCandidate(
        memory_id=str(getattr(hit, "memory_id", "") or ""),
        row_index=int(getattr(hit, "row_index", -1)),
        score=float(getattr(hit, "score", 0.0) or 0.0),
        evidence_class=evidence_class,
        plane_id=plane_for_evidence_class(evidence_class),
        hop_depth=int(getattr(hit, "hop", 0) or 0),
        match_count=int(getattr(hit, "match_count", 0) or 0),
        query_count=int(getattr(hit, "query_count", 0) or 0),
        recall_basis_id=recall_basis_id,
        attribution_status=attribution_for_evidence_class(evidence_class),
    )
