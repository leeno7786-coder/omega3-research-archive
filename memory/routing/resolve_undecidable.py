"""Phase 6 — advisory LFM seam for _unsorted holding items (default no-op)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

UNDECIDABLE: None = None


@dataclass(frozen=True)
class UndecidableSuggestion:
    """Advisory subdomain proposal for an _unsorted record."""

    lobe: int
    subdomain_y: int
    label: str
    confidence: float
    rationale: str = ""


def resolve_undecidable(
    text: str,
    context: Optional[Dict[str, Any]] = None,
) -> Optional[UndecidableSuggestion]:
    """Suggest a subdomain for an _unsorted item, or UNDECIDABLE.

    Production default is ``UNDECIDABLE`` (no-op). Future LFM finetune
    (Phase 8, data-driven) may return ``UndecidableSuggestion``; callers
    must validate against ``subdomain_for`` / the dynamic-Y allocator
    before re-homing a record.
    """
    _ = (text, context)
    return UNDECIDABLE
