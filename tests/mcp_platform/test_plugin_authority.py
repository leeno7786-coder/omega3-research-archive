from __future__ import annotations

import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import gc
from pathlib import Path

import pytest

import mcp_platform.authority as authority_module
from mcp_platform.authority import (
    McpOperationAuthorityEvaluator,
    _AuthorityIssuer,
    _AuthorityUseRegistry,
)
from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.operation_contracts import (
    McpAuthoritySnapshot,
    McpConnectionSnapshot,
    McpOperationProposal,
    McpToolAuthority,
)
from mcp_platform.package_contracts import RiskClass
from mcp_platform.plugin_catalog import PluginToolBinding
from mcp_platform.plugin_authority import (
    PluginAuthorityService,
    PluginAuthorityStore,
    PluginConnectionAuthority,
    PluginConnectionAuthorityKind,
    PluginExecutionPolicy,
    plugin_connection_authority_revision,
    plugin_plan_digest,
)
from mcp_platform.plugin_planning import PluginOperationPlan, PluginOperationStep

_INPUT_SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}},
    "required": ["title"],
    "additionalProperties": False,
}
_INPUT_SCHEMA_SHA256 = sha256_hex(canonical_bytes(_INPUT_SCHEMA))


def _plan(*, episode_id: str = "episode-1", argument: str = "quarterly") -> PluginOperationPlan:
    step = PluginOperationStep(
        step_id="step-1",
        candidate_id="1" * 64,
        arguments={"title": argument},
        consumes_step_ids=(),
    )
    return PluginOperationPlan(
        plan_id="2" * 64,
        episode_id=episode_id,
        objective_sha256="3" * 64,
        steps=(step,),
        authority_digest="4" * 64,
    )


def _repeated_candidate_plan() -> PluginOperationPlan:
    return PluginOperationPlan(
        plan_id="2" * 64,
        episode_id="episode-1",
        objective_sha256="3" * 64,
        steps=(
            PluginOperationStep(
                step_id="step-1",
                candidate_id="1" * 64,
                arguments={"title": "first"},
                consumes_step_ids=(),
            ),
            PluginOperationStep(
                step_id="step-2",
                candidate_id="1" * 64,
                arguments={"title": "second"},
                consumes_step_ids=("step-1",),
            ),
        ),
        authority_digest="4" * 64,
    )


def _connection_snapshot(*, enabled: bool = True) -> McpConnectionSnapshot:
    connection_authority = _connection_authority()
    return McpConnectionSnapshot(
        connection_id="connection-1",
        connection_revision="5" * 64,
        server_id="server.one",
        server_version="1.0.0",
        artifact_sha256="6" * 64,
        provider_id="provider.one",
        account_ref="7" * 64,
        enabled=enabled,
        capability_ceiling=("tool:issues.write",),
        allowed_tool_schemas={"issue.create": _INPUT_SCHEMA_SHA256},
        source_constraints={"project": "alpha"},
        destination_constraints={"project": "alpha"},
        authority_revision=connection_authority.revision,
    )


def _tool(*, risk: RiskClass = RiskClass.ADDITIVE_WRITE) -> McpToolAuthority:
    return McpToolAuthority(
        tool_name="issue.create",
        input_schema_sha256=_INPUT_SCHEMA_SHA256,
        capability_tags=("tool:issues.write",),
        risk_class=risk,
        effect="create",
        requires_revision=False,
    )


def _binding(
    *,
    candidate_id: str = "1" * 64,
    tool_name: str = "issue.create",
    risk: RiskClass = RiskClass.ADDITIVE_WRITE,
) -> PluginToolBinding:
    schema = _INPUT_SCHEMA
    output = {"type": "object"}
    return PluginToolBinding(
        candidate_id=candidate_id,
        tool_name=tool_name,
        plugin_id="plugin.issues",
        plugin_revision="a" * 64,
        app_id="app.issues",
        account_key_hash="7" * 64,
        connection_revision="5" * 64,
        capability="issues.write",
        title="Create issue",
        description="Create one issue",
        input_schema=schema,
        input_schema_sha256=sha256_hex(canonical_bytes(schema)),
        output_schema=output,
        output_schema_sha256=sha256_hex(canonical_bytes(output)),
        risk_class=risk,
        effect="create",
        requires_revision=False,
        max_result_bytes=4096,
        allowed_mime_types=("application/json",),
    )


def _binding_resolver(
    plan: PluginOperationPlan,
    account_key_hash: str,
) -> tuple[PluginToolBinding, ...]:
    assert account_key_hash == "7" * 64
    return tuple(_binding(candidate_id=step.candidate_id) for step in plan.steps)


def _issued(authority: McpAuthoritySnapshot) -> McpAuthoritySnapshot:
    return _AuthorityIssuer().issue(authority)


def _proposal(**overrides: object) -> McpOperationProposal:
    document: dict[str, object] = {
        "request_id": "request-1",
        "episode_id": "episode-1",
        "server_id": "server.one",
        "server_version": "1.0.0",
        "artifact_sha256": "6" * 64,
        "provider_id": "provider.one",
        "account_ref": "7" * 64,
        "connection_id": "connection-1",
        "connection_revision": "5" * 64,
        "tool_name": "issue.create",
        "tool_schema_sha256": _INPUT_SCHEMA_SHA256,
        "arguments": {"title": "quarterly"},
        "source_constraints": {"project": "alpha"},
        "destination_constraints": {"project": "alpha"},
        "expected_provider_revisions": {},
        "authority_kind": "direct_turn",
        "authority_id": "authority-1",
        "authority_revision": "9" * 64,
        "plan_digest": plugin_plan_digest(_plan()),
        "candidate_id": "1" * 64,
        "plan_step_id": "step-1",
        "connection_authority_revision": _connection_authority().revision,
        "idempotency_key": "request-1",
        "evidence_memory_ids": [],
    }
    document.update(overrides)
    document["proposal_digest"] = sha256_hex(canonical_bytes(document))
    return McpOperationProposal.from_dict(document)


def _authority(
    *,
    plan: PluginOperationPlan | None = None,
    kind: str = "direct_turn",
    job_revision: str = "",
) -> McpAuthoritySnapshot:
    selected = plan or _plan()
    return McpAuthoritySnapshot(
        authority_kind=kind,
        authority_id="authority-1",
        authority_revision="9" * 64,
        episode_id=selected.episode_id,
        job_id="" if kind == "direct_turn" else "job-1",
        job_revision=job_revision,
        connection_id="connection-1",
        connection_revision="5" * 64,
        server_id="server.one",
        server_version="1.0.0",
        artifact_sha256="6" * 64,
        tool_name="issue.create",
        tool_schema_sha256=_INPUT_SCHEMA_SHA256,
        capability_tags=("tool:issues.write",),
        source_constraints={"project": "alpha"},
        destination_constraints={"project": "alpha"},
        risk_classes=(RiskClass.ADDITIVE_WRITE.value,),
        direct_execution=kind == "direct_turn",
        standing_execution=kind == "standing_grant",
        allow_send_publish=False,
        allow_destructive=False,
        allow_cross_provider=False,
        plan_digest=plugin_plan_digest(selected),
        account_key_hash="7" * 64,
        candidate_id=selected.steps[0].candidate_id,
        plan_step_id=selected.steps[0].step_id,
        arguments_sha256=sha256_hex(canonical_bytes(dict(selected.steps[0].arguments))),
        connection_authority_revision=_connection_authority().revision,
        effect="create",
    )


def _connection_authority(
    *, policy: PluginExecutionPolicy = PluginExecutionPolicy.DIRECT_TURN
) -> PluginConnectionAuthority:
    values = {
        "plugin_id": "plugin.issues",
        "app_id": "app.issues",
        "account_key_hash": "7" * 64,
        "connection_revision": "5" * 64,
        "provider_scopes": ("issues.write",),
        "capability_ceiling": ("tool:issues.write",),
        "allowed_tool_schemas": {"issues.write": _INPUT_SCHEMA_SHA256},
        "source_constraints": {"project": "alpha"},
        "destination_constraints": {"project": "alpha"},
        "execution_policy": policy,
        "authority_kind": PluginConnectionAuthorityKind.OAUTH,
    }
    return PluginConnectionAuthority(
        **values,
        revision=plugin_connection_authority_revision(**values),
    )


def test_empty_scope_connection_authority_requires_explicit_credentialless_kind() -> None:
    values = {
        "plugin_id": "plugin.issues",
        "app_id": "app.issues",
        "account_key_hash": "7" * 64,
        "connection_revision": "5" * 64,
        "provider_scopes": (),
        "capability_ceiling": ("tool:issues.write",),
        "allowed_tool_schemas": {"issues.write": _INPUT_SCHEMA_SHA256},
        "source_constraints": {"project": "alpha"},
        "destination_constraints": {"project": "alpha"},
        "execution_policy": PluginExecutionPolicy.DIRECT_TURN,
        "authority_kind": PluginConnectionAuthorityKind.OAUTH,
    }

    with pytest.raises(ValueError, match="^plugin_authority_invalid$"):
        PluginConnectionAuthority(
            **values,
            revision=plugin_connection_authority_revision(**values),
        )


def test_credentialless_connection_authority_persists_with_explicit_kind(
    tmp_path: Path,
) -> None:
    values = {
        "plugin_id": "plugin.public",
        "app_id": "app.public",
        "account_key_hash": "6" * 64,
        "connection_revision": "4" * 64,
        "provider_scopes": (),
        "capability_ceiling": ("tool:records.read",),
        "allowed_tool_schemas": {"records.read": _INPUT_SCHEMA_SHA256},
        "source_constraints": {},
        "destination_constraints": {},
        "execution_policy": PluginExecutionPolicy.CONFIRM_EACH,
        "authority_kind": PluginConnectionAuthorityKind.CREDENTIALLESS,
    }
    authority = PluginConnectionAuthority(
        **values,
        revision=plugin_connection_authority_revision(**values),
    )
    path = tmp_path / "credentialless-authority.json"
    PluginAuthorityStore(path=path).publish_connection(authority, expected_revision="")

    assert (
        PluginAuthorityStore(path=path).connection(
            authority.plugin_id,
            authority.app_id,
            authority.account_key_hash,
        )
        == authority
    )


