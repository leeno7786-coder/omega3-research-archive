"""Signed OAuth profile contracts and exact-match registry tests."""

from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from hashlib import sha256

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mcp_platform.canonical import canonical_bytes
from mcp_platform.oauth_profiles import (
    OAuthAuthorizationFlow,
    OAuthClientMode,
    OAuthProfile,
    OAuthProfileCatalog,
    OAuthProfileMatchInput,
    OAuthProfileRegistry,
)
from plugin_platform.trust import PublisherTrustError, TrustedPublisherKeyRing


def _profile_revision(profile: dict[str, object]) -> str:
    unsigned = {key: deepcopy(value) for key, value in profile.items() if key != "revision"}
    return f"sha256:{sha256(canonical_bytes(unsigned)).hexdigest()}"


def _preregistered_profile() -> dict[str, object]:
    profile: dict[str, object] = {
        "schema_version": 1,
        "profile_id": "google.desktop.drive",
        "catalog_signer": "omega.featured",
        "allowed_plugin_publishers": ["omega.first_party"],
        "app_publisher": "google",
        "provider_id": "google.workspace.drive",
        "authorization_issuer": "https://accounts.google.com",
        "resource_origins": ["https://drivemcp.googleapis.com/mcp/v1"],
        "client_mode": "preregistered",
        "authorization_flow": "authorization_code_pkce",
        "client_id": "google-public-desktop-client",
        "client_secret_ref": "oauth-client/google.desktop/secret",
        "redirect_uri": "http://127.0.0.1:13310/oauth/google.desktop.drive/callback",
        "account_partition": "issuer_account",
    }
    profile["revision"] = _profile_revision(profile)
    return profile


def _device_profile() -> dict[str, object]:
    profile = _preregistered_profile()
    profile.update(
        {
            "profile_id": "github.desktop",
            "app_publisher": "github",
            "provider_id": "github.official",
            "authorization_issuer": "https://github.com/login/oauth",
            "resource_origins": ["https://api.githubcopilot.com/mcp/"],
            "client_id": "github-public-client",
            "authorization_flow": "device_code",
        }
    )
    profile.pop("client_secret_ref")
    profile.pop("redirect_uri")
    profile["revision"] = _profile_revision(profile)
    return profile


def _client_metadata_profile() -> dict[str, object]:
    profile = _preregistered_profile()
    profile["profile_id"] = "google.desktop.drive.metadata"
    profile["client_mode"] = "client_metadata_document"
    profile.pop("client_id")
    profile.pop("client_secret_ref")
    profile["client_metadata_document_url"] = (
        "https://oauth-metadata.omega2ai.com/google/drive.json"
    )
    profile["revision"] = _profile_revision(profile)
    return profile


def _dynamic_registration_profile(**overrides: object) -> dict[str, object]:
    profile: dict[str, object] = {
        "schema_version": 1,
        "profile_id": "notion.desktop",
        "catalog_signer": "omega.featured",
        "allowed_plugin_publishers": ["com.notion"],
        "app_publisher": "notion",
        "provider_id": "com.notion.mcp",
        "authorization_issuer": "https://mcp.notion.com",
        "resource_origins": ["https://mcp.notion.com/mcp"],
        "client_mode": "dynamic_registration",
        "authorization_flow": "authorization_code_pkce",
        "account_partition": "issuer_account",
        "redirect_uri": "http://127.0.0.1:13310/oauth/notion.desktop/callback",
        "registration": {
            "client_name": "Omega",
            "redirect_uris": ["http://127.0.0.1:13310/oauth/notion.desktop/callback"],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "scope": "",
            "software_id": "org.omega2ai.desktop",
        },
    }
    profile.update(overrides)
    profile["revision"] = _profile_revision(profile)
    return profile


def _sign_catalog(
    private_key: Ed25519PrivateKey,
    *,
    profiles: list[dict[str, object]] | None = None,
    sequence: int = 1,
) -> dict[str, object]:
    unsigned: dict[str, object] = {
        "schema_version": 1,
        "catalog_id": "omega.featured",
        "publisher_id": "omega.release",
        "sequence": sequence,
        "signing_key_id": "omega-featured-oauth-2026",
        "profiles": deepcopy(profiles if profiles is not None else [_preregistered_profile()]),
    }
    signature = base64.b64encode(private_key.sign(canonical_bytes(unsigned))).decode("ascii")
    return {**unsigned, "signature": signature}


