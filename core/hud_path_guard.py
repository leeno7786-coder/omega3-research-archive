from __future__ import annotations

import re
import os
from pathlib import Path

from core.paths import GUARDIAN_STATE_ROOT, OMEGA_ROOT

_SEGMENT = re.compile(r"^[A-Za-z0-9_\-.]{1,128}$")


def _resolve_candidate(file_path: str) -> Path:
    raw = (file_path or "").strip()
    if not raw:
        raise ValueError("empty path")
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = OMEGA_ROOT / raw
    return candidate.resolve(strict=False)


def _operator_workspace_roots() -> tuple[Path, ...]:
    """Return explicit operator grants plus the isolated test override."""
    try:
        from core.skill_execution_config import operator_workspace_env

        raw = os.environ.get(operator_workspace_env(), "").strip()
    except Exception:
        raw = os.environ.get("OMEGA_OPERATOR_WORKSPACE", "").strip()
    if raw:
        root = Path(raw)
        if not root.is_absolute():
            root = OMEGA_ROOT / root
        return (root.resolve(strict=False),)
    try:
        from core.trusted_folders import trusted_paths

        return tuple(Path(root).resolve(strict=False) for root in trusted_paths())
    except Exception:
        return ()


def _operator_workspace_root() -> Path | None:
    """Compatibility accessor for callers that need one selected root."""
    roots = _operator_workspace_roots()
    return roots[0] if roots else None


def validate_path_segment(segment: str) -> str:
    value = (segment or "").strip()
    if not value or ".." in value or "/" in value or "\\" in value:
        raise ValueError("invalid path segment")
    if not _SEGMENT.match(value):
        raise ValueError("invalid path segment")
    return value


def resolve_repo_path(file_path: str) -> Path:
    candidate = _resolve_candidate(file_path)
    root = OMEGA_ROOT.resolve()
    if not candidate.is_relative_to(root):
        raise ValueError("path outside repo")
    return candidate


def resolve_dev_edit_path(file_path: str) -> tuple[Path, str]:
    """Resolve a rollback target inside Omega or the operator workspace."""
    candidate = _resolve_candidate(file_path)
    roots: tuple[tuple[str, Path], ...]
    repo_root = OMEGA_ROOT.resolve(strict=False)
    workspace_roots = _operator_workspace_roots()
    roots = (("repo", repo_root),)
    roots = roots + tuple(("trusted_folder", root) for root in workspace_roots if root != repo_root)
    for scope, root in roots:
        if candidate.is_relative_to(root):
            return candidate, scope
    raise ValueError("path outside repo or operator workspace")


def resolve_face_image_path(person_id: str, filename: str) -> Path:
    pid = validate_path_segment(person_id)
    fn = validate_path_segment(filename)
    root = (GUARDIAN_STATE_ROOT / "face_images").resolve()
    candidate = (root / pid / fn).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError("invalid face image path")
    return candidate
