"""Shared routing predicates for durable plugin-result memory."""

from __future__ import annotations

import re


_PLUGIN_REFERENCE_RE = re.compile(
    r"\b(plugin|mcp|provider|slack|gmail|calendar|drive|github|notion|stripe|"
    r"discord|linear|zapier)\b",
    re.IGNORECASE,
)
_RETROSPECTIVE_RE = re.compile(
    r"\b(what did|what has|what was|what were|previous|last|earlier|before|"
    r"returned|return|said|result|results)\b",
    re.IGNORECASE,
)


def is_plugin_memory_recall_query(text: str) -> bool:
    """Distinguish recalling a prior plugin result from calling the plugin now."""
    value = str(text or "").strip()
    return bool(_PLUGIN_REFERENCE_RE.search(value) and _RETROSPECTIVE_RE.search(value))
