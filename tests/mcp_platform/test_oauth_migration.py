from __future__ import annotations

import base64
import json
import logging
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mcp_platform.canonical import canonical_bytes
from mcp_platform.oauth_migration import (
    LegacyOAuthRecord,
    OAuthBrokerMigration,
    OAuthMigrationExpectation,
)
from mcp_platform.oauth_profiles import (
    OAuthAuthorizationFlow,
    OAuthClientMode,
    OAuthProfileMatchInput,
)
from mcp_platform.oauth_vault import (
    OAuthClientCredential,
    OAuthCommittedGrant,
    OAuthCredentialVault,
    OAuthPreparedToken,
    OAuthTokenPartition,
)
from plugin_platform.trust import PublisherTrustError, TrustedPublisherKeyRing
from scripts.provision_oauth_profiles import (
    OAUTH_PROFILE_KEY_ID,
    OAuthProviderInput,
    provision_oauth_profiles,
)


_VAULT_KEY = b"k" * 32
_PROFILE_REVISION = "sha256:" + ("1" * 64)
_ACCOUNT = "2" * 64
_CONTEXT = "3" * 64
_PLUGIN_MANIFEST = "4" * 64
_APP_BINDING = "5" * 64
_CAPABILITY = "6" * 64
_AUTHORITY = "7" * 64
_TOOL_BINDING = "8" * 64
_SOURCE_REVISION = "9" * 64
_LEGACY_GOOGLE_CREDENTIAL_REF = "oauth-client/google.desktop/secret"


def _key_material(key_id=OAUTH_PROFILE_KEY_ID) -> tuple[bytes, TrustedPublisherKeyRing]:
    private_key = Ed25519PrivateKey.generate()
    seed = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    ring = TrustedPublisherKeyRing.from_dict(
        {
            "schema_version": 1,
            "keys": [
                {
                    "key_id": key_id,
                    "publisher_id": "omega.first_party",
                    "public_key_base64": base64.b64encode(public_key).decode("ascii"),
                }
            ],
            "revoked_key_ids": [],
            "revoked_publisher_ids": [],
            "revoked_subject_ids": [],
        }
    )
    return seed, ring


def _registration(path: Path, provider: str, *, client_secret: str | None = None) -> Path:
    """Factory provisioning reads a public, secret-free client registration."""
    if provider == "google":
        redirects = ["http://localhost"]
    else:
        redirects = []
    record: dict[str, object] = {
        "client_id": f"{provider}-public-client-id",
        "redirect_uris": redirects,
    }
    if client_secret is not None:
        record["client_secret"] = client_secret
    path.write_text(json.dumps({"installed": record}), encoding="utf-8")
    return path


def _credential(path: Path, provider: str) -> Path:
    """Only a local credential binding reads a registration that carries a secret."""
    return _registration(path, provider, client_secret=f"{provider}-secret")


def _profile_input(
    *,
    profile_id: str,
    app_publisher: str,
    provider_id: str,
    issuer: str,
    resource_origin: str,
    flow: OAuthAuthorizationFlow,
    redirect_uri: str | None,
    registration_file: Path,
) -> OAuthProviderInput:
    return OAuthProviderInput(
        profile_id=profile_id,
        app_publisher=app_publisher,
        provider_id=provider_id,
        authorization_issuer=issuer,
        resource_origins=(resource_origin,),
        authorization_flow=flow,
        client_mode=OAuthClientMode.PREREGISTERED,
        account_partition="issuer_account",
        allowed_plugin_publishers=("omega.first_party",),
        redirect_uri=redirect_uri,
        registration=None,
        registration_file=registration_file,
    )


