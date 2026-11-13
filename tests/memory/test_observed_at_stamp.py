"""Every committed record's real creation DATE is recorded in the observed_at sidecar (NOT the
record bytes, which stay deterministic content-hashes). Re-commit preserves it, and the sidecar
stays under the ISOLATED memory root.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.helpers.memory_isolation import redirect_memory_to_tmp

pytestmark = pytest.mark.usefixtures("deepsleep_sram_drain")


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    redirect_memory_to_tmp(tmp_path, monkeypatch)
    yield


def _commit(i: int):
    from memory.api import CanonicalEnvelope, write_memory

    env = CanonicalEnvelope(
        cycle_id=f"observed-{i}",
        coord=[2, 0, 0],
        type="knowledge",
        payload={
            "title": f"fact {i}",
            "summary": f"fact number {i}",
            "text": f"fact number {i}",
            "tags": ["observed_at_test"],
        },
        source="test.observed_at",
        confidence=0.8,
    )
    receipt = write_memory(env, propose=False, bypass_guardian=True, skip_embed=True)
    assert receipt.validation_status == "committed", receipt
    return receipt


def _mem_root(tmp_path) -> Path:
    return tmp_path / "_canonical_memory_isolation" / "memory"


def _sidecar(tmp_path) -> Path:
    return _mem_root(tmp_path) / "indexes" / "observed_at.json"


def _record(tmp_path, memory_id: str) -> dict:
    matches = list(_mem_root(tmp_path).rglob(f"{memory_id}.json"))
    assert matches, f"committed record {memory_id} not found"
    return json.loads(matches[0].read_text(encoding="utf-8"))


def test_committed_record_gets_an_observed_at_in_the_sidecar(tmp_path):
    r = _commit(1)
    sidecar = json.loads(_sidecar(tmp_path).read_text(encoding="utf-8"))
    assert r.path in sidecar
    from datetime import datetime

    datetime.fromisoformat(sidecar[r.path])  # a real wall-clock timestamp


def test_observed_at_is_NOT_written_into_the_record_bytes(tmp_path):
    # The deterministic-bytes invariant: wall-clock never enters the record itself.
    r = _commit(1)
    rec = _record(tmp_path, r.path)
    assert "observed_at" not in rec
    assert "observed_at" not in (rec.get("provenance") or {})


def test_recommit_preserves_the_observed_at(tmp_path):
    from memory.engine.validate_write import atomic_write

    r = _commit(1)
    original = json.loads(_sidecar(tmp_path).read_text(encoding="utf-8"))[r.path]
    atomic_write(_record(tmp_path, r.path), update_index=False)  # decay/embed re-write
    assert json.loads(_sidecar(tmp_path).read_text(encoding="utf-8"))[r.path] == original


def test_sidecar_stays_under_the_isolated_root(tmp_path):
    _commit(1)
    assert _sidecar(tmp_path).exists(), "observed_at sidecar escaped the isolated root"
