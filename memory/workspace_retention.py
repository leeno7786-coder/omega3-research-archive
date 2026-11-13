"""Workspace retention commands applied only during DeepSleep."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping

from core.symphony_time import now_iso
from memory.api import CanonicalEnvelope
from pressure_loop.contributors import make_contributor

RETENTION_MODES = frozenset({"purge_unavailable", "retain_historical"})
_PROCESSED_AUDIT = "workspace_retention_processed"


class WorkspaceRetentionError(RuntimeError):
    """A retention command is malformed or could not be applied."""


def normalize_retention_mode(mode: object) -> str:
    value = str(mode or "").strip()
    if value == "purge":
        value = "purge_unavailable"
    if value not in RETENTION_MODES:
        raise WorkspaceRetentionError(f"unknown Workspace retention mode: {value!r}")
    return value


def workspace_source_is_current(record: Mapping[str, Any]) -> bool:
    source = record.get("workspace_source")
    if not isinstance(source, Mapping):
        return True
    state = str(source.get("availability_state") or "available").strip().lower()
    return state in {"", "available"}


def build_disconnect_retention_contributor(
    contract: Mapping[str, Any],
    *,
    bundle_id: str,
    disconnected_at: str | None = None,
) -> Any:
    """Build the metadata-only queue item that DeepSleep later applies."""
    command = _disconnect_command(contract, bundle_id=bundle_id, disconnected_at=disconnected_at)
    cycle_id = f"workspace-retention-disconnect-{command['connection_id']}"
    command_id = _command_id(command["connection_id"], "disconnect")
    text = json.dumps(command, ensure_ascii=False, sort_keys=True)
    envelope = CanonicalEnvelope(
        cycle_id=cycle_id,
        coord=[9, 1, 0],
        type="workspace_retention_command",
        payload={
            "title": "Workspace disconnect retention",
            "summary": f"Workspace disconnect retention for {command['connection_id']}",
            "text": text,
            "subtopic": "workspace_retention",
            "tags": ["workspace", "retention", "disconnect", "metadata_only"],
            "enrichment": {"workspace_retention_command": dict(command)},
        },
        source="workspace_retention",
        confidence=1.0,
        id=command_id,
        timestamp_created=str(command["disconnected_at"]),
    )
    return make_contributor(
        "external",
        "sensory_observation",
        1.0,
        {
            "cycle_id": cycle_id,
            "message": f"workspace retention disconnect {command['connection_id']}",
            "ingress_class": "workspace_retention",
            "directive_intent": "LEARN",
            "memory_envelopes": [asdict(envelope)],
            "workspace_retention": dict(command),
        },
    )


def process_retention_commands(*, root: Path | str | None = None) -> dict[str, Any]:
    memory_root = _memory_root(root)
    result = {
        "commands": 0,
        "purged": 0,
        "retained": 0,
        "made_unavailable": 0,
        "skipped": [],
        "processed": [],
    }
    if not memory_root.exists():
        return result

    records = _load_records(memory_root)
    commands = [
        row
        for row in records
        if _is_retention_command(row["record"]) and not _already_processed(row["record"])
    ]
    for command_row in commands:
        command = _parse_command(command_row["record"])
        if command["kind"] != "workspace_disconnect":
            raise WorkspaceRetentionError(f"unsupported retention command kind: {command['kind']}")
        applied = _apply_disconnect_command(command_row, command, records)
        result["commands"] += 1
        result["purged"] += applied["purged"]
        result["retained"] += applied["retained"]
        result["made_unavailable"] += applied["made_unavailable"]
        result["processed"].append(applied)
    return result


def _disconnect_command(
    contract: Mapping[str, Any],
    *,
    bundle_id: str,
    disconnected_at: str | None,
) -> dict[str, str]:
    connection_id = _required_string(contract, "connection_id")
    retention = contract.get("retention")
    if not isinstance(retention, Mapping):
        raise WorkspaceRetentionError("connection contract retention must be an object")
    return {
        "kind": "workspace_disconnect",
        "connection_id": connection_id,
        "connection_revision": _required_string(contract, "revision"),
        "mode": normalize_retention_mode(retention.get("disconnect")),
        "provider": "google",
        "bundle_id": str(bundle_id or ""),
        "provider_subject": _required_string(contract, "provider_subject"),
        "provider_email": str(contract.get("provider_email") or ""),
        "disconnected_at": disconnected_at or now_iso(),
    }


def _required_string(mapping: Mapping[str, Any], field: str) -> str:
    value = mapping.get(field)
    if not isinstance(value, str) or not value.strip():
        raise WorkspaceRetentionError(f"connection contract {field} is required")
    return value


def _command_id(connection_id: str, kind: str) -> str:
    digest = hashlib.sha256(f"{kind}|{connection_id}".encode("utf-8")).hexdigest()[:32]
    return f"ws-retention-{digest}"


def _memory_root(root: Path | str | None) -> Path:
    if root is None:
        from memory.engine import validate_write

        return Path(validate_write.MEMORY_ROOT)
    candidate = Path(root)
    return candidate if candidate.name == "memory" else candidate / "memory"


def _load_records(memory_root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(memory_root.rglob("*.json")):
        relative = path.relative_to(memory_root)
        if relative.parts and relative.parts[0] in {"indexes", "projection", "tmp_writes"}:
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise WorkspaceRetentionError(f"unreadable memory record: {path}") from exc
        if isinstance(record, dict):
            rows.append({"path": path, "record": record})
    return rows


def _is_retention_command(record: Mapping[str, Any]) -> bool:
    if str(record.get("type") or "") == "workspace_retention_command":
        return True
    enrichment = record.get("enrichment")
    return isinstance(enrichment, Mapping) and isinstance(
        enrichment.get("workspace_retention_command"), Mapping
    )


def _already_processed(record: Mapping[str, Any]) -> bool:
    for entry in record.get("audit") or []:
        if isinstance(entry, Mapping) and entry.get("what") == _PROCESSED_AUDIT:
            return True
    return False


def _parse_command(record: Mapping[str, Any]) -> dict[str, str]:
    enrichment = record.get("enrichment")
    raw: Any = None
    if isinstance(enrichment, Mapping):
        raw = enrichment.get("workspace_retention_command")
    if raw is None:
        try:
            raw = json.loads(str(record.get("text") or "{}"))
        except json.JSONDecodeError as exc:
            raise WorkspaceRetentionError("retention command text is not JSON") from exc
    if not isinstance(raw, Mapping):
        raise WorkspaceRetentionError("retention command must be an object")
    kind = str(raw.get("kind") or "")
    connection_id = str(raw.get("connection_id") or "").strip()
    if not kind:
        raise WorkspaceRetentionError("retention command kind is required")
    if not connection_id:
        raise WorkspaceRetentionError("retention command connection_id is required")
    return {
        "kind": kind,
        "connection_id": connection_id,
        "mode": normalize_retention_mode(raw.get("mode")),
        "disconnected_at": str(raw.get("disconnected_at") or now_iso()),
    }


def _apply_disconnect_command(
    command_row: Mapping[str, Any],
    command: Mapping[str, str],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    command_path = Path(command_row["path"])
    if not command_path.is_file():
        raise WorkspaceRetentionError("retention tombstone is missing before purge")
    connection_id = command["connection_id"]
    targets = [
        row
        for row in rows
        if row["path"] != command_path
        and _workspace_source_connection(row["record"]) == connection_id
    ]
    unavailable = [_mark_unavailable(row, command) for row in targets]
    mode = command["mode"]
    purged = 0
    retained = 0
    if mode == "purge_unavailable":
        for row in unavailable:
            Path(row["path"]).unlink()
            purged += 1
    else:
        for row in unavailable:
            _write_json_atomic(Path(row["path"]), row["record"])
            retained += 1
    applied = {
        "command_id": str(command_row["record"].get("id") or ""),
        "connection_id": connection_id,
        "mode": mode,
        "made_unavailable": len(unavailable),
        "purged": purged,
        "retained": retained,
    }
    _mark_command_processed(command_path, command_row["record"], applied)
    return applied


def _workspace_source_connection(record: Mapping[str, Any]) -> str | None:
    source = record.get("workspace_source")
    if not isinstance(source, Mapping):
        return None
    value = str(source.get("connection_id") or "").strip()
    return value or None


def _mark_unavailable(
    row: Mapping[str, Any],
    command: Mapping[str, str],
) -> dict[str, Any]:
    record = copy.deepcopy(row["record"])
    source = dict(record["workspace_source"])
    if source.get("availability_state") != "unavailable":
        source["availability_state"] = "unavailable"
        record["workspace_source"] = source
    audit = list(record.get("audit") or [])
    audit.append(
        {
            "when": command["disconnected_at"],
            "what": "workspace_source_unavailable",
            "by": "memory.workspace_retention",
            "notes": f"connection disconnect {command['connection_id']}",
        }
    )
    record["audit"] = audit
    return {"path": row["path"], "record": record}


def _mark_command_processed(path: Path, record: Mapping[str, Any], applied: Mapping[str, Any]) -> None:
    updated = copy.deepcopy(dict(record))
    enrichment = dict(updated.get("enrichment") or {})
    enrichment["workspace_retention_result"] = dict(applied)
    updated["enrichment"] = enrichment
    audit = list(updated.get("audit") or [])
    audit.append(
        {
            "when": now_iso(),
            "what": _PROCESSED_AUDIT,
            "by": "memory.workspace_retention",
            "notes": json.dumps(dict(applied), sort_keys=True),
        }
    )
    updated["audit"] = audit
    _write_json_atomic(path, updated)


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


__all__ = [
    "WorkspaceRetentionError",
    "build_disconnect_retention_contributor",
    "normalize_retention_mode",
    "process_retention_commands",
    "workspace_source_is_current",
]
