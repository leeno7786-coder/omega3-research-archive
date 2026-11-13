from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path

import pytest

import mcp_platform.plugin_permissions as permission_module
from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.operation_contracts import McpAuthoritySnapshot
from mcp_platform.package_contracts import RiskClass
from mcp_platform.plugin_authority import plugin_plan_digest
from mcp_platform.plugin_catalog import PluginToolBinding
from mcp_platform.plugin_permissions import (
    PluginPermissionDisplay,
    PluginPermissionStore,
)
from mcp_platform.plugin_planning import PluginOperationPlan, PluginOperationStep


class _Clock:
    def __init__(self) -> None:
        self.monotonic_value = 10.0
        self.wall_value = 1_700_000_000.0

    def monotonic(self) -> float:
        return self.monotonic_value

    def wall(self) -> float:
        return self.wall_value


class _Random:
    def __init__(self, prefix: int = 0) -> None:
        self.value = prefix

    def __call__(self, size: int) -> bytes:
        self.value += 1
        return bytes([self.value % 251 + 1]) * size


def _plain(value: object) -> object:
    if isinstance(value, dict) or hasattr(value, "items"):
        return {key: _plain(item) for key, item in value.items()}
    if type(value) in {tuple, list}:
        return [_plain(item) for item in value]
    return value


def _plan(value: str = "quarterly") -> PluginOperationPlan:
    return PluginOperationPlan(
        plan_id="1" * 64,
        episode_id="episode-1",
        objective_sha256="2" * 64,
        steps=(
            PluginOperationStep(
                step_id="step-1",
                candidate_id="3" * 64,
                arguments={"title": value},
                consumes_step_ids=(),
            ),
        ),
        authority_digest="4" * 64,
    )


def test_permission_plan_decoder_round_trips_committed_shape_and_rejects_extensions() -> None:
    plan = _plan()
    committed = {
        "plan_id": plan.plan_id,
        "episode_id": plan.episode_id,
        "objective_sha256": plan.objective_sha256,
        "authority_digest": plan.authority_digest,
        "steps": [
            {
                "step_id": step.step_id,
                "candidate_id": step.candidate_id,
                "arguments": _plain(step.arguments),
                "consumes_step_ids": list(step.consumes_step_ids),
            }
            for step in plan.steps
        ],
    }

    assert permission_module._plan_from_dict(committed) == plan
    extended = {
        **committed,
        "prior_step_ids": ["protected-planner-step"],
        "steps": [
            {
                **committed["steps"][0],
                "consumes_step_ids": ["protected-planner-step"],
            }
        ],
    }
    with pytest.raises(ValueError, match="^plugin_permission_state_invalid$"):
        permission_module._plan_from_dict(extended)


def _two_step_plan() -> PluginOperationPlan:
    first = PluginOperationStep(
        step_id="step-1",
        candidate_id="3" * 64,
        arguments={"title": "first"},
        consumes_step_ids=(),
    )
    second = PluginOperationStep(
        step_id="step-2",
        candidate_id="5" * 64,
        arguments={"title": "second"},
        consumes_step_ids=("step-1",),
    )
    return PluginOperationPlan(
        plan_id="1" * 64,
        episode_id="episode-1",
        objective_sha256="2" * 64,
        steps=(first, second),
        authority_digest="4" * 64,
    )


def _display() -> PluginPermissionDisplay:
    return PluginPermissionDisplay(
        plugin_apps=(("plugin.issues", "Issues", "app.issues", "Issues"),),
        effect_summary="Create or update data",
        ordered_operations=("Create issue",),
        audience='{"project":"alpha"}',
        destination='{"project":"alpha"}',
        crosses_providers=False,
        field_names=("title",),
        risks=(RiskClass.ADDITIVE_WRITE.value,),
    )


def _signed_binding(
    *,
    candidate_id: str = "3" * 64,
    tool_name: str = "issue.create",
    title: str = "Create issue",
) -> PluginToolBinding:
    input_schema = {
        "type": "object",
        "properties": {
            "payload": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "details": {
                        "type": "object",
                        "properties": {"priority": {"type": "string"}},
                        "additionalProperties": False,
                    },
                },
                "additionalProperties": False,
            }
        },
        "additionalProperties": False,
    }
    output_schema = {"type": "object"}
    return PluginToolBinding(
        candidate_id=candidate_id,
        tool_name=tool_name,
        plugin_id="plugin.issues",
        plugin_revision="a" * 64,
        app_id="app.issues",
        account_key_hash="8" * 64,
        connection_revision="5" * 64,
        capability="issues.write",
        title=title,
        description="Create one issue",
        input_schema=input_schema,
        input_schema_sha256=sha256_hex(canonical_bytes(input_schema)),
        output_schema=output_schema,
        output_schema_sha256=sha256_hex(canonical_bytes(output_schema)),
        risk_class=RiskClass.ADDITIVE_WRITE,
        effect="create",
        requires_revision=False,
        max_result_bytes=4096,
        allowed_mime_types=("application/json",),
    )


