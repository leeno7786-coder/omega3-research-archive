"""Phase 7O — graph snapshot diff.

``capture_graph_state()`` reads the CURRENTLY-compiled graph and returns its identity sets
(node ids, edge ids, claim id->status) plus the stored snapshot hash. ``diff_graph_snapshots()``
compares two captured states and reports what the graph gained/lost between them — so an
operator can see exactly what a new batch of events added (skill-execution / workflow memory,
failure topology, ...). Deterministic, model-free, Guardian-free.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from memory.taxonomy import graph_store as G
from memory.taxonomy import paths as P


def _stored_snapshot_hash(root: Optional[Path | str]) -> str:
    from memory.projection.generation import active_graph_snapshot_hash  # Cut 13: manifest hash under generation mode

    published = active_graph_snapshot_hash(root)
    if published is not None:
        return published
    f = P.compiled_root(root) / "latest_snapshot_hash.txt"
    return f.read_text(encoding="utf-8").strip() if f.exists() else ""


def capture_graph_state(root: Optional[Path | str] = None) -> dict:
    """Capture the identity sets of the currently-compiled graph (input to the diff)."""
    from memory.projection.generation import projection_read_transaction

    with projection_read_transaction(root):  # Cut 13: the hash and the rows come from ONE generation
        return _capture_graph_state_in_transaction(root)


def _capture_graph_state_in_transaction(root: Optional[Path | str]) -> dict:
    conn = G.connect_for_read(root)
    try:
        nodes = [r[0] for r in conn.execute("SELECT node_id FROM nodes")]
        edges = [r[0] for r in conn.execute("SELECT edge_id FROM edges")]
        claims = {r[0]: r[1] for r in conn.execute("SELECT claim_id, status FROM claims")}
    finally:
        conn.close()
    return {
        "snapshot_hash": _stored_snapshot_hash(root),
        "node_ids": sorted(nodes),
        "edge_ids": sorted(edges),
        "claims": claims,
    }


def diff_graph_snapshots(old_snapshot: dict, new_snapshot: dict) -> dict:
    """Deterministic diff of two captured graph states (from capture_graph_state)."""
    old_nodes = set(old_snapshot.get("node_ids") or [])
    new_nodes = set(new_snapshot.get("node_ids") or [])
    old_edges = set(old_snapshot.get("edge_ids") or [])
    new_edges = set(new_snapshot.get("edge_ids") or [])
    old_claims = dict(old_snapshot.get("claims") or {})
    new_claims = dict(new_snapshot.get("claims") or {})

    # superseded: an ACTIVE old claim that is gone, or no longer active, in the new graph.
    # (The ledger is append-only, so this is normally empty; it exists for status changes.)
    claims_superseded = sorted(
        cid
        for cid, status in old_claims.items()
        if status == "active" and new_claims.get(cid, "superseded") != "active"
    )

    return {
        "old_snapshot_hash": str(old_snapshot.get("snapshot_hash") or ""),
        "new_snapshot_hash": str(new_snapshot.get("snapshot_hash") or ""),
        "nodes_added": sorted(new_nodes - old_nodes),
        "nodes_removed": sorted(old_nodes - new_nodes),
        "edges_added": sorted(new_edges - old_edges),
        "edges_removed": sorted(old_edges - new_edges),
        "claims_added": sorted(set(new_claims) - set(old_claims)),
        "claims_superseded": claims_superseded,
    }
