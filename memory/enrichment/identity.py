"""Identity enrichment — detects self-referential statements about Omega."""

import logging
import re
from typing import Dict, Optional

log = logging.getLogger("omega.memory.enrichment.identity")

_IDENTITY_PATTERNS = [
    (r"\b(?:you are|you're|your name is)\b", "identity_statement"),
    (r"\b(?:your purpose|your role|your function)\b", "purpose_statement"),
    (r"\b(?:created by|made by|built by)\b", "creator_statement"),
]


def enrich(text: str) -> Optional[Dict]:
    text_lower = text.lower()
    matches = []
    for pattern, kind in _IDENTITY_PATTERNS:
        if re.search(pattern, text_lower):
            matches.append(kind)
    if not matches:
        return None
    return {
        "decomposer": "identity",
        "kind": "identity",
        "matches": matches,
        "speaker": "external",
    }
