from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from mcp_platform.app_registry import InstalledMcpAppRecord
from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.operation_contracts import McpAuthoritySnapshot, McpConnectionSnapshot
from mcp_platform.package_contracts import RiskClass
from mcp_platform.plugin_catalog import PluginToolBinding
from mcp_platform.plugin_permissions import (
    PluginPermissionDisplay,
    PluginPermissionRequest,
    PluginPlanGrant,
)
from mcp_platform.plugin_runtime import PluginRuntime, PluginRuntimeError
from mcp_platform.plugin_authority import (
    PluginConnectionAuthority,
    PluginConnectionAuthorityKind,
    PluginExecutionPolicy,
    plugin_connection_authority_revision,
    plugin_plan_digest,
)
from plugin_platform.contracts import (
    PLUGIN_PROTOCOL_VERSION,
    AppDeployment,
    CapabilityKind,
    CapabilityRequirement,
    McpTransport,
    PluginAuthentication,
)
from auto_loop.execute.plugin_plan import (
    _catalog_digest,
    _deterministic_plan,
    _objective_receipt,
    _screen_failure,
    plugin_plan_grant_from_contributor,
    plan_and_execute_plugin_request,
)

_CAPABILITIES = (
    "create_issue",
    "events_archive",
    "follow_up",
    "list_events",
    "lookup",
)


def _binding(
    *,
    candidate: str,
    capability: str,
    title: str,
    risk: RiskClass,
    properties: dict[str, object],
    required: list[str],
    account: str = "b",
) -> PluginToolBinding:
    input_schema = {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }
    output_schema = {
        "type": "object",
        "properties": {"item": {"type": "string"}},
        "required": ["item"],
        "additionalProperties": False,
    }
    return PluginToolBinding(
        candidate_id=candidate * 64,
        tool_name=f"tool_{capability}",
        plugin_id="plugin.synthetic",
        plugin_revision="a" * 64,
        app_id="app.synthetic",
        account_key_hash=account * 64,
        connection_revision="c" * 64,
        capability=capability,
        title=title,
        description=title,
        input_schema=input_schema,
        input_schema_sha256=sha256_hex(canonical_bytes(input_schema)),
        output_schema=output_schema,
        output_schema_sha256=sha256_hex(canonical_bytes(output_schema)),
        risk_class=risk,
        effect="read" if risk is RiskClass.READ_ONLY else "create",
        requires_revision=False,
        max_result_bytes=4096,
        allowed_mime_types=("application/json",),
    )


def _app_record() -> InstalledMcpAppRecord:
    return InstalledMcpAppRecord(
        app_id="app.synthetic",
        publisher_id="publisher.synthetic",
        deployment=AppDeployment.REMOTE,
        provider_id="provider.synthetic",
        protocol_version=PLUGIN_PROTOCOL_VERSION,
        transports=(McpTransport.STREAMABLE_HTTP,),
        remote_origin="https://synthetic.example",
        authorization_issuer="https://synthetic.example/oauth",
        authentication=PluginAuthentication.from_dict(
            {
                "kind": "oauth_device_code",
                "scopes": ["synthetic.read"],
                "secret_fields": [],
            }
        ),
        capabilities=tuple(
            CapabilityRequirement(
                kind=CapabilityKind.TOOL,
                name=name,
                schema_sha256=None,
                version=None,
            )
            for name in _CAPABILITIES
        ),
        local_package_version=None,
        local_package_platform=None,
        local_package_manifest_sha256=None,
        installed=True,
        trusted=True,
        enabled=False,
    )


class _PluginRegistry:
    def get(self, plugin_id: str) -> object:
        if plugin_id != "plugin.synthetic":
            raise PluginRuntimeError("plugin_not_installed")
        return SimpleNamespace(status="installed", enabled=True)


class _AppRegistry:
    def __init__(self, record: InstalledMcpAppRecord) -> None:
        self._record = record

    def get(self, app_id: str) -> InstalledMcpAppRecord:
        if app_id != self._record.app_id:
            raise PluginRuntimeError("plugin_app_not_installed")
        return self._record


