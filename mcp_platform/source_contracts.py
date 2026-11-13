"""Strict provider-result identities for the signed MCP ingress boundary."""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Mapping

from mcp_platform.canonical import canonical_bytes, sha256_hex
from pressure_loop.contributors import Contributor

_RESULT_FIELDS = frozenset(
    {
        "result_kind",
        "provider_object_id",
        "provider_container_id",
        "provider_revision",
        "provider_change_token",
        "event_kind",
        "observed_at",
        "status",
        "mime_type",
        "content",
    }
)
_SOURCE_CONTEXT_FIELDS = frozenset(
    {
        "server_id",
        "server_version",
        "artifact_sha256",
        "provider_id",
        "account_ref",
        "connection_revision",
        "tool_name",
        "tool_schema_sha256",
        "call_timestamp",
        "omega_action_lineage_id",
        "max_result_bytes",
        "allowed_mime_types",
    }
)
MCP_RESULT_KINDS = frozenset({"observation"})
MCP_EVENT_KINDS = frozenset({"current", "historical", "deleted"})
MCP_RESULT_STATUSES = frozenset({"current", "historical", "deleted"})
_HEX = frozenset("0123456789abcdef")
_MIME_RE = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,63}\Z")
_STRUCTURAL_ID_RE = re.compile(r"[A-Za-z0-9_@.:+/=,%~-]{1,512}\Z")
_MEMORY_ID_RE = re.compile(r"mcp-[0-9a-f]{32}\Z")
_SOURCE_LEDGER_RECEIPT_RE = re.compile(r"src_[0-9a-f]{24}\Z")
_UTC_TIMESTAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z"
)
_MAX_JSON_DEPTH = 64
_MAX_JSON_NODES = 20_000
_MAX_STABLE_STRING_BYTES = 1_048_576
_PROVIDER_OBJECT_ID_MAX_BYTES = 2_048
_PROVIDER_CONTAINER_ID_MAX_BYTES = 2_048
_PROVIDER_REVISION_MAX_BYTES = 4_096
_PROVIDER_CHANGE_TOKEN_MAX_BYTES = 8_192


class McpSourceContractError(ValueError):
    """A stable, content-free MCP source/result contract rejection."""


def _object(value: Any, *, error: str) -> dict[str, Any]:
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise McpSourceContractError(error)
    return value


def _exact_fields(
    document: dict[str, Any], expected: frozenset[str], *, label: str
) -> None:
    unknown = set(document) - expected
    missing = expected - set(document)
    if unknown:
        raise McpSourceContractError(f"{label} has unknown fields")
    if missing:
        raise McpSourceContractError(f"{label} has missing fields")


def _stable_string(value: Any, *, error: str, optional: bool = False) -> str:
    if type(value) is not str:
        raise McpSourceContractError(error)
    if optional and value == "":
        return value
    if not value or value.strip() != value:
        raise McpSourceContractError(error)
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        raise McpSourceContractError(error) from None
    if len(encoded) > _MAX_STABLE_STRING_BYTES:
        raise McpSourceContractError(error)
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise McpSourceContractError(error)
    return value


def _enum(value: Any, allowed: frozenset[str], *, error: str) -> str:
    parsed = _stable_string(value, error=error)
    if parsed not in allowed:
        raise McpSourceContractError(error)
    return parsed


def _structural_id(value: Any, *, error: str, optional: bool = False) -> str:
    parsed = _stable_string(value, error=error, optional=optional)
    if optional and parsed == "":
        return parsed
    if (
        _STRUCTURAL_ID_RE.fullmatch(parsed) is None
        or parsed.startswith("/")
        or ".." in parsed
        or re.match(r"[A-Za-z]:/", parsed) is not None
    ):
        raise McpSourceContractError(error)
    return parsed


def _provider_opaque_token(
    value: Any,
    *,
    error: str,
    max_bytes: int,
    optional: bool = False,
) -> str:
    if type(value) is not str or (not optional and not value):
        raise McpSourceContractError(error)
    if optional and value == "":
        return value
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        raise McpSourceContractError(error) from None
    if not encoded or len(encoded) > max_bytes:
        raise McpSourceContractError(error)
    if any(
        character.isspace()
        or unicodedata.category(character) in {"Cc", "Cf", "Cs"}
        for character in value
    ):
        raise McpSourceContractError(error)
    return value


