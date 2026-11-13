"""Immutable receipts binding canonical claims to their cited source.

The SourceLedger proves what external span was admitted.  A projection receipt
proves which stable canonical claim Omega derived from that span.  Mutable
runtime counters and derived graph links are deliberately excluded; authority,
privacy, routing, enrichment, and contradiction fields are included.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping


TRANSFORM_VERSION = "omega.canonical_projection.v3"
RECEIPT_SCHEMA_VERSION = 2
PROVENANCE_CLAIM_EXCLUDE = frozenset({"confidence"})
DERIVED_CHAIN_FIELDS = frozenset({"version"})
CLAIM_FIELDS = (
    "id",
    "coord",
    "type",
    "title",
    "summary",
    "text",
    "tags",
    "domain",
    "subtopic",
    "canonical",
    "version",
    "status",
    "permissions",
    "contradiction_state",
    "flags",
    "enrichment",
    "tool_spec",
    "trait",
    "trait_observation",
    "speech_act",
    "write_strength",
    "provenance",
)
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._-]+")


class ProjectionReceiptConflictError(ValueError):
    """An immutable claim or finalized embedding binding changed in place."""


@dataclass(frozen=True)
class ProjectionReceipt:
    schema_version: int
    memory_id: str
    source_id: str
    source_content_hash: str
    source_span_hash: str
    canonical_claim_hash: str
    transform_version: str
    embedding_engine: str
    embedding_model: str
    embedding_dimension: int
    vector_schema_version: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ProjectionReceipt":
        return cls(
            schema_version=int(payload.get("schema_version") or 0),
            memory_id=str(payload.get("memory_id") or ""),
            source_id=str(payload.get("source_id") or ""),
            source_content_hash=str(payload.get("source_content_hash") or ""),
            source_span_hash=str(payload.get("source_span_hash") or ""),
            canonical_claim_hash=str(payload.get("canonical_claim_hash") or ""),
            transform_version=str(payload.get("transform_version") or ""),
            embedding_engine=str(payload.get("embedding_engine") or ""),
            embedding_model=str(payload.get("embedding_model") or ""),
            embedding_dimension=int(payload.get("embedding_dimension") or 0),
            vector_schema_version=int(payload.get("vector_schema_version") or 0),
        )


@dataclass(frozen=True)
class ReceiptVerification:
    ok: bool
    reason: str


def canonical_claim_hash(memory: Mapping[str, object]) -> str:
    # Supersession assigns ``version`` only after the canonical write and
    # projection receipt complete.  It is derived chain structure, like the
    # intentionally unbound ``supersedes``/``superseded_by``/``links`` fields,
    # rather than a claim derived from the cited source.  Keep the key at its
    # historical ``null`` value so receipts issued before chain linking remain
    # byte-compatible when the hook later assigns a numeric version.
    claim = {
        field: None if field in DERIVED_CHAIN_FIELDS else memory.get(field)
        for field in CLAIM_FIELDS
    }
    provenance = claim.get("provenance")
    if isinstance(provenance, Mapping):
        claim["provenance"] = {
            str(key): value
            for key, value in provenance.items()
            if str(key) not in PROVENANCE_CLAIM_EXCLUDE
        }
    encoded = json.dumps(
        claim,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def receipt_path(memory_id: str, root: Path | str | None = None) -> Path:
    directory = _memory_root(root) / "source_ledger" / "projection_receipts"
    path = directory / f"{_safe_id(memory_id)}.json"
    path.resolve().relative_to(directory.resolve())
    return path


def write_projection_receipt(
    memory: Mapping[str, object],
    root: Path | str | None = None,
) -> ProjectionReceipt:
    receipt = _receipt_for(memory)
    path = receipt_path(receipt.memory_id, root=root)
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        existing = _read_receipt(path)
        if existing is None:
            raise ProjectionReceiptConflictError(
                f"projection receipt for {receipt.memory_id} is unreadable"
            )
        if not _same_claim_binding(existing, receipt):
            raise ProjectionReceiptConflictError(
                f"immutable projection receipt already exists for {receipt.memory_id}; "
                "claim revisions require supersession with a new memory_id"
            )
        if _same_embedding_binding(existing, receipt):
            return existing
        if existing.embedding_dimension == 0 and receipt.embedding_dimension > 0:
            _atomic_write(path, receipt)
            return receipt
        raise ProjectionReceiptConflictError(
            f"finalized embedding projection changed for {receipt.memory_id}; "
            "embedding revisions require an explicit migration"
        )

    _atomic_write(path, receipt)
    return receipt


def rewrite_projection_receipt_for_repaired_claim(
    memory: Mapping[str, object],
    root: Path | str | None = None,
) -> ProjectionReceipt:
    """Refresh a receipt after a deterministic canonical repair.

    Normal writes must use :func:`write_projection_receipt`, which rejects claim
    changes in-place.  Integrity repair is a narrower authority: it may add or
    rebuild deterministic, source-preserving projection metadata (for example an
    intake packet) after the original receipt was issued.  In that case the
    canonical claim hash must be re-bound, but only if the immutable source and
    embedding bindings did not change.
    """
    replacement = _receipt_for(memory)
    path = receipt_path(replacement.memory_id, root=root)
    existing = _read_receipt(path) if path.exists() else None
    if existing is None:
        raise ProjectionReceiptConflictError(
            f"projection receipt for {replacement.memory_id} is missing or unreadable"
        )
    if not _same_source_binding(existing, replacement):
        raise ProjectionReceiptConflictError(
            f"cannot repair projection receipt for {replacement.memory_id}: source binding changed"
        )
    if not _same_embedding_binding(existing, replacement):
        raise ProjectionReceiptConflictError(
            f"cannot repair projection receipt for {replacement.memory_id}: "
            "embedding binding changed"
        )
    _atomic_write(path, replacement)
    return replacement


def verify_projection_receipt(
    memory: Mapping[str, object],
    root: Path | str | None = None,
) -> ReceiptVerification:
    memory_id = str(memory.get("id") or "")
    if not memory_id:
        return ReceiptVerification(False, "missing_memory_id")
    path = receipt_path(memory_id, root=root)
    if not path.exists():
        return ReceiptVerification(False, "missing_projection_receipt")
    receipt = _read_receipt(path)
    if receipt is None or receipt.schema_version != RECEIPT_SCHEMA_VERSION:
        return ReceiptVerification(False, "malformed_projection_receipt")

    try:
        live = _receipt_for(memory)
    except (TypeError, ValueError):
        return ReceiptVerification(False, "source_mismatch")
    if not _same_source_binding(receipt, live):
        return ReceiptVerification(False, "source_mismatch")
    if receipt.canonical_claim_hash != live.canonical_claim_hash:
        return ReceiptVerification(False, "canonical_claim_hash_mismatch")
    if not _same_embedding_binding(receipt, live):
        return ReceiptVerification(False, "embedding_projection_mismatch")
    return ReceiptVerification(True, "ok")


def memory_root_for_canonical_path(final_path: Path | str) -> Path | None:
    path = Path(final_path).resolve()
    for candidate in (path.parent, *path.parents):
        if candidate.name.lower() == "memory":
            return candidate
    return None


def _receipt_for(memory: Mapping[str, object]) -> ProjectionReceipt:
    memory_id = str(memory.get("id") or "").strip()
    if not memory_id:
        raise ValueError("projection receipt requires memory id")
    provenance = memory.get("provenance")
    provenance = provenance if isinstance(provenance, Mapping) else {}
    source_id = str(provenance.get("source_id") or "")
    content_hash = str(
        provenance.get("source_content_hash") or provenance.get("content_hash") or ""
    )
    span_hash = str(provenance.get("span_hash") or "")
    if not source_id or not content_hash or not span_hash:
        raise ValueError(f"projection receipt requires source citation for {memory_id}")

    engine, model, dimension, vector_schema = _embedding_binding(memory)
    return ProjectionReceipt(
        schema_version=RECEIPT_SCHEMA_VERSION,
        memory_id=memory_id,
        source_id=source_id,
        source_content_hash=content_hash,
        source_span_hash=span_hash,
        canonical_claim_hash=canonical_claim_hash(memory),
        transform_version=TRANSFORM_VERSION,
        embedding_engine=engine,
        embedding_model=model,
        embedding_dimension=dimension,
        vector_schema_version=vector_schema,
    )


def _embedding_binding(memory: Mapping[str, object]) -> tuple[str, str, int, int]:
    raw = memory.get("embeddings")
    embeddings = raw if isinstance(raw, Mapping) else {}
    dimension = int(embeddings.get("dimension") or embeddings.get("dim") or 0)
    if dimension <= 0:
        return "pending", "", 0, 0
    return (
        str(embeddings.get("engine") or "local"),
        str(embeddings.get("model") or ""),
        dimension,
        int(embeddings.get("vector_schema_version") or 1),
    )


def _same_source_binding(a: ProjectionReceipt, b: ProjectionReceipt) -> bool:
    return (
        a.memory_id == b.memory_id
        and a.source_id == b.source_id
        and a.source_content_hash == b.source_content_hash
        and a.source_span_hash == b.source_span_hash
    )


def _same_claim_binding(a: ProjectionReceipt, b: ProjectionReceipt) -> bool:
    return _same_source_binding(a, b) and a.canonical_claim_hash == b.canonical_claim_hash


def _same_embedding_binding(a: ProjectionReceipt, b: ProjectionReceipt) -> bool:
    return (
        a.embedding_engine == b.embedding_engine
        and a.embedding_model == b.embedding_model
        and a.embedding_dimension == b.embedding_dimension
        and a.vector_schema_version == b.vector_schema_version
    )


def _atomic_write(path: Path, receipt: ProjectionReceipt) -> None:
    payload = json.dumps(receipt.to_dict(), ensure_ascii=False, sort_keys=True, indent=2)
    tmp = path.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            with contextlib.suppress(OSError):
                tmp.unlink()


def _read_receipt(path: Path) -> ProjectionReceipt | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        return ProjectionReceipt.from_dict(payload)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _memory_root(root: Path | str | None) -> Path:
    if root is not None:
        base = Path(root)
        return base if base.name.lower() == "memory" else base / "memory"
    from core.paths import MEMORY_ROOT

    return Path(MEMORY_ROOT)


def _safe_id(memory_id: str) -> str:
    original = str(memory_id or "")
    cleaned = _SAFE_ID_RE.sub("_", original).replace("..", "__").strip(".")
    cleaned = cleaned[:96] or "unknown"
    if cleaned != original:
        digest = hashlib.sha256(original.encode("utf-8")).hexdigest()[:12]
        cleaned = f"{cleaned}-{digest}"
    return cleaned
