"""CLI surface tests for boot.py — agent-friendly flags and help."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import boot  # noqa: E402


def test_main_json_requires_once() -> None:
    with pytest.raises(SystemExit):
        boot.main(["--json"])


def test_main_once_json_emits_readiness(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        boot,
        "boot",
        lambda: {"runtime": "up", "memory": "up", "guardian": "up", "summary": "ok"},
    )
    # run_once lazy-imports run_pressure_tick_once from pressure_loop.loop, so
    # patch the source module (not a boot.* attr, which does not exist).
    monkeypatch.setattr("pressure_loop.loop.run_pressure_tick_once", lambda _rt: None)
    monkeypatch.setattr(
        "pressure_loop.runtime.get_default_runtime",
        lambda: object(),
    )

    rc = boot.main(["--once", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["mode"] == "once"
    assert payload["runtime"] == "up"
    assert payload["exit_code"] == 0


def test_run_continuous_crashes_when_cognition_runtime_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-fast boot: if gemma will not load at 64k or 25k, the cognition loop
    must NEVER start — run_continuous propagates RuntimeCognitionUnavailable
    (boot.py ties into start.bat: the whole startup crashes)."""
    import core.boot_lock as boot_lock
    import core.boot_lifecycle as boot_lifecycle
    import hud.spawner as spawner
    import pressure_loop.loop as ploop
    import pressure_loop.policy as policy
    import pressure_loop.runtime as pruntime
    import runtime.model_control as mc

    monkeypatch.setattr(boot_lock, "acquire", lambda *a, **k: None)
    monkeypatch.setattr(policy, "assert_pressure_boot_allowed", lambda *a, **k: None)
    monkeypatch.setattr(boot, "boot", lambda *a, **k: {"runtime": "down"})
    # This test isolates the cognition gate. Substrate restore is an earlier,
    # independently required boot phase and must be satisfied explicitly.
    monkeypatch.setattr(
        boot_lifecycle,
        "restore_substrate_state",
        lambda _runtime: {"shared_state": {}, "substrate_snapshot": {}},
    )
    monkeypatch.setattr(spawner, "spawn_hud_in_thread", lambda *a, **k: None)
    monkeypatch.setattr(spawner, "shutdown_hud", lambda *a, **k: None)
    monkeypatch.setattr(pruntime, "get_default_runtime", lambda: object())

    loop_calls = {"n": 0}
    monkeypatch.setattr(
        ploop,
        "run_pressure_tick_loop",
        lambda *a, **k: loop_calls.__setitem__("n", loop_calls["n"] + 1),
    )

    def _boom(*a, **k):
        raise mc.RuntimeCognitionUnavailable("cognition runtime unreachable")

    # Boot gates on REACHABILITY, not loadability: it probes and loads nothing, so
    # the tick loop must still refuse to start when that probe fails.
    monkeypatch.setattr(mc, "probe_cognition_runtime", _boom)

    with pytest.raises(mc.RuntimeCognitionUnavailable):
        boot.run_continuous()
    assert loop_calls["n"] == 0  # the loop must never have started


def test_main_help_includes_examples(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        boot.main(["--help"])
    out = capsys.readouterr().out
    assert ".\\.venv-brain\\Scripts\\python.exe boot.py --once --json" in out
    assert ".\\.venv-brain\\Scripts\\python.exe scripts\\shutdown.py --dry-run" in out
