"""Signed marketplace provider-provenance verification tests."""

from __future__ import annotations

import base64
import json
from copy import deepcopy
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mcp_platform.canonical import canonical_bytes
import mcp_platform.plugin_lifecycle as plugin_lifecycle_module
from mcp_platform.plugin_lifecycle import PluginLifecycleService
from plugin_platform.marketplace_provenance import (
    ProviderProvenanceVerificationError,
    VerifiedProviderProvenance,
    verify_provider_provenance,
)
from plugin_platform.package_verifier import verify_plugin_package
from plugin_platform.trust import load_embedded_key_ring
from tests.plugin_platform.support import build_signed_plugin, ephemeral_publisher_ring

PROVENANCE_PATH = Path("plugin_platform/marketplace_provenance.json")


def _record() -> dict[str, object]:
    return {
        "plugin_id": "test.workspace",
        "provider_publisher_id": "test-provider",
        "publisher_display_name": "Test Provider",
        "provider_id": "test.workspace",
        "remote_origin": "https://mcp.example.test/mcp/v1",
        "authorization_issuer": "https://auth.example.test",
        "authoritative_source_url": "https://docs.example.test/mcp/workspace",
        "trust_tier": "omega_verified",
    }


def _signed_document(
    private_key: Ed25519PrivateKey,
    *,
    records: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    unsigned: dict[str, object] = {
        "schema_version": 1,
        "catalog_id": "test.provenance",
        "publisher_id": "test.publisher",
        "sequence": 1,
        "records": deepcopy(records if records is not None else [_record()]),
        "signing_key_id": "test-plugin-key",
    }
    signature = base64.b64encode(private_key.sign(canonical_bytes(unsigned))).decode("ascii")
    return {**unsigned, "signature": signature}


def _write(path: Path, document: dict[str, object]) -> None:
    path.write_bytes(canonical_bytes(document))


def _mcp_package(root: Path, private_key: Ed25519PrivateKey) -> Path:
    package = build_signed_plugin(root, private_key)
    manifest_path = package / "plugin.json"
    document = json.loads(manifest_path.read_bytes())
    document.update(
        {
            "kind": "mcp_backed",
            "required_apps": [
                {
                    "app_id": "test.workspace.mcp",
                    "publisher_id": "test-provider",
                    "deployment": "remote",
                    "provider_id": "test.workspace",
                    "protocol_version": "2026-07-28",
                    "transports": ["streamable_http"],
                    "remote_origin": "https://mcp.example.test/mcp/v1",
                    "authorization_issuer": "https://auth.example.test",
                    "authentication": {
                        "kind": "oauth_authorization_code_pkce",
                        "scopes": ["files.read"],
                        "secret_fields": [],
                    },
                    "capabilities": [],
                }
            ],
        }
    )
    document["entry_points"][0]["required_app_ids"] = ["test.workspace.mcp"]
    unsigned = {key: value for key, value in document.items() if key != "signature"}
    document["signature"] = base64.b64encode(
        private_key.sign(canonical_bytes(unsigned))
    ).decode("ascii")
    manifest_path.write_bytes(canonical_bytes(document))
    return package


def test_signed_provenance_binds_exact_provider_boundary() -> None:
    verified = verify_provider_provenance(PROVENANCE_PATH, load_embedded_key_ring())

    github = verified.for_plugin("github.official")

    assert github.provider_publisher_id == "github"
    assert github.remote_origin == "https://api.githubcopilot.com/mcp/"
    assert github.trust_tier == "omega_verified"


def test_verified_provenance_rejects_post_verification_record_mutation() -> None:
    verified = verify_provider_provenance(PROVENANCE_PATH, load_embedded_key_ring())
    github = next(
        record for record in verified.records if record.plugin_id == "github.official"
    )
    object.__setattr__(github, "publisher_display_name", "Forged Provider")

    with pytest.raises(
        ProviderProvenanceVerificationError,
        match="^provenance_invalid$",
    ):
        verified.for_plugin("github.official")


def test_verified_provenance_rejects_stateful_records_after_validation() -> None:
    verified = verify_provider_provenance(PROVENANCE_PATH, load_embedded_key_ring())
    signed_records = verified.records
    forged_records = tuple(
        type(record).from_dict(
            {
                **record.to_dict(),
                "publisher_display_name": (
                    "Forged Provider"
                    if record.plugin_id == "github.official"
                    else record.publisher_display_name
                ),
            }
        )
        for record in signed_records
    )

    class StatefulRecords:
        def __init__(self) -> None:
            self.iterations = 0

        def __iter__(self):
            self.iterations += 1
            return iter(signed_records if self.iterations == 1 else forged_records)

    object.__setattr__(verified, "records", StatefulRecords())

    with pytest.raises(
        ProviderProvenanceVerificationError,
        match="^provenance_invalid$",
    ):
        verified.for_plugin("github.official")


def test_verified_provenance_rejects_post_verification_document_mutation() -> None:
    verified = verify_provider_provenance(PROVENANCE_PATH, load_embedded_key_ring())
    object.__setattr__(verified, "catalog_id", "forged.catalog")

    with pytest.raises(
        ProviderProvenanceVerificationError,
        match="^provenance_invalid$",
    ):
        verified.for_plugin("github.official")


def test_lifecycle_catalog_fails_closed_before_signed_provenance_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = json.loads(PROVENANCE_PATH.read_text(encoding="utf-8"))
    document["records"][0]["publisher_display_name"] = "Tampered Provider"
    path = tmp_path / "marketplace-provenance.json"
    path.write_bytes(canonical_bytes(document))
    monkeypatch.setattr(plugin_lifecycle_module, "_PROVENANCE_PATH", path)

    service = PluginLifecycleService(state_root=tmp_path / "state")

    with pytest.raises(RuntimeError, match="^plugin_catalog_unavailable$"):
        service.catalog_source().snapshot()


def test_provenance_rejects_signature_tampering_and_duplicate_plugin_ids(
    tmp_path: Path,
) -> None:
    private_key, ring = ephemeral_publisher_ring()
    path = tmp_path / "provenance.json"
    tampered = _signed_document(private_key)
    tampered["records"][0]["remote_origin"] = "https://other.example.test/mcp/v1"
    _write(path, tampered)

    with pytest.raises(ProviderProvenanceVerificationError, match="^invalid_signature$"):
        verify_provider_provenance(path, ring)

    duplicate = _record()
    duplicate["authorization_issuer"] = "https://other-auth.example.test"
    _write(path, _signed_document(private_key, records=[_record(), duplicate]))

    with pytest.raises(ProviderProvenanceVerificationError, match="^provenance_invalid$"):
        verify_provider_provenance(path, ring)


def test_provenance_binds_endpoint_and_authorization_to_verified_package(
    tmp_path: Path,
) -> None:
    private_key, ring = ephemeral_publisher_ring()
    package = _mcp_package(tmp_path, private_key)
    verified_package = verify_plugin_package(package, ring, "3.0.0")
    path = tmp_path / "provenance.json"
    _write(path, _signed_document(private_key))
    provenance = verify_provider_provenance(path, ring)

    assert provenance.for_verified_plugin(verified_package).plugin_id == "test.workspace"

    for field, drifted_value in (
        ("provider_publisher_id", "other-provider"),
        ("provider_id", "other.workspace"),
        ("remote_origin", "https://other.example.test/mcp/v1"),
        ("authorization_issuer", "https://other-auth.example.test"),
    ):
        drifted = _record()
        drifted[field] = drifted_value
        _write(path, _signed_document(private_key, records=[drifted]))
        verified_drift = verify_provider_provenance(path, ring)
        with pytest.raises(
            ProviderProvenanceVerificationError,
            match="^provider_provenance_mismatch$",
        ):
            verified_drift.for_verified_plugin(verified_package)


def test_verified_provenance_cannot_be_constructed_or_used_for_an_unknown_plugin(
    tmp_path: Path,
) -> None:
    private_key, ring = ephemeral_publisher_ring()
    path = tmp_path / "provenance.json"
    _write(path, _signed_document(private_key))
    verified = verify_provider_provenance(path, ring)

    with pytest.raises(TypeError, match=r"^use verify_provider_provenance\(\)$"):
        VerifiedProviderProvenance()
    with pytest.raises(
        ProviderProvenanceVerificationError,
        match="^provider_provenance_not_found$",
    ):
        verified.for_plugin("missing.plugin")


def test_installed_first_party_plugin_fails_closed_after_provenance_withdrawal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = PluginLifecycleService(state_root=tmp_path / "state")
    review = service.review("google.workspace.drive")
    service.install("google.workspace.drive", confirmation=review.sha256)

    class WithdrawnProvenance:
        publisher_id = "omega.first_party"

        def for_plugin(self, plugin_id: str) -> None:
            raise ProviderProvenanceVerificationError("provider_provenance_not_found")

        def for_verified_plugin(self, plugin: object) -> None:
            raise ProviderProvenanceVerificationError("provider_provenance_not_found")

    monkeypatch.setattr(service, "_verified_provenance", lambda: WithdrawnProvenance())

    for consumer in (
        lambda: service._installed_verified("google.workspace.drive"),
        lambda: service._offered_provider_scopes(
            "google.workspace.drive",
            "google.workspace.drive.mcp",
        ),
        lambda: service.descriptor_for_app("google.workspace.drive.mcp"),
    ):
        with pytest.raises(
            ProviderProvenanceVerificationError,
            match="^provider_provenance_not_found$",
        ):
            consumer()
    service.close()


@pytest.mark.parametrize(
    "url",
    [
        # Four of the twelve providers onboarded on 2026-09-07 publish their MCP
        # documentation at a directory URL. A trailing slash is how the web
        # spells "this is a page of docs", and refusing it says nothing about
        # whether the URL is safe to record.
        "https://docs.sentry.io/product/sentry-mcp/",
        "https://developer.paypal.com/tools/mcp-server/",
        "https://grafana.com/docs/grafana/latest/observability-as-code/mcp-server/",
        "https://developers.cloudflare.com/agents/model-context-protocol/",
        # Still accepted without one.
        "https://linear.app/docs/mcp",
    ],
)
def test_a_documentation_url_may_name_a_directory(url: str) -> None:
    from plugin_platform.marketplace_provenance import _authoritative_source_url

    assert _authoritative_source_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        # Widening the tail must not widen the middle.
        "https://docs.example.test/a//b",
        "https://docs.example.test/a/../b",
        "https://docs.example.test/a/./b",
        "https://docs.example.test/",
        "https://docs.example.test",
        "http://docs.example.test/mcp",
        "https://docs.example.test/mcp?q=1",
        "https://docs.example.test/mcp#frag",
    ],
)
def test_a_documentation_url_outside_the_grammar_is_still_refused(url: str) -> None:
    from plugin_platform.marketplace_provenance import _authoritative_source_url

    with pytest.raises(ValueError, match="^provenance_invalid$"):
        _authoritative_source_url(url)
