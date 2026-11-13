"""Every committed record is stamped once with a monotonic write-ordinal in
provenance — the recency ordering signal, since record timestamps are deterministic
content-hashes. Re-commit preserves it (decay / embed re-writes must not bump a
record's learned-order), and the counter file stays under the ISOLATED memory root.
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
        cycle_id=f"ordinal-{i}",
        coord=[2, 0, 0],
        type="knowledge",
        payload={
            "title": f"fact {i}",
            "summary": f"fact number {i}",
            "text": f"fact number {i}",
            "tags": ["ordinal_test"],
        },
        source="test.write_ordinal",
        confidence=0.8,
    )
    receipt = write_memory(env, propose=False, bypass_guardian=True, skip_embed=True)
    assert receipt.validation_status == "committed", receipt
    return receipt


def _mem_root(tmp_path) -> Path:
    return tmp_path / "_canonical_memory_isolation" / "memory"


def _load_by_id(tmp_path, memory_id: str) -> tuple[Path, dict]:
    # write_memory's receipt carries the memory ID, not a filesystem path — resolve
    # the committed record by globbing the isolated tree.
    matches = list(_mem_root(tmp_path).rglob(f"{memory_id}.json"))
    assert matches, f"committed record {memory_id} not found under {_mem_root(tmp_path)}"
    return matches[0], json.loads(matches[0].read_text(encoding="utf-8"))


def test_committed_records_carry_increasing_write_ordinal(tmp_path):
    r1 = _commit(1)
    r2 = _commit(2)
    o1 = _load_by_id(tmp_path, r1.path)[1]["provenance"]["write_ordinal"]
    o2 = _load_by_id(tmp_path, r2.path)[1]["provenance"]["write_ordinal"]
    assert isinstance(o1, int) and isinstance(o2, int)
    assert o2 > o1, f"second write should have a higher ordinal ({o1} -> {o2})"


def test_recommit_preserves_the_ordinal(tmp_path):
    from memory.engine.validate_write import atomic_write

    path, rec = _load_by_id(tmp_path, _commit(1).path)
    original = rec["provenance"]["write_ordinal"]
    # Re-write the SAME record (as a decay / embed-backfill re-write would).
    atomic_write(rec, update_index=False)
    assert json.loads(path.read_text(encoding="utf-8"))["provenance"]["write_ordinal"] == original


def test_counter_file_stays_under_isolated_root(tmp_path):
    _commit(1)
    isolated = (
        tmp_path / "_canonical_memory_isolation" / "memory" / "indexes" / "write_ordinal.json"
    )
    assert isolated.exists(), "write-ordinal counter escaped the isolated root"
