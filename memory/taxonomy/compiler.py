"""Memory graph compiler (v5) — projects the disposable graph from CANONICAL memory.

compile_memory_graph() is a FULL rebuild: it reads canonical Dewey records via
read_events_from_canonical() + the coord_index, derives entities/claims/nodes and
explicit Dewey edges (MENTIONS / SUPERSEDES / CONTRADICTS / CO_LOCATED / SHARES_DOMAIN /
SHARES_TAG / persona relations), rebuilds graph.sqlite transactionally, and computes a
snapshot hash from the compiled-graph read-back (graph_store.snapshot_hash_from_graph).
Same canonical memory + same COMPILER_VERSION + same config (edge params folded into
config_hash) => same hash. No model calls, no wall-clock, no path leakage, and — since
v7 (memory-organ Cut 5A) — NO vector read: the graph is STRUCTURAL-ONLY. The
SEMANTIC_SIMILARITY edge class (the retired dense semantic path) is retired;
a canonical record with or without a stored vector compiles to the identical graph
(17 Gate D1). The semantic projection representation is deliberately UNFROZEN pending
Program I (see docs/engineering/memory-organ/12 §4a); nothing here pre-decides it. The
graph is disposable and rebuildable.

The incremental compiler + compile_state cursor are retired (Stage 4): canonical memory
mutates via supersedes/decay, so an append-only cursor premise is void.
"""

from __future__ import annotations

import os

import heapq
import json
from itertools import groupby
from pathlib import Path


from memory.taxonomy import extract, graph_store as G, paths as P
from memory.taxonomy.canonical_events import read_events_from_canonical
from memory.taxonomy.compile_lock import compile_lock
from memory.taxonomy.failure_topology import failure_topology
from memory.taxonomy.workflow_memory import workflow_topology
from memory.taxonomy.hashing import edge_id_for
from memory.taxonomy.normalize import canonical_json
from memory.taxonomy.schemas import (
    MEMORY_EDGE_SCHEMA_VERSION,
    MEMORY_GRAPH_SNAPSHOT_SCHEMA_VERSION,
    MemoryLeafV2,
    MemoryEdge,
    MemoryGraphSnapshot,
)

# v7 (memory-organ Cut 5A): structural-only — the SEMANTIC_SIMILARITY class and
# its stored-vector read are gone; the compiler never touches `embeddings`.
# v6: named-entity projection (canonicalize.extract_named_entities) — records
# MENTION entity nodes so the hop engine bridges memory—entity—memory; dev
# lobe (11) restored to the recallable set. v5: compile from canonical
# memory, explicit Dewey + similarity edges.
COMPILER_VERSION = "omega.memory_compiler.v7"

# Dewey edge parameters. Folded into config_hash so a change is
# version-gated (deterministic: a param change => a different snapshot hash).
CO_EDGE_FANOUT = 8       # co-located / shares-domain / shares-tag fan-out
MIN_SHARED_TAGS = 2      # min tag overlap for a SHARES_TAG edge


def _effective_config_hash(base: str) -> str:
    """Fold the edge params into the caller's config_hash so the snapshot hash is
    gated on them (constraint: a param change must change the hash). Structural
    params only since v7 (no similarity params exist any more)."""
    return f"{base}|fanout={CO_EDGE_FANOUT};mintags={MIN_SHARED_TAGS}"


# predicate -> graph edge_type, for claims whose subject AND object are both entities
_PREDICATE_EDGE = {
    "used_tool": "USED_TOOL",
    "changed_file": "PRODUCED",
    "failed_because": "FAILED_BECAUSE",
    # Typed causal edges (spec section 9, T11): projected from canonical causal facts.
    "informed": "INFORMED",
    "modified": "MODIFIED",
    "observed": "OBSERVED",
    "continued": "CONTINUED",
    "superseded": "SUPERSEDED",
}


def _build_nodes(entities) -> list[dict]:
    """One graph node per entity (node_id == entity_id)."""
    return [
        {
            "node_id": e.entity_id,
            "node_type": e.entity_kind,
            "label": e.canonical_name,
            "payload": e.to_dict(),
            "source_event_id": e.source_event_id,
        }
        for e in entities
    ]