def _bindings_for_plan(
    plan: PluginOperationPlan, account_key_hash: str = "8" * 64
) -> tuple[PluginToolBinding, ...]:
    assert account_key_hash == "8" * 64
    return tuple(
        _signed_binding(
            candidate_id=step.candidate_id,
            tool_name="issue.create" if index == 0 else "issue.notify",
            title="Create issue" if index == 0 else "Notify issue",
        )
        for index, step in enumerate(plan.steps)
    )


def _nested_plan() -> PluginOperationPlan:
    return PluginOperationPlan(
        plan_id="1" * 64,
        episode_id="episode-1",
        objective_sha256="2" * 64,
        steps=(
            PluginOperationStep(
                step_id="step-1",
                candidate_id="3" * 64,
                arguments={
                    "payload": {
                        "title": "quarterly",
                        "details": {"priority": "high"},
                    }
                },
                consumes_step_ids=(),
            ),
        ),
        authority_digest="4" * 64,
    )


def _signed_display() -> PluginPermissionDisplay:
    return PluginPermissionDisplay(
        plugin_apps=(("plugin.issues", "Issues", "app.issues", "Issues"),),
        effect_summary="Create or update data",
        ordered_operations=("Create issue",),
        audience='{"project":"alpha"}',
        destination='{"project":"alpha"}',
        crosses_providers=False,
        field_names=("payload.details.priority", "payload.title"),
        risks=(RiskClass.ADDITIVE_WRITE.value,),
    )


def _factory(
    plan: PluginOperationPlan,
    account_key_hash: str,
    grant_id: str,
    *,
    step_index: int = 0,
    tool_name: str = "issue.create",
    tool_schema_sha256: str | None = None,
) -> McpAuthoritySnapshot:
    digest = plugin_plan_digest(plan)
    step = plan.steps[step_index]
    revision = sha256_hex(
        canonical_bytes({"grant_id": grant_id, "plan": digest, "account": account_key_hash})
    )
    return McpAuthoritySnapshot(
        authority_kind="direct_turn",
        authority_id=grant_id,
        authority_revision=revision,
        episode_id=plan.episode_id,
        job_id="",
        job_revision="",
        connection_id="connection-1",
        connection_revision="5" * 64,
        server_id="server.one",
        server_version="1.0.0",
        artifact_sha256="6" * 64,
        tool_name=tool_name,
        tool_schema_sha256=(
            _signed_binding(candidate_id=step.candidate_id).input_schema_sha256
            if tool_schema_sha256 is None
            else tool_schema_sha256
        ),
        capability_tags=("tool:issues.write",),
        source_constraints={"project": "alpha"},
        destination_constraints={"project": "alpha"},
        risk_classes=(RiskClass.ADDITIVE_WRITE.value,),
        direct_execution=True,
        standing_execution=False,
        allow_send_publish=False,
        allow_destructive=False,
        allow_cross_provider=False,
        plan_digest=digest,
        account_key_hash=account_key_hash,
        candidate_id=step.candidate_id,
        plan_step_id=step.step_id,
        arguments_sha256=sha256_hex(canonical_bytes(_plain(step.arguments))),
        connection_authority_revision="9" * 64,
        effect="create",
    )


def _store(path: Path, clock: _Clock, randomness: _Random) -> PluginPermissionStore:
    return PluginPermissionStore(
        path=path,
        monotonic=clock.monotonic,
        wall_time=clock.wall,
        random_bytes=randomness,
        authority_factory=_factory,
        binding_resolver=_bindings_for_plan,
        ttl_seconds=30.0,
    )


def _issue(
    store: PluginPermissionStore,
    plan: PluginOperationPlan | None = None,
):
    selected = plan or _plan()
    return store.issue(
        selected,
        account_key_hash="8" * 64,
        prior_episode_id=selected.episode_id,
        task_id="logical-task-1",
    )


