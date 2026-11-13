"""DeepSleep knowledge_integration.consume_on_consolidate flag."""

from __future__ import annotations

import json
from pathlib import Path

from tests.core.deepsleep_window_fixtures import producer_outcome_axes, window_input
from deepsleep_loop.consolidate.run import build_journal_entry
from pressure_loop.policy import (
    clear_policy_cache,
    deepsleep_knowledge_consume_on_consolidate,
)


def _ds_input(*, artifact_id: str):
    return window_input(
        window_id="ep-k1",
        episodes=[
            {
                "episode_id": "ep-k1",
                "intent": "LEARN",
                "action": "USE_TOOL",
                "outcome": {
                    "goal": {"intent": "LEARN", "target": "python"},
                    "outcome": {"goal_state": "completed"},
                },
                "outcome_axes": producer_outcome_axes(success=True),
                "outputs": {
                    "knowledge_artifact_id": artifact_id,
                    "tool_result": "facts",
                },
            }
        ],
        reflection={"reasoning": {}},
    )


def test_consume_on_from_config(tmp_path, monkeypatch):
    clear_policy_cache()
    assert deepsleep_knowledge_consume_on_consolidate() is True
    store = tmp_path / "artifacts.jsonl"
    row = {
        "artifact_id": "art-1",
        "domain": "python",
        "concepts": [],
        "facts": [],
        "relationships": [],
        "mastery_tags": [],
        "confidence": 0.5,
        "source_kind": "tool_output",
    }
    store.write_text(json.dumps(row) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        "pressure_loop.policy.deepsleep_knowledge_artifact_store_path",
        lambda policy=None: str(store),
    )
    entry = build_journal_entry(_ds_input(artifact_id="art-1"))
    assert entry.artifacts.get("knowledge") == row


def test_consume_skips_when_disabled(monkeypatch):
    clear_policy_cache()
    monkeypatch.setattr(
        "pressure_loop.policy.deepsleep_knowledge_consume_on_consolidate",
        lambda policy=None: False,
    )
    entry = build_journal_entry(_ds_input(artifact_id="art-1"))
    assert "knowledge" not in entry.artifacts


def test_consume_merges_artifact_when_enabled(tmp_path, monkeypatch):
    clear_policy_cache()
    store = tmp_path / "artifacts.jsonl"
    row = {
        "artifact_id": "art-42",
        "domain": "python",
        "concepts": ["recursion"],
        "facts": ["base case required"],
        "relationships": [],
        "mastery_tags": [],
        "confidence": 0.8,
        "source_kind": "tool_output",
    }
    store.write_text(json.dumps(row) + "\n", encoding="utf-8")

    monkeypatch.setattr(
        "pressure_loop.policy.deepsleep_knowledge_consume_on_consolidate",
        lambda policy=None: True,
    )
    monkeypatch.setattr(
        "pressure_loop.policy.deepsleep_knowledge_artifact_store_path",
        lambda policy=None: str(store),
    )

    entry = build_journal_entry(_ds_input(artifact_id="art-42"))
    assert entry.artifacts.get("knowledge") == row
