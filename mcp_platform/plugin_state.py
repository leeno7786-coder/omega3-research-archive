"""Immutable provider-neutral plugin lifecycle state contracts."""

from __future__ import annotations

import re
from dataclasses import InitVar, dataclass
from enum import StrEnum
from typing import Any


_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_ERROR = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")


class PluginLifecycleState(StrEnum):
    AVAILABLE = "available"
    REVIEW_REQUIRED = "review_required"
    INSTALLING = "installing"
    INSTALLED = "installed"
    SETUP_REQUIRED = "setup_required"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    OFFLINE = "offline"
    UPDATE_AVAILABLE = "update_available"
    UPDATE_REQUIRED = "update_required"
    PERMISSION_CHANGED = "permission_changed"
    REVOCATION_PENDING = "revocation_pending"
    BLOCKED = "blocked"
    INCOMPATIBLE = "incompatible"


_APP_STATES = frozenset(
    {
        PluginLifecycleState.SETUP_REQUIRED,
        PluginLifecycleState.CONNECTING,
        PluginLifecycleState.CONNECTED,
        PluginLifecycleState.OFFLINE,
        PluginLifecycleState.PERMISSION_CHANGED,
        PluginLifecycleState.REVOCATION_PENDING,
        PluginLifecycleState.BLOCKED,
        PluginLifecycleState.INCOMPATIBLE,
    }
)
_ERROR_BY_STATE = {
    PluginLifecycleState.SETUP_REQUIRED: "plugin_setup_required",
    PluginLifecycleState.OFFLINE: "plugin_offline",
    PluginLifecycleState.UPDATE_REQUIRED: "plugin_update_required",
    PluginLifecycleState.PERMISSION_CHANGED: "plugin_permission_changed",
    PluginLifecycleState.REVOCATION_PENDING: "plugin_revocation_pending",
    PluginLifecycleState.BLOCKED: "plugin_blocked",
    PluginLifecycleState.INCOMPATIBLE: "plugin_runtime_unsupported",
}
# A legacy-authority package is a mandatory migration signal: it outranks
# every ordinary connection state (including a live `connected` app) since
# the retired flat authority shape compiles zero runtime bindings regardless.
# An active revocation still takes precedence over the update prompt.
_REQUIRED_STATE_PRECEDENCE = (
    PluginLifecycleState.REVOCATION_PENDING,
    PluginLifecycleState.UPDATE_REQUIRED,
    PluginLifecycleState.PERMISSION_CHANGED,
    PluginLifecycleState.BLOCKED,
    PluginLifecycleState.INCOMPATIBLE,
    PluginLifecycleState.OFFLINE,
    PluginLifecycleState.CONNECTING,
    PluginLifecycleState.SETUP_REQUIRED,
    PluginLifecycleState.CONNECTED,
)
_REGISTRY_BLOCKER_AUTHORITY = object()
_LEGACY_AUTHORITY_MARKER = object()


def _identity(value: object) -> str:
    if type(value) is not str or _IDENTITY.fullmatch(value) is None:
        raise ValueError("plugin_lifecycle_snapshot_invalid")
    return value


def _revision(value: object, *, allow_empty: bool = False) -> str:
    if allow_empty and value == "":
        return ""
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise ValueError("plugin_lifecycle_snapshot_invalid")
    return value


def _error(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str or _ERROR.fullmatch(value) is None:
        raise ValueError("plugin_lifecycle_snapshot_invalid")
    return value


@dataclass(frozen=True, slots=True)
class PluginAppState:
    app_id: str
    required: bool
    state: PluginLifecycleState
    authority_revision: str
    error_code: str | None

    def __post_init__(self) -> None:
        try:
            app_id = _identity(self.app_id)
            if type(self.required) is not bool:
                raise ValueError
            state = PluginLifecycleState(self.state)
            if state not in _APP_STATES:
                raise ValueError
            authority = _revision(self.authority_revision, allow_empty=True)
            error = _error(self.error_code)
            if state is PluginLifecycleState.CONNECTED:
                if not authority or error is not None:
                    raise ValueError
            elif authority:
                raise ValueError
            if error != _ERROR_BY_STATE.get(state):
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("plugin_lifecycle_snapshot_invalid") from None
        object.__setattr__(self, "app_id", app_id)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "authority_revision", authority)
        object.__setattr__(self, "error_code", error)

    def to_dict(self) -> dict[str, object]:
        return {
            "app_id": self.app_id,
            "required": self.required,
            "state": self.state.value,
            "authority_revision": self.authority_revision,
            "error_code": self.error_code,
        }


