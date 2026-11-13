"""RecallBasis — verifier-recognized semantic routing proof (memory-organ Cut 3).

The sole owner of routing/topicality proofs (14 §5.2: canonical module path is
exactly ``memory/recall_basis.py``). A basis records HOW a DME candidate was
reached from the query inside the exact projection generation that scored it,
so the RecallVerifier can independently re-derive that route and let it
satisfy TOPICALITY for a candidate that shares no lexical token with the query.

What a basis is NOT — frozen contracts (12, 14, Cut 3 plan):

  * not source proof — the recalled FACT is still proven only by canonical /
    SourceLedger verification (``_verify_candidate``); a valid basis cannot
    rescue a missing/unreadable/quarantined/superseded record;
  * not a verdict — no status/verified/source_proof field exists here;
  * not association attribution (Fable B-1) — a ``structural_path`` proves a
    valid typed route exists; it never says which OR-plane edge supplied the
    score and never authorizes outcome credit. ``association_ids`` may name
    reconciled structural records for the validated path, but they remain
    routing metadata rather than causal attribution;
  * CUE != CLAIM — generated terms may appear as query-side routing metadata
    (``generated_query_terms``); they assert nothing.

Kinds: ``lexical`` (existing lexical topicality — no basis needed),
``canonical_coordinate`` (direct/union query-address overlap on the candidate's
own row), ``canonical_alias`` (durable rederivable alias/entity relation — not
generated synonyms), ``structural_path`` (strong-hop-1 typed graph path from a
query-overlapping source row), ``learned_path`` (schema-reserved, verifier-
disabled until Cut 9). Cut 3 admits only the middle three at topicality.

``basis_id`` is a canonical-JSON hash over every field except itself — no wall
clock. Stale/forged/malformed bases fail closed at the verifier.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, fields
from typing import Any, Iterable, Mapping

from memory.taxonomy.normalize import canonical_json

log = logging.getLogger("omega.memory.recall_basis")

RECALL_BASIS_SCHEMA_VERSION = "omega.recall_basis.v1"

RECALL_BASIS_KINDS = frozenset(
    {
        "lexical",
        "canonical_coordinate",
        "canonical_alias",
        "structural_path",
        "learned_path",
    }
)

# Cut 3 admits these at the topicality gate. ``lexical`` needs no basis (existing
# rule); ``learned_path`` stays disabled until Cut 9 learned planes exist.
CUT3_ADMISSIBLE_KINDS = frozenset({"canonical_coordinate", "canonical_alias", "structural_path"})

# Cut 3 structural scope: direct/union + strong hop 1 only. Strong hop 2 is too
# indirect for topicality admission; weak hops remain lexical-only.
MAX_STRUCTURAL_PATH_EDGES = 2
MAX_STRUCTURAL_MEDIATORS = 1

Address = tuple[int, int, int]


def cut3_admissible_kind(kind: str) -> bool:
    value = str(kind or "")
    if value == "learned_path":
        from memory.dme.learned_planes import learned_planes_enabled

        return learned_planes_enabled()
    return value in CUT3_ADMISSIBLE_KINDS


def recall_basis_topicality_enabled() -> bool:
    """Kill switch. Default ON: basis validation is deterministic. OFF returns
    the exact pre-Cut-3 topicality behaviour."""
    return os.environ.get("OMEGA_RECALL_BASIS_TOPICALITY", "1").strip().lower() not in {
        "0",
        "false",
        "off",
        "no",
    }


# ── schema ───────────────────────────────────────────────────────────────────


def _address_tuple(raw: Any) -> Address | None:
    try:
        seq = list(raw)
    except TypeError:
        return None
    if len(seq) != 3:
        return None
    try:
        return (int(seq[0]), int(seq[1]), int(seq[2]))
    except (TypeError, ValueError):
        return None


def _str_tuple(raw: Any) -> tuple[str, ...] | None:
    if raw is None:
        return ()
    if isinstance(raw, (str, bytes)):
        return None
    try:
        items = list(raw)
    except TypeError:
        return None
    if not all(isinstance(x, str) for x in items):
        return None
    return tuple(items)


@dataclass(frozen=True)
class RecallBasis:
    basis_id: str
    candidate_id: str
    kind: str
    query_terms: tuple[str, ...]
    query_addresses: tuple[Address, ...]
    generated_query_terms: tuple[str, ...]
    path_node_ids: tuple[str, ...]
    path_edge_ids: tuple[str, ...]
    relation_types: tuple[str, ...]
    association_ids: tuple[str, ...]
    projection_generation: str
    graph_snapshot_hash: str
    schema_version: str = RECALL_BASIS_SCHEMA_VERSION

    # ── serialization ───────────────────────────────────────────────────

    def identity_payload(self) -> dict[str, Any]:
        """Every field except basis_id, in canonical (JSON-stable) form."""
        return {
            "candidate_id": self.candidate_id,
            "kind": self.kind,
            "query_terms": list(self.query_terms),
            "query_addresses": [list(a) for a in self.query_addresses],
            "generated_query_terms": list(self.generated_query_terms),
            "path_node_ids": list(self.path_node_ids),
            "path_edge_ids": list(self.path_edge_ids),
            "relation_types": list(self.relation_types),
            "association_ids": list(self.association_ids),
            "projection_generation": self.projection_generation,
            "graph_snapshot_hash": self.graph_snapshot_hash,
            "schema_version": self.schema_version,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"type": "recall_basis", "basis_id": self.basis_id, **self.identity_payload()}

    @classmethod
    def from_dict(cls, raw: Any) -> "RecallBasis | None":
        """Parse an UNTRUSTED metadata row. Returns None when the row is not
        even shape-valid; callers must still run ``validate()``."""
        if not isinstance(raw, Mapping):
            return None
        try:
            addresses_raw = raw.get("query_addresses")
            if isinstance(addresses_raw, (str, bytes)) or addresses_raw is None:
                addresses: tuple[Address, ...] | None = None if addresses_raw is not None else ()
            else:
                parsed = [_address_tuple(a) for a in list(addresses_raw)]
                addresses = None if any(a is None for a in parsed) else tuple(a for a in parsed if a)
            if addresses is None:
                return None
            tuples = {}
            for name in ("query_terms", "generated_query_terms", "path_node_ids", "path_edge_ids", "relation_types", "association_ids"):
                value = _str_tuple(raw.get(name))
                if value is None:
                    return None
                tuples[name] = value
            kind = raw.get("kind")
            if not isinstance(kind, str):
                return None
            return cls(
                basis_id=str(raw.get("basis_id") or ""),
                candidate_id=str(raw.get("candidate_id") or ""),
                kind=kind,
                query_addresses=addresses,
                projection_generation=str(raw.get("projection_generation") or ""),
                graph_snapshot_hash=str(raw.get("graph_snapshot_hash") or ""),
                schema_version=str(raw.get("schema_version") or ""),
                **tuples,
            )
        except Exception:  # noqa: BLE001 — untrusted input; never crash the caller
            return None

    # ── validation ──────────────────────────────────────────────────────

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.schema_version != RECALL_BASIS_SCHEMA_VERSION:
            errors.append(f"unsupported schema_version={self.schema_version!r}")
        if not self.candidate_id:
            errors.append("missing candidate_id")
        if self.kind not in RECALL_BASIS_KINDS:
            errors.append(f"unknown kind={self.kind!r}")
        if not self.projection_generation:
            errors.append("missing projection_generation")
        for addr in self.query_addresses:
            if _address_tuple(addr) is None:
                errors.append(f"malformed query_addresses entry {addr!r}")
        if self.kind in ("canonical_coordinate", "canonical_alias") and not self.query_addresses:
            errors.append("coordinate/alias basis requires query_addresses")
        if self.kind == "structural_path":
            if not self.path_node_ids or not self.path_edge_ids:
                errors.append("structural_path basis requires path_node_ids and path_edge_ids")
            if len(self.path_edge_ids) > MAX_STRUCTURAL_PATH_EDGES:
                errors.append("structural_path exceeds Cut 3 edge bound")
            if len(self.relation_types) != len(self.path_edge_ids):
                errors.append("structural_path relation_types must align with path_edge_ids")
            if self.path_node_ids and self.path_node_ids[-1] != self.candidate_id:
                errors.append("structural_path must terminate at candidate_id")
        if not self.basis_id:
            errors.append("missing basis_id")
        return errors

    def identity_ok(self) -> bool:
        """True when basis_id equals the canonical hash of the other fields.
        Kept separate from structural validate(): the verifier requires it
        (a tampered receipt fails closed); hand-built diagnostic bases need not."""
        return bool(self.basis_id) and self.basis_id == basis_id_for(self.identity_payload())


def basis_id_for(payload: Mapping[str, Any]) -> str:
    """Deterministic id over every field except basis_id itself."""
    identity = {k: v for k, v in dict(payload).items() if k not in ("basis_id", "type")}
    return hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()


def make_recall_basis(
    *,
    candidate_id: str,
    kind: str,
    query_terms: Iterable[str] = (),
    query_addresses: Iterable[Iterable[int]] = (),
    generated_query_terms: Iterable[str] = (),
    path_node_ids: Iterable[str] = (),
    path_edge_ids: Iterable[str] = (),
    relation_types: Iterable[str] = (),
    association_ids: Iterable[str] = (),
    projection_generation: str,
    graph_snapshot_hash: str,
) -> RecallBasis:
    """Build a basis with its deterministic id."""
    addresses = tuple(a for a in (_address_tuple(x) for x in query_addresses) if a is not None)
    provisional = RecallBasis(
        basis_id="",
        candidate_id=str(candidate_id or ""),
        kind=str(kind or ""),
        query_terms=tuple(str(t) for t in query_terms),
        query_addresses=addresses,
        generated_query_terms=tuple(str(t) for t in generated_query_terms),
        path_node_ids=tuple(str(n) for n in path_node_ids),
        path_edge_ids=tuple(str(e) for e in path_edge_ids),
        relation_types=tuple(str(r) for r in relation_types),
        association_ids=tuple(str(a) for a in association_ids),
        projection_generation=str(projection_generation or ""),
        graph_snapshot_hash=str(graph_snapshot_hash or ""),
    )
    return RecallBasis(**{**{f.name: getattr(provisional, f.name) for f in fields(provisional)},
                         "basis_id": basis_id_for(provisional.identity_payload())})


# ── derivation: canonical_coordinate (direct / union hits) ───────────────────


def address_bit_set(row: Any, address: Address) -> bool:
    """Exact bit test: is ``address`` set in this SRAM entry? Uses the same
    lobe/page/slot → word/bit mapping the packer used to seat the row. No
    text heuristics."""
    from memory.dme.lobe_layout import addr_to_bit_position

    try:
        lobe, page, slot = int(address[0]), int(address[1]), int(address[2])
        _lobe, word_index, bit_index = addr_to_bit_position(lobe, page, slot)
        return bool(int(row[word_index]) >> bit_index & 1)
    except Exception:  # noqa: BLE001 — out-of-range page/slot etc. → not set
        return False


def _terms_for_addresses(cue_receipt: Any, addresses: Iterable[Address]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(query_terms, generated_query_terms): cue terms whose resolved address
    participates. Requires the Cut-2 receipt source map — never guesses a
    term/address correspondence."""
    wanted = {tuple(int(v) for v in a) for a in addresses}
    mapping = getattr(cue_receipt, "resolved_term_addresses", None) or ()
    terms: list[str] = []
    generated: list[str] = []
    for term, source, addr in mapping:
        if tuple(int(v) for v in addr) not in wanted:
            continue
        if term not in terms:
            terms.append(term)
        if source == "generated" and term not in generated:
            generated.append(term)
    return tuple(terms), tuple(generated)


