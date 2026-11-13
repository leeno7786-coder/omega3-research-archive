"""Local MCP execution-worker re-verification and containment tests."""

from __future__ import annotations

import asyncio
import dataclasses
import os
import shutil
import sys
import time
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any
from collections.abc import Mapping

import anyio
import pytest

from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.client_contracts import McpOperationKind, McpOperationRequest
from mcp_platform.plugin_execution_worker import (
    PluginExecutionAuthority,
    PluginExecutionWorker,
    PluginExecutionWorkerError,
)
from tests.mcp_platform.generic_plugin_install import (
    install_signed_northwind_plugins,
    stage_signed_northwind_candidate,
)
from tests.mcp_platform.generic_plugin_peers import (
    CALENDAR_APP,
    CALENDAR_PLUGIN,
    LEDGER_APP,
    LEDGER_PLUGIN,
)
from tests.mcp_platform.plugin_execution_worker_fixture import handle
from tests.helpers.fs import link_or_copy


_RUNNER = handle


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


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


def _local_state(tmp_path: Path) -> dict[str, Any]:
    installed = install_signed_northwind_plugins(
        tmp_path,
        peers=("calendar", "ledger"),
    )
    package_registry = installed["package_registry"]
    for app_id in (CALENDAR_APP, LEDGER_APP):
        package_registry.set_enabled(
            app_id,
            True,
            expected_revision=package_registry.snapshot().revision,
        )
    plugin_registry = installed["plugin_registry"]
    app_registry = installed["app_registry"]
    verified_plugin = plugin_registry.reverify_installed(CALENDAR_PLUGIN)
    verified_package = package_registry.reverify_installed(CALENDAR_APP)
    from mcp_platform.plugin_connections import (
        _verified_plugin_authority,
        descriptor_for_record,
    )

    descriptor = dataclasses.replace(
        descriptor_for_record(
            app_registry.get(CALENDAR_APP),
            signed_capability_authority=_verified_plugin_authority(
                verified_plugin,
                CALENDAR_APP,
            ),
        ),
        verified_launch_sha256=verified_package.manifest.artifact_sha256,
    )
    authority = PluginExecutionAuthority(
        plugin_id=CALENDAR_PLUGIN,
        app_id=CALENDAR_APP,
        account_key_sha256="a" * 64,
        package_revision=verified_plugin.package_sha256,
        connection_revision=sha256_hex(canonical_bytes(descriptor.to_dict())),
        authority_revision=sha256_hex(
            canonical_bytes(_plain(descriptor.signed_capability_authority))
        ),
        capability_revision=descriptor.capability_revision,
        capability_kind="tool",
        capability_id=next(iter(descriptor.signed_capability_authority["tools"])),
        server_id=verified_package.manifest.server_id,
        server_version=verified_package.manifest.server_version,
    )
    package_record = package_registry.get(CALENDAR_APP)
    execution_type = __import__(
        "mcp_platform.plugin_execution_worker",
        fromlist=["_LocalMcpWorkerExecution"],
    )._LocalMcpWorkerExecution
    execution = execution_type(
        plugin_registry_root=str(plugin_registry.state_root.resolve()),
        plugin_registry_revision=plugin_registry.snapshot().revision,
        plugin_package_root=str(verified_plugin.package_root.resolve()),
        mcp_state_root=str(package_registry.state_root.resolve()),
        app_registry_revision=app_registry.snapshot().revision,
        package_registry_revision=package_registry.snapshot().revision,
        mcp_package_root=str(package_registry.package_path(package_record).resolve()),
        mcp_package_manifest_sha256=verified_package.manifest_sha256,
        executable_path=str(verified_package.artifact_path.resolve()),
        executable_sha256=verified_package.manifest.artifact_sha256,
        executable_identity=verified_package.artifact_identity,
    )
    return {
        **installed,
        "authority": authority,
        "descriptor": descriptor,
        "execution": execution,
        "verified_package": verified_package,
    }


def _reverify(state: dict[str, Any]) -> object:
    from mcp_platform.plugin_execution_worker import _reverify_local_mcp_execution

    return _reverify_local_mcp_execution(
        state["descriptor"],
        state["authority"],
        state["execution"],
        plugin_registry=state["plugin_registry"],
        app_registry=state["app_registry"],
        package_registry=state["package_registry"],
    )


def _local_request(state: dict[str, Any]) -> McpOperationRequest:
    return McpOperationRequest(
        request_id="request-local-1",
        app_id=CALENDAR_APP,
        operation=McpOperationKind.TOOL_CALL,
        capability_id=f"tool:{state['authority'].capability_id}",
        arguments={},
        capability_revision=state["descriptor"].capability_revision,
        deadline_monotonic=time.monotonic() + 10.0,
        cancellation_id="cancel-local-1",
        task_id=None,
        input_request_id=None,
        request_state=None,
    )