def test_permission_request_persists_exact_read_only_continuation_lineage(
    tmp_path: Path,
) -> None:
    """A restarted process must reject process-bound continuation authority."""
    resolver = getattr(PluginPermissionStore, "resolve_continuation_lineage", None)
    assert callable(resolver), "the durable permission store has no continuation resolver"

    clock = _Clock()
    path = tmp_path / "permissions.json"
    store = _store(path, clock, _Random())
    request = store.issue(
        _plan(),
        account_key_hash="8" * 64,
        prior_episode_id="episode-1",
        task_id="logical-task-1",
    )
    before = path.read_bytes()

    restarted = _store(path, clock, _Random(prefix=40))
    with pytest.raises(ValueError, match="expired"):
        restarted.resolve_continuation_lineage(request.request_id, request.revision)
    assert path.read_bytes() == before


def test_continuation_lineage_resolver_rejects_terminal_and_drifted_state(
    tmp_path: Path,
) -> None:
    resolver = getattr(PluginPermissionStore, "resolve_continuation_lineage", None)
    assert callable(resolver), "the durable permission store has no continuation resolver"

    clock = _Clock()
    selected = [_signed_binding()]

    def bindings(plan, account_key_hash):
        assert account_key_hash == "8" * 64
        return tuple(replace(selected[0], candidate_id=step.candidate_id) for step in plan.steps)

    store = PluginPermissionStore(
        path=tmp_path / "permissions.json",
        monotonic=clock.monotonic,
        wall_time=clock.wall,
        random_bytes=_Random(),
        authority_factory=_factory,
        binding_resolver=bindings,
        ttl_seconds=30.0,
    )
    request = store.issue(
        _plan(),
        account_key_hash="8" * 64,
        prior_episode_id="episode-1",
        task_id="logical-task-1",
    )
    selected[0] = replace(selected[0], plugin_revision="b" * 64)
    with pytest.raises(ValueError, match="invalidated|consent"):
        store.resolve_continuation_lineage(request.request_id, request.revision)

    selected[0] = _signed_binding()
    store.cancel(request.request_id, expected_revision=request.revision)
    document = json.loads(store.path.read_text(encoding="utf-8"))
    terminal_revision = document["permission_requests"][request.request_id]["revision"]
    with pytest.raises(ValueError, match="cancelled"):
        store.resolve_continuation_lineage(request.request_id, terminal_revision)


def test_permission_approve_mints_exact_authority_and_consumes_once(tmp_path: Path) -> None:
    clock = _Clock()
    factory_calls: list[str] = []

    def factory(plan, account_key_hash, grant_id):
        factory_calls.append(grant_id)
        return _factory(plan, account_key_hash, grant_id)

    store = PluginPermissionStore(
        path=tmp_path / "permissions.json",
        monotonic=clock.monotonic,
        wall_time=clock.wall,
        random_bytes=_Random(),
        authority_factory=factory,
        binding_resolver=_bindings_for_plan,
        ttl_seconds=30.0,
    )
    plan = _plan()
    request = store.issue(
        plan,
        account_key_hash="8" * 64,
        prior_episode_id=plan.episode_id,
        task_id="logical-task-1",
    )

    assert len(factory_calls) == 1
    assert store.pending() == (request,)
    exposed = request.to_hud_dict()
    assert set(exposed) == {"request_id", "revision", "display"}
    assert "8" * 64 not in repr(exposed)
    assert plan.steps[0].arguments["title"] in repr(exposed)
    assert not any(
        marker in repr(exposed).lower()
        for marker in ("nonce", "authority_digest", "account_key", "token", "secret")
    )

    grant = store.approve(request.request_id, expected_revision=request.revision)
    assert factory_calls == [grant.grant_id, grant.grant_id]
    authority = store.authority(grant.grant_id)
    assert authority.authority_id == grant.grant_id
    assert authority.plan_digest == plugin_plan_digest(plan)
    assert authority.account_key_hash == "8" * 64
    store.consume(
        grant.grant_id,
        plan=plan,
        authority_digest=plan.authority_digest,
        account_key_hash="8" * 64,
    )
    with pytest.raises(ValueError, match="replayed"):
        store.consume(
            grant.grant_id,
            plan=plan,
            authority_digest=plan.authority_digest,
            account_key_hash="8" * 64,
        )
    with pytest.raises(ValueError, match="revision_conflict"):
        store.approve(request.request_id, expected_revision=request.revision)


