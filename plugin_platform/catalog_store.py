"""Atomic on-device retention of the last verified marketplace catalog snapshot.

The desktop always ships a signed bundled snapshot. Optional synchronization may
promote a newer signed snapshot beside it. A failed, tampered, rolled-back, or
merely absent synchronization must leave the previous verified snapshot readable:
this store never degrades to a fabricated empty marketplace.
"""

from __future__ import annotations

import os
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from core import paths as core_paths

from .catalog import (
    MARKETPLACE_CATALOG_PATH,
    MAX_CATALOG_BYTES,
    CatalogVerificationError,
    VerifiedMarketplaceCatalog,
    verify_catalog_successor,
    verify_plugin_catalog,
)
from .filesystem import PinnedDirectory, reject_reparse
from .trust import TrustedPublisherKeyRing, load_embedded_key_ring

CATALOG_FILE_NAME = "catalog.json"
CATALOG_LOCK_NAME = "catalog.lock"

_TEMPORARY_PREFIX = "catalog.json."
_TEMPORARY_SUFFIX = ".tmp"
_LOCK_TIMEOUT_SECONDS = 30.0

# Every rejection this layer publishes is a stable, content-free cause. The
# underlying verifier reasons are collapsed deliberately: from outside the
# boundary a malformed, non-canonical, unsigned, revoked, or tampered payload is
# one fact — these bytes are not an authentic signed catalog — and distinguishing
# them would leak the shape of the rejected content back to whoever supplied it.
_PROMOTION_CAUSES = {
    "catalog_identity_changed": "plugin_catalog_identity_changed",
    "catalog_sequence_equivocation": "plugin_catalog_sequence_equivocation",
    "catalog_sequence_rollback": "plugin_catalog_sequence_rollback",
    "catalog_too_large": "plugin_catalog_too_large",
}
_UNAUTHENTIC_CAUSE = "plugin_catalog_signature_invalid"

_MUTATION_LOCKS_GUARD = threading.Lock()
_MUTATION_LOCKS: dict[str, threading.RLock] = {}


class CatalogStoreError(Exception):
    """A stable, content-free marketplace snapshot storage rejection."""


class CatalogUnavailable(CatalogStoreError):
    """No verified snapshot — bundled or synchronized — exists on this device."""


def _absolute(path: Path) -> Path:
    try:
        return Path(os.path.abspath(os.fspath(path)))
    except (OSError, RuntimeError, TypeError, ValueError):
        raise ValueError("plugin_catalog_store_invalid") from None


def _mutation_lock(path: Path) -> threading.RLock:
    """Return the one in-process lock guarding this marketplace root."""
    key = os.path.normcase(os.fspath(path))
    with _MUTATION_LOCKS_GUARD:
        return _MUTATION_LOCKS.setdefault(key, threading.RLock())


def _promotion_cause(exc: CatalogVerificationError) -> str:
    return _PROMOTION_CAUSES.get(str(exc), _UNAUTHENTIC_CAUSE)


def _same_identity(
    left: VerifiedMarketplaceCatalog, right: VerifiedMarketplaceCatalog
) -> bool:
    """Report whether two verified snapshots carry one catalog identity."""
    return left.catalog_id == right.catalog_id and left.publisher_id == right.publisher_id


def _validate_plain_directory(path: Path) -> None:
    reject_reparse(path)
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise ValueError("invalid directory")


