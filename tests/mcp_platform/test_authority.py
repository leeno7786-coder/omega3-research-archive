from __future__ import annotations

import asyncio
import dataclasses
import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio
import pytest

import mcp_platform.authority as authority_module
import mcp_platform.service as service_module
from mcp_platform import action_ledger
from mcp_platform.authority import EXPECTED_CHECKS, McpAuthorityGateway
from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.external_switch import ExternalMcpSwitch
from mcp_platform.host import McpHost
from mcp_platform.ingress import build_mcp_queue_contributor
from mcp_platform.operation_contracts import (
    McpAuthoritySnapshot,
    McpConnectionSnapshot,
    McpOperationProposal,
    McpToolAuthority,
)
from mcp_platform.package_contracts import (
    MCP_PROTOCOL_VERSION,
    McpPackageManifest,
    NetworkClass,
    ResourceCatalog,
    RiskClass,
    ToolCatalog,
)
from mcp_platform.package_verifier import VerifiedPackage
from mcp_platform.registry import InstalledPackageRecord
from mcp_platform.plugin_authority import PluginAuthorityService, PluginAuthorityStore
from mcp_platform.source_contracts import (
    McpAdmissionReceipt,
    McpStructuredResult,
    deterministic_mcp_memory_ids,
)
from mcp_platform.supervisor import SupervisedSessionSnapshot
from tests.mcp_platform.test_operation_contracts import valid_proposal


def _tool_document(risk: RiskClass = RiskClass.READ_ONLY) -> dict[str, object]:
    is_write = risk is not RiskClass.READ_ONLY
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            **(
                {
                    "object_id": {"type": "string", "minLength": 1},
                    "expected_provider_revisions": {
                        "type": "object",
                        "additionalProperties": {"type": "string", "minLength": 1},
                    },
                }
                if is_write
                else {}
            ),
        },
        "required": [
            "query",
            *(["object_id", "expected_provider_revisions"] if is_write else []),
        ],
        "additionalProperties": False,
        **({"x-omega-provider-object-id-pointer": "/object_id"} if is_write else {}),
    }
    output_schema = {"type": "object"}
    return {
        "name": "omega.fake.search",
        "title": "Search fake workspace",
        "input_schema": input_schema,
        "output_schema": output_schema,
        "input_schema_sha256": sha256_hex(canonical_bytes(input_schema)),
        "output_schema_sha256": sha256_hex(canonical_bytes(output_schema)),
        "capability_tags": ["workspace.search"],
        "provider_extension_tags": [],
        "risk_class": risk.value,
        "effect": "signed effect",
        "requires_revision": risk is not RiskClass.READ_ONLY,
        "max_result_bytes": 8192,
        "allowed_mime_types": ["application/json"],
    }


def _verified_package(
    tmp_path: Path,
    risk: RiskClass,
    network_class: NetworkClass = NetworkClass.EXTERNAL,
) -> VerifiedPackage:
    tool_catalog = ToolCatalog.from_dict(
        {
            "catalog_version": 1,
            "server_id": "omega.first_party.external",
            "server_version": "1.0.0",
            "tools": [_tool_document(risk)],
        }
    )
    resource_catalog = ResourceCatalog.from_dict(
        {
            "catalog_version": 1,
            "server_id": "omega.first_party.external",
            "server_version": "1.0.0",
            "resources": [],
            "resource_templates": [],
        }
    )
    manifest = McpPackageManifest.from_dict(
        {
            "manifest_version": 1,
            "server_id": "omega.first_party.external",
            "publisher_id": "omega.first_party",
            "server_version": "1.0.0",
            "minimum_omega_version": "3.0.0",
            "supported_mcp_protocol_versions": [MCP_PROTOCOL_VERSION],
            "network_class": network_class.value,
            "platform": "windows-x86_64",
            "entrypoint": {"artifact": "server.exe", "arguments": []},
            "artifact_sha256": "a" * 64,
            "tool_catalog_sha256": sha256_hex(canonical_bytes(tool_catalog.to_dict())),
            "resource_catalog_sha256": sha256_hex(canonical_bytes(resource_catalog.to_dict())),
            "capability_tags": ["workspace.search"],
            "risk_declarations": [risk.value],
            "oauth_providers": (
                ["provider.fake"] if network_class is NetworkClass.EXTERNAL else []
            ),
            "allowed_network_destinations": (
                ["api.fake.invalid"] if network_class is NetworkClass.EXTERNAL else []
            ),
            "signing_key_id": "release-1",
            "signature": "signed",
        }
    )
    root = tmp_path / "package"
    root.mkdir(exist_ok=True)
    artifact = root / "server.exe"
    tools = root / "tool_catalog.json"
    resources = root / "resource_catalog.json"
    artifact.touch()
    tools.touch()
    resources.touch()
    return VerifiedPackage(
        manifest=manifest,
        manifest_sha256="e" * 64,
        artifact_path=artifact,
        tool_catalog_path=tools,
        resource_catalog_path=resources,
        tool_catalog=tool_catalog,
        resource_catalog=resource_catalog,
    )


class FakeRegistry:
    def __init__(self, package: VerifiedPackage) -> None:
        self.package = package
        self.record = dataclasses.replace(
            InstalledPackageRecord.from_verified(package), enabled=True
        )
        self.get_calls = 0

    def get(self, server_id: str) -> InstalledPackageRecord:
        self.get_calls += 1
        if server_id != self.record.server_id:
            raise RuntimeError("provider-registry-sentinel")
        return self.record

    def package_path(self, _record: InstalledPackageRecord) -> Path:
        return self.package.artifact_path.parent


