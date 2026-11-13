"""First-wake onboarding seed: occupation -> lobe routing; subcategories ->
dynamic subdomains + mastery gaps; the flag flips to {2,8}."""

import importlib
from types import SimpleNamespace

import pytest
from deepsleep_loop.mastery_store import load_mastery, normalize_domain_id

pytestmark = pytest.mark.usefixtures("deepsleep_sram_drain")


def test_occupation_lobe_routing():
    from hud.operator_onboarding import lobe_for_occupation

    assert lobe_for_occupation("software_engineer") == 8
    assert lobe_for_occupation("Software Engineer") == 8
    assert lobe_for_occupation("Data Scientist / ML Engineer") == 8
    assert lobe_for_occupation("Doctor / Healthcare") == 2
    assert lobe_for_occupation("Lawyer") == 2
    # free-text / unknown occupation -> knowledge (the "rest through knowledge" rule)
    assert lobe_for_occupation("astronaut") == 2
    assert lobe_for_occupation("") == 2


def _isolate(tmp_path, monkeypatch):
    root = tmp_path / "omega_root"
    root.mkdir()
    import core.paths as paths

    monkeypatch.setattr(paths, "OMEGA_ROOT", root)
    mastery_path = root / "mastery.json"
    monkeypatch.setattr(
        "pressure_loop.policy.deepsleep_mastery_path",
        lambda policy=None: mastery_path,
    )
    importlib.import_module("memory.knowledge_router")
    from memory.subdomain_flag import reset_flag_cache

    reset_flag_cache()
    # Legacy decomposer remains inert; current child planning runs in Omega foldback.
    monkeypatch.setattr("hud.onboarding_subtopics.decompose_subtopics", lambda *a, **k: {})
    return root, mastery_path


def test_seed_first_wake_flips_flag_routes_and_seeds_gaps(tmp_path, monkeypatch):
    root, mastery_path = _isolate(tmp_path, monkeypatch)
    from hud.operator_onboarding import seed_first_wake
    from memory.subdomain_flag import dynamic_y_lobes

    result = seed_first_wake("software_engineer", ["Python", "System Design"])

    # First-wake flip persisted + visible.
    assert dynamic_y_lobes() == frozenset({2, 8})
    # result["lobe"] is occupation-level metadata (SWE -> coding occupation).
    assert result["lobe"] == 8
    assert len(result["seeded"]) == 2
    assert not result["failed"]

    # PER-TOPIC discrimination (the operator's fix): the occupation NO LONGER forces the
    # lobe. "Python" is coding (lobe 8); "System Design" is knowledge (lobe 2) — a SWE's
    # set is not all-coding. Both lobe dirs get created.
    by_sub = {s["subcategory"]: s["lobe"] for s in result["seeded"]}
    assert by_sub["Python"] == 8
    assert by_sub["System Design"] == 2
    assert (root / "memory" / "800_coding").exists()
    assert (root / "memory" / "200_knowledge").exists()

    # Each seeded subcategory is a justified mastery GAP (target>0, actual=0).
    records = load_mastery(mastery_path)
    pid = normalize_domain_id("Python")
    assert pid in records
    assert records[pid].knowledge_mastery == 0.0
    assert records[pid].target_mastery > 0.0