def _resign_catalog(
    document: dict[str, object], private_key: Ed25519PrivateKey
) -> dict[str, object]:
    unsigned = {key: deepcopy(value) for key, value in document.items() if key != "signature"}
    signature = base64.b64encode(private_key.sign(canonical_bytes(unsigned))).decode("ascii")
    return {**unsigned, "signature": signature}


@pytest.fixture
def signing_material() -> tuple[Ed25519PrivateKey, TrustedPublisherKeyRing]:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    key_ring = TrustedPublisherKeyRing.from_dict(
        {
            "schema_version": 1,
            "keys": [
                {
                    "key_id": "omega-featured-oauth-2026",
                    "publisher_id": "omega.release",
                    "public_key_base64": base64.b64encode(public_key).decode("ascii"),
                }
            ],
            "revoked_key_ids": [],
            "revoked_publisher_ids": [],
            "revoked_subject_ids": [],
        }
    )
    return private_key, key_ring


@pytest.fixture
def private_key(
    signing_material: tuple[Ed25519PrivateKey, TrustedPublisherKeyRing],
) -> Ed25519PrivateKey:
    return signing_material[0]


@pytest.fixture
def key_ring(
    signing_material: tuple[Ed25519PrivateKey, TrustedPublisherKeyRing],
) -> TrustedPublisherKeyRing:
    return signing_material[1]


@pytest.fixture
def signed_profile_catalog(private_key: Ed25519PrivateKey) -> dict[str, object]:
    return _sign_catalog(private_key)


def _drive_match(**updates: str) -> OAuthProfileMatchInput:
    values = {
        "catalog_signer": "omega.featured",
        "plugin_publisher": "omega.first_party",
        "app_publisher": "google",
        "provider_id": "google.workspace.drive",
        "authorization_issuer": "https://accounts.google.com",
        "resource_origin": "https://drivemcp.googleapis.com/mcp/v1",
    }
    values.update(updates)
    return OAuthProfileMatchInput(**values)


def test_registry_resolves_only_the_full_signed_identity_tuple(
    signed_profile_catalog: dict[str, object],
    key_ring: TrustedPublisherKeyRing,
) -> None:
    """Catches prefix, suffix, alias, path, and port matches on signed identity."""
    registry = OAuthProfileRegistry.from_document(signed_profile_catalog, key_ring)
    match = _drive_match()

    profile = registry.resolve(match)

    assert profile.profile_id == "google.desktop.drive"
    assert profile.client_mode is OAuthClientMode.PREREGISTERED
    assert profile.client_secret_ref == "oauth-client/google.desktop/secret"
    assert "secret-value" not in repr(profile)

    attacks = (
        replace(match, catalog_signer="omega.featured.evil"),
        replace(match, plugin_publisher="omega.first_party.evil"),
        replace(match, app_publisher="google.evil"),
        replace(match, provider_id="google.workspace"),
        replace(match, authorization_issuer="https://accounts.google.com.evil"),
        replace(match, resource_origin="https://drivemcp.googleapis.com/mcp"),
        replace(match, resource_origin="https://drivemcp.googleapis.com/mcp/v1/"),
        replace(match, resource_origin="https://drivemcp.googleapis.com:444/mcp/v1"),
    )
    for attack in attacks:
        with pytest.raises(ValueError, match="^oauth_profile_unavailable$"):
            registry.resolve(attack)


def test_signed_profile_declares_exact_public_client_authorization_flow(
    private_key: Ed25519PrivateKey,
    key_ring: TrustedPublisherKeyRing,
) -> None:
    registry = OAuthProfileRegistry.from_document(
        _sign_catalog(private_key, profiles=[_device_profile()]),
        key_ring,
    )

    profile = registry.resolve(
        OAuthProfileMatchInput(
            catalog_signer="omega.featured",
            plugin_publisher="omega.first_party",
            app_publisher="github",
            provider_id="github.official",
            authorization_issuer="https://github.com/login/oauth",
            resource_origin="https://api.githubcopilot.com/mcp/",
        )
    )

    assert profile.authorization_flow is OAuthAuthorizationFlow.DEVICE_CODE
    assert profile.client_secret_ref is None
    assert profile.redirect_uri is None


