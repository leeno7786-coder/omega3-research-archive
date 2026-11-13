"""Fetch and verify signed marketplace package archives.

The marketplace catalog authenticates *what* may be installed.  This module
owns the small bridge between that signed metadata and the existing package
verifier: bytes are downloaded with the same bounded HTTP client as catalog
synchronisation, unpacked into an untrusted temporary directory, completely
verified, and only then atomically published under their logical package
digest.  An installed package is never used as a download target.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import httpx2

from core import paths as core_paths
from mcp_platform.transports.streamable_http import build_bounded_sync_http_client
from plugin_platform.catalog import (
    MarketplaceCatalogEntry,
    VerifiedMarketplaceCatalog,
    validate_verified_catalog_entry,
)
from plugin_platform.package_verifier import (
    MAX_PACKAGE_BYTES,
    PackageVerificationError,
    verify_plugin_package,
)
from plugin_platform.trust import TrustedPublisherKeyRing, load_embedded_key_ring
from runtime.pinned_filesystem import MAX_TREE_ENTRIES, reject_reparse


MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_FILES = MAX_TREE_ENTRIES
DEFAULT_CONNECT_SECONDS = 5.0
DEFAULT_READ_SECONDS = 15.0
DEFAULT_TOTAL_SECONDS = 30.0


class PackageArtifactError(ValueError):
    """Stable, content-free package artifact fetch or verification failure."""


def _origin(url: str) -> tuple[str, str, int | None]:
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError
        return parsed.scheme.casefold(), parsed.hostname.casefold(), parsed.port
    except (TypeError, ValueError):
        raise PackageArtifactError("package_artifact_url_invalid") from None


def _safe_member(name: str) -> tuple[str, ...]:
    """Return safe POSIX components for one archive member."""

    if type(name) is not str or "\\" in name or "\x00" in name:
        raise PackageArtifactError("package_artifact_archive_traversal")
    raw = name[:-1] if name.endswith("/") else name
    path = PurePosixPath(raw)
    if (
        not raw
        or path.is_absolute()
        or path.as_posix() != raw
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(":" in part for part in path.parts)
    ):
        raise PackageArtifactError("package_artifact_archive_traversal")
    return path.parts


def _is_link(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    return stat.S_ISLNK(mode) or stat.S_ISCHR(mode) or stat.S_ISBLK(mode) or stat.S_ISFIFO(mode)


def _map_verification_failure(exc: PackageVerificationError) -> str:
    reason = str(exc)
    if reason == "omega_version_incompatible":
        return "package_artifact_omega_version_incompatible"
    if reason in {
        "manifest_signature_invalid",
        "publisher_key_untrusted",
        "signature_invalid",
        "invalid_signature",
        "signature_encoding_invalid",
    }:
        return "package_artifact_signature_invalid"
    if reason in {"asset_digest_mismatch", "package_digest_mismatch"}:
        return "package_artifact_digest_mismatch"
    if reason == "package_tree_invalid":
        return "package_artifact_reparse_rejected"
    return "package_artifact_package_invalid"


class PackageArtifactFetcher:
    """Download a catalogued package and return only an authenticated tree."""

    def __init__(
        self,
        *,
        root: Path | None = None,
        key_ring: TrustedPublisherKeyRing | None = None,
        omega_version: str = "3.0.0",
        transport: httpx2.BaseTransport | None = None,
        connect_seconds: float = DEFAULT_CONNECT_SECONDS,
        read_seconds: float = DEFAULT_READ_SECONDS,
        total_seconds: float = DEFAULT_TOTAL_SECONDS,
        max_archive_bytes: int = MAX_ARCHIVE_BYTES,
    ) -> None:
        configured = core_paths.PLUGIN_ARTIFACTS_ROOT if root is None else Path(root)
        try:
            self._root = Path(os.path.abspath(os.fspath(configured)))
        except (OSError, TypeError, ValueError):
            raise PackageArtifactError("package_artifact_staging_invalid") from None
        if (
            type(connect_seconds) is not float
            or connect_seconds <= 0
            or type(read_seconds) is not float
            or read_seconds <= 0
            or type(total_seconds) is not float
            or total_seconds <= 0
            or type(max_archive_bytes) is not int
            or not 1 <= max_archive_bytes <= MAX_ARCHIVE_BYTES
        ):
            raise PackageArtifactError("package_artifact_config_invalid")
        if transport is not None and not isinstance(transport, httpx2.BaseTransport):
            raise PackageArtifactError("package_artifact_config_invalid")
        self._key_ring = load_embedded_key_ring() if key_ring is None else key_ring
        self._omega_version = omega_version
        self._transport = transport
        self._connect_seconds = connect_seconds
        self._read_seconds = read_seconds
        self._total_seconds = total_seconds
        self._max_archive_bytes = max_archive_bytes

    def fetch(
        self,
        entry: MarketplaceCatalogEntry,
        catalog: VerifiedMarketplaceCatalog,
    ) -> Path:
        """Fetch ``entry`` in the exact signed ``catalog`` context."""

        try:
            issued = validate_verified_catalog_entry(entry, catalog=catalog)
        except (TypeError, ValueError):
            raise PackageArtifactError("package_artifact_catalog_context_invalid") from None
        scheme, host, port = _origin(issued.package_uri)
        del scheme, port
        self._prepare_root()
        destination = self._root / issued.package_sha256
        if destination.exists():
            try:
                cached = verify_plugin_package(
                    destination, self._key_ring, self._omega_version
                )
            except (OSError, TypeError, ValueError, PackageVerificationError):
                raise PackageArtifactError("package_artifact_destination_conflict") from None
            self._verify_entry(cached, issued)
            return destination
        staging: Path | None = None
        archive: Path | None = None
        try:
            staging = Path(tempfile.mkdtemp(prefix="package.", dir=self._root))
            archive = staging / "artifact.zip"
            self._download(issued.package_uri, host, archive)
            package_dir = staging / "package"
            self._extract(archive, package_dir)
            try:
                verified = verify_plugin_package(
                    package_dir, self._key_ring, self._omega_version
                )
            except PackageVerificationError as exc:
                raise PackageArtifactError(_map_verification_failure(exc)) from None
            self._verify_entry(verified, issued)
            return self._promote(package_dir, destination, issued)
        except PackageArtifactError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError, zipfile.BadZipFile):
            raise PackageArtifactError("package_artifact_staging_invalid") from None
        finally:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)

    def _prepare_root(self) -> None:
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            reject_reparse(self._root)
            if not self._root.is_dir():
                raise ValueError
        except (OSError, ValueError):
            raise PackageArtifactError("package_artifact_staging_invalid") from None

    def _download(self, url: str, approved_host: str, target: Path) -> None:
        deadline = time.monotonic() + self._total_seconds
        client = build_bounded_sync_http_client(
            transport=self._transport,
            connection_seconds=self._connect_seconds,
            operation_seconds=self._read_seconds,
        )
        observed = 0
        hasher = hashlib.sha256()
        try:
            with client.stream("GET", url) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not isinstance(location, str):
                        raise PackageArtifactError("package_artifact_redirect_rejected")
                    try:
                        _, redirected_host, redirected_port = _origin(location)
                    except PackageArtifactError:
                        raise PackageArtifactError("package_artifact_redirect_rejected") from None
                    if redirected_host != approved_host or redirected_port is not None:
                        raise PackageArtifactError("package_artifact_redirect_rejected")
                    raise PackageArtifactError("package_artifact_redirect_rejected")
                if response.status_code != 200:
                    raise PackageArtifactError("package_artifact_unavailable")
                encoding = response.headers.get("Content-Encoding", "")
                if encoding.strip().casefold() not in {"", "identity"}:
                    raise PackageArtifactError("package_artifact_encoding_unsupported")
                with target.open("xb") as stream:
                    for chunk in response.iter_bytes():
                        if time.monotonic() > deadline:
                            raise PackageArtifactError("package_artifact_timeout")
                        payload = bytes(chunk)
                        observed += len(payload)
                        if observed > self._max_archive_bytes:
                            raise PackageArtifactError("package_artifact_too_large")
                        hasher.update(payload)
                        stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
        except PackageArtifactError:
            raise
        except httpx2.TimeoutException:
            raise PackageArtifactError("package_artifact_timeout") from None
        except (httpx2.HTTPError, OSError, ValueError, RuntimeError):
            raise PackageArtifactError("package_artifact_unavailable") from None
        finally:
            client.close()
        if observed == 0 or len(hasher.digest()) != 32:
            raise PackageArtifactError("package_artifact_unavailable")

    def _extract(self, archive: Path, destination: Path) -> None:
        try:
            with zipfile.ZipFile(archive, "r") as source:
                infos = source.infolist()
                if not infos or len(infos) > MAX_ARCHIVE_FILES:
                    raise PackageArtifactError("package_artifact_archive_invalid")
                names: set[str] = set()
                total = 0
                for info in infos:
                    parts = _safe_member(info.filename)
                    key = "/".join(parts).casefold()
                    if key in names:
                        raise PackageArtifactError("package_artifact_archive_duplicate")
                    names.add(key)
                    if _is_link(info):
                        raise PackageArtifactError("package_artifact_archive_reparse")
                    if info.file_size < 0:
                        raise PackageArtifactError("package_artifact_archive_invalid")
                    total += info.file_size
                    if total > MAX_PACKAGE_BYTES:
                        raise PackageArtifactError("package_artifact_archive_too_large")
                top_levels = {parts[0] for parts in (_safe_member(i.filename) for i in infos)}
                prefix: tuple[str, ...] = ()
                if len(top_levels) == 1:
                    top = next(iter(top_levels))
                    if any(
                        _safe_member(info.filename)[:2] == (top, "plugin.json")
                        for info in infos
                    ):
                        prefix = (top,)
                destination.mkdir(parents=False)
                for info in infos:
                    parts = _safe_member(info.filename)
                    if prefix and parts[: len(prefix)] != prefix:
                        raise PackageArtifactError("package_artifact_archive_invalid")
                    relative = parts[len(prefix) :]
                    if not relative:
                        continue
                    candidate = destination.joinpath(*relative)
                    if info.is_dir() or info.filename.endswith("/"):
                        # Parents may have been materialised by a preceding
                        # file entry; duplicates were already rejected above.
                        candidate.mkdir(parents=True, exist_ok=True)
                        continue
                    candidate.parent.mkdir(parents=True, exist_ok=True)
                    with source.open(info, "r") as incoming, candidate.open("xb") as outgoing:
                        copied = 0
                        remaining = info.file_size
                        while remaining > 0:
                            chunk = incoming.read(min(64 * 1024, remaining))
                            if not chunk:
                                break
                            copied += len(chunk)
                            if copied > info.file_size:
                                raise PackageArtifactError("package_artifact_archive_invalid")
                            outgoing.write(chunk)
                            remaining -= len(chunk)
                        # A ZIP member declaring a smaller size than its
                        # decompressed stream must fail closed just as a
                        # truncated member does.  The bounded one-byte probe
                        # preserves the old EOF validation without an
                        # unbounded copy loop.
                        if incoming.read(1):
                            raise PackageArtifactError("package_artifact_archive_invalid")
                        if copied != info.file_size:
                            raise PackageArtifactError("package_artifact_archive_invalid")
        except PackageArtifactError:
            raise
        except (OSError, ValueError, zipfile.BadZipFile, RuntimeError):
            raise PackageArtifactError("package_artifact_archive_invalid") from None

    def _promote(
        self,
        package_dir: Path,
        destination: Path,
        entry: MarketplaceCatalogEntry,
    ) -> Path:
        if destination.exists():
            try:
                verified = verify_plugin_package(destination, self._key_ring, self._omega_version)
            except (OSError, TypeError, ValueError, PackageVerificationError):
                raise PackageArtifactError("package_artifact_destination_conflict") from None
            try:
                self._verify_entry(verified, entry)
            except PackageArtifactError:
                raise PackageArtifactError("package_artifact_destination_conflict") from None
            return destination
        try:
            os.replace(package_dir, destination)
        except FileExistsError:
            return self._promote(package_dir, destination, entry)
        except (OSError, ValueError):
            raise PackageArtifactError("package_artifact_staging_invalid") from None
        return destination

    @staticmethod
    def _verify_entry(verified: object, entry: MarketplaceCatalogEntry) -> None:
        """Bind every executable identity field to the signed catalog listing."""
        try:
            manifest = verified.manifest
            plugin_id = manifest.plugin_id
            plugin_version = manifest.plugin_version
            publisher_id = manifest.publisher_id
            manifest_sha256 = verified.manifest_sha256
            package_sha256 = verified.package_sha256
        except AttributeError:
            raise PackageArtifactError("package_artifact_package_invalid") from None
        if plugin_id != entry.plugin_id or plugin_version != entry.plugin_version:
            raise PackageArtifactError("package_artifact_identity_mismatch")
        if publisher_id != entry.publisher_id:
            raise PackageArtifactError("package_artifact_publisher_mismatch")
        if manifest_sha256 != entry.manifest_sha256:
            raise PackageArtifactError("package_artifact_manifest_digest_mismatch")
        if package_sha256 != entry.package_sha256:
            raise PackageArtifactError("package_artifact_digest_mismatch")


__all__ = ["MAX_ARCHIVE_BYTES", "PackageArtifactError", "PackageArtifactFetcher"]
