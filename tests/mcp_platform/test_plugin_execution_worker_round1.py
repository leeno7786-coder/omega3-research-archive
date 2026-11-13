"""Round-one adversarial contracts for the private MCP execution worker."""

from __future__ import annotations

import asyncio
import ctypes
import multiprocessing
import os
import signal
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from mcp_platform import plugin_execution_worker as worker_module
from mcp_platform.plugin_execution_worker import (
    PluginExecutionAuthority,
    PluginExecutionWorker,
    PluginExecutionWorkerError,
    _LocalMcpWorkerExecution,
    _RunningWorker,
    _SpawnedWorker,
)
from tests.mcp_platform.plugin_execution_worker_fixture import handle
from tests.mcp_platform.plugin_execution_worker_round1_fixture import (
    blocked_handler,
    detach_and_kill_worker_handler,
    double_fork_reparented_handler,
    fork_escape_kill_worker_handler,
    kill_linux_guardian_handler,
    run_desktop_parent,
)


def _authority() -> PluginExecutionAuthority:
    return PluginExecutionAuthority(
        plugin_id="northwind.docs",
        app_id="northwind.docs.mcp",
        account_key_sha256="a" * 64,
        package_revision="b" * 64,
        connection_revision="c" * 64,
        authority_revision="d" * 64,
        capability_revision="e" * 64,
        capability_kind="tool",
        capability_id="search",
    )


def _local_execution(tmp_path: Path) -> _LocalMcpWorkerExecution:
    state_root = tmp_path / "mcp-state"
    state_root.mkdir(parents=True, exist_ok=True)
    return _LocalMcpWorkerExecution(
        plugin_registry_root=str(tmp_path / "plugin-registry"),
        plugin_registry_revision="1" * 64,
        plugin_package_root=str(tmp_path / "plugin-package"),
        mcp_state_root=str(state_root),
        app_registry_revision="2" * 64,
        package_registry_revision="3" * 64,
        mcp_package_root=str(tmp_path / "mcp-package"),
        mcp_package_manifest_sha256="4" * 64,
        executable_path=str(tmp_path / "mcp-package" / "provider"),
        executable_sha256="5" * 64,
        executable_identity=(1, 1),
    )


class _FakeProcess:
    pid = 8123

    def __init__(self) -> None:
        self.alive = True

    def is_alive(self) -> bool:
        return self.alive


class _FakeConnection:
    def close(self) -> None:
        return None


class _CloseRaisesConnection:
    def close(self) -> None:
        raise OSError("endpoint close failed")


def test_stdio_attached_worker_owner_retains_exact_pid_and_reaps_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_platform.transports import stdio as stdio_module

    waited: list[tuple[int, int]] = []
    statuses = iter(((0, 0), (7311, 9)))

    monkeypatch.setattr(stdio_module.os, "name", "posix")
    monkeypatch.setattr(stdio_module.sys, "platform", "linux")
    monkeypatch.setattr(stdio_module.os, "fork", lambda: 7311, raising=False)
    monkeypatch.setattr(stdio_module.os, "WNOHANG", 1, raising=False)

    def waitpid(pid: int, options: int) -> tuple[int, int]:
        waited.append((pid, options))
        return next(statuses)

    monkeypatch.setattr(stdio_module.os, "waitpid", waitpid, raising=False)

    owner = stdio_module._fork_attached_worker()
    assert owner is not None
    assert owner.pid == 7311
    assert owner.poll_wait_status() is None
    assert owner.poll_wait_status() == 9
    assert owner.poll_wait_status() == 9
    assert waited == [(7311, 1), (7311, 1)]


def test_stdio_attached_worker_owner_distinguishes_child_and_wrong_reap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_platform.transports import stdio as stdio_module

    monkeypatch.setattr(stdio_module.os, "name", "posix")
    monkeypatch.setattr(stdio_module.sys, "platform", "linux")
    monkeypatch.setattr(stdio_module.os, "WNOHANG", 1, raising=False)
    monkeypatch.setattr(stdio_module.os, "fork", lambda: 0, raising=False)
    assert stdio_module._fork_attached_worker() is None

    monkeypatch.setattr(stdio_module.os, "fork", lambda: 7311)
    monkeypatch.setattr(
        stdio_module.os,
        "waitpid",
        lambda _pid, _options: (7312, 0),
        raising=False,
    )
    owner = stdio_module._fork_attached_worker()
    assert owner is not None
    with pytest.raises(OSError, match="plugin_worker_process_model_unavailable"):
        owner.poll_wait_status()


def _pid_is_alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    process = kernel32.OpenProcess(0x1000, False, pid)
    if not process:
        return False
    try:
        exit_code = wintypes.DWORD()
        return bool(
            kernel32.GetExitCodeProcess(process, ctypes.byref(exit_code))
            and exit_code.value == 259
        )
    finally:
        kernel32.CloseHandle(process)


def _terminate_exact_process(pid: int) -> None:
    if not _pid_is_alive(pid):
        return
    if os.name != "nt":
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    process = kernel32.OpenProcess(0x0001 | 0x00100000, False, pid)
    if process:
        try:
            kernel32.TerminateProcess(process, 91)
        finally:
            kernel32.CloseHandle(process)


