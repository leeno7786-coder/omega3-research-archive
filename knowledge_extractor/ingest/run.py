"""Ingest phase — normalize a Source by kind (deterministic, no models)."""

from __future__ import annotations

import re

from knowledge_extractor._types import Source

_WHITESPACE_RE = re.compile(r"[ \t]+")
_MULTI_NEWLINE_RE = re.compile(r"\n{2,}")


def _strip_boilerplate(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines()]
    non_empty = [ln for ln in lines if ln]
    joined = "\n".join(non_empty)
    joined = _MULTI_NEWLINE_RE.sub("\n", joined)
    joined = _WHITESPACE_RE.sub(" ", joined)
    return joined.strip()


def _normalize_web_or_tool(source: Source) -> dict:
    text = _strip_boilerplate(source.content or "")
    title = (source.title or "").strip() or None
    source_ref = (source.source_ref or "").strip() or None
    return {
        "text": text,
        "title": title,
        "source_ref": source_ref,
        "source_kind": source.kind,
        "meta": dict(source.meta or {}),
    }


def run(source: Source) -> dict:
    """Normalize source by kind to {text, title, source_ref, source_kind, meta}."""
    kind = (source.kind or "").strip().lower()
    if kind in ("web_result", "tool_output"):
        return _normalize_web_or_tool(source)
    if kind in ("file", "pdf", "video_transcript"):
        text = _strip_boilerplate(source.content or "")
        return {
            "text": text,
            "title": (source.title or "").strip() or None,
            "source_ref": (source.source_ref or "").strip() or None,
            "source_kind": kind,
            "meta": dict(source.meta or {}),
        }
    return _normalize_web_or_tool(source)
