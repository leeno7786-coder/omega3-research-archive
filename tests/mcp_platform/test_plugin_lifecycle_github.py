from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mcp_platform.plugin_lifecycle import PluginLifecycleService
from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.client import _enforce_signed_tool_result
from mcp_platform.plugin_catalog import compile_plugin_tool_bindings


GITHUB = "github.official"
GITHUB_APP = "github.official.mcp"
DRIVE = "google.workspace.drive"
_OFFICIAL_SCHEMAS_PATH = Path(__file__).with_name("github_official_tool_schemas.json")


def _plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(item) for item in value]
    return value


def _install(service: PluginLifecycleService, plugin_id: str) -> None:
    review = service.review(plugin_id)
    service.install(plugin_id, confirmation=review.sha256)


def _enable(service: PluginLifecycleService, plugin_id: str) -> None:
    service.plugin_registry.set_enabled(
        plugin_id,
        True,
        expected_revision=service.plugin_registry.snapshot().revision,
    )


def test_lifecycle_catalog_includes_github(tmp_path: Path) -> None:
    service = PluginLifecycleService(state_root=tmp_path)
    by_id = {
        item.plugin_id: item for item in service.catalog_source().snapshot().plugins
    }
    gh = by_id["github.official"]
    assert gh.remote_origin == "https://api.githubcopilot.com/mcp/"
    assert gh.remote_origin.endswith("/mcp/")
    assert "/x/all" not in gh.remote_origin
    assert gh.required_app_ids == (GITHUB_APP,)
    assert "google.workspace.drive" in by_id
    service.close()


def test_github_contract_matches_official_live_input_schemas(tmp_path: Path) -> None:
    official_schemas = json.loads(_OFFICIAL_SCHEMAS_PATH.read_text(encoding="utf-8"))
    expected = {
        f"tool:{name}": sha256_hex(canonical_bytes(schema))
        for name, schema in official_schemas.items()
    }
    service = PluginLifecycleService(state_root=tmp_path)
    _install(service, GITHUB)

    descriptor = service.descriptor_for_app(GITHUB_APP)

    assert descriptor.declared_capabilities == expected
    assert descriptor.signed_capability_authority is not None
    assert {
        name: canonical_bytes(_plain_json(authority["input_schema"]))
        for name, authority in descriptor.signed_capability_authority["tools"].items()
    } == {name: canonical_bytes(schema) for name, schema in official_schemas.items()}
    service.close()


def test_github_policy_accepts_official_marshaled_text_result(tmp_path: Path) -> None:
    service = PluginLifecycleService(state_root=tmp_path)
    _install(service, GITHUB)
    descriptor = service.descriptor_for_app(GITHUB_APP)
    authority = descriptor.signed_capability_authority
    assert authority is not None
    policy = authority["tools"]["get_me"]
    normalized = {
        "content": [{"type": "text", "text": '{"login":"octocat"}'}],
        "structured_content": None,
        "is_error": False,
    }

    assert _enforce_signed_tool_result(normalized, policy) == normalized
    service.close()


def test_installed_github_compiles_planner_bindings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMEGA_OPERATOR_ACCOUNT_KEY", "account-1")
    service = PluginLifecycleService(state_root=tmp_path)
    review = service.review("github.official")
    service.install("github.official", confirmation=review.sha256)
    service.plugin_registry.set_enabled(
        "github.official",
        True,
        expected_revision=service.plugin_registry.snapshot().revision,
    )
    bindings = [
        item
        for item in compile_plugin_tool_bindings(
            plugin_snapshot=service.plugin_registry.snapshot(),
            app_snapshot=service.app_registry.snapshot(),
            descriptor_for_app=service.descriptor_for_plugin_app,
            account_key_hash=sha256(b"account-1").hexdigest(),
        )
        if item.plugin_id == "github.official"
    ]
    assert bindings
    assert {item.capability for item in bindings} >= {
        "get_me",
        "search_repositories",
        "get_file_contents",
        "list_issues",
        "issue_read",
        "issue_write",
    }
    service.close()


@pytest.mark.anyio
async def test_github_disconnect_does_not_revoke_google(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMEGA_OPERATOR_ACCOUNT_KEY", "account-1")
    service = PluginLifecycleService(state_root=tmp_path)
    for plugin_id in (DRIVE, GITHUB):
        _install(service, plugin_id)
        _enable(service, plugin_id)
    revoked: list[tuple[str, str, str]] = []

    async def _revoke(
        plugin_id: str,
        *,
        app_id: str,
        account_key: str,
    ) -> None:
        revoked.append((plugin_id, app_id, account_key))

    async def _disconnect(plugin_id: str, account_key: str):
        await _revoke(plugin_id, app_id=GITHUB_APP, account_key=account_key)
        return SimpleNamespace(plugin_id=plugin_id)

    async def _status(plugin_id: str, account_key: str):
        del account_key
        return SimpleNamespace(plugin_id=plugin_id, enabled=False)

    service._connection_coordinator = SimpleNamespace(
        disconnect=_disconnect,
        status=_status,
    )
    await service.disconnect(GITHUB)
    assert service.plugin_registry.get(DRIVE).enabled is True
    assert service.plugin_registry.get(GITHUB).enabled is False
    assert revoked == [(GITHUB, GITHUB_APP, "account-1")]
    service.close()


@pytest.mark.anyio
async def test_github_start_fails_loudly_without_explicit_oauth_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMEGA_OPERATOR_ACCOUNT_KEY", "account-1")
    service = PluginLifecycleService(state_root=tmp_path)
    _install(service, GITHUB)
    with pytest.raises(ValueError, match="^oauth_profile_catalog_unavailable$"):
        _ = service.connection_service
    service.close()


@pytest.mark.anyio
async def test_github_oauth_accepts_copilot_mcp_slash_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMEGA_OPERATOR_ACCOUNT_KEY", "account-1")
    service = PluginLifecycleService(state_root=tmp_path)
    _install(service, GITHUB)
    record = service.app_registry.get(GITHUB_APP)
    descriptor = service.descriptor_for_app(GITHUB_APP)
    assert descriptor.remote_origin == "https://api.githubcopilot.com/mcp/"
    assert record.remote_origin == descriptor.remote_origin
    assert str(descriptor.remote_origin).rstrip("/") == "https://api.githubcopilot.com/mcp"
    service.close()


def test_github_profile_uses_generic_oauth_callback_without_reflecting_query(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    import hud.server as hud_server
    from hud.server import create_app
    class _GithubLifecycle:
        revision = "a" * 64

        async def complete_connection(self, callback_uri: str):
            del callback_uri
            return SimpleNamespace(
                plugin_id="github.official",
                app_id="github.official.mcp",
                state="connected",
                next_required_app_id=None,
            )

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        hud_server, "get_default_plugin_lifecycle_service", lambda: _GithubLifecycle()
    )
    client = TestClient(create_app())
    response = client.get(
        "/oauth/github.desktop/callback?code=opaque-code&state=opaque-state"
    )
    assert response.status_code == 200
    assert "opaque-code" not in response.text
    assert "opaque-state" not in response.text
    assert "github.official" in response.text
    assert "token" not in response.text.casefold()
    assert "omega:plugin-connected" in response.text
    assert response.headers["cache-control"] == "no-store"
