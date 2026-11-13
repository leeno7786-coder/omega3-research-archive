"""Canonical store for generated image artifacts.

Generated images (SD-Turbo via the image_tool cognition path) land under
``guardian/state/images/<YYYY-MM-DD>/<HHMMSS>_<slug>.png``. This module is the
single source of truth for *writing* and *listing* them — shared by the AUTO
``IMAGE_CREATION`` execution and the HUD artifact-serving route. It depends
only on ``core.paths``; no cognition or HUD coupling, so either layer can
import it without a cycle.

URL rewriting (path -> ``/api/images/...``) is intentionally NOT here — that
is a HUD-serving concern and lives in ``hud.server``.
"""

from __future__ import annotations

import re
import heapq
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.paths import GUARDIAN_STATE_ROOT

from runtime.atomic_store import atomic_write_bytes, unique_timestamped_name

IMAGE_ROOT = GUARDIAN_STATE_ROOT / "images"


def _emit_image_store(event_name: str, *, ok: bool, **data) -> None:
    payload = {"ok": ok, **data}
    try:
        from guardian.terminal_feedback import emit_data

        emit_data("runtime", event_name, status="ok" if ok else "fail", **payload)
    except Exception:
        pass
    try:
        from guardian.terminal_feedback import emit_guardian_event

        emit_guardian_event("omega.runtime.image_store", event_name, **payload)
    except Exception:
        pass


def _slug(prompt: str) -> str:
    """Filesystem-safe slug from the prompt (<=40 chars; never empty)."""
    return re.sub(r"[^A-Za-z0-9]+", "_", (prompt or "").strip()).strip("_").lower()[:40] or "image"


def persist_generated_image(
    img_bytes: bytes,
    prompt: str,
    *,
    when: Optional[datetime] = None,
) -> Path:
    """Write generated image bytes to the canonical store; return the path.

    Path shape: ``guardian/state/images/<date>/<ts>_<slug>.png``.
    ``when`` is injectable for deterministic tests.
    """
    if not img_bytes:
        raise ValueError("persist_generated_image: empty image bytes")
    stamp = when or datetime.now(timezone.utc)
    out_dir = IMAGE_ROOT / stamp.strftime("%Y-%m-%d")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / unique_timestamped_name(stamp, _slug(prompt), ".png")
    atomic_write_bytes(out_path, bytes(img_bytes))
    _emit_image_store(
        "persist_generated_image",
        ok=True,
        path=str(out_path),
        size=len(img_bytes),
    )
    return out_path


def list_generated_images(limit: int = 50) -> List[Dict[str, Any]]:
    """Newest-first list of stored images (metadata + fs path; no URLs)."""
    items: List[Dict[str, Any]] = []
    if limit <= 0 or not IMAGE_ROOT.exists():
        return items
    for date_dir in sorted(IMAGE_ROOT.iterdir(), reverse=True):
        if not date_dir.is_dir():
            continue
        remaining = limit - len(items)
        candidates = (
            fp for fp in date_dir.iterdir() if fp.is_file() and fp.suffix.lower() == ".png"
        )
        for fp in heapq.nlargest(
            remaining,
            candidates,
            key=lambda p: p.stat().st_mtime,
        ):
            st = fp.stat()
            items.append(
                {
                    "date": date_dir.name,
                    "name": fp.name,
                    "path": str(fp),
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                }
            )
            if len(items) >= limit:
                _emit_image_store("list_generated_images", ok=True, count=len(items), limit=limit)
                return items
    _emit_image_store("list_generated_images", ok=True, count=len(items), limit=limit)
    return items
