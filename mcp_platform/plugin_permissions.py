"""Durable single-use permission requests for exact plugin plans."""

from __future__ import annotations

import json
import math
import os
import re
import time
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import TypeAlias

from core.paths import MCP_PLUGIN_PERMISSION_STATE_PATH
from mcp_platform.authority import _AuthorityIssuer
from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.operation_contracts import McpAuthoritySnapshot
from mcp_platform.package_contracts import RiskClass
from mcp_platform.plugin_authority import (
    _authority_from_dict,
    _authority_to_dict,
    _plan_document,
    plugin_plan_digest,
)
from mcp_platform.plugin_catalog import PluginToolBinding
from mcp_platform.plugin_planning import PluginOperationPlan, PluginOperationStep
from mcp_platform.state_store import McpStateStore

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_OPAQUE = re.compile(r"[0-9a-f]{32,128}\Z")
_SCHEMA_VERSION = 4
_MAX_DOCUMENT_BYTES = 2_097_152
_MAX_RECORDS = 256
_MAX_DISPLAY_BYTES = 16_384


def _lifecycle_mutation(method: Callable[..., object]) -> Callable[..., object]:
    @wraps(method)
    def guarded(self: object, *args: object, **kwargs: object) -> object:
        guard = getattr(self, "_lifecycle_mutation_guard", None)
        scope = nullcontext() if not callable(guard) else guard()
        with scope:
            return method(self, *args, **kwargs)

    return guarded
_MAX_TEXT_BYTES = 2_048
_SENSITIVE_ARGUMENT_RE = re.compile(
    r"(?i)(?:credential|token|secret|password|authorization|api[_ -]?key|"
    r"private[_ -]?key|client[_ -]?secret)"
)

_EFFECT_SUMMARIES = {
    "read": "Read data",
    "create": "Create data",
    "update": "Update data",
    "delete": "Delete data",
    "send": "Send or publish data",
    "transfer": "Transfer data between providers",
    # Matches client_contracts.UNREVIEWED_EFFECT: a locally sealed,
    # external_unreviewed capability from mcp_platform.plugin_discovery.
    "unreviewed": "Use an unreviewed external capability",
}

AuthorityBundle: TypeAlias = tuple[McpAuthoritySnapshot, ...]
AuthorityFactory: TypeAlias = Callable[
    [PluginOperationPlan, str, str], McpAuthoritySnapshot | AuthorityBundle
]
BindingResolver: TypeAlias = Callable[
    [PluginOperationPlan, str],
    tuple[PluginToolBinding, ...],
]