class _FakeBackend:
    """The provider transport seam — the only thing these tests substitute.

    ``PluginRuntime.invoke_tool`` runs unmodified above it, so every identity,
    schema, screening and ledger check the executor depends on is really
    exercised.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def call_tool_sync(
        self,
        app_id: str,
        name: str,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        self.calls.append((app_id, name, dict(arguments)))
        return {"item": "event"}


class _RaisingBackend(_FakeBackend):
    """A provider transport that fails on the wire."""

    def __init__(self, error: BaseException) -> None:
        super().__init__()
        self.error = error

    def call_tool_sync(self, *_args: Any) -> dict[str, Any]:
        raise self.error


class _Runtime:
    def __init__(
        self,
        bindings: tuple[PluginToolBinding, ...],
        *,
        backend: _FakeBackend | None = None,
    ) -> None:
        self.bindings = bindings
        self.backend = _FakeBackend() if backend is None else backend
        self.plugin_runtime = PluginRuntime(
            plugin_registry=_PluginRegistry(),
            app_registry=_AppRegistry(_app_record()),
            backend=self.backend,
        )

    @property
    def calls(self) -> list[tuple[str, str, dict[str, Any]]]:
        return self.backend.calls

    def plugin_tool_bindings(self) -> tuple[PluginToolBinding, ...]:
        return self.bindings


class _ChangingCatalogRuntime(_Runtime):
    def __init__(self, initial, changed):
        super().__init__((initial,))
        self.initial = initial
        self.changed = changed
        self.catalog_reads = 0

    def plugin_tool_bindings(self):
        self.catalog_reads += 1
        return (self.initial,) if self.catalog_reads < 3 else (self.changed,)


class _TwoStepRuntime(_Runtime):
    def generate_completion(self, *_args, **_kwargs):
        return {
            "decision": "call_tool",
            "steps": [
                {
                    "step_id": "lookup",
                    "candidate_id": "a" * 64,
                    "arguments": {"query": "events"},
                    "consumes_step_ids": [],
                },
                {
                    "step_id": "follow-up",
                    "candidate_id": "b" * 64,
                    "arguments": {
                        "item": {
                            "$from_step": {
                                "step_id": "lookup",
                                "path": ["item"],
                            }
                        }
                    },
                    "consumes_step_ids": ["lookup"],
                },
            ],
        }


class _TwoStepWriteRuntime(_Runtime):
    def __init__(self, bindings):
        super().__init__(bindings)
        self.generation_calls = 0

    def generate_completion(self, *_args, **_kwargs):
        self.generation_calls += 1
        return {
            "decision": "call_tool",
            "steps": [
                {
                    "step_id": "lookup",
                    "candidate_id": "a" * 64,
                    "arguments": {"priority": 1},
                    "consumes_step_ids": [],
                },
                {
                    "step_id": "create",
                    "candidate_id": "b" * 64,
                    "arguments": {
                        "item": {
                            "$from_step": {
                                "step_id": "lookup",
                                "path": ["item"],
                            }
                        }
                    },
                    "consumes_step_ids": ["lookup"],
                },
            ],
        }


class _ModelRuntime(_Runtime):
    def __init__(self, bindings, decisions):
        super().__init__(bindings)
        self.decisions = list(decisions)
        self.generation_calls = 0

    def generate_completion(self, *_args, **_kwargs):
        self.generation_calls += 1
        return self.decisions.pop(0)


class _RecordingModelRuntime(_ModelRuntime):
    def __init__(self, bindings, decisions):
        super().__init__(bindings, decisions)
        self.generation_kwargs: list[dict[str, object]] = []

    def generate_completion(self, *_args, **kwargs):
        self.generation_kwargs.append(dict(kwargs))
        return super().generate_completion(*_args, **kwargs)


class _DirectAuthorityService:
    def __init__(self, binding: PluginToolBinding) -> None:
        self.binding = binding
        self.calls = 0
        self.evaluations: list[tuple[str, str]] = []

    def evaluate_operation(self, proposal, *, connection, authority, tool, **_kwargs):
        self.evaluations.append((authority.authority_kind, tool.tool_name))
        return SimpleNamespace(allowed=True, reason="", obligations=())

    def authorize_direct_turn(self, plan, *, connection, connection_snapshot, **_kwargs):
        self.calls += 1
        step = plan.steps[0]
        return McpAuthoritySnapshot(
            authority_kind="direct_turn",
            authority_id="a" * 64,
            authority_revision="b" * 64,
            episode_id=plan.episode_id,
            job_id="",
            job_revision="",
            connection_id=connection_snapshot.connection_id,
            connection_revision=connection_snapshot.connection_revision,
            server_id=connection_snapshot.server_id,
            server_version=connection_snapshot.server_version,
            artifact_sha256=connection_snapshot.artifact_sha256,
            tool_name=self.binding.tool_name,
            tool_schema_sha256=self.binding.input_schema_sha256,
            capability_tags=(f"tool:{self.binding.capability}",),
            source_constraints=dict(connection.source_constraints),
            destination_constraints=dict(connection.destination_constraints),
            risk_classes=(self.binding.risk_class.value,),
            direct_execution=True,
            standing_execution=False,
            allow_send_publish=False,
            allow_destructive=False,
            allow_cross_provider=False,
            plan_digest=plugin_plan_digest(plan),
            account_key_hash=self.binding.account_key_hash,
            candidate_id=step.candidate_id,
            plan_step_id=step.step_id,
            arguments_sha256=sha256_hex(canonical_bytes(dict(step.arguments))),
            connection_authority_revision=connection.revision,
            effect=self.binding.effect,
        )


def _authority_pair(
    binding: PluginToolBinding,
    policy: PluginExecutionPolicy,
) -> tuple[PluginConnectionAuthority, McpConnectionSnapshot]:
    """The connection-scope authority and its snapshot for one binding."""
    ceiling = (f"tool:{binding.capability}",)
    schemas = {
        binding.capability: binding.input_schema_sha256,
        binding.tool_name: binding.input_schema_sha256,
    }
    revision = plugin_connection_authority_revision(
        plugin_id=binding.plugin_id,
        app_id=binding.app_id,
        account_key_hash=binding.account_key_hash,
        connection_revision=binding.connection_revision,
        provider_scopes=("scope",),
        capability_ceiling=ceiling,
        allowed_tool_schemas=schemas,
        source_constraints={},
        destination_constraints={},
        execution_policy=policy,
        authority_kind=PluginConnectionAuthorityKind.OAUTH,
    )
    authority = PluginConnectionAuthority(
        plugin_id=binding.plugin_id,
        app_id=binding.app_id,
        account_key_hash=binding.account_key_hash,
        connection_revision=binding.connection_revision,
        provider_scopes=("scope",),
        capability_ceiling=ceiling,
        allowed_tool_schemas=schemas,
        source_constraints={},
        destination_constraints={},
        execution_policy=policy,
        authority_kind=PluginConnectionAuthorityKind.OAUTH,
        revision=revision,
    )
    snapshot = McpConnectionSnapshot(
        connection_id="connection-direct",
        connection_revision=binding.connection_revision,
        server_id="server.synthetic",
        server_version="1.0.0",
        artifact_sha256="d" * 64,
        provider_id="provider.synthetic",
        account_ref=binding.account_key_hash,
        enabled=True,
        capability_ceiling=ceiling,
        allowed_tool_schemas=schemas,
        source_constraints={},
        destination_constraints={},
        authority_revision=revision,
    )
    return authority, snapshot


class _DirectRuntime(_Runtime):
    def __init__(self, binding: PluginToolBinding) -> None:
        super().__init__((binding,))
        self.binding = binding
        self.authority_service = _DirectAuthorityService(binding)
        self.connection_authority, self.connection_snapshot = _authority_pair(
            binding,
            PluginExecutionPolicy.DIRECT_TURN,
        )
        self.active_contributor: dict[str, object] = {}

    def resolve_plugin_authority(self, plan, *_args):
        self.active_contributor = {
            "source": "operator",
            "type": "command",
            "episode_id": plan.episode_id,
            "objective_sha256": plan.objective_sha256,
            "entailed_plan_digest": plugin_plan_digest(plan),
        }
        return {
            "connection_authority": self.connection_authority,
            "connection": self.connection_snapshot,
            "authority_service": self.authority_service,
            "active_contributor": self.active_contributor,
        }


class _LifecycleDirectRuntime(_DirectRuntime):
    """Production-shaped lifecycle result: the chat owner supplies the contributor."""

    def resolve_plugin_authority(self, plan, *_args):
        return (
            self.connection_authority,
            self.connection_snapshot,
            self.authority_service,
            None,
        )


def _model_step(
    candidate_id: str,
    step_id: str = "step-1",
    arguments: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "step_id": step_id,
        "candidate_id": candidate_id,
        "arguments": arguments or {"query": "events"},
        "consumes_step_ids": [],
    }


def test_read_only_natural_language_plan_executes_without_permission_prompt() -> None:
    binding = _binding(
        candidate="a",
        capability="list_events",
        title="List tomorrow events",
        risk=RiskClass.READ_ONLY,
        properties={"start": {"type": "string"}},
        required=["start"],
    )
    runtime = _Runtime((binding,))

    outcome = plan_and_execute_plugin_request(
        "show tomorrow's events",
        episode_id="episode-read",
        runtime_client=runtime,
    )

    assert outcome.status == "completed", outcome.failure_reason
    assert len(runtime.calls) == 1
    assert outcome.plan is not None
    assert outcome.step_results[0].status == "completed"


def test_slash_plugin_selector_reaches_github_account_capability(monkeypatch) -> None:
    input_schema = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }
    binding = replace(
        _binding(
            candidate="a",
            capability="get_me",
            title="Get GitHub account",
            risk=RiskClass.READ_ONLY,
            properties={"placeholder": {"type": "string"}},
            required=["placeholder"],
        ),
        plugin_id="github.official",
        app_id="github.official.mcp",
        capability="get_me",
        title="Get GitHub account",
        description="Read the connected GitHub account",
        tool_name="plugin__github.official__get_me",
        input_schema=input_schema,
        input_schema_sha256=sha256_hex(canonical_bytes(input_schema)),
    )
    monkeypatch.setattr(
        "auto_loop.execute.plugin_plan.run_plugin_plan_step",
        lambda *_args, **_kwargs: {"data": {"login": "operator"}},
    )
    runtime = _Runtime((binding,))

    outcome = plan_and_execute_plugin_request(
        "/plugin:github.official can you see my github account?",
        episode_id="episode-github-account",
        runtime_client=runtime,
        explicit_operation={"plugin_id": "github.official"},
    )

    assert outcome.status == "completed", outcome.failure_reason
    assert outcome.plan is not None
    assert outcome.plan.steps[0].candidate_id == binding.candidate_id
    assert outcome.plugin_result is not None
    assert outcome.plugin_result["capability"] == "get_me"


def test_closed_plugin_runtime_failure_is_not_rewrapped_as_a_planner_failure() -> None:
    assert _screen_failure(PluginRuntimeError("plugin_result_invalid")) == "plugin_result_invalid"


def test_plugin_id_selector_limits_generic_planning_without_privileged_fields() -> None:
    selected = _binding(
        candidate="a",
        capability="list_events",
        title="List tomorrow events",
        risk=RiskClass.READ_ONLY,
        properties={"start": {"type": "string"}},
        required=["start"],
    )
    other = replace(selected, candidate_id="b" * 64, plugin_id="plugin.other")
    runtime = _Runtime((selected, other))

    outcome = plan_and_execute_plugin_request(
        "show tomorrow's events",
        episode_id="episode-plugin-selector",
        runtime_client=runtime,
        explicit_operation={"plugin_id": "plugin.synthetic"},
    )

    assert outcome.status == "completed", outcome.failure_reason
    assert outcome.plan is not None
    assert outcome.plan.steps[0].candidate_id == selected.candidate_id
    assert len(runtime.calls) == 1


def test_fresh_catalog_recompile_rejects_changed_authority_before_call() -> None:
    initial = _binding(
        candidate="a",
        capability="list_events",
        title="List events",
        risk=RiskClass.READ_ONLY,
        properties={"start": {"type": "string"}},
        required=["start"],
    )
    changed = _binding(
        candidate="a",
        capability="list_events",
        title="List events",
        risk=RiskClass.READ_ONLY,
        properties={"start": {"type": "string", "maxLength": 64}},
        required=["start"],
    )
    runtime = _ChangingCatalogRuntime(initial, changed)

    outcome = plan_and_execute_plugin_request(
        "show tomorrow's events",
        episode_id="episode-recompile",
        runtime_client=runtime,
    )

    assert outcome.status == "failed"
    assert outcome.failure_reason == "plugin_catalog_changed"
    assert runtime.calls == []


def test_write_plan_requests_permission_before_any_provider_call() -> None:
    binding = _binding(
        candidate="b",
        capability="create_issue",
        title="Create issue",
        risk=RiskClass.ADDITIVE_WRITE,
        properties={"summary": {"type": "string"}},
        required=["summary"],
    )
    runtime = _Runtime((binding,))

    class PermissionStore:
        def issue(self, plan, **_kwargs):
            return PluginPermissionRequest(
                request_id="e" * 48,
                episode_id=plan.episode_id,
                prior_episode_id=plan.episode_id,
                task_id="task-synthetic",
                plan_id=plan.plan_id,
                account_key_hash=binding.account_key_hash,
                plan_digest=plugin_plan_digest(plan),
                authority_digest=plan.authority_digest,
                display=PluginPermissionDisplay(
                    plugin_apps=(
                        (
                            binding.plugin_id,
                            "Synthetic",
                            binding.app_id,
                            "Synthetic",
                        ),
                    ),
                    effect_summary="Create data",
                    ordered_operations=(binding.title,),
                    audience="",
                    destination="",
                    crosses_providers=False,
                    field_names=("summary",),
                    risks=(RiskClass.ADDITIVE_WRITE.value,),
                ),
                expires_monotonic=100.0,
                revision="f" * 64,
            )

    runtime.permission_store = PermissionStore()
    outcome = plan_and_execute_plugin_request(
        "create an issue from this summary",
        episode_id="episode-write",
        runtime_client=runtime,
    )

    assert outcome.status == "permission_required", outcome.failure_reason
    assert outcome.permission_request is not None
    assert runtime.calls == []


def test_direct_turn_authority_executes_once_without_duplicate_prompt() -> None:
    binding = _binding(
        candidate="b",
        capability="create_issue",
        title="Create issue",
        risk=RiskClass.ADDITIVE_WRITE,
        properties={"summary": {"type": "string"}},
        required=["summary"],
    )
    runtime = _DirectRuntime(binding)

    outcome = plan_and_execute_plugin_request(
        "create an issue from this summary",
        episode_id="episode-direct",
        runtime_client=runtime,
    )

    assert outcome.status == "completed", outcome.failure_reason
    assert len(runtime.calls) == 1
    assert runtime.authority_service.calls == 1
    # The unmodified runtime gated the operation on that authority before the call.
    assert runtime.authority_service.evaluations == [
        ("direct_turn", binding.tool_name)
    ]


def test_direct_turn_uses_the_live_chat_operator_contributor_when_lifecycle_has_none() -> None:
    binding = _binding(
        candidate="b",
        capability="create_issue",
        title="Create issue",
        risk=RiskClass.ADDITIVE_WRITE,
        properties={"summary": {"type": "string"}},
        required=["summary"],
    )
    runtime = _LifecycleDirectRuntime(binding)

    outcome = plan_and_execute_plugin_request(
        "create an issue from this summary",
        episode_id="episode-direct-live",
        runtime_client=runtime,
        active_contributor={"source": "operator", "type": "command"},
    )

    assert outcome.status == "completed", outcome.failure_reason
    assert runtime.authority_service.calls == 1


def test_multi_step_write_requests_permission_once_before_any_call() -> None:
    first = _binding(
        candidate="a",
        capability="lookup",
        title="Lookup events",
        risk=RiskClass.READ_ONLY,
        properties={"priority": {"type": "integer"}},
        required=["priority"],
        account="b",
    )
    second = _binding(
        candidate="b",
        capability="create_issue",
        title="Create issue",
        risk=RiskClass.ADDITIVE_WRITE,
        properties={"item": {"type": "string"}},
        required=["item"],
        account="b",
    )
    runtime = _TwoStepWriteRuntime((first, second))
    issue_calls = 0
    issue_accounts: list[str] = []

    class PermissionStore:
        def issue(self, plan, **kwargs):
            nonlocal issue_calls
            issue_calls += 1
            issue_accounts.append(kwargs["account_key_hash"])
            return PluginPermissionRequest(
                request_id="e" * 48,
                episode_id=plan.episode_id,
                prior_episode_id=plan.episode_id,
                task_id="task-synthetic",
                plan_id=plan.plan_id,
                account_key_hash=first.account_key_hash,
                plan_digest=plugin_plan_digest(plan),
                authority_digest=plan.authority_digest,
                display=PluginPermissionDisplay(
                    plugin_apps=(
                        (first.plugin_id, "Synthetic", first.app_id, "Synthetic"),
                        (second.plugin_id, "Synthetic", second.app_id, "Synthetic"),
                    ),
                    effect_summary="Read data | Create data",
                    ordered_operations=(first.title, second.title),
                    audience="",
                    destination="",
                    crosses_providers=False,
                    field_names=("priority", "item"),
                    risks=(
                        RiskClass.READ_ONLY.value,
                        RiskClass.ADDITIVE_WRITE.value,
                    ),
                ),
                expires_monotonic=100.0,
                revision="f" * 64,
            )

    runtime.permission_store = PermissionStore()
    outcome = plan_and_execute_plugin_request(
        "lookup events and create issue",
        episode_id="episode-multi-write",
        runtime_client=runtime,
    )

    assert outcome.status == "permission_required", (
        outcome.failure_reason,
        runtime.generation_calls,
    )
    assert issue_calls == 1
    assert issue_accounts == [second.account_key_hash]
    assert runtime.calls == []


def test_mixed_account_plan_fails_closed_before_permission_issue() -> None:
    first = _binding(
        candidate="a",
        capability="lookup",
        title="Lookup events",
        risk=RiskClass.READ_ONLY,
        properties={"priority": {"type": "integer"}},
        required=["priority"],
        account="b",
    )
    second = _binding(
        candidate="b",
        capability="create_issue",
        title="Create issue",
        risk=RiskClass.ADDITIVE_WRITE,
        properties={"item": {"type": "string"}},
        required=["item"],
        account="d",
    )
    runtime = _TwoStepWriteRuntime((first, second))

    outcome = plan_and_execute_plugin_request(
        "lookup events and create issue",
        episode_id="episode-mixed-account",
        runtime_client=runtime,
    )

    assert outcome.status == "failed"
    # The planner rejects cross-account data flow before a permission request
    # can be issued when the destination is not explicitly transfer-risk.
    assert outcome.failure_reason == "plugin_plan_repair_exhausted"
    assert runtime.generation_calls == 2
    assert runtime.calls == []


class _RevokedAuthorityService:
    """Direct-turn authority that preflight can see and dispatch cannot mint."""

    def __init__(self) -> None:
        self.calls = 0

    def authorize_direct_turn(self, _plan, **_kwargs):
        self.calls += 1
        return None

    def evaluate_operation(self, *_args, **_kwargs):
        raise AssertionError("an unauthorised step must never reach the provider")


class _RevokedAuthorityRuntime(_TwoStepWriteRuntime):
    def __init__(self, bindings: tuple[PluginToolBinding, ...]) -> None:
        super().__init__(bindings)
        self.authority_service = _RevokedAuthorityService()
        self.connection_authority, self.connection_snapshot = _authority_pair(
            bindings[1],
            PluginExecutionPolicy.DIRECT_TURN,
        )

    def resolve_plugin_authority(self, plan, *_args):
        return {
            "connection_authority": self.connection_authority,
            "connection": self.connection_snapshot,
            "authority_service": self.authority_service,
            "active_contributor": {
                "source": "operator",
                "type": "command",
                "episode_id": plan.episode_id,
                "objective_sha256": plan.objective_sha256,
                "entailed_plan_digest": plugin_plan_digest(plan),
            },
        }


def test_authority_lost_after_first_dispatch_fails_closed_with_prior_results() -> None:
    """A grant that dies mid-plan is a failure, never a fresh prompt."""
    first = _binding(
        candidate="a",
        capability="lookup",
        title="Lookup events",
        risk=RiskClass.READ_ONLY,
        properties={"priority": {"type": "integer"}},
        required=["priority"],
    )
    second = _binding(
        candidate="b",
        capability="create_issue",
        title="Create issue",
        risk=RiskClass.ADDITIVE_WRITE,
        properties={"item": {"type": "string"}},
        required=["item"],
    )
    runtime = _RevokedAuthorityRuntime((first, second))

    class _UnusedPermissionStore:
        def issue(self, *_args, **_kwargs):
            raise AssertionError("a dispatched plan must not re-prompt the operator")

    runtime.permission_store = _UnusedPermissionStore()

    outcome = plan_and_execute_plugin_request(
        "lookup events and create issue",
        episode_id="episode-authority-revoked",
        runtime_client=runtime,
    )

    assert outcome.status == "failed"
    assert outcome.failure_reason == "plugin_authority_missing_after_dispatch"
    assert outcome.permission_request is None
    # The read-only step really ran, so its result must survive the failure.
    assert len(outcome.step_results) == 1
    assert len(runtime.calls) == 1
    assert runtime.authority_service.calls == 1


def test_cross_step_arguments_use_typed_screened_output() -> None:
    first = _binding(
        candidate="a",
        capability="lookup",
        title="Lookup events",
        risk=RiskClass.READ_ONLY,
        properties={"query": {"type": "string"}},
        required=["query"],
    )
    second = _binding(
        candidate="b",
        capability="follow_up",
        title="Follow up event",
        risk=RiskClass.READ_ONLY,
        properties={"item": {"type": "string"}},
        required=["item"],
    )
    runtime = _TwoStepRuntime((first, second))

    outcome = plan_and_execute_plugin_request(
        "look up events and follow up",
        episode_id="episode-cross-step",
        runtime_client=runtime,
    )

    assert outcome.status == "completed", outcome.failure_reason
    assert len(runtime.calls) == 2
    assert runtime.calls[1][2] == {"item": "event"}


def test_model_invented_candidate_fails_closed_without_provider_call() -> None:
    binding = _binding(
        candidate="a",
        capability="list_events",
        title="List events",
        risk=RiskClass.READ_ONLY,
        properties={"priority": {"type": "integer"}},
        required=["priority"],
    )
    invalid = {
        "decision": "call_tool",
        "steps": [_model_step("e" * 64, arguments={"priority": 1})],
    }
    runtime = _ModelRuntime((binding,), [invalid, invalid])

    outcome = plan_and_execute_plugin_request(
        "look up events",
        episode_id="episode-model-candidate",
        runtime_client=runtime,
    )

    assert outcome.status == "failed"
    assert outcome.failure_reason == "plugin_plan_repair_exhausted"
    assert runtime.generation_calls == 2
    assert runtime.calls == []


def test_model_planner_uses_the_configured_brain_route() -> None:
    binding = _binding(
        candidate="a",
        capability="list_events",
        title="List events",
        risk=RiskClass.READ_ONLY,
        properties={"priority": {"type": "integer"}},
        required=["priority"],
    )
    decision = {
        "decision": "call_tool",
        "steps": [_model_step(binding.candidate_id, arguments={"priority": 1})],
    }
    runtime = _RecordingModelRuntime((binding,), [decision])

    outcome = plan_and_execute_plugin_request(
        "look up events",
        episode_id="episode-model-route",
        runtime_client=runtime,
    )

    assert outcome.status == "completed", outcome.failure_reason
    assert len(runtime.generation_kwargs) == 1
    assert runtime.generation_kwargs[0].get("local_only", False) is False


def test_model_planner_normalizes_a_bounded_proposal_wrapper() -> None:
    binding = _binding(
        candidate="a",
        capability="list_events",
        title="List events",
        risk=RiskClass.READ_ONLY,
        properties={"priority": {"type": "integer"}},
        required=["priority"],
    )
    proposal = {
        "proposal": {
            "candidate_id": binding.candidate_id,
            "capability": binding.capability,
            "input": {"priority": 1},
            "rationale": "bounded read-only operation",
        }
    }
    runtime = _ModelRuntime((binding,), [proposal])

    outcome = plan_and_execute_plugin_request(
        "look up events",
        episode_id="episode-model-proposal-wrapper",
        runtime_client=runtime,
    )

    assert outcome.status == "completed", outcome.failure_reason
    assert runtime.generation_calls == 1


def test_model_call_budget_caps_at_four_steps_and_one_repair() -> None:
    binding = _binding(
        candidate="a",
        capability="list_events",
        title="List events",
        risk=RiskClass.READ_ONLY,
        properties={"priority": {"type": "integer"}},
        required=["priority"],
    )
    too_many = {
        "decision": "call_tool",
        "steps": [
            _model_step("a" * 64, f"step-{index}", {"priority": 1}) for index in range(5)
        ],
    }
    runtime = _ModelRuntime((binding,), [too_many, too_many])

    outcome = plan_and_execute_plugin_request(
        "look up events",
        episode_id="episode-model-budget",
        runtime_client=runtime,
    )

    assert outcome.status == "failed"
    assert outcome.failure_reason == "plugin_plan_repair_exhausted"
    assert runtime.generation_calls == 2
    assert runtime.calls == []


def test_ambiguous_explicit_plugin_candidate_asks_operator() -> None:
    first = _binding(
        candidate="a",
        capability="list_events",
        title="List events",
        risk=RiskClass.READ_ONLY,
        properties={"query": {"type": "string"}},
        required=["query"],
    )
    second = _binding(
        candidate="b",
        capability="list_events",
        title="List events",
        risk=RiskClass.READ_ONLY,
        properties={"query": {"type": "string"}},
        required=["query"],
    )
    runtime = _Runtime((first, second))

    outcome = plan_and_execute_plugin_request(
        "look up events",
        episode_id="episode-ambiguous",
        runtime_client=runtime,
        explicit_operation={
            "plugin_id": first.plugin_id,
            "app_id": first.app_id,
            "capability": first.capability,
            "arguments": {"query": "events"},
        },
    )

    assert outcome.status == "ask_user"
    assert outcome.failure_reason == "Which plugin or connected account should I use?"
    assert runtime.calls == []


def test_missing_connection_returns_settings_input_state() -> None:
    binding = _binding(
        candidate="a",
        capability="list_events",
        title="List events",
        risk=RiskClass.READ_ONLY,
        properties={"start": {"type": "string"}},
        required=["start"],
    )

    class MissingConnection(_Runtime):
        def resolve_plugin_authority(self, *_args):
            raise ValueError("plugin_connection_not_found")

    runtime = MissingConnection((binding,))
    outcome = plan_and_execute_plugin_request(
        "show tomorrow's events",
        episode_id="episode-settings",
        runtime_client=runtime,
    )

    assert outcome.status == "ask_user"
    assert outcome.failure_reason == "plugin_settings_required"
    assert runtime.calls == []


def test_planning_cancellation_identity_is_preserved() -> None:
    binding = _binding(
        candidate="c",
        capability="create_issue",
        title="Create issue",
        risk=RiskClass.ADDITIVE_WRITE,
        properties={"priority": {"type": "integer"}},
        required=["priority"],
    )

    class CancelledPlanning(_Runtime):
        def generate_completion(self, *_args, **_kwargs):
            error = KeyboardInterrupt("planning cancelled")
            raise error

    with pytest.raises(KeyboardInterrupt, match="planning cancelled"):
        plan_and_execute_plugin_request(
            "create an issue from this summary",
            episode_id="episode-cancel",
            runtime_client=CancelledPlanning((binding,)),
        )


def test_execution_cancellation_identity_is_preserved() -> None:
    binding = _binding(
        candidate="d",
        capability="list_events",
        title="List events",
        risk=RiskClass.READ_ONLY,
        properties={"start": {"type": "string"}},
        required=["start"],
    )

    cancelled = _Runtime(
        (binding,),
        backend=_RaisingBackend(KeyboardInterrupt("execution cancelled")),
    )

    with pytest.raises(KeyboardInterrupt, match="execution cancelled"):
        plan_and_execute_plugin_request(
            "show tomorrow's events",
            episode_id="episode-cancel-execution",
            runtime_client=cancelled,
        )


def test_execution_asyncio_cancellation_identity_is_preserved() -> None:
    binding = _binding(
        candidate="d",
        capability="list_events",
        title="List events",
        risk=RiskClass.READ_ONLY,
        properties={"start": {"type": "string"}},
        required=["start"],
    )
    error = asyncio.CancelledError()
    cancelled = _Runtime((binding,), backend=_RaisingBackend(error))

    with pytest.raises(asyncio.CancelledError) as raised:
        plan_and_execute_plugin_request(
            "show tomorrow's events",
            episode_id="episode-cancel-asyncio",
            runtime_client=cancelled,
        )

    assert raised.value is error


def test_execution_failure_preserves_provider_cause() -> None:
    binding = _binding(
        candidate="d",
        capability="list_events",
        title="List events",
        risk=RiskClass.READ_ONLY,
        properties={"start": {"type": "string"}},
        required=["start"],
    )

    failing = _Runtime(
        (binding,),
        backend=_RaisingBackend(PluginRuntimeError("provider response malformed")),
    )

    outcome = plan_and_execute_plugin_request(
        "show tomorrow's events",
        episode_id="episode-failure-cause",
        runtime_client=failing,
    )

    assert outcome.status == "failed"
    assert "PluginRuntimeError('provider response malformed')" in (
        outcome.failure_reason or ""
    )


def test_unreadable_catalog_fails_closed_with_the_cause_kept() -> None:
    """Global A routes a plugin-shaped turn here when the catalog cannot answer.

    AUTO must not report "no eligible operation": it fails, and the cause rides
    out on ``failure_reason``, which the AUTO cycle stores as ``plugin_failure``
    episode content.
    """

    class _UnreadableCatalog(_Runtime):
        def plugin_tool_bindings(self):
            raise OSError("registry unreadable")

    outcome = plan_and_execute_plugin_request(
        "show tomorrow's events",
        episode_id="episode-catalog-unreadable",
        runtime_client=_UnreadableCatalog(()),
    )

    assert outcome.status == "failed"
    assert "registry unreadable" in (outcome.failure_reason or "")


def test_deterministic_authority_digest_covers_all_retrieved_candidates() -> None:
    winner = _binding(
        candidate="a",
        capability="list_events",
        title="List tomorrow events",
        risk=RiskClass.READ_ONLY,
        properties={"start": {"type": "string"}},
        required=["start"],
    )
    runner_up = _binding(
        candidate="b",
        capability="events_archive",
        title="Events archive",
        risk=RiskClass.READ_ONLY,
        properties={"start": {"type": "string"}},
        required=["start"],
    )
    runtime = _Runtime((winner, runner_up))

    outcome = plan_and_execute_plugin_request(
        "show tomorrow's events",
        episode_id="episode-two-candidates",
        runtime_client=runtime,
    )

    assert outcome.status == "completed", outcome.failure_reason
    assert outcome.plan is not None
    assert outcome.plan.steps[0].candidate_id == winner.candidate_id
    assert len(runtime.calls) == 1


def test_execution_catalog_digest_matches_explicit_kind_planning_authority() -> None:
    binding = _binding(
        candidate="a",
        capability="list_events",
        title="List events",
        risk=RiskClass.READ_ONLY,
        properties={"start": {"type": "string"}},
        required=["start"],
    )
    expected = sha256_hex(
        canonical_bytes(
            [
                {
                    "candidate_id": binding.candidate_id,
                    "capability_kind": binding.capability_kind,
                    "input": binding.input_schema_sha256,
                    "output": binding.output_schema_sha256,
                    "connection": binding.connection_revision,
                }
            ]
        )
    )

    assert _catalog_digest((binding,)) == expected


class _StandingAuthorityService(_DirectAuthorityService):
    def __init__(self, binding: PluginToolBinding) -> None:
        super().__init__(binding)
        self.standing_calls = 0

    def authorize_standing_grant(self, plan, **kwargs):
        self.standing_calls += 1
        authority = self.authorize_direct_turn(
            plan,
            connection=kwargs["connection"],
            connection_snapshot=getattr(
                self,
                "connection_snapshot",
                kwargs.get("connection_snapshot"),
            ),
        )
        return replace(
            authority,
            authority_kind="standing_grant",
            direct_execution=False,
            standing_execution=True,
            job_id=kwargs["job_id"],
            job_revision=kwargs["job_revision"],
        )


class _StandingRuntime(_DirectRuntime):
    def __init__(self, binding: PluginToolBinding) -> None:
        super().__init__(binding)
        self.authority_service = _StandingAuthorityService(binding)
        self.authority_service.connection_snapshot = self.connection_snapshot
        connection_revision = plugin_connection_authority_revision(
            plugin_id=binding.plugin_id,
            app_id=binding.app_id,
            account_key_hash=binding.account_key_hash,
            connection_revision=binding.connection_revision,
            provider_scopes=("scope",),
            capability_ceiling=(f"tool:{binding.capability}",),
            allowed_tool_schemas={
                binding.capability: binding.input_schema_sha256,
                binding.tool_name: binding.input_schema_sha256,
            },
            source_constraints={},
            destination_constraints={},
            execution_policy=PluginExecutionPolicy.CONFIRM_EACH,
            authority_kind=PluginConnectionAuthorityKind.OAUTH,
        )
        self.connection_authority = PluginConnectionAuthority(
            plugin_id=binding.plugin_id,
            app_id=binding.app_id,
            account_key_hash=binding.account_key_hash,
            connection_revision=binding.connection_revision,
            provider_scopes=("scope",),
            capability_ceiling=(f"tool:{binding.capability}",),
            allowed_tool_schemas={
                binding.capability: binding.input_schema_sha256,
                binding.tool_name: binding.input_schema_sha256,
            },
            source_constraints={},
            destination_constraints={},
            execution_policy=PluginExecutionPolicy.CONFIRM_EACH,
            authority_kind=PluginConnectionAuthorityKind.OAUTH,
            revision=connection_revision,
        )
        self.standing_grant = {
            "authority_id": "e" * 64,
            "job_id": "f" * 64,
            "job_revision": "1" * 64,
            "execution_revision": "2" * 64,
        }

    def resolve_plugin_authority(self, plan, *_args):
        context = super().resolve_plugin_authority(plan, *_args)
        context["standing_grant"] = self.standing_grant
        return context


def test_standing_grant_authority_is_authorized_before_provider_call() -> None:
    binding = _binding(
        candidate="b",
        capability="create_issue",
        title="Create issue",
        risk=RiskClass.ADDITIVE_WRITE,
        properties={"summary": {"type": "string"}},
        required=["summary"],
    )
    runtime = _StandingRuntime(binding)

    outcome = plan_and_execute_plugin_request(
        "create an issue from this summary",
        episode_id="episode-standing",
        runtime_client=runtime,
    )

    assert outcome.status == "completed", outcome.failure_reason
    assert runtime.authority_service.standing_calls == 1
    assert len(runtime.calls) == 1


def test_confirm_each_without_standing_grant_requests_permission_before_call() -> None:
    binding = _binding(
        candidate="b",
        capability="create_issue",
        title="Create issue",
        risk=RiskClass.ADDITIVE_WRITE,
        properties={"summary": {"type": "string"}},
        required=["summary"],
    )
    runtime = _StandingRuntime(binding)
    runtime.standing_grant = None

    class PermissionStore:
        def issue(self, plan, **_kwargs):
            return PluginPermissionRequest(
                request_id="e" * 48,
                episode_id=plan.episode_id,
                prior_episode_id=plan.episode_id,
                task_id="task-synthetic",
                plan_id=plan.plan_id,
                account_key_hash=binding.account_key_hash,
                plan_digest=plugin_plan_digest(plan),
                authority_digest=plan.authority_digest,
                display=PluginPermissionDisplay(
                    plugin_apps=((binding.plugin_id, "Synthetic", binding.app_id, "Synthetic"),),
                    effect_summary="Create data",
                    ordered_operations=(binding.title,),
                    audience="",
                    destination="",
                    crosses_providers=False,
                    field_names=("summary",),
                    risks=(RiskClass.ADDITIVE_WRITE.value,),
                ),
                expires_monotonic=100.0,
                revision="f" * 64,
            )

    runtime.permission_store = PermissionStore()
    outcome = plan_and_execute_plugin_request(
        "create an issue from this summary",
        episode_id="episode-confirm-each",
        runtime_client=runtime,
    )

    assert outcome.status == "permission_required", outcome.failure_reason
    assert outcome.permission_request is not None
    assert runtime.calls == []


def test_plan_override_ignores_unused_catalog_bindings() -> None:
    binding = _binding(
        candidate="b",
        capability="lookup",
        title="Lookup events",
        risk=RiskClass.READ_ONLY,
        properties={"query": {"type": "string"}},
        required=["query"],
    )
    unused = _binding(
        candidate="c",
        capability="create_issue",
        title="Create issue",
        risk=RiskClass.ADDITIVE_WRITE,
        properties={"summary": {"type": "string"}},
        required=["summary"],
    )
    plan = _deterministic_plan(
        _objective_receipt("lookup events"),
        episode_id="episode-override-unused",
        candidates=(binding,),
    )
    assert plan is not None
    runtime = _Runtime((binding, unused))

    outcome = plan_and_execute_plugin_request(
        "",
        episode_id="episode-override-unused",
        runtime_client=runtime,
        plan_override=plan,
    )

    assert outcome.status == "completed", outcome.failure_reason
    assert len(runtime.calls) == 1


def test_approved_grant_is_consumed_at_first_dispatch_boundary() -> None:
    binding = _binding(
        candidate="b",
        capability="create_issue",
        title="Create issue",
        risk=RiskClass.ADDITIVE_WRITE,
        properties={"summary": {"type": "string"}},
        required=["summary"],
    )
    runtime = _DirectRuntime(binding)
    plan = _deterministic_plan(
        _objective_receipt("create an issue from this summary"),
        episode_id="episode-grant",
        candidates=(binding,),
    )
    assert plan is not None
    authority = runtime.authority_service.authorize_direct_turn(
        plan,
        connection=runtime.connection_authority,
        connection_snapshot=runtime.connection_snapshot,
        candidate_id=binding.candidate_id,
        step_id=plan.steps[0].step_id,
        active_contributor={
            "source": "operator",
            "type": "command",
            "episode_id": plan.episode_id,
            "objective_sha256": plan.objective_sha256,
            "entailed_plan_digest": plugin_plan_digest(plan),
        },
    )

    class PermissionStore:
        def __init__(self) -> None:
            self.calls = 0

        def consume(self, *_args, **_kwargs):
            self.calls += 1
            assert runtime.calls == []
            return (authority,)

    store = PermissionStore()
    runtime.permission_store = store
    grant = SimpleNamespace(
        grant_id="a" * 48,
        request_id="b" * 48,
        episode_id=plan.episode_id,
        plan_digest=plugin_plan_digest(plan),
        authority_digest=plan.authority_digest,
        revision="c" * 64,
    )

    outcome = plan_and_execute_plugin_request(
        "create an issue from this summary",
        episode_id=plan.episode_id,
        runtime_client=runtime,
        grant=grant,
    )

    assert outcome.status == "completed", outcome.failure_reason
    assert store.calls == 1
    assert len(runtime.calls) == 1


def test_quarantined_provider_projection_cannot_feed_next_step(monkeypatch) -> None:
    binding = _binding(
        candidate="a",
        capability="list_events",
        title="List events",
        risk=RiskClass.READ_ONLY,
        properties={"start": {"type": "string"}},
        required=["start"],
    )
    runtime = _Runtime((binding,))
    monkeypatch.setattr(
        "auto_loop.execute.plugin_plan._guardian_result",
        lambda *_args, **_kwargs: (
            {"item": "[QUARANTINED_EXTERNAL_TEXT]"},
            {"quarantined": 1},
        ),
    )

    outcome = plan_and_execute_plugin_request(
        "show tomorrow's events",
        episode_id="episode-quarantine",
        runtime_client=runtime,
    )

    assert outcome.status == "failed"
    assert outcome.failure_reason == "plugin_result_quarantined"


def test_permission_continuation_revalidates_then_approves_grant(monkeypatch) -> None:
    calls: list[object] = []
    decision = SimpleNamespace(
        request_id="a" * 48,
        expected_revision="b" * 64,
        decision="allow",
    )
    lineage = SimpleNamespace(
        prior_episode_id="episode-continuation",
        plan_digest="c" * 64,
    )
    continuation = SimpleNamespace(decision=decision, lineage=lineage)

    def revalidate(contributor):
        calls.append(contributor)
        return continuation

    monkeypatch.setattr(
        "pressure_loop.plugin_continuations.plugin_permission_continuation_from_contributor",
        revalidate,
    )

    class PermissionStore:
        def approve(self, request_id, *, expected_revision):
            assert request_id == decision.request_id
            assert expected_revision == decision.expected_revision
            return PluginPlanGrant(
                grant_id="d" * 48,
                request_id=decision.request_id,
                episode_id=lineage.prior_episode_id,
                plan_digest=lineage.plan_digest,
                authority_digest="e" * 64,
                revision="f" * 64,
            )

    runtime = SimpleNamespace(permission_store=PermissionStore())
    grant = plugin_plan_grant_from_contributor(runtime, object())

    assert len(calls) == 1
    assert grant.grant_id == "d" * 48


def test_cancel_continuation_fail_closes_store_and_cannot_allow_later(
    tmp_path, monkeypatch
) -> None:
    from tests.mcp_platform.test_plugin_permissions import (
        _Clock,
        _Random,
        _issue,
        _store,
    )

    store = _store(tmp_path / "permissions.json", _Clock(), _Random())
    request = _issue(store)
    decision = SimpleNamespace(
        request_id=request.request_id,
        expected_revision=request.revision,
        decision="cancel",
    )
    monkeypatch.setattr(
        "pressure_loop.plugin_continuations.plugin_permission_continuation_from_contributor",
        lambda _contributor: SimpleNamespace(
            decision=decision,
            lineage=SimpleNamespace(prior_episode_id="episode-1", plan_digest="c" * 64),
        ),
    )

    with pytest.raises(ValueError, match="plugin_permission_cancelled"):
        plugin_plan_grant_from_contributor(
            SimpleNamespace(permission_store=store),
            object(),
        )

    assert store.pending() == ()
    with pytest.raises(ValueError, match="cancelled|already_decided|revision"):
        store.approve(request.request_id, expected_revision=request.revision)


def test_a_trailing_full_stop_does_not_hide_the_operators_best_word() -> None:
    """The last word of a sentence is usually the most specific one.

    `_score` replaced only apostrophes, so "…list my calendars." carried its
    full stop into the word set and matched nothing, while "calendar" appearing
    bare earlier in the same sentence did. Measured live on 2026-09-08 against
    the shipped Google Calendar package, that scored `list_events` ABOVE
    `list_calendars` for a request that named calendars twice.
    """
    from auto_loop.execute.plugin_plan import _score

    calendars = _binding(
        candidate="a",
        capability="list_calendars",
        title="List calendars",
        risk=RiskClass.READ_ONLY,
        properties={},
        required=[],
    )
    events = _binding(
        candidate="b",
        capability="list_events",
        title="List calendar events",
        risk=RiskClass.READ_ONLY,
        properties={},
        required=[],
    )

    punctuated = "Use the Google Calendar plugin to list my calendars."
    assert _score(punctuated, calendars) > _score(punctuated, events)
    # And it reads the same with or without the punctuation.
    assert _score(punctuated, calendars) == _score(
        "Use the Google Calendar plugin to list my calendars", calendars
    )


def test_a_capability_name_is_matchable_at_all() -> None:
    """`list_calendars` was one whitespace token, so a tool's own name never matched."""
    from auto_loop.execute.plugin_plan import _score

    binding = _binding(
        candidate="a",
        capability="search_threads",
        title="Untitled",
        risk=RiskClass.READ_ONLY,
        properties={},
        required=[],
    )

    assert _score("search threads for me", binding) > 0