class FakeSupervisor:
    def __init__(self, package: VerifiedPackage) -> None:
        self.package = package
        self.ensure_count = 0
        self.call_count = 0
        self.call_entered = threading.Event()
        self.release_call = threading.Event()
        self.block_call = False
        self.error: BaseException | None = None
        self.result = valid_result()
        self.protocol = MCP_PROTOCOL_VERSION
        self.stop_external_calls: list[float] = []
        self.arguments: list[dict[str, Any]] = []

    async def ensure_session(self, server_id: str) -> SupervisedSessionSnapshot:
        self.ensure_count += 1
        return SupervisedSessionSnapshot(
            server_id=server_id,
            state="ready",
            protocol_version=self.protocol,
            tool_catalog_sha256=self.package.manifest.tool_catalog_sha256,
            resource_catalog_sha256=self.package.manifest.resource_catalog_sha256,
        )

    async def call_tool(
        self, server_id: str, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        del server_id, name
        self.call_count += 1
        self.arguments.append(arguments)
        self.call_entered.set()
        if self.block_call:
            await anyio.to_thread.run_sync(self.release_call.wait)
        if self.error is not None:
            raise self.error
        return self.result

    def stop_external_sync(self, drain_timeout_s: float) -> dict[str, int]:
        self.stop_external_calls.append(drain_timeout_s)
        return {"sessions_closed": 1, "sessions_forced": 0, "errors": 0}


def valid_result() -> dict[str, Any]:
    return {
        "result_kind": "observation",
        "provider_object_id": "object-1",
        "provider_container_id": "container-1",
        "provider_revision": "revision-1",
        "provider_change_token": "",
        "event_kind": "current",
        "observed_at": "2026-08-11T12:00:00Z",
        "status": "current",
        "mime_type": "application/json",
        "content": {"body": "provider-content-sentinel"},
    }


def _admission(context, raw_result) -> McpAdmissionReceipt:
    result = McpStructuredResult.from_dict(raw_result)
    result_digest = sha256_hex(canonical_bytes(result.to_dict()))
    envelope = context.bind_result(
        result,
        result_digest=result_digest,
        source_ledger_receipt="src_" + "1" * 24,
    )
    memory_ids = deterministic_mcp_memory_ids(envelope)
    contributor = build_mcp_queue_contributor(
        envelope,
        guardian_receipt="2" * 64,
        guardian_block={"quarantined": 0, "screened_fields": 1},
        screened_content={"body": "screened"},
        memory_ids=memory_ids,
    )
    return McpAdmissionReceipt(
        source_envelope=envelope,
        guardian_receipt="2" * 64,
        memory_ids=memory_ids,
        enqueued=True,
        deduplicated=False,
        contributor=contributor,
    )


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        risk: RiskClass = RiskClass.READ_ONLY,
        network_class: NetworkClass = NetworkClass.EXTERNAL,
    ) -> None:
        self.package = _verified_package(tmp_path, risk, network_class)
        self.tool = self.package.tool_catalog.tools[0]
        self.registry = FakeRegistry(self.package)
        self.supervisor = FakeSupervisor(self.package)
        self.switch = ExternalMcpSwitch(tmp_path / "external-switch.json")
        off = self.switch.load()
        self.switch.set_enabled(True, expected_revision=off.revision)
        self.ledger_path = tmp_path / "action-ledger.json"
        self.connection_calls = 0
        self.authority_calls = 0
        self.ingress_calls = 0
        self.connection = McpConnectionSnapshot(
            connection_id="connection-1",
            connection_revision="b" * 64,
            server_id="omega.first_party.external",
            server_version="1.0.0",
            artifact_sha256="a" * 64,
            provider_id="provider.fake",
            account_ref="f" * 64,
            enabled=True,
            capability_ceiling=("workspace.search",),
            allowed_tool_schemas={self.tool.name: self.tool.input_schema_sha256},
            source_constraints={"folders": ["inbox"]},
            destination_constraints={},
            authority_revision="c" * 64,
        )
        arguments = (
            {
                "query": "inbox",
                "object_id": "object-1",
                "expected_provider_revisions": {"object-1": "revision-0"},
            }
            if risk is not RiskClass.READ_ONLY
            else {"query": "inbox"}
        )
        self.authority = McpAuthoritySnapshot(
            authority_kind="direct_turn",
            authority_id="authority-1",
            authority_revision="d" * 64,
            episode_id="episode-1",
            job_id="",
            job_revision="",
            connection_id="connection-1",
            connection_revision="b" * 64,
            server_id="omega.first_party.external",
            server_version="1.0.0",
            artifact_sha256="a" * 64,
            tool_name=self.tool.name,
            tool_schema_sha256=self.tool.input_schema_sha256,
            capability_tags=("workspace.search",),
            source_constraints={"folders": ["inbox"]},
            destination_constraints={},
            risk_classes=(risk.value,),
            direct_execution=True,
            standing_execution=False,
            allow_send_publish=risk is RiskClass.SEND_PUBLISH,
            allow_destructive=risk is RiskClass.DESTRUCTIVE,
            allow_cross_provider=risk is RiskClass.CROSS_PROVIDER_TRANSFER,
            plan_digest="e" * 64,
            account_key_hash="f" * 64,
            candidate_id="1" * 64,
            plan_step_id="step-1",
            arguments_sha256=sha256_hex(canonical_bytes(arguments)),
            connection_authority_revision="c" * 64,
            effect=self.tool.effect,
        )
        self.proposal = McpOperationProposal.from_dict(
            valid_proposal(
                tool_schema_sha256=self.tool.input_schema_sha256,
                account_ref="f" * 64,
                connection_authority_revision="c" * 64,
                expected_provider_revisions=(
                    {"object-1": "revision-0"} if risk is not RiskClass.READ_ONLY else {}
                ),
                arguments=arguments,
            )
        )
        self.gateway = self.make_gateway()

    def resolve_connection(self, _connection_id: str) -> McpConnectionSnapshot:
        self.connection_calls += 1
        return self.connection

    def resolve_authority(self, _kind: str, _identity: str) -> McpAuthoritySnapshot:
        self.authority_calls += 1
        return self.authority

    def admit(self, context, result) -> McpAdmissionReceipt:
        self.ingress_calls += 1
        return _admission(context, result)

    def make_gateway(
        self,
        *,
        connection_resolver: Callable | None = None,
        authority_resolver: Callable | None = None,
        timestamp: Callable[[], str] | None = None,
    ) -> McpAuthorityGateway:
        return McpAuthorityGateway(
            registry=self.registry,
            external_switch=self.switch,
            supervisor=self.supervisor,
            verifier=lambda _path: self.package,
            connection_resolver=connection_resolver or self.resolve_connection,
            authority_resolver=authority_resolver or self.resolve_authority,
            action_ledger_path=self.ledger_path,
            ingress_admitter=self.admit,
            timestamp=timestamp or (lambda: "2026-08-11T12:00:00Z"),
        )


def _replace_proposal(proposal: McpOperationProposal, **changes: object) -> McpOperationProposal:
    document = proposal.to_dict()
    document.update(changes)
    digest_material = dict(document)
    digest_material.pop("proposal_digest")
    document["proposal_digest"] = sha256_hex(canonical_bytes(digest_material))
    return McpOperationProposal.from_dict(document)


