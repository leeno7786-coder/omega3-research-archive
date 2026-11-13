"""Strict, durable operator control for externally networked MCP packages."""

from __future__ import annotations

import json
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp_platform.file_lock import file_lock
from core.paths import MCP_EXTERNAL_SWITCH_PATH
from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.package_contracts import NetworkClass
from runtime.atomic_store import atomic_write_bytes

_STATE_FIELDS = frozenset({"schema_version", "enabled", "revision"})
_REVISION_RE = re.compile(r"[0-9a-f]{64}\Z")


class ExternalMcpSwitchError(RuntimeError):
    """A stable failure while reading or changing the external-MCP switch."""


class ExternalMcpSwitchUnreadable(ExternalMcpSwitchError):
    """Persisted switch state is not the exact valid durable document."""


class ExternalMcpSwitchConflict(ExternalMcpSwitchError):
    """The caller's expected revision is no longer the current switch state."""


@dataclass(frozen=True)
class ExternalMcpState:
    """The complete content-free external MCP connectivity choice."""

    enabled: bool
    revision: str

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool or self.revision != _revision_for(self.enabled):
            raise ValueError("invalid_external_mcp_state")


class ExternalMcpSwitch:
    """Load and atomically compare-and-set external MCP eligibility state."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else MCP_EXTERNAL_SWITCH_PATH

    def load(self) -> ExternalMcpState:
        """Read the strict state; a missing document is the deterministic off state."""
        self._validate_durable_paths()
        return self._load_unlocked()

    def set_enabled(self, enabled: bool, *, expected_revision: str) -> ExternalMcpState:
        """Atomically replace state only when the operator's observed revision matches."""
        if type(enabled) is not bool:
            raise ValueError("invalid_external_mcp_switch_enabled")
        self._validate_durable_paths()
        with file_lock(self.path):
            self._validate_durable_paths()
            current = self._load_unlocked()
            if expected_revision != current.revision:
                raise ExternalMcpSwitchConflict("external_mcp_switch_conflict")
            updated = _state_for(enabled)
            atomic_write_bytes(self.path, canonical_bytes(_document_for(updated)))
            return updated

    def _load_unlocked(self) -> ExternalMcpState:
        if not self.path.exists():
            return _state_for(False)
        try:
            raw = json.loads(
                self.path.read_text(encoding="utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
            )
            document = _exact_object(raw)
            if type(document["schema_version"]) is not int or document["schema_version"] != 1:
                raise ValueError("invalid schema version")
            enabled = document["enabled"]
            if type(enabled) is not bool:
                raise ValueError("invalid enabled state")
            revision = document["revision"]
            if type(revision) is not str or not _REVISION_RE.fullmatch(revision):
                raise ValueError("invalid revision")
            return ExternalMcpState(enabled=enabled, revision=revision)
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            raise ExternalMcpSwitchUnreadable("external_mcp_switch_unreadable") from None

    def _validate_durable_paths(self) -> None:
        lock_path = Path(f"{self.path}.lock")
        _validate_parent_chain(self.path.parent)
        _validate_existing_path(self.path, directory=False)
        _validate_existing_path(lock_path, directory=False)


def mcp_eligible(network_class: NetworkClass, state: ExternalMcpState) -> bool:
    """Return whether the one switch permits this declared package network class."""
    if not isinstance(state, ExternalMcpState):
        raise ValueError("invalid_external_mcp_state")
    if network_class is NetworkClass.LOCAL_ONLY:
        return True
    if network_class is NetworkClass.EXTERNAL:
        return state.enabled
    raise ValueError("invalid_mcp_network_class")


def _state_for(enabled: bool) -> ExternalMcpState:
    return ExternalMcpState(enabled=enabled, revision=_revision_for(enabled))


def _revision_for(enabled: bool) -> str:
    return sha256_hex(canonical_bytes({"schema_version": 1, "enabled": enabled}))


def _document_for(state: ExternalMcpState) -> dict[str, object]:
    return {"schema_version": 1, "enabled": state.enabled, "revision": state.revision}


def _exact_object(raw: Any) -> dict[str, Any]:
    if (
        type(raw) is not dict
        or set(raw) != _STATE_FIELDS
        or any(type(key) is not str for key in raw)
    ):
        raise ValueError("invalid exact external MCP switch document")
    return raw


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("duplicate JSON field")
        document[key] = value
    return document


def _validate_parent_chain(parent: Path) -> None:
    chain: list[Path] = []
    current = parent
    while True:
        chain.append(current)
        if current.parent == current:
            break
        current = current.parent
    for directory in reversed(chain):
        _reject_reparse(directory)
        if directory.exists() and not directory.is_dir():
            raise ValueError("external_mcp_switch_path_unsafe")


def _validate_existing_path(path: Path, *, directory: bool) -> None:
    _reject_reparse(path)
    if not path.exists():
        return
    valid_kind = path.is_dir() if directory else path.is_file()
    if not valid_kind:
        raise ValueError("external_mcp_switch_path_unsafe")


def _reject_reparse(path: Path) -> None:
    try:
        path_status = path.lstat()
    except FileNotFoundError:
        return
    except OSError:
        raise ValueError("external_mcp_switch_path_unsafe") from None
    try:
        is_junction = path.is_junction()
    except (AttributeError, OSError):
        is_junction = False
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(path_status, "st_file_attributes", 0)
    if stat.S_ISLNK(path_status.st_mode) or is_junction or attributes & reparse_attribute:
        raise ValueError("external_mcp_switch_path_unsafe")
