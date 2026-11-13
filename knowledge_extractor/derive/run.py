"""Derive phase — deterministic extraction to KnowledgeArtifact fields (no models)."""

from __future__ import annotations

import hashlib
import re
from typing import Any
from urllib.parse import urlparse

# Extension point: a model-driven extractor may replace heuristics below in a later slice.
# Do NOT call any model from this module.

_STOPWORDS = frozenset(
    """
    a an the and or but in on at to for of is are was were be been being
    it its this that these those with from by as if not no so than then
    also into over under about after before during while when where which
    who whom whose what how why can could will would should may might must
    have has had do does did done being been am is are was were
    """.split()
)

_VERB_HINTS = frozenset(
    """
    is are was were be been being am have has had do does did can could
    will would should may might must make makes made take takes took give
    gives gave use uses used work works worked include includes included
    provide provides provided require requires required support supports
    learn learns learned explain explains explained describe describes
    """.split()
)

_SOURCE_KIND_WEIGHT: dict[str, float] = {
    "web_result": 0.2,
    "file": 0.15,
    "pdf": 0.15,
    "video_transcript": 0.18,
    "tool_output": 0.12,
}

_TOP_CONCEPTS = 8
_TOP_FACTS = 5
_MIN_FACT_CHARS = 40
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-']*")


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower().strip())
    return slug.strip("-") or "general"


def _domain_from_normalized(normalized: dict[str, Any]) -> str:
    meta = normalized.get("meta") or {}
    if isinstance(meta, dict):
        topic = meta.get("topic") or meta.get("domain")
        if isinstance(topic, str) and topic.strip():
            return _slug(topic)

    source_ref = normalized.get("source_ref")
    if isinstance(source_ref, str) and source_ref.strip():
        ref = source_ref.strip()
        if "://" in ref:
            parsed = urlparse(ref)
            host = (parsed.hostname or parsed.netloc or "").strip()
            if host:
                return _slug(host.split(".")[0] if "." in host else host)
        return _slug(ref.rsplit("/", 1)[-1] or ref)

    text = str(normalized.get("text") or "")
    for token in _TOKEN_RE.findall(text):
        low = token.lower()
        if len(low) >= 4 and low not in _STOPWORDS:
            return _slug(low)
    return "general"


def _salient_concepts(text: str) -> tuple[str, ...]:
    counts: dict[str, int] = {}
    for match in _TOKEN_RE.finditer(text):
        raw = match.group(0)
        low = raw.lower()
        if low in _STOPWORDS or len(low) < 3:
            continue
        is_salient = raw[0].isupper() or (len(low) >= 5 and low not in _VERB_HINTS)
        if not is_salient:
            continue
        key = low
        counts[key] = counts.get(key, 0) + 1

    ranked = sorted(
        counts.items(),
        key=lambda item: (-item[1], item[0]),
    )
    return tuple(word for word, _ in ranked[:_TOP_CONCEPTS])


def _has_verb(sentence: str) -> bool:
    for token in _TOKEN_RE.findall(sentence):
        low = token.lower()
        if low in _VERB_HINTS:
            return True
        if low.endswith(("ed", "ing", "es", "s")) and len(low) > 4:
            return True
    return False


def _declarative_facts(text: str) -> tuple[str, ...]:
    parts = _SENTENCE_SPLIT_RE.split(text.strip())
    facts: list[str] = []
    for part in parts:
        sentence = " ".join(part.split())
        if len(sentence) < _MIN_FACT_CHARS:
            continue
        if not _has_verb(sentence):
            continue
        facts.append(sentence)
    return tuple(facts[:_TOP_FACTS])


def _co_occurrence_relationships(
    text: str,
    concepts: tuple[str, ...],
) -> tuple[dict, ...]:
    if len(concepts) < 2:
        return ()
    concept_set = set(concepts)
    rels: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for part in _SENTENCE_SPLIT_RE.split(text):
        present = sorted({c for c in concept_set if re.search(rf"\b{re.escape(c)}\b", part, re.I)})
        for i, subj in enumerate(present):
            for obj in present[i + 1 :]:
                key = (subj, obj)
                if key in seen:
                    continue
                seen.add(key)
                rels.append(
                    {
                        "subject": subj,
                        "relation": "co_occurs",
                        "object": obj,
                    }
                )
    return tuple(rels)


def _mastery_tags(domain: str, source_kind: str) -> tuple[str, ...]:
    kind = (source_kind or "unknown").strip().lower() or "unknown"
    return (domain, f"kind:{kind}")


def _confidence(
    *,
    facts: tuple[str, ...],
    concepts: tuple[str, ...],
    title: str | None,
    source_kind: str,
) -> float:
    """Deterministic heuristic: min(1.0, facts*0.1 + concepts*0.05 + title*0.1 + kind_weight)."""
    has_title = 0.1 if title else 0.0
    kind_weight = _SOURCE_KIND_WEIGHT.get((source_kind or "").lower(), 0.1)
    raw = len(facts) * 0.1 + len(concepts) * 0.05 + has_title + kind_weight
    return min(1.0, round(raw, 4))


def _artifact_id(domain: str, source_ref: str | None, text: str) -> str:
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    key = f"{domain}|{source_ref or ''}|{content_hash}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def run(normalized: dict[str, Any]) -> dict[str, Any]:
    text = str(normalized.get("text") or "")
    title = normalized.get("title")
    source_ref = normalized.get("source_ref")
    source_kind = str(normalized.get("source_kind") or "")

    domain = _domain_from_normalized(normalized)
    concepts = _salient_concepts(text)
    facts = _declarative_facts(text)
    relationships = _co_occurrence_relationships(text, concepts)
    mastery_tags = _mastery_tags(domain, source_kind)
    confidence = _confidence(
        facts=facts,
        concepts=concepts,
        title=title if isinstance(title, str) else None,
        source_kind=source_kind,
    )
    artifact_id = _artifact_id(
        domain,
        source_ref if isinstance(source_ref, str) else None,
        text,
    )

    return {
        "domain": domain,
        "concepts": concepts,
        "facts": facts,
        "relationships": relationships,
        "mastery_tags": mastery_tags,
        "confidence": confidence,
        "artifact_id": artifact_id,
    }
