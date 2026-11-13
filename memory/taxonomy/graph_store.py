"""Phase 7H — the SQLite graph store (low-level, idempotent upserts).

This module owns connection + idempotent INSERT OR REPLACE writes into the compiled
graph. It does NOT decide what to compile (that's the 7I compiler) — it is a dumb,
deterministic store. The graph is disposable, but online rebuilds replace its rows in
one transaction so active readers retain a coherent snapshot. All writes are
content-keyed by primary id, so re-inserting the same record never duplicates or drifts.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Optional

from memory.taxonomy import graph_schema, paths as P
from memory.taxonomy.normalize import canonical_json
from memory.taxonomy.schemas import MemoryClaim, MemoryEdge, MemoryEntity, MemoryEvent

MEMORY_NODE_SCHEMA_VERSION = "omega.memory_node.v1"

# Task 1 instrumentation (projection-speed plan): realized-work counter --
# counts actual ``neighbors()`` calls, which fire once per seat (plus
# bridge-mediator lookups) inside ``hop_engine.build_hop_planes`` during
# DME publication, not just at recall time.
_neighbor_call_count = 0


def graph_neighbor_call_count() -> int:
    """Cumulative process-wide count of ``neighbors()`` calls."""
    return _neighbor_call_count


def graph_path(root: Path | str | None = None) -> Path:
    """The graph file for ``root``. Legacy (no projection manifest): today's fixed
    ``compiled/graph.sqlite``. Under generation mode (memory-organ Cut 13, D-13-2/19)
    the path is the AMBIENT view's immutable ``gen-<id>/graph.sqlite``; a manifest
    with no ambient view raises ``GenerationViewRequired`` — no unleased Path is ever
    returned."""
    from memory.projection.generation import resolve_view_or_legacy

    mode, view = resolve_view_or_legacy(root)
    if mode == "generation":
        return view.graph_sqlite
    return P.compiled_root(root) / "graph.sqlite"


def connect_readonly(path: Path | str) -> sqlite3.Connection:
    """Cut 13 (D-13-3): open a PUBLISHED graph read-only — URI ``mode=ro&immutable=1``,
    no schema creation, no journal_mode change, no -wal/-shm; INSERT raises."""
    from memory.projection.generation import connect_readonly as _ro

    return _ro(Path(path))


def connect_for_read(root: Path | str | None = None) -> sqlite3.Connection:
    """The READ-side connection every derived reader uses (Cut 13). Legacy: exactly
    today's ``connect(root)`` (byte-identical behaviour). Generation mode: the ambient
    view's verified ``graph.sqlite`` opened read-only; a manifest without a view raises
    ``GenerationViewRequired``; an unavailable lane raises ``GenerationUnavailable``."""
    from memory.projection.generation import resolve_view_or_legacy

    mode, view = resolve_view_or_legacy(root)
    if mode == "generation":
        return connect_readonly(view.verified_path("graph.sqlite"))
    return connect(root)


def connect(root: Path | str | None = None) -> sqlite3.Connection:
    """Open the graph in WAL mode and ensure the schema exists (WRITER / legacy path).

    WAL lets cognition finish reading the previous projection while a DeepSleep
    compile commits its successor. The busy timeout covers brief writer overlap;
    compile_lock remains the authority for serializing full rebuilds.
    """
    path = P.compiled_root(root) / "graph.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    if str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() != "wal":
        conn.execute("PRAGMA journal_mode = WAL")
    graph_schema.create_schema(conn)
    return conn


def reset_graph(root: Path | str | None = None) -> None:
    """Delete the graph for explicit offline reset operations only.

    Normal compilation uses :func:`clear_graph` and never unlinks the live file.
    Caller must close every connection before invoking this destructive helper.
    """
    path = P.compiled_root(root) / "graph.sqlite"  # legacy fixed name: never a generation dir
    if path.exists():
        path.unlink()


def clear_graph(conn: sqlite3.Connection) -> None:
    """Clear the complete projection inside the caller's active transaction."""
    for table in ("recall_log", "edges", "nodes", "claims", "entities", "events"):
        conn.execute(f"DELETE FROM {table}")


# ── idempotent upserts ───────────────────────────────────────────────────────--
def insert_event(conn: sqlite3.Connection, ev: MemoryEvent) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO events "
        "(event_id, event_kind, episode_id, source, created_at, content_hash, payload_json, schema_version) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            ev.event_id,
            ev.event_kind,
            ev.episode_id,
            ev.source,
            ev.created_at,
            ev.content_hash,
            canonical_json(ev.payload),
            ev.schema_version,
        ),
    )


def insert_entity(conn: sqlite3.Connection, e: MemoryEntity) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO entities "
        "(entity_id, entity_kind, canonical_name, aliases_json, source_event_id, schema_version) "
        "VALUES (?,?,?,?,?,?)",
        (
            e.entity_id,
            e.entity_kind,
            e.canonical_name,
            canonical_json(list(e.aliases)),
            e.source_event_id,
            e.schema_version,
        ),
    )