def build_coordinate_basis(
    *,
    candidate_id: str,
    candidate_row: Any,
    query_coordinates: Iterable[Iterable[int]],
    cue_receipt: Any,
    projection_generation: str,
    graph_snapshot_hash: str,
) -> RecallBasis | None:
    """canonical_coordinate basis for a direct/union DME hit: the exact query
    addresses whose bit is set in the candidate's OWN row of the scoring
    generation. None when no query address is present in the row."""
    overlapping: list[Address] = []
    seen: set[Address] = set()
    for raw in query_coordinates or ():
        addr = _address_tuple(raw)
        if addr is None or addr in seen:
            continue
        seen.add(addr)
        if address_bit_set(candidate_row, addr):
            overlapping.append(addr)
    if not overlapping:
        return None
    terms, generated = _terms_for_addresses(cue_receipt, overlapping)
    return make_recall_basis(
        candidate_id=candidate_id,
        kind="canonical_coordinate",
        query_terms=terms,
        query_addresses=overlapping,
        generated_query_terms=generated,
        projection_generation=projection_generation,
        graph_snapshot_hash=graph_snapshot_hash,
    )


# ── derivation: structural_path (strong hop 1) ───────────────────────────────


def _query_overlap_by_row(sram: Any, query_coordinates: Iterable[Iterable[int]]) -> tuple[list[int], int]:
    """Per-row count of query bits present in each seated row (exact, via the
    same overlap path the scorer uses) and the query popcount."""
    from memory.dme.hop_engine import overlaps_payload, query_payload_popcount
    from memory.dme.packer import pack_entry

    coords = [a for a in (_address_tuple(c) for c in query_coordinates or ()) if a is not None]
    if not coords or sram is None or len(sram) == 0:
        return [], 0
    query = pack_entry(0, 0.0, "query", coords)
    required = max(1, query_payload_popcount(query))
    return [int(v) for v in overlaps_payload(sram, query)], required


