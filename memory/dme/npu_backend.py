# Upstreamed 2026-08-07 from the omega-memory-mcp eval tree (the brain is the
# home tree; the standalone service re-vendors from here). Exists because the
# 2026-08-07 placement proof (omega-memory-mcp/.npu-runtime/proof_results.json)
# showed the ONLY model form VAIML compiles onto XDNA is the
# Q->DQ->float-MatMul->Q->DQ sandwich with the plane baked as a u8
# initializer: 0.015 ms/query at batch 64 vs 3.6 ms on the fail-safe CPU
# path the runtime ran for months while reporting a VitisAI session.
"""DME overlap matmul on the XDNA NPU: model bake, sharded sessions, and
execution-proof receipts.

Contract facts this module encodes (all measured, none aspirational):

- **Form**: DQ(query) + DQ(baked plane) -> float MatMul -> Q -> DQ, opset 13,
  ir 8, unit scale / zero-point 0. Float-output variants fail VAIML compile
  (``Unsupported dtype in BufferPad``) and silently fall back to CPU.
- **Exactness**: inputs are 0/1 bits so unit-scale QDQ is exact -- but the
  u8 quantized output stage clips at 255. Exact iff query popcount <= 255;
  ``overlap()`` enforces that instead of trusting callers.
- **Sharding**: the plane is column-sharded ([K, N] -> N-chunks). Each output
  element still reduces over the FULL K inside one shard, so sharding never
  creates partial sums and exactness is unaffected. K=261,952 (the repo's
  PAYLOAD_BITS) is placement-proven by the July probe; wide N is proven to
  8,192 at K=3,904 -- shard width trades compile size against dispatches.
- **Proof**: session registration proves nothing (the runtime's fail-safe
  taught us that). ``provider_execution_proven`` comes ONLY from an ONNX
  profile whose node list shows a ``vitis_ai_ep`` partition on
  VitisAIExecutionProvider and no MatMul-family node on CPU.
- **Bake vs. open**: ``bake_tiles`` needs ``onnx`` and runs in the brain venv;
  ``ShardedOverlapSession.from_manifest`` needs only ``onnxruntime`` and runs
  in the cp312 Vitis child (``runtime.vitis_worker``). The tile manifest is
  the contract between the two interpreters and carries no absolute paths.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from core.paths import GUARDIAN_STATE_ROOT

log = logging.getLogger("omega.memory.dme.npu_backend")

QUERY_POPCOUNT_LIMIT = 255  # u8 output stage clips above this
TILE_MANIFEST_FORMAT = "omega.dme.npu_tiles/1"

_PREFERRED_PROVIDERS = ["VitisAIExecutionProvider", "CPUExecutionProvider"]


def default_cache_root() -> Path:
    """Return the relocatable tile/model cache owned by this Omega tree."""
    return GUARDIAN_STATE_ROOT / "dme" / "npu_tiles"


def _ort():
    import onnxruntime as ort

    return ort


def vitisai_available() -> bool:
    try:
        return "VitisAIExecutionProvider" in _ort().get_available_providers()
    except Exception:
        return False


def build_overlap_model(plane_bits: np.ndarray, *, batch: int) -> bytes:
    """Serialize the proven QDQ-u8-io sandwich with ``plane_bits`` baked in.

    ``plane_bits``: uint8 {0,1}, shape [K, N] (bit dimension x columns).
    Query input is [batch, K]; overlap output is float [batch, N].
    """
    import onnx
    from onnx import TensorProto, helper, numpy_helper

    plane_bits = np.ascontiguousarray(plane_bits, dtype=np.uint8)
    if plane_bits.ndim != 2:
        raise ValueError(f"plane must be 2-D, got {plane_bits.shape}")
    if plane_bits.max(initial=0) > 1:
        raise ValueError("plane must be binary {0,1} bits")
    k, n = plane_bits.shape

    inits = [
        numpy_helper.from_array(plane_bits, "memory_quantized"),
        numpy_helper.from_array(np.array(1.0, dtype=np.float32), "unit_scale"),
        numpy_helper.from_array(np.array(0, dtype=np.uint8), "zero_point"),
    ]
    nodes = [
        helper.make_node("DequantizeLinear", ["query", "unit_scale", "zero_point"], ["query_dq"]),
        helper.make_node("DequantizeLinear", ["memory_quantized", "unit_scale", "zero_point"], ["memory_dq"]),
        helper.make_node("MatMul", ["query_dq", "memory_dq"], ["raw_overlap"]),
        helper.make_node("QuantizeLinear", ["raw_overlap", "unit_scale", "zero_point"], ["overlap_q"]),
        helper.make_node("DequantizeLinear", ["overlap_q", "unit_scale", "zero_point"], ["overlap"]),
    ]
    graph = helper.make_graph(
        nodes, f"dme_qdq_u8_io_k{k}_n{n}_b{batch}",
        [helper.make_tensor_value_info("query", TensorProto.UINT8, [batch, k])],
        [helper.make_tensor_value_info("overlap", TensorProto.FLOAT, [batch, n])],
        inits,
    )
    model = helper.make_model(
        graph, ir_version=8, opset_imports=[helper.make_opsetid("", 13)],
        producer_name="omega.dme.npu_backend",
    )
    onnx.checker.check_model(model)
    return model.SerializeToString()


def placement_receipt_from_events(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The acceptance test, as data: profile node events -> honest receipt."""
    matmul_on_cpu = False
    partition_on_npu = False
    placements: Dict[str, int] = {}
    for e in events:
        if e.get("cat") != "Node":
            continue
        args = e.get("args", {})
        op = str(args.get("op_name", ""))
        prov = str(args.get("provider", ""))
        placements[f"{op}|{prov}"] = placements.get(f"{op}|{prov}", 0) + 1
        if "MatMul" in op and prov == "CPUExecutionProvider":
            matmul_on_cpu = True
        if op.startswith("vitis_ai_ep") and prov == "VitisAIExecutionProvider":
            partition_on_npu = True
    return {
        "partition_on_npu": partition_on_npu,
        "matmul_on_cpu": matmul_on_cpu,
        "provider_execution_proven": partition_on_npu and not matmul_on_cpu,
        "placements": placements,
    }


