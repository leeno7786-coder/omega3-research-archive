"""Thought Engine v2 — full-graph parallel hop scoring over the DME SRAM.

Lineage: the v1 ThoughtEngine (<_frozen>/memory_engine_v1_2026-04-04) won on
graded union search + full-lobe parallel sweep; DME v2 kept the bitmaps but
regressed to strict intersection. This module restores GRADED scoring and adds
what v1 never had — GRAPH-EDGE HOP PLANES:

  plane-0  each leaf's own coordinate bitmap (the existing SRAM)
  plane-1  per leaf, the OR of its graph neighbors' payload bitmaps

One matmul against plane-1 scores EVERY leaf's 1-hop neighborhood
simultaneously; deeper hops run as iterative bitmap-BFS where each matmul is
one parallel hop across all leaves at once. Scores decay per hop (HOP_DECAY
semantics shared with the walker).

The overlap implementation is one deterministic NumPy path. Sorted neighbor
maps, stable tie-breaks, and no wall-clock keep the result byte-stable.

NOT AUTHORITY: like every DME surface, hits are candidates only — the
RecallVerifier gates everything before it can ground a response.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Literal, Mapping, Optional

import numpy as np

from memory.dme.geometry import (
    DmeGeometryError,
    scoring_geometry_receipt,
    validate_payload_operands,
    validate_query_entry,
    validate_weight_entries,
)
from memory.dme.lobe_layout import ENTRY_WORDS, PAYLOAD_BITS
from memory.dme.packer import HEADER_WORDS, pack_entry, unpack_source_authority, unpack_timestamp

log = logging.getLogger("omega.memory.dme.hop_engine")

DEFAULT_DECAY = 0.6
DEFAULT_FLOOR = 0.34
DEFAULT_HOPS = 2
# a mediating node shared by more leaves than this explains nothing — it is a
# degenerate hub (a word in every memory), not a connection. Legit popular
# entities stay well under it; the cap only guards plane saturation.
DEFAULT_BRIDGE_DEGREE_CAP = 64

# ── RATIFIED score combine table (2026-07-04) ────────────────────────────────
# Each weight is an evidence-CLASS ceiling; the bitmap ratio grades quality
# within the class. The floor gates the RATIO (match quality); the weighted
# score ranks. Hop-2 reach is binary, so its quality is 1.0 by construction —
# hop-2 candidates live or die at the pre-verifier anchor filter + verifier.
WEIGHT_DIRECT = 1.00  # perfect direct match (every query coordinate found)
WEIGHT_UNION = 0.80  # graded partial direct match (× ratio)
WEIGHT_STRONG_H1 = 0.60  # strong-edge 1-hop neighborhood (× ratio)
WEIGHT_WEAK_H1 = 0.25  # weak-edge 1-hop neighborhood (× ratio)
WEIGHT_STRONG_H2 = 0.20  # strong-edge frontier reach, hop ≥ 2
WEIGHT_WEAK_H2 = 0.05  # weak-edge frontier reach, hop ≥ 2
BONUS_TARGET_LOBE = 0.03
BONUS_AUTHORITY_MAX = 0.02  # × authority/100 (packer SOURCE_AUTHORITY tiers)
PENALTY_GENERIC = 0.05  # × generic fraction of a row's matched bits
GENERIC_DF_FRACTION = 0.25  # a bit in >25% of rows (min 3) is generic

# Edge classes (ratified): strong = compiler-proven record links + entity
# bridges + same-session; weak = placement/similarity co-occurrence.
STRONG_EDGE_TYPES = frozenset(
    {
        "MENTIONS",
        "SUPERSEDES",
        "CONTRADICTS",
        "REFINES",
        "ANSWERS",
        "SUPPORTS",
        "DERIVED_FROM",
        "DEPENDS_ON",
        "CAUSED_BY",
        "PRODUCED",
        "USED_TOOL",
        "FAILED_BECAUSE",
        "OCCURRED_IN",
        "OBSERVED_IN",
    }
)
WEAK_EDGE_TYPES = frozenset(
    {
        "CO_LOCATED",
        "SHARES_DOMAIN",
        "SHARES_TAG",
    }
)
# Retired edge classes (memory-organ Cut 5A): the compiler no longer produces
# them and the vocabulary no longer admits them. A STALE pre-v7 graph.sqlite
# may still carry rows of a retired type; the neighbor-map builder SKIPS them
# (neither strong nor weak — the "unknown edge type ⇒ weak" law is for
# provenance we do not know, not for provenance we have explicitly retired)
# and logs one WARNING per build naming the count, so the artifact gets
# recompiled instead of silently feeding hops.
RETIRED_EDGE_TYPES = frozenset({"SEMANTIC_SIMILARITY"})

# edge-class codes (int lanes inside the scorer; strings on the wire)
_CLS_NONE, _CLS_DIRECT, _CLS_UNION = 0, 1, 2
_CLS_STRONG_H1, _CLS_WEAK_H1, _CLS_STRONG_H2, _CLS_WEAK_H2 = 3, 4, 5, 6
_CLS_NAME = {
    _CLS_NONE: "",
    _CLS_DIRECT: "direct",
    _CLS_UNION: "union",
    _CLS_STRONG_H1: "strong_hop1",
    _CLS_WEAK_H1: "weak_hop1",
    _CLS_STRONG_H2: "strong_hop2",
    _CLS_WEAK_H2: "weak_hop2",
}

_BACKEND_OVERRIDE: Optional[Literal["numpy", "onnx"]] = None
_ORT_SESSION = None
_ORT_FAILED = False
_ORT_GEOMETRY_VALIDATED_SESSION = None


def _native_onnx_requested() -> bool:
    """Native ORT is an explicit acceleration opt-in for portable boots."""
    return (os.environ.get("OMEGA_NATIVE_ORT") or "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }

# DME owns the NPU lane. The dynamic MatMulInteger path uses the same provider
# order as the portable local service: NPU first, CPU second. GPU providers do
# not belong here; cloud-seat GPU placement is reserved for speech.
PREFERRED_ONNX_PROVIDERS = [
    "VitisAIExecutionProvider", "QNNExecutionProvider",
    "CPUExecutionProvider",
]


def set_backend(backend: Optional[Literal["numpy", "onnx"]]) -> None:
    """Force a compute backend (tests/diagnostics). None = auto (onnx→numpy)."""
    global _BACKEND_OVERRIDE
    _BACKEND_OVERRIDE = backend


def _payload_bits_u8(matrix: np.ndarray) -> np.ndarray:
    """Rows of payload bits as uint8 {0,1} — header words excluded."""
    if matrix.ndim == 1:
        validate_query_entry(matrix)
        matrix = matrix.reshape(1, -1)
    else:
        validate_weight_entries(matrix)
    payload = matrix[:, HEADER_WORDS:]
    bits = np.unpackbits(payload.copy().view(np.uint8), bitorder="little").reshape(
        len(matrix), PAYLOAD_BITS
    )
    return bits.astype(np.uint8)


def _onnx_session():
    """MatMulInteger session with NPU-first provider preference. Cached."""
    global _ORT_SESSION, _ORT_FAILED
    if _ORT_SESSION is not None or _ORT_FAILED:
        return _ORT_SESSION
    if not _native_onnx_requested():
        # NumPy is the complete, deterministic portable scorer.  Do not import
        # the architecture-sensitive native extension during gateway/brain boot.
        _ORT_FAILED = True
        return None
    try:
        import onnxruntime as ort
        from onnx import TensorProto, helper

        a = helper.make_tensor_value_info("A", TensorProto.UINT8, ["n", "k"])
        b = helper.make_tensor_value_info("B", TensorProto.UINT8, ["k", "m"])
        y = helper.make_tensor_value_info("Y", TensorProto.INT32, ["n", "m"])
        node = helper.make_node("MatMulInteger", ["A", "B"], ["Y"])
        graph = helper.make_graph([node], "dme_hop_matmul", [a, b], [y])
        model = helper.make_model(
            graph, opset_imports=[helper.make_opsetid("", 21)],
            producer_name="omega.thought_engine.v2",
        )
        preferred = list(PREFERRED_ONNX_PROVIDERS)
        available = ort.get_available_providers()
        providers = [p for p in preferred if p in available] or ["CPUExecutionProvider"]
        _ORT_SESSION = ort.InferenceSession(model.SerializeToString(), providers=providers)
        log.info("thought-engine matmul backend=onnx providers=%s", providers)
    except Exception as exc:
        log.warning("thought-engine onnx unavailable, numpy fallback: %s", exc)
        _ORT_FAILED = True
        _ORT_SESSION = None
    return _ORT_SESSION


def ensure_backend_loaded() -> dict[str, Any]:
    """Instantiate, validate, and retain the process-wide scoring seat."""
    session = _onnx_session()
    if session is not None:
        _validate_onnx_session_geometry(session)
    providers = (
        list(session.get_providers())
        if session is not None
        else ["CPUExecutionProvider"]
    )
    return {
        **scoring_geometry_receipt(),
        # NumPy is a complete local scoring fallback, so the lane remains
        # ready even when no accelerator provider accepted MatMulInteger.
        "ready": True,
        "onnx_session_ready": session is not None,
        "backend": "onnxruntime" if session is not None else "numpy",
        # ORT exposes registered session providers, not per-node assignment.
        # The exact-width run proves scoring compatibility only.
        "providers": providers,
        "provider_receipt_scope": "session_registration_only",
        "provider_execution_proven": False,
        "session_cached": session is not None,
        # NPU lane visibility (dormant until the sharded-session integration
        # replaces the per-query sweeps): availability facts only — the
        # execution claim stays reserved for npu_backend's profile receipts.
        "npu_lane": _npu_lane_receipt(),
    }


def _npu_lane_receipt() -> dict[str, Any]:
    if not _native_onnx_requested():
        return {
            "backend_module": "memory.dme.npu_backend",
            "vitisai_available": False,
            "query_popcount_limit": 255,
            "provider_execution_proven": False,
            "reason": "native_onnx_opt_in_required",
        }
    try:
        from memory.dme import npu_backend

        return {
            "backend_module": "memory.dme.npu_backend",
            "vitisai_available": npu_backend.vitisai_available(),
            "query_popcount_limit": npu_backend.QUERY_POPCOUNT_LIMIT,
            "provider_execution_proven": False,
        }
    except Exception as exc:  # pragma: no cover - import failure is env-only
        return {
            "backend_module": None,
            "vitisai_available": False,
            "error": str(exc),
            "provider_execution_proven": False,
        }


def _validate_onnx_session_geometry(session: Any) -> None:
    """Warm one exact-width MatMul and fail loudly on provider/model drift."""
    global _ORT_GEOMETRY_VALIDATED_SESSION
    if _ORT_GEOMETRY_VALIDATED_SESSION is session:
        return
    weights = np.zeros((1, PAYLOAD_BITS), dtype=np.uint8)
    query = np.zeros((PAYLOAD_BITS, 1), dtype=np.uint8)
    weights[0, 0] = 1
    query[0, 0] = 1
    output = np.asarray(session.run(None, {"A": weights, "B": query})[0])
    if output.shape != (1, 1) or int(output[0, 0]) != 1:
        raise DmeGeometryError(
            "DME ONNX geometry probe failed: "
            f"expected_shape=(1, 1) actual_shape={output.shape}"
        )
    _ORT_GEOMETRY_VALIDATED_SESSION = session


def close_backend() -> None:
    """Release the process-wide DME ONNX seat and unpacked-bit cache."""
    global _ORT_SESSION, _ORT_FAILED, _ORT_GEOMETRY_VALIDATED_SESSION
    session = _ORT_SESSION
    _ORT_SESSION = None
    _ORT_FAILED = False
    _ORT_GEOMETRY_VALIDATED_SESSION = None
    _BITS_CACHE.clear()
    closer = getattr(session, "close", None)
    if callable(closer):
        closer()


# Unpacked-bits memoization: the SAME sram/plane arrays serve every query in
# a session (adapter cache), so re-unpacking 64-bit words into uint8 bit rows
# per query is pure waste and made Mode-C latency contention-sensitive.
# Strong refs keep ids valid; FIFO-bounded and size-capped so a giant store
# can never pin unbounded memory. Pure function of content — determinism
# unaffected by hits vs misses.
_BITS_CACHE: dict[int, tuple[np.ndarray, np.ndarray]] = {}
_BITS_CACHE_MAX_ENTRIES = 6
_BITS_CACHE_MAX_BYTES = 512 * 1024 * 1024  # 262144-bit rows: ~1k-leaf store ≈ 250MB


def _payload_bits_cached(matrix: np.ndarray) -> np.ndarray:
    key = id(matrix)
    hit = _BITS_CACHE.get(key)
    if hit is not None and hit[0] is matrix:
        return hit[1]
    bits = _payload_bits_u8(matrix)
    if bits.nbytes <= _BITS_CACHE_MAX_BYTES:
        while len(_BITS_CACHE) >= _BITS_CACHE_MAX_ENTRIES:
            _BITS_CACHE.pop(next(iter(_BITS_CACHE)))
        _BITS_CACHE[key] = (matrix, bits)
    return bits


def _overlap_bits(rows: np.ndarray, qbits: np.ndarray) -> np.ndarray:
    """Matmul core over pre-unpacked uint8 bit rows (backend-dispatched)."""
    validate_payload_operands(rows, qbits)
    if rows.shape[0] == 0:
        return np.zeros(0, dtype=np.int64)
    backend = _BACKEND_OVERRIDE
    if backend in (None, "onnx"):
        sess = _onnx_session()
        if sess is not None:
            _validate_onnx_session_geometry(sess)
            out = sess.run(None, {"A": rows, "B": qbits.reshape(-1, 1)})[0]
            output = np.asarray(out)
            if output.shape != (rows.shape[0], 1):
                raise DmeGeometryError(
                    "DME ONNX output shape mismatch: "
                    f"expected={(rows.shape[0], 1)} actual={output.shape}"
                )
            return output.reshape(-1).astype(np.int64)
        if backend == "onnx":
            raise RuntimeError("onnx backend forced but onnxruntime unavailable")
    return (rows & qbits).sum(axis=1).astype(np.int64)


def overlaps_payload(matrix: np.ndarray, query: np.ndarray) -> np.ndarray:
    """Per-row count of shared payload bits between ``matrix`` rows and ``query``.

    Uses the single deterministic NumPy overlap path."""
    return _overlap_bits(_payload_bits_u8(matrix), _payload_bits_u8(query)[0])


def query_payload_popcount(query: np.ndarray) -> int:
    return int(_payload_bits_u8(query)[0].sum())


# ── hop plane ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class HopPlanes:
    """Edge-class hop planes: strong (compiler-proven / entity / session) and
    weak (placement / similarity). Either may be None (class contributes 0)."""

    strong: Optional[np.ndarray] = None
    weak: Optional[np.ndarray] = None


def build_hop_plane(
    sram: np.ndarray,
    row_index: List[Mapping[str, Any]],
    neighbor_map: Mapping[str, Iterable[str]],
) -> np.ndarray:
    """plane-1: row i = OR of graph-neighbor payload bitmaps (headers zeroed).

    Deterministic: neighbor lists are sorted before OR (order-independent
    anyway, sorted for hygiene)."""
    hop = np.zeros_like(sram)
    if sram.size == 0:
        return hop
    row_of = {str(m.get("memory_id") or ""): i for i, m in enumerate(row_index)}
    for i, meta in enumerate(row_index):
        mid = str(meta.get("memory_id") or "")
        acc = np.zeros(ENTRY_WORDS, dtype=np.uint64)
        for nid in sorted(str(n) for n in neighbor_map.get(mid, ())):
            j = row_of.get(nid)
            if j is None or j == i:
                continue
            acc |= sram[j]
        acc[:HEADER_WORDS] = 0
        hop[i] = acc
    return hop


def neighbor_map_from_graph(
    root: Path | str | None,
    memory_ids: Iterable[str],
    *,
    bridge_degree_cap: int = DEFAULT_BRIDGE_DEGREE_CAP,
) -> Dict[str, List[str]]:
    """Undirected leaf-to-leaf neighbor map from the taxonomy graph store.

    Two memories are neighbors when a DIRECT edge joins them OR when they share
    a mediating node — the compiler links memories THROUGH entity/event nodes
    (memory—entity—memory), so adjacency must bridge one non-leaf node or
    compiler-linked sessions would never hop. Nodes outside the seated id set
    are used only as bridges, never emitted; a mediator whose degree exceeds
    ``bridge_degree_cap`` is skipped (degenerate hub). Sorted, deterministic."""
    from memory.taxonomy import graph_store as G

    from memory.projection.generation import GenerationIntegrityError

    wanted = {str(m) for m in memory_ids}
    out: Dict[str, List[str]] = {}
    try:
        conn = G.connect_for_read(root)
    except GenerationIntegrityError:
        raise  # Cut 13: an unavailable generation is loud, never "no neighbors"
    except Exception as exc:
        log.warning("thought-engine: graph store unavailable (%s)", exc)
        return out
    try:
        for mid in sorted(wanted):
            try:
                neigh = G.neighbors(conn, mid)
            except Exception:
                continue
            found: set[str] = set()
            for n in neigh:
                nid = str(n.get("node_id") or "")
                if not nid or nid == mid:
                    continue
                if nid in wanted:
                    found.add(nid)
                    continue
                # mediating node (entity/event/claim): bridge one level to the
                # memories on its other side.
                try:
                    second = G.neighbors(conn, nid)
                except Exception:
                    continue
                if bridge_degree_cap and len(second) > bridge_degree_cap:
                    continue
                for m2 in second:
                    m2id = str(m2.get("node_id") or "")
                    if m2id in wanted and m2id != mid:
                        found.add(m2id)
            if found:
                out[mid] = sorted(found)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return out


# Upstreamed 2026-08-07 from the omega-memory-mcp eval tree (now the canonical
# home of this optimization). The per-node walk re-decoded edge payloads once per
# endpoint visit; on a dense LME-V2 graph (31,499 nodes / 1,131,732 edges)
# that spun build_hop_planes for 14+ CPU-hours. The walk only ever consumes
# node ids, edge types and mediator degrees, so one streamed pass over the
# edge table needs no payload JSON at all.
def _bulk_classed_adjacency(
    conn: Any, wanted: set, bridge_degree_cap: int
) -> tuple[Dict[str, set], Dict[str, set]]:
    """One streamed pass over the edge table → (strong, weak) adjacency sets.

    Replicates ``graph_store.neighbors()`` visibility semantics exactly: an
    edge row is visible from one endpoint only when the OTHER endpoint has a
    node row (the JOIN in ``neighbors()``), a mediator's degree counts visible
    rows from both directions, and self-loops never join a node to itself.
    ``graph_store.neighbors()`` itself is untouched — the live recall path
    (``recall_graph.expand_via_graph``) depends on its per-node semantics.
    """
    node_ids = {str(r[0]) for r in conn.execute("SELECT node_id FROM nodes")}
    strong: Dict[str, set] = {}
    weak: Dict[str, set] = {}
    degree: Dict[str, int] = {}
    med_sources: Dict[str, set] = {}  # mediator -> wanted leaves that see it
    med_targets: Dict[str, set] = {}  # mediator -> wanted leaves it can show
    retired_skipped: Dict[str, int] = {}  # local accounting only (no module global)
    for src, dst, etype in conn.execute(
        "SELECT source_id, target_id, edge_type FROM edges"
    ):
        etype_s = str(etype or "")
        if etype_s in RETIRED_EDGE_TYPES:
            # stale artifact row of a retired class: skipped entirely — it must
            # not count toward mediator degree, strong, or weak (Cut 5A)
            retired_skipped[etype_s] = retired_skipped.get(etype_s, 0) + 1
            continue
        src, dst = str(src or ""), str(dst or "")
        src_known, dst_known = src in node_ids, dst in node_ids
        if dst_known:
            degree[src] = degree.get(src, 0) + 1
        if src_known:
            degree[dst] = degree.get(dst, 0) + 1
        if not src or not dst or src == dst:
            continue
        src_wanted, dst_wanted = src in wanted, dst in wanted
        if src_wanted and dst_wanted:
            lane = strong if str(etype or "") in STRONG_EDGE_TYPES else weak
            if dst_known:
                lane.setdefault(src, set()).add(dst)
            if src_known:
                lane.setdefault(dst, set()).add(src)
        elif src_wanted or dst_wanted:
            leaf, mediator = (src, dst) if src_wanted else (dst, src)
            if mediator in node_ids:
                med_sources.setdefault(mediator, set()).add(leaf)
            if leaf in node_ids:
                med_targets.setdefault(mediator, set()).add(leaf)
    for mediator, sources in med_sources.items():
        if bridge_degree_cap and degree.get(mediator, 0) > bridge_degree_cap:
            continue
        targets = med_targets.get(mediator)
        if not targets:
            continue
        for mid in sources:
            reach = targets - {mid}
            if reach:
                strong.setdefault(mid, set()).update(reach)
    if retired_skipped:
        log.warning(
            "hop_engine: skipped %d retired edge row(s) of type(s) %s — stale graph artifact "
            "(pre-v7 compiler); recompile the graph",
            sum(retired_skipped.values()),
            ", ".join(f"{k}={v}" for k, v in sorted(retired_skipped.items())),
        )
    return strong, weak


def classed_neighbor_maps_from_graph(
    root: Path | str | None,
    memory_ids: Iterable[str],
    *,
    row_index: Optional[List[Mapping[str, Any]]] = None,
    bridge_degree_cap: int = DEFAULT_BRIDGE_DEGREE_CAP,
    graph_path: Path | str | None = None,
) -> tuple[Dict[str, List[str]], Dict[str, List[str]]]:
    """(strong, weak) leaf-to-leaf neighbor maps (ratified edge classes).

    STRONG: direct record links whose edge_type is compiler-proven
    (STRONG_EDGE_TYPES), entity-mediated bridges (same_entity/alias_of —
    memory—entity—memory through a degree-capped mediator), and same-session
    leaves (source_session_id equality from ``row_index``). WEAK: placement
    co-occurrence (CO_LOCATED / SHARES_DOMAIN / SHARES_TAG) plus any unknown
    edge type — unknown provenance never gets strong credit. RETIRED classes
    (``RETIRED_EDGE_TYPES``, e.g. the pre-v7 dense similarity edges) are
    skipped entirely with one WARNING per build. Sorted, deterministic."""
    from memory.taxonomy import graph_store as G

    from memory.projection.generation import GenerationIntegrityError

    wanted = {str(m) for m in memory_ids}
    strong: Dict[str, set] = {}
    weak: Dict[str, set] = {}
    try:
        # Cut 13 builder: an explicit candidate graph is opened read-only by path
        conn = G.connect_readonly(graph_path) if graph_path is not None else G.connect_for_read(root)
    except GenerationIntegrityError:
        raise  # Cut 13: an unavailable generation is loud, never empty adjacency
    except Exception as exc:
        log.warning("thought-engine: graph store unavailable (%s)", exc)
        conn = None
    if conn is not None:
        try:
            strong, weak = _bulk_classed_adjacency(conn, wanted, bridge_degree_cap)
        except Exception as exc:
            log.warning("thought-engine: bulk edge scan failed (%s)", exc)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # same-session adjacency is CONNECTIVITY, not provenance-relevance: at
    # chat scale a session holds ~20 memories, and classing them strong let
    # any weak brush anchor its whole session into the answer context
    # (LongMemEval shakedown: 38 cross-session junk admissions on one
    # question). WEAK class = still aids discovery, never anchors admission.
    if row_index:
        by_session: Dict[str, List[str]] = {}
        for meta in row_index:
            mid = str(meta.get("memory_id") or "")
            sid = str(meta.get("source_session_id") or "")
            if mid in wanted and sid:
                by_session.setdefault(sid, []).append(mid)
        for sid in sorted(by_session):
            group = sorted(set(by_session[sid]))
            if len(group) < 2:
                continue
            if bridge_degree_cap and len(group) > bridge_degree_cap:
                continue
            for a in group:
                for b in group:
                    if a != b:
                        weak.setdefault(a, set()).add(b)

    return (
        {k: sorted(v) for k, v in sorted(strong.items())},
        {k: sorted(v) for k, v in sorted(weak.items())},
    )


# ── search ───────────────────────────────────────────────────────────────────


@dataclass
class ThoughtHit:
    row_index: int
    memory_id: str
    score: float
    hop: int
    match_count: int
    query_count: int
    title: str = ""
    summary: str = ""
    authority: int = 0
    timestamp: float = 0.0
    edge_class: str = "direct"

    def to_candidate_dict(self, *, snapshot_hash: str, backend: str) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "coord": [],
            "score": self.score,
            "title": self.title,
            "summary": self.summary,
            "domain": "",
            "subtopic": "",
            "tags": [],
            "provenance": {
                "dme_snapshot_hash": snapshot_hash,
                "dme_backend": backend,
                "dme_match_count": self.match_count,
                "dme_hop": self.hop,
                "dme_edge_class": self.edge_class,
            },
        }


@dataclass
class ThoughtResult:
    hits: List[ThoughtHit]
    entries_scanned: int
    hops_run: int
    mode: str = "hop_scored"
    query_coordinates: list = field(default_factory=list)
    truncation_receipts: list[dict[str, Any]] = field(default_factory=list)


def search_thought(
    sram: np.ndarray,
    hop_sram: Optional[np.ndarray | HopPlanes],
    row_index: List[Mapping[str, Any]],
    query_coordinates: List[tuple],
    *,
    hops: int = DEFAULT_HOPS,
    decay: float = DEFAULT_DECAY,
    floor: float = DEFAULT_FLOOR,
    target_lobe: Optional[int] = None,
    max_results: int = 20,
) -> ThoughtResult:
    """Weighted, adaptively hop-scored search over the whole SRAM at once.

    RATIFIED score combine (2026-07-04): evidence = max over classes of
    weight·ratio (direct 1.00 / union 0.80 / strong_hop1 0.60 / weak_hop1
    0.25 / strong_hop2 0.20 / weak_hop2 0.05); the floor gates the class
    RATIO (match quality); ranking adds the target-lobe + source-authority
    bonuses and subtracts the generic-token penalty. Every class term is one
    matmul over ALL leaves — parallel traversal, never a row walk.

    Adaptive hop ladder: direct/union always; hop-1 default-on (Mode C —
    alias queries perfectly match the BRIDGE's vocabulary, so a confidence
    gate here would amputate recovery); hop-2 only when nothing clears the
    floor; hops>=3 is explicit exploratory mode (full traversal regardless
    of confidence).

    ``hop_sram`` accepts HopPlanes (strong+weak) or a legacy single ndarray
    (treated as the strong plane — preserves the old decay-0.6 weighting).
    ``decay`` is retained for API compatibility; the class-weight table
    supersedes it. ``target_lobe`` is a ranking BONUS, not a filter — lobe
    scoping stays with the caller's allowed/denied sets."""
    validate_weight_entries(sram)
    if len(sram) == 0 or not query_coordinates:
        return ThoughtResult(
            hits=[], entries_scanned=0, hops_run=0, query_coordinates=list(query_coordinates)
        )

    if isinstance(hop_sram, HopPlanes):
        p_strong, p_weak = hop_sram.strong, hop_sram.weak
    else:
        p_strong, p_weak = hop_sram, None
    if p_strong is not None and len(p_strong) != len(sram):
        p_strong = None
    if p_weak is not None and len(p_weak) != len(sram):
        p_weak = None

    query = pack_entry(0, 0.0, "query", query_coordinates)
    rows_bits = _payload_bits_cached(sram)
    qbits = _payload_bits_u8(query)[0]
    required = max(1, int(qbits.sum()))

    matches = _overlap_bits(rows_bits, qbits)
    ratio0 = np.minimum(matches.astype(np.float64) / required, 1.0)
    perfect = matches >= required
    evidence = np.where(perfect, WEIGHT_DIRECT, WEIGHT_UNION * ratio0)
    evidence = np.where(matches > 0, evidence, 0.0)
    quality = np.where(matches > 0, ratio0, 0.0)  # class-normalized (floor gate)
    hop_of = np.where(matches > 0, 0, -1)
    classes = np.where(perfect, _CLS_DIRECT, np.where(matches > 0, _CLS_UNION, _CLS_NONE))

    # generic-token penalty: bits present in most rows explain nothing.
    # Computed once, backend-independent (bookkeeping, not traversal).
    df = rows_bits.sum(axis=0)
    generic_bits = qbits.astype(bool) & (df > max(3, GENERIC_DF_FRACTION * len(sram)))
    if generic_bits.any():
        generic_matched = (rows_bits & generic_bits.astype(np.uint8)).sum(axis=1)
        penalty = PENALTY_GENERIC * generic_matched / np.maximum(matches, 1)
    else:
        penalty = np.zeros(len(sram), dtype=np.float64)

    exploratory = hops >= 3
    hops_run = 0

    # hop-1 is DEFAULT-ON (Mode C = graded + hop1): an alias query matches the
    # BRIDGE's vocabulary perfectly, so any confidence gate here amputates the
    # recovery mechanism the planes exist for. Only deeper hops are adaptive.
    if hops >= 1:
        for plane, weight, cls in (
            (p_strong, WEIGHT_STRONG_H1, _CLS_STRONG_H1),
            (p_weak, WEIGHT_WEAK_H1, _CLS_WEAK_H1),
        ):
            if plane is None:
                continue
            r1 = np.minimum(
                _overlap_bits(_payload_bits_cached(plane), qbits).astype(np.float64) / required,
                1.0,
            )
            s1 = weight * r1
            take = s1 > evidence
            evidence = np.where(take, s1, evidence)
            quality = np.where(take, r1, quality)
            hop_of = np.where(take, 1, hop_of)
            classes = np.where(take, cls, classes)
            hops_run = 1

    if hops >= 2 and (exploratory or not bool((quality >= floor).any())):
        # iterative bitmap-BFS: OR the frontier's OWN bitmaps into a
        # super-query; one matmul per plane per hop scores every leaf.
        frontier = evidence > 0
        for k in range(2, hops + 1):
            if not frontier.any():
                break
            fq = np.zeros(ENTRY_WORDS, dtype=np.uint64)
            for j in np.nonzero(frontier)[0]:
                fq |= sram[j]
            fq[:HEADER_WORDS] = 0
            fq_bits = _payload_bits_u8(fq.reshape(1, -1))[0]
            if not fq_bits.any():
                break
            ran_plane = False
            for plane, weight, cls in (
                (p_strong, WEIGHT_STRONG_H2, _CLS_STRONG_H2),
                (p_weak, WEIGHT_WEAK_H2, _CLS_WEAK_H2),
            ):
                if plane is None:
                    continue
                reach = _overlap_bits(_payload_bits_cached(plane), fq_bits)
                sk = np.where(reach > 0, weight, 0.0)
                take = sk > evidence
                evidence = np.where(take, sk, evidence)
                quality = np.where(take, 1.0, quality)  # reach is binary
                hop_of = np.where(take, k, hop_of)
                classes = np.where(take, cls, classes)
                ran_plane = True
            if not ran_plane:
                break
            hops_run = k
            frontier = frontier | (evidence > 0)
            if not exploratory and bool((quality >= floor).any()):
                break

    hits: List[ThoughtHit] = []
    for row in np.nonzero(quality >= max(floor, 1e-9))[0]:
        meta = row_index[row] if row < len(row_index) else {}
        coord = meta.get("coord") or ()
        authority = unpack_source_authority(sram[row])
        bonus = BONUS_AUTHORITY_MAX * min(int(authority), 100) / 100.0
        if target_lobe is not None and coord and int(coord[0]) == int(target_lobe):
            bonus += BONUS_TARGET_LOBE
        row_penalty = float(penalty[row]) if classes[row] in (_CLS_DIRECT, _CLS_UNION) else 0.0
        final = min(1.0, max(0.0, float(evidence[row]) + bonus - row_penalty))
        hits.append(
            ThoughtHit(
                row_index=int(row),
                memory_id=str(meta.get("memory_id") or ""),
                score=round(final, 6),
                hop=int(hop_of[row]),
                match_count=int(round(float(quality[row]) * required)),
                query_count=required,
                title=str(meta.get("title") or ""),
                summary=str(meta.get("summary") or ""),
                authority=int(authority),
                timestamp=unpack_timestamp(sram[row]),
                edge_class=_CLS_NAME[int(classes[row])],
            )
        )

    hits.sort(key=lambda h: (-h.score, h.hop, -h.authority, -h.timestamp, h.memory_id))
    selected_hits = hits[: max_results if max_results else len(hits)]
    from memory.recall_truncation import build_truncation_receipt

    truncation_receipts = [
        build_truncation_receipt(
            "dme_hop_search:max_results",
            (hit.memory_id for hit in hits),
            (hit.memory_id for hit in selected_hits),
            details={"max_results": max_results},
        )
    ]
    return ThoughtResult(
        hits=selected_hits,
        entries_scanned=len(sram),
        hops_run=hops_run,
        query_coordinates=list(query_coordinates),
        truncation_receipts=truncation_receipts,
    )


