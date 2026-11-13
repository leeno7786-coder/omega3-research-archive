"""Meta B reads consistency/disposition config via pressure_loop.policy."""

from __future__ import annotations

from pressure_loop.policy import (
    clear_policy_cache,
    meta_b_cycle_kind_map,
    meta_b_pack_stable_fields,
)
from meta_loop.consistency.run import run as check_consistency
from meta_loop.consistency.run import MetaBInput, meta_b_input_from_episode
from meta_loop.disposition.run import derive_disposition


def test_meta_b_stable_fields_from_config():
    clear_policy_cache()
    assert "action" in meta_b_pack_stable_fields()


def test_meta_b_cycle_kind_map_from_config():
    clear_policy_cache()
    m = meta_b_cycle_kind_map()
    assert m.get("success") == ("completed", "success")
    assert derive_disposition({"cycle_kind": "success"}) == ("completed", "success")


def test_meta_b_consistency_uses_config_stable_fields():
    clear_policy_cache()
    mbi = MetaBInput(
        episode_id="ep_mb",
        pressure={},
        source_action_goal={
            "episode_id": "ep_mb",
            "intent": "USE_TOOL",
            "target": "t",
            "confidence": 0.9,
            "reason": "r",
        },
        execution_pack={
            "episode_id": "ep_mb",
            "action": "USE_TOOL",
            "tool": "web_fetch",
            "execution_intent": "learn",
        },
        auto_outcome={
            "episode_id": "ep_mb",
            "source_action_goal": {
                "episode_id": "ep_mb",
                "intent": "USE_TOOL",
                "target": "t",
                "confidence": 0.9,
                "reason": "r",
            },
            "execution_pack": {
                "episode_id": "ep_mb",
                "action": "USE_TOOL",
                "tool": "web_fetch",
                "execution_intent": "learn",
            },
            "auto": {"cycle_kind": "success", "outputs": {}},
        },
    )
    consistency, critical = check_consistency(mbi)
    assert "goal_matches" not in consistency
    assert consistency["pack_matches"] is True
    assert critical is False


def test_meta_b_input_requires_auto():
    class Ep:
        episode_id = "ep_x"
        auto = None

    assert meta_b_input_from_episode(Ep) is None


# --- I1: Meta-B consistency compares the meta plan against an INDEPENDENT
#         Auto-side copy, not against itself ---------------------------------


def _fold(action="USE_TOOL", tool="web_fetch"):
    """Fold via the PRODUCTION _fold_episode_auto (not a hand-built dict)."""
    from auto_loop._auto_state import AutoInput, MemoryEnvelope, PromptPack
    from auto_loop.fold.run import _fold_episode_auto

    ai = AutoInput(
        episode_id="ep1",
        pressure={},
        source_action_goal={"episode_id": "ep1", "action": action},
        execution_pack={
            "episode_id": "ep1",
            "action": action,
            "tool": tool,
            "execution_intent": "learn",
        },
        trajectory_snapshot={},
    )
    folded = _fold_episode_auto(
        ai,
        status="success",
        cycle_kind="success",
        memory_envelope=MemoryEnvelope(False, "none", None, (), (), None),
        model_profile="p",
        model_id="m",
        prompt_pack=PromptPack("learn", "sys", None, "usr", {}, "mode", None, None),
        exec_result={"outputs": {}},
        runtime_ms=10,
    )
    return ai, folded


def test_fold_episode_auto_echoes_execution_pack():
    """The AUTO fold must echo the pack it executed into episode.auto so Meta-B has
    an independent Auto-side copy to compare against (mirrors auto_outcome_from_fold)."""
    ai, folded = _fold()
    assert folded.get("execution_pack") == ai.execution_pack


