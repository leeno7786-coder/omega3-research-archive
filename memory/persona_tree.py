"""Deterministic persona tree projection from canonical memory.

PersonaTree is a read projection, not a new store. It groups verified canonical
persona/situational records by the same link keys used by entity_linker and
separates current-head facts from superseded history.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class PersonaTree:
    person_id: str
    names: tuple[str, ...] = ()
    current_facts: tuple[dict[str, Any], ...] = ()
    relationships: tuple[dict[str, Any], ...] = ()
    events: tuple[dict[str, Any], ...] = ()
    face_links: tuple[dict[str, Any], ...] = ()
    history: tuple[dict[str, Any], ...] = ()
    source_ids: tuple[str, ...] = ()
    record_ids: tuple[str, ...] = ()
    type: str = "persona_tree"
    schema_version: str = "omega.persona_tree.v1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "person_id": self.person_id,
            "names": list(self.names),
            "current_facts": list(self.current_facts),
            "relationships": list(self.relationships),
            "events": list(self.events),
            "face_links": list(self.face_links),
            "history": list(self.history),
            "source_ids": list(self.source_ids),
            "record_ids": list(self.record_ids),
            "schema_version": self.schema_version,
        }


def build_persona_tree(person: str, *, root: Path | str | None = None) -> PersonaTree:
    """Build a verified PersonaTree for one person name/id."""
    person_id = _person_key(person)
    rows = _verified_person_records(person_id, root=root)
    names: set[str] = set()
    current_facts: list[dict[str, Any]] = []
    relationships: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    face_links: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = []
    source_ids: set[str] = set()
    record_ids: set[str] = set()

    for rec in rows:
        memory_id = str(rec.get("id") or "")
        if memory_id:
            record_ids.add(memory_id)
        provenance = rec.get("provenance") if isinstance(rec.get("provenance"), Mapping) else {}
        source_id = str(provenance.get("source_id") or "")
        if source_id:
            source_ids.add(source_id)
        names.update(_names_from_record(rec))

        fact = _fact_from_record(rec)
        if rec.get("superseded_by"):
            history.append(fact)
            continue
        current_facts.append(fact)
        coord = list(rec.get("coord") or [])
        if coord and coord[0] == 4:
            events.append(fact)
        relationships.extend(_relationships_from_record(rec))
        face_links.extend(_face_links_from_record(rec))

    return PersonaTree(
        person_id=person_id,
        names=tuple(sorted(names)),
        current_facts=tuple(sorted(current_facts, key=_record_sort_key)),
        relationships=tuple(sorted(_dedupe_dicts(relationships), key=_record_sort_key)),
        events=tuple(sorted(events, key=_record_sort_key)),
        face_links=tuple(sorted(_dedupe_dicts(face_links), key=_record_sort_key)),
        history=tuple(sorted(history, key=_record_sort_key)),
        source_ids=tuple(sorted(source_ids)),
        record_ids=tuple(sorted(record_ids)),
    )


def _verified_person_records(person_id: str, *, root: Path | str | None) -> list[dict[str, Any]]:
    from memory.engine.walker import coord_index_snapshot, load_coord_index, load_meta_by_path
    from memory.recall_verifier import verify_leaf

    try:
        load_coord_index()
    except Exception:
        pass
    out: list[dict[str, Any]] = []
    for memory_id, entry in sorted(coord_index_snapshot().items(), key=lambda item: str(item[0])):
        coord = list((entry or {}).get("coord") or [])
        if not coord or coord[0] not in {1, 4}:
            continue
        path = _resolve_path((entry or {}).get("path"), root=root)
        rec = load_meta_by_path(path) if path else None
        if not isinstance(rec, dict):
            continue
        if person_id not in _record_person_keys(rec):
            continue
        verdict = verify_leaf(str(memory_id), root=root)
        if verdict.status != "verified":
            continue
        out.append(rec)
    return out


def _record_person_keys(rec: Mapping[str, Any]) -> set[str]:
    try:
        from memory.enrichment.entity_linker import extract_link_keys_from_memory

        return {str(item) for item in extract_link_keys_from_memory(dict(rec))}
    except Exception:
        return set()


def _names_from_record(rec: Mapping[str, Any]) -> set[str]:
    names: set[str] = set()
    enrichment = rec.get("enrichment") if isinstance(rec.get("enrichment"), Mapping) else {}
    for item in enrichment.get("entities") or []:
        if isinstance(item, Mapping) and item.get("facet") == "name" and item.get("name"):
            names.add(str(item["name"]))
    domain = str(rec.get("domain") or "")
    if domain:
        names.add(domain)
    return names


def _fact_from_record(rec: Mapping[str, Any]) -> dict[str, Any]:
    provenance = rec.get("provenance") if isinstance(rec.get("provenance"), Mapping) else {}
    return {
        "memory_id": str(rec.get("id") or ""),
        "coord": list(rec.get("coord") or []),
        "type": str(rec.get("type") or ""),
        "title": str(rec.get("title") or ""),
        "summary": str(rec.get("summary") or rec.get("text") or ""),
        "domain": str(rec.get("domain") or ""),
        "subtopic": str(rec.get("subtopic") or ""),
        "version": int(rec.get("version") or 1),
        "supersedes": rec.get("supersedes"),
        "superseded_by": rec.get("superseded_by"),
        "source_id": str(provenance.get("source_id") or ""),
    }


def _relationships_from_record(rec: Mapping[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for link in rec.get("links") or []:
        if isinstance(link, Mapping) and link.get("kind") == "persona_relationship":
            out.append(
                {
                    "memory_id": str(rec.get("id") or ""),
                    "relation": str(link.get("relation") or ""),
                    "other_id": str(link.get("other_id") or ""),
                    "other_name": str(link.get("other_name") or ""),
                }
            )
    enrichment = rec.get("enrichment") if isinstance(rec.get("enrichment"), Mapping) else {}
    for item in enrichment.get("entities") or []:
        if isinstance(item, Mapping) and item.get("facet") == "relationship":
            out.append(
                {
                    "memory_id": str(rec.get("id") or ""),
                    "relation": str(item.get("name") or item.get("relationship") or ""),
                    "other_name": str(item.get("linked_name") or ""),
                    "other_id": str(item.get("linked_id") or ""),
                }
            )
    return out


def _face_links_from_record(rec: Mapping[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for link in rec.get("links") or []:
        if isinstance(link, Mapping) and link.get("kind") == "linked_face":
            out.append(
                {
                    "memory_id": str(rec.get("id") or ""),
                    "vision_path": str(link.get("vision_path") or ""),
                    "vision_coord": list(link.get("vision_coord") or []),
                }
            )
    return out


def _dedupe_dicts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for item in items:
        key = repr(sorted(item.items()))
        seen.setdefault(key, item)
    return [seen[key] for key in sorted(seen)]


def _record_sort_key(item: Mapping[str, Any]) -> tuple[str, str]:
    return (str(item.get("memory_id") or ""), str(item.get("title") or item.get("relation") or ""))


def _person_key(value: str) -> str:
    try:
        from core.persona_router import _slugify

        return _slugify(value)
    except Exception:
        return str(value or "").strip().lower()


def _resolve_path(raw_path: Any, *, root: Path | str | None) -> Path | None:
    if not raw_path:
        return None
    path = Path(str(raw_path))
    if path.is_absolute():
        return path
    if root is None:
        # coord_index entries are profile-relative; resolve like recall does.
        from memory.engine.walker import resolve_coord_index_path

        return resolve_coord_index_path(str(raw_path))
    base = Path(root)
    candidate = base / path
    if candidate.exists():
        return candidate
    # A memory-root override paired with a profile-relative ("memory/...") entry.
    if base.name == "memory" and path.parts and path.parts[0].lower() == "memory":
        alt = base.parent / path
        if alt.exists():
            return alt
    return candidate
