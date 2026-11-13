"""Coordinate-index failures must never masquerade as empty recall."""

from __future__ import annotations

import json
import os
import threading
import time

import pytest

from memory.engine import reindex, walker
from core.required_substrate import RequiredSubstrateError
from memory.taxonomy import recall_graph


@pytest.fixture(autouse=True)
def _isolated_walker_state(tmp_path, monkeypatch):
    index_path = tmp_path / "memory" / "indexes" / "coord_index.json"
    monkeypatch.setattr(walker, "INDEX_PATH", index_path)
    with walker._COORD_INDEX_LOCK:
        walker.COORD_INDEX.clear()
        walker.COORD_INDEX["stale"] = {
            "id": "stale",
            "coord": [2, 1, 1],
            "path": "memory/stale.json",
        }
    walker._coord_index_mtime = 0.0
    walker._coord_index_generation = 0
    yield index_path
    with walker._COORD_INDEX_LOCK:
        walker.COORD_INDEX.clear()
    walker._coord_index_mtime = 0.0
    walker._coord_index_generation = 0


def test_missing_index_raises_typed_unavailable_instead_of_serving_stale(_isolated_walker_state):
    with pytest.raises(walker.CoordIndexUnavailable) as caught:
        walker.load_coord_index()

    assert caught.value.reason == "missing"
    assert caught.value.path == _isolated_walker_state


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ("{broken", "invalid_json"),
        ("[]", "invalid_root"),
        (json.dumps({"m1": {"coord": [2, 1, 1]}}), "invalid_entry"),
    ],
)
def test_invalid_index_raises_typed_unavailable(_isolated_walker_state, body, reason):
    _isolated_walker_state.parent.mkdir(parents=True)
    _isolated_walker_state.write_text(body, encoding="utf-8")

    with pytest.raises(walker.CoordIndexUnavailable) as caught:
        walker.load_coord_index()

    assert caught.value.reason == reason


def test_valid_empty_index_is_distinguishable_from_unavailable(_isolated_walker_state):
    _isolated_walker_state.parent.mkdir(parents=True)
    _isolated_walker_state.write_text("{}", encoding="utf-8")

    assert walker.load_coord_index() == {}
    assert walker.coord_index_snapshot() == {}


def test_invalid_replacement_clears_the_previous_in_process_snapshot(
    _isolated_walker_state,
):
    _isolated_walker_state.parent.mkdir(parents=True)
    _isolated_walker_state.write_text(
        json.dumps(
            {
                "m1": {
                    "id": "m1",
                    "coord": [2, 1, 1],
                    "path": "memory/m1.json",
                }
            }
        ),
        encoding="utf-8",
    )
    walker.load_coord_index()
    assert "m1" in walker.coord_index_snapshot()

    time.sleep(0.01)
    _isolated_walker_state.write_text("{broken", encoding="utf-8")

    with pytest.raises(walker.CoordIndexUnavailable) as caught:
        walker.load_coord_index()

    assert caught.value.reason == "invalid_json"
    assert walker.coord_index_snapshot() == {}


def test_same_timestamp_invalid_replacement_cannot_serve_stale_snapshot(
    _isolated_walker_state,
):
    _isolated_walker_state.parent.mkdir(parents=True)
    _isolated_walker_state.write_text(
        json.dumps(
            {
                "m1": {
                    "id": "m1",
                    "coord": [2, 1, 1],
                    "path": "memory/m1.json",
                }
            }
        ),
        encoding="utf-8",
    )
    walker.load_coord_index()
    original_stat = _isolated_walker_state.stat()

    replacement = _isolated_walker_state.with_name("coord_index.replacement.json")
    replacement.write_text("{broken", encoding="utf-8")
    os.utime(
        replacement,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )
    os.replace(replacement, _isolated_walker_state)

    with pytest.raises(walker.CoordIndexUnavailable) as caught:
        walker.load_coord_index()

    assert caught.value.reason == "invalid_json"
    assert walker.coord_index_snapshot() == {}


