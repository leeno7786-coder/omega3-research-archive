"""Bounded optional synchronization of signed marketplace catalog snapshots.

The plugin foundation performs no network I/O. Synchronization is expressed here
as a pure state machine over an injected synchronous transport seam; the real
bounded transport lives in the layer that owns HTTP clients and is wired in by
the lifecycle service. A failed synchronization is reported as a failure with a
stable cause and never disturbs the retained snapshot.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from .catalog import MAX_CATALOG_BYTES
from .catalog_store import CatalogStore, CatalogStoreError, CatalogUnavailable

MAX_CATALOG_PAYLOAD_BYTES = MAX_CATALOG_BYTES

_CAUSE_RE = re.compile(r"plugin_catalog_[a-z][a-z_]{0,62}\Z")
_FETCH_INVALID_CAUSE = "plugin_catalog_fetch_invalid"


class CatalogSyncStatus(StrEnum):
    """Closed set of synchronization outcomes; failure is never silent success."""

    UPDATED = "updated"
    UNCHANGED = "unchanged"
    FAILED = "failed"


class CatalogFetchError(Exception):
    """A stable, content-free catalog transport failure cause."""

    def __init__(self, cause: str) -> None:
        if type(cause) is not str or _CAUSE_RE.fullmatch(cause) is None:
            raise ValueError("plugin_catalog_cause_invalid")
        super().__init__(cause)
        self.cause = cause


@dataclass(frozen=True, slots=True)
class CatalogFetchResponse:
    """The bounded bytes one fetch attempt returned for the canonical catalog."""

    payload: bytes

    def __post_init__(self) -> None:
        if type(self.payload) is not bytes:
            raise ValueError(_FETCH_INVALID_CAUSE)
        if len(self.payload) > MAX_CATALOG_PAYLOAD_BYTES:
            raise ValueError("plugin_catalog_too_large")


class CatalogFetch(Protocol):
    """Synchronous bounded transport seam supplied by the network-owning layer."""

    def __call__(self) -> CatalogFetchResponse: ...


@dataclass(frozen=True, slots=True)
class CatalogSyncResult:
    """One synchronization outcome: either a retained snapshot or a stable cause."""

    status: CatalogSyncStatus
    sequence: int | None = None
    revision: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        try:
            status = CatalogSyncStatus(self.status)
        except ValueError:
            raise ValueError("plugin_catalog_sync_result_invalid") from None
        failed = status is CatalogSyncStatus.FAILED
        if failed:
            valid = (
                self.sequence is None
                and self.revision is None
                and type(self.error) is str
                and _CAUSE_RE.fullmatch(self.error) is not None
            )
        else:
            valid = (
                self.error is None
                and type(self.sequence) is int
                and self.sequence >= 1
                and type(self.revision) is str
            )
        if not valid:
            raise ValueError("plugin_catalog_sync_result_invalid")
        object.__setattr__(self, "status", status)


class CatalogSynchronizer:
    """Fetch, verify, and promote one snapshot without ever emptying the catalog."""

    def __init__(self, *, store: CatalogStore, fetch: CatalogFetch) -> None:
        if type(store) is not CatalogStore or not callable(fetch):
            raise ValueError("plugin_catalog_synchronizer_invalid")
        self._store = store
        self._fetch = fetch

    def sync(self) -> CatalogSyncResult:
        """Attempt one bounded synchronization and report its separated outcome."""
        try:
            previous_revision: str | None = self._store.current().revision
        except CatalogUnavailable:
            previous_revision = None
        except CatalogStoreError as exc:
            return CatalogSyncResult(status=CatalogSyncStatus.FAILED, error=str(exc))
        try:
            response = self._fetch()
        except CatalogFetchError as exc:
            return CatalogSyncResult(status=CatalogSyncStatus.FAILED, error=exc.cause)
        if type(response) is not CatalogFetchResponse:
            return CatalogSyncResult(status=CatalogSyncStatus.FAILED, error=_FETCH_INVALID_CAUSE)
        try:
            promoted = self._store.promote(response.payload)
        except CatalogStoreError as exc:
            return CatalogSyncResult(status=CatalogSyncStatus.FAILED, error=str(exc))
        status = (
            CatalogSyncStatus.UNCHANGED
            if promoted.revision == previous_revision
            else CatalogSyncStatus.UPDATED
        )
        return CatalogSyncResult(
            status=status,
            sequence=promoted.sequence,
            revision=promoted.revision,
        )
