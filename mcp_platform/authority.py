"""Single ordered authority gateway for every public MCP operation."""

from __future__ import annotations

import re
import threading
import time
import weakref
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeAlias

import anyio
import jsonschema

from core.symphony_time import now_iso
from mcp_platform import action_ledger
from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.external_switch import ExternalMcpSwitch, mcp_eligible
from mcp_platform.ingress import admit_mcp_result
from mcp_platform.operation_contracts import (
    McpAdmissionSummary,
    McpAuthorityDecision,
    McpAuthoritySnapshot,
    McpConnectionSnapshot,
    McpDispatchOutcome,
    McpExternalDisableOutcome,
    McpOperationProposal,
    McpToolAuthority,
)
from mcp_platform.package_contracts import (
    MCP_PROTOCOL_VERSION,
    NetworkClass,
    RiskClass,
    ToolDefinition,
)
from mcp_platform.package_verifier import VerifiedPackage
from mcp_platform.registry import InstalledPackageRecord, PackageRegistry
from mcp_platform.source_contracts import (
    MCP_EVENT_KINDS,
    MCP_RESULT_KINDS,
    MCP_RESULT_STATUSES,
    McpAdmissionReceipt,
    McpSourceContext,
    McpStructuredResult,
)
from mcp_platform.supervisor import McpProcessSupervisor, SupervisedSessionSnapshot

EXPECTED_CHECKS = (
    "external_mcp_eligibility",
    "package_identity",
    "session_protocol_health",
    "signed_tool_membership",
    "input_schema",
    "connection_identity_revision",
    "connection_capability_ceiling",
    "authority_validity",
    "source_destination_constraints",
    "provider_revision_precondition",
    "risk_specific_authority",
    "action_ledger_idempotency",
)
EXTERNAL_CALL_DRAIN_TIMEOUT_S = 2.0
PACKAGE_CALL_DRAIN_TIMEOUT_S = 2.0
LEDGER_CANCELLATION_CLEANUP_TIMEOUT_S = 2.0
_MAX_LEDGER_IDENTITY_BYTES = 4_096
_CONFIRMED_ACTION_FIELDS = frozenset(
    {
        "omega_action_id",
        "originating_cycle_id",
        "request_id",
        "server_id",
        "server_version",
        "artifact_sha256",
        "provider",
        "account",
        "connection_id",
        "capability",
        "operation",
        "tool_schema_sha256",
        "risk_class",
        "authority_kind",
        "authority_id",
        "authority_revision",
        "proposal_digest",
        "source_constraints_sha256",
        "destination_constraints_sha256",
        "is_write",
        "caused_by_omega",
        "provider_request_id",
        "provider_correlation_id",
        "idempotency_key",
        "provider_idempotency",
        "connection_revision",
        "job_revision",
        "grant_id",
        "grant_approval_digest",
        "expected_revision",
        "provider_object_id",
        "provider_revision",
        "state",
        "dispatched_at",
        "closed_at",
        "receipt",
        "failure_reason",
    }
)
_PROVIDER_IDEMPOTENCY_FIELDS = frozenset(
    {"provider_request_id", "provider_correlation_id", "idempotency_key"}
)
_CONFIRMED_RECEIPT_FIELDS = frozenset(
    {
        "kind",
        "provider_id",
        "provider_object_id",
        "provider_revision",
        "status",
        "event_kind",
        "count",
        "deduped",
        "via",
        "omega_action_id",
    }
)

ConnectionResolver: TypeAlias = Callable[[str], McpConnectionSnapshot]
AuthorityResolver: TypeAlias = Callable[[str, str], McpAuthoritySnapshot]
Verifier: TypeAlias = Callable[[Path], VerifiedPackage]
IngressAdmitter: TypeAlias = Callable[[McpSourceContext, Mapping[str, Any]], McpAdmissionReceipt]
Timestamp: TypeAlias = Callable[[], str]


@dataclass(slots=True)
class _AuthorityUseCell:
    reference: weakref.ReferenceType[McpAuthoritySnapshot]
    fingerprint: str
    marker: object
    registry: _AuthorityUseRegistry
    lock: threading.Lock
    proposal_digest: str | None = None


class _AuthorityUseRegistry:
    """Own weak, process-local issuance proofs shared by every evaluator."""

    def __init__(self, max_live_authorities: int) -> None:
        self._max_live_authorities = max_live_authorities
        self._lock = threading.Lock()
        self._entries: dict[int, _AuthorityUseCell] = {}
        self._identity: Callable[[McpAuthoritySnapshot], int] = id

    @staticmethod
    def _fingerprint(authority: McpAuthoritySnapshot) -> str:
        return sha256_hex(
            canonical_bytes(
                {
                    key: _plain_json(value)
                    for key, value in authority.__dict__.items()
                    if key != "_mcp_authority_cell"
                }
            )
        )

    def _retire(self, identity: int, marker: object) -> None:
        with self._lock:
            current = self._entries.get(identity)
            if current is not None and current.marker is marker:
                self._entries.pop(identity, None)

    def issue(self, authority: McpAuthoritySnapshot) -> McpAuthoritySnapshot:
        if type(authority) is not McpAuthoritySnapshot:
            raise ValueError("authority_issuance_invalid")
        existing = authority.__dict__.get("_mcp_authority_cell")
        if type(existing) is _AuthorityUseCell:
            with existing.lock:
                identity = self._identity(authority)
                with self._lock:
                    active = self._entries.get(identity) is existing
                if (
                    active
                    and existing.reference() is authority
                    and existing.fingerprint == self._fingerprint(authority)
                    and existing.proposal_digest is None
                ):
                    return authority
            raise ValueError("authority_issuance_conflict")
        identity = self._identity(authority)
        marker = object()

        def retire(_reference: weakref.ReferenceType[McpAuthoritySnapshot]) -> None:
            self._retire(identity, marker)

        reference = weakref.ref(authority, retire)
        entry = _AuthorityUseCell(
            reference=reference,
            fingerprint=self._fingerprint(authority),
            marker=marker,
            registry=self,
            lock=threading.Lock(),
        )
        with self._lock:
            current = self._entries.get(identity)
            if current is not None and current.reference() is authority:
                if current.fingerprint != entry.fingerprint:
                    raise ValueError("authority_issuance_conflict")
                return authority
            if current is not None and current.reference() is not None:
                raise ValueError("authority_issuance_conflict")
            if len(self._entries) >= self._max_live_authorities:
                raise ValueError("authority_issuance_limit_exceeded")
            self._entries[identity] = entry
            object.__setattr__(authority, "_mcp_authority_cell", entry)
        return authority

    def consume(self, authority: McpAuthoritySnapshot, proposal_digest: str) -> bool:
        cell = authority.__dict__.get("_mcp_authority_cell")
        if type(cell) is not _AuthorityUseCell or cell.registry is not self:
            return False
        with cell.lock:
            if cell.reference() is not authority or cell.fingerprint != self._fingerprint(
                authority
            ):
                return False
            if cell.proposal_digest is not None:
                return False
            identity = self._identity(authority)
            with self._lock:
                if self._entries.get(identity) is not cell:
                    return False
                self._entries.pop(identity, None)
            cell.proposal_digest = proposal_digest
            return True

    def revoke(self, authority: McpAuthoritySnapshot) -> None:
        cell = authority.__dict__.get("_mcp_authority_cell")
        if type(cell) is not _AuthorityUseCell or cell.registry is not self:
            return
        with cell.lock:
            if cell.reference() is not authority or cell.proposal_digest is not None:
                return
            identity = self._identity(authority)
            with self._lock:
                if self._entries.get(identity) is cell:
                    self._entries.pop(identity, None)
            cell.proposal_digest = "revoked"


