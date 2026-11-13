"""verify_promotion enforces memory.yaml observation_to_episodic gate."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

from config import loader
from core import memory_config as mcfg
from memory.promotion import create_staging, verify_promotion


@pytest.fixture(autouse=True)
def _reset_caches():
    loader.clear_cache()
    mcfg.reset_memory_config_for_tests()
    yield
    loader.clear_cache()
    mcfg.reset_memory_config_for_tests()


@pytest.fixture
def staging_dir(tmp_path, monkeypatch):
    import memory.promotion as prom

    path = tmp_path / "staging"
    path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(prom, "STAGING_DIR", path)
    return path


def _envelope(*, confidence: float = 0.5) -> dict:
    suffix = uuid.uuid4().hex[:8]
    mem_id = f"9.01.{suffix}"
    return {
        "cycle_id": f"cycle-{suffix}",
        "id": mem_id,
        "coord": [9, 1, 0],
        "type": "episode",
        "payload": {
            "id": mem_id,
            "title": f"ep-{suffix}",
            "summary": f"episode {suffix}",
            "text": json.dumps({"episode_id": f"ep-{suffix}", "intent": "LEARN"}),
        },
        "source": "lobe:temporal",
        "confidence": confidence,
        "provenance": {"origin_id": "test", "confidence": confidence},
        "version": 1,
    }


def test_verify_promotion_passes_at_min_confidence(staging_dir):
    ref = create_staging(_envelope(confidence=0.5))
    result = verify_promotion(ref)
    names = [c["name"] for c in result.get("checks") or []]
    assert result["status"] == "passed"
    assert "promotion_confidence" in names


def test_verify_promotion_fails_below_min_confidence(staging_dir):
    ref = create_staging(_envelope(confidence=0.2))
    result = verify_promotion(ref)
    assert result["status"] == "failed"
    assert "below min_confidence" in str(result.get("reason") or "")