def _spans(total: int, width: int) -> List[tuple]:
    return [(lo, min(lo + width, total)) for lo in range(0, total, width)]


def _model_root(cache_root: Path | str | None) -> Path:
    root = Path(cache_root) if cache_root is not None else default_cache_root()
    root.mkdir(parents=True, exist_ok=True)
    return root


def bake_tiles(
    plane_bits: np.ndarray,
    *,
    batch: int,
    shard_columns: int,
    k_chunk: Optional[int] = None,
    cache_root: Path | str | None = None,
    cache_key: str = "dme_overlap",
) -> Dict[str, Any]:
    """Materialize every (n-shard x k-chunk) tile model; return the manifest.

    Content-addressed tiles: each model + vaip cache is keyed by the hash of
    THAT slice's bits (+ batch, which shapes the compiled kernel, + a format
    version). Deepsleep rebakes then recompile ONLY tiles whose content
    changed; identical tiles cache-hit forever. Never reuse a vaip cache_key
    across different weights — vaip is not trusted to notice.

    The manifest names tiles by key only, so it stays valid when the product
    tree (and the cache under it) moves.
    """
    plane_bits = np.ascontiguousarray(plane_bits, dtype=np.uint8)
    if plane_bits.ndim != 2:
        raise ValueError(f"plane must be 2-D, got {plane_bits.shape}")
    if plane_bits.max(initial=0) > 1:
        raise ValueError("plane must be binary {0,1} bits")
    k, n = plane_bits.shape
    batch = int(batch)
    # K past the EP envelope (measured 2026-08-07: K=261,952 is garbage at
    # N=128 and crashes at N>=256) decomposes into proven-width chunks;
    # outputs sum in int64 host-side. Exact because the global popcount<=255
    # guard bounds every chunk partial below the u8 clip.
    width = int(k_chunk) if k_chunk else k
    model_root = _model_root(cache_root)
    tiles: List[Dict[str, Any]] = []
    for start, stop in _spans(n, int(shard_columns)):
        for k0, k1 in _spans(k, width):
            tile = np.ascontiguousarray(plane_bits[k0:k1, start:stop])
            h = hashlib.sha256()
            h.update(b"qdq1")
            h.update(np.int64([batch, k1 - k0, stop - start]).tobytes())
            h.update(tile.tobytes())
            key = f"{cache_key}_{h.hexdigest()[:16]}"
            model_path = model_root / f"{key}.onnx"
            if not model_path.exists():
                model_path.write_bytes(build_overlap_model(tile, batch=batch))
            tiles.append({"start": start, "stop": stop, "k0": k0, "k1": k1, "key": key})
    return {
        "format": TILE_MANIFEST_FORMAT,
        "k": k,
        "n": n,
        "batch": batch,
        "shard_columns": int(shard_columns),
        "k_chunk": width,
        "tiles": tiles,
    }


