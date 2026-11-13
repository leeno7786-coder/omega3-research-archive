from __future__ import annotations

import base64
import json
from collections.abc import Callable
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mcp_platform.oauth_profiles import OAuthClientMode, OAuthProfileRegistry
from plugin_platform.trust import TrustedPublisherKeyRing, load_embedded_key_ring
import scripts.provision_oauth_profiles as provisioner
from scripts.provision_oauth_profiles import (
    OAUTH_PROFILE_KEY_ID,
    load_oauth_providers,
    provision_oauth_profiles,
)


_FIXTURE_SEED = bytes(range(32))


def _fixture_ring(seed: bytes = _FIXTURE_SEED) -> TrustedPublisherKeyRing:
    public_key = (
        Ed25519PrivateKey.from_private_bytes(seed)
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )
    return TrustedPublisherKeyRing.from_dict(
        {
            "schema_version": 1,
            "keys": [
                {
                    "key_id": OAUTH_PROFILE_KEY_ID,
                    "publisher_id": "omega.first_party",
                    "public_key_base64": base64.b64encode(public_key).decode("ascii"),
                }
            ],
            "revoked_key_ids": [],
            "revoked_publisher_ids": [],
            "revoked_subject_ids": [],
        }
    )


def _registration(path: Path, client_id: str, redirects: list[str]) -> None:
    path.write_text(
        json.dumps(
            {"installed": {"client_id": client_id, "redirect_uris": redirects}}
        ),
        encoding="utf-8",
    )


def _provider_document(registration_name: str) -> list[dict[str, object]]:
    dynamic_redirect = "http://127.0.0.1:13310/oauth/notion.desktop/callback"
    return [
        {
            "profile_id": "google.desktop.drive",
            "provider_id": "google.workspace.drive",
            "app_publisher": "google",
            "authorization_issuer": "https://accounts.google.com",
            "resource_origins": ["https://drivemcp.googleapis.com/mcp/v1"],
            "authorization_flow": "authorization_code_pkce",
            "client_mode": "preregistered",
            "account_partition": "issuer_account",
            "allowed_plugin_publishers": ["omega.first_party"],
            "redirect_uri": (
                "http://127.0.0.1:13310/oauth/google.desktop.drive/callback"
            ),
            "registration_file": registration_name,
        },
        {
            "profile_id": "notion.desktop",
            "provider_id": "com.notion.mcp",
            "app_publisher": "com.notion",
            "authorization_issuer": "https://mcp.notion.com",
            "resource_origins": ["https://mcp.notion.com/mcp"],
            "authorization_flow": "authorization_code_pkce",
            "client_mode": "dynamic_registration",
            "account_partition": "issuer_account",
            "allowed_plugin_publishers": ["com.notion"],
            "redirect_uri": dynamic_redirect,
            "registration": {
                "client_name": "Omega",
                "redirect_uris": [dynamic_redirect],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "scope": "",
                "software_id": "org.omega2ai.desktop",
            },
        },
    ]


def _write_fixture_providers(tmp_path: Path) -> Path:
    registration = tmp_path / "registration.json"
    _registration(
        registration,
        "fixture-public-client",
        ["http://localhost/oauth/google.desktop.drive/callback"],
    )
    providers = tmp_path / "oauth_providers.json"
    providers.write_text(
        json.dumps(_provider_document(registration.name)),
        encoding="utf-8",
    )
    return providers


