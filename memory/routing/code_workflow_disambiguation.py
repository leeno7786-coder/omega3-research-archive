"""Deterministic lobe 8 (coding artifact) vs 10 (workflow process) disambiguation."""

from __future__ import annotations

import re
from typing import List

LOBE_ID_TO_NAME = {
    0: "identity",
    1: "personas",
    2: "knowledge",
    3: "reasoning",
    4: "situational",
    5: "skills",
    6: "journal",
    7: "vision",
    8: "coding",
    9: "temporal",
    10: "workflows",
    11: "dev",
}


def _code_workflow_markers():
    artifact: set = set()
    process: set = set()
    try:
        from memory.dewey_map import SUBDOMAIN_MAP
    except Exception:
        SUBDOMAIN_MAP = {}
    _LANG = {
        "python": ["python"],
        "typescript": ["typescript", "javascript"],
        "rust": ["rust"],
        "go": ["golang"],
        "c": ["c programming"],
        "cpp": ["c++", "cpp"],
        "sql": ["sql"],
        "shell": ["bash", "powershell"],
        "html": ["html", "css"],
    }
    for (lobe, _y), dirname in SUBDOMAIN_MAP.items():
        if lobe == 8:
            for f in _LANG.get(str(dirname).split("_", 1)[0], []):
                artifact.add(f)
        elif lobe == 10:
            process.add(str(dirname).replace("_", " "))
    artifact.update(
        [
            "asyncio",
            "coroutine",
            "async def",
            "context manager",
            "decorator",
            "trait",
            "struct",
            "interface",
            "class ",
            "function",
            "method",
            "api ",
            "library",
            "framework",
            "exitstack",
            "generic",
            "lifetime",
            "pointer",
            "compiler",
            "regex",
            "docker",
            "kubernetes",
            "grpc",
        ]
    )
    process.update(
        [
            "step ",
            "first ",
            "then ",
            "next ",
            "finally",
            "workflow",
            "process",
            "pipeline",
            "procedure",
            "how i ",
            "how to ",
            "walk through",
            "walkthrough",
            "i refactored",
            "i debugged",
            "i reviewed",
            "i tested",
            "i authored",
            "recorded the",
            "sequence of",
            "the steps",
            "chain of",
        ]
    )
    return sorted(artifact), sorted(process)


_CODE_ARTIFACT_MARKERS, _CODE_PROCESS_MARKERS = _code_workflow_markers()


def _compile_markers(markers):
    """Boundary-aware matchers: a marker matches only as a whole token, never as
    a substring — so 'rust' does not fire inside 'frustrated', nor 'struct'
    inside 'structure'. ``(?<!\\w)...(?!\\w)`` brackets the (stripped) marker."""
    return [re.compile(r"(?<!\w)" + re.escape(m.strip()) + r"(?!\w)") for m in markers if m.strip()]


_CODE_ARTIFACT_RE = _compile_markers(_CODE_ARTIFACT_MARKERS)
_CODE_PROCESS_RE = _compile_markers(_CODE_PROCESS_MARKERS)


def _marker_counts(t: str) -> tuple:
    a = sum(1 for rx in _CODE_ARTIFACT_RE if rx.search(t))
    p = sum(1 for rx in _CODE_PROCESS_RE if rx.search(t))
    return a, p


def suggest_code_workflow_lobe(text: str) -> int | None:
    """Return 8 or 10 when marker counts disambiguate code vs process."""
    if not str(text or "").strip():
        return None
    a, p = _marker_counts(text.lower())
    if a > p:
        return 8
    if p > a:
        return 10
    return None


def disambiguate_code_workflow(text: str, coord: List[int]) -> List[int]:
    """Pin lobe 8 vs 10 by marker counts when coord is already 8 or 10."""
    if not coord or coord[0] not in (8, 10) or not text:
        return coord
    a, p = _marker_counts(text.lower())
    if p > a:
        target = 10
    elif a > p:
        target = 8
    else:
        return coord
    if target == coord[0]:
        return coord
    return [target] + ([0] + coord[2:] if len(coord) > 1 else [0])