def _association_ids_for_path(conn: Any, edge_ids: Iterable[str], index: Any) -> tuple[str, ...]:
    """Name only active structural associations already reconciled from graph edges."""
    from memory.association_strength import (
        STATUS_ACTIVE,
        STRUCTURAL_PROVENANCE_CLASS,
        association_id_for,
    )
    from memory.taxonomy import graph_store as G

    association_ids: list[str] = []
    for edge_id in edge_ids:
        edge = G.get_edge(conn, str(edge_id or ""))
        if edge is None:
            return ()
        association_id = association_id_for(
            str(edge.get("source_id") or ""),
            str(edge.get("edge_type") or ""),
            str(edge.get("target_id") or ""),
            STRUCTURAL_PROVENANCE_CLASS,
        )
        record = index.get_record(association_id)
        if (
            record is None
            or record.get("provenance_class") != STRUCTURAL_PROVENANCE_CLASS
            or record.get("status") != STATUS_ACTIVE
            or int(record.get("support_count") or 0) <= 0
            or float(record.get("structural_strength") or 0.0) <= 0.0
        ):
            return ()
        association_ids.append(association_id)
    return tuple(association_ids)


def build_learned_path_basis(
    *,
    candidate_id: str,
    sram: Any,
    row_index: Any,
    query_coordinates: Iterable[Iterable[int]],
    projection_generation: str,
    graph_snapshot_hash: str,
    cue_receipt: Any = None,
) -> RecallBasis | None:
    """Build an opt-in learned path proof from active sidecar endpoints."""
    from memory.association_strength import AssociationStrengthIndex, STATUS_ACTIVE, STRUCTURAL_PROVENANCE_CLASS, effective_strength
    from memory.dme.learned_planes import LearnedPlaneThresholds
    from memory.dme.hop_engine import WEAK_EDGE_TYPES

    if not cut3_admissible_kind("learned_path"):
        return None
    generation = str(projection_generation or "")
    if not generation or str(graph_snapshot_hash or "") != generation:
        return None
    rows = {
        str((meta or {}).get("memory_id") or ""): index
        for index, meta in enumerate(row_index or ())
        if str((meta or {}).get("memory_id") or "")
    }
    candidate_row = rows.get(candidate_id)
    if candidate_row is None:
        return None
    thresholds = LearnedPlaneThresholds()
    index = AssociationStrengthIndex()
    options: list[tuple[float, str, str, str, str]] = []
    for association_id, record in sorted(index.snapshot_records().items()):
        if not isinstance(record, Mapping) or record.get("status") != STATUS_ACTIVE:
            continue
        if str(record.get("provenance_class") or "") == STRUCTURAL_PROVENANCE_CLASS:
            continue
        source = str(record.get("source_id") or "")
        target = str(record.get("target_id") or "")
        relation = str(record.get("relation_type") or "")
        if target == candidate_id:
            source_id = source
        elif relation in WEAK_EDGE_TYPES and source == candidate_id:
            source_id = target
        else:
            continue
        source_row = rows.get(source_id)
        if source_row is None:
            continue
        strength = effective_strength(
            structural=float(record.get("structural_strength") or 0.0),
            experiential=float(record.get("experiential_strength") or 0.0),
        )
        if strength < thresholds.weak:
            continue
        overlapping = [
            address
            for address in (_address_tuple(raw) for raw in query_coordinates or ())
            if address is not None and address_bit_set(sram[source_row], address)
        ]
        if overlapping:
            options.append((strength, source_id, relation, association_id, source_id))
    if not options:
        return None
    options.sort(key=lambda value: (-value[0], value[1], value[3]))
    _strength, source_id, relation, _association_id, _source = options[0]
    source_row = rows[source_id]
    overlapping = [
        address
        for address in (_address_tuple(raw) for raw in query_coordinates or ())
        if address is not None and address_bit_set(sram[source_row], address)
    ]
    terms, generated = _terms_for_addresses(cue_receipt, overlapping)
    return make_recall_basis(
        candidate_id=candidate_id,
        kind="learned_path",
        query_terms=terms,
        query_addresses=overlapping,
        generated_query_terms=generated,
        path_node_ids=(source_id, candidate_id),
        path_edge_ids=(),
        relation_types=(relation,),
        association_ids=tuple(option[3] for option in options),
        projection_generation=generation,
        graph_snapshot_hash=generation,
    )


