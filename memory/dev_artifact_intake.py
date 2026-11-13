"""Continuous builder-artifact intake into the current episode's SRAM."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Iterable

from core.paths import OMEGA_ROOT
from memory import episode_sram
from memory.api import CanonicalEnvelope

_MAX_ARTIFACT_BYTES = 1 * 1024 * 1024


def _artifact_identity(scope: str, workspace_root: str, relative_path: str) -> tuple[str, str]:
    """Keep repo identity portable and external identity independent of grant nesting."""
    if scope == "repo":
        return scope, relative_path
    if scope != "operator_workspace":
        raise ValueError(f"unsupported dev artifact scope: {scope!r}")
    if not workspace_root or not Path(workspace_root).is_absolute():
        raise ValueError("dev artifact snapshot requires an absolute workspace_root")
    # These are physical paths resolved at capture, not a fresh filesystem
    # lookup. Changing grant representation cannot split one file's history.
    return scope, os.path.normcase(str(Path(workspace_root) / relative_path))


def _resolve_artifact(raw_path: Any) -> tuple[Path, str, str, str]:
    text = str(raw_path or "").strip()
    if not text:
        raise ValueError("builder ledger supplied an empty artifact path")
    from core.hud_path_guard import (
        _operator_workspace_root,
        _operator_workspace_roots,
        resolve_dev_edit_path,
    )

    workspace_root = _operator_workspace_root()
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = (workspace_root or OMEGA_ROOT) / candidate
    resolved_candidate = candidate.resolve(strict=False)
    # The compatibility accessor is also the selected trusted root in the
    # running process.  Checking it directly keeps the intake testable and
    # prevents a stale private-root cache from rejecting a valid grant.
    if workspace_root is not None and _within(resolved_candidate, workspace_root):
        target, scope = resolved_candidate, "operator_workspace"
    else:
        target, scope = resolve_dev_edit_path(str(resolved_candidate))
    # ``resolve_dev_edit_path`` calls an authorized external root a
    # ``trusted_folder``.  The artifact schema predates that name and exposes
    # the stable public scope ``operator_workspace``; select the actual root
    # by containment instead of assuming every non-repo path belongs to
    # OMEGA_ROOT.  The single-root accessor remains a test/runtime compatibility
    # path when a harness patches it directly.
    candidate_roots = list(_operator_workspace_roots())
    if workspace_root is not None:
        candidate_roots.append(workspace_root)
    candidate_roots = [Path(root).resolve(strict=False) for root in candidate_roots]
    root = OMEGA_ROOT.resolve(strict=False)
    public_scope = scope
    if scope != "repo":
        root = next((item for item in candidate_roots if _within(target, item)), None)
        if root is None:
            raise RuntimeError("operator workspace disappeared while capturing builder artifact")
        public_scope = "operator_workspace"
    relative_path = target.relative_to(root).as_posix()
    # External grants need their exact resolved root in memory. Repo artifacts
    # remain portable: their scope always resolves from the current OMEGA_ROOT.
    workspace_identity = str(root) if public_scope == "operator_workspace" else ""
    return target, public_scope, workspace_identity, relative_path


def _within(target: Path, root: Path) -> bool:
    try:
        target.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except (OSError, ValueError):
        return False


def hold_dev_artifact_snapshots(
    *,
    episode_id: str,
    objective: str,
    paths: Iterable[Any],
    job_id: str = "",
) -> tuple[str, ...]:
    """Snapshot every touched file into SRAM immediately when its ledger event arrives.

    ``job_id`` names the Coding Agent run that touched the files; its exact run identity is
    stamped on each snapshot so run-scoped recall can select it. An unreadable job raises.
    """
    ep_id = str(episode_id or "").strip()
    if not ep_id:
        raise ValueError("builder artifact episode_id is required")
    if not episode_sram.is_open(ep_id):
        raise RuntimeError("builder artifact episode_id does not match an open SRAM bucket")
    run_provenance = None
    if str(job_id or "").strip():
        from memory.run_anchor import run_provenance_for_job

        run_provenance = run_provenance_for_job(str(job_id).strip())
    references: list[str] = []
    for raw_path in paths:
        target, scope, workspace_root, relative_path = _resolve_artifact(raw_path)
        deleted = not target.is_file()
        if deleted:
            content = ""
            digest = hashlib.sha256(b"").hexdigest()
        else:
            size = target.stat().st_size
            if size > _MAX_ARTIFACT_BYTES:
                raise ValueError(
                    f"builder artifact exceeds {_MAX_ARTIFACT_BYTES} bytes: {relative_path}"
                )
            raw = target.read_bytes()
            content = raw.decode("utf-8", errors="replace")
            digest = hashlib.sha256(raw).hexdigest()
        latest_snapshot = None
        previous_snapshot_event_id = ""
        identity = _artifact_identity(scope, workspace_root, relative_path)
        for item in reversed(episode_sram.pending(ep_id)):
            if (
                item.kind != "memory"
                or getattr(item.envelope, "type", None) != "dev_artifact_snapshot"
            ):
                continue
            payload = dict(getattr(item.envelope, "payload", None) or {})
            if _artifact_identity(
                str(payload.get("scope") or ""),
                str(payload.get("workspace_root") or ""),
                str(payload.get("relative_path") or ""),
            ) == identity:
                latest_snapshot = payload
                previous_snapshot_event_id = item.event_id
                break
        # Repeated observations of the current state are idempotent; returning
        # to older bytes or changing existence is a new artifact transition.
        if (
            latest_snapshot is not None
            and latest_snapshot.get("content_sha256") == digest
            and latest_snapshot.get("deleted") == deleted
        ):
            continue
        artifact_metadata = {
            "episode_id": ep_id,
            "objective": str(objective or "").strip()[:4000],
            "scope": scope,
            "workspace_root": workspace_root,
            "relative_path": relative_path,
            "content_sha256": digest,
            "deleted": deleted,
            # The intake journal deduplicates identical envelopes. Chain each
            # transition to the previous event so A -> B -> A remains three
            # facts even though the first and final file bytes are equal.
            "previous_snapshot_event_id": previous_snapshot_event_id,
        }
        if run_provenance is not None:
            artifact_metadata["run_provenance"] = dict(run_provenance)
        references.append(
            episode_sram.hold_memory(
                CanonicalEnvelope(
                    cycle_id=ep_id,
                    coord=[11, 0, 0],
                    type="dev_artifact_snapshot",
                    payload={
                        **artifact_metadata,
                        "content": content,
                        "title": f"Builder artifact: {relative_path}",
                        "summary": (
                            f"Builder {'deleted' if deleted else 'captured'} {relative_path}"
                        ),
                        "text": content,
                        "tags": ["dev", "builder_artifact", scope],
                        "subtopic": "dev_artifact_snapshot",
                        "enrichment": artifact_metadata,
                    },
                    source="memory.dev_artifact_intake",
                    confidence=1.0,
                    provenance=["builder_ledger", "episode_sram"],
                    decay_policy="none",
                    permissions={"read": True, "write": True},
                    strength_index=1.0,
                ),
                propose=False,
                bypass_guardian=True,
                skip_embed=True,
                episode_id=ep_id,
            )
        )
    return tuple(references)