def test_coord_index_reader_waits_for_atomic_writer_replace(
    _isolated_walker_state, monkeypatch
):
    """The stat/open reader window shares the lock held by the atomic writer."""
    import memory.engine.validate_write as validate_write

    index_path = _isolated_walker_state
    index_path.parent.mkdir(parents=True)
    index_path.write_text(
        json.dumps({"old": {"path": "memory/old.json", "coord": [0, 0, 1]}}),
        encoding="utf-8",
    )
    walker.load_coord_index()
    monkeypatch.setattr(validate_write, "INDEX_PATH", index_path)

    writer_entered = threading.Event()
    allow_replace = threading.Event()
    reader_started = threading.Event()
    reader_done = threading.Event()
    errors: list[BaseException] = []
    read_snapshots: list[dict] = []

    def paused_replace(meta):
        writer_entered.set()
        if not allow_replace.wait(3):
            raise TimeoutError("test did not release the index writer")
        current = json.loads(index_path.read_text(encoding="utf-8"))
        current[str(meta["id"])] = {
            "path": str(meta["path"]), "coord": list(meta["coord"])
        }
        temporary = index_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(current), encoding="utf-8")
        temporary.replace(index_path)

    monkeypatch.setattr(validate_write, "_patch_index_unsafe", paused_replace)

    def write_index():
        try:
            validate_write.patch_index({
                "id": "new", "path": "memory/new.json", "coord": [0, 0, 2]
            })
        except BaseException as exc:  # surface a worker failure on the test thread
            errors.append(exc)

    def read_index():
        reader_started.set()
        try:
            walker.load_coord_index()
            read_snapshots.append(walker.coord_index_snapshot())
        except BaseException as exc:
            errors.append(exc)
        finally:
            reader_done.set()

    writer = threading.Thread(target=write_index, name="coord-index-writer")
    reader = threading.Thread(target=read_index, name="coord-index-reader")
    writer.start()
    assert writer_entered.wait(2)
    reader.start()
    assert reader_started.wait(2)
    assert not reader_done.wait(0.05), "reader must wait for the shared replace lock"
    allow_replace.set()
    writer.join(3)
    reader.join(3)

    assert not writer.is_alive() and not reader.is_alive()
    assert errors == []
    assert len(read_snapshots) == 1 and "new" in read_snapshots[0]


def test_canonical_recall_propagates_index_unavailability(monkeypatch):
    unavailable = walker.CoordIndexUnavailable("missing", walker.INDEX_PATH)
    monkeypatch.setattr(recall_graph, "load_coord_index", lambda: (_ for _ in ()).throw(unavailable))
    monkeypatch.setattr(
        recall_graph,
        "coord_index_snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("stale snapshot served")),
    )

    with pytest.raises(walker.CoordIndexUnavailable) as caught:
        recall_graph.load_canonical_candidates()

    assert caught.value is unavailable


def test_reindex_refuses_to_publish_when_schema_is_unavailable(tmp_path, monkeypatch):
    memory_root = tmp_path / "memory"
    memory_root.mkdir()
    index_path = memory_root / "indexes" / "coord_index.json"
    monkeypatch.setattr(reindex, "SCHEMA_PATH", tmp_path / "missing-schema.json")

    with pytest.raises(RequiredSubstrateError, match="schema"):
        reindex.build_index(root=tmp_path)

    assert not index_path.exists()


def test_reindex_refuses_partial_publish_when_a_record_is_unreadable(tmp_path, monkeypatch):
    bad = tmp_path / "memory" / "200_knowledge" / "bad.json"
    bad.parent.mkdir(parents=True)
    bad.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(reindex, "find_json_files", lambda root=None: [bad])
    monkeypatch.setattr(reindex, "load_schema", lambda: object())

    with pytest.raises(RequiredSubstrateError, match="unreadable"):
        reindex.build_index(root=tmp_path)

    assert not (tmp_path / "memory" / "indexes" / "coord_index.json").exists()