def test_fold_episode_auto_preserves_knowledge_artifact_keys():
    """I11: the fold outputs allowlist must keep knowledge_artifact_id/knowledge_domain
    so the episode->KnowledgeArtifact backlink survives into episode.auto.outputs (the
    deepsleep consume path reads it from there)."""
    from auto_loop._auto_state import AutoInput, MemoryEnvelope, PromptPack
    from auto_loop.fold.run import _fold_episode_auto

    ai = AutoInput(
        episode_id="ep1",
        pressure={},
        source_action_goal={},
        execution_pack={"action": "USE_TOOL"},
        trajectory_snapshot={},
    )
    folded = _fold_episode_auto(
        ai,
        status="success",
        cycle_kind="success",
        memory_envelope=MemoryEnvelope(False, "none", None, (), (), None),
        model_profile="p",
        model_id="m",
        prompt_pack=PromptPack("learn", "sys", None, "usr", {}, "mode", None, None),
        exec_result={
            "outputs": {
                "knowledge_artifact_id": "art-1",
                "knowledge_domain": "physics",
                "response_text": "x",
            }
        },
        runtime_ms=10,
    )
    assert folded["outputs"]["knowledge_artifact_id"] == "art-1"
    assert folded["outputs"]["knowledge_domain"] == "physics"


def test_fold_episode_auto_preserves_execution_trace_metadata():
    from auto_loop._auto_state import AutoInput, MemoryEnvelope, PromptPack
    from auto_loop.fold.run import _fold_episode_auto

    ai = AutoInput(
        episode_id="ep1",
        pressure={},
        source_action_goal={},
        execution_pack={"action": "USE_TOOL"},
        trajectory_snapshot={},
    )
    folded = _fold_episode_auto(
        ai,
        status="failed",
        cycle_kind="fail",
        memory_envelope=MemoryEnvelope(False, "none", None, (), (), None),
        model_profile="p",
        model_id="m",
        prompt_pack=PromptPack("tool", "sys", None, "usr", {}, "summary", None, None),
        exec_result={
            "failure_kind": "tool_failed",
            "failure_reason": "boom",
            "outputs": {
                "tool_status": "failed",
                "tool_error": "boom",
                "tool_args": {"query": "x"},
                "apply_result": {"summary": "not used"},
                "apply_status": "none",
                "apply_error": None,
            },
        },
        runtime_ms=10,
    )
    assert folded["outputs"]["tool_status"] == "failed"
    assert folded["outputs"]["tool_error"] == "boom"
    assert folded["outputs"]["tool_args"] == {"query": "x"}


def test_meta_b_detects_pack_divergence_on_live_folded_path():
    """With the fold echoing execution_pack, a meta-plan vs auto-executed divergence is
    now detectable on the LIVE assembly path. Previously auto_pack == pack trivially, so
    pack/action/tool mismatch could NEVER fire. Drives meta_b_input_from_episode (live),
    not a hand-injected MetaBInput."""
    from types import SimpleNamespace

    clear_policy_cache()
    _ai, folded = _fold(action="USE_TOOL", tool="web_fetch")  # AUTO executed USE_TOOL
    episode = SimpleNamespace(
        episode_id="ep1",
        auto=folded,
        pressure={},
        # Meta planned APPLY — a real divergence from what AUTO executed.
        meta={"execution_action_pack": {"episode_id": "ep1", "action": "APPLY", "tool": None}},
        global_={},
    )
    mbi = meta_b_input_from_episode(episode)
    assert mbi is not None
    consistency, critical = check_consistency(mbi)
    assert critical is True
    assert consistency["pack_matches"] is False


def test_meta_b_stable_pack_drift_is_fail_closed():
    mbi = MetaBInput(
        episode_id="ep_mb",
        pressure={},
        source_action_goal={"episode_id": "ep_mb", "intent": "USE_TOOL"},
        execution_pack={
            "episode_id": "ep_mb",
            "action": "USE_TOOL",
            "tool": "web_fetch",
            "execution_intent": "learn",
        },
        auto_outcome={
            "episode_id": "ep_mb",
            "source_action_goal": {"episode_id": "ep_mb", "intent": "USE_TOOL"},
            "execution_pack": {
                "episode_id": "ep_mb",
                "action": "USE_TOOL",
                "tool": "web_fetch",
                "execution_intent": "different-plan",
            },
            "auto": {"cycle_kind": "success", "outputs": {}},
        },
    )
    consistency, critical = check_consistency(mbi)
    assert consistency["pack_matches"] is False
    assert critical is True
