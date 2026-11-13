"""Adversarial worker-only OAuth refresh and durable CAS regressions."""

from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path

import pytest

from mcp_platform import plugin_execution_worker as worker_module
from mcp_platform.oauth_broker import OAuthBroker, _issue_broker_transport_authority
from mcp_platform.oauth_protocol import OAuthProviderError
from mcp_platform.plugin_execution_worker import (
    PluginExecutionAuthority,
    PluginExecutionWorker,
    PluginExecutionWorkerError,
)
from mcp_platform.supervisor import McpProcessSupervisor, McpSupervisorError
from tests.mcp_platform.oauth_refresh_worker_round1_fixture import (
    blocked_refresh_handler,
    cancellable_refresh_handler,
    crashing_refresh_handler,
    dynamic_invalid_client_refresh_handler,
    rejecting_refresh_handler,
    rotating_refresh_handler,
    stale_refresh_handler,
    winner_refresh_handler,
)
from tests.mcp_platform.test_oauth_broker import (
    _Clock,
    _Protocol,
    _Registry,
    _TrackingVault,
    _binding,
    _connected,
    _dynamic_profile,
    _profile,
    _request,
)


def _worker(handler: object) -> PluginExecutionWorker:
    return PluginExecutionWorker(
        child_handler=handler,
        shutdown_timeout_seconds=1.0,
    )


async def _expired_connection(tmp_path: Path):
    clock = _Clock()
    registry = _Registry(_profile())
    vault = _TrackingVault(tmp_path, clock)
    from mcp_platform.oauth_vault import OAuthClientCredential

    vault.store_credential(
        "oauth-client/example.desktop/secret",
        OAuthClientCredential("signed-public-client", "desktop-secret"),
        expected_revision=None,
    )
    protocol = _Protocol()
    broker = OAuthBroker(registry=registry, vault=vault, protocol=protocol, clock=clock)
    prepared, binding = await _connected(broker, _request())
    clock.value = 200.0
    authority = _issue_broker_transport_authority(
        broker,
        current_binding=lambda _observed: binding,
    )
    execution_authority = PluginExecutionAuthority(
        plugin_id=binding.plugin_id,
        app_id=binding.app_id,
        account_key_sha256="1" * 64,
        package_revision="6" * 64,
        connection_revision=binding.connection_revision,
        authority_revision=binding.authority_revision,
        capability_revision=binding.capability_revision,
        capability_kind="tool",
        capability_id="search",
    )
    return (
        clock,
        registry,
        vault,
        protocol,
        broker,
        prepared,
        binding,
        authority,
        execution_authority,
    )


def _transport_authority(broker: OAuthBroker, binding: object):
    return _issue_broker_transport_authority(
        broker,
        current_binding=lambda _observed: binding,
    )


async def _expired_dynamic_connection(tmp_path: Path):
    clock = _Clock()
    profile = _dynamic_profile()
    registry = _Registry(profile)
    vault = _TrackingVault(tmp_path, clock)
    protocol = _Protocol()
    broker = OAuthBroker(registry=registry, vault=vault, protocol=protocol, clock=clock)
    prepared, binding = await _connected(broker, _request())
    clock.value = 200.0
    authority = _transport_authority(broker, binding)
    execution_authority = PluginExecutionAuthority(
        plugin_id=binding.plugin_id,
        app_id=binding.app_id,
        account_key_sha256="1" * 64,
        package_revision="6" * 64,
        connection_revision=binding.connection_revision,
        authority_revision=binding.authority_revision,
        capability_revision=binding.capability_revision,
        capability_kind="tool",
        capability_id="search",
    )
    return clock, profile, vault, protocol, broker, prepared, binding, authority, execution_authority


@pytest.mark.anyio
async def test_dynamic_worker_invalid_client_invalidates_registration_until_next_connect(
    tmp_path: Path,
) -> None:
    (
        _clock,
        profile,
        vault,
        protocol,
        broker,
        prepared,
        binding,
        authority,
        execution_authority,
    ) = await _expired_dynamic_connection(tmp_path)
    worker = _worker(dynamic_invalid_client_refresh_handler)
    try:
        with pytest.raises(ValueError, match="^oauth_reauthorization_required$"):
            await broker._issue_execution_worker_access_token(
                binding,
                authority,
                execution_worker=worker,
                worker_authority=execution_authority,
                timeout_seconds=10.0,
            )
    finally:
        worker.close()

    assert vault.load_registration(profile.authorization_issuer, profile.profile_id) is None
    assert vault.load_token_partition(prepared.partition_key).reauthorization_required is True
    assert protocol.registration_calls == [profile.profile_id]
    await broker.begin(_request())
    assert protocol.registration_calls == [profile.profile_id, profile.profile_id]


