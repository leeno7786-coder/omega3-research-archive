"""Named relation edges from record enrichment — the graph's semantic wiring."""

from __future__ import annotations

from memory.taxonomy.extract_persona import relation_graph_from_record


def _op_id(entities):
    return next(e.entity_id for e in entities if e.entity_kind == "operator")


def test_relation_graph_projects_named_edges():
    meta = {
        "enrichment": {
            "entities": [
                {"name": "son", "facet": "relationship", "linked_name": "abel"},
                {"name": "omega ai", "facet": "business"},
                {"name": "cybersecurity", "facet": "education"},
            ]
        }
    }
    ents, edges = relation_graph_from_record("1.00.noah", meta)

    kinds = {(e.entity_kind, e.canonical_name) for e in ents}
    assert ("operator", "operator") in kinds
    assert ("named_entity", "abel") in kinds
    assert ("named_entity", "omega ai") in kinds

    by_type = {e.edge_type: e for e in edges}
    assert "FAMILY_OF" in by_type
    assert by_type["FAMILY_OF"].payload["relation"] == "son"  # specific relation preserved
    assert "OWNS" in by_type
    assert "STUDIES" in by_type
    # every relation edge originates at the operator entity + cites the record;
    # the anchor MENTIONS edge connects the record to that same operator entity.
    op = _op_id(ents)
    rel_edges = [e for e in edges if e.edge_type != "MENTIONS"]
    assert rel_edges and all(e.source_id == op for e in rel_edges)
    assert all(e.evidence_event_id == "dewey:1.00.noah" for e in edges)
    anchor = [e for e in edges if e.edge_type == "MENTIONS"]
    assert anchor and anchor[0].source_id == "1.00.noah" and anchor[0].target_id == op


def test_deterministic_ids():
    meta = {
        "enrichment": {
            "entities": [{"name": "son", "facet": "relationship", "linked_name": "abel"}]
        }
    }
    a = relation_graph_from_record("1.00.noah", meta)
    b = relation_graph_from_record("1.00.noah", meta)
    assert [e.edge_id for e in a[1]] == [e.edge_id for e in b[1]]


def test_subject_centric_third_person_hangs_off_the_person():
    # "Abel likes trains" (subject=abel) → the LIKES edge originates at person:abel, NOT
    # the operator, so Abel's own facts are one hop from ABEL (two from Noah -> low weight).
    meta = {
        "enrichment": {
            "entities": [
                {"name": "trains", "facet": "preference", "subject": "abel"},
            ]
        }
    }
    ents, edges = relation_graph_from_record("1.01.abel", meta)
    likes = next(e for e in edges if e.edge_type == "LIKES")
    abel = next(e for e in ents if e.entity_kind == "named_entity" and e.canonical_name == "abel")
    assert likes.source_id == abel.entity_id
    assert likes.target_id != abel.entity_id
    # and the operator node is NOT the subject of Abel's own fact.
    assert not any(e.entity_kind == "operator" for e in ents)


def test_operator_self_fact_still_hangs_off_operator():
    meta = {
        "enrichment": {
            "entities": [
                {"name": "omega ai", "facet": "business", "subject": "operator"},
            ]
        }
    }
    ents, edges = relation_graph_from_record("1.00.noah", meta)
    owns = next(e for e in edges if e.edge_type == "OWNS")
    op = next(e for e in ents if e.entity_kind == "operator")
    assert owns.source_id == op.entity_id


def test_name_fact_aliases_subject_to_its_name():
    # "operator's name is noah" → person:noah SAME_AS operator, both directions, so a NAME
    # query can reach the pivot the relations hang off.
    meta = {"enrichment": {"entities": [{"name": "noah", "facet": "name", "subject": "operator"}]}}
    ents, edges = relation_graph_from_record("1.00.opname", meta)
    same = [e for e in edges if e.edge_type == "SAME_AS"]
    assert same, "a name fact must create a SAME_AS alias"
    op = next(e for e in ents if e.entity_kind == "operator")
    noah = next(e for e in ents if e.entity_kind == "named_entity" and e.canonical_name == "noah")
    pairs = {(e.source_id, e.target_id) for e in same}
    assert (noah.entity_id, op.entity_id) in pairs  # name → pivot
    assert (op.entity_id, noah.entity_id) in pairs  # bidirectional


def test_a_persons_own_name_is_not_self_aliased():
    # a related person's own name (subject == name) is a no-op self-alias.
    meta = {"enrichment": {"entities": [{"name": "abel", "facet": "name", "subject": "abel"}]}}
    _ents, edges = relation_graph_from_record("x", meta)
    assert not any(e.edge_type == "SAME_AS" for e in edges)


def test_non_relational_records_are_noop():
    # no enrichment, and an unmapped facet (age) → nothing to project. (A NAME fact is NOT
    # a no-op — it aliases the subject to its name; covered by the SAME_AS tests above.)
    assert relation_graph_from_record("x", {}) == ([], [])
    assert relation_graph_from_record(
        "x", {"enrichment": {"entities": [{"name": "33", "facet": "age"}]}}
    ) == ([], [])


def test_persona_relations_land_in_the_compiled_graph(tmp_path):
    """End to end: a persona record's parsed relations become NAMED graph edges when the
    filesystem is compiled — the graph is finally the semantic recall plane."""
    import json

    from memory.taxonomy import graph_store as G
    from memory.taxonomy.compiler import compile_memory_graph

    mem = tmp_path / "memory"
    (mem / "indexes").mkdir(parents=True)
    (mem / "indexes" / "coord_index.json").write_text(
        json.dumps({"1.00.noah": {"coord": [1, 0, 0], "path": "memory/noah.json"}})
    )
    (mem / "noah.json").write_text(
        json.dumps(
            {
                "id": "1.00.noah",
                "coord": [1, 0, 0],
                "type": "user_profile",
                "title": "Operator son: abel",
                "summary": "Noah Lee's son is Abel. The operator's son is Abel.",
                "text": "Noah Lee's son is Abel. The operator's son is Abel.",
                "enrichment": {
                    "entities": [
                        {"name": "son", "facet": "relationship", "linked_name": "abel"},
                        {"name": "omega ai", "facet": "business"},
                    ]
                },
            }
        )
    )

    compile_memory_graph(root=tmp_path)
    conn = G.connect(root=tmp_path)
    try:
        edges = conn.execute("SELECT source_id, target_id, edge_type FROM edges").fetchall()
        types = {e["edge_type"] for e in edges}
        assert "FAMILY_OF" in types, f"no semantic relation edge compiled; got {types}"
        assert "OWNS" in types
        # the source record is anchored to the operator entity the relations hang off.
        operator_id = next(e["source_id"] for e in edges if e["edge_type"] == "FAMILY_OF")
        assert any(
            e["edge_type"] == "MENTIONS"
            and e["source_id"] == "1.00.noah"
            and e["target_id"] == operator_id
            for e in edges
        )
    finally:
        conn.close()
