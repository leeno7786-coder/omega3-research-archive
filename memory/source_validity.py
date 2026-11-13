"""Derived Workspace source-validity projection for recall verification."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Mapping

from core.paths import OMEGA_ROOT

SCHEMA_VERSION = 1
_PROJECTION_NAME = "source_validity.json"
_AVAILABLE_STATES = frozenset({"available"})
_TOMBSTONE_STATES = frozenset({"tombstoned", "tombstone", "removed", "deleted"})
_WORKSPACE_FIELDS = (
    "connection_id",
    "capability",
    "object_id",
    "revision_id",
    "content_digest",
    "source_ledger_receipt",
    "observed_at",
    "availability_state",
)


class SourceValidityError(RuntimeError):
    """Source-validity projection is missing, corrupt, or cannot prove a source."""


def projection_path(root: Path | str | None = None) -> Path:
    return _memory_root(root) / "indexes" / _PROJECTION_NAME


def rebuild_from_canonical(root: Path | str | None = None) -> dict[str, int]:
    """Rebuild the Workspace source projection from canonical memory files."""

    memory_root = _memory_root(root)
    root_base = memory_root.parent
    records_scanned = 0
    workspace_records = 0
    grouped: dict[str, list[dict[str, Any]]] = {}

    for path in _canonical_json_paths(memory_root):
        records_scanned += 1
        record = _read_record(path)
        if not isinstance(record, Mapping):
            continue
        raw_source = record.get("workspace_source")
        if raw_source is None:
            continue
        source = _validate_workspace_source(raw_source, path)
        workspace_records += 1
        key = _object_key(
            source["connection_id"],
            source["capability"],
            source["object_id"],
        )
        grouped.setdefault(key, []).append(_revision_entry(source, record, path, root_base))

    objects: dict[str, dict[str, Any]] = {}
    available_count = 0
    unavailable_count = 0
    tombstoned_count = 0
    revision_count = 0
    for key in sorted(grouped):
        revisions = sorted(grouped[key], key=_revision_sort_key)
        revision_count += len(revisions)
        state = _object_state(revisions)
        objects[key] = state
        if state["available"]:
            available_count += 1
        else:
            unavailable_count += 1
        if state.get("tombstone_revision_id"):
            tombstoned_count += 1

    counts = {
        "records_scanned": records_scanned,
        "workspace_records": workspace_records,
        "objects": len(objects),
        "revisions": revision_count,
        "available": available_count,
        "unavailable": unavailable_count,
        "tombstoned": tombstoned_count,
    }
    projection = {
        "schema_version": SCHEMA_VERSION,
        "counts": counts,
        "objects": objects,
    }
    _atomic_write_json(projection_path(root), projection)
    return counts


def load_projection(root: Path | str | None = None) -> dict[str, Any]:
    path = projection_path(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SourceValidityError(f"source validity projection missing: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise SourceValidityError(f"source validity projection unreadable: {path}") from exc
    if not isinstance(data, dict):
        raise SourceValidityError("source validity projection must be a JSON object")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise SourceValidityError("source validity projection schema version mismatch")
    if not isinstance(data.get("objects"), dict):
        raise SourceValidityError("source validity projection objects missing")
    if not isinstance(data.get("counts"), dict):
        raise SourceValidityError("source validity projection counts missing")
    return data


def lookup_object(
    root: Path | str | None,
    connection_id: str,
    capability: str,
    object_id: str,
) -> dict[str, Any] | None:
    projection = load_projection(root)
    item = projection["objects"].get(_object_key(connection_id, capability, object_id))
    return dict(item) if isinstance(item, Mapping) else None


def lookup_revision(
    root: Path | str | None,
    workspace_source: Mapping[str, Any],
) -> dict[str, Any] | None:
    source = _validate_workspace_source_identity(workspace_source, projection_path(root))
    state = lookup_object(root, source["connection_id"], source["capability"], source["object_id"])
    if not state:
        return None
    revisions = state.get("revisions")
    if not isinstance(revisions, Mapping):
        raise SourceValidityError("source validity projection revisions missing")
    revision = revisions.get(source["revision_id"])
    return dict(revision) if isinstance(revision, Mapping) else None


def verify_workspace_source(
    workspace_source: Mapping[str, Any],
    *,
    root: Path | str | None = None,
    include_historical_sources: bool = False,
) -> dict[str, Any]:
    """Return an ok/reason proof gate for a Workspace-backed canonical record."""

    source = _validate_workspace_source(workspace_source, projection_path(root))
    state = lookup_object(root, source["connection_id"], source["capability"], source["object_id"])
    revision = lookup_revision(root, source)
    if not state or not revision:
        return {"ok": False, "reason": "workspace source proof missing"}
    if not revision.get("source_ledger_receipt") or not revision.get("canonical_path"):
        return {"ok": False, "reason": "workspace source proof missing"}
    if include_historical_sources:
        if revision.get("retained"):
            return {"ok": True, "state": state, "revision": revision}
        return {"ok": False, "reason": "workspace source proof missing"}
    if (
        state.get("available") is True
        and revision.get("available") is True
        and str(source["revision_id"]) == str(state.get("latest_revision_id") or "")
    ):
        return {"ok": True, "state": state, "revision": revision}
    return {"ok": False, "reason": "workspace source revision is superseded"}


def _object_state(revisions: list[dict[str, Any]]) -> dict[str, Any]:
    latest_event = revisions[-1]
    available_revisions = [item for item in revisions if item["available"]]
    latest_available = available_revisions[-1] if available_revisions else None
    tombstones = [item for item in revisions if item["availability_state"] in _TOMBSTONE_STATES]
    unavailable = [item for item in revisions if not item["available"]]
    current_available = latest_event["available"]
    latest = latest_event if current_available else latest_available
    if latest is None:
        latest = latest_event
    evidence_paths = {
        "latest": str(latest.get("canonical_path") or ""),
    }
    if tombstones:
        evidence_paths["tombstone"] = str(tombstones[-1].get("canonical_path") or "")
    if unavailable:
        evidence_paths["unavailable"] = str(unavailable[-1].get("canonical_path") or "")
    return {
        "connection_id": latest_event["connection_id"],
        "capability": latest_event["capability"],
        "object_id": latest_event["object_id"],
        "latest_revision_id": str(latest.get("revision_id") or ""),
        "available": bool(current_available),
        "availability_state": str(latest_event.get("availability_state") or ""),
        "tombstone_revision_id": str(tombstones[-1]["revision_id"]) if tombstones else "",
        "unavailable_revision_id": str(unavailable[-1]["revision_id"]) if unavailable else "",
        "content_digest": str(latest.get("content_digest") or ""),
        "canonical_evidence_paths": evidence_paths,
        "revisions": {str(item["revision_id"]): item for item in revisions},
    }


def _revision_entry(
    source: Mapping[str, str],
    record: Mapping[str, Any],
    path: Path,
    root_base: Path,
) -> dict[str, Any]:
    state = source["availability_state"].strip().lower()
    return {
        "connection_id": source["connection_id"],
        "capability": source["capability"],
        "object_id": source["object_id"],
        "revision_id": source["revision_id"],
        "available": state in _AVAILABLE_STATES,
        "availability_state": state,
        "content_digest": source["content_digest"],
        "source_ledger_receipt": source["source_ledger_receipt"],
        "observed_at": source["observed_at"],
        "canonical_path": path.relative_to(root_base).as_posix(),
        "memory_id": str(record.get("id") or ""),
        "retained": True,
    }


def _canonical_json_paths(memory_root: Path) -> list[Path]:
    if not memory_root.exists():
        return []
    paths: list[Path] = []
    for path in memory_root.rglob("*.json"):
        rel = path.relative_to(memory_root)
        if rel.parts and rel.parts[0] in {
            "dme",
            "indexes",
            "projection",
            "recall_verifier",
            "source_ledger",
            "tmp_writes",
        }:
            continue
        paths.append(path)
    return sorted(paths, key=lambda p: p.relative_to(memory_root).as_posix())


def _read_record(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SourceValidityError(f"canonical memory file unreadable: {path}") from exc


def _validate_workspace_source(raw: Mapping[str, Any], path: Path) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        raise SourceValidityError(f"workspace_source must be an object in {path}")
    missing = [field for field in _WORKSPACE_FIELDS if not str(raw.get(field) or "").strip()]
    if missing:
        raise SourceValidityError(f"workspace_source missing fields {missing} in {path}")
    source = {field: str(raw[field]).strip() for field in _WORKSPACE_FIELDS}
    digest = source["content_digest"]
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise SourceValidityError(f"workspace_source content_digest invalid in {path}")
    return source


def _validate_workspace_source_identity(raw: Mapping[str, Any], path: Path) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        raise SourceValidityError(f"workspace_source must be an object in {path}")
    fields = ("connection_id", "capability", "object_id", "revision_id")
    missing = [field for field in fields if not str(raw.get(field) or "").strip()]
    if missing:
        raise SourceValidityError(f"workspace_source missing fields {missing} in {path}")
    return {field: str(raw[field]).strip() for field in fields}


def _revision_sort_key(item: Mapping[str, Any]) -> tuple[str, tuple[Any, ...], str]:
    return (
        str(item.get("observed_at") or ""),
        _natural_key(str(item.get("revision_id") or "")),
        str(item.get("canonical_path") or ""),
    )


def _natural_key(value: str) -> tuple[Any, ...]:
    parts: list[Any] = []
    for part in re.split(r"(\d+)", value):
        if part.isdigit():
            parts.append(int(part))
        elif part:
            parts.append(part)
    return tuple(parts)


def _object_key(connection_id: str, capability: str, object_id: str) -> str:
    return json.dumps([connection_id, capability, object_id], separators=(",", ":"))


def _memory_root(root: Path | str | None) -> Path:
    if root is not None:
        root_path = Path(root)
        return root_path if root_path.name == "memory" else root_path / "memory"
    return OMEGA_ROOT / "memory"


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(raw, encoding="utf-8")
    os.replace(tmp_path, path)