@pytest.mark.anyio
async def test_cancelled_refresh_reaps_worker_before_propagation_and_cannot_rotate_vault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        _clock,
        _registry,
        vault,
        _protocol,
        broker,
        prepared,
        binding,
        authority,
        execution_authority,
    ) = await _expired_connection(tmp_path)
    before_partition = vault.load_token_partition(prepared.partition_key)
    before_bytes = vault.path.read_bytes()
    started = tmp_path / "cancelled-refresh-started"
    late_effect = tmp_path / "cancelled-refresh-late-effect"
    monkeypatch.setenv("OMEGA_OAUTH_REFRESH_CANCEL_STARTED_PATH", str(started))
    monkeypatch.setenv("OMEGA_OAUTH_REFRESH_LATE_EFFECT_PATH", str(late_effect))
    worker = _worker(cancellable_refresh_handler)
    operation = asyncio.create_task(
        broker._issue_execution_worker_access_token(
            binding,
            authority,
            execution_worker=worker,
            worker_authority=execution_authority,
            timeout_seconds=60.0,
        )
    )
    try:
        deadline = asyncio.get_running_loop().time() + 10.0
        while not started.exists():
            if asyncio.get_running_loop().time() >= deadline:
                pytest.fail("refresh worker did not start")
            await asyncio.sleep(0.01)
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation

        assert worker.active_process_ids == ()
        assert worker.active_management_thread_count == 0
        assert vault.load_token_partition(prepared.partition_key) == before_partition
        assert vault.path.read_bytes() == before_bytes
        await asyncio.sleep(1.2)
        assert not late_effect.exists()
    finally:
        if not operation.done():
            operation.cancel()
        worker.close()


def test_supervisor_timeout_joins_refresh_cleanup_before_returning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        _clock,
        _registry,
        vault,
        _protocol,
        broker,
        prepared,
        binding,
        authority,
        execution_authority,
    ) = asyncio.run(_expired_connection(tmp_path))
    before_partition = vault.load_token_partition(prepared.partition_key)
    before_bytes = vault.path.read_bytes()
    started = tmp_path / "supervisor-refresh-started"
    late_effect = tmp_path / "supervisor-refresh-late-effect"
    release = tmp_path / "supervisor-refresh-release"
    monkeypatch.setenv("OMEGA_OAUTH_REFRESH_CANCEL_STARTED_PATH", str(started))
    monkeypatch.setenv("OMEGA_OAUTH_REFRESH_LATE_EFFECT_PATH", str(late_effect))
    monkeypatch.setenv("OMEGA_OAUTH_REFRESH_CANCEL_RELEASE_PATH", str(release))
    worker = _worker(cancellable_refresh_handler)

    class EmptyRegistry:
        @staticmethod
        def list_records() -> tuple[object, ...]:
            return ()

    supervisor = McpProcessSupervisor(
        registry=EmptyRegistry(),  # type: ignore[arg-type]
        external_switch=object(),  # type: ignore[arg-type]
        verifier=lambda _path: None,  # type: ignore[return-value]
        session_factory=lambda *_args: None,  # type: ignore[return-value]
    ).start()

    async def refresh() -> str:
        return await broker._issue_execution_worker_access_token(
            binding,
            authority,
            execution_worker=worker,
            worker_authority=execution_authority,
            timeout_seconds=60.0,
        )

    try:
        with pytest.raises(McpSupervisorError, match="^supervisor_operation_timeout$"):
            supervisor.submit_plugin_operation_sync(refresh, timeout_s=10.0)

        assert started.exists()
        assert worker.active_process_ids == ()
        assert worker.active_management_thread_count == 0
        assert vault.load_token_partition(prepared.partition_key) == before_partition
        assert vault.path.read_bytes() == before_bytes
        release.write_text("release", encoding="utf-8")
        import time

        time.sleep(0.5)
        assert not late_effect.exists()
    finally:
        try:
            supervisor.shutdown()
        finally:
            worker.close()


