"""Meta A intake phase — episode → MetaAInput."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional



@dataclass(frozen=True)
class MetaAInput:
    episode_id: str
    pressure: dict
    trajectory_snapshot: dict
    action_goal: dict
    browser_workflow: dict | None = None
    # Global A's routed lane for an operator turn ("chat" | "agent" | "browser"); empty otherwise.
    operator_route: str = ""
    # The deterministic previous-action binding the router used for a follow-up.
    # Carry it beyond Global A so AUTO sees the same task context that selected the lane.
    operator_route_context: str = ""
    # The host-assigned conversation identity, including a newly rooted Chat episode.
    thread_id: str = ""


def _dominant_text(mi: MetaAInput) -> str:
    parts: list[str] = []
    ag = mi.action_goal or {}
    target = ag.get("target")
    if isinstance(target, dict):
        parts.append(str(target.get("question") or target.get("message") or ""))
    elif target is not None:
        parts.append(str(target))
    pressure = mi.pressure or {}
    for row in pressure.get("contributors") or []:
        if not isinstance(row, dict):
            continue
        meta = dict(row.get("metadata") or row.get("detail") or {})
        parts.append(str(meta.get("message") or meta.get("text") or ""))
    # DEDUPE (operator 2026-07-02): the action-goal target and the operator pressure
    # contributor carry the SAME request text for an operator build — joining both
    # doubled the objective everywhere downstream (stash, monitor prompt, ✅ notice).
    seen: set[str] = set()
    unique: list[str] = []
    for p in parts:
        p = p.strip()
        if p and p not in seen:
            seen.add(p)
            unique.append(p)
    return " ".join(unique)


def _contributors_metadata(mi: MetaAInput) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for row in (mi.pressure or {}).get("contributors") or []:
        if isinstance(row, dict):
            meta = dict(row.get("metadata") or row.get("detail") or {})
            out.update(meta)
    return out


def meta_a_input_from_episode(episode) -> Optional[MetaAInput]:
    """Assemble MetaAInput from a slice-1 episode; None if no intent decision."""
    global_ = dict(getattr(episode, "global_", None) or {})
    action_goal = dict(global_.get("intent_decision") or global_.get("action_goal") or {})
    if not action_goal:
        return None

    pressure_raw = dict(getattr(episode, "pressure", None) or {})
    opened_by = str(pressure_raw.get("opened_by") or getattr(episode, "opened_by", "") or "")
    contributors = list(pressure_raw.get("contributors") or [])
    traj = pressure_raw.get("trajectory_snapshot")
    if not isinstance(traj, dict):
        orient = dict(global_.get("orient") or {})
        traj_candidate = orient.get("trajectory")
        traj = dict(traj_candidate) if isinstance(traj_candidate, dict) else {}

    return MetaAInput(
        episode_id=str(getattr(episode, "episode_id", "") or ""),
        pressure={
            "reason": opened_by,
            "contributors": contributors,
            "aggregate_pressure": float(pressure_raw.get("aggregate_pressure", 0.0)),
            "neuromod_state": dict(pressure_raw.get("neuromod_state") or {}),
        },
        trajectory_snapshot=dict(traj),
        action_goal=action_goal,
        browser_workflow=global_.get("browser_workflow"),
        operator_route=str((global_.get("operator_route") or {}).get("lane") or ""),
        operator_route_context=str((global_.get("operator_route") or {}).get("context") or ""),
        thread_id=str(getattr(episode, "thread_id", "") or ""),
    )


def run(episode) -> Optional[MetaAInput]:
    return meta_a_input_from_episode(episode)