def test_provider_input_drives_reproducible_preregistered_and_dynamic_profiles(
    tmp_path: Path,
) -> None:
    providers = load_oauth_providers(_write_fixture_providers(tmp_path))
    first = provision_oauth_profiles(
        providers=providers,
        catalog_path=tmp_path / "first.json",
        signing_seed=_FIXTURE_SEED,
        key_ring=_fixture_ring(),
    )
    second = provision_oauth_profiles(
        providers=providers,
        catalog_path=tmp_path / "second.json",
        signing_seed=_FIXTURE_SEED,
        key_ring=_fixture_ring(),
    )

    assert first.catalog_path.read_bytes() == second.catalog_path.read_bytes()
    assert first.profile_ids == ("google.desktop.drive", "notion.desktop")
    assert len(first.client_id_hashes) == 1
    catalog = json.loads(first.catalog_path.read_text(encoding="utf-8"))
    notion_document = next(
        profile for profile in catalog["profiles"] if profile["profile_id"] == "notion.desktop"
    )
    notion = first.registry.get_exact(
        notion_document["profile_id"], notion_document["revision"]
    )
    assert notion.client_mode is OAuthClientMode.DYNAMIC_REGISTRATION
    assert notion.client_id is None
    assert notion.registration is not None
    assert notion.registration.software_id == "org.omega2ai.desktop"


@pytest.mark.parametrize(
    "mutation",
    (
        lambda item: item.update({"unexpected": True}),
        lambda item: item.update({"resource_origins": "https://mcp.notion.com/mcp"}),
        lambda item: item.update({"registration_file": "also-preregistered.json"}),
    ),
)
def test_provider_input_rejects_unknown_malformed_and_mixed_mode_fields(
    tmp_path: Path,
    mutation: Callable[[dict[str, object]], None],
) -> None:
    document = _provider_document("registration.json")
    mutation(document[1])
    path = tmp_path / "providers.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match="^oauth_provider_input_invalid$"):
        load_oauth_providers(path)


def test_provider_registration_file_env_reference_resolves_without_a_cwd_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registration = tmp_path / "outside" / "registration.json"
    registration.parent.mkdir()
    _registration(registration, "fixture-public-client", ["http://localhost/callback"])
    monkeypatch.setenv("OMEGA_FIXTURE_REGISTRATION", str(registration))
    document = _provider_document("env:OMEGA_FIXTURE_REGISTRATION")[:1]
    providers_path = tmp_path / "manifest" / "providers.json"
    providers_path.parent.mkdir()
    providers_path.write_text(json.dumps(document), encoding="utf-8")

    providers = load_oauth_providers(providers_path)

    assert providers[0].registration_file == registration


def test_provider_registration_file_env_reference_requires_a_configured_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OMEGA_FIXTURE_REGISTRATION", raising=False)
    document = _provider_document("env:OMEGA_FIXTURE_REGISTRATION")[:1]
    path = tmp_path / "providers.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match="^oauth_provider_registration_file_required$"):
        load_oauth_providers(path)


def test_mint_seed_dpapi_path_writes_only_ciphertext_and_prints_only_public_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_key = Ed25519PrivateKey.from_private_bytes(_FIXTURE_SEED)
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    protected = b"fixture-dpapi-ciphertext"
    monkeypatch.setattr(provisioner.Ed25519PrivateKey, "generate", lambda: private_key)
    monkeypatch.setattr(provisioner, "_dpapi_protect", lambda seed: protected)
    destination = tmp_path / "custody" / "oauth-profile-key.dpapi"

    assert provisioner.main(["--mint-seed-dpapi-path", str(destination)]) == 0

    assert destination.read_bytes() == protected
    output = capsys.readouterr().out
    assert output == (
        "public_key_base64=" + base64.b64encode(public_key).decode("ascii") + "\n"
    )
    assert base64.b64encode(_FIXTURE_SEED).decode("ascii") not in output


