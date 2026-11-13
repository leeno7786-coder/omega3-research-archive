"""Trusted workspaces enter dev memory once per change and come back through remember_dev."""
from __future__ import annotations

from pathlib import Path

import pytest

from auto_loop.mril_tools.remember import invoke_remember_tool
from memory import episode_sram
from memory.dev_observation_intake import hold_trusted_workspaces


@pytest.fixture
def signature_path(monkeypatch, tmp_path: Path) -> Path:
    path = tmp_path / "dev_workspace_memory.json"
    monkeypatch.setenv("OMEGA_DEV_WORKSPACE_MEMORY_PATH", str(path))
    return path


def test_workspaces_are_held_once_per_change_and_recalled_by_remember_dev(signature_path):
    """Operator 2026-09-15: "save them to dev memory and have the dev mril tool recall them when
    working so omega has context of where its allowed to work"."""
    facts = {"roots": ["E:/lab", "J:/Projects/shop"], "active": "E:/lab"}
    episode_sram.open_bucket("ep-ws-1", pipeline_id=0)

    receipt = hold_trusted_workspaces(episode_id="ep-ws-1", facts=facts)

    assert receipt["status"] == "held"
    env = episode_sram.pending("ep-ws-1")[-1].envelope
    assert env.coord == [11, 4, 0] and env.type == "dev_tool_observation"
    assert env.payload["roots"] == facts["roots"] and env.payload["active"] == "E:/lab"
    assert "(active working folder)" in env.payload["text"] and "/cd <path>" in env.payload["text"]
    assert signature_path.is_file()
    # The same grants on the next agent episode are not written again.
    episode_sram.open_bucket("ep-ws-2", pipeline_id=1)
    assert hold_trusted_workspaces(episode_id="ep-ws-2", facts=facts)["status"] == "unchanged"
    assert episode_sram.pending("ep-ws-2") == []
    # A new grant is.
    changed = {"roots": [*facts["roots"], "D:/lab"], "active": "D:/lab"}
    assert hold_trusted_workspaces(episode_id="ep-ws-2", facts=changed)["status"] == "held"

    flushed = episode_sram.flush("ep-ws-1")
    assert flushed["memories"]
    recalled = invoke_remember_tool(
        {"tool": "remember_dev", "query": "which workspaces am I allowed to work in?",
         "allowed_lobes": [11], "allowed_subdomains": [4], "memory_type": "episodic"},
        "ws-recall",
    )
    assert recalled["grounding"] == "VERIFIED_RELEVANT"
    assert any("E:/lab" in str(item) and "J:/Projects/shop" in str(item) for item in recalled["evidence"])


def test_nothing_is_held_without_grants_or_an_open_episode(signature_path):
    assert hold_trusted_workspaces(episode_id="", facts={"roots": ["E:/lab"], "active": ""})["status"] == "not_held"
    episode_sram.open_bucket("ep-ws-3", pipeline_id=0)
    assert hold_trusted_workspaces(episode_id="ep-ws-3", facts={"roots": [], "active": ""}) == {
        "status": "not_held", "reason": "no_trusted_workspaces"}
