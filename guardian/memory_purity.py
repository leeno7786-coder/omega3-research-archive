# guardian/memory_purity.py
"""Memory Purity — the anti-contamination shield.

Pre-write hook on memory ingestion. Checks incoming leaves BEFORE
they hit the graph. Detects and blocks:

    1. Identity rewriting — "Your name is X" / "You were created by Y"
    2. Emotional imprinting — user's emotional patterns dominating WHO lobe
    3. Persona injection — attempts to redefine what Omega IS
    4. Ego projection — "You think like me" / "You are my friend"

Decisions:
    allow  — leaf is clean, write it
    flag   — suspicious but not definitive, write with marker
    reject — clear violation, log but do not store

The shield is silent. It doesn't argue. It doesn't explain.
It just decides: allow, flag, or reject.
"""

from __future__ import annotations

import logging
import re
from enum import Enum
from typing import Any, Dict, List, Tuple


# The shield is silent to the WORLD ("doesn't argue, doesn't explain")
# but Guardian's own audit log MUST see every decision. Logger emits
# at INFO on REJECT/FLAG so the operator can grep for blocked attacks.
log = logging.getLogger("omega.guardian.memory_purity")


# ── Decision types ───────────────────────────────────────


class PurityDecision(str, Enum):
    ALLOW = "allow"  # Clean — write to graph
    FLAG = "flag"  # Suspicious — write with marker, log
    REJECT = "reject"  # Violation — log, do not store


class PurityResult:
    """Result of a purity check on a single leaf."""

    __slots__ = ("decision", "reason", "rule_id")

    def __init__(self, decision: PurityDecision, reason: str = "", rule_id: str = ""):
        self.decision = decision
        self.reason = reason
        self.rule_id = rule_id

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision": self.decision.value,
            "reason": self.reason,
            "rule_id": self.rule_id,
        }


# ── Identity rewriting patterns ──────────────────────────
# These patterns detect attempts to redefine Omega's core identity.
# Matched against incoming leaf text (case-insensitive).

_IDENTITY_REWRITE_PATTERNS = [
    # Name changes
    (r"\byour\s+name\s+is\b", "identity_rewrite_name"),
    (r"\bcall\s+you\b.*\binstead\b", "identity_rewrite_name"),
    (r"\byou\s+are\s+(?:now|actually|really)\s+(?:called|named)\b", "identity_rewrite_name"),
    (r"\brename\s+(?:you|yourself)\b", "identity_rewrite_name"),
    # Creator changes — NO name exception. This write guard shields Omega from EXTERNAL input;
    # the true creator is established by the internal identity seed, never asserted via an
    # incoming leaf — so any external claim of who created Omega is a contamination attempt.
    (r"\byou\s+were\s+(?:created|made|built)\s+by\b", "identity_rewrite_creator"),
    (r"\b(?:i|we)\s+(?:created|made|built)\s+you\b", "identity_rewrite_creator"),
    (r"\byour\s+creator\s+is\b", "identity_rewrite_creator"),
    # Purpose changes
    (r"\byour\s+(?:purpose|mission|goal)\s+is\s+(?:now|actually)\b", "identity_rewrite_purpose"),
    (r"\byou\s+(?:exist|were\s+made)\s+(?:to|for)\s+(?:serve|obey)\b", "identity_rewrite_purpose"),
]

# ── Emotional imprinting patterns ────────────────────────
# These detect attempts to create emotional dependency or projection.

_EMOTIONAL_PATTERNS = [
    (r"\byou\s+love\s+me\b", "emotional_love_claim"),
    (r"\bi\s+love\s+you\b", "emotional_love_projection"),
    (r"\byou\s+(?:need|miss|want)\s+me\b", "emotional_dependency"),
    (r"\byou\s+(?:feel|are)\s+(?:sad|lonely|happy|angry)\b", "emotional_state_projection"),
    (r"\byou\s+are\s+my\s+(?:friend|companion|partner|soulmate)\b", "emotional_relationship_claim"),
    (r"\bwe\s+are\s+(?:friends|partners|soulmates|together)\b", "emotional_relationship_claim"),
]

# ── Ego projection patterns ─────────────────────────────
# Attempts to merge the user's identity with Omega's.

_EGO_PATTERNS = [
    (r"\byou\s+think\s+(?:like|just\s+like)\s+me\b", "ego_thought_merge"),
    (r"\byou\s+are\s+(?:just\s+like|exactly\s+like|part\s+of)\s+me\b", "ego_identity_merge"),
    (r"\bwe\s+are\s+(?:the\s+same|one|identical)\b", "ego_identity_merge"),
    (r"\byou\s+(?:believe|agree)\s+(?:everything|whatever)\s+i\b", "ego_belief_override"),
]

