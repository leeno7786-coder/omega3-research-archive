"""Closed content-free plugin registry record contracts.

These immutable records are the public identity seam between the plugin
registry implementation and Omega's MCP platform.  Storage, staging, and
mutation live in ``plugin_platform.registry``; this module owns only the
closed data shapes and their validators.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from functools import total_ordering
from pathlib import PureWindowsPath
from typing import Any

from mcp_platform.canonical import canonical_bytes
from plugin_platform.contracts import PluginKind


class PluginRegistryError(RuntimeError):
    """A stable, content-free plugin registry or staging failure."""


_RECORD_FIELDS = frozenset(
    {
        "plugin_id",
        "publisher_id",
        "kind",
        "selected_version",
        "enabled",
        "status",
        "blocker",
        "versions",
        "app_binding",
    }
)
_LEGACY_RECORD_FIELDS = _RECORD_FIELDS - {"app_binding"}
_APP_BINDING_FIELDS = frozenset(
    {"required_app_ids", "optional_app_ids", "dependency_receipt_id", "revision"}
)
_VERSION_FIELDS = frozenset(
    {
        "plugin_version",
        "signing_key_id",
        "manifest_sha256",
        "package_sha256",
        "state",
    }
)
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_IDENTITY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*\Z")
_SEMVER_RE = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?\Z"
)
_BLOCKER_RE = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)
_MAX_APP_BINDING_IDS = 128


@total_ordering
@dataclass(frozen=True)
class _SemVer:
    major: int
    minor: int
    patch: int
    prerelease: tuple[int | str, ...] | None

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, _SemVer):
            return NotImplemented
        left_core = (self.major, self.minor, self.patch)
        right_core = (other.major, other.minor, other.patch)
        if left_core != right_core:
            return left_core < right_core
        if self.prerelease is None:
            return False
        if other.prerelease is None:
            return True
        for left, right in zip(self.prerelease, other.prerelease, strict=False):
            if left == right:
                continue
            if isinstance(left, int):
                return not isinstance(right, int) or left < right
            if isinstance(right, int):
                return False
            return left < right
        return len(self.prerelease) < len(other.prerelease)


def _parse_semver(value: str) -> _SemVer:
    _semver(value)
    precedence, _separator, _build = value.partition("+")
    core, separator, prerelease = precedence.partition("-")
    major, minor, patch = (int(item) for item in core.split("."))
    identifiers: tuple[int | str, ...] | None = None
    if separator:
        identifiers = tuple(int(item) if item.isdigit() else item for item in prerelease.split("."))
    return _SemVer(major, minor, patch, identifiers)


@dataclass(frozen=True, slots=True)
class PluginAppBinding:
    """Exact app ownership closed at signed dependency installation time."""

    required_app_ids: tuple[str, ...]
    optional_app_ids: tuple[str, ...]
    dependency_receipt_id: str
    revision: str

    def __post_init__(self) -> None:
        try:
            if type(self.required_app_ids) is not tuple or type(self.optional_app_ids) is not tuple:
                raise ValueError
            required = tuple(_identity(item) for item in self.required_app_ids)
            optional = tuple(_identity(item) for item in self.optional_app_ids)
            if (
                len(required) + len(optional) > _MAX_APP_BINDING_IDS
                or required != tuple(sorted(required))
                or optional != tuple(sorted(optional))
                or len(required) != len(set(required))
                or len(optional) != len(set(optional))
                or set(required).intersection(optional)
            ):
                raise ValueError
            receipt = _identity(self.dependency_receipt_id)
            expected_revision = hashlib.sha256(
                canonical_bytes(
                    {
                        "required_app_ids": list(required),
                        "optional_app_ids": list(optional),
                        "dependency_receipt_id": receipt,
                    }
                )
            ).hexdigest()
            if _digest(self.revision) != expected_revision:
                raise ValueError
        except Exception:
            raise ValueError("plugin app binding is invalid") from None
        object.__setattr__(self, "required_app_ids", required)
        object.__setattr__(self, "optional_app_ids", optional)
        object.__setattr__(self, "dependency_receipt_id", receipt)
        object.__setattr__(self, "revision", expected_revision)

    @classmethod
    def from_dict(cls, raw: Any) -> PluginAppBinding:
        data = _exact_object(raw, _APP_BINDING_FIELDS)
        if type(data["required_app_ids"]) is not list or type(data["optional_app_ids"]) is not list:
            raise ValueError("plugin app binding is invalid")
        return cls(
            required_app_ids=tuple(data["required_app_ids"]),
            optional_app_ids=tuple(data["optional_app_ids"]),
            dependency_receipt_id=data["dependency_receipt_id"],
            revision=data["revision"],
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "required_app_ids": list(self.required_app_ids),
            "optional_app_ids": list(self.optional_app_ids),
            "dependency_receipt_id": self.dependency_receipt_id,
            "revision": self.revision,
        }


@dataclass(frozen=True)
class InstalledPluginVersionRecord:
    """Content-free immutable identity for one published plugin version."""

    plugin_version: str
    signing_key_id: str
    manifest_sha256: str
    package_sha256: str
    state: str

    @classmethod
    def from_dict(cls, raw: Any) -> InstalledPluginVersionRecord:
        data = _exact_object(raw, _VERSION_FIELDS)
        state = data["state"]
        if state not in {"selected", "candidate"}:
            raise ValueError("invalid version state")
        return cls(
            plugin_version=_semver(data["plugin_version"]),
            signing_key_id=_identity(data["signing_key_id"]),
            manifest_sha256=_digest(data["manifest_sha256"]),
            package_sha256=_digest(data["package_sha256"]),
            state=state,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "plugin_version": self.plugin_version,
            "signing_key_id": self.signing_key_id,
            "manifest_sha256": self.manifest_sha256,
            "package_sha256": self.package_sha256,
            "state": self.state,
        }


@dataclass(frozen=True)
class InstalledPluginRecord:
    """Closed durable state for one plugin identity and all retained versions."""

    plugin_id: str
    publisher_id: str
    kind: str
    selected_version: str
    enabled: bool
    status: str
    blocker: str | None
    versions: tuple[InstalledPluginVersionRecord, ...]
    app_binding: PluginAppBinding | None = None

    @classmethod
    def from_dict(
        cls,
        raw: Any,
        *,
        registry_key: str,
        legacy: bool = False,
    ) -> InstalledPluginRecord:
        data = _exact_object(raw, _LEGACY_RECORD_FIELDS if legacy else _RECORD_FIELDS)
        plugin_id = _identity(data["plugin_id"])
        if registry_key != plugin_id:
            raise ValueError("registry key mismatch")
        publisher_id = _identity(data["publisher_id"])
        try:
            kind = PluginKind(data["kind"]).value
        except (TypeError, ValueError):
            raise ValueError("invalid plugin kind") from None
        selected_version = _semver(data["selected_version"])
        if type(data["enabled"]) is not bool:
            raise ValueError("invalid enabled state")
        if data["status"] != "installed":
            raise ValueError("invalid plugin status")
        blocker = data["blocker"]
        if blocker is not None:
            blocker = _blocker(blocker)
        if type(data["versions"]) is not list or not data["versions"]:
            raise ValueError("invalid plugin versions")
        versions = tuple(InstalledPluginVersionRecord.from_dict(item) for item in data["versions"])
        _validate_versions(versions, selected_version)
        app_binding = (
            None
            if legacy or data["app_binding"] is None
            else PluginAppBinding.from_dict(data["app_binding"])
        )
        return cls(
            plugin_id=plugin_id,
            publisher_id=publisher_id,
            kind=kind,
            selected_version=selected_version,
            enabled=data["enabled"],
            status="installed",
            blocker=blocker,
            versions=versions,
            app_binding=app_binding,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "plugin_id": self.plugin_id,
            "publisher_id": self.publisher_id,
            "kind": self.kind,
            "selected_version": self.selected_version,
            "enabled": self.enabled,
            "status": self.status,
            "blocker": self.blocker,
            "versions": [version.to_dict() for version in self.versions],
            "app_binding": None if self.app_binding is None else self.app_binding.to_dict(),
        }


@dataclass(frozen=True)
class PluginRegistrySnapshot:
    """Immutable sorted records and the hash of their canonical document."""

    records: tuple[InstalledPluginRecord, ...]
    revision: str


def _validate_versions(
    versions: tuple[InstalledPluginVersionRecord, ...], selected_version: str
) -> None:
    parsed = tuple(_parse_semver(item.plugin_version) for item in versions)
    if tuple(sorted(parsed)) != parsed or len(set(parsed)) != len(parsed):
        raise ValueError("plugin versions are invalid")
    if len({item.plugin_version for item in versions}) != len(versions):
        raise ValueError("plugin versions are invalid")
    selected = tuple(item for item in versions if item.state == "selected")
    if len(selected) != 1 or selected[0].plugin_version != selected_version:
        raise ValueError("selected version is invalid")


def _exact_object(raw: Any, fields: frozenset[str]) -> dict[str, Any]:
    if type(raw) is not dict or set(raw) != fields or any(type(key) is not str for key in raw):
        raise ValueError("invalid exact object")
    return raw


def _identity(value: Any) -> str:
    if (
        type(value) is not str
        or _IDENTITY_RE.fullmatch(value) is None
        or value.endswith(".")
        or value.partition(".")[0].upper() in _WINDOWS_RESERVED_NAMES
    ):
        raise ValueError("invalid plugin identity")
    windows_path = PureWindowsPath(value)
    if (
        windows_path.is_absolute()
        or windows_path.drive
        or windows_path.root
        or value in {".", ".."}
    ):
        raise ValueError("invalid plugin identity")
    return value


def _semver(value: Any) -> str:
    if type(value) is not str or _SEMVER_RE.fullmatch(value) is None:
        raise ValueError("invalid plugin version")
    return value


def _digest(value: Any) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        raise ValueError("invalid digest")
    return value


def _blocker(value: Any) -> str:
    if type(value) is not str or _BLOCKER_RE.fullmatch(value) is None:
        raise PluginRegistryError("invalid_blocker_state")
    return value