def compile_source(source_id: str, *, root: Path | str | None = None) -> dict:
    """Return ledger-derived citation fields for a SourceLedger artifact."""
    clean_id = str(source_id or "").strip()
    if not clean_id:
        return {}
    try:
        from memory.source_ledger.store import read_source
    except Exception:
        return {}
    artifact = read_source(clean_id, root=root)
    if artifact is None:
        return {}
    return artifact.citation()


def _ledger_citation_from_provenance(
    provenance: dict,
    *,
    root: Path | str | None,
) -> tuple[dict, bool]:
    source_id = str(provenance.get("source_id") or "")
    if not source_id:
        return {}, False
    citation = compile_source(source_id, root=root)
    if not citation:
        return {}, True
    mismatch = False
    for key, expected in citation.items():
        cited = provenance.get(key)
        if cited is None or cited == "":
            continue
        if key in {"span_start", "span_end"}:
            try:
                if int(cited) != int(expected):
                    mismatch = True
            except (TypeError, ValueError):
                mismatch = True
        elif str(cited) != str(expected):
            mismatch = True
    return citation, mismatch


def _memory_leaf_from_meta(
    memory_id: str,
    meta: dict,
    *,
    root: Path | str | None,
) -> MemoryLeafV2:
    provenance = dict(meta.get("provenance") or {})
    enrichment = dict(meta.get("enrichment") or {})
    flags = dict(meta.get("flags") or {})
    status = str(meta.get("status") or enrichment.get("status") or "active")
    if meta.get("superseded_by"):
        status = "superseded"
    if status not in {"active", "superseded", "rejected", "quarantined"}:
        status = "active"
    ledger_citation, source_mismatch = _ledger_citation_from_provenance(provenance, root=root)
    source_fields = dict(provenance)
    source_fields.update(ledger_citation)
    if source_mismatch and status == "active":
        status = "quarantined"

    def _int_field(name: str) -> int:
        try:
            return int(source_fields.get(name) or 0)
        except (TypeError, ValueError):
            return 0

    sense_ids = enrichment.get("sense_ids") or meta.get("sense_ids") or ()
    if isinstance(sense_ids, str):
        sense_ids = (sense_ids,)

    return MemoryLeafV2(
        memory_id=memory_id,
        coord=tuple(meta.get("coord") or ()),
        title=str(meta.get("title") or ""),
        summary=str(meta.get("summary") or ""),
        source_id=str(source_fields.get("source_id") or ""),
        source_type=str(source_fields.get("source_type") or ""),
        span_start=_int_field("span_start"),
        span_end=_int_field("span_end"),
        span_hash=str(source_fields.get("span_hash") or ""),
        source_session_id=str(source_fields.get("source_session_id") or ""),
        source_session_timestamp=str(source_fields.get("source_session_timestamp") or ""),
        sense_ids=tuple(sense_ids or ()),
        status=status,
        domain=str(meta.get("domain") or ""),
        subtopic=str(meta.get("subtopic") or meta.get("topic") or ""),
        tags=tuple(meta.get("tags") or ()),
        contradiction=str(
            enrichment.get("contradiction_state")
            or meta.get("contradiction_state")
            or meta.get("contradiction")
            or ""
        ),
        flagged=bool(flags.get("flagged") or meta.get("flagged") or False),
        flag_reason=str(flags.get("flag_reason") or meta.get("flag_reason") or ""),
        flag_response_mode=str(
            flags.get("flag_response_mode") or meta.get("flag_response_mode") or ""
        ),
        via_link=bool(meta.get("via_link") or False),
        version=int(meta.get("version") or 1),
        source_content_hash=str(
            source_fields.get("source_content_hash") or source_fields.get("content_hash") or ""
        ),
        workspace_source=(
            dict(meta.get("workspace_source")) if isinstance(meta.get("workspace_source"), dict) else {}
        ),
    )


