"""Durable dev-artifact memory: workspace snapshot through verified recall."""

from __future__ import annotations

import pytest

from auto_loop.mril_tools.remember import invoke_remember_tool
from memory import episode_sram
from memory.api import QuerySpec, recall_verified
from memory.dev_artifact_intake import hold_dev_artifact_snapshots


pytestmark = pytest.mark.usefixtures("deepsleep_sram_drain")


def test_dev_artifact_snapshot_survives_sram_and_remember_dev(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "src" / "widget.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 42\n", encoding="utf-8")
    monkeypatch.setattr("core.hud_path_guard._operator_workspace_root", lambda: workspace)

    episode_sram.open_bucket("dev-recall-e2e", pipeline_id=0)
    hold_dev_artifact_snapshots(
        episode_id="dev-recall-e2e",
        objective="Build the widget",
        paths=["src/widget.py"],
    )
    episode_sram.flush("dev-recall-e2e")

    verdict = recall_verified(
        QuerySpec(
            cycle_id="dev-recall-query",
            text="what did we build in widget.py?",
            allowed_lobes=[11],
            denied_lobes=[],
            limit=10,
        )
    )
    assert verdict.status == "verified", verdict.reason
    candidates = list((verdict.answer_payload or {}).get("candidates") or [])
    assert any("widget.py" in str(candidate).lower() for candidate in candidates)

    recalled = invoke_remember_tool(
        {
            "tool": "remember_dev",
            "query": "what did we build in widget.py?",
            "allowed_lobes": [11],
            "memory_type": "episodic",
        },
        "dev-recall-query",
    )
    assert recalled["grounding"] == "VERIFIED_RELEVANT"
    assert any("widget.py" in str(item).lower() for item in recalled["evidence"])
