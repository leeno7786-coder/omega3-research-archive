"""Restore builder artifacts from verified dev-lobe snapshots.

The canonical ``dev_artifact_snapshot`` records are the sole content and
destination authority. Every external artifact carries the exact workspace
root from intake; restore also revalidates the current grant and job boundary.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, List, Mapping, Optional

from core.paths import OMEGA_ROOT
from memory.dev_artifact_intake import _artifact_identity

log = logging.getLogger("omega.memory.dev_project_store")

_MAX_FILE_BYTES = 1 * 1024 * 1024  # match the rollback-snapshot content cap


def _safe_relative_path(raw: Any) -> str:
    value = str(raw or "").strip()
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or "\\" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError(f"invalid dev artifact relative path: {value!r}")
    return path.as_posix()


def _snapshot_location(metadata: Mapping[str, Any]) -> tuple[str, str, str]:
    """Return the recorded scope, exact external root, and path without inference."""
    scope = str(metadata.get("scope") or "")
    if scope not in {"repo", "operator_workspace"}:
        raise ValueError(f"dev artifact snapshot has an unsupported scope: {scope!r}")
    workspace_root = ""
    if scope == "operator_workspace":
        raw = metadata.get("workspace_root")
        if not isinstance(raw, str) or not raw or not Path(raw).is_absolute():
            raise ValueError("dev artifact snapshot requires an absolute workspace_root")
        # Intake records physical identity, so a later junction/symlink must
        # never redirect history to a different, even separately granted root.
        root = Path(raw)
        resolved = root.resolve(strict=False)
        if resolved != root:
            raise ValueError("dev artifact snapshot workspace_root changed physical identity")
        workspace_root = str(resolved)
    return scope, workspace_root, _safe_relative_path(metadata.get("relative_path"))


def _snapshot_identity(metadata: Mapping[str, Any]) -> tuple[str, str]:
    return _artifact_identity(*_snapshot_location(metadata))


def _canonical_snapshot_records(
    project: str, *, exclude_episode: str = ""
) -> dict[tuple[str, str], dict[str, Any]]:
    """Return each path's latest verified canonical snapshot for one project."""
    from memory.engine import walker
    from memory.recall_verifier import verify_leaf

    walker.load_coord_index()
    index = walker.coord_index_snapshot()
    candidates: dict[tuple[str, str], list[tuple[int, str, Path, bytes, dict[str, Any]]]] = {}
    for memory_id, entry in index.items():
        if not isinstance(entry, dict):
            continue
        if str(entry.get("type") or "") != "dev_artifact_snapshot":
            continue
        coord = entry.get("coord")
        if not isinstance(coord, list) or coord[:2] != [11, 0]:
            raise ValueError(f"indexed dev artifact snapshot {memory_id} has an invalid coordinate")
        path = walker.resolve_coord_index_path(entry.get("path"))
        if path is None:
            raise ValueError(f"indexed dev artifact snapshot {memory_id} has no resolvable canonical path")
        try:
            before = path.read_bytes()
            record = json.loads(before.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"indexed dev artifact snapshot {memory_id} is unreadable") from exc
        if not isinstance(record, dict) or str(record.get("id") or "") != str(memory_id):
            raise ValueError(f"indexed dev artifact snapshot {memory_id} has an identity mismatch")
        enrichment = record.get("enrichment")
        if not isinstance(enrichment, dict):
            raise ValueError(f"indexed dev artifact snapshot {memory_id} is missing canonical metadata")
        if derive_project_name((), str(enrichment.get("objective") or "")) != project:
            continue
        episode_id = str(enrichment.get("episode_id") or "")
        if exclude_episode and episode_id == exclude_episode:
            continue
        identity = _snapshot_identity(enrichment)
        provenance = record.get("provenance")
        ordinal = provenance.get("write_ordinal") if isinstance(provenance, dict) else None
        if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 1:
            raise ValueError(f"dev artifact snapshot {memory_id} has no canonical write ordinal")
        candidates.setdefault(identity, []).append(
            (ordinal, str(memory_id), path, before, record)
        )

    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for key, rows in candidates.items():
        ordinal, memory_id, path, before, record = max(rows, key=lambda row: (row[0], row[1]))
        verdict = verify_leaf(memory_id)
        if verdict.status != "verified":
            raise ValueError(
                f"latest dev artifact snapshot {memory_id} is not verified: {verdict.reason}"
            )
        try:
            if path.read_bytes() != before:
                raise ValueError(
                    f"canonical dev artifact snapshot {memory_id} changed during verification"
                )
        except OSError as exc:
            raise ValueError(f"canonical dev artifact snapshot {memory_id} became unreadable") from exc
        verified = dict((verdict.answer_payload or {}).get("candidates", [{}])[0])
        if str(verified.get("type") or "") != "dev_artifact_snapshot":
            raise ValueError(f"verified leaf {memory_id} is not a dev artifact snapshot")
        snapshot = dict(record)
        snapshot["_write_ordinal"] = ordinal
        snapshot["_text"] = str(verified.get("text") or "")
        latest[key] = snapshot
    return latest


