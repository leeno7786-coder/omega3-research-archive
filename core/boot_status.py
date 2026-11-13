"""Atomic, portable boot-status handoff between Omega and its setup EXE."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


BOOT_STATUS_RELATIVE = Path("guardian") / "state" / "boot_status.json"
MAX_DETAIL_LENGTH = 240
VALID_PHASES = frozenset({"starting", "loading", "memory", "warming", "hud", "ready", "failed"})


def boot_status_path(product_root: Path | None = None) -> Path:
    """Return the status file below the portable product root."""
    if product_root is None:
        from core.paths import OMEGA_ROOT

        product_root = OMEGA_ROOT
    return Path(product_root).resolve() / BOOT_STATUS_RELATIVE


def write_boot_status(
    product_root: Path | None = None,
    *,
    phase: str,
    percent: int,
    detail: str,
    pid: int | None = None,
) -> dict[str, Any]:
    """Publish one bounded status record using an atomic replace."""
    phase = str(phase).strip().casefold()
    if phase not in VALID_PHASES:
        raise ValueError(f"unsupported Omega boot status phase: {phase!r}")
    if isinstance(percent, bool) or not isinstance(percent, int) or not 0 <= percent <= 100:
        raise ValueError("Omega boot status percent must be an integer from 0 through 100")
    detail = str(detail).strip()
    if not detail:
        raise ValueError("Omega boot status detail must not be empty")
    if pid is not None and (isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0):
        raise ValueError("Omega boot status pid must be a positive integer")

    payload: dict[str, Any] = {
        "schema": 1,
        "phase": phase,
        "percent": percent,
        "detail": detail[:MAX_DETAIL_LENGTH],
        "updated_at": int(time.time()),
    }
    if pid is not None:
        payload["pid"] = pid

    path = boot_status_path(product_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return payload


def read_boot_status(product_root: Path | None = None) -> dict[str, Any] | None:
    """Read one valid status record, or return ``None`` before first boot."""
    path = boot_status_path(product_root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Omega boot status is unreadable: {path}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != 1:
        raise RuntimeError(f"Omega boot status has an unsupported schema: {path}")
    phase = payload.get("phase")
    percent = payload.get("percent")
    detail = payload.get("detail")
    if phase not in VALID_PHASES or isinstance(percent, bool) or not isinstance(percent, int) or not 0 <= percent <= 100:
        raise RuntimeError(f"Omega boot status has invalid state: {path}")
    if not isinstance(detail, str) or not detail:
        raise RuntimeError(f"Omega boot status has invalid detail: {path}")
    return payload


__all__ = [
    "BOOT_STATUS_RELATIVE",
    "MAX_DETAIL_LENGTH",
    "VALID_PHASES",
    "boot_status_path",
    "read_boot_status",
    "write_boot_status",
]
