"""Operator-granted filesystem roots for agent tools.

The agent must never infer filesystem authority from a conversational path or
from its current process working directory.  A folder becomes an allowed root
only after the operator explicitly grants it (``/trust`` or the Settings
panel).  Grants are persisted inside Omega's Guardian state so they survive a
restart, and master reset removes the state tree and therefore every grant.

This module intentionally stores the canonical path, not file contents or
credentials.  A path may be unavailable after the SSD moves to another
machine; it remains a harmless stale record until the operator removes it.
"""

from __future__ import annotations

import json
import os
import shlex
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from core.paths import OMEGA_ROOT


TRUSTED_FOLDERS_RELATIVE = Path("guardian/state/trusted_folders.json")
TRUSTED_ACTIVE_RELATIVE = Path("guardian/state/trusted_active.txt")
TRUSTED_FOLDERS_SCHEMA = 1


@dataclass(frozen=True)
class TrustedFolder:
    """One explicit operator grant."""

    path: Path
    granted_at: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"path": str(self.path), "granted_at": self.granted_at}


def trusted_folders_path(omega_root: Path | None = None) -> Path:
    root = Path(omega_root) if omega_root is not None else OMEGA_ROOT
    return root.resolve() / TRUSTED_FOLDERS_RELATIVE


def trusted_active_path(omega_root: Path | None = None) -> Path:
    root = Path(omega_root) if omega_root is not None else OMEGA_ROOT
    return root.resolve() / TRUSTED_ACTIVE_RELATIVE


def _canonical(path: str | Path, *, base: Path | None = None) -> Path:
    value = str(path or "").strip()
    if not value:
        raise ValueError("trusted folder path is required")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = (base or Path.cwd()) / candidate
    return candidate.resolve(strict=False)


def _overlaps_omega(path: Path, omega_root: Path) -> bool:
    """Reject a grant that would expose Omega's protected source/state tree."""
    return path == omega_root or omega_root in path.parents or path in omega_root.parents


def validate_trusted_folder(
    path: str | Path,
    *,
    omega_root: Path | None = None,
    base: Path | None = None,
    require_existing: bool = True,
) -> Path:
    """Canonicalize and validate one operator-granted directory.

    Grants may target a directory or a whole drive, but never Omega itself,
    one of its parents, or a non-directory.  The parent/child overlap rule is
    deliberate: granting a drive root would otherwise grant access to the hidden
    product when it is mounted below that drive.
    """
    root = (Path(omega_root) if omega_root is not None else OMEGA_ROOT).resolve()
    candidate = _canonical(path, base=base)
    if _overlaps_omega(candidate, root):
        raise ValueError("trusted folder cannot overlap Omega's protected tree")
    if require_existing and not candidate.is_dir():
        raise ValueError(f"trusted folder is not a directory: {candidate}")
    return candidate


def _read_state(path: Path, *, omega_root: Path | None = None) -> list[TrustedFolder]:
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"trusted folder state is unreadable: {path}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != TRUSTED_FOLDERS_SCHEMA:
        raise RuntimeError(f"trusted folder state has an unsupported schema: {path}")
    raw_folders = payload.get("folders")
    if not isinstance(raw_folders, list):
        raise RuntimeError(f"trusted folder state has invalid folders: {path}")
    result: list[TrustedFolder] = []
    seen: set[Path] = set()
    for item in raw_folders:
        if not isinstance(item, dict):
            raise RuntimeError(f"trusted folder state contains a malformed entry: {path}")
        try:
            folder = validate_trusted_folder(
                str(item.get("path") or ""),
                omega_root=omega_root,
                require_existing=False,
            )
        except ValueError as exc:
            raise RuntimeError(f"trusted folder state contains an invalid path: {path}") from exc
        if folder in seen:
            continue
        seen.add(folder)
        result.append(TrustedFolder(folder, str(item.get("granted_at") or "")))
    return result


