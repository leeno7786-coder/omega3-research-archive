from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from auto_loop.execute.plugin_plan import plan_and_execute_plugin_request
from mcp_platform.package_contracts import RiskClass
from tests.auto_loop.test_plugin_plan import _Runtime, _binding


class _FencedRuntime(_Runtime):
    def __init__(self, bindings: tuple[object, ...]) -> None:
        super().__init__(bindings)
        self.scope_depth = 0
        self.scope_entries = 0
        self.scope_exits = 0
        call = self.backend.call_tool_sync

        def observed(app_id: str, name: str, arguments: object) -> object:
            assert self.scope_depth == 1
            return call(app_id, name, arguments)

        self.backend.call_tool_sync = observed

    @contextmanager
    def plugin_execution_mutation_scope(self) -> Iterator[None]:
        assert self.scope_depth == 0
        self.scope_depth = 1
        self.scope_entries += 1
        try:
            yield
        finally:
            self.scope_depth = 0
            self.scope_exits += 1


def test_authority_resolution_and_provider_dispatch_share_one_mutation_scope() -> None:
    binding = _binding(
        candidate="a",
        capability="list_events",
        title="List tomorrow events",
        risk=RiskClass.READ_ONLY,
        properties={"start": {"type": "string"}},
        required=["start"],
    )
    runtime = _FencedRuntime((binding,))

    outcome = plan_and_execute_plugin_request(
        "show tomorrow's events",
        episode_id="episode-fenced",
        runtime_client=runtime,
    )

    assert outcome.status == "completed", outcome.failure_reason
    assert runtime.scope_entries == 1
    assert runtime.scope_exits == 1
    assert runtime.scope_depth == 0
    assert len(runtime.calls) == 1