def test_seed_first_wake_defers_child_decomposition_until_foldback(tmp_path, monkeypatch):
    """Saving onboarding is deterministic and fast; model-driven child planning belongs
    to the first episode's Omega foldback, not the HTTP settings request."""
    root, mastery_path = _isolate(tmp_path, monkeypatch)
    import hud.operator_onboarding as ob

    model_calls = []
    monkeypatch.setattr(
        "hud.onboarding_subtopics.decompose_subtopics",
        lambda *a, **k: model_calls.append((a, k)) or {},
    )

    result = ob.seed_first_wake("software_engineer", ["Python", "Algorithms"])

    assert model_calls == []
    assert "subtopics" not in result

    children = ob.seed_foldback_subtopics(
        {
            "occupation": "software_engineer",
            "categories": result["seeded"],
        },
        [
            {"parent": "Python", "subtopics": ["asyncio", "decorators"]},
            {"parent": "Algorithms", "subtopics": ["Big-O analysis", "Graph traversal"]},
        ],
    )

    by_sub = {s["subtopic"]: s for s in children}
    # Children created UNDER their category, in the category's OWN lobe.
    assert by_sub["asyncio"]["parent"] == "Python"
    assert by_sub["asyncio"]["lobe"] == 8  # Python -> coding
    assert by_sub["Big-O analysis"]["parent"] == "Algorithms"
    assert by_sub["Big-O analysis"]["lobe"] == 2  # Algorithms -> knowledge
    # The child subdomain dirs exist under the right lobes.
    assert (root / "memory" / "800_coding" / "asyncio").exists()

    # Each subtopic is a learnable mastery GAP (target>0, actual=0) linked UNDER its parent.
    records = load_mastery(mastery_path)
    child = records[normalize_domain_id("asyncio")]
    assert child.knowledge_mastery == 0.0
    assert child.target_mastery > 0.0
    assert child.parent == normalize_domain_id("Python")
    # The parent category's mastery record lists the child in its subdomains tree.
    parent = records[normalize_domain_id("Python")]
    assert normalize_domain_id("asyncio") in parent.subdomains


def test_seed_first_wake_writes_operator_linked_topic_leaves(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    import hud.operator_onboarding as ob

    written = []
    monkeypatch.setattr(
        ob,
        "_write_work_topic_leaf",
        lambda **kwargs: written.append(kwargs) or f"memory-{len(written)}",
        raising=False,
    )

    citation = {"source_id": "src-onboarding", "source_type": "operator_onboarding"}
    result = ob.seed_first_wake(
        "software_engineer",
        ["Python", "System Design"],
        source_citation=citation,
    )

    assert [row["topic"] for row in written] == ["Python", "System Design"]
    assert {row["lobe"] for row in written} == {2, 8}
    assert all(row["operator_id"] == "operator" for row in written)
    assert all(row["source_citation"] == citation for row in written)
    assert [row["memory_id"] for row in result["seeded"]] == ["memory-1", "memory-2"]


def test_work_topic_leaf_has_operator_tag_and_entity_link_key(monkeypatch):
    import hud.operator_onboarding as ob

    captured = []
    monkeypatch.setattr(
        "memory.api.write_memory",
        lambda envelope, **kwargs: (
            captured.append((envelope, kwargs))
            or SimpleNamespace(
                validation_status="committed",
                path=envelope.id,
                message="ok",
            )
        ),
    )

    citation = {"source_id": "src-onboarding", "source_type": "operator_onboarding"}
    memory_id = ob._write_work_topic_leaf(
        topic="System Design",
        occupation="software_engineer",
        lobe=2,
        y=4,
        operator_id="operator",
        source_citation=citation,
    )

    envelope, write_kwargs = captured[0]
    assert memory_id == envelope.id
    assert "operator:operator" in envelope.payload["tags"]
    # entity_linker reads enrichment.linked_persons, so the operator relationship
    # must be a real graph link key in addition to a searchable leaf tag.
    assert envelope.payload["enrichment"]["linked_persons"] == ["operator"]
    assert envelope.payload["source_id"] == "src-onboarding"
    assert write_kwargs == {
        "propose": False,
        "bypass_guardian": True,
        "skip_embed": True,
    }


def test_seed_knowledge_occupation_routes_to_lobe_2(tmp_path, monkeypatch):
    root, mastery_path = _isolate(tmp_path, monkeypatch)
    from hud.operator_onboarding import seed_first_wake

    result = seed_first_wake("Doctor / Healthcare", ["Cardiology"])
    assert result["lobe"] == 2
    assert len(result["seeded"]) == 1
    assert (root / "memory" / "200_knowledge").exists()
    assert normalize_domain_id("Cardiology") in load_mastery(mastery_path)


def test_seed_dedups_and_skips_blank(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hud.operator_onboarding import seed_first_wake

    result = seed_first_wake("writer", ["Editing", "editing", "  ", ""])
    assert len(result["seeded"]) == 1  # deduped + blanks skipped
