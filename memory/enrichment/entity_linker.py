"""Entity linker — post-write hook that connects memories sharing
named entities.

After an entity-bearing memory is written, scans the coord_index for
other entity-bearing memories, loads their enrichment, finds shared
names, and patches bidirectional links into both files.

Linkable lobes are PERSONAS (1) + SITUATIONAL (4). Personas hold
"X is my dad" / "X is my cat" style records; situational holds "X
likes Y" / "X said Y" / "X did Y" records. Without cross-lobe linking,
expand_links has nothing to walk — the dad→Bob persona and
Bob→chicken assertion stay isolated and recall can't chain.
"""

import json
import logging
from pathlib import Path
from typing import Dict, Optional, Set

log = logging.getLogger("omega.memory.enrichment.entity_linker")

# Lobes whose records are scanned + linked by shared proper nouns.
# Sourced from memory.dewey_map.ENTITY_BEARING_LOBES — the single
# canonical set used by entity_linker, supersedes, brain.retrieve, and
# the post-commit hooks. Add lobes there, not here.
from memory.dewey_map import ENTITY_BEARING_LOBES as _LINKABLE_LOBES


def extract_names_from_enrichment(enrichment: Optional[Dict]) -> Set[str]:
    """Pull all name-facet entities from an enrichment dict."""
    if not enrichment:
        return set()
    return {
        e["name"].lower()
        for e in enrichment.get("entities", [])
        if e.get("facet") == "name" and e.get("name")
    }


def extract_names_from_text(text: str) -> Set[str]:
    """Fallback: extract capitalized proper nouns from raw text.

    Deprecated for entity linking — use ``extract_link_keys_from_memory``
    which keys on persona_router person IDs and situational linked_persons.
    """
    import re

    stopwords = {
        "the",
        "this",
        "that",
        "these",
        "those",
        "she",
        "her",
        "his",
        "they",
        "and",
        "but",
        "for",
        "not",
        "was",
        "are",
    }
    matches = re.findall(r"\b([A-Z][a-z]{1,20})\b", text)
    return {m.lower() for m in matches if m.lower() not in stopwords}


def _person_link_key(name_or_id: str) -> str:
    """Normalize a display name or person_id to a stable link key."""
    try:
        from core.persona_router import _slugify

        return _slugify(name_or_id)
    except Exception:
        return str(name_or_id or "").strip().lower()


def extract_link_keys_from_memory(mem: Dict) -> Set[str]:
    """Deterministic entity-link keys — persona IDs + linked_persons only."""
    keys: Set[str] = set()
    coord = mem.get("coord") or []
    enrichment = mem.get("enrichment") or {}

    for person in enrichment.get("linked_persons") or []:
        if person:
            keys.add(_person_link_key(str(person)))

    domain = str(mem.get("domain") or "").strip()
    if coord and coord[0] == 1 and domain:
        keys.add(domain.lower())

    for entity in enrichment.get("entities") or []:
        if entity.get("facet") == "name" and entity.get("name"):
            keys.add(_person_link_key(str(entity["name"])))

    return keys


