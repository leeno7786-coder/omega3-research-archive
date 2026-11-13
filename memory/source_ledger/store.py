"""Immutable source artifacts beneath the canonical Dewey corpus.

The ledger stores raw source text as hashed, span-addressed artifacts. Canonical
memory records cite these artifacts through provenance fields; the record remains
a living projection, while the source artifact is the immutable truth anchor.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from core.symphony_time import now_iso
from memory.taxonomy.normalize import canonical_json

SOURCE_ARTIFACT_SCHEMA_VERSION = "omega.source_artifact.v1"


@dataclass(frozen=True)
class SourceArtifact:
    source_id: str
    source_type: str
    content: str
    content_hash: str
    span_start: int
    span_end: int
    span_hash: str
    origin_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = ""
    source_session_id: str = ""
    source_session_timestamp: str = ""
    type: str = "source_artifact"
    schema_version: str = SOURCE_ARTIFACT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "source_id": self.source_id,
            "source_type": self.source_type,
            "origin_id": self.origin_id,
            "content": self.content,
            "content_hash": self.content_hash,
            "span_start": self.span_start,
            "span_end": self.span_end,
            "span_hash": self.span_hash,
            "metadata": dict(self.metadata or {}),
            "created_at": self.created_at,
            "source_session_id": self.source_session_id,
            "source_session_timestamp": self.source_session_timestamp,
            "schema_version": self.schema_version,
        }

    def citation(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_type": self.source_type,
            "span_start": self.span_start,
            "span_end": self.span_end,
            "span_hash": self.span_hash,
            "source_content_hash": self.content_hash,
            "source_session_id": self.source_session_id,
            "source_session_timestamp": self.source_session_timestamp,
        }


def source_text_from_payload(payload: Mapping[str, Any]) -> str:
    text = str(payload.get("text") or "")
    if text:
        return text
    summary = str(payload.get("summary") or "")
    title = str(payload.get("title") or "")
    if summary or title:
        return "\n".join(part for part in (title, summary) if part)
    return json.dumps(dict(payload), sort_keys=True, ensure_ascii=False, default=str)


def ensure_source_citation(
    payload: Mapping[str, Any],
    *,
    source_type: str,
    origin_id: str = "",
    root: Path | str | None = None,
) -> dict[str, Any]:
    """Return provenance citation fields, recording a ledger artifact when absent."""
    existing = {
        key: payload.get(key)
        for key in (
            "source_id",
            "source_type",
            "span_start",
            "span_end",
            "span_hash",
            "source_content_hash",
            "source_session_id",
            "source_session_timestamp",
        )
        if payload.get(key) is not None
    }
    if existing.get("source_id"):
        if not existing.get("source_session_id") or not existing.get("source_session_timestamp"):
            artifact = read_source(str(existing["source_id"]), root=root)
            if artifact is not None:
                existing.update(
                    {
                        key: value
                        for key, value in artifact.citation().items()
                        if value and not existing.get(key)
                    }
                )
        return existing
    artifact = record_source(
        source_text_from_payload(payload),
        source_type=str(source_type or "memory_write"),
        origin_id=str(origin_id or ""),
        metadata=_session_metadata_from_payload(payload, origin_id=origin_id),
        root=root,
    )
    return artifact.citation()


def record_operator_chat_source(
    message: str,
    *,
    cycle_id: str = "",
    session_id: str = "",
    session_timestamp: str = "",
    root: Path | str | None = None,
) -> dict[str, Any]:
    """Record an operator chat utterance and return its ledger citation."""
    intake_timestamp = str(session_timestamp or "").strip()
    metadata = {
        "source_session_id": str(session_id or cycle_id or ""),
    }
    if intake_timestamp:
        metadata["source_session_timestamp"] = intake_timestamp
        metadata["created_at"] = intake_timestamp
    artifact = record_source(
        str(message or ""),
        source_type="operator_chat",
        origin_id=str(cycle_id or ""),
        metadata=metadata,
        root=root,
    )
    return artifact.citation()


def record_source(
    content: str,
    *,
    source_type: str = "memory_write",
    origin_id: str = "",
    metadata: Mapping[str, Any] | None = None,
    span_start: int = 0,
    span_end: int | None = None,
    source_id: str | None = None,
    root: Path | str | None = None,
) -> SourceArtifact:
    text = str(content or "")
    start = max(0, int(span_start or 0))
    end = len(text) if span_end is None else int(span_end)
    end = max(start, min(end, len(text)))
    source_type = str(source_type or "memory_write")
    metadata_dict = dict(metadata or {})
    content_hash = _sha256(text)
    span_text = text[start:end]
    span_hash = _sha256(span_text)
    created_at = _artifact_created_at(
        source_type=source_type,
        origin_id=str(origin_id or ""),
        content_hash=content_hash,
        span_start=start,
        span_end=end,
        span_hash=span_hash,
        metadata=metadata_dict,
    )
    source_session_id = _source_session_id(metadata_dict, origin_id=str(origin_id or ""))
    source_session_timestamp = _source_session_timestamp(metadata_dict, fallback=created_at)
    sid = (
        _sanitize_source_id(source_id)
        if source_id
        else _source_id_for(
            source_type=source_type,
            origin_id=str(origin_id or ""),
            source_session_id=source_session_id,
            source_session_timestamp=source_session_timestamp,
            content_hash=content_hash,
            span_start=start,
            span_end=end,
            span_hash=span_hash,
        )
    )
    artifact = SourceArtifact(
        source_id=sid,
        source_type=source_type,
        origin_id=str(origin_id or ""),
        content=text,
        content_hash=content_hash,
        span_start=start,
        span_end=end,
        span_hash=span_hash,
        metadata=metadata_dict,
        created_at=created_at,
        source_session_id=source_session_id,
        source_session_timestamp=source_session_timestamp,
    )

    path = artifact_path(sid, root=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = read_source(sid, root=root)
        if existing and _same_immutable_artifact(existing, artifact):
            return existing
        raise ValueError(f"immutable source artifact already exists: {sid}")

    payload = canonical_json(artifact.to_dict())
    try:
        with open(path, "x", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
    except FileExistsError:
        existing = read_source(sid, root=root)
        if existing and _same_immutable_artifact(existing, artifact):
            return existing
        raise ValueError(f"immutable source artifact already exists: {sid}") from None
    return artifact


def record_source_bytes(
    data: bytes,
    *,
    source_type: str = "memory_write",
    origin_id: str = "",
    metadata: Mapping[str, Any] | None = None,
    root: Path | str | None = None,
) -> SourceArtifact:
    """Record immutable raw bytes; content is stored as base64 text in the artifact."""
    if data is None:
        raise ValueError("data must be bytes")
    if isinstance(data, str):
        raise TypeError("data must be bytes, not str")
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError(f"data must be bytes, not {type(data).__name__}")
    raw = bytes(data)
    encoded = base64.b64encode(raw).decode("ascii")
    source_type = str(source_type or "memory_write")
    metadata_dict = dict(metadata or {})
    metadata_dict.setdefault("content_encoding", "base64")
    metadata_dict.setdefault("content_kind", "bytes")
    start = 0
    end = len(encoded)
    content_hash = _sha256_bytes(raw)
    span_text = encoded[start:end]
    span_hash = _sha256(span_text)
    created_at = _artifact_created_at(
        source_type=source_type,
        origin_id=str(origin_id or ""),
        content_hash=content_hash,
        span_start=start,
        span_end=end,
        span_hash=span_hash,
        metadata=metadata_dict,
    )
    source_session_id = _source_session_id(metadata_dict, origin_id=str(origin_id or ""))
    source_session_timestamp = _source_session_timestamp(metadata_dict, fallback=created_at)
    origin = str(origin_id or "")
    if origin:
        sid = _source_id_for_event(
            source_type=source_type,
            origin_id=origin,
            source_session_id=source_session_id,
            source_session_timestamp=source_session_timestamp,
        )
    else:
        sid = _source_id_for(
            source_type=source_type,
            origin_id=origin,
            source_session_id=source_session_id,
            source_session_timestamp=source_session_timestamp,
            content_hash=content_hash,
            span_start=start,
            span_end=end,
            span_hash=span_hash,
        )
    artifact = SourceArtifact(
        source_id=sid,
        source_type=source_type,
        origin_id=str(origin_id or ""),
        content=encoded,
        content_hash=content_hash,
        span_start=start,
        span_end=end,
        span_hash=span_hash,
        metadata=metadata_dict,
        created_at=created_at,
        source_session_id=source_session_id,
        source_session_timestamp=source_session_timestamp,
    )

    path = artifact_path(sid, root=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = read_source(sid, root=root)
        if existing and _same_immutable_artifact(existing, artifact):
            return existing
        raise ValueError(f"immutable source artifact already exists: {sid}")

    payload = canonical_json(artifact.to_dict())
    try:
        with open(path, "x", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
    except FileExistsError:
        existing = read_source(sid, root=root)
        if existing and _same_immutable_artifact(existing, artifact):
            return existing
        raise ValueError(f"immutable source artifact already exists: {sid}") from None
    return artifact


def read_source(source_id: str, *, root: Path | str | None = None) -> SourceArtifact | None:
    sid = _sanitize_source_id(source_id)
    path = artifact_path(sid, root=root)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return SourceArtifact(
        source_id=str(data.get("source_id") or sid),
        source_type=str(data.get("source_type") or "memory_write"),
        origin_id=str(data.get("origin_id") or ""),
        content=str(data.get("content") or ""),
        content_hash=str(data.get("content_hash") or ""),
        span_start=int(data.get("span_start") or 0),
        span_end=int(data.get("span_end") or 0),
        span_hash=str(data.get("span_hash") or ""),
        metadata=dict(data.get("metadata") or {}),
        created_at=str(data.get("created_at") or ""),
        source_session_id=str(data.get("source_session_id") or data.get("origin_id") or ""),
        source_session_timestamp=str(
            data.get("source_session_timestamp") or data.get("created_at") or ""
        ),
        schema_version=str(data.get("schema_version") or SOURCE_ARTIFACT_SCHEMA_VERSION),
    )


def artifact_path(source_id: str, *, root: Path | str | None = None) -> Path:
    return _ledger_root(root) / "artifacts" / f"{_sanitize_source_id(source_id)}.json"


def _ledger_root(root: Path | str | None = None) -> Path:
    return _memory_root(root) / "source_ledger"


def _memory_root(root: Path | str | None = None) -> Path:
    if root is not None:
        base = Path(root)
        return base if base.name == "memory" else base / "memory"
    try:
        from memory.engine import validate_write

        return Path(validate_write.MEMORY_ROOT)
    except Exception:
        from core.paths import OMEGA_ROOT

        return OMEGA_ROOT / "memory"


def _source_id_for(
    *,
    source_type: str,
    origin_id: str,
    source_session_id: str,
    source_session_timestamp: str,
    content_hash: str,
    span_start: int,
    span_end: int,
    span_hash: str,
) -> str:
    # A non-empty origin_id is the immutable event identity (operator cycle,
    # connector revision, etc.). Its wall-clock timestamp belongs in the
    # artifact for temporal grounding, but not in the address: a retry of the
    # same event must resolve to the first immutable artifact.
    address_timestamp = "" if origin_id else source_session_timestamp
    raw = canonical_json(
        [
            source_type,
            origin_id,
            source_session_id,
            address_timestamp,
            content_hash,
            span_start,
            span_end,
            span_hash,
        ]
    )
    return "src_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _source_id_for_event(
    *,
    source_type: str,
    origin_id: str,
    source_session_id: str,
    source_session_timestamp: str,
) -> str:
    """Event identity for byte artifacts: origin_id alone addresses the immutable event."""
    address_timestamp = "" if origin_id else source_session_timestamp
    raw = canonical_json([source_type, origin_id, source_session_id, address_timestamp])
    return "src_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _sanitize_source_id(raw: str | None) -> str:
    value = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(raw or "").strip())
    value = value.strip("._-")
    if not value:
        raise ValueError("source_id is required")
    return value


def _same_immutable_artifact(existing: SourceArtifact, new: SourceArtifact) -> bool:
    return (
        existing.source_type == new.source_type
        and existing.origin_id == new.origin_id
        and existing.source_session_id == new.source_session_id
        and (
            existing.source_session_timestamp == new.source_session_timestamp
            or bool(existing.origin_id)
        )
        and existing.content_hash == new.content_hash
        and existing.span_start == new.span_start
        and existing.span_end == new.span_end
        and existing.span_hash == new.span_hash
    )


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _artifact_created_at(
    *,
    source_type: str,
    origin_id: str,
    content_hash: str,
    span_start: int,
    span_end: int,
    span_hash: str,
    metadata: Mapping[str, Any],
) -> str:
    explicit = dict(metadata or {}).get("created_at")
    if explicit:
        return str(explicit)
    return now_iso()


def _session_metadata_from_payload(
    payload: Mapping[str, Any], *, origin_id: str = ""
) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for source_key, dest_key in (
        ("source_session_id", "source_session_id"),
        ("session_id", "source_session_id"),
        ("source_session_timestamp", "source_session_timestamp"),
        ("session_timestamp", "source_session_timestamp"),
        ("timestamp_created", "source_session_timestamp"),
        ("created_at", "created_at"),
    ):
        value = payload.get(source_key)
        if value is not None and str(value).strip():
            metadata.setdefault(dest_key, str(value))
    if origin_id and "source_session_id" not in metadata:
        metadata["source_session_id"] = str(origin_id)
    return metadata


def _source_session_id(metadata: Mapping[str, Any], *, origin_id: str) -> str:
    for key in ("source_session_id", "session_id", "sessionId", "answer_session_id"):
        value = dict(metadata or {}).get(key)
        if value is not None and str(value).strip():
            return str(value)
    return str(origin_id or "")


def _source_session_timestamp(metadata: Mapping[str, Any], *, fallback: str) -> str:
    for key in (
        "source_session_timestamp",
        "session_timestamp",
        "sessionTimestamp",
        "timestamp_created",
        "created_at",
        "created",
        "timestamp",
    ):
        value = dict(metadata or {}).get(key)
        if value is not None and str(value).strip():
            return str(value)
    return str(fallback or "")