def _profile_inputs(google: Path, github: Path) -> tuple[OAuthProviderInput, ...]:
    return (
        _profile_input(
            profile_id="google.desktop.drive",
            app_publisher="google",
            provider_id="google.workspace.drive",
            issuer="https://accounts.google.com",
            resource_origin="https://drivemcp.googleapis.com/mcp/v1",
            flow=OAuthAuthorizationFlow.AUTHORIZATION_CODE_PKCE,
            redirect_uri="http://127.0.0.1:13310/oauth/google.desktop.drive/callback",
            registration_file=google,
        ),
        _profile_input(
            profile_id="google.desktop.calendar",
            app_publisher="google",
            provider_id="google.workspace.calendar",
            issuer="https://accounts.google.com",
            resource_origin="https://calendarmcp.googleapis.com/mcp/v1",
            flow=OAuthAuthorizationFlow.AUTHORIZATION_CODE_PKCE,
            redirect_uri="http://127.0.0.1:13310/oauth/google.desktop.calendar/callback",
            registration_file=google,
        ),
        _profile_input(
            profile_id="google.desktop.gmail",
            app_publisher="google",
            provider_id="google.workspace.gmail",
            issuer="https://accounts.google.com",
            resource_origin="https://gmailmcp.googleapis.com/mcp/v1",
            flow=OAuthAuthorizationFlow.AUTHORIZATION_CODE_PKCE,
            redirect_uri="http://127.0.0.1:13310/oauth/google.desktop.gmail/callback",
            registration_file=google,
        ),
        _profile_input(
            profile_id="github.desktop",
            app_publisher="github",
            provider_id="github.official",
            issuer="https://github.com/login/oauth",
            resource_origin="https://api.githubcopilot.com/mcp/",
            flow=OAuthAuthorizationFlow.DEVICE_CODE,
            redirect_uri=None,
            registration_file=github,
        ),
    )


def _provision(tmp_path: Path):
    seed, ring = _key_material()
    google = _registration(tmp_path / "google.json", "google")
    github = _registration(tmp_path / "github.json", "github")
    return provision_oauth_profiles(
        providers=_profile_inputs(google, github),
        catalog_path=tmp_path / "oauth_profiles.json",
        signing_seed=seed,
        key_ring=ring,
    )


def test_provisioning_writes_only_public_values_to_catalog(
    tmp_path: Path,
) -> None:
    result = _provision(tmp_path)

    assert (
        result.registry.resolve(
            OAuthProfileMatchInput(
                catalog_signer="omega.featured",
                plugin_publisher="omega.first_party",
                app_publisher="google",
                provider_id="google.workspace.drive",
                authorization_issuer="https://accounts.google.com",
                resource_origin="https://drivemcp.googleapis.com/mcp/v1",
            )
        ).profile_id
        == "google.desktop.drive"
    )

    assert "secret" not in repr(result).casefold()


def test_existing_profiles_can_bind_desktop_credentials_without_changing_other_providers(tmp_path):
    from scripts import provision_oauth_profiles as provision

    seed, ring = _key_material()
    google = _registration(tmp_path / "google.json", "google")
    github = _registration(tmp_path / "github.json", "github")
    result = provision_oauth_profiles(
        providers=_profile_inputs(google, github),
        catalog_path=tmp_path / "catalog.json", signing_seed=seed, key_ring=ring,
    )
    google_credential = _credential(tmp_path / "google-credential.json", "google")
    before = json.loads(result.catalog_path.read_text())
    vault = OAuthCredentialVault(root=tmp_path / "vault", key_provider=lambda: _VAULT_KEY)
    updated = provision.provision_client_credential(
        registration_path=google_credential, catalog_path=result.catalog_path,
        profile_ids=("google.desktop.drive", "google.desktop.calendar", "google.desktop.gmail"),
        signing_seed=seed, vault=vault, key_ring=ring,
    )
    after = json.loads(result.catalog_path.read_text())
    assert after["sequence"] == before["sequence"] + 1
    assert after["profiles"][-1] == before["profiles"][-1]
    for previous, current in zip(before["profiles"][:3], after["profiles"][:3]):
        assert {k: v for k, v in current.items() if k not in {"revision", "client_secret_ref"}} == {
            k: v for k, v in previous.items() if k != "revision"
        }
        credential = vault.load_credential(current["client_secret_ref"])[1]
        assert credential.client_id == "google-public-client-id"
        assert credential.client_secret == "google-secret"
    assert b"google-secret" not in result.catalog_path.read_bytes()
    assert b"google-secret" not in (tmp_path / "vault" / "oauth-vault.json").read_bytes()
    again = provision.provision_client_credential(
        registration_path=google_credential, catalog_path=result.catalog_path,
        profile_ids=updated.profile_ids, signing_seed=seed, vault=vault, key_ring=ring,
    )
    assert again.catalog_revision == updated.catalog_revision
    assert json.loads(result.catalog_path.read_text()) == after


