from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from auto_loop.execute.plugin_plan import plan_and_execute_plugin_request
from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.plugin_lifecycle import PluginLifecycleService
from mcp_platform.operation_contracts import McpAuthoritySnapshot
from mcp_platform.package_contracts import RiskClass
from mcp_platform.plugin_authority import plugin_plan_digest
from mcp_platform.plugin_catalog import compile_plugin_tool_bindings
from mcp_platform.plugin_permissions import PluginPermissionStore
from mcp_platform.plugin_runtime import PluginRuntime
from tests.auto_loop.test_plugin_plan import _FakeBackend


def _install_enabled_drive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PluginLifecycleService:
    monkeypatch.setenv("OMEGA_OPERATOR_ACCOUNT_KEY", "account-1")
    service = PluginLifecycleService(state_root=tmp_path)
    review = service.review("google.workspace.drive")
    service.install("google.workspace.drive", confirmation=review.sha256)
    service.plugin_registry.set_enabled(
        "google.workspace.drive",
        True,
        expected_revision=service.plugin_registry.snapshot().revision,
    )
    return service


def _compile(service: PluginLifecycleService):
    return compile_plugin_tool_bindings(
        plugin_snapshot=service.plugin_registry.snapshot(),
        app_snapshot=service.app_registry.snapshot(),
        descriptor_for_app=service.descriptor_for_plugin_app,
        account_key_hash=sha256(b"account-1").hexdigest(),
    )


class _LifecycleRuntime:
    def __init__(self, service: PluginLifecycleService, bindings, backend: _FakeBackend) -> None:
        self.bindings = bindings
        self.backend = backend
        self.permission_store = PluginPermissionStore(
            path=Path(service.plugin_registry.registry_path).parent / "permissions.json",
            authority_factory=self._authority_for_plan,
            binding_resolver=self._bindings_for_plan,
        )
        self.plugin_runtime = PluginRuntime(
            plugin_registry=service.plugin_registry,
            app_registry=service.app_registry,
            backend=backend,
        )

    def plugin_tool_bindings(self):
        return self.bindings

    def _bindings_for_plan(self, plan, account_key_hash):
        available = {
            item.candidate_id: item
            for item in self.bindings
            if item.account_key_hash == account_key_hash
        }
        return tuple(available[step.candidate_id] for step in plan.steps)

    def _authority_for_plan(self, plan, account_key_hash, grant_id):
        digest = plugin_plan_digest(plan)
        authorities = []
        for step in plan.steps:
            binding = next(item for item in self.bindings if item.candidate_id == step.candidate_id)
            risk = binding.risk_class
            authorities.append(
                McpAuthoritySnapshot(
                    authority_kind="direct_turn",
                    authority_id=grant_id,
                    authority_revision=sha256_hex(
                        canonical_bytes(
                            {
                                "grant_id": grant_id,
                                "plan": digest,
                                "account": account_key_hash,
                            }
                        )
                    ),
                    episode_id=plan.episode_id,
                    job_id="",
                    job_revision="",
                    connection_id="connection-drive",
                    connection_revision=binding.connection_revision,
                    server_id=binding.app_id,
                    server_version="1.0.0",
                    artifact_sha256="d" * 64,
                    tool_name=binding.tool_name,
                    tool_schema_sha256=binding.input_schema_sha256,
                    capability_tags=(f"tool:{binding.capability}",),
                    source_constraints={},
                    destination_constraints={},
                    risk_classes=(risk.value,),
                    direct_execution=True,
                    standing_execution=False,
                    allow_send_publish=risk is RiskClass.SEND_PUBLISH,
                    allow_destructive=risk is RiskClass.DESTRUCTIVE,
                    allow_cross_provider=risk is RiskClass.CROSS_PROVIDER_TRANSFER,
                    plan_digest=digest,
                    account_key_hash=account_key_hash,
                    candidate_id=step.candidate_id,
                    plan_step_id=step.step_id,
                    arguments_sha256=sha256_hex(canonical_bytes(dict(step.arguments))),
                    connection_authority_revision="9" * 64,
                    effect=binding.effect,
                )
            )
        return tuple(authorities)


def test_direct_turn_drive_search_does_not_open_permission_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _install_enabled_drive(tmp_path, monkeypatch)
    compiled = _compile(service)
    search = tuple(item for item in compiled if item.capability == "search_files")
    assert search and search[0].plugin_id == "google.workspace.drive"
    assert search[0].risk_class is RiskClass.READ_ONLY
    backend = _FakeBackend()
    runtime = _LifecycleRuntime(service, search, backend)
    outcome = plan_and_execute_plugin_request(
        "Find a Drive file named Quarterly",
        episode_id="episode-drive-read",
        runtime_client=runtime,
    )
    assert outcome.status == "completed", outcome.failure_reason
    assert outcome.permission_request is None
    assert runtime.permission_store.pending() == ()
    assert backend.calls
    service.close()


def test_confirm_each_drive_create_cancel_clears_permission_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _install_enabled_drive(tmp_path, monkeypatch)
    compiled = _compile(service)
    writes = tuple(item for item in compiled if item.capability == "create_file")
    assert writes and writes[0].risk_class is not RiskClass.READ_ONLY
    backend = _FakeBackend()
    runtime = _LifecycleRuntime(service, writes, backend)
    outcome = plan_and_execute_plugin_request(
        "Create a Drive file named Notes",
        episode_id="episode-drive-write",
        runtime_client=runtime,
    )
    assert outcome.status == "permission_required", outcome.failure_reason
    request = outcome.permission_request
    assert request is not None
    runtime.permission_store.cancel(request.request_id, expected_revision=request.revision)
    assert runtime.permission_store.pending() == ()
    assert backend.calls == []
    service.close()
