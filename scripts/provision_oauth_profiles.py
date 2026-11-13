"""Build Omega's signed OAuth profile catalog from the maintainer-curated provider list.

Two operations share this script:

* ``provision_oauth_profiles`` rebuilds the whole catalog from
  ``plugin_platform/marketplace_build/oauth_providers.json`` (public and hosted
  clients resolve their client id from a secret-free registration file;
  dynamic-registration entries carry an RFC 7591 template and no client id);
* ``provision_client_credential`` binds an already-catalogued preregistered
  client to vault custody (``client_secret_ref``) without changing its scope.

The signing seed is operator-held custody, never source: the passphrase-sealed
portable file on the drive (``--seed-sealed-path``), this machine's DPAPI
file (``--seed-dpapi-path``), or ``OMEGA_OAUTH_PROFILE_SIGNING_KEY_B64``.
"""

from __future__ import annotations

import argparse
import base64
import ipaddress
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp_platform.canonical import canonical_bytes
from mcp_platform.desktop_secrets import _dpapi_protect, _dpapi_unprotect
from mcp_platform.oauth_profiles import (
    OAuthAuthorizationFlow,
    OAuthClientMode,
    OAuthDynamicRegistrationTemplate,
    OAuthProfile,
    OAuthProfileRegistry,
    canonical_relay_redirect,
    computed_profile_revision,
)
from mcp_platform.oauth_vault import OAuthClientCredential, OAuthCredentialVault
from mcp_platform.sealed_secrets import (
    OAUTH_PROFILE_SIGNING_SEED_PATH,
    OAUTH_PROFILE_SIGNING_SEED_PURPOSE,
    SealedSecretError,
    read_sealed_seed,
    require_passphrase,
    write_sealed_file,
)
from plugin_platform.trust import TrustedPublisherKeyRing, load_embedded_key_ring


_MAX_PROVIDERS_BYTES = 1024 * 1024
_MAX_REGISTRATION_BYTES = 65_536
_CATALOG_ID = "omega.featured"
#: The sequence a full provisioning run stamps when none is given. It is
#: derived from the shipped catalog, never a constant: a constant here rewrote
#: the catalog's sequence from 22 back to 4 the first time a provider was added
#: after several key rotations. A sequence is a monotonic counter -- moving it
#: backwards is indistinguishable from a rollback, and the rotation path a few
#: hundred lines below has always incremented correctly, so the two disagreed.
_CATALOG_SEQUENCE_FLOOR = 4


def _next_catalog_sequence(catalog_path: Path | None = None) -> int:
    """One past whatever the shipped catalog currently declares."""
    path = _DEFAULT_CATALOG_PATH if catalog_path is None else Path(catalog_path)
    try:
        current = int(json.loads(path.read_text(encoding="utf-8"))["sequence"])
    except (OSError, ValueError, KeyError, TypeError):
        return _CATALOG_SEQUENCE_FLOOR
    return max(current + 1, _CATALOG_SEQUENCE_FLOOR)
_PUBLISHER_ID = "omega.first_party"
MARKETPLACE_PUBLISHER_ID = _PUBLISHER_ID
OAUTH_PROFILE_KEY_ID = "omega-oauth-2026-09-06-r2"
_DEFAULT_PROVIDERS_PATH = ROOT / "plugin_platform" / "marketplace_build" / "oauth_providers.json"
_DEFAULT_CATALOG_PATH = ROOT / "plugin_platform" / "oauth_profiles.json"
_DEFAULT_SEED_SEALED_PATH = OAUTH_PROFILE_SIGNING_SEED_PATH
_COMMON_PROVIDER_FIELDS = frozenset(
    {
        "profile_id",
        "provider_id",
        "app_publisher",
        "authorization_issuer",
        "resource_origins",
        "authorization_flow",
        "client_mode",
        "account_partition",
        "allowed_plugin_publishers",
        "redirect_uri",
    }
)
# RFC 9728 allows a resource identifier that is not the URL the client posts
# to; a provider states it only when the two differ (Slack identifies itself as
# https://mcp.slack.com while serving MCP at https://mcp.slack.com/mcp).
_OPTIONAL_PROVIDER_FIELDS = frozenset({"resource_identifier"})
_REGISTRATION_ENV_RE = re.compile(r"OMEGA_[A-Z0-9_]+")


