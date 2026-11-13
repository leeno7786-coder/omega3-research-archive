from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcp_platform.plugin_lifecycle import PluginLifecycleService
from mcp_platform.plugin_authority import PluginExecutionPolicy
from mcp_platform.plugin_connections import ConnectionStatus
from mcp_platform.plugin_state import PluginLifecycleState


def _patch_intake(monkeypatch: pytest.MonkeyPatch, calls: list[object]) -> None:
    def _intake(*args: object, **kwargs: object) -> None:
        calls.append((args, kwargs))

    import memory.api as memory_api

    monkeypatch.setattr(memory_api, "intake_memory", _intake)
    monkeypatch.setattr("memory.api.intake_memory", _intake, raising=False)
    for name, module in list(sys.modules.items()):
        if name == "mcp_platform" or name.startswith("mcp_platform."):
            if hasattr(module, "intake_memory"):
                monkeypatch.setattr(module, "intake_memory", _intake)


def test_lifecycle_connect_does_not_intake_provider_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []
    _patch_intake(monkeypatch, calls)
    service = PluginLifecycleService(state_root=tmp_path)
    review = service.review("google.workspace.drive")
    service.install("google.workspace.drive", confirmation=review.sha256)
    service._offered_provider_scopes(
        "google.workspace.drive",
        "google.workspace.drive.mcp",
    )
    assert calls == []
    service.close()


def test_drive_scopes_match_exact_sealed_app_authority(tmp_path: Path) -> None:
    service = PluginLifecycleService(state_root=tmp_path)
    review = service.review("google.workspace.drive")
    service.install("google.workspace.drive", confirmation=review.sha256)
    scopes = service._offered_provider_scopes(
        "google.workspace.drive",
        "google.workspace.drive.mcp",
    )
    assert scopes == (
        "https://www.googleapis.com/auth/drive.file",
        "https://www.googleapis.com/auth/drive.readonly",
    )
    source = (
        Path(__file__).resolve().parents[2] / "mcp_platform" / "plugin_connections.py"
    ).read_text(encoding="utf-8")
    assert "class PluginConnectionService" in source
    module_prefix = source.split("class PluginConnectionService", 1)[0]
    assert "_DEFAULT_SCOPES" not in module_prefix or "drive.file" not in module_prefix
    service.close()


@pytest.mark.anyio
async def test_complete_connection_callback_rejects_zero_bindings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMEGA_OPERATOR_ACCOUNT_KEY", "account-1")
    service = PluginLifecycleService(state_root=tmp_path)
    review = service.review("google.workspace.drive")
    service.install("google.workspace.drive", confirmation=review.sha256)

    async def _complete_callback(callback_uri: str) -> ConnectionStatus:
        return ConnectionStatus(
            plugin_id="google.workspace.drive",
            app_id="google.workspace.drive.mcp",
            account_key_sha256="a" * 64,
            status="connected",
            authorization_context_revision="b" * 64,
            token_revision=0,
            provider_scopes=(),
            execution_policy=PluginExecutionPolicy.CONFIRM_EACH,
            authority_revision="c" * 64,
        )

    async def _invalidate(_plugin_id: str, **_kwargs: object) -> None:
        return None

    service._connection_service = type(
        "_ConnectionService",
        (),
        {
            "complete_callback": staticmethod(_complete_callback),
            "invalidate_execution": staticmethod(_invalidate),
        },
    )()

    async def _aggregate(_plugin_id: str):
        return SimpleNamespace(state=PluginLifecycleState.CONNECTED)

    monkeypatch.setattr(service, "status", _aggregate)
    monkeypatch.setattr(
        "mcp_platform.plugin_catalog.compile_plugin_runtime_catalog",
        lambda **kwargs: SimpleNamespace(entries=()),
    )
    with pytest.raises(ValueError, match="plugin_catalog_authority_missing"):
        await service.complete_connection("https://omega2ai.com/oauth/callback?code=x")
    assert service.plugin_registry.get("google.workspace.drive").enabled is False
    service.close()


def _authorized_status() -> ConnectionStatus:
    return ConnectionStatus(
        plugin_id="google.workspace.drive",
        app_id="google.workspace.drive.mcp",
        account_key_sha256="a" * 64,
        status="connected",
        authorization_context_revision="b" * 64,
        token_revision=0,
        provider_scopes=(),
        execution_policy=PluginExecutionPolicy.CONFIRM_EACH,
        authority_revision="c" * 64,
    )


