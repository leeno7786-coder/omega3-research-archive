"""Collision-resistant, atomic writes for persisted runtime artifacts."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from uuid import uuid4


def unique_timestamped_name(
    stamp: datetime,
    slug: str,
    suffix: str,
) -> str:
    """Return a sortable artifact name with a collision-resistant identity."""
    ext = suffix if suffix.startswith(".") else f".{suffix}"
    return f"{stamp.strftime('%H%M%S_%f')}_{uuid4().hex}_{slug}{ext}"


def atomic_write_bytes(path: Path, payload: bytes) -> Path:
    """Publish complete bytes at a unique final path without partial visibility."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.part")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target
