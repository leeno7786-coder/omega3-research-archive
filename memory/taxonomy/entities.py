"""Phase 7G — deterministic entity construction.

A MemoryEntity is a stable anchor with a content-derived id. Entities are MODEL-FREE
projections of fields already present on a MemoryEvent — never inferred.
"""

from __future__ import annotations

from memory.taxonomy.hashing import entity_id_for
from memory.taxonomy.schemas import MEMORY_ENTITY_SCHEMA_VERSION, MemoryEntity


def entity(
    entity_kind: str, canonical_name: str, source_event_id: str, aliases: tuple = ()
) -> MemoryEntity:
    """Build a MemoryEntity with a deterministic, name-normalized id."""
    entity_id = entity_id_for(entity_kind, canonical_name, MEMORY_ENTITY_SCHEMA_VERSION)
    return MemoryEntity(
        entity_id=entity_id,
        entity_kind=entity_kind,
        canonical_name=canonical_name,
        source_event_id=source_event_id,
        aliases=tuple(aliases),
    )
