"""Brain-owned runtime lifecycle (operator 2026-07-19).

Contract:
- Runtime already healthy -> no spawn, no sweep, no ownership.
- Runtime down + interpreter present -> spawn the product venv's own Python as
  ``-m runtime.local_services`` (never a console-script .exe whose baked
  absolute path dies on relocation) with the client's host:port, poll /health,
  and own the pid on success.
- Interpreter missing -> honest {"ok": False, "reason": "runtime_exe_missing"}
  — never a fake "up".
- Health timeout / early exit -> honest failure, no ownership.
- Shutdown gracefully stops the local-services process Omega spawned and falls
  back to a process-tree kill when needed. There is no second runtime bundle or
  external process sweep.
"""

from __future__ import annotations

import os
import sys
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from runtime import supervisor


@pytest.fixture(autouse=True)
def _clean_ownership():
    supervisor._reset_for_tests()
    yield
    supervisor._reset_for_tests()


@pytest.fixture(autouse=True)
def _isolate_gateway_log(monkeypatch, tmp_path_factory):
    """Keep the gateway log sink out of the live guardian/logs tree.

    Every spawn in this module now opens a real file. ``gateway_log_path``
    resolves GUARDIAN_LOGS_ROOT at call time, so reassigning the module
    attribute isolates every write with no production change — the same
    technique conftest's ``_isolate_telemetry_paths`` uses, extended to the
    new sink rather than left to leak into the operator's real logs.
    """
    import core.paths as paths

    root = tmp_path_factory.mktemp("gateway-logs")
    monkeypatch.setattr(paths, "GUARDIAN_LOGS_ROOT", root)
    return root


@pytest.fixture(autouse=True)
def _clean_shutdown_flag(monkeypatch):
    """No test may leave the lifecycle latched 'shutting down' for the next one."""
    import core.boot_lifecycle as lifecycle

    monkeypatch.setattr(lifecycle, "_shutdown_done", False)


class FakeClient:
    base_url = "http://127.0.0.1:11434"


class FakeProc:
    def __init__(self, pid: int = 7001):
        self.pid = pid
        self.returncode = None

    def poll(self):
        return self.returncode


def _runtime_bundle(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "omega-runtime"
    exe = supervisor._gateway_exe(root)
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"stub")
    monkeypatch.setattr(supervisor, "runtime_root", lambda: root)
    return root


def test_healthy_runtime_is_left_alone(monkeypatch, tmp_path):
    _runtime_bundle(tmp_path, monkeypatch)
    result = supervisor.ensure_runtime_process(
        client=FakeClient(),
        popen=lambda *a, **k: pytest.fail("spawned despite healthy runtime"),
        health_probe=lambda url: True,
        sweep=lambda root: pytest.fail("swept despite healthy runtime"),
    )
    assert result == {"ok": True, "spawned": False}
    assert supervisor.runtime_owned() is False


@pytest.mark.parametrize(
    ("status_code", "payload", "expected"),
    [
        (200, {"service": "omega-local-services"}, True),
        (200, {"service": "ollama"}, False),
        (200, {"status": "ok"}, False),
        (503, {"service": "omega-local-services"}, False),
    ],
)
def test_http_health_requires_the_omega_service_identity(
    monkeypatch, status_code, payload, expected
):
    import requests

    monkeypatch.setattr(
        requests,
        "get",
        lambda _url, timeout: SimpleNamespace(
            status_code=status_code,
            json=lambda: payload,
        ),
    )

    assert supervisor._http_health("http://127.0.0.1:11434") is expected


def test_down_runtime_sweeps_spawns_and_owns(monkeypatch, tmp_path):
    root = _runtime_bundle(tmp_path, monkeypatch)
    swept: list[Path] = []
    spawned: dict = {}
    probes = iter([False, False, True])  # down -> spawn -> healthy

    def popen(cmd, **kwargs):
        spawned["cmd"] = list(cmd)
        spawned["kwargs"] = kwargs
        return FakeProc(pid=7001)

    result = supervisor.ensure_runtime_process(
        client=FakeClient(),
        popen=popen,
        health_probe=lambda url: next(probes),
        sweep=swept.append,
        timeout_s=5.0,
        poll_interval_s=0,
    )
    assert result == {"ok": True, "spawned": True, "pid": 7001}
    assert swept == [root]
    assert spawned["cmd"][0] == str(supervisor._gateway_exe(root))
    assert spawned["cmd"][1:] == [
        "-m",
        "runtime.local_services",
        "--host",
        "127.0.0.1",
        "--port",
        "11434",
    ]
    assert spawned["kwargs"]["cwd"] == str(root)
    assert spawned["kwargs"]["env"]["OMEGA_MODEL_DIR"] == str(root / "models")
    assert supervisor.runtime_owned() is True


