"""DeepSleep intake — build DeepSleepInput from WindowReflection."""

from __future__ import annotations

from typing import Any

from auto_loop.fold.run import screen_plugin_outputs
from deepsleep_loop._deepsleep_state import DeepSleepInput


def _screen_episode_body(value: Any) -> dict:
    body = dict(value) if isinstance(value, dict) else {}
    outputs = body.get("outputs")
    if isinstance(outputs, dict):
        body["outputs"] = screen_plugin_outputs(outputs, preserve_receipts=True)
    auto = body.get("auto")
    if isinstance(auto, dict) and isinstance(auto.get("outputs"), dict):
        auto = dict(auto)
        auto["outputs"] = screen_plugin_outputs(
            auto["outputs"],
            preserve_receipts=True,
        )
        body["auto"] = auto
    return body


def build_deepsleep_input_for_window(window: Any, omega_outcome: Any) -> DeepSleepInput:
    """Build window-level DeepSleep input from Omega WindowReflection.

    Fail-hard: this is the cognitive input the whole consolidation reflects over.
    A malformed window / failed episode-journal read is invalid brain state — it
    propagates and CRASHES rather than returning None and silently aborting
    DeepSleep with no signal."""
    window_id = str(getattr(window, "window_id", "") or "")
    snapshot = getattr(window, "trajectory_snapshot", None)
    trajectory_snapshot = dict(snapshot) if isinstance(snapshot, dict) else {}
    episode_window = [
        _screen_episode_body(b)
        for b in (getattr(window, "episode_bodies", None) or [])
        if isinstance(b, dict)
    ]

    outcome_dict: dict = {}
    if omega_outcome is not None:
        if hasattr(omega_outcome, "to_dict"):
            outcome_dict = dict(omega_outcome.to_dict())
        elif isinstance(omega_outcome, dict):
            outcome_dict = dict(omega_outcome)

    reflection = dict(outcome_dict.get("reflection") or {})
    return DeepSleepInput(
        window_id=window_id,
        window_reflection=outcome_dict,
        episode_window=episode_window,
        trajectory_snapshot=trajectory_snapshot,
        omega_reasoning=dict(reflection.get("reasoning") or {}),
    )


def build_deepsleep_input_for_episode(episode_body: dict, episode_id: str) -> DeepSleepInput:
    """Build per-episode DeepSleep input from a single enriched episode body."""
    body = _screen_episode_body(episode_body)
    return DeepSleepInput(
        window_id=str(episode_id),
        window_reflection={},
        episode_window=[body],
        trajectory_snapshot={},
        omega_reasoning={},
    )
