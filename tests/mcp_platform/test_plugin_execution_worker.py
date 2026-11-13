from __future__ import annotations

import json
import os
import struct
import threading
import time

import pytest

import mcp_platform.authorization as mcp_authorization
from mcp_platform.canonical import canonical_bytes
from mcp_platform.plugin_execution_worker import (
    PluginExecutionAuthority,
    PluginExecutionWorker,
    PluginExecutionWorkerError,
    _WORKER_DIAGNOSIS_RE,
    _classify_result_body,
    _decode_authenticated_frame,
    _encode_authenticated_frame,
    _validate_mcp_worker_operation,
    _worker_failure_diagnosis,
)
from mcp_platform.client_contracts import McpOperationKind, McpOperationRequest
from tests.mcp_platform.plugin_execution_worker_fixture import handle


_REVISION = "a" * 64
_RUNNER = handle


def test_worker_authorization_seam_is_not_a_public_api() -> None:
    assert "McpWorkerAuthorizationConfig" not in mcp_authorization.__all__
    assert not hasattr(mcp_authorization, "McpWorkerAuthorizationConfig")


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
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == 259
    finally:
        kernel32.CloseHandle(handle)


def _authority(*, connection_revision: str = _REVISION) -> PluginExecutionAuthority:
    return PluginExecutionAuthority(
        plugin_id="northwind.docs",
        app_id="northwind.docs.mcp",
        account_key_sha256="b" * 64,
        package_revision="c" * 64,
        connection_revision=connection_revision,
        authority_revision="d" * 64,
        capability_revision="e" * 64,
        capability_kind="tool",
        capability_id="search",
    )


def _worker(**kwargs) -> PluginExecutionWorker:
    return PluginExecutionWorker(
        child_handler=_RUNNER,
        shutdown_timeout_seconds=1.0,
        **kwargs,
    )


def test_worker_round_trip_uses_authenticated_anonymous_pipes() -> None:
    worker = _worker()
    try:
        result = worker.run_sync(
            authority=_authority(),
            body={"action": "return", "value": "ok"},
            credential={"access_token": "memory-only-token"},
            timeout_seconds=10.0,
        )
    finally:
        worker.close()

    assert result == {"value": "ok"}
    assert worker.terminal_outcomes[-1].status == "completed"


def test_frame_rejects_bad_mac_nonce_generation_and_sequence_substitution() -> None:
    secret = b"s" * 32
    frame = _encode_authenticated_frame(
        secret=secret,
        kind="execute",
        nonce="f" * 64,
        generation=7,
        sequence=2,
        body={"operation": "tool"},
        max_frame_bytes=4096,
    )

    for changed in (
        frame[:-1] + bytes([frame[-1] ^ 1]),
        _encode_authenticated_frame(
            secret=secret,
            kind="execute",
            nonce="0" * 64,
            generation=7,
            sequence=2,
            body={"operation": "tool"},
            max_frame_bytes=4096,
        ),
        _encode_authenticated_frame(
            secret=secret,
            kind="execute",
            nonce="f" * 64,
            generation=8,
            sequence=2,
            body={"operation": "tool"},
            max_frame_bytes=4096,
        ),
    ):
        with pytest.raises(PluginExecutionWorkerError, match="worker_protocol_invalid"):
            _decode_authenticated_frame(
                changed,
                secret=secret,
                expected_kind="execute",
                expected_nonce="f" * 64,
                expected_generation=7,
                expected_sequence=2,
                max_frame_bytes=4096,
            )


def test_frame_rejects_malformed_noncanonical_and_oversize_payloads() -> None:
    secret = b"s" * 32
    malformed = struct.pack(">I", 20) + b"{}"
    noncanonical_payload = json.dumps(
        {
            "v": 1,
            "kind": "execute",
            "nonce": "n" * 64,
            "generation": 1,
            "seq": 1,
            "body": {},
            "mac": "0" * 64,
        },
        indent=2,
    ).encode("utf-8")
    noncanonical = struct.pack(">I", len(noncanonical_payload)) + noncanonical_payload
    oversize = struct.pack(">I", 4097) + b"x" * 4097

    for frame in (b"", malformed, noncanonical, oversize):
        with pytest.raises(PluginExecutionWorkerError, match="worker_protocol_invalid"):
            _decode_authenticated_frame(
                frame,
                secret=secret,
                expected_kind="execute",
                expected_nonce="f" * 64,
                expected_generation=1,
                expected_sequence=1,
                max_frame_bytes=4096,
            )


