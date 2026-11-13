"""Deterministic intake packet stamped onto canonical memory records.

The packet is a compact audit record for the pre-write semantic intake step:
which source span produced this memory, which lobe/subtopic it routed to, and
which decomposition surfaces were present. It intentionally stores hashes and
counts, not raw duplicate text.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from memory.deterministic import stable_hash

MEMORY_INTAKE_PACKET_SCHEMA_VERSION = "omega.memory_intake_packet.v1"


def build_intake_packet(
    *,
    memory_id: str,
    text: str,
    coord: list[int],
    source_tag: str,
    metadata: Mapping[str, Any],
    enrichment: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a deterministic intake audit packet for a canonical write."""
    meta = dict(metadata or {})
    enrich = dict(enrichment or {})
    coord_list = _safe_coord(coord)
    packet_material = {
        "memory_id": str(memory_id or ""),
        "source_id": str(meta.get("source_id") or ""),
        "span_hash": str(meta.get("span_hash") or ""),
        "coord": coord_list,
        "type": str(meta.get("type") or "concept"),
        "title": str(meta.get("title") or ""),
        "summary_hash": stable_hash("summary", str(meta.get("summary") or ""), length=16),
        "text_hash": stable_hash("text", str(text or ""), length=16),
    }
    entities = enrich.get("entities") if isinstance(enrich.get("entities"), list) else []
    claims = enrich.get("claims") if isinstance(enrich.get("claims"), list) else []
    facets = enrich.get("facets") if isinstance(enrich.get("facets"), Mapping) else {}
    return {
        "type": "memory_intake_packet",
        "packet_id": "mip_" + stable_hash("memory.intake.packet", packet_material, length=16),
        "memory_id": str(memory_id or ""),
        "origin_id": str(meta.get("origin_id") or source_tag or ""),
        "episode_id": str(enrich.get("episode_id") or meta.get("episode_id") or ""),
        "source": str(source_tag or ""),
        "source_id": str(meta.get("source_id") or ""),
        "source_type": str(meta.get("source_type") or source_tag or ""),
        "span_hash": str(meta.get("span_hash") or ""),
        "source_content_hash": str(meta.get("source_content_hash") or ""),
        "coord": coord_list,
        "primary_lobe": coord_list[0],
        "subdomain": coord_list[1],
        "memory_type": str(meta.get("type") or "concept"),
        "domain": str(meta.get("domain") or ""),
        "subtopic": str(meta.get("subtopic") or meta.get("topic") or ""),
        "tags": sorted(str(tag) for tag in (meta.get("tags") or []) if str(tag).strip()),
        "text_hash": packet_material["text_hash"],
        "summary_hash": packet_material["summary_hash"],
        "decomposition": {
            "keys": sorted(str(key) for key in enrich),
            "entity_count": len(entities),
            "claim_count": len(claims),
            "facet_keys": sorted(str(key) for key in facets),
            "link_count": len(meta.get("links") or []),
            "sense_count": len(enrich.get("sense_ids") or []),
            "quarantined": bool(meta.get("quarantined") or enrich.get("status") == "quarantined"),
        },
        "schema_version": MEMORY_INTAKE_PACKET_SCHEMA_VERSION,
    }


def _safe_coord(coord: list[int] | tuple[int, ...] | None) -> list[int]:
    raw = list(coord or [])
    if len(raw) < 3:
        raw = raw + [0] * (3 - len(raw))
    return [int(raw[0]), int(raw[1]), int(raw[2])]
