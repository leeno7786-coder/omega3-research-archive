"""Round-three exact worker admission, containment, and injection contracts."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from mcp_platform import plugin_execution_worker as worker_module
from mcp_platform.plugin_backend import CurrentMcpPluginBackend
from mcp_platform.plugin_connections import PluginConnectionService
from mcp_platform.plugin_execution_worker import (
    PluginExecutionAuthority,
    PluginExecutionWorker,
    PluginExecutionWorkerError,
    _RunningWorker,
    _SpawnedWorker,
)
from tests.mcp_platform.plugin_execution_worker_fixture import handle
from tests.mcp_platform.plugin_execution_worker_round1_fixture import blocked_handler
from tests.mcp_platform.test_plugin_execution_worker_round1 import (
    _wait_for_process_exit,
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


def _other_authority() -> PluginExecutionAuthority:
    return replace(
        _authority(),
        plugin_id="northwind.calendar",
        app_id="northwind.calendar.mcp",
    )


class _FakeProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.alive = True

    def is_alive(self) -> bool:
        return self.alive


class _FakeConnection:
    def close(self) -> None:
        return None


def _spawned_generation(pid: int) -> _RunningWorker:
    connection = _FakeConnection()
    return _RunningWorker(
        authority=_authority(),
        generation=1,
        nonce="f" * 64,
        spawned=_SpawnedWorker(_FakeProcess(pid), None, None),  # type: ignore[arg-type]
        bootstrap_send=connection,  # type: ignore[arg-type]
        command_send=connection,  # type: ignore[arg-type]
        result_recv=connection,  # type: ignore[arg-type]
        local_execution=None,
    )


def test_concurrent_same_key_start_is_denied_before_a_second_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = PluginExecutionWorker(
        child_handler=handle,
        shutdown_timeout_seconds=1.0,
    )
    spawn_entered = threading.Event()
    release_spawn = threading.Event()
    spawn_calls: list[int] = []
    first_errors: list[BaseException] = []

    def gated_spawn(*_args: object, **_kwargs: object) -> _SpawnedWorker:
        spawn_calls.append(len(spawn_calls) + 1)
        if len(spawn_calls) > 1:
            raise AssertionError("a second same-key start reached spawn")
        spawn_entered.set()
        assert release_spawn.wait(10)
        return _SpawnedWorker(_FakeProcess(8123), None, None)  # type: ignore[arg-type]

    monkeypatch.setattr(worker, "_spawn", gated_spawn)
    monkeypatch.setattr(
        worker,
        "_kill_and_reap",
        lambda *_args, **_kwargs: (True, False),
    )

    def run_first() -> None:
        try:
            worker.run_sync(
                authority=_authority(),
                body={"action": "return", "value": "first"},
                credential={},
                timeout_seconds=1.0,
            )
        except BaseException as exc:  # noqa: BLE001 - recorded for the assertion
            first_errors.append(exc)

    first = threading.Thread(target=run_first)
    first.start()
    try:
        assert spawn_entered.wait(10)
        with pytest.raises(
            PluginExecutionWorkerError,
            match="^plugin_operation_in_progress$",
        ):
            worker.run_sync(
                authority=_authority(),
                body={"action": "return", "value": "second"},
                credential={},
                timeout_seconds=1.0,
            )
        assert spawn_calls == [1]
    finally:
        release_spawn.set()
        first.join(20)
    assert not first.is_alive()
    assert len(first_errors) == 1
    worker.close()


def test_registration_never_displaces_the_exact_active_reap_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retained: list[str] = []
    worker = PluginExecutionWorker(
        child_handler=handle,
        shutdown_timeout_seconds=0.1,
        reap_pending_callback=lambda: retained.append("retained"),
    )
    owner = _spawned_generation(8123)
    key = owner.authority.execution_key
    own_process = _FakeProcess(8124)

    def displacing_spawn(*_args: object, **_kwargs: object) -> _SpawnedWorker:
        # An exact owner for this key becomes visible between admission and
        # registration.  Registration must never overwrite it.
        worker._generations[key] = owner.generation
        worker._active[key] = owner
        return _SpawnedWorker(own_process, None, None)  # type: ignore[arg-type]

    monkeypatch.setattr(worker, "_spawn", displacing_spawn)
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
            body={"action": "return", "value": "displacing"},
            credential={},
            timeout_seconds=1.0,
        )

    assert worker._active[key] is owner
    assert retained == ["retained"]
    assert worker.active_process_ids == (8123, 8124)
    assert [outcome.status for outcome in worker.terminal_outcomes] == ["reap_failed"]

    # The spawned-but-unowned generation still fences every execution key.
    with pytest.raises(
        PluginExecutionWorkerError,
        match="^plugin_worker_reap_pending$",
    ):
        worker.run_sync(
            authority=_other_authority(),
            body={"action": "return", "value": "denied"},
            credential={},
            timeout_seconds=1.0,
        )

    own_process.alive = False
    owner.spawned.process.alive = False  # type: ignore[attr-defined]
    monkeypatch.setattr(
        worker,
        "_kill_and_reap",
        lambda *_args, **_kwargs: (True, False),
    )
    worker.close()

    assert worker.active_process_ids == ()
    assert sorted(outcome.status for outcome in worker.terminal_outcomes) == [
        "closed",
        "closed",
        "reap_failed",
    ]


def test_standalone_backend_requires_the_lifecycle_fenced_worker() -> None:
    class _Connections:
        async def prepare_execution_worker(
            self, *_args: object, **_kwargs: object
        ) -> object:
            raise AssertionError("construction must not reach preparation")

    with pytest.raises(ValueError, match="^plugin_backend_worker_invalid$"):
        CurrentMcpPluginBackend(
            connection_service=_Connections(),  # type: ignore[arg-type]
            descriptor_provider=lambda _plugin_id, _app_id: None,  # type: ignore[arg-type,return-value]
            plugin_id_for_app={"app.alpha": "plugin.alpha"},
            account_key_provider=lambda: "account-1",
            operation_submitter=lambda _operation, **_kwargs: None,
        )


class _FailingJob:
    """A post-start containment step that fails after the child exists."""

    def __init__(self) -> None:
        self.pid: int | None = None

    def assign(self, pid: int) -> None:
        self.pid = int(pid)
        raise OSError("job assignment failed")

    def close(self) -> None:
        return None


def test_post_start_containment_failure_keeps_the_exact_child_fenced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retained: list[str] = []
    job = _FailingJob()
    monkeypatch.setattr(worker_module._WindowsJob, "create", lambda: job)
    worker = PluginExecutionWorker(
        child_handler=blocked_handler,
        shutdown_timeout_seconds=2.0,
        reap_pending_callback=lambda: retained.append("retained"),
    )
    real_reap = worker._kill_and_reap
    attempts: list[_RunningWorker] = []

    def fail_first_reap(
        running: _RunningWorker,
        **kwargs: object,
    ) -> tuple[bool, bool]:
        attempts.append(running)
        if len(attempts) == 1:
            return False, False
        return real_reap(running, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(worker, "_kill_and_reap", fail_first_reap)

    try:
        with pytest.raises(
            PluginExecutionWorkerError,
            match="^plugin_worker_reap_failed$",
        ):
            worker.run_sync(
                authority=_authority(),
                body={
                    "worker_started_path": str(tmp_path / "started"),
                    "effect_path": str(tmp_path / "late-effect"),
                    "effect_delay_seconds": 60.0,
                },
                credential={},
                timeout_seconds=30.0,
            )

        assert job.pid is not None
        assert retained == ["retained"]
        assert [outcome.status for outcome in worker.terminal_outcomes] == [
            "reap_failed"
        ]
        with pytest.raises(
            PluginExecutionWorkerError,
            match="^plugin_worker_reap_pending$",
        ):
            worker.run_sync(
                authority=_other_authority(),
                body={"action": "return", "value": "denied"},
                credential={},
                timeout_seconds=1.0,
            )

        worker.close()
        assert len(attempts) == 2
        assert [outcome.status for outcome in worker.terminal_outcomes] == [
            "reap_failed",
            "closed",
        ]
        assert worker.active_process_ids == ()
        assert _wait_for_process_exit(job.pid, 10.0)
        assert not (tmp_path / "late-effect").exists()
    finally:
        try:
            worker.close()
        except PluginExecutionWorkerError:
            pass


class _StallingJob:
    """A post-start containment step that never returns."""

    def __init__(self) -> None:
        self.pid: int | None = None
        self.entered = threading.Event()
        self.release = threading.Event()

    def assign(self, pid: int) -> None:
        self.pid = int(pid)
        self.entered.set()
        assert self.release.wait(180)

    def close(self) -> None:
        return None


def test_permanently_stalled_containment_cancels_boundedly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _StallingJob()
    monkeypatch.setattr(worker_module._WindowsJob, "create", lambda: job)
    worker = PluginExecutionWorker(
        child_handler=blocked_handler,
        shutdown_timeout_seconds=1.0,
    )
    observed: list[object] = []

    def drive() -> None:
        async def main() -> None:
            operation = asyncio.create_task(
                worker.run_async(
                    authority=_authority(),
                    body={
                        "worker_started_path": str(tmp_path / "started"),
                        "effect_path": str(tmp_path / "late-effect"),
                        "effect_delay_seconds": 60.0,
                    },
                    credential={},
                    timeout_seconds=120.0,
                )
            )
            while not job.entered.is_set():
                await asyncio.sleep(0.01)
            operation.cancel()
            try:
                await operation
            except asyncio.CancelledError:
                observed.append("cancelled")
            except BaseException as exc:  # noqa: BLE001 - recorded for assertion
                observed.append(f"{type(exc).__name__}: {exc}")
            else:
                observed.append("returned")

        asyncio.run(main())

    driver = threading.Thread(target=drive, name="round3-stalled-setup-driver")
    driver.start()
    try:
        assert job.entered.wait(30)
        driver.join(30)
        assert not driver.is_alive(), "stalled containment never released cancellation"
        assert observed == ["cancelled"]
        assert job.pid is not None
        assert _wait_for_process_exit(job.pid, 10.0)
        assert worker.active_process_ids == ()
        assert worker.terminal_outcomes[-1].status == "cancelled"
        # The manager is still stuck inside containment, so close stays fenced.
        with pytest.raises(
            PluginExecutionWorkerError,
            match="^plugin_worker_reap_failed$",
        ):
            worker.close()
    finally:
        job.release.set()
        driver.join(30)
        try:
            worker.close()
        except PluginExecutionWorkerError:
            pass
    assert not (tmp_path / "late-effect").exists()


class _AnyCallable:
    """Minimal collaborator stub whose every attribute is callable."""

    def __getattr__(self, name: str) -> object:
        def _call(*_args: object, **_kwargs: object) -> None:
            raise AssertionError(f"configuration must fail before {name}")

        return _call


def test_injected_worker_without_run_async_is_a_typed_configuration_error() -> None:
    class _SyncOnlyWorker:
        def run_sync(self, **_kwargs: object) -> None:
            raise AssertionError("configuration must fail before execution")

        def close(self) -> None:
            return None

    with pytest.raises(
        ValueError,
        match="^plugin_connection_configuration_invalid$",
    ):
        PluginConnectionService(
            app_registry=_AnyCallable(),  # type: ignore[arg-type]
            plugin_registry=_AnyCallable(),  # type: ignore[arg-type]
            broker=_AnyCallable(),  # type: ignore[arg-type]
            grant_service=_AnyCallable(),  # type: ignore[arg-type]
            descriptor_provider=lambda *_args: None,  # type: ignore[arg-type]
            requirement_provider=lambda *_args: None,  # type: ignore[arg-type]
            offered_scopes_provider=lambda *_args: None,  # type: ignore[arg-type]
            operation_submitter=lambda *_args, **_kwargs: None,  # type: ignore[arg-type]
            execution_worker=_SyncOnlyWorker(),
        )


def test_omitted_execution_worker_is_a_typed_configuration_error() -> None:
    """`PluginConnectionService` must never default-construct an unfenced worker.

    A privately constructed worker would execute provider operations behind
    a second, unfenced lifecycle authority whose failed reaps are invisible
    to the shared worker's global admission and close accounting; production
    always injects the single lifecycle-fenced worker from
    `mcp_platform/plugin_lifecycle.py`.  Omitting `execution_worker` must
    fail fast at construction, exactly like `CurrentMcpPluginBackend`.
    """

    with pytest.raises(
        ValueError,
        match="^plugin_connection_configuration_invalid$",
    ):
        PluginConnectionService(
            app_registry=_AnyCallable(),  # type: ignore[arg-type]
            plugin_registry=_AnyCallable(),  # type: ignore[arg-type]
            broker=_AnyCallable(),  # type: ignore[arg-type]
            grant_service=_AnyCallable(),  # type: ignore[arg-type]
            descriptor_provider=lambda *_args: None,  # type: ignore[arg-type]
            requirement_provider=lambda *_args: None,  # type: ignore[arg-type]
            offered_scopes_provider=lambda *_args: None,  # type: ignore[arg-type]
            operation_submitter=lambda *_args, **_kwargs: None,  # type: ignore[arg-type]
        )


def test_failed_descendant_monitor_start_leaves_no_half_attached_containment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = PluginExecutionWorker(
        child_handler=handle,
        shutdown_timeout_seconds=1.0,
    )
    spawned = _SpawnedWorker(_FakeProcess(8321), None, None)  # type: ignore[arg-type]

    class _FailingMonitor:
        def __init__(self, _pid: int, *, monitor_deadline: float) -> None:
            assert monitor_deadline > 0

        def start(self) -> None:
            raise OSError("descendant monitor unavailable")

    monkeypatch.setattr(worker_module, "_PosixDescendantMonitor", _FailingMonitor)
    with pytest.raises(OSError, match="^descendant monitor unavailable$"):
        worker._attach_containment(
            spawned,
            local_contained=True,
            monitor_deadline=1.0,
        )

    assert spawned.descendant_monitor is None
    worker.close()


def test_unpublished_stalled_start_is_globally_fenced_after_the_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retained: list[str] = []
    worker = PluginExecutionWorker(
        child_handler=handle,
        shutdown_timeout_seconds=0.5,
        reap_pending_callback=lambda: retained.append("retained"),
    )
    spawn_entered = threading.Event()
    release_spawn = threading.Event()
    observed: list[object] = []

    def stalled_spawn(*_args: object, **_kwargs: object) -> _SpawnedWorker:
        spawn_entered.set()
        assert release_spawn.wait(180)
        raise OSError("process creation released without a child")

    monkeypatch.setattr(worker, "_spawn", stalled_spawn)

    def drive() -> None:
        async def main() -> None:
            operation = asyncio.create_task(
                worker.run_async(
                    authority=_authority(),
                    body={"action": "return", "value": "stalled"},
                    credential={},
                    timeout_seconds=120.0,
                )
            )
            while not spawn_entered.is_set():
                await asyncio.sleep(0.01)
            operation.cancel()
            try:
                await operation
            except asyncio.CancelledError:
                observed.append("cancelled")
            except BaseException as exc:  # noqa: BLE001 - recorded for assertion
                observed.append(f"{type(exc).__name__}: {exc}")
            else:
                observed.append("returned")

        asyncio.run(main())

    driver = threading.Thread(target=drive, name="round3-unpublished-start-driver")
    driver.start()
    try:
        assert spawn_entered.wait(30)
        driver.join(30)
        assert not driver.is_alive(), "unpublished start never released cancellation"
        assert observed == ["cancelled"]
        # No exact killable handle was ever published, so the lifecycle lease
        # is retained and every execution key stays denied.
        assert retained == ["retained"]
        with pytest.raises(
            PluginExecutionWorkerError,
            match="^plugin_worker_reap_pending$",
        ):
            worker.run_sync(
                authority=_other_authority(),
                body={"action": "return", "value": "denied"},
                credential={},
                timeout_seconds=1.0,
            )
    finally:
        release_spawn.set()
        driver.join(30)
    # The exact retry observes the finished manager and retires the fence.
    worker.close()
    assert worker.active_process_ids == ()
    assert [outcome.status for outcome in worker.terminal_outcomes] == ["spawn_failed"]
