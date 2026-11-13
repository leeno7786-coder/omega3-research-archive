"""Fresh-install OAuth release contract tests."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcp_platform.plugin_lifecycle import compose_plugin_lifecycle_service
from mcp_platform.oauth_profiles import (
    OAuthAuthorizationFlow,
    OAuthProfileMatchInput,
    OAuthProfileRegistry,
)
from plugin_platform.trust import load_embedded_key_ring
from core import paths as core_paths
from mcp_platform.plugin_authority import (
    PluginConnectionAuthority,
    PluginConnectionAuthorityKind,
    PluginExecutionPolicy,
    plugin_connection_authority_revision,
)
from mcp_platform.package_contracts import RiskClass
from mcp_platform.plugin_planning import PluginOperationPlan, PluginOperationStep
from runtime.client import RuntimeClient
from tests.auto_loop.test_plugin_plan import _binding


def test_embedded_profiles_are_public_and_keep_github_public() -> None:
    registry = OAuthProfileRegistry.from_path(
        core_paths.OAUTH_PROFILE_CATALOG_PATH,
        load_embedded_key_ring(),
    )
    profiles = registry._catalog.profiles

    google = tuple(profile for profile in profiles if profile.app_publisher == "google")
    github = tuple(profile for profile in profiles if profile.app_publisher == "github")

    assert google
    assert all(
        profile.authorization_flow is OAuthAuthorizationFlow.AUTHORIZATION_CODE_PKCE
        and profile.client_secret_ref is None
        for profile in google
    )
    assert len(github) == 1
    assert github[0].authorization_issuer == "https://github.com/login/oauth"
    assert github[0].authorization_flow is OAuthAuthorizationFlow.DEVICE_CODE
    assert github[0].client_secret_ref is None
    assert github[0].redirect_uri is None


def test_signed_profile_specializes_a_declared_provider_origin_without_aliasing() -> None:
    registry = OAuthProfileRegistry.from_path(
        core_paths.OAUTH_PROFILE_CATALOG_PATH,
        load_embedded_key_ring(),
    )
    declared = OAuthProfileMatchInput(
        catalog_signer="omega.featured",
        plugin_publisher="omega.first_party",
        app_publisher="github",
        provider_id="github.official",
        authorization_issuer="https://github.com",
        resource_origin="https://api.githubcopilot.com/mcp/",
    )

    profile = registry.resolve_declared_app(declared)

    assert profile.authorization_issuer == "https://github.com/login/oauth"
    with pytest.raises(ValueError, match="oauth_profile_unavailable"):
        registry.resolve_declared_app(
            replace(declared, authorization_issuer="https://github.example")
        )
    with pytest.raises(ValueError, match="oauth_profile_unavailable"):
        registry.resolve_declared_app(
            replace(declared, authorization_issuer="https://github.com/other")
        )


def test_fresh_desktop_composition_owns_the_complete_connection_runtime(
    tmp_path: Path,
) -> None:
    service = compose_plugin_lifecycle_service(
        state_root=tmp_path,
        oauth_key_provider=lambda: b"k" * 32,
    )

    assert service.connection_service is not None
    assert service.plugin_grants is not None
    assert service.plugin_runtime is not None
    assert service.plugin_backend is not None
    assert service.permission_store.path == tmp_path / "mcp" / "plugin_permissions.json"
    assert service.plugin_grants.journal_path == tmp_path / "mcp" / "plugin_grants.json"


def test_runtime_client_exposes_the_process_plugin_composition(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = compose_plugin_lifecycle_service(
        state_root=tmp_path,
        oauth_key_provider=lambda: b"k" * 32,
    )
    monkeypatch.setenv("OMEGA_OPERATOR_ACCOUNT_KEY", "desktop-operator")
    monkeypatch.setattr(
        "mcp_platform.plugin_lifecycle._DEFAULT_SERVICE",
        service,
    )
    runtime = RuntimeClient(base_url="http://127.0.0.1:9")

    assert runtime.plugin_lifecycle_service is service
    assert runtime.plugin_runtime is service.plugin_runtime
    assert runtime.permission_store is service.permission_store
    assert runtime.plugin_tool_bindings() == ()
    assert callable(runtime.resolve_plugin_authority)


def _connection_authority(binding) -> PluginConnectionAuthority:
    values = {
        "plugin_id": binding.plugin_id,
        "app_id": binding.app_id,
        "account_key_hash": binding.account_key_hash,
        "connection_revision": binding.connection_revision,
        "provider_scopes": ("issues.write",),
        "capability_ceiling": (f"tool:{binding.capability}",),
        "allowed_tool_schemas": {binding.capability: binding.input_schema_sha256},
        "source_constraints": {"repository": "omega"},
        "destination_constraints": {"repository": "omega"},
        "execution_policy": PluginExecutionPolicy.CONFIRM_EACH,
        "authority_kind": PluginConnectionAuthorityKind.OAUTH,
    }
    return PluginConnectionAuthority(
        **values,
        revision=plugin_connection_authority_revision(**values),
    )


def test_live_connection_context_drives_single_use_permission_authority(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = compose_plugin_lifecycle_service(
        state_root=tmp_path,
        oauth_key_provider=lambda: b"k" * 32,
        operation_submitter=lambda operation, **_kwargs: asyncio.run(operation()),
    )
    account_key = "desktop-operator"
    monkeypatch.setenv("OMEGA_OPERATOR_ACCOUNT_KEY", account_key)
    account_hash = hashlib.sha256(account_key.encode()).hexdigest()
    binding = replace(
        _binding(
            candidate="3",
            capability="create_issue",
            title="Create issue",
            risk=RiskClass.ADDITIVE_WRITE,
            properties={"title": {"type": "string"}},
            required=["title"],
        ),
        account_key_hash=account_hash,
    )
    connection = _connection_authority(binding)
    binding = replace(
        binding,
        authority_revision=connection.revision,
    )
    prepared = SimpleNamespace(
        authority=connection,
        server_id="github",
        server_version="1.0.0",
        package_revision="a" * 64,
    )
    service._connection_service = SimpleNamespace(
        prepare_authority_context=lambda *_args, **_kwargs: _return_async(prepared)
    )
    service.plugin_grants = SimpleNamespace(
        current=lambda *_args, **_kwargs: SimpleNamespace(
            authority=connection,
            activation=SimpleNamespace(package_revision="a" * 64),
        )
    )
    monkeypatch.setattr(service, "plugin_tool_bindings", lambda: (binding,))
    monkeypatch.setattr(
        service,
        "descriptor_for_plugin_app",
        lambda _plugin_id, _app_id: SimpleNamespace(
            provider_id="github.official",
            publisher_id="github",
        ),
    )
    plan = PluginOperationPlan(
        plan_id="1" * 64,
        episode_id="episode-1",
        objective_sha256="2" * 64,
        steps=(
            PluginOperationStep(
                step_id="step-1",
                candidate_id=binding.candidate_id,
                arguments={"title": "Release Omega"},
                consumes_step_ids=(),
            ),
        ),
        authority_digest="4" * 64,
    )
    monkeypatch.setattr(service, "bindings_for_plan", lambda *_args: (binding,))

    request = service.permission_store.issue(
        plan,
        account_key_hash=account_hash,
        prior_episode_id=plan.episode_id,
        task_id="desktop-task",
    )
    grant = service.permission_store.approve(
        request.request_id,
        expected_revision=request.revision,
    )
    authorities = service.permission_store.consume(
        grant.grant_id,
        plan=plan,
        authority_digest=plan.authority_digest,
        account_key_hash=account_hash,
    )

    assert len(authorities) == 1
    authority = authorities[0]
    assert authority.connection_authority_revision == connection.revision
    assert authority.tool_name == binding.tool_name
    assert authority.account_key_hash == account_hash
    with pytest.raises(ValueError, match="plugin_permission_replayed"):
        service.permission_store.consume(
            grant.grant_id,
            plan=plan,
            authority_digest=plan.authority_digest,
            account_key_hash=account_hash,
        )


async def _return_async(value):
    return value