def test_down_runtime_can_spawn_from_its_bundled_python_without_a_console_launcher(
    monkeypatch, tmp_path
):
    from core.paths import portable_python_path

    root = tmp_path / "omega-runtime"
    python = portable_python_path(root)
    python.parent.mkdir(parents=True)
    python.write_bytes(b"stub")
    monkeypatch.setattr(supervisor, "runtime_root", lambda: root)
    probes = iter([False, True])
    spawned: dict = {}

    result = supervisor.ensure_runtime_process(
        client=FakeClient(),
        popen=lambda cmd, **kwargs: spawned.update(cmd=list(cmd), kwargs=kwargs)
        or FakeProc(pid=7003),
        health_probe=lambda url: next(probes),
        sweep=lambda _root: None,
        timeout_s=5.0,
        poll_interval_s=0,
    )

    assert result == {"ok": True, "spawned": True, "pid": 7003}
    assert spawned["cmd"] == [
        str(python),
        "-m",
        "runtime.local_services",
        "--host",
        "127.0.0.1",
        "--port",
        "11434",
    ]


def test_spawn_sets_skip_resident_llm_under_cloud_brain(monkeypatch, tmp_path):
    """Cloud brain seat: the spawned gateway is told NOT to warm/pin the local
    resident LLM (gemma) — cognition runs on the frontier 27B, so it is dead VRAM."""
    _runtime_bundle(tmp_path, monkeypatch)
    from runtime import brain_cloud

    monkeypatch.setattr(brain_cloud, "enabled", lambda: True)
    spawned: dict = {}
    probes = iter([False, True])
    supervisor.ensure_runtime_process(
        client=FakeClient(),
        popen=lambda cmd, **kwargs: spawned.update(kwargs) or FakeProc(pid=7101),
        health_probe=lambda url: next(probes),
        sweep=lambda root: None,
        timeout_s=5.0,
        poll_interval_s=0,
    )
    assert spawned["env"]["OMEGA_SKIP_RESIDENT_LLM"] == "1"


def test_spawn_omits_skip_resident_llm_for_local_brain(monkeypatch, tmp_path):
    """Local-brain boots leave the flag unset — the gateway keeps gemma resident."""
    _runtime_bundle(tmp_path, monkeypatch)
    monkeypatch.delenv("OMEGA_SKIP_RESIDENT_LLM", raising=False)
    from runtime import brain_cloud

    monkeypatch.setattr(brain_cloud, "enabled", lambda: False)
    spawned: dict = {}
    probes = iter([False, True])
    supervisor.ensure_runtime_process(
        client=FakeClient(),
        popen=lambda cmd, **kwargs: spawned.update(kwargs) or FakeProc(pid=7102),
        health_probe=lambda url: next(probes),
        sweep=lambda root: None,
        timeout_s=5.0,
        poll_interval_s=0,
    )
    assert "OMEGA_SKIP_RESIDENT_LLM" not in spawned["env"]


def test_missing_exe_fails_honestly_without_spawn(monkeypatch, tmp_path):
    monkeypatch.setattr(supervisor, "runtime_root", lambda: tmp_path / "nowhere")
    result = supervisor.ensure_runtime_process(
        client=FakeClient(),
        popen=lambda *a, **k: pytest.fail("spawned with no exe"),
        health_probe=lambda url: False,
        sweep=lambda root: pytest.fail("swept with no exe"),
    )
    assert result["ok"] is False
    assert result["reason"] == "runtime_exe_missing"
    assert supervisor.runtime_owned() is False


