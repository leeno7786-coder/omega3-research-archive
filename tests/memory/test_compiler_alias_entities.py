"""Task #13 — compiler alias edge creation (Gate-1 autopsy findings, 2026-07-04).

Autopsy evidence (scripts/memory_alias_autopsy.py, fresh isolated store):
  1. BOTH failed compiler-linked alias pairs were lobe 11 — RECALLABLE_LOBES
     omitted the dev lobe, so L11 records got NO graph nodes and NO Dewey
     edges (aliasL structurally capped at 8/10; engine-mode direct hit@1
     capped at 20/24). Hyphen-digit tokenization was exonerated (Aegir-9
     linked; "Fathom" failed with no hyphen in sight).
  2. Alias pairs linked only via CO_LOCATED (same subdomain placement) — a
     coincidence of the corpus, not an alias mechanism. The ratified spec
     demands same_entity/alias_of STRONG edges: the compiler must extract
     named entities from record text and link memory—entity—memory.

Contract: extraction is deterministic + model-free (proper-name grammar);
entity ids are content-derived so repeated mentions converge on ONE node;
the hop engine bridges through entity nodes with a degree cap so degenerate
hubs never saturate the plane.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# ── extraction grammar (memory/taxonomy/canonicalize.py) ─────────────────────


class TestExtractNamedEntities:
    def _x(self, text: str) -> tuple:
        from memory.taxonomy.canonicalize import extract_named_entities

        return extract_named_entities(text)

    def test_hyphen_digit_compound(self):
        assert "aegir-9" in self._x("the Aegir-9 pump controller regulates ballast flow")

    def test_hyphen_digit_lowercase_still_extracts(self):
        # casual chat is often lowercase; hyphen-digit stays a strong signal
        assert "delta-4" in self._x("the delta-4 build emitted the sounding bundle")

    def test_capitalized_proper_name(self):
        assert "meridian" in self._x("the Meridian scope tracks vessel headings")

    def test_sentence_initial_name_extracts(self):
        assert "ingrid" in self._x("Ingrid maintains the harbor master's ledger")

    def test_stopwords_never_extract(self):
        got = self._x("The crew said We should go. What matters is Tuesday.")
        assert got == ()

    def test_plain_lowercase_prose_yields_nothing(self):
        assert self._x("the tide chart tool predicts the harbor windows") == ()

    def test_acronym_extracts(self):
        assert "npu" in self._x("the DME runs on the NPU overnight")

    def test_mixed_case_extracts(self):
        assert "iphone" in self._x("she synced the iPhone backup last night")

    def test_multiword_run_emits_compound_and_members(self):
        got = self._x("the Kindle Paperwhite arrived on the dock")
        assert "kindle paperwhite" in got
        assert "kindle" in got

    def test_sorted_and_deduped(self):
        got = self._x("Aegir-9 feeds Aegir-9 and the Meridian scope")
        assert got == tuple(sorted(set(got)))

    def test_deterministic(self):
        text = "the Aegir-9 and the Kindle Paperwhite met the Meridian"
        assert self._x(text) == self._x(text)


# ── store scaffolding (offline: records + coord_index written directly) ─────


def _store(tmp_path: Path, records: list[tuple[str, list[int], str, str]]) -> Path:
    mem = tmp_path / "memory"
    (mem / "indexes").mkdir(parents=True, exist_ok=True)
    index = {}
    for mid, coord, title, summary in records:
        p = mem / f"{mid}.json"
        p.write_text(
            json.dumps(
                {"id": mid, "coord": coord, "title": title, "summary": summary, "type": "semantic"}
            ),
            encoding="utf-8",
        )
        index[mid] = {"id": mid, "coord": coord, "path": str(p), "title": title}
    (mem / "indexes" / "coord_index.json").write_text(json.dumps(index), encoding="utf-8")
    return tmp_path


def _compile(root: Path):
    from memory.taxonomy.compiler import compile_memory_graph

    return compile_memory_graph(root=root)


def _edges_between(root: Path, a: str, b: str) -> set:
    from memory.taxonomy import graph_store as G

    conn = G.connect(root)
    try:
        rows = conn.execute(
            "SELECT edge_type FROM edges WHERE "
            "(source_id=? AND target_id=?) OR (source_id=? AND target_id=?)",
            (a, b, b, a),
        ).fetchall()
        return {r["edge_type"] for r in rows}
    finally:
        conn.close()


# ── the ablation failure, minimal (lobe 11 must have graph presence) ─────────


class TestDevLobeGraphPresence:
    def test_l11_records_become_nodes_with_co_located_edges(self, tmp_path):
        root = _store(
            tmp_path,
            [
                (
                    "11.00.aaaa1111",
                    [11, 0, 0],
                    "lnk-t",
                    "the Delta-4 build emitted the sounding bundle",
                ),
                (
                    "11.00.bbbb2222",
                    [11, 0, 1],
                    "lnk-b",
                    "the Delta-4 output is nicknamed the depth pack",
                ),
            ],
        )
        _compile(root)
        from memory.taxonomy import graph_store as G

        conn = G.connect(root)
        try:
            n = conn.execute(
                "SELECT COUNT(*) c FROM nodes WHERE node_type='memory_record'"
            ).fetchone()["c"]
        finally:
            conn.close()
        assert n == 2, "dev-lobe records must project into the graph"
        assert "CO_LOCATED" in _edges_between(root, "11.00.aaaa1111", "11.00.bbbb2222")

    def test_l11_pair_is_hop_adjacent(self, tmp_path):
        root = _store(
            tmp_path,
            [
                ("11.01.cccc3333", [11, 1, 0], "t", "the Fathom sensor line still needs splicing"),
                (
                    "11.01.dddd4444",
                    [11, 1, 1],
                    "b",
                    "the Fathom line is the depth string under pier one",
                ),
            ],
        )
        _compile(root)
        from memory.dme.hop_engine import neighbor_map_from_graph

        nm = neighbor_map_from_graph(root, ["11.01.cccc3333", "11.01.dddd4444"])
        assert "11.01.dddd4444" in nm.get("11.01.cccc3333", [])


# ── the alias lane proper: memory—entity—memory, independent of placement ───


class TestEntityMediatedAliasBridge:
    def test_shared_entity_bridges_across_subdomains(self, tmp_path):
        # different y => no CO_LOCATED, no SHARES_*; only the entity can join them
        root = _store(
            tmp_path,
            [
                (
                    "5.00.eeee5555",
                    [5, 0, 0],
                    "t",
                    "the Aegir-9 pump controller regulates ballast flow",
                ),
                (
                    "5.03.ffff6666",
                    [5, 3, 0],
                    "b",
                    "the crew calls the Aegir-9 the ballast balancer",
                ),
            ],
        )
        _compile(root)
        from memory.dme.hop_engine import neighbor_map_from_graph

        nm = neighbor_map_from_graph(root, ["5.00.eeee5555", "5.03.ffff6666"])
        assert "5.03.ffff6666" in nm.get("5.00.eeee5555", []), (
            "shared named entity must bridge memories regardless of Y placement"
        )

    def test_mentions_edge_to_named_entity_node_exists(self, tmp_path):
        root = _store(
            tmp_path,
            [
                (
                    "5.00.eeee5555",
                    [5, 0, 0],
                    "t",
                    "the Aegir-9 pump controller regulates ballast flow",
                ),
            ],
        )
        _compile(root)
        from memory.taxonomy import graph_store as G

        conn = G.connect(root)
        try:
            row = conn.execute(
                "SELECT n.node_id FROM nodes n JOIN edges e ON e.target_id = n.node_id "
                "WHERE e.source_id=? AND e.edge_type='MENTIONS' AND n.node_type='named_entity'",
                ("5.00.eeee5555",),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None, "record must MENTION a named_entity node"

    def test_short_name_bridges_to_full_title(self, tmp_path):
        # ratified: entity title<->short-name links
        root = _store(
            tmp_path,
            [
                ("2.00.aaaa7777", [2, 0, 0], "t", "the Kindle Paperwhite arrived on the dock"),
                ("2.05.bbbb8888", [2, 5, 0], "b", "she reads the Kindle every evening"),
            ],
        )
        _compile(root)
        from memory.dme.hop_engine import neighbor_map_from_graph

        nm = neighbor_map_from_graph(root, ["2.00.aaaa7777", "2.05.bbbb8888"])
        assert "2.05.bbbb8888" in nm.get("2.00.aaaa7777", [])

    def test_no_shared_entity_no_bridge(self, tmp_path):
        # the ISOLATED group's contract: no token bridge => stays unlinked
        root = _store(
            tmp_path,
            [
                ("5.00.cccc9999", [5, 0, 0], "t", "the salinity probe samples the estuary hourly"),
                ("5.03.dddd0000", [5, 3, 0], "b", "the manifest pipeline stamps every cargo entry"),
            ],
        )
        _compile(root)
        from memory.dme.hop_engine import neighbor_map_from_graph

        nm = neighbor_map_from_graph(root, ["5.00.cccc9999", "5.03.dddd0000"])
        assert "5.03.dddd0000" not in nm.get("5.00.cccc9999", [])

    def test_hub_degree_cap_blocks_degenerate_mediators(self, tmp_path):
        # a name shared by MANY memories must not saturate the plane
        recs = [
            (
                f"2.0{i}.hub{i}{i}{i}{i}",
                [2, i, 0],
                f"r{i}",
                f"Aegir-9 note number {i} in a different subdomain",
            )
            for i in range(5)
        ]
        root = _store(tmp_path, recs)
        _compile(root)
        from memory.dme.hop_engine import neighbor_map_from_graph

        ids = [r[0] for r in recs]
        nm = neighbor_map_from_graph(root, ids, bridge_degree_cap=3)
        assert nm == {}, "mediator above the degree cap must not bridge"


class TestCompileDeterminism:
    def test_snapshot_hash_stable_across_recompiles(self, tmp_path):
        root = _store(
            tmp_path,
            [
                (
                    "5.00.eeee5555",
                    [5, 0, 0],
                    "t",
                    "the Aegir-9 pump controller regulates ballast flow",
                ),
                (
                    "5.03.ffff6666",
                    [5, 3, 0],
                    "b",
                    "the crew calls the Aegir-9 the ballast balancer",
                ),
                (
                    "11.00.aaaa1111",
                    [11, 0, 0],
                    "d",
                    "the Delta-4 build emitted the sounding bundle",
                ),
            ],
        )
        first = _compile(root).snapshot_hash
        second = _compile(root).snapshot_hash
        assert first == second