def _wait_for_path(path: Path, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert path.exists()


def _wait_for_process_exit(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while _pid_is_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.01)
    return not _pid_is_alive(pid)


def _running_worker() -> tuple[PluginExecutionWorker, _RunningWorker, _FakeProcess]:
    worker = PluginExecutionWorker(
        child_handler=handle,
        shutdown_timeout_seconds=1.0,
    )
    process = _FakeProcess()
    connection = _FakeConnection()
    running = _RunningWorker(
        authority=_authority(),
        generation=1,
        nonce="f" * 64,
        spawned=_SpawnedWorker(process, None, None),  # type: ignore[arg-type]
        bootstrap_send=connection,  # type: ignore[arg-type]
        command_send=connection,  # type: ignore[arg-type]
        result_recv=connection,  # type: ignore[arg-type]
        local_execution=None,
    )
    worker._generations[running.authority.execution_key] = 1
    worker._active[running.authority.execution_key] = running
    return worker, running, process


def test_close_reap_failure_retains_and_retries_the_exact_live_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker, running, process = _running_worker()
    reap_attempts: list[object] = []

    def reap(candidate: object, **_kwargs: object) -> tuple[bool, bool]:
        reap_attempts.append(candidate)
        if len(reap_attempts) == 1:
            return False, False
        process.alive = False
        return True, False

    monkeypatch.setattr(worker, "_kill_and_reap", reap)
    with pytest.raises(
        PluginExecutionWorkerError,
        match="^plugin_worker_reap_failed$",
    ):
        worker.close()

    assert reap_attempts == [running]
    assert worker.active_process_ids == (8123,)
    assert [outcome.status for outcome in worker.terminal_outcomes] == ["reap_failed"]

    worker.close()

    assert reap_attempts == [running, running]
    assert worker.active_process_ids == ()
    assert [outcome.generation for outcome in worker.terminal_outcomes] == [1, 1]
    assert [outcome.status for outcome in worker.terminal_outcomes] == [
        "reap_failed",
        "closed",
    ]


def test_close_marks_initial_generation_pending_before_reap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker, running, process = _running_worker()
    reap_started = threading.Event()
    release_reap = threading.Event()
    reap_attempts: list[_RunningWorker] = []
    close_errors: list[BaseException] = []

    def reap(candidate: _RunningWorker, **_kwargs: object) -> tuple[bool, bool]:
        reap_attempts.append(candidate)
        if len(reap_attempts) == 1:
            reap_started.set()
            assert release_reap.wait(10)
            return False, False
        process.alive = False
        return True, False

    monkeypatch.setattr(worker, "_kill_and_reap", reap)

    def close_worker() -> None:
        try:
            worker.close()
        except BaseException as exc:
            close_errors.append(exc)

    closer = threading.Thread(target=close_worker)
    closer.start()
    try:
        assert reap_started.wait(10)
        worker._record_terminal(running, "completed")
        release_reap.set()
        closer.join(10)
        assert not closer.is_alive()

        assert len(close_errors) == 1
        assert isinstance(close_errors[0], PluginExecutionWorkerError)
        assert str(close_errors[0]) == "plugin_worker_reap_failed"
        assert worker.active_process_ids == (8123,)
        assert [outcome.status for outcome in worker.terminal_outcomes] == [
            "reap_failed"
        ]

        worker.close()
        assert reap_attempts == [running, running]
        assert worker.active_process_ids == ()
        assert [outcome.status for outcome in worker.terminal_outcomes] == [
            "reap_failed",
            "closed",
        ]
    finally:
        release_reap.set()
        closer.join(10)
        if worker.active_process_ids:
            worker.close()


def test_failed_operation_reap_keeps_the_generation_fenced_until_close_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker, running, process = _running_worker()
    reap_attempts: list[object] = []

    def reap(candidate: object, **_kwargs: object) -> tuple[bool, bool]:
        reap_attempts.append(candidate)
        if len(reap_attempts) == 1:
            return False, False
        process.alive = False
        return True, False

    monkeypatch.setattr(worker, "_kill_and_reap", reap)
    with pytest.raises(
        PluginExecutionWorkerError,
        match="^plugin_worker_reap_failed$",
    ):
        worker._fail_running(
            running,
            status="timeout",
            reason="plugin_operation_timeout",
        )

    assert worker.active_process_ids == (8123,)
    assert worker.terminal_outcomes[-1].status == "reap_failed"

    worker.close()

    assert reap_attempts == [running, running]
    assert worker.active_process_ids == ()
    assert [outcome.status for outcome in worker.terminal_outcomes] == [
        "reap_failed",
        "closed",
    ]


def test_linux_startup_deadline_without_worker_identity_retains_reap_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreported worker identity must not become a clean timeout receipt."""
    worker, running, process = _running_worker()
    running.spawned.linux_guarded = True
    assert running.spawned.worker_identity is None
    joins = []
    monkeypatch.setattr(process, "join", joins.append, raising=False)
    monkeypatch.setattr(process, "exitcode", None, raising=False)

    def unexpected_kill(*_args):
        raise AssertionError("first cleanup must not signal an unknown worker")

    def unexpected_spawn(*_args, **_kwargs):
        raise AssertionError("reap-pending worker must deny before spawn")

    monkeypatch.setattr(worker_module.os, "kill", unexpected_kill)
    monkeypatch.setattr(worker, "_spawn", unexpected_spawn)
    try:
        with pytest.raises(PluginExecutionWorkerError, match="^plugin_worker_reap_failed$"):
            worker._fail_running(
                running, status="timeout", reason="plugin_operation_timeout",
            )
        assert joins == [1.0 + worker_module._POSIX_PROCESS_CLEANUP_SECONDS]
        assert running.spawned.guardian_reap_failure_seen is True
        assert running.cleanup_pending is True
        assert worker.active_process_ids == (process.pid,)
        assert worker.terminal_outcomes[-1].status == "reap_failed"
        with pytest.raises(PluginExecutionWorkerError, match="^plugin_worker_reap_pending$"):
            worker.run_sync(
                authority=_authority(), body={"action": "return", "value": "blocked"},
                credential={}, timeout_seconds=1,
            )
    finally:
        process.alive = False
        process.exitcode = worker_module._LINUX_GUARDIAN_CLEAN_EXIT
        worker.close()
    assert worker.active_process_ids == ()


def test_cleanup_exception_records_reap_failure_and_retains_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker, running, process = _running_worker()
    retained: list[str] = []
    worker._reap_pending_callback = lambda: retained.append("retained")

    def failed_cleanup(*_args: object, **_kwargs: object) -> tuple[bool, bool]:
        raise OSError("cleanup backend unavailable")

    monkeypatch.setattr(worker, "_kill_and_reap", failed_cleanup)
    with pytest.raises(
        PluginExecutionWorkerError,
        match="^plugin_worker_reap_failed$",
    ):
        worker._fail_running(
            running,
            status="timeout",
            reason="plugin_operation_timeout",
        )

    assert retained == ["retained"]
    assert worker.active_process_ids == (8123,)
    assert running.cleanup_pending is True
    assert running.reap_failure_recorded is True
    assert [outcome.status for outcome in worker.terminal_outcomes] == [
        "reap_failed"
    ]

    process.alive = False
    monkeypatch.setattr(
        worker,
        "_kill_and_reap",
        lambda *_args, **_kwargs: (True, False),
    )
    worker.close()


def test_operation_reap_pending_globally_fences_every_execution_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker, running, _process = _running_worker()
    worker._record_reap_failure(running)
    other_authority = PluginExecutionAuthority(
        plugin_id="northwind.calendar",
        app_id="northwind.calendar.mcp",
        account_key_sha256="f" * 64,
        package_revision="1" * 64,
        connection_revision="2" * 64,
        authority_revision="3" * 64,
        capability_revision="4" * 64,
        capability_kind="tool",
        capability_id="list_events",
    )
    spawn_calls: list[object] = []

    def unexpected_spawn(*args: object, **kwargs: object) -> object:
        spawn_calls.append((args, kwargs))
        raise AssertionError("reap-pending worker must deny before spawn")

    monkeypatch.setattr(worker, "_spawn", unexpected_spawn)

    with pytest.raises(
        PluginExecutionWorkerError,
        match="^plugin_worker_reap_pending$",
    ):
        worker.run_sync(
            authority=other_authority,
            body={"action": "return", "value": "must-not-run"},
            credential={},
            timeout_seconds=1.0,
        )

    assert spawn_calls == []
    assert worker.active_process_ids == (8123,)


def test_late_terminal_cannot_clear_a_recorded_global_reap_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker, running, _process = _running_worker()
    worker._record_reap_failure(running)

    # A management thread may already have crossed its final generation check
    # when cancellation records an unproven reap.  Its stale completion must
    # never remove the shared worker's global cleanup fence.
    worker._record_terminal(running, "completed")

    spawn_calls: list[object] = []

    def unexpected_spawn(*args: object, **kwargs: object) -> object:
        spawn_calls.append((args, kwargs))
        raise AssertionError("late terminal must not erase reap-pending fence")

    monkeypatch.setattr(worker, "_spawn", unexpected_spawn)
    with pytest.raises(
        PluginExecutionWorkerError,
        match="^plugin_worker_reap_pending$",
    ):
        worker.run_sync(
            authority=replace(
                _authority(),
                plugin_id="northwind.calendar",
                app_id="northwind.calendar.mcp",
            ),
            body={"action": "return", "value": "must-not-run"},
            credential={},
            timeout_seconds=1.0,
        )

    assert spawn_calls == []
    assert worker.active_process_ids == (8123,)
    assert [outcome.status for outcome in worker.terminal_outcomes] == [
        "reap_failed"
    ]


@pytest.mark.anyio
async def test_close_fails_if_async_worker_registers_after_initial_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = PluginExecutionWorker(
        child_handler=handle,
        shutdown_timeout_seconds=1.0,
    )
    process = _FakeProcess()
    spawn_started = threading.Event()
    release_spawn = threading.Event()
    close_errors: list[BaseException] = []

    def gated_spawn(*_args: object, **_kwargs: object) -> _SpawnedWorker:
        spawn_started.set()
        assert release_spawn.wait(10)
        return _SpawnedWorker(process, None, None)  # type: ignore[arg-type]

    monkeypatch.setattr(worker, "_spawn", gated_spawn)
    monkeypatch.setattr(
        worker,
        "_kill_and_reap",
        lambda *_args, **_kwargs: (False, False),
    )
    operation = asyncio.create_task(
        worker.run_async(
            authority=_authority(),
            body={"action": "return", "value": "must-not-run"},
            credential={},
            timeout_seconds=10.0,
        )
    )
    closer: threading.Thread | None = None
    try:
        assert await asyncio.to_thread(spawn_started.wait, 10)

        def close_worker() -> None:
            try:
                worker.close()
            except BaseException as exc:
                close_errors.append(exc)

        closer = threading.Thread(target=close_worker)
        closer.start()
        deadline = asyncio.get_running_loop().time() + 10.0
        while not worker._closed:
            if asyncio.get_running_loop().time() >= deadline:
                pytest.fail("close did not seal worker admission")
            await asyncio.sleep(0.01)
        release_spawn.set()
        await asyncio.to_thread(closer.join, 10)
        assert not closer.is_alive()

        with pytest.raises(
            PluginExecutionWorkerError,
            match="^plugin_worker_reap_failed$",
        ):
            await operation
        assert len(close_errors) == 1
        assert isinstance(close_errors[0], PluginExecutionWorkerError)
        assert str(close_errors[0]) == "plugin_worker_reap_failed"
        assert worker.active_process_ids == (8123,)
        assert [outcome.status for outcome in worker.terminal_outcomes] == [
            "reap_failed"
        ]
    finally:
        release_spawn.set()
        if closer is not None:
            await asyncio.to_thread(closer.join, 10)
        if not operation.done():
            operation.cancel()
            with pytest.raises(asyncio.CancelledError):
                await operation
        process.alive = False
        monkeypatch.setattr(
            worker,
            "_kill_and_reap",
            lambda *_args, **_kwargs: (True, False),
        )
        worker.close()


def test_close_never_observes_a_registered_unstarted_management_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = PluginExecutionWorker(
        child_handler=handle,
        shutdown_timeout_seconds=1.0,
    )
    start_entered = threading.Event()
    release_start = threading.Event()
    close_entered = threading.Event()
    close_done = threading.Event()
    close_errors: list[BaseException] = []
    operation_errors: list[BaseException] = []
    real_thread_start = threading.Thread.start

    def gated_start(candidate: threading.Thread) -> None:
        if candidate.name == "omega-plugin-worker-management":
            start_entered.set()
            assert release_start.wait(10)
        real_thread_start(candidate)

    def unavailable_operation(**_kwargs: object) -> object:
        raise PluginExecutionWorkerError("plugin_runtime_closed")

    monkeypatch.setattr(threading.Thread, "start", gated_start)
    monkeypatch.setattr(worker, "run_sync", unavailable_operation)

    def run_operation() -> None:
        try:
            asyncio.run(
                worker.run_async(
                    authority=_authority(),
                    body={"action": "return", "value": "must-not-run"},
                    credential={},
                    timeout_seconds=10.0,
                )
            )
        except BaseException as exc:
            operation_errors.append(exc)

    def close_worker() -> None:
        close_entered.set()
        try:
            worker.close()
        except BaseException as exc:
            close_errors.append(exc)
        finally:
            close_done.set()

    operation_thread = threading.Thread(target=run_operation)
    close_thread = threading.Thread(target=close_worker)
    real_thread_start(operation_thread)
    try:
        assert start_entered.wait(10)
        real_thread_start(close_thread)
        assert close_entered.wait(10)
        assert not close_done.wait(0.2)
        release_start.set()
        operation_thread.join(10)
        close_thread.join(10)

        assert not operation_thread.is_alive()
        assert not close_thread.is_alive()
        assert close_errors == []
        assert len(operation_errors) == 1
        assert isinstance(operation_errors[0], PluginExecutionWorkerError)
        assert str(operation_errors[0]) == "plugin_runtime_closed"
        assert worker.active_management_thread_count == 0
        assert worker.active_process_ids == ()
    finally:
        release_start.set()
        operation_thread.join(10)
        if close_thread.ident is not None:
            close_thread.join(10)
        worker.close()


def test_close_fails_while_direct_sync_spawn_is_not_yet_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retained: list[str] = []
    worker = PluginExecutionWorker(
        child_handler=handle,
        shutdown_timeout_seconds=0.1,
        reap_pending_callback=lambda: retained.append("retained"),
    )
    process = _FakeProcess()
    spawn_started = threading.Event()
    release_spawn = threading.Event()
    operation_errors: list[BaseException] = []

    def gated_spawn(*_args: object, **_kwargs: object) -> _SpawnedWorker:
        spawn_started.set()
        assert release_spawn.wait(10)
        return _SpawnedWorker(process, None, None)  # type: ignore[arg-type]

    monkeypatch.setattr(worker, "_spawn", gated_spawn)
    monkeypatch.setattr(
        worker,
        "_kill_and_reap",
        lambda *_args, **_kwargs: (False, False),
    )

    def run_operation() -> None:
        try:
            worker.run_sync(
                authority=_authority(),
                body={"action": "return", "value": "must-not-run"},
                credential={},
                timeout_seconds=10.0,
            )
        except BaseException as exc:
            operation_errors.append(exc)

    operation_thread = threading.Thread(target=run_operation)
    operation_thread.start()
    try:
        assert spawn_started.wait(10)
        with pytest.raises(
            PluginExecutionWorkerError,
            match="^plugin_worker_reap_failed$",
        ):
            worker.close()

        release_spawn.set()
        operation_thread.join(10)
        assert not operation_thread.is_alive()
        assert len(operation_errors) == 1
        assert isinstance(operation_errors[0], PluginExecutionWorkerError)
        assert str(operation_errors[0]) == "plugin_worker_reap_failed"
        assert retained == ["retained"]
        assert worker.active_process_ids == (8123,)
        assert [outcome.status for outcome in worker.terminal_outcomes] == [
            "reap_failed"
        ]
    finally:
        release_spawn.set()
        operation_thread.join(10)
        process.alive = False
        monkeypatch.setattr(
            worker,
            "_kill_and_reap",
            lambda *_args, **_kwargs: (True, False),
        )
        worker.close()


def test_pipe_setup_failure_unconditionally_releases_start_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = PluginExecutionWorker(
        child_handler=handle,
        shutdown_timeout_seconds=0.1,
    )

    class FailingContext:
        @staticmethod
        def Pipe(*, duplex: bool) -> tuple[object, object]:
            assert duplex is False
            raise OSError("pipe allocation failed")

    monkeypatch.setattr(worker, "_context", FailingContext())

    with pytest.raises(
        PluginExecutionWorkerError,
        match="^plugin_worker_unavailable$",
    ):
        worker.run_sync(
            authority=_authority(),
            body={"action": "return", "value": "must-not-run"},
            credential={},
            timeout_seconds=1.0,
        )

    assert worker._starting_operations == set()
    assert [outcome.status for outcome in worker.terminal_outcomes] == [
        "spawn_failed"
    ]
    worker.close()


def test_post_spawn_endpoint_close_failure_keeps_live_worker_tracked_and_fenced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retained: list[str] = []
    worker = PluginExecutionWorker(
        child_handler=handle,
        shutdown_timeout_seconds=0.1,
        reap_pending_callback=lambda: retained.append("retained"),
    )
    process = _FakeProcess()

    class CloseFailingContext:
        @staticmethod
        def Pipe(*, duplex: bool) -> tuple[object, object]:
            assert duplex is False
            return _CloseRaisesConnection(), _FakeConnection()

    monkeypatch.setattr(worker, "_context", CloseFailingContext())
    monkeypatch.setattr(
        worker,
        "_spawn",
        lambda *_args, **_kwargs: _SpawnedWorker(  # type: ignore[arg-type]
            process,
            None,
            None,
        ),
    )
    monkeypatch.setattr(
        worker,
        "_kill_and_reap",
        lambda *_args, **_kwargs: (False, False),
    )

    with pytest.raises(
        PluginExecutionWorkerError,
        match="^plugin_worker_reap_failed$",
    ):
        worker.run_sync(
            authority=_authority(),
            body={"action": "return", "value": "must-not-run"},
            credential={},
            timeout_seconds=1.0,
        )

    assert worker._starting_operations == set()
    assert worker.active_process_ids == (8123,)
    assert retained == ["retained"]
    assert [outcome.status for outcome in worker.terminal_outcomes] == [
        "reap_failed"
    ]

    process.alive = False
    monkeypatch.setattr(
        worker,
        "_kill_and_reap",
        lambda *_args, **_kwargs: (True, False),
    )
    worker.close()


@pytest.mark.anyio
async def test_cancelled_async_operation_reaps_and_joins_before_propagation(
    tmp_path: Path,
) -> None:
    worker_started = tmp_path / "cancelled-worker-started"
    late_effect = tmp_path / "cancelled-worker-late-effect"
    worker = PluginExecutionWorker(
        child_handler=blocked_handler,
        shutdown_timeout_seconds=1.0,
    )
    operation = asyncio.create_task(
        worker.run_async(
            authority=_authority(),
            body={
                "worker_started_path": str(worker_started),
                "effect_path": str(late_effect),
                "effect_delay_seconds": 1.0,
            },
            credential={"access_token": "memory-only"},
            timeout_seconds=60.0,
        )
    )
    try:
        deadline = asyncio.get_running_loop().time() + 10.0
        while not worker_started.exists():
            if asyncio.get_running_loop().time() >= deadline:
                pytest.fail("async worker did not start")
            await asyncio.sleep(0.01)
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation

        assert worker.active_process_ids == ()
        assert worker.active_management_thread_count == 0
        await asyncio.sleep(1.2)
        assert not late_effect.exists()
        assert worker.terminal_outcomes[-1].status == "cancelled"
    finally:
        if not operation.done():
            operation.cancel()
        worker.close()


@pytest.mark.anyio
async def test_failed_cancellation_reap_globally_denies_successor_until_close_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker_started = tmp_path / "reap-pending-worker-started"
    late_effect = tmp_path / "reap-pending-worker-late-effect"
    retained: list[str] = []
    worker = PluginExecutionWorker(
        child_handler=blocked_handler,
        shutdown_timeout_seconds=1.0,
        reap_pending_callback=lambda: retained.append("retained"),
    )
    real_reap = worker._kill_and_reap
    reap_attempts = 0

    def fail_first_cleanup(
        running: _RunningWorker,
        **kwargs: object,
    ) -> tuple[bool, bool]:
        nonlocal reap_attempts
        reap_attempts += 1
        result = real_reap(running, **kwargs)  # type: ignore[arg-type]
        if reap_attempts == 1:
            assert result[0] is True
            return False, result[1]
        return result

    monkeypatch.setattr(worker, "_kill_and_reap", fail_first_cleanup)
    operation = asyncio.create_task(
        worker.run_async(
            authority=_authority(),
            body={
                "worker_started_path": str(worker_started),
                "effect_path": str(late_effect),
                "effect_delay_seconds": 1.0,
            },
            credential={},
            timeout_seconds=60.0,
        )
    )
    try:
        deadline = asyncio.get_running_loop().time() + 10.0
        while not worker_started.exists():
            if asyncio.get_running_loop().time() >= deadline:
                pytest.fail("async worker did not start")
            await asyncio.sleep(0.01)
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation

        assert retained == ["retained"]
        assert worker.active_management_thread_count == 0
        with pytest.raises(
            PluginExecutionWorkerError,
            match="^plugin_worker_reap_pending$",
        ):
            worker.run_sync(
                authority=replace(
                    _authority(),
                    plugin_id="northwind.calendar",
                    app_id="northwind.calendar.mcp",
                ),
                body={"action": "return", "value": "denied"},
                credential={},
                timeout_seconds=1.0,
            )

        worker.close()
        assert reap_attempts == 2
        assert worker.active_process_ids == ()
        assert [outcome.status for outcome in worker.terminal_outcomes] == [
            "reap_failed",
            "closed",
        ]
        await asyncio.sleep(1.2)
        assert not late_effect.exists()
    finally:
        if not operation.done():
            operation.cancel()
        if worker.active_process_ids:
            worker.close()


def test_parent_death_lifeline_kills_a_remote_worker_with_a_blocked_handler(
    tmp_path: Path,
) -> None:
    worker_started = tmp_path / "remote-worker-started"
    late_effect = tmp_path / "remote-late-effect"
    body = {
        "worker_started_path": str(worker_started),
        "effect_path": str(late_effect),
        "effect_delay_seconds": 2.0,
    }
    desktop = multiprocessing.get_context("spawn").Process(
        target=run_desktop_parent,
        args=(_authority(), body),
        kwargs={"disable_windows_job": True},
    )
    worker_pid: int | None = None
    try:
        desktop.start()
        _wait_for_path(worker_started)
        worker_pid = int(worker_started.read_text(encoding="utf-8"))
        desktop.kill()
        desktop.join(timeout=10.0)

        assert not desktop.is_alive()
        assert _wait_for_process_exit(worker_pid, timeout=5.0)
        time.sleep(2.2)
        assert not late_effect.exists()
    finally:
        if desktop.is_alive():
            desktop.kill()
            desktop.join(timeout=5.0)
        if worker_pid is not None:
            _terminate_exact_process(worker_pid)


@pytest.mark.skipif(os.name == "nt", reason="POSIX escaped-descendant containment")
def test_parent_death_kills_an_escaped_local_provider_before_late_effect(
    tmp_path: Path,
) -> None:
    worker_started = tmp_path / "local-worker-started"
    provider_started = tmp_path / "local-provider-started"
    late_effect = tmp_path / "local-late-effect"
    state_root = tmp_path / "mcp-state"
    state_root.mkdir()
    local_execution = _LocalMcpWorkerExecution(
        plugin_registry_root=str(tmp_path / "plugin-registry"),
        plugin_registry_revision="1" * 64,
        plugin_package_root=str(tmp_path / "plugin-package"),
        mcp_state_root=str(state_root),
        app_registry_revision="2" * 64,
        package_registry_revision="3" * 64,
        mcp_package_root=str(tmp_path / "mcp-package"),
        mcp_package_manifest_sha256="4" * 64,
        executable_path=str(tmp_path / "mcp-package" / "provider"),
        executable_sha256="5" * 64,
        executable_identity=(1, 1),
    )
    body = {
        "worker_started_path": str(worker_started),
        "provider_started_path": str(provider_started),
        "effect_path": str(late_effect),
        "effect_delay_seconds": 2.0,
        "local_execution": local_execution.to_dict(),
    }
    desktop = multiprocessing.get_context("spawn").Process(
        target=run_desktop_parent,
        args=(_authority(), body),
        kwargs={"disable_windows_job": False},
    )
    worker_pid: int | None = None
    provider_pid: int | None = None
    try:
        desktop.start()
        # This path starts a desktop, guardian and worker from the portable
        # interpreter. Cold imports from a mounted SSD can exceed 15 seconds;
        # readiness is separate from the provider's two-second effect clock.
        _wait_for_path(worker_started, timeout=45.0)
        _wait_for_path(provider_started)
        worker_pid = int(worker_started.read_text(encoding="utf-8"))
        provider_pid = int(provider_started.read_text(encoding="utf-8"))
        time.sleep(0.1)
        desktop.kill()
        desktop.join(timeout=10.0)

        assert _wait_for_process_exit(worker_pid, timeout=5.0)
        assert _wait_for_process_exit(provider_pid, timeout=5.0)
        time.sleep(2.2)
        assert not late_effect.exists()
    finally:
        if desktop.is_alive():
            desktop.kill()
            desktop.join(timeout=5.0)
        for pid in (provider_pid, worker_pid):
            if pid is not None:
                _terminate_exact_process(pid)


class _DarwinBsdInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


class _DarwinUniqueInfo(ctypes.Structure):
    _fields_ = [
        ("p_uuid", ctypes.c_uint8 * 16),
        ("p_uniqueid", ctypes.c_uint64),
        ("p_puniqueid", ctypes.c_uint64),
        ("p_idversion", ctypes.c_int32),
        ("p_orig_ppidversion", ctypes.c_int32),
        ("p_reserve2", ctypes.c_uint64),
        ("p_reserve3", ctypes.c_uint64),
    ]


class _DarwinBsdInfoWithUniqueId(ctypes.Structure):
    _fields_ = [
        ("pbsd", _DarwinBsdInfo),
        ("p_uniqidentifier", _DarwinUniqueInfo),
    ]


def test_darwin_libproc_process_table_uses_stable_unique_parent_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = {
        600: (10, 600, 2, 8000, 7000),
        601: (600, 601, 5, 8001, 8000),
        602: (600, 600, 2, 8002, 8000),
    }
    observed_flavors: list[int] = []

    def list_all_pids(buffer: object, _buffer_size: int) -> int:
        if not buffer:
            return len(rows)
        target = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_int32))
        for index, pid in enumerate(rows):
            target[index] = pid
        return len(rows)

    def pid_info(
        pid: int,
        flavor: int,
        _argument: int,
        buffer: object,
        buffer_size: int,
    ) -> int:
        observed_flavors.append(flavor)
        assert buffer_size == ctypes.sizeof(_DarwinBsdInfoWithUniqueId) == 192
        ppid, process_group, status, unique_id, parent_unique_id = rows[pid]
        target = ctypes.cast(
            buffer,
            ctypes.POINTER(_DarwinBsdInfoWithUniqueId),
        ).contents
        target.pbsd.pbi_status = status
        target.pbsd.pbi_pid = pid
        target.pbsd.pbi_ppid = ppid
        target.pbsd.pbi_pgid = process_group
        target.p_uniqidentifier.p_uniqueid = unique_id
        target.p_uniqidentifier.p_puniqueid = parent_unique_id
        return buffer_size

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        worker_module,
        "_load_darwin_libproc",
        lambda: (list_all_pids, pid_info),
        raising=False,
    )
    def session_id(pid: int) -> int:
        if pid == 602:
            raise ProcessLookupError
        return 600

    monkeypatch.setattr(worker_module, "_darwin_session_id", session_id, raising=False)

    table = worker_module._read_posix_process_table()

    assert set(table) == {600, 601}
    assert table[600].identity == (600, 8000)
    assert table[601].ppid == 600
    assert table[601].parent_start_time == 8000
    assert table[601].process_group == 601
    assert table[601].session == 600
    assert table[601].live is False
    assert observed_flavors == [18, 18, 18]


def test_darwin_reparented_escape_remains_discoverable_by_parent_unique_id() -> None:
    record = worker_module._PosixProcessRecord
    departed_parent = record(
        pid=601,
        ppid=600,
        process_group=600,
        session=600,
        start_time=8001,
        parent_start_time=8000,
    )
    table = {
        600: record(
            pid=600,
            ppid=10,
            process_group=600,
            session=600,
            start_time=8000,
            parent_start_time=7000,
        ),
        602: record(
            pid=602,
            ppid=1,
            process_group=602,
            session=602,
            start_time=8002,
            parent_start_time=8001,
        ),
    }

    descendants = worker_module._discover_posix_descendants(
        600,
        table,
        known=(departed_parent,),
    )

    assert set(descendants) == {602}
    assert not descendants[602].attached_to(600)


def test_darwin_known_parent_pid_reuse_never_claims_an_unrelated_tree() -> None:
    record = worker_module._PosixProcessRecord
    departed_parent = record(
        pid=601,
        ppid=600,
        process_group=600,
        session=600,
        start_time=8001,
        parent_start_time=8000,
    )
    table = {
        600: record(
            pid=600,
            ppid=10,
            process_group=600,
            session=600,
            start_time=8000,
            parent_start_time=7000,
        ),
        601: record(
            pid=601,
            ppid=10,
            process_group=601,
            session=601,
            start_time=9991,
            parent_start_time=7000,
        ),
        602: record(
            pid=602,
            ppid=1,
            process_group=602,
            session=602,
            start_time=8002,
            parent_start_time=8001,
        ),
        603: record(
            pid=603,
            ppid=601,
            process_group=601,
            session=601,
            start_time=9992,
            parent_start_time=9991,
        ),
    }

    descendants = worker_module._discover_posix_descendants(
        600,
        table,
        known=(departed_parent,),
    )

    assert set(descendants) == {602}


def test_monitor_retains_exact_root_identity_after_root_disappears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = worker_module._PosixProcessRecord
    root = record(
        pid=600,
        ppid=10,
        process_group=600,
        session=600,
        start_time=8000,
        parent_start_time=7000,
    )
    reparented_provider = record(
        pid=601,
        ppid=1,
        process_group=601,
        session=601,
        start_time=8001,
        parent_start_time=8000,
    )
    current = {600: root}
    monkeypatch.setattr(worker_module.os, "name", "posix")
    monkeypatch.setattr(worker_module.sys, "platform", "darwin")
    monkeypatch.setattr(
        worker_module,
        "_read_posix_process_table",
        lambda: dict(current),
    )
    monitor = worker_module._PosixDescendantMonitor(
        600,
        monitor_deadline=time.monotonic() + 5.0,
    )
    monitor._stop.set()
    monitor.start()
    monitor._thread.join(timeout=1.0)

    current.clear()
    current[601] = reparented_provider

    assert monitor._sample() == {601: reparented_provider}


def test_retained_root_identity_rejects_pid_reuse() -> None:
    record = worker_module._PosixProcessRecord
    departed_root = record(
        pid=600,
        ppid=10,
        process_group=600,
        session=600,
        start_time=8000,
        parent_start_time=7000,
    )
    table = {
        600: record(
            pid=600,
            ppid=10,
            process_group=600,
            session=600,
            start_time=9000,
            parent_start_time=7000,
        ),
        601: record(
            pid=601,
            ppid=600,
            process_group=600,
            session=600,
            start_time=9001,
            parent_start_time=9000,
        ),
    }

    assert worker_module._discover_posix_descendants(
        600,
        table,
        known=(departed_root,),
    ) == {}


def test_provider_pin_requires_one_live_exact_attached_direct_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = worker_module._PosixProcessRecord
    root = record(
        pid=600,
        ppid=10,
        process_group=600,
        session=600,
        start_time=8000,
        parent_start_time=7000,
    )
    current = {
        600: root,
        601: record(
            pid=601,
            ppid=600,
            process_group=600,
            session=600,
            start_time=8001,
            parent_start_time=8000,
        ),
    }
    monkeypatch.setattr(
        worker_module,
        "_read_posix_process_table",
        lambda: dict(current),
    )
    monitor = object.__new__(worker_module._PosixDescendantMonitor)
    monitor._worker_pid = 600
    monitor._finish_lock = threading.Lock()
    monitor._lock = threading.Lock()
    monitor._stop = threading.Event()
    monitor._root_record = root
    monitor._tracked = {}
    monitor._started = True
    monitor._finished = False

    assert monitor.pin(601).identity == (601, 8001)

    for replacement in (
        record(
            pid=601,
            ppid=1,
            process_group=600,
            session=600,
            start_time=8001,
            parent_start_time=8000,
        ),
        record(
            pid=601,
            ppid=600,
            process_group=601,
            session=601,
            start_time=8001,
            parent_start_time=8000,
        ),
        record(
            pid=601,
            ppid=600,
            process_group=600,
            session=600,
            start_time=8001,
            state="Z",
            parent_start_time=8000,
        ),
    ):
        current[601] = replacement
        with pytest.raises(OSError, match="plugin_worker_process_pin_invalid"):
            monitor.pin(601, expected_start_time=8001)

    current[601] = record(
        pid=601,
        ppid=600,
        process_group=600,
        session=600,
        start_time=9001,
        parent_start_time=8000,
    )
    with pytest.raises(OSError, match="plugin_worker_process_pin_invalid"):
        monitor.pin(601, expected_start_time=8001)
    with pytest.raises(OSError, match="plugin_worker_process_pin_invalid"):
        monitor.pin(600)


def test_provider_pin_ack_rejects_stale_generation_and_tamper() -> None:
    secret = b"s" * 32
    nonce = "a" * 64
    record = worker_module._PosixProcessRecord(
        pid=601,
        ppid=600,
        process_group=600,
        session=600,
        start_time=8001,
    )

    class Monitor:
        def pin(
            self,
            pid: int,
            *,
            expected_start_time: int | None = None,
        ) -> object:
            assert pid == 601
            assert expected_start_time in {None, 8001}
            return record

    for generation, frame in (
        (
            7,
            worker_module._encode_authenticated_frame(
                secret=secret,
                kind="provider_release",
                nonce=nonce,
                generation=6,
                sequence=6,
                body={"pid": 601, "start_time": 8001},
                max_frame_bytes=4096,
            ),
        ),
        (
            7,
            b"\x00\x00\x00\x01x",
        ),
        (
            7,
            worker_module._encode_authenticated_frame(
                secret=secret,
                kind="provider_release",
                nonce=nonce,
                generation=7,
                sequence=6,
                body={"pid": 999, "start_time": 8001},
                max_frame_bytes=4096,
            ),
        ),
    ):
        command_recv, command_send = multiprocessing.get_context("spawn").Pipe(
            duplex=False
        )
        result_recv, result_send = multiprocessing.get_context("spawn").Pipe(
            duplex=False
        )
        try:
            command_send.send_bytes(frame)
            protocol = worker_module._ProviderPinProtocol(
                secret=secret,
                nonce=nonce,
                generation=generation,
                command_recv=command_recv,
                result_send=result_send,
                max_frame_bytes=4096,
                monitor=Monitor(),
            )

            with pytest.raises(
                PluginExecutionWorkerError,
                match="worker_protocol_invalid",
            ):
                protocol.pin(601)

            notice = worker_module._decode_authenticated_frame(
                result_recv.recv_bytes(),
                secret=secret,
                expected_kind="provider_started",
                expected_nonce=nonce,
                expected_generation=7,
                expected_sequence=5,
                max_frame_bytes=4096,
            )
            assert notice["body"] == {"pid": 601, "start_time": 8001}
        finally:
            for connection in (
                command_recv,
                command_send,
                result_recv,
                result_send,
            ):
                connection.close()


def test_provider_pin_protocol_rechecks_identity_before_valid_release() -> None:
    secret = b"s" * 32
    nonce = "a" * 64
    record = worker_module._PosixProcessRecord(
        pid=601,
        ppid=600,
        process_group=600,
        session=600,
        start_time=8001,
    )
    pins: list[int | None] = []

    class Monitor:
        def pin(
            self,
            pid: int,
            *,
            expected_start_time: int | None = None,
        ) -> object:
            assert pid == 601
            pins.append(expected_start_time)
            return record

    command_recv, command_send = multiprocessing.get_context("spawn").Pipe(
        duplex=False
    )
    result_recv, result_send = multiprocessing.get_context("spawn").Pipe(
        duplex=False
    )
    try:
        command_send.send_bytes(
            worker_module._encode_authenticated_frame(
                secret=secret,
                kind="provider_release",
                nonce=nonce,
                generation=7,
                sequence=6,
                body={"pid": 601, "start_time": 8001},
                max_frame_bytes=4096,
            )
        )
        protocol = worker_module._ProviderPinProtocol(
            secret=secret,
            nonce=nonce,
            generation=7,
            command_recv=command_recv,
            result_send=result_send,
            max_frame_bytes=4096,
            monitor=Monitor(),
        )

        protocol.pin(601)

        notice = worker_module._decode_authenticated_frame(
            result_recv.recv_bytes(),
            secret=secret,
            expected_kind="provider_started",
            expected_nonce=nonce,
            expected_generation=7,
            expected_sequence=5,
            max_frame_bytes=4096,
        )
        assert notice["body"] == {"pid": 601, "start_time": 8001}
        assert pins == [None, 8001]
        assert protocol.used is True
    finally:
        for connection in (
            command_recv,
            command_send,
            result_recv,
            result_send,
        ):
            connection.close()


@pytest.mark.skipif(os.name == "nt", reason="real POSIX provider pin containment")
def test_preexec_pin_kills_provider_that_detaches_and_kills_worker_before_poll(
    tmp_path: Path,
) -> None:
    provider_started = tmp_path / "immediate-provider-started"
    late_effect = tmp_path / "immediate-provider-late-effect"
    state_root = tmp_path / "mcp-state"
    state_root.mkdir()
    local_execution = _LocalMcpWorkerExecution(
        plugin_registry_root=str(tmp_path / "plugin-registry"),
        plugin_registry_revision="1" * 64,
        plugin_package_root=str(tmp_path / "plugin-package"),
        mcp_state_root=str(state_root),
        app_registry_revision="2" * 64,
        package_registry_revision="3" * 64,
        mcp_package_root=str(tmp_path / "mcp-package"),
        mcp_package_manifest_sha256="4" * 64,
        executable_path=str(tmp_path / "mcp-package" / "provider"),
        executable_sha256="5" * 64,
        executable_identity=(1, 1),
    )
    worker = PluginExecutionWorker(
        child_handler=detach_and_kill_worker_handler,
        shutdown_timeout_seconds=2.0,
    )
    provider_pid: int | None = None
    try:
        with pytest.raises(
            PluginExecutionWorkerError,
            match="plugin_local_process_model_incompatible",
        ):
            worker.run_sync(
                authority=_authority(),
                body={
                    "provider_started_path": str(provider_started),
                    "effect_path": str(late_effect),
                    "effect_delay_seconds": 1.0,
                    "local_execution": local_execution.to_dict(),
                },
                credential={},
                timeout_seconds=10.0,
            )
        _wait_for_path(provider_started)
        provider_pid = int(provider_started.read_text(encoding="utf-8"))
        assert _wait_for_process_exit(provider_pid, timeout=5.0)
        time.sleep(1.2)
        assert not late_effect.exists()
        assert worker.terminal_outcomes[-1].status == "quarantined"
    finally:
        try:
            worker.close()
        except PluginExecutionWorkerError:
            pass
        if provider_pid is not None:
            _terminate_exact_process(provider_pid)


@pytest.mark.skipif(sys.platform != "linux", reason="real Linux subreaper contract")
def test_linux_subreaper_kills_reparented_double_fork_after_provider_release(
    tmp_path: Path,
) -> None:
    provider_started = tmp_path / "double-fork-provider-started"
    late_effect = tmp_path / "double-fork-provider-late-effect"
    state_root = tmp_path / "mcp-state"
    state_root.mkdir()
    local_execution = _LocalMcpWorkerExecution(
        plugin_registry_root=str(tmp_path / "plugin-registry"),
        plugin_registry_revision="1" * 64,
        plugin_package_root=str(tmp_path / "plugin-package"),
        mcp_state_root=str(state_root),
        app_registry_revision="2" * 64,
        package_registry_revision="3" * 64,
        mcp_package_root=str(tmp_path / "mcp-package"),
        mcp_package_manifest_sha256="4" * 64,
        executable_path=str(tmp_path / "mcp-package" / "provider"),
        executable_sha256="5" * 64,
        executable_identity=(1, 1),
    )
    worker = PluginExecutionWorker(
        child_handler=double_fork_reparented_handler,
        shutdown_timeout_seconds=2.0,
    )
    provider_pid: int | None = None
    try:
        with pytest.raises(
            PluginExecutionWorkerError,
            match="plugin_local_process_model_incompatible",
        ):
            worker.run_sync(
                authority=_authority(),
                body={
                    "provider_started_path": str(provider_started),
                    "effect_path": str(late_effect),
                    "effect_delay_seconds": 1.0,
                    "local_execution": local_execution.to_dict(),
                },
                credential={},
                timeout_seconds=10.0,
            )
        _wait_for_path(provider_started)
        provider_pid = int(provider_started.read_text(encoding="utf-8"))
        assert _wait_for_process_exit(provider_pid, timeout=5.0)
        time.sleep(1.2)
        assert not late_effect.exists()
        assert worker.terminal_outcomes[-1].status == "quarantined"
    finally:
        try:
            worker.close()
        except PluginExecutionWorkerError:
            pass
        if provider_pid is not None:
            _terminate_exact_process(provider_pid)


@pytest.mark.skipif(sys.platform != "linux", reason="real Linux guardian contract")
def test_linux_guardian_catches_fork_escape_that_kills_worker_before_sampling(
    tmp_path: Path,
) -> None:
    provider_started = tmp_path / "combined-provider-started"
    late_effect = tmp_path / "combined-provider-late-effect"
    state_root = tmp_path / "mcp-state"
    state_root.mkdir()
    local_execution = _LocalMcpWorkerExecution(
        plugin_registry_root=str(tmp_path / "plugin-registry"),
        plugin_registry_revision="1" * 64,
        plugin_package_root=str(tmp_path / "plugin-package"),
        mcp_state_root=str(state_root),
        app_registry_revision="2" * 64,
        package_registry_revision="3" * 64,
        mcp_package_root=str(tmp_path / "mcp-package"),
        mcp_package_manifest_sha256="4" * 64,
        executable_path=str(tmp_path / "mcp-package" / "provider"),
        executable_sha256="5" * 64,
        executable_identity=(1, 1),
    )
    worker = PluginExecutionWorker(
        child_handler=fork_escape_kill_worker_handler,
        shutdown_timeout_seconds=2.0,
    )
    escaped_pid: int | None = None
    try:
        with pytest.raises(
            PluginExecutionWorkerError,
            match="plugin_local_process_model_incompatible",
        ):
            worker.run_sync(
                authority=_authority(),
                body={
                    "provider_started_path": str(provider_started),
                    "effect_path": str(late_effect),
                    "effect_delay_seconds": 1.0,
                    "local_execution": local_execution.to_dict(),
                },
                credential={},
                timeout_seconds=10.0,
            )
        _wait_for_path(provider_started)
        escaped_pid = int(provider_started.read_text(encoding="utf-8"))
        assert _wait_for_process_exit(escaped_pid, timeout=5.0)
        time.sleep(1.2)
        assert not late_effect.exists()
        assert worker.terminal_outcomes[-1].status == "quarantined"
    finally:
        try:
            worker.close()
        except PluginExecutionWorkerError:
            pass
        if escaped_pid is not None:
            _terminate_exact_process(escaped_pid)


@pytest.mark.skipif(sys.platform != "linux", reason="real Linux guardian contract")
def test_linux_guardian_allows_a_clean_worker_and_fences_crash_successor(
    tmp_path: Path,
) -> None:
    worker = PluginExecutionWorker(
        child_handler=handle,
        shutdown_timeout_seconds=2.0,
    )
    local_execution = _local_execution(tmp_path)
    try:
        clean = worker.run_sync(
            authority=_authority(),
            body={
                "action": "return",
                "value": "clean",
                "local_execution": local_execution.to_dict(),
            },
            credential={},
            timeout_seconds=10.0,
        )
        assert clean == {"value": "clean"}
        with pytest.raises(
            PluginExecutionWorkerError,
            match="plugin_local_process_model_incompatible",
        ):
            worker.run_sync(
                authority=_authority(),
                body={
                    "action": "crash",
                    "local_execution": local_execution.to_dict(),
                },
                credential={},
                timeout_seconds=10.0,
            )
        successor = worker.run_sync(
            authority=_authority(),
            body={
                "action": "return",
                "value": "successor",
                "local_execution": _local_execution(tmp_path / "successor").to_dict(),
            },
            credential={},
            timeout_seconds=10.0,
        )
        assert successor == {"value": "successor"}
        assert [outcome.generation for outcome in worker.terminal_outcomes] == [1, 2, 4]
        assert [outcome.status for outcome in worker.terminal_outcomes] == [
            "completed",
            "quarantined",
            "completed",
        ]
    finally:
        worker.close()


@pytest.mark.skipif(sys.platform != "linux", reason="real Linux guardian contract")
def test_linux_guardian_timeout_and_guardian_crash_leave_no_late_effect(
    tmp_path: Path,
) -> None:
    timeout_started = tmp_path / "timeout-worker-started"
    timeout_started_at = tmp_path / "timeout-worker-started-at"
    timeout_effect = tmp_path / "timeout-late-effect"
    deadline_seconds = 15.0
    effect_delay_seconds = 16.0
    timeout_worker = PluginExecutionWorker(
        child_handler=handle,
        shutdown_timeout_seconds=2.0,
    )
    try:
        operation_started_at = time.monotonic()
        with pytest.raises(
            PluginExecutionWorkerError,
            # A started local worker killed at its deadline is quarantined,
            # as in the native local-worker timeout contract. A startup-only
            # timeout does not exercise this process-containment boundary.
            match="plugin_local_process_model_incompatible",
        ):
            timeout_worker.run_sync(
                authority=_authority(),
                body={
                    "action": "late_marker",
                    "started_path": str(timeout_started),
                    "started_at_path": str(timeout_started_at),
                    "delay_seconds": effect_delay_seconds,
                    "marker_path": str(timeout_effect),
                    "local_execution": _local_execution(tmp_path).to_dict(),
                },
                credential={},
                # A subsecond deadline expires during portable interpreter
                # startup, before the guardian can publish a worker identity.
                # Exercise timeout of a running handler, then prove its PID
                # is dead rather than assuming startup was instantaneous.
                timeout_seconds=deadline_seconds,
            )
        assert time.monotonic() - operation_started_at >= deadline_seconds
        assert timeout_started.is_file(), "timeout must exercise a started handler"
        timeout_pid = int(timeout_started.read_text(encoding="utf-8"))
        assert _wait_for_process_exit(timeout_pid, timeout=5.0)
        assert timeout_worker.active_process_ids == ()
        assert timeout_worker.terminal_outcomes[-1].status == "quarantined"
        effect_due = float(timeout_started_at.read_text(encoding="utf-8")) + effect_delay_seconds
        time.sleep(max(0.0, effect_due - time.monotonic()) + 0.25)
        assert not timeout_effect.exists()
    finally:
        timeout_worker.close()

    crash_started = tmp_path / "guardian-crash-started"
    crash_effect = tmp_path / "guardian-crash-late-effect"
    crash_worker = PluginExecutionWorker(
        child_handler=kill_linux_guardian_handler,
        shutdown_timeout_seconds=2.0,
    )
    try:
        with pytest.raises(
            PluginExecutionWorkerError,
            match="plugin_local_process_model_incompatible",
        ):
            crash_worker.run_sync(
                authority=_authority(),
                body={
                    "worker_started_path": str(crash_started),
                    "effect_path": str(crash_effect),
                    "effect_delay_seconds": 1.0,
                    # The timeout case above quarantines its exact identity.
                    # Exercise an actual guardian crash with a distinct job.
                    "local_execution": _local_execution(tmp_path / "guardian-crash").to_dict(),
                },
                credential={},
                timeout_seconds=10.0,
            )
        _wait_for_path(crash_started)
        worker_pid, guardian_pid = (
            int(value) for value in crash_started.read_text(encoding="utf-8").split(":")
        )
        assert _wait_for_process_exit(worker_pid, timeout=5.0)
        assert _wait_for_process_exit(guardian_pid, timeout=5.0)
        time.sleep(1.0)
        assert not crash_effect.exists()
    finally:
        crash_worker.close()


def test_linux_guardian_reap_failure_is_fenced_until_exact_retry() -> None:
    class GuardianProcess:
        pid = 8124
        exitcode = worker_module._LINUX_GUARDIAN_REAP_FAILED_EXIT

        @staticmethod
        def is_alive() -> bool:
            return False

        @staticmethod
        def join(_timeout: float) -> None:
            return None

    worker = PluginExecutionWorker(
        child_handler=handle,
        shutdown_timeout_seconds=1.0,
    )
    running = _RunningWorker(
        authority=_authority(),
        generation=1,
        nonce="nonce",
        spawned=_SpawnedWorker(
            GuardianProcess(),  # type: ignore[arg-type]
            None,
            None,
            linux_guarded=True,
        ),
        bootstrap_send=_FakeConnection(),  # type: ignore[arg-type]
        command_send=_FakeConnection(),  # type: ignore[arg-type]
        result_recv=_FakeConnection(),  # type: ignore[arg-type]
        local_execution=None,
    )

    assert worker._kill_and_reap(running) == (False, False)
    assert running.spawned.guardian_reap_failure_seen is True
    assert worker._kill_and_reap(running) == (True, True)


def test_live_linux_guardian_retries_cleanup_only_on_exact_parent_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class GuardianProcess:
        pid = 8124
        exitcode = worker_module._LINUX_GUARDIAN_REAP_FAILED_EXIT
        alive = True

        def is_alive(self) -> bool:
            return self.alive

        @staticmethod
        def join(_timeout: float) -> None:
            return None

    process = GuardianProcess()
    signals: list[tuple[int, int]] = []

    def signal_guardian(pid: int, requested_signal: int) -> None:
        signals.append((pid, requested_signal))
        process.alive = False

    monkeypatch.setattr(worker_module.os, "kill", signal_guardian)
    worker = PluginExecutionWorker(
        child_handler=handle,
        shutdown_timeout_seconds=1.0,
    )
    running = _RunningWorker(
        authority=_authority(),
        generation=1,
        nonce="nonce",
        spawned=_SpawnedWorker(
            process,  # type: ignore[arg-type]
            None,
            None,
            linux_guarded=True,
        ),
        bootstrap_send=_FakeConnection(),  # type: ignore[arg-type]
        command_send=_FakeConnection(),  # type: ignore[arg-type]
        result_recv=_FakeConnection(),  # type: ignore[arg-type]
        local_execution=None,
    )

    assert worker._kill_and_reap(running) == (False, False)
    assert running.spawned.guardian_reap_failure_seen is True
    assert signals == []

    assert worker._kill_and_reap(running) == (True, True)
    assert signals == [
        (8124, worker_module._LINUX_GUARDIAN_RETRY_SIGNAL),
    ]


def test_linux_guardian_ready_identity_rejects_stale_generation() -> None:
    secret = b"g" * 32
    encoded = worker_module._encode_authenticated_frame(
        secret=secret,
        kind="ready",
        nonce="a" * 64,
        generation=8,
        sequence=2,
        body={"pid": 4001, "start_time": 5001},
        max_frame_bytes=4096,
    )

    with pytest.raises(PluginExecutionWorkerError, match="worker_protocol_invalid"):
        worker_module._decode_authenticated_frame(
            encoded,
            secret=secret,
            expected_kind="ready",
            expected_nonce="a" * 64,
            expected_generation=9,
            expected_sequence=2,
            max_frame_bytes=4096,
        )


@pytest.mark.skipif(sys.platform != "darwin", reason="real macOS libproc contract")
def test_real_darwin_process_table_keeps_self_identity_stable() -> None:
    first = worker_module._read_posix_process_table()
    second = worker_module._read_posix_process_table()
    pid = os.getpid()

    assert first[pid].identity == second[pid].identity
    assert first[pid].process_group == os.getpgid(pid)
    assert first[pid].session == os.getsid(pid)