def test_health_timeout_kills_and_disowns(monkeypatch, tmp_path):
    _runtime_bundle(tmp_path, monkeypatch)
    killed: list[int] = []
    monkeypatch.setattr(supervisor, "_default_tree_kill", killed.append)
    result = supervisor.ensure_runtime_process(
        client=FakeClient(),
        popen=lambda *a, **k: FakeProc(pid=7002),
        health_probe=lambda url: False,
        sweep=lambda root: None,
        timeout_s=0,
        poll_interval_s=0,
    )
    assert result["ok"] is False
    assert result["reason"] == "runtime_health_timeout"
    assert killed == [7002]
    assert supervisor.runtime_owned() is False


def test_early_exit_reports_exit_code(monkeypatch, tmp_path):
    _runtime_bundle(tmp_path, monkeypatch)
    proc = FakeProc(pid=7003)
    proc.returncode = 3

    probes = iter([False])  # initial down-check only; loop sees the dead proc first
    result = supervisor.ensure_runtime_process(
        client=FakeClient(),
        popen=lambda *a, **k: proc,
        health_probe=lambda url: next(probes, False),
        sweep=lambda root: None,
        timeout_s=5.0,
        poll_interval_s=0,
    )
    assert result["ok"] is False
    assert result["reason"] == "runtime_exited_early"
    assert result["exit"] == 3
    assert supervisor.runtime_owned() is False


def test_shutdown_reports_no_owned_process_without_external_sweep(monkeypatch, tmp_path):
    # A process owned by a crashed boot is not recoverable through a second
    # runtime registry. The current local-services process is process-owned.
    root = _runtime_bundle(tmp_path, monkeypatch)
    swept: list[Path] = []
    result = supervisor.shutdown_runtime_process(stop_all=swept.append)
    assert result == {"ok": True, "owned": False, "swept": False}
    assert swept == [root]


def test_shutdown_graceful_when_process_exits(monkeypatch, tmp_path):
    root = _runtime_bundle(tmp_path, monkeypatch)
    proc = FakeProc(pid=7004)
    probes = iter([False, True])
    supervisor.ensure_runtime_process(
        client=FakeClient(),
        popen=lambda *a, **k: proc,
        health_probe=lambda url: next(probes),
        sweep=lambda root: None,
        timeout_s=5.0,
        poll_interval_s=0,
    )
    posted: list[str] = []
    swept: list[Path] = []

    def post(url: str) -> None:
        posted.append(url)
        proc.returncode = 0  # gateway exits on the graceful POST

    result = supervisor.shutdown_runtime_process(
        post=post,
        process_tree_kill=lambda pid: pytest.fail("tree-killed a graceful exit"),
        stop_all=swept.append,
        exit_wait_s=1.0,
    )
    assert posted == ["http://127.0.0.1:11434/admin/orchestration/shutdown"]
    assert result["ok"] is True and result["graceful"] is True and result["pid"] == 7004
    # The optional cleanup hook still runs, but no external sweep is reported.
    assert swept == [root]
    assert result["swept"] is False
    assert supervisor.runtime_owned() is False


def test_shutdown_falls_back_to_tree_kill(monkeypatch, tmp_path):
    root = _runtime_bundle(tmp_path, monkeypatch)
    proc = FakeProc(pid=7005)
    probes = iter([False, True])
    supervisor.ensure_runtime_process(
        client=FakeClient(),
        popen=lambda *a, **k: proc,
        health_probe=lambda url: next(probes),
        sweep=lambda root: None,
        timeout_s=5.0,
        poll_interval_s=0,
    )
    killed: list[int] = []
    swept: list[Path] = []
    result = supervisor.shutdown_runtime_process(
        post=lambda url: None,  # POST lands but the process never exits
        process_tree_kill=killed.append,
        stop_all=swept.append,
        exit_wait_s=0,
    )
    assert killed == [7005]
    assert result["graceful"] is False
    assert swept == [root]
    assert result["swept"] is False
    assert supervisor.runtime_owned() is False


def test_sweep_runtime_processes_invokes_supplied_cleanup_hook(monkeypatch, tmp_path):
    root = tmp_path / "omega-runtime"
    monkeypatch.setattr(supervisor, "runtime_root", lambda: root)
    called: list[Path] = []
    res = supervisor.sweep_runtime_processes(stop_all=called.append)
    assert res == {"ok": True, "swept": False, "reason": "owned_by_omega_boot"}
    assert called == [root]


def test_sweep_runtime_processes_default_is_a_noop(monkeypatch, tmp_path):
    monkeypatch.setattr(supervisor, "runtime_root", lambda: tmp_path / "nope")
    res = supervisor.sweep_runtime_processes()
    assert res == {"ok": True, "swept": False, "reason": "owned_by_omega_boot"}


