"""Durable, lifecycle-owned policy and result contracts for plugin updates.

The policy is deliberately small.  It is state, not a scheduler: the lifecycle
owner asks whether a check is due when it already owns a lifecycle operation.
No timer, browser callback, or cognition pressure is created here.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
import math
from pathlib import Path

from mcp_platform.canonical import canonical_bytes
from mcp_platform.state_store import McpStateStore

POLICY_SCHEMA_VERSION = 1
DEFAULT_UPDATE_INTERVAL_SECONDS = 24 * 60 * 60
MIN_UPDATE_INTERVAL_SECONDS = 60
MAX_UPDATE_INTERVAL_SECONDS = 31 * 24 * 60 * 60
MAX_UPDATE_BACKOFF_SECONDS = 7 * 24 * 60 * 60
SAFE_UPDATE_ERRORS = frozenset(
    {
        "plugin_catalog_unavailable",
        "plugin_catalog_timeout",
        "plugin_catalog_unreachable",
        "plugin_catalog_rejected",
        "plugin_catalog_redirect_rejected",
        "plugin_catalog_encoding_unsupported",
        "plugin_catalog_too_large",
        "plugin_update_failed",
        "plugin_update_not_available",
        "plugin_update_recovery_required",
        "plugin_install_review_changed",
        "plugin_lifecycle_closed",
    }
)


def _finite_time(value: object, *, allow_none: bool = True) -> float | None:
    if value is None and allow_none:
        return None
    if type(value) not in {int, float} or isinstance(value, bool):
        raise ValueError("plugin_update_policy_invalid")
    converted = float(value)
    if not math.isfinite(converted) or converted < 0:
        raise ValueError("plugin_update_policy_invalid")
    return converted


@dataclass(frozen=True, slots=True)
class PluginUpdatePolicy:
    """Strict persisted update policy for one Omega state root."""

    enabled: bool = True
    interval_seconds: int = DEFAULT_UPDATE_INTERVAL_SECONDS
    last_check_at: float | None = None
    next_check_at: float | None = None

    def __post_init__(self) -> None:
        if (
            type(self.enabled) is not bool
            or type(self.interval_seconds) is not int
            or not MIN_UPDATE_INTERVAL_SECONDS <= self.interval_seconds <= MAX_UPDATE_INTERVAL_SECONDS
        ):
            raise ValueError("plugin_update_policy_invalid")
        _finite_time(self.last_check_at)
        _finite_time(self.next_check_at)
        if (
            self.last_check_at is not None
            and self.next_check_at is not None
            and self.next_check_at < self.last_check_at
        ):
            raise ValueError("plugin_update_policy_invalid")

    @classmethod
    def default(cls) -> PluginUpdatePolicy:
        return cls()

    @classmethod
    def from_dict(cls, value: object) -> PluginUpdatePolicy:
        if type(value) is not dict or set(value) != {
            "schema_version",
            "enabled",
            "interval_seconds",
            "last_check_at",
            "next_check_at",
        } or value["schema_version"] != POLICY_SCHEMA_VERSION:
            raise ValueError("plugin_update_policy_invalid")
        try:
            return cls(
                enabled=value["enabled"],
                interval_seconds=value["interval_seconds"],
                last_check_at=_finite_time(value["last_check_at"]),
                next_check_at=_finite_time(value["next_check_at"]),
            )
        except (TypeError, ValueError):
            raise ValueError("plugin_update_policy_invalid") from None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": POLICY_SCHEMA_VERSION,
            "enabled": self.enabled,
            "interval_seconds": self.interval_seconds,
            "last_check_at": self.last_check_at,
            "next_check_at": self.next_check_at,
        }

    def due(self, now: float) -> bool:
        current = _finite_time(now, allow_none=False)
        assert current is not None
        return self.enabled and (
            self.next_check_at is None or current >= self.next_check_at
        )

    def after_success(self, now: float) -> PluginUpdatePolicy:
        current = _finite_time(now, allow_none=False)
        assert current is not None
        return replace(
            self,
            last_check_at=current,
            next_check_at=current + self.interval_seconds,
        )

    def after_failure(self, now: float) -> PluginUpdatePolicy:
        current = _finite_time(now, allow_none=False)
        assert current is not None
        prior_delay = self.interval_seconds
        if self.last_check_at is not None and self.next_check_at is not None:
            prior_delay = max(self.interval_seconds, self.next_check_at - self.last_check_at)
        delay = min(MAX_UPDATE_BACKOFF_SECONDS, max(self.interval_seconds, prior_delay * 2))
        return replace(self, last_check_at=current, next_check_at=current + delay)

    def after_deferred(self, now: float) -> PluginUpdatePolicy:
        """Advance a skipped deadline without recording a successful check."""

        current = _finite_time(now, allow_none=False)
        assert current is not None
        baseline = max(current, self.last_check_at or current)
        return replace(
            self,
            next_check_at=baseline + max(MIN_UPDATE_INTERVAL_SECONDS, self.interval_seconds),
        )


@dataclass(frozen=True, slots=True)
class PluginUpdateCheckResult:
    """Sanitized outcome of one bounded due check."""

    status: str
    checked_at: float | None
    next_check_at: float | None
    candidate_versions: tuple[tuple[str, str], ...] = ()
    staged: tuple[tuple[str, str], ...] = ()
    approval_required: tuple[tuple[str, str, tuple[str, ...]], ...] = ()
    error: str | None = None
    generation: int = 0

    def __post_init__(self) -> None:
        if self.status not in {"not_due", "disabled", "checked", "failed", "shutdown"}:
            raise ValueError("plugin_update_check_result_invalid")
        if type(self.generation) is not int or self.generation < 0:
            raise ValueError("plugin_update_check_result_invalid")
        _finite_time(self.checked_at)
        _finite_time(self.next_check_at)
        if type(self.candidate_versions) is not tuple:
            raise ValueError("plugin_update_check_result_invalid")
        for item in self.candidate_versions:
            if type(item) is not tuple or len(item) != 2 or any(
                type(value) is not str or not value or value != value.strip()
                for value in item
            ):
                raise ValueError("plugin_update_check_result_invalid")
        for item in self.staged:
            if type(item) is not tuple or len(item) != 2 or any(
                type(value) is not str or not value or value != value.strip()
                for value in item
            ):
                raise ValueError("plugin_update_check_result_invalid")
        for item in self.approval_required:
            if (
                type(item) is not tuple
                or len(item) != 3
                or type(item[0]) is not str
                or not item[0]
                or type(item[1]) is not str
                or not item[1]
                or type(item[2]) is not tuple
                or any(type(diff) is not str or not diff for diff in item[2])
            ):
                raise ValueError("plugin_update_check_result_invalid")
        if self.error is not None and self.error not in SAFE_UPDATE_ERRORS:
            raise ValueError("plugin_update_check_result_invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "checked_at": self.checked_at,
            "next_check_at": self.next_check_at,
            "candidate_versions": [list(item) for item in self.candidate_versions],
            "staged": [list(item) for item in self.staged],
            "approval_required": [
                [plugin_id, version, list(diff)]
                for plugin_id, version, diff in self.approval_required
            ],
            "error": self.error,
            "generation": self.generation,
        }

    @classmethod
    def from_dict(cls, value: object) -> PluginUpdateCheckResult:
        if type(value) is not dict or set(value) != {
            "status", "checked_at", "next_check_at", "candidate_versions",
            "staged", "approval_required", "error", "generation",
        }:
            raise ValueError("plugin_update_check_result_invalid")
        try:
            candidates = tuple(tuple(item) for item in value["candidate_versions"])
            staged = tuple(tuple(item) for item in value["staged"])
            approval = tuple(
                (item[0], item[1], tuple(item[2])) for item in value["approval_required"]
            )
            result = cls(
                status=value["status"],
                checked_at=_finite_time(value["checked_at"]),
                next_check_at=_finite_time(value["next_check_at"]),
                candidate_versions=candidates,
                staged=staged,
                approval_required=approval,
                error=value["error"],
                generation=value["generation"],
            )
        except (TypeError, ValueError, KeyError, IndexError):
            raise ValueError("plugin_update_check_result_invalid") from None
        if canonical_bytes(result.to_dict()) != canonical_bytes(value):
            raise ValueError("plugin_update_check_result_invalid")
        return result


@dataclass(frozen=True, slots=True)
class PluginUpdateResult:
    """Typed update result; approval diffs contain stable local labels only."""

    plugin_id: str
    version: str
    status: str
    approval_diff: tuple[str, ...] = ()
    error: str | None = None

    def __post_init__(self) -> None:
        if (
            type(self.plugin_id) is not str
            or not self.plugin_id
            or type(self.version) is not str
            or not self.version
            or self.status not in {"applied", "approval_required", "failed", "not_available"}
            or type(self.approval_diff) is not tuple
            or any(type(item) is not str or not item for item in self.approval_diff)
            or self.error is not None and self.error not in SAFE_UPDATE_ERRORS
        ):
            raise ValueError("plugin_update_result_invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "plugin_id": self.plugin_id,
            "version": self.version,
            "status": self.status,
            "approval_diff": list(self.approval_diff),
            "error": self.error,
        }


def policy_store(state_root: Path) -> McpStateStore:
    root = Path(state_root)
    return McpStateStore(
        state_root=root,
        document_path=root / "plugin-update-policy.json",
        invalid_path_reason="plugin_update_policy_unavailable",
        max_document_bytes=64 * 1024,
    )


def read_policy(store: McpStateStore) -> PluginUpdatePolicy:
    with store.locked():
        return _read_policy_locked(store)


def _read_policy_locked(store: McpStateStore) -> PluginUpdatePolicy:
    payload = store.read_bytes()
    if payload is None:
        policy = PluginUpdatePolicy.default()
        store.write_bytes(canonical_bytes(policy.to_dict()))
        return policy
    try:
        def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
            value: dict[str, object] = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError("plugin_update_policy_invalid")
                value[key] = item
            return value

        document = json.loads(
            payload.decode("utf-8"), object_pairs_hook=reject_duplicates
        )
        policy = PluginUpdatePolicy.from_dict(document)
        if canonical_bytes(policy.to_dict()) != payload:
            raise ValueError("plugin_update_policy_invalid")
        return policy
    except (UnicodeError, ValueError, json.JSONDecodeError):
        raise ValueError("plugin_update_policy_unavailable") from None


def defer_policy_if_current(
    store: McpStateStore,
    expected: PluginUpdatePolicy,
    now: float,
) -> PluginUpdatePolicy | None:
    """Advance a skipped deadline only if no newer policy was published."""

    if type(expected) is not PluginUpdatePolicy:
        raise ValueError("plugin_update_policy_invalid")
    with store.locked():
        current = _read_policy_locked(store)
        if current != expected:
            return None
        deferred = current.after_deferred(now)
        store.write_bytes(canonical_bytes(deferred.to_dict()))
        return deferred


def write_policy(store: McpStateStore, policy: PluginUpdatePolicy) -> None:
    if type(policy) is not PluginUpdatePolicy:
        raise ValueError("plugin_update_policy_invalid")
    with store.locked():
        store.write_bytes(canonical_bytes(policy.to_dict()))


def update_status_store(state_root: Path) -> McpStateStore:
    root = Path(state_root)
    return McpStateStore(
        state_root=root,
        document_path=root / "plugin-update-status.json",
        invalid_path_reason="plugin_update_status_unavailable",
        max_document_bytes=64 * 1024,
    )


def read_update_status(store: McpStateStore) -> PluginUpdateCheckResult | None:
    with store.locked():
        payload = store.read_bytes()
        if payload is None:
            return None
        try:
            def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
                result: dict[str, object] = {}
                for key, item in pairs:
                    if key in result:
                        raise ValueError("plugin_update_status_invalid")
                    result[key] = item
                return result
            document = json.loads(payload.decode("utf-8"), object_pairs_hook=reject_duplicates)
            result = PluginUpdateCheckResult.from_dict(document)
            if canonical_bytes(result.to_dict()) != payload:
                raise ValueError("plugin_update_status_invalid")
            return result
        except (UnicodeError, ValueError, json.JSONDecodeError):
            raise ValueError("plugin_update_status_unavailable") from None


def write_update_status(store: McpStateStore, result: PluginUpdateCheckResult) -> None:
    if type(result) is not PluginUpdateCheckResult:
        raise ValueError("plugin_update_result_invalid")
    with store.locked():
        store.write_bytes(canonical_bytes(result.to_dict()))


__all__ = [
    "DEFAULT_UPDATE_INTERVAL_SECONDS",
    "MAX_UPDATE_BACKOFF_SECONDS",
    "SAFE_UPDATE_ERRORS",
    "PluginUpdateCheckResult",
    "PluginUpdatePolicy",
    "PluginUpdateResult",
    "policy_store",
    "read_policy",
    "defer_policy_if_current",
    "write_policy",
    "read_update_status",
    "update_status_store",
    "write_update_status",
]