# ── sidecar persistence (hop planes live beside the SRAM) ────────────────────


def hop_plane_path(root: Path | str | None = None) -> Path:
    from memory.dme.adapter import dme_root

    return dme_root(root) / "hop_plane.bin"


HOP_PLANE_STRONG_ID = "hop_plane_strong"
HOP_PLANE_WEAK_ID = "hop_plane_weak"


def _generation_view(root: Path | str | None):
    from memory.dme.adapter import _generation_view as _gv

    return _gv(root)


def hop_plane_strong_path(root: Path | str | None = None) -> Path:
    from memory.dme.adapter import dme_root

    view = _generation_view(root)
    if view is not None:  # Cut 13: the view's bound plane (generic plane list; nothing assumes two planes)
        return view.path(HOP_PLANE_STRONG_ID)
    return dme_root(root) / "hop_plane_strong.bin"


def hop_plane_weak_path(root: Path | str | None = None) -> Path:
    from memory.dme.adapter import dme_root

    view = _generation_view(root)
    if view is not None:
        return view.path(HOP_PLANE_WEAK_ID)
    return dme_root(root) / "hop_plane_weak.bin"


def load_or_build_hop_plane(
    root: Path | str | None,
    sram: np.ndarray,
    row_index: List[Mapping[str, Any]],
) -> np.ndarray:
    """LEGACY single merged plane (pre edge-class split). Production recall
    uses load_or_build_hop_planes; this stays for compat surfaces only."""
    path = hop_plane_path(root)
    expected = sram.shape
    if path.exists():
        raw = path.read_bytes()
        if len(raw) == expected[0] * ENTRY_WORDS * 8:
            return np.frombuffer(raw, dtype=np.uint64).reshape(expected).copy()
    ids = [str(m.get("memory_id") or "") for m in row_index]
    plane = build_hop_plane(sram, row_index, neighbor_map_from_graph(root, ids))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(plane.tobytes())
    except Exception as exc:
        log.warning("thought-engine: hop plane persist failed (%s)", exc)
    return plane


