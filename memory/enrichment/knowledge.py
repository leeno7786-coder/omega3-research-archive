"""Knowledge enrichment — 5W1H entity extraction for lobe 5 (what/knowledge)."""

import logging
import re
from typing import Dict, Optional, List

log = logging.getLogger("omega.memory.enrichment.knowledge")

_WHO = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\b")
_WHEN = re.compile(
    r"\b(?:today|yesterday|tomorrow|last\s+\w+|next\s+\w+|\d{4}[-/]\d{2}[-/]\d{2}|\d{4})\b", re.I
)
_WHERE = re.compile(
    r"\b(?:[Ii]n|[Aa]t|[Oo]n|[Ff]rom|[Nn]ear)\s+(?:the\s+|a\s+|an\s+)?([A-Z][a-z]+(?:\s+(?:of\s+)?[A-Z][a-z]+)*)\b"
)

_STOPWORDS = {"The", "This", "That", "These", "Those"}


def enrich(text: str) -> Optional[Dict]:
    entities: List[Dict] = []

    for m in _WHO.finditer(text):
        name = m.group(1)
        if len(name) > 2 and name not in _STOPWORDS:
            entities.append({"name": name, "lobe": "WHO", "confidence": 0.7})

    for m in _WHEN.finditer(text):
        entities.append({"name": m.group(0), "lobe": "WHEN", "confidence": 0.8})

    for m in _WHERE.finditer(text):
        entities.append({"name": m.group(1), "lobe": "WHERE", "confidence": 0.7})

    if not entities:
        return None
    return {"decomposer": "knowledge", "kind": "knowledge", "entities": entities}
