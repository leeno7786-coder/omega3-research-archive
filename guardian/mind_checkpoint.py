"""Phase 0c — reversibility checkpoint/restore for Omega's live mind state.

git cannot roll back the mind: guardian/state, the memory lobes, and the
deepsleep/pressure state are all gitignored. So before a reversible code-executor
(APPLY) is allowed to mutate the mind, we snapshot the in-scope state (defined by
:mod:`core.mind_state_surface`) into ``guardian/checkpoints/<id>/`` with a hashed
manifest, and can restore it wholesale if the executor corrupts the mind.

Guardian owns mind-state writes (sole-writer doctrine), so the checkpoint store
lives under ``guardian/`` — a sibling of ``guardian/state`` and therefore outside
the checkpointed scope (a checkpoint never captures itself).

Every public function takes ``omega_root`` so callers (and tests) can target an
isolated tree; production callers default to the real OMEGA_ROOT.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from core.mind_state_surface import mind_state_files
from core.paths import OMEGA_ROOT

log = logging.getLogger("omega.guardian.mind_checkpoint")

CHECKPOINTS_RELDIR = "guardian/checkpoints"
MANIFEST_NAME = "MANIFEST.json"
DEFAULT_KEEP = 3


@dataclass(frozen=True)
class CheckpointResult:
    checkpoint_id: str
    checkpoint_root: str
    manifest_path: str
    file_count: int
    total_bytes: int
    root_sha256: str


@dataclass(frozen=True)
class RestoreReport:
    checkpoint_id: str
    ok: bool
    restored_files: int
    removed_files: int
    errors: List[str] = field(default_factory=list)


def _checkpoints_root(omega_root: Path | str) -> Path:
    return Path(omega_root) / CHECKPOINTS_RELDIR


def _new_checkpoint_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _root_hash(entries: List[Dict[str, object]]) -> str:
    canonical = sorted(entries, key=lambda e: str(e["path"]))
    payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _atomic_write_json(path: Path, obj: object) -> None:
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def checkpoint_mind_state(
    *,
    omega_root: Path | str = OMEGA_ROOT,
    label: str = "",
    checkpoint_id: Optional[str] = None,
    keep: int = DEFAULT_KEEP,
) -> CheckpointResult:
    """Snapshot the current in-scope mind state into a new checkpoint.

    Absent state (first boot) yields a valid empty checkpoint. After writing,
    prunes the store to the newest ``keep`` checkpoints.
    """
    root = Path(omega_root)
    cid = checkpoint_id or _new_checkpoint_id()
    cp_root = _checkpoints_root(root) / cid
    if cp_root.exists():
        shutil.rmtree(cp_root)
    cp_root.mkdir(parents=True)

    entries: List[Dict[str, object]] = []
    total = 0
    for src in mind_state_files(root):
        rel = src.relative_to(root)
        dst = cp_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        size = dst.stat().st_size
        entries.append({"path": rel.as_posix(), "size_bytes": size, "sha256": _sha256_file(dst)})
        total += size

    manifest = {
        "checkpoint_id": cid,
        "label": label,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "omega_root": str(root),
        "file_count": len(entries),
        "total_bytes": total,
        "root_sha256": _root_hash(entries),
        "files": entries,
    }
    manifest_path = cp_root / MANIFEST_NAME
    _atomic_write_json(manifest_path, manifest)
    log.info("mind checkpoint %s: %d files, %d bytes (label=%r)", cid, len(entries), total, label)

    prune_checkpoints(keep=keep, omega_root=root)
    return CheckpointResult(
        checkpoint_id=cid,
        checkpoint_root=str(cp_root),
        manifest_path=str(manifest_path),
        file_count=len(entries),
        total_bytes=total,
        root_sha256=str(manifest["root_sha256"]),
    )


def restore_mind_state(
    checkpoint_id: str,
    *,
    omega_root: Path | str = OMEGA_ROOT,
) -> RestoreReport:
    """Restore the mind to a checkpoint: drop in-scope files that the checkpoint
    did not capture, then copy the captured files back. Best-effort with error
    collection (mirrors memory/rollback.py); excluded telemetry is left untouched.
    """
    root = Path(omega_root)
    cp_root = _checkpoints_root(root) / checkpoint_id
    manifest_path = cp_root / MANIFEST_NAME
    if not manifest_path.exists():
        return RestoreReport(checkpoint_id, False, 0, 0, [f"no manifest at {manifest_path}"])

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    captured = {str(f["path"]) for f in manifest["files"]}
    errors: List[str] = []

    removed = 0
    for current in mind_state_files(root):
        rel = current.relative_to(root).as_posix()
        if rel not in captured:
            try:
                current.unlink()
                removed += 1
            except OSError as exc:
                errors.append(f"remove {rel}: {exc}")

    restored = 0
    for f in manifest["files"]:
        src = cp_root / str(f["path"])
        dst = root / str(f["path"])
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            restored += 1
        except OSError as exc:
            errors.append(f"restore {f['path']}: {exc}")

    log.info(
        "mind restore %s: %d restored, %d removed, %d errors",
        checkpoint_id,
        restored,
        removed,
        len(errors),
    )
    return RestoreReport(checkpoint_id, not errors, restored, removed, errors)


def list_checkpoints(omega_root: Path | str = OMEGA_ROOT) -> List[str]:
    """Checkpoint ids present (with a manifest), ascending — id is timestamp-sortable."""
    base = _checkpoints_root(omega_root)
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir() and (p / MANIFEST_NAME).exists())


def prune_checkpoints(
    keep: int = DEFAULT_KEEP, *, omega_root: Path | str = OMEGA_ROOT
) -> List[str]:
    """Keep the newest ``keep`` checkpoints (highest ids); remove the rest. Returns removed ids."""
    ids = list_checkpoints(omega_root)
    if keep is None or keep < 0 or len(ids) <= keep:
        return []
    to_remove = ids[: len(ids) - keep]
    base = _checkpoints_root(omega_root)
    removed: List[str] = []
    for cid in to_remove:
        try:
            shutil.rmtree(base / cid)
            removed.append(cid)
        except OSError as exc:
            log.warning("prune checkpoint %s failed: %s", cid, exc)
    return removed


def verify_checkpoint(checkpoint_id: str, *, omega_root: Path | str = OMEGA_ROOT) -> bool:
    """Re-hash every captured file and the manifest root hash; True iff intact."""
    cp_root = _checkpoints_root(omega_root) / checkpoint_id
    manifest_path = cp_root / MANIFEST_NAME
    if not manifest_path.exists():
        return False
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for f in manifest["files"]:
        path = cp_root / str(f["path"])
        if not path.is_file() or _sha256_file(path) != f["sha256"]:
            return False
    entries = [
        {"path": f["path"], "size_bytes": f["size_bytes"], "sha256": f["sha256"]}
        for f in manifest["files"]
    ]
    return _root_hash(entries) == manifest.get("root_sha256")
