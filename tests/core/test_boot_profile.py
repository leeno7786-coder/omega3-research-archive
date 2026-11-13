"""The live boot profile — the portable venv command boots the fully-configured version.

`_apply_live_boot_profile()` sets the operator-confirmed flag defaults via setdefault, so an
explicit env var always overrides. Flags deliberately left off (FAST_REPLY, PRODUCTION_MODE)
must NOT be set. These pin that contract (with env save/restore so nothing leaks).
"""

from __future__ import annotations

import os

import boot


def _run_with_clean_env(keys, mutate=None):
    saved = {k: os.environ.get(k) for k in keys}
    try:
        for k in keys:
            os.environ.pop(k, None)
        if mutate:
            mutate()
        boot._apply_live_boot_profile()
        return {k: os.environ.get(k) for k in keys}
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


_OFF = ("MIND_PRIMACY_SUSTAIN_ENABLED", "OMEGA_PRODUCTION_MODE")


def test_profile_sets_the_live_flags():
    keys = list(boot._LIVE_BOOT_PROFILE) + ["OMEGA_OPERATOR_WORKSPACE", *_OFF]
    env = _run_with_clean_env(keys)
    for k, v in boot._LIVE_BOOT_PROFILE.items():
        assert env[k] == v, k
    assert env["OMEGA_TRUSTED_FOLDERS_REQUIRED"] == "1"
    assert env["OMEGA_OPERATOR_WORKSPACE"] is None  # no implicit workspace
    # the mind-driven retirements stay off; production is never auto-set
    for k in _OFF:
        assert env[k] is None, f"{k} must NOT be auto-set"


def test_profile_does_not_create_implicit_workspace():
    env = _run_with_clean_env(["OMEGA_OPERATOR_WORKSPACE", "OMEGA_TRUSTED_FOLDERS_REQUIRED"])
    assert env["OMEGA_OPERATOR_WORKSPACE"] is None
    assert env["OMEGA_TRUSTED_FOLDERS_REQUIRED"] == "1"


def test_profile_enables_native_citizenship_without_legacy_embed_engine():
    """The live memory organ uses the brain-resident Ettin retina, not Gemma."""
    assert boot._LIVE_BOOT_PROFILE.get("OMEGA_MEMORY_NATIVE_CITIZENSHIP") == "1"
    assert "OMEGA_EMBED_ENGINE" not in boot._LIVE_BOOT_PROFILE
    env = _run_with_clean_env(
        ["OMEGA_MEMORY_NATIVE_CITIZENSHIP"],
        mutate=lambda: os.environ.__setitem__("OMEGA_MEMORY_NATIVE_CITIZENSHIP", "0"),
    )
    assert env["OMEGA_MEMORY_NATIVE_CITIZENSHIP"] == "0"