def _digest(value: Any, *, error: str) -> str:
    parsed = _stable_string(value, error=error)
    if len(parsed) != 64 or any(character not in _HEX for character in parsed):
        raise McpSourceContractError(error)
    return parsed


def _source_ledger_receipt(value: Any, *, error: str) -> str:
    if type(value) is not str or _SOURCE_LEDGER_RECEIPT_RE.fullmatch(value) is None:
        raise McpSourceContractError(error)
    return value


def _utc_timestamp(value: Any, *, error: str) -> str:
    parsed = _stable_string(value, error=error)
    if _UTC_TIMESTAMP_RE.fullmatch(parsed) is None:
        raise McpSourceContractError(error)
    try:
        datetime.fromisoformat(parsed[:-1] + "+00:00")
    except ValueError:
        raise McpSourceContractError(error) from None
    return parsed


def _mime_type(value: Any, *, error: str) -> str:
    parsed = _stable_string(value, error=error)
    if _MIME_RE.fullmatch(parsed) is None:
        raise McpSourceContractError(error)
    return parsed


def _copy_json(value: Any) -> Any:
    active: set[int] = set()
    nodes = 0

    def _walk(item: Any, depth: int) -> Any:
        nonlocal nodes
        nodes += 1
        if depth > _MAX_JSON_DEPTH or nodes > _MAX_JSON_NODES:
            raise McpSourceContractError("structured result content is too complex")
        if item is None or type(item) in {bool, int}:
            return item
        if type(item) is str:
            if len(item.encode("utf-8")) > _MAX_STABLE_STRING_BYTES:
                raise McpSourceContractError("structured result content is too large")
            return item
        if type(item) is float:
            if not math.isfinite(item):
                raise McpSourceContractError("structured result content is invalid")
            return item
        if type(item) not in {dict, list}:
            raise McpSourceContractError("structured result content is invalid")
        identity = id(item)
        if identity in active:
            raise McpSourceContractError("structured result content is cyclic")
        active.add(identity)
        try:
            if type(item) is list:
                return tuple(_walk(child, depth + 1) for child in item)
            if any(type(key) is not str for key in item):
                raise McpSourceContractError("structured result content is invalid")
            return MappingProxyType(
                {key: _walk(child, depth + 1) for key, child in item.items()}
            )
        finally:
            active.remove(identity)

    return _walk(value, 0)


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_thaw_json(item) for item in value]
    return value


@dataclass(frozen=True)
class McpStructuredResult:
    result_kind: str
    provider_object_id: str
    provider_container_id: str
    provider_revision: str
    provider_change_token: str
    event_kind: str
    observed_at: str
    status: str
    mime_type: str
    content: Mapping[str, Any]

    @classmethod
    def from_dict(cls, raw: Any) -> McpStructuredResult:
        document = _object(raw, error="structured result must be an object")
        _exact_fields(document, _RESULT_FIELDS, label="structured result")
        content = _object(
            document["content"], error="structured result content must be an object"
        )
        try:
            copied_content = _copy_json(content)
        except McpSourceContractError:
            raise
        except (OverflowError, RecursionError, UnicodeError):
            raise McpSourceContractError("structured result content is invalid") from None
        assert isinstance(copied_content, Mapping)
        return cls(
            result_kind=_enum(
                document["result_kind"],
                MCP_RESULT_KINDS,
                error="structured result result_kind is invalid",
            ),
            provider_object_id=_provider_opaque_token(
                document["provider_object_id"],
                error="structured result provider_object_id is invalid",
                max_bytes=_PROVIDER_OBJECT_ID_MAX_BYTES,
            ),
            provider_container_id=_provider_opaque_token(
                document["provider_container_id"],
                error="structured result provider_container_id is invalid",
                max_bytes=_PROVIDER_CONTAINER_ID_MAX_BYTES,
                optional=True,
            ),
            provider_revision=_provider_opaque_token(
                document["provider_revision"],
                error="structured result provider_revision is invalid",
                max_bytes=_PROVIDER_REVISION_MAX_BYTES,
                optional=True,
            ),
            provider_change_token=_provider_opaque_token(
                document["provider_change_token"],
                error="structured result provider_change_token is invalid",
                max_bytes=_PROVIDER_CHANGE_TOKEN_MAX_BYTES,
                optional=True,
            ),
            event_kind=_enum(
                document["event_kind"],
                MCP_EVENT_KINDS,
                error="structured result event_kind is invalid",
            ),
            observed_at=_utc_timestamp(
                document["observed_at"],
                error="structured result observed_at is invalid",
            ),
            status=_enum(
                document["status"],
                MCP_RESULT_STATUSES,
                error="structured result status is invalid",
            ),
            mime_type=_mime_type(
                document["mime_type"], error="structured result mime_type is invalid"
            ),
            content=copied_content,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "result_kind": self.result_kind,
            "provider_object_id": self.provider_object_id,
            "provider_container_id": self.provider_container_id,
            "provider_revision": self.provider_revision,
            "provider_change_token": self.provider_change_token,
            "event_kind": self.event_kind,
            "observed_at": self.observed_at,
            "status": self.status,
            "mime_type": self.mime_type,
            "content": _thaw_json(self.content),
        }