@pytest.mark.parametrize(
    "mutation",
    [
        {"authorization_flow": "device_code", "redirect_uri": _preregistered_profile()["redirect_uri"]},
        {"authorization_flow": "device_code", "client_secret_ref": "oauth-client/github.desktop/secret"},
        {"authorization_flow": "authorization_code_pkce", "redirect_uri": None},
    ],
)
def test_profile_rejects_flow_specific_secret_or_redirect_confusion(
    mutation: dict[str, object],
    private_key: Ed25519PrivateKey,
    key_ring: TrustedPublisherKeyRing,
) -> None:
    profile = _device_profile() if mutation["authorization_flow"] == "device_code" else _preregistered_profile()
    if mutation.get("redirect_uri") is None:
        profile.pop("redirect_uri", None)
    else:
        profile["redirect_uri"] = mutation["redirect_uri"]
    if "client_secret_ref" in mutation:
        profile["client_secret_ref"] = mutation["client_secret_ref"]
    profile["revision"] = _profile_revision(profile)

    with pytest.raises(ValueError, match="oauth_profile_(authorization_flow|client_mode_fields)_invalid"):
        OAuthProfileRegistry.from_document(
            _sign_catalog(private_key, profiles=[profile]),
            key_ring,
        )


def test_catalog_verifies_signature_before_parsing_profiles(
    signed_profile_catalog: dict[str, object],
    key_ring: TrustedPublisherKeyRing,
) -> None:
    """Catches profile parsing before the catalog signer has authenticated it."""
    document = deepcopy(signed_profile_catalog)
    document["profiles"] = "untrusted malformed profiles"
    document["signature"] = base64.b64encode(b"x" * 64).decode("ascii")

    with pytest.raises(PublisherTrustError, match="^invalid_signature$"):
        OAuthProfileRegistry.from_document(document, key_ring)


def test_catalog_rejects_duplicate_profile_ids(
    private_key: Ed25519PrivateKey,
    key_ring: TrustedPublisherKeyRing,
) -> None:
    """Catches selecting one of two signed definitions for the same profile ID."""
    profile = _preregistered_profile()
    document = _sign_catalog(private_key, profiles=[profile, deepcopy(profile)])

    with pytest.raises(ValueError, match="^oauth_profile_catalog_invalid$"):
        OAuthProfileRegistry.from_document(document, key_ring)


def test_registry_rejects_an_ambiguous_full_tuple(
    private_key: Ed25519PrivateKey,
    key_ring: TrustedPublisherKeyRing,
) -> None:
    """Catches silently choosing among two distinct profiles with one identity tuple."""
    first = _preregistered_profile()
    second = deepcopy(first)
    second["profile_id"] = "google.desktop.drive.alternate"
    second["client_id"] = "alternate-public-client"
    second["revision"] = _profile_revision(second)
    registry = OAuthProfileRegistry.from_document(
        _sign_catalog(private_key, profiles=[first, second]), key_ring
    )

    with pytest.raises(ValueError, match="^oauth_profile_ambiguous$"):
        registry.resolve(_drive_match())


@pytest.mark.parametrize(
    "revision",
    [
        "sha256:" + "A" * 64,
        "sha256:" + "0" * 64,
        "0" * 64,
        True,
    ],
)
def test_catalog_rejects_invalid_or_stale_profile_revision(
    revision: object,
    private_key: Ed25519PrivateKey,
    key_ring: TrustedPublisherKeyRing,
) -> None:
    """Catches malformed, bool, unprefixed, or content-stale profile revisions."""
    profile = _preregistered_profile()
    profile["revision"] = revision
    document = _sign_catalog(private_key, profiles=[profile])

    with pytest.raises(ValueError, match="^oauth_profile_revision_(invalid|mismatch)$"):
        OAuthProfileRegistry.from_document(document, key_ring)


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "http://example.com:13310/oauth/google.desktop.drive/callback",
        "http://localhost:13310/oauth/google.desktop.drive/callback",
        "http://127.0.0.1:13310/",
    ],
)
def test_desktop_profile_rejects_invalid_loopback_redirect(
    redirect_uri: str,
    private_key: Ed25519PrivateKey,
    key_ring: TrustedPublisherKeyRing,
) -> None:
    """Catches non-IP loopback, non-loopback, and root-only desktop callbacks."""
    profile = _preregistered_profile()
    profile["redirect_uri"] = redirect_uri
    profile["revision"] = _profile_revision(profile)

    with pytest.raises(ValueError, match="^oauth_profile_redirect_uri_invalid$"):
        OAuthProfileRegistry.from_document(_sign_catalog(private_key, profiles=[profile]), key_ring)


