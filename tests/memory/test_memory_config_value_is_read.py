"""§7-P1-3(c) — guard the config-in-wrong-file / silent-yaml-load-failure class.

The prior HIGH-1 decay_rate bug shipped a green suite: a tunable sat in the
WRONG file (never read), live code ran on the hardcoded default, and every
"behavior happens" test passed straight through. The promotion-confidence gate
has the identical exposure — ``memory.promotion.observation_to_episodic.min_confidence``
is declared in ``config/memory.yaml``, read by ``core.memory_config`` via
``config.loader.load_subsystem_bundle``, and consumed by
``memory.promotion.verify_promotion`` (the ``promotion_confidence`` check).

The existing ``test_verify_promotion_confidence.py`` only exercises the gate at
the DEFAULT value (0.50), so it cannot distinguish "value read from config" from
"hardcoded default that happens to equal config". These tests close that gap by
pinning a NON-DEFAULT value through the loader and asserting the consuming rule
moves with it — in BOTH directions of the gate.

Fail-mode pinned: if the config value is silently ignored (wrong file, yaml load
failure swallowed, reader hardcoding the default), the gate keeps deciding at
0.50. A confidence of 0.55 then PASSES (default 0.50) instead of FAILING under a
configured 0.70 floor — the exact silent drift the HIGH-1 class hides.
"""

from __future__ import annotations

import json
import uuid

import pytest

from config import loader
from core import memory_config as mcfg
from memory.promotion import create_staging, verify_promotion

# The hardcoded default in core/memory_config._DEFAULTS. The whole point of
# this guard is to prove the live gate does NOT silently fall back to this.
_DEFAULT_MIN_CONFIDENCE = 0.50

# A floor that is unambiguously ABOVE the default, chosen so a single
# confidence sample (0.55) lands on opposite sides of default vs configured.
_NON_DEFAULT_MIN_CONFIDENCE = 0.70

# A second floor BELOW the default, so the same 0.30 sample flips from
# "fail under default" to "pass under config" — proving the gate tracks the
# configured value downward too, not just upward.
_LOW_NON_DEFAULT_MIN_CONFIDENCE = 0.25

# Sample confidence between the default (0.50) and the high floor (0.70).
# Under the default it passes; under the configured floor it must fail.
_BETWEEN_SAMPLE = 0.55

# Sample confidence below the default (0.50) but above the low floor (0.25).
# Under the default it fails; under the configured low floor it must pass.
_BELOW_DEFAULT_SAMPLE = 0.30


@pytest.fixture(autouse=True)
def _reset_caches():
    """Drop the loader + memory_config caches around every test so the
    monkeypatched bundle is the one the reader sees, and no override leaks."""
    loader.clear_cache()
    mcfg.reset_memory_config_for_tests()
    yield
    loader.clear_cache()
    mcfg.reset_memory_config_for_tests()


@pytest.fixture
def staging_dir(tmp_path, monkeypatch):
    """Isolate promotion staging writes to a tmp dir."""
    import memory.promotion as prom

    path = tmp_path / "staging"
    path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(prom, "STAGING_DIR", path)
    return path


def _override_min_confidence(monkeypatch, value: float) -> None:
    """Force config/memory.yaml's observation_to_episodic.min_confidence to
    ``value`` AS IF it were read from disk — through the exact loader seam
    core.memory_config uses. Resets the memory_config cache so the next read
    rebuilds from the patched bundle.

    Patching ``load_subsystem_bundle`` (not the on-disk file) is what makes
    this a config-IS-READ assertion: if the reader ignored config and used the
    hardcoded default, this override would have no effect and the gate
    direction would not flip.
    """
    real_bundle = loader.load_subsystem_bundle

    def _patched_bundle(stem: str):
        bundle = real_bundle(stem)
        if stem != "memory":
            return bundle
        # Deep-copy the relevant nesting so we don't mutate the loader cache.
        cfg = dict(bundle.get("config") or {})
        memory_block = dict(cfg.get("memory") or {})
        promotion = dict(memory_block.get("promotion") or {})
        obs = dict(promotion.get("observation_to_episodic") or {})
        obs["min_confidence"] = value
        promotion["observation_to_episodic"] = obs
        memory_block["promotion"] = promotion
        cfg["memory"] = memory_block
        return {"config": cfg, "policy": bundle.get("policy")}

    monkeypatch.setattr(loader, "load_subsystem_bundle", _patched_bundle)
    mcfg.reset_memory_config_for_tests()


