"""MemoryCitizenshipReceipt v2 — canonical validity separated from projection
freshness (memory-organ Cut 4).

v1 citizenship (00 §6, 12 §3 era) implied a memory was "active" only once it
was DME-addressable. 16 (INV-MO-018) supersedes that: canonical persistence
and DME projection run on different clocks. A canonical non-identity write is
ACTIVE the moment its canonical steps are complete — file committed, coord
index citizenship, StrengthIndex birth, reflection eligibility, projection
ELIGIBILITY (work exists or is recoverably discoverable). Whether the active
DME generation has assimilated it yet is ``projection_pending`` — reported
honestly, never folded into ``active_canonical``.

Identity continuity: native identity writes use ``not_applicable``. Dense
embedding and self-vector continuity are not part of the native citizenship
receipt; the identity commit law itself is unchanged here.

No I/O, no projection. Pure receipt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

MEMORY_CITIZENSHIP_RECEIPT_SCHEMA_VERSION = "omega.memory_citizenship_receipt.v2"

IDENTITY_CONTINUITY_STATUSES = frozenset(
    {"not_applicable", "pass", "abstain", "conflict"}
)
_ACTIVE_IDENTITY_STATUSES = frozenset({"not_applicable", "pass"})


@dataclass(frozen=True)
class MemoryCitizenshipReceipt:
    memory_id: str
    canonical_path: str
    canonical_committed: bool
    indexed: bool
    strength_registered: bool
    reflection_eligible: bool
    projection_eligible: bool
    projection_pending: bool
    projection_delta_id: str | None
    identity_continuity_status: str
    steps_completed: tuple[str, ...]
    schema_version: str = MEMORY_CITIZENSHIP_RECEIPT_SCHEMA_VERSION

    @property
    def active_canonical(self) -> bool:
        """Canonically active. Projection currentness is DELIBERATELY absent:
        a projection lag is legal and explicit (INV-MO-018), never a truth
        failure."""
        identity_ok = self.identity_continuity_status in _ACTIVE_IDENTITY_STATUSES
        return bool(
            self.canonical_committed
            and self.indexed
            and self.strength_registered
            and self.reflection_eligible
            and self.projection_eligible
            and identity_ok
        )

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not str(self.memory_id or "").strip():
            errors.append("missing memory_id")
        if not str(self.canonical_path or "").strip():
            errors.append("missing canonical_path")
        if self.identity_continuity_status not in IDENTITY_CONTINUITY_STATUSES:
            errors.append(f"unknown identity_continuity_status={self.identity_continuity_status!r}")
        if self.schema_version != MEMORY_CITIZENSHIP_RECEIPT_SCHEMA_VERSION:
            errors.append(f"unsupported schema_version={self.schema_version!r}")
        if self.projection_pending and not self.projection_eligible:
            errors.append("projection_pending implies projection_eligible")
        return errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "memory_citizenship_receipt",
            "memory_id": self.memory_id,
            "canonical_path": self.canonical_path,
            "canonical_committed": bool(self.canonical_committed),
            "indexed": bool(self.indexed),
            "strength_registered": bool(self.strength_registered),
            "reflection_eligible": bool(self.reflection_eligible),
            "projection_eligible": bool(self.projection_eligible),
            "projection_pending": bool(self.projection_pending),
            "projection_delta_id": self.projection_delta_id,
            "identity_continuity_status": self.identity_continuity_status,
            "steps_completed": list(self.steps_completed),
            "active_canonical": self.active_canonical,
            "schema_version": self.schema_version,
        }