def test_windows_tree_kill_is_windowless_and_streamless(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(supervisor.sys, "platform", "win32")
    monkeypatch.setattr(supervisor.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(supervisor.subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs)))

    supervisor._default_tree_kill(42)

    assert calls == [
        (
            (["taskkill.exe", "/PID", "42", "/T", "/F"],),
            {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "check": False,
                "creationflags": 0x08000000,
            },
        )
    ]


# ---------------------------------------------------------------- lifecycle wiring


def test_runtime_process_is_a_boot_component():
    from core.boot_lifecycle import BOOT_COMPONENTS

    assert "runtime_process" in BOOT_COMPONENTS
    # teardown order: reverse of components — runtime dies AFTER substrate
    # persist (in-brain embeds make that safe) and before the lock releases.
    assert BOOT_COMPONENTS.index("runtime_process") < BOOT_COMPONENTS.index("substrate")


# ------------------------------------------------------------ gateway log sink
#
# The 2026-08-11 death was unexplainable because stdout/stderr were DEVNULL:
# no traceback, no exit banner, and the kernel logged no OOM kill either.


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell stub")
def test_gateway_log_sink_receives_child_output(monkeypatch, tmp_path):
    """The child's stderr lands in a file we can actually read afterwards."""
    root = tmp_path / "omega-runtime"
    exe = supervisor._gateway_exe(root)
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\necho 'gateway said goodbye' >&2\nexit 7\n", encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setattr(supervisor, "runtime_root", lambda: root)

    # Real subprocess.Popen — a fake would prove the kwargs, not the plumbing.
    result = supervisor.ensure_runtime_process(
        client=FakeClient(),
        health_probe=lambda url: False,
        sweep=lambda r: None,
        timeout_s=10.0,
        poll_interval_s=0.05,
    )
    assert result["reason"] == "runtime_exited_early"
    assert result["exit"] == 7
    assert "gateway said goodbye" in supervisor.gateway_log_path().read_text(encoding="utf-8")


def test_gateway_log_rotates_at_cap():
    path = supervisor.gateway_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x" * (supervisor._GATEWAY_LOG_MAX_BYTES + 1), encoding="utf-8")

    supervisor._close_sink(supervisor._open_gateway_log_sink())

    assert path.with_suffix(".log.1").is_file()
    assert path.stat().st_size == 0  # fresh generation, old one preserved beside it


def test_log_sink_failure_degrades_to_devnull_instead_of_blocking_the_spawn(monkeypatch):
    """Losing the log is bad; refusing to boot the brain's runtime is worse."""
    monkeypatch.setattr(
        supervisor, "gateway_log_path", lambda: (_ for _ in ()).throw(OSError("no disk"))
    )
    assert supervisor._open_gateway_log_sink() is supervisor.subprocess.DEVNULL


# ------------------------------------------------------------- gateway watchdog


class _DeadableProc(FakeProc):
    """A gateway handle that counts poll() — the call nobody made on 2026-08-11."""

    def __init__(self, pid: int = 8001):
        super().__init__(pid)
        self.polls = 0

    def poll(self):
        self.polls += 1
        return self.returncode


def _own_a_gateway(monkeypatch, tmp_path, proc) -> None:
    _runtime_bundle(tmp_path, monkeypatch)
    probes = iter([False, True])
    supervisor.ensure_runtime_process(
        client=FakeClient(),
        popen=lambda *a, **k: proc,
        health_probe=lambda url: next(probes),
        sweep=lambda root: None,
        timeout_s=5.0,
        poll_interval_s=0,
    )
    assert supervisor.runtime_owned() is True


