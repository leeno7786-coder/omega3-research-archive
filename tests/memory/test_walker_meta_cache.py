"""The meta cache must hand out independent objects, cheaply.

Callers mutate what ``load_meta_by_path`` returns. The cache used to guarantee
independence with ``copy.deepcopy``, which profiled as the largest single cost
in an LME-V2 recall (12.0s of 36.7s, 4,257,608 calls from 2,508 loads). It now
caches raw JSON text and re-parses, which gives the same guarantee.

These tests pin the guarantee, not the mechanism -- the whole point is that
the mechanism was allowed to change and the contract was not.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memory.engine import walker

PAYLOAD = {
    "id": "mem-1",
    "text": "hello",
    "score": {"strength": 0.5},
    "tags": ["a", "b"],
    "nested": {"deep": {"deeper": [1, 2, {"x": "y"}]}},
}


@pytest.fixture
def meta_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    path = tmp_path / "mem-1.json"
    path.write_text(json.dumps(PAYLOAD), encoding="utf-8")
    monkeypatch.setattr(walker, "resolve_coord_index_path", lambda p: Path(p))
    with walker._META_CACHE_LOCK:
        walker._META_CACHE.clear()
    return str(path)


def test_returns_the_payload(meta_file: str) -> None:
    assert walker.load_meta_by_path(meta_file) == PAYLOAD


def test_second_call_is_served_from_cache(meta_file: str) -> None:
    """The cached text is authoritative while the stamp matches.

    Not tested by deleting the file: the stamp is (mtime_ns, size), so a cache
    hit requires stat() to succeed. Instead the cached text is swapped for
    different valid JSON, which only surfaces if the cache is really consulted.
    """
    walker.load_meta_by_path(meta_file)
    assert meta_file in walker._META_CACHE
    with walker._META_CACHE_LOCK:
        mtime, size, _ = walker._META_CACHE[meta_file]
        walker._META_CACHE[meta_file] = (mtime, size, json.dumps({"id": "from-cache"}))
    assert walker.load_meta_by_path(meta_file) == {"id": "from-cache"}


def test_mutating_a_result_does_not_affect_the_next_caller(meta_file: str) -> None:
    first = walker.load_meta_by_path(meta_file)
    first["text"] = "MUTATED"
    first["tags"].append("injected")
    first["score"]["strength"] = 99.0
    assert walker.load_meta_by_path(meta_file) == PAYLOAD


def test_deep_mutation_does_not_leak_into_the_cache(meta_file: str) -> None:
    """Independence has to hold all the way down, not just at the top level."""
    first = walker.load_meta_by_path(meta_file)
    first["nested"]["deep"]["deeper"][2]["x"] = "CLOBBERED"
    second = walker.load_meta_by_path(meta_file)
    assert second["nested"]["deep"]["deeper"][2]["x"] == "y"


def test_two_results_do_not_share_nested_objects(meta_file: str) -> None:
    first = walker.load_meta_by_path(meta_file)
    second = walker.load_meta_by_path(meta_file)
    assert first is not second
    assert first["nested"] is not second["nested"]
    assert first["tags"] is not second["tags"]


def test_first_call_result_is_also_independent(meta_file: str) -> None:
    """The store path returns the freshly parsed object; it must not be shared."""
    first = walker.load_meta_by_path(meta_file)
    first["tags"].append("injected")
    assert walker.load_meta_by_path(meta_file)["tags"] == ["a", "b"]


def test_a_rewritten_file_invalidates_the_cache(meta_file: str) -> None:
    walker.load_meta_by_path(meta_file)
    updated = dict(PAYLOAD, text="rewritten")
    path = Path(meta_file)
    path.write_text(json.dumps(updated), encoding="utf-8")
    # Stamp is (mtime_ns, size); the size differs here, so the change is seen
    # even if the filesystem clock is coarse.
    assert walker.load_meta_by_path(meta_file)["text"] == "rewritten"


def test_corrupt_cache_entry_falls_back_to_the_file(meta_file: str) -> None:
    walker.load_meta_by_path(meta_file)
    with walker._META_CACHE_LOCK:
        stamp = walker._META_CACHE[meta_file]
        walker._META_CACHE[meta_file] = (stamp[0], stamp[1], "{not json")
    assert walker.load_meta_by_path(meta_file) == PAYLOAD


def test_missing_file_yields_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(walker, "resolve_coord_index_path", lambda p: Path(p))
    assert walker.load_meta_by_path(str(tmp_path / "nope.json")) == {}


def test_non_dict_json_yields_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "list.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    monkeypatch.setattr(walker, "resolve_coord_index_path", lambda p: Path(p))
    assert walker.load_meta_by_path(str(path)) == {}