def test_naming_a_plugin_narrows_the_candidates_and_never_widens_them() -> None:
    """The most explicit thing an operator can say used to be pure noise.

    `_score` never looked at which plugin a tool belongs to, so "use the Google
    Calendar plugin" earned that plugin nothing while its extra words dragged
    unrelated tools up on generic verbs -- six-way tie, four of them Neon's
    `list_*`. Narrowing can only REMOVE candidates, so it cannot promote a tool
    the scorer would have passed over, nor reach a plugin nobody named.
    """
    from types import SimpleNamespace

    from auto_loop.execute.plugin_plan import _named_plugin

    # `_named_plugin` reads one field, so this is its whole input surface.
    candidates = tuple(
        SimpleNamespace(plugin_id=plugin_id)
        for plugin_id in (
            "google.workspace.calendar",
            "google.workspace.gmail",
            "neon.official",
            "slack.official",
        )
    )

    assert _named_plugin("use the google calendar plugin to list", candidates) == (
        "google.workspace.calendar"
    )
    assert _named_plugin("use the slack plugin", candidates) == "slack.official"
    # Naming none leaves the field alone.
    assert _named_plugin("list my things", candidates) is None
    # A word two plugins share is not a disambiguation either.
    assert _named_plugin("google", candidates) is None
    # Structural words in an id name no provider.
    assert _named_plugin("official workspace mcp", candidates) is None


