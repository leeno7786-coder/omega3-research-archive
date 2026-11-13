"""The semantic retina: perception for the memory organ, authority over nothing.

Ported from the omega2.5 tree (``organ/retina.py``) on 2026-09-05: "the embed
model should be a 20m ettin encoder". The artifact schema is kept identical so a
generation built by either tree reads the same.

WHAT IT IS

A frozen Ettin 17M encoder, loaded in-process on CPU, whose FP32 output is
quantised per row to INT8, projected through a fixed Rademacher {-1,+1} sign
bank, and packed into a D=16384 BINARY code. Those codes are scanned by
popcount, exactly like the DME's own planes — which is the point. The organ
stores no dense vector anywhere (17 §1 as amended by 21 §2); it stores bits of
the same kind its engine already consumes.

WHY IT IS ALLOWED TO EXIST

`17_EMBED_MODEL_OBSOLESCENCE_MASTER_PLAN` ends the program when no dense model
"writes, indexes, relates, verifies, consolidates or identifies" a record. Every
verb there is an AUTHORITY. ADR-MO-018 and ADR-MO-020 place this encoder inside
the process holding none of them: it proposes candidates and nothing else. The
RecallVerifier still decides what may ground an answer; the canonical filesystem
is still truth; DeepSleep still owns persistence. A retina that proposed and
also decided would be the dense engine again under a new name.

Concretely, that means this module may only ever RETURN CANDIDATE IDS. It writes
no memory, sets no strength, creates no edge, and its output is never evidence.

WHY IT IS PERSISTED HERE RATHER THAN IN ``memory/semantic_plane.py``

``SemanticPlane`` is built and verified (5a338f85) and holds its codes in
memory. Rebuilding it at every session start would mean an Ettin forward pass
per record — minutes for a real organ, to recompute something deterministic.
So the codes are written beside the DME planes as a generation artifact and
mapped back on load. ``semantic_plane.py`` is composed, never modified: it
carries a proof about exactness and batch-invariance that an edit would put back
in question.

A GENERATION ARTIFACT, READ THROUGH THE GENERATION (audit 2026-09-28 M01/M05)

The codes and their index are pinned inside the generation content
(``components["semantic_retina"]``) with the encoder/render contract they are a
function of, so a missing, corrupt or foreign artifact fails validation instead
of recalling without perception. Reads bind to the generation captured by the
caller's projection read transaction: a lane still holding G1 perceives through
G1's codes while a lane that opened after G2 was published perceives G2. There is
no process-wide "current" retina for a publication to swap.

DETERMINISM CAVEAT THAT SHAPED THE WRITE PATH

Transformer forwards are NOT batch-invariant: changing the batch shape changes
GEMM tiling and the last ulp moves (measured 7.5e-08 … 1.2e-07). The INT8
quantisation absorbs it entirely — 0/2048 elements and 0/131072 bits differed
across batchings — so codes are stable even though the floats are not. Encoding
is still done in fixed-size batches so the FP32 stage is reproducible too.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

log = logging.getLogger("omega.memory.retina")

CODES_FILE = "retina_codes.bin"
INDEX_FILE = "retina_index.json"
SCHEMA = "organ.retina.v1"
RENDER_SCHEMA = "omega.semantic_retina.render.v1"
CONTRACT_SCHEMA = "omega.semantic_retina.contract.v1"

#: Matches ``memory.semantic_plane`` — changing it invalidates every stored code.
DIMENSION = 16384
_CODE_BYTES = DIMENSION // 8


class RetinaUnavailable(RuntimeError):
    """The retina cannot serve this call. The cause travels with it."""


@dataclass(frozen=True)
class RetinaHit:
    record_id: str
    distance: int          # Hamming distance in bits; lower is nearer

    @property
    def similarity(self) -> float:
        """Distance mapped to [0, 1] so lane arbitration can compare it."""
        return max(0.0, 1.0 - (self.distance / DIMENSION))


def render_record(record: Mapping[str, object]) -> str:
    """Render one canonical record through the frozen Program-I contract.

    Only ``title``, ``summary`` and ``text`` participate, in that order. Values
    are stripped, empty values are omitted, and exact duplicates retain only
    their first occurrence. Structured metadata must never leak into the
    encoder input.
    """
    rendered: list[str] = []
    seen: set[str] = set()
    for field_name in ("title", "summary", "text"):
        raw = record.get(field_name)
        value = raw.strip() if isinstance(raw, str) else ""
        if value and value not in seen:
            seen.add(value)
            rendered.append(value)
    return "\n".join(rendered)


# ── the encoder seat ─────────────────────────────────────────────────────────


def snapshot_path() -> Path:
    """Locate the pinned Ettin snapshot INSIDE THE BRAIN.

    Operator 2026-09-05: "the ettin goes in the brain and is loaded at boot."
    The weights live under
    ``<OMEGA_ROOT>/memory/models/`` (the encoder seat's own resolution order
    finds them there, after an explicit ``OMEGA_ETTIN_SNAPSHOT``), every pinned
    hash is verified before any load, and boot.py calls ``encoder()`` once so
    the seat is resident for the active session and released by shutdown.
    """
    from core.paths import OMEGA_ROOT
    from memory.ettin_encoder import (
        ETTIN_CACHE_DIRNAME,
        ETTIN_HASHES,
        ETTIN_SNAPSHOT,
        resolve_snapshot,
        verify_ettin_snapshot,
    )

    local = OMEGA_ROOT / "memory" / "models" / ETTIN_CACHE_DIRNAME / "snapshots" / ETTIN_SNAPSHOT
    if all((local / relative).exists() for relative in ETTIN_HASHES):
        verify_ettin_snapshot(local)
        return local
    # Not in the brain: an explicitly configured snapshot may still carry it;
    # the seat verifies that path and never searches host caches.
    return resolve_snapshot()


_encoder = None


def encoder():
    """The process-wide encoder. Loaded once, on first perception."""
    global _encoder
    if _encoder is None:
        from memory.ettin_encoder import load_ettin_fp32

        snapshot = snapshot_path()
        log.info("retina: loading Ettin FP32 from %s", snapshot)
        _encoder = load_ettin_fp32(snapshot)
    return _encoder


def reset_encoder() -> None:
    global _encoder
    _encoder = None


def _plane():
    """A SemanticPlane bound to this organ's encoder."""
    from memory.semantic_plane import PlaneConfig, SemanticPlane

    return SemanticPlane(encoder(), config=PlaneConfig(dimension=DIMENSION))