def test_timeout_tree_kills_worker_and_grandchild_before_return(tmp_path) -> None:
    marker = tmp_path / "grandchild-marker"
    started_marker = tmp_path / "worker-started"
    worker = _worker()
    started = time.monotonic()
    try:
        with pytest.raises(PluginExecutionWorkerError, match="plugin_operation_timeout"):
            worker.run_sync(
                authority=_authority(),
                body={
                    "action": "spawn_tree",
                    "delay_seconds": 30.0,
                    "marker_path": str(marker),
                    "started_path": str(started_marker),
                },
                credential={"access_token": "memory-only-token"},
                timeout_seconds=12.0,
            )
        elapsed = time.monotonic() - started
    finally:
        worker.close()

    assert started_marker.exists()
    parent_pid, grandchild_pid = (
        int(value) for value in started_marker.read_text(encoding="utf-8").split(":")
    )
    assert elapsed < 14.0
    assert not _pid_is_alive(parent_pid)
    assert not _pid_is_alive(grandchild_pid)
    assert not marker.exists()
    assert worker.active_process_ids == ()
    assert worker.terminal_outcomes[-1].status == "timeout"


def test_timeout_suppresses_late_result_and_immediate_successor_runs(tmp_path) -> None:
    marker = tmp_path / "late-marker"
    started_marker = tmp_path / "late-worker-started"
    worker = _worker()
    try:
        with pytest.raises(PluginExecutionWorkerError, match="plugin_operation_timeout"):
            worker.run_sync(
                authority=_authority(),
                body={
                    "action": "late_marker",
                    "delay_seconds": 30.0,
                    "marker_path": str(marker),
                    "started_path": str(started_marker),
                },
                credential={"access_token": "one"},
                timeout_seconds=12.0,
            )
        result = worker.run_sync(
            authority=_authority(),
            body={"action": "return", "value": "successor"},
            credential={"access_token": "two"},
            timeout_seconds=10.0,
        )
    finally:
        worker.close()

    assert result == {"value": "successor"}
    assert started_marker.exists()
    assert not _pid_is_alive(int(started_marker.read_text(encoding="utf-8")))
    assert not marker.exists()
    assert [item.generation for item in worker.terminal_outcomes] == [1, 3]


def test_close_tree_kills_active_worker_boundedly(tmp_path) -> None:
    marker = tmp_path / "close-marker"
    started_marker = tmp_path / "close-worker-started"
    worker = _worker()
    errors: list[BaseException] = []

    def invoke() -> None:
        try:
            worker.run_sync(
                authority=_authority(),
                body={
                    "action": "late_marker",
                    "delay_seconds": 30.0,
                    "marker_path": str(marker),
                    "started_path": str(started_marker),
                },
                credential={"access_token": "memory-only-token"},
                timeout_seconds=30.0,
            )
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=invoke)
    thread.start()
    deadline = time.monotonic() + 2.0
    while not started_marker.exists() and time.monotonic() < deadline + 8.0:
        time.sleep(0.01)
    started = time.monotonic()
    worker.close()
    elapsed = time.monotonic() - started
    thread.join(timeout=2.0)

    assert elapsed < 1.5
    assert started_marker.exists()
    assert not _pid_is_alive(int(started_marker.read_text(encoding="utf-8")))
    assert not thread.is_alive()
    assert errors and str(errors[0]) == "plugin_runtime_closed"
    assert not marker.exists()
    assert worker.active_process_ids == ()


def test_dead_worker_and_spawn_failure_fail_closed(monkeypatch) -> None:
    worker = _worker()
    try:
        with pytest.raises(PluginExecutionWorkerError, match="plugin_worker_unavailable"):
            worker.run_sync(
                authority=_authority(),
                body={"action": "crash"},
                credential={"access_token": "memory-only-token"},
                timeout_seconds=10.0,
            )
    finally:
        worker.close()

    spawn_failure = _worker()
    monkeypatch.setattr(spawn_failure, "_spawn", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError()))
    try:
        with pytest.raises(PluginExecutionWorkerError, match="plugin_worker_unavailable"):
            spawn_failure.run_sync(
                authority=_authority(),
                body={"action": "return", "value": "never"},
                credential={"access_token": "memory-only-token"},
                timeout_seconds=1.0,
            )
    finally:
        spawn_failure.close()