def build_structural_path_basis(
    *,
    candidate_id: str,
    evidence_class: str,
    sram: Any,
    row_index: Any,
    query_coordinates: Iterable[Iterable[int]],
    root: Any,
    projection_generation: str,
    graph_snapshot_hash: str,
    cue_receipt: Any = None,
) -> RecallBasis | None:
    """structural_path basis for a strong-hop-1 hit: a bounded, deterministic,
    typed graph route from a query-overlapping SOURCE row to the candidate,
    rederived in the exact generation that scored it.

      allowed sources    = seated rows whose OWN payload overlaps the query bits
      allowed relations  = hop_engine.STRONG_EDGE_TYPES (imported, not copied)
      search order       = 1) direct memory→memory strong edge
                           2) one mediator node with two strong edges
      winner             = fewest edges, then highest source overlap ratio,
                           then source memory id, then tuple(path_edge_ids)

    This proves a valid semantic route EXISTS. It does not prove which OR-plane
    edge supplied the score and must never set association credit (Fable B-1).
    None when out of Cut-3 scope, when generations disagree, when no source
    overlaps the query, or when only weak/over-long paths exist."""
    from memory.dme.adapter import _read_snapshot_hash
    from memory.dme.hop_engine import DEFAULT_BRIDGE_DEGREE_CAP, STRONG_EDGE_TYPES
    from memory.taxonomy import graph_store as G

    if str(evidence_class or "") != "strong_hop1":
        return None
    generation = str(projection_generation or "")
    if not generation or str(graph_snapshot_hash or "") != generation:
        return None
    # the graph we are about to walk must be the graph the planes were built from
    if str(_read_snapshot_hash(root) or "") != generation:
        return None

    overlaps, required = _query_overlap_by_row(sram, query_coordinates)
    if not overlaps:
        return None
    seated: dict[str, int] = {}
    sources: dict[str, tuple[float, int]] = {}  # memory_id → (overlap ratio, row)
    for i, meta in enumerate(row_index or ()):
        mid = str((meta or {}).get("memory_id") or "")
        if not mid:
            continue
        seated[mid] = i
        if i < len(overlaps) and overlaps[i] > 0 and mid != candidate_id:
            sources[mid] = (min(1.0, overlaps[i] / required), i)
    if not sources or candidate_id not in seated:
        return None

    try:
        conn = G.connect_for_read(root)
    except Exception as exc:  # noqa: BLE001
        log.warning("recall_basis: graph unavailable for structural path (%s)", exc)
        return None
    from memory.association_strength import AssociationStrengthIndex

    association_index = AssociationStrengthIndex()
    candidates_paths: list[
        tuple[int, float, str, tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]
    ] = []
    try:
        first_hop = G.neighbors(conn, candidate_id)
        for rel in first_hop:
            etype = str(rel.get("edge_type") or "")
            eid = str(rel.get("edge_id") or "")
            nid = str(rel.get("node_id") or "")
            if etype not in STRONG_EDGE_TYPES or not eid or not nid or nid == candidate_id:
                continue
            if nid in sources:
                # 1) direct memory→memory strong edge
                ratio, _row = sources[nid]
                candidates_paths.append(
                    (
                        1,
                        ratio,
                        nid,
                        (nid, candidate_id),
                        (eid,),
                        (etype,),
                        _association_ids_for_path(conn, (eid,), association_index),
                    )
                )
                continue
            if nid in seated:
                continue  # a seated memory that does not overlap the query is not a source
            # 2) mediator (entity/session/etc.) with two strong edges; hub-capped
            #    exactly like the hop plane (DEFAULT_BRIDGE_DEGREE_CAP)
            second_hop = G.neighbors(conn, nid)
            if DEFAULT_BRIDGE_DEGREE_CAP and len(second_hop) > DEFAULT_BRIDGE_DEGREE_CAP:
                continue
            for rel2 in second_hop:
                etype2 = str(rel2.get("edge_type") or "")
                eid2 = str(rel2.get("edge_id") or "")
                sid = str(rel2.get("node_id") or "")
                if etype2 not in STRONG_EDGE_TYPES or not eid2 or sid not in sources or sid == candidate_id:
                    continue
                ratio, _row = sources[sid]
                candidates_paths.append(
                    (
                        2,
                        ratio,
                        sid,
                        (sid, nid, candidate_id),
                        (eid2, eid),
                        (etype2, etype),
                        _association_ids_for_path(conn, (eid2, eid), association_index),
                    )
                )
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    if not candidates_paths:
        return None
    candidates_paths.sort(key=lambda p: (p[0], -p[1], p[2], p[4]))
    _edges, _ratio, source_id, nodes, edge_ids, types, association_ids = candidates_paths[0]

    # the source row's exact query overlap is the basis address set
    source_row = sram[sources[source_id][1]]
    overlapping = [
        a for a in (_address_tuple(c) for c in query_coordinates or ())
        if a is not None and address_bit_set(source_row, a)
    ]
    terms, generated = _terms_for_addresses(cue_receipt, overlapping)
    return make_recall_basis(
        candidate_id=candidate_id,
        kind="structural_path",
        query_terms=terms,
        query_addresses=overlapping,
        generated_query_terms=generated,
        path_node_ids=nodes,
        path_edge_ids=edge_ids,
        relation_types=types,
        association_ids=association_ids,
        projection_generation=generation,
        graph_snapshot_hash=generation,
    )


