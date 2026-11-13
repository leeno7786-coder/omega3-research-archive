"""Situational enrichment (lobe 4) — full event recollection.

Per `project_memory_lobe_restructure` (2026-05-06): every meta choice,
auto attempt, and global outcome lands in lobe 4 as a structured
situational record so the system has cortex-level recollection of its
OWN tasks/choices/feelings for feedback. The schema captures all 8
facets (5W1H + outcome + sentiment) plus success/failure/intensity +
links to other lobes.

This module is a deterministic decomposer — pattern-based, no LLM.
Returns None when the input doesn't carry enough structure to build a
meaningful situational record (graceful degradation, per the living-
contract doctrine: absence of signal IS data).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from memory.dewey_map import SITUATIONAL_FACETS

log = logging.getLogger("omega.memory.enrichment.situational")


# ── Facet detection cues ──
# A loose lexicon mapping surface words to one of the 8 facets. The
# enricher uses these to pick the dominant facet when the caller
# hasn't already named one (operator-driven writes can pre-pick).
_FACET_CUES: Dict[str, str] = {
    # who
    "who": "who",
    "whom": "who",
    "person": "who",
    "people": "who",
    "operator": "who",
    "user": "who",
    # what
    "what": "what",
    "task": "what",
    "action": "what",
    "did": "what",
    "happened": "what",
    "event": "what",
    # when
    "when": "when",
    "time": "when",
    "epoch": "when",
    "cycle": "when",
    "before": "when",
    "after": "when",
    "during": "when",
    # where
    "where": "where",
    "location": "where",
    "lobe": "where",
    "module": "where",
    "file": "where",
    "channel": "where",
    # why
    "why": "why",
    "because": "why",
    "reason": "why",
    "motivation": "why",
    "goal": "why",
    "intent": "why",
    # how
    "how": "how",
    "method": "how",
    "via": "how",
    "through": "how",
    "approach": "how",
    "tool": "how",
    "step": "how",
    # outcome
    "outcome": "outcome",
    "result": "outcome",
    "succeeded": "outcome",
    "failed": "outcome",
    "produced": "outcome",
    "delivered": "outcome",
    # sentiment
    "felt": "sentiment",
    "feeling": "sentiment",
    "anxious": "sentiment",
    "calm": "sentiment",
    "frustrated": "sentiment",
    "confident": "sentiment",
    "uncertain": "sentiment",
    "curious": "sentiment",
}


_SUCCESS_CUES = ("succeeded", "satisfied", "completed", "ok", "delivered", "done")
_FAILURE_CUES = ("failed", "error", "crashed", "rejected", "blocked", "stuck", "timeout")

_SENTIMENT_LEXICON: Dict[str, str] = {
    "calm": "calm",
    "stable": "calm",
    "anxious": "anxious",
    "stressed": "anxious",
    "tense": "anxious",
    "frustrated": "frustrated",
    "stuck": "frustrated",
    "confident": "confident",
    "satisfied": "confident",
    "uncertain": "uncertain",
    "ambiguous": "uncertain",
    "curious": "curious",
    "interested": "curious",
    "tired": "tired",
    "drained": "tired",
}


def _pick_facet(text_lo: str, hint: Optional[str]) -> Optional[str]:
    """Choose the dominant facet for this entry. Caller hint wins if
    valid; otherwise scan the lexicon and pick the most-frequent
    matching cue. Returns None when no signal at all."""
    if hint and hint in SITUATIONAL_FACETS:
        return hint
    counts: Dict[str, int] = {}
    for cue, facet in _FACET_CUES.items():
        if re.search(rf"\b{cue}\b", text_lo):
            counts[facet] = counts.get(facet, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


def _pick_success(text_lo: str, hint: Optional[bool]) -> Optional[bool]:
    """Trinary: True if explicit success cue, False if explicit failure
    cue, None if neither (genuinely undetermined)."""
    if hint is not None:
        return bool(hint)
    has_success = any(re.search(rf"\b{re.escape(c)}\b", text_lo) for c in _SUCCESS_CUES)
    has_failure = any(re.search(rf"\b{re.escape(c)}\b", text_lo) for c in _FAILURE_CUES)
    if has_failure and not has_success:
        return False
    if has_success and not has_failure:
        return True
    return None


def _pick_sentiment(text_lo: str, hint: Optional[str]) -> str:
    """Pick sentiment from the lexicon; default to 'neutral'."""
    if hint:
        return str(hint)
    for word, sent in _SENTIMENT_LEXICON.items():
        if re.search(rf"\b{word}\b", text_lo):
            return sent
    return "neutral"


def _extract_failure_reason(text: str, success: Optional[bool]) -> Optional[str]:
    """Pull a short reason string when success is explicitly False.
    Looks for `because <X>` / `due to <X>` / `failed: <X>` patterns."""
    if success is not False:
        return None
    for pat in (
        r"because\s+([^.\n]{1,200})",
        r"due to\s+([^.\n]{1,200})",
        r"failed[:\s-]+([^.\n]{1,200})",
        r"error[:\s-]+([^.\n]{1,200})",
    ):
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


def enrich(text: str, **kwargs: Any) -> Optional[Dict[str, Any]]:
    """Decompose a situational write into the full event-recollection
    schema. Caller may pass `facet`, `success`, `sentiment`,
    `linked_persons`, `linked_coding` as kwargs to override detection.
    """
    if not text or not isinstance(text, str):
        return None
    text_lo = text.lower()
    facet = _pick_facet(text_lo, kwargs.get("facet"))
    if facet is None:
        log.debug("situational: no facet signal in %r", text[:60])
        return None
    success = _pick_success(text_lo, kwargs.get("success"))
    sentiment = _pick_sentiment(text_lo, kwargs.get("sentiment"))
    failure_reason = _extract_failure_reason(text, success)
    intensity = float(kwargs.get("intensity", 0.5))
    linked_persons: List[str] = list(kwargs.get("linked_persons") or [])
    linked_coding: List[str] = list(kwargs.get("linked_coding") or [])

    return {
        "facet": facet,
        "event_summary": text[:240],
        "success": success,
        "failure_reason": failure_reason,
        "sentiment": sentiment,
        "intensity": max(0.0, min(1.0, intensity)),
        "linked_persons": linked_persons,
        "linked_coding": linked_coding,
    }


def enrich_situational(
    text: str,
    *,
    facet: str,
    success: Optional[bool] = None,
    failure_reason: Optional[str] = None,
    linked_persons: Optional[List[str]] = None,
    linked_coding: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Build a situational record with an EXPLICITLY PINNED facet (write-Y==read-Y).

    Thin wrapper over ``enrich``: the caller (canonical_capture) owns the facet
    deterministically, so the lexical facet-guesser is bypassed. An explicit
    ``failure_reason`` overrides the lexical ``_extract_failure_reason`` so a
    routed failure event keeps its real cause.
    """
    from memory.dewey_map import SITUATIONAL_FACETS

    if facet not in SITUATIONAL_FACETS:
        raise ValueError(f"unknown situational facet: {facet!r}")
    rec = enrich(
        text,
        facet=facet,
        success=success,
        linked_persons=list(linked_persons or []),
        linked_coding=list(linked_coding or []),
    )
    if rec is None:
        return None
    if failure_reason is not None:
        rec["failure_reason"] = str(failure_reason)
    return rec