@pytest.mark.parametrize("already_bound", [False, True])
@pytest.mark.parametrize("tampered", [False, True])
def test_client_credential_import_rotates_only_after_verifying_previous_catalog(
    tmp_path, already_bound, tampered,
):
    from mcp_platform.oauth_profiles import OAuthProfileRegistry
    from scripts import provision_oauth_profiles as provision

    seed, ring = _key_material()
    google = _registration(tmp_path / "google.json", "google")
    result = provision_oauth_profiles(
        providers=_profile_inputs(google, _registration(tmp_path / "github.json", "github")),
        catalog_path=tmp_path / "catalog.json", signing_seed=seed, key_ring=ring,
    )
    google_credential = _credential(tmp_path / "google-credential.json", "google")
    vault = OAuthCredentialVault(root=tmp_path / "vault", key_provider=lambda: _VAULT_KEY)
    profile_ids = ("google.desktop.drive", "google.desktop.calendar", "google.desktop.gmail")
    if already_bound:
        provision.provision_client_credential(
            registration_path=google_credential, catalog_path=result.catalog_path,
            profile_ids=profile_ids, signing_seed=seed, vault=vault, key_ring=ring,
        )
    old_seed, old_ring = _key_material("previous-oauth-key")
    previous = json.loads(result.catalog_path.read_text())
    del previous["signature"]
    previous["signing_key_id"] = "previous-oauth-key"
    previous["signature"] = base64.b64encode(
        Ed25519PrivateKey.from_private_bytes(old_seed).sign(canonical_bytes(previous))
    ).decode("ascii")
    if tampered:
        previous["profiles"][-1]["client_id"] = "unapproved-client"
    result.catalog_path.write_bytes(canonical_bytes(previous))
    before = result.catalog_path.read_bytes()
    vault_path = tmp_path / "vault" / "oauth-vault.json"
    vault_before = vault_path.read_bytes() if vault_path.exists() else None
    arguments = dict(
        registration_path=google_credential, catalog_path=result.catalog_path, profile_ids=profile_ids,
        signing_seed=seed, vault=vault, key_ring=ring, previous_key_ring=old_ring,
    )
    if tampered:
        with pytest.raises(PublisherTrustError, match="^invalid_signature$"):
            provision.provision_client_credential(**arguments)
        assert result.catalog_path.read_bytes() == before
        assert (vault_path.read_bytes() if vault_path.exists() else None) == vault_before
        return
    provision.provision_client_credential(**arguments)
    after = json.loads(result.catalog_path.read_text())
    assert after["sequence"] == previous["sequence"] + 1
    assert after["signing_key_id"] == OAUTH_PROFILE_KEY_ID
    assert len(after["profiles"]) == 4
    assert after["profiles"][-1] == previous["profiles"][-1]
    OAuthProfileRegistry.from_document(after, ring)
    with pytest.raises(PublisherTrustError, match="^unknown_signing_key$"):
        OAuthProfileRegistry.from_document(previous, ring)
    with pytest.raises(PublisherTrustError, match="^unknown_signing_key$"):
        OAuthProfileRegistry.from_document(after, old_ring)
    if already_bound:
        assert after["profiles"] == previous["profiles"]
        assert vault_path.read_bytes() == vault_before
    del arguments["previous_key_ring"]
    provision.provision_client_credential(**arguments)
    assert json.loads(result.catalog_path.read_text()) == after


def test_client_credential_import_rejects_wrong_client_before_writing(tmp_path):
    from scripts import provision_oauth_profiles as provision

    seed, ring = _key_material()
    google = _registration(tmp_path / "google.json", "google")
    github = _registration(tmp_path / "github.json", "github")
    result = provision_oauth_profiles(
        providers=_profile_inputs(google, github),
        catalog_path=tmp_path / "catalog.json", signing_seed=seed, key_ring=ring,
    )
    before = result.catalog_path.read_bytes()
    vault = OAuthCredentialVault(root=tmp_path / "vault", key_provider=lambda: _VAULT_KEY)
    with pytest.raises(ValueError, match="^oauth_registration_client_mismatch$"):
        provision.provision_client_credential(
            registration_path=_credential(tmp_path / "github-credential.json", "github"),
            catalog_path=result.catalog_path,
            profile_ids=("google.desktop.drive",), signing_seed=seed, vault=vault, key_ring=ring,
        )
    assert result.catalog_path.read_bytes() == before
    assert not (tmp_path / "vault" / "oauth-vault.json").exists()


