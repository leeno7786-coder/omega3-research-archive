"""The unconditional Program 0 projection owner.

Boot, reindex callers and the DeepSleep terminus all enter this dispatcher. It acquires the
publication authority once, reconciles the landed clock, replays incomplete generation
intents, then runs the current Program 0 drain. Program 0 builds from a closed canonical
snapshot and publishes an immutable generation; no fixed-name writer is selected here.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from core.timekeeper import CoverageClock
from memory.projection.generation import (
    PublicationResult,
    _open_lock_handle,
    _unlock,
    generation_authority_exists,
    projection_publication_authority,
    projection_root,
    read_coverage_seal,
    recover_generation_intents,
)

log = logging.getLogger(__name__)

__all__ = [
    "DispatchReport",
    "ProjectionPathConflict",
    "projection_dispatch",
    "assert_single_projection_path",
    "_dirty_marker_sha",
    "hold_lifecycle_lock",
    "release_lifecycle_lock",
    "LIFECYCLE_LOCK_FILE",
]

LIFECYCLE_LOCK_FILE = "lifecycle.lock"  # guardian/lifecycle.lock survives day-zero.


@dataclass(frozen=True, kw_only=True)
class DispatchReport:
    owner: Literal["program0"]
    action: Literal["published", "no_work", "failed"]
    publication: PublicationResult | None
    drain_status: str | None
    generation_id: str | None
    head_clock: CoverageClock | None = None
    live_clock: CoverageClock | None = None
    detail: dict | None = None


class ProjectionPathConflict(RuntimeError):
    """A fixed-name writer was asked to run over a published generation."""


def assert_single_projection_path(root: Path | str | None = None) -> None:
    """Keep retired in-place writers away from immutable generations.

    Production rebuilds use :func:`projection_dispatch`. The generation candidate builder
    has separate ``source_root``/``output_dir`` APIs and does not pass this guard.
    """
    proj = projection_root(root)
    exists, what = generation_authority_exists(proj) if proj.is_dir() else (False, None)
    if exists:
        raise ProjectionPathConflict(
            f"a published projection generation survives ({what}); fixed-name writers cannot "
            "change the immutable generation that recall serves"
        )


def _reconcile_landed_from_seal(proj: Path) -> None:
    """Reconcile the landed floor from the durable coverage seal before the drain."""
    from core import timekeeper as TK

    try:
        seal = read_coverage_seal(proj)
    except Exception:  # noqa: BLE001 — health reports malformed seals separately.
        return
    if seal is None:
        return
    try:
        TK.reconcile_landed(seal.commit_seq, clock_epoch_id=seal.clock_epoch_id)
    except Exception:  # noqa: BLE001 — reconciliation cannot replace the required drain.
        pass


def _dirty_marker_sha(root: Path | str | None) -> str | None:
    """sha256 of the projection dirty marker's bytes, if the marker is readable."""
    from memory.projection_delta import projection_dirty_path

    path = projection_dirty_path(root)
    if not path.exists():
        return None
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def projection_dispatch(
    root: Path | str | None = None, *, reason: str
) -> DispatchReport:
    """Run Program 0 unconditionally under the one publication authority."""
    proj = projection_root(root)
    proj.mkdir(parents=True, exist_ok=True)
    with projection_publication_authority(root) as authority:
        _reconcile_landed_from_seal(proj)
        recover_generation_intents(root, authority=authority, reason=f"{reason}:recovery")
        return run_program0_locked(root, reason=reason, authority=authority)


def run_program0_locked(
    root: Path | str | None, *, reason: str, authority: Any
) -> DispatchReport:
    """Invoke the one locked generation owner exactly once with the dispatcher token."""
    from memory.projection.projector import drain_once_locked

    report = drain_once_locked(root, reason=reason, authority=authority)
    publication = report.publication
    if publication is not None and report.cursor_settled:
        action: Literal["published", "no_work", "failed"] = (
            "no_work"
            if publication.outcome == "reused_existing"
            and not publication.publication_row_written
            else "published"
        )
    elif report.status in ("no_work", "bootstrapped"):
        action = "no_work"
    else:
        action = "failed"

    detail = dict(report.detail or {})
    detail.update(
        {
            "full_builds": report.full_builds,
            "deltas_seen": report.deltas_seen,
            "malformed_row_count": report.malformed_row_count,
            "cursor_settled": report.cursor_settled,
            "recovery_rotated": report.recovery_rotated,
        }
    )
    return DispatchReport(
        owner="program0",
        action=action,
        publication=publication,
        drain_status=report.status,
        generation_id=report.generation_id,
        head_clock=report.head_clock,
        live_clock=report.live_clock,
        detail=detail,
    )


# ── lifecycle interlock (D-13-75) ────────────────────────────────────────────

_lifecycle_handle: dict[str, int | None] = {"fd": None}


def lifecycle_lock_path() -> Path:
    from core.paths import OMEGA_ROOT

    return Path(OMEGA_ROOT) / "guardian" / LIFECYCLE_LOCK_FILE


def hold_lifecycle_lock() -> bool:
    """Hold the shared day-zero interlock for the process lifetime."""
    if _lifecycle_handle["fd"] is not None:
        return True
    from memory.projection.generation import _lock_shared_nb

    fd = _open_lock_handle(lifecycle_lock_path())
    if not _lock_shared_nb(fd):
        os.close(fd)
        return False
    _lifecycle_handle["fd"] = fd
    return True


def release_lifecycle_lock() -> None:
    fd = _lifecycle_handle.get("fd")
    if fd is not None:
        _unlock(fd)
        os.close(fd)
        _lifecycle_handle["fd"] = None
