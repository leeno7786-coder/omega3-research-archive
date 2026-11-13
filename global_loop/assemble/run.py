"""Global B phase — assemble input from episode + Meta B reflection pack."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class GlobalBInput:
    episode_id: str
    pressure: dict
    source_action_goal: dict
    execution_pack: dict
    auto_outcome: dict
    meta_reflection: dict
    trajectory_snapshot: dict
    omega_lane: str | None = None
    request_context: dict = field(default_factory=dict)
    global_outcome: dict = field(default_factory=dict)
    # Window lane only: the compacted view of the locked window this episode carries.
    window_view: dict | None = None

    def __post_init__(self):
        from schema.omega_control_decision import OMEGA_LANES

        lane = self.source_action_goal.get("omega_lane", "chat")
        if lane not in OMEGA_LANES or self.omega_lane not in (None, lane):
            raise ValueError("Global B omega lane mismatch")
        for goal in (self.execution_pack.get("source_action_goal"),
                     self.auto_outcome.get("source_action_goal")):
            if isinstance(goal, Mapping) and goal.get("omega_lane", "chat") != lane:
                raise ValueError("Global B source omega lane mismatch")
        if (lane == "window") != (self.window_view is not None):
            raise ValueError("Global B window view requires the window lane")
        object.__setattr__(self, "omega_lane", lane)


def auto_inner(auto_outcome: Mapping[str, Any]) -> dict:
    inner = auto_outcome.get("auto")
    if isinstance(inner, Mapping):
        return dict(inner)
    return dict(auto_outcome)


def global_b_input_from_episode(
    episode,
    meta_reflection: Mapping[str, Any],
) -> Optional[GlobalBInput]:
    """Assemble GlobalBInput; None if episode.auto missing."""
    from meta_loop.meta_b import meta_b_input_from_episode

    mbi_data = meta_b_input_from_episode(episode)
    if mbi_data is None:
        return None
    pressure_for_traj = dict(getattr(episode, "pressure", None) or {})
    traj_raw = pressure_for_traj.get("trajectory_snapshot")
    trajectory_snapshot = dict(traj_raw) if isinstance(traj_raw, dict) else {}
    from omega_loop.episode.packet import capture_execution_request_context

    global_context = dict(getattr(episode, "global_", None) or {})
    workflow = global_context.get("browser_workflow")
    workflow = dict(workflow) if isinstance(workflow, Mapping) and workflow.get("objective") else None
    original_text = ""
    for row in pressure_for_traj.get("contributors") or ():
        if not isinstance(row, Mapping) or row.get("source") not in (None, "operator"):
            continue
        if (row.get("type") or row.get("id")) not in {
            "operator_query", "operator_command", "operator_response", "operator_message"
        }:
            continue
        detail = row.get("metadata") or row.get("detail") or {}
        original_text = str(detail.get("message") or detail.get("text") or "").strip()
        if original_text:
            break
    carried_objective = ""
    if not original_text:
        # A successor's contributor is Omega's own, not the operator's, so the loop above finds
        # no text; the objective it carries IS the operator's request. Without this the agent
        # continuation reached Omega with an empty objective and it asked the operator what the
        # task was, mid-build (live 2026-09-15). The browser lane is spared only because its
        # bound workflow holds the objective.
        for row in pressure_for_traj.get("contributors") or ():
            if not isinstance(row, Mapping):
                continue
            detail = row.get("metadata") or row.get("detail") or {}
            carried_objective = str(detail.get("original_objective") or "").strip()
            if carried_objective:
                break
    request = capture_execution_request_context(episode, request_text=(
        str(workflow["objective"]) if workflow is not None else (original_text or carried_objective)))
    # A follow-up instruction belongs to this turn. The bound workflow retains the
    # operator's original objective across successive operator episodes.
    request["current_instruction"] = original_text
    context = dict(request_context=request,
                   global_outcome=dict(global_context.get("outcome") or {}))
    from pressure_loop import window_lane

    window = window_lane.carried_window(episode)
    if window is not None:
        from omega_loop.window.run import build_window_view

        context["window_view"] = build_window_view(window)
    return GlobalBInput(
        **context,
        episode_id=mbi_data.episode_id,
        pressure=dict(mbi_data.pressure),
        source_action_goal=dict(mbi_data.source_action_goal),
        execution_pack=dict(mbi_data.execution_pack),
        auto_outcome=dict(mbi_data.auto_outcome),
        meta_reflection=dict(meta_reflection),
        trajectory_snapshot=trajectory_snapshot,
    )


def run(episode, meta_reflection: Mapping[str, Any]) -> Optional[GlobalBInput]:
    return global_b_input_from_episode(episode, meta_reflection)
