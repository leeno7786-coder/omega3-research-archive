"""Phase 7C — content-derived deterministic IDs and the snapshot hash.

NO random UUIDs for canonical memory IDs (work-order rule 8/9). Every id is a pure
function of its semantic content via sha256. Components are joined with a unit
separator (\\x1f) so component boundaries can never collide (e.g. ("ab","c") vs
("a","bc")). Free-text identity fields are normalized first so whitespace/case
variants converge to the same id; structured ids/sources are used verbatim.
"""

from __future__ import annotations

import hashlib
from typing import Any

from memory.taxonomy.normalize import canonical_json, normalize_id_field, sorted_for_hash

_SEP = "\x1f"  # ASCII unit separator — non-colliding component boundary

# id prefixes — self-describing keys for ledger/graph/CLI debugging
_EVENT_PREFIX = "evt_"
_ENTITY_PREFIX = "ent_"
_CLAIM_PREFIX = "clm_"
_EDGE_PREFIX = "edg_"


def _sha256(parts: list[str]) -> str:
    blob = _SEP.join(parts)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def event_id_for(
    event_kind: str, episode_id: str, source: str, payload: Any, schema_version: str
) -> str:
    """sha256(schema_version, event_kind, episode_id, source, canonical_payload_json).

    payload is canonicalized (sorted keys) so dict insertion order never changes the id.
    Structured components stay verbatim — a different source IS a different event.
    """
    return _EVENT_PREFIX + _sha256(
        [schema_version, event_kind, episode_id, source, canonical_json(payload)]
    )


def entity_id_for(entity_kind: str, canonical_name: str, schema_version: str) -> str:
    """sha256(schema_version, entity_kind, normalized_canonical_name).

    canonical_name is normalized (trim/collapse/lowercase) so name variants converge.
    """
    return _ENTITY_PREFIX + _sha256(
        [schema_version, entity_kind, normalize_id_field(canonical_name)]
    )


def claim_id_for(
    subject: str, predicate: str, object: str, source_event_id: str, schema_version: str
) -> str:
    """sha256(schema_version, norm(subject), norm(predicate), norm(object), source_event_id).

    Same claim from the same source -> same claim_id (idempotent extraction).
    """
    return _CLAIM_PREFIX + _sha256(
        [
            schema_version,
            normalize_id_field(subject),
            normalize_id_field(predicate),
            normalize_id_field(object),
            source_event_id,
        ]
    )


def edge_id_for(
    source_id: str, edge_type: str, target_id: str, evidence_event_id: str, schema_version: str
) -> str:
    """sha256(schema_version, source_id, edge_type, target_id, evidence_event_id).

    source_id/target_id are already-canonical ids -> used verbatim.
    """
    return _EDGE_PREFIX + _sha256(
        [schema_version, source_id, edge_type, target_id, evidence_event_id]
    )


def snapshot_hash_for(
    canonical_events: Any,
    canonical_entities: Any,
    canonical_claims: Any,
    canonical_edges: Any,
    compiler_version: str,
    schema_version: str,
    config_hash: str,
) -> str:
    """sha256 over the SORTED canonical event/entity/claim/edge sets + version/config.

    Order-independent (each collection is sorted) so the same graph contents always
    yield the same hash regardless of compile order. Returns the raw 64-char hex
    (a hash, not a prefixed id).
    """
    parts: list[str] = [schema_version, compiler_version, config_hash]
    parts += sorted_for_hash(canonical_events)
    parts += sorted_for_hash(canonical_entities)
    parts += sorted_for_hash(canonical_claims)
    parts += sorted_for_hash(canonical_edges)
    return _sha256(parts)


def span_hash_for(text: str) -> str:
    """sha256 over a source span or canonical text slice (Memory Engine v2 proof)."""
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()
