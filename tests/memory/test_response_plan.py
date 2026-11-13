"""Subject-centered response planner — groups verified recall winners so the model
narrates the connected picture (Noah owns Omega AI AND his son is Abel) instead of
reciting one fact. Buckets: DIRECT / RELATIONAL / EPISODIC / CONFLICTING (operator spec)."""

from __future__ import annotations

from auto_loop.mril_tools.response_plan import (
    build_response_plan,
    render_response_plan,
    resolve_query_subject,
    resolve_query_subjects,
)


def _item(
    mid,
    *,
    coord=(1, 0, 0),
    title="",
    remembered_as="",
    facets=None,
    confidence=0.9,
    conflicting=False,
):
    return {
        "memory_id": mid,
        "coord": list(coord),
        "title": title,
        "remembered_as": remembered_as,
        "relation_facets": list(facets or []),
        "confidence": confidence,
        "conflicting": conflicting,
    }


def _facet(subject, facet, relation, obj):
    return {"subject": subject, "facet": facet, "relation": relation, "object": obj}


def test_operator_self_facts_group_as_direct():
    items = [
        _item(
            "1.00.a",
            title="Operator son",
            remembered_as="Noah's son is Abel.",
            facets=[_facet("operator", "relationship", "son", "abel")],
        ),
        _item(
            "1.00.b",
            title="Operator business",
            remembered_as="Noah owns Omega AI.",
            facets=[_facet("operator", "business", "business", "omega ai")],
        ),
    ]
    plan = build_response_plan(items, subject="operator")
    assert len(plan["direct"]) == 2
    assert plan["relational"] == []
    # the connected picture must expose BOTH relations for narration
    objs = {f["object"] for d in plan["direct"] for f in d["relation_facets"]}
    assert objs == {"abel", "omega ai"}


def test_third_person_fact_is_relational_not_direct_on_operator_query():
    # "Abel likes trains" (subject=abel) on a Noah/operator query -> RELATIONAL, never DIRECT.
    items = [
        _item(
            "1.00.a",
            title="Operator son",
            remembered_as="Noah's son is Abel.",
            facets=[_facet("operator", "relationship", "son", "abel")],
        ),
        _item(
            "1.01.abel",
            title="Abel likes trains",
            remembered_as="Abel likes trains.",
            facets=[_facet("abel", "preference", "preference", "trains")],
        ),
    ]
    plan = build_response_plan(items, subject="operator")
    direct_ids = {d["memory_id"] for d in plan["direct"]}
    relational_ids = {r["memory_id"] for r in plan["relational"]}
    assert "1.00.a" in direct_ids
    assert "1.01.abel" in relational_ids


def test_topical_no_facet_records_are_direct():
    # A knowledge hit (no relation facets) is a direct topical hit, not relational.
    items = [
        _item(
            "2.00.py",
            coord=(2, 0, 1),
            title="Python",
            remembered_as="A programming language",
            facets=[],
        )
    ]
    plan = build_response_plan(items, subject="operator")
    assert [d["memory_id"] for d in plan["direct"]] == ["2.00.py"]
    assert plan["relational"] == [] and plan["episodic"] == []


def test_episode_lobe_records_are_episodic():
    items = [
        _item(
            "9.00.ep",
            coord=(9, 0, 3),
            title="Yesterday",
            remembered_as="We talked about trains yesterday.",
        )
    ]
    plan = build_response_plan(items, subject="operator")
    assert [e["memory_id"] for e in plan["episodic"]] == ["9.00.ep"]
    assert plan["direct"] == []


def test_conflicting_records_bucket_and_never_pollute_direct():
    items = [
        _item(
            "1.00.name1",
            title="Operator name",
            remembered_as="The operator's name is Noah.",
            facets=[_facet("operator", "name", "name", "noah")],
        ),
        _item(
            "1.00.name2",
            title="Operator name",
            remembered_as="The operator's name is Kim.",
            facets=[_facet("operator", "name", "name", "kim")],
            conflicting=True,
        ),
    ]
    plan = build_response_plan(items, subject="operator")
    assert [c["memory_id"] for c in plan["conflicting"]] == ["1.00.name2"]
    assert "1.00.name2" not in {d["memory_id"] for d in plan["direct"]}


def test_resolve_query_subject_first_person_is_operator():
    assert resolve_query_subject("what do you remember about me", []) == "operator"
    assert resolve_query_subject("tell me about my son", []) == "operator"


def test_resolve_query_subject_named_person_from_items():
    items = [_item("1.01.abel", facets=[_facet("abel", "preference", "preference", "trains")])]
    assert resolve_query_subject("what does abel like", items) == "abel"


def test_named_entity_wins_over_incidental_first_person_pronoun():
    # "tell me about Abel" is ABOUT Abel — the incidental indirect-object "me" must NOT
    # short-circuit to operator and demote Abel's own facts to background (Slice 4 regression).
    items = [_item("1.01.abel", facets=[_facet("abel", "preference", "preference", "trains")])]
    assert resolve_query_subject("tell me about abel", items) == "abel"
    assert resolve_query_subject("what can you tell me about my son abel", items) == "abel"