@dataclass(frozen=True)
class McpSourceContext:
    server_id: str
    server_version: str
    artifact_sha256: str
    provider_id: str
    account_ref: str
    connection_revision: str
    tool_name: str
    tool_schema_sha256: str
    call_timestamp: str
    omega_action_lineage_id: str
    max_result_bytes: int
    allowed_mime_types: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "server_id",
            _structural_id(self.server_id, error="source server_id is invalid"),
        )
        object.__setattr__(
            self,
            "server_version",
            _structural_id(self.server_version, error="source server_version is invalid"),
        )
        object.__setattr__(
            self,
            "artifact_sha256",
            _digest(self.artifact_sha256, error="source artifact_sha256 is invalid"),
        )
        object.__setattr__(
            self,
            "provider_id",
            _structural_id(self.provider_id, error="source provider_id is invalid"),
        )
        object.__setattr__(
            self,
            "account_ref",
            _structural_id(self.account_ref, error="source account_ref is invalid"),
        )
        object.__setattr__(
            self,
            "connection_revision",
            _digest(
                self.connection_revision, error="source connection_revision is invalid"
            ),
        )
        object.__setattr__(
            self,
            "tool_name",
            _structural_id(self.tool_name, error="source tool_name is invalid"),
        )
        object.__setattr__(
            self,
            "tool_schema_sha256",
            _digest(
                self.tool_schema_sha256, error="source tool_schema_sha256 is invalid"
            ),
        )
        object.__setattr__(
            self,
            "call_timestamp",
            _utc_timestamp(self.call_timestamp, error="source call_timestamp is invalid"),
        )
        object.__setattr__(
            self,
            "omega_action_lineage_id",
            _structural_id(
                self.omega_action_lineage_id,
                error="source omega_action_lineage_id is invalid",
                optional=True,
            ),
        )
        if type(self.max_result_bytes) is not int or self.max_result_bytes <= 0:
            raise McpSourceContractError("source max_result_bytes is invalid")
        if type(self.allowed_mime_types) is not tuple or not self.allowed_mime_types:
            raise McpSourceContractError("source allowed_mime_types is invalid")
        allowed = tuple(
            _mime_type(item, error="source allowed_mime_types is invalid")
            for item in self.allowed_mime_types
        )
        if len(allowed) != len(set(allowed)):
            raise McpSourceContractError("source allowed_mime_types is invalid")
        object.__setattr__(self, "allowed_mime_types", allowed)

    @classmethod
    def from_dict(cls, raw: Any) -> McpSourceContext:
        document = _object(raw, error="source context must be an object")
        _exact_fields(document, _SOURCE_CONTEXT_FIELDS, label="source context")
        values = dict(document)
        allowed = values["allowed_mime_types"]
        if type(allowed) is not list:
            raise McpSourceContractError("source allowed_mime_types is invalid")
        values["allowed_mime_types"] = tuple(allowed)
        return cls(**values)

    def bind_result(
        self,
        result: McpStructuredResult,
        *,
        result_digest: str,
        source_ledger_receipt: str,
    ) -> McpSourceEnvelope:
        if type(result) is not McpStructuredResult:
            raise McpSourceContractError("source result is invalid")
        return McpSourceEnvelope(
            server_id=self.server_id,
            server_version=self.server_version,
            artifact_sha256=self.artifact_sha256,
            provider_id=self.provider_id,
            account_ref=self.account_ref,
            connection_revision=self.connection_revision,
            tool_name=self.tool_name,
            tool_schema_sha256=self.tool_schema_sha256,
            provider_object_id=result.provider_object_id,
            provider_container_id=result.provider_container_id,
            provider_revision=result.provider_revision,
            provider_change_token=result.provider_change_token,
            result_kind=result.result_kind,
            event_kind=result.event_kind,
            observed_at=result.observed_at,
            call_timestamp=self.call_timestamp,
            result_digest=result_digest,
            source_ledger_receipt=source_ledger_receipt,
            status=result.status,
            mime_type=result.mime_type,
            omega_action_lineage_id=self.omega_action_lineage_id,
        )


