"""Phase 7G — deterministic claim construction.

A MemoryClaim is an atomic (subject, predicate, object) assertion with a content-derived
id that cites its source event as evidence. Structural claims default to confidence 1.0.
MODEL-FREE: claims come from deterministic templates over event fields, never inference.
"""

from __future__ import annotations

from memory.taxonomy.hashing import claim_id_for
from memory.taxonomy.schemas import MEMORY_CLAIM_SCHEMA_VERSION, MemoryClaim


def claim(
    subject: str,
    predicate: str,
    object: str,
    source_event_id: str,
    confidence: float = 1.0,
    status: str = "active",
) -> MemoryClaim:
    """Build a structural MemoryClaim citing its source event as the sole evidence."""
    claim_id = claim_id_for(
        subject, predicate, object, source_event_id, MEMORY_CLAIM_SCHEMA_VERSION
    )
    return MemoryClaim(
        claim_id=claim_id,
        subject=subject,
        predicate=predicate,
        object=object,
        source_event_id=source_event_id,
        evidence_event_ids=(source_event_id,),
        confidence=confidence,
        status=status,
    )
