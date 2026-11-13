"""D-13-34/41/49 — publication lineage framing/chain/crash order on real publications."""

from __future__ import annotations

from memory.projection import generation as G
from tests.memory.projection._fixtures import clock, publish


def test_lineage_rows_chain_across_real_publications(tmp_path):
    root = tmp_path / "mem"
    r1 = publish(root, clock(100)); r2 = publish(root, clock(101), compiler_version="v8"); r3 = publish(root, clock(101))
    proj = G.projection_root(root)
    scan = G.read_publication_lineage(proj / G.LINEAGE_FILE)
    assert [r.event for r in scan.rows] == ["published_new", "published_new", "reactivated_retained"]
    assert scan.rows[0].previous_row_sha256 == G.GENESIS_ROW_SHA256
    assert scan.rows[1].previous_row_sha256 == G.row_sha256(scan.rows[0]) and scan.rows[2].previous_row_sha256 == G.row_sha256(scan.rows[1])
    assert scan.rows[1].parent_generation_id == r1.generation_id and scan.rows[2].parent_generation_id == r2.generation_id
    raw = (proj / G.LINEAGE_FILE).read_bytes()
    assert raw.endswith(b"\n") and raw.count(b"\n") == 3
    m = G.read_active_manifest(proj)
    assert m.lineage_seq == 3 and m.generation_id == r3.generation_id and m.coverage_clock == clock(101)
