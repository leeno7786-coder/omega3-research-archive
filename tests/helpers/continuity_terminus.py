"""A real, isolated DeepSleep terminus for the WO-07 suites.

Every carrier here is opened on a real ``EpisodeStore``, dressed by the real post-action
producers (Meta B's front, the whole Global B chain, the real Omega lane parser), and closed
through the real ``run_deepsleep_for_episode`` — the same seam ``cognition_chain`` hands every
episode to. Nothing below writes canonical memory itself: the only writer is the terminus, through
episode SRAM and its drain (AGENTS.md section 5).

The distinctive values are sentinels on purpose. A round trip that only proves "some record came
back" cannot tell a sealed record from a default-filled one; a sentinel thread id, error text and
Omega reason can only come back if they were carried the whole way.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

SENTINEL_THREAD = "thread-SENTINEL-wo07-7f3a"
SENTINEL_OPENER = "wo07_sentinel_opener"
SENTINEL_ERROR = "SENTINEL-ERROR: parser build exploded on line 42"
SENTINEL_REASON = "SENTINEL-REASON: fix the failing parser acceptance test"
SENTINEL_REQUEST = "SENTINEL-REQUEST: build and test the parser"
CRITERION = "pytest passes"
SHA_A = "a" * 64


def isolate(monkeypatch, tmp_path) -> dict:
    """Point every DeepSleep store at tmp (the continuity conftest already isolated memory)."""
    from tests.core.deepsleep_window_fixtures import patch_window_deepsleep_paths
    from tests.helpers.blueprint_driver import _ModelFreeProjectionEncoder

    # Exercise the real projection pipeline without loading a model in a
    # synthetic contract test. Canonical encoding and claim receipts stay real.
    monkeypatch.setattr("memory.retina.encoder", lambda: _ModelFreeProjectionEncoder())
    return patch_window_deepsleep_paths(monkeypatch, tmp_path)


def open_carrier(store, *, pipeline_id: int = 0, work_kind: str = "operator_turn"):
    """Open a real carrier whose thread is inherited from its opener, as the host does it."""
    from memory import episode_sram

    pressure = {
        "opened_by": SENTINEL_OPENER,
        "contributors": [
            {
                "id": SENTINEL_OPENER,
                "type": "operator_command",
                "source": "operator",
                "detail": {"thread_id": SENTINEL_THREAD},
                "metadata": {"message": SENTINEL_REQUEST},
            }
        ],
    }
    carrier = store.open(
        opened_by=SENTINEL_OPENER,
        pressure=pressure,
        pipeline_id=pipeline_id,
        work_kind=work_kind,
    )
    episode_sram.open_bucket(carrier.episode_id, pipeline_id=pipeline_id)
    return carrier


def _program(episode_id: str) -> dict:
    from tests.omega_loop.test_motor_pressure_resolution import _program_dict

    program = _program_dict()
    program["episode_id"] = episode_id
    return program


def dress_failed_build(carrier) -> Any:
    """A build whose action ran, whose tool errored, and whose acceptance criterion FAILED.

    The verification and tool receipts are captured through WO-05's real producers into this
    episode's events; Meta B and Global B then fold them exactly as the cascade does.
    """
    from global_loop.loop import run_global_b
    from memory.auto_evidence import (
        capture_artifact_revision,
        capture_tool_attempt,
        capture_tool_result,
        capture_verification,
    )
    from meta_loop.meta_b import run_meta_b_front

    eid = str(carrier.episode_id)
    capture_tool_attempt(carrier, tool="skill", attempt_token="wo07-attempt-1", lane="agent")
    capture_tool_result(
        carrier,
        tool="skill",
        attempt_token="wo07-attempt-1",
        ok=False,
        error_kind="tool_reported_failure",
        error_detail=SENTINEL_ERROR,
    )
    capture_artifact_revision(
        carrier, path="parser.py", revision=1, content_sha256=SHA_A, change_kind="changed"
    )
    capture_verification(
        carrier, criterion=CRITERION, path="parser.py", revision=1, content_sha256=SHA_A,
        passed=False,
    )

    goal = {"episode_id": eid, "intent": "USE_SKILL", "confidence": 0.9, "reason": "wo07",
            "omega_lane": "agent"}
    carrier.global_ = {"intent_decision": goal}
    carrier.meta = {
        "execution_action_pack": {
            "episode_id": eid, "action": "USE_SKILL", "tool": "skill",
            "execution_intent": "build", "source_action_goal": goal, "execution_inputs": {},
        },
        "motor_program": _program(eid),
    }
    carrier.auto = {
        "episode_id": eid,
        "action": "USE_SKILL",
        "source_action_goal": deepcopy(goal),
        "execution_pack": deepcopy(carrier.meta["execution_action_pack"]),
        "cycle_kind": "fail",
        "memory_status": "skipped",
        "outputs": {},
        "telemetry": {"warnings": []},
        "failure_kind": "tests_failed",
        "failure_reason": SENTINEL_ERROR,
    }
    assert run_meta_b_front(carrier) is not None
    run_global_b(carrier, carrier.meta["reflection"])
    _apply_omega(carrier)
    return carrier


def _apply_omega(carrier) -> None:
    """A real OmegaEpisodeResult from the real strict lane parser, placed as the chain places it."""
    from omega_loop.episode.run import _packet_from_episode
    from organ.promptorgan.packs.meta_omega.core import parse_lane

    packet = _packet_from_episode(carrier)
    assessment = dict.fromkeys(("who", "what", "when", "where", "why", "how"), "the parser build")
    control = {
        "assessment": {**assessment, "desired_state": "parser tests pass",
                       "observed_state": "the acceptance test fails", "gap": "one failing test",
                       "evidence": ["pytest: 1 failed"]},
        "decision": "PRESSURE",
        "pressure": {"intent": "USE_SKILL", "confidence": 0.8, "reason": SENTINEL_REASON,
                     "setpoints": {}, "desired_state": "parser tests pass",
                     "observed_state": "the acceptance test fails",
                     "resolution_conditions": [f"{CRITERION} on parser.py"],
                     "domain": "parsing"},
        "synthesis": "The parser build failed its acceptance test.",
    }
    payload = {
        "control": control,
        "episode_assessment": {"completed": False, "quality": 0.3, "reason": "the test fails"},
        "what_changed": {"learned": [], "did": ["wrote parser.py"], "verified": [],
                         "failed": [CRITERION]},
        "decisions": [{"statement": "keep the tokenizer hand-written",
                       "rationale": "the grammar is tiny"}],
    }
    result = parse_lane(payload, packet)
    global_ = dict(carrier.global_)
    global_["omega_result"] = result.to_dict()
    carrier.global_ = global_


def dress_learn(carrier, *, domain: str = "quantum_physics", knowledge: float = 0.5) -> Any:
    """A successful LEARN episode whose Omega fold grounds one mastery effect."""
    from tests.core.deepsleep_window_fixtures import fixture_omega_result

    eid = str(carrier.episode_id)
    carrier.global_ = {
        "action_goal": {"intent": "LEARN", "target": domain, "confidence": 0.9,
                        "reason": "curiosity"},
        "outcome": {
            "goal": {"intent": "LEARN", "target": domain},
            "outcome": {"goal_state": "completed", "summary": "success"},
            "observations": {"response_text": True, "memory_used": False},
            "artifacts": {"response_text": "basics summary"},
            "followups": [],
            "consistency": {},
        },
        "omega_result": fixture_omega_result(
            intent="LEARN", target=domain, success=True, knowledge_mastery=knowledge,
            application_mastery=0.0, knowledge_products=None,
        ),
    }
    carrier.meta = {
        "execution_action_pack": {"episode_id": eid, "action": "web_fetch", "target": domain},
        "reflection": {"execution": {"action": "web_fetch", "executed": True,
                                     "result": "success", "status": "success",
                                     "disposition": "completed"}},
    }
    carrier.auto = {"status": "success", "cycle_kind": "success"}
    carrier.omega = {}
    return carrier


def close(store, *, cascade_status: str = "completed"):
    """The one terminus seam every episode passes."""
    from deepsleep_loop.loop import run_deepsleep_for_episode

    return run_deepsleep_for_episode(store, cascade_status=cascade_status)


def canonical_episode_documents(episode_id: str) -> list[dict]:
    """Every canonical lobe-9 episode document on disk for *episode_id* (filesystem truth)."""
    import json
    from pathlib import Path

    from core import paths as core_paths

    found = []
    for path in Path(core_paths.MEMORY_ROOT).rglob("*.json"):
        if "indexes" in path.parts or "tmp_writes" in path.parts:
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict) or doc.get("type") != "episode":
            continue
        enrichment = doc.get("enrichment") or {}
        if str(enrichment.get("episode_id") or "") == episode_id:
            found.append(doc)
    return found
