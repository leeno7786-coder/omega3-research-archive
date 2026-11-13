"""Fail-hard cognition in the DeepSleep continuity stores.

A continuity store that is MISSING (first boot) is absence → empty. A store that
EXISTS but is corrupt (unparseable) is continuity corruption → it CRASHES, it is
not silently read as empty (which would make the mind reason on a falsified past).
"""

from __future__ import annotations

import json

import pytest

from deepsleep_loop.trajectory_store import load_trajectory


def test_load_trajectory_missing_file_is_absence(tmp_path):
    # Absence is not failure: a not-yet-created store reads as empty.
    assert load_trajectory(path=tmp_path / "does_not_exist.json") == {}


def test_load_trajectory_corrupt_file_crashes(tmp_path):
    # Corruption on an EXISTING continuity store crashes (it is not swallowed to {}).
    bad = tmp_path / "trajectory.json"
    bad.write_text("{ this is not valid json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        load_trajectory(path=bad)