def _build_edges(entities, claims) -> list[MemoryEdge]:
    """Deterministic edges from claims whose subject + object both resolve to entities."""
    name_to_id: dict[str, str] = {}
    for e in entities:
        name_to_id.setdefault(e.canonical_name, e.entity_id)
    seen: dict[str, MemoryEdge] = {}
    for c in claims:
        edge_type = _PREDICATE_EDGE.get(c.predicate)
        if not edge_type:
            continue
        src = name_to_id.get(c.subject)
        tgt = name_to_id.get(c.object)
        if not src or not tgt:
            continue
        edge_id = edge_id_for(src, edge_type, tgt, c.source_event_id, MEMORY_EDGE_SCHEMA_VERSION)
        seen.setdefault(
            edge_id,
            MemoryEdge(
                edge_id=edge_id,
                source_id=src,
                target_id=tgt,
                edge_type=edge_type,
                evidence_event_id=c.source_event_id,
            ),
        )
    return [seen[key] for key in sorted(seen)]


def build_projection(events):
    """Deterministic (entities, claims, nodes, edges) from a list of events.

    EXACTLY the full-compile derivation (extract entities/claims, then failure + workflow
    topology, keep-first dedup, build nodes/edges). The incremental compiler reuses this over
    the NEW events only; because entity/edge ids are content-derived (name-/source-keyed) the
    derived rows align with the existing graph.
    """
    entities, claims = extract.extract_from_events(events)
    fail_entities, fail_edges = failure_topology(events)
    wf_entities, wf_nodes, wf_edges = workflow_topology(events)

    entities_by_id = {e.entity_id: e for e in entities}
    for e in (*fail_entities, *wf_entities):
        entities_by_id.setdefault(e.entity_id, e)
    entities = [entities_by_id[key] for key in sorted(entities_by_id)]

    nodes = _build_nodes(entities) + wf_nodes
    edges_by_id = {g.edge_id: g for g in _build_edges(entities, claims)}
    for g in (*fail_edges, *wf_edges):
        edges_by_id.setdefault(g.edge_id, g)
    edges = [edges_by_id[key] for key in sorted(edges_by_id)]
    return entities, claims, nodes, edges


def compile_structural_edges(root: Path | str | None = None) -> list[MemoryEdge]:
    """Derive the compiler's current structural edge set without publishing it."""
    return list(_assemble_projection(root)[-1])


def _write_manifest(snap: MemoryGraphSnapshot, root: Path | str | None, *, output_dir: Path | None = None) -> None:
    if output_dir is not None:
        # Cut 13 generation build (D-13-21): the snapshot document is an immutable candidate
        # artifact — atomically replaced, no fixed-name marker file, no wall clock, no paths.
        from memory.atomic_replace import atomic_replace_bytes

        Path(output_dir).mkdir(parents=True, exist_ok=True)
        atomic_replace_bytes(Path(output_dir) / "graph_snapshot.json", canonical_json(snap.to_dict()).encode("utf-8"))
        return
    compiled = P.compiled_root(root)
    compiled.mkdir(parents=True, exist_ok=True)
    (compiled / "graph_snapshot.json").write_text(canonical_json(snap.to_dict()), encoding="utf-8")
    (compiled / "latest_snapshot_hash.txt").write_text(snap.snapshot_hash, encoding="utf-8")


def _finalize_compile(conn, *, root, config_hash: str, output_dir: Path | None = None) -> MemoryGraphSnapshot:
    """Hash the CURRENT graph (read-back), write the manifest, and return the snapshot.

    The advisory compile-state cursor write is retired (full compiles only).
    """
    snapshot_hash = G.snapshot_hash_from_graph(
        conn,
        compiler_version=COMPILER_VERSION,
        schema_version=MEMORY_GRAPH_SNAPSHOT_SCHEMA_VERSION,
        config_hash=config_hash,
    )
    snap = MemoryGraphSnapshot(
        snapshot_hash=snapshot_hash,
        event_count=G.count(conn, "events"),
        entity_count=G.count(conn, "entities"),
        claim_count=G.count(conn, "claims"),
        node_count=G.count(conn, "nodes"),
        edge_count=G.count(conn, "edges"),
        compiler_version=COMPILER_VERSION,
        schema_version_compiled=MEMORY_GRAPH_SNAPSHOT_SCHEMA_VERSION,
        config_hash=config_hash,
    )
    _write_manifest(snap, root, output_dir=output_dir)
    return snap