_AUTHORITY_USE_REGISTRY = _AuthorityUseRegistry(4_096)
_PLUGIN_AUTHORITY_ID = re.compile(r"[0-9a-f]{48}(?:[0-9a-f]{16})?\Z")


class _AuthorityIssuer:
    __slots__ = ("_registry",)

    def __init__(self, registry: _AuthorityUseRegistry | None = None) -> None:
        self._registry = registry or _AUTHORITY_USE_REGISTRY

    def issue(self, authority: McpAuthoritySnapshot) -> McpAuthoritySnapshot:
        return self._registry.issue(authority)

    def revoke(self, authority: McpAuthoritySnapshot) -> None:
        self._registry.revoke(authority)


def _consume_mcp_authority(authority: McpAuthoritySnapshot, proposal_digest: str) -> bool:
    cell = authority.__dict__.get("_mcp_authority_cell")
    if type(cell) is not _AuthorityUseCell:
        return False
    return cell.registry.consume(authority, proposal_digest)


def _is_issued_mcp_authority(authority: McpAuthoritySnapshot) -> bool:
    return type(authority.__dict__.get("_mcp_authority_cell")) is _AuthorityUseCell


class McpOperationAuthorityEvaluator:
    """Ordered authority law shared by the gateway and plugin execution."""

    @staticmethod
    def _connection_failure(
        proposal: McpOperationProposal,
        connection: McpConnectionSnapshot,
        tool: McpToolAuthority,
    ) -> str | None:
        if not (
            connection.enabled
            and connection.connection_id == proposal.connection_id
            and connection.connection_revision == proposal.connection_revision
            and connection.server_id == proposal.server_id
            and connection.server_version == proposal.server_version
            and connection.artifact_sha256 == proposal.artifact_sha256
            and connection.provider_id == proposal.provider_id
            and connection.account_ref == proposal.account_ref
        ):
            return "connection_identity_revision"
        signed_capabilities = set(tool.capability_tags)
        if not (
            proposal.tool_name == tool.tool_name
            and proposal.tool_schema_sha256 == tool.input_schema_sha256
            and signed_capabilities.issubset(connection.capability_ceiling)
            and connection.allowed_tool_schemas.get(tool.tool_name) == tool.input_schema_sha256
        ):
            return "connection_capability_ceiling"
        return None

    def evaluate_connection(
        self,
        proposal: McpOperationProposal,
        *,
        connection: McpConnectionSnapshot,
        tool: McpToolAuthority,
    ) -> McpAuthorityDecision:
        failed = self._connection_failure(proposal, connection, tool)
        return McpAuthorityDecision(allowed=failed is None, failed_check=failed)

    def evaluate(
        self,
        proposal: McpOperationProposal,
        *,
        connection: McpConnectionSnapshot,
        authority: McpAuthoritySnapshot,
        tool: McpToolAuthority,
    ) -> McpAuthorityDecision:
        failed = self._connection_failure(proposal, connection, tool)
        if failed is None and not (
            authority.authority_kind == proposal.authority_kind
            and authority.authority_id == proposal.authority_id
            and authority.authority_revision == proposal.authority_revision
            and authority.episode_id == proposal.episode_id
            and authority.connection_id == proposal.connection_id
            and authority.connection_revision == proposal.connection_revision
            and authority.server_id == proposal.server_id
            and authority.server_version == proposal.server_version
            and authority.artifact_sha256 == proposal.artifact_sha256
            and authority.tool_name == proposal.tool_name
            and authority.tool_schema_sha256 == proposal.tool_schema_sha256
            and authority.tool_name == tool.tool_name
            and authority.tool_schema_sha256 == tool.input_schema_sha256
            and tuple(authority.capability_tags) == tuple(tool.capability_tags)
            and authority.plan_digest == proposal.plan_digest
            and authority.candidate_id == proposal.candidate_id
            and authority.plan_step_id == proposal.plan_step_id
            and authority.account_key_hash == proposal.account_ref
            and authority.arguments_sha256 == sha256_hex(canonical_bytes(proposal.arguments_dict()))
            and (
                authority.connection_authority_revision
                == proposal.connection_authority_revision
                == connection.authority_revision
            )
            and authority.effect == tool.effect
            and (
                proposal.authority_kind == "direct_turn"
                and authority.direct_execution
                or proposal.authority_kind == "standing_grant"
                and authority.standing_execution
            )
        ):
            failed = "authority_validity"
        if failed is None:
            source = proposal.source_constraints_dict()
            destination = proposal.destination_constraints_dict()
            exact = (
                source == _plain_json(connection.source_constraints)
                and source == _plain_json(authority.source_constraints)
                and destination == _plain_json(connection.destination_constraints)
                and destination == _plain_json(authority.destination_constraints)
            )
            if not exact or (
                tool.risk_class is RiskClass.CROSS_PROVIDER_TRANSFER
                and (not source or not destination or source == destination)
            ):
                failed = "source_destination_constraints"
        if failed is None and tool.risk_class is not RiskClass.READ_ONLY and tool.requires_revision:
            revisions = proposal.expected_provider_revisions_dict()
            arguments = proposal.arguments_dict()
            pointer = tool.provider_object_id_pointer
            target = (
                _MISSING_POINTER_VALUE
                if pointer is None
                else _resolve_json_pointer(arguments, pointer)
            )
            if (
                not revisions
                or arguments.get("expected_provider_revisions") != revisions
                or not tool.revision_map_required
                or type(target) is not str
                or not target
                or set(revisions) != {target}
            ):
                failed = "provider_revision_precondition"
        if failed is None:
            risk = tool.risk_class
            if (
                risk.value not in authority.risk_classes
                or risk is RiskClass.SEND_PUBLISH
                and not authority.allow_send_publish
                or risk is RiskClass.DESTRUCTIVE
                and not authority.allow_destructive
                or risk is RiskClass.CROSS_PROVIDER_TRANSFER
                and not authority.allow_cross_provider
            ):
                failed = "risk_specific_authority"
        if failed is None:
            if not _consume_mcp_authority(authority, proposal.proposal_digest):
                failed = "authority_validity"
        return McpAuthorityDecision(allowed=failed is None, failed_check=failed)