@pytest.mark.parametrize("mutation", ["plan", "authority", "account"])
def test_permission_consume_rejects_every_exact_binding(tmp_path: Path, mutation: str) -> None:
    clock = _Clock()
    store = _store(tmp_path / "permissions.json", clock, _Random())
    plan = _plan()
    request = _issue(store, plan)
    grant = store.approve(request.request_id, expected_revision=request.revision)
    selected = _plan("changed") if mutation == "plan" else plan
    digest = "9" * 64 if mutation == "authority" else plan.authority_digest
    account = "a" * 64 if mutation == "account" else "8" * 64

    with pytest.raises(ValueError, match="binding"):
        store.consume(
            grant.grant_id,
            plan=selected,
            authority_digest=digest,
            account_key_hash=account,
        )


def test_permission_cancel_expiry_disconnect_and_restart_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "permissions.json"
    clock = _Clock()
    store = _store(path, clock, _Random())
    cancelled = _issue(store)
    store.cancel(cancelled.request_id, expected_revision=cancelled.revision)
    with pytest.raises(ValueError, match="revision_conflict"):
        store.approve(cancelled.request_id, expected_revision=cancelled.revision)

    expired = _issue(store, _plan("expire"))
    clock.monotonic_value = expired.expires_monotonic + 1
    with pytest.raises(ValueError, match="expired"):
        store.approve(expired.request_id, expected_revision=expired.revision)

    invalidated = _issue(store, _plan("disconnect"))
    assert store.invalidate_plugin("plugin.issues") == 1
    with pytest.raises(ValueError, match="revision_conflict"):
        store.approve(invalidated.request_id, expected_revision=invalidated.revision)

    pending = _issue(store, _plan("restart"))
    restarted = _store(path, clock, _Random(prefix=40))
    with pytest.raises(ValueError, match="expired"):
        restarted.approve(pending.request_id, expected_revision=pending.revision)


