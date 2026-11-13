"""Phase 0 — neuromod config foundation.

The live neuromod config surface is dopamine. The raw loader schema-validates
and raises on a bad YAML, but the neuromod formula must never raise, so the
accessor degrades to documented defaults instead.
"""

from __future__ import annotations

import pytest

from autonomic import neuromod_config as nc
from autonomic.symphony_neuromod import compute_episode_neuromod


def test_dopamine_accessor_reaches_live_config():
    dop = nc.dopamine_cfg()
    assert dop.get("limits", {}).get("min") == -1.0
    assert "thresholds" in dop and "expectation" in dop


def test_dopamine_limits_default_matches_yaml():
    assert nc.dopamine_limits() == (-1.0, 1.0)


def test_dopamine_thresholds_default_matches_yaml():
    # The clearly-rewarding / clearly-punished RPE gate (Phase 1 reinforce uses it).
    assert nc.dopamine_thresholds() == (0.20, -0.20)


def test_dopamine_reinforce_scale_default_matches_yaml():
    # dopamine RPE -> write_strength delta scale (newly-live config key).
    assert nc.dopamine_reinforce_scale() == 0.10


def test_accessor_is_failure_soft(monkeypatch):
    # A raising loader (missing/malformed yaml) must degrade to {} / defaults, never propagate —
    # otherwise compute_episode_neuromod's outer try would silently zero out all cognition.
    def _boom():
        raise RuntimeError("malformed config")

    monkeypatch.setattr("config.loader.load_dopamine_config", _boom)
    assert nc.dopamine_cfg() == {}
    assert nc.dopamine_limits() == (-1.0, 1.0)


def test_num_getter_walks_path_with_defaults():
    assert nc._num({}, "a.b", 0.6) == 0.6
    assert nc._num({"a": {"b": 0.3}}, "a.b", 0.6) == 0.3
    assert nc._num({"a": {"b": "nan-ish"}}, "a.b", 0.6) == 0.6  # garbage -> default


def test_dopamine_clamp_is_behavior_neutral_on_defaults():
    # rpe is already in [-1, 1]; clamping to the default limits leaves the formula byte-identical.
    assert compute_episode_neuromod({"cycle_kind": "success"}, 0.0)["dopamine"] == pytest.approx(
        1.0
    )
    assert compute_episode_neuromod({"cycle_kind": "fail"}, 1.0)["dopamine"] == pytest.approx(-1.0)
    assert compute_episode_neuromod({"cycle_kind": "success"}, 0.5)["dopamine"] == pytest.approx(
        0.5
    )
