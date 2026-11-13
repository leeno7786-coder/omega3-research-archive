"""Phase 7C — deterministic hashing + normalization.

Content-derived IDs (no random UUIDs) and canonical JSON. The whole taxonomy's
determinism (same ledger + same compiler => same graph hash) rests on these being
pure, total, and order-independent.
"""

from __future__ import annotations

from memory.taxonomy import hashing as H
from memory.taxonomy import normalize as N


# ── normalize ──────────────────────────────────────────────────────────────────
def test_normalize_text_trims_and_collapses_whitespace():
    assert N.normalize_text("  a   b\t c \n") == "a b c"


def test_normalize_text_preserves_case():
    assert N.normalize_text("Codex Grade") == "Codex Grade"


def test_normalize_id_field_lowercases():
    assert N.normalize_id_field("  Codex   GRADE ") == "codex grade"


def test_canonical_json_sorts_keys_and_strips_whitespace():
    assert N.canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'


def test_canonical_json_is_insertion_order_independent():
    assert N.canonical_json({"x": 1, "y": 2}) == N.canonical_json({"y": 2, "x": 1})


def test_canonical_json_nested_is_deterministic():
    a = {"outer": {"b": [3, 1, 2], "a": "Z"}}
    b = {"outer": {"a": "Z", "b": [3, 1, 2]}}
    assert N.canonical_json(a) == N.canonical_json(b)


# ── event_id ───────────────────────────────────────────────────────────────────
def _eid(**over):
    base = dict(
        event_kind="qwen_skill_result",
        episode_id="ep1",
        source="auto",
        payload={"a": 1, "b": 2},
        schema_version="omega.memory_event.v1",
    )
    base.update(over)
    return H.event_id_for(**base)


def test_event_id_is_deterministic():
    assert _eid() == _eid()


def test_event_id_has_prefix():
    assert _eid().startswith("evt_")


def test_event_id_payload_order_independent():
    assert _eid(payload={"a": 1, "b": 2}) == _eid(payload={"b": 2, "a": 1})


def test_event_id_different_payload_differs():
    assert _eid(payload={"a": 1}) != _eid(payload={"a": 2})


def test_event_id_different_kind_differs():
    assert _eid(event_kind="qwen_skill_result") != _eid(event_kind="tool_failure")


# ── entity_id ──────────────────────────────────────────────────────────────────
def _entid(**over):
    base = dict(
        entity_kind="skill",
        canonical_name="codex-grade-coding",
        schema_version="omega.memory_entity.v1",
    )
    base.update(over)
    return H.entity_id_for(**base)


def test_entity_id_deterministic_and_prefixed():
    assert _entid() == _entid()
    assert _entid().startswith("ent_")


def test_entity_id_whitespace_equivalent_same_id():
    assert _entid(canonical_name="codex-grade-coding") == _entid(
        canonical_name="  codex-grade-coding  "
    )


def test_entity_id_case_insensitive():
    assert _entid(canonical_name="Codex-Grade-Coding") == _entid(
        canonical_name="codex-grade-coding"
    )


def test_entity_id_kind_matters():
    assert _entid(entity_kind="skill") != _entid(entity_kind="tool")


# ── claim_id ───────────────────────────────────────────────────────────────────
def _cid(**over):
    base = dict(
        subject="codex-grade-coding",
        predicate="selected_for",
        object="inspect the dispatch schema",
        source_event_id="evt_abc",
        schema_version="omega.memory_claim.v1",
    )
    base.update(over)
    return H.claim_id_for(**base)


def test_claim_id_same_from_same_source():
    assert _cid() == _cid()
    assert _cid().startswith("clm_")


def test_claim_id_normalizes_text_fields():
    assert _cid(subject="  Codex-Grade-Coding ") == _cid(subject="codex-grade-coding")


def test_claim_id_different_source_differs():
    assert _cid(source_event_id="evt_abc") != _cid(source_event_id="evt_xyz")


def test_claim_id_predicate_matters():
    assert _cid(predicate="selected_for") != _cid(predicate="produced_status")


# ── edge_id ────────────────────────────────────────────────────────────────────
def _edid(**over):
    base = dict(
        source_id="ep1",
        edge_type="SELECTED_SKILL",
        target_id="ent_skill_x",
        evidence_event_id="evt_abc",
        schema_version="omega.memory_edge.v1",
    )
    base.update(over)
    return H.edge_id_for(**base)


def test_edge_id_same_edge_same_id():
    assert _edid() == _edid()
    assert _edid().startswith("edg_")


def test_edge_id_components_matter():
    assert _edid(edge_type="SELECTED_SKILL") != _edid(edge_type="USED_TOOL")
    assert _edid(target_id="a") != _edid(target_id="b")
    assert _edid(evidence_event_id="evt_abc") != _edid(evidence_event_id="evt_xyz")


# ── snapshot_hash ──────────────────────────────────────────────────────────────
def _snap(events):
    return H.snapshot_hash_for(
        canonical_events=events,
        canonical_entities=["E1", "E2"],
        canonical_claims=["C1"],
        canonical_edges=["G1"],
        compiler_version="omega.memory_compiler.v1",
        schema_version="omega.memory_event.v1",
        config_hash="cfg1",
    )


def test_snapshot_hash_deterministic():
    assert _snap(["a", "b", "c"]) == _snap(["a", "b", "c"])


def test_snapshot_hash_order_independent():
    assert _snap(["a", "b", "c"]) == _snap(["c", "a", "b"])


def test_snapshot_hash_changes_when_event_added():
    assert _snap(["a", "b"]) != _snap(["a", "b", "c"])


def test_snapshot_hash_no_prefix_is_hex():
    h = _snap(["a"])
    assert len(h) == 64 and all(c in "0123456789abcdef" for c in h)


def test_no_random_uuids_all_ids_pure_functions():
    # the whole point: identical inputs across calls never diverge
    assert _eid() == _eid() and _entid() == _entid() and _cid() == _cid() and _edid() == _edid()