@dataclass(frozen=True, slots=True)
class PluginLifecycleSnapshot:
    plugin_id: str
    state: PluginLifecycleState
    registry_revision: str
    package_version: str
    apps: tuple[PluginAppState, ...]
    next_required_app_id: str | None
    enabled: bool
    error_code: str | None
    _registry_blocker_authority: InitVar[object | None] = None
    _legacy_authority_marker: InitVar[object | None] = None

    def __post_init__(
        self,
        _registry_blocker_authority: object | None,
        _legacy_authority_marker: object | None,
    ) -> None:
        try:
            plugin_id = _identity(self.plugin_id)
            state = PluginLifecycleState(self.state)
            registry_revision = _revision(self.registry_revision)
            if (
                type(self.package_version) is not str
                or not self.package_version
                or self.package_version.strip() != self.package_version
            ):
                raise ValueError
            if type(self.apps) is not tuple or any(
                type(app) is not PluginAppState for app in self.apps
            ):
                raise ValueError
            if len({app.app_id for app in self.apps}) != len(self.apps):
                raise ValueError
            if type(self.enabled) is not bool:
                raise ValueError
            error = _error(self.error_code)
            incomplete = tuple(
                app
                for app in self.apps
                if app.required and app.state is not PluginLifecycleState.CONNECTED
            )
            expected_next = None if not incomplete else incomplete[0].app_id
            if self.next_required_app_id != expected_next:
                raise ValueError
            required_states = {app.state for app in self.apps if app.required}
            if _registry_blocker_authority is _REGISTRY_BLOCKER_AUTHORITY:
                required_states.add(PluginLifecycleState.BLOCKED)
            if _legacy_authority_marker is _LEGACY_AUTHORITY_MARKER:
                required_states.add(PluginLifecycleState.UPDATE_REQUIRED)
            derived = next(
                candidate
                for candidate in _REQUIRED_STATE_PRECEDENCE
                if candidate in required_states
                or (candidate is PluginLifecycleState.CONNECTED and not required_states)
            )
            if state is not derived or error != _ERROR_BY_STATE.get(derived):
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("plugin_lifecycle_snapshot_invalid") from None
        object.__setattr__(self, "plugin_id", plugin_id)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "registry_revision", registry_revision)
        object.__setattr__(self, "error_code", error)

    @classmethod
    def _from_registry_blocker(
        cls,
        *,
        plugin_id: str,
        registry_revision: str,
        package_version: str,
        apps: tuple[PluginAppState, ...],
        next_required_app_id: str | None,
        enabled: bool,
        legacy_authority: bool = False,
    ) -> PluginLifecycleSnapshot:
        required_states = {app.state for app in apps if app.required}
        required_states.add(PluginLifecycleState.BLOCKED)
        if legacy_authority:
            required_states.add(PluginLifecycleState.UPDATE_REQUIRED)
        state = next(
            candidate
            for candidate in _REQUIRED_STATE_PRECEDENCE
            if candidate in required_states
        )
        return cls(
            plugin_id=plugin_id,
            state=state,
            registry_revision=registry_revision,
            package_version=package_version,
            apps=apps,
            next_required_app_id=next_required_app_id,
            enabled=enabled,
            error_code=_ERROR_BY_STATE.get(state),
            _registry_blocker_authority=_REGISTRY_BLOCKER_AUTHORITY,
            _legacy_authority_marker=(_LEGACY_AUTHORITY_MARKER if legacy_authority else None),
        )

    @classmethod
    def _from_legacy_authority(
        cls,
        *,
        plugin_id: str,
        registry_revision: str,
        package_version: str,
        apps: tuple[PluginAppState, ...],
        next_required_app_id: str | None,
        enabled: bool,
    ) -> PluginLifecycleSnapshot:
        required_states = {app.state for app in apps if app.required}
        required_states.add(PluginLifecycleState.UPDATE_REQUIRED)
        state = next(
            candidate
            for candidate in _REQUIRED_STATE_PRECEDENCE
            if candidate in required_states
        )
        return cls(
            plugin_id=plugin_id,
            state=state,
            registry_revision=registry_revision,
            package_version=package_version,
            apps=apps,
            next_required_app_id=next_required_app_id,
            enabled=enabled,
            error_code=_ERROR_BY_STATE.get(state),
            _legacy_authority_marker=_LEGACY_AUTHORITY_MARKER,
        )

    def dependencies_ready(self, app_ids: tuple[str, ...]) -> bool:
        if type(app_ids) is not tuple:
            raise ValueError("plugin_capability_dependencies_invalid")
        requested = tuple(_identity(app_id) for app_id in app_ids)
        if len(requested) != len(set(requested)):
            raise ValueError("plugin_capability_dependencies_invalid")
        if not self.enabled or self.state is not PluginLifecycleState.CONNECTED:
            return False
        states = {app.app_id: app.state for app in self.apps}
        return all(states.get(app_id) is PluginLifecycleState.CONNECTED for app_id in requested)

    def to_dict(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "state": self.state.value,
            "registry_revision": self.registry_revision,
            "package_version": self.package_version,
            "apps": [app.to_dict() for app in self.apps],
            "next_required_app_id": self.next_required_app_id,
            "enabled": self.enabled,
            "error_code": self.error_code,
        }


@dataclass(frozen=True, slots=True)
class ConnectionStart:
    plugin_id: str
    app_id: str
    status: str
    request_id: str | None
    authorization_url: str | None
    expires_at: float | None
    authorization_flow: str | None
    user_code: str | None
    verification_uri: str | None
    poll_interval_seconds: int | None

    def __post_init__(self) -> None:
        try:
            _identity(self.plugin_id)
            _identity(self.app_id)
            if self.status not in {"connecting", "connected"}:
                raise ValueError
            for value in (self.request_id, self.authorization_url, self.authorization_flow):
                if value is not None and (type(value) is not str or not value):
                    raise ValueError
            if self.status == "connected" and any(
                value is not None
                for value in (
                    self.request_id,
                    self.authorization_url,
                    self.authorization_flow,
                    self.user_code,
                    self.verification_uri,
                    self.poll_interval_seconds,
                )
            ):
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("plugin_connection_start_invalid") from None

    def to_dict(self) -> dict[str, object]:
        return {
            "plugin_id": self.plugin_id,
            "app_id": self.app_id,
            "status": self.status,
            "request_id": self.request_id,
            "authorization_url": self.authorization_url,
            "expires_at": self.expires_at,
            "authorization_flow": self.authorization_flow,
            "user_code": self.user_code,
            "verification_uri": self.verification_uri,
            "poll_interval_seconds": self.poll_interval_seconds,
        }


__all__ = [
    "ConnectionStart",
    "PluginAppState",
    "PluginLifecycleSnapshot",
    "PluginLifecycleState",
]