def test_desktop_profile_rejects_https_redirect(
    private_key: Ed25519PrivateKey,
    key_ring: TrustedPublisherKeyRing,
) -> None:
    """Catches treating a hosted HTTPS callback as a desktop loopback callback."""
    profile = _preregistered_profile()
    profile["redirect_uri"] = "https://127.0.0.1:13310/oauth/google.desktop.drive/callback"
    profile["revision"] = _profile_revision(profile)

    with pytest.raises(ValueError, match="^oauth_profile_redirect_uri_invalid$"):
        OAuthProfileRegistry.from_document(_sign_catalog(private_key, profiles=[profile]), key_ring)


def test_loopback_host_case_is_lowered_without_rewriting_ipv6_text(
    private_key: Ed25519PrivateKey,
    key_ring: TrustedPublisherKeyRing,
) -> None:
    """Catches normalizing an IP host beyond the permitted lowercase operation."""
    profile = _preregistered_profile()
    profile["redirect_uri"] = "http://[0:0:0:0:0:0:0:1]:13310/oauth/google.desktop.drive/callback"
    profile["revision"] = _profile_revision(profile)

    registry = OAuthProfileRegistry.from_document(
        _sign_catalog(private_key, profiles=[profile]), key_ring
    )

    assert registry.resolve(_drive_match()).redirect_uri == profile["redirect_uri"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("client_secret", "secret-value"),
        ("client_secret_ref", "secret-value"),
    ],
)
def test_profile_rejects_embedded_secret_values(
    field: str,
    value: str,
    private_key: Ed25519PrivateKey,
    key_ring: TrustedPublisherKeyRing,
) -> None:
    """Catches a credential value masquerading as profile data or a vault reference."""
    profile = _preregistered_profile()
    profile[field] = value
    profile["revision"] = _profile_revision(profile)

    with pytest.raises(ValueError) as caught:
        OAuthProfileRegistry.from_document(_sign_catalog(private_key, profiles=[profile]), key_ring)

    assert value not in str(caught.value)


def test_client_metadata_document_mode_uses_only_its_exact_https_url(
    private_key: Ed25519PrivateKey,
    key_ring: TrustedPublisherKeyRing,
) -> None:
    """Catches cross-mode fallback to preregistered fields or dynamic registration."""
    profile_document = _client_metadata_profile()
    registry = OAuthProfileRegistry.from_document(
        _sign_catalog(private_key, profiles=[profile_document]), key_ring
    )

    profile = registry.resolve(_drive_match())

    assert profile.client_mode is OAuthClientMode.CLIENT_METADATA_DOCUMENT
    assert profile.client_id is None
    assert profile.client_secret_ref is None
    assert profile.client_metadata_document_url == (
        "https://oauth-metadata.omega2ai.com/google/drive.json"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("client_id", "forbidden-public-client"),
        ("client_secret_ref", "oauth-client/google.desktop/secret"),
        ("client_id", None),
        ("client_secret_ref", None),
    ],
)
def test_client_metadata_document_mode_rejects_preregistered_fields(
    field: str,
    value: str | None,
    private_key: Ed25519PrivateKey,
    key_ring: TrustedPublisherKeyRing,
) -> None:
    """Catches mode confusion that permits a preregistered fallback path."""
    profile = _client_metadata_profile()
    profile[field] = value
    profile["revision"] = _profile_revision(profile)

    with pytest.raises(ValueError, match="^oauth_profile_client_mode_fields_invalid$"):
        OAuthProfileRegistry.from_document(_sign_catalog(private_key, profiles=[profile]), key_ring)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("client_secret_ref", None),
        ("client_metadata_document_url", None),
    ],
)
def test_preregistered_mode_rejects_null_optional_or_cross_mode_fields(
    field: str,
    value: None,
    private_key: Ed25519PrivateKey,
    key_ring: TrustedPublisherKeyRing,
) -> None:
    """Catches treating a present null field as if the signed field were absent."""
    profile = _preregistered_profile()
    profile[field] = value
    profile["revision"] = _profile_revision(profile)

    with pytest.raises(ValueError, match="^oauth_profile_client_mode_fields_invalid$"):
        OAuthProfileRegistry.from_document(_sign_catalog(private_key, profiles=[profile]), key_ring)


