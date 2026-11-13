"""Persona-statement fan-out orchestrator — Slice 2 of the persona-routing
decompose fix. Confirms a personal statement decomposes into DISCRETE,
correctly-routed, leaf-tagged records instead of one monolithic blob, and
that every write lands only in the isolated tmp store, never the live
`memory/` tree.

Isolation: `tests/memory/conftest.py` installs the shared
`redirect_memory_to_tmp` autouse fixture (covers `memory.api.write_memory`'s
whole write chain). `core.persona_router` resolves its OWN persona
directory + `guardian/state/persona_index.json` from module-level
`OMEGA_ROOT` / `_INDEX_PATH` globals that the shared fixture does NOT
patch (confirmed by reading `core/persona_router.py` and the existing
isolation pattern in `tests/core/test_persona_router_fallback.py`), so
this suite patches those two attributes directly on `pr` as well.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

CANONICAL_TEXT = (
    "I'm Noah, I have a sister named Miranda, a son named Abel, "
    "and I go to college for cybersecurity."
)


@pytest.fixture(autouse=True)
def _isolate_persona_router(tmp_path, monkeypatch):
    """Redirect core.persona_router's own persona-dir + index globals to tmp.

    tests/memory/conftest.py already redirects memory.api's write chain
    (MEMORY_ROOT, coord_index, etc.) for every test in this directory.
    persona_router additionally owns two globals the shared fixture
    doesn't touch: OMEGA_ROOT (used by _ensure_persona_dir to build the
    on-disk person_stub path) and _INDEX_PATH (the durable
    guardian/state/persona_index.json catalog). Left un-patched, a real
    route_persona() call would create real directories under the live
    repo's memory/100_personas/ and write to the live persona_index.json.
    """
    import core.persona_router as pr

    monkeypatch.setattr(pr, "OMEGA_ROOT", tmp_path)
    monkeypatch.setattr(pr, "_INDEX_PATH", tmp_path / "guardian" / "state" / "persona_index.json")


def _load(path: str) -> dict:
    # Receipt paths are profile-relative by design; resolve like recall does.
    from memory.engine.walker import resolve_coord_index_path

    resolved = resolve_coord_index_path(path) or Path(path)
    return json.loads(Path(resolved).read_text(encoding="utf-8"))


def _operator_citation(text: str, *, cycle_id: str, timestamp: str) -> dict:
    from memory.source_ledger.store import record_operator_chat_source

    return record_operator_chat_source(
        text,
        cycle_id=cycle_id,
        session_id=f"session-{cycle_id}",
        session_timestamp=timestamp,
    )


@pytest.mark.usefixtures("deepsleep_sram_drain")
def test_ingest_persona_statement_decomposes_into_routed_leaf_records(tmp_path):
    from memory.persona_ingest import ingest_persona_statement

    # Snapshot the LIVE persona catalog before the isolated run. Pre-existing
    # live-brain entries (a real 'abel' the operator registered while testing) are
    # legitimate; the guard below asserts this run ADDED nothing to it — its
    # writes must land only in tmp.
    _live_index = REPO_ROOT / "guardian" / "state" / "persona_index.json"
    _live_before = (
        set((json.loads(_live_index.read_text(encoding="utf-8")) or {}).keys())
        if _live_index.exists()
        else set()
    )
    _stub_root = REPO_ROOT / "memory" / "100_personas" / "person_stub"
    _stubs_before = {n for n in ("miranda", "abel") if (_stub_root / n).exists()}

    result = ingest_persona_statement(CANONICAL_TEXT, speaker="operator")

    # ── Every write happened under the isolated tmp root, never live memory/ ──
    all_paths = [r.path for r in result.records if r.path]
    assert all_paths, "expected at least one record to be written"
    from memory.engine.walker import resolve_coord_index_path

    for p in all_paths:
        # Receipt paths are profile-relative by design; resolve like recall does
        # (against the isolated MEMORY_ROOT, never the CWD).
        resolved = resolve_coord_index_path(p) or Path(p).resolve()
        assert str(resolved).startswith(str(tmp_path.resolve())), (
            f"record escaped isolated root: {resolved}"
        )
        assert not str(resolved).startswith(str((REPO_ROOT / "memory").resolve())), (
            f"record landed in the LIVE memory store: {resolved}"
        )

    _live_after = (
        set((json.loads(_live_index.read_text(encoding="utf-8")) or {}).keys())
        if _live_index.exists()
        else set()
    )
    _added = _live_after - _live_before
    assert "miranda" not in _added and "abel" not in _added, (
        f"isolated run leaked personas into the LIVE catalog: {_added}"
    )
    _stubs_after = {n for n in ("miranda", "abel") if (_stub_root / n).exists()}
    assert _stubs_after == _stubs_before, (
        f"isolated run created LIVE person_stub dirs: {_stubs_after - _stubs_before}"
    )

    # ── Relationship entities routed to (1,1,<person_id>) person_stubs ──────
    person_records = {r.person_name: r for r in result.person_records}
    assert set(person_records) == {"miranda", "abel"}

    miranda = person_records["miranda"]
    assert miranda.coord == [1, 1]
    assert miranda.relation == "sister"
    assert miranda.person_id == "miranda"
    assert "linked_to:operator" in miranda.tags
    assert miranda.path is not None

    abel = person_records["abel"]
    assert abel.coord == [1, 1]
    assert abel.relation == "son"
    assert abel.person_id == "abel"
    assert "linked_to:operator" in abel.tags
    assert abel.path is not None

    miranda_record = _load(miranda.path)
    assert miranda_record.get("domain") == "miranda"
    assert "relationship" in (miranda_record.get("tags") or [])
    assert "sister" in (miranda_record.get("tags") or [])

    abel_record = _load(abel.path)
    assert abel_record.get("domain") == "abel"
    assert "son" in (abel_record.get("tags") or [])

    # entity_linker post-commit hook should have formed a real bidirectional
    # link back to the speaker's operator record (on top of the tag stamp).
    assert miranda_record.get("links"), (
        "expected entity_linker to link miranda's stub to an operator record"
    )
    assert abel_record.get("links"), (
        "expected entity_linker to link abel's stub to an operator record"
    )

    # ── Speaker's own attributes → discrete operator (1,0) leaves ──────────
    operator_by_facet = {r.facet: r for r in result.operator_records}
    assert "education" in operator_by_facet
    assert "name" in operator_by_facet

    education = operator_by_facet["education"]
    assert education.coord == [1, 0, 0]
    assert education.value == "cybersecurity"
    assert "profile:education" in education.tags
    education_record = _load(education.path)
    assert education_record.get("domain") == "operator"
    assert "cybersecurity" in education_record.get("text", "")
    # Operator self-facts are RECALLABLE plain facts — never lane-filtered as
    # private (being THE operator is the separate onboarding-only anchor).
    assert not education_record.get("flags", {}).get("private")

    name_leaf = operator_by_facet["name"]
    assert name_leaf.value == "noah"

    # facet entities that are the OBJECT of a relationship (miranda, abel)
    # must NOT also become a standalone operator "name" attribute leaf.
    operator_names = {r.value for r in result.operator_records if r.facet == "name"}
    assert "miranda" not in operator_names
    assert "abel" not in operator_names

    # ── No monolithic blob: every written record's own enrichment carries
    #    only ITS entity/entities, never all six facts at once. ────────────
    for rec in result.records:
        loaded = _load(rec.path)
        entities = (loaded.get("enrichment") or {}).get("entities") or []
        people_in_record = {
            e.get("linked_name") if e.get("facet") == "relationship" else e.get("name")
            for e in entities
            if isinstance(e, dict) and e.get("facet") in {"name", "relationship"}
        }
        people_in_record.discard(None)
        assert len(people_in_record) <= 1, (
            f"record {rec.path} looks like a monolithic blob: {people_in_record}"
        )
        # cross-contamination check: an education leaf must not mention
        # unrelated relationship names, and vice versa.
        text_blob = json.dumps(loaded)
        if rec.facet == "education":
            assert "miranda" not in text_blob.lower()
            assert "abel" not in text_blob.lower()


@pytest.mark.usefixtures("deepsleep_sram_drain")
def test_ingest_persona_statement_is_idempotent_on_person_routing(tmp_path):
    """A second ingest of the same statement reuses the same person_id
    (route_persona dedupes by slug) rather than minting duplicate persons."""
    from memory.persona_ingest import ingest_persona_statement

    first = ingest_persona_statement(CANONICAL_TEXT, speaker="operator")
    second = ingest_persona_statement(CANONICAL_TEXT, speaker="operator")

    first_ids = {r.person_name: r.person_id for r in first.person_records}
    second_ids = {r.person_name: r.person_id for r in second.person_records}
    assert first_ids == second_ids == {"miranda": "miranda", "abel": "abel"}


@pytest.mark.usefixtures("deepsleep_sram_drain")
def test_ingest_writes_reverse_operator_relation_leaves(tmp_path):
    """Relations are stored BOTH ways: the person_stub ('Abel is the operator's
    son', answers 'who is Abel') PLUS a reverse operator-side leaf ('The
    operator's son is Abel', recallable, answers 'who is the operator's son')
    so recall resolves either direction without the model reversing the fact."""
    from memory.persona_ingest import ingest_persona_statement

    result = ingest_persona_statement(CANONICAL_TEXT, speaker="operator")

    rel_by_person = {r.person_name: r for r in result.relation_records}
    assert set(rel_by_person) == {"miranda", "abel"}

    abel = rel_by_person["abel"]
    assert abel.coord == [1, 0, 0]  # operator side, NOT the (1,1) person_stub
    assert abel.relation == "son"
    rec = _load(abel.path)
    assert rec.get("domain") == "operator"
    assert not rec.get("flags", {}).get("private")  # recallable, not lane-filtered
    text = rec.get("text", "").lower()
    assert "abel" in text and "son" in text  # "The operator's son is Abel."

    # Forward person_stubs still exist alongside the reverse leaves.
    assert {r.person_name for r in result.person_records} == {"miranda", "abel"}


@pytest.mark.usefixtures("deepsleep_sram_drain")
def test_relations_carry_operator_name_when_known(tmp_path, monkeypatch):
    """When the operator is named, relation records carry the NAME ("Noah Lee's
    son is Abel" / "Abel is Noah Lee's son") so "tell me about Noah" matches them
    lexically — not only "the operator's son" (which a name query can't match)."""
    import memory.persona_ingest as pi

    monkeypatch.setattr(pi, "_resolve_operator_name", lambda: "Noah Lee")

    result = pi.ingest_persona_statement("I have a son named Abel.", speaker="operator")

    reverse = next(r for r in result.relation_records if r.person_name == "abel")
    assert "Noah Lee" in _load(reverse.path).get("text", "")  # "Noah Lee's son is Abel."

    stub = next(r for r in result.person_records if r.person_name == "abel")
    assert "Noah Lee" in _load(stub.path).get("text", "")  # "Abel is Noah Lee's son ..."


@pytest.mark.usefixtures("deepsleep_sram_drain")
def test_explicit_remember_command_resolves_named_person_relations_and_status(
    tmp_path, monkeypatch
):
    """The exact live utterance becomes complete Mitchell-scoped memory.

    Every independent assertion must survive as a cited sibling leaf: the
    operator relationship, organization role, unnamed-child count, and marital
    status. Coordinated pronouns must not make later clauses disappear.
    """
    import memory.persona_ingest as pi

    monkeypatch.setattr(pi, "_resolve_operator_name", lambda: "Noah Lee")
    text = (
        "Remember Mitchell Ray. He is Noahs best friend, and he is also part "
        "owner of Omega AI LLC. He has 2 daughters and he is married."
    )
    citation = _operator_citation(
        text,
        cycle_id="chat-mitchell",
        timestamp="2026-07-29T20:00:00+00:00",
    )

    result = pi.ingest_persona_statement(text, speaker="operator", source_citation=citation)

    relationships = {(record.relation, record.person_name) for record in result.relation_records}
    assert ("best_friend", "mitchell ray") in relationships
    assert ("friend", "mitchell ray") not in relationships

    qualified_relationships = [
        _load(record.path)
        for record in result.records
        if record.relation == "best_friend" and record.path
    ]
    assert len(qualified_relationships) == 2
    assert all("best friend" in record["text"].lower() for record in qualified_relationships)
    assert all(
        any(
            entity.get("facet") == "relationship"
            and entity.get("name") == "best_friend"
            and entity.get("base_relation") == "friend"
            and entity.get("qualifiers") == ["best"]
            for entity in record.get("enrichment", {}).get("entities", [])
        )
        for record in qualified_relationships
    )

    attributes = {
        record.facet: _load(record.path)
        for record in result.person_attribute_records
        if record.person_name == "mitchell ray"
    }
    assert set(attributes) >= {
        "organization_role",
        "children",
        "marital_status",
    }
    assert attributes["organization_role"]["text"] == (
        "Mitchell Ray is part owner of Omega AI LLC."
    )
    assert attributes["children"]["text"] == "Mitchell Ray has 2 daughters."
    assert attributes["marital_status"]["text"] == "Mitchell Ray is married."
    assert all(
        record.get("domain") == "mitchell_ray"
        and record.get("provenance", {}).get("source_id") == citation["source_id"]
        for record in attributes.values()
    )
    assert "spouse" not in attributes["marital_status"]["text"].lower()

    # These are sibling projections from one admitted statement, not revisions
    # of one another. Every fact must remain a recallable chain head.
    committed = [_load(record.path) for record in result.records if record.path]
    assert len(committed) == len(result.records)
    assert all(not record.get("superseded_by") for record in committed)
    assert all(not record.get("supersedes") for record in committed)

    # One durable profile root owns every sibling fact and relationship.
    assert len(result.profile_records) == 1
    profile = result.profile_records[0]
    assert profile.person_id == "mitchell_ray"
    assert profile.person_name == "Mitchell Ray"
    assert profile.id and profile.path

    import core.persona_router as pr
    from memory.persona_tree import build_persona_tree

    catalog = json.loads(pr._INDEX_PATH.read_text(encoding="utf-8"))
    assert catalog["mitchell_ray"]["name"] == "Mitchell Ray"
    # Layout 2026-07-31: a person sits DIRECTLY under the personas lobe, beside
    # the operator's own domain dir — not nested under the (1,1) subdomain name
    # "person_stub", which is a record TYPE rather than a person.
    profile_dir = pr.OMEGA_ROOT / "memory" / "100_personas" / "mitchell_ray"
    assert profile_dir.is_dir()
    assert not (pr.OMEGA_ROOT / "memory" / "100_personas" / "person_stub").exists()

    tree = build_persona_tree("Mitchell Ray").to_dict()
    assert tree["person_id"] == "mitchell_ray"
    assert "Mitchell Ray" in tree["names"]
    assert profile.id in tree["record_ids"]
    assert {item["relation"] for item in tree["relationships"]} >= {"best_friend"}
    tree_blob = json.dumps(tree).lower()
    assert "married" in tree_blob
    assert "part owner of omega ai llc" in tree_blob
    assert "2 daughters" in tree_blob


def test_bare_remembered_name_creates_a_profile_root(tmp_path, monkeypatch):
    """A new named person is a durable profile even before Omega knows any
    relationship or attribute about them.
    """
    import core.persona_router as pr
    import memory.episode_sram as sram
    import memory.persona_ingest as pi
    from memory.engine import walker
    from memory.persona_tree import build_persona_tree

    monkeypatch.setattr(pi, "_resolve_operator_name", lambda: "Noah Lee")
    text = "Remember Priya Shah."
    citation = _operator_citation(
        text,
        cycle_id="chat-priya",
        timestamp="2026-07-29T21:00:00+00:00",
    )

    sram.open_bucket("chat-priya", pipeline_id=0)
    result = pi.ingest_persona_statement(
        text,
        speaker="operator",
        source_citation=citation,
    )

    assert len(result.profile_records) == 1
    profile = result.profile_records[0]
    assert profile.person_id == "priya_shah"
    assert profile.person_name == "Priya Shah"
    assert profile.id.startswith("sram:chat-priya:")
    assert profile.path is None

    flush_result = sram.flush("chat-priya")
    assert len(flush_result["memories"]) == 1
    canonical_id = pr._canonical_profile_root_id("priya_shah")
    assert canonical_id

    walker.load_coord_index()
    entry = walker.coord_index_snapshot()[canonical_id]
    record = walker.load_meta_by_path(entry["path"])
    assert record["type"] == "persona_name"
    assert record["text"] == "Priya Shah is a known person."
    assert record["domain"] == "priya_shah"
    assert record["provenance"]["source_id"] == citation["source_id"]

    catalog = json.loads(pr._INDEX_PATH.read_text(encoding="utf-8"))
    assert catalog["priya_shah"]["name"] == "Priya Shah"
    tree = build_persona_tree("Priya Shah").to_dict()
    assert tree["person_id"] == "priya_shah"
    assert tree["names"] == ["Priya Shah", "priya_shah"]
    assert tree["record_ids"] == [canonical_id]


def test_profile_root_idempotency_does_not_depend_on_recall_verification(monkeypatch):
    """A missing external proof artifact may keep a leaf out of recall, but it
    must never make profile creation repeat the same immutable canonical write.
    """
    import memory.persona_ingest as pi
    import memory.episode_sram as sram
    from memory.engine import walker

    monkeypatch.setattr(pi, "_resolve_operator_name", lambda: "Noah Lee")
    citation_without_artifact = {
        "source_id": "missing-source-artifact",
        "source_type": "operator_chat",
        "span_start": 0,
        "span_end": 20,
        "span_hash": "missing-span-proof",
        "source_content_hash": "missing-content-proof",
    }

    sram.open_bucket("chat-jordan", pipeline_id=0)
    first = pi.ingest_persona_statement(
        "Remember Jordan Lee.",
        speaker="operator",
        source_citation=citation_without_artifact,
    )
    second = pi.ingest_persona_statement(
        "Remember Jordan Lee.",
        speaker="operator",
        source_citation=citation_without_artifact,
    )

    assert [record.id for record in first.profile_records] == [
        record.id for record in second.profile_records
    ]
    flush_result = sram.flush("chat-jordan")
    assert len(flush_result["memories"]) == 1
    walker.load_coord_index()
    roots = [
        entry
        for entry in walker.coord_index_snapshot().values()
        if entry.get("domain") == "jordan_lee" and entry.get("type") == "persona_name"
    ]
    assert len(roots) == 1


@pytest.mark.usefixtures("deepsleep_sram_drain")
def test_operator_name_is_not_registered_as_a_third_person_profile(monkeypatch):
    """Linking Mitchell to Noah must not create a second non-operator Noah tree."""
    import core.persona_router as pr
    import memory.persona_ingest as pi

    monkeypatch.setattr(pi, "_resolve_operator_name", lambda: "Noah Lee")
    pi.ingest_persona_statement(
        "Remember Mitchell Ray. He is Noah Lee's business partner.",
        speaker="operator",
    )

    catalog = json.loads(pr._INDEX_PATH.read_text(encoding="utf-8"))
    assert "mitchell_ray" in catalog
    assert "noah_lee" not in catalog


@pytest.mark.skipif(
    __import__("importlib").util.find_spec("spacy") is None,
    reason="spaCy / en_core_web_sm not installed",
)
@pytest.mark.usefixtures("deepsleep_sram_drain")
def test_ingest_captures_business_ownership(tmp_path):
    """The parse-based extractor fills the regex gap: "I own a business called
    Omega AI" — which matched no regex and was silently dropped — now becomes a
    recallable operator business fact."""
    from memory.persona_ingest import ingest_persona_statement

    result = ingest_persona_statement("I own a business called Omega AI.", speaker="operator")
    biz = [r for r in result.operator_records if r.facet == "business"]
    assert biz, f"business fact not ingested; facets={[r.facet for r in result.operator_records]}"
    rec = _load(biz[0].path)
    assert "omega ai" in rec.get("text", "").lower()
    assert rec.get("domain") == "operator"
    assert not rec.get("flags", {}).get("private")  # recallable operator self-fact


@pytest.mark.usefixtures("deepsleep_sram_drain")
def test_operator_attribute_deduped_on_restatement(tmp_path):
    """The operator's name uses the canonical identity anchor on every restatement."""
    from memory.persona_ingest import ingest_persona_statement

    first = ingest_persona_statement("My name is Avery Jones.", speaker="operator")
    second = ingest_persona_statement("I am Avery Jones.", speaker="operator")

    id1 = {r.id for r in first.operator_records if r.facet == "name" and r.id}
    id2 = {r.id for r in second.operator_records if r.facet == "name" and r.id}
    assert id1 == {"1.00.operator_name"}, "chat-derived names must mint the canonical anchor"
    assert id1 == id2, (
        "restatement minted a duplicate name record: "
        f"first={[(r.value, r.id) for r in first.operator_records if r.facet == 'name']} "
        f"second={[(r.value, r.id) for r in second.operator_records if r.facet == 'name']}"
    )
    anchor = _load(next(r.path for r in second.operator_records if r.facet == "name"))
    assert anchor["type"] == "identity_anchor"
    assert anchor["enrichment"]["operator_name"] == "Avery Jones"
    assert anchor["flags"]["private"] is False
    from memory.strength_index import ensure_strength_index

    assert ensure_strength_index().get_record("1.00.operator_name")["write_strength"] == 1.0


@pytest.mark.usefixtures("deepsleep_sram_drain")
def test_operator_name_change_updates_the_canonical_anchor(tmp_path):
    from memory.persona_ingest import ingest_persona_statement

    first = ingest_persona_statement("My name is Avery Jones.", speaker="operator")
    updated = ingest_persona_statement("My name is Taylor Reed.", speaker="operator")

    [prior_name] = [r for r in first.operator_records if r.facet == "name"]
    [name_record] = [r for r in updated.operator_records if r.facet == "name"]
    assert prior_name.id == "1.00.operator_name"
    assert name_record.id != prior_name.id
    anchor = _load(name_record.path)
    assert anchor["supersedes"] == prior_name.id
    assert anchor["version"] == 2
    assert anchor["enrichment"]["operator_name"] == "Taylor Reed"
    assert anchor["summary"] == "The operator is Taylor Reed."
    assert _load(prior_name.path)["superseded_by"] == name_record.id
    from memory.persona_ingest import _resolve_operator_name

    assert _resolve_operator_name() == "Taylor Reed"


@pytest.mark.skipif(
    __import__("importlib").util.find_spec("spacy") is None,
    reason="spaCy / en_core_web_sm not installed",
)
@pytest.mark.usefixtures("deepsleep_sram_drain")
def test_ingest_captures_education_bachelors_phrasing(tmp_path):
    """The live "where do I go to school?" miss: "going to college for my Bachelors in
    cybersecurity" — which the parser dropped (the field hangs off the verb, not the
    academic anchor) — now becomes a recallable operator education fact, end to end."""
    from memory.persona_ingest import ingest_persona_statement

    result = ingest_persona_statement(
        "I am going to college for my Bachelors in cybersecurity.", speaker="operator"
    )
    edu = [r for r in result.operator_records if r.facet == "education"]
    assert edu, f"education not ingested; facets={[r.facet for r in result.operator_records]}"
    rec = _load(edu[0].path)
    assert "cybersecurity" in rec.get("text", "").lower()
    assert rec.get("domain") == "operator"
    assert not rec.get("flags", {}).get("private")  # recallable operator self-fact
