from __future__ import annotations

from memory.taxonomy.paths import compiled_root, taxonomy_root


def test_profile_root_resolves_to_memory_taxonomy_store(tmp_path):
    (tmp_path / "memory").mkdir()
    assert taxonomy_root(tmp_path) == tmp_path / "memory" / "taxonomy_store"
    assert compiled_root(tmp_path) == tmp_path / "memory" / "taxonomy_store" / "compiled"


def test_memory_root_resolves_to_its_taxonomy_store(tmp_path):
    memory_root = tmp_path / "memory"
    assert taxonomy_root(memory_root) == memory_root / "taxonomy_store"


def test_explicit_taxonomy_store_root_is_preserved(tmp_path):
    store = tmp_path / "memory" / "taxonomy_store"
    assert taxonomy_root(store) == store