def test_gateway_and_plugin_path_independently_reject_same_argument_drift(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    drifted = _replace_proposal(harness.proposal, arguments={"query": "changed"})
    gateway_outcome = anyio.run(harness.gateway.execute, drifted)
    tool = McpToolAuthority(
        tool_name=harness.tool.name,
        input_schema_sha256=harness.tool.input_schema_sha256,
        capability_tags=harness.tool.capability_tags,
        risk_class=harness.tool.risk_class,
        effect=harness.tool.effect,
        requires_revision=harness.tool.requires_revision,
    )
    plugin_decision = PluginAuthorityService(
        store=PluginAuthorityStore(path=tmp_path / "plugin-authority.json"),
        binding_resolver=lambda _plan, _account_hash: (),
    ).evaluate_operation(
        drifted,
        connection=harness.connection,
        authority=harness.authority,
        tool=tool,
    )

    assert gateway_outcome.failed_check == "authority_validity"
    assert plugin_decision.failed_check == "authority_validity"


def test_gateway_never_issues_a_reconstructed_plugin_execution_snapshot(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    plugin_authority_id = "1" * 64
    harness.authority = dataclasses.replace(
        harness.authority,
        authority_id=plugin_authority_id,
    )
    proposal = _replace_proposal(harness.proposal, authority_id=plugin_authority_id)

    def reconstruct(_kind: str, _authority_id: str) -> McpAuthoritySnapshot:
        return McpAuthoritySnapshot(**harness.authority.__dict__)

    gateway = harness.make_gateway(authority_resolver=reconstruct)
    outcome = anyio.run(gateway.execute, proposal)

    assert outcome.failed_check == "authority_validity"
    assert harness.supervisor.call_count == 0


def test_gateway_accepts_the_exact_issued_plugin_execution_handle(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    plugin_authority_id = "1" * 64
    harness.authority = authority_module._AuthorityIssuer().issue(
        dataclasses.replace(
            harness.authority,
            authority_id=plugin_authority_id,
        )
    )
    proposal = _replace_proposal(harness.proposal, authority_id=plugin_authority_id)

    outcome = anyio.run(harness.gateway.execute, proposal)

    assert outcome.status == "confirmed"
    assert outcome.failed_check is None
    assert harness.supervisor.call_count == 1


def _seed_idempotency(harness: Harness, *, state: str, proposal_digest: str | None = None) -> None:
    record = action_ledger.record_dispatch(
        omega_action_id="act-existing",
        episode_id=harness.proposal.episode_id,
        request_id=harness.proposal.request_id,
        server_id=harness.proposal.server_id,
        server_version=harness.proposal.server_version,
        artifact_sha256=harness.proposal.artifact_sha256,
        provider=harness.proposal.provider_id,
        account=harness.proposal.account_ref,
        connection_id=harness.proposal.connection_id,
        connection_revision=harness.proposal.connection_revision,
        capability="workspace.search",
        operation=harness.proposal.tool_name,
        tool_schema_sha256=harness.proposal.tool_schema_sha256,
        risk_class=harness.tool.risk_class.value,
        authority_kind=harness.proposal.authority_kind,
        authority_id=harness.proposal.authority_id,
        authority_revision=harness.proposal.authority_revision,
        proposal_digest=proposal_digest or harness.proposal.proposal_digest,
        source_constraints_sha256=sha256_hex(
            canonical_bytes(harness.proposal.source_constraints_dict())
        ),
        destination_constraints_sha256=sha256_hex(
            canonical_bytes(harness.proposal.destination_constraints_dict())
        ),
        idempotency_key=harness.proposal.idempotency_key,
        job_revision=harness.authority.job_revision,
        grant_id=(
            harness.authority.authority_id
            if harness.authority.authority_kind == "standing_grant"
            else ""
        ),
        grant_approval_digest=(
            harness.authority.authority_revision
            if harness.authority.authority_kind == "standing_grant"
            else ""
        ),
        expected_revision=sha256_hex(
            canonical_bytes(harness.proposal.expected_provider_revisions_dict())
        ),
        is_write=harness.tool.risk_class is not RiskClass.READ_ONLY,
        path=harness.ledger_path,
    )
    if state == "confirmed":
        action_ledger.record_receipt(
            record["omega_action_id"],
            provider_object_id="object-1",
            provider_revision="revision-1",
            receipt={
                "kind": "observation",
                "provider_id": harness.proposal.provider_id,
                "provider_object_id": "object-1",
                "provider_revision": "revision-1",
                "status": "current",
                "event_kind": "current",
                "count": 1,
                "deduped": False,
                "via": "mcp_authority_gateway",
                "omega_action_id": record["omega_action_id"],
            },
            path=harness.ledger_path,
        )
    elif state == "failed":
        action_ledger.record_failure(
            record["omega_action_id"], "provider_rejected", path=harness.ledger_path
        )
    elif state == "outcome_unknown":
        action_ledger.record_failure(
            record["omega_action_id"],
            "provider_outcome_unknown",
            ambiguous=True,
            path=harness.ledger_path,
        )


@pytest.mark.parametrize("failed_check", EXPECTED_CHECKS)
def test_every_admission_check_fails_in_order_without_dispatch(
    tmp_path: Path, failed_check: str
) -> None:
    risk = (
        RiskClass.MUTATING_WRITE
        if failed_check in {"provider_revision_precondition", "risk_specific_authority"}
        else RiskClass.READ_ONLY
    )
    harness = Harness(tmp_path, risk)
    proposal = harness.proposal
    if failed_check == "external_mcp_eligibility":
        current = harness.switch.load()
        harness.switch.set_enabled(False, expected_revision=current.revision)
    elif failed_check == "package_identity":
        proposal = _replace_proposal(proposal, server_version="2.0.0")
    elif failed_check == "session_protocol_health":
        harness.supervisor.protocol = "2024-11-05"
    elif failed_check == "signed_tool_membership":
        proposal = _replace_proposal(proposal, tool_name="omega.fake.missing")
    elif failed_check == "input_schema":
        proposal = _replace_proposal(proposal, arguments={"query": 42})
    elif failed_check == "connection_identity_revision":
        harness.connection = dataclasses.replace(harness.connection, connection_revision="f" * 64)
    elif failed_check == "connection_capability_ceiling":
        harness.connection = dataclasses.replace(harness.connection, capability_ceiling=())
    elif failed_check == "authority_validity":
        harness.authority = dataclasses.replace(harness.authority, authority_revision="f" * 64)
    elif failed_check == "source_destination_constraints":
        proposal = _replace_proposal(proposal, source_constraints={"folders": ["all"]})
    elif failed_check == "provider_revision_precondition":
        proposal = _replace_proposal(proposal, expected_provider_revisions={})
    elif failed_check == "risk_specific_authority":
        harness.authority = dataclasses.replace(
            harness.authority, risk_classes=(RiskClass.READ_ONLY.value,)
        )
    else:
        _seed_idempotency(harness, state="dispatched")

    outcome = anyio.run(harness.gateway.execute, proposal)

    assert outcome.failed_check == failed_check
    assert harness.supervisor.call_count == 0
    assert (
        action_ledger.find_by_idempotency(proposal.idempotency_key, path=harness.ledger_path)
        is None
        or failed_check == "action_ledger_idempotency"
    )
    check_index = EXPECTED_CHECKS.index(failed_check)
    assert harness.connection_calls == int(check_index >= 5)
    assert harness.authority_calls == int(check_index >= 7)


@pytest.mark.parametrize(
    ("proposal_revisions", "argument_revisions", "failed_check"),
    [
        ({}, None, "input_schema"),
        (
            {"object-1": "revision-0"},
            {"object-1": "different-revision"},
            "provider_revision_precondition",
        ),
        (
            {"irrelevant-object": "revision-0"},
            {"irrelevant-object": "revision-0"},
            "provider_revision_precondition",
        ),
    ],
)
def test_revision_precondition_must_bind_the_signed_target_and_provider_arguments(
    tmp_path: Path,
    proposal_revisions: dict[str, str],
    argument_revisions: dict[str, str] | None,
    failed_check: str,
) -> None:
    harness = Harness(tmp_path, RiskClass.MUTATING_WRITE)
    arguments: dict[str, Any] = {"query": "inbox", "object_id": "object-1"}
    if argument_revisions is not None:
        arguments["expected_provider_revisions"] = argument_revisions
    proposal = _replace_proposal(
        harness.proposal,
        expected_provider_revisions=proposal_revisions,
        arguments=arguments,
    )
    harness.authority = dataclasses.replace(
        harness.authority,
        arguments_sha256=sha256_hex(canonical_bytes(arguments)),
    )

    outcome = anyio.run(harness.gateway.execute, proposal)

    assert outcome.status == "blocked"
    assert outcome.failed_check == failed_check
    assert harness.supervisor.call_count == 0
    assert (
        action_ledger.find_by_idempotency(proposal.idempotency_key, path=harness.ledger_path)
        is None
    )


def test_valid_opaque_provider_revision_reaches_the_provider_exactly(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, RiskClass.MUTATING_WRITE)
    opaque_etag = 'W/"opaque,quoted-etag"'
    revisions = {"object-1": opaque_etag}
    arguments = {
        "query": "inbox",
        "object_id": "object-1",
        "expected_provider_revisions": revisions,
    }
    proposal = _replace_proposal(
        harness.proposal,
        expected_provider_revisions=revisions,
        arguments=arguments,
    )
    harness.authority = dataclasses.replace(
        harness.authority,
        arguments_sha256=sha256_hex(canonical_bytes(arguments)),
    )

    outcome = anyio.run(harness.gateway.execute, proposal)

    assert outcome.status == "confirmed"
    assert harness.supervisor.arguments == [arguments]


def test_signed_revision_map_must_be_required_before_ledger_or_provider(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, RiskClass.MUTATING_WRITE)
    tool_document = harness.tool.to_dict()
    invalid_schema = tool_document["input_schema"]
    assert isinstance(invalid_schema, dict)
    invalid_schema["required"] = ["query", "object_id"]
    invalid_tool = dataclasses.replace(
        harness.tool,
        input_schema=invalid_schema,
    )
    invalid_catalog = dataclasses.replace(
        harness.package.tool_catalog,
        tools=(invalid_tool,),
    )
    invalid_package = dataclasses.replace(
        harness.package,
        tool_catalog=invalid_catalog,
    )
    harness.gateway._verifier = lambda _path: invalid_package

    outcome = anyio.run(harness.gateway.execute, harness.proposal)

    assert outcome.status == "blocked"
    assert outcome.failed_check == "provider_revision_precondition"
    assert harness.supervisor.call_count == 0
    assert (
        action_ledger.find_by_idempotency(
            harness.proposal.idempotency_key, path=harness.ledger_path
        )
        is None
    )


def test_resolver_exception_and_malformed_result_are_content_free(tmp_path: Path) -> None:
    harness = Harness(tmp_path)

    def fail(_connection_id: str):
        raise RuntimeError("provider-resolver-content-sentinel")

    gateway = harness.make_gateway(connection_resolver=fail)
    outcome = anyio.run(gateway.execute, harness.proposal)

    assert outcome.failed_check == "connection_identity_revision"
    assert "sentinel" not in repr(outcome)
    assert harness.authority_calls == 0
    assert harness.supervisor.call_count == 0


def test_confirmed_read_is_admitted_then_ledger_closed_with_exact_lineage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path)
    order: list[str] = []
    real_receipt = authority_module.action_ledger.record_receipt

    def receipt(*args, **kwargs):
        order.append("ledger_receipt")
        return real_receipt(*args, **kwargs)

    def admit(context, result):
        order.append("ingress")
        return _admission(context, result)

    harness.gateway._ingress_admitter = admit
    monkeypatch.setattr(authority_module.action_ledger, "record_receipt", receipt)

    outcome = anyio.run(harness.gateway.execute, harness.proposal)
    record = action_ledger.get_action(outcome.action_id, path=harness.ledger_path)

    assert outcome.status == "confirmed"
    assert outcome.admission is not None
    assert outcome.admission.source_ledger_receipt == "src_" + "1" * 24
    assert order == ["ingress", "ledger_receipt"]
    assert record is not None and record["state"] == "confirmed"
    assert record["server_id"] == harness.proposal.server_id
    assert record["artifact_sha256"] == harness.proposal.artifact_sha256
    assert record["connection_revision"] == harness.proposal.connection_revision
    assert record["authority_revision"] == harness.proposal.authority_revision
    assert record["proposal_digest"] == harness.proposal.proposal_digest
    assert record["source_constraints_sha256"] == sha256_hex(
        canonical_bytes(harness.proposal.source_constraints_dict())
    )
    serialized = json.dumps(outcome.to_dict())
    assert "provider-content-sentinel" not in serialized


def test_source_context_binds_the_timestamp_captured_before_provider_dispatch(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    order: list[str] = []
    real_call = harness.supervisor.call_tool

    def timestamp() -> str:
        order.append("timestamp")
        return "2026-08-11T12:00:00Z"

    async def call(*args, **kwargs):
        order.append("provider_call")
        return await real_call(*args, **kwargs)

    def admit(context, result):
        order.append("ingress")
        assert context.call_timestamp == "2026-08-11T12:00:00Z"
        return _admission(context, result)

    harness.supervisor.call_tool = call  # type: ignore[method-assign]
    harness.gateway = harness.make_gateway(timestamp=timestamp)
    harness.gateway._ingress_admitter = admit

    outcome = anyio.run(harness.gateway.execute, harness.proposal)

    assert outcome.status == "confirmed"
    assert order == ["timestamp", "provider_call", "ingress"]


def test_confirmed_replay_returns_only_structural_receipt_without_call_or_ingress(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    _seed_idempotency(harness, state="confirmed")

    outcome = anyio.run(harness.gateway.execute, harness.proposal)

    assert outcome.status == "confirmed"
    assert outcome.deduplicated is True
    assert outcome.admission is None
    assert outcome.provider_object_id == "object-1"
    assert harness.supervisor.call_count == 0
    assert harness.ingress_calls == 0


_CONFIRMED_REPLAY_CORRUPTIONS = [
    pytest.param("omega_action_id", 7, id="action-id-type"),
    pytest.param("omega_action_id", "x" * 5000, id="action-id-bound"),
    pytest.param("originating_cycle_id", "provider-replay-content-sentinel", id="episode"),
    pytest.param("request_id", "provider-replay-content-sentinel", id="request"),
    pytest.param("server_id", "provider-replay-content-sentinel", id="server"),
    pytest.param("server_version", "provider-replay-content-sentinel", id="version"),
    pytest.param("artifact_sha256", "9" * 64, id="artifact"),
    pytest.param("provider", "provider-replay-content-sentinel", id="provider"),
    pytest.param("account", "provider-replay-content-sentinel", id="account"),
    pytest.param("connection_id", "provider-replay-content-sentinel", id="connection"),
    pytest.param("connection_revision", "9" * 64, id="connection-revision"),
    pytest.param("capability", "provider-replay-content-sentinel", id="capabilities"),
    pytest.param("operation", "provider-replay-content-sentinel", id="operation"),
    pytest.param("tool_schema_sha256", "9" * 64, id="tool-schema"),
    pytest.param("risk_class", "provider-replay-content-sentinel", id="risk"),
    pytest.param("authority_kind", "provider-replay-content-sentinel", id="authority-kind"),
    pytest.param("authority_id", "provider-replay-content-sentinel", id="authority-id"),
    pytest.param("authority_revision", "9" * 64, id="authority-revision"),
    pytest.param("proposal_digest", "9" * 64, id="proposal-digest"),
    pytest.param("source_constraints_sha256", "9" * 64, id="source-constraints"),
    pytest.param("destination_constraints_sha256", "9" * 64, id="destination-constraints"),
    pytest.param("is_write", True, id="write-classification"),
    pytest.param("caused_by_omega", False, id="omega-causation"),
    pytest.param("idempotency_key", "provider-replay-content-sentinel", id="idempotency"),
    pytest.param("provider_request_id", 7, id="provider-request-type"),
    pytest.param("provider_request_id", "x" * 5000, id="provider-request-bound"),
    pytest.param("provider_correlation_id", [], id="provider-correlation-type"),
    pytest.param(
        "provider_idempotency.idempotency_key",
        "provider-replay-content-sentinel",
        id="nested-idempotency",
    ),
    pytest.param("job_revision", "provider-replay-content-sentinel", id="job-revision"),
    pytest.param("grant_id", "provider-replay-content-sentinel", id="grant"),
    pytest.param("grant_approval_digest", "provider-replay-content-sentinel", id="grant-digest"),
    pytest.param("expected_revision", "9" * 64, id="expected-revision"),
    pytest.param("provider_object_id", 7, id="provider-object-type"),
    pytest.param("provider_object_id", "x" * 5000, id="provider-object-bound"),
    pytest.param("provider_revision", [], id="provider-revision-type"),
    pytest.param("provider_revision", "x" * 5000, id="provider-revision-bound"),
    pytest.param("dispatched_at", 7, id="dispatch-time-type"),
    pytest.param("closed_at", 7, id="close-time-type"),
    pytest.param("failure_reason", "provider-replay-content-sentinel", id="failure"),
    pytest.param("receipt.kind", 7, id="receipt-kind"),
    pytest.param("receipt.kind", "x" * 5000, id="receipt-kind-bound"),
    pytest.param("receipt.provider_id", "provider-replay-content-sentinel", id="receipt-provider"),
    pytest.param("receipt.provider_object_id", "wrong-object", id="receipt-object"),
    pytest.param("receipt.provider_revision", "wrong-revision", id="receipt-revision"),
    pytest.param("receipt.status", 7, id="receipt-status"),
    pytest.param("receipt.event_kind", 7, id="receipt-event-kind"),
    pytest.param("receipt.count", "1", id="receipt-count"),
    pytest.param("receipt.deduped", "false", id="receipt-deduped"),
    pytest.param("receipt.via", "provider-replay-content-sentinel", id="receipt-route"),
    pytest.param("receipt.omega_action_id", "wrong-action", id="receipt-action"),
    pytest.param("receipt.__extra__", "provider-replay-content-sentinel", id="receipt-extra"),
    pytest.param("__extra__", "provider-replay-content-sentinel", id="record-extra"),
]


@pytest.mark.parametrize(("field_path", "corrupt_value"), _CONFIRMED_REPLAY_CORRUPTIONS)
def test_malformed_confirmed_record_never_dispatches_or_replays_success(
    tmp_path: Path,
    field_path: str,
    corrupt_value: Any,
) -> None:
    harness = Harness(tmp_path)
    _seed_idempotency(harness, state="confirmed")
    document = json.loads(harness.ledger_path.read_text(encoding="utf-8"))
    record = next(iter(document.values()))
    target = record
    segments = field_path.split(".")
    for segment in segments[:-1]:
        target = target[segment]
    target[segments[-1]] = corrupt_value
    harness.ledger_path.write_text(
        json.dumps(document, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )

    outcome = anyio.run(harness.gateway.execute, harness.proposal)

    assert outcome.status == "blocked"
    assert outcome.failed_check == "action_ledger_idempotency"
    assert harness.supervisor.call_count == 0
    assert harness.ingress_calls == 0
    assert "provider-replay-content-sentinel" not in repr(outcome)


_CONFIRMED_REPLAY_RECEIPT_CONTRACT = [
    pytest.param("kind", "observation", True, id="kind-observation"),
    pytest.param("kind", "", False, id="kind-empty"),
    pytest.param("kind", "current", False, id="kind-other-enum"),
    pytest.param("kind", "Observation", False, id="kind-case"),
    pytest.param("kind", None, False, id="kind-none"),
    pytest.param("kind", True, False, id="kind-bool"),
    pytest.param("kind", [], False, id="kind-list"),
    pytest.param("status", "current", True, id="status-current"),
    pytest.param("status", "historical", True, id="status-historical"),
    pytest.param("status", "deleted", True, id="status-deleted"),
    pytest.param("status", "", False, id="status-empty"),
    pytest.param("status", "unknown", False, id="status-other-enum"),
    pytest.param("status", "Current", False, id="status-case"),
    pytest.param("status", None, False, id="status-none"),
    pytest.param("status", True, False, id="status-bool"),
    pytest.param("status", {}, False, id="status-object"),
    pytest.param("event_kind", "current", True, id="event-current"),
    pytest.param("event_kind", "historical", True, id="event-historical"),
    pytest.param("event_kind", "deleted", True, id="event-deleted"),
    pytest.param("event_kind", "", False, id="event-empty"),
    pytest.param("event_kind", "unknown", False, id="event-other-enum"),
    pytest.param("event_kind", "Current", False, id="event-case"),
    pytest.param("event_kind", None, False, id="event-none"),
    pytest.param("event_kind", True, False, id="event-bool"),
    pytest.param("event_kind", [], False, id="event-list"),
    pytest.param("count", 0, False, id="count-zero"),
    pytest.param("count", 1, True, id="count-one"),
    pytest.param("count", -1, False, id="count-negative"),
    pytest.param("count", True, False, id="count-bool"),
    pytest.param("count", 1.0, False, id="count-float"),
    pytest.param("count", "1", False, id="count-string"),
    pytest.param("count", None, False, id="count-none"),
    pytest.param("count", [], False, id="count-list"),
    pytest.param("deduped", False, True, id="deduped-false"),
    pytest.param("deduped", True, True, id="deduped-true"),
    pytest.param("deduped", 0, False, id="deduped-zero"),
    pytest.param("deduped", 1, False, id="deduped-one"),
    pytest.param("deduped", "false", False, id="deduped-string"),
    pytest.param("deduped", None, False, id="deduped-none"),
    pytest.param("deduped", {}, False, id="deduped-object"),
]


@pytest.mark.parametrize(
    ("receipt_field", "value", "accepted"),
    _CONFIRMED_REPLAY_RECEIPT_CONTRACT,
)
def test_confirmed_replay_uses_exact_source_result_receipt_contract(
    tmp_path: Path,
    receipt_field: str,
    value: Any,
    accepted: bool,
) -> None:
    harness = Harness(tmp_path)
    _seed_idempotency(harness, state="confirmed")
    document = json.loads(harness.ledger_path.read_text(encoding="utf-8"))
    record = next(iter(document.values()))
    record["receipt"][receipt_field] = value
    harness.ledger_path.write_text(
        json.dumps(document, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )

    outcome = anyio.run(harness.gateway.execute, harness.proposal)

    assert outcome.status == ("confirmed" if accepted else "blocked")
    assert outcome.deduplicated is accepted
    assert harness.supervisor.call_count == 0
    assert harness.ingress_calls == 0


def test_same_idempotency_key_with_different_proposal_is_blocked(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    _seed_idempotency(harness, state="confirmed", proposal_digest="f" * 64)

    outcome = anyio.run(harness.gateway.execute, harness.proposal)

    assert outcome.blocker == "action_idempotency_conflict"
    assert outcome.failed_check == "action_ledger_idempotency"
    assert harness.supervisor.call_count == 0


def test_ambiguous_write_never_blind_retries(tmp_path: Path) -> None:
    harness = Harness(tmp_path, RiskClass.MUTATING_WRITE)
    harness.supervisor.error = TimeoutError("provider may have committed sentinel")

    first = anyio.run(harness.gateway.execute, harness.proposal)
    second = anyio.run(harness.gateway.execute, harness.proposal)

    assert first.status == "outcome_unknown"
    assert first.uncertain is True
    assert second.blocker == "authority_validity"
    assert second.failed_check == "authority_validity"
    assert harness.supervisor.call_count == 1
    record = action_ledger.get_action(first.action_id, path=harness.ledger_path)
    assert record is not None and record["state"] == "outcome_unknown"
    assert "sentinel" not in repr(first)


@pytest.mark.parametrize(
    "failure",
    [
        anyio.BrokenResourceError("transport disconnected sentinel"),
        anyio.ClosedResourceError("transport closed sentinel"),
        anyio.EndOfStream("transport ended sentinel"),
    ],
)
def test_anyio_transport_disconnect_after_write_dispatch_is_outcome_unknown(
    tmp_path: Path,
    failure: BaseException,
) -> None:
    harness = Harness(tmp_path, RiskClass.MUTATING_WRITE)
    harness.supervisor.error = failure

    outcome = anyio.run(harness.gateway.execute, harness.proposal)

    assert outcome.status == "outcome_unknown"
    assert outcome.uncertain is True
    assert "sentinel" not in repr(outcome)


def test_definitive_provider_rejection_closes_failed(tmp_path: Path) -> None:
    harness = Harness(tmp_path, RiskClass.MUTATING_WRITE)
    harness.supervisor.error = ValueError("provider rejection content sentinel")

    outcome = anyio.run(harness.gateway.execute, harness.proposal)

    assert outcome.status == "failed"
    record = action_ledger.get_action(outcome.action_id, path=harness.ledger_path)
    assert record is not None and record["state"] == "failed"
    assert "sentinel" not in repr(outcome)


@pytest.mark.parametrize(
    ("risk", "flag"),
    [
        (RiskClass.SEND_PUBLISH, "allow_send_publish"),
        (RiskClass.DESTRUCTIVE, "allow_destructive"),
    ],
)
def test_signed_high_risk_operation_requires_its_exact_authority_flag(
    tmp_path: Path,
    risk: RiskClass,
    flag: str,
) -> None:
    harness = Harness(tmp_path, risk)
    harness.authority = dataclasses.replace(harness.authority, **{flag: False})

    outcome = anyio.run(harness.gateway.execute, harness.proposal)

    assert outcome.failed_check == "risk_specific_authority"
    assert harness.supervisor.call_count == 0


def test_cross_provider_transfer_requires_distinct_exact_constraints_in_one_authority(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, RiskClass.CROSS_PROVIDER_TRANSFER)
    source = {"provider": "provider.fake", "folders": ["inbox"]}
    destination = {"provider": "provider.other", "folder": "archive"}
    harness.connection = dataclasses.replace(
        harness.connection,
        source_constraints=source,
        destination_constraints=destination,
    )
    harness.authority = dataclasses.replace(
        harness.authority,
        source_constraints=source,
        destination_constraints=destination,
    )
    proposal = _replace_proposal(
        harness.proposal,
        source_constraints=source,
        destination_constraints=destination,
    )

    outcome = anyio.run(harness.gateway.execute, proposal)

    assert outcome.status == "confirmed"
    assert harness.supervisor.call_count == 1


def test_standing_grant_dispatch_binds_job_and_grant_lineage(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.authority = dataclasses.replace(
        harness.authority,
        authority_kind="standing_grant",
        authority_id="grant-1",
        authority_revision="f" * 64,
        job_id="job-1",
        job_revision="0" * 64,
        direct_execution=False,
        standing_execution=True,
    )
    proposal = _replace_proposal(
        harness.proposal,
        authority_kind="standing_grant",
        authority_id="grant-1",
        authority_revision="f" * 64,
    )

    outcome = anyio.run(harness.gateway.execute, proposal)
    record = action_ledger.get_action(outcome.action_id, path=harness.ledger_path)

    assert outcome.status == "confirmed"
    assert record is not None
    assert record["job_revision"] == "0" * 64
    assert record["grant_id"] == "grant-1"
    assert record["grant_approval_digest"] == "f" * 64


def test_concurrent_same_proposal_can_dispatch_only_once(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.supervisor.block_call = True
    outcomes = []

    async def scenario() -> None:
        async with anyio.create_task_group() as tasks:

            async def execute() -> None:
                outcomes.append(await harness.gateway.execute(harness.proposal))

            tasks.start_soon(execute)
            await anyio.to_thread.run_sync(harness.supervisor.call_entered.wait)
            tasks.start_soon(execute)
            await anyio.sleep(0.05)
            harness.supervisor.release_call.set()

    anyio.run(scenario)

    assert harness.supervisor.call_count == 1
    assert sorted(outcome.status for outcome in outcomes) == ["blocked", "confirmed"]
    blocked = next(outcome for outcome in outcomes if outcome.status == "blocked")
    assert blocked.blocker == "authority_validity"
    assert blocked.failed_check == "authority_validity"


def test_cancellation_preserves_identity_and_closes_write_unknown(tmp_path: Path) -> None:
    harness = Harness(tmp_path, RiskClass.MUTATING_WRITE)
    started = anyio.Event()
    release_provider = threading.Event()
    provider_cancellation: list[BaseException] = []
    caught: list[BaseException] = []

    async def blocking_call(*_args, **_kwargs):
        harness.supervisor.call_count += 1
        started.set()
        await anyio.to_thread.run_sync(release_provider.wait)
        cancellation = anyio.get_cancelled_exc_class()()
        provider_cancellation.append(cancellation)
        raise cancellation

    harness.supervisor.call_tool = blocking_call  # type: ignore[method-assign]

    async def scenario() -> None:
        with anyio.CancelScope() as scope:

            async def execute() -> None:
                try:
                    await harness.gateway.execute(harness.proposal)
                except anyio.get_cancelled_exc_class() as exc:
                    caught.append(exc)

            async with anyio.create_task_group() as tasks:
                tasks.start_soon(execute)
                await started.wait()
                scope.cancel()
                release_provider.set()

    anyio.run(scenario)

    record = action_ledger.find_by_idempotency(
        harness.proposal.idempotency_key, path=harness.ledger_path
    )
    assert caught == provider_cancellation
    assert record is not None and record["state"] == "outcome_unknown"


class _CleanupBaseFailure(BaseException):
    pass


@pytest.mark.parametrize(
    "cleanup_error",
    [
        RuntimeError("provider-cleanup-content-sentinel"),
        _CleanupBaseFailure("provider-cleanup-content-sentinel"),
    ],
)
def test_cancellation_cleanup_failure_never_replaces_the_original_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_error: BaseException,
) -> None:
    harness = Harness(tmp_path, RiskClass.MUTATING_WRITE)
    started = anyio.Event()
    release_provider = threading.Event()
    caught: list[BaseException] = []
    cancellation_types: list[type[BaseException]] = []
    provider_cancellation: list[BaseException] = []

    async def blocking_call(*_args, **_kwargs):
        started.set()
        await anyio.to_thread.run_sync(release_provider.wait)
        cancellation = anyio.get_cancelled_exc_class()()
        provider_cancellation.append(cancellation)
        raise cancellation

    def fail_cleanup(*_args, **_kwargs):
        raise cleanup_error

    harness.supervisor.call_tool = blocking_call  # type: ignore[method-assign]
    monkeypatch.setattr(authority_module.action_ledger, "record_failure", fail_cleanup)

    async def scenario() -> None:
        cancellation_types.append(anyio.get_cancelled_exc_class())
        with anyio.CancelScope() as scope:

            async def execute() -> None:
                try:
                    await harness.gateway.execute(harness.proposal)
                except BaseException as exc:
                    caught.append(exc)

            async with anyio.create_task_group() as tasks:
                tasks.start_soon(execute)
                await started.wait()
                scope.cancel()
                release_provider.set()

    anyio.run(scenario)

    assert len(caught) == 1
    assert type(caught[0]) is cancellation_types[0]
    assert caught == provider_cancellation
    assert caught[0] is not cleanup_error
    assert "provider-cleanup-content-sentinel" not in repr(caught[0])


def test_boot_built_host_composes_real_gateway_with_fail_closed_default_resolvers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness(tmp_path)
    monkeypatch.setattr(service_module, "load_embedded_key_ring", lambda: {})
    monkeypatch.setattr(service_module, "current_platform_tag", lambda: "test-platform")
    monkeypatch.setattr(
        service_module,
        "verify_package",
        lambda _path, **_kwargs: harness.package,
    )
    monkeypatch.setattr(
        service_module,
        "PackageRegistry",
        lambda **_kwargs: harness.registry,
    )
    monkeypatch.setattr(
        service_module,
        "ExternalMcpSwitch",
        lambda _path: harness.switch,
    )
    monkeypatch.setattr(
        service_module,
        "McpProcessSupervisor",
        lambda **_kwargs: harness.supervisor,
    )

    fail_closed_host = service_module._build_default_host(
        action_ledger_path=harness.ledger_path,
    )
    blocked = anyio.run(fail_closed_host.execute_operation, harness.proposal)
    harness.supervisor.error = ValueError("provider-definitive-rejection")
    composed_host = service_module._build_default_host(
        connection_resolver=harness.resolve_connection,
        authority_resolver=harness.resolve_authority,
        action_ledger_path=harness.ledger_path,
    )
    provider_failure = anyio.run(composed_host.execute_operation, harness.proposal)

    assert blocked.status == "blocked"
    assert blocked.failed_check == "connection_identity_revision"
    assert provider_failure.status == "failed"
    assert harness.connection_calls == 1
    assert harness.authority_calls == 1
    assert harness.supervisor.call_count == 1


def test_hostile_result_never_appears_in_gateway_outcome(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.supervisor.result = {"provider-content-sentinel": object()}

    outcome = anyio.run(harness.gateway.execute, harness.proposal)

    assert outcome.status == "failed"
    assert "provider-content-sentinel" not in repr(outcome)


def test_host_exposes_only_the_authority_gated_operation_surface(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    host = McpHost(
        registry=harness.registry,  # type: ignore[arg-type]
        external_switch=harness.switch,
        supervisor=harness.supervisor,  # type: ignore[arg-type]
        authority_gateway=harness.gateway,
    )

    outcome = anyio.run(host.execute_operation, harness.proposal)

    assert outcome.status == "confirmed"
    assert not hasattr(host, "call_tool")
    assert not hasattr(host, "read_resource")


@pytest.mark.parametrize(
    ("risk", "uncertain_writes", "failed_reads", "final_state"),
    [
        (RiskClass.MUTATING_WRITE, 1, 0, "outcome_unknown"),
        (RiskClass.READ_ONLY, 0, 1, "failed"),
    ],
)
def test_package_drain_finalizes_exact_local_provider_call_at_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    risk: RiskClass,
    uncertain_writes: int,
    failed_reads: int,
    final_state: str,
) -> None:
    monkeypatch.setattr(authority_module, "PACKAGE_CALL_DRAIN_TIMEOUT_S", 0.05)
    harness = Harness(tmp_path, risk, NetworkClass.LOCAL_ONLY)
    harness.supervisor.block_call = True
    outcomes = []
    drain_results: list[dict[str, int]] = []

    async def scenario() -> None:
        async with anyio.create_task_group() as tasks:

            async def execute() -> None:
                outcomes.append(await harness.gateway.execute(harness.proposal))

            tasks.start_soon(execute)
            await anyio.to_thread.run_sync(harness.supervisor.call_entered.wait)
            harness.registry.record = dataclasses.replace(
                harness.registry.record,
                enabled=False,
            )
            try:
                drain_package = getattr(harness.gateway, "drain_package")
                drain_results.append(await drain_package(harness.proposal.server_id))
            finally:
                harness.supervisor.release_call.set()

    anyio.run(scenario)

    record = action_ledger.find_by_idempotency(
        harness.proposal.idempotency_key,
        path=harness.ledger_path,
    )
    assert drain_results == [
        {
            "drained_calls": 0,
            "uncertain_writes": uncertain_writes,
            "failed_reads": failed_reads,
            "errors": 0,
        }
    ]
    assert record is not None and record["state"] == final_state
    assert record["failure_reason"] == "package_disabled"
    assert outcomes[0].status == final_state
    assert outcomes[0].blocker == "package_disabled"
    assert harness.gateway._active_calls == {}


def test_external_disable_does_not_wait_on_local_only_call(tmp_path: Path) -> None:
    harness = Harness(tmp_path, network_class=NetworkClass.LOCAL_ONLY)
    tracker = authority_module._TrackedExternalCall(
        token=1,
        server_id=harness.proposal.server_id,
        external=False,
    )
    harness.gateway._active_calls[tracker.token] = tracker

    started = time.monotonic()
    outcome = anyio.run(
        harness.gateway.disable_external,
        harness.switch.load().revision,
    )

    assert time.monotonic() - started < 0.20
    assert outcome.drained_calls == 0
    assert harness.gateway._active_calls == {tracker.token: tracker}
    assert harness.gateway._active_external == {}


def test_package_drain_retains_and_retries_failed_ledger_finalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(authority_module, "PACKAGE_CALL_DRAIN_TIMEOUT_S", 0.05)
    harness = Harness(tmp_path, RiskClass.MUTATING_WRITE, NetworkClass.LOCAL_ONLY)
    harness.supervisor.block_call = True
    real_batch = authority_module.action_ledger.record_switch_deadline_batch
    attempts = 0

    def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("ledger-content-sentinel")
        return real_batch(*args, **kwargs)

    monkeypatch.setattr(
        authority_module.action_ledger,
        "record_switch_deadline_batch",
        fail_once,
    )
    first: list[dict[str, int]] = []
    second: list[dict[str, int]] = []

    async def scenario() -> None:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(harness.gateway.execute, harness.proposal)
            await anyio.to_thread.run_sync(harness.supervisor.call_entered.wait)
            harness.registry.record = dataclasses.replace(
                harness.registry.record,
                enabled=False,
            )
            try:
                drain_package = getattr(harness.gateway, "drain_package")
                first.append(await drain_package(harness.proposal.server_id))
                second.append(await drain_package(harness.proposal.server_id))
            finally:
                harness.supervisor.release_call.set()

    anyio.run(scenario)

    record = action_ledger.find_by_idempotency(
        harness.proposal.idempotency_key,
        path=harness.ledger_path,
    )
    assert first == [
        {
            "drained_calls": 0,
            "uncertain_writes": 0,
            "failed_reads": 0,
            "errors": 1,
        }
    ]
    assert second == [
        {
            "drained_calls": 0,
            "uncertain_writes": 1,
            "failed_reads": 0,
            "errors": 0,
        }
    ]
    assert record is not None and record["state"] == "outcome_unknown"
    assert harness.gateway._active_calls == {}


def test_package_recovery_is_not_owned_by_external_disable_cache(tmp_path: Path) -> None:
    harness = Harness(tmp_path, network_class=NetworkClass.LOCAL_ONLY)
    tracker = authority_module._TrackedExternalCall(
        token=7,
        server_id=harness.proposal.server_id,
        external=False,
        deadline_blocker="package_disabled",
        action_id="act-package-recovery",
        is_write=True,
        provider_claimed=True,
        timed_out=True,
        deadline_finalized=True,
        owner_finished=True,
    )
    harness.gateway._disable_recovery = authority_module._DisableRecovery(
        attempt=1,
        expected_revision="0" * 64,
        classifications={tracker.token: "uncertain_write"},
        finalized_tokens=set(),
    )

    assert harness.gateway._tracker_requires_reconciliation(tracker) is False