def test_watchdog_reaps_the_zombie_and_respawns_exactly_once(monkeypatch, tmp_path):
    """The whole defect in one test: dead handle -> reaped -> revived -> left alone."""
    proc = _DeadableProc(pid=8001)
    _own_a_gateway(monkeypatch, tmp_path, proc)

    proc.returncode = -9  # the gateway dies; nothing has poll()ed it yet
    serving = {"up": False}
    spawns: list[dict] = []

    def fake_ensure(**kwargs):
        spawns.append(kwargs)
        serving["up"] = True
        return {"ok": True, "spawned": True, "pid": 8002}

    first = supervisor.gateway_watchdog_tick(
        client=FakeClient(),
        health_probe=lambda url: serving["up"],
        ensure=fake_ensure,
    )
    assert first["action"] == "revived"
    assert first["exit_code"] == -9  # the death is now on the record
    assert proc.polls >= 1  # the corpse was actually waitpid()'d, not left <defunct>
    assert supervisor.runtime_owned() is False  # stale pid disowned, so teardown
    assert len(spawns) == 1  # cannot killpg a recycled pid

    # Second pass over a now-healthy gateway must not spawn a second one.
    second = supervisor.gateway_watchdog_tick(
        client=FakeClient(),
        health_probe=lambda url: serving["up"],
        ensure=fake_ensure,
    )
    assert second["action"] == "healthy"
    assert len(spawns) == 1


def test_watchdog_leaves_a_healthy_gateway_alone(monkeypatch, tmp_path):
    proc = _DeadableProc(pid=8010)
    _own_a_gateway(monkeypatch, tmp_path, proc)
    result = supervisor.gateway_watchdog_tick(
        client=FakeClient(),
        health_probe=lambda url: True,
        ensure=lambda **k: pytest.fail("respawned a healthy gateway"),
    )
    assert result["action"] == "healthy"
    assert supervisor.runtime_owned() is True  # ownership survives a healthy tick


def test_watchdog_does_not_respawn_a_stopping_appliance(monkeypatch):
    """A deliberate shutdown is never fought — the lifecycle flag closes the gate."""
    import core.boot_lifecycle as lifecycle

    monkeypatch.setattr(lifecycle, "_shutdown_done", True)
    result = supervisor.gateway_watchdog_tick(
        client=FakeClient(),
        health_probe=lambda url: pytest.fail("probed a stopping appliance"),
        ensure=lambda **k: pytest.fail("respawned a stopping appliance"),
    )
    assert result == {"action": "skipped", "reason": "stopping"}


def test_teardown_closes_the_watchdog_gate(monkeypatch, tmp_path):
    """shutdown_runtime_process latches the stop itself, so EVERY teardown path
    (boot_lifecycle, scripts/shutdown.py, the HUD button) is covered."""
    _runtime_bundle(tmp_path, monkeypatch)
    supervisor.shutdown_runtime_process(stop_all=lambda root: None)
    result = supervisor.gateway_watchdog_tick(
        client=FakeClient(),
        health_probe=lambda url: pytest.fail("probed after teardown"),
        ensure=lambda **k: pytest.fail("respawned after teardown"),
    )
    assert result == {"action": "skipped", "reason": "stopping"}


def test_watchdog_tears_down_a_gateway_it_revived_into_a_shutdown(monkeypatch, tmp_path):
    """The respawn blocks up to 120s on /health — ample time for the operator to
    hit stop. A gateway revived into a shutdown must not be left stranded."""
    import core.boot_lifecycle as lifecycle

    proc = _DeadableProc(pid=8030)
    _own_a_gateway(monkeypatch, tmp_path, proc)
    proc.returncode = -9  # a corpse, so the tick reaches the respawn on tick one
    torn: list[bool] = []
    monkeypatch.setattr(
        supervisor, "shutdown_runtime_process", lambda **k: torn.append(True) or {"ok": True}
    )

    def slow_ensure(**kwargs):
        # Shutdown begins while the spawn is still in flight.
        monkeypatch.setattr(lifecycle, "_shutdown_done", True)
        return {"ok": True, "spawned": True, "pid": 8003}

    result = supervisor.gateway_watchdog_tick(
        client=FakeClient(),
        health_probe=lambda url: False,
        ensure=slow_ensure,
    )
    assert result["action"] == "revived_then_stopped"
    assert torn == [True]


def test_watchdog_reports_a_failed_respawn_honestly(monkeypatch, tmp_path):
    _runtime_bundle(tmp_path, monkeypatch)
    ensure = lambda **k: {"ok": False, "reason": "runtime_exe_missing"}  # noqa: E731
    for _ in range(supervisor._WATCHDOG_UNREACHABLE_STRIKES - 1):
        supervisor.gateway_watchdog_tick(
            client=FakeClient(), health_probe=lambda url: False, ensure=ensure
        )
    result = supervisor.gateway_watchdog_tick(
        client=FakeClient(), health_probe=lambda url: False, ensure=ensure
    )
    assert result["action"] == "respawn_failed"
    assert result["spawn"]["reason"] == "runtime_exe_missing"