@pytest.mark.anyio
async def test_complete_device_connection_enables_after_resource_only_compilation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMEGA_OPERATOR_ACCOUNT_KEY", "account-1")
    service = PluginLifecycleService(state_root=tmp_path)
    review = service.review("google.workspace.drive")
    service.install("google.workspace.drive", confirmation=review.sha256)

    invalidated = False

    async def _complete_device(request_id: str) -> ConnectionStatus:
        assert request_id == "a" * 64
        return _authorized_status()

    async def _invalidate(_plugin_id: str, **_kwargs: object) -> None:
        nonlocal invalidated
        invalidated = True

    service._connection_service = type(
        "_ConnectionService",
        (),
        {
            "complete_device": staticmethod(_complete_device),
            "invalidate_execution": staticmethod(_invalidate),
        },
    )()

    async def _aggregate(_plugin_id: str):
        return SimpleNamespace(state=PluginLifecycleState.CONNECTED)

    monkeypatch.setattr(service, "status", _aggregate)
    monkeypatch.setattr(
        "mcp_platform.plugin_catalog.compile_plugin_runtime_catalog",
        lambda **kwargs: SimpleNamespace(
            entries=(
                SimpleNamespace(
                    plugin_id="google.workspace.drive",
                    capability_kind="resource",
                ),
            )
        ),
    )

    status = await service.poll_connection("a" * 64)

    assert status.state is PluginLifecycleState.CONNECTED
    assert invalidated is False
    assert service.plugin_registry.get("google.workspace.drive").enabled is True
    service.close()


@pytest.mark.anyio
async def test_device_authorization_finalizing_does_not_enable_or_compile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMEGA_OPERATOR_ACCOUNT_KEY", "account-1")
    service = PluginLifecycleService(state_root=tmp_path)
    review = service.review("google.workspace.drive")
    service.install("google.workspace.drive", confirmation=review.sha256)

    async def _complete_device(_request_id: str) -> ConnectionStatus:
        return replace(_authorized_status(), status="finalizing")

    service._connection_service = type(
        "_ConnectionService",
        (),
        {"complete_device": staticmethod(_complete_device)},
    )()
    monkeypatch.setattr(
        service,
        "_enable_if_compiled",
        lambda _plugin_id: pytest.fail("finalizing authorization compiled too early"),
    )

    status = await service.poll_connection("a" * 64)

    assert status.status == "finalizing"
    assert service.plugin_registry.get("google.workspace.drive").enabled is False
    service.close()


def test_ensure_plugin_ready_does_not_enable_when_compile_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMEGA_OPERATOR_ACCOUNT_KEY", "account-1")
    service = PluginLifecycleService(state_root=tmp_path)
    review = service.review("google.workspace.drive")
    service.install("google.workspace.drive", confirmation=review.sha256)

    invalidated = False

    async def _status(plugin_id: str, *, app_id: str, account_key: str) -> ConnectionStatus:
        del plugin_id, app_id, account_key
        return replace(
            _authorized_status(),
            status="installed_disconnected" if invalidated else "connected",
        )

    async def _invalidate(_plugin_id: str, **_kwargs: object) -> None:
        nonlocal invalidated
        invalidated = True

    service._connection_service = type(
        "_ConnectionService",
        (),
        {
            "status": staticmethod(_status),
            "invalidate_execution": staticmethod(_invalidate),
        },
    )()
    service._operation_submitter = lambda operation, **_kwargs: asyncio.run(operation())
    monkeypatch.setattr(
        "mcp_platform.plugin_catalog.compile_plugin_runtime_catalog",
        lambda **kwargs: SimpleNamespace(entries=()),
    )
    assert service.ensure_plugin_ready("google.workspace.drive") is False
    assert invalidated is True
    assert service.plugin_registry.get("google.workspace.drive").enabled is False
    service.close()


def test_connection_and_lifecycle_paths_have_no_private_event_loop() -> None:
    root = Path(__file__).resolve().parents[2] / "mcp_platform"
    source = "\n".join(
        (root / name).read_text(encoding="utf-8")
        for name in ("plugin_connections.py", "plugin_lifecycle.py")
    )
    for forbidden in ("asyncio.run", "new_event_loop", "_runner"):
        assert forbidden not in source
