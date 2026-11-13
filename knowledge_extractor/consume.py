"""Read KnowledgeArtifact rows from the JSONL buffer (DeepSleep consolidate)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

_LOG = logging.getLogger(__name__)


def _resolve_path(rel_path: str) -> Path:
    path = Path(rel_path)
    if path.is_absolute():
        return path
    repo_root = Path(__file__).resolve().parent.parent
    return repo_root / path


def read_artifact_by_id(
    artifact_id: str,
    *,
    store_path: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Return the last JSONL row matching ``artifact_id``, or None."""
    if not artifact_id or not str(artifact_id).strip():
        return None
    target = str(artifact_id).strip()
    if store_path is None:
        from pressure_loop.policy import knowledge_artifact_store_path

        store_path = knowledge_artifact_store_path()
    path = _resolve_path(store_path)
    if not path.is_file():
        return None
    found: Optional[dict[str, Any]] = None
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                if str(row.get("artifact_id") or "") == target:
                    found = row
    except OSError as exc:
        _LOG.debug("knowledge consume read failed: %s", exc)
        return None
    return found


def knowledge_artifact_for_episode(auto_outcome: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Resolve artifact dict from AUTO outputs when an id was recorded."""
    outputs = auto_outcome.get("outputs")
    if not isinstance(outputs, dict):
        return None
    artifact_id = outputs.get("knowledge_artifact_id")
    if not isinstance(artifact_id, str) or not artifact_id.strip():
        return None
    from pressure_loop.policy import deepsleep_knowledge_artifact_store_path

    return read_artifact_by_id(
        artifact_id,
        store_path=deepsleep_knowledge_artifact_store_path(),
    )
