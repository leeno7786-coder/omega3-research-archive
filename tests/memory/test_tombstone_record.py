"""Reversible tombstone/archive primitive for contradiction losers.

A tombstone archives the record's bytes OUTSIDE the memory tree (reversible), unlinks the canonical
file (path-guarded to the memory tree), and unpatches the coord index. It refuses canonical records
(immune) and refuses anything outside the memory tree (path-traversal guard), and it archives
BEFORE it removes so a failed archive never loses a record.
"""

from __future__ import annotations

import json

import memory.tombstone as tomb


def _write(root, mid, doc):
    d = root / "1_lobe"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{mid}.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


def _patch(monkeypatch, root, path_map):
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("memory.engine.validate_write.MEMORY_ROOT", root, raising=False)
    monkeypatch.setattr("memory.engine.walker.path_for_id", lambda mid: path_map.get(mid))
    unpatched: list[str] = []
    monkeypatch.setattr(
        "memory.engine.validate_write.unpatch_index",
        lambda mid: unpatched.append(mid) or True,
    )
    return unpatched


def test_tombstone_archives_unlinks_and_unpatches(tmp_path, monkeypatch):
    root = tmp_path / "memory"
    doc = {
        "id": "1.00.kim",
        "coord": [1, 0, 0],
        "flags": {"canonical": False},
        "title": "Operator",
        "summary": "operator name is kim",
    }
    p = _write(root, "1.00.kim", doc)
    unpatched = _patch(monkeypatch, root, {"1.00.kim": str(p)})
    archive = tmp_path / "arch"

    out = tomb.tombstone_memory_record(
        "1.00.kim", reason="contradiction_loser", archive_dir=archive
    )

    assert out["tombstoned"] is True
    assert not p.exists()  # canonical file removed
    assert (archive / "1.00.kim.json").exists()  # bytes archived (reversible)
    assert unpatched == ["1.00.kim"]  # dropped from the coord index

    env = json.loads((archive / "1.00.kim.json").read_text(encoding="utf-8"))
    assert env["record"]["id"] == "1.00.kim"  # full record preserved for reversal
    assert env["reason"] == "contradiction_loser"


def test_tombstone_refuses_canonical(tmp_path, monkeypatch):
    root = tmp_path / "memory"
    doc = {"id": "1.00.noah", "coord": [1, 0, 0], "flags": {"canonical": True}}
    p = _write(root, "1.00.noah", doc)
    unpatched = _patch(monkeypatch, root, {"1.00.noah": str(p)})

    out = tomb.tombstone_memory_record("1.00.noah", archive_dir=tmp_path / "arch")

    assert out["tombstoned"] is False
    assert out["reason"] == "canonical_immune"
    assert p.exists()  # canonical record is untouched
    assert unpatched == []
    assert not (tmp_path / "arch").exists()  # not even archived


def test_tombstone_path_guard_refuses_outside_memory_root(tmp_path, monkeypatch):
    root = tmp_path / "memory"
    outside = tmp_path / "outside.json"
    doc = {"id": "9.9.x", "coord": [9, 9, 0]}
    outside.write_text(json.dumps(doc), encoding="utf-8")
    unpatched = _patch(monkeypatch, root, {"9.9.x": str(outside)})

    out = tomb.tombstone_memory_record("9.9.x", archive_dir=tmp_path / "arch")

    assert out["tombstoned"] is False
    assert out["reason"] == "outside_memory_root"
    assert outside.exists()  # nothing outside the tree is touched
    assert unpatched == []


def test_tombstone_missing_record(tmp_path, monkeypatch):
    root = tmp_path / "memory"
    _patch(monkeypatch, root, {})  # path_for_id returns None -> record not found

    out = tomb.tombstone_memory_record("nope", archive_dir=tmp_path / "arch")

    assert out["tombstoned"] is False
    assert out["reason"] in ("not_found", "path_unresolved")


def test_tombstone_aborts_before_unlink_when_archive_fails(tmp_path, monkeypatch):
    # Review finding 4: the binding reversibility invariant — archive BEFORE unlink. If the archive
    # write fails, the record must NOT be removed (no irreversible loss).
    root = tmp_path / "memory"
    doc = {
        "id": "1.00.kim",
        "coord": [1, 0, 0],
        "flags": {"canonical": False},
        "title": "Operator",
        "summary": "operator name is kim",
    }
    p = _write(root, "1.00.kim", doc)
    unpatched = _patch(monkeypatch, root, {"1.00.kim": str(p)})
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a dir", encoding="utf-8")
    archive = blocker / "sub"  # mkdir under a FILE raises OSError -> archive fails

    out = tomb.tombstone_memory_record("1.00.kim", archive_dir=archive)

    assert out["tombstoned"] is False
    assert out["reason"] == "archive_failed"
    assert out["file_removed"] is False
    assert p.exists()  # REVERSIBILITY: the record is never removed when the archive failed
    assert unpatched == []  # index untouched too