@pytest.mark.anyio
async def test_provider_rejection_is_worker_bounded_and_keeps_vault_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        _clock,
        _registry,
        vault,
        _protocol,
        broker,
        prepared,
        binding,
        authority,
        execution_authority,
    ) = await _expired_connection(tmp_path)
    before_partition = vault.load_token_partition(prepared.partition_key)
    before_bytes = vault.path.read_bytes()
    child_pid_path = tmp_path / "rejecting-worker-pid"
    monkeypatch.setenv("OMEGA_OAUTH_REFRESH_TEST_PID_PATH", str(child_pid_path))
    worker = _worker(rejecting_refresh_handler)
    try:
        with pytest.raises(OAuthProviderError, match="^oauth_provider_rejected$"):
            await broker._issue_execution_worker_access_token(
                binding,
                authority,
                execution_worker=worker,
                worker_authority=execution_authority,
                timeout_seconds=10.0,
            )
    finally:
        worker.close()

    assert int(child_pid_path.read_text(encoding="utf-8")) != os.getpid()
    assert vault.load_token_partition(prepared.partition_key) == before_partition
    assert vault.path.read_bytes() == before_bytes


@pytest.mark.anyio
async def test_refresh_after_restart_rotates_through_generation_authenticated_worker(
    tmp_path: Path,
) -> None:
    (
        clock,
        registry,
        _vault,
        protocol,
        _broker,
        prepared,
        binding,
        _authority,
        execution_authority,
    ) = await _expired_connection(tmp_path)
    restarted_vault = _TrackingVault(tmp_path, clock)
    restarted = OAuthBroker(
        registry=registry,
        vault=restarted_vault,
        protocol=protocol,
        clock=clock,
    )
    worker = _worker(rotating_refresh_handler)
    try:
        token = await restarted._issue_execution_worker_access_token(
            binding,
            _transport_authority(restarted, binding),
            execution_worker=worker,
            worker_authority=execution_authority,
            timeout_seconds=10.0,
        )
    finally:
        worker.close()

    partition = restarted_vault.load_token_partition(prepared.partition_key)
    assert token == "rotated-access"
    assert partition.token.access_token == "rotated-access"
    assert partition.token.refresh_token == "rotated-refresh"
    assert partition.revision == 1
    assert worker.terminal_outcomes[-1].status == "completed"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("handler", "timeout_seconds", "reason"),
    [
        (blocked_refresh_handler, 3.0, "plugin_operation_timeout"),
        (crashing_refresh_handler, 10.0, "plugin_worker_unavailable"),
    ],
)
async def test_timeout_or_crash_leaves_exact_vault_partition_unchanged(
    tmp_path: Path,
    handler: object,
    timeout_seconds: float,
    reason: str,
) -> None:
    (
        _clock,
        _registry,
        vault,
        _protocol,
        broker,
        prepared,
        binding,
        authority,
        execution_authority,
    ) = await _expired_connection(tmp_path)
    before_partition = vault.load_token_partition(prepared.partition_key)
    before_bytes = vault.path.read_bytes()
    worker = _worker(handler)
    try:
        with pytest.raises(PluginExecutionWorkerError, match=f"^{reason}$"):
            await broker._issue_execution_worker_access_token(
                binding,
                authority,
                execution_worker=worker,
                worker_authority=execution_authority,
                timeout_seconds=timeout_seconds,
            )
    finally:
        worker.close()

    assert vault.load_token_partition(prepared.partition_key) == before_partition
    assert vault.path.read_bytes() == before_bytes