# --- the two-tier evidence rule -------------------------------------------------
#
# Measured on this watchdog's first live run: one missed probe respawned a
# HEALTHY gateway, and since respawning sweeps :11434 first, the sweep POSTed
# /admin/orchestration/shutdown to the live process and got 200 OK. The
# watchdog killed the thing it exists to protect. Certainty now sets the bar.


def test_a_busy_gateway_is_not_executed_for_missing_one_probe(monkeypatch, tmp_path):
    """A LIVE process that misses a probe is busy, not dead. Do not sweep it."""
    proc = _DeadableProc(pid=8020)
    _own_a_gateway(monkeypatch, tmp_path, proc)  # proc.returncode stays None = alive

    for strike in range(1, supervisor._WATCHDOG_UNREACHABLE_STRIKES):
        result = supervisor.gateway_watchdog_tick(
            client=FakeClient(),
            health_probe=lambda url: False,
            ensure=lambda **k: pytest.fail("swept a live gateway over one slow probe"),
        )
        assert result == {"action": "unreachable", "strikes": strike}
    assert supervisor.runtime_owned() is True  # never disowned, never killed


def test_a_wedged_gateway_is_recycled_once_the_silence_is_sustained(monkeypatch, tmp_path):
    """Sustained silence from a live process IS actionable — a pid that never
    answers is a dead one to every caller that matters. Just not on tick one."""
    proc = _DeadableProc(pid=8021)
    _own_a_gateway(monkeypatch, tmp_path, proc)
    spawns: list[dict] = []

    def fake_ensure(**kwargs):
        spawns.append(kwargs)
        return {"ok": True, "spawned": True, "pid": 8022}

    results = [
        supervisor.gateway_watchdog_tick(
            client=FakeClient(), health_probe=lambda url: False, ensure=fake_ensure
        )
        for _ in range(supervisor._WATCHDOG_UNREACHABLE_STRIKES)
    ]
    assert [r["action"] for r in results[:-1]] == ["unreachable"] * (
        supervisor._WATCHDOG_UNREACHABLE_STRIKES - 1
    )
    assert results[-1]["action"] == "revived"
    assert len(spawns) == 1


def test_a_recovered_probe_clears_the_strikes(monkeypatch, tmp_path):
    """A slow patch must not accumulate toward an execution across minutes."""
    proc = _DeadableProc(pid=8023)
    _own_a_gateway(monkeypatch, tmp_path, proc)
    serving = {"up": False}

    def ensure(**k):
        return pytest.fail("respawned after the gateway came back")

    for _ in range(supervisor._WATCHDOG_UNREACHABLE_STRIKES - 1):
        supervisor.gateway_watchdog_tick(
            client=FakeClient(), health_probe=lambda url: serving["up"], ensure=ensure
        )
    serving["up"] = True
    assert (
        supervisor.gateway_watchdog_tick(
            client=FakeClient(), health_probe=lambda url: serving["up"], ensure=ensure
        )["action"]
        == "healthy"
    )
    serving["up"] = False
    # Counting restarts from one, so the earlier slow patch is not held against it.
    assert supervisor.gateway_watchdog_tick(
        client=FakeClient(), health_probe=lambda url: serving["up"], ensure=ensure
    ) == {"action": "unreachable", "strikes": 1}


def test_a_corpse_is_revived_on_the_first_tick_no_strikes_needed(monkeypatch, tmp_path):
    """The zombie case — the one this watchdog exists for — pays no strike tax.
    A reaped exit code is proof, not a rumour."""
    proc = _DeadableProc(pid=8024)
    _own_a_gateway(monkeypatch, tmp_path, proc)
    proc.returncode = -9
    spawns: list[dict] = []
    result = supervisor.gateway_watchdog_tick(
        client=FakeClient(),
        health_probe=lambda url: False,
        ensure=lambda **k: spawns.append(k) or {"ok": True, "pid": 8025},
    )
    assert result["action"] == "revived"
    assert len(spawns) == 1