def _text(value: object, *, error: str, maximum: int = _MAX_TEXT_BYTES) -> str:
    if (
        type(value) is not str
        or not value
        or value.strip() != value
        or len(value.encode("utf-8")) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(error)
    return value


def _digest(value: object, *, error: str) -> str:
    value = _text(value, error=error, maximum=64)
    if _DIGEST.fullmatch(value) is None:
        raise ValueError(error)
    return value


def _finite(value: object, *, error: str) -> float:
    if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
        raise ValueError(error)
    return float(value)


def _strings(
    value: object,
    *,
    error: str,
    allow_empty: bool = True,
    unique: bool = True,
) -> tuple[str, ...]:
    if type(value) not in {tuple, list}:
        raise ValueError(error)
    result = tuple(_text(item, error=error) for item in value)
    if (not allow_empty and not result) or unique and len(result) != len(set(result)):
        raise ValueError(error)
    return result


def _random_hex(random_bytes: Callable[[int], bytes], size: int) -> str:
    value = random_bytes(size)
    if type(value) is not bytes or len(value) != size:
        raise ValueError("plugin_permission_randomness_invalid")
    return value.hex()


def _display_label(identifier: str) -> str:
    parts = [part for part in re.split(r"[._-]+", identifier) if part]
    if parts and parts[0].casefold() in {"plugin", "app"}:
        parts = parts[1:]
    if not parts:
        raise ValueError("plugin_permission_consent_mismatch")
    acronyms = {"api": "API", "mcp": "MCP", "oauth": "OAuth"}
    return " ".join(acronyms.get(part.casefold(), part.capitalize()) for part in parts)


def _field_paths(value: object, prefix: str = "") -> tuple[str, ...]:
    paths: list[str] = []
    if isinstance(value, Mapping):
        if not value and prefix:
            paths.append(prefix)
        for key in sorted(value):
            if type(key) is not str:
                raise ValueError("plugin_permission_consent_mismatch")
            child = key if not prefix else f"{prefix}.{key}"
            paths.extend(_field_paths(value[key], child))
    elif type(value) in {tuple, list}:
        if not value and prefix:
            paths.append(prefix)
        for item in value:
            paths.extend(_field_paths(item, f"{prefix}[]"))
    elif prefix:
        paths.append(prefix)
    return tuple(sorted(dict.fromkeys(paths)))


def _argument_previews(plan: PluginOperationPlan) -> tuple[str, ...]:
    """Render useful argument values while redacting secret-shaped keys."""
    previews: list[str] = []

    def walk(value: object, prefix: str) -> None:
        if isinstance(value, Mapping):
            for key in sorted(value):
                if type(key) is not str:
                    raise ValueError("plugin_permission_consent_mismatch")
                walk(value[key], key if not prefix else f"{prefix}.{key}")
            return
        if type(value) in {list, tuple}:
            for index, child in enumerate(value):
                walk(child, f"{prefix}[{index}]")
            return
        key = prefix.rsplit(".", 1)[-1].split("[", 1)[0]
        if _SENSITIVE_ARGUMENT_RE.search(key):
            rendered = "<redacted>"
        else:
            try:
                rendered = canonical_bytes(_plain_json(value)).decode("utf-8")[:256]
            except (TypeError, ValueError, UnicodeError):
                rendered = "<unavailable>"
        previews.append(f"{prefix}={rendered}")

    for step in plan.steps:
        before = len(previews)
        walk(step.arguments, "")
        for index in range(before, len(previews)):
            previews[index] = f"{step.step_id}:{previews[index]}"
    return tuple(previews)


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if type(value) in {tuple, list}:
        return [_plain_json(item) for item in value]
    return value


def _binding_projection(
    plan: PluginOperationPlan,
    bindings: tuple[PluginToolBinding, ...],
    account_key_hash: str,
) -> list[dict[str, str]]:
    if (
        type(bindings) is not tuple
        or len(bindings) != len(plan.steps)
        or any(type(binding) is not PluginToolBinding for binding in bindings)
    ):
        raise ValueError("plugin_permission_consent_mismatch")
    selected: list[dict[str, str]] = []
    for step, binding in zip(plan.steps, bindings, strict=True):
        if (
            binding.candidate_id != step.candidate_id
            or binding.account_key_hash != account_key_hash
            or ":" in binding.capability
        ):
            raise ValueError("plugin_permission_consent_mismatch")
        selected.append(
            {
                "candidate_id": binding.candidate_id,
                "plugin_id": binding.plugin_id,
                "plugin_revision": binding.plugin_revision,
                "app_id": binding.app_id,
                "plugin_name": _display_label(binding.plugin_id),
                "app_name": _display_label(binding.app_id),
                "connection_revision": binding.connection_revision,
                "capability": binding.capability,
                "tool_name": binding.tool_name,
                "tool_schema_sha256": binding.input_schema_sha256,
                "title": binding.title,
                "risk_class": binding.risk_class.value,
                "effect": binding.effect,
            }
        )
    return selected


def _derived_display(
    plan: PluginOperationPlan,
    projection: list[dict[str, str]],
    authorities: AuthorityBundle,
) -> PluginPermissionDisplay:
    risks = tuple(dict.fromkeys(item["risk_class"] for item in projection))
    effects = tuple(dict.fromkeys(item["effect"] for item in projection))
    fields = tuple(
        dict.fromkeys(path for step in plan.steps for path in _field_paths(step.arguments))
    )
    sources = tuple(
        canonical_bytes(dict(authority.source_constraints)).decode("utf-8")
        for authority in authorities
    )
    destinations = tuple(
        canonical_bytes(dict(authority.destination_constraints)).decode("utf-8")
        for authority in authorities
    )
    audience = sources[0] if sources and len(set(sources)) == 1 else " | ".join(sources)
    destination = (
        destinations[0]
        if destinations and len(set(destinations)) == 1
        else " | ".join(destinations)
    )
    try:
        effect_summary = " | ".join(_EFFECT_SUMMARIES[effect] for effect in effects)
    except KeyError:
        raise ValueError("plugin_permission_consent_mismatch") from None
    return PluginPermissionDisplay(
        plugin_apps=tuple(
            (
                item["plugin_id"],
                item["plugin_name"],
                item["app_id"],
                item["app_name"],
            )
            for item in projection
        ),
        effect_summary=effect_summary,
        ordered_operations=tuple(item["title"] for item in projection),
        audience=audience,
        destination=destination,
        crosses_providers=RiskClass.CROSS_PROVIDER_TRANSFER.value in risks,
        field_names=fields,
        risks=risks,
        argument_previews=_argument_previews(plan),
    )


def _display_projection_matches(
    display: PluginPermissionDisplay,
    plan: PluginOperationPlan,
    projection: list[dict[str, str]],
) -> bool:
    risks = tuple(dict.fromkeys(item["risk_class"] for item in projection))
    effects = tuple(dict.fromkeys(item["effect"] for item in projection))
    try:
        effect_summary = " | ".join(_EFFECT_SUMMARIES[effect] for effect in effects)
    except KeyError:
        return False
    return (
        display.plugin_apps
        == tuple(
            (
                item["plugin_id"],
                item["plugin_name"],
                item["app_id"],
                item["app_name"],
            )
            for item in projection
        )
        and display.effect_summary == effect_summary
        and display.ordered_operations == tuple(item["title"] for item in projection)
        and display.crosses_providers == (RiskClass.CROSS_PROVIDER_TRANSFER.value in risks)
        and display.field_names
        == tuple(
            dict.fromkeys(path for step in plan.steps for path in _field_paths(step.arguments))
        )
        and display.risks == risks
        and (
            not display.argument_previews
            or display.argument_previews == _argument_previews(plan)
        )
    )


def _authority_bundle(value: object) -> AuthorityBundle:
    authorities = value if type(value) is tuple else (value,)
    if not authorities or any(
        type(authority) is not McpAuthoritySnapshot for authority in authorities
    ):
        raise ValueError("plugin_permission_authority_invalid")
    return authorities


def _authority_bundle_matches(
    plan: PluginOperationPlan,
    account_key_hash: str,
    projection: list[dict[str, str]],
    authorities: AuthorityBundle,
) -> bool:
    return (
        len(authorities) == len(plan.steps) == len(projection)
        and len({authority.authority_id for authority in authorities}) == len(authorities)
        and all(
            authority.authority_kind == "direct_turn"
            and authority.episode_id == plan.episode_id
            and authority.plan_digest == plugin_plan_digest(plan)
            and authority.account_key_hash == account_key_hash
            and authority.candidate_id == step.candidate_id == item["candidate_id"]
            and authority.plan_step_id == step.step_id
            and authority.arguments_sha256
            == sha256_hex(canonical_bytes(_plain_json(step.arguments)))
            and authority.connection_revision == item["connection_revision"]
            and authority.tool_name == item["tool_name"]
            and authority.tool_schema_sha256 == item["tool_schema_sha256"]
            and tuple(authority.capability_tags) == (f"tool:{item['capability']}",)
            and authority.effect == item["effect"]
            and tuple(authority.risk_classes) == (item["risk_class"],)
            and authority.direct_execution
            and not authority.standing_execution
            and authority.allow_send_publish == (item["risk_class"] == RiskClass.SEND_PUBLISH.value)
            and authority.allow_destructive == (item["risk_class"] == RiskClass.DESTRUCTIVE.value)
            and authority.allow_cross_provider
            == (item["risk_class"] == RiskClass.CROSS_PROVIDER_TRANSFER.value)
            for authority, step, item in zip(
                authorities,
                plan.steps,
                projection,
                strict=True,
            )
        )
    )


@dataclass(frozen=True, slots=True)
class PluginPermissionDisplay:
    plugin_apps: tuple[tuple[str, str, str, str], ...]
    effect_summary: str
    ordered_operations: tuple[str, ...]
    audience: str
    destination: str
    crosses_providers: bool
    field_names: tuple[str, ...]
    risks: tuple[str, ...]
    argument_previews: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        error = "plugin_permission_display_invalid"
        if type(self.plugin_apps) is not tuple or not self.plugin_apps:
            raise ValueError(error)
        identities: list[tuple[str, str, str, str]] = []
        for identity in self.plugin_apps:
            if type(identity) is not tuple or len(identity) != 4:
                raise ValueError(error)
            identities.append(tuple(_text(item, error=error) for item in identity))
        object.__setattr__(self, "plugin_apps", tuple(identities))
        for field in ("effect_summary",):
            object.__setattr__(self, field, _text(getattr(self, field), error=error))
        for field in ("audience", "destination"):
            value = getattr(self, field)
            if value == "":
                continue
            object.__setattr__(self, field, _text(value, error=error))
        object.__setattr__(
            self,
            "ordered_operations",
            _strings(
                self.ordered_operations,
                error=error,
                allow_empty=False,
                unique=False,
            ),
        )
        object.__setattr__(self, "field_names", _strings(self.field_names, error=error))
        object.__setattr__(
            self,
            "risks",
            _strings(self.risks, error=error, allow_empty=False),
        )
        object.__setattr__(
            self,
            "argument_previews",
            _strings(self.argument_previews, error=error, unique=False),
        )
        if type(self.crosses_providers) is not bool:
            raise ValueError(error)
        if len(canonical_bytes(self.to_dict())) > _MAX_DISPLAY_BYTES:
            raise ValueError(error)

    def to_dict(self) -> dict[str, object]:
        payload = {
            "plugin_apps": [
                {
                    "plugin_id": plugin_id,
                    "plugin_name": plugin_name,
                    "app_id": app_id,
                    "app_name": app_name,
                }
                for plugin_id, plugin_name, app_id, app_name in self.plugin_apps
            ],
            "effect_summary": self.effect_summary,
            "ordered_operations": list(self.ordered_operations),
            "audience": self.audience,
            "destination": self.destination,
            "crosses_providers": self.crosses_providers,
            "field_names": list(self.field_names),
            "risks": list(self.risks),
        }
        if self.argument_previews:
            payload["argument_previews"] = list(self.argument_previews)
        return payload

    @classmethod
    def from_dict(cls, value: object) -> PluginPermissionDisplay:
        required = {
            "plugin_apps",
            "effect_summary",
            "ordered_operations",
            "audience",
            "destination",
            "crosses_providers",
            "field_names",
            "risks",
        }
        if (
            type(value) is not dict
            or not required <= set(value)
            or set(value) - required - {"argument_previews"}
        ):
            raise ValueError("plugin_permission_display_invalid")
        try:
            plugin_apps = value["plugin_apps"]
            if type(plugin_apps) is not list or any(
                type(item) is not dict
                or set(item) != {"plugin_id", "plugin_name", "app_id", "app_name"}
                for item in plugin_apps
            ):
                raise ValueError
            return cls(
                **{
                    **value,
                    "plugin_apps": tuple(
                        (
                            item["plugin_id"],
                            item["plugin_name"],
                            item["app_id"],
                            item["app_name"],
                        )
                        for item in plugin_apps
                    ),
                    "argument_previews": tuple(value.get("argument_previews") or ()),
                }
            )
        except (TypeError, ValueError):
            raise ValueError("plugin_permission_display_invalid") from None

    def to_hud_dict(self) -> dict[str, object]:
        payload = {
            "plugin_apps": self.to_dict()["plugin_apps"],
            "effect_summary": self.effect_summary,
            "ordered_operations": list(self.ordered_operations),
            "audience": self.audience,
            "destination": self.destination,
            "crosses_providers": self.crosses_providers,
            "field_names": list(self.field_names),
            "risks": list(self.risks),
        }
        if self.argument_previews:
            payload["argument_previews"] = list(self.argument_previews)
        return payload


@dataclass(frozen=True, slots=True)
class PluginPermissionRequest:
    request_id: str
    episode_id: str
    prior_episode_id: str
    task_id: str
    plan_id: str
    account_key_hash: str
    plan_digest: str
    authority_digest: str
    display: PluginPermissionDisplay
    expires_monotonic: float
    revision: str

    def __post_init__(self) -> None:
        error = "plugin_permission_request_invalid"
        if type(self.request_id) is not str or _OPAQUE.fullmatch(self.request_id) is None:
            raise ValueError(error)
        object.__setattr__(self, "episode_id", _text(self.episode_id, error=error))
        object.__setattr__(
            self,
            "prior_episode_id",
            _text(self.prior_episode_id, error=error),
        )
        object.__setattr__(self, "task_id", _text(self.task_id, error=error))
        for field in (
            "plan_id",
            "account_key_hash",
            "plan_digest",
            "authority_digest",
            "revision",
        ):
            object.__setattr__(self, field, _digest(getattr(self, field), error=error))
        if type(self.display) is not PluginPermissionDisplay:
            raise ValueError(error)
        object.__setattr__(self, "expires_monotonic", _finite(self.expires_monotonic, error=error))

    def to_hud_dict(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "revision": self.revision,
            "display": self.display.to_hud_dict(),
        }


@dataclass(frozen=True, slots=True)
class PluginPermissionContinuationLineage:
    prior_episode_id: str
    task_id: str
    plan_id: str
    plan_digest: str
    revision: str
    status: str

    def __post_init__(self) -> None:
        error = "plugin_permission_lineage_invalid"
        object.__setattr__(
            self,
            "prior_episode_id",
            _text(self.prior_episode_id, error=error),
        )
        object.__setattr__(self, "task_id", _text(self.task_id, error=error))
        for field in ("plan_id", "plan_digest", "revision"):
            object.__setattr__(self, field, _digest(getattr(self, field), error=error))
        if self.status != "pending":
            raise ValueError(error)


@dataclass(frozen=True, slots=True)
class PluginPlanGrant:
    grant_id: str
    request_id: str
    episode_id: str
    plan_digest: str
    authority_digest: str
    revision: str

    def __post_init__(self) -> None:
        error = "plugin_plan_grant_invalid"
        for field in ("grant_id", "request_id"):
            value = getattr(self, field)
            if type(value) is not str or _OPAQUE.fullmatch(value) is None:
                raise ValueError(error)
        object.__setattr__(self, "episode_id", _text(self.episode_id, error=error))
        for field in ("plan_digest", "authority_digest", "revision"):
            object.__setattr__(self, field, _digest(getattr(self, field), error=error))


def _plan_from_dict(value: object) -> PluginOperationPlan:
    required = {
        "plan_id",
        "episode_id",
        "objective_sha256",
        "authority_digest",
        "steps",
    }
    if (
        type(value) is not dict
        or set(value) != required
        or type(value["steps"]) is not list
    ):
        raise ValueError("plugin_permission_state_invalid")
    try:
        steps = tuple(
            PluginOperationStep(
                step_id=item["step_id"],
                candidate_id=item["candidate_id"],
                arguments=item["arguments"],
                consumes_step_ids=tuple(item["consumes_step_ids"]),
            )
            for item in value["steps"]
            if type(item) is dict
            and set(item) == {"step_id", "candidate_id", "arguments", "consumes_step_ids"}
        )
        if len(steps) != len(value["steps"]):
            raise ValueError
        return PluginOperationPlan(
            plan_id=value["plan_id"],
            episode_id=value["episode_id"],
            objective_sha256=value["objective_sha256"],
            steps=steps,
            authority_digest=value["authority_digest"],
        )
    except (KeyError, TypeError, ValueError):
        raise ValueError("plugin_permission_state_invalid") from None


class PluginPermissionStore:
    def __init__(
        self,
        *,
        path: Path | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        wall_time: Callable[[], float] = time.time,
        random_bytes: Callable[[int], bytes] = os.urandom,
        authority_factory: AuthorityFactory,
        binding_resolver: BindingResolver,
        ttl_seconds: float = 300.0,
        max_document_bytes: int = _MAX_DOCUMENT_BYTES,
        max_records: int = _MAX_RECORDS,
    ) -> None:
        selected = Path(path or MCP_PLUGIN_PERMISSION_STATE_PATH)
        self.path = selected
        self._state = McpStateStore(
            state_root=selected.parent,
            document_path=selected,
            invalid_path_reason="plugin_permission_store_unavailable",
            max_document_bytes=max_document_bytes,
        )
        if (
            not callable(monotonic)
            or not callable(wall_time)
            or not callable(random_bytes)
            or not callable(binding_resolver)
        ):
            raise ValueError("plugin_permission_clock_invalid")
        if not callable(authority_factory):
            raise ValueError("plugin_permission_authority_invalid")
        self._monotonic = monotonic
        self._wall_time = wall_time
        self._random_bytes = random_bytes
        self._authority_factory = authority_factory
        self._binding_resolver = binding_resolver
        self._authority_issuer = _AuthorityIssuer()
        self._ttl_seconds = _finite(ttl_seconds, error="plugin_permission_clock_invalid")
        if self._ttl_seconds <= 0:
            raise ValueError("plugin_permission_clock_invalid")
        if type(max_records) is not int or max_records <= 0 or max_records > _MAX_RECORDS:
            raise ValueError("plugin_permission_limit_invalid")
        self._max_records = max_records
        self._clock_epoch = _random_hex(random_bytes, 32)

    @staticmethod
    def _empty() -> dict[str, object]:
        return {"schema_version": _SCHEMA_VERSION, "permission_requests": {}}

    @staticmethod
    def _fingerprint(record: Mapping[str, object]) -> str:
        immutable = {
            key: record[key]
            for key in (
                "request_id",
                "grant_id",
                "episode_id",
                "prior_episode_id",
                "task_id",
                "plan_id",
                "account_key_hash",
                "plan_digest",
                "authority_digest",
                "display",
                "binding_projection",
                "authority_preview",
                "plan",
                "expires_monotonic",
                "expires_at",
                "clock_epoch",
                "nonce",
                "grant_revision",
                "authorities",
            )
        }
        return sha256_hex(canonical_bytes(immutable))

    @staticmethod
    def _request_revision(record: Mapping[str, object]) -> str:
        return sha256_hex(
            canonical_bytes(
                {
                    "request_id": record["request_id"],
                    "fingerprint": record["fingerprint"],
                    "status": record["status"],
                }
            )
        )

    @staticmethod
    def _grant_revision(record: Mapping[str, object]) -> str:
        return sha256_hex(
            canonical_bytes(
                {
                    "request_id": record["request_id"],
                    "grant_id": record["grant_id"],
                    "nonce": record["nonce"],
                    "plan_digest": record["plan_digest"],
                    "account_key_hash": record["account_key_hash"],
                    "authorities": record["authorities"],
                    "status": "approved",
                }
            )
        )

    def _validate_record(self, record: object) -> None:
        if type(record) is not dict or set(record) != {
            "request_id",
            "grant_id",
            "episode_id",
            "prior_episode_id",
            "task_id",
            "plan_id",
            "account_key_hash",
            "plan_digest",
            "authority_digest",
            "display",
            "binding_projection",
            "authority_preview",
            "plan",
            "expires_monotonic",
            "expires_at",
            "clock_epoch",
            "nonce",
            "fingerprint",
            "revision",
            "grant_revision",
            "status",
            "authorities",
        }:
            raise ValueError("plugin_permission_state_invalid")
        if record["status"] not in {
            "pending",
            "approved",
            "consumed",
            "cancelled",
            "expired",
            "invalidated",
        }:
            raise ValueError("plugin_permission_state_invalid")
        if (
            type(record["request_id"]) is not str
            or _OPAQUE.fullmatch(record["request_id"]) is None
            or type(record["grant_id"]) is not str
            or _OPAQUE.fullmatch(record["grant_id"]) is None
            or type(record["clock_epoch"]) is not str
            or _OPAQUE.fullmatch(record["clock_epoch"]) is None
            or type(record["nonce"]) is not str
            or _OPAQUE.fullmatch(record["nonce"]) is None
        ):
            raise ValueError("plugin_permission_state_invalid")
        _text(record["episode_id"], error="plugin_permission_state_invalid")
        _text(record["prior_episode_id"], error="plugin_permission_state_invalid")
        _text(record["task_id"], error="plugin_permission_state_invalid")
        for field in (
            "plan_id",
            "account_key_hash",
            "plan_digest",
            "authority_digest",
            "fingerprint",
            "revision",
        ):
            _digest(record[field], error="plugin_permission_state_invalid")
        if record["grant_revision"]:
            _digest(record["grant_revision"], error="plugin_permission_state_invalid")
        _finite(record["expires_monotonic"], error="plugin_permission_state_invalid")
        _finite(record["expires_at"], error="plugin_permission_state_invalid")
        display = PluginPermissionDisplay.from_dict(record["display"])
        plan = _plan_from_dict(record["plan"])
        if record["prior_episode_id"] != record["episode_id"] or record["plan_id"] != plan.plan_id:
            raise ValueError("plugin_permission_state_invalid")
        projection = record["binding_projection"]
        if (
            type(projection) is not list
            or len(projection) != len(plan.steps)
            or any(
                type(item) is not dict
                or set(item)
                != {
                    "candidate_id",
                    "plugin_id",
                    "plugin_revision",
                    "app_id",
                    "plugin_name",
                    "app_name",
                    "connection_revision",
                    "capability",
                    "tool_name",
                    "tool_schema_sha256",
                    "title",
                    "risk_class",
                    "effect",
                }
                or any(type(value) is not str or not value for value in item.values())
                or item["risk_class"] not in {risk.value for risk in RiskClass}
                or item["effect"] not in _EFFECT_SUMMARIES
                for item in projection
            )
            or not _display_projection_matches(display, plan, projection)
        ):
            raise ValueError("plugin_permission_state_invalid")
        raw_preview = record["authority_preview"]
        if type(raw_preview) is not list:
            raise ValueError("plugin_permission_state_invalid")
        preview = tuple(_authority_from_dict(item) for item in raw_preview)
        if (
            any(
                _authority_to_dict(authority) != raw
                for authority, raw in zip(preview, raw_preview, strict=True)
            )
            or not _authority_bundle_matches(
                plan,
                record["account_key_hash"],
                projection,
                preview,
            )
            or _derived_display(plan, projection, preview) != display
        ):
            raise ValueError("plugin_permission_state_invalid")
        terminal_without_grant = {
            "pending",
            "cancelled",
            "expired",
            "invalidated",
            "consumed",
        }
        if record["status"] in terminal_without_grant:
            if record["authorities"] is not None or record["grant_revision"]:
                raise ValueError("plugin_permission_state_invalid")
            authorities: tuple[McpAuthoritySnapshot, ...] = ()
        else:
            raw_authorities = record["authorities"]
            if type(raw_authorities) is not list or not raw_authorities:
                raise ValueError("plugin_permission_state_invalid")
            authorities = tuple(_authority_from_dict(item) for item in raw_authorities)
            if (
                not record["grant_revision"]
                or record["grant_revision"] != self._grant_revision(record)
                or any(
                    _authority_to_dict(authority) != raw
                    for authority, raw in zip(authorities, raw_authorities, strict=True)
                )
            ):
                raise ValueError("plugin_permission_state_invalid")
        if (
            plugin_plan_digest(plan) != record["plan_digest"]
            or plan.authority_digest != record["authority_digest"]
            or (
                authorities
                and (
                    len(authorities) != len(plan.steps)
                    or len({authority.authority_id for authority in authorities})
                    != len(authorities)
                    or any(
                        authority.episode_id != record["episode_id"]
                        or authority.plan_digest != record["plan_digest"]
                        or authority.account_key_hash != record["account_key_hash"]
                        or authority.candidate_id != step.candidate_id
                        or authority.plan_step_id != step.step_id
                        or authority.arguments_sha256
                        != sha256_hex(canonical_bytes(_plain_json(step.arguments)))
                        for authority, step in zip(authorities, plan.steps, strict=True)
                    )
                )
            )
            or self._fingerprint(record) != record["fingerprint"]
            or self._request_revision(record) != record["revision"]
        ):
            raise ValueError("plugin_permission_state_invalid")

    @staticmethod
    def _legacy_records(document: Mapping[str, object]) -> dict[str, object]:
        """Return the record mapping from a retired (versions 1-3) layout.

        Retired layouts stored records under one sibling key next to
        ``schema_version``.  The retired key name is matched by position so the
        retired literal never re-enters the source tree.
        """
        if len(document) != 2 or "schema_version" not in document:
            raise ValueError("plugin_permission_state_invalid")
        records: object = None
        for key, value in document.items():
            if key != "schema_version":
                records = value
        if type(records) is not dict:
            raise ValueError("plugin_permission_state_invalid")
        return records

    def _read_locked(self) -> dict[str, object]:
        raw = self._state.read_bytes()
        if raw is None:
            return self._empty()
        try:
            document = json.loads(raw.decode("utf-8"))
            if type(document) is not dict:
                raise ValueError
            version = document.get("schema_version")
            if version == 1:
                self._legacy_records(document)
                return self._empty()
            if version == 2:
                document = self._migrate_v2(document)
            elif version == 3:
                document = self._migrate_v3(document)
            if (
                type(document) is not dict
                or set(document) != {"schema_version", "permission_requests"}
                or document["schema_version"] != _SCHEMA_VERSION
                or type(document["permission_requests"]) is not dict
                or len(document["permission_requests"]) > self._max_records
            ):
                raise ValueError
            for request_id, record in document["permission_requests"].items():
                if request_id != record.get("request_id"):
                    raise ValueError
                self._validate_record(record)
            return document
        except (AttributeError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            raise ValueError("plugin_permission_state_invalid") from None

    def _migrate_v2(self, document: dict[str, object]) -> dict[str, object]:
        migrated = {"schema_version": _SCHEMA_VERSION, "permission_requests": {}}
        legacy = self._legacy_records(document)
        if len(legacy) > self._max_records:
            raise ValueError("plugin_permission_state_invalid")
        for request_id, raw in legacy.items():
            if type(raw) is not dict or request_id != raw.get("request_id"):
                raise ValueError("plugin_permission_state_invalid")
            record = dict(raw)
            plan = _plan_from_dict(record.get("plan"))
            record["prior_episode_id"] = record.get("episode_id")
            record["task_id"] = record.get("episode_id")
            record["plan_id"] = plan.plan_id
            record["fingerprint"] = self._fingerprint(record)
            record["revision"] = self._request_revision(record)
            self._validate_record(record)
            migrated["permission_requests"][request_id] = record
        return migrated

    def _migrate_v3(self, document: dict[str, object]) -> dict[str, object]:
        records = self._legacy_records(document)
        if len(records) > self._max_records:
            raise ValueError("plugin_permission_state_invalid")
        return {"schema_version": _SCHEMA_VERSION, "permission_requests": records}

    def _write_locked(self, document: dict[str, object]) -> None:
        payload = canonical_bytes(document)
        if len(payload) > self._state.max_document_bytes:
            raise ValueError("plugin_permission_store_limit_exceeded")
        self._state.write_bytes(payload)

    def _request(self, record: Mapping[str, object]) -> PluginPermissionRequest:
        return PluginPermissionRequest(
            request_id=record["request_id"],
            episode_id=record["episode_id"],
            prior_episode_id=record["prior_episode_id"],
            task_id=record["task_id"],
            plan_id=record["plan_id"],
            account_key_hash=record["account_key_hash"],
            plan_digest=record["plan_digest"],
            authority_digest=record["authority_digest"],
            display=PluginPermissionDisplay.from_dict(record["display"]),
            expires_monotonic=record["expires_monotonic"],
            revision=record["revision"],
        )

    @staticmethod
    def _grant(record: Mapping[str, object]) -> PluginPlanGrant:
        return PluginPlanGrant(
            grant_id=record["grant_id"],
            request_id=record["request_id"],
            episode_id=record["episode_id"],
            plan_digest=record["plan_digest"],
            authority_digest=record["authority_digest"],
            revision=record["grant_revision"],
        )

    def _resolve_projection(
        self,
        plan: PluginOperationPlan,
        account_key_hash: str,
    ) -> list[dict[str, str]]:
        try:
            bindings = self._binding_resolver(plan, account_key_hash)
        except (LookupError, OSError, RuntimeError, TypeError, ValueError):
            raise ValueError("plugin_permission_consent_mismatch") from None
        return _binding_projection(plan, bindings, account_key_hash)

    @_lifecycle_mutation
    def issue(
        self,
        plan: PluginOperationPlan,
        *,
        account_key_hash: str,
        prior_episode_id: str,
        task_id: str,
    ) -> PluginPermissionRequest:
        unsupported_prior = getattr(plan, "prior_step_ids", ())
        if type(plan) is not PluginOperationPlan or unsupported_prior != ():
            raise ValueError("plugin_permission_request_invalid")
        account = _digest(account_key_hash, error="plugin_permission_request_invalid")
        prior_episode = _text(prior_episode_id, error="plugin_permission_request_invalid")
        task = _text(task_id, error="plugin_permission_request_invalid")
        if prior_episode != plan.episode_id:
            raise ValueError("plugin_permission_request_invalid")
        plan_digest = plugin_plan_digest(plan)
        projection = self._resolve_projection(plan, account)
        request_id = _random_hex(self._random_bytes, 24)
        grant_id = _random_hex(self._random_bytes, 24)
        nonce = _random_hex(self._random_bytes, 32)
        preview = _authority_bundle(self._authority_factory(plan, account, grant_id))
        if not _authority_bundle_matches(plan, account, projection, preview):
            raise ValueError("plugin_permission_authority_invalid")
        display = _derived_display(plan, projection, preview)
        now_monotonic = _finite(self._monotonic(), error="plugin_permission_clock_invalid")
        now_wall = _finite(self._wall_time(), error="plugin_permission_clock_invalid")
        record: dict[str, object] = {
            "request_id": request_id,
            "grant_id": grant_id,
            "episode_id": plan.episode_id,
            "prior_episode_id": prior_episode,
            "task_id": task,
            "plan_id": plan.plan_id,
            "account_key_hash": account,
            "plan_digest": plan_digest,
            "authority_digest": plan.authority_digest,
            "display": display.to_dict(),
            "binding_projection": projection,
            "authority_preview": [_authority_to_dict(authority) for authority in preview],
            "plan": _plan_document(plan),
            "expires_monotonic": now_monotonic + self._ttl_seconds,
            "expires_at": now_wall + self._ttl_seconds,
            "clock_epoch": self._clock_epoch,
            "nonce": nonce,
            "fingerprint": "",
            "revision": "",
            "grant_revision": "",
            "status": "pending",
            "authorities": None,
        }
        record["fingerprint"] = self._fingerprint(record)
        record["revision"] = self._request_revision(record)
        self._validate_record(record)
        with self._state.locked():
            document = self._read_locked()
            if len(document["permission_requests"]) >= self._max_records:
                raise ValueError("plugin_permission_store_limit_exceeded")
            if request_id in document["permission_requests"]:
                raise ValueError("plugin_permission_randomness_invalid")
            document["permission_requests"][request_id] = record
            self._write_locked(document)
        return self._request(record)

    def resolve_continuation_lineage(
        self,
        request_id: str,
        expected_revision: str,
    ) -> PluginPermissionContinuationLineage:
        """Read and revalidate pending lineage without changing permission state."""
        with self._state.locked():
            document = self._read_locked()
            record = self._load_for_transition(document, request_id, expected_revision)
            status = record["status"]
            if status == "expired":
                raise ValueError("plugin_permission_expired")
            if status == "cancelled":
                raise ValueError("plugin_permission_cancelled")
            if status == "invalidated":
                raise ValueError("plugin_permission_invalidated")
            if status in {"approved", "consumed"}:
                raise ValueError("plugin_permission_replayed")
            if status != "pending":
                raise ValueError("plugin_permission_already_decided")
            if self._record_expired(record):
                raise ValueError("plugin_permission_expired")
            plan = _plan_from_dict(record["plan"])
            if (
                self._resolve_projection(plan, record["account_key_hash"])
                != record["binding_projection"]
            ):
                raise ValueError("plugin_permission_invalidated")
            return PluginPermissionContinuationLineage(
                prior_episode_id=record["prior_episode_id"],
                task_id=record["task_id"],
                plan_id=record["plan_id"],
                plan_digest=record["plan_digest"],
                revision=record["revision"],
                status="pending",
            )

    def _load_for_transition(
        self,
        document: dict[str, object],
        request_id: str,
        expected_revision: str,
    ) -> dict[str, object]:
        if type(request_id) is not str or _OPAQUE.fullmatch(request_id) is None:
            raise ValueError("plugin_permission_request_invalid")
        record = document["permission_requests"].get(request_id)
        if record is None:
            raise ValueError("plugin_permission_request_not_found")
        self._validate_record(record)
        if record["revision"] != expected_revision:
            raise ValueError("plugin_permission_revision_conflict")
        return record

    def _expire_if_needed(self, document: dict[str, object], record: dict[str, object]) -> None:
        if record["status"] != "pending":
            return
        if not self._record_expired(record):
            return
        record["status"] = "expired"
        record["revision"] = self._request_revision(record)
        self._write_locked(document)
        raise ValueError("plugin_permission_expired")

    def _record_expired(self, record: Mapping[str, object]) -> bool:
        """Invalidate process-bound grants when the authority clock epoch changes."""
        if record["clock_epoch"] != self._clock_epoch:
            return True
        now = _finite(self._monotonic(), error="plugin_permission_clock_invalid")
        return now > record["expires_monotonic"]

    @staticmethod
    def _consent_matches(
        display: PluginPermissionDisplay,
        plan: PluginOperationPlan,
        authorities: AuthorityBundle,
        projection: list[dict[str, str]],
    ) -> bool:
        return _authority_bundle_matches(
            plan,
            authorities[0].account_key_hash,
            projection,
            authorities,
        ) and (
            display == _derived_display(plan, projection, authorities)
            or (
                not display.argument_previews
                and {
                    key: value
                    for key, value in display.to_dict().items()
                    if key != "argument_previews"
                }
                == {
                    key: value
                    for key, value in _derived_display(plan, projection, authorities)
                    .to_dict()
                    .items()
                    if key != "argument_previews"
                }
            )
        )

    @_lifecycle_mutation
    def approve(self, request_id: str, *, expected_revision: str) -> PluginPlanGrant:
        with self._state.locked():
            document = self._read_locked()
            record = self._load_for_transition(document, request_id, expected_revision)
            self._expire_if_needed(document, record)
            if record["status"] == "invalidated":
                raise ValueError("plugin_permission_invalidated")
            if record["status"] != "pending":
                raise ValueError("plugin_permission_already_decided")
            plan = _plan_from_dict(record["plan"])
            if (
                self._resolve_projection(plan, record["account_key_hash"])
                != record["binding_projection"]
            ):
                raise ValueError("plugin_permission_consent_mismatch")
            authorities = _authority_bundle(
                self._authority_factory(
                    plan,
                    record["account_key_hash"],
                    record["grant_id"],
                )
            )
            if [_authority_to_dict(authority) for authority in authorities] != record[
                "authority_preview"
            ] or not _authority_bundle_matches(
                plan,
                record["account_key_hash"],
                record["binding_projection"],
                authorities,
            ):
                raise ValueError("plugin_permission_authority_invalid")
            if not self._consent_matches(
                PluginPermissionDisplay.from_dict(record["display"]),
                plan,
                authorities,
                record["binding_projection"],
            ):
                raise ValueError("plugin_permission_consent_mismatch")
            authority_documents = [_authority_to_dict(authority) for authority in authorities]
            record["status"] = "approved"
            record["authorities"] = authority_documents
            record["grant_revision"] = self._grant_revision(record)
            record["fingerprint"] = self._fingerprint(record)
            record["revision"] = self._request_revision(record)
            self._write_locked(document)
            return self._grant(record)

    @_lifecycle_mutation
    def authority(self, grant_id: str) -> McpAuthoritySnapshot:
        if type(grant_id) is not str or _OPAQUE.fullmatch(grant_id) is None:
            raise ValueError("plugin_permission_grant_invalid")
        with self._state.locked():
            document = self._read_locked()
            matches = [
                record
                for record in document["permission_requests"].values()
                if record["grant_id"] == grant_id and record["status"] == "approved"
            ]
            if len(matches) != 1:
                raise ValueError("plugin_permission_grant_not_found")
            record = matches[0]
            if self._record_expired(record):
                if record["status"] == "approved":
                    record["status"] = "expired"
                    record["authorities"] = None
                    record["grant_revision"] = ""
                    record["fingerprint"] = self._fingerprint(record)
                    record["revision"] = self._request_revision(record)
                    self._write_locked(document)
                raise ValueError("plugin_permission_expired")
            authorities = tuple(_authority_from_dict(value) for value in record["authorities"])
            if len(authorities) != 1:
                raise ValueError("plugin_permission_grant_bundle_required")
            return authorities[0]

    def plan_for_grant(self, grant_id: str) -> PluginOperationPlan:
        if type(grant_id) is not str or _OPAQUE.fullmatch(grant_id) is None:
            raise ValueError("plugin_permission_grant_invalid")
        with self._state.locked():
            document = self._read_locked()
            matches = [
                record
                for record in document["permission_requests"].values()
                if record["grant_id"] == grant_id
                and record["status"] in {"approved", "consumed"}
            ]
            if len(matches) != 1:
                raise ValueError("plugin_permission_grant_not_found")
            return _plan_from_dict(matches[0]["plan"])

    @_lifecycle_mutation
    def revoke_grant(self, grant_id: str) -> bool:
        """Invalidate an approved-but-not-consumed grant after execution setup fails."""
        if type(grant_id) is not str or _OPAQUE.fullmatch(grant_id) is None:
            raise ValueError("plugin_permission_grant_invalid")
        with self._state.locked():
            document = self._read_locked()
            matches = [
                record
                for record in document["permission_requests"].values()
                if record["grant_id"] == grant_id
            ]
            if len(matches) != 1:
                raise ValueError("plugin_permission_grant_not_found")
            record = matches[0]
            if record["status"] == "consumed":
                return False
            if record["status"] != "approved":
                return False
            record["status"] = "invalidated"
            record["authorities"] = None
            record["grant_revision"] = ""
            record["fingerprint"] = self._fingerprint(record)
            record["revision"] = self._request_revision(record)
            self._write_locked(document)
            return True

    @_lifecycle_mutation
    def consume(
        self,
        grant_id: str,
        *,
        plan: PluginOperationPlan,
        authority_digest: str,
        account_key_hash: str,
    ) -> AuthorityBundle:
        digest = plugin_plan_digest(plan)
        authority_value = _digest(authority_digest, error="plugin_permission_binding_invalid")
        account = _digest(account_key_hash, error="plugin_permission_binding_invalid")
        with self._state.locked():
            document = self._read_locked()
            matches = [
                record for record in document["permission_requests"].values() if record["grant_id"] == grant_id
            ]
            if len(matches) != 1:
                raise ValueError("plugin_permission_grant_not_found")
            record = matches[0]
            if record["status"] == "consumed":
                raise ValueError("plugin_permission_replayed")
            if record["status"] != "approved":
                raise ValueError("plugin_permission_grant_invalid")
            if self._record_expired(record):
                record["status"] = "expired"
                record["authorities"] = None
                record["grant_revision"] = ""
                record["fingerprint"] = self._fingerprint(record)
                record["revision"] = self._request_revision(record)
                self._write_locked(document)
                raise ValueError("plugin_permission_expired")
            if (
                record["plan_digest"] != digest
                or record["authority_digest"] != authority_value
                or record["account_key_hash"] != account
                or plan.episode_id != record["episode_id"]
            ):
                raise ValueError("plugin_permission_binding_invalid")
            authorities = tuple(_authority_from_dict(value) for value in record["authorities"])
            issued = tuple(self._authority_issuer.issue(authority) for authority in authorities)
            record["status"] = "consumed"
            record["authorities"] = None
            record["grant_revision"] = ""
            record["fingerprint"] = self._fingerprint(record)
            record["revision"] = self._request_revision(record)
            self._write_locked(document)
            return issued

    @_lifecycle_mutation
    def cancel(self, request_id: str, *, expected_revision: str) -> None:
        with self._state.locked():
            document = self._read_locked()
            record = self._load_for_transition(document, request_id, expected_revision)
            self._expire_if_needed(document, record)
            if record["status"] != "pending":
                raise ValueError("plugin_permission_already_decided")
            record["status"] = "cancelled"
            record["revision"] = self._request_revision(record)
            self._write_locked(document)

    @_lifecycle_mutation
    def invalidate_plugin(self, plugin_id: str) -> int:
        plugin = _text(plugin_id, error="plugin_permission_plugin_invalid")
        with self._state.locked():
            document = self._read_locked()
            changed = 0
            for record in document["permission_requests"].values():
                plugin_ids = {
                    identity["plugin_id"] for identity in record["display"]["plugin_apps"]
                }
                if plugin in plugin_ids and record["status"] in {
                    "pending",
                    "approved",
                }:
                    record["status"] = "invalidated"
                    record["authorities"] = None
                    record["grant_revision"] = ""
                    record["fingerprint"] = self._fingerprint(record)
                    record["revision"] = self._request_revision(record)
                    changed += 1
            if changed:
                self._write_locked(document)
            return changed

    def checkpoint_plugin(self, plugin_id: str) -> dict[str, object]:
        """Seal the exact permission records invalidated by a connection cutover."""

        plugin = _text(plugin_id, error="plugin_permission_plugin_invalid")
        with self._state.locked():
            document = self._read_locked()
            records = {
                request_id: record
                for request_id, record in document["permission_requests"].items()
                if plugin
                in {
                    identity["plugin_id"]
                    for identity in record["display"]["plugin_apps"]
                }
            }
        material = {
            "plugin_id": plugin,
            "records": json.loads(canonical_bytes(records).decode("utf-8")),
        }
        return {
            **material,
            "revision": sha256_hex(
                canonical_bytes(
                    {
                        "domain": "omega.plugin-permission-update-recovery.v1",
                        "material": material,
                    }
                )
            ),
        }

    def _plugin_recovery(self, value: object) -> dict[str, object]:
        if type(value) is not dict or set(value) != {
            "plugin_id",
            "records",
            "revision",
        }:
            raise ValueError("plugin_permission_state_invalid")
        plugin = _text(
            value["plugin_id"],
            error="plugin_permission_plugin_invalid",
        )
        records = value["records"]
        if type(records) is not dict:
            raise ValueError("plugin_permission_state_invalid")
        for request_id, record in records.items():
            if type(request_id) is not str:
                raise ValueError("plugin_permission_state_invalid")
            self._validate_record(record)
            if request_id != record["request_id"] or plugin not in {
                identity["plugin_id"]
                for identity in record["display"]["plugin_apps"]
            }:
                raise ValueError("plugin_permission_state_invalid")
        material = {"plugin_id": plugin, "records": records}
        revision = sha256_hex(
            canonical_bytes(
                {
                    "domain": "omega.plugin-permission-update-recovery.v1",
                    "material": material,
                }
            )
        )
        if value["revision"] != revision:
            raise ValueError("plugin_permission_state_invalid")
        return {**material, "revision": revision}

    @_lifecycle_mutation
    def restore_plugin_checkpoint(self, recovery: object) -> None:
        checked = self._plugin_recovery(recovery)
        plugin = str(checked["plugin_id"])
        expected = checked["records"]
        assert type(expected) is dict
        with self._state.locked():
            document = self._read_locked()
            retained = {
                request_id: record
                for request_id, record in document["permission_requests"].items()
                if plugin
                not in {
                    identity["plugin_id"]
                    for identity in record["display"]["plugin_apps"]
                }
            }
            retained.update(json.loads(canonical_bytes(expected).decode("utf-8")))
            document["permission_requests"] = retained
            self._write_locked(document)

    @_lifecycle_mutation
    def pending(self) -> tuple[PluginPermissionRequest, ...]:
        with self._state.locked():
            document = self._read_locked()
            changed = False
            result: list[PluginPermissionRequest] = []
            for record in document["permission_requests"].values():
                if record["status"] != "pending":
                    continue
                if self._record_expired(record):
                    record["status"] = "expired"
                    record["revision"] = self._request_revision(record)
                    changed = True
                else:
                    result.append(self._request(record))
            if changed:
                self._write_locked(document)
        return tuple(sorted(result, key=lambda item: item.request_id))


def get_default_plugin_permission_store() -> PluginPermissionStore:
    """Return the permission store owned by Omega's process plugin composition."""

    from mcp_platform.plugin_lifecycle import get_default_plugin_lifecycle_service

    store = get_default_plugin_lifecycle_service().permission_store
    if type(store) is not PluginPermissionStore:
        raise RuntimeError("plugin_permission_store_unavailable")
    return store


__all__ = [
    "PluginPermissionContinuationLineage",
    "PluginPermissionDisplay",
    "PluginPermissionRequest",
    "PluginPermissionStore",
    "PluginPlanGrant",
    "get_default_plugin_permission_store",
]
