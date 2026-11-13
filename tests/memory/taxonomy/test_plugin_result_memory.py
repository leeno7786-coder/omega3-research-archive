"""A plugin's answer must survive the turn as its own recallable record.

Before this, the only trace of what a plugin returned was an opaque JSON
fragment inside the whole-episode archive: recall could reach the episode but
never the fact the provider actually supplied.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from memory.taxonomy import adapters as A
from memory.taxonomy.canonical_capture import EVENT_KIND_TO_FACET, capture_episode_to_canonical
from memory.dewey_map import SITUATIONAL_FACETS

CREATED = "2026-09-06T22:00:00Z"


def _episode(data: Any = None, **overrides: Any) -> dict[str, Any]:
    result = {
        "plugin_id": "slack.official",
        "app_id": "slack.official.mcp",
        "capability": "search_messages",
        "capability_kind": "tool",
        "operation": "tool_call",
        "status": "completed",
        "trust": "untrusted_provider_data",
        "data": {"messages": [{"channel": "#release", "text": "shipping friday"}]}
        if data is None
        else data,
        **overrides,
    }
    return {
        "episode_id": "ep-plugin-1",
        "closed_at": CREATED,
        "auto": {"outputs": {"plugin_result": result}},
    }


def test_a_plugin_result_becomes_its_own_memory_event() -> None:
    event = A.event_from_plugin_result(_episode(), CREATED)

    assert event is not None
    assert event.event_kind == "tool_result"
    assert event.episode_id == "ep-plugin-1"
    assert event.validate() == []
    assert event.payload["plugin_id"] == "slack.official"
    assert event.payload["capability"] == "search_messages"
    assert event.payload["status"] == "completed"
    # Provider data stays labelled as provider data wherever it travels.
    assert event.payload["trust"] == "untrusted_provider_data"
    assert "shipping friday" in event.payload["data"]


def test_enriched_closed_episode_shape_still_mints_the_plugin_event() -> None:
    enriched = {
        "episode_id": "ep-plugin-enriched",
        "closed_at": CREATED,
        "cascade_status": "completed",
        "intent": "PLUGIN_OPERATION",
        "action": "PLUGIN_OPERATION",
        "outputs": {"plugin_result": _episode()["auto"]["outputs"]["plugin_result"]},
        "outcome": {"status": "completed"},
        "execution": {},
    }

    event = A.event_from_plugin_result(A.canonical_event_view(enriched), CREATED)

    assert event is not None
    assert event.episode_id == "ep-plugin-enriched"
    assert event.payload["capability"] == "search_messages"


def test_the_record_is_titled_so_a_recall_hit_can_be_read() -> None:
    """Catches a record titled with a bare identifier, which recall cannot use."""
    event = A.event_from_plugin_result(_episode(), CREATED)

    assert event.payload["title"] == "slack.official search_messages result"
    assert event.payload["summary"] == "slack.official search_messages result (completed)"
    assert event.episode_id not in event.payload["title"]


def test_a_large_provider_response_is_bounded_but_still_attributable() -> None:
    """One oversized response must not dominate a memory record."""
    event = A.event_from_plugin_result(_episode(data={"blob": "x" * 50_000}), CREATED)

    assert len(event.payload["data"]) == A._MAX_PLUGIN_RESULT_CHARS
    assert event.payload["data_truncated"] is True
    assert len(event.payload["data_sha256"]) == 64
    assert event.payload["capability"] == "search_messages"


def test_an_episode_without_a_plugin_result_mints_nothing() -> None:
    assert A.event_from_plugin_result({"episode_id": "x", "closed_at": CREATED}, CREATED) is None
    assert (
        A.event_from_plugin_result(
            {"episode_id": "x", "closed_at": CREATED, "auto": {"outputs": {"plugin_result": {}}}},
            CREATED,
        )
        is None
    )


def test_the_adapter_runs_for_every_closed_episode() -> None:
    """Catches an adapter that exists but is never invoked at DeepSleep."""
    assert A.event_from_plugin_result in A._EPISODE_ADAPTERS


def test_the_event_kind_routes_to_the_situational_outcome_facet() -> None:
    assert EVENT_KIND_TO_FACET["tool_result"] == "outcome"


def test_capture_writes_one_readable_situational_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end: the closed episode yields a readable lobe-4 outcome record."""
    written: list[Any] = []

    def _intake(envelope: Any, **_kwargs: Any) -> str:
        written.append(envelope)
        return "ok"

    monkeypatch.setattr("memory.api.intake_memory", _intake)
    monkeypatch.setattr("memory.taxonomy.canonical_capture.write_episode_to_temporal", lambda _e: None)

    capture_episode_to_canonical(_episode(), cycle_id="cycle-1")

    outcome_y = SITUATIONAL_FACETS.index("outcome")
    records = [
        item
        for item in written
        if item.coord == [4, outcome_y, 0]
        and item.payload["enrichment"].get("event_kind") == "tool_result"
    ]
    assert len(records) == 1
    record = records[0]
    assert record.payload["title"] == "slack.official search_messages result"
    assert record.payload["subtopic"] == "tool_result"
    assert "shipping friday" in json.dumps(record.payload["enrichment"]["event_payload"])


def test_capture_normalizes_the_real_enriched_close_body(monkeypatch: pytest.MonkeyPatch) -> None:
    written: list[Any] = []
    enriched = {
        "episode_id": "ep-plugin-enriched-capture",
        "closed_at": CREATED,
        "cascade_status": "completed",
        "intent": "PLUGIN_OPERATION",
        "action": "PLUGIN_OPERATION",
        "outputs": {"plugin_result": _episode()["auto"]["outputs"]["plugin_result"]},
        "outcome": {"status": "completed"},
        "execution": {},
    }

    monkeypatch.setattr("memory.api.intake_memory", lambda envelope, **_kwargs: written.append(envelope))
    monkeypatch.setattr(
        "memory.taxonomy.canonical_capture.write_episode_to_temporal",
        lambda _episode: None,
    )

    report = capture_episode_to_canonical(enriched, cycle_id="cycle-enriched")

    assert not report["errors"]
    assert any(
        envelope.payload["enrichment"].get("event_kind") == "tool_result"
        for envelope in written
    )