def _build_shared_tag_edges(records: dict[str, dict]) -> list[MemoryEdge]:
    """Build legacy-identical SHARES_TAG edges from an inverted tag index."""
    record_tags: dict[str, set] = {}
    postings: dict[object, list[str]] = {}
    for memory_id, meta in records.items():
        tags = set(meta.get("tags") or [])
        record_tags[memory_id] = tags
        for tag in tags:
            postings.setdefault(tag, []).append(memory_id)

    for posting in postings.values():
        posting.sort()

    edges: list[MemoryEdge] = []
    for memory_id in records:
        tags = record_tags[memory_id]
        if not tags or CO_EDGE_FANOUT == 0:
            continue

        merged = heapq.merge(*(postings[tag] for tag in tags))
        selected = 0
        for target, occurrences in groupby(merged):
            overlap = sum(1 for _ in occurrences)
            if target == memory_id or overlap < MIN_SHARED_TAGS:
                continue
            evidence_id = f"dewey:{memory_id}"
            edges.append(
                MemoryEdge(
                    edge_id=edge_id_for(
                        memory_id,
                        "SHARES_TAG",
                        target,
                        evidence_id,
                        MEMORY_EDGE_SCHEMA_VERSION,
                    ),
                    source_id=memory_id,
                    target_id=target,
                    edge_type="SHARES_TAG",
                    evidence_event_id=evidence_id,
                )
            )
            selected += 1
            if selected >= CO_EDGE_FANOUT:
                break
    return edges