def insert_claim(conn: sqlite3.Connection, c: MemoryClaim) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO claims "
        "(claim_id, subject, predicate, object, confidence, status, source_event_id, evidence_event_ids_json, schema_version) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            c.claim_id,
            c.subject,
            c.predicate,
            c.object,
            c.confidence,
            c.status,
            c.source_event_id,
            canonical_json(list(c.evidence_event_ids)),
            c.schema_version,
        ),
    )


def insert_node(
    conn: sqlite3.Connection,
    *,
    node_id: str,
    node_type: str,
    label: str,
    payload: Any,
    source_event_id: str = "",
    schema_version: str = MEMORY_NODE_SCHEMA_VERSION,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO nodes "
        "(node_id, node_type, label, payload_json, source_event_id, schema_version) "
        "VALUES (?,?,?,?,?,?)",
        (node_id, node_type, label, canonical_json(payload), source_event_id, schema_version),
    )


def insert_node_if_absent(
    conn: sqlite3.Connection,
    *,
    node_id: str,
    node_type: str,
    label: str,
    payload: Any,
    source_event_id: str = "",
    schema_version: str = MEMORY_NODE_SCHEMA_VERSION,
) -> None:
    """Insert a node ONLY if absent (KEEP-FIRST) — an entity node's id (== entity_id) excludes
    source, so a later occurrence must not overwrite the first's payload. Used by incremental
    compile; workflow_memory nodes have unique ids so this is equivalent to OR REPLACE there."""
    conn.execute(
        "INSERT OR IGNORE INTO nodes "
        "(node_id, node_type, label, payload_json, source_event_id, schema_version) "
        "VALUES (?,?,?,?,?,?)",
        (node_id, node_type, label, canonical_json(payload), source_event_id, schema_version),
    )


def insert_edge(conn: sqlite3.Connection, e: MemoryEdge) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO edges "
        "(edge_id, source_id, target_id, edge_type, confidence, evidence_event_id, payload_json, schema_version) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            e.edge_id,
            e.source_id,
            e.target_id,
            e.edge_type,
            e.confidence,
            e.evidence_event_id,
            canonical_json(e.payload),
            e.schema_version,
        ),
    )


def insert_recall_log(
    conn: sqlite3.Connection,
    *,
    recall_id: str,
    query_hash: str,
    query: str,
    recall_packet_json: str,
    snapshot_hash: str,
    created_at: str,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO recall_log "
        "(recall_id, query_hash, query, recall_packet_json, snapshot_hash, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (recall_id, query_hash, query, recall_packet_json, snapshot_hash, created_at),
    )


def insert_entity_if_absent(conn: sqlite3.Connection, e: MemoryEntity) -> None:
    """Insert an entity ONLY if its id is absent (KEEP-FIRST). entity_id excludes
    source_event_id, so a later occurrence of the same entity must NOT overwrite the first
    — this matches the full compiler's extract_from_events dedup (setdefault keeps first).
    Used by the incremental compiler; full compile inserts each unique entity once."""
    conn.execute(
        "INSERT OR IGNORE INTO entities "
        "(entity_id, entity_kind, canonical_name, aliases_json, source_event_id, schema_version) "
        "VALUES (?,?,?,?,?,?)",
        (
            e.entity_id,
            e.entity_kind,
            e.canonical_name,
            canonical_json(list(e.aliases)),
            e.source_event_id,
            e.schema_version,
        ),
    )