def _validated_grid(manifest: Dict[str, Any]) -> List[tuple]:
    """Return ((start, stop), [((k0, k1), key), ...]) rows, or raise."""
    if manifest.get("format") != TILE_MANIFEST_FORMAT:
        raise ValueError(
            f"unsupported DME tile manifest format: {manifest.get('format')!r}"
        )
    k, n = int(manifest["k"]), int(manifest["n"])
    shard_spans = _spans(n, int(manifest["shard_columns"]))
    k_spans = _spans(k, int(manifest["k_chunk"]))
    expected = [(shard, span) for shard in shard_spans for span in k_spans]
    tiles = list(manifest.get("tiles") or [])
    actual = [((t["start"], t["stop"]), (t["k0"], t["k1"])) for t in tiles]
    if actual != expected:
        raise ValueError(
            f"DME tile manifest does not cover the tile grid: {len(tiles)} tile(s) "
            f"for {len(shard_spans)} shard(s) x {len(k_spans)} chunk(s)"
        )
    keys = iter(str(t["key"]) for t in tiles)
    return [(shard, [(span, next(keys)) for span in k_spans]) for shard in shard_spans]


class ShardedOverlapSession:
    """Column-sharded overlap scorer over one baked plane.

    One ONNX session per N-shard, all built at a fixed ``batch``; queries
    arrive as [Q, K] u8 bits, outputs as int64 [Q, N] exact overlap counts.
    Short final batches are zero-padded (a zero query row overlaps nothing)
    and truncated on the way out.
    """

    def __init__(
        self,
        plane_bits: np.ndarray,
        *,
        batch: int = 8,
        shard_columns: int = 2048,
        k_chunk: Optional[int] = None,
        cache_root: Path | str | None = None,
        cache_key: str = "dme_overlap",
        verify_npu: bool = False,
        engage_retries: int = 3,
    ) -> None:
        manifest = bake_tiles(
            plane_bits, batch=batch, shard_columns=shard_columns,
            k_chunk=k_chunk, cache_root=cache_root, cache_key=cache_key,
        )
        self._open(
            manifest, cache_root=cache_root, verify_npu=verify_npu,
            engage_retries=engage_retries,
        )

    @classmethod
    def from_manifest(
        cls,
        manifest: Dict[str, Any],
        *,
        cache_root: Path | str,
        verify_npu: bool = False,
        engage_retries: int = 3,
    ) -> "ShardedOverlapSession":
        """Open sessions over tiles another process already baked.

        This is the Vitis child's entry point: it never builds a model, so it
        never needs ``onnx``. A tile that is absent is a bake failure on the
        other side of the manifest and is reported as one.
        """
        session = cls.__new__(cls)
        session._open(
            manifest, cache_root=cache_root, verify_npu=verify_npu,
            engage_retries=engage_retries,
        )
        return session

    def _open(
        self,
        manifest: Dict[str, Any],
        *,
        cache_root: Path | str | None,
        verify_npu: bool,
        engage_retries: int,
    ) -> None:
        grid = _validated_grid(manifest)
        self.k = int(manifest["k"])
        self.n = int(manifest["n"])
        self.batch = int(manifest["batch"])
        self.shard_columns = int(manifest["shard_columns"])
        self.k_chunk = int(manifest["k_chunk"])
        self._k_spans = _spans(self.k, self.k_chunk)
        self.k_chunks = len(self._k_spans)
        self._shard_spans: List[tuple] = []
        # sessions[n_shard_index][k_chunk_index]
        self._sessions: List[List[Any]] = []

        ort = _ort()
        available = set(ort.get_available_providers())
        providers = [p for p in _PREFERRED_PROVIDERS if p in available] or [
            "CPUExecutionProvider"
        ]
        self.providers = providers

        # VitisAI only ENGAGES for sessions created from a model file path —
        # every placed session to date (July probe, 2026-08-07 proofs) loaded
        # from disk; in-memory bytes silently skip the EP (empty vaip cache,
        # CPU fallback, no error). Models are therefore always materialized.
        model_root = _model_root(cache_root)

        self._shard0_path: Optional[Path] = None
        self._shard0_provider_options: List[Dict[str, str]] = []
        self._engagement: List[Dict[str, Any]] = []
        self.tile_keys: List[str] = []
        vitisai_live = "VitisAIExecutionProvider" in providers
        for (start, stop), row in grid:
            chunk_sessions: List[Any] = []
            for (k0, k1), tile_key in row:
                self.tile_keys.append(tile_key)
                model_path = model_root / f"{tile_key}.onnx"
                if not model_path.is_file():
                    raise FileNotFoundError(
                        f"DME tile model is not baked: {model_path}"
                    )
                provider_options: List[Dict[str, str]] = []
                for p in providers:
                    if p == "VitisAIExecutionProvider" and cache_root is not None:
                        shard_cache = Path(cache_root) / tile_key
                        shard_cache.mkdir(parents=True, exist_ok=True)
                        provider_options.append({
                            "cache_dir": str(shard_cache),
                            "cache_key": tile_key,
                        })
                    else:
                        provider_options.append({})

                sess = None
                # VitisAI engagement at creation is FLAKY (measured
                # 2026-08-07: multi-session builds silently fail-safe to
                # CPU — EP registered, vaip cache empty, no error — while a
                # lone session over the same model places in seconds).
                # verify_npu turns that flake into determinism: prove each
                # session via a cache-backed profile and recreate the
                # fail-safed ones. Bake-time (deepsleep) cost only.
                attempts = max(1, engage_retries) if (verify_npu and vitisai_live) else 1
                for attempt in range(attempts):
                    sess = ort.InferenceSession(
                        str(model_path), sess_options=self._session_options(),
                        providers=providers, provider_options=provider_options,
                    )
                    warmup = np.zeros((self.batch, k1 - k0), dtype=np.uint8)
                    sess.run(None, {"query": warmup})
                    if not (verify_npu and vitisai_live):
                        break
                    receipt = self._prove_session(model_path, provider_options)
                    if receipt.get("provider_execution_proven"):
                        self._engagement.append({
                            "shard": start, "chunk": k0, "proven": True,
                            "attempts": attempt + 1,
                        })
                        break
                    del sess
                    sess = None
                else:
                    if verify_npu and vitisai_live:
                        # ran out of retries: rebuild once more, degraded
                        sess = ort.InferenceSession(
                            str(model_path), sess_options=self._session_options(),
                            providers=providers,
                            provider_options=provider_options,
                        )
                        self._engagement.append({
                            "shard": start, "chunk": k0, "proven": False,
                            "attempts": attempts, "reason": "retries_exhausted",
                        })
                        log.warning(
                            "npu_backend: shard %d chunk %d degraded to CPU "
                            "after %d engagement attempts", start, k0, attempts,
                        )
                if verify_npu and not vitisai_live:
                    self._engagement.append({
                        "shard": start, "chunk": k0, "proven": False,
                        "attempts": 0, "reason": "ep_unavailable",
                    })
                if start == 0 and k0 == 0:
                    self._shard0_path = model_path
                    self._shard0_provider_options = provider_options
                chunk_sessions.append(sess)
            self._sessions.append(chunk_sessions)
            self._shard_spans.append((start, stop))
        log.info(
            "npu_backend: %d shard x %d chunk session(s) k=%d n=%d batch=%d providers=%s",
            len(self._sessions), self.k_chunks, self.k, self.n, self.batch,
            providers,
        )

    @staticmethod
    def _session_options() -> Any:
        so = _ort().SessionOptions()
        so.intra_op_num_threads = 1
        so.inter_op_num_threads = 1
        return so

    def _prove_session(
        self, model_path: Path, provider_options: List[Dict[str, str]]
    ) -> Dict[str, Any]:
        """Cache-backed profiled re-open of one model → placement receipt."""
        import json

        ort = _ort()
        so = self._session_options()
        so.enable_profiling = True
        try:
            profiled = ort.InferenceSession(
                str(model_path), sess_options=so,
                providers=self.providers, provider_options=provider_options,
            )
            probe = np.zeros(
                (self.batch, profiled.get_inputs()[0].shape[1]), dtype=np.uint8
            )
            probe[:, 0] = 1
            for _ in range(3):
                profiled.run(None, {"query": probe})
            events = json.loads(
                Path(profiled.end_profiling()).read_text(encoding="utf-8")
            )
        except Exception as exc:
            return {
                "provider_execution_proven": False,
                "partition_on_npu": False,
                "matmul_on_cpu": True,
                "placements": {},
                "note": f"profiling session unavailable: {exc}",
            }
        return placement_receipt_from_events(events)

    def engagement_report(self) -> List[Dict[str, Any]]:
        """Per-(shard, chunk) engagement outcome when verify_npu was on."""
        return list(self._engagement)

    def close(self) -> None:
        """Release every ONNX tile session and clear the placement receipt."""
        sessions = self._sessions
        self._sessions = []
        self._shard_spans = []
        self._k_spans = []
        self._engagement = []
        self.tile_keys = []
        failures: List[Exception] = []
        for group in sessions:
            for session in group:
                closer = getattr(session, "close", None)
                if callable(closer):
                    try:
                        closer()
                    except Exception as exc:  # noqa: BLE001 - report after all releases
                        failures.append(exc)
        if failures:
            raise ExceptionGroup("DME tile release failed", failures)

    def overlap(self, queries: np.ndarray) -> np.ndarray:
        """Exact overlap counts for u8 bit queries [Q, K] -> int64 [Q, N]."""
        queries = np.ascontiguousarray(queries, dtype=np.uint8)
        if queries.ndim != 2 or queries.shape[1] != self.k:
            raise ValueError(
                f"queries must be [Q, {self.k}], got {queries.shape}"
            )
        popcounts = queries.sum(axis=1, dtype=np.int64)
        worst = int(popcounts.max(initial=0))
        if worst > QUERY_POPCOUNT_LIMIT:
            raise ValueError(
                f"query popcount {worst} exceeds the u8 clip bound "
                f"({QUERY_POPCOUNT_LIMIT}); counts would saturate silently"
            )

        q_total = queries.shape[0]
        out = np.zeros((q_total, self.n), dtype=np.int64)
        for row0 in range(0, q_total, self.batch):
            row1 = min(row0 + self.batch, q_total)
            block = queries[row0:row1]
            if block.shape[0] < self.batch:
                pad = np.zeros((self.batch - block.shape[0], self.k), np.uint8)
                block = np.vstack([block, pad])
            for chunk_sessions, (start, stop) in zip(
                self._sessions, self._shard_spans
            ):
                acc = np.zeros((row1 - row0, stop - start), dtype=np.int64)
                for sess, (k0, k1) in zip(chunk_sessions, self._k_spans):
                    chunk_q = np.ascontiguousarray(block[:, k0:k1])
                    y = sess.run(None, {"query": chunk_q})[0]
                    acc += np.asarray(y)[: row1 - row0].astype(np.int64)
                out[row0:row1, start:stop] = acc
        return out

    def prove_execution(self) -> Dict[str, Any]:
        """Profile one real run of shard 0 / chunk 0 and return the honest
        receipt. Never infers from session registration."""
        receipt = self._prove_session(
            self._shard0_path, self._shard0_provider_options
        )
        receipt["providers"] = list(self.providers)
        receipt["span"] = list(self._shard_spans[0])
        return receipt