def test_mint_seed_dpapi_path_refuses_to_overwrite_existing_custody(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "existing.dpapi"
    destination.write_bytes(b"existing-custody")

    with pytest.raises(SystemExit, match="^oauth_profile_signing_seed_exists$"):
        provisioner.main(["--mint-seed-dpapi-path", str(destination)])

    assert destination.read_bytes() == b"existing-custody"


def test_shipped_catalog_uses_only_the_rotated_public_key() -> None:
    ring = load_embedded_key_ring()

    assert "omega-oauth-2026-08-26" not in ring.key_ids
    assert "omega-oauth-2026-09-04-r1" not in ring.key_ids
    assert "omega-oauth-2026-09-06-r1" not in ring.key_ids
    assert OAUTH_PROFILE_KEY_ID == "omega-oauth-2026-09-06-r2"
    assert OAUTH_PROFILE_KEY_ID in ring.key_ids
    OAuthProfileRegistry.from_path(
        Path("plugin_platform/oauth_profiles.json"),
        ring,
    )
    catalog = json.loads(
        Path("plugin_platform/oauth_profiles.json").read_text(encoding="utf-8")
    )
    assert catalog["catalog_id"] == "omega.featured"
    # No pinned sequence or profile count. This test is about the KEY -- that
    # the retired ones are gone and the rotated one signs the catalog -- and
    # pinning a number here made it fail every time a provider was added, which
    # is the routine act this whole pipeline exists to make cheap.
    assert catalog["sequence"] >= 19
    # Nor a floor that only survives ADDITIONS. Retiring a provider is the same
    # routine act in the other direction -- monday.com and Grafana Cloud both
    # left on 2026-09-08 -- and `>= 32` turned each departure into a failure
    # about a number rather than about the key this test is for.
    assert catalog["profiles"]
    assert catalog["signing_key_id"] == OAUTH_PROFILE_KEY_ID


def test_rotated_catalog_preserves_all_four_original_profile_values() -> None:
    """The four original profiles keep their public shape across key rotations.

    Their redirect_uri is deliberately NOT pinned here: Google's three moved to
    loopback once it was clear its desktop client type accepts nothing else.
    What must survive a rotation is identity and custody, not a value a vendor
    can force us to change.
    """
    catalog = json.loads(
        Path("plugin_platform/oauth_profiles.json").read_text(encoding="utf-8")
    )
    observed = [
        (
            profile["schema_version"],
            profile["profile_id"],
            profile["catalog_signer"],
            tuple(profile["allowed_plugin_publishers"]),
            profile["app_publisher"],
            profile["provider_id"],
            profile["authorization_issuer"],
            tuple(profile["resource_origins"]),
            profile["client_mode"],
            profile["client_id"],
            profile.get("redirect_uri"),
            profile["authorization_flow"],
            profile["account_partition"],
        )
        for profile in catalog["profiles"][:4]
    ]

    assert observed == [
        (
            1,
            "google.desktop.drive",
            "omega.featured",
            ("omega.first_party",),
            "google",
            "google.workspace.drive",
            "https://accounts.google.com",
            ("https://drivemcp.googleapis.com/mcp/v1",),
            "preregistered",
            "682673926224-3nasg0oidhsoouetkn6dekfmumboe81h.apps.googleusercontent.com",
            "http://127.0.0.1:13310/oauth/google.desktop.drive/callback",
            "authorization_code_pkce",
            "issuer_account",
        ),
        (
            1,
            "google.desktop.calendar",
            "omega.featured",
            ("omega.first_party",),
            "google",
            "google.workspace.calendar",
            "https://accounts.google.com",
            ("https://calendarmcp.googleapis.com/mcp/v1",),
            "preregistered",
            "682673926224-3nasg0oidhsoouetkn6dekfmumboe81h.apps.googleusercontent.com",
            "http://127.0.0.1:13310/oauth/google.desktop.calendar/callback",
            "authorization_code_pkce",
            "issuer_account",
        ),
        (
            1,
            "google.desktop.gmail",
            "omega.featured",
            ("omega.first_party",),
            "google",
            "google.workspace.gmail",
            "https://accounts.google.com",
            ("https://gmailmcp.googleapis.com/mcp/v1",),
            "preregistered",
            "682673926224-3nasg0oidhsoouetkn6dekfmumboe81h.apps.googleusercontent.com",
            "http://127.0.0.1:13310/oauth/google.desktop.gmail/callback",
            "authorization_code_pkce",
            "issuer_account",
        ),
        (
            1,
            "github.desktop",
            "omega.featured",
            ("omega.first_party",),
            "github",
            "github.official",
            "https://github.com/login/oauth",
            ("https://api.githubcopilot.com/mcp/",),
            "preregistered",
            "Ov23ctLhv645KtMeltHf",
            None,
            "device_code",
            "issuer_account",
        ),
    ]


def test_shipped_provider_catalog_has_public_dynamic_and_one_hosted_mode() -> None:
    """Counts move when a provider is added or retired; the split does not."""
    providers = load_oauth_providers(
        Path("plugin_platform/marketplace_build/oauth_providers.json")
    )

    preregistered = sum(item.client_mode is OAuthClientMode.PREREGISTERED for item in providers)
    dynamic_count = sum(
        item.client_mode is OAuthClientMode.DYNAMIC_REGISTRATION for item in providers
    )
    hosted = sum(item.client_mode is OAuthClientMode.HOSTED_BRIDGE for item in providers)
    assert preregistered == 4
    assert hosted == 1
    assert preregistered + dynamic_count + hosted == len(providers)
    dynamic = tuple(
        item
        for item in providers
        if item.client_mode is OAuthClientMode.DYNAMIC_REGISTRATION
    )
    assert {item.profile_id for item in dynamic} == {
        "algolia.desktop",
        "atlassian.desktop",
        "canva.desktop",
        "cloudflare.desktop",
        "context7.desktop",
        "figma.desktop",
        "gitlab.desktop",
        "huggingface.desktop",
        "intercom.desktop",
        "linear.desktop",
        "mapbox.desktop",
        "miro.desktop",
        "neon.desktop",
        "netlify.desktop",
        "notion.desktop",
        "paypal.desktop",
        "postman.desktop",
        "sentry.desktop",
        "square.desktop",
        "stripe.desktop",
        "supabase.desktop",
        "vercel.desktop",
        "webflow.desktop",
        "wix.desktop",
        "zapier.desktop",
    }
    # A profile names who may PACKAGE against it, and that is not fixed to the
    # vendor. A provider Omega has not packaged yet names the vendor; packaging
    # one means allowing `omega.first_party`, exactly as the five shipped
    # plugins do. Supabase was the first of these to be packaged, and the
    # old form of this assertion -- vendor, always -- was what stood in the way.
    for item in dynamic:
        assert item.allowed_plugin_publishers in (
            (item.app_publisher,),
            ("omega.first_party",),
        ), item.profile_id
    assert next(item for item in providers if item.profile_id == "notion.desktop").provider_id == (
        "com.notion.mcp"
    )


def test_mint_seed_sealed_path_is_portable_and_prints_only_public_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The sealed seed opens with the passphrase alone, on any machine."""
    from mcp_platform import sealed_secrets

    private_key = Ed25519PrivateKey.from_private_bytes(_FIXTURE_SEED)
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    monkeypatch.setattr(provisioner.Ed25519PrivateKey, "generate", lambda: private_key)
    monkeypatch.delenv(sealed_secrets.PASSPHRASE_FILE_ENV, raising=False)
    monkeypatch.setenv(sealed_secrets.PASSPHRASE_ENV, "portable-pw")
    destination = tmp_path / "custody" / "oauth-profiles-ed25519.sealed"

    assert provisioner.main(["--mint-seed-sealed-path", str(destination)]) == 0

    output = capsys.readouterr().out
    assert output == "public_key_base64=" + base64.b64encode(public_key).decode("ascii") + "\n"
    assert base64.b64encode(_FIXTURE_SEED).decode("ascii") not in output
    assert _FIXTURE_SEED not in destination.read_bytes()
    assert provisioner._seed_from_sealed(destination) == _FIXTURE_SEED
    assert sealed_secrets.unseal(
        destination.read_bytes(),
        "portable-pw",
        purpose=sealed_secrets.OAUTH_PROFILE_SIGNING_SEED_PURPOSE,
    ) == _FIXTURE_SEED

    with pytest.raises(SystemExit, match="^oauth_profile_signing_seed_exists$"):
        provisioner.main(["--mint-seed-sealed-path", str(destination)])
    monkeypatch.setenv(sealed_secrets.PASSPHRASE_ENV, "wrong")
    with pytest.raises(ValueError, match="^oauth_profile_signing_seed_locked$"):
        provisioner._seed_from_sealed(destination)
    monkeypatch.delenv(sealed_secrets.PASSPHRASE_ENV)
    with pytest.raises(ValueError, match="^oauth_profile_signing_seed_locked$"):
        provisioner._seed_from_sealed(destination)
    with pytest.raises(SystemExit, match="^oauth_profile_signing_seed_locked$"):
        provisioner.main(["--mint-seed-sealed-path", str(tmp_path / "other.sealed")])
    with pytest.raises(ValueError, match="^oauth_profile_signing_seed_unreadable$"):
        provisioner._seed_from_sealed(tmp_path / "absent.sealed")


def test_signing_seed_resolution_prefers_explicit_custody_then_env_then_drive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_platform import sealed_secrets

    monkeypatch.delenv(sealed_secrets.PASSPHRASE_FILE_ENV, raising=False)
    monkeypatch.setenv(sealed_secrets.PASSPHRASE_ENV, "pw")
    sealed = tmp_path / "seed.sealed"
    sealed_secrets.write_sealed_file(
        sealed, _FIXTURE_SEED, "pw", purpose=sealed_secrets.OAUTH_PROFILE_SIGNING_SEED_PURPOSE
    )
    monkeypatch.setattr(provisioner, "_DEFAULT_SEED_SEALED_PATH", tmp_path / "absent.sealed")
    monkeypatch.delenv("OMEGA_OAUTH_PROFILE_SIGNING_KEY_B64", raising=False)
    parser = provisioner._parser()

    with pytest.raises(ValueError, match="^oauth_profile_signing_seed_required$"):
        provisioner._signing_seed(parser.parse_args([]))
    assert provisioner._signing_seed(
        parser.parse_args(["--seed-sealed-path", str(sealed)])
    ) == _FIXTURE_SEED
    other = bytes(range(32, 64))
    monkeypatch.setenv("OMEGA_OAUTH_PROFILE_SIGNING_KEY_B64", base64.b64encode(other).decode())
    assert provisioner._signing_seed(parser.parse_args([])) == other
    assert provisioner._signing_seed(
        parser.parse_args(["--seed-sealed-path", str(sealed)])
    ) == _FIXTURE_SEED
    monkeypatch.delenv("OMEGA_OAUTH_PROFILE_SIGNING_KEY_B64")
    monkeypatch.setattr(provisioner, "_DEFAULT_SEED_SEALED_PATH", sealed)
    assert provisioner._signing_seed(parser.parse_args([])) == _FIXTURE_SEED


def test_provisioning_accepts_relay_redirects_and_rejects_other_hosts(tmp_path: Path) -> None:
    """A preregistered PKCE client may register the HTTPS relay; never another host."""
    relay = [
        "https://plugins.omega2ai.com/oauth/google.desktop.drive/callback",
        "http://127.0.0.1:13310/oauth/google.desktop.drive/callback",
    ]
    registration = tmp_path / "registration.json"
    _registration(registration, "fixture-public-client", relay)
    providers_path = tmp_path / "oauth_providers.json"
    document = _provider_document(registration.name)[:1]
    document[0]["redirect_uri"] = relay[0]
    providers_path.write_text(json.dumps(document), encoding="utf-8")

    result = provision_oauth_profiles(
        providers=load_oauth_providers(providers_path),
        catalog_path=tmp_path / "catalog.json",
        signing_seed=_FIXTURE_SEED,
        key_ring=_fixture_ring(),
    )
    catalog = json.loads(result.catalog_path.read_text(encoding="utf-8"))
    assert catalog["profiles"][0]["redirect_uri"] == relay[0]

    _registration(registration, "fixture-public-client", ["https://omega.example/callback"])
    with pytest.raises(ValueError, match="^oauth_registration_redirect_mismatch$"):
        provision_oauth_profiles(
            providers=load_oauth_providers(providers_path),
            catalog_path=tmp_path / "other.json",
            signing_seed=_FIXTURE_SEED,
            key_ring=_fixture_ring(),
        )


def test_shipped_provider_inputs_all_use_the_https_relay() -> None:
    """Every shipped redirect is one of the two forms Omega can actually receive.

    Not "all relay". Google's desktop client type accepts ONLY loopback, and
    Slack refuses loopback outright -- so the two forms are not a preference,
    they are what different vendor classes will each accept. Asserting one of
    them universally described neither.
    """
    providers = load_oauth_providers(
        Path("plugin_platform/marketplace_build/oauth_providers.json")
    )
    relay = 0
    loopback = 0
    for item in providers:
        if item.redirect_uri is None:
            continue
        assert item.redirect_uri in (
            f"https://plugins.omega2ai.com/oauth/{item.profile_id}/callback",
            f"http://127.0.0.1:13310/oauth/{item.profile_id}/callback",
        ), item.profile_id
        if item.redirect_uri.startswith("https://"):
            relay += 1
        else:
            loopback += 1
        if item.registration is not None:
            assert item.registration.redirect_uris == (item.redirect_uri,), item.profile_id
    # Both classes are shipped; a change that silently collapsed one into the
    # other would break every vendor on the other side.
    assert relay and loopback


def test_shipped_slack_profile_names_resource_identifier_and_hosted_mode() -> None:
    """Slack identifies its resource as the origin while serving MCP under /mcp.

    Catches a regression that collapses the two back together, which would send
    discovery to a metadata path Slack does not serve.
    """
    catalog = json.loads(
        Path("plugin_platform/oauth_profiles.json").read_text(encoding="utf-8")
    )
    slack = next(
        profile for profile in catalog["profiles"] if profile["profile_id"] == "slack.desktop"
    )

    assert slack["resource_origins"] == ["https://mcp.slack.com/mcp"]
    assert slack["resource_identifier"] == "https://mcp.slack.com"
    assert slack["authorization_issuer"] == "https://mcp.slack.com"
    assert slack["client_mode"] == "hosted_bridge"
    assert slack["authorization_flow"] == "authorization_code_pkce"
    assert "client_secret_ref" not in slack
    assert slack["redirect_uri"] == "https://plugins.omega2ai.com/oauth/slack.desktop/callback"
    # Omega publishes this package and owns the Slack OAuth client, so only
    # Omega-published packages may use the profile.
    assert slack["allowed_plugin_publishers"] == ["omega.first_party"]
    # The signed catalog carries neither a secret nor a secret reference.
    assert "client_secret" not in slack
    assert set(slack) == {
        "account_partition",
        "allowed_plugin_publishers",
        "app_publisher",
        "authorization_flow",
        "authorization_issuer",
        "catalog_signer",
        "client_id",
        "client_mode",
        "profile_id",
        "provider_id",
        "redirect_uri",
        "resource_identifier",
        "resource_origins",
        "revision",
        "schema_version",
    }

    registry = OAuthProfileRegistry.from_path(
        Path("plugin_platform/oauth_profiles.json"), load_embedded_key_ring()
    )
    profile = registry.resolve_by_profile_id("slack.desktop")
    assert profile.identifier_for("https://mcp.slack.com/mcp") == "https://mcp.slack.com"


def test_a_rebuild_does_not_inherit_local_client_secret_bindings(
    tmp_path: Path,
) -> None:
    """A local binding must not become a required credential for new installs."""
    providers = load_oauth_providers(_write_fixture_providers(tmp_path))
    catalog = tmp_path / "catalog.json"
    provision_oauth_profiles(
        providers=providers,
        catalog_path=catalog,
        signing_seed=_FIXTURE_SEED,
        key_ring=_fixture_ring(),
    )
    document = json.loads(catalog.read_text(encoding="utf-8"))
    bound = "oauth-client/google.desktop.drive/secret"
    for profile in document["profiles"]:
        if profile["profile_id"] == "google.desktop.drive":
            profile["client_secret_ref"] = bound
    catalog.write_text(json.dumps(document), encoding="utf-8")

    provision_oauth_profiles(
        providers=providers,
        catalog_path=catalog,
        signing_seed=_FIXTURE_SEED,
        key_ring=_fixture_ring(),
    )

    rebuilt = json.loads(catalog.read_text(encoding="utf-8"))
    entry = next(p for p in rebuilt["profiles"] if p["profile_id"] == "google.desktop.drive")
    assert "client_secret_ref" not in entry


def test_shipped_confidential_slack_requires_hosted_bridge() -> None:
    providers = load_oauth_providers(
        Path("plugin_platform/marketplace_build/oauth_providers.json")
    )
    slack = next(item for item in providers if item.profile_id == "slack.desktop")

    assert slack.client_mode is OAuthClientMode.HOSTED_BRIDGE


def test_provisioning_refuses_a_client_secret_in_factory_registration(tmp_path: Path) -> None:
    provider_path = _write_fixture_providers(tmp_path)
    registration = tmp_path / "registration.json"
    document = json.loads(registration.read_text(encoding="utf-8"))
    document["installed"]["client_secret"] = "developer-secret"
    registration.write_text(json.dumps(document), encoding="utf-8")
    providers = load_oauth_providers(provider_path)

    with pytest.raises(ValueError, match="^oauth_registration_invalid$"):
        provision_oauth_profiles(
            providers=providers,
            catalog_path=tmp_path / "catalog.json",
            signing_seed=_FIXTURE_SEED,
            key_ring=_fixture_ring(),
        )


def test_shipped_catalog_never_requires_developer_client_secrets() -> None:
    catalog = json.loads(Path("plugin_platform/oauth_profiles.json").read_text(encoding="utf-8"))
    assert all("client_secret_ref" not in profile for profile in catalog["profiles"])


def test_the_default_sequence_never_moves_backwards(tmp_path: Path) -> None:
    """A sequence is monotonic; moving it back is indistinguishable from rollback.

    The default was a hardcoded 4, so the first provisioning run after several
    key rotations rewrote the shipped catalog's sequence from 22 to 4 -- while
    the rotation path a few hundred lines away had always incremented correctly.
    Two paths, one tracking reality and one not.
    """
    from scripts.provision_oauth_profiles import _next_catalog_sequence

    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"sequence": 22, "profiles": []}), encoding="utf-8")
    assert _next_catalog_sequence(catalog) == 23

    catalog.write_text(json.dumps({"sequence": 1, "profiles": []}), encoding="utf-8")
    assert _next_catalog_sequence(catalog) >= 4, "the historical floor still applies"

    assert _next_catalog_sequence(tmp_path / "absent.json") == 4


def test_the_shipped_catalog_sequence_only_ever_advances() -> None:
    """Catches a release that rewound the number consumers use to detect replay."""
    from scripts.provision_oauth_profiles import _next_catalog_sequence

    shipped = json.loads(
        Path("plugin_platform/oauth_profiles.json").read_text(encoding="utf-8")
    )

    assert _next_catalog_sequence() == shipped["sequence"] + 1
