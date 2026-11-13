"""Pressure reads config/pressure.yaml as the base, overlaid per-key by
policy/symphony.policy.yaml — the same merge every other section uses.

 P0-2 (BUILD_STATUS_2026-06-12 §7.2). Before the fix, ``_pressure_section``
 returned policy/symphony.policy.yaml's ``pressure`` block *wholesale* (config/pressure.yaml
was read only when the overlay was empty). Two footguns followed:

  1. SHADOWING — any pressure tunable set only in config/pressure.yaml was dead
     in production (policy/symphony.policy.yaml silently won), the exact mechanism of the
     prior HIGH-1 ``decay_rate``-in-the-wrong-file bug.
These tests pin the remaining pressure-policy contract: the config base must
surface under the symphony overlay, the overlay must still win per-key, and the
live values must be unchanged. Cognition stages are not policy flags.
"""

from __future__ import annotations

import pytest

from pressure_loop import policy
from pressure_loop.policy import (
    clear_policy_cache,
    pressure_neuromod_tonic_decay,
    pressure_neuromod_weight_modulation,
    pressure_open_threshold,
    pressure_winner_margin,
)


def _safe_clear() -> None:
    # clear_policy_cache() calls .cache_clear() on the lru-cached loaders; a
    # test may monkeypatch one of those loaders to a plain lambda (which has no
    # cache_clear), so tolerate that during setup/teardown.
    try:
        clear_policy_cache()
    except AttributeError:
        pass


@pytest.fixture(autouse=True)
def _clear_caches():
    _safe_clear()
    yield
    _safe_clear()


# --- live values are unchanged by the merge (regression guard) -------------


def test_live_pressure_tunables_unchanged():
    assert pressure_open_threshold() == pytest.approx(0.10)
    assert pressure_winner_margin() == pytest.approx(0.05)
    assert pressure_neuromod_tonic_decay()["decay_rate"] == pytest.approx(0.4)
    assert pressure_neuromod_weight_modulation()["dopamine_coeff"] == pytest.approx(0.5)


# --- shadowing is killed: a config-only key surfaces under the overlay ------


def test_config_only_key_is_live_under_symphony_overlay(monkeypatch):
    """A neuromod tunable present ONLY in config/pressure.yaml (the base) must
      reach the live helper even though policy/symphony.policy.yaml (the overlay) omits it.
    Pre-fix the overlay was returned wholesale and the base was discarded."""
    base = {
        "open_threshold": 0.10,
        "neuromod": {
            "weight_modulation": {
                "enabled": True,
                "dopamine_coeff": 0.99,  # sentinel, != hardcoded default 0.5
                "gamma_min": 0.25,
                "gamma_max": 2.5,
            }
        },
    }
 # policy/symphony.policy.yaml overlay omits weight_modulation entirely.
    overlay = {"pressure": {"open_threshold": 0.10}}
    monkeypatch.setattr(policy, "_load_pressure_config_yaml", lambda: dict(base))
    cfg = pressure_neuromod_weight_modulation(overlay)
    assert cfg["dopamine_coeff"] == pytest.approx(0.99)


 # --- policy/symphony.policy.yaml overlay still wins per-key over the config base ----------


def test_symphony_overlay_wins_over_config_base(monkeypatch):
    base = {"open_threshold": 0.10, "winner_margin": 0.05}
    overlay = {"pressure": {"open_threshold": 0.99}}
    monkeypatch.setattr(policy, "_load_pressure_config_yaml", lambda: dict(base))
    assert pressure_open_threshold(overlay) == pytest.approx(0.99)
