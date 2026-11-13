"""Closed, local planning over already-signed plugin bindings; never dispatches."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from jsonschema import Draft202012Validator, SchemaError, ValidationError

from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.ingress import (
    McpPlannerTextReceipt,
    screen_mcp_planner_text,
    validate_mcp_planner_text,
)
from mcp_platform.plugin_catalog import PluginToolBinding
from mcp_platform.package_contracts import RiskClass

_MAX_CANDIDATES, _MAX_STEPS, _MAX_ITEMS, _MAX_DEPTH = 12, 4, 32, 8
_MAX_TEXT, _MAX_BYTES = 1024, 8192
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_WORD = re.compile(r"[A-Za-z0-9]+")
_TAINTED_ARGUMENT_KEY = re.compile(
    r"(?i)(?:url|uri|path|file|directory|credential|token|secret|password|"
    r"authorization|api[_ -]?(?:key|token)|command|executable)"
)
_TEXT_ARGUMENT_FIELDS = frozenset(
    {"query", "title", "summary", "description", "text", "body", "name"}
)
_DATE_ARGUMENT_FIELDS = frozenset({"date", "start", "end", "when", "time"})
# Annotation keywords describe an argument rather than constrain it. Every real
# MCP tool schema annotates its properties -- a planner cannot fill an argument
# it has no description for -- so refusing them made every shipped plugin tool
# unplannable and the chat route to a plugin could never fire. They are
# admitted, individually bounded below, and never read as constraints.
_ANNOTATION_KEYS = frozenset(
    {"description", "title", "format", "default", "examples", "deprecated", "readOnly", "writeOnly"}
)
# Vendor extension keywords (the "x-" namespace JSON Schema reserves for them)
# carry no validation semantics and every validator ignores them. They are
# dropped rather than refused, so one provider annotation cannot make an
# otherwise ordinary tool unusable.
_EXTENSION_PREFIX = "x-"
# Keywords a provider writes that the contract has no use for. They are dropped
# for the same reason as the "x-" namespace -- one unanswerable keyword must not
# make an otherwise ordinary tool uncallable -- but each for its own reason:
# "$schema"/"$id"/"$comment" declare a dialect or carry prose; "nullable" is the
# OpenAPI spelling of a type union, and dropping it only narrows what the
# planner may emit; "contentEncoding"/"contentMediaType" annotate a payload.
# "pattern" and "propertyNames" are dropped deliberately rather than admitted:
# the normalized document reaches a jsonschema validator, and a provider-authored
# regex compiled there is a denial-of-service surface. The provider still
# enforces its own pattern -- this contract bounds what the PLANNER may emit.
_DROPPED_KEYS = frozenset(
    {
        "$schema",
        "$id",
        "$comment",
        "nullable",
        "pattern",
        "propertyNames",
        "contentEncoding",
        "contentMediaType",
    }
)
#: Both spellings of a local definition pointer. ``definitions`` is what draft-07
#: called ``$defs``, it means exactly the same thing, and providers emit whichever
#: their generator targets -- Notion and Zapier emit the older one.
_LOCAL_REF_PREFIXES = ("#/$defs/", "#/definitions/")
_DEFINITION_KEYS = ("$defs", "definitions")
_SCHEMA_TYPES = frozenset(
    {"object", "array", "string", "integer", "number", "boolean", "null"}
)
_MAX_TYPE_UNION = 4
# A description is prose for the planner, not a value it will echo, so it is
# bounded separately and more generously than an argument value. Gmail's
# search syntax alone runs past three thousand characters; the whole schema
# is still capped by _MAX_BYTES.
_MAX_ANNOTATION_TEXT = 4096
_CONSTRAINT_KEYS = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "anyOf",
        "oneOf",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minItems",
        "maxItems",
    }
)
_SCHEMA_KEYS = _CONSTRAINT_KEYS | _ANNOTATION_KEYS


class _FrozenDict(dict[str, object]):
    def __setitem__(self, key: str, value: object) -> None:
        raise TypeError("immutable mapping")

    def __delitem__(self, key: str) -> None:
        raise TypeError("immutable mapping")

    def update(self, *args: object, **kwargs: object) -> None:
        raise TypeError("immutable mapping")


class _FrozenList(list[object]):
    def __setitem__(self, key: object, value: object) -> None:
        raise TypeError("immutable sequence")

    def append(self, value: object) -> None:
        raise TypeError("immutable sequence")

    def extend(self, value: object) -> None:
        raise TypeError("immutable sequence")


def _frozen_schema(value: object) -> object:
    if isinstance(value, Mapping):
        return _FrozenDict({key: _frozen_schema(item) for key, item in value.items()})
    if type(value) is list:
        return _FrozenList(_frozen_schema(item) for item in value)
    return value


def _fail() -> None:
    raise ValueError("plugin_plan_invalid")


def _text(value: object, maximum: int = _MAX_TEXT) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > maximum
        or value.strip() != value
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        _fail()
    return value


def _id(value: object) -> str:
    value = _text(value, 128)
    if _ID.fullmatch(value) is None:
        _fail()
    return value


def _digest(value: object) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        _fail()
    return value


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_plain(item) for item in value]
    return value


def plain_arguments(arguments: Mapping[str, object]) -> dict[str, object]:
    """A plan step's frozen arguments as plain JSON data, all the way down.

    ``dict(step.arguments)`` thaws only the top level; a nested object stays a
    read-only mapping (and a list a tuple) that canonical JSON refuses.
    """
    thawed = _plain(arguments)
    assert type(thawed) is dict
    return thawed


def _freeze(value: object, depth: int = 0) -> object:
    if depth > _MAX_DEPTH:
        raise ValueError("plugin_plan_limit_exceeded")
    if isinstance(value, Mapping):
        if len(value) > _MAX_ITEMS or any(type(key) is not str for key in value):
            raise ValueError("plugin_plan_limit_exceeded")
        return MappingProxyType({key: _freeze(value[key], depth + 1) for key in sorted(value)})
    if type(value) in {list, tuple}:
        if len(value) > _MAX_ITEMS:
            raise ValueError("plugin_plan_limit_exceeded")
        return tuple(_freeze(item, depth + 1) for item in value)
    if type(value) is str:
        return _text(value, _MAX_TEXT)
    if value is None or type(value) in {bool, int, float}:
        return value
    _fail()


def _bindings(
    value: Sequence[PluginToolBinding], *, catalog: bool = False
) -> tuple[PluginToolBinding, ...]:
    if not isinstance(value, Sequence) or (not catalog and len(value) > _MAX_CANDIDATES):
        raise ValueError("plugin_plan_limit_exceeded")
    result = tuple(value)
    if any(type(item) is not PluginToolBinding for item in result) or len(
        {item.candidate_id for item in result}
    ) != len(result):
        _fail()
    return result


def _inline_refs(schema: object) -> object:
    """Flatten local ``$defs``/``$ref`` composition into one self-contained schema.

    Real tool schemas factor shared shapes into ``$defs`` and point at them with
    ``$ref`` -- Calendar's attendee, Gmail's attachment. The planning contract
    describes one flat document, so the references are resolved here rather than
    refused. Only local ``#/$defs/<name>`` pointers resolve; a remote pointer, an
    unknown name, or a cycle fails closed, and the result still faces every
    depth, size and keyword bound below.
    """
    if not isinstance(schema, Mapping):
        return schema
    definitions: dict[str, object] = {}
    for key in _DEFINITION_KEYS:
        block = schema.get(key)
        if isinstance(block, Mapping):
            definitions.update(block)

    def resolve(node: object, stack: tuple[str, ...], depth: int) -> object:
        if depth > _MAX_DEPTH * 2:
            _fail()
        if isinstance(node, Mapping):
            reference = node.get("$ref")
            if reference is not None:
                if type(reference) is not str:
                    _fail()
                prefix = next(
                    (item for item in _LOCAL_REF_PREFIXES if reference.startswith(item)),
                    None,
                )
                if prefix is None:
                    _fail()
                name = reference[len(prefix) :]
                if name not in definitions or name in stack:
                    _fail()
                target = definitions[name]
                if not isinstance(target, Mapping):
                    _fail()
                merged = {
                    **{
                        key: value
                        for key, value in target.items()
                        if key not in _DEFINITION_KEYS
                    },
                    **{key: value for key, value in node.items() if key != "$ref"},
                }
                return resolve(merged, stack + (name,), depth + 1)
            return {
                key: resolve(value, stack, depth + 1)
                for key, value in node.items()
                if key not in _DEFINITION_KEYS
            }
        # Tuples too. `_freeze` turns every array into a tuple, and the planner
        # is handed frozen schemas, so matching only `list` meant `$ref`
        # resolution AND the cycle guard silently skipped anything inside an
        # array on the production path. The same schema then answered
        # differently depending on whether it arrived frozen or plain --
        # Zapier's self-referential `__schema0` failed closed when read from the
        # package file and passed when read from a live binding.
        if type(node) in {list, tuple}:
            return [resolve(item, stack, depth + 1) for item in node]
        return node

    return resolve(schema, (), 0)


def _result_schema(schema: object, depth: int = 0) -> dict[str, object]:
    """Accept closed result objects or the sealed open-object MCP result shape."""
    document = _plain(schema)
    if (
        isinstance(document, dict)
        and document.get("type") == "object"
        and document.get("additionalProperties") is True
        and set(document) <= {"type", "properties", "additionalProperties"}
        and document.get("properties", {}) == {}
    ):
        if depth > _MAX_DEPTH or len(canonical_bytes(document)) > _MAX_BYTES:
            raise ValueError("plugin_plan_limit_exceeded")
        return {"type": "object", "additionalProperties": True, "properties": {}}
    # A result describes provider data Omega reads, not arguments it writes, so
    # an unstated object opens: a provider adding a field must not fail a read.
    try:
        return _schema(schema, depth, open_default=True)
    except ValueError:
        pass
    # Real result schemas routinely compose with "$defs"/"$ref" (Drive's file
    # resource is recursive), which the closed contract cannot express. Refusing
    # them made the tool unplannable, which is strictly worse than describing
    # the result as the open object it already is: the planner never fills a
    # result, and every byte still passes the guardian projection and the
    # binding's own size and media-type bounds before anything sees it.
    if depth > _MAX_DEPTH:
        raise ValueError("plugin_plan_limit_exceeded")
    return {"type": "object", "additionalProperties": True, "properties": {}}


def _schema(schema: object, depth: int = 0, *, open_default: bool = False) -> dict[str, object]:
    """Normalize one declared schema against the closed planning contract.

    ``additionalProperties`` is normalized rather than demanded. Real MCP
    schemas almost never write it, and refusing them left every shipped plugin
    tool unplannable. The safe default differs by direction and is applied
    here, not trusted from the author: an argument object closes, so the
    planner can never invent an argument the provider did not declare, while a
    result object opens, so a provider adding a field does not fail a read.
    """
    if (
        depth > _MAX_DEPTH
        or not isinstance(schema, Mapping)
        or any(type(key) is not str for key in schema)
    ):
        _fail()
    if depth == 0:
        schema = _inline_refs(schema)
    document = _plain(schema)
    if isinstance(document, dict):
        document = {
            key: value
            for key, value in document.items()
            if not key.startswith(_EXTENSION_PREFIX) and key not in _DROPPED_KEYS
        }
        # ``const`` is the narrowest constraint JSON Schema has and the contract
        # already bounds its general form, so it is normalized rather than
        # dropped. A schema stating both keeps the enum it wrote.
        if "const" in document:
            constant = document.pop("const")
            document.setdefault("enum", [constant])
    if not isinstance(document, dict) or set(document).difference(_SCHEMA_KEYS):
        _fail()
    for key in ("deprecated", "readOnly", "writeOnly"):
        if key in document and type(document[key]) is not bool:
            _fail()
    # A branch union describes the shape instead of naming a type, so a schema
    # carrying one may omit "type" -- which is how Netlify writes every
    # discriminated operation. Each branch is normalized by this same contract,
    # so a branch can admit nothing an ordinary subschema could not.
    branch_keys = [key for key in ("anyOf", "oneOf") if key in document]
    for key in branch_keys:
        branches = document[key]
        if not isinstance(branches, list) or not branches or len(branches) > _MAX_ITEMS:
            _fail()
        document[key] = [
            _schema(branch, depth + 1, open_default=open_default) for branch in branches
        ]
    # "type" may name one type or a union of them ("string" or null is the
    # ordinary way a provider marks an optional field). Both are admitted; every
    # member must still be one of the types the contract knows.
    declared = document.get("type")
    kinds: list[object] = []
    if declared is not None or not branch_keys:
        kinds = declared if type(declared) is list else [declared]
        if (
            not kinds
            or len(kinds) > _MAX_TYPE_UNION
            or len(kinds) != len(set(kinds))
            or any(item not in _SCHEMA_TYPES for item in kinds)
        ):
            _fail()
    if depth == 0 and "object" not in kinds:
        _fail()
    if "object" in kinds:
        props = document.get("properties", {})
        if not isinstance(props, dict) or len(props) > _MAX_ITEMS:
            _fail()
        if open_default:
            # A result object opens when unstated, and keeps a deliberate
            # choice when the provider made one -- but only a boolean is a
            # choice this contract knows how to carry.
            if "additionalProperties" not in document:
                document["additionalProperties"] = True
            elif document["additionalProperties"] not in (True, False):
                _fail()
        else:
            # An argument object closes however it was written, including when
            # the provider wrote it open -- PROVIDED it declares something to
            # fill. The invariant is that the PLANNER never emits an argument
            # the provider did not declare, and that is enforced by the closed
            # copy Omega hands the planner, not by whether the provider happened
            # to write `false`. Refusing every open one cost 41 of 431 tools
            # their reachability from chat while 107 others were already relying
            # on this same normalization for an omitted value.
            #
            # The TOOL'S OWN argument object is the exception, and only when it
            # declares nothing: closing that would seal a tool whose entire
            # argument surface is the part being dropped -- callable with `{}`
            # and nothing else, which is not what the provider offered. It is
            # also how an unreviewed local plugin asks for a free-form bag, and
            # that must stay unapprovable rather than become approvable and
            # useless.
            #
            # A NESTED open object is different and is closed like any other: it
            # is one field inside a tool that does declare its arguments, so the
            # tool stays reachable and only that field is limited. Refusing
            # those instead took 431 reachable tools down to 396.
            # It must be STATED open. An omitted value with no properties is the
            # ordinary way to say "this tool takes no arguments" -- `get_me`,
            # `list_labels`, `hf_whoami` -- and closing that is exactly right.
            if (
                depth == 0
                and not props
                and "additionalProperties" in document
                and document["additionalProperties"] is not False
            ):
                _fail()
            document["additionalProperties"] = False
        if any(type(key) is not str or not _ID.fullmatch(key) for key in props):
            _fail()
        document["properties"] = {
            key: _schema(item, depth + 1, open_default=open_default) for key, item in props.items()
        }
        required = document.get("required", [])
        if (
            not isinstance(required, list)
            or any(type(key) is not str for key in required)
            or len(required) != len(set(required))
            or not set(required).issubset(props)
        ):
            _fail()
    if "array" in kinds:
        if not isinstance(document.get("items"), dict):
            _fail()
        document["items"] = _schema(document["items"], depth + 1, open_default=open_default)
    for key, bound in (
        ("description", _MAX_ANNOTATION_TEXT),
        ("title", _MAX_TEXT),
        ("format", _MAX_TEXT),
    ):
        if key in document and (type(document[key]) is not str or len(document[key]) > bound):
            _fail()
    if "examples" in document and (
        not isinstance(document["examples"], list) or len(document["examples"]) > _MAX_ITEMS
    ):
        _fail()
    for key in ("maxLength", "maxItems"):
        if key in document:
            stated_bound = document[key]
            if type(stated_bound) is not int or stated_bound < 0:
                _fail()
            # A bound LOOSER than Omega's is the provider describing its own
            # limit, not a violation of this contract. Refusing it bounded
            # nothing and cost the tool: Miro declares maxLength 200000 on an
            # SVG body. Clamping leaves Omega's bound the operative one.
            document[key] = min(stated_bound, _MAX_TEXT)
    if "enum" in document and (
        not isinstance(document["enum"], list)
        or len(document["enum"]) > _MAX_ITEMS
        or any(
            not isinstance(item, (str, int, float, bool, type(None), list))
            for item in document["enum"]
        )
    ):
        _fail()
    try:
        if len(canonical_bytes(document)) > _MAX_BYTES:
            raise ValueError
        Draft202012Validator.check_schema(document)
    except (SchemaError, TypeError, ValueError):
        _fail()
    return document


def _screen_schema_annotations(value: object, *, annotation: bool = False) -> object:
    """Screen provider prose before it is placed in the planner prompt."""
    if isinstance(value, Mapping):
        return {
            key: _screen_schema_annotations(child, annotation=annotation or key in _ANNOTATION_KEYS)
            for key, child in value.items()
        }
    if type(value) in {list, tuple}:
        return [
            _screen_schema_annotations(child, annotation=annotation)
            for child in value
        ]
    if annotation and type(value) is str:
        return screen_mcp_planner_text(value, source="catalog-schema").value
    return value


def project_provider_input_schema(schema: object) -> dict[str, object]:
    """Validate and close one provider-declared tool input schema.

    Raises ``ValueError("plugin_plan_invalid")`` unless ``schema`` already
    satisfies the closed planning contract this module enforces for every
    candidate binding (bounded depth/bytes/item count, ``additionalProperties:
    false`` at every object level, and only the small whitelisted JSON Schema
    keywords). Exported so a capability that could never become a plan step
    is caught before it is ever sealed (``mcp_platform.plugin_discovery``),
    not only after it is already an installed, uncallable binding.
    """

    return _schema(schema)


def project_provider_output_schema(schema: object) -> dict[str, object]:
    """Validate and close one provider-declared tool output schema.

    The same closed contract as :func:`project_provider_input_schema`, plus
    the one open-object MCP result shape (``{"type": "object",
    "additionalProperties": true, "properties": {}}``) this module already
    exempts for a tool that declares no result schema of its own.
    """

    return _result_schema(schema)


ScreenedPlannerText = McpPlannerTextReceipt


def _screened(value: object) -> bool:
    try:
        validate_mcp_planner_text(value)
    except ValueError:
        return False
    return True


def _screened_value(value: object) -> str:
    try:
        return validate_mcp_planner_text(value)
    except ValueError:
        _fail()


@dataclass(frozen=True, slots=True)
class PluginPlannerContext:
    episode_id: str
    objective_sha256: str
    remaining_call_budget: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "episode_id", _id(self.episode_id))
        object.__setattr__(self, "objective_sha256", _digest(self.objective_sha256))
        if (
            type(self.remaining_call_budget) is not int
            or not 0 <= self.remaining_call_budget <= _MAX_STEPS
        ):
            _fail()


@dataclass(frozen=True, slots=True)
class PluginPlannerMetadata:
    candidate_id: str
    title: ScreenedPlannerText
    description: ScreenedPlannerText
    capability: ScreenedPlannerText
    effect: ScreenedPlannerText

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_id", _digest(self.candidate_id))
        if any(
            not _screened(item)
            for item in (self.title, self.description, self.capability, self.effect)
        ):
            _fail()


@dataclass(frozen=True, slots=True)
class PluginStepResult:
    step_id: str
    candidate_id: str
    status: str
    summary: ScreenedPlannerText

    def __post_init__(self) -> None:
        object.__setattr__(self, "step_id", _id(self.step_id))
        object.__setattr__(self, "candidate_id", _digest(self.candidate_id))
        if self.status not in {"completed", "failed"} or not _screened(self.summary):
            _fail()


@dataclass(frozen=True, slots=True)
class PluginOperationStep:
    step_id: str
    candidate_id: str
    arguments: Mapping[str, object]
    consumes_step_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "step_id", _id(self.step_id))
        object.__setattr__(self, "candidate_id", _digest(self.candidate_id))
        frozen = _freeze(self.arguments)
        if (
            not isinstance(frozen, Mapping)
            or type(self.consumes_step_ids) is not tuple
            or len(self.consumes_step_ids) > 3
        ):
            _fail()
        consumed = tuple(_id(item) for item in self.consumes_step_ids)
        if len(consumed) != len(set(consumed)) or self.step_id in consumed:
            _fail()
        object.__setattr__(self, "arguments", frozen)
        object.__setattr__(self, "consumes_step_ids", consumed)


@dataclass(frozen=True, slots=True)
class PluginOperationPlan:
    plan_id: str
    episode_id: str
    objective_sha256: str
    steps: tuple[PluginOperationStep, ...]
    authority_digest: str

    def __post_init__(self) -> None:
        if (
            type(self.steps) is not tuple
            or not 1 <= len(self.steps) <= _MAX_STEPS
            or len({item.step_id for item in self.steps}) != len(self.steps)
        ):
            _fail()
        seen: set[str] = set()
        for step in self.steps:
            if type(step) is not PluginOperationStep or not set(step.consumes_step_ids).issubset(
                seen
            ):
                _fail()
            seen.add(step.step_id)
        object.__setattr__(self, "plan_id", _digest(self.plan_id))
        object.__setattr__(self, "episode_id", _id(self.episode_id))
        object.__setattr__(self, "objective_sha256", _digest(self.objective_sha256))
        object.__setattr__(self, "authority_digest", _digest(self.authority_digest))


class PluginPlanDecisionKind(StrEnum):
    ASK_USER = "ask_user"
    CALL_TOOL = "call_tool"
    FINISH = "finish"


@dataclass(frozen=True, slots=True)
class PluginPlanDecision:
    kind: PluginPlanDecisionKind
    question: str | None = None
    plan: PluginOperationPlan | None = None
    summary: str | None = None


def _authority(candidates: Sequence[PluginToolBinding]) -> str:
    return sha256_hex(
        canonical_bytes(
            [
                {
                    "candidate_id": item.candidate_id,
                    "capability_kind": item.capability_kind,
                    "input": item.input_schema_sha256,
                    "output": item.output_schema_sha256,
                    "connection": item.connection_revision,
                }
                for item in sorted(candidates, key=lambda item: item.candidate_id)
            ]
        )
    )


def _words(value: str) -> set[str]:
    return set(_WORD.findall(value.casefold()))


def _terms(schema: Mapping[str, object]) -> set[str]:
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return set()
    terms = {str(key).casefold() for key in properties}
    for child in properties.values():
        if isinstance(child, Mapping) and isinstance(child.get("enum"), (list, tuple)):
            terms.update(str(item).casefold() for item in child["enum"] if type(item) is str)
    return terms


def plugin_objective_is_plannable(objective: object) -> bool:
    """Whether this text can be a planner objective at all.

    A padded, empty, oversized or control-character objective means there is
    nothing to retrieve. It is never evidence about the catalog.
    """
    try:
        _text(objective)
    except ValueError:
        return False
    return True


def plugin_binding_is_plannable(binding: object) -> bool:
    """Whether both declared schemas satisfy the closed planning contract."""
    if type(binding) is not PluginToolBinding:
        return False
    try:
        _schema(binding.input_schema)
        _result_schema(binding.output_schema)
    except ValueError:
        return False
    return True


def deterministic_plugin_arguments(
    objective: str,
    binding: PluginToolBinding,
) -> dict[str, object] | None:
    """Resolve every required argument from the objective alone, or return None.

    AUTO uses this for the deterministic fast path. Incomplete arguments are
    still plugin-eligible at Global A and belong to the planner's ``ask_user``.
    """
    schema = binding.input_schema
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    if not isinstance(properties, Mapping) or not isinstance(required, Sequence):
        return None
    lowered = objective.casefold()
    arguments: dict[str, object] = {}
    for key in required:
        child = properties.get(key)
        if not isinstance(child, Mapping) or child.get("type") != "string":
            return None
        enum = child.get("enum")
        if isinstance(enum, Sequence) and not isinstance(enum, str):
            selected = next(
                (item for item in enum if type(item) is str and item.casefold() in lowered),
                None,
            )
            if selected is None and len(enum) == 1 and type(enum[0]) is str:
                selected = enum[0]
            if selected is None:
                return None
            arguments[key] = selected
        elif key.casefold() in _DATE_ARGUMENT_FIELDS:
            if "tomorrow" in lowered:
                arguments[key] = "tomorrow"
            elif "today" in lowered:
                arguments[key] = "today"
            else:
                return None
        elif key.casefold() in _TEXT_ARGUMENT_FIELDS:
            arguments[key] = objective
        else:
            return None
    return arguments


def retrieve_plugin_candidates(
    objective: str,
    bindings: Sequence[PluginToolBinding],
    *,
    explicit_plugin_id: str | None = None,
    limit: int = _MAX_CANDIDATES,
) -> tuple[PluginToolBinding, ...]:
    objective = _text(objective)
    if type(limit) is not int or not 1 <= limit <= _MAX_CANDIDATES:
        raise ValueError("plugin_plan_limit_exceeded")
    # A binding whose declared schemas cannot pass the closed planning contract can
    # never become a plan step, so it is not a candidate. One such installed tool
    # must not take the whole retrieval down with it.
    candidates = tuple(
        item for item in _bindings(bindings, catalog=True) if plugin_binding_is_plannable(item)
    )
    if explicit_plugin_id is not None:
        plugin = _id(explicit_plugin_id)
        return tuple(
            sorted(
                (item for item in candidates if item.plugin_id == plugin),
                key=lambda item: item.candidate_id,
            )[:limit]
        )
    words = _words(objective)

    def score(item: PluginToolBinding) -> int:
        fields = (item.title, item.description, item.capability)
        return sum(10 for field in fields for word in _words(field) if word in words) + sum(
            3 for term in _terms(_schema(item.input_schema)) if term in words
        )

    plugin_mentions = set(
        re.findall(r"@([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})(?![A-Za-z0-9_.:-])", objective)
    )
    ranked = [
        (score(item) + (10_000 if item.plugin_id in plugin_mentions else 0), item)
        for item in candidates
    ]
    return tuple(
        item
        for score_value, item in sorted(
            (row for row in ranked if row[0]), key=lambda row: (-row[0], row[1].candidate_id)
        )[:limit]
    )


def _response_schema(candidates: tuple[PluginToolBinding, ...], budget: int) -> dict[str, object]:
    branches: list[dict[str, object]] = [
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["decision", "question"],
            "properties": {
                "decision": {"const": "ask_user"},
                "question": {"type": "string", "maxLength": _MAX_TEXT},
            },
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["decision", "summary"],
            "properties": {
                "decision": {"const": "finish"},
                "summary": {"type": "string", "maxLength": _MAX_TEXT},
            },
        },
    ]
    if budget:
        steps = {
            "type": "object",
            "additionalProperties": False,
            "required": ["step_id", "candidate_id", "arguments", "consumes_step_ids"],
            "properties": {
                "step_id": {"type": "string", "pattern": _ID.pattern},
                "candidate_id": {"enum": [item.candidate_id for item in candidates]},
                "arguments": {"type": "object"},
                "consumes_step_ids": {
                    "type": "array",
                    "maxItems": 3,
                    "items": {"type": "string", "pattern": _ID.pattern},
                },
            },
        }
        branches.append(
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["decision", "steps"],
                "properties": {
                    "decision": {"const": "call_tool"},
                    "steps": {"type": "array", "minItems": 1, "maxItems": budget, "items": steps},
                },
            }
        )
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "oneOf": branches}


def plugin_decision_schema(
    candidates: Sequence[PluginToolBinding], *, remaining_call_budget: int
) -> dict[str, object]:
    selected = _bindings(candidates)
    if type(remaining_call_budget) is not int or not 0 <= remaining_call_budget <= _MAX_STEPS:
        _fail()
    for item in selected:
        _schema(item.input_schema)
        _result_schema(item.output_schema)
    schema = _response_schema(selected, remaining_call_budget)
    Draft202012Validator.check_schema(schema)
    frozen = _frozen_schema(schema)
    if not isinstance(frozen, dict):
        _fail()
    return frozen


def _metadata(
    candidates: tuple[PluginToolBinding, ...], metadata: Sequence[PluginPlannerMetadata]
) -> tuple[PluginPlannerMetadata, ...]:
    if not isinstance(metadata, Sequence):
        _fail()
    rows = tuple(metadata)
    if any(type(row) is not PluginPlannerMetadata for row in rows) or {
        row.candidate_id for row in rows
    } != {item.candidate_id for item in candidates}:
        _fail()
    return tuple(sorted(rows, key=lambda row: row.candidate_id))


def _context(
    context: object, objective: ScreenedPlannerText, prior: tuple[PluginStepResult, ...]
) -> PluginPlannerContext:
    if (
        type(context) is not PluginPlannerContext
        or context.objective_sha256 != sha256_hex(canonical_bytes(_screened_value(objective)))
        or context.remaining_call_budget
        != _MAX_STEPS - sum(item.status == "completed" for item in prior)
    ):
        _fail()
    return context


def build_plugin_planner_prompt(
    objective: ScreenedPlannerText,
    candidates: Sequence[PluginToolBinding],
    prior_steps: Sequence[PluginStepResult],
    *,
    context: PluginPlannerContext,
    metadata: Sequence[PluginPlannerMetadata],
) -> tuple[str, dict[str, object]]:
    if (
        type(objective) is not ScreenedPlannerText
        or not isinstance(prior_steps, Sequence)
        or len(prior_steps) > _MAX_STEPS
    ):
        _fail()
    selected, prior = _bindings(candidates), tuple(prior_steps)
    if any(type(item) is not PluginStepResult for item in prior):
        _fail()
    context = _context(context, objective, prior)
    rows = _metadata(selected, metadata)
    prompt = {
        "objective": _screened_value(objective),
        "remaining_call_budget": context.remaining_call_budget,
        "authority_digest": _authority(selected),
        "candidates": [
            {
                "candidate_id": row.candidate_id,
                "capability_kind": next(
                    item for item in selected if item.candidate_id == row.candidate_id
                ).capability_kind,
                "title": _screened_value(row.title),
                "description": _screened_value(row.description),
                "capability": _screened_value(row.capability),
                "effect": _screened_value(row.effect),
                "input_schema": _screen_schema_annotations(
                    _schema(
                        next(
                            item for item in selected if item.candidate_id == row.candidate_id
                        ).input_schema
                    )
                ),
            }
            for row in rows
        ],
        "prior_steps": [
            {
                "step_id": item.step_id,
                "candidate_id": item.candidate_id,
                "status": item.status,
                "summary": _screened_value(item.summary),
            }
            for item in prior
        ],
    }
    return json.dumps(prompt, sort_keys=True, separators=(",", ":")), plugin_decision_schema(
        selected, remaining_call_budget=context.remaining_call_budget
    )


def _at(schema: dict[str, object], path: list[object]) -> dict[str, object]:
    current = schema
    for part in path:
        if (
            type(part) is not str
            or current.get("type") != "object"
            or not isinstance(current.get("properties"), dict)
            or part not in current["properties"]
        ):
            _fail()
        current = current["properties"][part]
    return current


def _compatible(source: dict[str, object], target: dict[str, object]) -> bool:
    return canonical_bytes(source) == canonical_bytes(target)


def validate_plugin_argument_provenance(value: object, *, field_name: str = "") -> None:
    """Reject new external literals while preserving benign topical operator text."""
    if isinstance(value, Mapping) and set(value) == {"$from_step"}:
        return
    if type(value) is str:
        if field_name and _TAINTED_ARGUMENT_KEY.search(field_name):
            _fail()
        try:
            screen_mcp_planner_text(value, source="planner-argument")
        except ValueError:
            _fail()
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            if type(key) is not str:
                _fail()
            validate_plugin_argument_provenance(child, field_name=key)
        return
    if type(value) in {list, tuple}:
        for child in value:
            validate_plugin_argument_provenance(child, field_name=field_name)


def _validate_arguments(
    value: object,
    schema: dict[str, object],
    outputs: Mapping[str, dict[str, object]],
    consumed: tuple[str, ...],
) -> None:
    if isinstance(value, str) and ("$from_step" in value or "${" in value or "{{" in value):
        _fail()
    if isinstance(value, Mapping) and "$from_step" in value and set(value) != {"$from_step"}:
        _fail()
    if isinstance(value, Mapping) and set(value) == {"$from_step"}:
        ref = value["$from_step"]
        if (
            not isinstance(ref, Mapping)
            or set(ref) != {"step_id", "path"}
            or ref["step_id"] not in consumed
            or ref["step_id"] not in outputs
            or not isinstance(ref["path"], list)
            or len(ref["path"]) > _MAX_DEPTH
            or not _compatible(_at(outputs[ref["step_id"]], ref["path"]), schema)
        ):
            _fail()
        return
    validate_plugin_argument_provenance(value)
    if schema.get("type") == "object":
        properties = schema.get("properties", {})
        if (
            not isinstance(value, Mapping)
            or not set(value).issubset(properties)
            or not set(schema.get("required", [])).issubset(value)
        ):
            _fail()
        for key, child in value.items():
            _validate_arguments(child, schema["properties"][key], outputs, consumed)
    elif schema.get("type") == "array" and isinstance(value, list):
        if ("minItems" in schema and len(value) < schema["minItems"]) or (
            "maxItems" in schema and len(value) > schema["maxItems"]
        ):
            _fail()
        if "enum" in schema and value not in schema["enum"]:
            _fail()
        for child in value:
            _validate_arguments(child, schema["items"], outputs, consumed)
    else:
        try:
            Draft202012Validator(schema).validate(value)
        except ValidationError:
            _fail()


def _referenced_step_ids(value: object) -> set[str]:
    """Return every prior-step reference embedded in one argument tree."""
    if isinstance(value, Mapping):
        if set(value) == {"$from_step"}:
            reference = value["$from_step"]
            if isinstance(reference, Mapping) and type(reference.get("step_id")) is str:
                return {reference["step_id"]}
            return set()
        found: set[str] = set()
        for child in value.values():
            found.update(_referenced_step_ids(child))
        return found
    if type(value) in {list, tuple}:
        found: set[str] = set()
        for child in value:
            found.update(_referenced_step_ids(child))
        return found
    return set()


def _binding_identity(binding: PluginToolBinding) -> tuple[str, str]:
    """Identify the provider/account that owns a planner binding."""
    return (binding.plugin_id, binding.account_key_hash)


def validate_plugin_plan_provenance(
    plan: PluginOperationPlan,
    bindings: Sequence[PluginToolBinding],
) -> None:
    """Recheck cross-provider references when executing a persisted plan."""
    if type(plan) is not PluginOperationPlan or len(bindings) != len(plan.steps):
        _fail()
    known = {
        binding.candidate_id: binding
        for binding in bindings
        if type(binding) is PluginToolBinding
    }
    if any(type(binding) is not PluginToolBinding for binding in bindings) or any(
        step.candidate_id not in known for step in plan.steps
    ):
        _fail()
    provenance: dict[str, set[tuple[str, str]]] = {}
    for step in plan.steps:
        binding = known.get(step.candidate_id)
        if binding is None:
            _fail()
        source_identities = {
            identity
            for reference in _referenced_step_ids(_plain(step.arguments))
            for identity in provenance.get(reference, set())
        }
        target_identity = _binding_identity(binding)
        if (
            source_identities
            and any(identity != target_identity for identity in source_identities)
            and binding.risk_class is not RiskClass.CROSS_PROVIDER_TRANSFER
        ):
            _fail()
        provenance[step.step_id] = {target_identity, *source_identities}


def parse_plugin_plan_decision(
    raw: object,
    *,
    candidates: Sequence[PluginToolBinding],
    repair_used: bool,
    context: PluginPlannerContext,
    prior_steps: Sequence[PluginStepResult],
) -> PluginPlanDecision:
    selected = _bindings(candidates)
    prior = tuple(prior_steps) if isinstance(prior_steps, Sequence) else ()
    try:
        if len(canonical_bytes(_plain(raw))) > _MAX_BYTES:
            raise ValueError("plugin_plan_limit_exceeded")
        if type(repair_used) is not bool or any(
            type(item) is not PluginStepResult for item in prior
        ):
            _fail()
        if type(
            context
        ) is not PluginPlannerContext or context.remaining_call_budget != _MAX_STEPS - sum(
            item.status == "completed" for item in prior
        ):
            _fail()
        Draft202012Validator(
            _plain(
                plugin_decision_schema(
                    selected, remaining_call_budget=context.remaining_call_budget
                )
            )
        ).validate(raw)
        if not isinstance(raw, Mapping):
            _fail()
        if raw["decision"] == "ask_user":
            return PluginPlanDecision(
                PluginPlanDecisionKind.ASK_USER, question=_text(raw["question"])
            )
        if raw["decision"] == "finish":
            return PluginPlanDecision(PluginPlanDecisionKind.FINISH, summary=_text(raw["summary"]))
        known = {item.candidate_id: item for item in selected}
        outputs: dict[str, dict[str, object]] = {}
        provenance: dict[str, set[tuple[str, str]]] = {}
        steps: list[PluginOperationStep] = []
        for item in raw["steps"]:
            binding = known.get(item["candidate_id"])
            if binding is None:
                _fail()
            step = PluginOperationStep(
                item["step_id"],
                item["candidate_id"],
                item["arguments"],
                tuple(item["consumes_step_ids"]),
            )
            if len(canonical_bytes(_plain(step.arguments))) > _MAX_BYTES:
                raise ValueError("plugin_plan_limit_exceeded")
            _validate_arguments(
                _plain(step.arguments),
                _schema(binding.input_schema),
                outputs,
                step.consumes_step_ids,
            )
            target_identity = _binding_identity(binding)
            source_identities = {
                identity
                for reference in _referenced_step_ids(_plain(step.arguments))
                for identity in provenance.get(reference, set())
            }
            if (
                source_identities
                and any(identity != target_identity for identity in source_identities)
                and binding.risk_class is not RiskClass.CROSS_PROVIDER_TRANSFER
            ):
                _fail()
            provenance[step.step_id] = {target_identity, *source_identities}
            outputs[step.step_id] = _result_schema(binding.output_schema)
            steps.append(step)
        plan_steps = tuple(steps)
        authority = _authority(selected)
        plan_id = sha256_hex(
            canonical_bytes(
                {
                    "episode": context.episode_id,
                    "objective": context.objective_sha256,
                    "authority": authority,
                    "steps": [
                        {
                            "id": step.step_id,
                            "candidate": step.candidate_id,
                            "arguments": sha256_hex(canonical_bytes(_plain(step.arguments))),
                            "consumes": list(step.consumes_step_ids),
                        }
                        for step in plan_steps
                    ],
                }
            )
        )
        return PluginPlanDecision(
            PluginPlanDecisionKind.CALL_TOOL,
            plan=PluginOperationPlan(
                plan_id, context.episode_id, context.objective_sha256, plan_steps, authority
            ),
        )
    except (KeyError, SchemaError, ValidationError, TypeError, ValueError):
        raise ValueError(
            "plugin_plan_repair_exhausted" if repair_used else "plugin_plan_repair_required"
        ) from None


__all__ = [
    "PluginOperationPlan",
    "PluginOperationStep",
    "PluginPlanDecision",
    "PluginPlanDecisionKind",
    "PluginPlannerContext",
    "PluginPlannerMetadata",
    "PluginStepResult",
    "ScreenedPlannerText",
    "build_plugin_planner_prompt",
    "deterministic_plugin_arguments",
    "parse_plugin_plan_decision",
    "plugin_binding_is_plannable",
    "plugin_decision_schema",
    "plugin_objective_is_plannable",
    "retrieve_plugin_candidates",
    "validate_plugin_argument_provenance",
    "validate_plugin_plan_provenance",
]