def test_client_metadata_document_mode_rejects_non_https_url(
    private_key: Ed25519PrivateKey,
    key_ring: TrustedPublisherKeyRing,
) -> None:
    """Catches transport downgrade of the exact client metadata document."""
    profile = _client_metadata_profile()
    profile["client_metadata_document_url"] = "http://oauth-metadata.omega2ai.com/google/drive.json"
    profile["revision"] = _profile_revision(profile)

    with pytest.raises(ValueError, match="^oauth_profile_metadata_url_invalid$"):
        OAuthProfileRegistry.from_document(_sign_catalog(private_key, profiles=[profile]), key_ring)


@pytest.mark.parametrize(
    ("profile_kind", "field", "value"),
    [
        ("preregistered", "authorization_issuer", "https://user@accounts.google.com"),
        ("preregistered", "authorization_issuer", "https://accounts.google.com?evil=1"),
        ("preregistered", "authorization_issuer", "https://accounts.google.com#evil"),
        (
            "preregistered",
            "resource_origins",
            ["https://user@drivemcp.googleapis.com/mcp/v1"],
        ),
        (
            "preregistered",
            "resource_origins",
            ["https://drivemcp.googleapis.com/mcp/v1?evil=1"],
        ),
        (
            "preregistered",
            "redirect_uri",
            "http://127.0.0.1:13310/oauth/google.desktop.drive/callback#evil",
        ),
        (
            "metadata",
            "client_metadata_document_url",
            "https://user@oauth-metadata.omega2ai.com/google/drive.json",
        ),
        (
            "metadata",
            "client_metadata_document_url",
            "https://oauth-metadata.omega2ai.com/google/drive.json?evil=1",
        ),
    ],
)
def test_profile_rejects_userinfo_query_and_fragment_url_attacks(
    profile_kind: str,
    field: str,
    value: object,
    private_key: Ed25519PrivateKey,
    key_ring: TrustedPublisherKeyRing,
) -> None:
    """Catches URL authority confusion through userinfo, query, or fragment material."""
    profile = _client_metadata_profile() if profile_kind == "metadata" else _preregistered_profile()
    profile[field] = value
    profile["revision"] = _profile_revision(profile)

    with pytest.raises(ValueError, match="^oauth_profile_.*_invalid$"):
        OAuthProfileRegistry.from_document(_sign_catalog(private_key, profiles=[profile]), key_ring)


def test_issuer_path_is_canonical_and_matched_exactly(
    signed_profile_catalog: dict[str, object],
    private_key: Ed25519PrivateKey,
    key_ring: TrustedPublisherKeyRing,
) -> None:
    """RFC 8414 permits an issuer path; signed matching must retain it exactly."""
    document = deepcopy(signed_profile_catalog)
    profiles = document["profiles"]
    assert isinstance(profiles, list)
    profile = profiles[0]
    assert isinstance(profile, dict)
    profile["authorization_issuer"] = "https://accounts.google.com/oauth2"
    profile["revision"] = _profile_revision(profile)

    path_registry = OAuthProfileRegistry.from_document(
        _resign_catalog(document, private_key), key_ring
    )
    assert path_registry.resolve(
        _drive_match(authorization_issuer="https://accounts.google.com/oauth2")
    ).authorization_issuer == "https://accounts.google.com/oauth2"

    root_registry = OAuthProfileRegistry.from_document(signed_profile_catalog, key_ring)
    with pytest.raises(ValueError, match="^oauth_profile_unavailable$"):
        root_registry.resolve(_drive_match(authorization_issuer="https://accounts.google.com/oauth2"))