def link_personal_memories(
    new_mem_path: str,
    coord_index: Dict,
    min_shared_names: int = 1,
    *,
    report: Optional[Dict] = None,
) -> int:
    """Scan personal memories for shared names and add bidirectional links.

    Args:
        new_mem_path: Path to the newly written memory file.
        coord_index: Current COORD_INDEX dict.
        min_shared_names: Minimum shared names to create a link.
        report: optional dict the linker fills with write outcomes for the
            post-commit owner (Cut 4 rework 4): ``back_link_failures`` (peer
            rewrites that failed — peer bytes intact), ``forward_link_failed``
            (new-record rewrite failed — new bytes intact, peers already
            carry back-links), ``errors``.

    Every canonical rewrite here is an exact-path ATOMIC replacement
    (``memory.atomic_replace``): a failure leaves the previous canonical bytes
    byte-identical (CUT4_REVIEW_REWORK_4 §5).

    Returns:
        Number of links created.
    """
    if report is not None:
        report.setdefault("back_link_failures", 0)
        report.setdefault("forward_link_failed", False)
        report.setdefault("errors", [])
        # every peer canonical file this pass REWROTE (id + path), so the
        # write owner can represent each as projection work instead of
        # marking the whole projection dirty (rebuild-required)
        report.setdefault("peer_rewrites", [])
    new_path = Path(new_mem_path)
    if not new_path.exists():
        return 0

    try:
        with open(new_path, "r", encoding="utf-8") as f:
            new_mem = json.load(f)
    except FileNotFoundError:
        log.warning("Entity linker: memory file not found: %s", new_path)
        return 0
    except json.JSONDecodeError as e:
        log.error("Entity linker: invalid JSON in %s: %s", new_path, e)
        return 0
    except Exception as e:
        log.error("Entity linker: failed to load %s: %s", new_path, e)
        return 0

    new_id = new_mem.get("id", "")
    new_coord = new_mem.get("coord", [])

    # Skip writes outside the entity-bearing lobes — coding/temporal/
    # journal records don't carry the proper-noun semantics this linker
    # is designed for, and walking them would generate spurious links
    # off shared common nouns.
    if not new_coord or new_coord[0] not in _LINKABLE_LOBES:
        return 0

    # Get deterministic link keys from structured enrichment / persona domain.
    new_names = extract_link_keys_from_memory(new_mem)
    if not new_names:
        return 0

    links_created = 0

    # Pre-build a coord → {ids at that coord} lookup ONCE before the
    # per-record loop. _normalize_existing_links is called twice per
    # candidate (new_mem + each other_mem), and the prior naive
    # implementation iterated the whole coord_index per coord-form
    # link — O(K·L·N) on each entity-bearing write, which becomes a
    # perceptible stall as the corpus grows. With this lookup, each
    # coord expansion is O(1).
    _coord_to_ids: dict = {}
    for _eid, _entry in coord_index.items():
        _ec = _entry.get("coord") or []
        for _depth in range(min(3, len(_ec)) + 1):
            _key = tuple(_ec[:_depth]) if _depth else ()
            _coord_to_ids.setdefault(_key, set()).add(_eid)

    def _expand_link_to_ids(lnk):
        """Resolve a single link to a set of id-strings.

        - String link → {link} (id passes through; we don't validate
          here, an unresolvable id will simply never match a real id).
        - Coord-triple link → {all ids at that coord prefix}, looked
          up via the pre-built _coord_to_ids index. Legacy coord-form
          links are inherently ambiguous when multiple records share a
          coord, so we treat them as "any record at this coord." This
          means dedupe is conservative: if a record carries both legacy
          `[1, 0, 0]` AND a new id-form link to a record at coord
          [1, 0, 0], the id will be recognized as already-linked.
        """
        if isinstance(lnk, str):
            return {lnk}
        if isinstance(lnk, (list, tuple)) and len(lnk) >= 2:
            return set(_coord_to_ids.get(tuple(lnk[:3]), set()))
        return set()

    def _normalize_existing_links(links):
        """Dedupe set for a record's `links` array; coord-form expanded
        to id-form via the pre-built lookup."""
        out: set = set()
        for lnk in links or []:
            out.update(_expand_link_to_ids(lnk))
        return out

    existing_links = _normalize_existing_links(new_mem.get("links", []))

    for other_id, entry in coord_index.items():
        if other_id == new_id:
            continue
        other_coord = entry.get("coord", [])
        if not other_coord or other_coord[0] not in _LINKABLE_LOBES:
            continue

        from memory.engine.walker import resolve_coord_index_path

        other_path = resolve_coord_index_path(entry.get("path"))
        if other_path is None or not other_path.exists():
            continue

        try:
            with open(other_path, "r", encoding="utf-8") as f:
                other_mem = json.load(f)
        except Exception:
            continue

        # Skip records that have been superseded by a newer revision.
        # Linking to a stale record produces edges recall has to walk
        # through to reach the current head — wasted hops AND
        # the model sees historical content as if it were current.
        # The supersedes module's chain walk handles "X was Y" framing;
        # the linker only stitches CURRENT facts about CURRENT entities.
        if other_mem.get("superseded_by"):
            continue

        other_names = extract_link_keys_from_memory(other_mem)
        if not other_names:
            continue

        shared = new_names & other_names
        if len(shared) < min_shared_names:
            continue

        # Bidirectional edge: forward (new → other) AND back (other →
        # new). The two writes are persisted at different points (back
        # immediately on the other_path; forward batched at end on
        # new_path), so we do the back-link write FIRST and only count
        # the link as created if the on-disk back-edge succeeded. If
        # the back-write fails, revert the in-memory new_mem mutation
        # — otherwise we leave a one-way edge: new→other persists,
        # other→new doesn't, and recall walking from `other` never
        # finds `new`. Asymmetric edges produce mysterious "dad→Bob
        # works but Bob→dad doesn't" recall holes.
        if not other_id or other_id in existing_links:
            continue

        # Try the back-link first.
        other_existing = _normalize_existing_links(other_mem.get("links", []))
        back_link_persisted = True  # default true when the link already exists
        if new_id and new_id not in other_existing:
            other_mem.setdefault("links", []).append(new_id)
            try:
                from memory.atomic_replace import atomic_replace_json

                atomic_replace_json(other_path, other_mem)  # peer bytes intact on failure
                if report is not None:
                    report["peer_rewrites"].append({"memory_id": other_id, "path": str(other_path)})
            except Exception as e:
                if report is not None:
                    report["back_link_failures"] += 1
                    report["errors"].append(f"back_link:{other_id}: {type(e).__name__}: {e}")
                # Revert the in-memory append so the dict matches disk.
                if (
                    other_mem.get("links")
                    and other_mem["links"]
                    and other_mem["links"][-1] == new_id
                ):
                    other_mem["links"].pop()
                back_link_persisted = False
                log.error(
                    "entity_linker: back-link write failed for %s "
                    "(target of new=%s): %s — skipping forward edge "
                    "to keep links symmetric",
                    other_path,
                    new_id,
                    e,
                )
                from guardian.terminal_feedback import emit_data

                emit_data(
                    "memory",
                    "entity_linker",
                    status="degraded",
                    ok=False,
                    reason="back_link_write_failed",
                    target_path=other_path,
                    error=str(e),
                )

        if not back_link_persisted:
            # Don't add the forward edge — keeping links symmetric is
            # more important than maximum coverage. expand_links walks
            # both directions during recall, so dropping ONE edge means
            # the linker will retry next time the new record is touched.
            continue

        # Both back-link succeeded (or already present) → add forward.
        new_mem.setdefault("links", []).append(other_id)
        existing_links.add(other_id)
        links_created += 1

    # Persist forward links on the new record.
    if links_created > 0:
        try:
            from memory.atomic_replace import atomic_replace_json

            atomic_replace_json(new_path, new_mem)  # new-record bytes intact on failure
            log.debug("Entity linker: %d links added to %s", links_created, new_id)
        except Exception as e:
            if report is not None:
                report["forward_link_failed"] = True
                report["errors"].append(f"forward_link: {type(e).__name__}: {e}")
            # Forward-link persist failed AFTER all back-links wrote.
            # Now we have asymmetric edges in the OPPOSITE direction
            # (back-edges on disk, forward edges in memory only).
            # Promote to .error so the inconsistency is visible.
            log.error(
                "entity_linker: forward-links write failed for %s: %s — "
                "%d back-link(s) on peers now point at a record whose own "
                "links file is stale; next linker pass will reconcile",
                new_path,
                e,
                links_created,
            )
            from guardian.terminal_feedback import emit_data

            emit_data(
                "memory",
                "entity_linker",
                status="degraded",
                ok=False,
                reason="forward_link_write_failed",
                new_path=new_path,
                error=str(e),
            )

    return links_created
