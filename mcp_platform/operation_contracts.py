"""Closed immutable contracts for authority-gated MCP operations."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.package_contracts import RiskClass

_PROPOSAL_FIELDS = frozenset(
    {
        "request_id",
        "episode_id",
        "server_id",
        "server_version",
        "artifact_sha256",
        "provider_id",
        "account_ref",
        "connection_id",
        "connection_revision",
        "tool_name",
        "tool_schema_sha256",
        "arguments",
        "source_constraints",
        "destination_constraints",
        "expected_provider_revisions",
        "authority_kind",
        "authority_id",
        "authority_revision",
        "plan_digest",
        "candidate_id",
        "plan_step_id",
        "connection_authority_revision",
        "idempotency_key",
        "evidence_memory_ids",
        "proposal_digest",
    }
)
_AUTHORITY_KINDS = frozenset({"direct_turn", "standing_grant"})
_RISK_CLASSES = frozenset(
    {
        "read_only",
        "additive_write",
        "mutating_write",
        "destructive",
        "send_publish",
        "cross_provider_transfer",
        # Discovered (never Omega-reviewed) capability from a publisher-verified
        # or personal connection (mcp_platform.plugin_discovery). Admitted here so
        # a McpAuthoritySnapshot can carry it; McpAuthoritySnapshot's own
        # allow_send_publish/allow_destructive/allow_cross_provider predicates
        # never match it (they are exact `is RiskClass.X` comparisons), so it
        # never carries elevated or standing authority by construction.
        "external_unreviewed",
    }
)
_OUTCOME_STATUSES = frozenset({"blocked", "confirmed", "failed", "outcome_unknown"})
_HEX_RE = re.compile(r"[0-9a-f]{64}\Z")
_SOURCE_RECEIPT_RE = re.compile(r"src_[0-9a-f]{24}\Z")
_MEMORY_ID_RE = re.compile(r"mcp-[0-9a-f]{32}\Z")
_BLOCKER_RE = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")
_MAX_JSON_DEPTH = 48
_MAX_JSON_NODES = 20_000
_MAX_JSON_BYTES = 1_048_576
_MAX_STRING_BYTES = 65_536
_MAPPING_PROXY_TYPE = type(MappingProxyType({}))
_SECRET_KEY_SUFFIXES = frozenset(
    {
        "authorization",
        "providertoken",
        "accesstoken",
        "refreshtoken",
        "clientsecret",
        "vaultkey",
        "apikey",
        "privatekey",
        "password",
        "passphrase",
    }
)


def _stable_string(
    value: Any,
    *,
    error: str,
    optional: bool = False,
    max_bytes: int = 1024,
) -> str:
    if type(value) is not str:
        raise ValueError(error)
    if optional and value == "":
        return value
    if not value or value.strip() != value:
        raise ValueError(error)
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        raise ValueError(error) from None
    if len(encoded) > max_bytes or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise ValueError(error)
    return value


def _digest(value: Any, *, error: str) -> str:
    digest = _stable_string(value, error=error, max_bytes=64)
    if _HEX_RE.fullmatch(digest) is None:
        raise ValueError(error)
    return digest


def _stable_tuple(
    value: Any,
    *,
    error: str,
    allow_empty: bool = True,
) -> tuple[str, ...]:
    if type(value) not in {list, tuple}:
        raise ValueError(error)
    result = tuple(_stable_string(item, error=error) for item in value)
    if (not allow_empty and not result) or len(result) != len(set(result)):
        raise ValueError(error)
    return result


def _normalized_key(key: str) -> str:
    return "".join(character for character in key.casefold() if character.isalnum())


def _secret_key(key: str) -> bool:
    normalized = _normalized_key(key)
    return any(normalized.endswith(suffix) for suffix in _SECRET_KEY_SUFFIXES)


def _owned_json(
    value: Any,
    *,
    reject_secrets: bool,
    error: str,
) -> Any:
    seen: set[int] = set()
    nodes = 0

    def copy(item: Any, depth: int) -> Any:
        nonlocal nodes
        nodes += 1
        if depth > _MAX_JSON_DEPTH or nodes > _MAX_JSON_NODES:
            raise ValueError(error)
        if item is None or type(item) in {bool, int}:
            return item
        if type(item) is float:
            if item != item or item in {float("inf"), float("-inf")}:
                raise ValueError(error)
            return item
        if type(item) is str:
            try:
                if len(item.encode("utf-8")) > _MAX_STRING_BYTES:
                    raise ValueError(error)
            except UnicodeError:
                raise ValueError(error) from None
            return item
        if type(item) is dict:
            identity = id(item)
            if identity in seen or any(type(key) is not str for key in item):
                raise ValueError(error)
            seen.add(identity)
            try:
                copied: dict[str, Any] = {}
                for key, child in item.items():
                    if reject_secrets and _secret_key(key):
                        raise ValueError("secret_bearing_operation_field")
                    copied[key] = copy(child, depth + 1)
                return MappingProxyType(copied)
            finally:
                seen.remove(identity)
        if type(item) is list:
            identity = id(item)
            if identity in seen:
                raise ValueError(error)
            seen.add(identity)
            try:
                return tuple(copy(child, depth + 1) for child in item)
            finally:
                seen.remove(identity)
        raise ValueError(error)

    try:
        copied = copy(value, 0)
        if len(canonical_bytes(_thaw_json(copied))) > _MAX_JSON_BYTES:
            raise ValueError(error)
        return copied
    except ValueError as exc:
        if str(exc) == "secret_bearing_operation_field":
            raise
        raise ValueError(error) from None
    except (OverflowError, RecursionError, TypeError, UnicodeError):
        raise ValueError(error) from None


def _json_object(value: Any, *, reject_secrets: bool, error: str) -> Mapping[str, Any]:
    if type(value) is _MAPPING_PROXY_TYPE:
        value = _thaw_json(value)
    if type(value) is not dict:
        raise ValueError(error)
    copied = _owned_json(value, reject_secrets=reject_secrets, error=error)
    if not isinstance(copied, Mapping):
        raise ValueError(error)
    return copied


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_thaw_json(item) for item in value]
    return value


def _schema_mapping(value: Any, *, error: str) -> Mapping[str, str]:
    if type(value) is _MAPPING_PROXY_TYPE:
        value = dict(value)
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise ValueError(error)
    parsed = {
        _stable_string(key, error=error): _digest(digest, error=error)
        for key, digest in value.items()
    }
    return MappingProxyType(dict(sorted(parsed.items())))


def _provider_revisions(value: Any) -> Mapping[str, str]:
    if type(value) is _MAPPING_PROXY_TYPE:
        value = dict(value)
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise ValueError("invalid_operation_proposal")
    parsed = {
        _stable_string(key, error="invalid_operation_proposal"): _stable_string(
            revision,
            error="invalid_operation_proposal",
            max_bytes=4096,
        )
        for key, revision in value.items()
    }
    return MappingProxyType(dict(sorted(parsed.items())))


def _allowed_tools_tuple(value: Any, *, error: str) -> tuple[Mapping[str, str], ...]:
    """Validate a template grant's closed ``{tool_name, tool_schema_sha256,
    risk_class, effect, capability}`` allow-list: unique tool names, exactly
    these five keys, every risk class drawn from the closed risk vocabulary.

    Each entry is fully self-describing -- carrying its own schema digest,
    risk class, effect, and capability -- so a template grant spanning
    several tools (e.g. a read tool and a write tool with genuinely
    different effects and capabilities) binds each one exactly, rather than
    through the grant's single top-level ``effect``/``capability_tags``
    scalars (fix round 2; round 1's `capability_tags`-membership check and
    the schema-set publish-time proxy it required are both superseded by
    per-entry ``effect``/``capability`` identity).
    """

    if type(value) not in {tuple, list}:
        raise ValueError(error)
    parsed: list[dict[str, str]] = []
    for entry in value:
        if type(entry) is _MAPPING_PROXY_TYPE:
            entry = dict(entry)
        if type(entry) is not dict or set(entry) != {
            "tool_name",
            "tool_schema_sha256",
            "risk_class",
            "effect",
            "capability",
        }:
            raise ValueError(error)
        risk_class = _stable_string(entry["risk_class"], error=error)
        if risk_class not in _RISK_CLASSES:
            raise ValueError(error)
        # A capability must be usable to build a "tool:<capability>" tag the
        # same way ``mcp_platform.plugin_authority._tool_capability_id``
        # builds one from a live binding, so it is held to the same
        # no-colon rule (that module cannot be imported here without a
        # cycle, since it imports this one).
        capability = _stable_string(entry["capability"], error=error)
        if ":" in capability:
            raise ValueError(error)
        parsed.append(
            {
                "tool_name": _stable_string(entry["tool_name"], error=error),
                "tool_schema_sha256": _digest(entry["tool_schema_sha256"], error=error),
                "risk_class": risk_class,
                "effect": _stable_string(entry["effect"], error=error),
                "capability": capability,
            }
        )
    names = tuple(entry["tool_name"] for entry in parsed)
    if len(set(names)) != len(names):
        raise ValueError(error)
    parsed.sort(key=lambda entry: entry["tool_name"])
    return tuple(MappingProxyType(entry) for entry in parsed)


def _argument_constraints_mapping(
    value: Any, *, tool_names: frozenset[str], error: str
) -> Mapping[str, Mapping[str, object]]:
    """Validate a template grant's per-tool argument-constraint mapping.

    Each key must name a tool declared in ``allowed_tools``; each value is a
    generic bounded JSON object (the closed constraint grammar itself is
    sealed by ``mcp_platform.plugin_argument_constraints`` at grant-authoring
    time, against the tool's live input schema, which this layer does not
    carry). The per-tool value is stored as a plain ``dict`` -- not a
    ``MappingProxyType`` -- because ``evaluate_argument_constraints`` type-
    checks its ``constraints`` argument with ``type(...) is dict``.

    Every field on ``McpAuthoritySnapshot`` is re-validated on every
    construction, including a ``dataclasses.replace`` of an already-valid
    instance (the standing-execution chain does exactly this). A per-tool
    spec's own nested values -- e.g. a ``one_of`` list -- come back from
    ``_owned_json`` as a tuple on that first pass, and ``_owned_json`` only
    recognizes a bare ``list`` as array-shaped JSON. Each spec is thawed
    (``Mapping``/tuple back to ``dict``/list, recursively) before every
    validation pass, first or Nth, so re-validating an already-valid grant
    can never itself raise.
    """

    if type(value) is _MAPPING_PROXY_TYPE:
        value = dict(value)
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise ValueError(error)
    if any(key not in tool_names for key in value):
        raise ValueError(error)
    parsed = {
        key: dict(_json_object(_thaw_json(spec), reject_secrets=False, error=error))
        for key, spec in value.items()
    }
    return MappingProxyType(dict(sorted(parsed.items())))


@dataclass(frozen=True)
class McpConnectionSnapshot:
    """Trusted current connection identity, revision, and capability ceilings."""

    connection_id: str
    connection_revision: str
    server_id: str
    server_version: str
    artifact_sha256: str
    provider_id: str
    account_ref: str
    enabled: bool
    capability_ceiling: tuple[str, ...]
    allowed_tool_schemas: Mapping[str, str]
    source_constraints: Mapping[str, Any]
    destination_constraints: Mapping[str, Any]
    authority_revision: str = ""

    def __post_init__(self) -> None:
        error = "invalid_connection_snapshot"
        for field_name in (
            "connection_id",
            "server_id",
            "server_version",
            "provider_id",
            "account_ref",
        ):
            object.__setattr__(
                self, field_name, _stable_string(getattr(self, field_name), error=error)
            )
        object.__setattr__(
            self, "connection_revision", _digest(self.connection_revision, error=error)
        )
        object.__setattr__(self, "artifact_sha256", _digest(self.artifact_sha256, error=error))
        object.__setattr__(
            self,
            "authority_revision",
            _digest(self.authority_revision, error=error) if self.authority_revision else "",
        )
        if type(self.enabled) is not bool:
            raise ValueError(error)
        object.__setattr__(
            self,
            "capability_ceiling",
            _stable_tuple(self.capability_ceiling, error=error),
        )
        object.__setattr__(
            self,
            "allowed_tool_schemas",
            _schema_mapping(self.allowed_tool_schemas, error=error),
        )
        object.__setattr__(
            self,
            "source_constraints",
            _json_object(self.source_constraints, reject_secrets=False, error=error),
        )
        object.__setattr__(
            self,
            "destination_constraints",
            _json_object(self.destination_constraints, reject_secrets=False, error=error),
        )


@dataclass(frozen=True)
class McpAuthoritySnapshot:
    """Trusted direct-turn or standing-grant authority bound to exact signed material."""

    authority_kind: str
    authority_id: str
    authority_revision: str
    episode_id: str
    job_id: str
    job_revision: str
    connection_id: str
    connection_revision: str
    server_id: str
    server_version: str
    artifact_sha256: str
    tool_name: str
    tool_schema_sha256: str
    capability_tags: tuple[str, ...]
    source_constraints: Mapping[str, Any]
    destination_constraints: Mapping[str, Any]
    risk_classes: tuple[str, ...]
    direct_execution: bool
    standing_execution: bool
    allow_send_publish: bool
    allow_destructive: bool
    allow_cross_provider: bool
    plan_digest: str = ""
    account_key_hash: str = ""
    candidate_id: str = ""
    plan_step_id: str = ""
    arguments_sha256: str = ""
    connection_authority_revision: str = ""
    effect: str = ""
    allowed_tools: tuple[Mapping[str, str], ...] = ()
    argument_constraints: Mapping[str, Mapping[str, object]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        error = "invalid_authority_snapshot"
        if self.authority_kind not in _AUTHORITY_KINDS:
            raise ValueError(error)
        for field_name in (
            "authority_id",
            "episode_id",
            "connection_id",
            "server_id",
            "server_version",
            "tool_name",
        ):
            object.__setattr__(
                self, field_name, _stable_string(getattr(self, field_name), error=error)
            )
        for field_name in (
            "authority_revision",
            "connection_revision",
            "artifact_sha256",
            "tool_schema_sha256",
        ):
            object.__setattr__(
                self, field_name, _digest(getattr(self, field_name), error=error)
            )
        object.__setattr__(self, "job_id", _stable_string(self.job_id, error=error, optional=True))
        object.__setattr__(
            self,
            "job_revision",
            _digest(self.job_revision, error=error) if self.job_revision else "",
        )
        object.__setattr__(
            self, "capability_tags", _stable_tuple(self.capability_tags, error=error)
        )
        risks = _stable_tuple(self.risk_classes, error=error, allow_empty=False)
        if any(risk not in _RISK_CLASSES for risk in risks):
            raise ValueError(error)
        object.__setattr__(self, "risk_classes", risks)
        for field_name in (
            "direct_execution",
            "standing_execution",
            "allow_send_publish",
            "allow_destructive",
            "allow_cross_provider",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise ValueError(error)

        # -- allowed_tools / argument_constraints: the template-grant scope --
        object.__setattr__(
            self, "allowed_tools", _allowed_tools_tuple(self.allowed_tools, error=error)
        )
        tool_names = frozenset(entry["tool_name"] for entry in self.allowed_tools)
        object.__setattr__(
            self,
            "argument_constraints",
            _argument_constraints_mapping(
                self.argument_constraints, tool_names=tool_names, error=error
            ),
        )
        is_template = bool(self.argument_constraints)
        if is_template and self.authority_kind != "standing_grant":
            raise ValueError(error)
        if not is_template and self.allowed_tools:
            raise ValueError(error)

        if is_template:
            # Per-plan ephemera (a recurring job plans fresh steps every run)
            # never accompany a template grant; the connection/account
            # identity the grant is scoped to, and the tool effect it
            # authorizes, always do.
            if (
                self.plan_digest
                or self.candidate_id
                or self.plan_step_id
                or self.arguments_sha256
            ):
                raise ValueError(error)
            object.__setattr__(
                self, "account_key_hash", _digest(self.account_key_hash, error=error)
            )
            object.__setattr__(
                self,
                "connection_authority_revision",
                _digest(self.connection_authority_revision, error=error),
            )
            object.__setattr__(self, "effect", _stable_string(self.effect, error=error))
        else:
            if bool(self.plan_digest) != bool(self.account_key_hash):
                raise ValueError(error)
            if self.plan_digest:
                object.__setattr__(self, "plan_digest", _digest(self.plan_digest, error=error))
                object.__setattr__(
                    self,
                    "account_key_hash",
                    _digest(self.account_key_hash, error=error),
                )
                for field_name in ("candidate_id", "plan_step_id", "effect"):
                    object.__setattr__(
                        self,
                        field_name,
                        _stable_string(getattr(self, field_name), error=error),
                    )
                for field_name in ("arguments_sha256", "connection_authority_revision"):
                    object.__setattr__(
                        self,
                        field_name,
                        _digest(getattr(self, field_name), error=error),
                    )
            elif any(
                (
                    self.candidate_id,
                    self.plan_step_id,
                    self.arguments_sha256,
                    self.connection_authority_revision,
                    self.effect,
                )
            ):
                raise ValueError(error)
        if self.authority_kind == "direct_turn":
            if (
                not self.direct_execution
                or self.standing_execution
                or self.job_id
                or self.job_revision
            ):
                raise ValueError(error)
        elif (
            not self.standing_execution
            or self.direct_execution
            or not self.job_id
            or not self.job_revision
        ):
            raise ValueError(error)
        object.__setattr__(
            self,
            "source_constraints",
            _json_object(self.source_constraints, reject_secrets=False, error=error),
        )
        object.__setattr__(
            self,
            "destination_constraints",
            _json_object(self.destination_constraints, reject_secrets=False, error=error),
        )

    def __copy__(self) -> McpAuthoritySnapshot:
        return self

    def __deepcopy__(self, _memo: dict[int, object]) -> McpAuthoritySnapshot:
        return self


@dataclass(frozen=True, slots=True)
class McpToolAuthority:
    """Signed tool facts consumed by every MCP authority evaluator."""

    tool_name: str
    input_schema_sha256: str
    capability_tags: tuple[str, ...]
    risk_class: RiskClass
    effect: str
    requires_revision: bool
    provider_object_id_pointer: str | None = None
    revision_map_required: bool = False

    def __post_init__(self) -> None:
        error = "invalid_tool_authority"
        object.__setattr__(self, "tool_name", _stable_string(self.tool_name, error=error))
        object.__setattr__(
            self,
            "input_schema_sha256",
            _digest(self.input_schema_sha256, error=error),
        )
        object.__setattr__(
            self,
            "capability_tags",
            _stable_tuple(self.capability_tags, error=error),
        )
        if type(self.risk_class) is not RiskClass:
            raise ValueError(error)
        object.__setattr__(self, "effect", _stable_string(self.effect, error=error))
        if type(self.requires_revision) is not bool:
            raise ValueError(error)
        if type(self.revision_map_required) is not bool:
            raise ValueError(error)
        if self.provider_object_id_pointer is not None:
            pointer = _stable_string(self.provider_object_id_pointer, error=error)
            if not pointer.startswith("/"):
                raise ValueError(error)
            object.__setattr__(self, "provider_object_id_pointer", pointer)


@dataclass(frozen=True, slots=True)
class McpAuthorityDecision:
    allowed: bool
    failed_check: str | None

    def __post_init__(self) -> None:
        if type(self.allowed) is not bool or self.allowed == (self.failed_check is not None):
            raise ValueError("invalid_authority_decision")
        if self.failed_check is not None:
            object.__setattr__(
                self,
                "failed_check",
                _stable_string(self.failed_check, error="invalid_authority_decision"),
            )


@dataclass(frozen=True)
class McpOperationProposal:
    request_id: str
    episode_id: str
    server_id: str
    server_version: str
    artifact_sha256: str
    provider_id: str
    account_ref: str
    connection_id: str
    connection_revision: str
    tool_name: str
    tool_schema_sha256: str
    arguments: Mapping[str, Any]
    source_constraints: Mapping[str, Any]
    destination_constraints: Mapping[str, Any]
    expected_provider_revisions: Mapping[str, str]
    authority_kind: str
    authority_id: str
    authority_revision: str
    plan_digest: str
    candidate_id: str
    plan_step_id: str
    connection_authority_revision: str
    idempotency_key: str
    evidence_memory_ids: tuple[str, ...]
    proposal_digest: str

    def __post_init__(self) -> None:
        error = "invalid_operation_proposal"
        for field_name in (
            "request_id",
            "episode_id",
            "server_id",
            "server_version",
            "provider_id",
            "account_ref",
            "connection_id",
            "tool_name",
            "authority_id",
            "plan_step_id",
            "idempotency_key",
        ):
            object.__setattr__(
                self, field_name, _stable_string(getattr(self, field_name), error=error)
            )
        for field_name in (
            "artifact_sha256",
            "connection_revision",
            "tool_schema_sha256",
            "authority_revision",
            "plan_digest",
            "candidate_id",
            "connection_authority_revision",
            "proposal_digest",
        ):
            object.__setattr__(
                self, field_name, _digest(getattr(self, field_name), error=error)
            )
        if self.authority_kind not in _AUTHORITY_KINDS:
            raise ValueError(error)
        object.__setattr__(
            self,
            "arguments",
            _json_object(self.arguments, reject_secrets=True, error=error),
        )
        object.__setattr__(
            self,
            "source_constraints",
            _json_object(self.source_constraints, reject_secrets=True, error=error),
        )
        object.__setattr__(
            self,
            "destination_constraints",
            _json_object(self.destination_constraints, reject_secrets=True, error=error),
        )
        object.__setattr__(
            self,
            "expected_provider_revisions",
            _provider_revisions(self.expected_provider_revisions),
        )
        object.__setattr__(
            self,
            "evidence_memory_ids",
            _stable_tuple(self.evidence_memory_ids, error=error),
        )
        if self.compute_digest() != self.proposal_digest:
            raise ValueError(error)

    @classmethod
    def from_dict(cls, raw: Any) -> McpOperationProposal:
        if type(raw) is not dict or any(type(key) is not str for key in raw):
            raise ValueError("invalid_operation_proposal")
        if set(raw) != _PROPOSAL_FIELDS:
            raise ValueError("invalid_operation_proposal")
        try:
            _owned_json(raw, reject_secrets=True, error="invalid_operation_proposal")
            return cls(**raw)
        except ValueError as exc:
            if str(exc) == "secret_bearing_operation_field":
                raise
            raise ValueError("invalid_operation_proposal") from None
        except (TypeError, OverflowError, RecursionError, UnicodeError):
            raise ValueError("invalid_operation_proposal") from None

    def unsigned_dict(self) -> dict[str, object]:
        document = self.to_dict()
        del document["proposal_digest"]
        return document

    def compute_digest(self) -> str:
        return sha256_hex(canonical_bytes(self.unsigned_dict()))

    def arguments_dict(self) -> dict[str, Any]:
        return _thaw_json(self.arguments)

    def source_constraints_dict(self) -> dict[str, Any]:
        return _thaw_json(self.source_constraints)

    def destination_constraints_dict(self) -> dict[str, Any]:
        return _thaw_json(self.destination_constraints)

    def expected_provider_revisions_dict(self) -> dict[str, str]:
        return dict(self.expected_provider_revisions)

    def to_dict(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "episode_id": self.episode_id,
            "server_id": self.server_id,
            "server_version": self.server_version,
            "artifact_sha256": self.artifact_sha256,
            "provider_id": self.provider_id,
            "account_ref": self.account_ref,
            "connection_id": self.connection_id,
            "connection_revision": self.connection_revision,
            "tool_name": self.tool_name,
            "tool_schema_sha256": self.tool_schema_sha256,
            "arguments": self.arguments_dict(),
            "source_constraints": self.source_constraints_dict(),
            "destination_constraints": self.destination_constraints_dict(),
            "expected_provider_revisions": self.expected_provider_revisions_dict(),
            "authority_kind": self.authority_kind,
            "authority_id": self.authority_id,
            "authority_revision": self.authority_revision,
            "plan_digest": self.plan_digest,
            "candidate_id": self.candidate_id,
            "plan_step_id": self.plan_step_id,
            "connection_authority_revision": self.connection_authority_revision,
            "idempotency_key": self.idempotency_key,
            "evidence_memory_ids": list(self.evidence_memory_ids),
            "proposal_digest": self.proposal_digest,
        }


@dataclass(frozen=True)
class McpAdmissionSummary:
    source_ledger_receipt: str
    guardian_receipt: str
    memory_ids: tuple[str, ...]
    admitted_count: int
    deduplicated: bool

    def __post_init__(self) -> None:
        if (
            type(self.source_ledger_receipt) is not str
            or _SOURCE_RECEIPT_RE.fullmatch(self.source_ledger_receipt) is None
        ):
            raise ValueError("invalid_admission_summary")
        object.__setattr__(
            self,
            "guardian_receipt",
            _digest(self.guardian_receipt, error="invalid_admission_summary"),
        )
        if (
            type(self.memory_ids) is not tuple
            or not self.memory_ids
            or any(
                type(item) is not str or _MEMORY_ID_RE.fullmatch(item) is None
                for item in self.memory_ids
            )
            or type(self.admitted_count) is not int
            or self.admitted_count != len(self.memory_ids)
            or type(self.deduplicated) is not bool
        ):
            raise ValueError("invalid_admission_summary")

    def to_dict(self) -> dict[str, object]:
        return {
            "source_ledger_receipt": self.source_ledger_receipt,
            "guardian_receipt": self.guardian_receipt,
            "memory_ids": list(self.memory_ids),
            "admitted_count": self.admitted_count,
            "deduplicated": self.deduplicated,
        }


@dataclass(frozen=True)
class McpDispatchOutcome:
    status: str
    action_id: str
    blocker: str | None
    failed_check: str | None
    server_id: str
    server_version: str
    artifact_sha256: str
    provider_id: str
    account_ref: str
    connection_id: str
    tool_name: str
    tool_schema_sha256: str
    provider_object_id: str
    provider_revision: str
    admission: McpAdmissionSummary | None
    deduplicated: bool
    uncertain: bool

    def __post_init__(self) -> None:
        error = "invalid_dispatch_outcome"
        if self.status not in _OUTCOME_STATUSES:
            raise ValueError(error)
        for field_name in (
            "action_id",
            "server_id",
            "server_version",
            "provider_id",
            "account_ref",
            "connection_id",
            "tool_name",
            "provider_object_id",
            "provider_revision",
        ):
            object.__setattr__(
                self,
                field_name,
                _stable_string(
                    getattr(self, field_name), error=error, optional=True, max_bytes=4096
                ),
            )
        for field_name in ("artifact_sha256", "tool_schema_sha256"):
            value = getattr(self, field_name)
            object.__setattr__(self, field_name, _digest(value, error=error) if value else "")
        for field_name in ("blocker", "failed_check"):
            value = getattr(self, field_name)
            if value is not None and (
                type(value) is not str or _BLOCKER_RE.fullmatch(value) is None
            ):
                raise ValueError(error)
        if self.admission is not None and type(self.admission) is not McpAdmissionSummary:
            raise ValueError(error)
        if any(type(value) is not bool for value in (self.deduplicated, self.uncertain)):
            raise ValueError(error)
        if self.uncertain != (self.status == "outcome_unknown"):
            raise ValueError(error)

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "action_id": self.action_id,
            "blocker": self.blocker,
            "failed_check": self.failed_check,
            "server_id": self.server_id,
            "server_version": self.server_version,
            "artifact_sha256": self.artifact_sha256,
            "provider_id": self.provider_id,
            "account_ref": self.account_ref,
            "connection_id": self.connection_id,
            "tool_name": self.tool_name,
            "tool_schema_sha256": self.tool_schema_sha256,
            "provider_object_id": self.provider_object_id,
            "provider_revision": self.provider_revision,
            "admission": self.admission.to_dict() if self.admission is not None else None,
            "deduplicated": self.deduplicated,
            "uncertain": self.uncertain,
        }


@dataclass(frozen=True)
class McpExternalDisableOutcome:
    drained_calls: int
    uncertain_writes: int
    failed_reads: int
    sessions_closed: int
    sessions_forced: int
    errors: int

    def __post_init__(self) -> None:
        if any(type(value) is not int or value < 0 for value in self.to_dict().values()):
            raise ValueError("invalid_external_disable_outcome")

    def to_dict(self) -> dict[str, int]:
        return {
            "drained_calls": self.drained_calls,
            "uncertain_writes": self.uncertain_writes,
            "failed_reads": self.failed_reads,
            "sessions_closed": self.sessions_closed,
            "sessions_forced": self.sessions_forced,
            "errors": self.errors,
        }
