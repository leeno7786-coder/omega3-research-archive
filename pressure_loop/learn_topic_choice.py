"""Omega's chosen learning topic — the specific sub-aspect it picked to learn.

Operator directive 2026-07-08: whenever Omega decides to LEARN it also names the
SPECIFIC topic — not the broad domain. This is GENERAL to every mind-driven
learning path, not one feature: the idle awakening sets it, and every
per-episode intent_decision that names a concrete topic captures it too
(cognition_chain shadow-write). The choice is stashed here (one-shot,
process-local) and consumed by the LEARN topic resolver so the fetch query is
what Omega chose, not the domain overview fetched on repeat. Meta A then picks
the best API for that specific topic. (Operator-directed and onboarding LEARN
carry their own specific topics through their own resolvers — this channel is
for the mind's autonomous choices.)

One pending choice at a time: a decision picks a domain + specific topic, and
the LEARN it ignites consumes it. A stale choice self-clears on the next set.
"""

from __future__ import annotations

from typing import Optional

_pending: Optional[dict] = None


def set_chosen_topic(domain: str, topic: str) -> None:
    """Record Omega's chosen specific learning topic for a domain. No-op on
    empty; a specific topic that merely echoes the domain is still recorded
    (the resolver treats it as 'no more-specific choice')."""
    global _pending
    d = str(domain or "").strip()
    t = str(topic or "").strip()
    if not d or not t:
        return
    _pending = {"domain": d, "topic": t}


def peek_chosen_topic() -> Optional[dict]:
    return dict(_pending) if _pending else None


def take_chosen_topic() -> Optional[dict]:
    """Return and clear the pending choice (one-shot)."""
    global _pending
    v, _pending = _pending, None
    return v


def _reset_for_tests() -> None:
    global _pending
    _pending = None
