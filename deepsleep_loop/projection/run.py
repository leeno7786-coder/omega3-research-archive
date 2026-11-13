"""DeepSleep Phase 9 — read-only prompt projection from DeepSleep stores."""

from __future__ import annotations

from typing import Any

from deepsleep_loop._deepsleep_state import ProjectionState
from deepsleep_loop.identity_store import load_identity
from deepsleep_loop.mastery_store import load_mastery
from deepsleep_loop.projection_store import write_projection
from deepsleep_loop.trajectory_store import load_trajectory


def _identity_projection(identity_path: Any = None) -> dict:
    traits = load_identity(identity_path)
    ranked = sorted(
        traits.values(),
        key=lambda t: (float(t.strength), int(t.evidence_count)),
        reverse=True,
    )
    top = [
        {
            "name": trait.name,
            "strength": float(trait.strength),
            "evidence_count": int(trait.evidence_count),
        }
        for trait in ranked[:5]
    ]
    strengths = [float(t.strength) for t in ranked if float(t.strength) > 0.0]
    return {
        "traits": top,
        "strengths": strengths,
    }


def _mastery_projection(mastery_path: Any = None) -> dict:
    records = load_mastery(mastery_path)
    out: dict[str, dict[str, float | str]] = {}
    for domain_id, record in records.items():
        out[domain_id] = {
            "domain_name": record.domain_name,
            "knowledge": float(record.knowledge_mastery),
            "application": float(record.application_mastery),
            "status": record.status,
        }
    return out


def _trajectory_projection(trajectory_path: Any = None) -> dict:
    records = load_trajectory(trajectory_path)
    active = [record for record in records.values() if record.status == "active"]
    if not active:
        return {}
    best = max(active, key=lambda r: (float(r.confidence), int(r.reinforcement_count)))
    return {
        "target_domain": best.target_domain,
        "target_capability": best.target_capability,
        "confidence": float(best.confidence),
        "status": best.status,
    }


def build_projection(
    *,
    identity_path: Any = None,
    mastery_path: Any = None,
    trajectory_path: Any = None,
    projection_path: Any = None,
    persist: bool = True,
) -> ProjectionState:
    """Build read-only ProjectionState from identity/mastery/trajectory stores."""
    state = ProjectionState(
        identity_projection=_identity_projection(identity_path),
        mastery_projection=_mastery_projection(mastery_path),
        trajectory_projection=_trajectory_projection(trajectory_path),
    )
    if persist:
        write_projection(state, path=projection_path)
    return state
