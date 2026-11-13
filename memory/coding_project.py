"""Coding lobe project helpers.

Deterministic project-tree scan and snapshot utilities for lobe 8
(`800_coding`). Exact source copies are stored as dev-lobe artifacts;
lobe-8 memory writes store compact anchors with links to the manifest
and copied tree.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from core.paths import OMEGA_ROOT, dev_artifacts_root
from memory.dewey_map import CODING_DOMAINS, coding_coord
import logging

log = logging.getLogger("omega.memory.coding_project")


DEFAULT_EXCLUDE_DIRS = {
    ".git",
    ".omega_data",  # legacy artifact dir name in scanned trees (not our write root)
    ".venv",
    ".venv-brain",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".code-graph",
    ".serena",
    ".fallow",
    ".claude",
    ".vscode",
    "__pycache__",
    "node_modules",
}

DEFAULT_EXCLUDE_GLOBS = {
    "*.pyc",
    "*.pyo",
    "*.tmp",
    "*.log",
}

_WINDOWS_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}

_EXT_LANGUAGE = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "typescript",
    ".jsx": "typescript",
    ".rs": "rust",
    ".go": "go",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".hpp": "cpp",
    ".sql": "sql",
    ".sh": "shell",
    ".ps1": "shell",
    ".html": "html_css",
    ".css": "html_css",
}


class ProjectSnapshotError(RuntimeError):
    """Raised when an exact snapshot cannot be produced."""


@dataclass(frozen=True)
class ProjectFileEntry:
    relative_path: str
    size_bytes: int
    sha256: Optional[str] = None
    kind: str = "file"

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "relative_path": self.relative_path,
            "size_bytes": self.size_bytes,
            "kind": self.kind,
        }
        if self.sha256 is not None:
            data["sha256"] = self.sha256
        return data


@dataclass(frozen=True)
class ProjectTreeManifest:
    project_name: str
    root_path: str
    generated_at: str
    file_count: int
    total_bytes: int
    root_sha256: str
    language_counts: Dict[str, int] = field(default_factory=dict)
    extension_counts: Dict[str, int] = field(default_factory=dict)
    files: List[ProjectFileEntry] = field(default_factory=list)
    skipped: List[Dict[str, Any]] = field(default_factory=list)
    snapshot_kind: str = "scan"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "project_name": self.project_name,
            "root_path": self.root_path,
            "generated_at": self.generated_at,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "root_sha256": self.root_sha256,
            "language_counts": dict(sorted(self.language_counts.items())),
            "extension_counts": dict(sorted(self.extension_counts.items())),
            "files": [f.to_dict() for f in self.files],
            "skipped": list(self.skipped),
            "snapshot_kind": self.snapshot_kind,
        }


@dataclass(frozen=True)
class ProjectSnapshotResult:
    project_name: str
    snapshot_id: str
    snapshot_root: str
    tree_path: str
    manifest_path: str
    manifest: ProjectTreeManifest

    def to_dict(self) -> Dict[str, Any]:
        return {
            "project_name": self.project_name,
            "snapshot_id": self.snapshot_id,
            "snapshot_root": self.snapshot_root,
            "tree_path": self.tree_path,
            "manifest_path": self.manifest_path,
            "manifest": self.manifest.to_dict(),
        }


def sanitize_project_name(name: str) -> str:
    """Return a filesystem-safe top-level project name."""
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", (name or "").strip()).strip("-. _")
    cleaned = re.sub(r"-{2,}", "-", cleaned).lower()
    if not cleaned:
        cleaned = "project"
    if cleaned in _WINDOWS_RESERVED_NAMES:
        cleaned = f"{cleaned}-project"
    return cleaned[:80]


def scan_project_tree(
    root: Path | str,
    *,
    project_name: Optional[str] = None,
    include_globs: Optional[Iterable[str]] = None,
    exclude_dirs: Optional[Iterable[str]] = None,
    exclude_globs: Optional[Iterable[str]] = None,
    max_file_bytes: Optional[int] = None,
    hash_files: bool = True,
    generated_at: Optional[str] = None,
) -> ProjectTreeManifest:
    """Build a deterministic project manifest.

    Scans never dereference symlinks. When `max_file_bytes` is set,
    oversize files are skipped and reported; snapshots default to no cap.
    """
    root_path = Path(root).resolve()
    if not root_path.exists() or not root_path.is_dir():
        raise FileNotFoundError(f"project root not found: {root_path}")
    name = sanitize_project_name(project_name or root_path.name)
    entries, skipped = _collect_file_entries(
        root_path,
        include_globs=include_globs,
        exclude_dirs=exclude_dirs,
        exclude_globs=exclude_globs,
        max_file_bytes=max_file_bytes,
        hash_files=hash_files,
        dest_root=None,
    )
    return _manifest_from_entries(
        project_name=name,
        root_path=root_path,
        entries=entries,
        skipped=skipped,
        generated_at=generated_at or _now_iso(),
        snapshot_kind="scan",
    )


def snapshot_project_tree(
    root: Path | str,
    *,
    dest_root: Optional[Path | str] = None,
    project_name: Optional[str] = None,
    include_globs: Optional[Iterable[str]] = None,
    exclude_dirs: Optional[Iterable[str]] = None,
    exclude_globs: Optional[Iterable[str]] = None,
    max_file_bytes: Optional[int] = None,
    allow_partial: bool = False,
    snapshot_id: Optional[str] = None,
) -> ProjectSnapshotResult:
    """Copy a project tree exactly, then hash the copied bytes.

    By default this fails when a file would be skipped. Pass
    `allow_partial=True` to create a partial snapshot with skipped entries
    recorded in the manifest.
    """
    root_path = Path(root).resolve()
    if not root_path.exists() or not root_path.is_dir():
        raise FileNotFoundError(f"project root not found: {root_path}")

    name = sanitize_project_name(project_name or root_path.name)
    sid = sanitize_project_name(snapshot_id or _snapshot_id())
    base = Path(dest_root).resolve() if dest_root else dev_artifacts_root(OMEGA_ROOT).resolve()
    snapshot_root = base / name / sid
    tree_path = snapshot_root / "tree" / name
    manifest_path = snapshot_root / "MANIFEST.json"

    entries, skipped = _collect_file_entries(
        root_path,
        include_globs=include_globs,
        exclude_dirs=exclude_dirs,
        exclude_globs=exclude_globs,
        max_file_bytes=max_file_bytes,
        hash_files=False,
        dest_root=base,
    )
    if skipped and not allow_partial:
        reasons = ", ".join(sorted({str(s.get("reason")) for s in skipped}))
        raise ProjectSnapshotError(f"exact snapshot refused; skipped entries present: {reasons}")

    if snapshot_root.exists():
        raise FileExistsError(f"snapshot already exists: {snapshot_root}")
    tree_path.mkdir(parents=True, exist_ok=False)

    copied_entries: List[ProjectFileEntry] = []
    copy_errors: List[Dict[str, Any]] = []
    for entry in entries:
        src = root_path / Path(entry.relative_path)
        dst = tree_path / Path(entry.relative_path)
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst, follow_symlinks=False)
            copied_entries.append(
                ProjectFileEntry(
                    relative_path=entry.relative_path,
                    size_bytes=dst.stat().st_size,
                    sha256=_sha256_file(dst),
                )
            )
        except OSError as exc:
            copy_errors.append(
                {
                    "relative_path": entry.relative_path,
                    "reason": "copy_error",
                    "detail": str(exc),
                }
            )

    all_skipped = list(skipped) + copy_errors
    if all_skipped and not allow_partial:
        shutil.rmtree(snapshot_root, ignore_errors=True)
        raise ProjectSnapshotError("exact snapshot refused; copy errors present")

    manifest = _manifest_from_entries(
        project_name=name,
        root_path=root_path,
        entries=copied_entries,
        skipped=all_skipped,
        generated_at=_now_iso(),
        snapshot_kind="partial" if all_skipped else "exact",
    )
    manifest_path.write_text(
        json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (snapshot_root / "EVENTS.jsonl").write_text(
        "".join(json.dumps(s, ensure_ascii=False, sort_keys=True) + "\n" for s in all_skipped),
        encoding="utf-8",
    )
    return ProjectSnapshotResult(
        project_name=name,
        snapshot_id=sid,
        snapshot_root=str(snapshot_root),
        tree_path=str(tree_path),
        manifest_path=str(manifest_path),
        manifest=manifest,
    )


def verify_project_snapshot(snapshot_root: Path | str) -> bool:
    """Re-hash a snapshot tree and compare it to MANIFEST.json."""
    root = Path(snapshot_root).resolve()
    manifest_path = root / "MANIFEST.json"
    if not manifest_path.exists():
        return False
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    project_name = sanitize_project_name(str(data.get("project_name") or "project"))
    tree_path = root / "tree" / project_name
    if not tree_path.exists():
        return False
    entries, skipped = _collect_file_entries(
        tree_path,
        hash_files=True,
        dest_root=None,
    )
    manifest = _manifest_from_entries(
        project_name=project_name,
        root_path=Path(str(data.get("root_path") or tree_path)),
        entries=entries,
        skipped=skipped,
        generated_at=str(data.get("generated_at") or ""),
        snapshot_kind=str(data.get("snapshot_kind") or "exact"),
    )
    return manifest.root_sha256 == data.get("root_sha256")


def write_project_manifest_to_memory(
    manifest: ProjectTreeManifest,
    cycle_id: str,
    *,
    propose: bool = False,
    writer: Optional[Callable[..., Any]] = None,
) -> Any:
    """Write a lobe-8 memory anchor for a project manifest.

    Direct commit by default: these are internal derived writes
    (source="memory.coding_project"), and write_memory rejects
    propose=True for any non-user-chat source.
    """
    envelope = _manifest_envelope(manifest, cycle_id)
    if writer is None:
        from memory.api import intake_memory as writer
    return writer(envelope, propose=propose)


def write_project_snapshot_to_memory(
    snapshot: ProjectSnapshotResult,
    cycle_id: str,
    *,
    propose: bool = False,
    writer: Optional[Callable[..., Any]] = None,
) -> Any:
    """Write a lobe-8 memory anchor for an exact/partial project copy.

    Direct commit by default — same doctrine as
    write_project_manifest_to_memory.
    """
    envelope = _snapshot_envelope(snapshot, cycle_id)
    if writer is None:
        from memory.api import intake_memory as writer
    return writer(envelope, propose=propose)


def build_project_manifest_envelope(
    manifest: ProjectTreeManifest,
    cycle_id: str,
) -> Any:
    """Build the CanonicalEnvelope without writing it."""
    return _manifest_envelope(manifest, cycle_id)


def build_project_snapshot_envelope(
    snapshot: ProjectSnapshotResult,
    cycle_id: str,
) -> Any:
    """Build the CanonicalEnvelope without writing it."""
    return _snapshot_envelope(snapshot, cycle_id)


def _manifest_envelope(manifest: ProjectTreeManifest, cycle_id: str) -> Any:
    from memory.api import CanonicalEnvelope

    language, coord = _storage_coord(manifest)
    return CanonicalEnvelope(
        cycle_id=cycle_id,
        coord=coord,
        type="coding_project_manifest",
        payload={
            "title": f"{manifest.project_name} project manifest",
            "summary": (
                f"{manifest.project_name}: {manifest.file_count} files, "
                f"{manifest.total_bytes} bytes, root {manifest.root_sha256[:12]}"
            ),
            "text": _anchor_text("project_manifest", manifest.to_dict()),
            "tags": ["coding", "project_manifest", manifest.project_name, language],
            "domain": _canonical_domain(language),
            "subtopic": "project_manifest",
            "links": {
                "root_path": manifest.root_path,
                "root_sha256": manifest.root_sha256,
            },
            "enrichment": {
                "project_name": manifest.project_name,
                "root_sha256": manifest.root_sha256,
                "file_count": manifest.file_count,
                "total_bytes": manifest.total_bytes,
                "language_counts": manifest.language_counts,
            },
        },
        source="memory.coding_project",
        confidence=0.95,
        provenance=["coding_lobe_project_helper"],
    )


def _snapshot_envelope(snapshot: ProjectSnapshotResult, cycle_id: str) -> Any:
    from memory.api import CanonicalEnvelope

    manifest = snapshot.manifest
    language, coord = _storage_coord(manifest)
    return CanonicalEnvelope(
        cycle_id=cycle_id,
        coord=coord,
        type="coding_project_snapshot",
        payload={
            "title": f"{manifest.project_name} project snapshot {snapshot.snapshot_id}",
            "summary": (
                f"{manifest.project_name} snapshot {snapshot.snapshot_id}: "
                f"{manifest.snapshot_kind}, {manifest.file_count} files, "
                f"root {manifest.root_sha256[:12]}"
            ),
            "text": _anchor_text("project_snapshot", snapshot.to_dict()),
            "tags": [
                "coding",
                "project_snapshot",
                manifest.project_name,
                snapshot.snapshot_id,
                language,
            ],
            "domain": _canonical_domain(language),
            "subtopic": "project_snapshot",
            "links": {
                "snapshot_root": snapshot.snapshot_root,
                "tree_path": snapshot.tree_path,
                "manifest_path": snapshot.manifest_path,
                "root_sha256": manifest.root_sha256,
            },
            "enrichment": {
                "project_name": manifest.project_name,
                "snapshot_id": snapshot.snapshot_id,
                "snapshot_kind": manifest.snapshot_kind,
                "root_sha256": manifest.root_sha256,
                "file_count": manifest.file_count,
                "total_bytes": manifest.total_bytes,
                "language_counts": manifest.language_counts,
            },
        },
        source="memory.coding_project",
        confidence=0.98 if manifest.snapshot_kind == "exact" else 0.7,
        provenance=["coding_lobe_project_helper"],
    )


def _collect_file_entries(
    root_path: Path,
    *,
    include_globs: Optional[Iterable[str]] = None,
    exclude_dirs: Optional[Iterable[str]] = None,
    exclude_globs: Optional[Iterable[str]] = None,
    max_file_bytes: Optional[int] = None,
    hash_files: bool = True,
    dest_root: Optional[Path] = None,
) -> Tuple[List[ProjectFileEntry], List[Dict[str, Any]]]:
    includes = tuple(include_globs or ())
    excludes = set(DEFAULT_EXCLUDE_DIRS)
    if exclude_dirs:
        excludes.update(str(d) for d in exclude_dirs)
    excluded_globs = set(DEFAULT_EXCLUDE_GLOBS)
    if exclude_globs:
        excluded_globs.update(str(g) for g in exclude_globs)
    dest = dest_root.resolve() if dest_root is not None else None

    entries: List[ProjectFileEntry] = []
    skipped: List[Dict[str, Any]] = []
    for current, dirnames, filenames in os.walk(root_path, topdown=True, followlinks=False):
        current_path = Path(current).resolve()
        if dest is not None and _is_relative_to(current_path, dest):
            dirnames[:] = []
            continue
        dirnames[:] = sorted(
            d
            for d in dirnames
            if d not in excludes and not _is_symlink_dir(current_path / d, skipped, root_path)
        )
        for filename in sorted(filenames):
            path = current_path / filename
            rel = _relpath(path, root_path)
            if _matches_any(rel, excluded_globs):
                skipped.append({"relative_path": rel, "reason": "excluded_glob"})
                continue
            if includes and not _matches_any(rel, includes):
                skipped.append({"relative_path": rel, "reason": "not_included"})
                continue
            if path.is_symlink():
                skipped.append(
                    {
                        "relative_path": rel,
                        "reason": "symlink",
                        "target": os.readlink(path),
                    }
                )
                continue
            try:
                size = path.stat().st_size
            except OSError as exc:
                skipped.append({"relative_path": rel, "reason": "stat_error", "detail": str(exc)})
                continue
            if max_file_bytes is not None and size > max_file_bytes:
                skipped.append(
                    {
                        "relative_path": rel,
                        "reason": "size_limit",
                        "size_bytes": size,
                        "max_file_bytes": max_file_bytes,
                    }
                )
                continue
            sha = _sha256_file(path) if hash_files else None
            entries.append(ProjectFileEntry(relative_path=rel, size_bytes=size, sha256=sha))
    return entries, skipped


def _manifest_from_entries(
    *,
    project_name: str,
    root_path: Path,
    entries: List[ProjectFileEntry],
    skipped: List[Dict[str, Any]],
    generated_at: str,
    snapshot_kind: str,
) -> ProjectTreeManifest:
    ordered = sorted(entries, key=lambda e: e.relative_path)
    total_bytes = sum(e.size_bytes for e in ordered)
    extension_counts: Dict[str, int] = {}
    language_counts: Dict[str, int] = {}
    for entry in ordered:
        suffix = Path(entry.relative_path).suffix.lower() or "<none>"
        extension_counts[suffix] = extension_counts.get(suffix, 0) + 1
        language = _EXT_LANGUAGE.get(suffix)
        if language:
            language_counts[language] = language_counts.get(language, 0) + 1
    return ProjectTreeManifest(
        project_name=project_name,
        root_path=str(root_path),
        generated_at=generated_at,
        file_count=len(ordered),
        total_bytes=total_bytes,
        root_sha256=_root_hash(ordered),
        language_counts=language_counts,
        extension_counts=extension_counts,
        files=ordered,
        skipped=sorted(skipped, key=lambda s: str(s.get("relative_path", ""))),
        snapshot_kind=snapshot_kind,
    )


def _root_hash(entries: List[ProjectFileEntry]) -> str:
    canonical = [
        {
            "relative_path": e.relative_path,
            "size_bytes": e.size_bytes,
            "sha256": e.sha256,
            "kind": e.kind,
        }
        for e in entries
    ]
    payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _relpath(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _matches_any(relative_path: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatch(relative_path, pattern) for pattern in patterns)


def _is_symlink_dir(path: Path, skipped: List[Dict[str, Any]], root: Path) -> bool:
    if not path.is_symlink():
        return False
    skipped.append(
        {
            "relative_path": _relpath(path, root),
            "reason": "symlink_dir",
            "target": os.readlink(path),
        }
    )
    return True


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _canonical_domain(language: str) -> str:
    """Canonical mastery domain_id for a coding language.

    The engram ``domain`` must match how the deepsleep mastery store keys
    domains (``normalize_domain_id`` -> ``_slug``), or coding-domain hotness
    lookups miss and APPLY never fires. The ``{language}_systems`` dewey key
    still drives the physical coord lookup in :func:`_dominant_language_coord`
    (unchanged).
    """
    from deepsleep_loop.mastery_store import normalize_domain_id

    return normalize_domain_id(language)


def _dominant_language_coord(manifest: ProjectTreeManifest) -> Tuple[str, List[int]]:
    if manifest.language_counts:
        language = sorted(
            manifest.language_counts.items(),
            key=lambda item: (-item[1], item[0]),
        )[0][0]
    else:
        language = "python"
    domain = f"{language}_systems"
    if domain not in CODING_DOMAINS:
        domain = f"{language}_web" if f"{language}_web" in CODING_DOMAINS else "python_systems"
    coord = coding_coord(domain)
    return domain.split("_", 1)[0], [int(coord[0]), int(coord[1]), 0]


def _storage_coord(manifest: ProjectTreeManifest) -> Tuple[str, List[int]]:
    """Resolve the storage coord, honoring dynamic lobe-8 routing when enabled."""
    language, coord = _dominant_language_coord(manifest)
    try:
        from memory.subdomain_flag import dynamic_y_lobes

        flag = dynamic_y_lobes()
    except Exception:
        flag = frozenset()

    if 8 not in flag:
        return language, coord

    from memory.subdomain_allocator import get_or_create_subdomain

    language_slug = _canonical_domain(language)
    y, _dirname = get_or_create_subdomain(
        lobe=8,
        seed_text=language_slug,
        title=language_slug,
        dynamic_y_lobes=set(flag),
    )
    return language, [8, y, 0]


def _anchor_text(kind: str, payload: Dict[str, Any]) -> str:
    compact = {
        "kind": kind,
        "project_name": payload.get("project_name"),
        "snapshot_id": payload.get("snapshot_id"),
        "snapshot_kind": (payload.get("manifest") or payload).get("snapshot_kind"),
        "root_sha256": (payload.get("manifest") or payload).get("root_sha256"),
        "file_count": (payload.get("manifest") or payload).get("file_count"),
        "total_bytes": (payload.get("manifest") or payload).get("total_bytes"),
        "manifest_path": payload.get("manifest_path"),
        "tree_path": payload.get("tree_path"),
        "language_counts": (payload.get("manifest") or payload).get("language_counts"),
    }
    return json.dumps(compact, ensure_ascii=False, sort_keys=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _snapshot_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Coding lobe project scan/snapshot helper")
    sub = parser.add_subparsers(dest="command", required=True)

    scan_p = sub.add_parser("scan", help="Print a deterministic project manifest")
    scan_p.add_argument("root")
    scan_p.add_argument("--project-name")
    scan_p.add_argument("--no-hash", action="store_true")

    snap_p = sub.add_parser("snapshot", help="Create an exact project snapshot")
    snap_p.add_argument("root")
    snap_p.add_argument("--dest-root")
    snap_p.add_argument("--project-name")
    snap_p.add_argument("--snapshot-id")
    snap_p.add_argument("--allow-partial", action="store_true")
    snap_p.add_argument("--write-memory", action="store_true")
    snap_p.add_argument("--cycle-id", default="")

    args = parser.parse_args(argv)
    if args.command == "scan":
        manifest = scan_project_tree(
            args.root,
            project_name=args.project_name,
            hash_files=not args.no_hash,
        )
        print(json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    snapshot = snapshot_project_tree(
        args.root,
        dest_root=args.dest_root,
        project_name=args.project_name,
        snapshot_id=args.snapshot_id,
        allow_partial=args.allow_partial,
    )
    print(json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    if args.write_memory:
        if not args.cycle_id:
            raise ValueError("--cycle-id is required with --write-memory")
        receipt = write_project_snapshot_to_memory(snapshot, args.cycle_id)
        print(
            json.dumps(
                getattr(receipt, "__dict__", {"receipt": str(receipt)}),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