def test_shared_evaluator_returns_the_first_exact_authority_failure(
    tmp_path: Path,
) -> None:
    evaluator = McpOperationAuthorityEvaluator()
    service = PluginAuthorityService(
        store=PluginAuthorityStore(path=tmp_path / "authority.json"),
        binding_resolver=_binding_resolver,
        authority_evaluator=evaluator,
    )
    proposal = _proposal()
    connection = _connection_snapshot()
    authority = _issued(_authority())
    tool = _tool()

    assert (
        evaluator.evaluate(proposal, connection=connection, authority=authority, tool=tool).allowed
        is True
    )
    assert service.evaluate_operation(
        proposal, connection=connection, authority=authority, tool=tool
    ) == evaluator.evaluate(proposal, connection=connection, authority=authority, tool=tool)
    assert (
        evaluator.evaluate(
            proposal,
            connection=replace(connection, enabled=False),
            authority=authority,
            tool=tool,
        ).failed_check
        == "connection_identity_revision"
    )
    assert (
        evaluator.evaluate(
            proposal,
            connection=replace(connection, capability_ceiling=()),
            authority=authority,
            tool=tool,
        ).failed_check
        == "connection_capability_ceiling"
    )
    assert (
        evaluator.evaluate(
            proposal,
            connection=connection,
            authority=replace(authority, connection_revision="a" * 64),
            tool=tool,
        ).failed_check
        == "authority_validity"
    )
    assert (
        evaluator.evaluate(
            proposal,
            connection=connection,
            authority=replace(authority, destination_constraints={"project": "beta"}),
            tool=tool,
        ).failed_check
        == "source_destination_constraints"
    )
    assert (
        evaluator.evaluate(
            proposal,
            connection=connection,
            authority=replace(authority, risk_classes=(RiskClass.READ_ONLY.value,)),
            tool=tool,
        ).failed_check
        == "risk_specific_authority"
    )


def test_evaluator_rejects_supplied_tool_argument_account_and_empty_plan_mismatch() -> None:
    evaluator = McpOperationAuthorityEvaluator()
    proposal = _proposal()
    connection = _connection_snapshot()
    authority = _authority()
    tool = _tool()
    other_tool = replace(tool, tool_name="issue.delete")
    other_connection = replace(
        connection,
        allowed_tool_schemas={"issue.delete": tool.input_schema_sha256},
    )

    assert (
        evaluator.evaluate(
            proposal,
            connection=other_connection,
            authority=authority,
            tool=other_tool,
        ).allowed
        is False
    )
    assert (
        evaluator.evaluate(
            _proposal(plan_digest="b" * 64),
            connection=connection,
            authority=authority,
            tool=tool,
        ).allowed
        is False
    )
    assert (
        evaluator.evaluate(
            _proposal(candidate_id="b" * 64),
            connection=connection,
            authority=authority,
            tool=tool,
        ).allowed
        is False
    )
    assert (
        evaluator.evaluate(
            _proposal(arguments={"title": "changed"}),
            connection=connection,
            authority=authority,
            tool=tool,
        ).allowed
        is False
    )
    assert (
        evaluator.evaluate(
            _proposal(account_ref="a" * 64),
            connection=replace(connection, account_ref="a" * 64),
            authority=authority,
            tool=tool,
        ).allowed
        is False
    )
    with pytest.raises(ValueError, match="invalid_authority_snapshot"):
        replace(authority, plan_digest="", account_key_hash="")


def test_direct_turn_rejects_candidate_tool_schema_and_arguments_mismatch(
    tmp_path: Path,
) -> None:
    store = PluginAuthorityStore(path=tmp_path / "authority.json")
    connection = _connection_authority()
    store.publish_connection(connection, expected_revision="")

    def forged_resolver(plan, account_key_hash):
        assert account_key_hash == "7" * 64
        return (
            _binding(
                candidate_id="a" * 64,
                tool_name="issue.delete",
                risk=RiskClass.DESTRUCTIVE,
            ),
        )

    service = PluginAuthorityService(store=store, binding_resolver=forged_resolver)
    plan = _plan()
    contributor = {
        "source": "operator",
        "type": "command",
        "episode_id": plan.episode_id,
        "objective_sha256": plan.objective_sha256,
        "entailed_plan_digest": plugin_plan_digest(plan),
    }

    assert (
        service.authorize_direct_turn(
            plan,
            connection=connection,
            connection_snapshot=_connection_snapshot(),
            candidate_id=plan.steps[0].candidate_id,
            step_id=plan.steps[0].step_id,
            active_contributor=contributor,
        )
        is None
    )

    with pytest.raises(TypeError):
        service.authorize_direct_turn(
            plan,
            binding=_binding(candidate_id=plan.steps[0].candidate_id),
            connection=connection,
            connection_snapshot=_connection_snapshot(),
            candidate_id=plan.steps[0].candidate_id,
            step_id=plan.steps[0].step_id,
            active_contributor=contributor,
        )


def test_direct_turn_requires_exact_operator_command_entailment_and_policy(tmp_path: Path) -> None:
    store = PluginAuthorityStore(path=tmp_path / "authority.json")
    connection = _connection_authority()
    store.publish_connection(connection, expected_revision="")
    service = PluginAuthorityService(
        store=store,
        binding_resolver=_binding_resolver,
        random_bytes=lambda size: b"d" * size,
        active_episode_id=lambda: plan.episode_id,
    )
    plan = _plan()
    contributor = {
        "source": "operator",
        "type": "command",
        "episode_id": plan.episode_id,
        "objective_sha256": plan.objective_sha256,
        "entailed_plan_digest": plugin_plan_digest(plan),
    }

    for mutation in (
        {**contributor, "source": "model"},
        {**contributor, "type": "chat"},
        {**contributor, "entailed_plan_digest": "a" * 64},
    ):
        assert (
            service.authorize_direct_turn(
                plan,
                connection=connection,
                connection_snapshot=_connection_snapshot(),
                candidate_id=plan.steps[0].candidate_id,
                step_id=plan.steps[0].step_id,
                active_contributor=mutation,
            )
            is None
        )
    assert (
        service.authorize_direct_turn(
            plan,
            connection=_connection_authority(policy=PluginExecutionPolicy.CONFIRM_EACH),
            connection_snapshot=_connection_snapshot(),
            candidate_id=plan.steps[0].candidate_id,
            step_id=plan.steps[0].step_id,
            active_contributor=contributor,
        )
        is None
    )

    first = service.authorize_direct_turn(
        plan,
        connection=connection,
        connection_snapshot=_connection_snapshot(),
        candidate_id=plan.steps[0].candidate_id,
        step_id=plan.steps[0].step_id,
        active_contributor=contributor,
    )
    second = service.authorize_direct_turn(
        plan,
        connection=connection,
        connection_snapshot=_connection_snapshot(),
        candidate_id=plan.steps[0].candidate_id,
        step_id=plan.steps[0].step_id,
        active_contributor=contributor,
    )
    assert second is None
    assert first is not None
    assert first.authority_kind == "direct_turn"
    assert first.plan_digest == plugin_plan_digest(plan)


def test_connection_and_standing_authority_are_restart_safe_cas_and_invalidated(
    tmp_path: Path,
) -> None:
    path = tmp_path / "authority.json"
    store = PluginAuthorityStore(path=path)
    connection = _connection_authority()
    assert store.publish_connection(connection, expected_revision="") == connection
    with pytest.raises(ValueError, match="revision_conflict"):
        store.publish_connection(connection, expected_revision="")
    assert (
        PluginAuthorityStore(path=path).connection(
            connection.plugin_id, connection.app_id, connection.account_key_hash
        )
        == connection
    )

    plan = _plan()
    standing = _authority(plan=plan, kind="standing_grant", job_revision="a" * 64)
    store.publish_standing_grant(standing, expected_revision="")
    service = PluginAuthorityService(
        store=store,
        binding_resolver=_binding_resolver,
        random_bytes=lambda size: b"s" * size,
    )
    assert (
        service.authorize_standing_grant(
            plan,
            authority_id=standing.authority_id,
            job_id="job-1",
            job_revision="a" * 64,
            execution_revision=standing.authority_revision,
            candidate_id=plan.steps[0].candidate_id,
            step_id=plan.steps[0].step_id,
            connection=connection,
        )
        is not None
    )
    assert (
        service.authorize_standing_grant(
            plan,
            authority_id=standing.authority_id,
            job_id="job-1",
            job_revision="b" * 64,
            execution_revision="2" * 64,
            candidate_id=plan.steps[0].candidate_id,
            step_id=plan.steps[0].step_id,
            connection=connection,
        )
        is None
    )

    assert (
        store.invalidate_connection(
            connection.plugin_id, connection.app_id, connection.account_key_hash
        )
        == 3
    )
    with pytest.raises(ValueError, match="not_found"):
        store.connection(connection.plugin_id, connection.app_id, connection.account_key_hash)
    with pytest.raises(ValueError, match="not_found"):
        store.authority("standing_grant", standing.authority_id)
    contributor = {
        "source": "operator",
        "type": "command",
        "episode_id": plan.episode_id,
        "objective_sha256": plan.objective_sha256,
        "entailed_plan_digest": plugin_plan_digest(plan),
    }
    assert (
        PluginAuthorityService(
            store=store, binding_resolver=_binding_resolver
        ).authorize_direct_turn(
            plan,
            connection=connection,
            connection_snapshot=_connection_snapshot(),
            candidate_id=plan.steps[0].candidate_id,
            step_id=plan.steps[0].step_id,
            active_contributor=contributor,
        )
        is None
    )


def test_update_lineage_checkpoint_restores_connection_standing_and_execution_state(
    tmp_path: Path,
) -> None:
    """Candidate publication cannot erase the predecessor's authority lineage."""

    store = PluginAuthorityStore(path=tmp_path / "authority.json")
    predecessor = _connection_authority()
    store.publish_connection(predecessor, expected_revision="")
    standing = _authority(
        plan=_plan(),
        kind="standing_grant",
        job_revision="a" * 64,
    )
    store.publish_standing_grant(standing, expected_revision="")
    checkpoint = store.checkpoint_connection_lineage(
        predecessor.plugin_id,
        predecessor.app_id,
        predecessor.account_key_hash,
    )
    candidate_values = {
        "plugin_id": predecessor.plugin_id,
        "app_id": predecessor.app_id,
        "account_key_hash": predecessor.account_key_hash,
        "connection_revision": "6" * 64,
        "provider_scopes": predecessor.provider_scopes,
        "capability_ceiling": predecessor.capability_ceiling,
        "allowed_tool_schemas": predecessor.allowed_tool_schemas,
        "source_constraints": predecessor.source_constraints,
        "destination_constraints": predecessor.destination_constraints,
        "execution_policy": predecessor.execution_policy,
        "authority_kind": predecessor.authority_kind,
    }
    candidate = PluginConnectionAuthority(
        **candidate_values,
        revision=plugin_connection_authority_revision(**candidate_values),
    )
    store.publish_connection(candidate, expected_revision=predecessor.revision)
    with pytest.raises(ValueError, match="^plugin_authority_not_found$"):
        store.authority(standing.authority_kind, standing.authority_id)

    store.restore_connection_lineage(checkpoint)

    assert store.connection(
        predecessor.plugin_id,
        predecessor.app_id,
        predecessor.account_key_hash,
    ) == predecessor
    assert store.authority(standing.authority_kind, standing.authority_id) == standing


