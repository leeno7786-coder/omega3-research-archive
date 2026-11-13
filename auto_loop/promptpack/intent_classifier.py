"""Deterministic chat-intent classification.

The former SetFit HTTP classifier is gone. This module owns the one stable
speech-act label assigned at ingress; AUTO receives that label and never
reclassifies the turn.
"""

from __future__ import annotations

import re
from typing import Optional

from auto_loop.promptpack.question_shape import is_question_shaped as _is_question_shaped

ALLOWED_INTENTS = ("freeform", "reasoning", "question", "ack", "temporal", "command")

_ACK_RE = re.compile(
    r"^(?:yes|yep|yeah|ok(?:ay)?|sure|agreed|correct|right|go ahead|do it|sounds good|thanks?)\s*[.!]*$",
    re.IGNORECASE,
)
_REASONING_RE = re.compile(
    r"^(?:please\s+)?(?:reason(?:\s+over)?|analy[sz]e|compare|contrast|evaluate|"
    r"work\s+through|think\s+through|walk\s+me\s+through|help\s+me\s+reason|"
    r"explain\s+why|why\s+(?:does|do|did|is|are|would|could))\b",
    re.IGNORECASE,
)
_CONNECTOR_COMMAND_RE = re.compile(
    r"\b(?:my|our)\s+(?:calendar|schedule|inbox|email|drive|tasks?)\b|"
    r"\b(?:remind\s+me|schedule|reschedule|cancel\s+(?:my|the)\s+(?:meeting|appointment)|"
    r"send\s+(?:an?\s+)?email|email\s+\S+|add\b.{0,40}\bcalendar)\b",
    re.IGNORECASE,
)


def deterministic_chat_intent(text: str) -> Optional[str]:
    """Classify one speech-act shape without a model or runtime service."""
    clean = str(text or "").strip()
    if not clean:
        return None
    if _ACK_RE.fullmatch(clean):
        return "ack"
    if _CONNECTOR_COMMAND_RE.search(clean):
        return "command"
    try:
        from memory.temporal_range import looks_like_temporal_recall

        if looks_like_temporal_recall(clean):
            return "temporal"
    except Exception:
        pass
    if _REASONING_RE.search(clean):
        return "reasoning"
    if _is_question_shaped(clean):
        return "question"
    return "freeform"


def classify_chat_intent(text: str) -> Optional[str]:
    """Public ingress classifier; intentionally the deterministic classifier."""
    return deterministic_chat_intent(text)


def reset_for_tests() -> None:
    """The classifier is stateless."""
    return None