def _pending_episode_snapshots(
    episode_id: str, project: str
) -> dict[tuple[str, str], dict[str, Any]]:
    """Return the last held snapshot for each project path in the open episode."""
    from memory import episode_sram

    snapshots: dict[tuple[str, str], dict[str, Any]] = {}
    for item in episode_sram.pending(episode_id):
        envelope = getattr(item, "envelope", None)
        if (
            getattr(item, "kind", "") != "memory"
            or getattr(envelope, "type", "") != "dev_artifact_snapshot"
        ):
            continue
        payload = dict(getattr(envelope, "payload", None) or {})
        if derive_project_name((), str(payload.get("objective") or "")) != project:
            continue
        identity = _snapshot_identity(payload)
        snapshots[identity] = {
            "enrichment": payload,
            "text": str(payload.get("content") or payload.get("text") or ""),
            "type": "dev_artifact_snapshot",
        }
    return snapshots


def _snapshot_text(snapshot: Mapping[str, Any]) -> str:
    enrichment = snapshot.get("enrichment")
    if not isinstance(enrichment, Mapping):
        raise ValueError("dev artifact snapshot is missing intake metadata")
    text = str(snapshot.get("_text", snapshot.get("text", "")))
    encoded = text.encode("utf-8")
    if len(encoded) > _MAX_FILE_BYTES:
        raise ValueError("dev artifact snapshot exceeds the restore size limit")
    expected = str(enrichment.get("content_sha256") or "")
    if not expected or hashlib.sha256(encoded).hexdigest() != expected:
        raise ValueError("dev artifact snapshot content hash does not match its intake digest")
    return text


def _default_destination(scope: str, workspace_root: str, relative_path: str) -> Path:
    if scope == "repo":
        root = OMEGA_ROOT.resolve(strict=False)
    elif scope == "operator_workspace":
        root = Path(workspace_root)
    else:
        raise ValueError(f"unsupported dev artifact scope: {scope!r}")
    recorded_target = root / Path(relative_path)
    destination = recorded_target.resolve(strict=False)
    if destination != recorded_target:
        raise ValueError("dev artifact restore target changed physical identity")
    if not destination.is_relative_to(root):
        raise ValueError("dev artifact restore destination is outside its recorded workspace_root")
    from core.hud_path_guard import resolve_dev_edit_path

    checked, _scope = resolve_dev_edit_path(str(destination))
    if checked != destination:
        raise ValueError("dev artifact restore destination changed during jail validation")
    return destination


def restore_project_files(
    objective: str,
    *,
    destination_resolver: Any = None,
) -> list[tuple[str, str]]:
    """Restore latest verified canonical snapshots, validating every destination first.

    The return value is ``(relative_path, absolute_destination)`` pairs for files
    actually materialized or deleted. A destination resolver such as
    ``ExecutionContext.write_target`` can apply a narrower per-job project jail.
    """
    project = derive_project_name((), objective)
    snapshots = _canonical_snapshot_records(project)
    if not snapshots:
        return []
    plans: list[tuple[str, Path, bool, bytes | None]] = []
    for identity, snapshot in sorted(snapshots.items()):
        enrichment = snapshot.get("enrichment")
        if not isinstance(enrichment, Mapping):
            raise ValueError("verified dev artifact snapshot is missing metadata")
        if _snapshot_identity(enrichment) != identity:
            raise ValueError("verified dev artifact snapshot identity does not match its lookup key")
        scope, workspace_root, relative_path = _snapshot_location(enrichment)
        deleted = bool(enrichment.get("deleted"))
        text = _snapshot_text(snapshot)
        if deleted and text:
            raise ValueError("deleted dev artifact snapshot unexpectedly contains file content")
        content = None if deleted else text.encode("utf-8")
        destination = _default_destination(scope, workspace_root, relative_path)
        if destination_resolver is not None:
            checked = Path(destination_resolver(destination)).resolve(strict=False)
            if checked != destination:
                raise ValueError("dev artifact destination changed during job validation")
        plans.append((relative_path, destination, deleted, content))

    restored: list[tuple[str, str]] = []
    for relative_path, destination, deleted, content in plans:
        if deleted:
            if destination.exists():
                destination.unlink()
                restored.append((relative_path, str(destination)))
            continue
        assert content is not None
        if destination.is_file() and destination.read_bytes() == content:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        restored.append((relative_path, str(destination)))
    return restored


