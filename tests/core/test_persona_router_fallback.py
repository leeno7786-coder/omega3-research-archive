"""Persona router rejects failed canonical writes without alternate stores."""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.usefixtures("deepsleep_sram_drain")


def test_add_memory_for_person_does_not_retry_with_skip_embed(monkeypatch, tmp_path):
    from core import persona_router as pr

    persona_root = tmp_path / "memory" / "001_personas" / "person_stub" / "bob-jones"
    persona_root.mkdir(parents=True)
    monkeypatch.setattr(pr, "_ensure_persona_dir", lambda _pid: persona_root)
    monkeypatch.setattr(pr, "route_persona", lambda _name: (1, 1, "bob-jones"))

    class _RejectedReceipt:
        validation_status = "rejected"
        path = None
        staging_ref = None

    calls = []

    def _fake_write(env, *, propose=True, bypass_guardian=False, skip_embed=False, **_kwargs):
        calls.append(
            {
                "coord": list(env.coord),
                "type": env.type,
                "domain": env.payload.get("domain"),
                "subtopic": env.payload.get("subtopic"),
                "propose": propose,
                "bypass_guardian": bypass_guardian,
                "skip_embed": skip_embed,
            }
        )
        return _RejectedReceipt()

    monkeypatch.setattr("memory.api.write_memory", _fake_write)

    result = pr.add_memory_for_person(
        "Bob Jones",
        {"text": "Bob works remotely"},
        cycle_id="test-cycle",
    )

    assert result is None
    assert calls == [
        {
            "coord": [1, 1],
            "type": "persona_note",
            "domain": "bob-jones",
            "subtopic": "persona_note",
            "propose": False,
            "bypass_guardian": True,
            "skip_embed": False,
        },
    ]
    assert list(persona_root.rglob("*.json")) == []


def test_add_memory_for_person_returns_none_when_canonical_fallback_rejects(monkeypatch, tmp_path):
    from core import persona_router as pr

    persona_root = tmp_path / "memory" / "001_personas" / "person_stub" / "bob-jones"
    persona_root.mkdir(parents=True)
    monkeypatch.setattr(pr, "_ensure_persona_dir", lambda _pid: persona_root)
    monkeypatch.setattr(pr, "route_persona", lambda _name: (1, 1, "bob-jones"))

    class _RejectedReceipt:
        validation_status = "rejected"
        path = None
        staging_ref = None
        message = "rejected in test"

    monkeypatch.setattr("memory.api.write_memory", lambda *_args, **_kwargs: _RejectedReceipt())

    result = pr.add_memory_for_person(
        "Bob Jones",
        {"text": "Bob works remotely"},
        cycle_id="test-cycle",
    )

    assert result is None
    assert list(persona_root.rglob("*.json")) == []


def test_search_by_decimal_returns_verified_records_by_default(tmp_path, monkeypatch):
    from core import persona_router as pr
    import memory.engine.validate_write as validate_write
    import memory.engine.walker as walker
    from memory.core import build_memory_dict

    memory_root = tmp_path / "memory"
    index_path = memory_root / "indexes" / "coord_index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text("{}", encoding="utf-8")
    (memory_root / "tmp_writes").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(pr, "OMEGA_ROOT", tmp_path)
    monkeypatch.setattr(validate_write, "MEMORY_ROOT", memory_root)
    monkeypatch.setattr(validate_write, "TMP_DIR", memory_root / "tmp_writes")
    monkeypatch.setattr(validate_write, "INDEX_PATH", index_path)
    monkeypatch.setattr(walker, "MEMORY_ROOT", memory_root)
    monkeypatch.setattr(walker, "INDEX_PATH", index_path)
    with walker._COORD_INDEX_LOCK:
        walker.COORD_INDEX.clear()
    walker._coord_index_mtime = 0.0

    record = build_memory_dict(
        "Alice likes hiking.",
        [1, 1, 1],
        "test:persona",
        metadata={
            "title": "Alice preference",
            "summary": "Alice likes hiking.",
            "domain": "alice",
            "subtopic": "persona_note",
            "type": "concept",
        },
    )
    validate_write.atomic_write(record, update_index=True)

    raw_dir = pr._ensure_persona_dir("alice") / "raw_note"
    raw_dir.mkdir(parents=True)
    (raw_dir / "raw.json").write_text(
        json.dumps({"id": "raw", "text": "unverified"}), encoding="utf-8"
    )

    results = pr.search_by_decimal((1, 1, "alice"))

    assert [item["id"] for item in results] == [record["id"]]
    assert results[0]["grounding_authority"] == "verified_persona_decimal"
    assert results[0]["recall_verdict"]["status"] == "verified"


def test_search_by_decimal_marks_raw_files_as_inspection_only(tmp_path, monkeypatch):
    from core import persona_router as pr
    import memory.engine.validate_write as validate_write
    import memory.engine.walker as walker

    memory_root = tmp_path / "memory"
    index_path = memory_root / "indexes" / "coord_index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(pr, "OMEGA_ROOT", tmp_path)
    monkeypatch.setattr(validate_write, "MEMORY_ROOT", memory_root)
    monkeypatch.setattr(validate_write, "INDEX_PATH", index_path)
    monkeypatch.setattr(walker, "MEMORY_ROOT", memory_root)
    monkeypatch.setattr(walker, "INDEX_PATH", index_path)
    with walker._COORD_INDEX_LOCK:
        walker.COORD_INDEX.clear()
    walker._coord_index_mtime = 0.0

    raw_dir = pr._ensure_persona_dir("bob") / "persona_note"
    raw_dir.mkdir(parents=True)
    (raw_dir / "raw.json").write_text(
        json.dumps({"id": "raw", "text": "Bob raw note"}), encoding="utf-8"
    )

    assert pr.search_by_decimal((1, 1, "bob")) == []

    raw_results = pr.search_by_decimal((1, 1, "bob"), include_unverified=True)
    assert [item["id"] for item in raw_results] == ["raw"]
    assert raw_results[0]["grounding_authority"] == "unverified_inspection_only"
    assert raw_results[0]["recall_verdict"]["status"] == "unknown"