def test_authority_store_enforces_record_ceiling(tmp_path: Path) -> None:
    store = PluginAuthorityStore(
        path=tmp_path / "limited-authority.json",
        max_records=1,
    )
    connection = _connection_authority()
    store.publish_connection(connection, expected_revision="")

    with pytest.raises(ValueError, match="limit_exceeded"):
        store.publish_standing_grant(
            _authority(kind="standing_grant", job_revision="a" * 64),
            expected_revision="",
        )


def test_settings_revision_change_retires_linked_standing_grants(tmp_path: Path) -> None:
    store = PluginAuthorityStore(path=tmp_path / "authority.json")
    first = _connection_authority(policy=PluginExecutionPolicy.DIRECT_TURN)
    store.publish_connection(first, expected_revision="")
    standing = _authority(kind="standing_grant", job_revision="a" * 64)
    store.publish_standing_grant(standing, expected_revision="")
    changed = _connection_authority(policy=PluginExecutionPolicy.CONFIRM_EACH)

    store.publish_connection(changed, expected_revision=first.revision)

    with pytest.raises(ValueError, match="not_found"):
        store.authority("standing_grant", standing.authority_id)


def test_evaluator_rejects_stale_authority_handle_for_a_second_request() -> None:
    evaluator = McpOperationAuthorityEvaluator()
    authority = _issued(_authority())
    first = _proposal()
    second = _proposal(request_id="request-2", idempotency_key="request-2")

    assert (
        evaluator.evaluate(
            first,
            connection=_connection_snapshot(),
            authority=authority,
            tool=_tool(),
        ).allowed
        is True
    )
    assert (
        evaluator.evaluate(
            second,
            connection=_connection_snapshot(),
            authority=authority,
            tool=_tool(),
        ).allowed
        is False
    )


def test_namespaced_connection_capability_matches_raw_catalog_binding_without_ambiguity(
    tmp_path: Path,
) -> None:
    values = {
        "plugin_id": "plugin.issues",
        "app_id": "app.issues",
        "account_key_hash": "7" * 64,
        "connection_revision": "5" * 64,
        "provider_scopes": ("issues.write",),
        "capability_ceiling": ("tool:issues.write",),
        "allowed_tool_schemas": {"issues.write": _INPUT_SCHEMA_SHA256},
        "source_constraints": {"project": "alpha"},
        "destination_constraints": {"project": "alpha"},
        "execution_policy": PluginExecutionPolicy.DIRECT_TURN,
        "authority_kind": PluginConnectionAuthorityKind.OAUTH,
    }
    connection = PluginConnectionAuthority(
        **values,
        revision=plugin_connection_authority_revision(**values),
    )
    snapshot = replace(
        _connection_snapshot(),
        capability_ceiling=("tool:issues.write",),
        authority_revision=connection.revision,
    )
    store = PluginAuthorityStore(path=tmp_path / "authority.json")
    store.publish_connection(connection, expected_revision="")
    service = PluginAuthorityService(
        store=store,
        binding_resolver=_binding_resolver,
        active_episode_id=lambda: plan.episode_id,
    )
    plan = _plan()
    contributor = {
        "source": "operator",
        "type": "command",
        "episode_id": plan.episode_id,
        "objective_sha256": plan.objective_sha256,
        "entailed_plan_digest": plugin_plan_digest(plan),
    }

    assert (
        service.authorize_direct_turn(
            plan,
            connection=connection,
            connection_snapshot=snapshot,
            candidate_id=plan.steps[0].candidate_id,
            step_id=plan.steps[0].step_id,
            active_contributor=contributor,
        )
        is not None
    )
    ambiguous_values = {
        **values,
        "capability_ceiling": ("tool:issues.write", "issues.write"),
    }
    ambiguous = PluginConnectionAuthority(
        **ambiguous_values,
        revision=plugin_connection_authority_revision(**ambiguous_values),
    )
    ambiguous_store = PluginAuthorityStore(path=tmp_path / "ambiguous.json")
    ambiguous_store.publish_connection(ambiguous, expected_revision="")
    assert (
        PluginAuthorityService(
            store=ambiguous_store, binding_resolver=_binding_resolver
        ).authorize_direct_turn(
            plan,
            connection=ambiguous,
            connection_snapshot=replace(
                snapshot,
                capability_ceiling=("tool:issues.write", "issues.write"),
                authority_revision=ambiguous.revision,
            ),
            candidate_id=plan.steps[0].candidate_id,
            step_id=plan.steps[0].step_id,
            active_contributor=contributor,
        )
        is None
    )
    invalid_service = PluginAuthorityService(
        store=store,
        binding_resolver=lambda plan, account: (
            replace(
                _binding(candidate_id=plan.steps[0].candidate_id), capability="tool:issues.write"
            ),
        ),
    )
    assert (
        invalid_service.authorize_direct_turn(
            plan,
            connection=connection,
            connection_snapshot=snapshot,
            candidate_id=plan.steps[0].candidate_id,
            step_id=plan.steps[0].step_id,
            active_contributor=contributor,
        )
        is None
    )


def test_issued_authority_is_single_use_across_evaluator_instances() -> None:
    authority = _issued(_authority())

    assert (
        McpOperationAuthorityEvaluator()
        .evaluate(
            _proposal(),
            connection=_connection_snapshot(),
            authority=authority,
            tool=_tool(),
        )
        .allowed
    )
    assert (
        not McpOperationAuthorityEvaluator()
        .evaluate(
            _proposal(request_id="request-2", idempotency_key="request-2"),
            connection=_connection_snapshot(),
            authority=authority,
            tool=_tool(),
        )
        .allowed
    )


def test_authority_use_capacity_reclaims_only_dead_issued_objects() -> None:
    registry = _AuthorityUseRegistry(2)
    issuer = _AuthorityIssuer(registry)
    evaluator = McpOperationAuthorityEvaluator()
    stale = issuer.issue(_authority())
    assert evaluator.evaluate(
        _proposal(), connection=_connection_snapshot(), authority=stale, tool=_tool()
    ).allowed

    for index in range(6):
        authority_id = f"authority-{index + 2}"
        revision = sha256_hex(canonical_bytes({"authority": authority_id}))
        authority = issuer.issue(
            replace(_authority(), authority_id=authority_id, authority_revision=revision)
        )
        proposal = _proposal(
            request_id=f"request-{index + 2}",
            idempotency_key=f"request-{index + 2}",
            authority_id=authority_id,
            authority_revision=revision,
        )
        assert (
            McpOperationAuthorityEvaluator()
            .evaluate(
                proposal,
                connection=_connection_snapshot(),
                authority=authority,
                tool=_tool(),
            )
            .allowed
        )
        del authority
        gc.collect()

    assert (
        not McpOperationAuthorityEvaluator()
        .evaluate(
            _proposal(request_id="stale", idempotency_key="stale"),
            connection=_connection_snapshot(),
            authority=stale,
            tool=_tool(),
        )
        .allowed
    )


def test_copied_reconstructed_and_object_new_authority_cannot_forge_issuance() -> None:
    authority = _issued(_authority())
    public_document = {
        key: value for key, value in authority.__dict__.items() if key != "_mcp_authority_cell"
    }
    copies = [
        McpAuthoritySnapshot(**public_document),
    ]
    forged = object.__new__(McpAuthoritySnapshot)
    forged.__dict__.update(authority.__dict__)
    copies.append(forged)

    for candidate in copies:
        assert (
            not McpOperationAuthorityEvaluator()
            .evaluate(
                _proposal(),
                connection=_connection_snapshot(),
                authority=candidate,
                tool=_tool(),
            )
            .allowed
        )


def test_weak_issuance_cleanup_does_not_deadlock_under_concurrent_gc() -> None:
    def issue_batch(worker: int) -> int:
        issuer = _AuthorityIssuer(_AuthorityUseRegistry(73))
        for index in range(5):
            authority_id = f"worker-{worker}-authority-{index}"
            authority = issuer.issue(
                replace(
                    _authority(),
                    authority_id=authority_id,
                    authority_revision=sha256_hex(canonical_bytes({"authority": authority_id})),
                )
            )
            del authority
            gc.collect(0)
        return worker

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(issue_batch, worker) for worker in range(4)]
        assert [future.result(timeout=10) for future in futures] == list(range(4))


def test_stale_weak_cleanup_cannot_remove_new_identity_reuse_proof() -> None:
    evaluator = McpOperationAuthorityEvaluator()
    registry = _AuthorityUseRegistry(2)
    issuer = _AuthorityIssuer(registry)
    registry._identity = lambda _authority: 7
    first = issuer.issue(_authority())
    first_marker = registry._entries[7].marker
    registry._retire(7, first_marker)
    second = issuer.issue(
        replace(
            _authority(),
            authority_id="authority-2",
            authority_revision=sha256_hex(canonical_bytes({"authority": 2})),
        )
    )
    registry._retire(7, first_marker)

    assert evaluator.evaluate(
        _proposal(
            authority_id=second.authority_id,
            authority_revision=second.authority_revision,
        ),
        connection=_connection_snapshot(),
        authority=second,
        tool=_tool(),
    ).allowed
    del first


def test_public_authority_construction_and_caller_selected_partitions_are_unavailable() -> None:
    assert not hasattr(authority_module, "issue_mcp_authority")
    with pytest.raises(TypeError):
        McpOperationAuthorityEvaluator(max_authority_uses=1)


def test_authority_shallow_and_deep_copy_share_the_exact_used_cell() -> None:
    authority = _issued(_authority())

    assert copy.copy(authority) is authority
    assert copy.deepcopy(authority) is authority


def test_consumption_releases_active_capacity_while_live_handle_stays_used() -> None:
    registry = _AuthorityUseRegistry(1)
    issuer = _AuthorityIssuer(registry)
    evaluator = McpOperationAuthorityEvaluator()
    first = issuer.issue(_authority())
    assert evaluator.evaluate(
        _proposal(),
        connection=_connection_snapshot(),
        authority=first,
        tool=_tool(),
    ).allowed

    second = issuer.issue(
        replace(
            _authority(),
            authority_id="authority-2",
            authority_revision=sha256_hex(canonical_bytes({"authority": 2})),
        )
    )
    assert second.authority_id == "authority-2"
    assert not evaluator.evaluate(
        _proposal(request_id="request-2", idempotency_key="request-2"),
        connection=_connection_snapshot(),
        authority=first,
        tool=_tool(),
    ).allowed


