"""Phase 6 — self-driving continuation: Global B continuation pressure ignites the next cycle."""

from __future__ import annotations

import importlib

import pytest

from pressure_loop.contributors import EpisodeIgnition, make_contributor

_INTENT = importlib.import_module("global_loop.intent.run")


def _ignition(opened_by, msg=""):
    c = make_contributor("system", opened_by, 1.0, {"message": msg})
    return EpisodeIgnition(
        episode_id="e",
        aggregate_pressure=1.0,
        opened_by=opened_by,
        dominant_contributor=c,
        contributors=(c,),
        trajectory_snapshot=None,
    )


# ── intent routing: agentic execution + continuation route USE_SKILL when enabled ──
def test_gaps_and_continuation_route_use_skill_when_enabled(monkeypatch):
    monkeypatch.delenv("OMEGA_USE_SKILL_ROUTING", raising=False)
    monkeypatch.setattr(_INTENT, "OMEGA_USE_SKILL_ROUTING", True)
    # the routing MAP sends agentic gaps to USE_SKILL; the dispatchability guard
    # (test_use_skill_dispatchability_guard) then requires an objective before USE_SKILL
    # survives — mint one so this test isolates the ROUTING decision, not the downgrade.
    monkeypatch.setattr("pressure_loop.practice_mint.mint_for_prepared_apply", lambda: True)
    for ob in ("execution_gap", "application_gap", "execution_continuation"):
        assert _INTENT.run(_ignition(ob, "do the work")) == "USE_SKILL"


def test_gaps_fall_back_to_use_tool_when_disabled(monkeypatch):
    monkeypatch.delenv("OMEGA_USE_SKILL_ROUTING", raising=False)
    monkeypatch.setattr(_INTENT, "OMEGA_USE_SKILL_ROUTING", False)
    # kill-switch: APPLY is RETIRED, so the gaps fall back to USE_TOOL (no qwen dispatch)
    assert _INTENT.run(_ignition("execution_gap")) == "USE_TOOL"
    assert _INTENT.run(_ignition("application_gap")) == "USE_TOOL"


# Deterministic continuation staging REMOVED 2026-06-25 — the MIND owns continue/idle now
# (Omega reasons over the result + input and raises pressure_params, or idles). The tests for
# _stage_skill_continuation / _next_continuation_message / _MAX_CONTINUATION_DEPTH were deleted
# with the machinery. Intent ROUTING above (gap/continuation → USE_SKILL) stays: that's how the
# mind's RAISED pressure becomes an action. opened_by=execution_continuation still authorizes a
# prior-chained episode's bounded writes (below), independent of who staged it.
def test_execution_continuation_authorizes_bounded_writes(monkeypatch):
    """With autonomous execution DISABLED, only operator-opened and prior-chained
    continuation episodes authorize bounded writes — the original Phase-6 gate."""
    from auto_loop.skill.run import skill_dispatch_confirmed

    monkeypatch.setenv("OMEGA_AUTONOMOUS_SKILL_EXEC", "off")
    assert skill_dispatch_confirmed({"opened_by": "operator_command"}) is True
    assert (
        skill_dispatch_confirmed({"opened_by": "execution_continuation", "prior_episode": "ep-1"})
        is True
    )
    assert skill_dispatch_confirmed({"opened_by": "execution_continuation"}) is False
    assert skill_dispatch_confirmed({"opened_by": "learning_debt"}) is False


def test_autonomous_skill_exec_lets_the_mind_execute_in_the_jail(monkeypatch):
    """Operator doctrine 2026-06-25 'let the mind execute in the jail': with
    OMEGA_AUTONOMOUS_SKILL_EXEC explicitly ON, a self-driven episode authorizes its
    bounded writes. confirmed=True only satisfies the confirmation gate — the workspace
    jail, self-mod denylist, command policy, and forbidden-tool set still bound it."""
    from auto_loop.skill.run import skill_dispatch_confirmed

    # The production default is fail-closed.
    monkeypatch.delenv("OMEGA_AUTONOMOUS_SKILL_EXEC", raising=False)
    assert skill_dispatch_confirmed({"opened_by": "learning_debt"}) is False
    assert skill_dispatch_confirmed({"opened_by": "application_gap"}) is False
    # operator work is authorized regardless of the flag
    assert skill_dispatch_confirmed({"opened_by": "operator_command"}) is True

    monkeypatch.setenv("OMEGA_AUTONOMOUS_SKILL_EXEC", "on")
    assert skill_dispatch_confirmed({"opened_by": "curiosity_gap"}) is True


def test_global_apply_canonicalizes_to_use_skill(monkeypatch):
    monkeypatch.delenv("OMEGA_USE_SKILL_ROUTING", raising=False)
    monkeypatch.setattr(_INTENT, "OMEGA_USE_SKILL_ROUTING", True)
    assert _INTENT._canonical_intent("APPLY") == "USE_SKILL"
    assert _INTENT._canonical_intent("LEARN") == "LEARN"
