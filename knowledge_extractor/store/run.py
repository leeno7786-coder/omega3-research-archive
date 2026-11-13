"""Store phase — append KnowledgeArtifact to JSONL buffer (failure-soft)."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path

from knowledge_extractor._types import KnowledgeArtifact
from pressure_loop import policy as _policy

_LOG = logging.getLogger(__name__)


def _artifact_line(artifact: KnowledgeArtifact) -> str:
    payload = asdict(artifact)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def run(artifact: KnowledgeArtifact) -> str:
    """Append artifact as one JSON line; return artifact_id (never raises)."""
    artifact_id = artifact.artifact_id or ""
    try:
        rel_path = _policy.knowledge_artifact_store_path()
        path = Path(rel_path)
        if not path.is_absolute():
            repo_root = Path(__file__).resolve().parent.parent.parent
            path = repo_root / path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(_artifact_line(artifact) + "\n")
    except Exception as exc:  # noqa: BLE001 — failure-soft by design
        _LOG.debug("knowledge_extractor store failed: %s", exc)
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "knowledge",
                "store_failed",
                status="fail",
                ok=False,
                summary=str(exc),
                artifact_id=artifact_id,
                error=str(exc),
            )
        except Exception:
            pass
    return artifact_id
