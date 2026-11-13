"""One-time migration of legacy profile state paths.

Older standalone profiles stored StrengthIndex under
``<profile>/state/``. Canonical layout is ``<profile>/guardian/state/``
(aligned with day-zero wipe paths). Migrate known files once, without
overwriting newer destinations.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

log = logging.getLogger("omega.memory.legacy_state")

_MIGRATE_NAMES = (
    "strength_index.json",
)


def migrate_legacy_guardian_state(profile_root: Path | str | None = None) -> list[str]:
    """Copy legacy ``state/*`` files into ``guardian/state/`` when missing.

    Returns the list of basenames that were migrated.
    """
    if profile_root is None:
        from core.paths import OMEGA_ROOT

        root = Path(OMEGA_ROOT)
    else:
        root = Path(profile_root)

    legacy = root / "state"
    modern = root / "guardian" / "state"
    if not legacy.is_dir():
        return []

    modern.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    for name in _MIGRATE_NAMES:
        src = legacy / name
        dst = modern / name
        if not src.is_file():
            continue
        if dst.exists():
            continue
        try:
            shutil.copy2(src, dst)
            moved.append(name)
            log.info("migrated legacy state file %s -> %s", src, dst)
        except OSError as exc:
            log.warning("legacy state migrate failed for %s: %s", name, exc)
    return moved