def test_match_normalizes_only_scheme_and_idna_host_case(
    signed_profile_catalog: dict[str, object],
    key_ring: TrustedPublisherKeyRing,
) -> None:
    """Catches case-sensitive authority comparison while retaining exact path matching."""
    registry = OAuthProfileRegistry.from_document(signed_profile_catalog, key_ring)

    profile = registry.resolve(
        _drive_match(
            authorization_issuer="HTTPS://ACCOUNTS.GOOGLE.COM",
            resource_origin="HTTPS://DRIVEMCP.GOOGLEAPIS.COM/mcp/v1",
        )
    )

    assert profile.profile_id == "google.desktop.drive"


def test_match_removes_only_an_explicit_default_port(
    signed_profile_catalog: dict[str, object],
    key_ring: TrustedPublisherKeyRing,
) -> None:
    """Catches failing to canonicalize HTTPS :443 while preserving path bytes."""
    registry = OAuthProfileRegistry.from_document(signed_profile_catalog, key_ring)

    profile = registry.resolve(
        _drive_match(
            authorization_issuer="https://accounts.google.com:443",
            resource_origin="https://drivemcp.googleapis.com:443/mcp/v1",
        )
    )

    assert profile.profile_id == "google.desktop.drive"


def test_non_default_port_is_preserved_and_matched_exactly(
    private_key: Ed25519PrivateKey,
    key_ring: TrustedPublisherKeyRing,
) -> None:
    """Catches dropping a non-default port into the ordinary provider authority."""
    profile = _preregistered_profile()
    profile["resource_origins"] = ["https://drivemcp.googleapis.com:444/mcp/v1"]
    profile["revision"] = _profile_revision(profile)
    registry = OAuthProfileRegistry.from_document(
        _sign_catalog(private_key, profiles=[profile]), key_ring
    )

    assert (
        registry.resolve(
            _drive_match(resource_origin="https://drivemcp.googleapis.com:444/mcp/v1")
        ).profile_id
        == "google.desktop.drive"
    )
    with pytest.raises(ValueError, match="^oauth_profile_unavailable$"):
        registry.resolve(_drive_match())


def test_catalog_signature_and_profile_revision_change_with_their_content(
    signed_profile_catalog: dict[str, object],
    private_key: Ed25519PrivateKey,
    key_ring: TrustedPublisherKeyRing,
) -> None:
    """Catches stale catalog signatures or stale exact profile revision lookups."""
    original = OAuthProfileRegistry.from_document(signed_profile_catalog, key_ring)
    old_profile = original.resolve(_drive_match())

    changed_sequence = deepcopy(signed_profile_catalog)
    changed_sequence["sequence"] = 2
    with pytest.raises(PublisherTrustError, match="^invalid_signature$"):
        OAuthProfileRegistry.from_document(changed_sequence, key_ring)

    changed_profile_document = deepcopy(signed_profile_catalog)
    profiles = changed_profile_document["profiles"]
    assert isinstance(profiles, list)
    profile = profiles[0]
    assert isinstance(profile, dict)
    profile["client_id"] = "replacement-public-client"
    profile["revision"] = _profile_revision(profile)
    changed_profile_document["sequence"] = 2
    changed = OAuthProfileRegistry.from_document(
        _resign_catalog(changed_profile_document, private_key), key_ring
    )
    new_profile = changed.resolve(_drive_match())

    assert new_profile.revision != old_profile.revision
    with pytest.raises(ValueError, match="^oauth_profile_unavailable$"):
        changed.get_exact(old_profile.profile_id, old_profile.revision)
    assert changed.get_exact(new_profile.profile_id, new_profile.revision) == new_profile


def test_profile_contracts_are_immutable(
    signed_profile_catalog: dict[str, object],
    key_ring: TrustedPublisherKeyRing,
) -> None:
    """Catches callers mutating a verified profile after exact resolution."""
    registry = OAuthProfileRegistry.from_document(signed_profile_catalog, key_ring)
    profile = registry.resolve(_drive_match())
    catalog = OAuthProfileCatalog.from_signed_dict(signed_profile_catalog, key_ring)

    with pytest.raises(FrozenInstanceError):
        profile.profile_id = "attacker.profile"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        catalog.sequence = 2  # type: ignore[misc]


