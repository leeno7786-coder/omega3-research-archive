"""Named relation edges from record enrichment — the graph's semantic wiring.

The filesystem is the source of truth; the graph is the recall plane. Until now the plane
only had CO-OCCURRENCE edges (shares-a-tag, semantic-similarity) plus named claims for the
DEV lobe — so a hop from ``person:noah`` could reach a record but never *because* Noah owns
Omega AI or has a son named Abel. Those relations are ALREADY extracted by the deterministic
parser and sit in each record's ``enrichment.entities``; this module projects them into the
graph as SEMANTIC edges between entity nodes, so recall traverses the relation, not the
coincidence.

MODEL-FREE + deterministic (same record -> same entities/edges/ids), so the compiler stays
idempotent. Subject of a persona self-fact is the OPERATOR entity; the object is the linked
value. (Alias resolution — operator == "Noah" == "Noah Lee" — is a separate pass; here the
edges are built canonically against the ``operator`` node.)
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from memory.taxonomy.entities import entity as _entity
from memory.taxonomy.hashing import edge_id_for
from memory.taxonomy.schemas import MEMORY_EDGE_SCHEMA_VERSION, MemoryEdge, MemoryEntity

# parser facet -> (graph edge_type, object entity_kind). The specific relation (e.g. "son")
# rides in the edge payload so the planner can narrate it without an unbounded edge vocabulary.
_FACET_EDGE: Dict[str, Tuple[str, str]] = {
    "relationship": ("FAMILY_OF", "named_entity"),
    "business": ("OWNS", "named_entity"),
    "education": ("STUDIES", "named_entity"),
    "workplace": ("WORKS_AT", "named_entity"),
    "occupation": ("WORKS_AS", "named_entity"),
    "location": ("LIVES_IN", "named_entity"),
    "preference": ("LIKES", "named_entity"),
}

# Edge types this module introduces — must be registered in schemas.EDGE_TYPES and, for the
# recall plane to walk them, in recall_graph._VIA_LINK_EDGE_TYPES.
PERSONA_RELATION_EDGE_TYPES = frozenset(t for t, _ in _FACET_EDGE.values())


def relation_graph_from_record(
    memory_id: str, meta: Dict[str, Any]
) -> Tuple[List[MemoryEntity], List[MemoryEdge]]:
    """Entity nodes + named relation edges for one record's parsed relations.

    Returns ([], []) for records with no relational enrichment (the common case), so the
    caller can fold the result unconditionally."""
    enrichment = meta.get("enrichment")
    if not isinstance(enrichment, dict):
        return [], []
    parsed = enrichment.get("entities")
    if not isinstance(parsed, list) or not parsed:
        return [], []

    src_ev = f"dewey:{memory_id}"
    entities: Dict[str, MemoryEntity] = {}
    edges: Dict[str, MemoryEdge] = {}
    subjects: Dict[str, MemoryEntity] = {}

    def _add_entity(e: MemoryEntity) -> None:
        entities.setdefault(e.entity_id, e)

    def _subject_entity(value: str) -> MemoryEntity:
        # the operator is a stable pivot node; anyone else is a named entity. (Alias
        # resolution — operator == "Noah", one node per person — is a separate pass.)
        return (
            _entity("operator", "operator", src_ev)
            if value == "operator"
            else _entity("named_entity", value, src_ev)
        )

    def _add_edge(a: MemoryEntity, b: MemoryEntity, edge_type: str, payload: dict) -> None:
        eid = edge_id_for(a.entity_id, edge_type, b.entity_id, src_ev, MEMORY_EDGE_SCHEMA_VERSION)
        edges.setdefault(
            eid,
            MemoryEdge(
                edge_id=eid,
                source_id=a.entity_id,
                target_id=b.entity_id,
                edge_type=edge_type,
                evidence_event_id=src_ev,
                payload=payload,
            ),
        )

    for item in parsed:
        if not isinstance(item, dict):
            continue
        facet = str(item.get("facet") or "").strip().lower()

        if facet == "name":
            # A name fact ALIASES its subject to its name entity, so a name query reaches
            # the subject pivot the relations hang off ("operator's name is noah" ->
            # person:noah SAME_AS operator). Bidirectional (recall walks either way). The
            # operator's own name resolves the pivot; a related person's own name is a
            # self-alias (skipped).
            name_value = str(item.get("name") or "").strip().lower()
            subject_value = str(item.get("subject") or "operator").strip().lower()
            if name_value and subject_value and name_value != subject_value:
                subj = _subject_entity(subject_value)
                name_ent = _entity("named_entity", name_value, src_ev)
                _add_entity(subj)
                _add_entity(name_ent)
                subjects.setdefault(subj.entity_id, subj)
                _add_edge(name_ent, subj, "SAME_AS", {"alias": name_value, "record_id": memory_id})
                _add_edge(subj, name_ent, "SAME_AS", {"alias": name_value, "record_id": memory_id})
            continue

        mapping = _FACET_EDGE.get(facet)
        if mapping is None:
            continue
        edge_type, object_kind = mapping
        # relationships name the person in linked_name; other facets in name.
        raw_object = item.get("linked_name") if facet == "relationship" else item.get("name")
        object_value = str(raw_object or "").strip().lower()
        if not object_value:
            continue
        # the SPECIFIC relation the planner narrates: "son" for a relationship, else the verb.
        relation = (
            str(item.get("name") or facet).strip().lower() if facet == "relationship" else facet
        )
        # SUBJECT-CENTRIC: the fact hangs off the entity it is ABOUT — the operator for a
        # self-fact, the named person for "Abel likes trains". So Abel's OWN facts hang
        # off Abel (2 hops from Noah -> low weight), not the operator.
        subject_value = str(item.get("subject") or "operator").strip().lower()
        subj = _subject_entity(subject_value)
        obj = _entity(object_kind, object_value, src_ev)
        _add_entity(subj)
        _add_entity(obj)
        subjects.setdefault(subj.entity_id, subj)
        edge_id = edge_id_for(
            subj.entity_id, edge_type, obj.entity_id, src_ev, MEMORY_EDGE_SCHEMA_VERSION
        )
        edges.setdefault(
            edge_id,
            MemoryEdge(
                edge_id=edge_id,
                source_id=subj.entity_id,
                target_id=obj.entity_id,
                edge_type=edge_type,
                evidence_event_id=src_ev,
                payload={"relation": relation, "facet": facet, "record_id": memory_id},
            ),
        )

    if not edges:
        return [], []

    # Anchor the record to each subject entity so its subgraph is reachable from the
    # source record (records MENTION proper nouns, not the pivot node).
    for subj in subjects.values():
        anchor_id = edge_id_for(
            memory_id, "MENTIONS", subj.entity_id, src_ev, MEMORY_EDGE_SCHEMA_VERSION
        )
        edges.setdefault(
            anchor_id,
            MemoryEdge(
                edge_id=anchor_id,
                source_id=memory_id,
                target_id=subj.entity_id,
                edge_type="MENTIONS",
                evidence_event_id=src_ev,
                payload={"anchor": "subject"},
            ),
        )

    return list(entities.values()), list(edges.values())