def test_narrowing_by_plugin_name_does_not_move_the_authority_basis() -> None:
    """A plan is authorized against the candidate set it was planned from.

    `plan.authority_digest` hashes the candidates handed to
    `parse_plugin_plan_decision`, and before running any step the executor
    recomputes that digest over the CALLER's full candidate set. So narrowing
    the field to score it must not rebind `candidates` -- doing so authorized
    the plan against nine calendar tools and checked it against all 431, and
    every resolved plan then died at execution with `plugin_catalog_changed`.

    Unit tests passed throughout, because none of them ran the executor's
    recomputation; only the live chat turn showed it.
    """
    from auto_loop.execute.plugin_plan import _catalog_digest, _deterministic_plan
    from mcp_platform.ingress import screen_mcp_planner_text

    wanted = _binding(
        candidate="a",
        capability="list_calendars",
        title="List calendars",
        risk=RiskClass.READ_ONLY,
        properties={},
        required=[],
    )
    other = _binding(
        candidate="b",
        capability="list_functions",
        title="List functions",
        risk=RiskClass.READ_ONLY,
        properties={},
        required=[],
    )
    candidates = (wanted, other)

    plan = _deterministic_plan(
        screen_mcp_planner_text("list calendars", source="operator-objective"),
        episode_id="episode-authority",
        candidates=candidates,
    )

    assert plan is not None
    assert plan.steps[0].candidate_id == wanted.candidate_id
    # The digest covers everything the caller offered, which is what the
    # executor will recompute it against.
    assert plan.authority_digest == _catalog_digest(candidates)
