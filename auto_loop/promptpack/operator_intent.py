"""Deterministic workspace-inspection recognition inside the agent lane.

Routing an operator turn to the chat, agent, browser or document lane is Global A's
intent router (``global_loop.intent.route``). This module only recognizes, inside a
turn already routed to the agent lane, the narrow read-only directory inspection that
Meta A grounds with the trusted-folder tool instead of conversational recall.
"""

from __future__ import annotations

import re

# A small, high-confidence read-only capability floor.  These phrases ask for
# an observation of the operator's already-authorized folder; they must not be
# left to the answer model, because a recalled chat turn can otherwise look
# like a valid response while no workspace tool ran.
_WORKSPACE_LOCATION = (
    r"(?:the\s+)?(?:(?:current\s+working|current|working)\s+)?"
    r"(?:directory|folder|workspace)"
)
_WORKSPACE_INSPECTION_RE = re.compile(
    r"^\s*(?:(?:please\s+)?(?:can\s+you\s+)?"
    r"(?:inspect|review|list|show|display|view|check)\s+"
    + _WORKSPACE_LOCATION
    + r"(?:\s+(?:contents?|files?))?"
    r"(?:\s+and\s+(?:tell|show)\s+me\s+what\s+(?:shows?|is)\s+up)?|"
    r"(?:please\s+)?(?:list|show|display)\s+(?:the\s+)?"
    r"(?:files?|contents?)\s+(?:in|of)\s+"
    + _WORKSPACE_LOCATION
    + r"|(?:what(?:'s| is)\s+in)\s+"
    + _WORKSPACE_LOCATION
    + r")\s*[?.!]*\s*$",
    re.IGNORECASE,
)


def is_workspace_inspection_request(text: str) -> bool:
    """Recognize only unambiguous read-only directory inspection requests."""
    clean = " ".join(str(text or "").split())
    return bool(clean and _WORKSPACE_INSPECTION_RE.fullmatch(clean))


__all__ = ["is_workspace_inspection_request"]