def test_reloaded_standing_grant_derives_distinct_execution_authority(tmp_path: Path) -> None:
    store = PluginAuthorityStore(path=tmp_path / "authority.json")
    connection = _connection_authority()
    store.publish_connection(connection, expected_revision="")
    plan = _plan()
    standing = _authority(plan=plan, kind="standing_grant", job_revision="a" * 64)
    store.publish_standing_grant(standing, expected_revision="")

    first = PluginAuthorityService(
        store=store, binding_resolver=_binding_resolver
    ).authorize_standing_grant(
        plan,
        authority_id=standing.authority_id,
        job_id="job-1",
        job_revision="a" * 64,
        execution_revision=standing.authority_revision,
        candidate_id=plan.steps[0].candidate_id,
        step_id=plan.steps[0].step_id,
        connection=connection,
    )
    assert first is not None
    second = PluginAuthorityService(
        store=PluginAuthorityStore(path=store.path),
        binding_resolver=_binding_resolver,
    ).authorize_standing_grant(
        plan,
        authority_id=standing.authority_id,
        job_id="job-1",
        job_revision="a" * 64,
        execution_revision=first.authority_revision,
        candidate_id=plan.steps[0].candidate_id,
        step_id=plan.steps[0].step_id,
        connection=connection,
    )

    assert second is not None
    assert first.authority_id != second.authority_id


def test_identical_evaluator_calls_consume_one_issued_handle_atomically() -> None:
    authority = _issued(_authority())
    evaluator = McpOperationAuthorityEvaluator()
    proposal = _proposal()

    def evaluate() -> bool:
        return evaluator.evaluate(
            proposal,
            connection=_connection_snapshot(),
            authority=authority,
            tool=_tool(),
        ).allowed

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = sorted(executor.map(lambda _index: evaluate(), range(2)))

    assert results == [False, True]
    assert evaluate() is False


def test_direct_turn_execution_is_durable_across_services_and_concurrent_claims(
    tmp_path: Path,
) -> None:
    path = tmp_path / "authority.json"
    store = PluginAuthorityStore(path=path)
    connection = _connection_authority()
    store.publish_connection(connection, expected_revision="")
    plan = _plan()
    contributor = {
        "source": "operator",
        "type": "command",
        "episode_id": plan.episode_id,
        "objective_sha256": plan.objective_sha256,
        "entailed_plan_digest": plugin_plan_digest(plan),
    }

    def authorize() -> McpAuthoritySnapshot | None:
        return PluginAuthorityService(
            store=PluginAuthorityStore(path=path),
            binding_resolver=_binding_resolver,
            active_episode_id=lambda: plan.episode_id,
        ).authorize_direct_turn(
            plan,
            connection=connection,
            connection_snapshot=_connection_snapshot(),
            candidate_id=plan.steps[0].candidate_id,
            step_id=plan.steps[0].step_id,
            active_contributor=contributor,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _index: authorize(), range(2)))

    assert sum(result is not None for result in results) == 1
    assert authorize() is None


def test_standing_execution_revision_is_durable_and_new_revision_is_distinct(
    tmp_path: Path,
) -> None:
    path = tmp_path / "authority.json"
    store = PluginAuthorityStore(path=path)
    connection = _connection_authority()
    store.publish_connection(connection, expected_revision="")
    plan = _plan()
    standing = _authority(plan=plan, kind="standing_grant", job_revision="a" * 64)
    store.publish_standing_grant(standing, expected_revision="")

    def authorize(execution_revision: str) -> McpAuthoritySnapshot | None:
        return PluginAuthorityService(
            store=PluginAuthorityStore(path=path),
            binding_resolver=_binding_resolver,
        ).authorize_standing_grant(
            plan,
            authority_id=standing.authority_id,
            job_id="job-1",
            job_revision="a" * 64,
            execution_revision=execution_revision,
            candidate_id=plan.steps[0].candidate_id,
            step_id=plan.steps[0].step_id,
            connection=connection,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        competing = tuple(
            executor.map(lambda _index: authorize(standing.authority_revision), range(2))
        )
    assert sum(result is not None for result in competing) == 1
    first = next(result for result in competing if result is not None)
    assert authorize(standing.authority_revision) is None
    second = authorize(first.authority_revision)
    assert second is not None
    assert second.authority_id != first.authority_id


@pytest.mark.parametrize(
    "failure",
    (
        ValueError("authority_issuance_limit_exceeded"),
        KeyboardInterrupt("issuance_cancelled"),
        BaseException("issuance_aborted"),
    ),
)
def test_direct_turn_issuance_failure_rolls_back_durable_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    path = tmp_path / "authority.json"
    store = PluginAuthorityStore(path=path)
    connection = _connection_authority()
    store.publish_connection(connection, expected_revision="")
    plan = _plan()
    contributor = {
        "source": "operator",
        "type": "command",
        "episode_id": plan.episode_id,
        "objective_sha256": plan.objective_sha256,
        "entailed_plan_digest": plugin_plan_digest(plan),
    }
    service = PluginAuthorityService(
        store=store,
        binding_resolver=_binding_resolver,
        active_episode_id=lambda: plan.episode_id,
    )
    real_issue = authority_module._AuthorityIssuer.issue

    def abort(_issuer, _authority):
        raise failure

    monkeypatch.setattr(authority_module._AuthorityIssuer, "issue", abort)
    with pytest.raises(type(failure)):
        service.authorize_direct_turn(
            plan,
            connection=connection,
            connection_snapshot=_connection_snapshot(),
            candidate_id=plan.steps[0].candidate_id,
            step_id=plan.steps[0].step_id,
            active_contributor=contributor,
        )

    monkeypatch.setattr(authority_module._AuthorityIssuer, "issue", real_issue)
    assert (
        service.authorize_direct_turn(
            plan,
            connection=connection,
            connection_snapshot=_connection_snapshot(),
            candidate_id=plan.steps[0].candidate_id,
            step_id=plan.steps[0].step_id,
            active_contributor=contributor,
        )
        is not None
    )


def test_repeated_candidate_steps_mint_distinct_ordered_direct_authority(
    tmp_path: Path,
) -> None:
    path = tmp_path / "authority.json"
    store = PluginAuthorityStore(path=path)
    connection = _connection_authority()
    store.publish_connection(connection, expected_revision="")
    plan = _repeated_candidate_plan()
    contributor = {
        "source": "operator",
        "type": "command",
        "episode_id": plan.episode_id,
        "objective_sha256": plan.objective_sha256,
        "entailed_plan_digest": plugin_plan_digest(plan),
    }
    service = PluginAuthorityService(
        store=store,
        binding_resolver=_binding_resolver,
        active_episode_id=lambda: plan.episode_id,
    )

    issued = tuple(
        service.authorize_direct_turn(
            plan,
            connection=connection,
            connection_snapshot=_connection_snapshot(),
            candidate_id=step.candidate_id,
            step_id=step.step_id,
            active_contributor=contributor,
        )
        for step in plan.steps
    )

    assert all(authority is not None for authority in issued)
    assert tuple(authority.plan_step_id for authority in issued if authority is not None) == (
        "step-1",
        "step-2",
    )
    assert tuple(authority.arguments_sha256 for authority in issued if authority is not None) == (
        sha256_hex(canonical_bytes({"title": "first"})),
        sha256_hex(canonical_bytes({"title": "second"})),
    )


def test_repeated_candidate_steps_bind_exact_ordered_standing_authority(
    tmp_path: Path,
) -> None:
    path = tmp_path / "authority.json"
    store = PluginAuthorityStore(path=path)
    connection = _connection_authority()
    store.publish_connection(connection, expected_revision="")
    plan = _repeated_candidate_plan()
    first_grant = _authority(plan=plan, kind="standing_grant", job_revision="a" * 64)
    second_step = plan.steps[1]
    second_grant = replace(
        first_grant,
        authority_id="authority-2",
        authority_revision="b" * 64,
        plan_step_id=second_step.step_id,
        arguments_sha256=sha256_hex(canonical_bytes({"title": "second"})),
    )
    store.publish_standing_grant(first_grant, expected_revision="")
    store.publish_standing_grant(second_grant, expected_revision="")
    service = PluginAuthorityService(store=store, binding_resolver=_binding_resolver)

    first = service.authorize_standing_grant(
        plan,
        authority_id=first_grant.authority_id,
        job_id="job-1",
        job_revision="a" * 64,
        execution_revision=first_grant.authority_revision,
        candidate_id=plan.steps[0].candidate_id,
        step_id=plan.steps[0].step_id,
        connection=connection,
    )
    second = service.authorize_standing_grant(
        plan,
        authority_id=second_grant.authority_id,
        job_id="job-1",
        job_revision="a" * 64,
        execution_revision=second_grant.authority_revision,
        candidate_id=second_step.candidate_id,
        step_id=second_step.step_id,
        connection=connection,
    )

    assert first is not None and second is not None
    assert (first.plan_step_id, second.plan_step_id) == ("step-1", "step-2")
    assert (
        service.authorize_standing_grant(
            plan,
            authority_id=first_grant.authority_id,
            job_id="job-1",
            job_revision="a" * 64,
            execution_revision="3" * 64,
            candidate_id=second_step.candidate_id,
            step_id=second_step.step_id,
            connection=connection,
        )
        is None
    )


class _StoreAbort(BaseException):
    pass


def _store_failures() -> tuple[BaseException, ...]:
    return (
        RuntimeError("store_failed"),
        asyncio.CancelledError("store_cancelled"),
        KeyboardInterrupt("store_interrupted"),
        _StoreAbort("store_aborted"),
    )


def _direct_context(
    path: Path,
) -> tuple[
    PluginAuthorityStore,
    PluginAuthorityService,
    PluginConnectionAuthority,
    PluginOperationPlan,
    dict[str, object],
]:
    store = PluginAuthorityStore(path=path)
    connection = _connection_authority()
    store.publish_connection(connection, expected_revision="")
    plan = _plan()
    contributor: dict[str, object] = {
        "source": "operator",
        "type": "command",
        "episode_id": plan.episode_id,
        "objective_sha256": plan.objective_sha256,
        "entailed_plan_digest": plugin_plan_digest(plan),
    }
    return (
        store,
        PluginAuthorityService(
            store=store,
            binding_resolver=_binding_resolver,
            active_episode_id=lambda: plan.episode_id,
        ),
        connection,
        plan,
        contributor,
    )


def _authorize_direct(
    service: PluginAuthorityService,
    connection: PluginConnectionAuthority,
    plan: PluginOperationPlan,
    contributor: dict[str, object],
) -> McpAuthoritySnapshot | None:
    return service.authorize_direct_turn(
        plan,
        connection=connection,
        connection_snapshot=_connection_snapshot(),
        candidate_id=plan.steps[0].candidate_id,
        step_id=plan.steps[0].step_id,
        active_contributor=contributor,
    )


@pytest.mark.parametrize("failure", _store_failures(), ids=lambda value: type(value).__name__)
def test_reserve_publish_then_raise_reconciles_exact_direct_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    store, service, connection, plan, contributor = _direct_context(tmp_path / "reserve.json")
    real_reserve = store.reserve_execution

    def publish_then_raise(**kwargs: object) -> str | None:
        real_reserve(**kwargs)
        raise failure

    monkeypatch.setattr(store, "reserve_execution", publish_then_raise)
    with pytest.raises(type(failure)) as caught:
        _authorize_direct(service, connection, plan, contributor)
    assert caught.value is failure

    monkeypatch.setattr(store, "reserve_execution", real_reserve)
    assert _authorize_direct(service, connection, plan, contributor) is not None


@pytest.mark.parametrize("failure", _store_failures(), ids=lambda value: type(value).__name__)
def test_commit_publish_then_raise_reconciles_exact_direct_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    store, service, connection, plan, contributor = _direct_context(tmp_path / "commit.json")
    real_commit = store.commit_execution

    def publish_then_raise(execution_key: str, *, expected_revision: str) -> None:
        real_commit(execution_key, expected_revision=expected_revision)
        raise failure

    monkeypatch.setattr(store, "commit_execution", publish_then_raise)
    with pytest.raises(type(failure)) as caught:
        _authorize_direct(service, connection, plan, contributor)
    assert caught.value is failure

    monkeypatch.setattr(store, "commit_execution", real_commit)
    assert _authorize_direct(service, connection, plan, contributor) is not None


@pytest.mark.parametrize("failure", _store_failures(), ids=lambda value: type(value).__name__)
@pytest.mark.parametrize("published", (False, True), ids=("before_publish", "after_publish"))
def test_rollback_failure_reconciles_without_masking_active_baseexception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
    published: bool,
) -> None:
    store, service, connection, plan, contributor = _direct_context(tmp_path / "rollback.json")
    primary = _StoreAbort("issuance_aborted")
    real_rollback = store.rollback_execution
    real_issue = authority_module._AuthorityIssuer.issue

    def abort_issue(_issuer: object, _authority: McpAuthoritySnapshot) -> None:
        raise primary

    def fail_rollback(
        execution_key: str, *, expected_revision: str, reservation_id: str = ""
    ) -> None:
        if published:
            real_rollback(
                execution_key,
                expected_revision=expected_revision,
                reservation_id=reservation_id,
            )
        raise failure

    monkeypatch.setattr(authority_module._AuthorityIssuer, "issue", abort_issue)
    monkeypatch.setattr(store, "rollback_execution", fail_rollback)
    with pytest.raises(_StoreAbort) as caught:
        _authorize_direct(service, connection, plan, contributor)
    assert caught.value is primary

    monkeypatch.setattr(authority_module._AuthorityIssuer, "issue", real_issue)
    monkeypatch.setattr(store, "rollback_execution", real_rollback)
    assert _authorize_direct(service, connection, plan, contributor) is not None


