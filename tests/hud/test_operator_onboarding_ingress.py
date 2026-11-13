from __future__ import annotations

import json

import hud.operator_identity as operator_identity
import hud.settings as settings


def test_profile_ingress_records_raw_source_before_recursive_guardian_screen(monkeypatch):
    events = []
    intake_time = "2026-07-29T14:15:16+00:00"

    class Artifact:
        def citation(self):
            return {
                "source_id": "src-onboarding",
                "source_type": "operator_onboarding",
                "span_start": 0,
                "span_end": 80,
                "span_hash": "span",
                "source_content_hash": "content",
            }

    def record_source(content, **kwargs):
        events.append(("ledger", content, kwargs))
        return Artifact()

    def screen_receipt(payload, **kwargs):
        events.append(("guardian", dict(payload), kwargs))
        return {
            **payload,
            "guardian": {
                "screened": True,
                "quarantined": 0,
                "screened_fields": 3,
                "decisions": {},
            },
        }

    monkeypatch.setattr("memory.source_ledger.store.record_source", record_source)
    monkeypatch.setattr("guardian.external_ingress.screen_receipt", screen_receipt)
    monkeypatch.setattr(settings, "_now_iso", lambda: intake_time)

    result = settings._gate_operator_profile_ingress(
        {"name": "Ada", "occupation": "software_engineer", "subcategories": ["Python"]}
    )

    assert [event[0] for event in events] == ["ledger", "guardian"]
    assert events[0][2]["source_type"] == "operator_onboarding"
    assert events[0][2]["metadata"]["source_session_timestamp"] == intake_time
    assert result["decision"] == "allow"
    assert result["citation"]["source_id"] == "src-onboarding"
    assert result["payload"]["subcategories"] == ["Python"]


def test_quarantined_profile_never_reaches_canonical_memory(monkeypatch):
    monkeypatch.setattr(operator_identity, "is_onboarded", lambda: False)
    monkeypatch.setattr(
        settings,
        "_gate_operator_profile_ingress",
        lambda payload: {
            "decision": "quarantine",
            "payload": {},
            "citation": {"source_id": "src-hostile"},
            "guardian": {"decisions": {"notes": "quarantine"}},
        },
        raising=False,
    )
    queued = []
    monkeypatch.setattr(
        "pressure_loop.cognition_queue.enqueue_input",
        lambda contributor, **kwargs: queued.append(contributor) or True,
    )

    result = settings.save_operator_profile({"name": "Ada", "notes": "hostile"})

    assert result["ok"] is False
    assert result["guardian"]["decision"] == "quarantine"
    assert queued == []


def test_profile_save_queues_profile_anchor_and_fanout_inputs_with_one_lineage(monkeypatch):
    monkeypatch.setattr(operator_identity, "is_onboarded", lambda: False)
    citation = {
        "source_id": "src-onboarding",
        "source_type": "operator_onboarding",
        "span_start": 0,
        "span_end": 80,
        "span_hash": "span",
        "source_content_hash": "content",
    }
    monkeypatch.setattr(
        settings,
        "_gate_operator_profile_ingress",
        lambda payload: {
            "decision": "allow",
            "payload": dict(payload),
            "citation": citation,
            "guardian": {"screened_fields": 4, "decisions": {}},
        },
        raising=False,
    )

    queued = []
    monkeypatch.setattr(
        "pressure_loop.cognition_queue.enqueue_input",
        lambda contributor, **kwargs: queued.append(contributor) or True,
    )

    result = settings.save_operator_profile(
        {
            "name": "Ada Lovelace",
            "pronouns": "she/her",
            "occupation": "software_engineer",
            "subcategories": ["Python", "System Design"],
        }
    )

    assert result["ok"] is True
    assert "queued" in result["acknowledgement"].lower()
    [contributor] = queued
    envelopes = contributor.metadata["memory_envelopes"]
    profile = next(env for env in envelopes if env["type"] == "user_profile")
    anchor = next(env for env in envelopes if env["type"] == "identity_anchor")
    assert profile["payload"]["source_id"] == "src-onboarding"
    assert anchor["payload"]["source_id"] == "src-onboarding"
    onboarding = contributor.metadata["onboarding_profile"]
    assert onboarding["profile"]["occupation"] == "software_engineer"
    assert onboarding["subcategories"] == ["Python", "System Design"]
    assert onboarding["source_citation"] == citation