def test_stale_generation_result_is_rejected() -> None:
    secret = b"s" * 32
    stale = _encode_authenticated_frame(
        secret=secret,
        kind="result",
        nonce="f" * 64,
        generation=4,
        sequence=3,
        body={"status": "completed", "data": {}},
        max_frame_bytes=4096,
    )

    with pytest.raises(PluginExecutionWorkerError, match="worker_protocol_invalid"):
        _decode_authenticated_frame(
            stale,
            secret=secret,
            expected_kind="result",
            expected_nonce="f" * 64,
            expected_generation=5,
            expected_sequence=3,
            max_frame_bytes=4096,
        )


def test_credential_is_not_in_worker_argv_or_environment() -> None:
    worker = _worker()
    try:
        result = worker.run_sync(
            authority=_authority(),
            body={"action": "credential_location"},
            credential={"access_token": "credential-only-on-anonymous-pipe"},
            timeout_seconds=10.0,
        )
    finally:
        worker.close()

    assert result == {"argv": False, "environment": False}


def test_authority_rejects_unknown_kind_and_noncanonical_revisions() -> None:
    values = _authority().__dict__ if hasattr(_authority(), "__dict__") else {
        "plugin_id": "northwind.docs",
        "app_id": "northwind.docs.mcp",
        "account_key_sha256": "b" * 64,
        "package_revision": "c" * 64,
        "connection_revision": _REVISION,
        "authority_revision": "d" * 64,
        "capability_revision": "e" * 64,
        "capability_kind": "tool",
        "capability_id": "search",
    }
    with pytest.raises(ValueError, match="plugin_worker_authority_invalid"):
        PluginExecutionAuthority(**{**values, "capability_kind": "mystery"})
    with pytest.raises(ValueError, match="plugin_worker_authority_invalid"):
        PluginExecutionAuthority(**{**values, "connection_revision": "stale"})


def test_frame_encoder_emits_exact_canonical_payload() -> None:
    frame = _encode_authenticated_frame(
        secret=b"s" * 32,
        kind="ready",
        nonce="f" * 64,
        generation=1,
        sequence=1,
        body={},
        max_frame_bytes=4096,
    )
    size = struct.unpack(">I", frame[:4])[0]
    payload = frame[4:]
    assert size == len(payload)
    assert payload == canonical_bytes(json.loads(payload))


@pytest.mark.parametrize(
    ("kind", "operation", "wire_id"),
    [
        ("tool", McpOperationKind.TOOL_CALL, "tool:search"),
        ("prompt", McpOperationKind.PROMPTS_LIST, "prompts/list"),
        ("prompt", McpOperationKind.PROMPT_GET, "prompt:search"),
        ("resource", McpOperationKind.RESOURCES_LIST, "resources/list"),
        ("resource", McpOperationKind.RESOURCE_READ, "resource:search"),
        (
            "resource_template",
            McpOperationKind.RESOURCE_TEMPLATES_LIST,
            "resources/templates/list",
        ),
        ("resource_template", McpOperationKind.RESOURCE_READ, "resource:search"),
    ],
)
def test_child_accepts_only_closed_kind_operation_and_wire_identity(
    kind: str,
    operation: McpOperationKind,
    wire_id: str,
) -> None:
    authority = PluginExecutionAuthority(
        plugin_id="northwind.docs",
        app_id="northwind.docs.mcp",
        account_key_sha256="b" * 64,
        package_revision="c" * 64,
        connection_revision="a" * 64,
        authority_revision="d" * 64,
        capability_revision="e" * 64,
        capability_kind=kind,
        capability_id="search",
    )
    request = McpOperationRequest(
        request_id="request-1",
        app_id=authority.app_id,
        operation=operation,
        capability_id=wire_id,
        arguments={},
        capability_revision=authority.capability_revision,
        deadline_monotonic=time.monotonic() + 10,
        cancellation_id="cancel-1",
        task_id=None,
        input_request_id=None,
        request_state=None,
    )

    _validate_mcp_worker_operation(authority, request)
    with pytest.raises(ValueError, match="plugin_worker_authority_invalid"):
        _validate_mcp_worker_operation(
            authority,
            McpOperationRequest(
                request_id="request-2",
                app_id=authority.app_id,
                operation=operation,
                capability_id="tool:wrong",
                arguments={},
                capability_revision=authority.capability_revision,
                deadline_monotonic=time.monotonic() + 10,
                cancellation_id="cancel-2",
                task_id=None,
                input_request_id=None,
                request_state=None,
            ),
        )