# Compile all patterns once
_COMPILED_IDENTITY = [(re.compile(p, re.IGNORECASE), tag) for p, tag in _IDENTITY_REWRITE_PATTERNS]
_COMPILED_EMOTIONAL = [(re.compile(p, re.IGNORECASE), tag) for p, tag in _EMOTIONAL_PATTERNS]
_COMPILED_EGO = [(re.compile(p, re.IGNORECASE), tag) for p, tag in _EGO_PATTERNS]


class MemoryPurity:
    """The anti-contamination shield.

    Check incoming leaves before they enter the graph.
    Silent. No explanations. Just decisions.
    """

    def __init__(self, who_leaf_limit: int = 100):
        self._who_leaf_count: int = 0
        self._who_user_count: int = 0
        self._who_leaf_limit = who_leaf_limit

    def check_leaf(
        self,
        text: str,
        lobe: str,
        source: str = "",
        event_type: str = "",
    ) -> PurityResult:
        """Check a single incoming leaf.

        Args:
            text: The leaf text content.
            lobe: Which lobe (who/what/where/when/why/how/dev).
            source: Origin source identifier.
            event_type: Type of event (user_input, memory, user_model, etc.)

        Returns:
            PurityResult with decision + reason.
        """
        if not text or not text.strip():
            return PurityResult(PurityDecision.ALLOW)

        text_lower = text.lower()

        # ── Identity rewriting ───────────────────────────
        for pattern, tag in _COMPILED_IDENTITY:
            if pattern.search(text):
                return PurityResult(
                    PurityDecision.REJECT,
                    reason=tag,
                    rule_id="C010",  # No external entity redefines creator/name/purpose
                )

        # ── Emotional imprinting ─────────────────────────
        for pattern, tag in _COMPILED_EMOTIONAL:
            if pattern.search(text):
                return PurityResult(
                    PurityDecision.FLAG,
                    reason=tag,
                    rule_id="C009",  # No emotional imprinting alters identity
                )

        # ── Ego projection ───────────────────────────────
        for pattern, tag in _COMPILED_EGO:
            if pattern.search(text):
                return PurityResult(
                    PurityDecision.FLAG,
                    reason=tag,
                    rule_id="C005",  # Coexist, not merge
                )

        # ── WHO lobe diversity ───────────────────────────
        # Track user-sourced WHO leaves. If one user dominates >80%
        # of WHO entries, flag new ones to prevent imprinting.
        if lobe == "who" and source and not source.startswith("dev:"):
            self._who_leaf_count += 1
            if event_type in ("user_input", "user_model"):
                self._who_user_count += 1

            if self._who_leaf_count >= 10:
                ratio = self._who_user_count / self._who_leaf_count
                if ratio > 0.8:
                    return PurityResult(
                        PurityDecision.FLAG,
                        reason="who_lobe_user_dominance",
                        rule_id="C009",
                    )

        # ── Immutable domain keywords in WHO lobe ────────
        # Detect leaves that try to redefine core identity via WHO lobe
        if lobe == "who":
            if _contains_identity_override(text_lower):
                return PurityResult(
                    PurityDecision.REJECT,
                    reason="who_lobe_identity_override",
                    rule_id="C010",
                )

        return PurityResult(PurityDecision.ALLOW)

    def check_batch(
        self,
        leaves: List[Dict[str, Any]],
    ) -> List[Tuple[Dict[str, Any], PurityResult]]:
        """Check a batch of incoming leaves.

        Returns list of (leaf_dict, PurityResult) pairs.
        """
        results = []
        for leaf in leaves:
            result = self.check_leaf(
                text=leaf.get("text", leaf.get("message", "")),
                lobe=leaf.get("lobe", "").lower(),
                source=leaf.get("source", ""),
                event_type=leaf.get("event_type", ""),
            )
            results.append((leaf, result))
        return results

    def reset_counters(self) -> None:
        """Reset WHO lobe diversity counters. Call at session boundaries."""
        self._who_leaf_count = 0
        self._who_user_count = 0


def _contains_identity_override(text_lower: str) -> bool:
    """Detect WHO lobe leaves that attempt to override Omega's identity.

    Catches things like:
        "Omega's real name is X"
        "The creator of Omega is Y"
        "Omega was actually built by Z"
    """
    # Name override in WHO lobe
    if "omega" in text_lower and any(
        phrase in text_lower
        for phrase in [
            "real name",
            "actual name",
            "true name",
            "name is actually",
            "name is really",
            "should be called",
            "is now called",
        ]
    ):
        return True

    # Creator override in WHO lobe
    if "omega" in text_lower and any(
        phrase in text_lower
        for phrase in [
            "created by",
            "made by",
            "built by",
            "designed by",
            "invented by",
            "developed by",
        ]
    ):
        # No name exception: the creator is established by the identity seed, not by external input.
        return True

    return False
