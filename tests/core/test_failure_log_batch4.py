"""Batch 4 (11 map-miss rows) — failure-log emits per FAILURE_LOG_MAPMISS_SWEEP.md."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from guardian.telemetry import writer
from memory.encoding_journal import journal_open, recover_unclosed
from memory.promotion import commit_flagged, commit_promotion, commit_quarantine, create_staging
from memory.supersedes import link_supersedes


def _failure_records():
    p = writer.FAILURE_LOG
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def _has_failure(
    *, reason: str | None = None, phase: str | None = None, status: str | None = None
) -> bool:
    for rec in _failure_records():
        if reason is not None and rec.get("reason") != reason:
            continue
        if phase is not None and rec.get("phase") != phase:
            continue
        if status is not None and rec.get("status") != status:
            continue
        return True
    return False


@pytest.fixture
def isolated_memory(tmp_path, monkeypatch):
    """Hermetic memory tree for promotion / journal tests."""
    import memory.engine.validate_write as vw
    import memory.engine.walker as walker
    import memory.encoding_journal as ej
    import memory.promotion as prom

    staging_dir = tmp_path / "staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    mem_root = tmp_path / "memory"
    mem_root.mkdir(parents=True, exist_ok=True)
    (mem_root / "tmp_writes").mkdir(parents=True, exist_ok=True)
    (mem_root / "indexes").mkdir(parents=True, exist_ok=True)
    index_path = mem_root / "indexes" / "coord_index.json"
    index_path.write_text("{}", encoding="utf-8")
    quarantine_dir = tmp_path / "quarantine"
    quarantine_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(prom, "STAGING_DIR", staging_dir)
    monkeypatch.setattr(prom, "QUARANTINE_DIR", quarantine_dir)
    monkeypatch.setattr(prom, "QUARANTINE_INDEX", quarantine_dir / "quarantine_index.json")
    monkeypatch.setattr(vw, "MEMORY_ROOT", mem_root)
    monkeypatch.setattr(vw, "TMP_DIR", mem_root / "tmp_writes")
    monkeypatch.setattr(vw, "INDEX_PATH", index_path)
    monkeypatch.setattr(walker, "MEMORY_ROOT", mem_root)
    monkeypatch.setattr(walker, "INDEX_PATH", index_path)
    ej.JOURNAL_PATH = tmp_path / "encoding_journal.jsonl"
    with walker._COORD_INDEX_LOCK:
        walker.COORD_INDEX.clear()
    walker._coord_index_mtime = 0.0
    return tmp_path


def _staging_envelope(*, suffix: str | None = None) -> dict:
    suffix = suffix or uuid.uuid4().hex[:8]
    mem_id = f"9.01.{suffix}"
    return {
        "cycle_id": f"cycle-{suffix}",
        "id": mem_id,
        "coord": [9, 1, 0],
        "type": "episode",
        "payload": {
            "id": mem_id,
            "title": f"ep-{suffix}",
            "summary": f"episode {suffix}",
            "text": json.dumps({"episode_id": f"ep-{suffix}", "intent": "LEARN"}),
        },
        "source": "lobe:temporal",
        "confidence": 0.5,
        "provenance": [],
        "timestamp_created": None,
        "decay_policy": None,
        "contradiction_state": None,
        "permissions": {},
        "version": 1,
        "strength_index": 0.5,
    }


# Row P0-1 — commit_promotion atomic_write failure
def test_commit_promotion_atomic_write_failed_is_logged(isolated_memory, monkeypatch):
    staging_ref = create_staging(_staging_envelope())

    def _boom(*_a, **_kw):
        raise OSError("atomic_write induced")

    monkeypatch.setattr("memory.promotion.atomic_write", _boom)
    committed, ref, _ = commit_promotion(staging_ref, "test-approver", skip_embed=True)
    assert committed is False
    assert ref is None
    assert _has_failure(phase="commit_promotion", reason="atomic_write_failed", status="fail")


# Row P0-2 — commit_flagged atomic_write failure
def test_commit_flagged_atomic_write_failed_is_logged(isolated_memory, monkeypatch):
    staging_ref = create_staging(_staging_envelope())

    def _boom(*_a, **_kw):
        raise OSError("atomic_write induced")

    monkeypatch.setattr("memory.promotion.atomic_write", _boom)
    committed, ref, _ = commit_flagged(
        staging_ref,
        "test-approver",
        flag_reason="ambiguous",
        response_mode="hedge",
    )
    assert committed is False
    assert ref is None
    assert _has_failure(phase="commit_flagged", reason="atomic_write_failed", status="fail")


# Row P1-3 — commit_quarantine file write failure
def test_commit_quarantine_write_failed_is_logged(isolated_memory, monkeypatch):
    staging_ref = create_staging(_staging_envelope())

    def _boom(*_a, **_kw):
        raise OSError("quarantine write induced")

    monkeypatch.setattr("memory.promotion.os.replace", _boom)
    committed, ref, _ = commit_quarantine(
        staging_ref,
        "test-approver",
        quarantine_reason="attack",
        response_mode="block",
    )
    assert committed is False
    assert _has_failure(phase="commit_quarantine", reason="quarantine_write_failed", status="fail")


# Row P1-4 — commit_quarantine index update failure
def test_commit_quarantine_index_update_failed_is_logged(isolated_memory, monkeypatch):
    staging_ref = create_staging(_staging_envelope())
    call_count = {"n": 0}
    real_replace = __import__("os").replace

    def _replace_once(src, dst):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise OSError("index replace induced")
        return real_replace(src, dst)

    monkeypatch.setattr("memory.promotion.os.replace", _replace_once)
    committed, ref, _ = commit_quarantine(
        staging_ref,
        "test-approver",
        quarantine_reason="attack",
        response_mode="block",
    )
    assert committed is True
    assert _has_failure(
        phase="commit_quarantine", reason="quarantine_index_update_failed", status="degraded"
    )


# Row P1-5 — encoding_journal _append OSError
def test_encoding_journal_append_failed_is_logged(tmp_path, monkeypatch):
    import memory.encoding_journal as ej

    ej.JOURNAL_PATH = tmp_path / "encoding_journal.jsonl"
    real_open = open

    def _open(path, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if Path(path).resolve() == ej.JOURNAL_PATH.resolve() and "a" in str(mode):
            raise OSError("journal append induced")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", _open)
    journal_open("mem-1", "staging-1", "/fake/path.json")
    assert _has_failure(
        phase="encoding_journal_append", reason="journal_append_failed", status="fail"
    )


# Row P1-6 — recover_unclosed rollback_fn raises
def test_encoding_journal_recovery_rollback_failed_is_logged(tmp_path):
    import memory.encoding_journal as ej

    ej.JOURNAL_PATH = tmp_path / "encoding_journal.jsonl"
    jid = ej.journal_open("0.00.0099", "staging-rec", "/path/rec.json")
    ej.journal_step(jid, "0.00.0099", "atomic_write")

    def _boom(_mem, _path):
        raise RuntimeError("rollback induced")

    recover_unclosed(_boom)
    assert _has_failure(phase="encoding_journal_recovery", reason="rollback_failed", status="fail")


# Row P1-7 — link_supersedes write_new failure
def test_supersedes_write_new_failed_is_logged(tmp_path):
    bad_new_path = tmp_path / "is_a_directory"
    bad_new_path.mkdir()
    prior_path = tmp_path / "prior.json"
    prior = {"id": "p1", "coord": [1, 0, 0], "title": "v1", "version": 1}
    new = {"id": "n1", "coord": [1, 0, 0], "title": "v2"}
    prior_path.write_text(json.dumps(prior), encoding="utf-8")

    report = link_supersedes(
        new_meta_path=str(bad_new_path),
        new_meta=new,
        prior_meta=prior,
        prior_path=str(prior_path),
    )
    assert report["ok"] is False
    assert report["phase"] == "write_new"
    assert _has_failure(phase="supersedes_link", reason="write_new_failed", status="fail")


# Row P1-8 — link_supersedes write_prior failure
def test_supersedes_write_prior_failed_is_logged(tmp_path):
    new_path = tmp_path / "new.json"
    bad_prior_path = tmp_path / "prior_is_dir"
    bad_prior_path.mkdir()
    prior = {"id": "p1", "coord": [1, 0, 0], "title": "v1", "version": 1}
    new = {"id": "n1", "coord": [1, 0, 0], "title": "v2"}
    new_path.write_text(json.dumps(new), encoding="utf-8")

    report = link_supersedes(
        new_meta_path=str(new_path),
        new_meta=new,
        prior_meta=prior,
        prior_path=str(bad_prior_path),
    )
    assert report["ok"] is False
    assert report["phase"] == "write_prior"
    assert _has_failure(phase="supersedes_link", reason="write_prior_failed", status="fail")


# Row P2-9 — entity_linker back-link write failure
def test_entity_linker_back_link_write_failed_is_logged(tmp_path, monkeypatch):
    from memory.enrichment.entity_linker import link_personal_memories
    import memory.engine.walker as walker

    monkeypatch.setattr(walker, "MEMORY_ROOT", tmp_path)

    new_path = tmp_path / "new.json"
    other_path = tmp_path / "other.json"
    new_mem = {
        "id": "1.00.new1",
        "coord": [1, 0, 0],
        "text": "Alice knows Bob",
        "links": [],
        "enrichment": {"entities": [{"name": "Bob", "facet": "name"}]},
    }
    other_mem = {
        "id": "1.00.bob1",
        "coord": [1, 0, 1],
        "text": "Bob here",
        "links": [],
        "enrichment": {"entities": [{"name": "Bob", "facet": "name"}]},
    }
    new_path.write_text(json.dumps(new_mem), encoding="utf-8")
    other_path.write_text(json.dumps(other_mem), encoding="utf-8")
    coord_index = {
        "1.00.new1": {"id": "1.00.new1", "coord": [1, 0, 0], "path": str(new_path)},
        "1.00.bob1": {"id": "1.00.bob1", "coord": [1, 0, 1], "path": str(other_path)},
    }
    # Cut 4 rework 4: canonical rewrites are exact-path atomic replacements;
    # inject the failure at the atomic replace boundary of the peer file.
    import memory.atomic_replace as ar

    real_replace = ar._replace

    def _replace(tmp, target):
        if Path(target).resolve() == other_path.resolve():
            raise OSError("back link write induced")
        return real_replace(tmp, target)

    monkeypatch.setattr(ar, "_replace", _replace)
    link_personal_memories(str(new_path), coord_index)
    assert _has_failure(phase="entity_linker", reason="back_link_write_failed", status="degraded")


# Row P2-10 — entity_linker forward-link write failure
def test_entity_linker_forward_link_write_failed_is_logged(tmp_path, monkeypatch):
    from memory.enrichment.entity_linker import link_personal_memories
    import memory.engine.walker as walker

    monkeypatch.setattr(walker, "MEMORY_ROOT", tmp_path)

    new_path = tmp_path / "new.json"
    other_path = tmp_path / "other.json"
    new_mem = {
        "id": "1.00.new1",
        "coord": [1, 0, 0],
        "text": "Alice knows Bob",
        "links": [],
        "enrichment": {"entities": [{"name": "Bob", "facet": "name"}]},
    }
    other_mem = {
        "id": "1.00.bob1",
        "coord": [1, 0, 1],
        "text": "Bob here",
        "links": [],
        "enrichment": {"entities": [{"name": "Bob", "facet": "name"}]},
    }
    new_path.write_text(json.dumps(new_mem), encoding="utf-8")
    other_path.write_text(json.dumps(other_mem), encoding="utf-8")
    coord_index = {
        "1.00.new1": {"id": "1.00.new1", "coord": [1, 0, 0], "path": str(new_path)},
        "1.00.bob1": {"id": "1.00.bob1", "coord": [1, 0, 1], "path": str(other_path)},
    }
    # Cut 4 rework 4: inject at the atomic replace boundary of the new record
    import memory.atomic_replace as ar

    real_replace = ar._replace

    def _replace(tmp, target):
        if Path(target).resolve() == new_path.resolve():
            raise OSError("forward link write induced")
        return real_replace(tmp, target)

    monkeypatch.setattr(ar, "_replace", _replace)
    link_personal_memories(str(new_path), coord_index)
    assert _has_failure(
        phase="entity_linker", reason="forward_link_write_failed", status="degraded"
    )


# Row P2-11 — strength_index shutdown flush failure
def test_strength_index_shutdown_flush_failed_is_logged(monkeypatch):
    import memory.strength_index as si_mod

    class _BoomIndex:
        def save(self, *, force: bool = False):
            raise OSError("shutdown flush induced")

    monkeypatch.setattr(si_mod, "_singleton", _BoomIndex())
    si_mod._flush_singleton("test_shutdown")
    assert _has_failure(
        phase="strength_index_flush", reason="shutdown_flush_failed", status="degraded"
    )


# Row P2-12 — face enrollment queue admission failure
def test_face_enrollment_admission_failure_preserves_cause(monkeypatch):
    from runtime.face import recognition as face_rec

    emb = np.random.RandomState(3).randn(512).astype(np.float32)
    ident = {
        "ok": True,
        "degraded": False,
        "embedding": emb.tolist(),
        "bbox": [0, 0, 10, 10],
        "name": None,
        "confidence": 0.0,
    }

    def _boom(*_a, **_kw):
        raise OSError("face admission induced")

    with (
        patch.object(face_rec, "_identify_impl", return_value=ident),
    ):
        with patch("pressure_loop.operator_vision.queue_face_memory", side_effect=_boom):
            with pytest.raises(OSError, match="face admission induced"):
                face_rec.enroll(b"jpeg-bytes", "Noah")