@pytest.mark.parametrize(("case", "reason"), [
    ("missing_secret", "oauth_client_secret_required"),
    ("device", "oauth_client_mode_mismatch"),
    ("unknown", "oauth_profile_unavailable"),
    ("rotation", "oauth_client_credential_conflict"),
    ("bad_signer", "oauth_profile_signing_key_mismatch"),
])
def test_client_credential_import_fails_before_publication(tmp_path, case, reason):
    from scripts import provision_oauth_profiles as provision

    seed, ring = _key_material()
    google = _registration(tmp_path / "google.json", "google")
    github = _registration(tmp_path / "github.json", "github")
    result = provision_oauth_profiles(
        providers=_profile_inputs(google, github),
        catalog_path=tmp_path / "catalog.json", signing_seed=seed, key_ring=ring,
    )
    before = result.catalog_path.read_bytes()
    vault = OAuthCredentialVault(root=tmp_path / "vault", key_provider=lambda: _VAULT_KEY)
    profile_ids = ("google.desktop.drive",)
    registration = _credential(tmp_path / "google-credential.json", "google")
    if case == "missing_secret":
        registration = google
    elif case == "device":
        profile_ids = ("github.desktop",)
        registration = _credential(tmp_path / "github-credential.json", "github")
    elif case == "unknown":
        profile_ids = ("google.desktop.drive", "missing.profile")
    elif case == "rotation":
        vault.store_credential(
            "oauth-client/google.desktop.drive/secret",
            OAuthClientCredential("google-public-client-id", "already-stored-secret"),
            expected_revision=None,
        )
    elif case == "bad_signer":
        seed = b"x" * 32
    vault_path = tmp_path / "vault" / "oauth-vault.json"
    vault_before = vault_path.read_bytes() if vault_path.exists() else None
    with pytest.raises(ValueError, match=f"^{reason}$"):
        provision.provision_client_credential(
            registration_path=registration, catalog_path=result.catalog_path,
            profile_ids=profile_ids, signing_seed=seed, vault=vault, key_ring=ring,
        )
    assert result.catalog_path.read_bytes() == before
    assert (vault_path.read_bytes() if vault_path.exists() else None) == vault_before


def test_public_profile_signatures_and_exact_definitions_reload_through_registry(
    tmp_path: Path,
) -> None:
    result = _provision(tmp_path)
    inputs = _profile_inputs(tmp_path / "google.json", tmp_path / "github.json")

    resolved = tuple(
        result.registry.resolve(
            OAuthProfileMatchInput(
                catalog_signer="omega.featured",
                plugin_publisher="omega.first_party",
                app_publisher=item.app_publisher,
                provider_id=item.provider_id,
                authorization_issuer=item.authorization_issuer,
                resource_origin=item.resource_origins[0],
            )
        )
        for item in inputs
    )

    assert tuple(profile.profile_id for profile in resolved) == (
        "google.desktop.drive",
        "google.desktop.calendar",
        "google.desktop.gmail",
        "github.desktop",
    )
    assert tuple(profile.redirect_uri for profile in resolved) == tuple(
        item.redirect_uri for item in inputs
    )
    assert tuple(profile.authorization_flow for profile in resolved) == (
        OAuthAuthorizationFlow.AUTHORIZATION_CODE_PKCE,
        OAuthAuthorizationFlow.AUTHORIZATION_CODE_PKCE,
        OAuthAuthorizationFlow.AUTHORIZATION_CODE_PKCE,
        OAuthAuthorizationFlow.DEVICE_CODE,
    )
    assert all(profile.client_secret_ref is None for profile in resolved)
    assert all(profile.revision.startswith("sha256:") for profile in resolved)


def test_provisioning_requires_both_public_client_registrations_and_google_loopback(
    tmp_path: Path,
) -> None:
    seed, ring = _key_material()
    google = _registration(tmp_path / "google.json", "google")

    with pytest.raises(ValueError, match="^oauth_registration_required$"):
        provision_oauth_profiles(
            providers=_profile_inputs(google, tmp_path / "missing-github.json"),
            catalog_path=tmp_path / "oauth_profiles.json",
            signing_seed=seed,
            key_ring=ring,
        )

    document = json.loads(google.read_text(encoding="utf-8"))
    document["installed"]["redirect_uris"] = ["https://omega.example/callback"]
    google.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="^oauth_registration_redirect_mismatch$"):
        provision_oauth_profiles(
            providers=_profile_inputs(
                google,
                _registration(tmp_path / "github.json", "github"),
            ),
            catalog_path=tmp_path / "oauth_profiles.json",
            signing_seed=seed,
            key_ring=ring,
        )


