"""Bounded, content-addressed retention of imported third-party plugin artifacts.

An artifact is inert bytes a publisher distributes: Omega pins it so a later
install can prove it received exactly the bytes discovery described. Nothing here
executes, unpacks, or interprets an artifact, and nothing here decides whether an
artifact may be installed -- that decision belongs to the importer and the
installer, on evidence this store only preserves.

Three properties are structural rather than advisory:

* **A content address is earned, never asserted.** ``stage`` consumes an injected
  iterable of byte chunks. Each chunk is bounded and hashed as it arrives -- one
  running SHA-256, fed chunk by chunk, never a second pass over the assembled
  buffer -- then buffered up to the bound. Only once that running digest is
  exactly the digest the caller pinned is anything written to staging; from
  there ``promote`` lands it at its content address with one atomic replace. A
  stream that is short, long, swapped, or merely different never reaches the
  store.
* **The bound is checked before the bytes are absorbed, not after.** ``stage``
  stops pulling chunks the moment one would carry the total past ``max_bytes``,
  so a hostile or broken source cannot spend Omega's memory or disk by lying
  about a length. That bound is also what makes buffering safe: the staged
  payload is never larger than a bound the caller had to state up front.
* **Staging is never a place to run from.** Staged bytes live under a directory
  reserved for them, are named so they can never be mistaken for a content
  address, and are exposed to callers only as a receipt carrying no path.
  ``promote`` re-reads and re-proves the staged bytes rather than trusting the
  receipt, so bytes swapped between the two calls are caught, not published.

Promotion writes the artifact through a same-directory temporary inside its own
``<sha256[:2]>`` shard and lands it with one atomic replace, matching the
convention ``catalog_store`` and ``registry`` already use: the atomicity comes
from the rename never crossing a directory, and the safety from every component
being opened through a pinned, reparse-rejecting chain.

The plugin foundation performs no network I/O, so the byte source is always
injected by the layer that owns the network.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from core import paths as core_paths

from .filesystem import PinnedDirectory, reject_reparse

MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
STAGING_DIRECTORY_NAME = ".staging"

_STAGING_PREFIX = "artifact."
_STAGING_SUFFIX = ".part"
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_STAGING_NAME_RE = re.compile(r"artifact\.[0-9a-f]{64}\.[0-9a-f]{32}\.part\Z")

_BOUND_INVALID = "plugin_artifact_bound_invalid"
_DIGEST_INVALID = "plugin_artifact_digest_invalid"
_DIGEST_MISMATCH = "plugin_artifact_digest_mismatch"
_MISSING = "plugin_artifact_missing"
_STAGING_INVALID = "plugin_artifact_staging_invalid"
_STORE_INVALID = "plugin_artifact_store_invalid"
_STORE_UNWRITABLE = "plugin_artifact_store_unwritable"
_STREAM_INVALID = "plugin_artifact_stream_invalid"
_TOO_LARGE = "plugin_artifact_too_large"


class ArtifactStoreError(Exception):
    """A stable, content-free artifact retention rejection cause."""


class ArtifactStreamError(ArtifactStoreError):
    """The injected source never delivered a usable byte stream.

    Kept distinct from its base so a caller can tell "these bytes are not the
    artifact" from "no bytes arrived": the first is a refusal that will never
    succeed, the second is a delivery failure a caller may retry. The cause text
    stays content-free either way, so a source's own error never crosses out.
    """


def _absolute(path: Path) -> Path:
    try:
        return Path(os.path.abspath(os.fspath(path)))
    except (OSError, RuntimeError, TypeError, ValueError):
        raise ArtifactStoreError(_STORE_INVALID) from None


def _digest(value: object) -> str:
    """Return one bare lowercase SHA-256 digest, or refuse to name a path with it."""
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        raise ArtifactStoreError(_DIGEST_INVALID)
    return value


def _bound(value: object) -> int:
    if type(value) is not int or not 1 <= value <= MAX_ARTIFACT_BYTES:
        raise ArtifactStoreError(_BOUND_INVALID)
    return value


def _staging_name(sha256: str) -> str:
    """Return a staging name that can never be read back as a content address."""
    return f"{_STAGING_PREFIX}{sha256}.{uuid4().hex}{_STAGING_SUFFIX}"


def _validate_plain_directory(path: Path) -> None:
    reject_reparse(path)
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise ValueError("invalid directory")


@dataclass(frozen=True, slots=True)
class StagedArtifact:
    """One proved-but-unpublished artifact, named without a path to run from."""

    sha256: str
    byte_length: int
    name: str

    def __post_init__(self) -> None:
        _digest(self.sha256)
        if type(self.byte_length) is not int or not 0 <= self.byte_length <= MAX_ARTIFACT_BYTES:
            raise ArtifactStoreError(_STAGING_INVALID)
        if type(self.name) is not str or _STAGING_NAME_RE.fullmatch(self.name) is None:
            raise ArtifactStoreError(_STAGING_INVALID)
        if not self.name.startswith(f"{_STAGING_PREFIX}{self.sha256}."):
            raise ArtifactStoreError(_STAGING_INVALID)


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    """One promoted artifact and the content address that now names it."""

    sha256: str
    byte_length: int
    path: Path


class ArtifactStore:
    """Pin third-party artifact bytes by digest without ever running them."""

    def __init__(self, root: Path | None = None) -> None:
        configured = core_paths.PLUGIN_ARTIFACTS_ROOT if root is None else Path(root)
        self.root = _absolute(configured)

    def stage(
        self,
        stream: Iterable[bytes],
        expected_sha256: str,
        max_bytes: int,
    ) -> StagedArtifact:
        """Absorb a bounded byte stream and keep it only if it is exactly this artifact.

        Each chunk is bounded and hashed as it arrives -- a running SHA-256 is
        updated in the same pass that buffers the chunk, never recomputed from
        the assembled payload afterward -- and buffered in memory up to the
        bound. Only once the running digest is proven equal to the digest the
        caller pinned is anything written to staging; from there ``promote``
        lands it at its content address with one atomic replace. The bound is
        enforced against the running total as each chunk arrives, so the stream
        is abandoned at the first chunk that would cross it rather than after
        it has been absorbed.
        """
        digest = _digest(expected_sha256)
        limit = _bound(max_bytes)
        payload, computed = _absorb(stream, limit)
        if computed != digest:
            raise ArtifactStoreError(_DIGEST_MISMATCH)
        staging = self._staging_directory()
        name = _staging_name(digest)
        try:
            with PinnedDirectory.open(staging) as directory:
                directory.write_bytes(name, payload, exclusive=True)
        except (OSError, RuntimeError, TypeError, ValueError):
            raise ArtifactStoreError(_STORE_UNWRITABLE) from None
        return StagedArtifact(sha256=digest, byte_length=len(payload), name=name)

    def promote(self, staged: StagedArtifact) -> StoredArtifact:
        """Re-prove staged bytes and land them atomically at their content address.

        The receipt is treated as a claim, not a fact: the staged file is read
        back under its recorded bound and re-hashed, so bytes replaced after
        staging are refused instead of published. A content address already
        holding these exact bytes is left untouched, which makes a duplicate
        import idempotent rather than a rewrite of something in use.
        """
        record = _copy_staged(staged)
        staging = self._staging_directory()
        payload = self._read_staged(staging, record)
        shard = self._shard_directory(record.sha256)
        try:
            with PinnedDirectory.open(shard) as directory:
                if not directory.exists(record.sha256) or not _matches(
                    self._read_promoted(directory, record), record
                ):
                    temporary = f"{record.sha256}.{uuid4().hex}{_STAGING_SUFFIX}"
                    try:
                        directory.write_bytes(temporary, payload, exclusive=True)
                        directory.replace(temporary, record.sha256)
                    except BaseException:
                        _discard(directory, temporary)
                        raise
        except ArtifactStoreError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError):
            raise ArtifactStoreError(_STORE_UNWRITABLE) from None
        self._discard_staged(staging, record)
        return StoredArtifact(
            sha256=record.sha256,
            byte_length=record.byte_length,
            path=shard / record.sha256,
        )

    def verify(self, stored: StoredArtifact) -> StoredArtifact:
        """Re-read a promoted artifact and prove it is still what its address names."""
        if type(stored) is not StoredArtifact:
            raise ArtifactStoreError(_STAGING_INVALID)
        record = StagedArtifact(
            sha256=_digest(stored.sha256),
            byte_length=stored.byte_length,
            name=_staging_name(stored.sha256),
        )
        shard = self._shard_directory(record.sha256)
        if stored.path != shard / record.sha256:
            raise ArtifactStoreError(_STAGING_INVALID)
        try:
            with PinnedDirectory.open(shard) as directory:
                payload = self._read_promoted(directory, record)
        except ArtifactStoreError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError):
            raise ArtifactStoreError(_STORE_INVALID) from None
        if payload is None:
            raise ArtifactStoreError(_MISSING)
        if not _matches(payload, record):
            raise ArtifactStoreError(_DIGEST_MISMATCH)
        return stored

    def discard(self, staged: StagedArtifact) -> None:
        """Remove one abandoned staged artifact so a failed import leaves no residue."""
        record = _copy_staged(staged)
        self._discard_staged(self._staging_directory(), record)

    def _staging_directory(self) -> Path:
        return self._prepared(self.root / STAGING_DIRECTORY_NAME)

    def _shard_directory(self, sha256: str) -> Path:
        return self._prepared(self.root / sha256[:2])

    def _prepared(self, path: Path) -> Path:
        """Create and validate one plain, non-reparse directory beneath the root."""
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            _validate_plain_directory(self.root)
            path.mkdir(parents=True, exist_ok=True)
            _validate_plain_directory(path)
            with PinnedDirectory.open(path) as directory:
                directory.validate_binding()
        except (OSError, RuntimeError, TypeError, ValueError):
            raise ArtifactStoreError(_STORE_INVALID) from None
        return path

    def _read_staged(self, staging: Path, record: StagedArtifact) -> bytes:
        try:
            with PinnedDirectory.open(staging) as directory:
                if not directory.exists(record.name):
                    raise ArtifactStoreError(_MISSING)
                payload = directory.read_bytes(record.name, limit=record.byte_length)
        except ArtifactStoreError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError):
            raise ArtifactStoreError(_DIGEST_MISMATCH) from None
        if not _matches(payload, record):
            raise ArtifactStoreError(_DIGEST_MISMATCH)
        return payload

    @staticmethod
    def _read_promoted(directory: PinnedDirectory, record: StagedArtifact) -> bytes | None:
        """Return the bytes already at this content address, or None if absent."""
        try:
            if not directory.exists(record.sha256):
                return None
            return directory.read_bytes(record.sha256, limit=MAX_ARTIFACT_BYTES)
        except (OSError, RuntimeError, TypeError, ValueError):
            return None

    @staticmethod
    def _discard_staged(staging: Path, record: StagedArtifact) -> None:
        try:
            with PinnedDirectory.open(staging) as directory:
                _discard(directory, record.name)
        except (OSError, RuntimeError, TypeError, ValueError):
            return


def _absorb(stream: Iterable[bytes], limit: int) -> tuple[bytes, str]:
    """Pull exact byte chunks up to the bound, hashing each as it arrives.

    The bound is checked against the running total before a chunk is absorbed,
    exactly as before; the only addition is a running SHA-256 fed one chunk at a
    time in the same pass, so the digest returned alongside the buffered payload
    is never a second, post-hoc pass over the assembled bytes.

    The source is third-party code the network-owning layer supplied, so it may
    fail part-way through -- a reset connection is the ordinary case, not the
    exotic one. Any failure it raises is replaced with a stable cause here, so
    transport text never reaches a caller through a half-delivered artifact.
    """
    payload = bytearray()
    hasher = hashlib.sha256()
    try:
        for chunk in _iterate(stream):
            if type(chunk) is not bytes:
                raise ArtifactStreamError(_STREAM_INVALID)
            if len(payload) + len(chunk) > limit:
                raise ArtifactStoreError(_TOO_LARGE)
            payload.extend(chunk)
            hasher.update(chunk)
    except ArtifactStoreError:
        raise
    except Exception:
        raise ArtifactStreamError(_STREAM_INVALID) from None
    return bytes(payload), hasher.hexdigest()


def _iterate(stream: Iterable[bytes]) -> Any:
    try:
        return iter(stream)
    except TypeError:
        raise ArtifactStreamError(_STREAM_INVALID) from None


def _matches(payload: bytes | None, record: StagedArtifact) -> bool:
    return (
        payload is not None
        and len(payload) == record.byte_length
        and hashlib.sha256(payload).hexdigest() == record.sha256
    )


def _copy_staged(staged: StagedArtifact) -> StagedArtifact:
    """Revalidate a staging receipt through its own constructor before using it."""
    if type(staged) is not StagedArtifact:
        raise ArtifactStoreError(_STAGING_INVALID)
    return StagedArtifact(
        sha256=staged.sha256, byte_length=staged.byte_length, name=staged.name
    )


def _discard(directory: PinnedDirectory, name: str) -> None:
    try:
        directory.unlink(name, missing_ok=True)
    except (OSError, RuntimeError, TypeError, ValueError):
        return


__all__ = [
    "MAX_ARTIFACT_BYTES",
    "STAGING_DIRECTORY_NAME",
    "ArtifactStore",
    "ArtifactStoreError",
    "ArtifactStreamError",
    "StagedArtifact",
    "StoredArtifact",
]
