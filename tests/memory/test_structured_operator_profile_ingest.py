from __future__ import annotations

from memory.persona_ingest import IngestedRecord


def test_structured_profile_becomes_discrete_persona_lobe_leaves(monkeypatch):
    import memory.persona_ingest as persona_ingest

    calls = []

    def fake_write(speaker, source_text, entity, *, source_citation=None):
        calls.append((speaker, source_text, dict(entity), dict(source_citation or {})))
        return IngestedRecord(
            kind="operator_attribute",
            facet=entity["facet"],
            value=entity["name"],
            coord=[1, 0, 0],
            tags=[f"profile:{entity['facet']}", "operator_profile"],
        )

    monkeypatch.setattr(persona_ingest, "_write_operator_attribute", fake_write)
    citation = {"source_id": "src-profile", "source_type": "operator_onboarding"}
    profile = {
        "name": "Ada Lovelace",
        "preferred_name": "Ada",
        "birthday": "1815-12-10",
        "pronouns": "she/her",
        "location": "London",
        "timezone": "Europe/London",
        "occupation": "mathematician",
        "relationship": "operator",
        "notes": "Interested in analytical engines",
        "picture_path": "/guardian/state/uploads/images/ada.png",
    }

    result = persona_ingest.ingest_operator_profile(profile, source_citation=citation)

    assert [record.coord for record in result.records] == [[1, 0, 0]] * len(profile)
    assert {call[2]["facet"] for call in calls} == set(profile)
    assert all(call[0] == "operator" for call in calls)
    assert all(call[3] == citation for call in calls)
    private_by_facet = {call[2]["facet"]: call[2]["private"] for call in calls}
    assert private_by_facet["birthday"] is True
    assert private_by_facet["notes"] is True
    assert private_by_facet["picture_path"] is True
    assert private_by_facet["occupation"] is False


def test_onboarding_notes_are_decomposed_into_atomic_persona_facts(monkeypatch):
    import memory.persona_ingest as persona_ingest

    operator_calls = []

    def fake_operator_write(speaker, source_text, entity, *, source_citation=None):
        operator_calls.append(dict(entity))
        return IngestedRecord(
            kind="operator_attribute",
            facet=entity["facet"],
            value=entity["name"],
            coord=[1, 0, 0],
            tags=[f"profile:{entity['facet']}", "operator_profile"],
        )

    monkeypatch.setattr(persona_ingest, "_write_operator_attribute", fake_operator_write)
    profile = {
        "name": "Noah Lee",
        "occupation": "software_engineer",
        "notes": "I own a business called Omega AI LLC.",
    }

    result = persona_ingest.ingest_operator_profile(profile)

    facets = [(record.facet, str(record.value).lower()) for record in result.records]
    assert ("notes", "i own a business called omega ai llc.") in facets
    assert ("business", "omega ai llc") in facets
    assert [call["facet"] for call in operator_calls].count("notes") == 1
    assert [call["facet"] for call in operator_calls].count("business") == 1


def test_onboarding_business_fact_survives_unavailable_language_parser(monkeypatch):
    import memory.persona_ingest as persona_ingest

    operator_calls = []

    def fake_operator_write(speaker, source_text, entity, *, source_citation=None):
        operator_calls.append(dict(entity))
        return IngestedRecord(
            kind="operator_attribute",
            facet=entity["facet"],
            value=entity["name"],
            coord=[1, 0, 0],
            tags=[f"profile:{entity['facet']}", "operator_profile"],
        )

    monkeypatch.setattr(persona_ingest, "_write_operator_attribute", fake_operator_write)
    monkeypatch.setattr(
        persona_ingest,
        "ingest_persona_statement",
        lambda *args, **kwargs: persona_ingest.PersonaIngestResult(
            speaker="operator", text=str(args[0]), records=[]
        ),
    )

    result = persona_ingest.ingest_operator_profile(
        {
            "name": "Noah Lee",
            "notes": "I also own a company called Omega AI LLC.",
        }
    )

    assert ("business", "omega ai llc") in [
        (record.facet, str(record.value).lower()) for record in result.records
    ]
    assert [call["facet"] for call in operator_calls].count("business") == 1