@dataclass(frozen=True, slots=True)
class OAuthProviderInput:
    profile_id: str
    provider_id: str
    app_publisher: str
    authorization_issuer: str
    resource_origins: tuple[str, ...]
    authorization_flow: OAuthAuthorizationFlow
    client_mode: OAuthClientMode
    account_partition: str
    allowed_plugin_publishers: tuple[str, ...]
    redirect_uri: str | None
    registration: OAuthDynamicRegistrationTemplate | None
    registration_file: Path | None
    resource_identifier: str | None = None


@dataclass(frozen=True, slots=True, repr=False)
class PublicClientRegistration:
    client_id: str
    redirect_uris: tuple[str, ...]
    client_secret: str | None = None

    def __repr__(self) -> str:
        return "PublicClientRegistration(<public-client>)"


@dataclass(frozen=True, slots=True, repr=False)
class ProvisioningResult:
    catalog_path: Path
    registry: OAuthProfileRegistry
    profile_ids: tuple[str, ...]
    client_id_hashes: tuple[str, ...]
    catalog_revision: str

    def __repr__(self) -> str:
        return (
            "ProvisioningResult("
            f"catalog_path={str(self.catalog_path)!r}, "
            f"profile_ids={self.profile_ids!r}, "
            f"catalog_revision={self.catalog_revision!r})"
        )