# ── execution binding: what the scorer ACTUALLY executed ─────────────────────
#
# Two distinct sources, kept apart on purpose (CUT3_REVIEW_REWORK_2.md):
#
#   replay   — raw query tokens + recompiled RecallPlan cues (deterministic,
#              replayable) resolved through the production resolver under the
#              EXACT two production lobe policies; the verifier's reference for
#              everything that can be re-derived;
#   binding  — DmeExecutionBinding, an ephemeral carrier created by the ACTUAL
#              DME run (DmeRecallExecution / active session): the query
#              coordinates that were really passed to search_thought. Always
#              emitted when DME ran; independent of the diagnostic trace flag;
#              never reconstructed from basis metadata or from a receipt.
#
# The executed set the verifier accepts is the binding's coordinates, and only
# after it has been validated against the replay: everything replayable must be
# present, and anything extra must be a generated-cue address that was actually
# applied (APPLY on) through the receipt the binding itself names. A forged
# receipt alone can never add an executed address; a forged binding must also
# survive query-hash / generation / registry / lobe / linkage checks and may
# still only carry extras the linked receipt's generated terms explain.


DME_EXECUTION_BINDING_SCHEMA_VERSION = "omega.dme_execution_binding.v1"


def _sha(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DmeExecutionBinding:
    """Ephemeral, runtime-local record of one DME execution. Not canonical
    memory; authorizes no plasticity credit; exists only so the verifier can
    bind a RecallBasis to what the scorer actually ran."""

    query_hash: str
    cue_receipt_id: str | None
    query_coordinates: tuple[Address, ...]
    projection_generation: str
    backend: str = ""
    schema_version: str = DME_EXECUTION_BINDING_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "dme_execution_binding",
            "query_hash": self.query_hash,
            "cue_receipt_id": self.cue_receipt_id,
            "query_coordinates": [list(a) for a in self.query_coordinates],
            "projection_generation": self.projection_generation,
            "backend": self.backend,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "DmeExecutionBinding | None":
        if not isinstance(raw, Mapping):
            return None
        try:
            coords_raw = raw.get("query_coordinates")
            if isinstance(coords_raw, (str, bytes)) or coords_raw is None:
                return None
            parsed = [_address_tuple(c) for c in list(coords_raw)]
            if any(c is None for c in parsed):
                return None
            cue = raw.get("cue_receipt_id")
            return cls(
                query_hash=str(raw.get("query_hash") or ""),
                cue_receipt_id=str(cue) if cue else None,
                query_coordinates=tuple(c for c in parsed if c is not None),
                projection_generation=str(raw.get("projection_generation") or ""),
                backend=str(raw.get("backend") or ""),
                schema_version=str(raw.get("schema_version") or ""),
            )
        except Exception:  # noqa: BLE001 — untrusted input
            return None


def build_execution_binding(execution: Any, session: Any, cue_receipt: Any, query_text: str) -> DmeExecutionBinding:
    """Create the binding FROM THE RUN: coordinates come from
    execution.thought_result.query_coordinates (what search_thought received;
    () when the scorer did not run), generation from the active session."""
    thought = getattr(execution, "thought_result", None)
    coords_raw = list(getattr(thought, "query_coordinates", None) or ()) if thought is not None else []
    coords = tuple(a for a in (_address_tuple(c) for c in coords_raw) if a is not None)
    index = getattr(session, "index", None)
    generation = str(getattr(session, "snapshot_hash", "") or getattr(index, "snapshot_hash", "") or "")
    return DmeExecutionBinding(
        query_hash=_sha(query_text),
        cue_receipt_id=(str(getattr(cue_receipt, "receipt_id", "") or "") or None),
        query_coordinates=coords,
        projection_generation=generation,
        backend=str(getattr(execution, "backend", "") or ""),
    )


class _ReceiptView:
    """Duck-typed CueReceipt over a REBUILT receipt exposing addresses_for(*sources)
    for adapter._cue_addresses — never the untrusted meta dict."""

    def __init__(self, receipt: Any):
        self._receipt = receipt

    def addresses_for(self, *sources: str) -> tuple[Address, ...]:
        return tuple(self._receipt.addresses_for(*sources))


def _policies(allowed_lobes: Iterable[int] | None, denied_lobes: Iterable[int] | None):
    """The two production lobe policies, EXACTLY as the code applies them:
      receipt (memory.api._compile_routing_cue_receipt): (denied or {6,11}) ∪ NON_RECALLABLE
      adapter (memory.dme.adapter._recall_execution_loaded): (denied or ∅) ∪ NON_RECALLABLE"""
    from memory.dewey_map import NON_RECALLABLE_LOBES

    allowed = list(allowed_lobes) if allowed_lobes is not None else None
    receipt_denied = set(int(x) for x in (denied_lobes if denied_lobes is not None else (6, 11))) | set(NON_RECALLABLE_LOBES)
    adapter_denied = set(int(x) for x in (denied_lobes or ())) | set(NON_RECALLABLE_LOBES)
    return allowed, receipt_denied, adapter_denied


def _rebuild_receipt(query_text: str, *, registry: Any, allowed, receipt_denied, query_spec: Any, generated_terms: Iterable[str] = (),
                     generator_version: str = ""):
    """Rebuild the Cut-2 receipt exactly as production compiles it (recompiled
    plan on the recall's own QuerySpec; receipt lobe policy). Generated terms
    are only what the caller passes — this function trusts nothing."""
    from types import SimpleNamespace

    from memory import routing_cues as rc
    from memory.recall_compiler import compile_recall_plan

    spec = query_spec if query_spec is not None else SimpleNamespace(
        text=query_text, cycle_id="basis-verify", task_scope=None, domain=None, modality="text", limit=10
    )
    plan = compile_recall_plan(spec)
    gen = tuple(generated_terms or ())
    return rc.build_cue_receipt(
        query_text=query_text,
        deterministic_terms=rc.deterministic_terms_from_plan(plan),
        entity_terms=rc.entity_terms_from_plan(plan),
        generated_terms=gen,
        registry=registry,
        allowed_lobes=allowed,
        denied_lobes=receipt_denied,
        generator_used=bool(gen),
        generator_version=str(generator_version or ""),
    )


def replay_query_addresses(
    query_text: str,
    *,
    root: Any = None,
    allowed_lobes: Iterable[int] | None,
    denied_lobes: Iterable[int] | None,
    query_spec: Any = None,
) -> tuple[Address, ...]:
    """Deterministic replay of the REPLAYABLE inputs: raw query tokens +
    recompiled deterministic/entity cues, through adapter._query_coordinates,
    under the exact two production lobe policies. No generated terms (they are
    not replayable), no receipt trust, no minting. Fails closed to raw-token
    resolution (a subset) if the plan cannot be recompiled."""
    from memory.dme.adapter import _query_coordinates
    from memory.dme.coordinate_registry import load_registry

    text = str(query_text or "")
    registry = load_registry(root)
    allowed, receipt_denied, adapter_denied = _policies(allowed_lobes, denied_lobes)
    try:
        rebuilt = _rebuild_receipt(text, registry=registry, allowed=allowed, receipt_denied=receipt_denied, query_spec=query_spec)
        view: Any = _ReceiptView(rebuilt)
    except Exception as exc:  # noqa: BLE001
        log.warning("recall_basis: cue replay skipped (%s); raw resolution only", exc)
        view = None
    coords = _query_coordinates(text, registry, allowed_lobes=allowed, denied_lobes=adapter_denied, cue_receipt=view)
    return tuple((int(a[0]), int(a[1]), int(a[2])) for a in coords)


def executed_query_addresses(
    query_text: str,
    *,
    root: Any = None,
    allowed_lobes: Iterable[int] | None,
    denied_lobes: Iterable[int] | None,
    cue_receipt: Any = None,  # accepted for compatibility; NEVER trusted for addresses
    query_spec: Any = None,
) -> tuple[Address, ...]:
    """Backward-compatible name for the deterministic replay. The receipt
    argument is ignored on purpose (CUT3_REVIEW_REWORK_2.md): generated
    execution is proven only through the run's DmeExecutionBinding."""
    return replay_query_addresses(query_text, root=root, allowed_lobes=allowed_lobes, denied_lobes=denied_lobes, query_spec=query_spec)


def bind_executed_addresses(
    meta: Mapping[str, Any] | None,
    *,
    query: str,
    root: Any = None,
    allowed_lobes: Iterable[int] | None,
    denied_lobes: Iterable[int] | None,
    query_spec: Any = None,
) -> tuple[tuple[Address, ...], str]:
    """Establish the executed query-address set for THIS verdict from the run's
    DmeExecutionBinding in ``meta``, validated independently. Returns
    ``(addresses, reason)``; ``((), reason)`` whenever the binding is absent or
    fails any check — nothing validates then (fail closed)."""
    from memory.dme.adapter import _read_snapshot_hash
    from memory.dme.coordinate_registry import load_registry
    from memory.dme.recall_hook import dme_recall_session
    from memory import routing_cues as rc

    if not isinstance(meta, Mapping):
        return (), "no metadata"
    binding = DmeExecutionBinding.from_dict(meta.get("dme_execution_binding"))
    if binding is None:
        return (), "no execution binding for this recall"
    if binding.schema_version != DME_EXECUTION_BINDING_SCHEMA_VERSION:
        return (), "unsupported execution binding schema"
    text = str(query or "")
    if binding.query_hash != _sha(text):
        return (), "execution binding is for a different query"
    generation = binding.projection_generation
    if not generation or str(_read_snapshot_hash(root) or "") != generation:
        return (), "execution binding generation is stale (graph snapshot moved)"
    try:
        with dme_recall_session(root) as session:
            index = getattr(session, "index", None)
            if index is None or str(index.snapshot_hash or "") != generation:
                return (), "execution binding generation is stale (DME generation differs)"
    except Exception as exc:  # noqa: BLE001
        return (), f"execution binding could not be checked against DME: {type(exc).__name__}"

    registry = load_registry(root)
    allowed, receipt_denied, adapter_denied = _policies(allowed_lobes, denied_lobes)
    registered = set(getattr(registry, "_addresses", {}).values())
    for addr in binding.query_coordinates:
        if addr not in registered:
            return (), f"execution binding names an unregistered address {addr}"
        lobe = int(addr[0])
        if lobe in adapter_denied or (allowed is not None and lobe not in allowed):
            return (), f"execution binding names a lobe-illegal address {addr}"

    # cue linkage: binding ↔ meta routing_cues must agree (both absent is fine)
    receipt_meta = meta.get("routing_cues")
    receipt_meta = receipt_meta if isinstance(receipt_meta, Mapping) else None
    meta_receipt_id = str(receipt_meta.get("receipt_id") or "") if receipt_meta else ""
    if (binding.cue_receipt_id or "") != meta_receipt_id:
        return (), "execution binding cue receipt does not match the recall's routing cue receipt"

    # replayable part must be present in the run
    replay = set(replay_query_addresses(text, root=root, allowed_lobes=allowed_lobes, denied_lobes=denied_lobes, query_spec=query_spec))
    bound = set(binding.query_coordinates)
    if not replay <= bound:
        return (), "execution binding lacks addresses the replayable query policy resolves"
    extras = bound - replay
    if extras:
        if not rc.generated_cues_apply_enabled():
            return (), "execution binding carries non-replayable addresses while generated application is off"
        if receipt_meta is None:
            return (), "execution binding carries non-replayable addresses without a linked cue receipt"
        claimed = tuple(str(t) for t in (receipt_meta.get("generated_terms") or ()) if isinstance(t, str))
        try:
            rebuilt = _rebuild_receipt(
                text, registry=registry, allowed=allowed, receipt_denied=receipt_denied, query_spec=query_spec,
                generated_terms=claimed, generator_version=str(receipt_meta.get("generator_version") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            return (), f"cue receipt could not be rebuilt: {type(exc).__name__}"
        if rebuilt.receipt_id != meta_receipt_id:
            return (), "linked cue receipt is not what the compiler produces for this query"
        allowed_extras = set(rebuilt.generated_addresses())
        if not extras <= allowed_extras:
            return (), "execution binding carries addresses not explained by applied generated cues"
    return tuple(binding.query_coordinates), "execution binding validated"


# ── verification: independent revalidation against the live projection ──────


@dataclass(frozen=True)
class RecallBasisVerification:
    valid: bool
    reason: str
    basis_id: str = ""
    kind: str = ""


def _reject(basis: RecallBasis, reason: str) -> RecallBasisVerification:
    return RecallBasisVerification(False, reason, basis.basis_id, basis.kind)


def _receipt_consistent(basis: RecallBasis, cue_receipt: Any) -> str:
    """'' when the basis terms agree with the supplied CueReceipt (dict or
    object), else a reason. Missing receipt → no check (Cut 3 plan: only when
    available)."""
    if cue_receipt is None:
        return ""
    if isinstance(cue_receipt, Mapping):
        generated = set(str(t) for t in cue_receipt.get("generated_terms") or ())
        mapping = cue_receipt.get("resolved_term_addresses") or ()
    else:
        generated = set(getattr(cue_receipt, "generated_terms", ()) or ())
        mapping = getattr(cue_receipt, "resolved_term_addresses", ()) or ()
    if not generated and not mapping:
        return ""
    for term in basis.generated_query_terms:
        if term not in generated:
            return f"generated term {term!r} not in cue receipt"
    wanted = set(basis.query_addresses)
    term_ok = {str(t) for t, _s, a in mapping if _address_tuple(a) in wanted}
    for term in basis.query_terms:
        if term not in term_ok:
            return f"basis term {term!r} does not resolve to a basis address in the cue receipt"
    return ""


def verify_recall_basis(
    basis: RecallBasis,
    *,
    candidate_id: str,
    root: Any = None,
    cue_receipt: Any = None,
    executed_addresses: Iterable[Iterable[int]] | None = None,
) -> RecallBasisVerification:
    """Independently revalidate an UNTRUSTED basis against the live projection
    and graph. Runs AFTER canonical/source verification; a valid basis is
    routing evidence only. Never raises: any failure → invalid with a reason.

    ``executed_addresses`` is the query-coordinate set this recall was allowed
    to execute, established by the CALLER independently of the basis (see
    ``executed_query_addresses``). Every basis address must belong to it — a
    real address in a real row that this query never used is a forgery. A
    caller that cannot supply it cannot obtain a valid basis (fail closed)."""
    try:
        if not isinstance(basis, RecallBasis):
            return RecallBasisVerification(False, "not a RecallBasis")
        errors = basis.validate()
        if errors:
            return _reject(basis, "malformed basis: " + "; ".join(errors))
        if not basis.identity_ok():
            return _reject(basis, "basis_id does not match its content (tampered)")
        if not cut3_admissible_kind(basis.kind):
            return _reject(basis, f"kind {basis.kind!r} is not admissible in Cut 3")
        if str(candidate_id or "") != basis.candidate_id:
            return _reject(basis, "basis candidate_id does not match candidate")

        # ── execution binding (CUT3_REVIEW_REWORK.md): basis.query_addresses ⊆
        #    the independently re-derived executed set, non-empty
        if executed_addresses is None:
            return _reject(basis, "no executed query-address set supplied — cannot bind basis to this query")
        executed = {a for a in (_address_tuple(x) for x in executed_addresses) if a is not None}
        if not basis.query_addresses:
            return _reject(basis, "basis names no query address")
        outside = [a for a in basis.query_addresses if a not in executed]
        if outside:
            return _reject(basis, f"basis address(es) {outside} were not in the executed query set")

        # ── generation identity: DME projection + graph snapshot must both be
        #    the generation the basis names, and must be current
        from memory.dme.adapter import _read_snapshot_hash
        from memory.dme.recall_hook import dme_recall_session

        graph_snapshot = str(_read_snapshot_hash(root) or "")
        if basis.graph_snapshot_hash != basis.projection_generation:
            return _reject(basis, "basis graph snapshot != projection generation")
        if graph_snapshot != basis.projection_generation:
            return _reject(basis, "basis projection generation is stale (graph snapshot moved)")
        with dme_recall_session(root) as session:
            index = getattr(session, "index", None)
            if index is None or str(index.snapshot_hash or "") != basis.projection_generation:
                return _reject(basis, "basis projection generation is stale (DME generation differs)")
            rows = {
                str((meta or {}).get("memory_id") or ""): i for i, meta in enumerate(index.row_index or ())
            }
            cand_row = rows.get(basis.candidate_id)
            if cand_row is None:
                return _reject(basis, "candidate has no active DME row in this generation")

            if basis.kind in ("canonical_coordinate", "canonical_alias"):
                present = [a for a in basis.query_addresses if address_bit_set(index.sram[cand_row], a)]
                if not present:
                    return _reject(basis, "no basis query address is present in the candidate row")
                receipt_reason = _receipt_consistent(basis, cue_receipt)
                if receipt_reason:
                    return _reject(basis, receipt_reason)
                return RecallBasisVerification(True, "coordinate basis revalidated", basis.basis_id, basis.kind)

            if basis.kind == "learned_path":
                from memory.association_strength import (
                    AssociationStrengthIndex,
                    STATUS_ACTIVE,
                    STRUCTURAL_PROVENANCE_CLASS,
                    effective_strength,
                )
                from memory.dme.learned_planes import LearnedPlaneThresholds
                from memory.dme.hop_engine import WEAK_EDGE_TYPES

                if len(basis.path_node_ids) != 2 or not basis.association_ids:
                    return _reject(basis, "learned path lacks bounded endpoints or associations")
                source_id, target_id = basis.path_node_ids
                if target_id != basis.candidate_id:
                    return _reject(basis, "learned path candidate is not its terminal")
                source_row = rows.get(source_id)
                if source_row is None:
                    return _reject(basis, "learned source has no active DME row")
                if not any(address_bit_set(index.sram[source_row], a) for a in basis.query_addresses):
                    return _reject(basis, "learned source row does not overlap the basis query addresses")
                learned_index = AssociationStrengthIndex()
                threshold = LearnedPlaneThresholds().weak
                for association_id in basis.association_ids:
                    record = learned_index.get_record(association_id)
                    if record is None or record.get("status") != STATUS_ACTIVE:
                        return _reject(basis, f"learned association {association_id!r} is inactive or missing")
                    if record.get("provenance_class") == STRUCTURAL_PROVENANCE_CLASS:
                        return _reject(basis, "learned path cites structural provenance")
                    relation = str(record.get("relation_type") or "")
                    source = str(record.get("source_id") or "")
                    target = str(record.get("target_id") or "")
                    endpoints_match = (source == source_id and target == target_id) or (
                        relation in WEAK_EDGE_TYPES
                        and {source, target} == {source_id, target_id}
                    )
                    if not endpoints_match:
                        return _reject(basis, f"learned association {association_id!r} endpoints mismatch")
                    if relation != basis.relation_types[0]:
                        return _reject(basis, f"learned association {association_id!r} relation mismatch")
                    strength = effective_strength(
                        structural=float(record.get("structural_strength") or 0.0),
                        experiential=float(record.get("experiential_strength") or 0.0),
                    )
                    if strength < threshold:
                        return _reject(basis, f"learned association {association_id!r} below active tier")
                receipt_reason = _receipt_consistent(basis, cue_receipt)
                if receipt_reason:
                    return _reject(basis, receipt_reason)
                return RecallBasisVerification(True, "learned path revalidated", basis.basis_id, basis.kind)

            # structural_path
            from memory.dme.hop_engine import STRONG_EDGE_TYPES
            from memory.taxonomy import graph_store as G

            nodes, edges, types = basis.path_node_ids, basis.path_edge_ids, basis.relation_types
            if len(edges) > MAX_STRUCTURAL_PATH_EDGES or len(nodes) != len(edges) + 1:
                return _reject(basis, "structural path exceeds Cut 3 bounds or is malformed")
            if nodes[-1] != basis.candidate_id:
                return _reject(basis, "candidate is not the structural path terminal")
            if any(t not in STRONG_EDGE_TYPES for t in types):
                return _reject(basis, "structural path uses a non-strong relation")
            source_row = rows.get(nodes[0])
            if source_row is None:
                return _reject(basis, "structural source has no active DME row")
            if not any(address_bit_set(index.sram[source_row], a) for a in basis.query_addresses):
                return _reject(basis, "structural source row does not overlap the basis query addresses")
            try:
                conn = G.connect_for_read(root)
            except Exception as exc:  # noqa: BLE001
                return _reject(basis, f"graph unavailable: {exc}")
            try:
                for i, edge_id in enumerate(edges):
                    row = conn.execute(
                        "SELECT source_id, target_id, edge_type FROM edges WHERE edge_id = ?", (edge_id,)
                    ).fetchone()
                    if row is None:
                        return _reject(basis, f"structural path edge {edge_id!r} does not exist")
                    src, dst, etype = str(row[0]), str(row[1]), str(row[2])
                    a, b = nodes[i], nodes[i + 1]
                    if {src, dst} != {a, b}:
                        return _reject(basis, f"structural path edge {edge_id!r} endpoints do not match")
                    if etype != types[i]:
                        return _reject(basis, f"structural path edge {edge_id!r} relation type mismatch")
            finally:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
            receipt_reason = _receipt_consistent(basis, cue_receipt)
            if receipt_reason:
                return _reject(basis, receipt_reason)
            return RecallBasisVerification(True, "structural path revalidated", basis.basis_id, basis.kind)
    except Exception as exc:  # noqa: BLE001 — verifier must never crash on a basis
        log.warning("recall_basis: verification error (%s: %s)", type(exc).__name__, exc)
        try:
            return RecallBasisVerification(False, f"verification error: {type(exc).__name__}", getattr(basis, "basis_id", ""), getattr(basis, "kind", ""))
        except Exception:  # noqa: BLE001
            return RecallBasisVerification(False, "verification error")
