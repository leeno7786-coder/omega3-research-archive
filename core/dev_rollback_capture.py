"""Record an autonomous build's touched files into the dev-lobe rollback sandbox.

These artifacts are the SAME records the HUD rollback sandbox reads
(``GET /api/dev/snapshots``) and rolls back (``POST /api/dev/rollback``), stored under
``memory/1100_dev/artifacts/*.json``. The interactive dev pill already records its edits
there; this module lets the AUTONOMOUS fold-back surface a build's files in the same
operator rollback area, so the existing lifecycle applies for free:

* the operator sees each file in Settings -> terminal -> rollback sandbox;
* rolling one back restores it (or deletes it, if the build created it) AND enqueues a
  system event the NEXT episode drains (``pressure_loop.system_event_queue``) -> Omega
  learns its work was undone;
* on server shutdown the session rollback list resets while the files stay in the workspace
  (``hud.server._clear_dev_rollback_snapshots``).

Before/after content comes from the permanent per-project working store
(:mod:`memory.dev_project_store`): a re-edited file rolls back to the version the store held
before this fold-back; a newly created file has no prior version and rolls back to a delete.
Everything here is failure-soft: a capture hiccup must never break the cognition cascade.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from core.paths import OMEGA_ROOT, dev_artifacts_root

log = logging.getLogger("omega.core.dev_rollback_capture")

# Mirror hud.server._DEV_SNAPSHOT_MAX_BYTES so a captured content field never trips the
# listing endpoint's per-record read cost / disk bound.
_SNAPSHOT_MAX_BYTES = 1 * 1024 * 1024


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sanitize_id_fragment(value: str) -> str:
    frag = "".join(c if c.isalnum() or c in "._-" else "_" for c in str(value or "").strip())
    return frag[:96] or "x"


def _cap(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    if len(text) > _SNAPSHOT_MAX_BYTES:
        return None  # too large for a snapshot record; drop rather than truncate a rollback
    return text


def clear_workspace_rollback_records(
    *,
    workspace_root: Optional[Path] = None,
    artifacts_dir: Optional[Path] = None,
) -> int:
    """Retire every WORKSPACE-scoped rollback record (operator 2026-07-03).

    Runs after a finished build is ported to the operator's completed directory and the
    workspace cleared: any rollback row targeting workspace files is stale — restoring
    one would resurrect debris into the fresh workspace (or delete files already gone).
    Repo-scoped dev-pill rows survive: their targets still exist. A record is workspace-
    scoped when its ``scope`` says so OR its file_path resolves under the workspace root.
    Failure-soft per record."""
    base = Path(artifacts_dir) if artifacts_dir is not None else dev_artifacts_root(OMEGA_ROOT)
    if not base.is_dir():
        return 0
    ws: Optional[Path] = None
    try:
        if workspace_root is not None:
            ws = Path(workspace_root).resolve()
        else:
            from core.hud_path_guard import _operator_workspace_root

            root = _operator_workspace_root()
            ws = Path(root).resolve() if root else None
    except Exception:
        ws = None

    cleared = 0
    for record_path in base.glob("*.json"):
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
            scope = str(record.get("scope") or "")
            in_workspace = scope == "operator_workspace"
            # An explicit non-workspace scope (e.g. "repo") is authoritative — the
            # path-containment fallback applies only to records with NO scope.
            if not in_workspace and not scope and ws is not None:
                file_path = str(record.get("file_path") or "")
                if file_path:
                    resolved = Path(file_path)
                    if not resolved.is_absolute():
                        resolved = ws / file_path
                    resolved = resolved.resolve()
                    in_workspace = resolved == ws or ws in resolved.parents
            if in_workspace:
                record_path.unlink()
                cleared += 1
        except Exception as exc:
            log.warning("rollback record retirement failed for %s: %s", record_path, exc)
    if cleared:
        log.info("retired %d stale workspace rollback record(s)", cleared)
    return cleared


def record_file_snapshots(
    changes: Iterable[Any],
    *,
    objective: str = "",
    session_id: str = "",
    artifacts_dir: Optional[Path] = None,
) -> list[dict[str, Any]]:
    """Write a rollback snapshot artifact per :class:`memory.dev_project_store.FileChange`.

    Deterministic ids keyed on (session, path) mean the same build re-yielding across
    fold-backs refreshes its own record instead of piling up duplicates. Returns the records
    written (empty on no changes / any soft failure).
    """
    changes = [c for c in (changes or ())]
    if not changes:
        return []

    base = Path(artifacts_dir) if artifacts_dir is not None else dev_artifacts_root(OMEGA_ROOT)
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("dev rollback capture: artifacts dir unavailable: %s", exc)
        return []

    obj = str(objective or "").strip()
    sid = str(session_id or "")
    written: list[dict[str, Any]] = []
    for ch in changes:
        file_path = str(getattr(ch, "file_path", "") or "")
        if not file_path:
            continue
        created = bool(getattr(ch, "created", True))
        before = None if created else _cap(getattr(ch, "before_content", None))
        after = _cap(getattr(ch, "after_content", None))
        # An edit we somehow have NO before-content for cannot be safely restored — recording
        # it as existed_before=False would DELETE the operator's file on rollback. Skip it.
        if not created and before is None:
            continue
        verb = "created" if created else "edited"
        summary = f"autonomous build ({verb}): {obj[:70]}" if obj else f"autonomous build ({verb})"
        normalized_path = file_path.replace("\\", "/").casefold()
        path_digest = hashlib.sha256(normalized_path.encode("utf-8")).hexdigest()[:16]
        path_hint = _sanitize_id_fragment(file_path)[:72]
        snap_id = (
            f"autobuild-{_sanitize_id_fragment(sid) if sid else 'x'}-{path_hint}-{path_digest}"
        )
        record = {
            "id": snap_id,
            "timestamp": _now_iso(),
            "file_path": file_path,
            "before_content": before,
            "after_content": after,
            "existed_before": (not created),  # -> HUD action: restore (edit) vs delete (created)
            "summary": summary,
            "tool": "write_file" if created else "edit_file",
            "session_id": sid,
            "scope": str(getattr(ch, "scope", "") or "operator_workspace"),
            "lifecycle": "session",
            # project + relpath (dev-memory Change 4a): let POST /api/dev/rollback locate the
            # lobe-11 store file (dev_projects_root/project/relpath) to re-mirror on rollback.
            "project": str(getattr(ch, "project", "") or ""),
            "relpath": str(getattr(ch, "relpath", "") or ""),
        }
        try:
            (base / f"{snap_id}.json").write_text(
                json.dumps(record, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            written.append(record)
        except OSError as exc:
            log.warning("dev rollback capture: write failed for %s: %s", file_path, exc)

    if written:
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "memory",
                "dev_rollback_capture",
                status="ok",
                summary=f"captured {len(written)} build file(s) into the rollback sandbox",
                count=len(written),
                session_id=sid,
            )
        except Exception:
            pass
    return written
