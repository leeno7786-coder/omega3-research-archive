"""RED-first integration tests for Slice 2 -- dynamic Y wiring into the
write path (Surface 3).

Tests assert:
  1. flag-ON:  write lands via allocator (dynamic Y in coord + on disk).
  2. flag-OFF: static path byte-for-byte unchanged.
  3. idempotency: same title/text -> same subdomain (no duplicate).
  4. resolve_subdomain_dir_name: path resolver contract for both directions.
"""

import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_omega_root(tmp_path, monkeypatch):
    root = tmp_path / "omega_root"
    root.mkdir(exist_ok=True)
    import core.paths as paths

    monkeypatch.setattr(paths, "OMEGA_ROOT", root)
    mem = root / "memory"
    (mem / "200_knowledge").mkdir(parents=True, exist_ok=True)
    (mem / "800_coding").mkdir(parents=True, exist_ok=True)
    schema_dir = root / "schema"
    schema_dir.mkdir(parents=True, exist_ok=True)
    (schema_dir / "memory_schema.json").write_text(
        json.dumps({"type": "object", "additionalProperties": True}),
        encoding="utf-8",
    )
    (root / "guardian" / "state").mkdir(parents=True, exist_ok=True)
    return root


class _StubClassifier:
    def __init__(self, coord=None):
        self._coord = coord or [2, 5, 0]

    def classify(self, text):
        c = list(self._coord)
        return (f"{c[0]}.{c[1]:02d}", c, 0.8, ["stub"])

    def classify_within_lobe(self, text, lobe):
        c = [lobe, self._coord[1], 0]
        return (f"{c[0]}.{c[1]:02d}", c, 0.8, ["stub"])


def _patch_gate(tmp_path, monkeypatch, *, flag_set):
    root = _setup_omega_root(tmp_path, monkeypatch)
    import memory.gates.base as base_mod
    from memory.gates.base import MemoryGate

    stub_clf = _StubClassifier(coord=[2, 5, 0])
    MemoryGate._classifier = stub_clf
    MemoryGate._classifier_loaded = True
    MemoryGate._strength_index = None
    monkeypatch.setattr(MemoryGate, "_get_strength_index", classmethod(lambda cls: None))

    import memory.subdomain_flag as flag_mod

    flag_list = sorted(flag_set)
    monkeypatch.setattr(flag_mod, "_FLAG_CACHE", frozenset(flag_set))
    monkeypatch.setattr(flag_mod, "_RAW_CACHE", flag_list)

    monkeypatch.setattr(base_mod.MemoryGate, "_guardian_check", lambda self, text, source: True)
    monkeypatch.setattr(base_mod.MemoryGate, "_enrich", lambda self, text, lobe: None)
    from memory.gates import subdomain_override

    monkeypatch.setattr(
        subdomain_override,
        "apply_subdomain_override",
        lambda text, coord, label_id: (coord, label_id),
    )
    return root


# ---------------------------------------------------------------------------
# 1. flag-ON: dynamic Y allocation in the write path
# ---------------------------------------------------------------------------


def test_flag_on_ingest_uses_allocator(tmp_path, monkeypatch):
    root = _patch_gate(tmp_path, monkeypatch, flag_set={2})

    from memory.gates.lobe_gates import KnowledgeGate

    gate = KnowledgeGate()
    text = "Quantum entanglement explained clearly"
    metadata = {"title": "quantum entanglement"}
    result = gate.ingest(text, metadata)

    assert result is not None, "ingest should succeed with flag ON"
    assert "200_knowledge" in result, f"expected 200_knowledge in path: {result}"

    from memory.subdomain_allocator import _load_index

    idx = _load_index()
    subs = idx.get("lobes", {}).get("2", {}).get("subdomains", {})
    assert len(subs) >= 1, "allocator should have created >= 1 subdomain"

    for dn in subs:
        if dn in result:
            break
    else:
        pytest.fail(f"path {result} does not contain allocator dirname {list(subs.keys())}")

    # value assertion: the first dynamic allocation is Y=0 (coord reflects it)
    assert list(subs.values())[0]["y"] == 0, "first dynamic subdomain must be Y=0"


# ---------------------------------------------------------------------------
# 2. flag-OFF: static path unchanged
# ---------------------------------------------------------------------------


