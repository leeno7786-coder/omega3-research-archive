"""Persona consolidation is a cited cognitive self-write, not external ingress.

Operator chat crosses SourceLedger + Guardian once at chat ingress.  DeepSleep's
derived persona leaves preserve that citation and commit through the direct mind
write path; sending them back through Guardian would conflate the immune boundary
with cognition and can strand already-approved facts.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.usefixtures("deepsleep_sram_drain")

REPO_ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(autouse=True)
def _isolate_persona_router(tmp_path, monkeypatch):
    import core.persona_router as pr

    monkeypatch.setattr(pr, "OMEGA_ROOT", tmp_path)
    monkeypatch.setattr(pr, "_INDEX_PATH", tmp_path / "guardian" / "state" / "persona_index.json")


def test_operator_statement_derived_write_does_not_reenter_guardian(monkeypatch):
    import guardian.decision as gd
    from memory.persona_ingest import ingest_persona_statement
    from memory.source_ledger.store import record_operator_chat_source

    calls: list = []
    monkeypatch.setattr(
        gd,
        "decide_request",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    statement = "I go to college for cybersecurity."
    citation = record_operator_chat_source(statement, cycle_id="test-direct-persona")
    result = ingest_persona_statement(
        statement,
        speaker="operator",
        source_citation=citation,
    )

    assert calls == []
    assert any(record.id for record in result.records)


def test_operator_relationship_derived_write_does_not_reenter_guardian(monkeypatch):
    import guardian.decision as gd
    from memory.persona_ingest import ingest_persona_statement
    from memory.source_ledger.store import record_operator_chat_source

    calls: list = []
    monkeypatch.setattr(
        gd,
        "decide_request",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    statement = "I have a son named Abel."
    citation = record_operator_chat_source(statement, cycle_id="test-direct-relation")
    result = ingest_persona_statement(
        statement,
        speaker="operator",
        source_citation=citation,
    )

    assert calls == []
    assert any(record.id for record in result.records)


def test_source_citation_reaches_every_derived_persona_write(monkeypatch):
    import core.persona_router as persona_router
    import memory.persona_ingest as persona_ingest

    citation = {
        "source_id": "src-chat-citation",
        "source_type": "operator_chat",
        "span_start": 0,
        "span_end": 36,
        "span_hash": "span-chat-citation",
        "source_content_hash": "content-chat-citation",
        "source_session_id": "session-chat-citation",
        "source_session_timestamp": "2026-07-27T12:00:00+00:00",
    }
    envelopes: list[tuple[object, dict]] = []
    person_calls: list[dict] = []
    profile_calls: list[dict] = []

    monkeypatch.setattr(
        "memory.enrichment.personas.enrich",
        lambda _text: {
            "entities": [
                {"facet": "education", "name": "cybersecurity", "confidence": 0.9},
                {
                    "facet": "relationship",
                    "name": "son",
                    "linked_name": "Abel",
                    "confidence": 0.9,
                },
            ]
        },
    )
    monkeypatch.setattr(persona_ingest, "_existing_operator_attribute", lambda *_a: None)
    monkeypatch.setattr(persona_ingest, "_resolve_operator_name", lambda: None)
    monkeypatch.setattr(persona_ingest, "_resolve_record_path", lambda _mid: None)
    monkeypatch.setattr(
        persona_router,
        "route_persona",
        lambda _name, **_kwargs: (1, 1, "abel"),
    )

    def _ensure_persona_profile(_name, **kwargs):
        profile_calls.append(kwargs)
        return "mem-profile"

    monkeypatch.setattr(persona_router, "ensure_persona_profile", _ensure_persona_profile)

    def _write_memory(envelope, **kwargs):
        envelopes.append((envelope, kwargs))
        return SimpleNamespace(validation_status="committed", path=f"mem-{len(envelopes)}")

    def _add_memory_for_person(_name, _payload, **kwargs):
        person_calls.append(kwargs)
        return "mem-person"

    monkeypatch.setattr("memory.api.write_memory", _write_memory)
    monkeypatch.setattr(persona_router, "add_memory_for_person", _add_memory_for_person)

    result = persona_ingest.ingest_persona_statement(
        "I study cybersecurity and have a son named Abel.",
        source_citation=citation,
    )

    assert len(result.records) == 4
    assert len(envelopes) == 2
    for envelope, kwargs in envelopes:
        assert envelope.source == "memory.persona_ingest"
        assert {key: envelope.payload[key] for key in citation} == citation
        assert kwargs["propose"] is False
        assert kwargs["bypass_guardian"] is True
    assert person_calls == [
        {
            "type": "persona_relationship",
            "cycle_id": person_calls[0]["cycle_id"],
            "confidence": 0.9,
            "human_sourced": True,
            "source_citation": citation,
        }
    ]
    assert profile_calls == [{"source_citation": citation}]