def project_artifact_paths(objective: str) -> list[str]:
    """List latest verified snapshot paths for directive context, without reading mirror data."""
    project = derive_project_name((), objective)
    snapshots = _canonical_snapshot_records(project)
    for snapshot in snapshots.values():
        _snapshot_text(snapshot)
    return sorted({
        _snapshot_location(snapshot["enrichment"])[2] for snapshot in snapshots.values()
    })


# Byte-compile / test byproducts are not source artifacts — never persist or offer them for
# rollback (a build produces __pycache__/*.pyc, .coverage, .pytest_cache alongside its real files).
_EXCLUDE_DIR_PARTS = {
    "__pycache__",
    ".pytest_cache",
    ".git",
    ".venv",
    ".venv-brain",
    "node_modules",
}
_EXCLUDE_SUFFIXES = {".pyc", ".pyo"}
_EXCLUDE_NAMES = {".coverage"}


def _is_build_junk(rel: str) -> bool:
    parts = [p for p in str(rel).split("/") if p]
    if any(p in _EXCLUDE_DIR_PARTS for p in parts):
        return True
    name = parts[-1] if parts else str(rel)
    if name in _EXCLUDE_NAMES:
        return True
    from pathlib import PurePosixPath

    return PurePosixPath(name).suffix in _EXCLUDE_SUFFIXES


_BINARY_SUFFIXES = frozenset({".exe", ".dll", ".pyd", ".so", ".whl"})


def _drop_vendored(targets: list[tuple[Path, str]]) -> list[tuple[Path, str]]:
    """Drop pip-vendored dependency files from a build's persist set.

    A pre-venv-law build that ran ``pip install --target .`` dumped entire site-packages
    trees into the workspace, and the store faithfully persisted all of it as "project
    files" (live 2026-07-10: 511 of 528 files in one record were colorama/pygments/pytest
    debris, with ``bin/*.exe`` stored as mangled text). The ``*.dist-info`` directory is
    the definitive pip marker AND names the package it belongs to — so when any is present,
    exclude the dist-info trees themselves, top-level dirs matching a dist package name
    (underscore-normalized, so ``_pytest`` matches ``pytest``), and ``bin``/``scripts``.
    Binary artifacts are always dropped: they cannot round-trip through the text store.
    A clean build (no dist-info anywhere) passes through untouched."""
    kept = [
        (t, rel) for t, rel in targets if PurePosixPath(rel).suffix.lower() not in _BINARY_SUFFIXES
    ]
    dist_pkgs: set[str] = set()
    for _t, rel in kept:
        for part in rel.split("/"):
            m = re.match(r"(?P<name>.+?)-\d[\w.]*\.dist-info$", part)
            if m:
                dist_pkgs.add(m.group("name").lower().replace("_", "-"))
    if not dist_pkgs:
        return kept

    def _vendored(rel: str) -> bool:
        parts = [p for p in rel.split("/") if p]
        if any(p.endswith(".dist-info") for p in parts):
            return True
        top = parts[0].lower()
        if top in {"bin", "scripts"}:
            return True
        return top.lstrip("_").replace("_", "-") in dist_pkgs

    dropped = [rel for _t, rel in kept if _vendored(rel)]
    if dropped:
        log.info(
            "dev project store: dropped %d pip-vendored file(s) from persist set (packages: %s)",
            len(dropped),
            ", ".join(sorted(dist_pkgs)),
        )
    return [(t, rel) for t, rel in kept if not _vendored(rel)]


def _resolve_build_path(raw: str) -> Optional[Path]:
    """Resolve a build's file path. The bridge reports WORKSPACE-relative paths (e.g.
    ``todo_app/main.py``); resolve those against the operator workspace root — where the build
    actually ran — NOT OMEGA_ROOT (the old bug pointed capture at ``<omega-root>/todo_app`` in the
    repo, which does not exist, so every after_content came back null). Absolute paths pass through.
    """
    p = str(raw or "").strip()
    if not p:
        return None
    cand = Path(p)
    if not cand.is_absolute():
        from core.hud_path_guard import _operator_workspace_root  # type: ignore

        ws = _operator_workspace_root()
        base = ws if ws is not None else OMEGA_ROOT
        cand = base / p
    return cand.resolve(strict=False)


@dataclass(frozen=True)
class FileChange:
    """One episode snapshot's before/after content for rollback capture."""

    file_path: str  # absolute workspace path
    project: str  # objective-derived project key
    relpath: str  # scope-relative canonical artifact path (posix)
    before_content: Optional[str]  # latest prior verified snapshot, or None if newly created
    after_content: Optional[str]  # current episode snapshot, or None when deleted
    created: bool  # True iff the prior canonical snapshot was absent or deleted