def _stable_text(value: object, *, maximum: int) -> str:
    if (
        type(value) is not str
        or not value
        or value.strip() != value
        or len(value.encode("utf-8")) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("oauth_registration_invalid")
    return value


def _input_text(value: object) -> str:
    try:
        return _stable_text(value, maximum=2048)
    except ValueError:
        raise ValueError("oauth_provider_input_invalid") from None


def _input_text_tuple(value: object) -> tuple[str, ...]:
    if type(value) is not list or not value:
        raise ValueError("oauth_provider_input_invalid")
    values = tuple(_input_text(item) for item in value)
    if len(values) != len(set(values)):
        raise ValueError("oauth_provider_input_invalid")
    return values


def _load_registration(path: Path) -> PublicClientRegistration:
    registration_path = Path(path)
    try:
        raw = registration_path.read_bytes()
    except OSError:
        raise ValueError("oauth_registration_required") from None
    try:
        if not raw or len(raw) > _MAX_REGISTRATION_BYTES:
            raise ValueError
        document = json.loads(raw.decode("utf-8"))
        wrappers = tuple(
            name
            for name in ("installed", "native")
            if type(document) is dict and name in document
        )
        if type(document) is not dict or len(wrappers) != 1 or set(document) != {wrappers[0]}:
            raise ValueError
        record = document[wrappers[0]]
        if type(record) is not dict:
            raise ValueError
        client_id = _stable_text(record.get("client_id"), maximum=2048)
        redirects = record.get("redirect_uris", [])
        if type(redirects) is not list:
            raise ValueError
        redirect_uris = tuple(_stable_text(item, maximum=2048) for item in redirects)
        if len(redirect_uris) != len(set(redirect_uris)):
            raise ValueError
        secret = record.get("client_secret")
        if secret is not None:
            secret = _stable_text(secret, maximum=1024)
        return PublicClientRegistration(client_id, redirect_uris, secret)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        raise ValueError("oauth_registration_invalid") from None


def _is_loopback_redirect(uri: str) -> bool:
    try:
        parsed = urlsplit(uri)
        if parsed.scheme != "http" or parsed.username is not None or parsed.password is not None:
            return False
        if parsed.query or parsed.fragment or parsed.hostname is None:
            return False
        if parsed.hostname.casefold() == "localhost":
            return True
        return ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        return False


def _is_relay_redirect(uri: str) -> bool:
    try:
        canonical_relay_redirect(uri)
    except ValueError:
        return False
    return True


def _is_accepted_redirect(uri: str) -> bool:
    """Loopback (RFC 8252) or Omega's HTTPS relay; never any other host."""
    return _is_loopback_redirect(uri) or _is_relay_redirect(uri)


def _registration_file(value: object, *, base: Path) -> Path:
    reference = _input_text(value)
    if reference.startswith("env:"):
        variable = reference.removeprefix("env:")
        if _REGISTRATION_ENV_RE.fullmatch(variable) is None:
            raise ValueError("oauth_provider_input_invalid")
        configured = os.environ.get(variable)
        if not configured:
            raise ValueError("oauth_provider_registration_file_required")
        return Path(configured)
    relative = Path(reference)
    if relative.is_absolute():
        raise ValueError("oauth_provider_input_invalid")
    return base / relative


def _provider_from_dict(value: object, *, base: Path) -> OAuthProviderInput:
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise ValueError("oauth_provider_input_invalid")
    fields = set(value) - _OPTIONAL_PROVIDER_FIELDS
    preregistered = fields == _COMMON_PROVIDER_FIELDS | {"registration_file"}
    dynamic = fields == _COMMON_PROVIDER_FIELDS | {"registration"}
    if not preregistered and not dynamic:
        raise ValueError("oauth_provider_input_invalid")
    try:
        client_mode = OAuthClientMode(value["client_mode"])
        authorization_flow = OAuthAuthorizationFlow(value["authorization_flow"])
    except (TypeError, ValueError):
        raise ValueError("oauth_provider_input_invalid") from None
    if value["account_partition"] != "issuer_account":
        raise ValueError("oauth_provider_input_invalid")
    redirect_value = value["redirect_uri"]
    redirect_uri = None if redirect_value is None else _input_text(redirect_value)
    registration: OAuthDynamicRegistrationTemplate | None = None
    registration_file: Path | None = None
    if preregistered:
        if client_mode not in (OAuthClientMode.PREREGISTERED, OAuthClientMode.HOSTED_BRIDGE):
            raise ValueError("oauth_provider_input_invalid")
        registration_file = _registration_file(value["registration_file"], base=base)
    else:
        if client_mode is not OAuthClientMode.DYNAMIC_REGISTRATION:
            raise ValueError("oauth_provider_input_invalid")
        try:
            registration = OAuthDynamicRegistrationTemplate.from_dict(value["registration"])
        except (TypeError, ValueError):
            raise ValueError("oauth_provider_input_invalid") from None
    return OAuthProviderInput(
        profile_id=_input_text(value["profile_id"]),
        provider_id=_input_text(value["provider_id"]),
        app_publisher=_input_text(value["app_publisher"]),
        authorization_issuer=_input_text(value["authorization_issuer"]),
        resource_origins=_input_text_tuple(value["resource_origins"]),
        authorization_flow=authorization_flow,
        client_mode=client_mode,
        account_partition="issuer_account",
        allowed_plugin_publishers=_input_text_tuple(value["allowed_plugin_publishers"]),
        redirect_uri=redirect_uri,
        registration=registration,
        registration_file=registration_file,
        resource_identifier=(
            None
            if value.get("resource_identifier") is None
            else _input_text(value["resource_identifier"])
        ),
    )


def load_oauth_providers(path: Path) -> tuple[OAuthProviderInput, ...]:
    """Load one exact maintainer-authored provider list without reading client files."""

    source = Path(path)
    try:
        encoded = source.read_bytes()
        if not encoded or len(encoded) > _MAX_PROVIDERS_BYTES:
            raise ValueError
        document = json.loads(encoded.decode("utf-8"))
        if type(document) is not list or not document:
            raise ValueError
        providers = tuple(_provider_from_dict(item, base=source.parent) for item in document)
        profile_ids = tuple(item.profile_id for item in providers)
        if len(profile_ids) != len(set(profile_ids)):
            raise ValueError
        return providers
    except OSError:
        raise ValueError("oauth_provider_input_unreadable") from None
    except ValueError as exc:
        if str(exc) == "oauth_provider_registration_file_required":
            raise
        raise ValueError("oauth_provider_input_invalid") from None
    except (UnicodeError, json.JSONDecodeError, TypeError):
        raise ValueError("oauth_provider_input_invalid") from None


def _profile_dict(profile: OAuthProfile) -> dict[str, object]:
    document: dict[str, object] = {
        "schema_version": profile.schema_version,
        "profile_id": profile.profile_id,
        "catalog_signer": profile.catalog_signer,
        "allowed_plugin_publishers": list(profile.allowed_plugin_publishers),
        "app_publisher": profile.app_publisher,
        "provider_id": profile.provider_id,
        "authorization_issuer": profile.authorization_issuer,
        "resource_origins": list(profile.resource_origins),
        "client_mode": profile.client_mode.value,
        "authorization_flow": profile.authorization_flow.value,
        "account_partition": profile.account_partition,
        "revision": profile.revision,
    }
    if profile.client_id is not None:
        document["client_id"] = profile.client_id
    if profile.client_secret_ref is not None:
        document["client_secret_ref"] = profile.client_secret_ref
    if profile.client_metadata_document_url is not None:
        document["client_metadata_document_url"] = profile.client_metadata_document_url
    if profile.redirect_uri is not None:
        document["redirect_uri"] = profile.redirect_uri
    if profile.registration is not None:
        document["registration"] = profile.registration.to_dict()
    if profile.resource_identifier is not None:
        document["resource_identifier"] = profile.resource_identifier
    return document


def _build_profile(
    item: OAuthProviderInput,
    registration: PublicClientRegistration | None,
) -> OAuthProfile:
    if item.client_mode in (OAuthClientMode.PREREGISTERED, OAuthClientMode.HOSTED_BRIDGE):
        if registration is None:
            raise ValueError("oauth_registration_required")
        client_id = registration.client_id
        dynamic_registration = None
    else:
        if registration is not None or item.registration is None:
            raise ValueError("oauth_provider_input_invalid")
        client_id = None
        dynamic_registration = item.registration
    profile = OAuthProfile(
        schema_version=1,
        profile_id=item.profile_id,
        catalog_signer=_CATALOG_ID,
        allowed_plugin_publishers=item.allowed_plugin_publishers,
        app_publisher=item.app_publisher,
        provider_id=item.provider_id,
        authorization_issuer=item.authorization_issuer,
        resource_origins=item.resource_origins,
        client_mode=item.client_mode,
        client_id=client_id,
        client_secret_ref=None,
        client_metadata_document_url=None,
        redirect_uri=item.redirect_uri,
        account_partition=item.account_partition,
        revision="sha256:" + ("0" * 64),
        authorization_flow=item.authorization_flow,
        registration=dynamic_registration,
        resource_identifier=item.resource_identifier,
    )
    profile = replace(profile, revision=computed_profile_revision(profile))
    return OAuthProfile.from_verified_dict(_profile_dict(profile), catalog_signer=_CATALOG_ID)


def _oauth_signing_key_from_seed(
    seed: bytes,
    key_ring: TrustedPublisherKeyRing,
) -> Ed25519PrivateKey:
    if type(seed) is not bytes or len(seed) != 32:
        raise ValueError("oauth_profile_signing_key_invalid")
    try:
        private_key = Ed25519PrivateKey.from_private_bytes(seed)
    except (TypeError, ValueError):
        raise ValueError("oauth_profile_signing_key_invalid") from None
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    expected = key_ring.public_key_bytes(
        OAUTH_PROFILE_KEY_ID,
        _PUBLISHER_ID,
        _CATALOG_ID,
    )
    if public_key != expected:
        raise ValueError("oauth_profile_signing_key_mismatch")
    return private_key


def _signed_catalog(
    providers: tuple[OAuthProviderInput, ...],
    registrations: Mapping[Path, PublicClientRegistration],
    private_key: Ed25519PrivateKey,
    *,
    sequence: int,
) -> tuple[dict[str, object], str]:
    profiles = tuple(
        _build_profile(
            item,
            registrations[item.registration_file]
            if item.registration_file is not None
            else None,
        )
        for item in providers
    )
    unsigned = {
        "schema_version": 1,
        "catalog_id": _CATALOG_ID,
        "publisher_id": _PUBLISHER_ID,
        "sequence": sequence,
        "signing_key_id": OAUTH_PROFILE_KEY_ID,
        "profiles": [_profile_dict(profile) for profile in profiles],
    }
    signature = base64.b64encode(private_key.sign(canonical_bytes(unsigned))).decode("ascii")
    return {**unsigned, "signature": signature}, sha256(canonical_bytes(unsigned)).hexdigest()


def _atomic_fsync_write(path: Path, payload: bytes) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        with target.open("rb+") as stream:
            os.fsync(stream.fileno())
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ValueError("oauth_catalog_write_failed") from exc


def provision_oauth_profiles(
    *,
    providers: tuple[OAuthProviderInput, ...],
    catalog_path: Path,
    signing_seed: bytes,
    key_ring: TrustedPublisherKeyRing | None = None,
    sequence: int | None = None,
    remove_staging: Path | None = None,
) -> ProvisioningResult:
    """Validate provider inputs and signature before replacing the catalog.

    Every preregistered PKCE client must declare an accepted redirect; the
    whole catalog is signed and re-verified through the ring before the
    atomic publication, and any staging file is removed only after that
    durable commit.
    """

    if type(providers) is not tuple or not providers:
        raise ValueError("oauth_provider_input_invalid")
    if sequence is None:
        sequence = _next_catalog_sequence(catalog_path)
    if type(sequence) is not int or sequence < 1:
        raise ValueError("oauth_profile_catalog_invalid")
    registrations: dict[Path, PublicClientRegistration] = {}
    for item in providers:
        if type(item) is not OAuthProviderInput:
            raise ValueError("oauth_provider_input_invalid")
        if item.registration_file is None:
            continue
        if item.registration_file not in registrations:
            registrations[item.registration_file] = _load_registration(item.registration_file)
        registration = registrations[item.registration_file]
        if registration.client_secret is not None:
            raise ValueError("oauth_registration_invalid")
        if (
            item.authorization_flow is OAuthAuthorizationFlow.AUTHORIZATION_CODE_PKCE
            and (
                not registration.redirect_uris
                or any(not _is_accepted_redirect(uri) for uri in registration.redirect_uris)
            )
        ):
            raise ValueError("oauth_registration_redirect_mismatch")
    ring = load_embedded_key_ring() if key_ring is None else key_ring
    private_key = _oauth_signing_key_from_seed(signing_seed, ring)
    catalog, catalog_revision = _signed_catalog(
        providers, registrations, private_key, sequence=sequence
    )
    registry = OAuthProfileRegistry.from_document(catalog, ring)
    target = Path(catalog_path)
    _atomic_fsync_write(target, canonical_bytes(catalog))

    if remove_staging is not None:
        staging = Path(remove_staging)
        if staging.resolve(strict=False) == target.resolve(strict=False):
            raise ValueError("oauth_staging_path_invalid")
        try:
            staging.unlink()
        except OSError as exc:
            raise ValueError("oauth_staging_removal_failed") from exc

    unique_client_ids = tuple(
        dict.fromkeys(registration.client_id for registration in registrations.values())
    )
    return ProvisioningResult(
        catalog_path=target,
        registry=registry,
        profile_ids=tuple(item.profile_id for item in providers),
        client_id_hashes=tuple(
            sha256(value.encode("utf-8")).hexdigest() for value in unique_client_ids
        ),
        catalog_revision=catalog_revision,
    )


def provision_client_credential(
    *,
    registration_path: Path,
    catalog_path: Path,
    profile_ids: tuple[str, ...],
    signing_seed: bytes,
    vault: OAuthCredentialVault,
    key_ring: TrustedPublisherKeyRing | None = None,
    previous_key_ring: TrustedPublisherKeyRing | None = None,
) -> ProvisioningResult:
    """Bind an existing desktop client to vault custody without changing its scope.

    The signed catalog contains references only. Existing credentials may be
    reused, never silently replaced; a conflicting credential requires rotation.
    Vault records precede the atomic catalog publication, so interruption can
    leave an unused encrypted record but never a reference to an absent secret.
    During an approved signer rotation, the previous ring verifies the input;
    only the current ring verifies the output. Stop consumers until both the
    catalog and current ring have been deployed together.
    """
    if not profile_ids or len(set(profile_ids)) != len(profile_ids):
        raise ValueError("oauth_profile_unavailable")
    ring = load_embedded_key_ring() if key_ring is None else key_ring
    private_key = _oauth_signing_key_from_seed(signing_seed, ring)
    target = Path(catalog_path)
    document = json.loads(target.read_text(encoding="utf-8"))
    registry = OAuthProfileRegistry.from_document(
        document, ring if previous_key_ring is None else previous_key_ring,
    )
    if document["catalog_id"] != _CATALOG_ID:
        raise ValueError("oauth_profile_catalog_invalid")
    registration = _load_registration(registration_path)
    if registration.client_secret is None:
        raise ValueError("oauth_client_secret_required")
    credential = OAuthClientCredential(registration.client_id, registration.client_secret)
    entries = {entry["profile_id"]: entry for entry in document["profiles"]}
    if any(profile_id not in entries for profile_id in profile_ids):
        raise ValueError("oauth_profile_unavailable")
    records = {}
    updated_entries = {}
    for profile_id in profile_ids:
        entry = entries[profile_id]
        profile = registry.get_exact(profile_id, entry["revision"])
        if profile.client_id != registration.client_id:
            raise ValueError("oauth_registration_client_mismatch")
        if (
            profile.client_mode is not OAuthClientMode.PREREGISTERED
            or profile.authorization_flow is not OAuthAuthorizationFlow.AUTHORIZATION_CODE_PKCE
        ):
            raise ValueError("oauth_client_mode_mismatch")
        reference = profile.client_secret_ref or f"oauth-client/{profile_id}/secret"
        stored = vault.load_credential(reference)
        if stored is not None and stored[1] != credential:
            raise ValueError("oauth_client_credential_conflict")
        records[reference] = stored
        bound = replace(profile, client_secret_ref=reference)
        updated_entries[profile_id] = {
            **entry, "client_secret_ref": reference,
            "revision": computed_profile_revision(bound),
        }
    unsigned = {key: value for key, value in document.items() if key != "signature"}
    changed = (
        document["signing_key_id"] != OAUTH_PROFILE_KEY_ID
        or any(updated_entries[key] != entries[key] for key in profile_ids)
    )
    if changed:
        unsigned["signing_key_id"] = OAUTH_PROFILE_KEY_ID
        unsigned["sequence"] += 1
        unsigned["profiles"] = [
            updated_entries.get(entry["profile_id"], entry) for entry in document["profiles"]
        ]
    catalog = {
        **unsigned,
        "signature": base64.b64encode(private_key.sign(canonical_bytes(unsigned))).decode("ascii"),
    }
    verified = OAuthProfileRegistry.from_document(catalog, ring)
    for reference, stored in records.items():
        if stored is None:
            vault.store_credential(reference, credential, expected_revision=None)
    if changed:
        _atomic_fsync_write(target, canonical_bytes(catalog))
    return ProvisioningResult(
        catalog_path=target, registry=verified, profile_ids=profile_ids,
        client_id_hashes=(sha256(registration.client_id.encode()).hexdigest(),),
        catalog_revision=sha256(canonical_bytes(unsigned)).hexdigest(),
    )


def _signing_seed_from_environment() -> bytes:
    encoded = os.environ.get("OMEGA_OAUTH_PROFILE_SIGNING_KEY_B64", "").strip()
    if not encoded:
        raise ValueError("oauth_profile_signing_key_required")
    try:
        seed = base64.b64decode(encoded, validate=True)
    except (TypeError, ValueError):
        raise ValueError("oauth_profile_signing_key_invalid") from None
    if base64.b64encode(seed).decode("ascii") != encoded or len(seed) != 32:
        raise ValueError("oauth_profile_signing_key_invalid")
    return seed


def _seed_from_dpapi(path: Path) -> bytes:
    try:
        protected = Path(path).read_bytes()
    except OSError:
        raise ValueError("oauth_profile_signing_seed_unreadable") from None
    try:
        seed = _dpapi_unprotect(protected)
    except (OSError, ValueError):
        raise ValueError("oauth_profile_signing_seed_locked") from None
    if type(seed) is not bytes or len(seed) != 32:
        raise ValueError("oauth_profile_signing_key_invalid")
    return seed


def _seed_from_sealed(path: Path) -> bytes:
    try:
        return read_sealed_seed(Path(path), purpose=OAUTH_PROFILE_SIGNING_SEED_PURPOSE)
    except SealedSecretError as exc:
        reason = str(exc)
        if reason == "sealed_secret_unreadable":
            raise ValueError("oauth_profile_signing_seed_unreadable") from None
        if reason in ("sealed_passphrase_required", "sealed_secret_locked"):
            raise ValueError("oauth_profile_signing_seed_locked") from None
        raise ValueError("oauth_profile_signing_key_invalid") from None


def _generate_seed() -> tuple[Ed25519PrivateKey, bytes, str]:
    private_key = Ed25519PrivateKey.generate()
    seed = private_key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return private_key, seed, base64.b64encode(public_key).decode("ascii")


def _mint_seed_dpapi(path: Path) -> str:
    """Mint this machine's DPAPI-held seed; print only the public key."""

    target = Path(path)
    if target.exists():
        raise ValueError("oauth_profile_signing_seed_exists")
    _private_key, seed, public_key = _generate_seed()
    try:
        protected = _dpapi_protect(seed)
    except (OSError, ValueError):
        raise ValueError("oauth_profile_signing_seed_locked") from None
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
    except FileExistsError:
        raise ValueError("oauth_profile_signing_seed_exists") from None
    except OSError:
        raise ValueError("oauth_profile_signing_seed_write_failed") from None
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(protected)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return public_key


def _mint_seed_sealed(path: Path) -> str:
    """Mint the portable seed sealed under the operator passphrase; print only the public key."""

    target = Path(path)
    if target.exists():
        raise ValueError("oauth_profile_signing_seed_exists")
    try:
        passphrase = require_passphrase()
    except SealedSecretError:
        raise ValueError("oauth_profile_signing_seed_locked") from None
    _private_key, seed, public_key = _generate_seed()
    try:
        write_sealed_file(target, seed, passphrase, purpose=OAUTH_PROFILE_SIGNING_SEED_PURPOSE)
    except SealedSecretError as exc:
        reason = str(exc).removeprefix("sealed_secret_")
        raise ValueError(f"oauth_profile_signing_seed_{reason}") from None
    return public_key


def _signing_seed(arguments: argparse.Namespace) -> bytes:
    """Explicit custody first, then the environment, then the drive's sealed seed."""

    if arguments.seed_dpapi_path is not None:
        return _seed_from_dpapi(Path(arguments.seed_dpapi_path))
    if arguments.seed_sealed_path is not None:
        return _seed_from_sealed(Path(arguments.seed_sealed_path))
    if os.environ.get("OMEGA_OAUTH_PROFILE_SIGNING_KEY_B64", "").strip():
        return _signing_seed_from_environment()
    if _DEFAULT_SEED_SEALED_PATH.exists():
        return _seed_from_sealed(_DEFAULT_SEED_SEALED_PATH)
    raise ValueError("oauth_profile_signing_seed_required")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--providers", type=Path, default=_DEFAULT_PROVIDERS_PATH)
    parser.add_argument("--catalog", type=Path, default=_DEFAULT_CATALOG_PATH)
    parser.add_argument("--sequence", type=int, default=None)
    parser.add_argument("--remove-staging", type=Path)
    parser.add_argument(
        "--seed-dpapi-path",
        type=Path,
    )
    parser.add_argument(
        "--seed-sealed-path",
        nargs="?",
        const=str(_DEFAULT_SEED_SEALED_PATH),
        default=None,
    )
    parser.add_argument("--mint-seed-dpapi-path", type=Path)
    parser.add_argument(
        "--mint-seed-sealed-path",
        nargs="?",
        const=str(_DEFAULT_SEED_SEALED_PATH),
        default=None,
    )
    binding = parser.add_argument_group("client credential binding")
    binding.add_argument(
        "--bind-registration",
        type=Path,
        help="registration document carrying the client secret to bind into vault custody",
    )
    binding.add_argument(
        "--bind-profile",
        action="append",
        dest="bind_profiles",
        help="profile id to bind (repeatable); requires --bind-registration",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.mint_seed_dpapi_path is not None:
            print(f"public_key_base64={_mint_seed_dpapi(arguments.mint_seed_dpapi_path)}")
            return 0
        if arguments.mint_seed_sealed_path is not None:
            print(f"public_key_base64={_mint_seed_sealed(Path(arguments.mint_seed_sealed_path))}")
            return 0
        if arguments.bind_registration is not None:
            if not arguments.bind_profiles:
                raise ValueError("oauth_profile_unavailable")
            result = provision_client_credential(
                registration_path=arguments.bind_registration,
                catalog_path=arguments.catalog,
                profile_ids=tuple(arguments.bind_profiles),
                signing_seed=_signing_seed(arguments),
                vault=OAuthCredentialVault(),
            )
        else:
            result = provision_oauth_profiles(
                providers=load_oauth_providers(arguments.providers),
                catalog_path=arguments.catalog,
                signing_seed=_signing_seed(arguments),
                sequence=arguments.sequence,
                remove_staging=arguments.remove_staging,
            )
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    for profile_id in result.profile_ids:
        print(f"profile_id={profile_id}")
    for client_id_hash in result.client_id_hashes:
        print(f"client_id_sha256={client_id_hash}")
    print(f"catalog_revision={result.catalog_revision}")
    print(f"catalog_path={result.catalog_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