def test_local_envelope_rejects_path_traversal(tmp_path: Path) -> None:
    state = _local_state(tmp_path)
    execution_type = type(state["execution"])
    document = state["execution"].to_dict()
    document["mcp_package_root"] = str(
        Path(document["mcp_state_root"]) / "packages" / ".." / "escape"
    )

    with pytest.raises(ValueError, match="plugin_worker_local_execution_invalid"):
        execution_type.from_dict(document)


def test_local_envelope_seals_the_attached_worker_process_model(tmp_path: Path) -> None:
    state = _local_state(tmp_path)
    execution_type = type(state["execution"])
    document = state["execution"].to_dict()

    assert document["process_model"] == "attached-worker"
    for invalid in (None, "daemon", "setsid", "breakaway"):
        changed = dict(document)
        if invalid is None:
            changed.pop("process_model")
        else:
            changed["process_model"] = invalid
        with pytest.raises(ValueError, match="plugin_worker_local_execution_invalid"):
            execution_type.from_dict(changed)


def test_local_child_reverifies_exact_registry_package_descriptor_and_authority(
    tmp_path: Path,
) -> None:
    state = _local_state(tmp_path)

    prepared = _reverify(state)

    assert prepared.verified_package.manifest.server_id == CALENDAR_APP
    assert prepared.package_root == Path(state["execution"].mcp_package_root)
    assert dict(prepared.environment) == {
        "OMEGA_MCP_PACKAGE_ROOT": state["execution"].mcp_package_root,
        "OMEGA_MCP_SERVER_ID": CALENDAR_APP,
    }


def test_local_child_reverifies_exact_published_candidate_package(
    tmp_path: Path,
) -> None:
    from mcp_platform.plugin_connections import (
        _verified_plugin_authority,
        descriptor_for_record,
    )

    state = _local_state(tmp_path)
    candidate, result = stage_signed_northwind_candidate(
        tmp_path,
        state,
        plugin_id=CALENDAR_PLUGIN,
        peer="calendar",
    )
    published = state["plugin_registry"].reverify_candidate(
        CALENDAR_PLUGIN,
        next(
            version
            for version in result.record.versions
            if version.plugin_version == candidate.manifest.plugin_version
        ),
    )
    verified_package = state["package_registry"].reverify_installed(CALENDAR_APP)
    descriptor = dataclasses.replace(
        descriptor_for_record(
            state["app_registry"].get(CALENDAR_APP),
            signed_capability_authority=_verified_plugin_authority(
                published,
                CALENDAR_APP,
            ),
        ),
        verified_launch_sha256=verified_package.manifest.artifact_sha256,
    )
    authority = dataclasses.replace(
        state["authority"],
        package_revision=published.package_sha256,
        connection_revision="c" * 64,
        authority_revision="d" * 64,
        capability_revision=descriptor.capability_revision,
    )
    execution = dataclasses.replace(
        state["execution"],
        plugin_registry_revision=result.revision,
        plugin_package_root=str(published.package_root.resolve()),
    )

    prepared = _reverify(
        {
            **state,
            "authority": authority,
            "descriptor": descriptor,
            "execution": execution,
        }
    )

    assert prepared.verified_package.manifest.server_id == CALENDAR_APP
    assert published.package_root != state["plugin_registry"].reverify_installed(
        CALENDAR_PLUGIN
    ).package_root


def test_local_child_rejects_signed_package_substitution(tmp_path: Path) -> None:
    state = _local_state(tmp_path)
    ledger_record = state["package_registry"].get(LEDGER_APP)
    substituted = dataclasses.replace(
        state["execution"],
        mcp_package_root=str(
            state["package_registry"].package_path(ledger_record).resolve()
        ),
    )

    with pytest.raises(ValueError, match="plugin_worker_local_authority_invalid"):
        _reverify({**state, "execution": substituted})


def test_local_child_rejects_stale_registry_snapshot(tmp_path: Path) -> None:
    state = _local_state(tmp_path)
    state["plugin_registry"].set_enabled(
        LEDGER_PLUGIN,
        False,
        expected_revision=state["plugin_registry"].snapshot().revision,
    )

    with pytest.raises(ValueError, match="plugin_worker_local_authority_invalid"):
        _reverify(state)


def test_local_child_rejects_executable_mutation(tmp_path: Path) -> None:
    state = _local_state(tmp_path)
    Path(state["execution"].executable_path).write_bytes(b"substituted executable")

    with pytest.raises(ValueError, match="plugin_worker_local_authority_invalid"):
        _reverify(state)