def test_flag_off_ingest_uses_static_path(tmp_path, monkeypatch):
    root = _patch_gate(tmp_path, monkeypatch, flag_set=set())

    from memory.gates.lobe_gates import KnowledgeGate

    gate = KnowledgeGate()
    text = "A text about physics and thermodynamics"
    metadata = {"title": "physics thermodynamics"}
    result = gate.ingest(text, metadata)

    assert result is not None, "ingest should succeed with flag OFF"
    dyn_index = root / "guardian" / "state" / "dynamic_y_index.json"
    assert not dyn_index.exists(), "dynamic_y_index.json must not exist when flag is OFF"
    # byte-for-byte static: the resolver must return the exact static
    # SUBDOMAIN_MAP value for a non-flagged lobe — the new final_path_for_meta
    # routing must not alter the static path.
    from memory.dewey_map import SUBDOMAIN_MAP
    from memory.subdomain_allocator import resolve_subdomain_dir_name

    assert resolve_subdomain_dir_name(2, 5) == SUBDOMAIN_MAP.get((2, 5), "05"), (
        "flag-OFF resolver must return the static SUBDOMAIN_MAP value unchanged"
    )


# ---------------------------------------------------------------------------
# 3. Idempotency: same title -> same subdomain
# ---------------------------------------------------------------------------


def test_idempotent_writes_same_title_same_subdomain(tmp_path, monkeypatch):
    root = _patch_gate(tmp_path, monkeypatch, flag_set={2})

    from memory.gates.lobe_gates import KnowledgeGate

    gate = KnowledgeGate()
    text = "Machine learning transformer architectures"
    metadata = {"title": "transformer architecture"}

    result1 = gate.ingest(text, metadata)
    assert result1 is not None

    result2 = gate.ingest(text, metadata)
    assert result2 is not None

    dir1 = str(Path(result1).parent)
    dir2 = str(Path(result2).parent)
    assert dir1 == dir2, f"idempotency violated: {dir1} != {dir2}"

    from memory.subdomain_allocator import _load_index

    idx = _load_index()
    subs = idx.get("lobes", {}).get("2", {}).get("subdomains", {})
    assert len(subs) == 1, f"expected 1 subdomain after 2 identical writes, got {len(subs)}"


# ---------------------------------------------------------------------------
# 4. resolve_subdomain_dir_name -- path resolver contract
# ---------------------------------------------------------------------------


def test_resolve_subdomain_dir_name_flag_on_returns_allocator_dirname(tmp_path, monkeypatch):
    root = _setup_omega_root(tmp_path, monkeypatch)
    import memory.subdomain_allocator as alloc

    alloc.get_or_create_subdomain(
        lobe=2,
        seed_text="test content",
        title="Test",
        dynamic_y_lobes={2},
    )
    import memory.subdomain_flag as flag_mod

    monkeypatch.setattr(flag_mod, "_FLAG_CACHE", frozenset({2}))

    from memory.knowledge_router import _derive_name

    expected_dn = _derive_name("test content", "Test")
    dirname = alloc.resolve_subdomain_dir_name(2, 0)
    assert dirname == expected_dn, (
        f"resolver must return the EXACT allocator dirname {expected_dn!r}, got {dirname!r}"
    )


def test_resolve_subdomain_dir_name_flag_off_returns_static(tmp_path, monkeypatch):
    from memory.dewey_map import SUBDOMAIN_MAP
    import memory.subdomain_flag as flag_mod

    monkeypatch.setattr(flag_mod, "_FLAG_CACHE", frozenset())
    import memory.subdomain_allocator as alloc

    known = SUBDOMAIN_MAP.get((2, 0))
    if known is None:
        pytest.skip("no static SUBDOMAIN_MAP entry for (2,0)")

    dirname = alloc.resolve_subdomain_dir_name(2, 0)
    assert dirname == known


# ---------------------------------------------------------------------------
# 6. FAIL-MODE: flag-ON allocator failure must fail HARD, never silently
#    mis-route a dynamic memory into the static taxonomy we are retiring.
#    (This is the test that would have caught the silent `except: pass`.)
# ---------------------------------------------------------------------------


class _AllocatorDown(RuntimeError):
    pass


