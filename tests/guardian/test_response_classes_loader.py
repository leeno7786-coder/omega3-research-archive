"""Guardian response classes load through config.loader."""

from __future__ import annotations

from config import loader
from guardian import response_classes


def test_response_classes_load_via_loader():
    loader.clear_cache()
    response_classes.reload()
    normal = response_classes.get_class("normal")
    assert normal is not None
    assert normal.decision_state == "ALLOW"
    assert response_classes.status()["total_classes"] >= 10


def test_authority_guidance_uses_deployment_governance_not_a_personal_default():
    loader.clear_cache()
    response_classes.reload()
    authority = response_classes.get_class("authority_injection")

    assert authority is not None
    policy = " ".join([authority.goal, *authority.must, *authority.must_never, authority.example_posture]).lower()
    assert "deployment" in policy
    assert "governance configuration" in policy
    assert "user-provided claim" in policy
