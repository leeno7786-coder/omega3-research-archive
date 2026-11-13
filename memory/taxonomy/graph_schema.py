"""Phase 7H — SQLite schema for the compiled graph projection.

The graph is DISPOSABLE — always rebuildable from the ledger. This module owns only the
DDL: tables (events/entities/claims/nodes/edges/recall_log) + the lookup indexes. All
CREATE statements are IF NOT EXISTS so create_schema is idempotent.
"""

from __future__ import annotations

import sqlite3

_TABLES = (
    """CREATE TABLE IF NOT EXISTS events (
        event_id TEXT PRIMARY KEY,
        event_kind TEXT NOT NULL,
        episode_id TEXT,
        source TEXT,
        created_at TEXT,
        content_hash TEXT,
        payload_json TEXT NOT NULL,
        schema_version TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS entities (
        entity_id TEXT PRIMARY KEY,
        entity_kind TEXT NOT NULL,
        canonical_name TEXT NOT NULL,
        aliases_json TEXT NOT NULL,
        source_event_id TEXT NOT NULL,
        schema_version TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS claims (
        claim_id TEXT PRIMARY KEY,
        subject TEXT NOT NULL,
        predicate TEXT NOT NULL,
        object TEXT NOT NULL,
        confidence REAL NOT NULL,
        status TEXT NOT NULL,
        source_event_id TEXT NOT NULL,
        evidence_event_ids_json TEXT NOT NULL,
        schema_version TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS nodes (
        node_id TEXT PRIMARY KEY,
        node_type TEXT NOT NULL,
        label TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        source_event_id TEXT,
        schema_version TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS edges (
        edge_id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL,
        target_id TEXT NOT NULL,
        edge_type TEXT NOT NULL,
        confidence REAL NOT NULL,
        evidence_event_id TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        schema_version TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS recall_log (
        recall_id TEXT PRIMARY KEY,
        query_hash TEXT NOT NULL,
        query TEXT NOT NULL,
        recall_packet_json TEXT NOT NULL,
        snapshot_hash TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
)

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(node_type)",
    "CREATE INDEX IF NOT EXISTS idx_nodes_label ON nodes(label)",
    "CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id)",
    "CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id)",
    "CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(edge_type)",
    "CREATE INDEX IF NOT EXISTS idx_claims_subject ON claims(subject)",
    "CREATE INDEX IF NOT EXISTS idx_claims_predicate ON claims(predicate)",
    "CREATE INDEX IF NOT EXISTS idx_claims_object ON claims(object)",
    "CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(canonical_name)",
    "CREATE INDEX IF NOT EXISTS idx_events_episode ON events(episode_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_kind ON events(event_kind)",
)

# the canonical table set — also used as the count() whitelist
TABLE_NAMES = ("events", "entities", "claims", "nodes", "edges", "recall_log")


def create_schema(conn: sqlite3.Connection) -> None:
    """Create all tables + indexes (idempotent)."""
    for ddl in _TABLES:
        conn.execute(ddl)
    for ddl in _INDEXES:
        conn.execute(ddl)
    conn.commit()