def test_watchdog_start_is_idempotent(monkeypatch):
    monkeypatch.delenv("OMEGA_GATEWAY_WATCHDOG", raising=False)
    monkeypatch.setattr(supervisor, "gateway_watchdog_tick", lambda **k: {"action": "healthy"})
    try:
        first = supervisor.start_gateway_watchdog(interval_s=30.0)
        second = supervisor.start_gateway_watchdog(interval_s=30.0)
        assert first["started"] is True
        assert second["started"] is False and second["reason"] == "already_running"
        assert supervisor.watchdog_running() is True
    finally:
        supervisor.stop_gateway_watchdog()
    # Event.wait doubles as the sleep, so stopping does not wait out an interval.
    assert supervisor.watchdog_running() is False


def test_watchdog_kill_switch(monkeypatch):
    monkeypatch.setenv("OMEGA_GATEWAY_WATCHDOG", "0")
    result = supervisor.start_gateway_watchdog()
    assert result["started"] is False and result["reason"] == "disabled"
    assert supervisor.watchdog_running() is False


def test_watchdog_survives_a_raising_tick(monkeypatch):
    """The keeper outlives every tick it runs — a thrown tick must not kill it."""
    import threading

    monkeypatch.delenv("OMEGA_GATEWAY_WATCHDOG", raising=False)
    ticked = threading.Event()

    def boom(**kwargs):
        ticked.set()
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(supervisor, "gateway_watchdog_tick", boom)
    try:
        supervisor.start_gateway_watchdog(interval_s=0.01)
        assert ticked.wait(timeout=5.0)
        assert supervisor.watchdog_running() is True
    finally:
        supervisor.stop_gateway_watchdog()


def test_shutdown_in_progress_is_public_and_latches():
    import core.boot_lifecycle as lifecycle

    lifecycle.reset_lifecycle_state()
    assert lifecycle.shutdown_in_progress() is False
    lifecycle.shutdown_system(reason="test")
    assert lifecycle.shutdown_in_progress() is True
    lifecycle.reset_lifecycle_state()


def test_shutdown_system_tears_down_owned_runtime(monkeypatch, tmp_path):
    _runtime_bundle(tmp_path, monkeypatch)
    proc = FakeProc(pid=7006)
    probes = iter([False, True])
    supervisor.ensure_runtime_process(
        client=FakeClient(),
        popen=lambda *a, **k: proc,
        health_probe=lambda url: next(probes),
        sweep=lambda root: None,
        timeout_s=5.0,
        poll_interval_s=0,
    )

    import core.boot_lifecycle as lifecycle

    torn: list[dict] = []

    def fake_shutdown(**kwargs):
        torn.append({"called": True})
        supervisor._reset_for_tests()
        return {"ok": True, "owned": True, "graceful": True, "pid": 7006}

    monkeypatch.setattr(supervisor, "shutdown_runtime_process", fake_shutdown)
    monkeypatch.setattr(lifecycle, "_started", ["runtime_process"])
    monkeypatch.setattr(lifecycle, "_shutdown_done", False)

    summary = lifecycle.shutdown_system(reason="test")
    assert torn == [{"called": True}]
    assert summary["runtime_process"]["owned"] is True
    step = next(s for s in summary["steps"] if s["component"] == "runtime_process")
    assert step["ok"] is True


# ------------------------------------------- the respawn cannot outlive a stop
#
# "Never fights a deliberate shutdown" was gated on two checks: the tick's
# entry gate, and a re-check AFTER ensure_runtime_process returns. Between them
# sits the spawn WAIT, and that wait grew from 120s to 300s in the same round
# the watchdog landed (the cold-boot measurement). Teardown's
# stop_gateway_watchdog joins this thread for 2s and then proceeds; the thread
# is a daemon, so the interpreter can exit with the wait still running — and a
# gateway that binds :11434 after teardown's sweep has already passed is an
# orphan the brain will never reap.