def test_permission_revision_cas_and_nonce_fingerprint_fail_closed(tmp_path: Path) -> None:
    clock = _Clock()
    path = tmp_path / "permissions.json"
    store = _store(path, clock, _Random())
    request = _issue(store)
    with pytest.raises(ValueError, match="revision_conflict"):
        store.approve(request.request_id, expected_revision="f" * 64)

    payload = path.read_text(encoding="utf-8")
    assert "nonce" in payload
    path.write_text(payload.replace('"nonce":"', '"nonce":"forged'), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid"):
        store.approve(request.request_id, expected_revision=request.revision)


def test_permission_store_rejects_oversized_display_without_partial_state(tmp_path: Path) -> None:
    clock = _Clock()

    def oversized_resolver(plan, account_key_hash):
        return tuple(
            replace(binding, title="x" * 20_000)
            for binding in _bindings_for_plan(plan, account_key_hash)
        )

    store = PluginPermissionStore(
        path=tmp_path / "permissions.json",
        monotonic=clock.monotonic,
        wall_time=clock.wall,
        random_bytes=_Random(),
        authority_factory=_factory,
        binding_resolver=oversized_resolver,
    )
    with pytest.raises(ValueError, match="display"):
        store.issue(
            _plan(),
            account_key_hash="8" * 64,
            prior_episode_id="episode-1",
            task_id="logical-task-1",
        )
    assert store.pending() == ()


def test_approved_grant_can_be_invalidated_when_execution_setup_fails(tmp_path: Path) -> None:
    clock = _Clock()
    store = _store(tmp_path / "permissions.json", clock, _Random())
    request = _issue(store)
    grant = store.approve(request.request_id, expected_revision=request.revision)

    assert store.revoke_grant(grant.grant_id) is True
    with pytest.raises(ValueError, match="plugin_permission_grant_not_found"):
        store.plan_for_grant(grant.grant_id)


def test_permission_store_enforces_record_and_document_ceilings(tmp_path: Path) -> None:
    clock = _Clock()
    count_store = PluginPermissionStore(
        path=tmp_path / "count.json",
        monotonic=clock.monotonic,
        wall_time=clock.wall,
        random_bytes=_Random(),
        authority_factory=_factory,
        binding_resolver=_bindings_for_plan,
        max_records=1,
    )
    first = _issue(count_store)
    with pytest.raises(ValueError, match="limit_exceeded"):
        _issue(count_store, _plan("second"))
    assert count_store.pending() == (first,)

    byte_store = PluginPermissionStore(
        path=tmp_path / "bytes.json",
        monotonic=clock.monotonic,
        wall_time=clock.wall,
        random_bytes=_Random(prefix=20),
        authority_factory=_factory,
        binding_resolver=_bindings_for_plan,
        max_document_bytes=512,
    )
    with pytest.raises(ValueError, match="limit_exceeded"):
        _issue(byte_store)
    assert byte_store.pending() == ()


def test_approved_permission_expires_before_authority_or_consume(tmp_path: Path) -> None:
    clock = _Clock()
    store = _store(tmp_path / "permissions.json", clock, _Random())
    plan = _plan()
    request = _issue(store, plan)
    grant = store.approve(request.request_id, expected_revision=request.revision)
    clock.monotonic_value = request.expires_monotonic + 1

    with pytest.raises(ValueError, match="expired"):
        store.authority(grant.grant_id)
    with pytest.raises(ValueError, match="grant_invalid"):
        store.consume(
            grant.grant_id,
            plan=plan,
            authority_digest=plan.authority_digest,
            account_key_hash="8" * 64,
        )


def test_permission_approval_is_single_winner_cas(tmp_path: Path) -> None:
    clock = _Clock()
    store = _store(tmp_path / "permissions.json", clock, _Random())
    request = _issue(store)

    def approve() -> str:
        try:
            store.approve(request.request_id, expected_revision=request.revision)
        except ValueError as exc:
            return str(exc)
        return "approved"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = sorted(executor.map(lambda _value: approve(), range(2)))

    assert results == ["approved", "plugin_permission_revision_conflict"]


def test_authority_factory_baseexception_during_approval_leaves_request_pending(
    tmp_path: Path,
) -> None:
    class FactoryAbort(BaseException):
        pass

    clock = _Clock()

    calls = 0

    def abort_factory(plan, account, grant_id):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise FactoryAbort
        return _factory(plan, account, grant_id)

    store = PluginPermissionStore(
        path=tmp_path / "permissions.json",
        monotonic=clock.monotonic,
        wall_time=clock.wall,
        random_bytes=_Random(),
        authority_factory=abort_factory,
        binding_resolver=_bindings_for_plan,
        ttl_seconds=30.0,
    )
    request = _issue(store)

    with pytest.raises(FactoryAbort):
        store.approve(request.request_id, expected_revision=request.revision)
    assert store.pending() == (request,)


def test_one_approval_returns_full_ordered_multi_tool_authority_bundle_once(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    plan = _two_step_plan()

    def bundle_factory(plan, account_key_hash, grant_id):
        first = _factory(plan, account_key_hash, grant_id + "01", step_index=0)
        second = _factory(
            plan,
            account_key_hash,
            grant_id + "02",
            step_index=1,
            tool_name="issue.notify",
        )
        return (first, second)

    store = PluginPermissionStore(
        path=tmp_path / "permissions.json",
        monotonic=clock.monotonic,
        wall_time=clock.wall,
        random_bytes=_Random(),
        authority_factory=bundle_factory,
        binding_resolver=_bindings_for_plan,
    )
    request = store.issue(
        plan,
        account_key_hash="8" * 64,
        prior_episode_id=plan.episode_id,
        task_id="logical-task-1",
    )
    grant = store.approve(request.request_id, expected_revision=request.revision)

    bundle = store.consume(
        grant.grant_id,
        plan=plan,
        authority_digest=plan.authority_digest,
        account_key_hash="8" * 64,
    )
    assert tuple(item.tool_name for item in bundle) == ("issue.create", "issue.notify")
    with pytest.raises(ValueError, match="replayed"):
        store.consume(
            grant.grant_id,
            plan=plan,
            authority_digest=plan.authority_digest,
            account_key_hash="8" * 64,
        )


def test_approval_rejects_authority_preview_drift_before_grant(tmp_path: Path) -> None:
    clock = _Clock()
    source = {"project": "alpha"}

    def factory(plan, account_key_hash, grant_id):
        return replace(
            _factory(plan, account_key_hash, grant_id),
            source_constraints=source,
        )

    store = PluginPermissionStore(
        path=tmp_path / "permissions.json",
        monotonic=clock.monotonic,
        wall_time=clock.wall,
        random_bytes=_Random(),
        authority_factory=factory,
        binding_resolver=_bindings_for_plan,
    )
    request = _issue(store)
    source["project"] = "beta"

    with pytest.raises(ValueError, match="authority_invalid"):
        store.approve(request.request_id, expected_revision=request.revision)
    assert store.pending() == (request,)


def test_authority_and_grant_revision_are_covered_by_nonce_fingerprint(tmp_path: Path) -> None:
    clock = _Clock()
    path = tmp_path / "permissions.json"
    store = _store(path, clock, _Random())
    request = _issue(store)
    grant = store.approve(request.request_id, expected_revision=request.revision)
    document = json.loads(path.read_text(encoding="utf-8"))
    record = next(iter(document["permission_requests"].values()))
    record["authorities"][0]["risk_classes"] = [RiskClass.DESTRUCTIVE.value]
    record["authorities"][0]["allow_destructive"] = True
    path.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")), encoding="utf-8")

    with pytest.raises(ValueError, match="state_invalid"):
        store.authority(grant.grant_id)


def test_invalidation_retires_approved_authority_without_corrupting_restart(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    path = tmp_path / "permissions.json"
    store = _store(path, clock, _Random())
    request = _issue(store)
    store.approve(request.request_id, expected_revision=request.revision)

    assert store.invalidate_plugin("plugin.issues") == 1
    assert _store(path, clock, _Random(prefix=40)).pending() == ()


def test_consumed_authority_cannot_be_retrieved_or_reused(tmp_path: Path) -> None:
    clock = _Clock()
    store = _store(tmp_path / "permissions.json", clock, _Random())
    plan = _plan()
    request = _issue(store, plan)
    grant = store.approve(request.request_id, expected_revision=request.revision)
    assert store.authority(grant.grant_id).authority_id == grant.grant_id
    store.consume(
        grant.grant_id,
        plan=plan,
        authority_digest=plan.authority_digest,
        account_key_hash="8" * 64,
    )

    with pytest.raises(ValueError, match="not_found"):
        store.authority(grant.grant_id)


def test_signed_binding_derives_plain_consent_projection_before_authority_mint(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    store = _store(tmp_path / "permissions.json", clock, _Random())
    plan = _nested_plan()
    request = store.issue(
        plan,
        account_key_hash="8" * 64,
        prior_episode_id=plan.episode_id,
        task_id="logical-task-1",
    )

    grant = store.approve(request.request_id, expected_revision=request.revision)
    assert grant.plan_digest == plugin_plan_digest(plan)


def test_signed_binding_rejects_extra_undisclosed_authority_risk(tmp_path: Path) -> None:
    def overstated_factory(plan, account_key_hash, grant_id):
        return replace(
            _factory(plan, account_key_hash, grant_id),
            risk_classes=(
                RiskClass.ADDITIVE_WRITE.value,
                RiskClass.DESTRUCTIVE.value,
            ),
            allow_destructive=True,
        )

    store = PluginPermissionStore(
        path=tmp_path / "permissions.json",
        monotonic=_Clock().monotonic,
        wall_time=_Clock().wall,
        random_bytes=_Random(),
        authority_factory=overstated_factory,
        binding_resolver=_bindings_for_plan,
    )
    with pytest.raises(ValueError, match="authority_invalid"):
        _issue(store)


def test_matching_forged_binding_and_display_cannot_self_attest(tmp_path: Path) -> None:
    store = _store(tmp_path / "permissions.json", _Clock(), _Random())
    forged = replace(
        _signed_binding(),
        plugin_id="plugin.forged",
        app_id="app.forged",
    )
    forged_display = replace(
        _signed_display(),
        plugin_apps=(("plugin.forged", "Forged", "app.forged", "Forged"),),
    )

    with pytest.raises(TypeError):
        store.issue(
            _nested_plan(),
            display=forged_display,
            bindings=(forged,),
            account_key_hash="8" * 64,
        )
    with pytest.raises(TypeError):
        store.issue(
            _nested_plan(),
            display=forged_display,
            account_key_hash="8" * 64,
        )


def test_approval_re_resolves_current_compiled_binding_revision(tmp_path: Path) -> None:
    selected = [_signed_binding()]

    def resolver(plan, account_key_hash):
        assert account_key_hash == "8" * 64
        return tuple(replace(selected[0], candidate_id=step.candidate_id) for step in plan.steps)

    store = PluginPermissionStore(
        path=tmp_path / "permissions.json",
        monotonic=_Clock().monotonic,
        wall_time=_Clock().wall,
        random_bytes=_Random(),
        authority_factory=_factory,
        binding_resolver=resolver,
    )
    request = _issue(store)
    selected[0] = replace(selected[0], plugin_revision="b" * 64)

    with pytest.raises(ValueError, match="consent"):
        store.approve(request.request_id, expected_revision=request.revision)
    assert store.pending() == (request,)


@pytest.mark.parametrize(
    "failure",
    (
        ValueError("authority_issuance_limit_exceeded"),
        asyncio.CancelledError(),
        BaseException("issuance_aborted"),
    ),
)
def test_permission_consume_issuance_failure_preserves_approved_grant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    store = _store(tmp_path / "permissions.json", _Clock(), _Random())
    plan = _plan()
    request = _issue(store, plan)
    grant = store.approve(request.request_id, expected_revision=request.revision)

    def abort(_issuer, _authority):
        raise failure

    monkeypatch.setattr(permission_module._AuthorityIssuer, "issue", abort)
    with pytest.raises(type(failure)):
        store.consume(
            grant.grant_id,
            plan=plan,
            authority_digest=plan.authority_digest,
            account_key_hash="8" * 64,
        )
    assert store.authority(grant.grant_id).authority_id == grant.grant_id


def test_permission_concurrent_consume_issues_one_exact_bundle(tmp_path: Path) -> None:
    store = _store(tmp_path / "permissions.json", _Clock(), _Random())
    plan = _plan()
    request = _issue(store, plan)
    grant = store.approve(request.request_id, expected_revision=request.revision)

    def consume() -> str:
        try:
            bundle = store.consume(
                grant.grant_id,
                plan=plan,
                authority_digest=plan.authority_digest,
                account_key_hash="8" * 64,
            )
        except ValueError as exc:
            return str(exc)
        return f"issued:{bundle[0].authority_id}"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = sorted(executor.map(lambda _index: consume(), range(2)))

    assert results == [
        f"issued:{grant.grant_id}",
        "plugin_permission_replayed",
    ]


def test_permission_display_represents_exact_ordered_multi_plugin_identities() -> None:
    display = PluginPermissionDisplay.from_dict(
        {
            "plugin_apps": [
                {
                    "plugin_id": "plugin.issues",
                    "plugin_name": "Issues",
                    "app_id": "app.issues",
                    "app_name": "Issues",
                },
                {
                    "plugin_id": "plugin.chat",
                    "plugin_name": "Chat",
                    "app_id": "app.chat",
                    "app_name": "Chat",
                },
            ],
            "effect_summary": "Create or update data | Send or publish data",
            "ordered_operations": ["Create issue", "Notify channel"],
            "audience": '{"project":"alpha"}',
            "destination": '{"channel":"ops"}',
            "crosses_providers": True,
            "field_names": ["payload.title", "message"],
            "risks": ["additive_write", "send_publish"],
        }
    )

    assert display.to_hud_dict()["plugin_apps"] == [
        {
            "plugin_id": "plugin.issues",
            "plugin_name": "Issues",
            "app_id": "app.issues",
            "app_name": "Issues",
        },
        {
            "plugin_id": "plugin.chat",
            "plugin_name": "Chat",
            "app_id": "app.chat",
            "app_name": "Chat",
        },
    ]


def test_permission_approves_exact_ordered_multi_plugin_plan_and_rejects_reordering(
    tmp_path: Path,
) -> None:
    plan = _two_step_plan()
    first = _signed_binding(candidate_id=plan.steps[0].candidate_id)
    second = replace(
        _signed_binding(
            candidate_id=plan.steps[1].candidate_id,
            tool_name="chat.notify",
            title="Notify channel",
        ),
        plugin_id="plugin.chat",
        app_id="app.chat",
    )

    current = [(first, second)]

    def resolver(_plan, account_key_hash):
        assert _plan == plan
        assert account_key_hash == "8" * 64
        return current[0]

    def bundle_factory(selected_plan, account_key_hash, grant_id):
        return (
            _factory(selected_plan, account_key_hash, grant_id + "01", step_index=0),
            _factory(
                selected_plan,
                account_key_hash,
                grant_id + "02",
                step_index=1,
                tool_name="chat.notify",
                tool_schema_sha256=second.input_schema_sha256,
            ),
        )

    store = PluginPermissionStore(
        path=tmp_path / "permissions.json",
        monotonic=_Clock().monotonic,
        wall_time=_Clock().wall,
        random_bytes=_Random(),
        authority_factory=bundle_factory,
        binding_resolver=resolver,
    )
    request = store.issue(
        plan,
        account_key_hash="8" * 64,
        prior_episode_id=plan.episode_id,
        task_id="logical-task-1",
    )
    current[0] = (second, first)
    with pytest.raises(ValueError, match="consent"):
        store.approve(request.request_id, expected_revision=request.revision)

    current[0] = (first, second)
    grant = store.approve(request.request_id, expected_revision=request.revision)
    assert tuple(
        authority.tool_name
        for authority in store.consume(
            grant.grant_id,
            plan=plan,
            authority_digest=plan.authority_digest,
            account_key_hash="8" * 64,
        )
    ) == ("issue.create", "chat.notify")


def test_permission_issue_derives_the_complete_hud_card_before_returning(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path / "permissions.json", _Clock(), _Random())

    plan = _nested_plan()
    request = store.issue(
        plan,
        account_key_hash="8" * 64,
        prior_episode_id=plan.episode_id,
        task_id="logical-task-1",
    )

    assert request.display.to_hud_dict() == {
        "plugin_apps": [
            {
                "plugin_id": "plugin.issues",
                "plugin_name": "Issues",
                "app_id": "app.issues",
                "app_name": "Issues",
            }
        ],
        "effect_summary": "Create data",
        "ordered_operations": ["Create issue"],
        "audience": '{"project":"alpha"}',
        "destination": '{"project":"alpha"}',
        "crosses_providers": False,
        "field_names": ["payload.details.priority", "payload.title"],
        "risks": ["additive_write"],
        "argument_previews": [
            'step-1:payload.details.priority="high"',
            'step-1:payload.title="quarterly"',
        ],
    }
    with pytest.raises(TypeError):
        store.issue(
            _nested_plan(),
            display=replace(request.display, audience='{"project":"forged"}'),
            account_key_hash="8" * 64,
        )


def test_permission_bundle_supports_one_candidate_invoked_twice_in_order(
    tmp_path: Path,
) -> None:
    first = PluginOperationStep(
        step_id="step-1",
        candidate_id="3" * 64,
        arguments={"title": "first"},
        consumes_step_ids=(),
    )
    second = PluginOperationStep(
        step_id="step-2",
        candidate_id="3" * 64,
        arguments={"title": "second"},
        consumes_step_ids=("step-1",),
    )
    plan = PluginOperationPlan(
        plan_id="1" * 64,
        episode_id="episode-1",
        objective_sha256="2" * 64,
        steps=(first, second),
        authority_digest="4" * 64,
    )

    def resolver(selected_plan, account_key_hash):
        assert selected_plan == plan
        assert account_key_hash == "8" * 64
        return tuple(_signed_binding(candidate_id=step.candidate_id) for step in plan.steps)

    def factory(selected_plan, account_key_hash, grant_id):
        return tuple(
            _factory(
                selected_plan,
                account_key_hash,
                f"{grant_id[:-2]}{index + 1:02d}",
                step_index=index,
            )
            for index in range(2)
        )

    store = PluginPermissionStore(
        path=tmp_path / "permissions.json",
        monotonic=_Clock().monotonic,
        wall_time=_Clock().wall,
        random_bytes=_Random(),
        authority_factory=factory,
        binding_resolver=resolver,
    )

    request = store.issue(
        plan,
        account_key_hash="8" * 64,
        prior_episode_id=plan.episode_id,
        task_id="logical-task-1",
    )
    grant = store.approve(request.request_id, expected_revision=request.revision)
    authorities = store.consume(
        grant.grant_id,
        plan=plan,
        authority_digest=plan.authority_digest,
        account_key_hash="8" * 64,
    )

    assert tuple(authority.plan_step_id for authority in authorities) == ("step-1", "step-2")
    assert tuple(authority.tool_name for authority in authorities) == (
        "issue.create",
        "issue.create",
    )


def test_schema_three_permission_state_migrates_to_current_shape(tmp_path: Path) -> None:
    """Catches rejecting the prior durable schema instead of migrating it."""

    path = tmp_path / "permissions.json"
    clock = _Clock()
    store = _store(path, clock, _Random())
    request = _issue(store)
    document = json.loads(path.read_text(encoding="utf-8"))
    legacy = {"schema_version": 3, "requests": document["permission_requests"]}
    path.write_text(json.dumps(legacy), encoding="utf-8")

    pending = store.pending()

    assert [item.request_id for item in pending] == [request.request_id]
    lineage = store.resolve_continuation_lineage(request.request_id, request.revision)
    assert lineage.plan_digest == request.plan_digest

    store.cancel(request.request_id, expected_revision=request.revision)
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["schema_version"] == 4
    assert set(persisted) == {"schema_version", "permission_requests"}
    assert request.request_id in persisted["permission_requests"]


def test_schema_one_permission_state_is_discarded(tmp_path: Path) -> None:
    """Catches retired v1 state being resurrected instead of dropped."""

    path = tmp_path / "permissions.json"
    path.write_text(
        json.dumps({"schema_version": 1, "requests": {"stale": {"request_id": "stale"}}}),
        encoding="utf-8",
    )

    store = _store(path, _Clock(), _Random())

    assert store.pending() == ()
