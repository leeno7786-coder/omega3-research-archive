"""Bounded download and archive verification tests for marketplace packages."""

from __future__ import annotations

import base64
import io
import zipfile
from pathlib import Path

import httpx2
import pytest

from mcp_platform.canonical import canonical_bytes
from mcp_platform.package_fetch import PackageArtifactError, PackageArtifactFetcher
from plugin_platform.catalog import verify_plugin_catalog
from plugin_platform.package_verifier import verify_plugin_package
from tests.plugin_platform.support import (
    build_signed_plugin,
    ephemeral_publisher_ring,
    marketplace_entry_document,
)


def _archive(package: Path) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(package.rglob("*")):
            if path.is_file():
                archive.writestr(path.relative_to(package).as_posix(), path.read_bytes())
    return output.getvalue()


def _fixture(
    tmp_path: Path,
    *,
    payload: bytes | None = None,
    catalog_plugin_version: str | None = None,
):
    private_key, ring = ephemeral_publisher_ring()
    package = build_signed_plugin(tmp_path, private_key)
    verified = verify_plugin_package(package, ring, "3.0.0")
    document = {
        "schema_version": 2,
        "catalog_id": "test.catalog",
        "publisher_id": "test.publisher",
        "sequence": 1,
        "entries": [
            marketplace_entry_document(
                plugin_id=verified.manifest.plugin_id,
                publisher_id=verified.manifest.publisher_id,
                plugin_version=(
                    verified.manifest.plugin_version
                    if catalog_plugin_version is None
                    else catalog_plugin_version
                ),
                manifest_sha256=verified.manifest_sha256,
                package_sha256=verified.package_sha256,
                package_uri="https://plugins.example.test/test.workspace/1.0.0",
            )
        ],
        "signing_key_id": "test-plugin-key",
    }
    document["signature"] = base64.b64encode(
        private_key.sign(canonical_bytes({key: value for key, value in document.items()}))
    ).decode("ascii")
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_bytes(canonical_bytes(document))
    catalog = verify_plugin_catalog(catalog_path, ring)
    body = _archive(package) if payload is None else payload

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=body, request=request)

    return ring, catalog, catalog.entries[0], body, httpx2.MockTransport(handler)


def test_fetch_extracts_and_publishes_only_after_package_verification(tmp_path: Path) -> None:
    ring, catalog, entry, _body, transport = _fixture(tmp_path)
    fetched = PackageArtifactFetcher(
        root=tmp_path / "artifacts", key_ring=ring, transport=transport
    ).fetch(entry, catalog)

    assert fetched == tmp_path / "artifacts" / entry.package_sha256
    assert fetched.joinpath("plugin.json").is_file()
    assert verify_plugin_package(fetched, ring, "3.0.0").package_sha256 == entry.package_sha256


def test_fetch_reuses_a_verified_content_addressed_cache_offline(tmp_path: Path) -> None:
    ring, catalog, entry, _body, transport = _fixture(tmp_path)
    fetcher = PackageArtifactFetcher(
        root=tmp_path / "artifacts", key_ring=ring, transport=transport
    )
    first = fetcher.fetch(entry, catalog)
    offline = PackageArtifactFetcher(
        root=tmp_path / "artifacts",
        key_ring=ring,
        transport=httpx2.MockTransport(
            lambda request: (_ for _ in ()).throw(httpx2.ConnectError("offline"))
        ),
    )
    assert offline.fetch(entry, catalog) == first


def test_fetch_rejects_package_manifest_version_drift(tmp_path: Path) -> None:
    ring, catalog, entry, _body, transport = _fixture(
        tmp_path, catalog_plugin_version="2.0.0"
    )

    with pytest.raises(
        PackageArtifactError, match="^package_artifact_identity_mismatch$"
    ):
        PackageArtifactFetcher(
            root=tmp_path / "artifacts", key_ring=ring, transport=transport
        ).fetch(entry, catalog)

    assert not (tmp_path / "artifacts" / entry.package_sha256).exists()


def test_fetch_does_not_replace_an_invalid_cached_destination(tmp_path: Path) -> None:
    ring, catalog, entry, _body, transport = _fixture(tmp_path)
    fetcher = PackageArtifactFetcher(
        root=tmp_path / "artifacts", key_ring=ring, transport=transport
    )
    cached = fetcher.fetch(entry, catalog)
    marker = b"preserve this invalid cache"
    cached.joinpath("plugin.json").write_bytes(marker)

    offline = PackageArtifactFetcher(
        root=tmp_path / "artifacts",
        key_ring=ring,
        transport=httpx2.MockTransport(
            lambda request: (_ for _ in ()).throw(httpx2.ConnectError("offline"))
        ),
    )
    with pytest.raises(
        PackageArtifactError, match="^package_artifact_destination_conflict$"
    ):
        offline.fetch(entry, catalog)
    assert cached.joinpath("plugin.json").read_bytes() == marker


def test_fetch_rejects_digest_drift_and_leaves_existing_destination_untouched(
    tmp_path: Path,
) -> None:
    ring, catalog, entry, body, _transport = _fixture(tmp_path)
    del body
    bad_io = io.BytesIO()
    with zipfile.ZipFile(bad_io, "w") as archive:
        archive.writestr("plugin.json", (tmp_path / "signed-plugin" / "plugin.json").read_bytes())
        archive.writestr("skills/workspace/SKILL.md", b"# altered\n")
    bad = bad_io.getvalue()
    fetcher = PackageArtifactFetcher(
        root=tmp_path / "artifacts",
        key_ring=ring,
        transport=httpx2.MockTransport(
            lambda request: httpx2.Response(200, content=bad, request=request)
        ),
    )
    with pytest.raises(PackageArtifactError, match="^package_artifact_digest_mismatch$"):
        fetcher.fetch(entry, catalog)
    assert not (tmp_path / "artifacts" / entry.package_sha256).exists()


def test_fetch_rejects_archive_traversal_and_redirects(tmp_path: Path) -> None:
    ring, catalog, entry, _body, _transport = _fixture(tmp_path)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("../plugin.json", b"bad")
    traversal = httpx2.MockTransport(
        lambda request: httpx2.Response(200, content=output.getvalue(), request=request)
    )
    with pytest.raises(PackageArtifactError, match="^package_artifact_archive_traversal$"):
        PackageArtifactFetcher(root=tmp_path / "artifacts", key_ring=ring, transport=traversal).fetch(
            entry, catalog
        )

    redirect = httpx2.MockTransport(
        lambda request: httpx2.Response(
            302,
            headers={"location": "https://evil.example/archive.zip"},
            request=request,
        )
    )
    with pytest.raises(PackageArtifactError, match="^package_artifact_redirect_rejected$"):
        PackageArtifactFetcher(root=tmp_path / "redirect", key_ring=ring, transport=redirect).fetch(
            entry, catalog
        )


def test_fetch_rejects_entry_not_issued_by_catalog(tmp_path: Path) -> None:
    ring, catalog, entry, _body, transport = _fixture(tmp_path)
    _other_ring, other_catalog, _other_entry, _body, _other_transport = _fixture(
        tmp_path / "other"
    )
    with pytest.raises(PackageArtifactError, match="^package_artifact_catalog_context_invalid$"):
        PackageArtifactFetcher(root=tmp_path / "artifacts", key_ring=ring, transport=transport).fetch(
            entry, other_catalog
        )