def test_profile_save_queues_screened_onboarding_without_writing_outside_an_episode(monkeypatch):
    monkeypatch.setattr(operator_identity, "is_onboarded", lambda: False)
    citation = {"source_id": "src-onboarding", "source_type": "operator_onboarding"}
    monkeypatch.setattr(
        settings,
        "_gate_operator_profile_ingress",
        lambda payload: {
            "decision": "allow",
            "payload": dict(payload),
            "citation": citation,
            "guardian": {"screened_fields": 3, "decisions": {}},
        },
    )
    monkeypatch.setattr(
        "memory.api.intake_memory",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("HTTP boundary wrote memory")),
    )
    queued = []
    monkeypatch.setattr(
        "pressure_loop.cognition_queue.enqueue_input",
        lambda contributor, **kwargs: queued.append(contributor) or True,
    )

    result = settings.save_operator_profile(
        {"name": "Ada", "occupation": "software_engineer", "subcategories": ["Python"]}
    )

    assert result["ok"] is True
    assert result["memory"]["status"] == "queued"
    assert result["persona_records"] == 0
    assert result["onboarding_seed"] is None
    assert "queued" in result["acknowledgement"].lower()
    [contributor] = queued
    assert contributor.source == "external"
    assert contributor.type == "sensory_observation"
    assert contributor.metadata["onboarding_profile"]["profile"]["occupation"] == (
        "software_engineer"
    )
    assert len(contributor.metadata["memory_envelopes"]) == 2


def test_profile_save_survives_real_chat_queue_reload_and_lease(tmp_path, monkeypatch):
    from pressure_loop import cognition_queue as queue

    queue_path = tmp_path / "guardian" / "state" / "cognition_queue.json"
    monkeypatch.setattr(queue, "_queue_path", lambda: queue_path)
    queue._reset_cache_for_tests()
    monkeypatch.setattr(operator_identity, "is_onboarded", lambda: False)
    citation = {"source_id": "src-onboarding", "source_type": "operator_onboarding"}
    monkeypatch.setattr(
        settings,
        "_gate_operator_profile_ingress",
        lambda payload: {
            "decision": "allow",
            "payload": dict(payload),
            "citation": citation,
            "guardian": {"screened_fields": 3, "decisions": {}},
        },
    )
    monkeypatch.setattr(
        "memory.api.intake_memory",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("HTTP boundary wrote memory")),
    )
    payload = {"name": "Ada", "occupation": "software_engineer", "subcategories": ["Python"]}

    result = settings.save_operator_profile(payload)

    assert result["ok"] is True
    assert result["memory"]["status"] == "queued"
    [persisted] = json.loads(queue_path.read_text(encoding="utf-8"))["queue"]
    assert persisted["lane"] == "chat"
    assert persisted["metadata"]["lane"] == "chat"
    assert persisted["metadata"]["cycle_id"] == "operator-onboarding"
    assert persisted["metadata"]["arrival_seq"] > 0
    envelopes = persisted["metadata"]["memory_envelopes"]
    assert {envelope["type"] for envelope in envelopes} == {"user_profile", "identity_anchor"}

    # Lose process-local ownership and reconstruct solely from the durable row.
    queue._reset_cache_for_tests()
    [restored] = queue.peek_inputs()
    assert queue.serialize_input(restored) == persisted
    assert settings.save_operator_profile(payload)["locked"] is True
    assert len(queue.peek_inputs()) == 1

    for specialist_pipeline in (1, 2, 3):
        assert queue.lease_next_input(specialist_pipeline) is None
    claimed = queue.lease_next_input(0)
    assert claimed is not None
    assert claimed.queue_id == persisted["queue_id"]
    assert claimed.metadata["memory_envelopes"] == envelopes
    assert settings.save_operator_profile(payload)["locked"] is True
    assert queue.lease_next_input(0) is None
    assert queue.acknowledge_input(claimed, 0) is True

    queue._reset_cache_for_tests()
    assert queue.peek_inputs() == ()