def sanitize_project(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", (name or "").strip()).strip("-. _")
    cleaned = re.sub(r"-{2,}", "-", cleaned).lower()
    return cleaned[:80] or "workspace"


def _scope_relpath(target: Path) -> str:
    """The file's path relative to its dev-edit scope root (repo / operator workspace)."""
    from core.hud_path_guard import resolve_dev_edit_path

    _resolved, _scope = resolve_dev_edit_path(str(target))
    from core.hud_path_guard import _operator_workspace_root  # type: ignore

    roots = []
    ws = _operator_workspace_root()
    if ws is not None:
        roots.append(ws.resolve(strict=False))
    roots.append(OMEGA_ROOT.resolve(strict=False))
    for root in roots:
        try:
            if target.resolve(strict=False).is_relative_to(root):
                return target.resolve(strict=False).relative_to(root).as_posix()
        except (ValueError, OSError):
            continue
    return target.name


def _shared_top_dir(relpaths: Iterable[str]) -> Optional[str]:
    """The single raw top-level directory every file shares, or None (bare files / mixed)."""
    tops: set[str] = set()
    has_bare = False
    for rp in relpaths:
        parts = [p for p in str(rp).split("/") if p]
        if len(parts) >= 2:  # has a containing directory
            tops.add(parts[0])
        else:
            has_bare = True
    if len(tops) == 1 and not has_bare:
        return next(iter(tops))
    return None


def derive_project_name(relpaths: Iterable[str], objective: str) -> str:
    """One project name for the whole build: the objective when present, else a shared top dir.

    Objective-first (operator 2026-07-10): naming by the cycle's shared top-level dir fractured
    multi-cycle builds — the fold-back cycle that only touched ``src/`` stored under project
    "src", the ``tests/`` cycle under "tests" — and each fragment was then verified against the
    FULL request (false reds like "request mentions tests but no test file exists" while the
    tests sat in another fragment). The objective is the one name stable across every cycle of
    the same build; the shared-dir name remains only for objective-less ad-hoc edits."""
    if str(objective or "").strip():
        return sanitize_project(objective)
    shared = _shared_top_dir(relpaths)
    return sanitize_project(shared) if shared is not None else sanitize_project(objective)


def persist_project_files(
    paths: Iterable[str],
    *,
    objective: str = "",
    episode_id: str = "",
    session_id: str = "",
) -> List[FileChange]:
    """Pair held SRAM snapshots with the latest prior verified canonical versions.

    This function reads no builder file content from the workspace and writes no
    project mirror. It exists to produce rollback before/after evidence from the
    intake snapshot and canonical history.
    """
    from memory.dev_artifact_intake import _resolve_artifact

    targets: list[tuple[Path, str, str, str]] = []
    for raw in paths or ():
        try:
            target, scope, workspace_root, rel = _resolve_artifact(raw)
        except (OSError, TypeError, ValueError, RuntimeError):
            continue
        if _is_build_junk(rel):
            continue  # pytest / byte-compile byproducts are not source artifacts
        targets.append((target, scope, workspace_root, rel))
    if not targets:
        return []

    filtered = _drop_vendored([(target, rel) for target, _scope, _workspace_root, rel in targets])
    allowed = {(str(target), rel) for target, rel in filtered}
    targets = [
        (target, scope, workspace_root, rel)
        for target, scope, workspace_root, rel in targets
        if (str(target), rel) in allowed
    ]
    if not targets:
        return []

    project = derive_project_name((rel for _target, _scope, _workspace_root, rel in targets), objective)
    current = _pending_episode_snapshots(str(episode_id or ""), project)
    previous = _canonical_snapshot_records(project, exclude_episode=str(episode_id or ""))
    changes: List[FileChange] = []
    for target, scope, workspace_root, rel in targets:
        key = _artifact_identity(scope, workspace_root, _safe_relative_path(rel))
        after_snapshot = current.get(key)
        if after_snapshot is None:
            continue  # only SRAM intake can supply the current bytes
        after_enrichment = after_snapshot["enrichment"]
        after_text = _snapshot_text(after_snapshot)
        after_deleted = bool(after_enrichment.get("deleted"))
        if after_deleted and after_text:
            raise ValueError("deleted dev artifact snapshot unexpectedly contains file content")
        after = None if after_deleted else after_text
        before_snapshot = previous.get(key)
        before_enrichment = (
            before_snapshot.get("enrichment") if isinstance(before_snapshot, Mapping) else None
        )
        before_deleted = bool(
            before_enrichment.get("deleted") if isinstance(before_enrichment, Mapping) else False
        )
        before_text = _snapshot_text(before_snapshot) if before_snapshot is not None else None
        if before_deleted and before_text:
            raise ValueError("deleted canonical dev artifact snapshot unexpectedly contains content")
        before = (
            None
            if before_snapshot is None or before_deleted
            else before_text
        )
        changes.append(
            FileChange(
                file_path=str(target),
                project=project,
                relpath=rel,
                before_content=before,
                after_content=after,
                created=before is None,
            )
        )

    return changes
