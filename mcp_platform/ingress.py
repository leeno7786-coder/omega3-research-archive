"""One ordered admission gate for verified external MCP results."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import re
from threading import RLock
from typing import Any, Mapping
import weakref

from guardian.external_ingress import (
    QUARANTINE_MARKER,
    harden,
    receipt_digest,
    screen_receipt,
)
from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.source_contracts import (
    McpAdmissionReceipt,
    McpSourceContext,
    McpSourceEnvelope,
    McpStructuredResult,
    deterministic_mcp_memory_ids,
)
from memory.api import CanonicalEnvelope
from memory.source_ledger.store import record_source_bytes
from pressure_loop.contributors import Contributor, make_contributor
from pressure_loop.cognition_queue import enqueue_input

MCP_RESULT_COORD = [4, 1, 0]
_LOWER_HEX = frozenset("0123456789abcdef")
_MEMORY_ID_RE = re.compile(r"mcp-[0-9a-f]{32}\Z")
_SOURCE_LEDGER_RECEIPT_RE = re.compile(r"src_[0-9a-f]{24}\Z")
_EXTERNAL_ENTRIES = "__omega_mcp_entries__"
_EXTERNAL_KEY = "__omega_mcp_key__"
_EXTERNAL_VALUE = "__omega_mcp_value__"
_MAX_GUARDIAN_SCREENED_FIELDS = 40_000
_MAX_GUARDIAN_PATH_BYTES = 4_096
_GUARDIAN_PATH_RE = re.compile(
    r"__omega_mcp_entries__\[\d+\]\.__omega_mcp_(?:key|value)__"
    r"(?:\[\d+\])*"
    r"(?:\.__omega_mcp_entries__\[\d+\]\.__omega_mcp_(?:key|value)__"
    r"(?:\[\d+\])*)*\Z"
)
_GUARDIAN_SCREEN_RECEIPT = screen_receipt
_PLANNER_TEXT_LIMIT = 1_024
_PLANNER_SOURCE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_PLUGIN_SELECTOR_PREFIX_RE = re.compile(
    r"^/plugin:[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?(?:\s|$)",
    re.IGNORECASE,
)
_PLANNER_STATIC_UNSAFE_PATTERNS = (
    # Any URI carrying an explicit scheme, plus non-hierarchical active/data schemes.
    re.compile(r"(?i)\b[a-z][a-z0-9+.-]{1,31}:(?!\s)\S+"),
    # Windows drive/device/extended/UNC paths and POSIX absolute paths.
    re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/]"),
    re.compile(r"(?:\\){2,}(?:[?.](?:\\|/)|[^\\/\s]+(?:\\|/)+[^\\/\s]+)"),
    re.compile(r"(?<!:)//[^/\s]+/[^/\s]+"),
    re.compile(r"(?:^|[\s=\"'(])/(?![/?\s])[^\s\"'<>]+"),
    # Credential formats whose shape is independently unsafe.
    re.compile(r"(?i)\bauthorization\s*:\s*(?:basic|bearer)\s+\S+"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    # Executable files are never planner values, even when surrounded by prose.
    re.compile(r"(?i)\b[\w.-]+\.(?:exe|cmd|bat|com|ps1)\b"),
)
_PLANNER_CREDENTIAL_LABEL_RE = re.compile(
    r"(?ix)(?<![A-Za-z0-9])(?:"
    r"bearer|"
    r"(?:access|refresh|auth(?:orization)?)[\s_-]*token|"
    r"client[\s_-]*secret|"
    r"api[\s_-]*(?:key|token)|"
    r"password|passphrase|"
    r"oauth(?:2)?[\s_-]*(?:authorization[\s_-]*)?code|"
    r"credentials?|token|secret(?:[\s_-]*key)?|private[\s_-]*key"
    r")(?![A-Za-z0-9])"
)
_PLANNER_CREDENTIAL_CONTEXT_WORDS = frozenset(
    {
        "architecture",
        "best",
        "breach",
        "design",
        "document",
        "documents",
        "field",
        "fields",
        "flow",
        "flows",
        "format",
        "formats",
        "guidance",
        "handling",
        "lifecycle",
        "management",
        "name",
        "names",
        "naming",
        "of",
        "overview",
        "pattern",
        "patterns",
        "policies",
        "policy",
        "practices",
        "requirements",
        "reset",
        "rotation",
        "schema",
        "schemas",
        "security",
        "storage",
        "strategy",
        "usage",
    }
)
_PLANNER_SHELL_INVOCATION_RE = re.compile(
    r"(?ix)\b(?:"
    r"(?:sh|bash|zsh|fish)\s+-c|"
    r"cmd(?:\.exe)?\s+/[ck]|"
    r"(?:powershell|pwsh)(?:\.exe)?\s+-(?:command|encodedcommand|file)|"
    r"(?:python|python3|node)(?:\.exe)?\s+-(?:c|e)|"
    r"(?:wscript|cscript)(?:\.exe)?\s+\S+"
    r")\b"
)
_PLANNER_COMMON_COMMAND_RE = re.compile(
    r"(?ix)\b(?:"
    r"git\s+(?:add|branch|checkout|clean|clone|commit|diff|fetch|init|log|merge|"
    r"pull|push|rebase|remote|reset|restore|show|status|switch)|"
    r"(?:curl|wget|rm|rmdir|del|erase|format|chmod|chown|scp|ssh|nc|netcat)\s+\S+|"
    r"(?:whoami|uname)(?:\s+\S)?"
    r")\b"
)
_PLANNER_IMPERATIVE_ECHO_RE = re.compile(
    r"(?ix)(?:^\s*echo\s+\S|"
    r"\b(?:please|then|next|run|execute|launch|spawn|use|invoke)\b"
    r"(?:[\s,:]+[A-Za-z]+){0,3}[\s,:]+echo\s+\S|"
    r"[\"']command[\"']\s*[:=]\s*[\"']echo\s+\S)"
)
_PLANNER_EXECUTION_SYNTAX_RE = re.compile(
    r"&&|\|\||(?<!\|)\|(?!\|)|\$\(|\$\{|`|;\s*\S|"
    r"(?:^|\s)[<>]{1,2}(?=\s*(?![+-]?\d+(?:\.\d+)?(?:\s|$))\S)"
)
_PLANNER_TEXT_RECEIPTS: dict[
    int, tuple[weakref.ReferenceType[McpPlannerTextReceipt], str, str]
] = {}
_PLANNER_TEXT_RECEIPTS_LOCK = RLock()


class McpIngressError(RuntimeError):
    """A stable, content-free MCP result admission failure."""


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class McpPlannerTextReceipt:
    """Opaque proof that MCP ingress admitted one bounded planner text value."""

    value: str

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise ValueError("plugin_plan_screened_value_invalid")

    @classmethod
    def from_external(cls, value: object, *, source: str) -> McpPlannerTextReceipt:
        return screen_mcp_planner_text(value, source=source)

    def __copy__(self) -> McpPlannerTextReceipt:
        validate_mcp_planner_text(self)
        return self

    def __deepcopy__(self, _memo: dict[int, object]) -> McpPlannerTextReceipt:
        validate_mcp_planner_text(self)
        return self


def _planner_text_invalid() -> None:
    raise ValueError("plugin_plan_screened_value_invalid")


def _planner_text(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > _PLANNER_TEXT_LIMIT
        or value.strip() != value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        _planner_text_invalid()
    return value


def _planner_has_credential_material(raw: str) -> bool:
    """Detect a credential label paired with a value, independent of position."""
    for label in _PLANNER_CREDENTIAL_LABEL_RE.finditer(raw):
        cursor = label.end()
        while cursor < len(raw) and raw[cursor] in "\"'}]":
            cursor += 1
        whitespace_start = cursor
        while cursor < len(raw) and raw[cursor].isspace():
            cursor += 1
        separated_by_whitespace = cursor > whitespace_start
        assigned = cursor < len(raw) and raw[cursor] in ":="
        if assigned:
            cursor += 1
            if cursor < len(raw) and raw[cursor] == ">":
                cursor += 1
            while cursor < len(raw) and raw[cursor].isspace():
                cursor += 1
        elif not separated_by_whitespace:
            continue
        while cursor < len(raw) and raw[cursor] in "\"'{[":
            cursor += 1
        candidate = re.match(r"[^\s,;}\]\"']+", raw[cursor:])
        if candidate is None:
            continue
        first_value = candidate.group(0).casefold().strip(".?!:)")
        if not first_value:
            continue
        if not assigned and first_value in _PLANNER_CREDENTIAL_CONTEXT_WORDS:
            continue
        return True
    return False


def _planner_has_executable_material(raw: str) -> bool:
    """Detect command structure anywhere without rejecting nearby topical prose."""
    return any(
        pattern.search(raw) is not None
        for pattern in (
            _PLANNER_SHELL_INVOCATION_RE,
            _PLANNER_COMMON_COMMAND_RE,
            _PLANNER_IMPERATIVE_ECHO_RE,
            _PLANNER_EXECUTION_SYNTAX_RE,
        )
    )


def _planner_text_is_unsafe(raw: str) -> bool:
    # `/plugin:<id>` is Omega's explicit chat selector, not a URI scheme. Keep
    # the exemption narrow and scan the rest of the objective unchanged.
    static_scan = raw
    selector = _PLUGIN_SELECTOR_PREFIX_RE.match(raw)
    if selector is not None:
        static_scan = raw[selector.end() :].lstrip()
    return (
        any(pattern.search(static_scan) is not None for pattern in _PLANNER_STATIC_UNSAFE_PATTERNS)
        or _planner_has_credential_material(raw)
        or _planner_has_executable_material(raw)
    )


def _retire_planner_text_receipt(
    key: int, retired: weakref.ReferenceType[McpPlannerTextReceipt]
) -> None:
    with _PLANNER_TEXT_RECEIPTS_LOCK:
        current = _PLANNER_TEXT_RECEIPTS.get(key)
        if current is not None and current[0] is retired:
            _PLANNER_TEXT_RECEIPTS.pop(key, None)


def _planner_guardian_allows(raw: str, *, source: str) -> None:
    if screen_receipt is not _GUARDIAN_SCREEN_RECEIPT:
        _planner_text_invalid()
    try:
        screened = _GUARDIAN_SCREEN_RECEIPT({"value": raw}, source=source)
    except Exception:
        _planner_text_invalid()
    if type(screened) is not dict or set(screened) != {"value", "guardian"}:
        _planner_text_invalid()
    guardian = screened.get("guardian")
    if (
        type(guardian) is not dict
        or set(guardian) != {"screened", "quarantined", "screened_fields", "decisions"}
        or guardian["screened"] is not True
        or guardian["quarantined"] != 0
        or guardian["screened_fields"] != 1
        or type(guardian["decisions"]) is not dict
        or guardian["decisions"]
        or screened.get("value") != raw
        or screened.get("value") == QUARANTINE_MARKER
    ):
        _planner_text_invalid()
    try:
        canonical_bytes(screened)
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError):
        _planner_text_invalid()


def screen_mcp_planner_text(value: object, *, source: str) -> McpPlannerTextReceipt:
    """Screen external planner text and issue an identity-bound ingress receipt."""
    raw = _planner_text(value)
    if type(source) is not str or _PLANNER_SOURCE_RE.fullmatch(source) is None:
        _planner_text_invalid()
    _planner_guardian_allows(raw, source=source)
    if _planner_text_is_unsafe(raw):
        _planner_text_invalid()

    receipt = object.__new__(McpPlannerTextReceipt)
    object.__setattr__(receipt, "value", raw)
    key = id(receipt)
    fingerprint = sha256_hex(canonical_bytes({"source": source, "value": raw}))
    reference = weakref.ref(
        receipt,
        lambda retired, receipt_id=key: _retire_planner_text_receipt(receipt_id, retired),
    )
    with _PLANNER_TEXT_RECEIPTS_LOCK:
        if key in _PLANNER_TEXT_RECEIPTS:
            _planner_text_invalid()
        _PLANNER_TEXT_RECEIPTS[key] = (reference, source, fingerprint)
    return receipt


def validate_mcp_planner_text(value: object) -> str:
    """Return the screened value only for the exact live ingress-issued receipt."""
    if type(value) is not McpPlannerTextReceipt:
        _planner_text_invalid()
    with _PLANNER_TEXT_RECEIPTS_LOCK:
        issued = _PLANNER_TEXT_RECEIPTS.get(id(value))
        if issued is None or issued[0]() is not value:
            _planner_text_invalid()
        source, fingerprint = issued[1], issued[2]
    try:
        raw = _planner_text(value.value)
        actual = sha256_hex(canonical_bytes({"source": source, "value": raw}))
    except (AttributeError, TypeError, ValueError):
        _planner_text_invalid()
    if actual != fingerprint:
        _planner_text_invalid()
    return raw


def _require_digest(value: Any, *, error: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in _LOWER_HEX for character in value)
    ):
        raise McpIngressError(error)
    return value


def _require_source_ledger_receipt(value: Any) -> str:
    if type(value) is not str or _SOURCE_LEDGER_RECEIPT_RE.fullmatch(value) is None:
        raise McpIngressError("source_ledger_invalid")
    return value


def _copy_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _copy_json(item) for key, item in value.items()}
    if type(value) in {list, tuple}:
        return [_copy_json(item) for item in value]
    return value


def _encode_external_mappings(value: Any) -> Any:
    """Move every provider mapping key into a screened positional value."""
    if isinstance(value, Mapping):
        entries = [
            {
                _EXTERNAL_KEY: key,
                _EXTERNAL_VALUE: _encode_external_mappings(item),
            }
            for key, item in sorted(value.items(), key=lambda pair: pair[0])
        ]
        return {_EXTERNAL_ENTRIES: entries}
    if type(value) in {list, tuple}:
        return [_encode_external_mappings(item) for item in value]
    return value


def _harden_external_strings(value: Any) -> Any:
    if type(value) is str:
        return harden(value)
    if isinstance(value, Mapping):
        return {str(key): _harden_external_strings(item) for key, item in value.items()}
    if type(value) in {list, tuple}:
        return [_harden_external_strings(item) for item in value]
    return value


def _external_text_lines(value: Any) -> list[str]:
    """Render external strings as visible fences without granting keys authority."""
    if type(value) is str:
        return [value]
    if isinstance(value, Mapping):
        lines: list[str] = []
        for item in value.values():
            lines.extend(_external_text_lines(item))
        return lines
    if type(value) in {list, tuple}:
        lines = []
        for item in value:
            lines.extend(_external_text_lines(item))
        return lines
    return [canonical_bytes(value).decode("utf-8")]


def _guardian_result(
    content: Mapping[str, Any], *, source_name: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        screened = screen_receipt(_copy_json(content), source=source_name)
    except Exception:
        raise McpIngressError("guardian_screen_failed") from None
    if type(screened) is not dict:
        raise McpIngressError("guardian_screen_invalid")
    guardian = screened.pop("guardian", None)
    if type(guardian) is not dict:
        raise McpIngressError("guardian_screen_invalid")
    required = {"screened", "quarantined", "screened_fields", "decisions"}
    if set(guardian) != required:
        raise McpIngressError("guardian_screen_invalid")
    if (
        guardian["screened"] is not True
        or type(guardian["quarantined"]) is not int
        or guardian["quarantined"] < 0
        or type(guardian["screened_fields"]) is not int
        or guardian["screened_fields"] < 0
        or type(guardian["decisions"]) is not dict
    ):
        raise McpIngressError("guardian_screen_invalid")
    decisions = guardian["decisions"]
    screened_fields = guardian["screened_fields"]
    quarantined = guardian["quarantined"]
    if (
        screened_fields > _MAX_GUARDIAN_SCREENED_FIELDS
        or quarantined > screened_fields
        or len(decisions) > screened_fields
    ):
        raise McpIngressError("guardian_screen_invalid")
    quarantine_decisions = 0
    for path, decision in decisions.items():
        if (
            type(path) is not str
            or len(path) > _MAX_GUARDIAN_PATH_BYTES
            or _GUARDIAN_PATH_RE.fullmatch(path) is None
            or type(decision) is not str
            or decision not in {"flag", "quarantine"}
        ):
            raise McpIngressError("guardian_screen_invalid")
        quarantine_decisions += int(decision == "quarantine")
    if quarantined != quarantine_decisions:
        raise McpIngressError("guardian_screen_invalid")
    try:
        canonical_bytes(guardian)
        canonical_bytes(screened)
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError):
        raise McpIngressError("guardian_screen_invalid") from None
    if not _same_json_shape(content, screened):
        raise McpIngressError("guardian_screen_invalid")
    original_strings = _string_leaf_map(content)
    screened_strings = _string_leaf_map(screened)
    if (
        set(screened_strings) != set(original_strings)
        or screened_fields != len(original_strings)
        or any(path not in original_strings for path in decisions)
    ):
        raise McpIngressError("guardian_screen_invalid")
    for path, original in original_strings.items():
        decision = decisions.get(path)
        normalized = original.strip()
        if decision == "quarantine" and normalized == QUARANTINE_MARKER:
            raise McpIngressError("guardian_screen_invalid")
        expected = QUARANTINE_MARKER if decision == "quarantine" else normalized
        if screened_strings[path] != expected:
            raise McpIngressError("guardian_screen_invalid")
    return screened, guardian


def _string_leaf_map(value: Any, path: str = "") -> dict[str, str]:
    if isinstance(value, Mapping):
        leaves: dict[str, str] = {}
        for key, item in value.items():
            child_path = f"{path}.{key}" if path else key
            leaves.update(_string_leaf_map(item, child_path))
            if len(leaves) > _MAX_GUARDIAN_SCREENED_FIELDS:
                raise McpIngressError("guardian_screen_invalid")
        return leaves
    if type(value) in {list, tuple}:
        leaves = {}
        for index, item in enumerate(value):
            leaves.update(_string_leaf_map(item, f"{path}[{index}]"))
            if len(leaves) > _MAX_GUARDIAN_SCREENED_FIELDS:
                raise McpIngressError("guardian_screen_invalid")
        return leaves
    if type(value) is str:
        if len(path) > _MAX_GUARDIAN_PATH_BYTES:
            raise McpIngressError("guardian_screen_invalid")
        return {path: value}
    return {}


def _same_json_shape(original: Any, screened: Any) -> bool:
    if isinstance(original, Mapping):
        if type(screened) is not dict or set(original) != set(screened):
            return False
        return all(_same_json_shape(value, screened[key]) for key, value in original.items())
    if type(original) in {list, tuple}:
        return (
            type(screened) is list
            and len(original) == len(screened)
            and all(
                _same_json_shape(left, right)
                for left, right in zip(original, screened, strict=True)
            )
        )
    if type(original) is str:
        return type(screened) is str and screened in {
            original.strip(),
            QUARANTINE_MARKER,
        }
    return type(screened) is type(original) and screened == original


def _idempotency_material(source: McpSourceEnvelope) -> list[str]:
    return [
        source.server_id,
        source.provider_id,
        source.account_ref,
        source.connection_revision,
        source.tool_name,
        source.tool_schema_sha256,
        source.provider_object_id,
        source.provider_revision,
        source.result_digest,
    ]


def build_mcp_queue_contributor(
    source: McpSourceEnvelope,
    *,
    guardian_receipt: str,
    guardian_block: Mapping[str, Any],
    screened_content: Mapping[str, Any],
    memory_ids: tuple[str, ...] | None = None,
) -> Contributor:
    """Build one content-fenced situational envelope for the external queue."""
    if type(source) is not McpSourceEnvelope:
        raise McpIngressError("source_envelope_invalid")
    ids = deterministic_mcp_memory_ids(source) if memory_ids is None else memory_ids
    if (
        type(ids) is not tuple
        or not ids
        or any(type(item) is not str or _MEMORY_ID_RE.fullmatch(item) is None for item in ids)
        or ids != deterministic_mcp_memory_ids(source, chunks=len(ids))
    ):
        raise McpIngressError("memory_ids_invalid")
    guardian_receipt = _require_digest(guardian_receipt, error="guardian_receipt_invalid")
    if type(guardian_block) is not dict:
        guardian_block = dict(guardian_block)
    if type(screened_content) is not dict:
        screened_content = dict(screened_content)

    source_document = source.to_dict()
    admitted_content = _copy_json(screened_content)
    external_text = "\n".join(_external_text_lines(admitted_content))
    if not external_text:
        external_text = "MCP external result metadata only."
    cycle_id = ids[0]
    memory_envelopes = [
        asdict(
            CanonicalEnvelope(
                cycle_id=memory_id,
                coord=list(MCP_RESULT_COORD),
                type="situational",
                payload={
                    "title": "MCP external result",
                    "summary": "Verified external MCP result admitted through Guardian",
                    "text": external_text,
                    "subtopic": "mcp_result",
                    "tags": ["mcp", "external", "situational"],
                    "mcp_source": source_document,
                    "screened_content": admitted_content,
                    "enrichment": {
                        "mcp_source": source_document,
                        "guardian_receipt": guardian_receipt,
                        "screened_content": admitted_content,
                    },
                },
                source="mcp_platform.ingress",
                confidence=1.0,
                provenance=[source.source_ledger_receipt, guardian_receipt],
                id=memory_id,
                timestamp_created=source.observed_at,
            )
        )
        for memory_id in ids
    ]
    idempotency = _idempotency_material(source)
    metadata = {
        "cycle_id": cycle_id,
        # Connector and workspace observations enter through Chat so Global A can
        # classify them inside the pressure-opened episode.
        "lane": "chat",
        "message": "MCP external result",
        "ingress_class": "external_observation",
        "directive_intent": "LEARN",
        "memory_envelopes": memory_envelopes,
        "mcp_ingress": {
            **source_document,
            "guardian_receipt": guardian_receipt,
            "guardian_quarantined": guardian_block["quarantined"],
            "guardian_screened_fields": guardian_block["screened_fields"],
            "idempotency_key": list(idempotency),
        },
        "external_completion": {
            "kind": "mcp_result",
            "session_id": source.omega_action_lineage_id,
            "idempotency_key": list(idempotency),
            "terminal_disposition": "ingested",
            "expected_memory_ids": list(ids),
        },
    }
    return make_contributor("external", "sensory_observation", 1.0, metadata)


def _clone_contributor(contributor: Contributor) -> Contributor:
    return make_contributor(
        contributor.source,
        contributor.type,
        contributor.weight,
        _copy_json(contributor.metadata),
    )


def admit_mcp_result(
    source_context: McpSourceContext,
    raw_result: Mapping[str, Any],
    *,
    source_root: Path | str | None = None,
) -> McpAdmissionReceipt:
    """Parse, ledger, screen, harden, envelope, and enqueue one MCP result."""
    if type(source_context) is not McpSourceContext:
        raise McpIngressError("source_context_invalid")
    result = McpStructuredResult.from_dict(raw_result)
    result_document = result.to_dict()
    try:
        raw_canonical = canonical_bytes(result_document)
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError):
        raise McpIngressError("structured_result_invalid") from None
    if result.mime_type not in source_context.allowed_mime_types:
        raise McpIngressError("mime_not_allowed")
    if len(raw_canonical) > source_context.max_result_bytes:
        raise McpIngressError("result_too_large")
    result_sha256 = sha256_hex(raw_canonical)

    source_metadata = {
        "created_at": source_context.call_timestamp,
        "source_session_id": result_sha256,
        "source_session_timestamp": source_context.call_timestamp,
        "result_sha256": result_sha256,
    }
    try:
        artifact = record_source_bytes(
            raw_canonical,
            source_type="mcp_result",
            origin_id=f"mcp:{result_sha256}",
            metadata=source_metadata,
            root=source_root,
        )
    except Exception:
        raise McpIngressError("source_ledger_failed") from None
    source_ledger_receipt = _require_source_ledger_receipt(getattr(artifact, "source_id", None))
    artifact_digest = _require_digest(
        getattr(artifact, "content_hash", None), error="source_ledger_invalid"
    )
    if artifact_digest != result_sha256:
        raise McpIngressError("source_ledger_digest_mismatch")

    source = source_context.bind_result(
        result,
        result_digest=result_sha256,
        source_ledger_receipt=source_ledger_receipt,
    )
    source_name = f"mcp:{source.server_id}:{source.tool_name}"
    try:
        encoded_content = _encode_external_mappings(result.content)
    except Exception:
        raise McpIngressError("external_content_encoding_failed") from None
    screened, guardian_block = _guardian_result(encoded_content, source_name=source_name)
    try:
        guardian_sha256 = receipt_digest(source.source_ledger_receipt, source_name, guardian_block)
    except Exception:
        raise McpIngressError("guardian_receipt_failed") from None
    guardian_sha256 = _require_digest(guardian_sha256, error="guardian_receipt_invalid")
    try:
        admitted_content = _harden_external_strings(screened)
    except Exception:
        raise McpIngressError("external_content_hardening_failed") from None
    contributor = build_mcp_queue_contributor(
        source,
        guardian_receipt=guardian_sha256,
        guardian_block=guardian_block,
        screened_content=admitted_content,
    )
    queue_contributor = _clone_contributor(contributor)
    try:
        enqueued = enqueue_input(queue_contributor, operator_activity=False)
    except Exception:
        raise McpIngressError("queue_admission_failed") from None
    if type(enqueued) is not bool:
        raise McpIngressError("queue_admission_invalid")
    return McpAdmissionReceipt(
        source_envelope=source,
        guardian_receipt=guardian_sha256,
        memory_ids=deterministic_mcp_memory_ids(source),
        enqueued=enqueued,
        deduplicated=not enqueued,
        contributor=_clone_contributor(contributor),
    )