def test_local_child_rejects_package_root_swap_in_verifier_to_guard_gap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _local_state(tmp_path)
    package_registry = state["package_registry"]
    authenticated_root = Path(state["execution"].mcp_package_root)
    replacement_root = authenticated_root.with_name(
        f"{authenticated_root.name}-replacement"
    )
    retained_root = authenticated_root.with_name(
        f"{authenticated_root.name}-authenticated"
    )
    artifact_relative = Path(state["execution"].executable_path).relative_to(
        authenticated_root
    )
    shutil.copytree(authenticated_root, replacement_root)
    replacement_artifact = replacement_root / artifact_relative
    replacement_artifact.unlink()
    link_or_copy(Path(state["execution"].executable_path), replacement_artifact)

    registry_type = type(package_registry)
    original_reverify = registry_type.reverify_installed

    def reverify_then_swap(registry: object, server_id: str) -> object:
        verified = original_reverify(registry, server_id)
        if registry is package_registry:
            os.replace(authenticated_root, retained_root)
            os.replace(replacement_root, authenticated_root)
        return verified

    monkeypatch.setattr(registry_type, "reverify_installed", reverify_then_swap)

    with pytest.raises(ValueError, match="plugin_worker_local_authority_invalid"):
        _reverify(state)


def test_local_child_rejects_cross_plugin_authority(tmp_path: Path) -> None:
    state = _local_state(tmp_path)
    ledger = state["plugin_registry"].reverify_installed(LEDGER_PLUGIN)
    crossed = dataclasses.replace(
        state["authority"],
        plugin_id=LEDGER_PLUGIN,
        package_revision=ledger.package_sha256,
    )

    with pytest.raises(ValueError, match="plugin_worker_local_authority_invalid"):
        _reverify({**state, "authority": crossed})


@pytest.mark.parametrize(
    "change",
    [
        {"publisher_id": "substituted.publisher"},
        {"verified_launch_sha256": "0" * 64},
        {"capability_revision": "1" * 64},
    ],
)
def test_local_child_rejects_descriptor_substitution(
    tmp_path: Path,
    change: dict[str, object],
) -> None:
    state = _local_state(tmp_path)
    substituted = dataclasses.replace(state["descriptor"], **change)

    with pytest.raises(ValueError, match="plugin_worker_local_authority_invalid"):
        _reverify({**state, "descriptor": substituted})


def test_local_launch_material_never_contains_pipe_credential(tmp_path: Path) -> None:
    state = _local_state(tmp_path)
    prepared = _reverify(state)
    token = "credential-only-on-authenticated-anonymous-pipe"
    launch_text = "\0".join(
        (
            str(prepared.verified_package.artifact_path),
            *prepared.verified_package.manifest.arguments,
            *(f"{key}={value}" for key, value in prepared.environment.items()),
        )
    )

    assert token not in launch_text
    assert set(prepared.environment) == {
        "OMEGA_MCP_PACKAGE_ROOT",
        "OMEGA_MCP_SERVER_ID",
    }
    assert isinstance(prepared.environment, MappingProxyType)


def test_local_reverified_launch_is_descriptor_bound_and_worker_contained(
    tmp_path: Path,
) -> None:
    from mcp_platform.transports import stdio as stdio_module

    state = _local_state(tmp_path)
    prepared = _reverify(state)

    with stdio_module.VerifiedStdioLaunchGuard._acquire_worker_contained(
        prepared.verified_package,
        package_root=prepared.package_root,
        environment=dict(prepared.environment),
        descriptor=state["descriptor"],
        expected_component_identities=prepared.component_identities,
        provider_pin=None if sys.platform == "win32" else lambda _pid: None,
    ) as guard:
        record = stdio_module._launch_record(guard)

        assert record.worker_contained is True
        assert record.process_model == "attached-worker"
        assert record.descriptor_sha256 == sha256_hex(
            canonical_bytes(state["descriptor"].to_dict())
        )
        if os.name == "nt":
            assert record.pass_fds == ()
            assert record.launch_command == str(prepared.verified_package.artifact_path)
        else:
            assert len(record.pass_fds) == 2
            assert record.launch_command.endswith(f"/{record.pass_fds[1]}")