def test_direct_lineage_is_bounded_to_the_current_active_episode_across_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "direct-lineage.json"
    store = PluginAuthorityStore(path=path, max_records=3)
    connection = _connection_authority()
    store.publish_connection(connection, expected_revision="")
    active = ["episode-0"]

    def service() -> PluginAuthorityService:
        return PluginAuthorityService(
            store=PluginAuthorityStore(path=path, max_records=3),
            binding_resolver=_binding_resolver,
            active_episode_id=lambda: active[0],
        )

    first_plan = _plan(episode_id=active[0])
    first_contributor = {
        "source": "operator",
        "type": "command",
        "episode_id": first_plan.episode_id,
        "objective_sha256": first_plan.objective_sha256,
        "entailed_plan_digest": plugin_plan_digest(first_plan),
    }
    assert _authorize_direct(service(), connection, first_plan, first_contributor) is not None

    for index in range(1, 301):
        active[0] = f"episode-{index}"
        plan = _plan(episode_id=active[0])
        contributor = {
            "source": "operator",
            "type": "command",
            "episode_id": plan.episode_id,
            "objective_sha256": plan.objective_sha256,
            "entailed_plan_digest": plugin_plan_digest(plan),
        }
        assert _authorize_direct(service(), connection, plan, contributor) is not None

    assert _authorize_direct(service(), connection, first_plan, first_contributor) is None


def test_standing_grant_rotates_one_durable_current_execution_revision(
    tmp_path: Path,
) -> None:
    path = tmp_path / "standing-rotation.json"
    store = PluginAuthorityStore(path=path, max_records=3)
    connection = _connection_authority()
    store.publish_connection(connection, expected_revision="")
    plan = _plan()
    standing = _authority(plan=plan, kind="standing_grant", job_revision="a" * 64)
    store.publish_standing_grant(standing, expected_revision="")
    current_revision = standing.authority_revision

    for _index in range(300):
        issued = PluginAuthorityService(
            store=PluginAuthorityStore(path=path, max_records=3),
            binding_resolver=_binding_resolver,
        ).authorize_standing_grant(
            plan,
            authority_id=standing.authority_id,
            job_id="job-1",
            job_revision="a" * 64,
            execution_revision=current_revision,
            candidate_id=plan.steps[0].candidate_id,
            step_id=plan.steps[0].step_id,
            connection=connection,
        )
        assert issued is not None
        assert issued.authority_revision != current_revision
        assert (
            PluginAuthorityStore(path=path, max_records=3)
            .authority("standing_grant", standing.authority_id)
            .authority_revision
            == issued.authority_revision
        )
        assert (
            PluginAuthorityService(
                store=PluginAuthorityStore(path=path, max_records=3),
                binding_resolver=_binding_resolver,
            ).authorize_standing_grant(
                plan,
                authority_id=standing.authority_id,
                job_id="job-1",
                job_revision="a" * 64,
                execution_revision=current_revision,
                candidate_id=plan.steps[0].candidate_id,
                step_id=plan.steps[0].step_id,
                connection=connection,
            )
            is None
        )
        current_revision = issued.authority_revision


def test_republishing_standing_grant_retires_prior_execution_revision(tmp_path: Path) -> None:
    path = tmp_path / "standing-republish.json"
    store = PluginAuthorityStore(path=path, max_records=3)
    connection = _connection_authority()
    store.publish_connection(connection, expected_revision="")
    plan = _plan()
    standing = _authority(plan=plan, kind="standing_grant", job_revision="a" * 64)
    store.publish_standing_grant(standing, expected_revision="")
    first = PluginAuthorityService(
        store=store,
        binding_resolver=_binding_resolver,
    ).authorize_standing_grant(
        plan,
        authority_id=standing.authority_id,
        job_id="job-1",
        job_revision="a" * 64,
        execution_revision=standing.authority_revision,
        candidate_id=plan.steps[0].candidate_id,
        step_id=plan.steps[0].step_id,
        connection=connection,
    )
    assert first is not None
    replacement = replace(
        store.authority("standing_grant", standing.authority_id),
        authority_revision="e" * 64,
    )
    store.publish_standing_grant(
        replacement,
        expected_revision=first.authority_revision,
    )

    issued = PluginAuthorityService(
        store=PluginAuthorityStore(path=path, max_records=3),
        binding_resolver=_binding_resolver,
    ).authorize_standing_grant(
        plan,
        authority_id=standing.authority_id,
        job_id="job-1",
        job_revision="a" * 64,
        execution_revision=replacement.authority_revision,
        candidate_id=plan.steps[0].candidate_id,
        step_id=plan.steps[0].step_id,
        connection=connection,
    )

    assert issued is not None


def test_schema_two_authority_state_fails_closed_without_authentication_kind(
    tmp_path: Path,
) -> None:
    """Catches inferring OAuth versus credentialless authority from legacy scopes."""
    import json

    store = PluginAuthorityStore(path=tmp_path / "authority.json")
    connection = _connection_authority()
    store.publish_connection(connection, expected_revision="")
    path = tmp_path / "authority.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["schema_version"] = 2
    document["executions"] = {
        "8" * 64: {
            "execution_key": "8" * 64,
            "kind": "direct_turn",
            "connection_key": next(iter(document["connections"])),
            "status": "reserved",
            "process_epoch": "9" * 64,
            "reservation_id": "a" * 64,
            "revision": "b" * 64,
        }
    }
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match="^plugin_connection_authority_not_found$"):
        store.connection(connection.plugin_id, connection.app_id, connection.account_key_hash)
    migrated = json.loads(path.read_text(encoding="utf-8"))
    assert migrated == {
        "schema_version": 4,
        "connections": {},
        "authorities": {},
        "executions": {},
    }


def test_legacy_authentication_ambiguous_connection_authority_fails_closed(
    tmp_path: Path,
) -> None:
    import json

    path = tmp_path / "legacy-authority.json"
    store = PluginAuthorityStore(path=path)
    connection = _connection_authority()
    store.publish_connection(connection, expected_revision="")
    legacy = json.loads(path.read_text(encoding="utf-8"))
    legacy["schema_version"] = 3
    for record in legacy["connections"].values():
        record.pop("authority_kind")
    path.write_text(json.dumps(legacy), encoding="utf-8")

    with pytest.raises(ValueError, match="^plugin_connection_authority_not_found$"):
        PluginAuthorityStore(path=path).connection(
            connection.plugin_id,
            connection.app_id,
            connection.account_key_hash,
        )

    migrated = json.loads(path.read_text(encoding="utf-8"))
    assert migrated == {
        "schema_version": 4,
        "connections": {},
        "authorities": {},
        "executions": {},
    }


def test_restart_rolls_back_an_undelivered_authority_reservation(tmp_path: Path, monkeypatch) -> None:
    """Catches a reservation surviving process restart as usable authority."""
    import json
    import mcp_platform.plugin_authority as authority_store_module

    store = PluginAuthorityStore(path=tmp_path / "authority.json")
    connection = _connection_authority()
    store.publish_connection(connection, expected_revision="")
    store.reserve_execution(
        kind="direct_turn",
        execution_key="c" * 64,
        plugin_id=connection.plugin_id,
        app_id=connection.app_id,
        account_key_hash=connection.account_key_hash,
        process_epoch="d" * 64,
        reservation_id="e" * 64,
        lineage_key="f" * 64,
        active_episode_id="episode-1",
    )

    monkeypatch.setattr(authority_store_module, "_PROCESS_EPOCH", "0" * 64)
    store.reconcile()
    assert store.connection(connection.plugin_id, connection.app_id, connection.account_key_hash)
    recovered = json.loads((tmp_path / "authority.json").read_text(encoding="utf-8"))
    assert recovered["executions"] == {}


