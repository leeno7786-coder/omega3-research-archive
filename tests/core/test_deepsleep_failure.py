"""Slice 6c B4 — DeepSleep failure pattern consolidation."""

from __future__ import annotations

import pytest

from deepsleep_loop._deepsleep_state import FailurePattern
from knowledge_extractor.derive.run import _slug
from tests.core.deepsleep_window_fixtures import episode_entry, learn_input, window_input
from deepsleep_loop.failure.run import consolidate_failures, failure_effects_from_patterns
from deepsleep_loop.failure_store import failure_key, load_failures, upsert_failure


def _failure_input(
    episode_id: str,
    *,
    failure_kind: str = "tool_failed",
    target: str = "Python",
):
    return window_input(
        window_id=episode_id,
        episodes=[
            episode_entry(
                episode_id,
                intent="USE_TOOL",
                target=target,
                success=False,
                action="run_code",
                failure_kind=failure_kind,
            )
        ],
    )


def test_failure_pattern_round_trip():
    original = FailurePattern(
        kind="tool",
        context="Python",
        domain_id="python",
        frequency=2,
        last_episode="ep-2",
    )
    restored = FailurePattern.from_dict(original.to_dict())
    assert restored == original


def test_failure_pattern_from_dict_with_domain_id():
    restored = FailurePattern.from_dict(
        {
            "kind": "tool",
            "context": "Python",
            "domain_id": "python",
            "frequency": 1,
            "last_episode": "ep-1",
        }
    )
    assert restored.domain_id == "python"


def test_failure_pattern_from_dict_without_domain_id():
    restored = FailurePattern.from_dict(
        {
            "kind": "tool",
            "context": "Python",
            "frequency": 1,
            "last_episode": "ep-1",
        }
    )
    assert restored.domain_id == "python"


def test_tool_failure_creates_pattern(tmp_path, monkeypatch):
    path = tmp_path / "failure.json"
    monkeypatch.setattr(
        "pressure_loop.policy.deepsleep_failure_path",
        lambda policy=None: path,
    )

    patterns = consolidate_failures(_failure_input("ep-1"))
    assert len(patterns) == 1
    assert patterns[0].kind == "tool"
    assert patterns[0].frequency == 1

    loaded = load_failures(path=path)
    key = failure_key("tool", "Python")
    assert key in loaded


def test_repeated_failures_aggregate_frequency(tmp_path, monkeypatch):
    path = tmp_path / "failure.json"
    monkeypatch.setattr(
        "pressure_loop.policy.deepsleep_failure_path",
        lambda policy=None: path,
    )

    consolidate_failures(_failure_input("ep-1"))
    patterns = consolidate_failures(_failure_input("ep-2"))

    assert patterns[0].frequency == 2
    assert patterns[0].last_episode == "ep-2"


def test_success_episode_adds_nothing(tmp_path, monkeypatch):
    path = tmp_path / "failure.json"
    monkeypatch.setattr(
        "pressure_loop.policy.deepsleep_failure_path",
        lambda policy=None: path,
    )

    patterns = consolidate_failures(learn_input())
    assert patterns == []
    assert load_failures(path=path) == {}


def test_failure_effects_from_patterns():
    patterns = [FailurePattern(kind="tool", context="Python", frequency=1, last_episode="ep-1")]
    effects = failure_effects_from_patterns(patterns, prior=None)
    key = failure_key("tool", "Python")
    assert key in effects


def test_failure_context_uses_execution_query_not_trajectory_domain(tmp_path, monkeypatch):
    path = tmp_path / "failure.json"
    monkeypatch.setattr(
        "pressure_loop.policy.deepsleep_failure_path",
        lambda policy=None: path,
    )
    ds_input = window_input(
        window_id="ep-query",
        episodes=[
            {
                "episode_id": "ep-query",
                "execution": {
                    "execution_action_pack": {
                        "execution_inputs": {"query": "X"},
                    }
                },
                "outcome": {
                    "outcome": {
                        "status": "failed",
                        "goal_state": "failed",
                        "failure_kind": "tool_failed",
                    }
                },
                "auto": {
                    "status": "failed",
                    "cycle_kind": "failure",
                    "failure_kind": "tool_failed",
                },
            }
        ],
        trajectory_snapshot={"target_domain": "Python"},
    )
    patterns = consolidate_failures(ds_input)
    assert len(patterns) == 1
    assert patterns[0].context == "X"


def test_consolidate_failures_writes_domain_id(tmp_path, monkeypatch):
    path = tmp_path / "failure.json"
    monkeypatch.setattr(
        "pressure_loop.policy.deepsleep_failure_path",
        lambda policy=None: path,
    )

    patterns = consolidate_failures(_failure_input("ep-1", target="Attention Mechanisms"))
    assert len(patterns) == 1
    assert patterns[0].domain_id == _slug("Attention Mechanisms")


@pytest.mark.parametrize("phase", ["meta_b", "global_b", "omega"])
def test_cascade_phase_failure_creates_a_failure_pattern(tmp_path, monkeypatch, phase):
    path = tmp_path / "failure.json"
    monkeypatch.setattr(
        "pressure_loop.policy.deepsleep_failure_path",
        lambda policy=None: path,
    )
    episode_id = f"ep-{phase}"
    entry = episode_entry(episode_id, target="compile the report", success=True)
    entry["cascade_status"] = "failed"
    entry["phase_trail"] = [
        {
            "phase": phase,
            "status": "failed",
            "cause": f"{phase} raised RuntimeError: reflection packet missing",
            "evidence": [],
        }
    ]
    ds_input = window_input(window_id=episode_id, episodes=[entry])

    patterns = consolidate_failures(ds_input)

    assert len(patterns) == 1
    assert patterns[0].kind == "response"
    assert patterns[0].context == "compile the report"
    assert patterns[0].last_episode == episode_id


@pytest.mark.parametrize(
    "failure_meta",
    [
        {"cascade_failure": {"error_type": "RuntimeError", "message": "settlement failed"}},
        {"omega_fold_failure": "control settlement failed"},
    ],
)
def test_failure_metadata_is_learned_when_every_cascade_phase_ran(
    tmp_path, monkeypatch, failure_meta
):
    """A post-Omega settlement failure has no missing phase row to carry its cause."""
    path = tmp_path / "failure.json"
    monkeypatch.setattr(
        "pressure_loop.policy.deepsleep_failure_path",
        lambda policy=None: path,
    )
    episode_id = "ep-post-omega-settlement"
    entry = episode_entry(episode_id, target="compile the report", success=True)
    entry["cascade_status"] = "failed"
    entry["meta"] = failure_meta
    entry["phase_trail"] = [
        {"phase": phase, "status": "ran", "cause": "", "evidence": []}
        for phase in (
            "admission", "pressure", "global_a", "meta_a", "auto", "meta_b",
            "global_b", "omega",
        )
    ] + [{
        "phase": "deepsleep", "status": "in_progress",
        "cause": "terminus not yet complete", "evidence": [],
    }]
    ds_input = window_input(window_id=episode_id, episodes=[entry])

    patterns = consolidate_failures(ds_input)

    assert len(patterns) == 1
    assert patterns[0].kind == "response"
    assert patterns[0].context == "compile the report"
    assert patterns[0].last_episode == episode_id