def _assemble_projection(root: Path | str | None):
    """Everything up to the transactional publish: canonical events → projection → dewey
    nodes/edges over the coord index and canonical records (reads resolve under ``root``)."""
    events = read_events_from_canonical(root=root)
    entities, claims, nodes, edges = build_projection(events)

    import logging

    logger = logging.getLogger("omega.memory.compiler")

    # Load coordinate index
    from core.paths import OMEGA_ROOT

    if root is not None:
        memory_root = Path(root) / "memory"
    else:
        memory_root = OMEGA_ROOT / "memory"

    index_path = memory_root / "indexes" / "coord_index.json"
    coord_index = {}
    if index_path.exists():
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                coord_index = json.load(f)
        except Exception as exc:
            logger.warning("Failed to load coord index for compiler: %s", exc)

    # 1. Build Dewey edges & nodes
    dewey_nodes = []
    dewey_edges = []

    # Projection set = every lobe MINUS the structural recall-lane boundary
    # (dewey_map.NON_RECALLABLE_LOBES: 6 journal = metacognitive state, 7 vision
    # = dedicated visual pipeline — operator 2026-07-18). 11 (dev) IS
    # recallable — its omission was the Gate-1 autopsy root cause: L11 records
    # projected NO nodes/edges, capping alias pairs at 8/10 and engine-direct
    # hit@1 at 20/24 (2026-07-04).
    from memory.dewey_map import LOBE_MAP, NON_RECALLABLE_LOBES

    RECALLABLE_LOBES = set(LOBE_MAP) - set(NON_RECALLABLE_LOBES)
    # CO_EDGE_FANOUT / MIN_SHARED_TAGS are module-level (folded into config_hash).

    records = {}
    coord_to_ids = {}
    subdomain_groups = {}
    domain_groups = {}
    subtopic_groups = {}

    for mid, entry in coord_index.items():
        coord = entry.get("coord")
        if not coord or not isinstance(coord, list) or len(coord) < 3:
            continue
        lobe = coord[0]
        if lobe not in RECALLABLE_LOBES:
            continue

        path_str = entry.get("path")
        if not path_str:
            continue
        p = Path(path_str)
        if not p.is_absolute():
            # Profile-relative coord_index paths (memory/...) must resolve
            # against the active profile root. When root is None the worker
            # has already bound OMEGA_ROOT to that profile.
            base = Path(root) if root is not None else OMEGA_ROOT
            p = base / p
        if not p.exists():
            continue

        try:
            with open(p, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            continue

        records[mid] = meta
        c = tuple(coord)
        coord_to_ids.setdefault(c[:3], []).append(mid)
        subdomain_groups.setdefault(c[:2], []).append(mid)

        domain = meta.get("domain")
        if domain and str(domain).strip():
            domain_groups.setdefault(str(domain).strip(), []).append(mid)
        subtopic = meta.get("subtopic")
        if subtopic and str(subtopic).strip():
            subtopic_groups.setdefault(str(subtopic).strip(), []).append(mid)

    # Create memory_record nodes
    for mid, meta in records.items():
        leaf = _memory_leaf_from_meta(mid, meta, root=root)
        dewey_nodes.append(
            {
                "node_id": mid,
                "node_type": "memory_record",
                "label": meta.get("title") or f"Record {mid}",
                "payload": leaf.to_dict(),
                "source_event_id": f"dewey:{mid}",
            }
        )

    # Named-entity projection (Task #13 alias lane): deterministic proper-name
    # extraction from record text; each record MENTIONS its entity nodes and
    # the hop engine bridges memory—entity—memory, so alias recovery no longer
    # depends on the accident of shared subdomain placement.
    from memory.taxonomy.canonicalize import extract_named_entities
    from memory.taxonomy.entities import entity as _named_entity
    from memory.taxonomy.extract_persona import relation_graph_from_record

    dewey_entities: dict = {}
    for mid in sorted(records):
        meta = records[mid]
        text = " ".join(str(meta.get(key) or "") for key in ("title", "summary", "text"))
        for name in extract_named_entities(text):
            ent = _named_entity("named_entity", name, f"dewey:{mid}")
            dewey_entities.setdefault(ent.entity_id, ent)
            edge_id = edge_id_for(
                mid, "MENTIONS", ent.entity_id, f"dewey:{mid}", MEMORY_EDGE_SCHEMA_VERSION
            )
            dewey_edges.append(
                MemoryEdge(
                    edge_id=edge_id,
                    source_id=mid,
                    target_id=ent.entity_id,
                    edge_type="MENTIONS",
                    evidence_event_id=f"dewey:{mid}",
                )
            )

        # Semantic wiring: project the parser's parsed relations (son/owns/studies/...)
        # from this record's enrichment into named entity->entity edges, so the recall
        # plane walks the relation, not just co-occurrence (extract_persona).
        rel_entities, rel_edges = relation_graph_from_record(mid, meta)
        for ent in rel_entities:
            dewey_entities.setdefault(ent.entity_id, ent)
        dewey_edges.extend(rel_edges)

    # Helper to resolve links (either ID or coord)
    def resolve_target(lnk):
        if isinstance(lnk, str):
            if lnk in coord_index:
                return [lnk]
        elif isinstance(lnk, list) and len(lnk) >= 2:
            return coord_to_ids.get(tuple(lnk[:3]), [])
        return []

    for mid, meta in records.items():
        # Links -> MENTIONS
        links = meta.get("links") or []
        for lnk in links:
            for tgt in resolve_target(lnk):
                if tgt != mid:
                    edge_id = edge_id_for(
                        mid, "MENTIONS", tgt, f"dewey:{mid}", MEMORY_EDGE_SCHEMA_VERSION
                    )
                    dewey_edges.append(
                        MemoryEdge(
                            edge_id=edge_id,
                            source_id=mid,
                            target_id=tgt,
                            edge_type="MENTIONS",
                            evidence_event_id=f"dewey:{mid}",
                        )
                    )

        # Supersedes -> SUPERSEDES
        supersedes = meta.get("supersedes")
        if supersedes:
            for tgt in resolve_target(supersedes) if isinstance(supersedes, (list, str)) else []:
                if tgt != mid:
                    edge_id = edge_id_for(
                        mid, "SUPERSEDES", tgt, f"dewey:{mid}", MEMORY_EDGE_SCHEMA_VERSION
                    )
                    dewey_edges.append(
                        MemoryEdge(
                            edge_id=edge_id,
                            source_id=mid,
                            target_id=tgt,
                            edge_type="SUPERSEDES",
                            evidence_event_id=f"dewey:{mid}",
                        )
                    )

        # Contradiction_state -> CONTRADICTS. The write path stores it under
        # `enrichment` (root schema is additionalProperties:false); fall back to a
        # top-level value for any record that carries it directly.
        contradicts = (meta.get("enrichment") or {}).get("contradiction_state") or meta.get(
            "contradiction_state"
        )
        if contradicts:
            for tgt in resolve_target(contradicts) if isinstance(contradicts, (list, str)) else []:
                if tgt != mid:
                    edge_id = edge_id_for(
                        mid, "CONTRADICTS", tgt, f"dewey:{mid}", MEMORY_EDGE_SCHEMA_VERSION
                    )
                    dewey_edges.append(
                        MemoryEdge(
                            edge_id=edge_id,
                            source_id=mid,
                            target_id=tgt,
                            edge_type="CONTRADICTS",
                            evidence_event_id=f"dewey:{mid}",
                        )
                    )

    # Co-located edges
    for _group_key, group_mids in subdomain_groups.items():
        if len(group_mids) < 2:
            continue
        group_mids = sorted(group_mids)
        n = len(group_mids)
        for i, src in enumerate(group_mids):
            for offset in range(1, min(CO_EDGE_FANOUT + 1, n)):
                tgt = group_mids[(i + offset) % n]
                if src != tgt:
                    edge_id = edge_id_for(
                        src, "CO_LOCATED", tgt, f"dewey:{src}", MEMORY_EDGE_SCHEMA_VERSION
                    )
                    dewey_edges.append(
                        MemoryEdge(
                            edge_id=edge_id,
                            source_id=src,
                            target_id=tgt,
                            edge_type="CO_LOCATED",
                            evidence_event_id=f"dewey:{src}",
                        )
                    )

    # Shares Domain edges
    grouped_mids = [domain_groups[key] for key in sorted(domain_groups)]
    grouped_mids.extend(subtopic_groups[key] for key in sorted(subtopic_groups))
    for group_mids in grouped_mids:
        if len(group_mids) < 2:
            continue
        group_mids = sorted(list(set(group_mids)))
        n = len(group_mids)
        for i, src in enumerate(group_mids):
            for offset in range(1, min(CO_EDGE_FANOUT + 1, n)):
                tgt = group_mids[(i + offset) % n]
                if src != tgt:
                    edge_id = edge_id_for(
                        src, "SHARES_DOMAIN", tgt, f"dewey:{src}", MEMORY_EDGE_SCHEMA_VERSION
                    )
                    dewey_edges.append(
                        MemoryEdge(
                            edge_id=edge_id,
                            source_id=src,
                            target_id=tgt,
                            edge_type="SHARES_DOMAIN",
                            evidence_event_id=f"dewey:{src}",
                        )
                    )

    # Shares Tag edges
    dewey_edges.extend(_build_shared_tag_edges(records))

    # (v6 built cross-lobe SEMANTIC_SIMILARITY edges from stored vectors here;
    # retired in v7 — the graph is structural-only, Cut 5A.)

    # Combine nodes & edges (event-derived entities keep precedence on id clash)
    known_entity_ids = {e.entity_id for e in entities}
    extra_entities = [
        dewey_entities[key] for key in sorted(dewey_entities) if key not in known_entity_ids
    ]
    entities = list(entities) + extra_entities
    nodes = list(nodes) + dewey_nodes + _build_nodes(extra_entities)
    edges = list(edges) + dewey_edges

    return events, entities, claims, nodes, edges


def _compile_generation(source_root: Path, output_dir: Path, *, config_hash: str) -> MemoryGraphSnapshot:
    """Cut 13 generation build: reads under ``source_root``, writes ONLY into ``output_dir``."""
    import sqlite3

    from memory.taxonomy import graph_schema

    events, entities, claims, nodes, edges = _assemble_projection(source_root)
    output_dir.mkdir(parents=True, exist_ok=True)
    graph_path = output_dir / "graph.sqlite"
    for stale in (graph_path, output_dir / "graph.sqlite-wal", output_dir / "graph.sqlite-shm"):
        if stale.exists():
            stale.unlink()
    conn = sqlite3.connect(str(graph_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode = DELETE")   # published graphs never carry -wal/-shm (D-13-3)
        graph_schema.create_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        for ev in events:
            G.insert_event(conn, ev)
        for e in entities:
            G.insert_entity(conn, e)
        for c in claims:
            G.insert_claim(conn, c)
        for n in nodes:
            G.insert_node(conn, **n)
        for edge in edges:
            G.insert_edge(conn, edge)
        conn.commit()
        snap = _finalize_compile(conn, root=source_root, config_hash=_effective_config_hash(config_hash), output_dir=output_dir)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()
    # Windows' fsync (_commit) needs a writable fd and raises EBADF on a read-only
    # one; POSIX fsyncs a read-only fd fine, so it keeps least privilege.
    fd = os.open(graph_path, os.O_RDWR if os.name == "nt" else os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return snap


def compile_memory_graph(
    root: Path | str | None = None,
    *,
    config_hash: str = "default",
    source_root: Path | str | None = None,
    output_dir: Path | str | None = None,
) -> MemoryGraphSnapshot:
    """Rebuild the graph projection from canonical memory and return its snapshot.

    Full rebuild only — the incremental compiler is retired (Stage 4). Canonical memory
    mutates via supersedes/decay, so an append-only cursor premise is void.

    Memory-organ Cut 13 (D-13-26/36): ``source_root`` + ``output_dir`` select the
    GENERATION build — every read (canonical events, coord index, records, SourceLedger)
    resolves under the captured profile-form ``source_root`` and every write lands in
    ``output_dir`` (``graph.sqlite`` created fresh with ``journal_mode=DELETE`` and
    ``graph_snapshot.json`` atomically replaced); no compile lock (the publication
    authority is the sole writer), no fixed-name marker, no DME rebuild here (the
    builder does that from the candidate graph). The in-place ``root=`` mode remains
    ONLY for the legacy comparator.
    """
    if (source_root is None) != (output_dir is None):
        raise ValueError("compile_memory_graph: source_root and output_dir must be given together")
    if source_root is not None:
        return _compile_generation(Path(source_root), Path(output_dir), config_hash=config_hash)
    # AGENTS.md §2: past this point the compile writes the FIXED-NAME graph in place, and
    # with a manifest on disk ``G.connect``/``rebuild_sram_from_graph`` resolve into the
    # published generation's own files. Refuse before the assemble, so a surviving
    # generation is never read into, written over, or rebuilt around.
    # The candidate mode above is the publication builder's own seam and is never gated:
    # it reads a captured input profile and writes only inside the candidate directory.
    from memory.projection.dispatch import assert_single_projection_path

    assert_single_projection_path(root)
    events, entities, claims, nodes, edges = _assemble_projection(root)
    # Single-writer lock: replace+finalize must never interleave with another
    # rebuild (compile-on-close / on-boot / reindex), which could split graph.sqlite
    # from the manifest. Rows are replaced in one WAL transaction rather than
    # unlinking graph.sqlite: cognition may still be reading the prior snapshot.
    with compile_lock(root):
        conn = G.connect(root)
        try:
            conn.execute("BEGIN IMMEDIATE")
            G.clear_graph(conn)
            for ev in events:
                G.insert_event(conn, ev)
            for e in entities:
                G.insert_entity(conn, e)
            for c in claims:
                G.insert_claim(conn, c)
            for n in nodes:
                G.insert_node(conn, **n)
            for edge in edges:
                G.insert_edge(conn, edge)
            conn.commit()
            snap = _finalize_compile(
                conn, root=root, config_hash=_effective_config_hash(config_hash)
            )
            from memory.dme.adapter import rebuild_sram_from_graph
            from memory.rebuild_instrumentation import stage_timer

            with stage_timer("rebuild_sram_from_graph"):
                rebuild_sram_from_graph(root=root, snapshot_hash=snap.snapshot_hash)
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()
    return snap