# --- discovered (external_unreviewed) capability authority -------------------


def test_external_unreviewed_risk_matches_no_elevated_authority_predicate() -> None:
    """Catches a widened elevated-authority comparison silently covering EXTERNAL_UNREVIEWED.

    ``mcp_platform.plugin_discovery`` classifies every capability it
    enumerates from a publisher-verified or personal connection as
    ``RiskClass.EXTERNAL_UNREVIEWED`` -- never an Omega-signed risk
    declaration. ``PluginAuthorityService._snapshot`` derives
    ``allow_send_publish``, ``allow_destructive``, and
    ``allow_cross_provider`` from exactly the three ``is`` comparisons this
    pins; none of them can ever match ``EXTERNAL_UNREVIEWED``, so a
    discovered capability can never carry elevated authority by construction,
    and MCP annotations reported by the publisher have no way to widen that.
    """

    risk = RiskClass.EXTERNAL_UNREVIEWED
    assert risk is not RiskClass.SEND_PUBLISH
    assert risk is not RiskClass.DESTRUCTIVE
    assert risk is not RiskClass.CROSS_PROVIDER_TRANSFER


def test_external_unreviewed_risk_has_no_autonomous_authority_and_requires_connection_scope(
    tmp_path: Path,
) -> None:
    """A discovered capability may execute only inside the user's live connection turn.

    ``mcp_platform.operation_contracts._RISK_CLASSES`` now admits
    ``external_unreviewed`` (mcp_platform.plugin_discovery's classification for
    every capability a publisher-verified or personal connection enumerates), so
    ``PluginAuthorityService.authorize_direct_turn`` can issue a snapshot for it.
    That snapshot never carries autonomous/standing authority
    (``standing_execution`` is always false for a ``direct_turn`` authorization)
    and never carries elevated send/publish, destructive, or cross-provider
    authority -- only the user's own current, direct connection scope
    (``direct_execution``) can ever invoke it.
    """

    store = PluginAuthorityStore(path=tmp_path / "authority.json")
    connection = _connection_authority()
    store.publish_connection(connection, expected_revision="")

    plan = _plan()

    def unreviewed_resolver(plan, account_key_hash):
        assert account_key_hash == "7" * 64
        return tuple(
            _binding(candidate_id=step.candidate_id, risk=RiskClass.EXTERNAL_UNREVIEWED)
            for step in plan.steps
        )

    service = PluginAuthorityService(
        store=store,
        binding_resolver=unreviewed_resolver,
        active_episode_id=lambda: plan.episode_id,
    )
    contributor = {
        "source": "operator",
        "type": "command",
        "episode_id": plan.episode_id,
        "objective_sha256": plan.objective_sha256,
        "entailed_plan_digest": plugin_plan_digest(plan),
    }

    snapshot = service.authorize_direct_turn(
        plan,
        connection=connection,
        connection_snapshot=_connection_snapshot(),
        candidate_id=plan.steps[0].candidate_id,
        step_id=plan.steps[0].step_id,
        active_contributor=contributor,
    )

    assert snapshot is not None
    assert snapshot.risk_classes == ("external_unreviewed",)
    assert snapshot.direct_execution is True
    assert snapshot.standing_execution is False
    assert snapshot.allow_send_publish is False
    assert snapshot.allow_destructive is False
    assert snapshot.allow_cross_provider is False


# --- constraint-scoped ("template") standing grants -------------------------


def _template_authority(
    *,
    plan: PluginOperationPlan,
    allowed_tools: tuple[dict[str, str], ...],
    argument_constraints: dict[str, dict[str, object]],
    tool_name: str = "issue.create",
    authority_id: str = "authority-template-1",
    authority_revision: str = "9" * 64,
    job_id: str = "job-1",
    job_revision: str = "a" * 64,
    capability_tags: tuple[str, ...] = ("tool:issues.write",),
    risk_classes: tuple[str, ...] = (RiskClass.ADDITIVE_WRITE.value,),
    connection_authority_revision: str | None = None,
) -> McpAuthoritySnapshot:
    return McpAuthoritySnapshot(
        authority_kind="standing_grant",
        authority_id=authority_id,
        authority_revision=authority_revision,
        episode_id=plan.episode_id,
        job_id=job_id,
        job_revision=job_revision,
        connection_id="connection-1",
        connection_revision="5" * 64,
        server_id="server.one",
        server_version="1.0.0",
        artifact_sha256="6" * 64,
        tool_name=tool_name,
        tool_schema_sha256=_INPUT_SCHEMA_SHA256,
        capability_tags=capability_tags,
        source_constraints={"project": "alpha"},
        destination_constraints={"project": "alpha"},
        risk_classes=risk_classes,
        direct_execution=False,
        standing_execution=True,
        allow_send_publish=False,
        allow_destructive=False,
        allow_cross_provider=False,
        account_key_hash="7" * 64,
        connection_authority_revision=(
            _connection_authority().revision
            if connection_authority_revision is None
            else connection_authority_revision
        ),
        effect="create",
        allowed_tools=allowed_tools,
        argument_constraints=argument_constraints,
    )


def _issue_creates_entry() -> dict[str, str]:
    return {
        "tool_name": "issue.create",
        "tool_schema_sha256": _INPUT_SCHEMA_SHA256,
        "risk_class": RiskClass.ADDITIVE_WRITE.value,
        "effect": "create",
        "capability": "issues.write",
    }


def test_template_authority_snapshot_requires_connection_context_but_no_plan_ephemera() -> None:
    plan = _plan()
    grant = _template_authority(
        plan=plan,
        allowed_tools=(_issue_creates_entry(),),
        argument_constraints={"issue.create": {"title": {}}},
    )

    assert grant.plan_digest == ""
    assert grant.candidate_id == ""
    assert grant.plan_step_id == ""
    assert grant.arguments_sha256 == ""
    assert grant.account_key_hash == "7" * 64
    assert grant.connection_authority_revision == _connection_authority().revision
    assert grant.effect == "create"
    assert tuple(dict(entry) for entry in grant.allowed_tools) == (_issue_creates_entry(),)
    # `evaluate_argument_constraints` type-checks its `constraints` argument
    # with `type(...) is dict`, so the per-tool value must be a plain dict --
    # not a `MappingProxyType` -- even though the outer mapping is immutable.
    assert set(grant.argument_constraints) == {"issue.create"}
    assert type(grant.argument_constraints["issue.create"]) is dict
    assert grant.argument_constraints["issue.create"]["title"] == {}

    with pytest.raises(ValueError, match="^invalid_authority_snapshot$"):
        replace(grant, authority_kind="direct_turn", job_id="", job_revision="")


def test_publish_standing_grant_still_rejects_context_free_snapshot(tmp_path: Path) -> None:
    """A grant carrying neither plan ephemera nor argument constraints stays unpublishable."""

    store = PluginAuthorityStore(path=tmp_path / "authority.json")
    connection = _connection_authority()
    store.publish_connection(connection, expected_revision="")
    context_free = replace(
        _authority(kind="standing_grant", job_revision="a" * 64),
        plan_digest="",
        account_key_hash="",
        candidate_id="",
        plan_step_id="",
        arguments_sha256="",
        connection_authority_revision="",
        effect="",
    )

    with pytest.raises(ValueError, match="^plugin_authority_invalid$"):
        store.publish_standing_grant(context_free, expected_revision="")


def test_template_grant_admits_in_constraint_arguments(tmp_path: Path) -> None:
    store = PluginAuthorityStore(path=tmp_path / "authority.json")
    connection = _connection_authority()
    store.publish_connection(connection, expected_revision="")
    plan = _plan(argument="quarterly")
    grant = _template_authority(
        plan=plan,
        allowed_tools=(_issue_creates_entry(),),
        argument_constraints={
            "issue.create": {"title": {"one_of": ["quarterly"], "required": True}}
        },
    )
    store.publish_standing_grant(grant, expected_revision="")
    service = PluginAuthorityService(store=store, binding_resolver=_binding_resolver)

    authorized = service.authorize_standing_grant(
        plan,
        authority_id=grant.authority_id,
        job_id="job-1",
        job_revision="a" * 64,
        execution_revision=grant.authority_revision,
        candidate_id=plan.steps[0].candidate_id,
        step_id=plan.steps[0].step_id,
        connection=connection,
    )

    assert authorized is not None
    assert authorized.authority_revision != grant.authority_revision
    assert (
        PluginAuthorityStore(path=store.path)
        .authority("standing_grant", grant.authority_id)
        .authority_revision
        == authorized.authority_revision
    )


@pytest.mark.parametrize(
    "mutate",
    (
        lambda arguments: {**arguments, "title": "not-quarterly"},
        lambda arguments: {**arguments, "extra": "x"},
    ),
    ids=("out_of_constraint_value", "undeclared_extra_key"),
)
def test_template_grant_rejects_out_of_constraint_arguments(mutate, tmp_path: Path) -> None:
    store = PluginAuthorityStore(path=tmp_path / "authority.json")
    connection = _connection_authority()
    store.publish_connection(connection, expected_revision="")
    plan = _plan(argument="quarterly")
    bad_plan = replace(
        plan,
        steps=(replace(plan.steps[0], arguments=mutate(dict(plan.steps[0].arguments))),),
    )
    grant = _template_authority(
        plan=plan,
        allowed_tools=(_issue_creates_entry(),),
        argument_constraints={
            "issue.create": {"title": {"one_of": ["quarterly"], "required": True}}
        },
    )
    store.publish_standing_grant(grant, expected_revision="")
    service = PluginAuthorityService(store=store, binding_resolver=_binding_resolver)

    authorized = service.authorize_standing_grant(
        bad_plan,
        authority_id=grant.authority_id,
        job_id="job-1",
        job_revision="a" * 64,
        execution_revision=grant.authority_revision,
        candidate_id=bad_plan.steps[0].candidate_id,
        step_id=bad_plan.steps[0].step_id,
        connection=connection,
    )

    assert authorized is None


