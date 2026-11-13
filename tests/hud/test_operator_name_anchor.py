"""Operator name ABOVE ALL: saving the operator must also write a prominent,
RECALLABLE anchor for their name — non-private (so the recall lane doesn't filter
it, unlike the private profile blob), strength 1.0, and bridging <name> == the
operator so relational memories ("the operator's son") resolve to "<name>'s son".
This closes the named-operator recall gap.
"""

from __future__ import annotations

import inspect

import pytest

import hud.operator_identity as oi
import hud.settings as hud_settings


@pytest.fixture(autouse=True)
def _allow_profile_ingress(monkeypatch):
    monkeypatch.setattr(
        hud_settings,
        "_gate_operator_profile_ingress",
        lambda payload: {
            "decision": "allow",
            "payload": dict(payload),
            "citation": {"source_id": "src-test", "source_type": "operator_onboarding"},
            "guardian": {"screened_fields": 1, "decisions": {}},
        },
        raising=False,
    )


def test_name_anchor_envelope_is_prominent_recallable_and_bridges_operator():
    env = hud_settings.build_operator_name_anchor_envelope("Avery Jones")
    p = env.payload

    assert env.type == "identity_anchor"
    assert env.coord == [1, 0, 0]
    assert env.strength_index == 1.0  # above all
    assert env.confidence == 1.0
    assert p["title"] == "Operator name"  # stable title allows name revisions to supersede
    assert p.get("private") is False  # recallable — NOT lane-filtered
    assert "Avery Jones" in p["summary"]
    # Bridges the named user to operator so "operator's son" is name-bearing.
    text = p["text"].lower()
    assert "avery jones" in text and "operator" in text
    assert "operator_name" in p["tags"]


def test_save_operator_profile_queues_the_name_anchor(monkeypatch):
    queued = []
    monkeypatch.setattr(oi, "is_onboarded", lambda: False)
    monkeypatch.setattr(
        "pressure_loop.cognition_queue.enqueue_input",
        lambda contributor, **kwargs: queued.append(contributor) or True,
    )

    result = hud_settings.save_operator_profile(
        {"name": "Avery Jones", "occupation": "cybersecurity"}
    )

    assert result["ok"] is True
    [contributor] = queued
    anchors = [
        env for env in contributor.metadata["memory_envelopes"] if env["type"] == "identity_anchor"
    ]
    assert len(anchors) == 1
    anchor = anchors[0]
    assert anchor["payload"].get("private") is False
    assert anchor["strength_index"] == 1.0
    assert "Avery Jones" in anchor["payload"]["summary"]


def test_name_anchor_prefers_preferred_name(monkeypatch):
    queued = []
    monkeypatch.setattr(oi, "is_onboarded", lambda: False)
    monkeypatch.setattr(
        "pressure_loop.cognition_queue.enqueue_input",
        lambda contributor, **kwargs: queued.append(contributor) or True,
    )

    hud_settings.save_operator_profile({"name": "Avery Jones", "preferred_name": "Riley"})

    anchor = next(
        env
        for env in queued[0].metadata["memory_envelopes"]
        if env["type"] == "identity_anchor"
    )
    assert "Riley" in anchor["payload"]["summary"]


def test_boot_never_writes_a_compatibility_anchor_outside_an_episode():
    import boot

    assert "ensure_operator_name_anchor" not in inspect.getsource(boot.boot)
