"""Deterministic question-shape detector.

The recall-skip floor (Stage 0): decide "is this operator turn a question?" from the
text alone, so QUESTION-intent recall is grounded by a deterministic rule. Pure and
deterministic; no model or runtime flag is involved.
"""

from __future__ import annotations

import re

# Leading interrogatives (5W1H + common auxiliaries) that mark a question even with no '?'.
_INTERROGATIVE_LEAD = re.compile(
    r"^\s*(who|what|when|where|why|how|which|whose|whom|"
    r"is|are|am|do|does|did|can|could|will|would|should|shall|may|might)\b",
    re.I,
)


def is_question_shaped(text: str) -> bool:
    """True when ``text`` is a question by shape: ends with '?' or opens with an
    interrogative word. Deterministic; safe on empty/whitespace input."""
    t = str(text or "").strip()
    if not t:
        return False
    if t.endswith("?"):
        return True
    return bool(_INTERROGATIVE_LEAD.match(t))