def test_dynamic_registration_mode_accepts_only_the_registration_block() -> None:
    profile = OAuthProfile.from_verified_dict(
        _dynamic_registration_profile(), catalog_signer="omega.featured"
    )
    assert profile.client_mode is OAuthClientMode.DYNAMIC_REGISTRATION
    assert profile.client_id is None
    assert profile.client_secret_ref is None
    assert profile.client_metadata_document_url is None
    assert profile.registration.software_id == "org.omega2ai.desktop"


@pytest.mark.parametrize(
    "overrides",
    [
        {"client_id": "abc"},
        {"client_secret_ref": "oauth-client/notion/secret"},
        {"client_metadata_document_url": "https://example.com/client.json"},
    ],
)
def test_dynamic_registration_mode_rejects_any_other_mode_field(
    overrides: dict[str, object],
) -> None:
    document = _dynamic_registration_profile(**overrides)
    with pytest.raises(ValueError, match="oauth_profile_client_mode_fields_invalid"):
        OAuthProfile.from_verified_dict(document, catalog_signer="omega.featured")


def test_dynamic_registration_requires_authorization_code_pkce() -> None:
    document = _dynamic_registration_profile(authorization_flow="device_code", redirect_uri=None)
    document["registration"]["redirect_uris"] = []
    with pytest.raises(ValueError, match="oauth_profile_authorization_flow_invalid"):
        OAuthProfile.from_verified_dict(document, catalog_signer="omega.featured")


def test_dynamic_registration_template_redirect_must_match_profile_redirect() -> None:
    document = _dynamic_registration_profile()
    document["registration"]["redirect_uris"] = [
        "http://127.0.0.1:13310/oauth/other.desktop/callback"
    ]
    with pytest.raises(ValueError, match="oauth_dynamic_registration_template_invalid"):
        OAuthProfile.from_verified_dict(document, catalog_signer="omega.featured")


def test_dynamic_registration_template_rejects_a_nonfixed_field() -> None:
    document = _dynamic_registration_profile()
    document["registration"]["client_name"] = "Not Omega"
    with pytest.raises(ValueError, match="oauth_dynamic_registration_template_invalid"):
        OAuthProfile.from_verified_dict(document, catalog_signer="omega.featured")


# ---------------------------------------------------------------------------
# HTTPS relay redirects (vendors that refuse plain-HTTP loopback callbacks)
# ---------------------------------------------------------------------------


def test_desktop_profile_accepts_the_https_relay_redirect(
    private_key: Ed25519PrivateKey,
    key_ring: TrustedPublisherKeyRing,
) -> None:
    """The relay form is the one HTTPS redirect a profile may declare."""
    from mcp_platform.oauth_profiles import callback_landing_uri

    profile = _preregistered_profile()
    profile["redirect_uri"] = "https://plugins.omega2ai.com/oauth/google.desktop.drive/callback"
    profile["revision"] = _profile_revision(profile)

    registry = OAuthProfileRegistry.from_document(
        _sign_catalog(private_key, profiles=[profile]), key_ring
    )

    loaded = registry.get_exact(profile["profile_id"], profile["revision"])
    assert loaded.redirect_uri == "https://plugins.omega2ai.com/oauth/google.desktop.drive/callback"
    assert callback_landing_uri(loaded.redirect_uri) == (
        "http://127.0.0.1:13310/oauth/google.desktop.drive/callback"
    )
    loopback = "http://127.0.0.1:13310/oauth/google.desktop.drive/callback"
    assert callback_landing_uri(loopback) == loopback


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "https://plugins.omega2ai.com/oauth/google.desktop.drive",
        "https://plugins.omega2ai.com/oauth/google.desktop.drive/callback/extra",
        "https://plugins.omega2ai.com/callback",
        "https://plugins.omega2ai.com:8443/oauth/google.desktop.drive/callback",
        "https://evil.omega2ai.com/oauth/google.desktop.drive/callback",
        "https://omega2ai.com/oauth/google.desktop.drive/callback",
        "http://plugins.omega2ai.com/oauth/google.desktop.drive/callback",
    ],
)
def test_desktop_profile_rejects_every_other_hosted_redirect(
    redirect_uri: str,
    private_key: Ed25519PrivateKey,
    key_ring: TrustedPublisherKeyRing,
) -> None:
    """Only the exact relay origin and callback path shape are admitted."""
    profile = _preregistered_profile()
    profile["redirect_uri"] = redirect_uri
    profile["revision"] = _profile_revision(profile)
    with pytest.raises(ValueError, match="^oauth_profile_redirect_uri_invalid$"):
        OAuthProfileRegistry.from_document(_sign_catalog(private_key, profiles=[profile]), key_ring)