def _write_state(folders: Iterable[TrustedFolder], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": TRUSTED_FOLDERS_SCHEMA,
        "folders": [folder.to_dict() for folder in folders],
    }
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def load_trusted_folders(omega_root: Path | None = None) -> tuple[TrustedFolder, ...]:
    """Return persisted grants in stable path order."""
    path = trusted_folders_path(omega_root)
    folders = _read_state(path, omega_root=omega_root)
    return tuple(sorted(folders, key=lambda folder: str(folder.path).casefold()))


def trusted_paths(omega_root: Path | None = None) -> tuple[Path, ...]:
    return tuple(folder.path for folder in load_trusted_folders(omega_root))


def grant_trusted_folder(
    path: str | Path,
    *,
    omega_root: Path | None = None,
    base: Path | None = None,
    granted_at: str = "",
) -> TrustedFolder:
    """Persist one explicit grant and return its canonical record."""
    candidate = validate_trusted_folder(path, omega_root=omega_root, base=base)
    root = Path(omega_root) if omega_root is not None else OMEGA_ROOT
    current = list(load_trusted_folders(root))
    for folder in current:
        if folder.path == candidate:
            return folder
    record = TrustedFolder(candidate, granted_at=granted_at or "operator")
    _write_state([*current, record], trusted_folders_path(root))
    return record


def revoke_trusted_folder(
    path: str | Path,
    *,
    omega_root: Path | None = None,
    base: Path | None = None,
) -> bool:
    """Remove one exact grant; descendants remain independently managed."""
    candidate = _canonical(path, base=base)
    root = Path(omega_root) if omega_root is not None else OMEGA_ROOT
    current = list(load_trusted_folders(root))
    remaining = [folder for folder in current if folder.path != candidate]
    if len(remaining) == len(current):
        return False
    _write_state(remaining, trusted_folders_path(root))
    return True


def clear_trusted_folders(omega_root: Path | None = None) -> None:
    """Revoke all grants (used by reset and test cleanup)."""
    trusted_folders_path(omega_root).unlink(missing_ok=True)
    trusted_active_path(omega_root).unlink(missing_ok=True)


def active_trusted_folder(omega_root: Path | None = None) -> Path | None:
    """Return the selected working directory when it is still trusted."""
    path = trusted_active_path(omega_root)
    if not path.is_file():
        return None
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return None
    try:
        candidate = resolve_trusted_path(raw, omega_root=omega_root)
    except (OSError, ValueError):
        return None
    return candidate if candidate.is_dir() else None