def test_flag_on_allocator_failure_fails_hard(tmp_path, monkeypatch):
    _patch_gate(tmp_path, monkeypatch, flag_set={2})

    import memory.subdomain_allocator as alloc_mod

    def _boom(**kwargs):
        raise _AllocatorDown("allocator unavailable")

    monkeypatch.setattr(alloc_mod, "get_or_create_subdomain", _boom)

    from memory.gates.lobe_gates import KnowledgeGate

    gate = KnowledgeGate()

    # When the operator has ENABLED dynamic for this lobe, an allocation
    # failure must surface — a silently static-mis-routed memory poisons
    # recall and cognition. Fail-hard, do not swallow.
    with pytest.raises(_AllocatorDown):
        gate.ingest("Content that must never land static", {"title": "dynamic only"})


def test_resolve_flagged_lobe_missing_index_raises(tmp_path, monkeypatch):
    """A flagged (X,Y) with no dynamic_y_index entry is corruption — the
    resolver must raise, never silently fall back to the static map."""
    _setup_omega_root(tmp_path, monkeypatch)
    import memory.subdomain_flag as flag_mod

    monkeypatch.setattr(flag_mod, "_FLAG_CACHE", frozenset({2}))
    import memory.subdomain_allocator as alloc

    # Nothing allocated for (2, 7) -> must raise for a flagged lobe.
    with pytest.raises(Exception):
        alloc.resolve_subdomain_dir_name(2, 7)


# ---------------------------------------------------------------------------
# 7. RECALL SUBSTRATE: a dynamically-written memory must land in
#    coord_index.json with its DYNAMIC path. Recall (via the recall tools)
#    loads memories BY that stored path — so this is the unit-provable half of
#    recall tolerance. The semantic recall round-trip needs a model tool call
#    on a running runtime; that lives in the first-wake LIVE checklist.
# ---------------------------------------------------------------------------


def test_dynamic_coord_resolves_to_dynamic_storage_path(tmp_path, monkeypatch):
    """SUBSTRATE (hermetic): a flagged-lobe coord with a dynamically-allocated
    Y resolves — through ``final_path_for_meta`` — to a storage path under its
    dynamic dirname. ``coord_index`` stores exactly this path and the recall
    tools load BY it, so this is the unit-provable half of recall tolerance.
    (The semantic recall round-trip needs a model tool call on a running
    runtime; it lives in the first-wake LIVE checklist.)

    Proven at the path layer (not the full gate) on purpose: the gate's
    classifier carries leaked class-level state across the suite, which would
    make a full-ingest assertion flaky for harness reasons unrelated to the
    production behavior under test.
    """
    root = tmp_path / "omega_root"
    (root / "memory" / "200_knowledge").mkdir(parents=True, exist_ok=True)
    (root / "guardian" / "state").mkdir(parents=True, exist_ok=True)
    import core.paths as paths

    monkeypatch.setattr(paths, "OMEGA_ROOT", root)
    import memory.subdomain_flag as flag_mod

    monkeypatch.setattr(flag_mod, "_FLAG_CACHE", frozenset({2}))

    import memory.subdomain_allocator as alloc

    y, dirname = alloc.get_or_create_subdomain(
        lobe=2,
        seed_text="Quantum entanglement explained clearly",
        title="quantum entanglement",
        dynamic_y_lobes={2},
    )
    # The dirname carries the WHOLE topic, not its head token — "quantum" alone
    # named the subdomain after the first word and hid what it was about
    # (live 2026-07-31: "Machine Learning" landed as `machine`, "Deep Learning"
    # as `deep`). The Y allocation is unchanged.
    assert (y, dirname) == (0, "quantum_entanglement")

    # read-side coord -> dirname resolution (what recall/index resolve through)
    assert alloc.resolve_subdomain_dir_name(2, y) == dirname

    # the path coord_index STORES (final_path_for_meta) uses the dynamic dirname
    from memory.engine.validate_write import final_path_for_meta

    meta = {
        "coord": [2, y, 0],
        "id": f"2.{y:02d}.subtest",
        "title": "quantum entanglement",
        "text": "Quantum entanglement explained clearly",
        "type": "knowledge",
    }
    path = str(final_path_for_meta(meta, memory_root=root / "memory"))
    assert dirname in path and "200_knowledge" in path, (
        f"stored path for a flagged-lobe coord must use the dynamic dirname {dirname!r}; got {path}"
    )