@pytest.mark.anyio
async def test_stale_concurrent_worker_result_cannot_overwrite_cas_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        clock,
        registry,
        vault,
        protocol,
        stale_broker,
        prepared,
        binding,
        stale_transport_authority,
        execution_authority,
    ) = await _expired_connection(tmp_path)
    winner_vault = _TrackingVault(tmp_path, clock)
    winner_broker = OAuthBroker(
        registry=registry,
        vault=winner_vault,
        protocol=protocol,
        clock=clock,
    )
    stale_worker = _worker(stale_refresh_handler)
    winner_worker = _worker(winner_refresh_handler)
    release_path = tmp_path / "release-stale-worker"
    monkeypatch.setenv("OMEGA_OAUTH_REFRESH_TEST_RELEASE_PATH", str(release_path))
    stale_call = asyncio.create_task(
        stale_broker._issue_execution_worker_access_token(
            binding,
            stale_transport_authority,
            execution_worker=stale_worker,
            worker_authority=execution_authority,
            timeout_seconds=20.0,
        )
    )
    deadline = asyncio.get_running_loop().time() + 10.0
    while not stale_worker.active_process_ids:
        if asyncio.get_running_loop().time() >= deadline:
            pytest.fail("stale refresh worker did not start")
        await asyncio.sleep(0.01)
    try:
        winner = await winner_broker._issue_execution_worker_access_token(
            binding,
            _transport_authority(winner_broker, binding),
            execution_worker=winner_worker,
            worker_authority=execution_authority,
            timeout_seconds=10.0,
        )
        release_path.write_text("release", encoding="utf-8")
        stale = await stale_call
    finally:
        if not stale_call.done():
            stale_call.cancel()
        stale_worker.close()
        winner_worker.close()

    partition = vault.load_token_partition(prepared.partition_key)
    assert winner == "winner-access"
    assert stale == "winner-access"
    assert partition.token.access_token == "winner-access"
    assert partition.token.refresh_token == "winner-refresh"
    assert partition.revision == 1