def set_active_trusted_folder(
    path: str | Path,
    *,
    omega_root: Path | None = None,
) -> Path:
    """Persist the active directory, which must already be a trusted path."""
    candidate = resolve_trusted_path(path, omega_root=omega_root)
    if not candidate.is_dir():
        raise ValueError(f"trusted folder is unavailable: {candidate}")
    target = trusted_active_path(omega_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(str(candidate) + "\n", encoding="utf-8")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return candidate


def activate_grant_if_unset(
    folder: TrustedFolder | str | Path,
    *,
    omega_root: Path | None = None,
) -> Path:
    """Select a newly granted folder when no active root is available.

    Granting access from Settings (or ``/trust``) is an explicit operator
    action. When it is the first usable grant, making that same folder active
    closes the gap where the grant was persisted but chat still saw no
    workspace tools. Existing active selections are never changed.
    """
    current = active_trusted_folder(omega_root)
    if current is not None:
        return current
    candidate = folder.path if isinstance(folder, TrustedFolder) else Path(folder)
    return set_active_trusted_folder(candidate, omega_root=omega_root)


def is_trusted_path(
    path: str | Path,
    *,
    omega_root: Path | None = None,
    roots: Iterable[Path] | None = None,
) -> bool:
    candidate = _canonical(path)
    allowed = tuple(roots) if roots is not None else trusted_paths(omega_root)
    return any(candidate == root or root in candidate.parents for root in allowed)


def resolve_trusted_path(
    path: str | Path,
    *,
    active_root: Path | None = None,
    omega_root: Path | None = None,
    roots: Iterable[Path] | None = None,
) -> Path:
    """Resolve a path under a persisted grant, refusing escapes and symlinks."""
    candidate = _canonical(path, base=active_root)
    allowed = tuple(Path(root).resolve(strict=False) for root in (roots if roots is not None else trusted_paths(omega_root)))
    if not any(candidate == root or root in candidate.parents for root in allowed):
        raise ValueError(f"path is outside trusted folders: {candidate}")
    return candidate


def handle_trusted_folder_command(
    text: str,
    *,
    omega_root: Path | None = None,
) -> dict[str, Any] | None:
    """Handle explicit chat commands without sending them through model recall."""
    clean = " ".join(str(text or "").split())
    if not clean.startswith("/"):
        return None
    try:
        parts = shlex.split(clean, posix=False)
    except ValueError as exc:
        return {"ok": False, "message": f"Invalid trusted-folder command: {exc}"}
    command = parts[0].casefold() if parts else ""
    if command not in {"/trust", "/untrust", "/trusted", "/cd"}:
        return None
    if command == "/trusted":
        rows = load_trusted_folders(omega_root)
        if not rows:
            return {"ok": True, "message": "No trusted folders are configured."}
        return {
            "ok": True,
            "message": "Trusted folders:\n" + "\n".join(f"- {row.path}" for row in rows),
            "folders": [row.to_dict() for row in rows],
        }
    if len(parts) < 2 or not str(parts[1]).strip():
        return {"ok": False, "message": f"{command} requires a directory path."}
    raw_path = " ".join(parts[1:]).strip()
    if len(raw_path) >= 2 and raw_path[0] == raw_path[-1] and raw_path[0] in "\"'":
        raw_path = raw_path[1:-1]
    rows = load_trusted_folders(omega_root)
    roots = tuple(row.path for row in rows)
    active = active_trusted_folder(omega_root)
    if active is None and len(rows) == 1:
        # With one grant, a relative /cd target is unambiguous.  Multiple
        # roots require an absolute path so Omega never guesses the operator's
        # intended directory.
        active = rows[0].path
    if command == "/trust":
        try:
            record = grant_trusted_folder(raw_path, omega_root=omega_root, base=active)
            selected = activate_grant_if_unset(record, omega_root=omega_root)
        except (OSError, RuntimeError, ValueError) as exc:
            return {"ok": False, "message": f"Cannot trust folder: {exc}"}
        return {
            "ok": True,
            "message": f"Trusted folder added: {record.path}",
            "folder": record.to_dict(),
            "active": str(selected),
        }
    try:
        active = resolve_trusted_path(
            raw_path,
            active_root=active,
            omega_root=omega_root,
            roots=roots,
        )
    except (OSError, ValueError) as exc:
        return {"ok": False, "message": f"Folder is not trusted: {exc}. Use /trust first."}
    if command == "/cd":
        if not active.is_dir():
            return {"ok": False, "message": f"Trusted folder is unavailable: {active}"}
        try:
            selected = set_active_trusted_folder(active, omega_root=omega_root)
        except (OSError, ValueError) as exc:
            return {"ok": False, "message": f"Cannot select trusted folder: {exc}"}
        return {"ok": True, "message": f"Active trusted folder: {selected}", "active": str(selected)}
    removed = revoke_trusted_folder(active, omega_root=omega_root)
    return {
        "ok": True,
        "message": f"Trusted folder removed: {active}" if removed else f"Folder was not a root grant: {active}",
        "removed": removed,
    }


__all__ = [
    "TRUSTED_ACTIVE_RELATIVE",
    "TRUSTED_FOLDERS_RELATIVE",
    "TrustedFolder",
    "active_trusted_folder",
    "activate_grant_if_unset",
    "clear_trusted_folders",
    "grant_trusted_folder",
    "handle_trusted_folder_command",
    "is_trusted_path",
    "load_trusted_folders",
    "resolve_trusted_path",
    "revoke_trusted_folder",
    "trusted_folders_path",
    "trusted_active_path",
    "trusted_paths",
    "set_active_trusted_folder",
    "validate_trusted_folder",
]
