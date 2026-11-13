"""Canonical store for generated document artifacts."""

from __future__ import annotations

import heapq
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from core.paths import GUARDIAN_STATE_ROOT
from runtime.atomic_store import atomic_write_bytes, unique_timestamped_name

DOCUMENT_ROOT = GUARDIAN_STATE_ROOT / "documents"


def _emit_document_store(event_name: str, *, ok: bool, **data) -> None:
    payload = {"ok": ok, **data}
    try:
        from guardian.terminal_feedback import emit_data

        emit_data("runtime", event_name, status="ok" if ok else "fail", **payload)
    except Exception:
        pass
    try:
        from guardian.terminal_feedback import emit_guardian_event

        emit_guardian_event("omega.runtime.document_store", event_name, **payload)
    except Exception:
        pass


def _slug(title: str) -> str:
    return (
        re.sub(r"[^A-Za-z0-9]+", "_", (title or "").strip()).strip("_").lower()[:40] or "document"
    )


def persist_document(
    bytes_or_text: Union[bytes, str],
    title: str,
    fmt: str,
    *,
    when: Optional[datetime] = None,
) -> Path:
    """Write document to guardian/state/documents/<date>/<ts>_<slug>.<fmt>."""
    ext = str(fmt or "md").lower().lstrip(".")
    if ext not in ("md", "docx", "pdf"):
        raise ValueError(f"persist_document: unsupported format {fmt!r}")
    if bytes_or_text is None or bytes_or_text == "" or bytes_or_text == b"":
        raise ValueError("persist_document: empty document content")
    stamp = when or datetime.now(timezone.utc)
    out_dir = DOCUMENT_ROOT / stamp.strftime("%Y-%m-%d")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / unique_timestamped_name(stamp, _slug(title), f".{ext}")
    payload = (
        bytes_or_text.encode("utf-8") if isinstance(bytes_or_text, str) else bytes(bytes_or_text)
    )
    atomic_write_bytes(out_path, payload)
    size = len(payload)
    _emit_document_store(
        "persist_document",
        ok=True,
        path=str(out_path),
        size=size,
        format=ext,
    )
    return out_path


def list_documents(limit: int = 50) -> List[Dict[str, Any]]:
    """Newest-first list of stored documents (metadata + fs path; no URLs)."""
    items: List[Dict[str, Any]] = []
    if limit <= 0 or not DOCUMENT_ROOT.exists():
        return items
    for date_dir in sorted(DOCUMENT_ROOT.iterdir(), reverse=True):
        if not date_dir.is_dir():
            continue
        remaining = limit - len(items)
        candidates = (
            fp
            for fp in date_dir.iterdir()
            if fp.is_file() and fp.suffix.lower().lstrip(".") in ("md", "docx", "pdf")
        )
        for fp in heapq.nlargest(
            remaining,
            candidates,
            key=lambda p: p.stat().st_mtime,
        ):
            ext = fp.suffix.lower().lstrip(".")
            st = fp.stat()
            items.append(
                {
                    "date": date_dir.name,
                    "name": fp.name,
                    "path": str(fp),
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                    "format": ext,
                }
            )
            if len(items) >= limit:
                _emit_document_store("list_documents", ok=True, count=len(items), limit=limit)
                return items
    _emit_document_store("list_documents", ok=True, count=len(items), limit=limit)
    return items