@dataclass(frozen=True)
class McpSourceEnvelope:
    server_id: str
    server_version: str
    artifact_sha256: str
    provider_id: str
    account_ref: str
    connection_revision: str
    tool_name: str
    tool_schema_sha256: str
    provider_object_id: str
    provider_container_id: str
    provider_revision: str
    provider_change_token: str
    result_kind: str
    event_kind: str
    observed_at: str
    call_timestamp: str
    result_digest: str
    source_ledger_receipt: str
    status: str
    mime_type: str
    omega_action_lineage_id: str

    def __post_init__(self) -> None:
        for field_name in (
            "server_id",
            "server_version",
            "provider_id",
            "account_ref",
            "tool_name",
        ):
            object.__setattr__(
                self,
                field_name,
                _structural_id(
                    getattr(self, field_name), error="MCP source envelope identity is invalid"
                ),
            )
        object.__setattr__(
            self,
            "source_ledger_receipt",
            _source_ledger_receipt(
                self.source_ledger_receipt,
                error="MCP source envelope identity is invalid",
            ),
        )
        object.__setattr__(
            self,
            "provider_object_id",
            _provider_opaque_token(
                self.provider_object_id,
                error="MCP source envelope identity is invalid",
                max_bytes=_PROVIDER_OBJECT_ID_MAX_BYTES,
            ),
        )
        for field_name, max_bytes in (
            ("provider_container_id", _PROVIDER_CONTAINER_ID_MAX_BYTES),
            ("provider_revision", _PROVIDER_REVISION_MAX_BYTES),
            ("provider_change_token", _PROVIDER_CHANGE_TOKEN_MAX_BYTES),
        ):
            object.__setattr__(
                self,
                field_name,
                _provider_opaque_token(
                    getattr(self, field_name),
                    error="MCP source envelope identity is invalid",
                    max_bytes=max_bytes,
                    optional=True,
                ),
            )
        object.__setattr__(
            self,
            "omega_action_lineage_id",
            _structural_id(
                self.omega_action_lineage_id,
                error="MCP source envelope identity is invalid",
                optional=True,
            ),
        )
        for field_name in (
            "artifact_sha256",
            "connection_revision",
            "tool_schema_sha256",
            "result_digest",
        ):
            object.__setattr__(
                self,
                field_name,
                _digest(
                    getattr(self, field_name), error="MCP source envelope digest is invalid"
                ),
            )
        object.__setattr__(
            self,
            "result_kind",
            _enum(
                self.result_kind,
                MCP_RESULT_KINDS,
                error="MCP source envelope result_kind is invalid",
            ),
        )
        object.__setattr__(
            self,
            "event_kind",
            _enum(
                self.event_kind,
                MCP_EVENT_KINDS,
                error="MCP source envelope event_kind is invalid",
            ),
        )
        object.__setattr__(
            self,
            "status",
            _enum(
                self.status,
                MCP_RESULT_STATUSES,
                error="MCP source envelope status is invalid",
            ),
        )
        object.__setattr__(
            self,
            "observed_at",
            _utc_timestamp(
                self.observed_at, error="MCP source envelope observed_at is invalid"
            ),
        )
        object.__setattr__(
            self,
            "call_timestamp",
            _utc_timestamp(
                self.call_timestamp, error="MCP source envelope call_timestamp is invalid"
            ),
        )
        object.__setattr__(
            self,
            "mime_type",
            _mime_type(self.mime_type, error="MCP source envelope mime_type is invalid"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "server_id": self.server_id,
            "server_version": self.server_version,
            "artifact_sha256": self.artifact_sha256,
            "provider_id": self.provider_id,
            "account_ref": self.account_ref,
            "connection_revision": self.connection_revision,
            "tool_name": self.tool_name,
            "tool_schema_sha256": self.tool_schema_sha256,
            "provider_object_id": self.provider_object_id,
            "provider_container_id": self.provider_container_id,
            "provider_revision": self.provider_revision,
            "provider_change_token": self.provider_change_token,
            "result_kind": self.result_kind,
            "event_kind": self.event_kind,
            "observed_at": self.observed_at,
            "call_timestamp": self.call_timestamp,
            "result_digest": self.result_digest,
            "source_ledger_receipt": self.source_ledger_receipt,
            "status": self.status,
            "mime_type": self.mime_type,
            "omega_action_lineage_id": self.omega_action_lineage_id,
        }


@dataclass(frozen=True)
class McpAdmissionReceipt:
    source_envelope: McpSourceEnvelope
    guardian_receipt: str
    memory_ids: tuple[str, ...]
    enqueued: bool
    deduplicated: bool
    contributor: Contributor

    def __post_init__(self) -> None:
        if type(self.source_envelope) is not McpSourceEnvelope:
            raise McpSourceContractError("admission source envelope is invalid")
        object.__setattr__(
            self,
            "guardian_receipt",
            _digest(self.guardian_receipt, error="admission guardian receipt is invalid"),
        )
        if type(self.memory_ids) is not tuple or not self.memory_ids:
            raise McpSourceContractError("admission memory IDs are invalid")
        if any(
            type(item) is not str or _MEMORY_ID_RE.fullmatch(item) is None
            for item in self.memory_ids
        ):
            raise McpSourceContractError("admission memory IDs are invalid")
        expected_ids = deterministic_mcp_memory_ids(
            self.source_envelope, chunks=len(self.memory_ids)
        )
        if self.memory_ids != expected_ids:
            raise McpSourceContractError("admission memory IDs are invalid")
        if type(self.enqueued) is not bool or type(self.deduplicated) is not bool:
            raise McpSourceContractError("admission queue state is invalid")
        if self.enqueued == self.deduplicated:
            raise McpSourceContractError("admission queue state is invalid")
        if type(self.contributor) is not Contributor:
            raise McpSourceContractError("admission contributor is invalid")
        metadata = self.contributor.metadata
        try:
            envelopes = metadata["memory_envelopes"]
            completion = metadata["external_completion"]
            if (
                type(metadata) is not dict
                or type(envelopes) is not list
                or len(envelopes) != len(self.memory_ids)
                or any(type(envelope) is not dict for envelope in envelopes)
                or tuple(envelope.get("id") for envelope in envelopes)
                != self.memory_ids
                or tuple(envelope.get("cycle_id") for envelope in envelopes)
                != self.memory_ids
                or metadata.get("cycle_id") != self.memory_ids[0]
                or type(completion) is not dict
                or completion.get("expected_memory_ids") != list(self.memory_ids)
            ):
                raise McpSourceContractError(
                    "admission contributor identity is invalid"
                )
        except (KeyError, TypeError):
            raise McpSourceContractError(
                "admission contributor identity is invalid"
            ) from None

    @property
    def source_ledger_receipt(self) -> str:
        return self.source_envelope.source_ledger_receipt

    @property
    def result_digest(self) -> str:
        return self.source_envelope.result_digest

    @property
    def server_id(self) -> str:
        return self.source_envelope.server_id

    @property
    def provider_id(self) -> str:
        return self.source_envelope.provider_id

    @property
    def provider_object_id(self) -> str:
        return self.source_envelope.provider_object_id

    @property
    def provider_revision(self) -> str:
        return self.source_envelope.provider_revision


def deterministic_mcp_memory_ids(
    source: McpSourceEnvelope, *, chunks: int = 1
) -> tuple[str, ...]:
    """Hash the approved RFC 8785 source tuple with one exact chunk index."""
    if type(source) is not McpSourceEnvelope:
        raise McpSourceContractError("MCP source envelope is required")
    if type(chunks) is not int or chunks < 1:
        raise McpSourceContractError("chunks must be a positive integer")
    identity = (
        source.server_id,
        source.provider_id,
        source.account_ref,
        source.connection_revision,
        source.tool_name,
        source.tool_schema_sha256,
        source.provider_object_id,
        source.provider_revision,
        source.result_digest,
    )
    return tuple(
        f"mcp-{sha256_hex(canonical_bytes([*identity, chunk_index]))[:32]}"
        for chunk_index in range(chunks)
    )