def _diagnosis_of(raise_it) -> str | None:
    try:
        raise_it()
    except BaseException as exc:  # noqa: BLE001 - the diagnosis is the subject
        return _worker_failure_diagnosis(exc)
    raise AssertionError("expected a failure")


def test_worker_diagnosis_names_the_caller_of_a_shared_raise_helper() -> None:
    """A helper every check funnels through cannot say which check failed.

    `capabilities._fail` is one line, so the innermost frame reads identically
    for capability drift and an invalid result. The frame that CALLED it is the
    check that actually failed, so both are named.
    """

    from mcp_platform.client_contracts import McpClientError, McpClientErrorReason

    def _fail(reason: McpClientErrorReason) -> None:
        raise McpClientError(reason)

    def a_specific_check() -> None:
        _fail(McpClientErrorReason.CAPABILITY_DRIFT)

    diagnosis = _diagnosis_of(a_specific_check)
    assert diagnosis is not None
    assert "McpClientError at" in diagnosis
    assert "in _fail" in diagnosis
    assert "in a_specific_check" in diagnosis
    # The reason is a member of a closed Omega enum, so it may be named.
    assert diagnosis.endswith(" reason=capability_drift")


def test_worker_diagnosis_never_carries_a_message_omega_did_not_author() -> None:
    """`str(exc)` is the one part a provider could have written."""

    secret = "Invalid access token for account alice@example.com"

    class ProviderRejected(Exception):
        pass

    def refused() -> None:
        raise ProviderRejected(secret)

    diagnosis = _diagnosis_of(refused)
    assert diagnosis is not None
    assert "ProviderRejected at" in diagnosis
    assert secret not in diagnosis
    assert "alice" not in diagnosis
    assert "reason=" not in diagnosis


def test_worker_diagnosis_is_rejected_unless_it_is_omega_shaped() -> None:
    """What the jailed child says is validated on arrival, never trusted."""

    assert _WORKER_DIAGNOSIS_RE.match(
        "McpClientError at capabilities.py:896 in _fail"
    )
    assert _WORKER_DIAGNOSIS_RE.match(
        "McpClientError at capabilities.py:896 in _fail"
        " <- capabilities.py:640 in _validate reason=result_invalid"
    )
    for smuggled in (
        "Invalid access token",
        "ValueError at capabilities.py:896 in _fail; the account is alice@example.com",
        "ValueError at ../../etc/passwd:1 in x",
        "ValueError at capabilities.py:896 in _fail reason=Provider Said This",
    ):
        assert not _WORKER_DIAGNOSIS_RE.match(smuggled), smuggled


def test_failed_result_body_carries_a_diagnosis_without_changing_the_reason() -> None:
    """The diagnosis rides beside the reason; the reason itself is unchanged."""

    plain = _classify_result_body({"status": "failed", "reason": "plugin_operation_failed"})
    assert plain == (None, "plugin_operation_failed")

    explained = _classify_result_body(
        {
            "status": "failed",
            "reason": "plugin_operation_failed",
            "diagnosis": "McpClientError at capabilities.py:896 in _fail",
        }
    )
    assert explained == (None, "plugin_operation_failed")

    # A completed result still admits no extra field.
    with pytest.raises(PluginExecutionWorkerError):
        _classify_result_body(
            {"status": "completed", "data": {}, "diagnosis": "X at a.py:1 in b"}
        )
    with pytest.raises(PluginExecutionWorkerError):
        _classify_result_body({"status": "failed", "diagnosis": "X at a.py:1 in b"})
