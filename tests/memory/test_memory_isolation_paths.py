from __future__ import annotations


def test_redirect_clears_cached_profile_relative_paths(tmp_path, monkeypatch):
    from memory.engine import walker
    from tests.helpers.memory_isolation import redirect_memory_to_tmp

    walker.invalidate_meta_cache(None)
    redirect_memory_to_tmp(tmp_path / "first", monkeypatch)
    first = walker.resolve_coord_index_path("memory/100_personas/operator/operator_name/example.json")

    redirect_memory_to_tmp(tmp_path / "second", monkeypatch)
    second = walker.resolve_coord_index_path("memory/100_personas/operator/operator_name/example.json")

    assert first is not None and first.is_relative_to(tmp_path / "first")
    assert second is not None and second.is_relative_to(tmp_path / "second")


def test_shared_memory_isolation_redirects_encoding_journal(tmp_path, monkeypatch):
    from memory import encoding_journal
    from tests.helpers.memory_isolation import redirect_memory_to_tmp

    redirect_memory_to_tmp(tmp_path, monkeypatch, stub_encoding=False)

    assert encoding_journal.JOURNAL_PATH.is_relative_to(tmp_path)


def test_shared_memory_isolation_redirects_intake_journal(tmp_path, monkeypatch):
    from memory import intake_journal
    from tests.helpers.memory_isolation import redirect_memory_to_tmp

    redirect_memory_to_tmp(tmp_path, monkeypatch, stub_encoding=False)

    assert intake_journal.journal_path().is_relative_to(tmp_path)


def test_shared_memory_isolation_redirects_association_sidecar(tmp_path, monkeypatch):
    from memory import association_strength
    from tests.helpers.memory_isolation import redirect_memory_to_tmp

    redirect_memory_to_tmp(tmp_path, monkeypatch, stub_encoding=False)

    assert association_strength.association_path().is_relative_to(tmp_path)
