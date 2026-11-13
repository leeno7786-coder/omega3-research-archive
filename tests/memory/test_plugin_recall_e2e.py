"""Durable plugin-result memory: capture, verify, and answer-facing recall."""

from __future__ import annotations

import pytest

from memory.api import QuerySpec, recall_verified
from memory.taxonomy.canonical_capture import capture_episode_to_canonical


pytestmark = pytest.mark.usefixtures("deepsleep_sram_drain")


def test_plugin_result_survives_canonical_capture_and_verified_recall() -> None:
    episode = {
        "episode_id": "ep-plugin-recall-e2e",
        "closed_at": "2026-09-10T00:00:00Z",
        "cascade_status": "completed",
        "intent": "PLUGIN_OPERATION",
        "action": "PLUGIN_OPERATION",
        "outputs": {
            "plugin_result": {
                "plugin_id": "slack.official",
                "app_id": "slack.official.mcp",
                "capability": "search_messages",
                "capability_kind": "tool",
                "operation": "tool_call",
                "status": "completed",
                "trust": "untrusted_provider_data",
                "data": {"channel": "#release", "text": "shipping friday"},
            }
        },
        "outcome": {"status": "completed"},
        "execution": {},
    }

    report = capture_episode_to_canonical(episode, cycle_id=episode["episode_id"])
    assert not report["errors"], report

    verdict = recall_verified(
        QuerySpec(
            cycle_id="plugin-recall-query",
            text="what did Slack say about shipping?",
            allowed_lobes=[4],
            limit=10,
        )
    )

    assert verdict.status == "verified", verdict.reason
    candidates = list((verdict.answer_payload or {}).get("candidates") or [])
    assert candidates
    assert any(
        "shipping friday" in str(candidate).lower()
        for candidate in candidates
    )
