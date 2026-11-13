"""Cross-process fencing tests for long-lived plugin lifecycle ownership."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import multiprocessing
import os
from pathlib import Path
from queue import Empty
import threading

import pytest

import mcp_platform.plugin_lifecycle as lifecycle_module
from mcp_platform.canonical import canonical_bytes
from mcp_platform.plugin_authority import PluginAuthorityService, PluginAuthorityStore
from mcp_platform.plugin_backend import CurrentMcpPluginBackend
from mcp_platform.plugin_lifecycle import PluginLifecycleService
from mcp_platform.plugin_permissions import PluginPermissionStore
from mcp_platform.supervisor import McpProcessSupervisor, McpSupervisorError
from tests.mcp_platform.test_plugin_authority import _connection_authority
from tests.mcp_platform.test_plugin_permissions import (
    _Clock,
    _Random,
    _bindings_for_plan,
    _factory,
    _plan,
)


def _admission_owner(
    root: str,
    ready: object,
    commands: object,
    results: object,
) -> None:
    service = PluginLifecycleService(state_root=Path(root))
    accepted = service._admit_lifecycle_operation(
        kind="update",
        plugin_id="plugin.alpha",
    )
    results.put(("accepted", accepted))
    ready.set()
    while True:
        command = commands.get()
        if command == "release":
            try:
                result = service._release_lifecycle_operation_if_current(accepted)
            except Exception as exc:  # pragma: no cover - asserted in the parent
                results.put(("release_error", str(exc)))
            else:
                results.put(("released", result))
        elif command == "close":
            service.close()
            results.put(("closed", None))
        elif command == "stop":
            return


def _lease_before_journal_owner(root: str, ready: object, commands: object) -> None:
    service = PluginLifecycleService(state_root=Path(root))
    assert service._acquire_lifecycle_owner_lease() is True
    ready.set()
    commands.get()
    service.close()


def _normal_mutation_owner(root: str, ready: object, commands: object) -> None:
    service = PluginLifecycleService(state_root=Path(root))
    with service._lifecycle_mutation_lease():
        ready.set()
        commands.get()
    service.close()


def _read_authority_connection(
    path: str,
    plugin_id: str,
    app_id: str,
    account_key_hash: str,
    results: object,
) -> None:
    try:
        connection = PluginAuthorityStore(path=Path(path)).connection(
            plugin_id,
            app_id,
            account_key_hash,
        )
    except Exception as exc:  # pragma: no cover - asserted in the parent
        results.put(("error", str(exc)))
    else:
        results.put(("connection", connection.revision))


def _claim_or_observe_owner(
    root: str,
    ready: object,
    release: object,
    results: object,
) -> None:
    service = PluginLifecycleService.__new__(PluginLifecycleService)
    service._configure_lifecycle_owner_state(Path(root) / "plugins")
    claimed = service._claim_lifecycle_operation_ownership()
    operation = service._read_lifecycle_operation_document()["operation"]
    results.put((claimed, operation))
    ready.set()
    release.wait(15)
    service.close()


def _construct_lifecycle(root: str, results: object) -> None:
    try:
        service = PluginLifecycleService(state_root=Path(root))
    except Exception as exc:  # pragma: no cover - asserted in the parent
        results.put(("error", str(exc)))
    else:
        service.close()
        results.put(("constructed", None))


class _EmptySupervisorRegistry:
    @staticmethod
    def list_records() -> tuple[object, ...]:
        return ()


def _context() -> multiprocessing.context.BaseContext:
    return multiprocessing.get_context("spawn")


def _result(queue: object) -> tuple[str, object]:
    try:
        return queue.get(timeout=15)
    except Empty:  # pragma: no cover - clearer failure than a hung test
        pytest.fail("lifecycle owner process produced no result")


def test_live_cross_process_owner_is_never_recovered_by_a_constructor(
    tmp_path: Path,
) -> None:
    context = _context()
    ready = context.Event()
    commands = context.Queue()
    results = context.Queue()
    owner = context.Process(
        target=_admission_owner,
        args=(str(tmp_path), ready, commands, results),
    )
    owner.start()
    try:
        assert ready.wait(15)
        _, accepted = _result(results)
        before = (tmp_path / "plugins" / "lifecycle-operation.json").read_bytes()
        with pytest.raises(
            ValueError,
            match="^plugin_lifecycle_operation_in_progress$",
        ):
            PluginLifecycleService(state_root=tmp_path)
        assert (tmp_path / "plugins" / "lifecycle-operation.json").read_bytes() == before
        commands.put("release")
        assert _result(results) == ("released", True)
        commands.put("stop")
        owner.join(15)
        assert owner.exitcode == 0
    finally:
        if owner.is_alive():
            owner.terminate()
            owner.join(15)


def test_live_os_lease_cannot_be_substituted_by_a_durable_owner_identity(
    tmp_path: Path,
) -> None:
    context = _context()
    ready = context.Event()
    commands = context.Queue()
    results = context.Queue()
    owner = context.Process(
        target=_admission_owner,
        args=(str(tmp_path), ready, commands, results),
    )
    owner.start()
    try:
        assert ready.wait(15)
        _kind, accepted = _result(results)
        assert accepted["operation_id"] != "f" * 64
        path = tmp_path / "plugins" / "lifecycle-operation.json"
        document = PluginLifecycleService._decode_lifecycle_operation_document(
            path.read_bytes()
        )
        document["operation"]["operation_id"] = "f" * 64
        substituted = canonical_bytes(document)
        path.write_bytes(substituted)

        with pytest.raises(
            ValueError,
            match="^plugin_lifecycle_operation_in_progress$",
        ):
            PluginLifecycleService(state_root=tmp_path)
        assert path.read_bytes() == substituted

        commands.put("stop")
        owner.join(15)
        assert owner.exitcode == 0
    finally:
        if owner.is_alive():
            owner.terminate()
            owner.join(15)


def test_live_owner_lease_path_cannot_create_a_replacement_owner(
    tmp_path: Path,
) -> None:
    context = _context()
    ready = context.Event()
    commands = context.Queue()
    results = context.Queue()
    owner = context.Process(
        target=_admission_owner,
        args=(str(tmp_path), ready, commands, results),
    )
    owner.start()
    try:
        assert ready.wait(15)
        _kind, accepted = _result(results)
        operation_path = tmp_path / "plugins" / "lifecycle-operation.json"
        lease_path = tmp_path / "plugins" / PluginLifecycleService._lifecycle_owner_lease_name(
            int(accepted["generation"]) - 1
        )
        before = operation_path.read_bytes()

        if os.name == "nt":
            with pytest.raises(OSError):
                lease_path.unlink()
            replacement = tmp_path / "plugins" / "replacement.lease"
            replacement.write_bytes(b"")
            with pytest.raises(OSError):
                replacement.replace(lease_path)
        else:
            lease_path.unlink()

        with pytest.raises(
            ValueError,
            match="^plugin_lifecycle_operation_in_progress$",
        ):
            PluginLifecycleService(state_root=tmp_path)
        assert operation_path.read_bytes() == before

        commands.put("release")
        expected = (
            ("released", True)
            if os.name == "nt"
            else ("release_error", "plugin_lifecycle_operation_conflict")
        )
        assert _result(results) == expected
        commands.put("stop")
        owner.join(15)
        assert owner.exitcode == 0
    finally:
        if owner.is_alive():
            owner.terminate()
            owner.join(15)


def test_os_lease_closes_the_admission_before_journal_crash_window(
    tmp_path: Path,
) -> None:
    context = _context()
    ready = context.Event()
    commands = context.Queue()
    owner = context.Process(
        target=_lease_before_journal_owner,
        args=(str(tmp_path), ready, commands),
    )
    owner.start()
    try:
        assert ready.wait(15)
        with pytest.raises(
            ValueError,
            match="^plugin_lifecycle_operation_in_progress$",
        ):
            PluginLifecycleService(state_root=tmp_path)

        owner.terminate()
        owner.join(15)
        assert owner.exitcode is not None
        successor = PluginLifecycleService(state_root=tmp_path)
        accepted = successor._admit_lifecycle_operation(
            kind="install",
            plugin_id="plugin.beta",
        )
        assert accepted["generation"] == 1
        successor._release_lifecycle_operation(accepted)
        successor.close()
    finally:
        if owner.is_alive():
            owner.terminate()
            owner.join(15)


def test_live_lifecycle_owner_denies_a_normal_cross_process_mutation(
    tmp_path: Path,
) -> None:
    context = _context()
    ready = context.Event()
    commands = context.Queue()
    results = context.Queue()
    owner = context.Process(
        target=_admission_owner,
        args=(str(tmp_path), ready, commands, results),
    )
    owner.start()
    try:
        assert ready.wait(15)
        _kind, _accepted = _result(results)
        before = (tmp_path / "plugins" / "lifecycle-operation.json").read_bytes()
        mutator = PluginLifecycleService.__new__(PluginLifecycleService)
        mutator._configure_lifecycle_owner_state(tmp_path / "plugins")
        with pytest.raises(
            ValueError,
            match="^plugin_lifecycle_operation_in_progress$",
        ):
            with mutator._lifecycle_mutation_lease():
                pytest.fail("normal mutation entered a live lifecycle owner")
        assert (tmp_path / "plugins" / "lifecycle-operation.json").read_bytes() == before
        mutator.close()
        commands.put("release")
        assert _result(results) == ("released", True)
        commands.put("stop")
        owner.join(15)
        assert owner.exitcode == 0
    finally:
        if owner.is_alive():
            owner.terminate()
            owner.join(15)


def test_normal_cross_process_mutation_lease_serializes_lifecycle_admission(
    tmp_path: Path,
) -> None:
    context = _context()
    ready = context.Event()
    commands = context.Queue()
    owner = context.Process(
        target=_normal_mutation_owner,
        args=(str(tmp_path), ready, commands),
    )
    owner.start()
    try:
        assert ready.wait(15)
        with pytest.raises(
            ValueError,
            match="^plugin_lifecycle_operation_in_progress$",
        ):
            PluginLifecycleService(state_root=tmp_path)
        commands.put("release")
        owner.join(15)
        assert owner.exitcode == 0
        contender = PluginLifecycleService(state_root=tmp_path)
        accepted = contender._admit_lifecycle_operation(
            kind="update",
            plugin_id="plugin.alpha",
        )
        contender._release_lifecycle_operation(accepted)
        contender.close()
    finally:
        if owner.is_alive():
            owner.terminate()
            owner.join(15)


@pytest.mark.anyio
async def test_inherited_child_cannot_outlive_parent_mutation_lease(
    tmp_path: Path,
) -> None:
    service = PluginLifecycleService(state_root=tmp_path)
    entered = asyncio.Event()
    release_child = asyncio.Event()
    mutations: list[str] = []

    async def inherited_child() -> None:
        with service._component_mutation_lease():
            entered.set()
            await release_child.wait()
            mutations.append("unfenced")

    with service._authority_mutation_lock:
        child = asyncio.create_task(inherited_child())
        await asyncio.sleep(0)

    context = _context()
    ready = context.Event()
    commands = context.Queue()
    results = context.Queue()
    owner = context.Process(
        target=_admission_owner,
        args=(str(tmp_path), ready, commands, results),
    )
    owner.start()
    try:
        assert await asyncio.to_thread(ready.wait, 15)
        _kind, _accepted = await asyncio.to_thread(_result, results)
        release_child.set()
        with pytest.raises(
            ValueError,
            match="^plugin_lifecycle_operation_in_progress$",
        ):
            await child
        assert not entered.is_set()
        assert mutations == []
        commands.put("release")
        assert await asyncio.to_thread(_result, results) == ("released", True)
        commands.put("stop")
        await asyncio.to_thread(owner.join, 15)
        assert owner.exitcode == 0
    finally:
        release_child.set()
        if not child.done():
            child.cancel()
            await asyncio.gather(child, return_exceptions=True)
        if owner.is_alive():
            owner.terminate()
            owner.join(15)
        service.close()


def test_foreign_read_does_not_recover_a_live_execution_reservation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "authority.json"
    store = PluginAuthorityStore(path=path)
    connection = _connection_authority()
    store.publish_connection(connection, expected_revision="")
    store.reserve_execution(
        kind="direct_turn",
        execution_key="c" * 64,
        plugin_id=connection.plugin_id,
        app_id=connection.app_id,
        account_key_hash=connection.account_key_hash,
        process_epoch="d" * 64,
        reservation_id="e" * 64,
        lineage_key="f" * 64,
        active_episode_id="episode-1",
    )
    before = path.read_bytes()
    context = _context()
    results = context.Queue()
    reader = context.Process(
        target=_read_authority_connection,
        args=(
            str(path),
            connection.plugin_id,
            connection.app_id,
            connection.account_key_hash,
            results,
        ),
    )
    reader.start()
    reader.join(15)
    try:
        assert reader.exitcode == 0
        assert _result(results) == ("connection", connection.revision)
        assert path.read_bytes() == before
    finally:
        if reader.is_alive():
            reader.terminate()
            reader.join(15)


def test_same_process_authority_mutations_serialize_on_one_boundary(
    tmp_path: Path,
) -> None:
    store = PluginPermissionStore(
        path=tmp_path / "permission.json",
        authority_factory=lambda *_args: (),
        binding_resolver=lambda *_args: (),
    )
    service = PluginLifecycleService(state_root=tmp_path, permission_store=store)
    entered = threading.Event()
    release = threading.Event()
    second_completed = threading.Event()

    def hold_boundary() -> None:
        with service.plugin_execution_mutation_scope():
            entered.set()
            assert release.wait(15)

    def read_pending() -> tuple[object, ...]:
        result = store.pending()
        second_completed.set()
        return result

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            owner = pool.submit(hold_boundary)
            assert entered.wait(15)
            contender = pool.submit(read_pending)
            assert not second_completed.wait(0.2)
            release.set()
            owner.result(timeout=15)
            assert contender.result(timeout=15) == ()
            assert second_completed.is_set()
    finally:
        release.set()
        service.close()


def test_permission_issue_hands_owner_capability_to_joined_transport_task(
    tmp_path: Path,
) -> None:
    holder: dict[str, object] = {}
    child_calls: list[str] = []

    def authority_factory(plan: object, account: str, grant_id: str) -> object:
        async def resolve() -> None:
            store = holder["store"]
            assert isinstance(store, PluginPermissionStore)
            assert store.pending() == ()
            child_calls.append("resolved")

        service = holder["service"]
        assert isinstance(service, PluginLifecycleService)
        service._submit_connection_operation(resolve)
        return _factory(plan, account, grant_id)

    store = PluginPermissionStore(
        path=tmp_path / "permission.json",
        monotonic=_Clock().monotonic,
        wall_time=_Clock().wall,
        random_bytes=_Random(),
        authority_factory=authority_factory,
        binding_resolver=_bindings_for_plan,
        ttl_seconds=30.0,
    )
    service = PluginLifecycleService(state_root=tmp_path, permission_store=store)
    service._operation_submitter = lambda operation, **_kwargs: asyncio.run(operation())
    holder.update(service=service, store=store)
    try:
        request = store.issue(
            _plan(),
            account_key_hash="8" * 64,
            prior_episode_id="episode-1",
            task_id="logical-task-1",
        )
        assert request.request_id
        assert child_calls == ["resolved"]
    finally:
        service.close()


def test_timed_out_supervisor_cleanup_retains_cross_process_lifecycle_lease(
    tmp_path: Path,
) -> None:
    service = PluginLifecycleService(state_root=tmp_path)
    supervisor = McpProcessSupervisor(
        registry=_EmptySupervisorRegistry(),  # type: ignore[arg-type]
        external_switch=object(),  # type: ignore[arg-type]
        verifier=lambda _path: None,  # type: ignore[return-value]
        session_factory=lambda *_args: None,  # type: ignore[return-value]
    ).start()
    entered = threading.Event()
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()
    returned = threading.Event()
    errors: list[BaseException] = []

    async def operation() -> None:
        with service._component_mutation_lease():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await asyncio.to_thread(release_cleanup.wait)

    def submit() -> None:
        try:
            with service.plugin_execution_mutation_scope():
                supervisor.submit_plugin_operation_sync(
                    service._handoff_lifecycle_mutation_operation(operation),
                    timeout_s=0.01,
                )
        except BaseException as exc:
            errors.append(exc)
        finally:
            returned.set()

    caller = threading.Thread(target=submit)
    caller.start()
    context = _context()
    results = context.Queue()
    contender = context.Process(
        target=_construct_lifecycle,
        args=(str(tmp_path), results),
    )
    try:
        assert entered.wait(15)
        assert cleanup_started.wait(15)
        assert returned.wait(0.1) is False
        contender.start()
        contender.join(15)
        assert contender.exitcode == 0
        assert _result(results) == (
            "error",
            "plugin_lifecycle_operation_in_progress",
        )
        release_cleanup.set()
        caller.join(15)
        assert returned.is_set()
        assert len(errors) == 1
        assert isinstance(errors[0], McpSupervisorError)
        assert str(errors[0]) == "supervisor_operation_timeout"
        successor = PluginLifecycleService(state_root=tmp_path)
        successor.close()
    finally:
        release_cleanup.set()
        caller.join(15)
        if contender.is_alive():
            contender.terminate()
            contender.join(15)
        supervisor.shutdown()
        service.close()


def test_close_retains_cross_process_lease_until_session_cleanup_finishes(
    tmp_path: Path,
) -> None:
    service = PluginLifecycleService(state_root=tmp_path)
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()

    class BlockingBackend:
        def close(self) -> None:
            cleanup_started.set()
            assert release_cleanup.wait(15)

    service.plugin_backend = BlockingBackend()  # type: ignore[assignment]
    closer = threading.Thread(target=service.close)
    closer.start()
    context = _context()
    results = context.Queue()
    contender = context.Process(
        target=_construct_lifecycle,
        args=(str(tmp_path), results),
    )
    try:
        assert cleanup_started.wait(15)
        contender.start()
        contender.join(15)
        assert contender.exitcode == 0
        assert _result(results) == (
            "error",
            "plugin_lifecycle_operation_in_progress",
        )
        release_cleanup.set()
        closer.join(15)
        assert not closer.is_alive()
        successor = PluginLifecycleService(state_root=tmp_path)
        successor.close()
    finally:
        release_cleanup.set()
        closer.join(15)
        if contender.is_alive():
            contender.terminate()
            contender.join(15)


def test_close_reap_failure_retains_exact_lease_until_retry_succeeds(
    tmp_path: Path,
) -> None:
    service = PluginLifecycleService(state_root=tmp_path)
    attempts = 0

    class RetryBackend:
        def close(self) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ValueError("plugin_worker_reap_failed")

    service.plugin_backend = RetryBackend()  # type: ignore[assignment]
    context = _context()
    results = context.Queue()
    contender = context.Process(
        target=_construct_lifecycle,
        args=(str(tmp_path), results),
    )
    try:
        with pytest.raises(ValueError, match="^plugin_worker_reap_failed$"):
            service.close()
        contender.start()
        contender.join(15)
        assert contender.exitcode == 0
        assert _result(results) == (
            "error",
            "plugin_lifecycle_operation_in_progress",
        )

        service.close()
        assert attempts == 2
        successor = PluginLifecycleService(state_root=tmp_path)
        successor.close()
    finally:
        if contender.is_alive():
            contender.terminate()
            contender.join(15)
        if not getattr(service, "_closed", True):
            service.close()


def test_operation_reap_pending_retains_cross_process_lease_until_close_retry(
    tmp_path: Path,
) -> None:
    service = PluginLifecycleService(state_root=tmp_path)
    close_calls = 0

    class CleanupBackend:
        def close(self) -> None:
            nonlocal close_calls
            close_calls += 1

    service.plugin_backend = CleanupBackend()  # type: ignore[assignment]
    with service.plugin_execution_mutation_scope():
        service._retain_lifecycle_owner_lease_for_worker_reap()

    context = _context()
    results = context.Queue()
    contender = context.Process(
        target=_construct_lifecycle,
        args=(str(tmp_path), results),
    )
    try:
        contender.start()
        contender.join(15)
        assert contender.exitcode == 0
        assert _result(results) == (
            "error",
            "plugin_lifecycle_operation_in_progress",
        )

        service.close()
        assert close_calls == 1
        successor = PluginLifecycleService(state_root=tmp_path)
        successor.close()
    finally:
        if contender.is_alive():
            contender.terminate()
            contender.join(15)
        if not getattr(service, "_closed", True):
            service.close()


def test_close_uses_execution_lock_order_without_default_service_deadlock(
    tmp_path: Path,
) -> None:
    service = PluginLifecycleService(state_root=tmp_path)
    close_waiting = threading.Event()
    allow_default_lookup = threading.Event()
    worker_returned = threading.Event()
    close_returned = threading.Event()
    errors: list[BaseException] = []

    class ObservedBoundary(lifecycle_module._LifecycleMutationBoundary):
        def __enter__(self) -> lifecycle_module._LifecycleMutationBoundary:
            if threading.current_thread().name == "lifecycle-close":
                close_waiting.set()
            return super().__enter__()

    service._authority_mutation_lock = ObservedBoundary(
        service._lifecycle_mutation_lease
    )
    with lifecycle_module._DEFAULT_SERVICE_LOCK:
        previous_default = lifecycle_module._DEFAULT_SERVICE
        lifecycle_module._DEFAULT_SERVICE = service

    def execute() -> None:
        try:
            with service.plugin_execution_mutation_scope():
                assert allow_default_lookup.wait(15)
                assert lifecycle_module.get_default_plugin_lifecycle_service() is service
        except BaseException as exc:
            errors.append(exc)
        finally:
            worker_returned.set()

    def close() -> None:
        try:
            service.close()
        except BaseException as exc:
            errors.append(exc)
        finally:
            close_returned.set()

    worker = threading.Thread(target=execute, name="plugin-execution")
    closer = threading.Thread(target=close, name="lifecycle-close")
    worker.start()
    closer.start()
    try:
        assert close_waiting.wait(15)
        allow_default_lookup.set()
        assert worker_returned.wait(15)
        assert close_returned.wait(15)
        assert errors == []
    finally:
        allow_default_lookup.set()
        worker.join(15)
        closer.join(15)
        with lifecycle_module._DEFAULT_SERVICE_LOCK:
            if lifecycle_module._DEFAULT_SERVICE is service:
                lifecycle_module._DEFAULT_SERVICE = previous_default
        service.close()


def test_close_seals_boundary_before_releasing_queued_mutator(
    tmp_path: Path,
) -> None:
    service = PluginLifecycleService(state_root=tmp_path)
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()
    exit_released = threading.Event()
    allow_close_return = threading.Event()
    mutator_started = threading.Event()
    mutator_returned = threading.Event()
    close_returned = threading.Event()
    reached_body = threading.Event()
    errors: list[str] = []

    class PausedExitBoundary(lifecycle_module._LifecycleMutationBoundary):
        def __exit__(self, *args: object) -> None:
            super().__exit__(*args)
            if threading.current_thread().name == "lifecycle-close":
                exit_released.set()
                assert allow_close_return.wait(15)

    class BlockingBackend:
        def close(self) -> None:
            cleanup_started.set()
            assert release_cleanup.wait(15)

    service._authority_mutation_lock = PausedExitBoundary(
        service._lifecycle_mutation_lease
    )
    service.plugin_backend = BlockingBackend()  # type: ignore[assignment]

    def mutate() -> None:
        mutator_started.set()
        try:
            with service.plugin_execution_mutation_scope():
                reached_body.set()
        except ValueError as exc:
            errors.append(str(exc))
        finally:
            mutator_returned.set()

    closer = threading.Thread(
        target=lambda: (service.close(), close_returned.set()),
        name="lifecycle-close",
    )
    mutator = threading.Thread(target=mutate, name="queued-mutator")
    closer.start()
    try:
        assert cleanup_started.wait(15)
        mutator.start()
        assert mutator_started.wait(15)
        release_cleanup.set()
        assert exit_released.wait(15)
        assert mutator_returned.wait(15)
        assert not reached_body.is_set()
        assert errors == ["plugin_lifecycle_operation_conflict"]
        allow_close_return.set()
        assert close_returned.wait(15)
    finally:
        release_cleanup.set()
        allow_close_return.set()
        closer.join(15)
        mutator.join(15)
        service.close()


def test_preconstructed_process_denies_every_real_authority_mutation_entry(
    tmp_path: Path,
) -> None:
    permission_store = PluginPermissionStore(
        path=tmp_path / "mcp" / "permission.json",
        authority_factory=lambda *_args: (),
        binding_resolver=lambda *_args: (),
    )
    authority_service = PluginAuthorityService(
        store=PluginAuthorityStore(path=tmp_path / "mcp" / "authority.json"),
        binding_resolver=lambda *_args: (),
    )
    mutator = PluginLifecycleService(
        state_root=tmp_path,
        permission_store=permission_store,
        authority_service=authority_service,
    )
    backend = CurrentMcpPluginBackend.__new__(CurrentMcpPluginBackend)
    backend._lifecycle_mutation_guard = mutator._component_mutation_lease
    context = _context()
    ready = context.Event()
    commands = context.Queue()
    results = context.Queue()
    owner = context.Process(
        target=_admission_owner,
        args=(str(tmp_path), ready, commands, results),
    )
    owner.start()
    try:
        assert ready.wait(15)
        _kind, _accepted = _result(results)
        calls = (
            lambda: asyncio.run(mutator.start_connection("plugin.alpha")),
            lambda: asyncio.run(
                mutator.complete_connection("https://omega.test/callback")
            ),
            lambda: asyncio.run(mutator.poll_connection("a" * 64)),
            lambda: asyncio.run(
                mutator.set_permissions(
                    "plugin.alpha",
                    revision="b" * 64,
                    settings={},
                )
            ),
            lambda: asyncio.run(mutator.disconnect("plugin.alpha")),
            lambda: mutator.ensure_plugin_ready("plugin.alpha"),
            lambda: mutator.authority_context(None, None, None),
            permission_store.pending,
            lambda: authority_service.authorize_direct_turn(
                None,
                connection=None,
                connection_snapshot=None,
                candidate_id="c" * 64,
                step_id="step",
                active_contributor={},
            ),
        )
        for call in calls:
            with pytest.raises(
                ValueError,
                match="^plugin_lifecycle_operation_in_progress$",
            ):
                call()
        commands.put("release")
        assert _result(results) == ("released", True)
        commands.put("stop")
        owner.join(15)
        assert owner.exitcode == 0
    finally:
        mutator.close()
        if owner.is_alive():
            owner.terminate()
            owner.join(15)


def test_backend_sync_facade_rejects_foreign_owner_before_submission(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    class Connections:
        async def prepare_execution_worker(
            self, *_args: object, **_kwargs: object
        ) -> object:
            calls.append("client")
            raise AssertionError("foreign owner reached client resolution")

        async def close_all(self) -> None:
            return None

    def descriptor(_plugin_id: str, _app_id: str) -> object:
        calls.append("descriptor")
        raise AssertionError("foreign owner reached descriptor resolution")

    def account() -> str:
        calls.append("account")
        return "account-1"

    def submit(_operation: object, **_kwargs: object) -> object:
        calls.append("submit")
        raise AssertionError("foreign owner reached supervisor submission")

    class Worker:
        """The backend never builds its own worker; injection is required."""

        def run_sync(self, **_kwargs: object) -> object:
            calls.append("worker")
            raise AssertionError("foreign owner reached worker execution")

        def close(self) -> None:
            return None

    mutator = PluginLifecycleService(state_root=tmp_path)
    backend = CurrentMcpPluginBackend(
        connection_service=Connections(),  # type: ignore[arg-type]
        descriptor_provider=descriptor,  # type: ignore[arg-type]
        plugin_id_for_app={"app.alpha": "plugin.alpha"},
        account_key_provider=account,
        operation_submitter=submit,
        execution_worker=Worker(),
    )
    backend._lifecycle_mutation_guard = mutator._component_mutation_lease
    context = _context()
    ready = context.Event()
    commands = context.Queue()
    results = context.Queue()
    owner = context.Process(
        target=_admission_owner,
        args=(str(tmp_path), ready, commands, results),
    )
    owner.start()
    try:
        assert ready.wait(15)
        _kind, _accepted = _result(results)
        with pytest.raises(
            ValueError,
            match="^plugin_lifecycle_operation_in_progress$",
        ):
            backend.call_tool_sync_for_plugin(
                "plugin.alpha",
                "app.alpha",
                "tool.alpha",
                {},
            )
        assert calls == []
        commands.put("release")
        assert _result(results) == ("released", True)
        commands.put("stop")
        owner.join(15)
        assert owner.exitcode == 0
    finally:
        mutator.close()
        if owner.is_alive():
            owner.terminate()
            owner.join(15)


def test_constructor_failure_releases_the_process_lease_immediately(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="^oauth_profile_catalog_unavailable$"):
        PluginLifecycleService(state_root=tmp_path, oauth_broker=object())

    successor = PluginLifecycleService(state_root=tmp_path)
    accepted = successor._admit_lifecycle_operation(
        kind="install",
        plugin_id="plugin.after-failure",
    )
    successor._release_lifecycle_operation(accepted)
    successor.close()


def test_idle_constructor_releases_process_lease_before_returning(
    tmp_path: Path,
) -> None:
    service = PluginLifecycleService(state_root=tmp_path)
    context = _context()
    results = context.Queue()
    contender = context.Process(
        target=_construct_lifecycle,
        args=(str(tmp_path), results),
    )
    try:
        assert service._lifecycle_owner_lease is None
        contender.start()
        contender.join(15)
        assert contender.exitcode == 0
        assert _result(results) == ("constructed", None)
    finally:
        if contender.is_alive():
            contender.terminate()
            contender.join(15)
        service.close()


def test_constructor_recovery_keeps_reused_component_guarded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = PluginPermissionStore(
        path=tmp_path / "permission.json",
        authority_factory=lambda *_args: (),
        binding_resolver=lambda *_args: (),
    )
    store._lifecycle_mutation_guard = lambda: pytest.raises(AssertionError)
    entered = threading.Event()
    release = threading.Event()
    original = PluginLifecycleService._reconcile_uninstall_intent

    def paused_recovery(service: PluginLifecycleService) -> None:
        entered.set()
        assert release.wait(15)
        original(service)

    monkeypatch.setattr(
        PluginLifecycleService,
        "_reconcile_uninstall_intent",
        paused_recovery,
    )
    services: list[PluginLifecycleService] = []
    errors: list[BaseException] = []

    def construct() -> None:
        try:
            services.append(
                PluginLifecycleService(
                    state_root=tmp_path / "new-owner",
                    permission_store=store,
                )
            )
        except BaseException as exc:
            errors.append(exc)

    constructor = threading.Thread(target=construct)
    constructor.start()
    try:
        assert entered.wait(15)
        with pytest.raises(
            ValueError,
            match="^plugin_lifecycle_operation_conflict$",
        ):
            store.pending()
        assert services == []
        release.set()
        constructor.join(15)
        assert not constructor.is_alive()
        assert errors == []
        assert len(services) == 1
    finally:
        release.set()
        constructor.join(15)
        for service in services:
            service.close()


def test_constructor_cannot_steal_a_live_component_mutation_guard(
    tmp_path: Path,
) -> None:
    store = PluginPermissionStore(
        path=tmp_path / "permission.json",
        authority_factory=lambda *_args: (),
        binding_resolver=lambda *_args: (),
    )
    first = PluginLifecycleService(
        state_root=tmp_path / "first",
        permission_store=store,
    )
    successor: PluginLifecycleService | None = None
    try:
        with pytest.raises(
            ValueError,
            match="^plugin_lifecycle_operation_conflict$",
        ):
            PluginLifecycleService(
                state_root=tmp_path / "second",
                permission_store=store,
            )
        accepted = first._admit_lifecycle_operation(
            kind="install",
            plugin_id="plugin.alpha",
        )
        with pytest.raises(
            ValueError,
            match="^plugin_lifecycle_operation_in_progress$",
        ):
            store.pending()
        first._release_lifecycle_operation(accepted)
        first.close()
        successor = PluginLifecycleService(
            state_root=tmp_path / "second",
            permission_store=store,
        )
    finally:
        first.close()
        if successor is not None:
            successor.close()


def test_concurrent_constructors_atomically_claim_one_component_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = PluginPermissionStore(
        path=tmp_path / "permission.json",
        authority_factory=lambda *_args: (),
        binding_resolver=lambda *_args: (),
    )
    barrier = threading.Barrier(2)
    original = lifecycle_module._LifecycleMutationBoundary.__init__

    def synchronized_boundary(
        boundary: lifecycle_module._LifecycleMutationBoundary,
        mutation_lease: object = None,
    ) -> None:
        original(boundary, mutation_lease)  # type: ignore[arg-type]
        barrier.wait(timeout=15)

    monkeypatch.setattr(
        lifecycle_module._LifecycleMutationBoundary,
        "__init__",
        synchronized_boundary,
    )
    services: list[PluginLifecycleService] = []
    errors: list[BaseException] = []

    def construct(name: str) -> None:
        try:
            services.append(
                PluginLifecycleService(
                    state_root=tmp_path / name,
                    permission_store=store,
                )
            )
        except BaseException as exc:
            errors.append(exc)

    threads = tuple(
        threading.Thread(target=construct, args=(f"owner-{index}",))
        for index in range(2)
    )
    for thread in threads:
        thread.start()
    try:
        for thread in threads:
            thread.join(15)
        assert all(not thread.is_alive() for thread in threads)
        assert len(services) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], ValueError)
        assert str(errors[0]) == "plugin_lifecycle_operation_conflict"
        owner = services[0]
        accepted = owner._admit_lifecycle_operation(
            kind="install",
            plugin_id="plugin.alpha",
        )
        with pytest.raises(
            ValueError,
            match="^plugin_lifecycle_operation_in_progress$",
        ):
            store.pending()
        owner._release_lifecycle_operation(accepted)
    finally:
        for service in services:
            service.close()


def test_killed_active_owner_is_recovered_by_a_full_constructor_once(
    tmp_path: Path,
) -> None:
    context = _context()
    ready = context.Event()
    commands = context.Queue()
    results = context.Queue()
    owner = context.Process(
        target=_admission_owner,
        args=(str(tmp_path), ready, commands, results),
    )
    owner.start()
    assert ready.wait(15)
    _, stale = _result(results)
    owner.terminate()
    owner.join(15)
    assert owner.exitcode is not None

    restarted = PluginLifecycleService(state_root=tmp_path)
    assert restarted._read_lifecycle_operation_document()["operation"] is None
    replacement = restarted._admit_lifecycle_operation(
        kind="install",
        plugin_id="plugin.after-kill",
    )
    assert replacement["generation"] > stale["generation"]
    restarted._release_lifecycle_operation(replacement)
    restarted.close()


def test_killed_owner_has_exactly_one_concurrent_generation_successor(
    tmp_path: Path,
) -> None:
    context = _context()
    owner_ready = context.Event()
    owner_commands = context.Queue()
    owner_results = context.Queue()
    owner = context.Process(
        target=_admission_owner,
        args=(str(tmp_path), owner_ready, owner_commands, owner_results),
    )
    owner.start()
    assert owner_ready.wait(15)
    _, stale = _result(owner_results)
    owner.terminate()
    owner.join(15)
    assert owner.exitcode is not None

    claim_ready = (context.Event(), context.Event())
    claim_release = context.Event()
    claim_results = context.Queue()
    successors = tuple(
        context.Process(
            target=_claim_or_observe_owner,
            args=(str(tmp_path), claim_ready[index], claim_release, claim_results),
        )
        for index in range(2)
    )
    for successor in successors:
        successor.start()
    try:
        assert all(event.wait(15) for event in claim_ready)
        outcomes = (claim_results.get(timeout=15), claim_results.get(timeout=15))
        winners = tuple(item for item in outcomes if item[0] is True)
        losers = tuple(item for item in outcomes if item[0] is False)
        assert len(winners) == 1
        assert len(losers) == 1
        replacement = winners[0][1]
        assert replacement["operation_id"] == stale["operation_id"]
        assert replacement["generation"] == stale["generation"] + 1
        if os.name != "nt":
            assert replacement["lease_identity"] != stale["lease_identity"]
        # A losing lease claimant may observe the predecessor journal before
        # the winner publishes its generation CAS.  The failed OS lease, not
        # that racy read, is authoritative; the durable state must converge on
        # the one winner.
        durable = PluginLifecycleService._decode_lifecycle_operation_document(
            (tmp_path / "plugins" / "lifecycle-operation.json").read_bytes()
        )
        assert durable["operation"] == replacement
    finally:
        claim_release.set()
        for successor in successors:
            successor.join(15)
            if successor.is_alive():
                successor.terminate()
                successor.join(15)


def test_dead_owner_has_one_generation_fenced_successor_and_stale_clear_loses(
    tmp_path: Path,
) -> None:
    context = _context()
    owner_ready = context.Event()
    owner_commands = context.Queue()
    owner_results = context.Queue()
    owner = context.Process(
        target=_admission_owner,
        args=(str(tmp_path), owner_ready, owner_commands, owner_results),
    )
    owner.start()
    assert owner_ready.wait(15)
    _, stale = _result(owner_results)

    # Release only the OS lease.  The durable operation deliberately survives.
    owner_commands.put("close")
    assert _result(owner_results) == ("closed", None)

    claim_ready = (context.Event(), context.Event())
    claim_release = context.Event()
    claim_results = context.Queue()
    successors = tuple(
        context.Process(
            target=_claim_or_observe_owner,
            args=(str(tmp_path), claim_ready[index], claim_release, claim_results),
        )
        for index in range(2)
    )
    for successor in successors:
        successor.start()
    try:
        assert all(event.wait(15) for event in claim_ready)
        outcomes = (claim_results.get(timeout=15), claim_results.get(timeout=15))
        winners = tuple(item for item in outcomes if item[0] is True)
        losers = tuple(item for item in outcomes if item[0] is False)
        assert len(winners) == 1
        assert len(losers) == 1
        replacement = winners[0][1]
        assert replacement["operation_id"] == stale["operation_id"]
        assert replacement["generation"] == stale["generation"] + 1
        if os.name != "nt":
            assert replacement["lease_identity"] != stale["lease_identity"]

        # The old process remains alive, but its exact generation cannot clear
        # the successor that now owns the operating-system lease.
        owner_commands.put("release")
        assert _result(owner_results) == (
            "release_error",
            "plugin_lifecycle_operation_conflict",
        )
        probe = PluginLifecycleService.__new__(PluginLifecycleService)
        probe._configure_lifecycle_owner_state(tmp_path / "plugins")
        assert probe._read_lifecycle_operation_document()["operation"] == replacement
        probe.close()
    finally:
        claim_release.set()
        for successor in successors:
            successor.join(15)
            if successor.is_alive():
                successor.terminate()
                successor.join(15)
        owner_commands.put("stop")
        owner.join(15)
        if owner.is_alive():
            owner.terminate()
            owner.join(15)
