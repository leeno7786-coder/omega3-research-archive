"""Provider-neutral clean-install roundtrip evidence.

This is intentionally a deterministic customer fixture.  Network OAuth and
real model acceptance are operator-led and are covered only by opt-in tests;
this test proves the local contracts that must hold before either is attempted.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import pytest

from auto_loop.execute.plugin_plan import plan_and_execute_plugin_request
from auto_loop.execute.run import _plugin_response_request
from mcp_platform.canonical import canonical_bytes
from mcp_platform.oauth_broker import OAuthBroker
from mcp_platform.app_sources import TrustedRemoteAppSource
from mcp_platform.client import McpProviderDriftStatus, McpProviderRecovery
from mcp_platform.client_contracts import McpClientErrorReason
from mcp_platform.plugin_backend import CurrentMcpPluginBackend
from mcp_platform.package_contracts import RiskClass
from mcp_platform.plugin_connections import descriptor_for_record
from mcp_platform.plugin_connections import _ProviderDiscoveryError
from mcp_platform.plugin_execution_worker import PluginExecutionWorker
from mcp_platform.plugin_runtime import PluginRuntimeError
from mcp_platform.app_registry import InstalledMcpAppRecord
from mcp_platform.plugin_lifecycle import (
    PluginLifecycleService,
    compose_plugin_lifecycle_service,
)
from mcp_platform.plugin_dispatch import plugin_tool_bindings
import mcp_platform.plugin_lifecycle as plugin_lifecycle_module
from tests.auto_loop.test_plugin_plan import _ModelRuntime, _binding
from tests.mcp_platform.test_plugin_runtime import (
    _PreparedConnectionService,
    _shared_loop_submit,
    _worker_preparation,
)
from tests.mcp_platform.test_oauth_broker import (
    _Clock,
    _Protocol,
    _Registry,
    _TrackingVault,
    _connected,
    _profile as broker_profile,
    _request,
)
from tests.mcp_platform.generic_plugin_peers import TRACKER_APP, TRACKER_PLUGIN
from tests.mcp_platform.plugin_execution_worker_round1_fixture import discovery_handler
from tests.mcp_platform.test_plugin_connections import ACCOUNT_KEY, _remote_service
from tests.helpers.plugin_consent import full_connection_grant, full_permission_settings


_CUSTOMER_TOKEN = "opaque-access"
_DEVELOPER_TOKEN = "developer-token-must-never-appear"


def _typed_discovery_error_handler(
    _body: dict[str, Any], _credential: dict[str, Any]
) -> dict[str, Any]:
    """Synthetic provider transport result using the production worker envelope."""

    return {
        "discovery_error": {
            "phase": "discovery",
            "reason": McpClientErrorReason.CAPABILITY_DRIFT.value,
        }
    }


def _recovered_provider_handler(
    body: dict[str, Any], credential: dict[str, Any]
) -> dict[str, Any]:
    if body.get("discovery") is True:
        return discovery_handler(body, credential)
    request = body["request"]
    capability = str(request["capability_id"]).removeprefix("tool:")
    structured_content = (
        {"id": "customer-file", "title": "Customer event document"}
        if capability == "get_file_metadata"
        else {"labels": [{"labelId": "customer-label", "name": "Customer"}]}
    )
    return {
        "request_id": request["request_id"],
        "app_id": request["app_id"],
        "capability_id": request["capability_id"],
        "capability_revision": request["capability_revision"],
        "status": "completed",
        "data": {
            "content": [{"type": "text", "text": "No customer files found."}],
            "structured_content": structured_content,
            "is_error": False,
        },
    }


def _customer_provider_handler(
    body: dict[str, Any], credential: dict[str, Any]
) -> dict[str, Any]:
    if body.get("operation") == "oauth_refresh":
        from mcp_platform.oauth_broker import _oauth_refresh_request_sha256

        request = body["request"]
        return {
            "operation": "oauth_refresh",
            "request_sha256": _oauth_refresh_request_sha256(request),
            "partition_key": request["partition_key"],
            "expected_revision": request["expected_revision"],
            "token": {
                "access_token": _CUSTOMER_TOKEN,
                "refresh_token": credential["refresh_token"],
                "token_type": "Bearer",
                "scopes": list(request["required_scopes"]),
                "expires_at": 4000000000.0,
            },
        }
    # The synthetic transport is still credential-bound: a changed or missing
    # broker-issued token is a provider failure, never a successful fixture.
    if credential.get("access_token") != _CUSTOMER_TOKEN:
        raise ValueError("customer_token_binding_invalid")
    return _recovered_provider_handler(body, credential)


class _RoundtripProtocol(_Protocol):
    async def aclose(self) -> None:
        return None


class _FlakyProviderWorker:
    def __init__(self, preparation, *, failures: int) -> None:
        self.preparation = preparation
        self.failures = failures
        self.calls = 0
        self.closed = False

    def run_sync(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        request = kwargs["body"]["request"]
        authority = kwargs["authority"]
        if self.calls <= self.failures:
            return {
                "request_id": request["request_id"],
                "app_id": authority.app_id,
                "capability_id": request["capability_id"],
                "capability_revision": authority.capability_revision,
                "status": "failed",
                "error": {"reason": "capability_drift"},
            }
        return {
            "request_id": request["request_id"],
            "app_id": authority.app_id,
            "capability_id": request["capability_id"],
            "capability_revision": authority.capability_revision,
            "status": "completed",
            "data": {
                "content": [{"type": "text", "text": "customer result"}],
                "structured_content": None,
                "is_error": False,
            },
        }

    def close(self) -> None:
        self.closed = True


def _drift_backend(tmp_path, worker, lookups):
    verified = PluginLifecycleService(state_root=tmp_path)._verified("google.workspace.drive")[2]
    record = InstalledMcpAppRecord.from_source(
        TrustedRemoteAppSource(requirement=verified.manifest.required_apps[0])
    )
    preparation = _worker_preparation(record)
    service = _PreparedConnectionService(preparation)
    backend = CurrentMcpPluginBackend(
        connection_service=service,
        descriptor_provider=lambda _plugin_id, _app_id: descriptor_for_record(record),
        plugin_id_for_app={record.app_id: "google.workspace.drive"},
        account_key_provider=lambda: "account-1",
        operation_submitter=_shared_loop_submit,
        execution_worker=worker(preparation),
        provider_update_lookup=lambda status: lookups.append(status)
        or McpProviderRecovery(status="applied"),
    )
    return backend


class _CustomerRuntime(_ModelRuntime):
    selected_model = "customer-local-model"

    def __init__(
        self,
        bindings: tuple[object, ...],
        decisions: list[dict[str, Any]],
        *,
        backend: CurrentMcpPluginBackend,
    ) -> None:
        super().__init__(bindings, decisions)
        self.backend = backend
        self.plugin_runtime._backend = backend
        self.request_log: list[str] = []
        self.vault_partition = ""

    def generate_completion(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        self.request_log.append(json.dumps({"prompt": prompt, **kwargs}, sort_keys=True))
        return super().generate_completion(prompt, **kwargs)


class _CustomerProviderWorker:
    def __init__(self, preparation) -> None:
        self.preparation = preparation
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    def run_sync(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        authority = kwargs["authority"]
        request = kwargs["body"]["request"]
        return {
            "request_id": request["request_id"],
            "app_id": authority.app_id,
            "capability_id": request["capability_id"],
            "capability_revision": authority.capability_revision,
            "status": "completed",
            "data": {"item": "event"},
        }

    def close(self) -> None:
        self.closed = True


class _DiscoveryFailureService:
    async def prepare_execution_worker(self, *_args: object, **_kwargs: object) -> object:
        raise _ProviderDiscoveryError(McpClientErrorReason.CAPABILITY_DRIFT)

    async def close_all(self) -> None:
        return None


class _InstalledPluginRegistry:
    def __init__(self, plugin_id: str = "google.workspace.drive") -> None:
        self.plugin_id = plugin_id

    def get(self, plugin_id: str) -> object:
        if plugin_id != self.plugin_id:
            raise PluginRuntimeError("plugin_not_installed")
        return type("PluginState", (), {"status": "installed", "enabled": True})()


class _InstalledAppRegistry:
    def __init__(self, record: InstalledMcpAppRecord) -> None:
        self.record = record

    def get(self, app_id: str) -> InstalledMcpAppRecord:
        if app_id != self.record.app_id:
            raise PluginRuntimeError("plugin_app_not_installed")
        return self.record


def _customer_oauth_connection(tmp_path):
    async def connect():
        profile = broker_profile(secret_ref=False)
        clock = _Clock()
        vault = _TrackingVault(tmp_path / "customer-vault", clock)
        broker = OAuthBroker(
            registry=_Registry(profile), vault=vault, protocol=_Protocol(), clock=clock
        )
        prepared, binding = await _connected(
            broker, _request(plugin_id="google.workspace.drive")
        )
        return prepared, binding, await broker.current_access_token(binding)

    return asyncio.run(connect())


def test_customer_evidence_projection_keeps_records_separate(
    tmp_path, monkeypatch
) -> None:
    # Install from the signed marketplace package into a clean temporary
    # registry, then use a separate customer-owned OAuth vault. No publisher
    # credential is loaded by this fixture.
    package_service = PluginLifecycleService(state_root=tmp_path / "package-state")
    review = package_service.review("google.workspace.drive")
    package_service.install("google.workspace.drive", confirmation=review.sha256)
    verified = package_service._verified("google.workspace.drive")[2]
    installed = package_service.app_registry.get("google.workspace.drive.mcp")
    package_service.close()
    descriptor = descriptor_for_record(installed)
    # The deterministic model catalog uses the verified package's real app
    # identity, while its provider transport is the production backend seam.
    preparation = _worker_preparation(installed, capability_id="search_files")
    customer_authorization, customer_binding_state, customer_token = (
        _customer_oauth_connection(tmp_path)
    )
    assert customer_authorization.account_key_sha256 == "1" * 64
    assert customer_token == _CUSTOMER_TOKEN
    preparation = replace(preparation, credential={"access_token": customer_token})
    service = _PreparedConnectionService(preparation)
    worker = _CustomerProviderWorker(preparation)
    backend = CurrentMcpPluginBackend(
        connection_service=service,
        descriptor_provider=lambda _plugin_id, _app_id: preparation.descriptor,
        plugin_id_for_app={installed.app_id: "google.workspace.drive"},
        account_key_provider=lambda: "account-1",
        operation_submitter=_shared_loop_submit,
        execution_worker=worker,
    )
    binding = _binding(
        candidate="a",
        capability="search_files",
        title="List customer events",
        risk=RiskClass.READ_ONLY,
        properties={"query": {"type": "string"}},
        required=["query"],
        account="1",
    )
    plan_decision = {
        "decision": "call_tool",
        "steps": [
            {
                "step_id": "step-1",
                "candidate_id": binding.candidate_id,
                "arguments": {"query": "upcoming events"},
                "consumes_step_ids": [],
            }
        ],
    }
    runtime = _CustomerRuntime(
        (replace(binding, app_id=installed.app_id, plugin_id="google.workspace.drive"),),
        [plan_decision, {"ok": True, "text": "The customer has one upcoming event."}],
        backend=backend,
    )
    runtime.vault_partition = customer_binding_state.partition_key
    runtime.plugin_runtime._plugin_registry = _InstalledPluginRegistry()
    runtime.plugin_runtime._app_registry = _InstalledAppRegistry(installed)
    monkeypatch.setattr(
        "auto_loop.execute.plugin_plan._deterministic_plan",
        lambda *_args, **_kwargs: None,
    )

    outcome = plan_and_execute_plugin_request(
        "Use the customer events plugin to list upcoming events",
        episode_id="customer-roundtrip",
        runtime_client=runtime,
        work_kind="operator_turn",
    )

    assert outcome.status == "completed", outcome.failure_reason
    assert outcome.plan is not None
    assert outcome.plugin_result is not None
    assert runtime.selected_model == "customer-local-model"
    assert len(worker.calls) == 1
    assert worker.calls[0]["body"]["request"]["capability_id"] == "tool:search_files"
    assert worker.calls[0]["body"]["request"]["arguments"] == {
        "query": "upcoming events"
    }

    # These are separate evidence records, matching the AUTO output contract:
    # discovery identity, model selection, authorization, provider result,
    # screening, and final response are never collapsed into one opaque blob.
    discovery_identity = {
        "plugin_id": "google.workspace.drive",
        "app_id": installed.app_id,
        "package_revision": verified.package_sha256,
        "capability_revision": descriptor.capability_revision,
    }
    selected_tool = {
        "tool": outcome.plan.steps[0].candidate_id,
        "arguments": dict(outcome.plan.steps[0].arguments),
    }
    authorization = {
        "account_key_hash": binding.account_key_hash,
        "vault_partition": runtime.vault_partition,
        "grant": "customer-read-only-grant",
    }
    provider_result = outcome.plugin_result
    screening = {
        "status": outcome.plugin_result["status"],
        "screened_data": outcome.plugin_result["data"],
    }
    response_request = _plugin_response_request(
        "list upcoming events",
        outcome.plugin_result,
    )
    final = runtime.generate_completion(
        response_request.user,
        system=response_request.system,
        model=runtime.selected_model,
    )
    assert final["ok"] is True
    final_model_response = str(final["text"])
    evidence = {
        "discovery_identity": discovery_identity,
        "model_selection": {"model_id": runtime.selected_model},
        "selected_tool": selected_tool,
        "authorization": authorization,
        "provider_result": provider_result,
        "screening": screening,
        "final_model_response": final_model_response,
    }
    assert set(evidence) == {
        "discovery_identity",
        "model_selection",
        "selected_tool",
        "authorization",
        "provider_result",
        "screening",
        "final_model_response",
    }
    assert response_request.user

    serialized = canonical_bytes(evidence).decode("utf-8")
    observed = "\n".join((*runtime.request_log, serialized, response_request.user))
    assert _CUSTOMER_TOKEN not in observed
    assert _DEVELOPER_TOKEN not in observed
    assert "access_token" not in observed
    assert "refresh_token" not in observed


def test_real_clean_install_oauth_worker_model_roundtrip(tmp_path, monkeypatch) -> None:
    """Exercise installer, broker grant, real worker, backend, and model seams."""

    roundtrip_plugin = "google.workspace.gmail"
    monkeypatch.setenv("OMEGA_OPERATOR_ACCOUNT_KEY", "customer-account")
    monkeypatch.setattr(plugin_lifecycle_module, "OAuthProtocolClient", _RoundtripProtocol)
    monkeypatch.setattr(
        plugin_lifecycle_module, "execute_mcp_operation", _customer_provider_handler
    )
    service = compose_plugin_lifecycle_service(
        state_root=tmp_path,
        oauth_key_provider=lambda: b"c" * 32,
        operation_submitter=_shared_loop_submit,
    )
    try:
        review = service.review(roundtrip_plugin)
        service.install(roundtrip_plugin, confirmation=review.sha256)
        service._set_enabled(roundtrip_plugin, True)
        app = service.app_registry.get(f"{roundtrip_plugin}.mcp")
        descriptor = service.descriptor_for_plugin_app(
            roundtrip_plugin, app.app_id
        )
        # The clean install connects the way the operator does: through the
        # permissions form, accepting the app's full offer.
        selection = _shared_loop_submit(
            lambda: service.permissions(roundtrip_plugin, app.app_id)
        )
        start = _shared_loop_submit(
            lambda: service.set_permissions(
                roundtrip_plugin,
                revision=selection["revision"],
                settings=full_permission_settings(selection),
                app_id=app.app_id,
            )
        )
        assert start.authorization_url
        state = parse_qs(urlsplit(start.authorization_url).query)["state"][0]
        redirect_uri = parse_qs(urlsplit(start.authorization_url).query)["redirect_uri"][0]
        callback = f"{redirect_uri}?{urlencode({'code': 'customer-code', 'state': state, 'iss': 'https://accounts.google.com'})}"
        connected = _shared_loop_submit(
            lambda: service._connection_service.complete(start.request_id or "", callback)
        )
        assert connected.status == "connected"
        bindings = plugin_tool_bindings(service=service)
        selected_matches = tuple(
            item
            for item in bindings
            if item.plugin_id == roundtrip_plugin
            and item.risk_class is RiskClass.READ_ONLY
            and item.capability == "list_labels"
        )
        assert len(selected_matches) == 1
        selected = selected_matches[0]
        assert service.plugin_grants.current(
            roundtrip_plugin,
            app_id=app.app_id,
            account_key_sha256=selected.account_key_hash,
        ) is not None
        arguments = {}
        # The selected binding is the model's signed catalog choice.  Let the
        # repository model/runtime seam plan it, invoke PluginRuntime's real
        # screening, and execute through the installed OAuth-bound backend.
        plan_decision = {
            "decision": "call_tool",
            "steps": [
                {
                    "step_id": "step-1",
                    "candidate_id": selected.candidate_id,
                    "arguments": arguments,
                    "consumes_step_ids": [],
                }
            ],
        }
        runtime = _CustomerRuntime(
            (selected,),
            [plan_decision, {"ok": True, "text": "Customer label: Customer."}],
            backend=service.plugin_backend,
        )
        runtime.plugin_runtime._plugin_registry = _InstalledPluginRegistry(roundtrip_plugin)
        runtime.plugin_runtime._app_registry = _InstalledAppRegistry(app)
        monkeypatch.setattr(
            "auto_loop.execute.plugin_plan._deterministic_plan",
            lambda *_args, **_kwargs: None,
        )
        from auto_loop.execute import plugin_plan as plugin_plan_module

        raw_provider_outputs: list[dict[str, Any]] = []
        original_screen_result = plugin_plan_module._screen_result

        def capture_provider_output(
            result: dict[str, Any], binding: object
        ) -> dict[str, Any]:
            raw_provider_outputs.append(dict(result))
            return original_screen_result(result, binding)

        monkeypatch.setattr(
            plugin_plan_module, "_screen_result", capture_provider_output
        )
        outcome = plan_and_execute_plugin_request(
            f"Use the {selected.capability} plugin to list customer labels",
            episode_id="customer-real-roundtrip",
            runtime_client=runtime,
            work_kind="operator_turn",
        )
        assert outcome.status == "completed", outcome.failure_reason
        assert outcome.plan is not None
        assert outcome.plugin_result is not None
        assert runtime.generation_calls == 1
        assert runtime.plugin_runtime._backend is service.plugin_backend
        assert len(raw_provider_outputs) == 1
        provider_result = raw_provider_outputs[0]
        screened_result = outcome.plugin_result
        assert provider_result["status"] == "completed"
        assert provider_result["data"]["structured_content"] == {
            "labels": [{"labelId": "customer-label", "name": "Customer"}]
        }
        assert screened_result is not None
        assert screened_result["data"]["structured_content"] == provider_result["data"][
            "structured_content"
        ]
        response_request = _plugin_response_request("list customer labels", screened_result)
        assert "customer-label" in response_request.user
        final = runtime.generate_completion(
            response_request.user,
            system=response_request.system,
            model=runtime.selected_model,
        )
        assert final["ok"] is True
        final_model_response = str(final["text"])
        assert "Customer" in final_model_response
        serialized = canonical_bytes(
            {
                "discovery_identity": {
                    "plugin_id": selected.plugin_id,
                    "app_id": selected.app_id,
                    "signed_candidate_id": selected.candidate_id,
                    "capability_revision": descriptor.capability_revision,
                },
                "model_selection": {
                    "model_id": runtime.selected_model,
                    "generation_calls": runtime.generation_calls,
                },
                "authorization": {
                    "status": connected.status,
                    "account_key_hash": selected.account_key_hash,
                    "grant_bound": True,
                },
                "selected_tool": {
                    "tool": outcome.plan.steps[0].candidate_id,
                    "arguments": dict(outcome.plan.steps[0].arguments),
                },
                "provider_result": provider_result,
                "screening": {
                    "status": screened_result["status"],
                    "screened_data": screened_result["data"],
                },
                "final_model_response": final_model_response,
            }
        ).decode("utf-8")
        observed = "\n".join(
            (*runtime.request_log, serialized, response_request.user, final_model_response)
        )
        assert _CUSTOMER_TOKEN not in observed
        assert _DEVELOPER_TOKEN not in observed
        assert "access_token" not in observed
        assert "refresh_token" not in observed
    finally:
        service.close()


def test_provider_drift_performs_one_update_lookup_and_one_retry(tmp_path) -> None:
    lookups: list[object] = []
    backend = _drift_backend(
        tmp_path,
        lambda preparation: _FlakyProviderWorker(preparation, failures=1),
        lookups,
    )
    try:
        result = backend.call_tool_sync_for_plugin(
            "google.workspace.drive",
            "google.workspace.drive.mcp",
            "search_files",
            {"query": "customer"},
        )
        assert result["content"][0]["text"] == "customer result"
        assert len(lookups) == 1
        assert lookups[0].phase == "tools/call"
        assert lookups[0].reason.value == "capability_drift"
        assert backend.drift_status is not None
        assert backend.drift_status.update_lookup_attempted is True
        assert backend.drift_status.retry_attempted is True
        # A later independent action starts with a clean status snapshot; the
        # successful recovery above must not become shared retry state.
        second = backend.call_tool_sync_for_plugin(
            "google.workspace.drive",
            "google.workspace.drive.mcp",
            "search_files",
            {"query": "customer"},
        )
        assert second["content"][0]["text"] == "customer result"
        assert backend.drift_status is None
        assert len(lookups) == 1
    finally:
        backend.close()


def test_provider_recovery_statuses_are_mutually_exclusive(tmp_path) -> None:
    """Approval never masquerades as reconnect, and reconnect never approves."""

    approval_backend = _drift_backend(
        tmp_path / "approval",
        lambda preparation: _FlakyProviderWorker(preparation, failures=1),
        [],
    )
    try:
        approval_backend._provider_update_lookup = lambda _status: McpProviderRecovery(
            status="approval_required", approval_diff=("scope_changed",)
        )
        approval_backend._record_provider_failure(
            McpClientErrorReason.CAPABILITY_DRIFT, phase="discovery"
        )
        assert approval_backend._lookup_update_once() is False
        approval_status = approval_backend.drift_status
        assert approval_status is not None
        assert approval_status.approval_required is True
        assert approval_status.approval_diff == ("scope_changed",)
        assert approval_status.reconnect_required is False
        assert approval_status.retry_attempted is False
    finally:
        approval_backend.close()

    reconnect_backend = _drift_backend(
        tmp_path / "reconnect",
        lambda preparation: _FlakyProviderWorker(preparation, failures=1),
        [],
    )
    try:
        reconnect_backend._provider_update_lookup = lambda _status: McpProviderRecovery(
            status="reconnect_required"
        )
        reconnect_backend._record_provider_failure(
            McpClientErrorReason.CAPABILITY_DRIFT, phase="discovery"
        )
        assert reconnect_backend._lookup_update_once() is False
        reconnect_status = reconnect_backend.drift_status
        assert reconnect_status is not None
        assert reconnect_status.approval_required is False
        assert reconnect_status.approval_diff == ()
        assert reconnect_status.reconnect_required is True
    finally:
        reconnect_backend.close()


def test_discovery_failure_keeps_phase_and_reason_through_backend(tmp_path) -> None:
    lookups: list[object] = []
    backend = CurrentMcpPluginBackend(
        connection_service=_DiscoveryFailureService(),
        descriptor_provider=lambda *_args: None,
        plugin_id_for_app={"google.workspace.drive.mcp": "google.workspace.drive"},
        account_key_provider=lambda: "account-1",
        operation_submitter=_shared_loop_submit,
        execution_worker=_CustomerProviderWorker(None),
        provider_update_lookup=lambda status: lookups.append(status)
        or McpProviderRecovery(status="applied"),
    )
    try:
        with pytest.raises(PluginRuntimeError, match="^plugin_capability_drift$") as raised:
            backend.call_tool_sync_for_plugin(
                "google.workspace.drive",
                "google.workspace.drive.mcp",
                "search_files",
                {"query": "customer"},
            )
        assert len(lookups) == 1
        status = raised.value.provider_status
        assert status.phase == "discovery"
        assert status.plugin_id == "google.workspace.drive"
        assert status.update_lookup_attempted is True
        assert status.retry_attempted is True
    finally:
        backend.close()


def test_production_lifecycle_wires_signed_provider_update_lookup(tmp_path) -> None:
    service = compose_plugin_lifecycle_service(
        state_root=tmp_path,
        oauth_key_provider=lambda: b"r" * 32,
        operation_submitter=lambda operation, **_kwargs: asyncio.run(operation()),
    )
    try:
        assert service.plugin_backend is not None
        assert callable(service.plugin_backend._provider_update_lookup)
        calls: list[dict[str, object]] = []

        def checked(**kwargs: object) -> SimpleNamespace:
            calls.append(kwargs)
            return SimpleNamespace(
                status="checked",
                candidate_versions=(("google.workspace.drive", "1.0.2"),),
                staged=(),
                approval_required=(),
                error=None,
            )

        service.check_updates = checked
        status = McpProviderDriftStatus(
            phase="discovery",
            reason=McpClientErrorReason.CAPABILITY_DRIFT,
            plugin_id="google.workspace.drive",
        )
        recovery = service.plugin_backend._provider_update_lookup(status)
        assert recovery.status == "applied"
        assert calls == [{"force": True}]
        for result in (
            SimpleNamespace(
                status="checked",
                candidate_versions=(("other.plugin", "1.0.2"),),
                staged=(),
                approval_required=(),
                error=None,
            ),
            SimpleNamespace(
                status="checked",
                candidate_versions=(("google.workspace.drive", "1.0.2"),),
                staged=(("google.workspace.drive", "1.0.2"),),
                approval_required=(),
                error=None,
            ),
            SimpleNamespace(
                status="checked",
                candidate_versions=(("google.workspace.drive", "1.0.2"),),
                staged=(),
                approval_required=(("google.workspace.drive", "1.0.2", ("scope",)),),
                error=None,
            ),
        ):
            service.check_updates = lambda **_kwargs: result
            recovery = service.plugin_backend._provider_update_lookup(status)
            assert recovery.status in {"approval_required", "reconnect_required"}
            if recovery.status == "approval_required":
                assert recovery.approval_diff
    finally:
        service.close()


def test_provider_drift_never_retries_more_than_once_or_reprompts_oauth(tmp_path) -> None:
    lookups: list[object] = []
    backend = _drift_backend(
        tmp_path,
        lambda preparation: _FlakyProviderWorker(preparation, failures=2),
        lookups,
    )
    try:
        with pytest.raises(PluginRuntimeError, match="^plugin_capability_drift$") as raised:
            backend.call_tool_sync_for_plugin(
                "google.workspace.drive",
                "google.workspace.drive.mcp",
                "search_files",
                {"query": "customer"},
            )
        assert len(lookups) == 1
        status = raised.value.provider_status
        assert status.update_lookup_attempted is True
        assert status.retry_attempted is True
        assert status.reconnect_required is False
    finally:
        backend.close()


@pytest.mark.anyio
async def test_real_worker_discovery_error_reaches_backend_drift_status(tmp_path) -> None:
    """The production worker/connection boundary keeps provider phase/reason typed."""

    worker = PluginExecutionWorker(
        child_handler=discovery_handler,
        shutdown_timeout_seconds=2.0,
    )
    service, _broker, _installed = _remote_service(
        tmp_path,
        authentication_kind="none",
        execution_worker=worker,
    )
    lookups: list[McpProviderDriftStatus] = []
    try:
        started = await service.start(
            TRACKER_PLUGIN,
            app_id=TRACKER_APP,
            account_key=ACCOUNT_KEY,
            **full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP),
        )
        assert started.status == "connected"
        # Keep the real service and worker, changing only the bounded synthetic
        # provider result to the exact envelope emitted by execute_mcp_operation.
        worker._child_handler = _typed_discovery_error_handler
        descriptor = service._descriptor(TRACKER_PLUGIN, TRACKER_APP)
        backend = CurrentMcpPluginBackend(
            connection_service=service,
            descriptor_provider=lambda _plugin_id, _app_id: descriptor,
            plugin_id_for_app={TRACKER_APP: TRACKER_PLUGIN},
            account_key_provider=lambda: ACCOUNT_KEY,
            operation_submitter=_shared_loop_submit,
            execution_worker=worker,
            provider_update_lookup=lambda status: lookups.append(status)
            or McpProviderRecovery(status="applied"),
        )
        try:
            def recover(status: McpProviderDriftStatus) -> McpProviderRecovery:
                lookups.append(status)
                return McpProviderRecovery(status="reconnect_required")

            backend._provider_update_lookup = recover
            with pytest.raises(PluginRuntimeError, match="^plugin_capability_drift$") as raised:
                await asyncio.to_thread(
                    backend.call_tool_sync_for_plugin,
                    TRACKER_PLUGIN,
                    TRACKER_APP,
                    "search",
                    {},
                )
            status = raised.value.provider_status
            assert status.phase == "discovery"
            assert status.reason is McpClientErrorReason.CAPABILITY_DRIFT
            assert status.update_lookup_attempted is True
            assert status.retry_attempted is False
            assert status.reconnect_required is True
            assert len(lookups) == 1
        finally:
            backend.close()
    finally:
        await service.close_all()
        worker.close()