def test_provisioning_accepts_google_desktop_loopback_registration(
    tmp_path: Path,
) -> None:
    seed, ring = _key_material()
    google = _registration(tmp_path / "google.json", "google")

    result = provision_oauth_profiles(
        providers=_profile_inputs(
            google,
            _registration(tmp_path / "github.json", "github"),
        ),
        catalog_path=tmp_path / "oauth_profiles.json",
        signing_seed=seed,
        key_ring=ring,
    )

    assert result.profile_ids[:3] == (
        "google.desktop.drive",
        "google.desktop.calendar",
        "google.desktop.gmail",
    )


def _legacy_record() -> tuple[LegacyOAuthRecord, OAuthMigrationExpectation]:
    credential_identity = sha256(
        canonical_bytes(
            {
                "client_id": "public-client-id",
                "client_metadata_document_url": None,
                "client_mode": "preregistered",
                "client_secret_ref": _LEGACY_GOOGLE_CREDENTIAL_REF,
            }
        )
    ).hexdigest()
    partition_key = sha256(
        canonical_bytes(
            {
                "account_key_sha256": _ACCOUNT,
                "account_partition": "issuer_account",
                "authorization_issuer": "https://accounts.google.com",
                "credential_identity": credential_identity,
            }
        )
    ).hexdigest()
    scopes = ("scope.read", "scope.write")
    partition = OAuthTokenPartition(
        partition_key=partition_key,
        profile_id="google.desktop.drive",
        profile_revision=_PROFILE_REVISION,
        authorization_issuer="https://accounts.google.com",
        resource_origin="https://drivemcp.googleapis.com/mcp/v1",
        account_key_sha256=_ACCOUNT,
        client_id="public-client-id",
        authorization_context_revision=_CONTEXT,
        token=OAuthPreparedToken(
            access_token="migration-access-secret",
            refresh_token="migration-refresh-secret",
            token_type="Bearer",
            scopes=scopes,
            expires_at=9999.0,
        ),
        dependent_plugin_ids=("google.workspace.drive",),
        revision=0,
        credential_identity=credential_identity,
    )
    grant = OAuthCommittedGrant(
        receipt_id="a" * 64,
        plugin_id="google.workspace.drive",
        app_id="google.workspace.drive.mcp",
        account_key_sha256=_ACCOUNT,
        profile_id="google.desktop.drive",
        profile_revision=_PROFILE_REVISION,
        token_partition_key=partition_key,
        connection_revision="b" * 64,
        capability_revision=_CAPABILITY,
        authority_revision=_AUTHORITY,
        capability_binding_revision=_TOOL_BINDING,
        scopes=scopes,
        execution_policy="confirm_each",
        claimed_authority_revision=_AUTHORITY,
    )
    record = LegacyOAuthRecord(
        source_revision=_SOURCE_REVISION,
        token_key=partition_key,
        plugin_id="google.workspace.drive",
        client_id="public-client-id",
        authorization_issuer="https://accounts.google.com",
        resource_origin="https://drivemcp.googleapis.com/mcp/v1",
        redirect_uri="http://127.0.0.1:13310/oauth/google.desktop.drive/callback",
        account_partition="issuer_account",
        scopes=scopes,
        profile_revision=_PROFILE_REVISION,
        plugin_manifest_revision=_PLUGIN_MANIFEST,
        app_binding_revision=_APP_BINDING,
        capability_authority_revision=_CAPABILITY,
        partition=partition,
        grant=grant,
    )
    expected = OAuthMigrationExpectation(
        token_key=partition_key,
        client_id=record.client_id,
        authorization_issuer=record.authorization_issuer,
        resource_origin=record.resource_origin,
        redirect_uri=record.redirect_uri,
        account_partition=record.account_partition,
        scopes=record.scopes,
        profile_revision=record.profile_revision,
        plugin_manifest_revision=record.plugin_manifest_revision,
        app_binding_revision=record.app_binding_revision,
        capability_authority_revision=record.capability_authority_revision,
    )
    return record, expected