# ── the contract every stored code is a function of ──────────────────────────


def encoder_contract() -> dict:
    """The pinned encoder, renderer and projection identity behind every code.

    A code is a pure function of (rendered text, this contract). The encoder is
    identified by its content hashes — the seat refuses to load anything else —
    so codes built under one contract are never read or reused under another.
    """
    from memory.ettin_encoder import ETTIN_DIM, ETTIN_HASHES, ETTIN_MAX_TOKENS, ETTIN_MODEL_ID, ETTIN_SNAPSHOT
    from memory.semantic_plane import ALGORITHM, PROJECTION_NAMESPACE, PlaneConfig

    return {
        "schema": CONTRACT_SCHEMA,
        "codes_schema": SCHEMA,
        "render_schema": RENDER_SCHEMA,
        "encoder": {
            "model": ETTIN_MODEL_ID,
            "snapshot": ETTIN_SNAPSHOT,
            "latent_dim": ETTIN_DIM,
            "max_tokens": ETTIN_MAX_TOKENS,
            "artifact_sha256": dict(sorted(ETTIN_HASHES.items())),
        },
        "plane": {
            "algorithm": ALGORITHM,
            "projection_namespace": PROJECTION_NAMESPACE,
            "dimension": DIMENSION,
            "seed_index": PlaneConfig(dimension=DIMENSION).seed_index,
        },
    }