def test_multi_person_comparison_keeps_every_named_subject_direct():
    items = [
        _item(
            "1.01.mitchell-owner",
            remembered_as="Mitchell Ray is part owner of Omega AI LLC.",
            facets=[
                _facet("mitchell ray", "organization_role", "part owner", "omega ai llc")
            ],
        ),
        _item(
            "1.00.noah-owner",
            remembered_as="Noah Lee is part owner of Omega AI LLC.",
            facets=[_facet("operator", "organization_role", "part owner", "omega ai llc")],
        ),
    ]
    query = "What do Mitchell and Noah do at Omega AI?"

    subjects = resolve_query_subjects(query, items)
    assert set(subjects) == {"mitchell ray", "operator"}

    plan = build_response_plan(items, subject=subjects)
    assert {item["memory_id"] for item in plan["direct"]} == {
        "1.01.mitchell-owner",
        "1.00.noah-owner",
    }
    assert plan["relational"] == []
    assert "background only" not in render_response_plan(plan, query=query)


def test_self_query_stays_operator_when_no_recalled_entity_is_named():
    # A first-person query that names no recalled subject is still the operator, even when an
    # unrelated entity fact is co-recalled (the entity name is absent from the query tokens).
    items = [_item("1.01.abel", facets=[_facet("abel", "preference", "preference", "trains")])]
    assert resolve_query_subject("what is my name", items) == "operator"


def test_render_weaves_direct_facts_into_one_subject_block():
    items = [
        _item(
            "1.00.a",
            remembered_as="Noah's son is Abel.",
            facets=[_facet("operator", "relationship", "son", "abel")],
        ),
        _item(
            "1.00.b",
            remembered_as="Noah owns Omega AI.",
            facets=[_facet("operator", "business", "business", "omega ai")],
        ),
    ]
    plan = build_response_plan(items, subject="operator")
    block = render_response_plan(plan, query="tell me about noah")
    # both facts must appear in the rendered block so the model can weave them
    assert "abel" in block.lower()
    assert "omega ai" in block.lower()


def test_render_subordinates_relational_facts_under_a_context_header():
    # Abel's own fact on a Noah query is kept but SUBORDINATED (background, attributed) so it
    # can never masquerade as a fact about Noah — and is never silently deleted.
    items = [
        _item(
            "1.00.a",
            remembered_as="Noah's son is Abel.",
            facets=[_facet("operator", "relationship", "son", "abel")],
        ),
        _item(
            "1.01.abel",
            remembered_as="Abel likes trains.",
            facets=[_facet("abel", "preference", "preference", "trains")],
        ),
    ]
    plan = build_response_plan(items, subject="operator")
    block = render_response_plan(plan, query="tell me about noah")
    main, _, related = block.partition("Related context")
    assert "abel" in main.lower()  # the son fact is in the main woven block
    assert "trains" not in main.lower()  # Abel's own fact is NOT in the main block
    assert "trains" in related.lower()  # it is kept, subordinated + attributed
    assert "(about abel)" in related.lower()


def test_render_empty_plan_is_empty_string():
    plan = build_response_plan([], subject="operator")
    assert render_response_plan(plan, query="anything") == ""


def test_all_relational_facts_are_the_answer_not_background():
    # Pronoun-only third-person query ("my sister") names no entity -> subject resolves to
    # operator -> the sister's own facts are the ONLY recall. They must be presented as the
    # answer (attributed), never demoted to "background only" (which would leave an EMPTY main
    # answer and a self-contradictory block).
    items = [
        _item(
            "1.02.mir",
            remembered_as="Miranda plays cello.",
            facets=[_facet("miranda", "preference", "preference", "cello")],
        ),
    ]
    plan = build_response_plan(items, subject="operator")
    assert plan["direct"] == [] and plan["relational"]  # precondition: all-relational
    block = render_response_plan(plan, query="what is my sister into")
    assert "cello" in block.lower()  # the sole fact IS the answer
    assert "background only" not in block.lower()  # never demoted
    assert "(about miranda)" in block.lower()  # still attributed


def test_all_relational_answer_is_not_capped():
    # When related-entity facts ARE the answer (no direct/episodic), none may be silently
    # dropped past the subordination cap — the flat fallback would have grounded them all.
    items = [
        _item(
            f"1.0{i}.x",
            remembered_as=f"Fact number {i}.",
            facets=[_facet("miranda", "preference", "preference", f"thing{i}")],
        )
        for i in range(7)
    ]
    plan = build_response_plan(items, subject="operator")
    block = render_response_plan(plan, query="what is my sister into").lower()
    for i in range(7):
        assert f"thing{i}" in block or f"number {i}" in block