class CatalogStore:
    """Retain, serve, and atomically replace one verified catalog snapshot."""

    def __init__(
        self,
        *,
        root: Path | None = None,
        bundled_path: Path | None = None,
        key_ring: TrustedPublisherKeyRing | None = None,
    ) -> None:
        configured = core_paths.PLUGIN_MARKETPLACE_ROOT if root is None else Path(root)
        bundled = MARKETPLACE_CATALOG_PATH if bundled_path is None else Path(bundled_path)
        self.root = _absolute(configured)
        self.catalog_path = self.root / CATALOG_FILE_NAME
        self.bundled_path = _absolute(bundled)
        ring = load_embedded_key_ring() if key_ring is None else key_ring
        if type(ring) is not TrustedPublisherKeyRing:
            raise ValueError("plugin_catalog_store_invalid")
        self._key_ring = ring

    def current(self) -> VerifiedMarketplaceCatalog:
        """Serve the greater-sequence verified snapshot, bundled or synchronized.

        The shipped bundled snapshot is a rollback floor, not merely a fallback: a
        synchronized copy is served only when it carries the same catalog identity
        and a strictly greater sequence. A stale mirror therefore cannot pin this
        device below the catalog the release shipped with.
        """
        synchronized = self._verified_or_none(self.catalog_path)
        bundled = self._verified_or_none(self.bundled_path)
        if bundled is None:
            if synchronized is None:
                raise CatalogUnavailable("plugin_catalog_unavailable")
            return synchronized
        if synchronized is None or not _same_identity(synchronized, bundled):
            return bundled
        return synchronized if synchronized.sequence > bundled.sequence else bundled

    def promote(self, candidate_bytes: bytes) -> VerifiedMarketplaceCatalog:
        """Verify candidate bytes, then replace the snapshot atomically under lock."""
        if type(candidate_bytes) is not bytes:
            raise CatalogStoreError(_UNAUTHENTIC_CAUSE)
        if len(candidate_bytes) > MAX_CATALOG_BYTES:
            raise CatalogStoreError("plugin_catalog_too_large")
        self._prepare_root()
        temporary = f"{_TEMPORARY_PREFIX}{uuid4().hex}{_TEMPORARY_SUFFIX}"
        # Verification happens on the staged bytes before any lock is taken, so a
        # forged candidate never delays a concurrent legitimate promotion.
        candidate = self._stage(temporary, candidate_bytes)
        try:
            with self._locked() as directory:
                previous = self._current_or_none()
                if previous is not None:
                    try:
                        verify_catalog_successor(previous, candidate)
                    except CatalogVerificationError as exc:
                        raise CatalogStoreError(_promotion_cause(exc)) from None
                try:
                    directory.replace(temporary, CATALOG_FILE_NAME)
                except (OSError, ValueError):
                    raise CatalogStoreError("plugin_catalog_store_unwritable") from None
        except BaseException:
            self._discard(temporary)
            raise
        return candidate

    def _current_or_none(self) -> VerifiedMarketplaceCatalog | None:
        """Return the promotion baseline: the snapshot this device would serve."""
        try:
            return self.current()
        except CatalogUnavailable:
            return None

    def _verified_or_none(self, path: Path) -> VerifiedMarketplaceCatalog | None:
        try:
            return verify_plugin_catalog(path, self._key_ring)
        except CatalogVerificationError:
            return None

    def _prepare_root(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            _validate_plain_directory(self.root)
            with PinnedDirectory.open(self.root) as directory:
                directory.validate_binding()
        except (OSError, RuntimeError, TypeError, ValueError):
            raise CatalogStoreError("plugin_catalog_store_invalid") from None

    def _stage(self, temporary: str, payload: bytes) -> VerifiedMarketplaceCatalog:
        """Write the candidate to a same-directory temporary, fsync it, verify it."""
        try:
            with PinnedDirectory.open(self.root) as directory:
                directory.write_bytes(temporary, payload, exclusive=True)
        except (OSError, RuntimeError, TypeError, ValueError):
            raise CatalogStoreError("plugin_catalog_store_unwritable") from None
        try:
            return verify_plugin_catalog(self.root / temporary, self._key_ring)
        except CatalogVerificationError as exc:
            self._discard(temporary)
            raise CatalogStoreError(_promotion_cause(exc)) from None
        except BaseException:
            self._discard(temporary)
            raise

    @contextmanager
    def _locked(self) -> Iterator[PinnedDirectory]:
        """Hold the in-process lock and the sidecar file lock over one promotion."""
        process_lock = _mutation_lock(self.root)
        with process_lock:
            directory: PinnedDirectory | None = None
            try:
                directory = PinnedDirectory.open(self.root)
                with directory.lock_file(CATALOG_LOCK_NAME, timeout=_LOCK_TIMEOUT_SECONDS):
                    directory.validate_binding()
                    yield directory
                    directory.validate_binding()
            except CatalogStoreError:
                raise
            except (OSError, RuntimeError, TimeoutError, TypeError, ValueError):
                raise CatalogStoreError("plugin_catalog_store_invalid") from None
            finally:
                if directory is not None:
                    directory.close()

    def _discard(self, temporary: str) -> None:
        """Remove one abandoned staging file so a failure leaves no residue."""
        try:
            with PinnedDirectory.open(self.root) as directory:
                directory.unlink(temporary, missing_ok=True)
        except (OSError, RuntimeError, TypeError, ValueError):
            return