def test_the_spawn_wait_aborts_when_a_shutdown_begins(monkeypatch, tmp_path):
    """The gap between the two checks: a spawn already in flight.

    The child is spawned and we are polling /health when the operator stops the
    appliance. The wait must not run to its deadline — it must kill the child it
    spawned, while it still has the pid.
    """
    import core.boot_lifecycle as lifecycle

    _runtime_bundle(tmp_path, monkeypatch)
    proc = FakeProc(pid=8090)
    killed: list[int] = []
    monkeypatch.setattr(supervisor, "_default_tree_kill", lambda pid: killed.append(pid))

    probes = {"n": 0}

    def _never_healthy(_url: str) -> bool:
        probes["n"] += 1
        if probes["n"] == 2:
            # The operator hits stop while we are waiting.
            monkeypatch.setattr(lifecycle, "_shutdown_done", True)
        return False

    result = supervisor.ensure_runtime_process(
        client=FakeClient(),
        popen=lambda *a, **k: proc,
        health_probe=_never_healthy,
        sweep=lambda root: None,
        timeout_s=300.0,  # the real deadline — this must NOT be what ends the wait
        poll_interval_s=0,
        abort_check=supervisor._watchdog_should_stop,
    )

    assert result == {"ok": False, "spawned": True, "reason": "shutdown_in_progress"}
    assert killed == [8090], "the child we spawned must not be left holding :11434"
    assert supervisor.runtime_owned() is False


def test_boot_s_own_spawn_is_not_given_an_abort_check(monkeypatch, tmp_path):
    """Boot cannot be overtaken by a teardown of a boot that has not finished,
    so it passes no abort_check and behaves exactly as before. Pinned because a
    default-on abort would make a stale lifecycle flag able to refuse a boot."""
    _runtime_bundle(tmp_path, monkeypatch)
    import core.boot_lifecycle as lifecycle

    monkeypatch.setattr(lifecycle, "_shutdown_done", True)  # a stale flag
    proc = FakeProc(pid=8091)
    probes = iter([False, True])

    result = supervisor.ensure_runtime_process(
        client=FakeClient(),
        popen=lambda *a, **k: proc,
        health_probe=lambda url: next(probes),
        sweep=lambda root: None,
        timeout_s=5.0,
        poll_interval_s=0,
    )

    assert result["ok"] is True and result["spawned"] is True


def test_the_watchdog_hands_its_stop_signal_down_into_the_spawn(monkeypatch, tmp_path):
    """The wiring itself: the tick must pass the SAME question down, or the two
    sides could disagree about whether a shutdown is under way."""
    proc = _DeadableProc(pid=8092)
    _own_a_gateway(monkeypatch, tmp_path, proc)
    proc.returncode = -9  # a corpse: revives on tick one, no strikes needed
    seen: dict = {}

    supervisor.gateway_watchdog_tick(
        client=FakeClient(),
        health_probe=lambda url: False,
        ensure=lambda **kwargs: seen.update(kwargs) or {"ok": True, "pid": 8093},
    )

    assert seen.get("abort_check") is supervisor._watchdog_should_stop


def test_the_stop_question_is_one_question(monkeypatch):
    """Either signal alone must answer True — the teardown latch and the
    lifecycle flag close different halves of the same window."""
    import core.boot_lifecycle as lifecycle

    assert supervisor._watchdog_should_stop() is False
    monkeypatch.setattr(lifecycle, "_shutdown_done", True)
    assert supervisor._watchdog_should_stop() is True
    monkeypatch.setattr(lifecycle, "_shutdown_done", False)
    assert supervisor._watchdog_should_stop() is False
    supervisor._watchdog_stop.set()
    assert supervisor._watchdog_should_stop() is True


@pytest.mark.skipif(os.name == "nt", reason="Windows binds the gateway with a kill-on-close job")
def test_posix_gateway_is_told_which_brain_owns_it(monkeypatch, tmp_path):
    """POSIX has no job object; the gateway learns its owner's pid so it can take
    its process group down when that brain is gone (local_services.exit_with_parent)."""
    from core.paths import portable_python_path

    root = tmp_path / "omega-runtime"
    python = portable_python_path(root)
    python.parent.mkdir(parents=True)
    python.write_bytes(b"stub")
    monkeypatch.setattr(supervisor, "runtime_root", lambda: root)
    probes = iter([False, True])
    spawned: dict = {}

    supervisor.ensure_runtime_process(
        client=FakeClient(),
        popen=lambda cmd, **kwargs: spawned.update(cmd=list(cmd), kwargs=kwargs)
        or FakeProc(pid=7004),
        health_probe=lambda url: next(probes),
        sweep=lambda _root: None,
        timeout_s=5.0,
        poll_interval_s=0,
    )

    assert spawned["kwargs"]["env"]["OMEGA_RUNTIME_PARENT_PID"] == str(os.getpid())