# ---------------------------------------------------------------------------
# RFC 9728 resource identifier distinct from the transport endpoint
# ---------------------------------------------------------------------------


def test_profile_may_name_a_resource_identifier_its_endpoint_lives_under(
    private_key: Ed25519PrivateKey,
    key_ring: TrustedPublisherKeyRing,
) -> None:
    """Slack publishes its metadata under the origin, not the endpoint path."""
    profile = _preregistered_profile()
    profile["resource_origins"] = ["https://mcp.example.com/mcp"]
    profile["resource_identifier"] = "https://mcp.example.com"
    profile["revision"] = _profile_revision(profile)

    registry = OAuthProfileRegistry.from_document(
        _sign_catalog(private_key, profiles=[profile]), key_ring
    )

    loaded = registry.get_exact(profile["profile_id"], profile["revision"])
    assert loaded.resource_identifier == "https://mcp.example.com"
    assert loaded.identifier_for("https://mcp.example.com/mcp") == "https://mcp.example.com"
    with pytest.raises(ValueError, match="^oauth_resource_mismatch$"):
        loaded.identifier_for("https://mcp.example.com/other")


def test_a_profile_without_an_identifier_lets_each_endpoint_identify_itself(
    private_key: Ed25519PrivateKey,
    key_ring: TrustedPublisherKeyRing,
) -> None:
    """Every provider shipped before the split relies on this exact behaviour."""
    profile = _preregistered_profile()
    profile["revision"] = _profile_revision(profile)

    registry = OAuthProfileRegistry.from_document(
        _sign_catalog(private_key, profiles=[profile]), key_ring
    )

    loaded = registry.get_exact(profile["profile_id"], profile["revision"])
    assert loaded.resource_identifier is None
    origin = loaded.resource_origins[0]
    assert loaded.identifier_for(origin) == origin


@pytest.mark.parametrize(
    "identifier",
    [
        "https://elsewhere.example.com",
        "https://mcp.example.com:8443",
        "http://mcp.example.com",
        "https://mcp.example.com.evil.test",
    ],
)
def test_a_resource_identifier_may_not_leave_its_endpoint_origin(
    identifier: str,
    private_key: Ed25519PrivateKey,
    key_ring: TrustedPublisherKeyRing,
) -> None:
    """Catches a signed profile binding a token audience to a foreign host."""
    profile = _preregistered_profile()
    profile["resource_origins"] = ["https://mcp.example.com/mcp"]
    profile["resource_identifier"] = identifier
    profile["revision"] = _profile_revision(profile)

    with pytest.raises(
        ValueError, match="^oauth_profile_resource_identifier_(invalid|unavailable)$|^oauth_profile_"
    ):
        OAuthProfileRegistry.from_document(
            _sign_catalog(private_key, profiles=[profile]), key_ring
        )


def test_the_resource_identifier_is_covered_by_the_profile_revision(
    private_key: Ed25519PrivateKey,
    key_ring: TrustedPublisherKeyRing,
) -> None:
    """Catches an identifier that could be swapped without changing the digest."""
    profile = _preregistered_profile()
    profile["resource_origins"] = ["https://mcp.example.com/mcp"]
    profile["resource_identifier"] = "https://mcp.example.com"
    profile["revision"] = _profile_revision(profile)
    tampered = dict(profile, resource_identifier="https://mcp.example.com/mcp")

    with pytest.raises(ValueError, match="^oauth_profile_revision_mismatch$"):
        OAuthProfileRegistry.from_document(
            _sign_catalog(private_key, profiles=[tampered]), key_ring
        )
