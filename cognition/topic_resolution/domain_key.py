"""Canonical domain keys shared by topic resolution and failure consolidation."""

from __future__ import annotations

import re

from knowledge_extractor.derive.run import _slug

_LEARN_PREFIXES = (
    "learn ",
    "study ",
    "teach me ",
    "research ",
    "explore ",
    "about ",
)

_QUESTION_PATTERNS = (
    re.compile(
        r"^how (?:do|does|can|could|would|will) (.+?) (?:work|function|operate)\??$",
        re.I,
    ),
    re.compile(r"^what (?:is|are) (.+?)\??$", re.I),
    re.compile(r"^explain (.+?)\??$", re.I),
    re.compile(r"^tell me about (.+?)\??$", re.I),
    re.compile(r"^why (?:do|does|can|could) (.+?)(?: occur| happen| emerge)?\??$", re.I),
)

_LEARN_RATIONALE_SPLIT = re.compile(
    r"\s*[,;]\s+(?=(?:it|this|that|which)\s+(?:is|are|was|were)\b)",
    re.I,
)


def _strip_learn_prefixes(text: str) -> str:
    cleaned = str(text or "").strip()
    lowered = cleaned.lower()
    for prefix in _LEARN_PREFIXES:
        if lowered.startswith(prefix):
            rest = cleaned[len(prefix) :].strip().rstrip(".!?")
            if rest:
                return _LEARN_RATIONALE_SPLIT.split(rest, maxsplit=1)[0].strip()
    return cleaned


def extract_topic_from_text(text: str) -> str:
    cleaned = _strip_learn_prefixes(text).strip().rstrip(".!?")
    if not cleaned:
        return ""
    for pattern in _QUESTION_PATTERNS:
        match = pattern.match(cleaned)
        if match:
            topic = match.group(1).strip().rstrip(".!?")
            if topic:
                return topic
    return cleaned


def canonical_domain_key(text: str) -> str:
    """Stable mastery/failure domain key — scoped 'domain: question' uses the domain head."""
    cleaned = str(text or "").strip()
    if ": " in cleaned:
        head, _ = cleaned.split(": ", 1)
        return _slug(head.strip())
    return _slug(extract_topic_from_text(cleaned))