def _mcp_now_iso() -> str:
    observed = now_iso()
    if observed.endswith("+00:00"):
        return observed[:-6] + "Z"
    return observed


@dataclass
class _TrackedExternalCall:
    token: int
    server_id: str
    external: bool
    deadline_blocker: str = "external_mcp_switched_off"
    action_id: str = ""
    is_write: bool | None = None
    provider_claimed: bool = False
    timed_out: bool = False
    deadline_finalized: bool = False
    owner_finished: bool = False


@dataclass
class _DisableRecovery:
    attempt: int
    expected_revision: str
    classifications: dict[int, str | None]
    finalized_tokens: set[int]


class McpAuthorityGateway:
    """Validate the twelve ordered checks, then durably dispatch once."""

    def __init__(
        self,
        *,
        registry: PackageRegistry,
        external_switch: ExternalMcpSwitch,
        supervisor: McpProcessSupervisor,
        verifier: Verifier,
        connection_resolver: ConnectionResolver,
        authority_resolver: AuthorityResolver,
        action_ledger_path: Path | None = None,
        ingress_admitter: IngressAdmitter | None = None,
        source_root: Path | str | None = None,
        timestamp: Timestamp = _mcp_now_iso,
        authority_evaluator: McpOperationAuthorityEvaluator | None = None,
    ) -> None:
        self._registry = registry
        self._external_switch = external_switch
        self._supervisor = supervisor
        self._verifier = verifier
        self._connection_resolver = connection_resolver
        self._authority_resolver = authority_resolver
        self._action_ledger_path = action_ledger_path
        self._source_root = source_root
        self._timestamp = timestamp
        self._ingress_admitter = ingress_admitter or self._admit_default
        self._authority_evaluator = authority_evaluator or McpOperationAuthorityEvaluator()
        self._authority_issuer = _AuthorityIssuer()
        self._active_condition = threading.Condition(threading.Lock())
        self._active_calls: dict[int, _TrackedExternalCall] = {}
        self._active_external: dict[int, _TrackedExternalCall] = {}
        self._next_tracking_token = 0
        self._disable_lock = threading.Lock()
        self._package_drain_lock = threading.Lock()
        self._cleanup_lock = threading.Lock()
        self._cleanup_threads: set[threading.Thread] = set()
        self._last_disable_expected: str | None = None
        self._next_disable_attempt = 0
        self._last_disable_attempt: int | None = None
        self._last_disable_outcome: McpExternalDisableOutcome | None = None
        self._disable_recovery: _DisableRecovery | None = None

    def _admit_default(
        self,
        context: McpSourceContext,
        result: Mapping[str, Any],
    ) -> McpAdmissionReceipt:
        return admit_mcp_result(context, result, source_root=self._source_root)

    async def execute(
        self, proposal: McpOperationProposal | Mapping[str, Any]
    ) -> McpDispatchOutcome:
        """Execute one operation through the exact ordered authority gate."""
        parsed = (
            proposal
            if type(proposal) is McpOperationProposal
            else McpOperationProposal.from_dict(proposal)
        )
        record, tracker, blocked = self._enter_external_gate(parsed)
        if blocked is not None:
            return blocked
        try:
            return await self._execute_after_eligibility(parsed, record, tracker)
        finally:
            if tracker is not None:
                self._finish_external(tracker)

    def _enter_external_gate(
        self,
        proposal: McpOperationProposal,
    ) -> tuple[
        InstalledPackageRecord | None,
        _TrackedExternalCall | None,
        McpDispatchOutcome | None,
    ]:
        with self._active_condition:
            try:
                record = self._registry.get(proposal.server_id)
            except Exception:
                record = None
            if record is None:
                return record, None, None
            try:
                network_class = NetworkClass(record.network_class)
                eligible = mcp_eligible(network_class, self._external_switch.load())
            except Exception:
                return (
                    record,
                    None,
                    self._blocked(proposal, "external_mcp_eligibility"),
                )
            if not eligible:
                return (
                    record,
                    None,
                    self._blocked(proposal, "external_mcp_eligibility"),
                )
            self._next_tracking_token += 1
            tracker = _TrackedExternalCall(
                token=self._next_tracking_token,
                server_id=record.server_id,
                external=network_class is NetworkClass.EXTERNAL,
            )
            self._active_calls[tracker.token] = tracker
            if tracker.external:
                self._active_external[tracker.token] = tracker
            return record, tracker, None

    async def _execute_after_eligibility(
        self,
        proposal: McpOperationProposal,
        record: InstalledPackageRecord | None,
        tracker: _TrackedExternalCall | None,
    ) -> McpDispatchOutcome:
        package = self._verified_package(proposal, record)
        if package is None:
            return self._blocked(proposal, "package_identity")

        snapshot = await self._healthy_session(proposal, package)
        if snapshot is None:
            return self._blocked(proposal, "session_protocol_health")

        tool = next(
            (
                definition
                for definition in package.tool_catalog.tools
                if definition.name == proposal.tool_name
            ),
            None,
        )
        if tool is None or tool.input_schema_sha256 != proposal.tool_schema_sha256:
            return self._blocked(proposal, "signed_tool_membership")

        if not self._valid_arguments(proposal, tool):
            return self._blocked(proposal, "input_schema")

        try:
            connection = self._connection_resolver(proposal.connection_id)
        except Exception:
            return self._blocked(proposal, "connection_identity_revision")
        if type(connection) is not McpConnectionSnapshot:
            return self._blocked(proposal, "connection_identity_revision")
        provider_pointer = (
            tool.input_schema.get("x-omega-provider-object-id-pointer")
            if isinstance(tool.input_schema, Mapping)
            else None
        )
        tool_authority = McpToolAuthority(
            tool_name=tool.name,
            input_schema_sha256=tool.input_schema_sha256,
            capability_tags=tool.capability_tags,
            risk_class=tool.risk_class,
            effect=tool.effect,
            requires_revision=tool.requires_revision,
            provider_object_id_pointer=(
                provider_pointer if type(provider_pointer) is str else None
            ),
            revision_map_required=(
                isinstance(tool.input_schema, Mapping)
                and isinstance(tool.input_schema.get("required"), (list, tuple))
                and "expected_provider_revisions" in tool.input_schema["required"]
            ),
        )
        connection_decision = self._authority_evaluator.evaluate_connection(
            proposal,
            connection=connection,
            tool=tool_authority,
        )
        if not connection_decision.allowed:
            assert connection_decision.failed_check is not None
            return self._blocked(proposal, connection_decision.failed_check)
        if not set(tool.capability_tags).issubset(package.manifest.capability_tags):
            return self._blocked(proposal, "connection_capability_ceiling")

        try:
            authority = self._authority_resolver(proposal.authority_kind, proposal.authority_id)
        except Exception:
            return self._blocked(proposal, "authority_validity")
        if type(authority) is not McpAuthoritySnapshot:
            return self._blocked(proposal, "authority_validity")
        if not _is_issued_mcp_authority(authority):
            if _PLUGIN_AUTHORITY_ID.fullmatch(authority.authority_id):
                return self._blocked(proposal, "authority_validity")
            try:
                authority = self._authority_issuer.issue(authority)
            except ValueError:
                return self._blocked(proposal, "authority_validity")
        decision = self._authority_evaluator.evaluate(
            proposal,
            connection=connection,
            authority=authority,
            tool=tool_authority,
        )
        if not decision.allowed:
            assert decision.failed_check is not None
            return self._blocked(proposal, decision.failed_check)
        if tool.risk_class not in package.manifest.risk_declarations:
            return self._blocked(proposal, "risk_specific_authority")

        is_write = tool.risk_class is not RiskClass.READ_ONLY

        if tracker is not None:
            with self._active_condition:
                if tracker.timed_out:
                    return self._blocked(proposal, "external_mcp_eligibility")
                tracker.is_write = is_write

        try:
            action_id = action_ledger.mint_action_id()
            if tracker is not None:
                with self._active_condition:
                    if tracker.timed_out:
                        return self._blocked(proposal, "external_mcp_eligibility")
                    tracker.action_id = action_id
                    tracker.is_write = is_write
            prior, created = action_ledger.find_or_record_dispatch(
                omega_action_id=action_id,
                episode_id=proposal.episode_id,
                request_id=proposal.request_id,
                server_id=proposal.server_id,
                server_version=proposal.server_version,
                artifact_sha256=proposal.artifact_sha256,
                provider=proposal.provider_id,
                account=proposal.account_ref,
                connection_id=proposal.connection_id,
                connection_revision=proposal.connection_revision,
                capability=",".join(tool.capability_tags),
                operation=tool.name,
                tool_schema_sha256=tool.input_schema_sha256,
                risk_class=tool.risk_class.value,
                authority_kind=proposal.authority_kind,
                authority_id=proposal.authority_id,
                authority_revision=proposal.authority_revision,
                proposal_digest=proposal.proposal_digest,
                source_constraints_sha256=sha256_hex(
                    canonical_bytes(proposal.source_constraints_dict())
                ),
                destination_constraints_sha256=sha256_hex(
                    canonical_bytes(proposal.destination_constraints_dict())
                ),
                idempotency_key=proposal.idempotency_key,
                job_revision=authority.job_revision,
                grant_id=(
                    authority.authority_id if authority.authority_kind == "standing_grant" else ""
                ),
                grant_approval_digest=(
                    authority.authority_revision
                    if authority.authority_kind == "standing_grant"
                    else ""
                ),
                expected_revision=sha256_hex(
                    canonical_bytes(proposal.expected_provider_revisions_dict())
                ),
                is_write=is_write,
                path=self._action_ledger_path,
            )
        except Exception:
            return self._blocked(proposal, "action_ledger_idempotency")
        if not created:
            replay = self._prior_outcome(
                proposal,
                prior,
                tool=tool,
                authority=authority,
                is_write=is_write,
            )
            if replay is not None:
                return replay
            return self._blocked(proposal, "action_ledger_idempotency")

        if tracker is not None:
            with self._active_condition:
                deadline_reached = tracker.timed_out
            if deadline_reached:
                return self._deadline_outcome(proposal, tracker)

        try:
            source_context = McpSourceContext(
                server_id=proposal.server_id,
                server_version=proposal.server_version,
                artifact_sha256=proposal.artifact_sha256,
                provider_id=proposal.provider_id,
                account_ref=proposal.account_ref,
                connection_revision=proposal.connection_revision,
                tool_name=proposal.tool_name,
                tool_schema_sha256=proposal.tool_schema_sha256,
                call_timestamp=self._timestamp(),
                omega_action_lineage_id=action_id,
                max_result_bytes=tool.max_result_bytes,
                allowed_mime_types=tool.allowed_mime_types,
            )
        except Exception:
            return self._close_failed_call(
                proposal,
                action_id,
                ambiguous=False,
            )

        if tracker is not None:
            with self._active_condition:
                if tracker.timed_out:
                    deadline_reached = True
                else:
                    tracker.provider_claimed = True
                    deadline_reached = False
            if deadline_reached:
                return self._deadline_outcome(proposal, tracker)

        with anyio.CancelScope(shield=True):
            outcome = await self._execute_owned_provider_call(
                proposal=proposal,
                tracker=tracker,
                action_id=action_id,
                is_write=is_write,
                source_context=source_context,
            )
        await anyio.lowlevel.checkpoint_if_cancelled()
        return outcome

    async def _execute_owned_provider_call(
        self,
        *,
        proposal: McpOperationProposal,
        tracker: _TrackedExternalCall | None,
        action_id: str,
        is_write: bool,
        source_context: McpSourceContext,
    ) -> McpDispatchOutcome:
        try:
            raw_result = await self._supervisor.call_tool(
                proposal.server_id,
                proposal.tool_name,
                proposal.arguments_dict(),
            )
        except BaseException as exc:
            if tracker is not None and self._provider_finished(tracker):
                if isinstance(exc, anyio.get_cancelled_exc_class()):
                    raise
                return self._deadline_outcome(proposal, tracker)
            if isinstance(exc, anyio.get_cancelled_exc_class()):
                await self._close_cancelled(action_id, is_write=is_write)
                raise
            ambiguous = is_write and self._ambiguous_provider_failure(exc)
            return self._close_failed_call(
                proposal,
                action_id,
                ambiguous=ambiguous,
            )

        if tracker is not None and self._provider_finished(tracker):
            return self._deadline_outcome(proposal, tracker)

        try:
            structured = McpStructuredResult.from_dict(raw_result)
            admitted = self._ingress_admitter(source_context, structured.to_dict())
            admission = self._admission_summary(admitted, proposal, structured)
        except BaseException as exc:
            if isinstance(exc, anyio.get_cancelled_exc_class()):
                await self._close_cancelled(action_id, is_write=is_write)
                raise
            return self._close_failed_call(
                proposal,
                action_id,
                ambiguous=is_write,
            )

        if tracker is not None and tracker.timed_out:
            return self._deadline_outcome(proposal, tracker)

        try:
            closed = action_ledger.record_receipt(
                action_id,
                provider_object_id=structured.provider_object_id,
                provider_revision=structured.provider_revision,
                receipt={
                    "kind": structured.result_kind,
                    "provider_id": proposal.provider_id,
                    "provider_object_id": structured.provider_object_id,
                    "provider_revision": structured.provider_revision,
                    "status": structured.status,
                    "event_kind": structured.event_kind,
                    "count": admission.admitted_count,
                    "deduped": admission.deduplicated,
                    "via": "mcp_authority_gateway",
                    "omega_action_id": action_id,
                },
                path=self._action_ledger_path,
            )
        except Exception:
            return self._close_failed_call(
                proposal,
                action_id,
                ambiguous=is_write,
            )
        if closed is None or closed.get("state") != "confirmed":
            return self._close_failed_call(
                proposal,
                action_id,
                ambiguous=is_write,
            )
        return self._outcome(
            proposal,
            status="confirmed",
            action_id=action_id,
            provider_object_id=structured.provider_object_id,
            provider_revision=structured.provider_revision,
            admission=admission,
            deduplicated=admission.deduplicated,
        )

    def _verified_package(
        self,
        proposal: McpOperationProposal,
        record: InstalledPackageRecord | None,
    ) -> VerifiedPackage | None:
        try:
            current = self._registry.get(proposal.server_id)
        except Exception:
            return None
        if (
            record is None
            or current != record
            or not current.enabled
            or current.blocker is not None
            or current.server_id != proposal.server_id
            or current.server_version != proposal.server_version
            or current.artifact_sha256 != proposal.artifact_sha256
        ):
            return None
        try:
            package = self._verifier(self._registry.package_path(current))
        except Exception:
            return None
        if type(package) is not VerifiedPackage:
            return None
        manifest = package.manifest
        if (
            manifest.server_id != current.server_id
            or manifest.server_version != current.server_version
            or manifest.platform != current.platform
            or manifest.publisher_id != current.publisher_id
            or manifest.signing_key_id != current.signing_key_id
            or package.manifest_sha256 != current.manifest_digest
            or manifest.artifact_sha256 != current.artifact_sha256
            or manifest.tool_catalog_sha256 != current.tool_catalog_sha256
            or manifest.resource_catalog_sha256 != current.resource_catalog_sha256
            or manifest.network_class.value != current.network_class
            or manifest.server_id != proposal.server_id
            or manifest.server_version != proposal.server_version
            or manifest.artifact_sha256 != proposal.artifact_sha256
            or manifest.publisher_id != "omega.first_party"
        ):
            return None
        return package

    async def _healthy_session(
        self,
        proposal: McpOperationProposal,
        package: VerifiedPackage,
    ) -> SupervisedSessionSnapshot | None:
        try:
            snapshot = await self._supervisor.ensure_session(proposal.server_id)
        except BaseException as exc:
            if isinstance(exc, anyio.get_cancelled_exc_class()):
                raise
            return None
        if type(snapshot) is not SupervisedSessionSnapshot:
            return None
        if (
            snapshot.server_id != proposal.server_id
            or snapshot.state != "ready"
            or snapshot.protocol_version != MCP_PROTOCOL_VERSION
            or snapshot.tool_catalog_sha256 != package.manifest.tool_catalog_sha256
            or snapshot.resource_catalog_sha256 != package.manifest.resource_catalog_sha256
        ):
            return None
        return snapshot

    @staticmethod
    def _valid_arguments(
        proposal: McpOperationProposal,
        tool: ToolDefinition,
    ) -> bool:
        try:
            jsonschema.Draft202012Validator(tool.input_schema).validate(proposal.arguments_dict())
        except (jsonschema.SchemaError, jsonschema.ValidationError, TypeError, ValueError):
            return False
        return True

    def _prior_outcome(
        self,
        proposal: McpOperationProposal,
        prior: dict[str, Any] | None,
        *,
        tool: ToolDefinition,
        authority: McpAuthoritySnapshot,
        is_write: bool,
    ) -> McpDispatchOutcome | None:
        if prior is None:
            return None
        action_id = _bounded_ledger_text(prior.get("omega_action_id"))
        prior_digest = prior.get("proposal_digest")
        if action_id is None or not _exact_sha256(prior_digest):
            return self._blocked(proposal, "action_ledger_idempotency")
        if prior_digest != proposal.proposal_digest:
            return self._blocked(
                proposal,
                "action_ledger_idempotency",
                blocker="action_idempotency_conflict",
                action_id=action_id,
            )
        state = prior.get("state")
        if state == "failed":
            return None
        if state == "dispatched":
            return self._blocked(
                proposal,
                "action_ledger_idempotency",
                blocker="action_dispatched",
                action_id=action_id,
            )
        if state == "outcome_unknown":
            return self._blocked(
                proposal,
                "action_ledger_idempotency",
                blocker="action_outcome_unknown",
                action_id=action_id,
            )
        if state != "confirmed":
            return self._blocked(proposal, "action_ledger_idempotency")
        if not self._confirmed_replay_matches(
            proposal,
            prior,
            tool=tool,
            authority=authority,
            is_write=is_write,
        ):
            return self._blocked(proposal, "action_ledger_idempotency")
        return self._outcome(
            proposal,
            status="confirmed",
            action_id=action_id,
            provider_object_id=prior["provider_object_id"],
            provider_revision=prior["provider_revision"],
            deduplicated=True,
        )

    @staticmethod
    def _confirmed_replay_matches(
        proposal: McpOperationProposal,
        prior: dict[str, Any],
        *,
        tool: ToolDefinition,
        authority: McpAuthoritySnapshot,
        is_write: bool,
    ) -> bool:
        if set(prior) != _CONFIRMED_ACTION_FIELDS:
            return False
        expected = {
            "originating_cycle_id": proposal.episode_id,
            "request_id": proposal.request_id,
            "server_id": proposal.server_id,
            "server_version": proposal.server_version,
            "artifact_sha256": proposal.artifact_sha256,
            "provider": proposal.provider_id,
            "account": proposal.account_ref,
            "connection_id": proposal.connection_id,
            "capability": ",".join(tool.capability_tags),
            "operation": tool.name,
            "tool_schema_sha256": tool.input_schema_sha256,
            "risk_class": tool.risk_class.value,
            "authority_kind": proposal.authority_kind,
            "authority_id": proposal.authority_id,
            "authority_revision": proposal.authority_revision,
            "proposal_digest": proposal.proposal_digest,
            "source_constraints_sha256": sha256_hex(
                canonical_bytes(proposal.source_constraints_dict())
            ),
            "destination_constraints_sha256": sha256_hex(
                canonical_bytes(proposal.destination_constraints_dict())
            ),
            "is_write": is_write,
            "caused_by_omega": True,
            "idempotency_key": proposal.idempotency_key,
            "connection_revision": proposal.connection_revision,
            "job_revision": authority.job_revision,
            "grant_id": (
                authority.authority_id if authority.authority_kind == "standing_grant" else ""
            ),
            "grant_approval_digest": (
                authority.authority_revision if authority.authority_kind == "standing_grant" else ""
            ),
            "expected_revision": sha256_hex(
                canonical_bytes(proposal.expected_provider_revisions_dict())
            ),
            "state": "confirmed",
            "failure_reason": "",
        }
        if any(prior.get(field) != value for field, value in expected.items()):
            return False
        action_id = _bounded_ledger_text(prior.get("omega_action_id"))
        provider_request_id = _bounded_ledger_text(prior.get("provider_request_id"))
        provider_correlation_id = _bounded_ledger_text(
            prior.get("provider_correlation_id"), optional=True
        )
        provider_object_id = _bounded_ledger_text(prior.get("provider_object_id"))
        provider_revision = _bounded_ledger_text(prior.get("provider_revision"), optional=True)
        dispatched_at = _bounded_ledger_text(prior.get("dispatched_at"))
        closed_at = _bounded_ledger_text(prior.get("closed_at"))
        if None in {
            action_id,
            provider_request_id,
            provider_correlation_id,
            provider_object_id,
            provider_revision,
            dispatched_at,
            closed_at,
        }:
            return False
        provider_idempotency = prior.get("provider_idempotency")
        if (
            type(provider_idempotency) is not dict
            or set(provider_idempotency) != _PROVIDER_IDEMPOTENCY_FIELDS
            or provider_idempotency.get("provider_request_id") != provider_request_id
            or provider_idempotency.get("provider_correlation_id") != provider_correlation_id
            or provider_idempotency.get("idempotency_key") != proposal.idempotency_key
        ):
            return False
        receipt = prior.get("receipt")
        if type(receipt) is not dict or set(receipt) != _CONFIRMED_RECEIPT_FIELDS:
            return False
        if (
            _bounded_ledger_text(receipt.get("kind")) is None
            or receipt.get("kind") not in MCP_RESULT_KINDS
            or _bounded_ledger_text(receipt.get("status")) is None
            or receipt.get("status") not in MCP_RESULT_STATUSES
            or _bounded_ledger_text(receipt.get("event_kind")) is None
            or receipt.get("event_kind") not in MCP_EVENT_KINDS
            or receipt.get("provider_id") != proposal.provider_id
            or receipt.get("provider_object_id") != provider_object_id
            or receipt.get("provider_revision") != provider_revision
            or type(receipt.get("count")) is not int
            or receipt["count"] <= 0
            or type(receipt.get("deduped")) is not bool
            or receipt.get("via") != "mcp_authority_gateway"
            or receipt.get("omega_action_id") != action_id
        ):
            return False
        return True

    @staticmethod
    def _ambiguous_provider_failure(error: BaseException) -> bool:
        if isinstance(
            error,
            (
                TimeoutError,
                ConnectionError,
                BrokenPipeError,
                EOFError,
                anyio.BrokenResourceError,
                anyio.ClosedResourceError,
                anyio.EndOfStream,
            ),
        ):
            return True
        status_code = getattr(error, "status_code", None)
        if type(status_code) is int and status_code >= 500:
            return True
        return type(error).__name__ == "McpSessionError" and str(error) == "tool_call_failed"

    def _close_failed_call(
        self,
        proposal: McpOperationProposal,
        action_id: str,
        *,
        ambiguous: bool,
    ) -> McpDispatchOutcome:
        try:
            action_ledger.record_failure(
                action_id,
                "provider_outcome_unknown" if ambiguous else "provider_call_failed",
                ambiguous=ambiguous,
                path=self._action_ledger_path,
            )
        except Exception:
            ambiguous = True
        return self._outcome(
            proposal,
            status="outcome_unknown" if ambiguous else "failed",
            action_id=action_id,
            blocker="provider_outcome_unknown" if ambiguous else "provider_call_failed",
        )

    async def _close_cancelled(self, action_id: str, *, is_write: bool) -> None:
        def close() -> None:
            action_ledger.record_failure(
                action_id,
                "provider_outcome_unknown" if is_write else "provider_call_cancelled",
                ambiguous=is_write,
                path=self._action_ledger_path,
            )

        try:
            with anyio.move_on_after(LEDGER_CANCELLATION_CLEANUP_TIMEOUT_S, shield=True):
                await anyio.to_thread.run_sync(close, abandon_on_cancel=True)
        except BaseException:
            return

    @staticmethod
    def _admission_summary(
        admitted: McpAdmissionReceipt,
        proposal: McpOperationProposal,
        structured: McpStructuredResult,
    ) -> McpAdmissionSummary:
        if type(admitted) is not McpAdmissionReceipt:
            raise ValueError("invalid_mcp_admission")
        source = admitted.source_envelope
        if (
            source.server_id != proposal.server_id
            or source.server_version != proposal.server_version
            or source.artifact_sha256 != proposal.artifact_sha256
            or source.provider_id != proposal.provider_id
            or source.account_ref != proposal.account_ref
            or source.connection_revision != proposal.connection_revision
            or source.tool_name != proposal.tool_name
            or source.tool_schema_sha256 != proposal.tool_schema_sha256
            or source.provider_object_id != structured.provider_object_id
            or source.provider_revision != structured.provider_revision
        ):
            raise ValueError("invalid_mcp_admission")
        return McpAdmissionSummary(
            source_ledger_receipt=admitted.source_ledger_receipt,
            guardian_receipt=admitted.guardian_receipt,
            memory_ids=admitted.memory_ids,
            admitted_count=len(admitted.memory_ids),
            deduplicated=admitted.deduplicated,
        )

    async def disable_external(
        self,
        expected_revision: str,
    ) -> McpExternalDisableOutcome:
        """Turn external MCP off first, then drain under one two-second deadline."""
        return await anyio.to_thread.run_sync(
            self._disable_external_sync,
            expected_revision,
        )

    async def drain_package(self, server_id: str) -> dict[str, int]:
        """Boundedly finalize in-flight operations for one disabled package."""
        return await anyio.to_thread.run_sync(self._drain_package_sync, server_id)

    def _drain_package_sync(self, server_id: str) -> dict[str, int]:
        if type(server_id) is not str or not server_id:
            raise ValueError("invalid_package_identity")
        with self._package_drain_lock:
            with self._active_condition:
                initial_tokens = {
                    token
                    for token, tracker in self._active_calls.items()
                    if tracker.server_id == server_id
                }
                deadline = time.monotonic() + PACKAGE_CALL_DRAIN_TIMEOUT_S
                while initial_tokens.intersection(self._active_calls):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._active_condition.wait(timeout=remaining)
                unresolved_tokens = initial_tokens.intersection(self._active_calls)
                unresolved = [self._active_calls[token] for token in unresolved_tokens]
                for tracker in unresolved:
                    tracker.timed_out = True
                    tracker.deadline_blocker = "package_disabled"
                provider_unresolved = [
                    tracker for tracker in unresolved if tracker.provider_claimed
                ]
                definitive_pre_provider = len(unresolved) - len(provider_unresolved)
                durable_trackers = [
                    tracker
                    for tracker in unresolved
                    if tracker.action_id and tracker.is_write is not None
                ]
                finalized = self._finalize_deadline_trackers_locked(durable_trackers)
                batch_failed = bool(durable_trackers) and finalized is None
                return {
                    "drained_calls": (
                        len(initial_tokens) - len(unresolved_tokens) + definitive_pre_provider
                    ),
                    "uncertain_writes": (
                        sum(tracker.is_write is True for tracker in provider_unresolved)
                        if not batch_failed
                        else 0
                    ),
                    "failed_reads": (
                        sum(tracker.is_write is False for tracker in provider_unresolved)
                        if not batch_failed
                        else 0
                    ),
                    "errors": int(batch_failed),
                }

    def _disable_external_sync(
        self,
        expected_revision: str,
    ) -> McpExternalDisableOutcome:
        with self._disable_lock:
            with self._active_condition:
                current = self._external_switch.load()
                pending_retry = any(
                    self._tracker_requires_reconciliation(tracker)
                    for tracker in self._active_external.values()
                )
                if (
                    not current.enabled
                    and self._last_disable_expected == expected_revision
                    and self._last_disable_outcome is not None
                    and not pending_retry
                ):
                    return self._last_disable_outcome
                retrying_previous_disable = bool(
                    not current.enabled
                    and self._last_disable_expected == expected_revision
                    and self._last_disable_outcome is not None
                    and pending_retry
                )
                if not retrying_previous_disable:
                    self._external_switch.set_enabled(
                        False,
                        expected_revision=expected_revision,
                    )
                    self._next_disable_attempt += 1
                    disable_attempt = self._next_disable_attempt
                else:
                    disable_attempt = self._last_disable_attempt
                    if disable_attempt is None:
                        raise RuntimeError("invalid_disable_recovery")
                initial_tokens = set(self._active_external)
                deadline = time.monotonic() + EXTERNAL_CALL_DRAIN_TIMEOUT_S
                while initial_tokens.intersection(self._active_external):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._active_condition.wait(timeout=remaining)
                unresolved_tokens = initial_tokens.intersection(self._active_external)
                unresolved = [self._active_external[token] for token in unresolved_tokens]
                for tracker in unresolved:
                    tracker.timed_out = True
                provider_unresolved = [
                    tracker for tracker in unresolved if tracker.provider_claimed
                ]
                definitive_pre_provider = len(unresolved) - len(provider_unresolved)
                durable_trackers = [
                    tracker
                    for tracker in unresolved
                    if tracker.action_id and tracker.is_write is not None
                ]
                finalized = self._finalize_deadline_trackers_locked(durable_trackers)
                batch_failed = bool(durable_trackers) and finalized is None
                if batch_failed:
                    self._disable_recovery = _DisableRecovery(
                        attempt=disable_attempt,
                        expected_revision=expected_revision,
                        classifications={
                            tracker.token: (
                                "uncertain_write"
                                if tracker.provider_claimed and tracker.is_write is True
                                else (
                                    "failed_read"
                                    if tracker.provider_claimed and tracker.is_write is False
                                    else None
                                )
                            )
                            for tracker in durable_trackers
                        },
                        finalized_tokens=set(),
                    )
                if (
                    retrying_previous_disable
                    and not batch_failed
                    and self._disable_recovery is None
                    and self._last_disable_attempt == disable_attempt
                    and self._last_disable_outcome is not None
                ):
                    return self._last_disable_outcome
            uncertain_writes = (
                sum(tracker.is_write is True for tracker in provider_unresolved)
                if not batch_failed
                else 0
            )
            failed_reads = (
                sum(tracker.is_write is False for tracker in provider_unresolved)
                if not batch_failed
                else 0
            )
            remaining = max(0.0, deadline - time.monotonic())
            stopped = self._stop_external_within(remaining)
            outcome = McpExternalDisableOutcome(
                drained_calls=(
                    len(initial_tokens) - len(unresolved_tokens) + definitive_pre_provider
                ),
                uncertain_writes=uncertain_writes,
                failed_reads=failed_reads,
                sessions_closed=stopped["sessions_closed"],
                sessions_forced=stopped["sessions_forced"],
                errors=stopped["errors"] + int(batch_failed),
            )
            with self._active_condition:
                self._last_disable_expected = expected_revision
                self._last_disable_attempt = disable_attempt
                self._last_disable_outcome = outcome
                self._refresh_disable_recovery_locked()
                return self._last_disable_outcome

    def _stop_external_within(self, timeout_s: float) -> dict[str, int]:
        completed = threading.Event()
        result: dict[str, object] = {}

        def stop() -> None:
            try:
                result["value"] = self._supervisor.stop_external_sync(timeout_s)
            except BaseException:
                result["failed"] = True
            finally:
                completed.set()
                with self._cleanup_lock:
                    self._cleanup_threads.discard(threading.current_thread())

        worker = threading.Thread(
            target=stop,
            name="mcp-external-session-cleanup",
            daemon=False,
        )
        with self._cleanup_lock:
            self._cleanup_threads.add(worker)
        try:
            worker.start()
        except BaseException:
            with self._cleanup_lock:
                self._cleanup_threads.discard(worker)
            return {"sessions_closed": 0, "sessions_forced": 0, "errors": 1}
        completed.wait(timeout=max(0.0, timeout_s))
        if not completed.is_set():
            return {"sessions_closed": 0, "sessions_forced": 0, "errors": 1}
        stopped = result.get("value")
        if (
            type(stopped) is not dict
            or set(stopped) != {"sessions_closed", "sessions_forced", "errors"}
            or any(type(value) is not int or value < 0 for value in stopped.values())
        ):
            return {"sessions_closed": 0, "sessions_forced": 0, "errors": 1}
        return stopped

    def wait_for_cleanup_sync(self, timeout_s: float) -> bool:
        """Finish durable deadline and session cleanup before host shutdown."""
        if type(timeout_s) not in (int, float) or timeout_s < 0:
            raise ValueError("invalid_cleanup_timeout")
        deadline = time.monotonic() + timeout_s
        remaining = max(0.0, deadline - time.monotonic())
        if not self._disable_lock.acquire(timeout=remaining):
            return False
        try:
            remaining = max(0.0, deadline - time.monotonic())
            if not self._package_drain_lock.acquire(timeout=remaining):
                return False
            try:
                with self._active_condition:
                    pending = [
                        tracker
                        for tracker in self._active_calls.values()
                        if self._tracker_requires_reconciliation(tracker)
                    ]
                    for failure_reason in (
                        "external_mcp_switched_off",
                        "package_disabled",
                    ):
                        group = [
                            tracker
                            for tracker in pending
                            if tracker.deadline_blocker == failure_reason
                        ]
                        if group and self._finalize_deadline_trackers_locked(group) is None:
                            return False
                    if self._active_calls:
                        return False
            finally:
                self._package_drain_lock.release()
        finally:
            self._disable_lock.release()
        while True:
            with self._cleanup_lock:
                workers = tuple(self._cleanup_threads)
            if not workers:
                return True
            for worker in workers:
                if worker is threading.current_thread():
                    return False
                worker.join(timeout=max(0.0, deadline - time.monotonic()))
                if worker.is_alive():
                    return False

    def _tracker_requires_reconciliation(self, tracker: _TrackedExternalCall) -> bool:
        durable_reconciliation = bool(
            tracker.timed_out
            and tracker.action_id
            and tracker.is_write is not None
            and not tracker.deadline_finalized
        )
        recovery = self._disable_recovery if tracker.external else None
        cached_reconciliation = bool(
            recovery is not None and tracker.token in recovery.classifications
        )
        return durable_reconciliation or cached_reconciliation

    def _refresh_disable_recovery_locked(self) -> None:
        recovery = self._disable_recovery
        if recovery is None or set(recovery.classifications) != recovery.finalized_tokens:
            return
        if (
            self._last_disable_attempt != recovery.attempt
            or self._last_disable_expected != recovery.expected_revision
            or self._last_disable_outcome is None
            or self._last_disable_outcome.errors < 1
        ):
            return
        cached = self._last_disable_outcome
        self._last_disable_outcome = McpExternalDisableOutcome(
            drained_calls=cached.drained_calls,
            uncertain_writes=cached.uncertain_writes
            + sum(
                classification == "uncertain_write"
                for classification in recovery.classifications.values()
            ),
            failed_reads=cached.failed_reads
            + sum(
                classification == "failed_read"
                for classification in recovery.classifications.values()
            ),
            sessions_closed=cached.sessions_closed,
            sessions_forced=cached.sessions_forced,
            errors=cached.errors - 1,
        )
        recovered_tokens = tuple(recovery.classifications)
        self._disable_recovery = None
        for token in recovered_tokens:
            tracker = self._active_external.get(token)
            if tracker is not None and tracker.deadline_finalized and tracker.owner_finished:
                self._remove_tracker_locked(tracker)
        self._active_condition.notify_all()

    def _finalize_deadline_trackers_locked(
        self,
        trackers: list[_TrackedExternalCall],
    ) -> dict[str, str] | None:
        actions = {
            tracker.action_id: bool(tracker.provider_claimed and tracker.is_write is True)
            for tracker in trackers
        }
        if not actions:
            return {}
        failure_reasons = {tracker.deadline_blocker for tracker in trackers}
        if len(failure_reasons) != 1:
            return None
        failure_reason = next(iter(failure_reasons))
        expected = {
            action_id: "outcome_unknown" if ambiguous else "failed"
            for action_id, ambiguous in actions.items()
        }
        try:
            finalized = action_ledger.record_switch_deadline_batch(
                actions,
                failure_reason=failure_reason,
                path=self._action_ledger_path,
                lock_timeout_s=0.0,
            )
        except Exception:
            return None
        if type(finalized) is not dict or finalized != expected:
            return None
        recovery = self._disable_recovery if failure_reason == "external_mcp_switched_off" else None
        for tracker in trackers:
            tracker.deadline_finalized = tracker.action_id in finalized
            if (
                tracker.deadline_finalized
                and recovery is not None
                and tracker.token in recovery.classifications
            ):
                recovery.finalized_tokens.add(tracker.token)
        if recovery is not None:
            self._refresh_disable_recovery_locked()
        for tracker in trackers:
            if (
                tracker.deadline_finalized
                and tracker.owner_finished
                and not self._tracker_requires_reconciliation(tracker)
            ):
                self._remove_tracker_locked(tracker)
        self._active_condition.notify_all()
        return finalized

    def _finish_external(self, tracker: _TrackedExternalCall) -> None:
        with self._active_condition:
            tracker.owner_finished = True
            if not self._tracker_requires_reconciliation(tracker):
                self._remove_tracker_locked(tracker)
            self._active_condition.notify_all()

    def _provider_finished(self, tracker: _TrackedExternalCall) -> bool:
        """End network-call tracking before local result admission continues."""
        with self._active_condition:
            timed_out = tracker.timed_out
            if not timed_out:
                self._remove_tracker_locked(tracker)
                self._active_condition.notify_all()
            return timed_out

    def _remove_tracker_locked(self, tracker: _TrackedExternalCall) -> None:
        self._active_calls.pop(tracker.token, None)
        self._active_external.pop(tracker.token, None)

    def _deadline_outcome(
        self,
        proposal: McpOperationProposal,
        tracker: _TrackedExternalCall,
    ) -> McpDispatchOutcome:
        if not tracker.deadline_finalized:
            with self._active_condition:
                self._finalize_deadline_trackers_locked([tracker])
        is_write = tracker.is_write is True and tracker.provider_claimed
        return self._outcome(
            proposal,
            status="outcome_unknown" if is_write else "failed",
            action_id=tracker.action_id,
            blocker=tracker.deadline_blocker,
            failed_check="external_mcp_eligibility",
        )

    def _blocked(
        self,
        proposal: McpOperationProposal,
        failed_check: str,
        *,
        blocker: str | None = None,
        action_id: str = "",
    ) -> McpDispatchOutcome:
        return self._outcome(
            proposal,
            status="blocked",
            action_id=action_id,
            blocker=blocker or failed_check,
            failed_check=failed_check,
        )

    @staticmethod
    def _outcome(
        proposal: McpOperationProposal,
        *,
        status: str,
        action_id: str,
        blocker: str | None = None,
        failed_check: str | None = None,
        provider_object_id: str = "",
        provider_revision: str = "",
        admission: McpAdmissionSummary | None = None,
        deduplicated: bool = False,
    ) -> McpDispatchOutcome:
        return McpDispatchOutcome(
            status=status,
            action_id=action_id,
            blocker=blocker,
            failed_check=failed_check,
            server_id=proposal.server_id,
            server_version=proposal.server_version,
            artifact_sha256=proposal.artifact_sha256,
            provider_id=proposal.provider_id,
            account_ref=proposal.account_ref,
            connection_id=proposal.connection_id,
            tool_name=proposal.tool_name,
            tool_schema_sha256=proposal.tool_schema_sha256,
            provider_object_id=provider_object_id,
            provider_revision=provider_revision,
            admission=admission,
            deduplicated=deduplicated,
            uncertain=status == "outcome_unknown",
        )


def _plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_plain_json(item) for item in value]
    return value


def _bounded_ledger_text(value: Any, *, optional: bool = False) -> str | None:
    if type(value) is not str:
        return None
    if optional and value == "":
        return value
    if not value or value.strip() != value:
        return None
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        return None
    if len(encoded) > _MAX_LEDGER_IDENTITY_BYTES:
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return None
    return value


def _exact_sha256(value: Any) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


_MISSING_POINTER_VALUE = object()


def _resolve_json_pointer(document: Any, pointer: Any) -> Any:
    if type(pointer) is not str or not pointer.startswith("/"):
        return _MISSING_POINTER_VALUE
    current = document
    for encoded in pointer[1:].split("/"):
        segment = encoded.replace("~1", "/").replace("~0", "~")
        if "~" in encoded.replace("~0", "").replace("~1", ""):
            return _MISSING_POINTER_VALUE
        if type(current) is dict:
            if segment not in current:
                return _MISSING_POINTER_VALUE
            current = current[segment]
            continue
        if type(current) is list and segment.isdigit():
            index = int(segment)
            if index >= len(current):
                return _MISSING_POINTER_VALUE
            current = current[index]
            continue
        return _MISSING_POINTER_VALUE
    return current
