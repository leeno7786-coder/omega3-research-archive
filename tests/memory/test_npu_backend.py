"""DME NPU backend: QDQ-u8-io overlap models, sharded bake, proof receipts.

The 2026-08-07 placement proof established: the Q->DQ->float-MatMul->Q->DQ sandwich at
unit scale fuses into a vitis_ai_ep partition on XDNA (0.015 ms/query at
batch 64), float-output forms fail VAIML compile, and the u8 output stage
clips at 255 so exactness requires query popcount <= 255.

CPU correctness tests use the brain environment. The final hardware test
opens baked tiles in the shipped Vitis child and verifies a profiled NPU probe
and exact serving arithmetic. It skips only for missing hardware/runtime.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from memory.dme import npu_backend

RNG = np.random.default_rng(42)


def test_default_tile_cache_is_inside_portable_omega_tree():
    cache_root = npu_backend.default_cache_root()

    assert cache_root == npu_backend.GUARDIAN_STATE_ROOT / "dme" / "npu_tiles"


def _bits(rows: int, cols: int, density: float = 0.1) -> np.ndarray:
    return (RNG.random((rows, cols)) < density).astype(np.uint8)


def _query(bits: int, popcount: int) -> np.ndarray:
    q = np.zeros((1, bits), dtype=np.uint8)
    q[0, RNG.choice(bits, size=popcount, replace=False)] = 1
    return q


class TestModelBuild:
    def test_builds_u8_io_sandwich(self):
        plane = _bits(512, 96)  # [K, N]
        model_bytes = npu_backend.build_overlap_model(plane, batch=4)
        import onnx

        model = onnx.load_from_string(model_bytes)
        ops = [n.op_type for n in model.graph.node]
        assert ops == [
            "DequantizeLinear", "DequantizeLinear", "MatMul",
            "QuantizeLinear", "DequantizeLinear",
        ], "must be the proven Q/DQ sandwich, nothing else"
        assert model.opset_import[0].version == 13
        assert model.ir_version == 8

    def test_rejects_non_binary_plane(self):
        plane = _bits(64, 8)
        plane[0, 0] = 2
        with pytest.raises(ValueError, match="binary"):
            npu_backend.build_overlap_model(plane, batch=1)


class TestShardedSession:
    def test_close_releases_all_onnx_sessions(self):
        class _Closable:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        sessions = [[_Closable(), _Closable()], [_Closable()]]
        session = object.__new__(npu_backend.ShardedOverlapSession)
        session._sessions = sessions
        session._shard_spans = [(0, 1)]
        session._k_spans = [(0, 1)]
        session._engagement = [{"proven": True}]
        session.tile_keys = ["tile"]

        session.close()

        assert all(item.closed for group in sessions for item in group)
        assert session._sessions == []
        assert session._shard_spans == []
        assert session._k_spans == []
        assert session._engagement == []
        assert session.tile_keys == []

    def test_exact_against_numpy_reference(self, tmp_path):
        k, n = 512, 300  # n not a multiple of shard width
        plane = _bits(k, n)
        sess = npu_backend.ShardedOverlapSession(
            plane, batch=2, shard_columns=128, cache_root=tmp_path,
        )
        q = np.vstack([_query(k, 40)[0], _query(k, 200)[0]]).astype(np.uint8)
        got = sess.overlap(q)
        want = q.astype(np.int32) @ plane.astype(np.int32)
        assert got.dtype == np.int64
        assert got.shape == (2, n)
        assert np.array_equal(got, want)

    def test_single_query_convenience_shape(self, tmp_path):
        k, n = 512, 64
        plane = _bits(k, n)
        sess = npu_backend.ShardedOverlapSession(
            plane, batch=1, shard_columns=64, cache_root=tmp_path,
        )
        q = _query(k, 10)
        got = sess.overlap(q)
        assert got.shape == (1, n)

    def test_popcount_guard_raises_over_clip_bound(self, tmp_path):
        k, n = 512, 64
        plane = _bits(k, n)
        sess = npu_backend.ShardedOverlapSession(
            plane, batch=1, shard_columns=64, cache_root=tmp_path,
        )
        q = _query(k, 256)  # > 255: u8 stage would clip silently
        with pytest.raises(ValueError, match="popcount"):
            sess.overlap(q)

    def test_batch_padding_for_short_final_batch(self, tmp_path):
        """3 queries through a batch-4 session: padded, then truncated."""
        k, n = 512, 64
        plane = _bits(k, n)
        sess = npu_backend.ShardedOverlapSession(
            plane, batch=4, shard_columns=64, cache_root=tmp_path,
        )
        q = np.vstack([_query(k, 5)[0] for _ in range(3)]).astype(np.uint8)
        got = sess.overlap(q)
        want = q.astype(np.int32) @ plane.astype(np.int32)
        assert got.shape == (3, n)
        assert np.array_equal(got, want)


class TestKChunkedSession:
    """K above the EP envelope must decompose into proven-width chunks.

    Measured 2026-08-07 on the embedded VitisAI ORT: K=261,952 direct is
    garbage at N=128 (max_abs err 69, 1023/1024 wrong) and crashes the EP
    at N>=256 — so wide-K planes are served by chunking K into proven-width
    slices and summing chunk outputs in int64 on the host. Exact because
    total popcount <= 255 bounds every chunk partial below the u8 clip.
    """

    def test_k_chunked_exactness(self, tmp_path):
        k, n, chunk = 1000, 96, 256  # k not a multiple of chunk
        plane = _bits(k, n)
        sess = npu_backend.ShardedOverlapSession(
            plane, batch=2, shard_columns=48, k_chunk=chunk,
            cache_root=tmp_path,
        )
        q = np.vstack([_query(k, 30)[0], _query(k, 250)[0]]).astype(np.uint8)
        got = sess.overlap(q)
        want = q.astype(np.int32) @ plane.astype(np.int32)
        assert np.array_equal(got, want)

    def test_k_chunk_count_recorded(self, tmp_path):
        plane = _bits(1000, 48)
        sess = npu_backend.ShardedOverlapSession(
            plane, batch=1, shard_columns=48, k_chunk=256, cache_root=tmp_path,
        )
        assert sess.k_chunks == 4  # ceil(1000/256)

    def test_unchunked_default_is_single_chunk(self, tmp_path):
        plane = _bits(512, 48)
        sess = npu_backend.ShardedOverlapSession(
            plane, batch=1, shard_columns=48, cache_root=tmp_path,
        )
        assert sess.k_chunks == 1


class TestContentAddressedTiles:
    """Each (k-chunk x n-shard) tile's model file and vaip cache key derive
    from the hash of THAT tile's plane slice — so an incremental reindex
    recompiles only the tiles whose bits actually changed, and identical
    content always cache-hits regardless of when it was baked."""

    def test_same_content_same_tile_paths(self, tmp_path):
        plane = _bits(512, 96)
        a = npu_backend.ShardedOverlapSession(
            plane, batch=1, shard_columns=48, k_chunk=256, cache_root=tmp_path,
        )
        b = npu_backend.ShardedOverlapSession(
            plane.copy(), batch=1, shard_columns=48, k_chunk=256,
            cache_root=tmp_path,
        )
        assert a.tile_keys == b.tile_keys
        assert len(a.tile_keys) == 4

    def test_changed_tile_changes_only_its_key(self, tmp_path):
        plane = _bits(512, 96)
        a = npu_backend.ShardedOverlapSession(
            plane, batch=1, shard_columns=48, k_chunk=256, cache_root=tmp_path,
        )
        mutated = plane.copy()
        mutated[0, 0] ^= 1  # flip one bit in tile (shard 0, chunk 0)
        b = npu_backend.ShardedOverlapSession(
            mutated, batch=1, shard_columns=48, k_chunk=256,
            cache_root=tmp_path,
        )
        diffs = [i for i, (x, y) in enumerate(zip(a.tile_keys, b.tile_keys))
                 if x != y]
        assert diffs == [0], "exactly the mutated tile re-keys"

    def test_batch_participates_in_key(self, tmp_path):
        """A cached compile is batch-shaped; a different batch must re-key."""
        plane = _bits(512, 48)
        a = npu_backend.ShardedOverlapSession(
            plane, batch=1, shard_columns=48, cache_root=tmp_path,
        )
        b = npu_backend.ShardedOverlapSession(
            plane, batch=4, shard_columns=48, cache_root=tmp_path,
        )
        assert a.tile_keys[0] != b.tile_keys[0]


class TestBakedTileManifest:
    """The Vitis lane splits bake from execution across two interpreters.

    The brain venv owns ``onnx`` and bakes content-addressed tile models; the
    cp312 Vitis child owns only ``onnxruntime`` and opens those files from a
    manifest.  The manifest is the contract between them, so it round-trips
    against the plane constructor and never needs ``onnx`` on the open side.
    """

    def test_manifest_keys_match_the_plane_constructor(self, tmp_path):
        plane = _bits(512, 96)
        manifest = npu_backend.bake_tiles(
            plane, batch=1, shard_columns=48, k_chunk=256, cache_root=tmp_path,
        )
        direct = npu_backend.ShardedOverlapSession(
            plane, batch=1, shard_columns=48, k_chunk=256, cache_root=tmp_path,
        )

        assert manifest["format"] == npu_backend.TILE_MANIFEST_FORMAT
        assert [tile["key"] for tile in manifest["tiles"]] == direct.tile_keys
        assert all((tmp_path / f"{tile['key']}.onnx").is_file() for tile in manifest["tiles"])

    def test_manifest_is_relocatable(self, tmp_path):
        manifest = npu_backend.bake_tiles(
            _bits(512, 48), batch=1, shard_columns=48, cache_root=tmp_path,
        )

        assert str(tmp_path) not in repr(manifest)

    def test_open_from_manifest_is_exact_without_importing_onnx(self, tmp_path, monkeypatch):
        k, n = 1000, 96
        plane = _bits(k, n)
        manifest = npu_backend.bake_tiles(
            plane, batch=2, shard_columns=48, k_chunk=256, cache_root=tmp_path,
        )

        import builtins

        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name == "onnx" or name.startswith("onnx."):
                raise AssertionError("the Vitis child must not need onnx to open tiles")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", guarded_import)
        sess = npu_backend.ShardedOverlapSession.from_manifest(manifest, cache_root=tmp_path)
        q = np.vstack([_query(k, 30)[0], _query(k, 250)[0]]).astype(np.uint8)

        assert np.array_equal(sess.overlap(q), q.astype(np.int32) @ plane.astype(np.int32))
        assert sess.k_chunks == 4

    def test_open_from_manifest_fails_on_an_unbaked_tile(self, tmp_path):
        manifest = npu_backend.bake_tiles(
            _bits(512, 48), batch=1, shard_columns=48, cache_root=tmp_path,
        )
        missing = tmp_path / f"{manifest['tiles'][0]['key']}.onnx"
        missing.unlink()

        with pytest.raises(FileNotFoundError, match="not baked"):
            npu_backend.ShardedOverlapSession.from_manifest(manifest, cache_root=tmp_path)

    def test_open_from_manifest_rejects_a_foreign_format(self, tmp_path):
        manifest = npu_backend.bake_tiles(
            _bits(512, 48), batch=1, shard_columns=48, cache_root=tmp_path,
        )
        manifest["format"] = "something/else"

        with pytest.raises(ValueError, match="manifest format"):
            npu_backend.ShardedOverlapSession.from_manifest(manifest, cache_root=tmp_path)

    def test_open_from_manifest_rejects_an_incomplete_tile_grid(self, tmp_path):
        manifest = npu_backend.bake_tiles(
            _bits(512, 96), batch=1, shard_columns=48, k_chunk=256, cache_root=tmp_path,
        )
        manifest["tiles"].pop()

        with pytest.raises(ValueError, match="tile grid"):
            npu_backend.ShardedOverlapSession.from_manifest(manifest, cache_root=tmp_path)


class TestEngagementVerification:
    """VitisAI engagement is flaky at session creation (measured: multi-
    session builds silently fail-safe to CPU with a registered EP). The
    bake-time answer is verify-and-retry: prove each session's placement
    and recreate fail-safed ones. On boxes without the EP, verification
    reports honestly and never raises."""

    def test_verify_on_cpu_box_reports_unavailable(self, tmp_path):
        plane = _bits(512, 96)
        sess = npu_backend.ShardedOverlapSession(
            plane, batch=1, shard_columns=48, k_chunk=256,
            cache_root=tmp_path, verify_npu=True,
        )
        report = sess.engagement_report()
        assert len(report) == 4  # 2 shards x 2 chunks
        assert all(e["proven"] is False for e in report)
        if not npu_backend.vitisai_available():
            assert all(e["reason"] == "ep_unavailable" for e in report)
        # verification must never break correctness
        q = _query(512, 20)
        assert np.array_equal(
            sess.overlap(q),
            q.astype(np.int32) @ plane.astype(np.int32),
        )

    def test_no_verify_by_default(self, tmp_path):
        plane = _bits(512, 48)
        sess = npu_backend.ShardedOverlapSession(
            plane, batch=1, shard_columns=48, cache_root=tmp_path,
        )
        assert sess.engagement_report() == []


class TestProofReceipt:
    def test_receipt_from_synthetic_profile_npu(self):
        events = [
            {"cat": "Node", "args": {"op_name": "vitis_ai_ep_1",
                                     "provider": "VitisAIExecutionProvider"}},
            {"cat": "Node", "args": {"op_name": "DequantizeLinear",
                                     "provider": "VitisAIExecutionProvider"}},
        ]
        receipt = npu_backend.placement_receipt_from_events(events)
        assert receipt["provider_execution_proven"] is True
        assert receipt["partition_on_npu"] is True
        assert receipt["matmul_on_cpu"] is False

    def test_receipt_from_synthetic_profile_fallback(self):
        events = [
            {"cat": "Node", "args": {"op_name": "MatMul",
                                     "provider": "CPUExecutionProvider"}},
        ]
        receipt = npu_backend.placement_receipt_from_events(events)
        assert receipt["provider_execution_proven"] is False
        assert receipt["matmul_on_cpu"] is True

    def test_session_receipt_on_cpu_is_honest(self, tmp_path):
        """A CPU-EP session must NEVER report proven NPU execution."""
        plane = _bits(512, 64)
        sess = npu_backend.ShardedOverlapSession(
            plane, batch=1, shard_columns=64, cache_root=tmp_path,
        )
        receipt = sess.prove_execution()
        assert receipt["provider_execution_proven"] is False


class TestLiveVitisAI:
    def test_live_partition_placement(self, tmp_path):
        """Prove the child's profiled NPU probe and its serving result separately.

        Verification profiles a reopened session. It does not establish the
        placement of the resident, unprofiled session answering overlap calls.
        """
        import json
        import os

        from core.paths import OMEGA_ROOT, vitis_python_path
        from runtime.vitis_lane import (
            VitisWorkerClient, encode_bits, worker_argv, worker_environment,
        )
        from tests.helpers.vitis_hardware import require_vitis_hardware

        require_vitis_hardware()
        plane = _bits(3904, 512)
        manifest = npu_backend.bake_tiles(
            plane, batch=8, shard_columns=512, cache_root=tmp_path,
        )
        capi = vitis_python_path().parent.parent / "Lib" / "site-packages" / "onnxruntime" / "capi"
        client = VitisWorkerClient(
            worker_argv(OMEGA_ROOT), cwd=tmp_path, log_path=tmp_path / "vitis-worker.log",
            env=worker_environment(os.environ, capi_dir=capi, state_root=tmp_path / "worker-state"),
            request_timeout_s=1800,
        )
        try:
            hello = client.start()
            assert hello["python"].startswith("3.12.")
            assert Path(hello["executable"]).resolve() == vitis_python_path().resolve()
            assert hello["pid"] != os.getpid()
            assert "VitisAIExecutionProvider" in hello["providers"]
            opened = client.request("open_tiles", {
                "manifest": manifest, "cache_root": str(tmp_path), "verify_npu": True,
            })
            assert opened["engagement"]
            assert all(tile["proven"] for tile in opened["engagement"]), opened
            query = np.vstack([_query(3904, count)[0] for count in (1, 40, 200, 255)])
            result = client.request("overlap", {"bits": encode_bits(query)})
            assert np.array_equal(
                np.asarray(result["overlaps"]), query.astype(np.int64) @ plane.astype(np.int64),
            )
        finally:
            client.close()
        profiles = list(tmp_path.glob("onnxruntime_profile*.json"))
        assert profiles, "native execution must leave its placement evidence"
        receipts = [npu_backend.placement_receipt_from_events(
            json.loads(path.read_text(encoding="utf-8")),
        ) for path in profiles]
        # A legal verification retry may leave an earlier CPU profile behind.
        # Require a successful native probe without merging failed attempts.
        proven = [receipt for receipt in receipts if receipt["provider_execution_proven"]]
        assert proven, receipts
        receipt = proven[-1]
        (tmp_path / "placement-receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
        assert receipt["provider_execution_proven"] is True
        assert receipt["partition_on_npu"] is True
        assert receipt["matmul_on_cpu"] is False