def contract_sha256() -> str:
    return _sha256(json.dumps(encoder_contract(), sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _text_hash(text: str) -> str:
    return _sha256(str(text).encode("utf-8"))


# ── the immutable view one reader perceives through ──────────────────────────


@dataclass(frozen=True, eq=False)
class RetinaView:
    """One generation's codes, verified and read-only."""

    directory: Path
    generation_id: str | None      # None for the retired fixed-name directory
    ids: tuple[str, ...]
    text_sha256: tuple[str, ...]
    codes: np.ndarray               # (len(ids), DIMENSION // 8) uint8, read-only
    contract_sha256: str

    def __len__(self) -> int:
        return len(self.ids)


def _parse(index_bytes: bytes, codes_bytes: bytes, *, directory: Path, generation_id: str | None) -> RetinaView:
    """Strictly decode one retina artifact pair. Anything short of a complete,
    self-consistent artifact under the pinned contract raises."""
    where = generation_id[:12] + "…" if generation_id else str(directory)
    try:
        meta = json.loads(index_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RetinaUnavailable(f"retina index for {where} is unreadable: {exc}") from exc
    if not isinstance(meta, dict):
        raise RetinaUnavailable(f"retina index for {where} is not an object")
    if meta.get("schema") != SCHEMA or meta.get("render_schema") != RENDER_SCHEMA:
        raise RetinaUnavailable(
            f"retina index for {where} has schema ({meta.get('schema')!r}, {meta.get('render_schema')!r})"
        )
    if meta.get("dimension") != DIMENSION:
        raise RetinaUnavailable(f"retina index for {where} has dimension {meta.get('dimension')!r} != {DIMENSION}")
    expected_contract = contract_sha256()
    if meta.get("contract_sha256") != expected_contract:
        raise RetinaUnavailable(
            f"retina codes for {where} were built under encoder contract "
            f"{str(meta.get('contract_sha256'))[:12]}…, not the pinned {expected_contract[:12]}…"
        )
    ids, hashes = meta.get("record_ids"), meta.get("text_sha256")
    if (
        not isinstance(ids, list) or not ids
        or not all(isinstance(rid, str) and rid for rid in ids)
        or len(set(ids)) != len(ids)
        or not isinstance(hashes, list) or len(hashes) != len(ids)
        or not all(isinstance(h, str) and len(h) == 64 for h in hashes)
        or meta.get("count") != len(ids)
    ):
        raise RetinaUnavailable(f"retina index for {where} is malformed")
    if len(codes_bytes) != len(ids) * _CODE_BYTES:
        raise RetinaUnavailable(
            f"retina codes for {where} hold {len(codes_bytes)} bytes, index names {len(ids)} rows"
        )
    codes = np.frombuffer(codes_bytes, dtype=np.uint8).reshape(len(ids), _CODE_BYTES).copy()
    codes.flags.writeable = False
    return RetinaView(directory=Path(directory), generation_id=generation_id, ids=tuple(ids),
                      text_sha256=tuple(hashes), codes=codes, contract_sha256=expected_contract)


def read(directory: Path) -> RetinaView:
    """Read a retina artifact pair from ``directory``. Missing or invalid raises."""
    directory = Path(directory)
    try:
        index_bytes = (directory / INDEX_FILE).read_bytes()
        codes_bytes = (directory / CODES_FILE).read_bytes()
    except OSError as exc:
        raise RetinaUnavailable(f"retina artifacts unreadable in {directory}: {exc}") from exc
    return _parse(index_bytes, codes_bytes, directory=directory, generation_id=None)


# ── build (projection time) ──────────────────────────────────────────────────


def empty_component() -> dict:
    """The generation component for a corpus with nothing to perceive."""
    return {"status": "empty", "schema": SCHEMA, "contract_sha256": contract_sha256(), "records": 0}


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(path)


def build(records: Sequence[tuple[str, str]], out_dir: Path, *, prior: RetinaView | None = None) -> dict:
    """Encode ``(record_id, text)`` pairs into a persisted code plane.

    Called by DeepSleep when it publishes a generation, so the cost lands in
    consolidation where slow work belongs — never on a recall.

    Incremental (audit 2026-09-28 M08): ``prior`` is the previous generation's
    VERIFIED view. A code is a pure function of the rendered text under the
    pinned contract, so a text whose hash the prior already holds reuses that
    code instead of a forward pass; only new or changed texts are encoded. A
    prior built under another contract is never consulted.

    Returns the build report; ``report["component"]`` is the generation content
    that pins these exact artifacts. An encoder failure raises ``RetinaUnavailable``
    with its cause — a generation is never published without the perception it
    was asked to carry.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    from memory.semantic_plane import STORED

    pinned = contract_sha256()
    reusable: dict[str, np.ndarray] = {}
    if prior is not None and prior.contract_sha256 == pinned:
        reusable = {digest: prior.codes[row] for row, digest in enumerate(prior.text_sha256)}
    plane = _plane()
    ids: list[str] = []
    hashes: list[str] = []
    reused = 0
    encoded = 0
    for record_id, text in records:
        if not str(text or "").strip():
            continue
        if record_id in plane:
            raise RetinaUnavailable(f"duplicate retina record id {record_id!r}")
        digest = _text_hash(text)
        code = reusable.get(digest)
        if code is not None:
            plane.put_code(record_id, text, code)
            reused += 1
        else:
            try:
                stored = plane.put(record_id, text)
            except Exception as exc:  # noqa: BLE001 — the cause travels with the failure
                raise RetinaUnavailable(
                    f"encode failed for {record_id!r}: {type(exc).__name__}: {exc}"
                ) from exc
            if stored != STORED:
                continue                     # quantises to no direction: nothing to perceive
            reusable[digest] = plane.code(record_id)
            encoded += 1
        ids.append(record_id)
        hashes.append(digest)

    if not ids:
        raise RetinaUnavailable("no encodable records")

    codes = np.stack([plane.code(rid) for rid in ids]).astype(np.uint8)
    if codes.shape[1] != _CODE_BYTES:
        raise RetinaUnavailable(
            f"code width {codes.shape[1]} bytes != expected {_CODE_BYTES}"
        )
    codes_bytes = codes.tobytes()
    index_bytes = json.dumps(
        {
            "schema": SCHEMA,
            "render_schema": RENDER_SCHEMA,
            "dimension": DIMENSION,
            "contract_sha256": pinned,
            "contract": encoder_contract(),
            "count": len(ids),
            "fingerprint": plane.fingerprint(),
            "record_ids": ids,
            "text_sha256": hashes,
        },
        indent=2,
    ).encode("utf-8")
    _write_atomic(out_dir / CODES_FILE, codes_bytes)
    _write_atomic(out_dir / INDEX_FILE, index_bytes)
    log.info(
        "retina: built %d codes into %s (encoded %d, reused %d)",
        len(ids), out_dir, encoded, reused,
    )
    return {
        "count": len(ids),
        "dimension": DIMENSION,
        "fingerprint": plane.fingerprint(),
        "encoded": encoded,
        "reused": reused,
        "component": {
            "status": "built",
            "schema": SCHEMA,
            "contract_sha256": pinned,
            "records": len(ids),
            "codes_sha256": _sha256(codes_bytes),
            "index_sha256": _sha256(index_bytes),
        },
    }


# ── generation-bound views (recall time) ─────────────────────────────────────

_POPCOUNT = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)
_VIEW_CACHE_LIMIT = 4       # G-1, G, G+1 around a publication, with headroom
_views: "OrderedDict[str, RetinaView]" = OrderedDict()
_views_lock = threading.Lock()
# The retired fixed-name projection (no manifest) maps its directory here via
# ``load``. Generation reads never consult it.
_legacy: dict[str, RetinaView | None] = {"view": None}


def generation_retina(view: Any) -> RetinaView | None:
    """The verified retina of one captured generation; None for an empty plane.

    Artifact bytes are hashed against the generation's pinned component before a
    single code is served, and the component's contract must be the pinned one.
    Missing, corrupt, foreign or pre-contract artifacts raise ``RetinaUnavailable``
    carrying the cause. Generations are immutable, so a verified view is cached by
    generation id and shared by every reader of that generation.
    """
    from memory.projection.generation import GenerationIntegrityError

    generation_id = view.generation_id
    component = view.manifest.content.components.get("semantic_retina")
    where = generation_id[:12] + "…"
    if not isinstance(component, dict):
        raise RetinaUnavailable(f"generation {where} predates the semantic-retina contract (no pinned component)")
    pinned = contract_sha256()
    if component.get("contract_sha256") != pinned:
        raise RetinaUnavailable(
            f"generation {where} was built under encoder contract "
            f"{str(component.get('contract_sha256'))[:12]}…, not the pinned {pinned[:12]}…"
        )
    if component.get("status") == "empty":
        return None
    with _views_lock:
        cached = _views.get(generation_id)
        if cached is not None:
            _views.move_to_end(generation_id)
            return cached
    try:
        index_bytes = view.read_bytes(INDEX_FILE)
        codes_bytes = view.read_bytes(CODES_FILE)
    except (GenerationIntegrityError, OSError) as exc:
        raise RetinaUnavailable(f"generation {where} retina artifacts unavailable: {exc}") from exc
    loaded = _parse(index_bytes, codes_bytes, directory=view.generation_dir, generation_id=generation_id)
    if len(loaded) != component.get("records"):
        raise RetinaUnavailable(
            f"generation {where} retina holds {len(loaded)} codes, component pins {component.get('records')}"
        )
    with _views_lock:
        _views[generation_id] = loaded
        _views.move_to_end(generation_id)
        while len(_views) > _VIEW_CACHE_LIMIT:
            _views.popitem(last=False)
    return loaded


def _binding() -> tuple[str, RetinaView | None]:
    """What the caller perceives through: the generation its read transaction
    captured, or — with no manifest — the retired fixed-name mapping."""
    from memory.projection.generation import current_read_context

    ctx = current_read_context()
    if ctx is None or ctx.mode == "legacy":
        return "legacy", _legacy["view"]
    if ctx.mode == "unavailable":
        raise RetinaUnavailable(
            f"projection generation unavailable: {ctx.reason or 'unspecified cause'}"
        )
    return "generation", generation_retina(ctx.view)


def bound_generation() -> dict:
    """Verify the retina of the generation captured by the ambient read transaction.

    Boot's readiness check: raises ``RetinaUnavailable`` unless a published
    generation is bound and its semantic plane is either verified or declared
    empty.
    """
    from memory.projection.generation import current_read_context

    ctx = current_read_context()
    if ctx is None or ctx.mode != "generation":
        mode = ctx.mode if ctx is not None else "no read transaction"
        reason = f": {ctx.reason}" if ctx is not None and ctx.reason else ""
        raise RetinaUnavailable(f"no published projection generation is bound ({mode}{reason})")
    view = generation_retina(ctx.view)
    return {
        "state": "mapped" if view is not None else "empty",
        "generation_id": ctx.view.generation_id,
        "codes": len(view) if view is not None else 0,
    }


def load(gen_dir: Path) -> RetinaView:
    """Map the retired fixed-name projection's retina for no-manifest reads.

    A view switch is atomic from the reader's perspective: the previous mapping
    is dropped first, so a missing or invalid artifact leaves nothing eligible —
    and raises with its cause instead of reporting a cold retina.
    """
    _legacy["view"] = None
    view = read(gen_dir)
    _legacy["view"] = view
    return view


def legacy_view() -> RetinaView | None:
    return _legacy["view"]


def unload() -> None:
    _legacy["view"] = None
    with _views_lock:
        _views.clear()


def is_loaded() -> bool:
    """True when the caller's generation carries codes to perceive through."""
    return _binding()[1] is not None


def encode_code(text: str) -> np.ndarray | None:
    """Text -> packed D=16384 binary code, by the same path ``put`` uses.

    ``SemanticPlane._encode_one`` returns the FP32 LATENT, not the code; the
    code is ``quantize_row`` then the Rademacher sign projection. Going through
    the plane's own helpers keeps the query on exactly the write path's
    arithmetic — a query projected any other way would be measuring a different
    geometry than the one the records were stored in.

    Returns None for text the plane would refuse (no signal, or a latent that
    quantises to all zeros), because a code that means nothing is worse than no
    code at all.
    """
    from memory.semantic_plane import NO_SIGNAL, _project, quantize_row

    try:
        plane = _plane()
        if plane.query_status(text) == NO_SIGNAL:
            return None
        latent = plane._encode_one(text)          # noqa: SLF001 — the encode contract
        codes = quantize_row(latent)
        if not np.any(codes):
            return None
        packed = _project(codes, plane._bank, plane._tie_bits)   # noqa: SLF001
    except Exception as exc:  # noqa: BLE001
        raise RetinaUnavailable(
            f"query encode failed: {type(exc).__name__}: {exc}"
        ) from exc
    return np.asarray(packed, dtype=np.uint8)


def similarity(left: str, right: str) -> float | None:
    """Compare two query strings in the Ettin retina's binary geometry."""
    left_code = encode_code(left)
    right_code = encode_code(right)
    if left_code is None or right_code is None:
        return None
    distance = sum(
        (int(a) ^ int(b)).bit_count()
        for a, b in zip(left_code, right_code, strict=True)
    )
    return max(0.0, 1.0 - (distance / DIMENSION))


def window(text: str, k: int) -> list[RetinaHit]:
    """The ``k`` nearest stored codes to ``text``, nearest first.

    Scans the codes of the generation the caller's read transaction captured.
    Returns [] when that generation holds nothing to perceive or the query
    carries no signal; an unavailable retina raises ``RetinaUnavailable``.
    """
    view = _binding()[1]
    if view is None or k <= 0:
        return []
    code = encode_code(text)
    if code is None:
        return []

    distances = _POPCOUNT[view.codes ^ code[None, :]].sum(axis=1, dtype=np.int32)
    k = min(k, len(distances))
    nearest = np.argpartition(distances, k - 1)[:k]
    nearest = nearest[np.argsort(distances[nearest], kind="stable")]
    return [RetinaHit(view.ids[i], int(distances[i])) for i in nearest]


def retrieval_k(pool: int | None = None) -> int:
    """K sized to the pool by the A11 curve, floored at the frozen minimum."""
    from memory.semantic_plane import retrieval_k_for_pool

    if pool is None:
        view = _binding()[1]
        pool = len(view) if view is not None else 0
    return retrieval_k_for_pool(max(0, int(pool)))


def record_ids() -> tuple[str, ...]:
    """The immutable record IDs in the caller's bound perception plane."""
    view = _binding()[1]
    return view.ids if view is not None else ()


def _describe(kind: str, view: RetinaView | None, generation_id: str | None) -> dict:
    state = {"generation": "mapped" if view is not None else "empty",
             "legacy": "mapped" if view is not None else "unbound"}[kind]
    return {
        "encoder_resident": _encoder is not None,
        "model": "tasksource/ettin-17m-embed",
        "loaded": view is not None,
        "state": state,
        "generation": str(view.directory) if view is not None else None,
        "generation_id": generation_id,
        "codes": len(view) if view is not None else 0,
        "dimension": DIMENSION,
    }


def stats() -> dict:
    """The retina the caller perceives through — the ambient read transaction's
    generation, else the active generation of the default memory root. An
    unavailable retina raises rather than reporting a cold one."""
    from memory.projection.generation import current_read_context, projection_read_transaction

    if current_read_context() is None:
        with projection_read_transaction(None) as ctx:
            kind, view = _binding()
            return _describe(kind, view, ctx.generation_id)
    ctx = current_read_context()
    kind, view = _binding()
    return _describe(kind, view, ctx.generation_id)


__all__ = [
    "CODES_FILE", "CONTRACT_SCHEMA", "DIMENSION", "INDEX_FILE", "RENDER_SCHEMA", "RetinaHit",
    "RetinaUnavailable", "RetinaView", "bound_generation", "build", "contract_sha256",
    "empty_component", "encode_code", "encoder", "encoder_contract", "generation_retina",
    "is_loaded", "legacy_view", "load", "read", "record_ids", "render_record", "reset_encoder",
    "retrieval_k", "similarity", "snapshot_path", "stats", "unload", "window",
]