def _envelope(*, confidence: float) -> dict:
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


def _promotion_confidence_check(result: dict) -> dict | None:
    for check in result.get("checks") or []:
        if check.get("name") == "promotion_confidence":
            return check
    return None


# ---------------------------------------------------------------------------
# Reader layer — core.memory_config actually surfaces the CONFIGURED value,
# not the hardcoded _DEFAULTS. (The narrowest pin of the HIGH-1 class.)
# ---------------------------------------------------------------------------


def test_reader_surfaces_configured_min_confidence(monkeypatch):
    """observation_to_episodic_rules() returns the value the loader provides,
    not the _DEFAULTS constant. Proves the YAML→reader wire is live."""
    # Sanity: the default really is what we think (else the test is hollow).
    assert mcfg.observation_to_episodic_rules().get("min_confidence") == _DEFAULT_MIN_CONFIDENCE

    _override_min_confidence(monkeypatch, _NON_DEFAULT_MIN_CONFIDENCE)

    assert (
        mcfg.observation_to_episodic_rules().get("min_confidence") == _NON_DEFAULT_MIN_CONFIDENCE
    ), "reader returned the hardcoded default instead of the configured value"


# ---------------------------------------------------------------------------
# Consuming rule — verify_promotion's promotion_confidence gate moves with the
# configured floor. BOTH directions of the gate, at the same sample.
# ---------------------------------------------------------------------------


def test_gate_uses_configured_value_not_hardcoded_default(staging_dir, monkeypatch):
    """A 0.55 confidence PASSES under the default 0.50 but must FAIL once the
    configured floor is raised to 0.70. If the gate ignored config and used the
    hardcoded default, this would wrongly pass — the silent-drift fail-mode."""
    # Direction proof 1: under the default, 0.55 passes (control).
    ref_default = create_staging(_envelope(confidence=_BETWEEN_SAMPLE))
    default_result = verify_promotion(ref_default)
    assert default_result["status"] == "passed", (
        "control failed: 0.55 should pass the default 0.50 gate"
    )

    # Now raise the configured floor above the sample.
    _override_min_confidence(monkeypatch, _NON_DEFAULT_MIN_CONFIDENCE)

    ref = create_staging(_envelope(confidence=_BETWEEN_SAMPLE))
    result = verify_promotion(ref)

    assert result["status"] == "failed", (
        "gate did not honor the configured min_confidence — it is reading the "
        "hardcoded default (the HIGH-1 config-in-wrong-file fail-mode)"
    )
    # The reason must cite the CONFIGURED floor (0.700), not the default.
    reason = str(result.get("reason") or "")
    assert "0.700" in reason, (
        f"failure reason did not reflect the configured floor 0.700: {reason!r}"
    )
    check = _promotion_confidence_check(result)
    assert check is not None and check["passed"] is False


def test_gate_tracks_configured_value_downward(staging_dir, monkeypatch):
    """The opposite direction: a 0.30 confidence FAILS under the default 0.50
    but must PASS once the configured floor is lowered to 0.25. Pins that the
    gate follows config DOWN as well as up — a one-directional read could fake
    the upward test."""
    # Control: under the default, 0.30 fails.
    ref_default = create_staging(_envelope(confidence=_BELOW_DEFAULT_SAMPLE))
    default_result = verify_promotion(ref_default)
    assert default_result["status"] == "failed", (
        "control failed: 0.30 should fail the default 0.50 gate"
    )

    # Lower the configured floor below the sample.
    _override_min_confidence(monkeypatch, _LOW_NON_DEFAULT_MIN_CONFIDENCE)

    ref = create_staging(_envelope(confidence=_BELOW_DEFAULT_SAMPLE))
    result = verify_promotion(ref)

    assert result["status"] == "passed", (
        "gate did not track the lowered configured min_confidence — it is "
        "reading the hardcoded default instead of config"
    )
    check = _promotion_confidence_check(result)
    assert check is not None and check["passed"] is True
