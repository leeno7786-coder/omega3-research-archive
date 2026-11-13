"""NPU packed-plane client guards (memory/dme/matmul_engine.py, task #17).

The NPU must never score a DIFFERENT plane than the numpy path would. These
tests pin the guards that make that impossible, and that no HTTP ever fires
when a guard rejects the call.
"""

import os

import numpy as np
import pytest

from memory.dme import matmul_engine
from memory.dme.lobe_layout import ENTRY_WORDS
from memory.dme.packer import pack_entry


@pytest.fixture()
def npu_client_state(monkeypatch):
    """Isolated client state: NPU enabled, nothing registered, probe cold."""
    monkeypatch.setenv("OMEGA_DME_NPU", "1")
    monkeypatch.setattr(
        matmul_engine, "_registered", {"path": "", "rows": 0, "sig": None}
    )
    monkeypatch.setattr(matmul_engine, "_baked", {"sig": None})
    monkeypatch.setattr(matmul_engine, "_probe", {"at": 0.0, "ok": False})
    monkeypatch.setattr(matmul_engine, "_ACTIVE_BACKEND", None)

    def no_http(*a, **k):
        raise AssertionError("guarded call must not dial the NPU worker")

    monkeypatch.setattr(matmul_engine, "_http", no_http)
    return monkeypatch


def _query():
    return pack_entry(0, 0.0, "query", [(2, 0, 1)])


def test_npu_client_defaults_to_omega_local_services():
    assert matmul_engine.npu_service_base_url() == "http://127.0.0.1:11434"


def test_npu_client_resolves_the_local_services_endpoint_per_request(
    tmp_path, npu_client_state, monkeypatch
):
    plane = tmp_path / "sram.bin"
    plane.write_bytes(np.zeros((3, ENTRY_WORDS), dtype=np.uint64).tobytes())
    matmul_engine.register_plane(plane, 3)
    calls = []

    def fake_http(url, payload=None, *, timeout):
        calls.append(url)
        if url.endswith("/packed/health"):
            return {"ok": True}
        if url.endswith("/packed/plane"):
            return {"ok": True}
        return {"ok": True, "overlaps": [[1, 0, 0]]}

    monkeypatch.setattr(matmul_engine, "_http", fake_http)
    monkeypatch.setenv("OMEGA_LOCAL_SERVICES_URL", "http://127.0.0.1:22111")
    assert matmul_engine._npu_overlaps(3, _query()) is not None

    assert calls == [
        "http://127.0.0.1:22111/dme/packed/health",
        "http://127.0.0.1:22111/dme/packed/plane",
        "http://127.0.0.1:22111/dme/packed/overlap",
    ]


def test_npu_client_waits_for_the_cold_vitis_plane_load(
    tmp_path, npu_client_state, monkeypatch
):
    plane = tmp_path / "sram.bin"
    plane.write_bytes(np.zeros((3, ENTRY_WORDS), dtype=np.uint64).tobytes())
    matmul_engine.register_plane(plane, 3)
    calls = []

    def fake_http(url, payload=None, *, timeout):
        calls.append((url, timeout))
        if url.endswith("/packed/health"):
            return {"ok": True}
        if url.endswith("/packed/plane"):
            return {"ok": True}
        return {"ok": True, "overlaps": [[1, 0, 0]]}

    monkeypatch.setattr(matmul_engine, "_http", fake_http)
    assert matmul_engine._npu_overlaps(3, _query()) is not None

    plane_load = next(timeout for url, timeout in calls if url.endswith("/packed/plane"))
    assert plane_load >= 1800.0


def test_unregistered_plane_never_dials(npu_client_state):
    assert matmul_engine._npu_overlaps(5, _query()) is None


def test_row_mismatch_never_dials(tmp_path, npu_client_state):
    plane = tmp_path / "sram.bin"
    plane.write_bytes(np.zeros((3, ENTRY_WORDS), dtype=np.uint64).tobytes())
    matmul_engine.register_plane(plane, 3)
    assert matmul_engine._npu_overlaps(5, _query()) is None


def test_stale_file_never_dials(tmp_path, npu_client_state):
    plane = tmp_path / "sram.bin"
    plane.write_bytes(np.zeros((3, ENTRY_WORDS), dtype=np.uint64).tobytes())
    matmul_engine.register_plane(plane, 3)
    # the file moves on after registration: the caller's array is older. The staleness
    # signature is (st_mtime_ns, st_size); a same-size rewrite inside one filesystem clock
    # tick (Windows: up to ~15 ms) would leave it unchanged, so advance the mtime explicitly.
    plane.write_bytes(np.ones((3, ENTRY_WORDS), dtype=np.uint64).tobytes())
    st = plane.stat()
    os.utime(plane, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert matmul_engine._npu_overlaps(3, _query()) is None


def test_kill_switch_never_dials(tmp_path, npu_client_state, monkeypatch):
    plane = tmp_path / "sram.bin"
    plane.write_bytes(np.zeros((3, ENTRY_WORDS), dtype=np.uint64).tobytes())
    matmul_engine.register_plane(plane, 3)
    monkeypatch.setenv("OMEGA_DME_NPU", "0")
    assert matmul_engine._npu_overlaps(3, _query()) is None


def test_register_plane_resets_bake_marker(tmp_path, npu_client_state):
    plane = tmp_path / "sram.bin"
    plane.write_bytes(np.zeros((3, ENTRY_WORDS), dtype=np.uint64).tobytes())
    matmul_engine._baked["sig"] = ("stale", 1)
    matmul_engine.register_plane(plane, 3)
    assert matmul_engine._baked["sig"] is None
    assert matmul_engine._registered["rows"] == 3
    assert matmul_engine._registered["sig"] is not None


def test_test_roots_do_not_register(tmp_path, npu_client_state):
    """rebuild with an explicit root must NOT touch live registration."""
    import json

    from memory.dme.adapter import rebuild_sram_from_graph
    from memory.taxonomy.compiler import compile_memory_graph

    # rebuild legitimately probes backend health; only registration is barred
    npu_client_state.setattr(matmul_engine, "_http", lambda *a, **k: None)

    memory_root = tmp_path / "memory"
    memory_root.mkdir(exist_ok=True)
    record = {
        "id": "k1",
        "coord": [2, 0, 1],
        "type": "concept",
        "title": "Neuron",
        "summary": "Neurons pass signals through synapses.",
        "domain": "biology",
        "tags": ["biology"],
        "enrichment": {"canonical": True},
    }
    (memory_root / "k1.json").write_text(json.dumps(record), encoding="utf-8")
    index_path = memory_root / "indexes" / "coord_index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        json.dumps({"k1": {"coord": [2, 0, 1], "path": "memory/k1.json"}}),
        encoding="utf-8",
    )
    compile_memory_graph(root=tmp_path)
    rebuild_sram_from_graph(root=tmp_path)

    assert matmul_engine._registered["path"] == ""


def test_compute_overlaps_falls_back_to_numpy(npu_client_state):
    """With nothing registered, scoring is exact numpy and says so."""
    sram = np.vstack(
        [
            pack_entry(1, 1.0, "user", [(2, 0, 1), (2, 0, 2)]),
            pack_entry(2, 2.0, "web", [(2, 0, 3)]),
        ]
    )
    query = pack_entry(0, 0.0, "query", [(2, 0, 1)])
    counts = matmul_engine.compute_overlaps(sram, query)
    assert counts.tolist() == [1, 0]
    assert matmul_engine.active_backend() == "numpy"