@pytest.mark.skipif(os.name != "nt", reason="Windows retained-handle regression")
def test_windows_local_launch_rejects_substituted_root_with_same_artifact_hardlink(
    tmp_path: Path,
) -> None:
    from mcp_platform.artifact_guard import (
        ArtifactGuardError,
        _capture_artifact_identity_chain,
        descriptor_file_identity,
    )
    from mcp_platform.transports import stdio as stdio_module

    state = _local_state(tmp_path)
    prepared = _reverify(state)
    authenticated_root = prepared.package_root
    replacement_root = authenticated_root.with_name(
        f"{authenticated_root.name}-replacement"
    )
    retained_root = authenticated_root.with_name(
        f"{authenticated_root.name}-authenticated"
    )
    artifact_relative = prepared.verified_package.artifact_path.relative_to(
        authenticated_root
    )
    shutil.copytree(authenticated_root, replacement_root)
    replacement_artifact = replacement_root / artifact_relative
    replacement_artifact.unlink()
    link_or_copy(prepared.verified_package.artifact_path, replacement_artifact)

    os.replace(authenticated_root, retained_root)
    os.replace(replacement_root, authenticated_root)

    assert _capture_artifact_identity_chain(
        authenticated_root,
        prepared.verified_package.artifact_path,
    ) != prepared.component_identities
    with prepared.verified_package.artifact_path.open("rb") as artifact:
        assert descriptor_file_identity(artifact.fileno()) == (
            prepared.verified_package.artifact_identity
        )
    with pytest.raises(ArtifactGuardError, match="^artifact_identity_mismatch$"):
        stdio_module.VerifiedStdioLaunchGuard._acquire_worker_contained(
            prepared.verified_package,
            package_root=prepared.package_root,
            environment=dict(prepared.environment),
            descriptor=state["descriptor"],
            expected_component_identities=prepared.component_identities,
            provider_pin=None,
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows retained-handle regression")
def test_windows_guard_rejects_nested_directory_swap_with_same_artifact_hardlink(
    tmp_path: Path,
) -> None:
    from mcp_platform import artifact_guard as guard_module

    package_root = tmp_path / "package"
    nested = package_root / "bin"
    nested.mkdir(parents=True)
    artifact = nested / "server.exe"
    artifact.write_bytes(b"exact signed executable")
    expected_chain = guard_module._capture_artifact_identity_chain(
        package_root,
        artifact,
    )
    with artifact.open("rb") as executable:
        expected_identity = guard_module.descriptor_file_identity(executable.fileno())

    replacement = package_root / "bin-replacement"
    retained = package_root / "bin-authenticated"
    shutil.copytree(nested, replacement)
    replacement_artifact = replacement / artifact.name
    replacement_artifact.unlink()
    link_or_copy(artifact, replacement_artifact)
    os.replace(nested, retained)
    os.replace(replacement, nested)

    with pytest.raises(
        guard_module.ArtifactGuardError,
        match="^artifact_identity_mismatch$",
    ):
        guard_module.ArtifactLaunchGuard.acquire(
            package_root=package_root,
            artifact_path=artifact,
            expected_sha256=sha256_hex(artifact.read_bytes()),
            expected_identity=expected_identity,
            expected_component_identities=expected_chain,
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows component identity chain")
def test_component_identity_chain_rejects_directory_reparse(
    tmp_path: Path,
) -> None:
    from mcp_platform import artifact_guard as guard_module

    package_root = tmp_path / "package"
    real_directory = package_root / "real-bin"
    real_directory.mkdir(parents=True)
    (real_directory / "server.exe").write_bytes(b"signed executable")
    linked_directory = package_root / "bin"
    try:
        linked_directory.symlink_to(real_directory, target_is_directory=True)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"directory symlink unavailable: {error}")

    with pytest.raises(
        guard_module.ArtifactGuardError,
        match="^artifact_identity_mismatch$",
    ):
        guard_module._capture_artifact_identity_chain(
            package_root,
            linked_directory / "server.exe",
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows native identity fallback")
def test_windows_component_identity_fallback_is_exact_and_handle_derived(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_platform import artifact_guard as guard_module

    handle = guard_module._open_guarded(tmp_path, directory=True)
    try:
        information = guard_module._file_information(handle)

        def unavailable_file_id(supplied: int) -> None:
            assert supplied == handle

        monkeypatch.setattr(
            guard_module,
            "_file_id_information",
            unavailable_file_id,
        )

        identity = guard_module._component_file_identity(handle)
    finally:
        guard_module._close_handles((handle,))

    assert identity == (
        1,
        information.volume_serial_number,
        (information.file_index_high << 32) | information.file_index_low,
    )


@pytest.mark.parametrize("error_code", [1, 50, 87])
def test_windows_file_id_unsupported_errors_select_legacy_identity_only(
    monkeypatch: pytest.MonkeyPatch,
    error_code: int,
) -> None:
    from mcp_platform import artifact_guard as guard_module

    class _Function:
        def __init__(self, result: object) -> None:
            self.result = result

        def __call__(self, *_args: object) -> object:
            return self.result

    kernel = SimpleNamespace(
        GetFileInformationByHandleEx=_Function(False),
        GetLastError=_Function(error_code),
    )
    monkeypatch.setattr(guard_module, "_kernel32", lambda: kernel)

    assert guard_module._file_id_information(41) is None


def test_windows_file_id_operational_error_never_downgrades_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_platform import artifact_guard as guard_module

    class _Function:
        def __init__(self, result: object) -> None:
            self.result = result

        def __call__(self, *_args: object) -> object:
            return self.result

    kernel = SimpleNamespace(
        GetFileInformationByHandleEx=_Function(False),
        GetLastError=_Function(5),
    )
    monkeypatch.setattr(guard_module, "_kernel32", lambda: kernel)

    with pytest.raises(
        guard_module.ArtifactGuardError,
        match="^artifact_guard_unavailable$",
    ):
        guard_module._file_id_information(41)


def test_windows_file_id_reads_last_error_without_intervening_symbol_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_platform import artifact_guard as guard_module

    class _Function:
        def __init__(self, callback: object) -> None:
            self.callback = callback

        def __call__(self, *_args: object) -> object:
            return self.callback()  # type: ignore[operator]

    class _Kernel:
        def __init__(self) -> None:
            self.error = 0
            self.GetFileInformationByHandleEx = _Function(self._unsupported)

        def _unsupported(self) -> bool:
            self.error = 50
            return False

        @property
        def GetLastError(self) -> _Function:
            self.error = 5
            return _Function(lambda: self.error)

    monkeypatch.setattr(guard_module, "_kernel32", _Kernel)

    assert guard_module._file_id_information(41) is None


def test_local_dispatch_rejects_pipe_credential_before_provider_launch(
    tmp_path: Path,
) -> None:
    from mcp_platform.plugin_execution_worker import execute_mcp_operation

    state = _local_state(tmp_path)
    request = _local_request(state)

    with pytest.raises(ValueError, match="plugin_worker_payload_invalid"):
        execute_mcp_operation(
            {
                "descriptor": state["descriptor"].to_dict(),
                "request": request.to_dict(),
                "worker_authority": state["authority"].to_dict(),
                "local_execution": state["execution"].to_dict(),
            },
            {"access_token": "must-never-reach-local-provider"},
        )


def test_local_dispatch_reopens_registries_and_uses_contained_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_platform import client as client_module
    from mcp_platform import plugin_execution_worker as worker_module

    state = _local_state(tmp_path)
    observed: dict[str, object] = {}
    factory = object()
    request = _local_request(state)

    def open_registries(execution: object) -> tuple[object, object, object]:
        observed["execution"] = execution
        return (
            state["plugin_registry"],
            state["app_registry"],
            state["package_registry"],
        )

    def build_local(descriptor: object, prepared: object) -> object:
        observed["descriptor"] = descriptor
        observed["prepared"] = prepared
        return factory

    class _Result:
        def to_dict(self) -> dict[str, object]:
            return {"contained": True}

    class _Client:
        def __init__(self, descriptor: object, supplied_factory: object) -> None:
            assert descriptor == state["descriptor"]
            assert supplied_factory is factory

        async def start(self) -> object:
            observed["started"] = True
            return SimpleNamespace(
                server_id=state["authority"].server_id,
                server_version=state["authority"].server_version,
            )

        async def close(self) -> None:
            observed["closed"] = True

        async def invoke_plugin_tool(self, request: object) -> _Result:
            assert request == request_document
            return _Result()

        list_prompts = invoke_plugin_tool
        get_prompt = invoke_plugin_tool
        list_resources = invoke_plugin_tool
        list_resource_templates = invoke_plugin_tool
        read_resource = invoke_plugin_tool

    request_document = request

    monkeypatch.setattr(
        worker_module,
        "_open_local_mcp_registries",
        open_registries,
        raising=False,
    )
    monkeypatch.setattr(
        worker_module,
        "_build_local_mcp_factory",
        build_local,
        raising=False,
    )
    monkeypatch.setattr(client_module, "CurrentMcpClient", _Client)

    result = worker_module.execute_mcp_operation(
        {
            "descriptor": state["descriptor"].to_dict(),
            "request": request.to_dict(),
            "worker_authority": state["authority"].to_dict(),
            "local_execution": state["execution"].to_dict(),
        },
        {},
    )

    assert result == {"contained": True}
    assert observed["execution"] == state["execution"]
    assert observed["descriptor"] == state["descriptor"]
    assert (
        observed["prepared"].verified_package.manifest_sha256
        == state["verified_package"].manifest_sha256
    )
    assert observed["started"] is True
    assert observed["closed"] is True


def _contained_execution(tmp_path: Path) -> object:
    from mcp_platform.plugin_execution_worker import _LocalMcpWorkerExecution

    plugin_registry_root = tmp_path / "plugins"
    plugin_package_root = tmp_path / "plugin-package"
    mcp_state_root = tmp_path / "mcp"
    mcp_package_root = tmp_path / "mcp-package"
    executable = mcp_package_root / "server"
    for directory in (
        plugin_registry_root,
        plugin_package_root,
        mcp_state_root,
        mcp_package_root,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    executable.write_bytes(b"sealed executable")
    status = executable.stat()
    return _LocalMcpWorkerExecution(
        plugin_registry_root=str(plugin_registry_root.resolve()),
        plugin_registry_revision="1" * 64,
        plugin_package_root=str(plugin_package_root.resolve()),
        mcp_state_root=str(mcp_state_root.resolve()),
        app_registry_revision="2" * 64,
        package_registry_revision="3" * 64,
        mcp_package_root=str(mcp_package_root.resolve()),
        mcp_package_manifest_sha256="4" * 64,
        executable_path=str(executable.resolve()),
        executable_sha256="5" * 64,
        executable_identity=(status.st_dev, status.st_ino),
    )


def _contained_authority() -> PluginExecutionAuthority:
    return PluginExecutionAuthority(
        plugin_id="northwind.local",
        app_id="northwind.local.mcp",
        account_key_sha256="a" * 64,
        package_revision="b" * 64,
        connection_revision="c" * 64,
        authority_revision="d" * 64,
        capability_revision="e" * 64,
        capability_kind="tool",
        capability_id="echo",
    )


def test_quarantine_survives_worker_restart_and_binds_exact_executable_identity(
    tmp_path: Path,
) -> None:
    from mcp_platform import plugin_execution_worker as worker_module

    execution = _contained_execution(tmp_path)
    authority = _contained_authority()
    worker_module._quarantine_local_execution(authority, execution)

    restarted = PluginExecutionWorker(
        child_handler=_RUNNER,
        shutdown_timeout_seconds=2.0,
    )
    try:
        with pytest.raises(
            PluginExecutionWorkerError,
            match="plugin_local_process_model_incompatible",
        ):
            restarted.run_sync(
                authority=authority,
                body={
                    "action": "return",
                    "value": "must-not-run",
                    "local_execution": execution.to_dict(),
                },
                credential={},
                timeout_seconds=10.0,
            )

        changed_identity = dataclasses.replace(
            execution,
            executable_identity=(
                execution.executable_identity[0],
                execution.executable_identity[1] + 1,
            ),
        )
        result = restarted.run_sync(
            authority=authority,
            body={
                "action": "return",
                "value": "replacement",
                "local_execution": changed_identity.to_dict(),
            },
            credential={},
            timeout_seconds=10.0,
        )
    finally:
        restarted.close()

    assert result == {"value": "replacement"}
    assert restarted.terminal_outcomes[-1].status == "completed"


def test_posix_process_table_finds_attached_and_escaped_descendants() -> None:
    from mcp_platform import plugin_execution_worker as worker_module

    record = worker_module._PosixProcessRecord
    table = {
        500: record(pid=500, ppid=10, process_group=500, session=500, start_time=1),
        501: record(pid=501, ppid=500, process_group=500, session=500, start_time=2),
        502: record(pid=502, ppid=501, process_group=502, session=500, start_time=3),
        700: record(pid=700, ppid=10, process_group=700, session=700, start_time=4),
    }

    descendants = worker_module._discover_posix_descendants(500, table)

    assert set(descendants) == {501, 502}
    assert descendants[501].attached_to(500)
    assert not descendants[502].attached_to(500)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process containment contract")
def test_posix_normal_attached_tree_exits_without_quarantine(
    tmp_path: Path,
) -> None:
    started_path = tmp_path / "provider-started"
    worker = PluginExecutionWorker(
        child_handler=_RUNNER,
        shutdown_timeout_seconds=2.0,
    )
    execution = _contained_execution(tmp_path)
    try:
        result = worker.run_sync(
            authority=_contained_authority(),
            body={
                "action": "spawn_attached_tree_and_wait",
                "local_execution": execution.to_dict(),
                "started_path": str(started_path),
            },
            credential={},
            timeout_seconds=15.0,
        )
    finally:
        worker.close()

    provider_pid = int(started_path.read_text(encoding="utf-8"))
    assert result == {"attached": True}
    assert not _pid_is_alive(provider_pid)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process containment contract")
@pytest.mark.parametrize(
    "action",
    [
        "setsid_escape_then_return",
        "setpgid_breakaway_then_return",
        "double_fork_then_return",
        "inherit_pipes_then_return",
    ],
)
def test_posix_escape_is_killed_quarantined_and_never_publishes_success(
    tmp_path: Path,
    action: str,
) -> None:
    started_path = tmp_path / f"{action}-started"
    marker_path = tmp_path / f"{action}-late-marker"
    execution = _contained_execution(tmp_path)
    authority = _contained_authority()
    worker = PluginExecutionWorker(
        child_handler=_RUNNER,
        shutdown_timeout_seconds=2.0,
    )
    try:
        with pytest.raises(
            PluginExecutionWorkerError,
            match="plugin_local_process_model_incompatible",
        ):
            worker.run_sync(
                authority=authority,
                body={
                    "action": action,
                    "delay_seconds": 30.0,
                    "local_execution": execution.to_dict(),
                    "marker_path": str(marker_path),
                    "started_path": str(started_path),
                },
                credential={},
                timeout_seconds=15.0,
            )
    finally:
        worker.close()

    escaped_pid = int(started_path.read_text(encoding="utf-8"))
    assert not _pid_is_alive(escaped_pid)
    time.sleep(0.1)
    assert not marker_path.exists()
    assert worker.terminal_outcomes[-1].status == "quarantined"

    restarted = PluginExecutionWorker(
        child_handler=_RUNNER,
        shutdown_timeout_seconds=2.0,
    )
    try:
        with pytest.raises(
            PluginExecutionWorkerError,
            match="plugin_local_process_model_incompatible",
        ):
            restarted.run_sync(
                authority=authority,
                body={
                    "action": "return",
                    "value": "must-not-run",
                    "local_execution": execution.to_dict(),
                },
                credential={},
                timeout_seconds=10.0,
            )
    finally:
        restarted.close()
    assert restarted.terminal_outcomes == ()


@pytest.mark.skipif(os.name == "nt", reason="POSIX process containment contract")
def test_posix_timeout_kills_attached_tree_and_quarantines_exact_identity(
    tmp_path: Path,
) -> None:
    started_path = tmp_path / "timeout-started"
    marker_path = tmp_path / "timeout-marker"
    execution = _contained_execution(tmp_path)
    authority = _contained_authority()
    worker = PluginExecutionWorker(
        child_handler=_RUNNER,
        shutdown_timeout_seconds=2.0,
    )
    try:
        with pytest.raises(
            PluginExecutionWorkerError,
            match="plugin_local_process_model_incompatible",
        ):
            worker.run_sync(
                authority=authority,
                body={
                    "action": "attached_tree_timeout",
                    "delay_seconds": 30.0,
                    "local_execution": execution.to_dict(),
                    "marker_path": str(marker_path),
                    "started_path": str(started_path),
                },
                credential={},
                timeout_seconds=10.0,
            )
    finally:
        worker.close()

    provider_pid = int(started_path.read_text(encoding="utf-8"))
    assert not _pid_is_alive(provider_pid)
    assert not marker_path.exists()
    assert worker.terminal_outcomes[-1].status == "quarantined"


@pytest.mark.parametrize(
    ("platform", "worker_contained", "expected_new_session"),
    [
        ("linux", False, True),
        ("linux", True, False),
    ],
)
def test_posix_stdio_containment_mode_controls_process_group_inheritance(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    worker_contained: bool,
    expected_new_session: bool,
) -> None:
    from mcp_platform.transports import stdio as stdio_module

    observed: dict[str, object] = {}

    async def open_process(*args: object, **kwargs: object) -> object:
        observed["args"] = args
        observed.update(kwargs)
        return SimpleNamespace(pid=601)

    monkeypatch.setattr(stdio_module.sys, "platform", platform)
    monkeypatch.setattr(stdio_module.anyio, "open_process", open_process)
    monkeypatch.setattr(stdio_module.os, "write", lambda _fd, data: len(data))

    result = asyncio.run(
        stdio_module._create_platform_process(
            command="server",
            args=[],
            env={},
            errlog=None,
            cwd=None,
            worker_contained=worker_contained,
            pass_fds=(),
            provider_pin=(lambda _pid: None) if worker_contained else None,
        )
    )

    assert result is not None
    assert observed["start_new_session"] is expected_new_session


def test_posix_preexec_gate_opens_only_after_provider_identity_is_pinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_platform.transports import stdio as stdio_module

    events: list[object] = []

    async def open_process(*_args: object, **kwargs: object) -> object:
        events.append(("spawn", kwargs["pass_fds"][-1]))
        return SimpleNamespace(pid=601)

    def pin(pid: int) -> None:
        events.append(("pin", pid))
        assert all(event != "release" for event in events)

    def release(_fd: int, data: bytes) -> int:
        events.append("release")
        return len(data)

    monkeypatch.setattr(stdio_module.sys, "platform", "linux")
    monkeypatch.setattr(stdio_module.anyio, "open_process", open_process)
    monkeypatch.setattr(stdio_module.os, "write", release)

    result = asyncio.run(
        stdio_module._create_platform_process(
            command="/proc/self/fd/9",
            args=["--serve"],
            env={},
            errlog=None,
            cwd="/proc/self/fd/8",
            worker_contained=True,
            pass_fds=(8, 9),
            provider_pin=pin,
        )
    )

    assert result is not None
    assert events[0][0] == "spawn"
    assert events[1:] == [("pin", 601), "release"]


def test_posix_preexec_gate_never_opens_when_provider_pin_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_platform.transports import stdio as stdio_module

    events: list[str] = []

    class Process:
        pid = 601

        def kill(self) -> None:
            events.append("kill")

        async def wait(self) -> int:
            events.append("reap")
            return -9

    async def open_process(*_args: object, **_kwargs: object) -> object:
        events.append("spawn")
        return Process()

    def pin(_pid: int) -> None:
        events.append("pin-failed")
        raise PluginExecutionWorkerError("worker_protocol_invalid")

    def release(_fd: int, _data: bytes) -> int:
        events.append("release")
        return 1

    monkeypatch.setattr(stdio_module.sys, "platform", "linux")
    monkeypatch.setattr(stdio_module.anyio, "open_process", open_process)
    monkeypatch.setattr(stdio_module.os, "write", release)

    with pytest.raises(
        PluginExecutionWorkerError,
        match="worker_protocol_invalid",
    ):
        asyncio.run(
            stdio_module._create_platform_process(
                command="/proc/self/fd/9",
                args=[],
                env={},
                errlog=None,
                cwd=None,
                worker_contained=True,
                provider_pin=pin,
            )
        )

    assert events == ["spawn", "pin-failed", "kill", "reap"]


def test_darwin_worker_containment_uses_a_no_fork_exec_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_platform.transports import stdio as stdio_module

    observed: dict[str, object] = {}

    async def open_process(*args: object, **kwargs: object) -> object:
        observed["args"] = args
        observed.update(kwargs)
        return SimpleNamespace(pid=601)

    monkeypatch.setattr(stdio_module.sys, "platform", "darwin")
    monkeypatch.setattr(stdio_module.anyio, "open_process", open_process)
    monkeypatch.setattr(stdio_module.os, "write", lambda _fd, data: len(data))

    asyncio.run(
        stdio_module._create_platform_process(
            command="/dev/fd/9",
            args=["--serve"],
            env={"PLUGIN_MODE": "test"},
            errlog=None,
            cwd="/dev/fd/8",
            worker_contained=True,
            pass_fds=(8, 9),
            provider_pin=lambda _pid: None,
        )
    )

    gate_fd = observed["pass_fds"][-1]
    assert observed["args"] == (
        [
            str(stdio_module.portable_python_path(platform="darwin")),
            "-I",
            "-S",
            "-c",
            stdio_module._POSIX_ATTACHED_EXEC,
            str(gate_fd),
            "/dev/fd/9",
            "--serve",
        ],
    )
    assert observed["env"] == {"PLUGIN_MODE": "test"}
    assert observed["cwd"] == "/dev/fd/8"
    assert observed["pass_fds"] == (8, 9, gate_fd)
    assert observed["start_new_session"] is False


@pytest.mark.skipif(sys.platform != "darwin", reason="real macOS no-fork boundary")
def test_darwin_no_fork_bootstrap_blocks_an_immediate_double_fork(
    tmp_path: Path,
) -> None:
    from mcp_platform.transports import stdio as stdio_module

    marker = tmp_path / "escaped-marker"
    blocked = tmp_path / "fork-blocked"
    provider = (
        "import os,pathlib,time; "
        "marker=pathlib.Path(os.environ['OMEGA_TEST_ESCAPE_MARKER']); "
        "blocked=pathlib.Path(os.environ['OMEGA_TEST_FORK_BLOCKED']); "
        "\ntry: first=os.fork()\n"
        "except OSError: blocked.write_text('blocked', encoding='utf-8')\n"
        "else:\n"
        " if first == 0:\n"
        "  os.setsid(); second=os.fork()\n"
        "  if second == 0: time.sleep(0.1); marker.write_text('escaped', encoding='utf-8')\n"
        "  os._exit(0)\n"
        " os.waitpid(first, 0)\n"
    )

    async def run() -> int:
        process = await stdio_module._create_platform_process(
            command=sys.executable,
            args=["-I", "-S", "-c", provider],
            env={
                "OMEGA_TEST_ESCAPE_MARKER": str(marker),
                "OMEGA_TEST_FORK_BLOCKED": str(blocked),
            },
            errlog=None,
            cwd=None,
            worker_contained=True,
            provider_pin=lambda _pid: None,
        )
        with anyio.fail_after(5.0):
            return await process.wait()

    assert asyncio.run(run()) == 0
    time.sleep(0.2)
    assert blocked.read_text(encoding="utf-8") == "blocked"
    assert not marker.exists()


def test_posix_contained_shutdown_never_terminates_the_worker_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_platform.transports import stdio as stdio_module

    calls: list[str] = []

    class _Process:
        pid = 41
        returncode: int | None = None

        def terminate(self) -> None:
            calls.append("terminate-provider")
            self.returncode = 0

        def kill(self) -> None:
            calls.append("kill-provider")
            self.returncode = -9

    async def terminate_group(*_args: object) -> None:
        raise AssertionError("contained provider shutdown targeted the worker group")

    monkeypatch.setattr(stdio_module.sys, "platform", "linux")
    monkeypatch.setattr(stdio_module, "Process", _Process)
    monkeypatch.setattr(stdio_module, "terminate_posix_process_tree", terminate_group)

    asyncio.run(
        stdio_module._terminate_process_tree(
            _Process(),
            worker_contained=True,
        )
    )

    assert calls == ["terminate-provider"]


def test_windows_stdio_worker_containment_never_requests_breakaway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_platform.transports import stdio as stdio_module

    observed: dict[str, object] = {}

    async def create(
        command: str,
        args: list[str],
        env: dict[str, str],
        errlog: object,
        cwd: object,
    ) -> object:
        observed.update(
            command=command,
            args=args,
            env=env,
            errlog=errlog,
            cwd=cwd,
        )
        return object()

    monkeypatch.setattr(stdio_module.sys, "platform", "win32")
    monkeypatch.setattr(stdio_module, "create_windows_process", create)

    result = asyncio.run(
        stdio_module._create_platform_process(
            command="server.exe",
            args=["--stdio"],
            env={},
            errlog=None,
            cwd=None,
            worker_contained=True,
            pass_fds=(),
            provider_pin=None,
        )
    )

    assert result is not None
    assert observed == {
        "command": "server.exe",
        "args": ["--stdio"],
        "env": {},
        "errlog": None,
        "cwd": None,
    }