def test_template_grant_rejects_tool_not_named_in_allowed_tools(tmp_path: Path) -> None:
    store = PluginAuthorityStore(path=tmp_path / "authority.json")
    connection = _connection_authority()
    store.publish_connection(connection, expected_revision="")
    plan = _plan()
    grant = _template_authority(
        plan=plan,
        allowed_tools=(
            {
                "tool_name": "issue.delete",
                "tool_schema_sha256": _INPUT_SCHEMA_SHA256,
                "risk_class": RiskClass.ADDITIVE_WRITE.value,
                "effect": "delete",
                # Deliberately the connection's real (single) capability,
                # not "issues.delete" -- fix round 2's publish-time identity
                # check requires every declared capability_tags entry to be
                # in the connection's ceiling, and this connection only
                # ceils "issues.write". The tool_name mismatch below is what
                # this test is actually about; the capability here just
                # needs to satisfy publish so authorization is reached.
                "capability": "issues.write",
            },
        ),
        argument_constraints={"issue.delete": {}},
    )
    store.publish_standing_grant(grant, expected_revision="")
    service = PluginAuthorityService(store=store, binding_resolver=_binding_resolver)

    authorized = service.authorize_standing_grant(
        plan,
        authority_id=grant.authority_id,
        job_id="job-1",
        job_revision="a" * 64,
        execution_revision=grant.authority_revision,
        candidate_id=plan.steps[0].candidate_id,
        step_id=plan.steps[0].step_id,
        connection=connection,
    )

    assert authorized is None


def test_template_grant_settings_revision_change_retires_it_too(tmp_path: Path) -> None:
    """Mirrors ``test_settings_revision_change_retires_linked_standing_grants`` for templates."""

    store = PluginAuthorityStore(path=tmp_path / "authority.json")
    first = _connection_authority(policy=PluginExecutionPolicy.DIRECT_TURN)
    store.publish_connection(first, expected_revision="")
    plan = _plan()
    grant = _template_authority(
        plan=plan,
        allowed_tools=(_issue_creates_entry(),),
        argument_constraints={"issue.create": {"title": {}}},
    )
    store.publish_standing_grant(grant, expected_revision="")
    changed = _connection_authority(policy=PluginExecutionPolicy.CONFIRM_EACH)

    store.publish_connection(changed, expected_revision=first.revision)

    with pytest.raises(ValueError, match="not_found"):
        store.authority("standing_grant", grant.authority_id)


def test_template_grant_execution_revision_is_single_use(tmp_path: Path) -> None:
    store = PluginAuthorityStore(path=tmp_path / "authority.json")
    connection = _connection_authority()
    store.publish_connection(connection, expected_revision="")
    plan = _plan()
    grant = _template_authority(
        plan=plan,
        allowed_tools=(_issue_creates_entry(),),
        argument_constraints={"issue.create": {"title": {}}},
    )
    store.publish_standing_grant(grant, expected_revision="")
    service = PluginAuthorityService(store=store, binding_resolver=_binding_resolver)

    def authorize(execution_revision: str) -> McpAuthoritySnapshot | None:
        return service.authorize_standing_grant(
            plan,
            authority_id=grant.authority_id,
            job_id="job-1",
            job_revision="a" * 64,
            execution_revision=execution_revision,
            candidate_id=plan.steps[0].candidate_id,
            step_id=plan.steps[0].step_id,
            connection=connection,
        )

    first = authorize(grant.authority_revision)
    replayed = authorize(grant.authority_revision)

    assert first is not None
    assert replayed is None

    second = authorize(first.authority_revision)
    assert second is not None
    assert second.authority_id != first.authority_id


def test_revoke_standing_grant_removes_only_that_authority_and_its_executions(
    tmp_path: Path,
) -> None:
    store = PluginAuthorityStore(path=tmp_path / "authority.json")
    connection = _connection_authority()
    store.publish_connection(connection, expected_revision="")
    plan = _repeated_candidate_plan()
    first_grant = _authority(plan=plan, kind="standing_grant", job_revision="a" * 64)
    second_step = plan.steps[1]
    second_grant = replace(
        first_grant,
        authority_id="authority-2",
        authority_revision="b" * 64,
        plan_step_id=second_step.step_id,
        arguments_sha256=sha256_hex(canonical_bytes({"title": "second"})),
    )
    store.publish_standing_grant(first_grant, expected_revision="")
    store.publish_standing_grant(second_grant, expected_revision="")
    service = PluginAuthorityService(store=store, binding_resolver=_binding_resolver)
    issued = service.authorize_standing_grant(
        plan,
        authority_id=first_grant.authority_id,
        job_id="job-1",
        job_revision="a" * 64,
        execution_revision=first_grant.authority_revision,
        candidate_id=plan.steps[0].candidate_id,
        step_id=plan.steps[0].step_id,
        connection=connection,
    )
    assert issued is not None

    store.revoke_standing_grant(first_grant.authority_id)

    with pytest.raises(ValueError, match="^plugin_authority_not_found$"):
        store.authority("standing_grant", first_grant.authority_id)
    assert store.authority("standing_grant", second_grant.authority_id) == second_grant
    assert (
        store.connection(connection.plugin_id, connection.app_id, connection.account_key_hash)
        == connection
    )

    # Idempotent: revoking an already-absent authority is a no-op, not an error.
    store.revoke_standing_grant(first_grant.authority_id)


# --- template grants spanning multiple allowed_tools (fix round 1) ----------

_READ_SCHEMA = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}
_READ_SCHEMA_SHA256 = sha256_hex(canonical_bytes(_READ_SCHEMA))


def _read_binding(*, candidate_id: str = "1" * 64) -> PluginToolBinding:
    # `effect="read"` (fix round 2): each allowed_tools entry now carries
    # its own `effect`/`capability`, so a template grant can bind tools with
    # genuinely different effects -- this fixture no longer needs the
    # round-1 workaround of sharing "create" across both tools.
    output = {"type": "object"}
    return PluginToolBinding(
        candidate_id=candidate_id,
        tool_name="issue.read",
        plugin_id="plugin.issues",
        plugin_revision="a" * 64,
        app_id="app.issues",
        account_key_hash="7" * 64,
        connection_revision="5" * 64,
        capability="issues.read",
        title="Read issue",
        description="Read one issue",
        input_schema=_READ_SCHEMA,
        input_schema_sha256=_READ_SCHEMA_SHA256,
        output_schema=output,
        output_schema_sha256=sha256_hex(canonical_bytes(output)),
        risk_class=RiskClass.READ_ONLY,
        effect="read",
        requires_revision=False,
        max_result_bytes=4096,
        allowed_mime_types=("application/json",),
    )


def _issue_reads_entry() -> dict[str, str]:
    return {
        "tool_name": "issue.read",
        "tool_schema_sha256": _READ_SCHEMA_SHA256,
        "risk_class": RiskClass.READ_ONLY.value,
        "effect": "read",
        "capability": "issues.read",
    }


def _two_capability_connection_authority() -> PluginConnectionAuthority:
    values = {
        "plugin_id": "plugin.issues",
        "app_id": "app.issues",
        "account_key_hash": "7" * 64,
        "connection_revision": "5" * 64,
        "provider_scopes": ("issues.write", "issues.read"),
        "capability_ceiling": ("tool:issues.write", "tool:issues.read"),
        "allowed_tool_schemas": {
            "issues.write": _INPUT_SCHEMA_SHA256,
            "issues.read": _READ_SCHEMA_SHA256,
        },
        "source_constraints": {"project": "alpha"},
        "destination_constraints": {"project": "alpha"},
        "execution_policy": PluginExecutionPolicy.DIRECT_TURN,
        "authority_kind": PluginConnectionAuthorityKind.OAUTH,
    }
    return PluginConnectionAuthority(
        **values,
        revision=plugin_connection_authority_revision(**values),
    )


def _two_tool_plan() -> PluginOperationPlan:
    return PluginOperationPlan(
        plan_id="2" * 64,
        episode_id="episode-1",
        objective_sha256="3" * 64,
        steps=(
            PluginOperationStep(
                step_id="step-1",
                candidate_id="1" * 64,
                arguments={"title": "quarterly"},
                consumes_step_ids=(),
            ),
            PluginOperationStep(
                step_id="step-2",
                candidate_id="1" * 64,
                arguments={},
                consumes_step_ids=("step-1",),
            ),
        ),
        authority_digest="4" * 64,
    )


def _two_tool_resolver(
    plan: PluginOperationPlan, account_key_hash: str
) -> tuple[PluginToolBinding, ...]:
    assert account_key_hash == "7" * 64
    return (
        _binding(candidate_id=plan.steps[0].candidate_id),
        _read_binding(candidate_id=plan.steps[1].candidate_id),
    )


def test_template_grant_spans_multiple_allowed_tools(tmp_path: Path) -> None:
    """A recurring job typically needs more than one tool (e.g. a read tool
    to watch a tracker and a write tool to file an issue); a template grant
    must be able to authorize any of them, not just the first."""

    store = PluginAuthorityStore(path=tmp_path / "authority.json")
    connection = _two_capability_connection_authority()
    store.publish_connection(connection, expected_revision="")
    plan = _two_tool_plan()
    grant = _template_authority(
        plan=plan,
        allowed_tools=(_issue_creates_entry(), _issue_reads_entry()),
        argument_constraints={"issue.create": {"title": {}}, "issue.read": {}},
        capability_tags=("tool:issues.write", "tool:issues.read"),
        risk_classes=(RiskClass.ADDITIVE_WRITE.value, RiskClass.READ_ONLY.value),
        connection_authority_revision=connection.revision,
    )
    store.publish_standing_grant(grant, expected_revision="")
    service = PluginAuthorityService(store=store, binding_resolver=_two_tool_resolver)

    first = service.authorize_standing_grant(
        plan,
        authority_id=grant.authority_id,
        job_id="job-1",
        job_revision="a" * 64,
        execution_revision=grant.authority_revision,
        candidate_id=plan.steps[0].candidate_id,
        step_id=plan.steps[0].step_id,
        connection=connection,
    )
    assert first is not None

    second = service.authorize_standing_grant(
        plan,
        authority_id=grant.authority_id,
        job_id="job-1",
        job_revision="a" * 64,
        execution_revision=first.authority_revision,
        candidate_id=plan.steps[1].candidate_id,
        step_id=plan.steps[1].step_id,
        connection=connection,
    )
    assert second is not None
    assert second.authority_id != first.authority_id


