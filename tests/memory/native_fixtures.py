"""Shared fixtures for native memory citizenship tests."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest


@pytest.fixture
def isolated_memory(tmp_path, monkeypatch):
    """Hermetic memory tree for the real native encoding pipeline."""
    import core.paths as paths
    import memory.engine.validate_write as vw
    import memory.engine.walker as walker
    import memory.encoding_journal as ej
    import memory.promotion as prom

    monkeypatch.setattr(paths, "OMEGA_ROOT", tmp_path)
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    mem_root = tmp_path / "memory"
    mem_root.mkdir(parents=True, exist_ok=True)
    (mem_root / "tmp_writes").mkdir(parents=True, exist_ok=True)
    (mem_root / "indexes").mkdir(parents=True, exist_ok=True)
    index_path = mem_root / "indexes" / "coord_index.json"
    index_path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(prom, "STAGING_DIR", staging_dir)
    monkeypatch.setattr(vw, "MEMORY_ROOT", mem_root)
    monkeypatch.setattr(vw, "TMP_DIR", mem_root / "tmp_writes")
    monkeypatch.setattr(vw, "INDEX_PATH", index_path)
    monkeypatch.setattr(walker, "MEMORY_ROOT", mem_root)
    monkeypatch.setattr(walker, "INDEX_PATH", index_path)
    # <profile>/guardian/state/: the journal derives the profile its relative canonical paths
    # resolve against from this location. A journal at tmp_path/ named the pytest base dir
    # as the profile; monkeypatch so the next test never inherits this tree's journal.
    monkeypatch.setattr(ej, "JOURNAL_PATH", tmp_path / "guardian" / "state" / "encoding_journal.jsonl")
    with walker._COORD_INDEX_LOCK:
        walker.COORD_INDEX.clear()
    walker._coord_index_mtime = 0.0
    return tmp_path


def _lobe9_memory_dict(*, suffix: str | None = None) -> dict:
    from memory.engine import validate_write as vw
    from memory.source_ledger.store import record_source

    suffix = suffix or uuid.uuid4().hex[:8]
    mem_id = f"9.01.{suffix}"
    ingested_at = "2026-06-07T12:00:00+00:00"
    text = json.dumps({"episode_id": f"ep-{suffix}", "intent": "LEARN"})
    artifact = record_source(
        text,
        source_type="lobe:temporal",
        origin_id=f"cycle-{suffix}",
        metadata={
            "source_session_id": f"cycle-{suffix}",
            "source_session_timestamp": ingested_at,
            "created_at": ingested_at,
        },
        root=vw.MEMORY_ROOT,
    )
    return {
        "id": mem_id,
        "coord": [9, 1, 0],
        "type": "episode",
        "title": f"ep-{suffix}",
        "summary": f"episode {suffix}",
        "text": text,
        "source": "lobe:temporal",
        "created": ingested_at,
        "modified": ingested_at,
        "tags": [],
        "provenance": {
            "origin_id": f"cycle-{suffix}",
            "origin_type": "test",
            "author": "native_memory_test",
            "ingested_at": ingested_at,
            "confidence": 0.5,
            **artifact.citation(),
        },
        "flags": {"canonical": False, "private": False},
        "audit": [{"when": ingested_at, "what": "ingested", "by": "native_memory_test"}],
    }


def _identity_memory_dict(*, suffix: str | None = None) -> dict:
    from memory.engine import validate_write as vw
    from memory.source_ledger.store import record_source

    suffix = suffix or uuid.uuid4().hex[:8]
    mem_id = f"0.99.{suffix}"
    ingested_at = "2026-06-07T12:00:00+00:00"
    text = "I prefer rigor over speed in testing"
    artifact = record_source(
        text,
        source_type="lobe:identity",
        origin_id=f"cycle-{suffix}",
        metadata={
            "source_session_id": f"cycle-{suffix}",
            "source_session_timestamp": ingested_at,
            "created_at": ingested_at,
        },
        root=vw.MEMORY_ROOT,
    )
    return {
        "id": mem_id,
        "coord": [0, 99, 0],
        "type": "concept",
        "title": f"id-{suffix}",
        "summary": "identity test",
        "text": text,
        "source": "test",
        "created": ingested_at,
        "modified": ingested_at,
        "tags": [],
        "provenance": {
            "origin_id": f"cycle-{suffix}",
            "origin_type": "test",
            "author": "native_memory_test",
            "ingested_at": ingested_at,
            "confidence": 0.7,
            **artifact.citation(),
        },
        "flags": {"canonical": False, "private": False},
        "audit": [{"when": ingested_at, "what": "ingested", "by": "native_memory_test"}],
    }


def _env_for(mem: dict) -> dict:
    return {
        "cycle_id": f"native-{mem['id']}",
        "id": mem["id"],
        "coord": mem["coord"],
        "type": mem["type"],
        "payload": {
            "id": mem["id"],
            "title": mem["title"],
            "summary": mem["summary"],
            "text": mem["text"],
        },
        "source": mem["source"],
        "confidence": (mem.get("provenance") or {}).get("confidence", 0.5),
        "provenance": [],
        "timestamp_created": None,
        "decay_policy": None,
        "contradiction_state": None,
        "permissions": {},
        "version": 1,
        "strength_index": 0.5,
    }


def _committed_path(memory_id: str):
    from memory.engine.walker import COORD_INDEX, load_coord_index, resolve_coord_index_path

    load_coord_index()
    raw = (COORD_INDEX.get(memory_id) or {}).get("path")
    if not raw:
        return None
    resolved = resolve_coord_index_path(raw)
    return str(resolved) if resolved is not None else None