class _LegacySource:
    def __init__(
        self,
        vault: OAuthCredentialVault,
        records: tuple[LegacyOAuthRecord, ...],
        *,
        interrupt_once: bool = False,
    ) -> None:
        self.vault = vault
        self._records = records
        self.interrupt_once = interrupt_once
        self.remove_calls = 0
        self.statuses: dict[str, str] = {}

    def records(self) -> tuple[LegacyOAuthRecord, ...]:
        return self._records

    def remove_committed(
        self,
        source_revisions: tuple[str, ...],
        *,
        disconnected_plugin_ids: tuple[str, ...],
    ) -> None:
        assert self.vault.migration_receipt(OAuthBrokerMigration.MIGRATION_ID) is not None
        self.remove_calls += 1
        if self.interrupt_once and self.remove_calls == 1:
            raise OSError("interrupted")
        self._records = tuple(
            record for record in self._records if record.source_revision not in source_revisions
        )
        self.statuses.update(
            {plugin_id: "installed_disconnected" for plugin_id in disconnected_plugin_ids}
        )


def _migrator(
    tmp_path: Path,
    records: tuple[LegacyOAuthRecord, ...],
    expectations: dict[str, OAuthMigrationExpectation],
    *,
    interrupt_once: bool = False,
) -> tuple[OAuthBrokerMigration, OAuthCredentialVault, _LegacySource]:
    vault = OAuthCredentialVault(tmp_path / "vault", key_provider=lambda: _VAULT_KEY)
    source = _LegacySource(vault, records, interrupt_once=interrupt_once)
    migrator = OAuthBrokerMigration(
        vault=vault,
        legacy=source,
        expected=lambda record: expectations.get(record.token_key),
    )
    return migrator, vault, source


def test_migration_preserves_only_full_context_equivalence(tmp_path: Path) -> None:
    record, expected = _legacy_record()
    migrator, vault, _source = _migrator(tmp_path, (record,), {record.token_key: expected})

    migrated = migrator.run()

    assert migrated.preserved_token_keys == (expected.token_key,)
    assert vault.load_token_partition(expected.token_key).profile_id == "google.desktop.drive"
    altered = replace(
        record,
        redirect_uri="http://localhost:13310/oauth/google.desktop.drive/callback",
    )
    assert migrator.evaluate(altered).action == "delete_and_reauthorize"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("client_id", "other-client"),
        ("redirect_uri", "http://127.0.0.1:13310/oauth/other/callback"),
        ("authorization_issuer", "https://issuer.example"),
        ("resource_origin", "https://resource.example/mcp"),
        ("scopes", ("scope.read",)),
        ("account_partition", "issuer_resource_account"),
        ("profile_revision", "sha256:" + ("c" * 64)),
        ("plugin_manifest_revision", "d" * 64),
        ("app_binding_revision", "e" * 64),
        ("capability_authority_revision", "f" * 64),
    ),
)
def test_migration_rejects_every_non_equivalent_context_field(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    record, expected = _legacy_record()
    migrator, _vault, _source = _migrator(tmp_path, (), {record.token_key: expected})

    assert migrator.evaluate(replace(record, **{field: value})).action == ("delete_and_reauthorize")


def test_corrupt_record_is_deleted_and_plugin_becomes_installed_disconnected(
    tmp_path: Path,
) -> None:
    record, expected = _legacy_record()
    corrupt = replace(record, client_id="")
    migrator, _vault, source = _migrator(
        tmp_path,
        (corrupt,),
        {record.token_key: expected},
    )

    receipt = migrator.run()

    assert receipt.deleted_token_keys == (record.token_key,)
    assert source.statuses == {record.plugin_id: "installed_disconnected"}


def test_migration_marker_is_idempotent_and_retries_interrupted_source_removal(
    tmp_path: Path,
) -> None:
    record, expected = _legacy_record()
    migrator, vault, source = _migrator(
        tmp_path,
        (record,),
        {record.token_key: expected},
        interrupt_once=True,
    )

    with pytest.raises(ValueError, match="^oauth_migration_source_removal_failed$"):
        migrator.run()
    committed = vault.migration_receipt(OAuthBrokerMigration.MIGRATION_ID)
    assert committed is not None

    receipt = migrator.run()
    repeated = migrator.run()

    assert receipt == repeated
    assert source.remove_calls == 3
    assert vault.migration_receipt(OAuthBrokerMigration.MIGRATION_ID) == committed


def test_migration_results_repr_and_logs_never_contain_token_secrets(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    record, expected = _legacy_record()
    migrator, _vault, _source = _migrator(tmp_path, (record,), {record.token_key: expected})
    caplog.set_level(logging.DEBUG)

    receipt = migrator.run()
    rendered = repr((record, migrator.evaluate(record), receipt)) + caplog.text

    assert "migration-access-secret" not in rendered
    assert "migration-refresh-secret" not in rendered
