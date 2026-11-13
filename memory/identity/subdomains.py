"""000_identity subdomain coordinates — constitution + traits only for operator recall."""

from __future__ import annotations

from typing import List

# Operator-facing identity lobe has two subcategories:
#   Y=0 constitution — founding doctrine, creator anchor, immutable self-model
#   Y=1 traits       — promoted patterns with repeated independent evidence
CONSTITUTION_Y = 0
TRAITS_Y = 1
# Pre-2026-07 migration: traits landed at Y=2 (emergent_traits). Reads still scan both.
LEGACY_TRAITS_Y = 2
# Y=3 evolution also stores unpromoted trait observations. It is deliberately
# absent from identity_recall_subdomains until those observations form a pattern.


def identity_recall_subdomains(*, include_legacy: bool = True) -> List[int]:
    """Subdomains pulled by freeform + question chat identity grounding."""
    subs = [CONSTITUTION_Y, TRAITS_Y]
    if include_legacy:
        subs.append(LEGACY_TRAITS_Y)
    return subs


def is_traits_subdomain(y: int) -> bool:
    return int(y) in (TRAITS_Y, LEGACY_TRAITS_Y)
