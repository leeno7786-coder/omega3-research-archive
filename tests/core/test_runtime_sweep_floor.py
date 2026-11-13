"""The test suite may never kill the operator's live appliance.

Measured on 2026-08-11. The runtime supervisor owns the local-services process
and must resolve its default product root from the active Omega profile. Tests
must never turn a default teardown into an operation against a developer's
separate runtime bundle. These pin that default and the explicit test seam.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import boot_lifecycle as bl  # noqa: E402
from pressure_loop.runtime import PressureRuntime  # noqa: E402
from runtime import supervisor  # noqa: E402

LIVE_BUNDLE = ROOT.parent / "omega-runtime"


@pytest.fixture
def launches(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Every command the code under test tries to launch, launched by nobody."""
    seen: list[list[str]] = []

    class _Done:
        returncode = 0
        stdout = ""
        stderr = ""

    def _spy(args, *a, **k):
        seen.append([str(x) for x in args])
        return _Done()

    monkeypatch.setattr(subprocess, "run", _spy)
    return seen


def _live_sweeps(launches: list[list[str]]) -> list[list[str]]:
    """Only launches aimed at the OPERATOR'S bundle — a tmp one is harmless."""
    return [cmd for cmd in launches if any(str(LIVE_BUNDLE) in part for part in cmd)]


def test_the_full_reverse_walk_never_reaches_the_live_sweep(
    launches: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact shape that leaked: record every component, then shut down.

    Both gates open at once here — ``boot_lock`` licenses
    ``_run_runtime_teardown`` and ``runtime_process`` licenses
    ``shutdown_runtime_process`` — which is precisely what
    ``test_shutdown_reverses_boot_order`` does. Nothing may reach the operator's
    bundle anyway.
    """
    bl.reset_lifecycle_state()
    for component in bl.BOOT_COMPONENTS:
        bl.record_started(component)
    monkeypatch.setattr(bl, "persist_substrate_state", lambda *_a, **_k: {})
    monkeypatch.setattr("core.boot_lock.release", lambda: None)

    bl.shutdown_system(reason="test", runtime=PressureRuntime())

    assert _live_sweeps(launches) == [], (
        "a pytest run reached omega-runtime's real stop script — that force-kills "
        "the operator's live gateway and every worker on the box"
    )


def test_the_ownership_independent_sweep_cannot_find_the_live_bundle(
    launches: list[list[str]],
) -> None:
    """``shutdown_runtime_process`` sweeps even with nothing owned — that is its
    whole point (it catches a runtime orphaned by a crashed boot). Under test it
    must find a sandbox, not the appliance."""
    supervisor.shutdown_runtime_process()

    assert _live_sweeps(launches) == []


def test_an_explicit_runtime_root_still_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The floor changes what the DEFAULT means, nothing else.

    ``tests/runtime/test_runtime_supervisor.py`` points OMEGA_RUNTIME_ROOT at a
    tmp bundle and expects the supervisor to use it. A floor that ignored the
    override would silently gut that suite.
    """
    monkeypatch.setenv("OMEGA_RUNTIME_ROOT", str(tmp_path / "bundle"))

    assert supervisor.runtime_root() == tmp_path / "bundle"


def test_the_default_root_is_not_the_operators_bundle() -> None:
    """With no override, both resolvers point somewhere that is not the appliance."""
    assert supervisor.runtime_root() != LIVE_BUNDLE


def test_the_boot_manifest_is_not_the_live_one() -> None:
    """The other live file a teardown test touches.

    ``record_started`` writes it and ``shutdown_system`` UNLINKS it, both
    unconditionally, so a suite that drives either leaves the appliance's boot
    record naming a dead pytest pid. Nothing in production reads it, which is
    why it went unnoticed — it cannot break a boot, only lie to the operator
    reading one.
    """
    from core.paths import GUARDIAN_STATE_ROOT

    assert bl.BOOT_MANIFEST_PATH != Path(GUARDIAN_STATE_ROOT) / "boot.manifest.json"


def test_driving_a_shutdown_does_not_touch_the_live_boot_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end: record + shut down, and the operator's file is untouched."""
    from core.paths import GUARDIAN_STATE_ROOT

    live = Path(GUARDIAN_STATE_ROOT) / "boot.manifest.json"
    before = live.stat().st_mtime_ns if live.exists() else None

    bl.reset_lifecycle_state()
    bl.record_started("boot_lock")
    bl.record_started("substrate")
    monkeypatch.setattr(bl, "persist_substrate_state", lambda *_a, **_k: {})
    monkeypatch.setattr("core.boot_lock.release", lambda: None)
    bl.shutdown_system(reason="test", runtime=PressureRuntime())

    after = live.stat().st_mtime_ns if live.exists() else None
    assert after == before, "a test rewrote or deleted the live boot manifest"