def build_hop_planes(
    root: Path | str | None,
    sram: np.ndarray,
    row_index: List[Mapping[str, Any]],
    *,
    graph_path: Path | str | None = None,
) -> HopPlanes:
    """Build both edge-class planes from the graph store + row metadata (``graph_path``:
    an explicit candidate graph, Cut 13 builder)."""
    ids = [str(m.get("memory_id") or "") for m in row_index]
    strong_nm, weak_nm = classed_neighbor_maps_from_graph(root, ids, row_index=row_index, graph_path=graph_path)
    return HopPlanes(
        strong=build_hop_plane(sram, row_index, strong_nm),
        weak=build_hop_plane(sram, row_index, weak_nm),
    )


def load_or_build_hop_planes(
    root: Path | str | None,
    sram: np.ndarray,
    row_index: List[Mapping[str, Any]],
) -> HopPlanes:
    """Load the persisted strong+weak planes; rebuild when either is absent
    or stale (row-count mismatch). Failure-soft: an unreadable graph yields
    empty planes (hop scoring contributes nothing; direct unaffected)."""
    view = _generation_view(root)
    if view is not None:
        return _load_hop_planes_from_view(view, sram)
    sp, wp = hop_plane_strong_path(root), hop_plane_weak_path(root)
    expected_bytes = sram.shape[0] * ENTRY_WORDS * 8
    if sp.exists() and wp.exists():
        raw_s, raw_w = sp.read_bytes(), wp.read_bytes()
        if len(raw_s) == expected_bytes and len(raw_w) == expected_bytes:
            return HopPlanes(
                strong=np.frombuffer(raw_s, dtype=np.uint64).reshape(sram.shape).copy(),
                weak=np.frombuffer(raw_w, dtype=np.uint64).reshape(sram.shape).copy(),
            )
    planes = build_hop_planes(root, sram, row_index)
    try:
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_bytes(planes.strong.tobytes())
        wp.write_bytes(planes.weak.tobytes())
    except Exception as exc:
        log.warning("thought-engine: hop planes persist failed (%s)", exc)
    return planes


def _load_hop_planes_from_view(view, sram: np.ndarray) -> HopPlanes:
    """Cut 13 generation mode (D-13-3): the planes are immutable, verified artifacts of ONE
    generation — loaded from the view, never built, never written; a missing / corrupt /
    wrong-size plane raises GenerationIntegrityError (lane unavailable)."""
    from memory.projection.generation import GenerationIntegrityError

    expected_bytes = sram.shape[0] * ENTRY_WORDS * 8
    raw_s = view.read_bytes(HOP_PLANE_STRONG_ID)
    raw_w = view.read_bytes(HOP_PLANE_WEAK_ID)
    if len(raw_s) != expected_bytes or len(raw_w) != expected_bytes:
        raise GenerationIntegrityError(
            f"generation {view.generation_id[:12]}…: hop plane size {len(raw_s)}/{len(raw_w)} != sram {expected_bytes} bytes")
    return HopPlanes(
        strong=np.frombuffer(raw_s, dtype=np.uint64).reshape(sram.shape).copy(),
        weak=np.frombuffer(raw_w, dtype=np.uint64).reshape(sram.shape).copy(),
    )
