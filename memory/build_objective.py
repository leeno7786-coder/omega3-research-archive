"""Episode-scoped objective selected before an autonomous builder dispatch."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from memory import episode_sram
from memory.api import CanonicalEnvelope


@dataclass(frozen=True)
class BuildObjective:
    episode_id: str
    objective: str
    origin_opened_by: str
    practice_domain: str = ""

    def __post_init__(self) -> None:
        for field_name, limit in (
            ("episode_id", 200),
            ("objective", 4000),
            ("origin_opened_by", 200),
            ("practice_domain", 500),
        ):
            object.__setattr__(
                self,
                field_name,
                str(getattr(self, field_name) or "").strip()[:limit],
            )
        if not self.episode_id:
            raise ValueError("build objective episode_id is required")
        if not self.objective:
            raise ValueError("build objective text is required")
        if not self.origin_opened_by:
            raise ValueError("build objective origin_opened_by is required")

    def to_payload(self) -> dict[str, str]:
        return {
            "episode_id": self.episode_id,
            "objective": self.objective,
            "origin_opened_by": self.origin_opened_by,
            "practice_domain": self.practice_domain,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "BuildObjective":
        return cls(
            episode_id=str(payload.get("episode_id") or ""),
            objective=str(payload.get("objective") or ""),
            origin_opened_by=str(payload.get("origin_opened_by") or ""),
            practice_domain=str(payload.get("practice_domain") or ""),
        )


def hold_build_objective(objective: BuildObjective) -> str:
    if not isinstance(objective, BuildObjective):
        raise TypeError("objective must be a BuildObjective")
    return episode_sram.hold_memory(
        CanonicalEnvelope(
            cycle_id=objective.episode_id,
            coord=[11, 2, 0],
            type="build_objective",
            payload=objective.to_payload(),
            source="memory.build_objective",
            confidence=1.0,
            provenance=["omega", "episode_sram"],
            decay_policy="none",
            permissions={"read": True, "write": True},
            strength_index=1.0,
        ),
        propose=False,
        bypass_guardian=True,
        skip_embed=False,
    )


def current_build_objective() -> BuildObjective | None:
    for item in reversed(episode_sram.pending()):
        envelope = getattr(item, "envelope", None)
        if item.kind != "memory" or getattr(envelope, "type", None) != "build_objective":
            continue
        payload = getattr(envelope, "payload", None)
        if not isinstance(payload, Mapping):
            raise RuntimeError("build_objective SRAM envelope has no payload mapping")
        return BuildObjective.from_payload(payload)
    return None