def test_template_grant_rejects_step_effect_disagreeing_with_its_entry(tmp_path: Path) -> None:
    """Fix round 2: effect is now bound per allowed_tools entry, not by the
    grant's single top-level ``effect`` scalar."""

    store = PluginAuthorityStore(path=tmp_path / "authority.json")
    connection = _connection_authority()
    store.publish_connection(connection, expected_revision="")
    plan = _plan()
    grant = _template_authority(
        plan=plan,
        allowed_tools=(_issue_creates_entry(),),  # entry effect is "create"
        argument_constraints={"issue.create": {"title": {}}},
    )
    store.publish_standing_grant(grant, expected_revision="")

    def wrong_effect_resolver(
        resolved_plan: PluginOperationPlan, account_key_hash: str
    ) -> tuple[PluginToolBinding, ...]:
        assert account_key_hash == "7" * 64
        return (
            replace(
                _binding(candidate_id=resolved_plan.steps[0].candidate_id), effect="update"
            ),
        )

    service = PluginAuthorityService(store=store, binding_resolver=wrong_effect_resolver)

    authorized = service.authorize_standing_grant(
        plan,
        authority_id=grant.authority_id,
        job_id="job-1",
        job_revision="a" * 64,
        execution_revision=grant.authority_revision,
        candidate_id=plan.steps[0].candidate_id,
        step_id=plan.steps[0].step_id,
        connection=connection,
    )

    assert authorized is None


def test_template_grant_rejects_step_capability_disagreeing_with_its_entry(tmp_path: Path) -> None:
    """Fix round 2: capability is now bound per allowed_tools entry, not by
    grant-level ``capability_tags`` membership (round 1's approach)."""

    store = PluginAuthorityStore(path=tmp_path / "authority.json")
    connection = _connection_authority()
    store.publish_connection(connection, expected_revision="")
    plan = _plan()
    grant = _template_authority(
        plan=plan,
        allowed_tools=(_issue_creates_entry(),),  # entry capability is "issues.write"
        argument_constraints={"issue.create": {"title": {}}},
    )
    store.publish_standing_grant(grant, expected_revision="")

    def wrong_capability_resolver(
        resolved_plan: PluginOperationPlan, account_key_hash: str
    ) -> tuple[PluginToolBinding, ...]:
        assert account_key_hash == "7" * 64
        return (
            replace(
                _binding(candidate_id=resolved_plan.steps[0].candidate_id),
                capability="issues.other",
            ),
        )

    service = PluginAuthorityService(store=store, binding_resolver=wrong_capability_resolver)

    authorized = service.authorize_standing_grant(
        plan,
        authority_id=grant.authority_id,
        job_id="job-1",
        job_revision="a" * 64,
        execution_revision=grant.authority_revision,
        candidate_id=plan.steps[0].candidate_id,
        step_id=plan.steps[0].step_id,
        connection=connection,
    )

    assert authorized is None


def test_publish_standing_grant_rejects_template_capability_tags_with_extra_uncovered_tag(
    tmp_path: Path,
) -> None:
    """The other direction of "disagrees": every tag must be backed by an
    allowed_tools entry, not just every entry backed by a tag."""

    store = PluginAuthorityStore(path=tmp_path / "authority.json")
    connection = _two_capability_connection_authority()
    store.publish_connection(connection, expected_revision="")
    plan = _plan()
    grant = _template_authority(
        plan=plan,
        allowed_tools=(_issue_creates_entry(),),  # only "issues.write"
        argument_constraints={"issue.create": {"title": {}}},
        capability_tags=("tool:issues.write", "tool:issues.read"),  # extra, uncovered
        connection_authority_revision=connection.revision,
    )

    with pytest.raises(ValueError, match="^plugin_authority_connection_ambiguous$"):
        store.publish_standing_grant(grant, expected_revision="")


def test_publish_standing_grant_rejects_template_schema_collision_with_mismatched_tags(
    tmp_path: Path,
) -> None:
    """Fix round 1's schema-set proxy could be fooled by two tools sharing a
    schema (see round 1's report, Concern B); round 2 replaced it with exact
    capability identity, so the same schema collision no longer masks a
    capability_tags mismatch."""

    connection_values = {
        "plugin_id": "plugin.issues",
        "app_id": "app.issues",
        "account_key_hash": "7" * 64,
        "connection_revision": "5" * 64,
        "provider_scopes": ("issues.write", "issues.read"),
        "capability_ceiling": ("tool:issues.write", "tool:issues.read"),
        "allowed_tool_schemas": {
            "issues.write": _INPUT_SCHEMA_SHA256,
            "issues.read": _INPUT_SCHEMA_SHA256,  # deliberately identical schema
        },
        "source_constraints": {"project": "alpha"},
        "destination_constraints": {"project": "alpha"},
        "execution_policy": PluginExecutionPolicy.DIRECT_TURN,
        "authority_kind": PluginConnectionAuthorityKind.OAUTH,
    }
    connection = PluginConnectionAuthority(
        **connection_values,
        revision=plugin_connection_authority_revision(**connection_values),
    )
    store = PluginAuthorityStore(path=tmp_path / "authority.json")
    store.publish_connection(connection, expected_revision="")

    read_entry = {
        "tool_name": "issue.read",
        "tool_schema_sha256": _INPUT_SCHEMA_SHA256,  # same schema as issue.create
        "risk_class": RiskClass.READ_ONLY.value,
        "effect": "read",
        "capability": "issues.read",
    }
    plan = _two_tool_plan()
    grant = _template_authority(
        plan=plan,
        allowed_tools=(_issue_creates_entry(), read_entry),
        argument_constraints={"issue.create": {"title": {}}, "issue.read": {}},
        # Only "issues.write" is declared even though allowed_tools names
        # two capabilities; under round 1's schema-set proxy this would
        # have published (both entries share one schema). Round 2's exact
        # capability-identity check refuses it.
        capability_tags=("tool:issues.write",),
        risk_classes=(RiskClass.ADDITIVE_WRITE.value, RiskClass.READ_ONLY.value),
        connection_authority_revision=connection.revision,
    )

    with pytest.raises(ValueError, match="^plugin_authority_connection_ambiguous$"):
        store.publish_standing_grant(grant, expected_revision="")


def test_publish_standing_grant_rejects_template_tag_outside_connection_ceiling(
    tmp_path: Path,
) -> None:
    store = PluginAuthorityStore(path=tmp_path / "authority.json")
    connection = _connection_authority()
    store.publish_connection(connection, expected_revision="")
    plan = _plan()
    grant = _template_authority(
        plan=plan,
        allowed_tools=(_issue_creates_entry(),),
        argument_constraints={"issue.create": {"title": {}}},
        capability_tags=("tool:issues.delete",),
    )

    with pytest.raises(ValueError, match="^plugin_authority_connection_ambiguous$"):
        store.publish_standing_grant(grant, expected_revision="")


def test_publish_standing_grant_rejects_template_allowed_tools_disagreeing_with_capability_tags(
    tmp_path: Path,
) -> None:
    store = PluginAuthorityStore(path=tmp_path / "authority.json")
    connection = _two_capability_connection_authority()
    store.publish_connection(connection, expected_revision="")
    plan = _two_tool_plan()
    grant = _template_authority(
        plan=plan,
        allowed_tools=(_issue_creates_entry(), _issue_reads_entry()),
        argument_constraints={"issue.create": {"title": {}}, "issue.read": {}},
        capability_tags=("tool:issues.write",),  # missing "tool:issues.read"
        risk_classes=(RiskClass.ADDITIVE_WRITE.value, RiskClass.READ_ONLY.value),
        connection_authority_revision=connection.revision,
    )

    with pytest.raises(ValueError, match="^plugin_authority_connection_ambiguous$"):
        store.publish_standing_grant(grant, expected_revision="")


def test_template_authority_snapshot_rejects_duplicate_capability_tags() -> None:
    plan = _plan()

    with pytest.raises(ValueError, match="^invalid_authority_snapshot$"):
        _template_authority(
            plan=plan,
            allowed_tools=(_issue_creates_entry(),),
            argument_constraints={"issue.create": {"title": {}}},
            capability_tags=("tool:issues.write", "tool:issues.write"),
        )


def test_publish_standing_grant_exact_hash_still_requires_exactly_one_capability_tag(
    tmp_path: Path,
) -> None:
    """The template fix must not loosen the exact-hash single-tag gate."""

    store = PluginAuthorityStore(path=tmp_path / "authority.json")
    connection = _two_capability_connection_authority()
    store.publish_connection(connection, expected_revision="")
    exact = replace(
        _authority(kind="standing_grant", job_revision="a" * 64),
        capability_tags=("tool:issues.write", "tool:issues.read"),
        connection_authority_revision=connection.revision,
    )

    with pytest.raises(ValueError, match="^plugin_authority_connection_ambiguous$"):
        store.publish_standing_grant(exact, expected_revision="")


# --- closed-vocabulary parser rejections (fix round 1, finding 2) -----------


def test_allowed_tools_rejects_duplicate_tool_name() -> None:
    plan = _plan()
    duplicated = (
        _issue_creates_entry(),
        {**_issue_creates_entry(), "risk_class": RiskClass.READ_ONLY.value},
    )

    with pytest.raises(ValueError, match="^invalid_authority_snapshot$"):
        _template_authority(
            plan=plan,
            allowed_tools=duplicated,
            argument_constraints={"issue.create": {}},
        )


@pytest.mark.parametrize(
    "mutate",
    (
        lambda entry: {k: v for k, v in entry.items() if k != "risk_class"},
        lambda entry: {**entry, "extra_unexpected_key": "x"},
        lambda entry: {**entry, "tool_name": 123},
        lambda entry: {**entry, "risk_class": "not_a_real_risk_class"},
        lambda entry: {k: v for k, v in entry.items() if k != "effect"},
        lambda entry: {k: v for k, v in entry.items() if k != "capability"},
    ),
    ids=(
        "missing_key",
        "unknown_key",
        "non_string_value",
        "unknown_risk_class",
        "missing_effect",
        "missing_capability",
    ),
)
def test_allowed_tools_rejects_malformed_entry(mutate) -> None:
    plan = _plan()

    with pytest.raises(ValueError, match="^invalid_authority_snapshot$"):
        _template_authority(
            plan=plan,
            allowed_tools=(mutate(_issue_creates_entry()),),
            argument_constraints={"issue.create": {}},
        )


def test_argument_constraints_rejects_key_naming_a_tool_absent_from_allowed_tools() -> None:
    plan = _plan()

    with pytest.raises(ValueError, match="^invalid_authority_snapshot$"):
        _template_authority(
            plan=plan,
            allowed_tools=(_issue_creates_entry(),),
            argument_constraints={"issue.delete": {}},
        )


def test_snapshot_rejects_allowed_tools_present_with_empty_argument_constraints() -> None:
    plan = _plan()

    with pytest.raises(ValueError, match="^invalid_authority_snapshot$"):
        _template_authority(
            plan=plan,
            allowed_tools=(_issue_creates_entry(),),
            argument_constraints={},
        )