@pytest.mark.anyio
async def test_completed_refresh_is_delivered_once_when_cancellation_arrives_late(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider rotation that already happened may not be lost to cancellation."""

    (
        _clock,
        _registry,
        vault,
        _protocol,
        broker,
        prepared,
        binding,
        authority,
        execution_authority,
    ) = await _expired_connection(tmp_path)
    before_partition = vault.load_token_partition(prepared.partition_key)
    worker = _worker(rotating_refresh_handler)
    completed = asyncio.Event()
    release = asyncio.Event()
    real_to_thread = asyncio.to_thread

    async def delayed_delivery(function: object, /, *args: object, **kwargs: object):
        # Delay only the delivery of an already-set terminal event so the
        # cancellation lands after the exact operation completed and reaped.
        result = await real_to_thread(function, *args, **kwargs)  # type: ignore[arg-type]
        completed.set()
        await release.wait()
        return result

    monkeypatch.setattr(worker_module.asyncio, "to_thread", delayed_delivery)
    operation = asyncio.create_task(
        broker._issue_execution_worker_access_token(
            binding,
            authority,
            execution_worker=worker,
            worker_authority=execution_authority,
            timeout_seconds=60.0,
        )
    )
    observed: list[object] = []
    try:
        await asyncio.wait_for(completed.wait(), timeout=60.0)
        assert worker.terminal_outcomes[-1].status == "completed"
        operation.cancel()
        try:
            observed.append(await asyncio.wait_for(operation, timeout=60.0))
        except asyncio.CancelledError:
            observed.append("discarded_as_cancelled")
    finally:
        release.set()
        if not operation.done():
            operation.cancel()
        worker.close()

    partition = vault.load_token_partition(prepared.partition_key)
    assert observed == ["rotated-access"]
    assert partition.token.access_token == "rotated-access"
    assert partition.token.refresh_token == "rotated-refresh"
    assert partition.revision == before_partition.revision + 1
    assert worker.active_process_ids == ()
    assert worker.active_management_thread_count == 0
    assert [outcome.status for outcome in worker.terminal_outcomes] == ["completed"]


def _delay_join_with_event(
    worker: PluginExecutionWorker,
    *,
    entered: threading.Event,
    release: threading.Event,
) -> None:
    """Hold the manager thread inside its post-result join with a real event.

    The result frame authenticates strictly before the join is entered, so a
    caller that waits for ``entered`` before cancelling is deterministically
    inside the narrow window this residual protects -- no sleep involved.
    """

    original_spawn = worker._spawn

    def delayed_spawn(*args: object, **kwargs: object) -> object:
        spawned = original_spawn(*args, **kwargs)
        original_join = spawned.process.join

        def delayed_join(timeout: float | None = None) -> None:
            entered.set()
            release.wait(20.0)
            return original_join(timeout)

        spawned.process.join = delayed_join
        return spawned

    worker._spawn = delayed_spawn  # type: ignore[method-assign]


def _force_one_stale_alive_check(worker: PluginExecutionWorker) -> None:
    """Force exactly one `is_alive()` observation to report a live child.

    This lands the manager thread in the same post-authentication branch a
    real deadline expiry would: the join returned, the child had not yet
    exited, but the result frame already authenticated.
    """

    original_spawn = worker._spawn
    state = {"forced": False}

    def forcing_spawn(*args: object, **kwargs: object) -> object:
        spawned = original_spawn(*args, **kwargs)
        real_is_alive = spawned.process.is_alive

        def forced_is_alive() -> bool:
            if not state["forced"]:
                state["forced"] = True
                return True
            return real_is_alive()

        spawned.process.is_alive = forced_is_alive
        return spawned

    worker._spawn = forcing_spawn  # type: ignore[method-assign]


@pytest.mark.anyio
async def test_cancellation_racing_the_post_result_join_still_delivers_completed(
    tmp_path: Path,
) -> None:
    """A cancellation landing inside the post-authentication join is absorbed.

    Between the authenticated result frame and `_record_terminal`, the
    manager joins the child.  A cancellation racing that exact window must
    not discard the already-committed provider rotation as `cancelled`.
    """

    (
        _clock,
        _registry,
        vault,
        _protocol,
        broker,
        prepared,
        binding,
        authority,
        execution_authority,
    ) = await _expired_connection(tmp_path)
    before_partition = vault.load_token_partition(prepared.partition_key)
    worker = _worker(rotating_refresh_handler)
    entered = threading.Event()
    release = threading.Event()
    _delay_join_with_event(worker, entered=entered, release=release)
    operation = asyncio.create_task(
        broker._issue_execution_worker_access_token(
            binding,
            authority,
            execution_worker=worker,
            worker_authority=execution_authority,
            timeout_seconds=60.0,
        )
    )
    try:
        reached = await asyncio.get_running_loop().run_in_executor(
            None, entered.wait, 20.0
        )
        assert reached, "the post-result join was never entered"
        operation.cancel()
        release.set()
        token = await operation
    finally:
        release.set()
        if not operation.done():
            operation.cancel()
        worker.close()

    partition = vault.load_token_partition(prepared.partition_key)
    assert token == "rotated-access"
    assert partition.token.access_token == "rotated-access"
    assert partition.token.refresh_token == "rotated-refresh"
    assert partition.revision == before_partition.revision + 1
    assert worker.active_process_ids == ()
    assert worker.active_management_thread_count == 0
    assert [outcome.status for outcome in worker.terminal_outcomes] == ["completed"]


@pytest.mark.anyio
async def test_deadline_expiry_racing_the_post_result_join_still_delivers_completed(
    tmp_path: Path,
) -> None:
    """A join-window deadline expiry must not discard an authenticated result.

    Forces exactly one still-alive observation immediately after the result
    frame authenticates -- the same branch a real deadline expiry inside the
    join would reach -- and proves the effect delivers as `completed`
    (cleanup-proven) instead of raising `plugin_operation_timeout`.
    """

    (
        _clock,
        _registry,
        vault,
        _protocol,
        broker,
        prepared,
        binding,
        authority,
        execution_authority,
    ) = await _expired_connection(tmp_path)
    before_partition = vault.load_token_partition(prepared.partition_key)
    worker = _worker(rotating_refresh_handler)
    _force_one_stale_alive_check(worker)
    try:
        token = await broker._issue_execution_worker_access_token(
            binding,
            authority,
            execution_worker=worker,
            worker_authority=execution_authority,
            timeout_seconds=60.0,
        )
    finally:
        worker.close()

    partition = vault.load_token_partition(prepared.partition_key)
    assert token == "rotated-access"
    assert partition.token.access_token == "rotated-access"
    assert partition.token.refresh_token == "rotated-refresh"
    assert partition.revision == before_partition.revision + 1
    assert worker.active_process_ids == ()
    assert worker.active_management_thread_count == 0
    assert [outcome.status for outcome in worker.terminal_outcomes] == ["completed"]
