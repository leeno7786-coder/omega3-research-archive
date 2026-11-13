"""Opt-in live Mistral coverage for the provider-neutral plugin pipeline."""

from __future__ import annotations

import os
from contextlib import nullcontext
from dataclasses import replace
from typing import Any

import pytest

from auto_loop.execute.plugin_plan import plan_and_execute_plugin_request
from mcp_platform.package_contracts import RiskClass
from runtime.client import RuntimeClient


pytestmark = pytest.mark.skipif(
    not os.environ.get("OMEGA_LIVE_MISTRAL_PLUGIN"),
    reason="set OMEGA_LIVE_MISTRAL_PLUGIN=1 for the networked Mistral smoke",
)


class _LiveRuntime:
    """Mistral cognition plus the real provider-neutral plugin runtime."""

    def __init__(
        self,
        client: RuntimeClient,
        plugin_runtime: object,
        binding: object,
        *,
        permission_store: object | None = None,
        connection_snapshot: object | None = None,
        authority_service: object | None = None,
    ) -> None:
        self._client = client
        self.plugin_runtime = plugin_runtime
        self._binding = binding
        self.permission_store = permission_store
        self.connection_snapshot = connection_snapshot
        self.authority_service = authority_service
        self.model_calls: list[dict[str, Any]] = []

    def generate_completion(self, prompt: str, **kwargs: Any) -> dict[str, Any] | None:
        call = {"prompt": prompt, **kwargs}
        self.model_calls.append(call)
        response = self._client.generate_completion(prompt, **kwargs)
        call["response"] = response
        return response

    def plugin_tool_bindings(self) -> tuple[object, ...]:
        return (self._binding,)

    def plugin_execution_mutation_scope(self):
        return nullcontext()

    def resolve_plugin_authority(self, *_args: Any) -> dict[str, Any]:
        return {
            "connection": self.connection_snapshot,
            "authority_service": self.authority_service,
        }


def test_mistral_plans_and_executes_a_real_plugin_runtime_operation(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OMEGA_MIND_BACKEND", "cloud")
    monkeypatch.setenv("OMEGA_MIND_PROVIDER", "mistral")
    monkeypatch.setenv("OMEGA_MIND_BASE_URL", "https://api.mistral.ai/v1")
    monkeypatch.setenv("OMEGA_MIND_MODEL", "mistral-small-latest")
    monkeypatch.setenv("OMEGA_GENERATE_RETRIES", "0")
    monkeypatch.setenv("OMEGA_BRAIN_READ_TIMEOUT", "120")

    from tests.auto_loop.test_plugin_plan import _binding
    from tests.mcp_platform.test_plugin_runtime import _runtime

    binding = replace(
        _binding(
            candidate="a",
            capability="search_files",
            title="Search Google Drive files",
            risk=RiskClass.READ_ONLY,
            properties={"query": {"type": "string"}},
            required=["query"],
            account="c",
        ),
        plugin_id="google.workspace.drive",
        app_id="google.workspace.drive.mcp",
        tool_name="search_files",
    )
    plugin_runtime, backend = _runtime(tmp_path)
    # The test backend is the provider transport seam. Return the same small
    # screened object used by the provider-neutral acceptance fixtures.
    def call_tool(app_id, name, arguments):
        backend.calls.append((app_id, name, dict(arguments)))
        return {"item": "quarterly-plan.pdf"}

    backend.call_tool_sync = call_tool
    runtime = _LiveRuntime(RuntimeClient(), plugin_runtime, binding)

    # The normal planner prefers a deterministic plan where possible. This
    # smoke intentionally exercises the live model-plan contract.
    from auto_loop.execute import plugin_plan as plugin_plan_module

    deterministic_plan = plugin_plan_module._deterministic_plan
    monkeypatch.setattr(
        plugin_plan_module,
        "_deterministic_plan",
        lambda *_args, **_kwargs: None,
    )
    outcome = plan_and_execute_plugin_request(
        "Use the Google Drive plugin to search files for the quarterly plan",
        episode_id="live-mistral-plugin",
        runtime_client=runtime,
        work_kind="operator_turn",
        cycle_id="live-mistral-plugin-chat",
    )

    assert runtime.model_calls, "the live Mistral planner was not called"
    assert outcome.status == "completed", (
        outcome.failure_reason,
        [call.get("response") for call in runtime.model_calls],
        backend.calls,
        getattr(plugin_runtime, "_catalog_provider", "missing"),
    )
    assert outcome.plan is not None
    assert outcome.plan.steps[0].candidate_id == binding.candidate_id
    assert outcome.plugin_result is not None
    assert len(backend.calls) == 1

    from auto_loop.execute.run import execute
    from tests.core.test_auto_execution import _mem, _operator_pp, _pack

    # The model-planning path was exercised above. Use Omega's deterministic
    # selector for the response-delivery leg so this assertion isolates AUTO's
    # live result text rather than spending another network call on selection.
    monkeypatch.setattr(plugin_plan_module, "_deterministic_plan", deterministic_plan)

    chat_result = execute(
        _operator_pp("search files for the quarterly plan"),
        "TEXT_FAST",
        _pack(
            action="PLUGIN_OPERATION",
            execution_inputs={
                "plugin_objective": "Use the Google Drive plugin to search files for the quarterly plan"
            },
        ),
        _mem(),
        "live-mistral-plugin-chat",
        runtime_client=runtime,
        opened_by="operator_query",
        chat_cycle_id="live-mistral-plugin-chat",
        work_kind="operator_turn",
    )
    assert chat_result["failure_kind"] is None, (chat_result, runtime.model_calls[-2:])
    # Completed app results are now interpreted by the selected model, not
    # delivered using fixed completion wording.
    assert chat_result["outputs"]["response_text"].strip()
    assert "quarterly-plan.pdf" in chat_result["outputs"]["response_text"]

    from tests.mcp_platform.test_generic_plugin_acceptance import (
        _AllowEvaluate,
        _PermissionStore,
        _connection_snapshot,
    )

    write_binding = replace(binding, risk_class=RiskClass.ADDITIVE_WRITE, effect="create")
    write_store = _PermissionStore(write_binding)
    write_runtime = _LiveRuntime(
        RuntimeClient(),
        plugin_runtime,
        write_binding,
        permission_store=write_store,
        connection_snapshot=_connection_snapshot(write_binding),
        authority_service=_AllowEvaluate(),
    )
    write = plan_and_execute_plugin_request(
        "Use the Google Drive plugin to search files for the quarterly plan",
        episode_id="live-mistral-plugin-write",
        runtime_client=write_runtime,
        work_kind="operator_turn",
        cycle_id="live-mistral-plugin-write-chat",
    )
    assert write.status == "permission_required", write.failure_reason
    assert write.permission_request is not None
    assert write_store.issued is write.permission_request
    assert len(backend.calls) == 2

    grant = write_store.approve_plan(write.plan)
    resumed = plan_and_execute_plugin_request(
        "__approved_plugin_plan__",
        episode_id="live-mistral-plugin-write",
        runtime_client=write_runtime,
        grant=grant,
        plan_override=write.plan,
    )
    assert resumed.status == "completed", resumed.failure_reason
    assert write_store.consumed is True
    assert len(backend.calls) == 3
