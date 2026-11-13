"""Operator identity reads the HUD settings persona store."""

from __future__ import annotations

import json

from hud import operator_identity
from hud import settings as hud_settings


def test_operator_context_returns_empty_dict_when_no_persona(monkeypatch, tmp_path):
    monkeypatch.setattr(operator_identity, "_OPERATOR_PERSONA_DIR", tmp_path / "missing")
    assert operator_identity.operator_context() == {}


def test_operator_context_fails_loudly_on_bad_data(monkeypatch, tmp_path):
    import pytest

    persona_dir = tmp_path / "operator"
    persona_dir.mkdir(parents=True)
    (persona_dir / "bad.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(operator_identity, "_OPERATOR_PERSONA_DIR", persona_dir)
    with pytest.raises(operator_identity.OperatorProfileUnreadable, match="record_unreadable"):
        operator_identity.operator_context()


def test_onboarding_fails_loudly_when_profile_scan_is_unreadable(monkeypatch, tmp_path):
    import pytest

    persona_dir = tmp_path / "operator"
    persona_dir.mkdir(parents=True)
    monkeypatch.setattr(operator_identity, "_OPERATOR_PERSONA_DIR", persona_dir)

    def unreadable_walk(_path, *, onerror, followlinks):
        assert followlinks is False
        onerror(PermissionError("profile directory cannot be read"))
        return iter(())

    monkeypatch.setattr(operator_identity.os, "walk", unreadable_walk)
    with pytest.raises(
        operator_identity.OperatorProfileUnreadable, match="directory_unreadable"
    ):
        operator_identity.is_onboarded()


def test_operator_context_round_trip_from_settings_envelope(monkeypatch, tmp_path):
    persona_dir = tmp_path / "operator"
    persona_dir.mkdir(parents=True)
    monkeypatch.setattr(operator_identity, "_OPERATOR_PERSONA_DIR", persona_dir)

    envelope, _ = hud_settings.build_operator_profile_envelope(
        {
            "name": "Avery Jones",
            "preferred_name": "Avery",
            "role": "operator",
        }
    )
    record = {
        "coord": envelope.coord,
        "enrichment": envelope.payload.get("enrichment"),
        "provenance": {"ingested_at": envelope.timestamp_created},
        "timestamp_created": envelope.timestamp_created,
    }
    profile_dir = persona_dir / str(envelope.payload.get("subtopic") or "profile")
    profile_dir.mkdir(parents=True)
    (profile_dir / "profile.json").write_text(json.dumps(record), encoding="utf-8")

    ctx = operator_identity.operator_context()
    assert ctx == {"name": "Avery", "relationship": "operator"}


def test_operator_context_importable():
    from hud.operator_identity import operator_context

    assert callable(operator_context)
    assert isinstance(operator_context(), dict)
