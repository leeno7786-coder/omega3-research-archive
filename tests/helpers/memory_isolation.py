"""Hermetic memory-store redirect shared by test-suite conftests.

Found the hard way (2026-07-11): suites without this fixture wrote REAL memories
into the canonical brain — 76 test-junk files (physics/quantum "knowledge",
cascade records, capability rows) landed in a freshly day-zeroed corpus, and the
motor-E2E isolation guard tripped on the pollution. Every suite whose tests can
reach a memory write path (cascade folds, episode cognition, deepsleep, direct
memory.api) must install the autouse fixture:

    from tests.helpers.memory_isolation import redirect_memory_to_tmp

    @pytest.fixture(autouse=True)
    def _isolate_memory_root(tmp_path, monkeypatch):
        redirect_memory_to_tmp(tmp_path, monkeypatch)
        yield

Mirrors tests/core/conftest.py's redirect exactly: staging, memory root, tmp
writes, coord index, walker cache, and a stubbed encode step (no :11434 embed).
"""

from __future__ import annotations


def redirect_memory_to_tmp(tmp_path, monkeypatch, *, stub_encoding: bool = True) -> None:
    """Point every memory write surface at tmp_path — never the real memory/ tree."""
    import core.paths as core_paths
    import memory.engine.reindex as reindex
    import memory.engine.validate_write as vw
    import memory.engine.walker as walker
    import memory.engine.walker_loader as walker_loader
    import memory.encoding_journal as encoding_journal
    import memory.intake_journal as intake_journal
    import memory.association_strength as association_strength
    import memory.promotion as prom
    import memory.strength_index as strength_index
    import memory.taxonomy.paths as taxonomy_paths
    import pressure_loop.reflection_window as reflection_window

    isolation_root = tmp_path / "_canonical_memory_isolation"
    staging_dir = isolation_root / "staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    mem_root = isolation_root / "memory"
    mem_root.mkdir(parents=True, exist_ok=True)
    (mem_root / "tmp_writes").mkdir(parents=True, exist_ok=True)
    (mem_root / "indexes").mkdir(parents=True, exist_ok=True)
    index_path = mem_root / "indexes" / "coord_index.json"
    # A zero-record profile still has a valid, explicit empty index. Missing is
    # an unavailable substrate state and must not be used as an empty fixture.
    index_path.write_text("{}\n", encoding="utf-8")
    taxonomy_root = mem_root / "taxonomy_store"

    monkeypatch.setattr(core_paths, "MEMORY_ROOT", mem_root)
    monkeypatch.setattr(core_paths, "MEMORY_TAXONOMY_ROOT", taxonomy_root)
    monkeypatch.setattr(
        core_paths, "COGNITIVE_TOOL_LOBE_ROOT", mem_root / "500_tools" / "cognitive"
    )
    monkeypatch.setattr(core_paths, "FACE_THUMBNAILS_ROOT", mem_root / "700_vision" / "images")
    monkeypatch.setattr(prom, "STAGING_DIR", staging_dir)
    monkeypatch.setattr(vw, "MEMORY_ROOT", mem_root)
    monkeypatch.setattr(vw, "TMP_DIR", mem_root / "tmp_writes")
    monkeypatch.setattr(vw, "INDEX_PATH", index_path)
    # reindex binds MEMORY_ROOT at IMPORT, so without this the first test to import it decided
    # where every later build_index(root=None) wrote — once, the operator's live coord index.
    monkeypatch.setattr(reindex, "MEMORY_ROOT", mem_root)
    monkeypatch.setattr(reindex, "INDEX_PATH", index_path)
    monkeypatch.setattr(walker, "MEMORY_ROOT", mem_root)
    monkeypatch.setattr(walker, "INDEX_PATH", index_path)
    monkeypatch.setattr(walker_loader, "MEMORY_ROOT", mem_root)
    monkeypatch.setattr(walker_loader, "INDEX_PATH", index_path)
    monkeypatch.setattr(taxonomy_paths, "MEMORY_TAXONOMY_ROOT", taxonomy_root)
    monkeypatch.setattr(
        reflection_window,
        "reflection_window_archive_dir",
        lambda: mem_root / "600_journal" / "reflection_windows",
    )
    monkeypatch.setattr(
        encoding_journal,
        "JOURNAL_PATH",
        isolation_root / "guardian" / "state" / "encoding_journal.jsonl",
    )
    monkeypatch.setattr(
        intake_journal,
        "journal_root",
        lambda: isolation_root / "guardian" / "state" / "intake",
    )
    intake_journal.reset_cache_for_tests()
    monkeypatch.setattr(
        association_strength,
        "association_path",
        lambda: isolation_root / "guardian" / "state" / "association_strength.json",
    )
    # The strength_index is another OMEGA_ROOT-relative memory surface this
    # paths-only redirect used to miss: STRENGTH_INDEX_PATH is guardian/state-
    # relative and the singleton is a process global. Left un-isolated, an
    # "isolated" test's readback runs the real recall pipeline's lazy confidence
    # decay + sustained-floor demotion against the LIVE strength_index — whose
    # persisted last_access for a deterministic content-hash id can be days stale —
    # slamming confidence to the floor and DEMOTING a freshly-written record
    # (episode -> observation, live 2026-07-15: test_write_episode_to_temporal).
    # Redirect the path AND drop the singleton so ensure_strength_index() reloads
    # fresh from tmp (monkeypatch restores both at teardown).
    monkeypatch.setattr(
        strength_index,
        "STRENGTH_INDEX_PATH",
        isolation_root / "guardian" / "state" / "strength_index.json",
    )
    monkeypatch.setattr(strength_index, "_singleton", None)
    if stub_encoding:
        from memory.encoding import EncodingResult

        monkeypatch.setattr(
            "memory.encoding.encode_committed_memory",
            lambda md, fp, **kwargs: EncodingResult(ok=True),
        )
    # resolve_coord_index_path memoizes profile-relative paths by their raw
    # string. A test profile switch must invalidate that cache or records can
    # resolve back into the prior profile even when COORD_INDEX is empty.
    walker.invalidate_meta_cache(None)
    with walker._COORD_INDEX_LOCK:
        walker.COORD_INDEX.clear()
    walker._coord_index_mtime = 0.0
