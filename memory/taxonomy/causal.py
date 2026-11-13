"""The vocabulary of typed causal edges (cross-lane spec section 9, T11).

Lives in the taxonomy package because the extractor may depend on nothing else: the edge
kinds it projects and the host's reference kinds are graph vocabulary, not cognition.
"""

from __future__ import annotations

EDGE_KINDS = frozenset({"informed", "modified", "observed", "continued", "superseded"})
REFERENCE_KINDS = frozenset({"episode", "continuation", "mail", "intake", "memory", "window"})


def reference_kind(reference: str) -> str:
    """The kind prefix of a host reference (``episode:ep-1`` → ``episode``)."""
    return str(reference).partition(":")[0]


__all__ = ["EDGE_KINDS", "REFERENCE_KINDS", "reference_kind"]
