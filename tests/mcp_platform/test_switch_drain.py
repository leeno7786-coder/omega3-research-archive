from __future__ import annotations

import asyncio
import dataclasses
import json
import time
import threading
from pathlib import Path

import anyio
import pytest

import mcp_platform.authority as authority_module
from mcp_platform import action_ledger
from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.package_contracts import NetworkClass, RiskClass, ToolCatalog
from mcp_platform.registry import InstalledPackageRecord
from tests.mcp_platform.test_authority import (
    Harness,
    _admission,
    _replace_proposal,
    _tool_document,
)


def test_disable_switches_off_before_rejecting_new_external_calls(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    expected = harness.switch.load().revision

    drained = anyio.run(harness.gateway.disable_external, expected)
    outcome = anyio.run(harness.gateway.execute, harness.proposal)

    assert harness.switch.load().enabled is False
    assert outcome.failed_check == "external_mcp_eligibility"
    assert harness.supervisor.call_count == 0
    assert drained.sessions_closed == 1


def test_external_call_completed_during_drain_finishes_normally(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.supervisor.block_call = True
    expected = harness.switch.load().revision
    results: dict[str, object] = {}

    async def scenario() -> None:
        async with anyio.create_task_group() as tasks:
            async def execute() -> None:
                results["call"] = await harness.gateway.execute(harness.proposal)

            async def disable() -> None:
                results["drain"] = await harness.gateway.disable_external(expected)

            tasks.start_soon(execute)
            await anyio.to_thread.run_sync(harness.supervisor.call_entered.wait)
            tasks.start_soon(disable)
            await anyio.sleep(0.05)
            assert harness.switch.load().enabled is False
            harness.supervisor.release_call.set()

    anyio.run(scenario)

    call = results["call"]
    drain = results["drain"]
    assert call.status == "confirmed"  # type: ignore[union-attr]
    assert drain.drained_calls == 1  # type: ignore[union-attr]
    assert drain.uncertain_writes == 0  # type: ignore[union-attr]


def test_provider_response_stops_drain_tracking_while_local_ingress_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(authority_module, "EXTERNAL_CALL_DRAIN_TIMEOUT_S", 0.05)
    harness = Harness(tmp_path, RiskClass.MUTATING_WRITE)
    ingress_entered = threading.Event()
    release_ingress = threading.Event()
    result: dict[str, object] = {}

    def admit(context, raw_result):
        ingress_entered.set()
        assert release_ingress.wait(2.0)
        return _admission(context, raw_result)

    harness.gateway._ingress_admitter = admit

    def execute() -> None:
        result["call"] = anyio.run(harness.gateway.execute, harness.proposal)

    worker = threading.Thread(target=execute)
    worker.start()
    assert ingress_entered.wait(2.0)
    try:
        result["drain"] = anyio.run(
            harness.gateway.disable_external, harness.switch.load().revision
        )
    finally:
        release_ingress.set()
        worker.join(2.0)

    call = result["call"]
    drain = result["drain"]
    assert call.status == "confirmed"  # type: ignore[union-attr]
    assert drain.drained_calls == 0  # type: ignore[union-attr]
    assert drain.uncertain_writes == 0  # type: ignore[union-attr]


def test_switch_deadline_during_atomic_dispatch_is_definitive_without_provider_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(authority_module, "EXTERNAL_CALL_DRAIN_TIMEOUT_S", 0.05)
    harness = Harness(tmp_path, RiskClass.MUTATING_WRITE)
    dispatch_entered = threading.Event()
    release_dispatch = threading.Event()
    real_find_or_record = action_ledger.find_or_record_dispatch
    results: dict[str, object] = {}

    def blocked_find_or_record(**kwargs):
        dispatch_entered.set()
        assert release_dispatch.wait(2.0)
        return real_find_or_record(**kwargs)

    monkeypatch.setattr(
        authority_module.action_ledger,
        "find_or_record_dispatch",
        blocked_find_or_record,
    )

    def execute() -> None:
        results["call"] = anyio.run(harness.gateway.execute, harness.proposal)

    worker = threading.Thread(target=execute)
    worker.start()
    assert dispatch_entered.wait(2.0)
    try:
        results["drain"] = anyio.run(
            harness.gateway.disable_external, harness.switch.load().revision
        )
    finally:
        release_dispatch.set()
        worker.join(2.0)

    assert not worker.is_alive()
    call = results["call"]
    drain = results["drain"]
    record = action_ledger.find_by_idempotency(
        harness.proposal.idempotency_key, path=harness.ledger_path
    )
    assert harness.supervisor.call_count == 0
    assert call.status == "failed"  # type: ignore[union-attr]
    assert call.uncertain is False  # type: ignore[union-attr]
    assert record is not None and record["state"] == "failed"
    assert drain.drained_calls == 1  # type: ignore[union-attr]
    assert drain.uncertain_writes == 0  # type: ignore[union-attr]


def test_caller_cancellation_keeps_shielded_provider_owned_until_reconciled(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    provider_started = anyio.Event()
    provider_finished = threading.Event()
    release_provider = threading.Event()
    disable_finished = anyio.Event()
    scope_ready = anyio.Event()
    scope_box: dict[str, anyio.CancelScope] = {}
    caught: list[BaseException] = []
    cancellation_types: list[type[BaseException]] = []
    provider_tasks: list[asyncio.Task] = []
    results: dict[str, object] = {}

    async def shielded_call(*_args, **_kwargs):
        async def provider_call():
            provider_started.set()
            await anyio.to_thread.run_sync(release_provider.wait)
            provider_finished.set()
            return harness.supervisor.result

        provider_task = asyncio.create_task(provider_call())
        provider_tasks.append(provider_task)
        return await asyncio.shield(provider_task)

    harness.supervisor.call_tool = shielded_call  # type: ignore[method-assign]

    async def scenario() -> None:
        cancellation_types.append(anyio.get_cancelled_exc_class())

        async def execute() -> None:
            with anyio.CancelScope() as scope:
                scope_box["scope"] = scope
                scope_ready.set()
                try:
                    await harness.gateway.execute(harness.proposal)
                except BaseException as exc:
                    caught.append(exc)

        async def disable() -> None:
            results["drain"] = await harness.gateway.disable_external(
                harness.switch.load().revision
            )
            disable_finished.set()

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(execute)
            await scope_ready.wait()
            await provider_started.wait()
            scope_box["scope"].cancel()
            tasks.start_soon(disable)
            await anyio.sleep(0.05)
            results["disable_finished_before_provider"] = disable_finished.is_set()
            release_provider.set()

    try:
        anyio.run(scenario)
    finally:
        release_provider.set()

    record = action_ledger.find_by_idempotency(
        harness.proposal.idempotency_key, path=harness.ledger_path
    )
    assert len(caught) == 1
    assert type(caught[0]) is cancellation_types[0]
    assert provider_tasks and all(task.done() for task in provider_tasks)
    assert (
        results["disable_finished_before_provider"],
        provider_finished.is_set(),
        harness.ingress_calls,
        None if record is None else record["state"],
        results["drain"].drained_calls,  # type: ignore[union-attr]
    ) == (False, True, 1, "confirmed", 1)


@pytest.mark.parametrize(
    ("risk", "expected_state", "uncertain_writes", "failed_reads"),
    [
        (RiskClass.MUTATING_WRITE, "outcome_unknown", 1, 0),
        (RiskClass.READ_ONLY, "failed", 0, 1),
    ],
)
def test_drain_deadline_closes_unresolved_actions_without_provider_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    risk: RiskClass,
    expected_state: str,
    uncertain_writes: int,
    failed_reads: int,
) -> None:
    monkeypatch.setattr(authority_module, "EXTERNAL_CALL_DRAIN_TIMEOUT_S", 0.05)
    harness = Harness(tmp_path, risk)
    harness.supervisor.block_call = True
    expected = harness.switch.load().revision
    results: dict[str, object] = {}
    owner_failure_calls = 0
    real_record_failure = action_ledger.record_failure

    def counted_record_failure(*args, **kwargs):
        nonlocal owner_failure_calls
        owner_failure_calls += 1
        return real_record_failure(*args, **kwargs)

    monkeypatch.setattr(
        authority_module.action_ledger,
        "record_failure",
        counted_record_failure,
    )

    async def scenario() -> None:
        async with anyio.create_task_group() as tasks:
            async def execute() -> None:
                results["call"] = await harness.gateway.execute(harness.proposal)

            tasks.start_soon(execute)
            await anyio.to_thread.run_sync(harness.supervisor.call_entered.wait)
            started = time.monotonic()
            results["drain"] = await harness.gateway.disable_external(expected)
            results["elapsed"] = time.monotonic() - started
            results["record_at_return"] = action_ledger.find_by_idempotency(
                harness.proposal.idempotency_key,
                path=harness.ledger_path,
            )
            results["call_finished_at_return"] = "call" in results
            harness.supervisor.release_call.set()

    anyio.run(scenario)

    drain = results["drain"]
    call = results["call"]
    record_at_return = results["record_at_return"]
    record = action_ledger.find_by_idempotency(
        harness.proposal.idempotency_key, path=harness.ledger_path
    )
    assert 0.04 <= results["elapsed"] < 0.20  # type: ignore[operator]
    assert results["call_finished_at_return"] is False
    assert record_at_return is not None and record_at_return["state"] == expected_state
    assert record is not None and record["state"] == expected_state
    assert call.status == expected_state  # type: ignore[union-attr]
    assert drain.uncertain_writes == uncertain_writes  # type: ignore[union-attr]
    assert drain.failed_reads == failed_reads  # type: ignore[union-attr]
    assert owner_failure_calls == 0
    assert "provider-content-sentinel" not in repr(call)


def test_disable_does_not_gate_or_track_local_only_operation(tmp_path: Path) -> None:
    harness = Harness(tmp_path, network_class=NetworkClass.LOCAL_ONLY)
    current = harness.switch.load()
    harness.switch.set_enabled(False, expected_revision=current.revision)
    expected = harness.switch.load().revision

    drain = anyio.run(harness.gateway.disable_external, expected)
    outcome = anyio.run(harness.gateway.execute, harness.proposal)

    assert outcome.status == "confirmed"
    assert drain.drained_calls == 0
    assert drain.uncertain_writes == 0
    assert harness.supervisor.call_count == 1


def test_concurrent_duplicate_disable_is_idempotent(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    expected = harness.switch.load().revision
    outcomes = []

    async def scenario() -> None:
        async with anyio.create_task_group() as tasks:
            async def disable() -> None:
                outcomes.append(await harness.gateway.disable_external(expected))

            tasks.start_soon(disable)
            tasks.start_soon(disable)

    anyio.run(scenario)

    assert len(outcomes) == 2
    assert outcomes[0] == outcomes[1]
    assert len(harness.supervisor.stop_external_calls) == 1


def test_disable_result_is_exact_content_free_counts(tmp_path: Path) -> None:
    harness = Harness(tmp_path)

    outcome = anyio.run(
        harness.gateway.disable_external, harness.switch.load().revision
    )

    assert outcome.to_dict() == {
        "drained_calls": 0,
        "uncertain_writes": 0,
        "failed_reads": 0,
        "sessions_closed": 1,
        "sessions_forced": 0,
        "errors": 0,
    }


def test_session_cleanup_thread_start_failure_is_content_free_and_untracked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness(tmp_path)
    real_start = threading.Thread.start

    def start(worker: threading.Thread) -> None:
        if worker.name == "mcp-external-session-cleanup":
            raise RuntimeError("provider-thread-start-content-sentinel")
        real_start(worker)

    monkeypatch.setattr(threading.Thread, "start", start)

    outcome = anyio.run(
        harness.gateway.disable_external, harness.switch.load().revision
    )

    assert outcome.errors == 1
    assert harness.gateway._cleanup_threads == set()
    assert "sentinel" not in repr(outcome)


def test_drain_reports_ledger_cleanup_failure_without_leaking_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(authority_module, "EXTERNAL_CALL_DRAIN_TIMEOUT_S", 0.05)
    harness = Harness(tmp_path, RiskClass.MUTATING_WRITE)
    harness.supervisor.block_call = True
    expected = harness.switch.load().revision
    result: dict[str, object] = {}
    real_batch = action_ledger.record_switch_deadline_batch

    def incomplete_batch(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(
        authority_module.action_ledger,
        "record_switch_deadline_batch",
        incomplete_batch,
    )
    harness.gateway._stop_external_within = lambda _timeout: {  # type: ignore[method-assign]
        "sessions_closed": 0,
        "sessions_forced": 0,
        "errors": 0,
    }

    async def scenario() -> None:
        async with anyio.create_task_group() as tasks:
            async def execute() -> None:
                result["call"] = await harness.gateway.execute(harness.proposal)

            tasks.start_soon(execute)
            await anyio.to_thread.run_sync(harness.supervisor.call_entered.wait)
            result["drain"] = await harness.gateway.disable_external(expected)
            result["record_at_return"] = action_ledger.find_by_idempotency(
                harness.proposal.idempotency_key,
                path=harness.ledger_path,
            )
            harness.supervisor.release_call.set()

    anyio.run(scenario)

    drain = result["drain"]
    call = result["call"]
    record_at_return = result["record_at_return"]
    record_before_recovery = action_ledger.find_by_idempotency(
        harness.proposal.idempotency_key,
        path=harness.ledger_path,
    )
    with harness.gateway._active_condition:
        pending_before_recovery = len(harness.gateway._active_external)
    monkeypatch.setattr(
        authority_module.action_ledger,
        "record_switch_deadline_batch",
        real_batch,
    )
    cleanup_recovered = harness.gateway.wait_for_cleanup_sync(0.5)
    record = action_ledger.find_by_idempotency(
        harness.proposal.idempotency_key,
        path=harness.ledger_path,
    )
    assert drain.uncertain_writes == 0  # type: ignore[union-attr]
    assert drain.failed_reads == 0  # type: ignore[union-attr]
    assert drain.errors == 1  # type: ignore[union-attr]
    assert record_at_return is not None and record_at_return["state"] == "dispatched"
    assert call.status == "outcome_unknown"  # type: ignore[union-attr]
    assert record_before_recovery is not None
    assert record_before_recovery["state"] == "dispatched"
    assert pending_before_recovery == 1
    assert cleanup_recovered is True
    assert record is not None and record["state"] == "outcome_unknown"
    with harness.gateway._active_condition:
        assert harness.gateway._active_external == {}
    assert "sentinel" not in repr((drain, call))


def test_failed_deadline_and_owner_closures_retain_reconciliation_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(authority_module, "EXTERNAL_CALL_DRAIN_TIMEOUT_S", 0.05)
    harness = Harness(tmp_path, RiskClass.MUTATING_WRITE)
    harness.supervisor.block_call = True
    results: dict[str, object] = {}
    call_finished = threading.Event()
    storage_held = threading.Event()
    release_storage = threading.Event()

    def execute() -> None:
        try:
            results["call"] = anyio.run(harness.gateway.execute, harness.proposal)
        finally:
            call_finished.set()

    def hold_storage() -> None:
        with action_ledger._lock:
            storage_held.set()
            assert release_storage.wait(2.0)

    caller = threading.Thread(target=execute)
    holder = threading.Thread(target=hold_storage)
    caller.start()
    assert harness.supervisor.call_entered.wait(2.0)
    holder.start()
    assert storage_held.wait(2.0)
    try:
        results["first_disable"] = anyio.run(
            harness.gateway.disable_external,
            harness.switch.load().revision,
        )
        raw_at_return = json.loads(harness.ledger_path.read_text(encoding="utf-8"))
        results["state_at_return"] = next(iter(raw_at_return.values()))["state"]
        harness.supervisor.release_call.set()
        results["caller_returned_while_unavailable"] = call_finished.wait(0.20)
        with harness.gateway._active_condition:
            results["pending_while_unavailable"] = len(
                harness.gateway._active_external
            )
    finally:
        release_storage.set()
        holder.join(2.0)
        caller.join(2.0)

    results["cleanup_recovered"] = harness.gateway.wait_for_cleanup_sync(0.5)
    record = action_ledger.find_by_idempotency(
        harness.proposal.idempotency_key,
        path=harness.ledger_path,
    )
    first_disable = results["first_disable"]
    call = results["call"]
    assert not holder.is_alive()
    assert not caller.is_alive()
    assert results["state_at_return"] == "dispatched"
    assert first_disable.uncertain_writes == 0  # type: ignore[union-attr]
    assert first_disable.failed_reads == 0  # type: ignore[union-attr]
    assert first_disable.errors == 1  # type: ignore[union-attr]
    assert results["caller_returned_while_unavailable"] is True
    assert results["pending_while_unavailable"] == 1
    assert call.status == "outcome_unknown"  # type: ignore[union-attr]
    assert results["cleanup_recovered"] is True
    assert record is not None and record["state"] == "outcome_unknown"
    assert harness.supervisor.call_count == 1
    with harness.gateway._active_condition:
        assert harness.gateway._active_external == {}


@pytest.mark.parametrize(
    ("risk", "expected_state", "uncertain_writes", "failed_reads"),
    [
        (RiskClass.MUTATING_WRITE, "outcome_unknown", 1, 0),
        (RiskClass.READ_ONLY, "failed", 0, 1),
    ],
)
def test_cleanup_recovery_refreshes_the_exact_cached_disable_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    risk: RiskClass,
    expected_state: str,
    uncertain_writes: int,
    failed_reads: int,
) -> None:
    monkeypatch.setattr(authority_module, "EXTERNAL_CALL_DRAIN_TIMEOUT_S", 0.05)
    harness = Harness(tmp_path, risk)
    harness.supervisor.block_call = True
    expected_revision = harness.switch.load().revision
    real_batch = action_ledger.record_switch_deadline_batch

    monkeypatch.setattr(
        authority_module.action_ledger,
        "record_switch_deadline_batch",
        lambda *_args, **_kwargs: {},
    )

    caller = threading.Thread(
        target=lambda: anyio.run(harness.gateway.execute, harness.proposal)
    )
    caller.start()
    assert harness.supervisor.call_entered.wait(2.0)
    first = anyio.run(harness.gateway.disable_external, expected_revision)
    harness.supervisor.release_call.set()
    caller.join(2.0)
    assert not caller.is_alive()

    monkeypatch.setattr(
        authority_module.action_ledger,
        "record_switch_deadline_batch",
        real_batch,
    )
    assert harness.gateway.wait_for_cleanup_sync(0.5) is True
    second = anyio.run(harness.gateway.disable_external, expected_revision)
    record = action_ledger.find_by_idempotency(
        harness.proposal.idempotency_key,
        path=harness.ledger_path,
    )

    assert first.to_dict() == {
        "drained_calls": 0,
        "uncertain_writes": 0,
        "failed_reads": 0,
        "sessions_closed": 1,
        "sessions_forced": 0,
        "errors": 1,
    }
    assert second.to_dict() == {
        "drained_calls": 0,
        "uncertain_writes": uncertain_writes,
        "failed_reads": failed_reads,
        "sessions_closed": 1,
        "sessions_forced": 0,
        "errors": 0,
    }
    assert record is not None and record["state"] == expected_state
    assert harness.supervisor.call_count == 1
    with harness.gateway._active_condition:
        assert harness.gateway._active_external == {}


def test_owner_recovery_before_disable_result_does_not_leave_a_tracker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(authority_module, "EXTERNAL_CALL_DRAIN_TIMEOUT_S", 0.05)
    harness = Harness(tmp_path, RiskClass.MUTATING_WRITE)
    harness.supervisor.block_call = True
    expected_revision = harness.switch.load().revision
    real_batch = action_ledger.record_switch_deadline_batch
    session_cleanup_entered = threading.Event()
    release_session_cleanup = threading.Event()
    disable_results: list[object] = []

    monkeypatch.setattr(
        authority_module.action_ledger,
        "record_switch_deadline_batch",
        lambda *_args, **_kwargs: {},
    )

    def stop_external(_timeout: float) -> dict[str, int]:
        session_cleanup_entered.set()
        assert release_session_cleanup.wait(2.0)
        return {"sessions_closed": 1, "sessions_forced": 0, "errors": 0}

    harness.gateway._stop_external_within = stop_external  # type: ignore[method-assign]
    caller = threading.Thread(
        target=lambda: anyio.run(harness.gateway.execute, harness.proposal)
    )
    caller.start()
    assert harness.supervisor.call_entered.wait(2.0)
    disabler = threading.Thread(
        target=lambda: disable_results.append(
            anyio.run(harness.gateway.disable_external, expected_revision)
        )
    )
    disabler.start()
    assert session_cleanup_entered.wait(2.0)

    monkeypatch.setattr(
        authority_module.action_ledger,
        "record_switch_deadline_batch",
        real_batch,
    )
    harness.supervisor.release_call.set()
    caller.join(2.0)
    assert not caller.is_alive()
    release_session_cleanup.set()
    disabler.join(2.0)

    assert not disabler.is_alive()
    assert len(disable_results) == 1
    assert disable_results[0].uncertain_writes == 1  # type: ignore[union-attr]
    assert disable_results[0].errors == 0  # type: ignore[union-attr]
    assert harness.gateway.wait_for_cleanup_sync(0.5) is True
    with harness.gateway._active_condition:
        assert harness.gateway._active_external == {}


def test_concurrent_cached_retry_preserves_independent_session_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(authority_module, "EXTERNAL_CALL_DRAIN_TIMEOUT_S", 0.05)
    harness = Harness(tmp_path, RiskClass.MUTATING_WRITE)
    harness.supervisor.block_call = True
    expected_revision = harness.switch.load().revision
    real_batch = action_ledger.record_switch_deadline_batch
    stop_calls = 0

    monkeypatch.setattr(
        authority_module.action_ledger,
        "record_switch_deadline_batch",
        lambda *_args, **_kwargs: {},
    )

    def stop_external(_timeout: float) -> dict[str, int]:
        nonlocal stop_calls
        stop_calls += 1
        return {"sessions_closed": 2, "sessions_forced": 1, "errors": 2}

    harness.gateway._stop_external_within = stop_external  # type: ignore[method-assign]
    caller = threading.Thread(
        target=lambda: anyio.run(harness.gateway.execute, harness.proposal)
    )
    caller.start()
    assert harness.supervisor.call_entered.wait(2.0)
    first = anyio.run(harness.gateway.disable_external, expected_revision)
    harness.supervisor.release_call.set()
    caller.join(2.0)
    assert not caller.is_alive()

    monkeypatch.setattr(
        authority_module.action_ledger,
        "record_switch_deadline_batch",
        real_batch,
    )
    assert harness.gateway.wait_for_cleanup_sync(0.5) is True
    monkeypatch.setattr(
        harness.switch,
        "set_enabled",
        lambda *_args, **_kwargs: pytest.fail("cached retry repeated switch CAS"),
    )
    retries: list[object] = []
    workers = [
        threading.Thread(
            target=lambda: retries.append(
                anyio.run(harness.gateway.disable_external, expected_revision)
            )
        )
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(2.0)

    assert all(not worker.is_alive() for worker in workers)
    assert first.errors == 3
    assert len(retries) == 2
    assert retries[0] is retries[1]
    assert retries[0].to_dict() == {  # type: ignore[union-attr]
        "drained_calls": 0,
        "uncertain_writes": 1,
        "failed_reads": 0,
        "sessions_closed": 2,
        "sessions_forced": 1,
        "errors": 2,
    }
    assert stop_calls == 1
    assert harness.supervisor.call_count == 1


def test_mixed_cleanup_recovery_refreshes_each_exact_classification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(authority_module, "EXTERNAL_CALL_DRAIN_TIMEOUT_S", 0.05)
    harness = Harness(tmp_path, RiskClass.MUTATING_WRITE)
    read_document = _tool_document(RiskClass.READ_ONLY)
    read_document["name"] = "omega.fake.read"
    catalog = ToolCatalog.from_dict(
        {
            "catalog_version": 1,
            "server_id": harness.package.manifest.server_id,
            "server_version": harness.package.manifest.server_version,
            "tools": [harness.tool.to_dict(), read_document],
        }
    )
    catalog_digest = sha256_hex(canonical_bytes(catalog.to_dict()))
    package = dataclasses.replace(
        harness.package,
        manifest=dataclasses.replace(
            harness.package.manifest,
            tool_catalog_sha256=catalog_digest,
            risk_declarations=(RiskClass.MUTATING_WRITE, RiskClass.READ_ONLY),
        ),
        tool_catalog=catalog,
    )
    harness.package = package
    harness.registry.package = package
    harness.registry.record = dataclasses.replace(
        InstalledPackageRecord.from_verified(package), enabled=True
    )
    harness.supervisor.package = package
    read_tool = catalog.tools[1]
    harness.connection = dataclasses.replace(
        harness.connection,
        allowed_tool_schemas={
            harness.tool.name: harness.tool.input_schema_sha256,
            read_tool.name: read_tool.input_schema_sha256,
        },
    )
    read_authority = dataclasses.replace(
        harness.authority,
        authority_id="authority-read",
        authority_revision="c" * 64,
        tool_name=read_tool.name,
        tool_schema_sha256=read_tool.input_schema_sha256,
        risk_classes=(RiskClass.READ_ONLY.value,),
        arguments_sha256=sha256_hex(canonical_bytes({"query": "inbox"})),
    )
    authorities = {
        harness.authority.authority_id: harness.authority,
        read_authority.authority_id: read_authority,
    }
    harness.gateway = harness.make_gateway(
        authority_resolver=lambda _kind, identity: authorities[identity]
    )
    expected_revision = harness.switch.load().revision
    read_proposal = _replace_proposal(
        harness.proposal,
        request_id="request-read-recovery",
        tool_name=read_tool.name,
        tool_schema_sha256=read_tool.input_schema_sha256,
        arguments={"query": "inbox"},
        expected_provider_revisions={},
        authority_id=read_authority.authority_id,
        authority_revision=read_authority.authority_revision,
        idempotency_key="idempotency-read-recovery",
    )
    proposals = (harness.proposal, read_proposal)
    both_entered = threading.Event()
    real_batch = action_ledger.record_switch_deadline_batch

    async def blocked_call(_server_id, _name, arguments):
        harness.supervisor.call_count += 1
        harness.supervisor.arguments.append(arguments)
        if harness.supervisor.call_count == 2:
            both_entered.set()
        await anyio.to_thread.run_sync(harness.supervisor.release_call.wait)
        return harness.supervisor.result

    harness.supervisor.call_tool = blocked_call  # type: ignore[method-assign]
    monkeypatch.setattr(
        authority_module.action_ledger,
        "record_switch_deadline_batch",
        lambda *_args, **_kwargs: {},
    )
    callers = [
        threading.Thread(
            target=lambda proposal=proposal: anyio.run(
                harness.gateway.execute, proposal
            )
        )
        for proposal in proposals
    ]
    for caller in callers:
        caller.start()
    assert both_entered.wait(2.0)
    first = anyio.run(
        harness.gateway.disable_external,
        expected_revision,
    )
    harness.supervisor.release_call.set()
    for caller in callers:
        caller.join(2.0)
    assert all(not caller.is_alive() for caller in callers)

    monkeypatch.setattr(
        authority_module.action_ledger,
        "record_switch_deadline_batch",
        real_batch,
    )
    assert harness.gateway.wait_for_cleanup_sync(0.5) is True
    recovered = anyio.run(
        harness.gateway.disable_external,
        expected_revision,
    )
    states = {
        action_ledger.find_by_idempotency(
            proposal.idempotency_key,
            path=harness.ledger_path,
        )["state"]
        for proposal in proposals
    }

    assert first.errors == 1
    assert first.uncertain_writes == 0
    assert first.failed_reads == 0
    assert recovered.errors == 0
    assert recovered.uncertain_writes == 1
    assert recovered.failed_reads == 1
    assert states == {"outcome_unknown", "failed"}
    assert harness.supervisor.call_count == 2
    with harness.gateway._active_condition:
        assert harness.gateway._active_external == {}


def test_concurrent_duplicate_disable_retries_one_pending_action_without_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(authority_module, "EXTERNAL_CALL_DRAIN_TIMEOUT_S", 0.05)
    harness = Harness(tmp_path, RiskClass.MUTATING_WRITE)
    harness.supervisor.block_call = True
    expected = harness.switch.load().revision
    call_results: list[object] = []
    call_finished = threading.Event()
    storage_held = threading.Event()
    release_storage = threading.Event()

    def execute() -> None:
        try:
            call_results.append(anyio.run(harness.gateway.execute, harness.proposal))
        finally:
            call_finished.set()

    def hold_storage() -> None:
        with action_ledger._lock:
            storage_held.set()
            assert release_storage.wait(2.0)

    caller = threading.Thread(target=execute)
    holder = threading.Thread(target=hold_storage)
    caller.start()
    assert harness.supervisor.call_entered.wait(2.0)
    holder.start()
    assert storage_held.wait(2.0)
    first = anyio.run(harness.gateway.disable_external, expected)
    harness.supervisor.release_call.set()
    assert call_finished.wait(0.20)
    release_storage.set()
    holder.join(2.0)
    caller.join(2.0)

    retries: list[object] = []
    retry_threads = [
        threading.Thread(
            target=lambda: retries.append(
                anyio.run(harness.gateway.disable_external, expected)
            )
        )
        for _ in range(2)
    ]
    for retry in retry_threads:
        retry.start()
    for retry in retry_threads:
        retry.join(2.0)

    record = action_ledger.find_by_idempotency(
        harness.proposal.idempotency_key,
        path=harness.ledger_path,
    )
    assert not holder.is_alive()
    assert not caller.is_alive()
    assert all(not retry.is_alive() for retry in retry_threads)
    assert first.errors == 1
    assert first.uncertain_writes == 0
    assert len(retries) == 2
    assert all(retry.errors == 0 for retry in retries)  # type: ignore[union-attr]
    assert all(retry.uncertain_writes == 1 for retry in retries)  # type: ignore[union-attr]
    assert record is not None and record["state"] == "outcome_unknown"
    assert harness.supervisor.call_count == 1
    assert len(call_results) == 1
    assert call_results[0].status == "outcome_unknown"  # type: ignore[union-attr]
    with harness.gateway._active_condition:
        assert harness.gateway._active_external == {}


def test_disable_uses_one_deadline_for_n_slow_ledger_cleanups(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(authority_module, "EXTERNAL_CALL_DRAIN_TIMEOUT_S", 0.05)
    harness = Harness(tmp_path, RiskClass.MUTATING_WRITE)
    harness.supervisor.block_call = True
    authorities = {
        f"authority-slow-ledger-{index}": dataclasses.replace(
            harness.authority,
            authority_id=f"authority-slow-ledger-{index}",
            authority_revision=sha256_hex(canonical_bytes({"authority": index})),
        )
        for index in range(3)
    }
    proposals = [
        _replace_proposal(
            harness.proposal,
            request_id=f"request-slow-ledger-{index}",
            idempotency_key=f"idempotency-slow-ledger-{index}",
            authority_id=authority.authority_id,
            authority_revision=authority.authority_revision,
        )
        for index, authority in enumerate(authorities.values())
    ]
    harness.gateway = harness.make_gateway(
        authority_resolver=lambda _kind, identity: authorities[identity]
    )
    provider_condition = threading.Condition()
    provider_count = 0
    call_results: list[object] = []
    real_call = harness.supervisor.call_tool
    real_record_failure = action_ledger.record_failure

    async def counted_call(*args, **kwargs):
        nonlocal provider_count
        with provider_condition:
            provider_count += 1
            provider_condition.notify_all()
        return await real_call(*args, **kwargs)

    def slow_record_failure(*args, **kwargs):
        time.sleep(0.15)
        return real_record_failure(*args, **kwargs)

    harness.supervisor.call_tool = counted_call  # type: ignore[method-assign]
    monkeypatch.setattr(
        authority_module.action_ledger,
        "record_failure",
        slow_record_failure,
    )

    workers = [
        threading.Thread(
            target=lambda proposal=proposal: call_results.append(
                anyio.run(harness.gateway.execute, proposal)
            )
        )
        for proposal in proposals
    ]
    for worker in workers:
        worker.start()
    with provider_condition:
        assert provider_condition.wait_for(lambda: provider_count == 3, timeout=2.0)

    try:
        started = time.monotonic()
        drain = anyio.run(
            harness.gateway.disable_external, harness.switch.load().revision
        )
        elapsed = time.monotonic() - started
    finally:
        harness.supervisor.release_call.set()
        for worker in workers:
            worker.join(2.0)

    records = [
        action_ledger.find_by_idempotency(
            proposal.idempotency_key, path=harness.ledger_path
        )
        for proposal in proposals
    ]
    assert 0.04 <= elapsed < 0.20
    assert drain.drained_calls + drain.uncertain_writes + drain.failed_reads == 3
    assert drain.uncertain_writes == 3
    assert len(call_results) == 3
    assert all(result.status == "outcome_unknown" for result in call_results)  # type: ignore[union-attr]
    assert all(record is not None and record["state"] == "outcome_unknown" for record in records)
    assert "provider-content-sentinel" not in repr((drain, call_results))


def test_disable_bounds_slow_session_cleanup_by_the_same_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(authority_module, "EXTERNAL_CALL_DRAIN_TIMEOUT_S", 0.05)
    harness = Harness(tmp_path, RiskClass.MUTATING_WRITE)
    harness.supervisor.block_call = True
    result: dict[str, object] = {}
    real_stop = harness.supervisor.stop_external_sync
    stop_finished = threading.Event()

    def slow_stop(remaining: float) -> dict[str, int]:
        try:
            time.sleep(0.30)
            return real_stop(remaining)
        finally:
            stop_finished.set()

    harness.supervisor.stop_external_sync = slow_stop  # type: ignore[method-assign]

    def execute() -> None:
        result["call"] = anyio.run(harness.gateway.execute, harness.proposal)

    worker = threading.Thread(target=execute)
    worker.start()
    assert harness.supervisor.call_entered.wait(2.0)
    try:
        started = time.monotonic()
        result["drain"] = anyio.run(
            harness.gateway.disable_external, harness.switch.load().revision
        )
        result["elapsed"] = time.monotonic() - started
    finally:
        harness.supervisor.release_call.set()
        worker.join(2.0)

    call = result["call"]
    drain = result["drain"]
    record = action_ledger.find_by_idempotency(
        harness.proposal.idempotency_key, path=harness.ledger_path
    )
    assert 0.04 <= result["elapsed"] < 0.20  # type: ignore[operator]
    assert drain.drained_calls + drain.uncertain_writes + drain.failed_reads == 1  # type: ignore[union-attr]
    assert call.status == "outcome_unknown"  # type: ignore[union-attr]
    assert record is not None and record["state"] == "outcome_unknown"
    assert stop_finished.wait(1.0)
    assert "provider-content-sentinel" not in repr((drain, call))