def snapshot_hash_from_graph(
    conn: sqlite3.Connection, *, compiler_version: str, schema_version: str, config_hash: str
) -> str:
    """Compute the snapshot hash from the CURRENT graph state (read-back).

    Shared by full AND incremental compile, so both hash the IDENTICAL final graph — making
    full-vs-incremental snapshot parity STRUCTURAL (same rows in the graph => same hash, no
    matter how they were inserted). Reconstructs the canonical dataclass form so the hash
    matches snapshot_hash_for's contract; order-independent (snapshot_hash_for sorts).
    """
    import json

    from memory.taxonomy.hashing import snapshot_hash_for

    events = [
        MemoryEvent(
            event_id=r["event_id"],
            event_kind=r["event_kind"],
            episode_id=r["episode_id"],
            source=r["source"],
            created_at=r["created_at"],
            content_hash=r["content_hash"],
            payload=json.loads(r["payload_json"]),
            schema_version=r["schema_version"],
        ).to_dict()
        for r in conn.execute("SELECT * FROM events")
    ]
    entities = [
        MemoryEntity(
            entity_id=r["entity_id"],
            entity_kind=r["entity_kind"],
            canonical_name=r["canonical_name"],
            source_event_id=r["source_event_id"],
            aliases=tuple(json.loads(r["aliases_json"])),
            schema_version=r["schema_version"],
        ).to_dict()
        for r in conn.execute("SELECT * FROM entities")
    ]
    claims = [
        MemoryClaim(
            claim_id=r["claim_id"],
            subject=r["subject"],
            predicate=r["predicate"],
            object=r["object"],
            source_event_id=r["source_event_id"],
            evidence_event_ids=tuple(json.loads(r["evidence_event_ids_json"])),
            confidence=r["confidence"],
            status=r["status"],
            schema_version=r["schema_version"],
        ).to_dict()
        for r in conn.execute("SELECT * FROM claims")
    ]
    edges = [
        MemoryEdge(
            edge_id=r["edge_id"],
            source_id=r["source_id"],
            target_id=r["target_id"],
            edge_type=r["edge_type"],
            evidence_event_id=r["evidence_event_id"],
            confidence=r["confidence"],
            payload=json.loads(r["payload_json"]),
            schema_version=r["schema_version"],
        ).to_dict()
        for r in conn.execute("SELECT * FROM edges")
    ]
    return snapshot_hash_for(
        canonical_events=[canonical_json(e) for e in events],
        canonical_entities=[canonical_json(e) for e in entities],
        canonical_claims=[canonical_json(c) for c in claims],
        canonical_edges=[canonical_json(g) for g in edges],
        compiler_version=compiler_version,
        schema_version=schema_version,
        config_hash=config_hash,
    )


# ── reads ────────────────────────────────────────────────────────────────────--
def count(conn: sqlite3.Connection, table: str) -> int:
    if table not in graph_schema.TABLE_NAMES:
        raise ValueError(f"unknown table: {table!r}")
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def get_event(conn: sqlite3.Connection, event_id: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
    return dict(row) if row else None


def table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r[0] for r in rows}


def index_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    return {r[0] for r in rows}


def get_node(conn: sqlite3.Connection, node_id: str) -> Optional[dict[str, Any]]:
    """Return a node dict by node_id, or None if not found."""
    import json

    row = conn.execute(
        "SELECT node_id, node_type, label, payload_json, source_event_id, schema_version FROM nodes WHERE node_id = ?",
        (node_id,),
    ).fetchone()
    if row:
        res = dict(row)
        try:
            res["payload"] = json.loads(res.pop("payload_json"))
        except Exception:
            res["payload"] = {}
        return res
    return None


def get_edge(conn: sqlite3.Connection, edge_id: str) -> Optional[dict[str, Any]]:
    """Return one graph edge's directed endpoints and relation type."""
    row = conn.execute(
        "SELECT edge_id, source_id, target_id, edge_type, confidence, evidence_event_id "
        "FROM edges WHERE edge_id = ?",
        (edge_id,),
    ).fetchone()
    return dict(row) if row else None


def neighbors(
    conn: sqlite3.Connection, node_id: str, edge_types: Optional[list[str]] = None
) -> list[dict[str, Any]]:
    """Return list of outgoing/incoming neighbor relations:
    [{"node_id", "node_type", "label", "edge_type", "direction", "confidence", "payload",
      "edge_payload", "edge_id", "evidence_event_id"}]
    """
    global _neighbor_call_count
    _neighbor_call_count += 1
    import json

    query = """
        SELECT n.node_id, n.node_type, n.label, n.payload_json, e.edge_type, 'outbound' as direction, e.confidence, e.payload_json as edge_payload_json, e.edge_id, e.evidence_event_id
        FROM edges e
        JOIN nodes n ON e.target_id = n.node_id
        WHERE e.source_id = ?
        UNION ALL
        SELECT n.node_id, n.node_type, n.label, n.payload_json, e.edge_type, 'inbound' as direction, e.confidence, e.payload_json as edge_payload_json, e.edge_id, e.evidence_event_id
        FROM edges e
        JOIN nodes n ON e.source_id = n.node_id
        WHERE e.target_id = ?
    """
    params = [node_id, node_id]
    rows = conn.execute(query, params).fetchall()

    results = []
    for r in rows:
        edge_type = r["edge_type"]
        if edge_types is not None and edge_type not in edge_types:
            continue
        try:
            payload = json.loads(r["payload_json"])
        except Exception:
            payload = {}
        try:
            edge_payload = json.loads(r["edge_payload_json"])
        except Exception:
            edge_payload = {}
        results.append(
            {
                "node_id": r["node_id"],
                "node_type": r["node_type"],
                "label": r["label"],
                "edge_type": edge_type,
                "direction": r["direction"],
                "confidence": r["confidence"],
                "payload": payload,
                "edge_payload": edge_payload,
                # Cut 3: exact edge identity so a RecallBasis structural path
                # can name — and the verifier can re-derive — the precise
                # graph edges it crossed. Additive; existing keys unchanged.
                "edge_id": r["edge_id"],
                "evidence_event_id": r["evidence_event_id"],
            }
        )
    return results
