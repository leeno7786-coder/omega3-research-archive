"""Omega must see rollback facts as authoritative episode context."""

from __future__ import annotations

from omega_loop.episode.prompts import prompt_spec_for
from omega_loop.episode.types import OmegaEpisodeInput


def test_knowledge_prompt_includes_workspace_rollback_events():
    packet = OmegaEpisodeInput(
        episode_id="ep-rollback",
        intent="IDLE",
        action="USE_TOOL",
        tool=None,
        domain=None,
        operator_text=None,
        outcome="none",
        failure_reason=None,
        raw_tool_result=None,
        recall=(),
        recall_error=None,
        prior_mastery=None,
        rollback_events=(
            {
                "type": "dev_rollback",
                "snapshot_id": "snap-prompt",
                "file_path": "F:/workspace/app.py",
                "action": "restore",
                "message": "rolled back F:/workspace/app.py",
            },
        ),
    )

    spec = prompt_spec_for(packet)
    assert spec is not None
    system, user = spec.build(packet)
    assert "rollback" in system.lower()
    assert "workspace_rollback_events" in user
    assert "F:/workspace/app.py" in user
    assert "snap-prompt" in user
