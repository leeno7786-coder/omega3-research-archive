"""Signed, provider-neutral OAuth client profiles and exact identity resolution."""

from __future__ import annotations

import ipaddress
import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import Enum
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from mcp_platform.canonical import canonical_bytes
from plugin_platform.trust import TrustedPublisherKeyRing, verify_signed_bytes

_CATALOG_FIELDS = frozenset(
    {
        "schema_version",
        "catalog_id",
        "publisher_id",
        "sequence",
        "signing_key_id",
        "profiles",
        "signature",
    }
)
_COMMON_PROFILE_FIELDS = frozenset(
    {
        "schema_version",
        "profile_id",
        "catalog_signer",
        "allowed_plugin_publishers",
        "app_publisher",
        "provider_id",
        "authorization_issuer",
        "resource_origins",
        "client_mode",
        "authorization_flow",
        "account_partition",
        "revision",
    }
)
_MODE_PROFILE_FIELDS = frozenset(
    {"client_id", "client_secret_ref", "client_metadata_document_url", "registration"}
)
_FLOW_PROFILE_FIELDS = frozenset({"redirect_uri"})
# RFC 9728 lets a protected resource publish an identifier that is not the URL
# a client posts to (Slack names ``https://mcp.slack.com`` while serving MCP at
# ``https://mcp.slack.com/mcp``). A profile may therefore declare the identifier
# separately; when absent the endpoint is its own identifier, which is what
# every provider shipped before 2026-09-06 relies on.
_RESOURCE_PROFILE_FIELDS = frozenset({"resource_identifier"})
_DYNAMIC_REGISTRATION_TEMPLATE_FIELDS = frozenset(
    {
        "client_name",
        "redirect_uris",
        "token_endpoint_auth_method",
        "grant_types",
        "response_types",
        "scope",
        "software_id",
    }
)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*\Z")
_HOST_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*\Z")
_CREDENTIAL_REF_RE = re.compile(r"oauth-client/[A-Za-z0-9][A-Za-z0-9._+-]*/secret\Z")
_SCOPE_TOKEN_RE = re.compile(r"[\x21\x23-\x5B\x5D-\x7E]+\Z")
_LOWER_HEX = frozenset("0123456789abcdef")


class OAuthClientMode(str, Enum):
    """Signed, non-fallback OAuth client identity modes."""

    PREREGISTERED = "preregistered"
    CLIENT_METADATA_DOCUMENT = "client_metadata_document"
    DYNAMIC_REGISTRATION = "dynamic_registration"
    HOSTED_BRIDGE = "hosted_bridge"


class OAuthAuthorizationFlow(str, Enum):
    """The exact grant flow authorized by a signed public-client profile."""

    AUTHORIZATION_CODE_PKCE = "authorization_code_pkce"
    DEVICE_CODE = "device_code"


OMEGA_DYNAMIC_REGISTRATION_CLIENT_NAME = "Omega"
OMEGA_DYNAMIC_REGISTRATION_SOFTWARE_ID = "org.omega2ai.desktop"
OMEGA_DYNAMIC_REGISTRATION_TOKEN_ENDPOINT_AUTH_METHOD = "none"
OMEGA_DYNAMIC_REGISTRATION_GRANT_TYPES = ("authorization_code", "refresh_token")
OMEGA_DYNAMIC_REGISTRATION_RESPONSE_TYPES = ("code",)


def _stable_string(value: Any) -> str:
    if (
        type(value) is not str
        or not value
        or value.strip() != value
        or unicodedata.normalize("NFC", value) != value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("oauth_profile_value_invalid")
    return value


def stable_id(value: Any) -> str:
    identifier = _stable_string(value)
    if _IDENTIFIER_RE.fullmatch(identifier) is None:
        raise ValueError("oauth_profile_identity_invalid")
    return identifier


def stable_id_tuple(value: Any) -> tuple[str, ...]:
    if type(value) is not list:
        raise ValueError("oauth_profile_identity_invalid")
    identifiers = tuple(stable_id(item) for item in value)
    if not identifiers or len(identifiers) != len(set(identifiers)):
        raise ValueError("oauth_profile_identity_invalid")
    return identifiers


def exact_schema_version(value: Any) -> int:
    if type(value) is not int or value != 1:
        raise ValueError("oauth_profile_schema_version_invalid")
    return value


def positive_sequence(value: Any) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("oauth_profile_catalog_invalid")
    return value


def exact_list(value: Any) -> list[Any]:
    if type(value) is not list:
        raise ValueError("oauth_profile_catalog_invalid")
    return value


def exact_object(
    value: Any,
    fields: frozenset[str],
    *,
    reason: str,
) -> dict[str, Any]:
    if (
        type(value) is not dict
        or any(type(key) is not str for key in value)
        or set(value) != fields
    ):
        raise ValueError(reason)
    return dict(value)


def exact_profile_fields(value: Any) -> dict[str, Any]:
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise ValueError("oauth_profile_invalid")
    observed = set(value)
    if not _COMMON_PROFILE_FIELDS.issubset(observed) or not observed.issubset(
        _COMMON_PROFILE_FIELDS
        | _MODE_PROFILE_FIELDS
        | _FLOW_PROFILE_FIELDS
        | _RESOURCE_PROFILE_FIELDS
    ):
        raise ValueError("oauth_profile_invalid")
    return dict(value)


def validate_client_mode_document_fields(
    fields: Mapping[str, object], client_mode: OAuthClientMode
) -> None:
    observed = set(fields) & _MODE_PROFILE_FIELDS
    if client_mode in (OAuthClientMode.PREREGISTERED, OAuthClientMode.HOSTED_BRIDGE):
        valid_field_sets = ({"client_id"}, {"client_id", "client_secret_ref"})
        valid = observed in valid_field_sets and all(
            fields[field] is not None for field in observed
        )
        if client_mode is OAuthClientMode.HOSTED_BRIDGE:
            valid = observed == {"client_id"}
    elif client_mode is OAuthClientMode.CLIENT_METADATA_DOCUMENT:
        valid = (
            observed == {"client_metadata_document_url"}
            and fields["client_metadata_document_url"] is not None
        )
    else:
        valid = observed == {"registration"} and fields["registration"] is not None
    if not valid:
        raise ValueError("oauth_profile_client_mode_fields_invalid")


def validate_authorization_flow_document_fields(
    fields: Mapping[str, object],
    authorization_flow: OAuthAuthorizationFlow,
    client_mode: OAuthClientMode,
) -> None:
    has_redirect = "redirect_uri" in fields and fields["redirect_uri"] is not None
    if authorization_flow is OAuthAuthorizationFlow.AUTHORIZATION_CODE_PKCE:
        valid = has_redirect
    else:
        valid = (
            not has_redirect
            and client_mode is OAuthClientMode.PREREGISTERED
            and "client_secret_ref" not in fields
        )
    if not valid:
        raise ValueError("oauth_profile_authorization_flow_invalid")


def stable_signature(value: Any) -> str:
    try:
        return _stable_string(value)
    except ValueError:
        raise ValueError("oauth_profile_catalog_invalid") from None


def revision_digest(value: Any) -> str:
    try:
        revision = _stable_string(value)
    except ValueError:
        raise ValueError("oauth_profile_revision_invalid") from None
    digest = revision.removeprefix("sha256:")
    if (
        not revision.startswith("sha256:")
        or len(digest) != 64
        or any(character not in _LOWER_HEX for character in digest)
    ):
        raise ValueError("oauth_profile_revision_invalid")
    return revision


def _canonical_host(hostname: str, *, loopback: bool) -> tuple[str, bool]:
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        if loopback:
            raise ValueError("oauth_url_invalid") from None
        try:
            host = hostname.lower().encode("idna").decode("ascii").lower()
        except (UnicodeError, ValueError):
            raise ValueError("oauth_url_invalid") from None
        if _HOST_RE.fullmatch(host) is None:
            raise ValueError("oauth_url_invalid")
        return host, False
    if loopback and not address.is_loopback:
        raise ValueError("oauth_url_invalid")
    return hostname.lower(), address.version == 6


def _explicit_port(netloc: str, *, ipv6: bool) -> str | None:
    if ipv6:
        if not netloc.startswith("[") or "]" not in netloc:
            raise ValueError("oauth_url_invalid")
        suffix = netloc[netloc.index("]") + 1 :]
        if not suffix:
            return None
        if not suffix.startswith(":"):
            raise ValueError("oauth_url_invalid")
        port_text = suffix[1:]
    else:
        if ":" not in netloc:
            return None
        if netloc.count(":") != 1:
            raise ValueError("oauth_url_invalid")
        port_text = netloc.rsplit(":", 1)[1]
    if not port_text or not port_text.isascii() or not port_text.isdecimal():
        raise ValueError("oauth_url_invalid")
    return port_text


def _canonical_https_url(
    value: Any,
    *,
    allow_path: bool,
    loopback: bool = False,
) -> str:
    """Canonicalize only scheme/IDNA host/default port and preserve path bytes."""
    try:
        url = _stable_string(value)
        if any(character.isspace() for character in url) or "?" in url or "#" in url:
            raise ValueError("oauth_url_invalid")
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        if scheme != ("http" if loopback else "https"):
            raise ValueError("oauth_url_invalid")
        if (
            not parsed.netloc
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or "\\" in parsed.path
        ):
            raise ValueError("oauth_url_invalid")
        host, ipv6 = _canonical_host(parsed.hostname, loopback=loopback)
        port = parsed.port
        port_text = _explicit_port(parsed.netloc, ipv6=ipv6)
        if port_text is not None:
            if port is None or port < 1 or port_text != str(port):
                raise ValueError("oauth_url_invalid")
        if port is not None and port < 1:
            raise ValueError("oauth_url_invalid")
        if not allow_path and parsed.path not in {"", "/"}:
            raise ValueError("oauth_url_invalid")
    except (TypeError, ValueError):
        raise ValueError("oauth_url_invalid") from None

    authority = f"[{host}]" if ipv6 else host
    default_port = 80 if loopback else 443
    if port is not None and port != default_port:
        authority = f"{authority}:{port}"
    path = parsed.path if allow_path else ""
    return f"{scheme}://{authority}{path}"


def canonical_issuer(value: Any) -> str:
    try:
        return _canonical_https_url(value, allow_path=True)
    except ValueError:
        raise ValueError("oauth_profile_issuer_invalid") from None


def canonical_resource_tuple(value: Any) -> tuple[str, ...]:
    if type(value) is not list or not value:
        raise ValueError("oauth_profile_resource_origin_invalid")
    try:
        resources = tuple(_canonical_https_url(item, allow_path=True) for item in value)
    except ValueError:
        raise ValueError("oauth_profile_resource_origin_invalid") from None
    if len(resources) != len(set(resources)):
        raise ValueError("oauth_profile_resource_origin_invalid")
    return resources


def canonical_loopback_redirect(value: Any) -> str:
    try:
        redirect = _canonical_https_url(value, allow_path=True, loopback=True)
        if urlsplit(redirect).path in {"", "/"}:
            raise ValueError("oauth_url_invalid")
        return redirect
    except ValueError:
        raise ValueError("oauth_profile_redirect_uri_invalid") from None


# Vendors such as Slack refuse plain-HTTP loopback redirects. Omega therefore
# registers one public HTTPS relay that answers ``/oauth/<profile_id>/callback``
# with a 302 to the same path on the loopback HUD, query string intact. The
# relay never sees a token: the authorization code is bound to this device's
# PKCE verifier and ``state``, so the redirect is useless to anyone else.
OAUTH_REDIRECT_RELAY_ORIGIN = "https://plugins.omega2ai.com"
OAUTH_LOOPBACK_CALLBACK_ORIGIN = "http://127.0.0.1:13310"
_OAUTH_CALLBACK_PATH_PREFIX = "/oauth/"


def canonical_relay_redirect(value: Any) -> str:
    """The relay form: ``https://plugins.omega2ai.com/oauth/<profile_id>/callback``."""
    try:
        redirect = _canonical_https_url(value, allow_path=True)
        parsed = urlsplit(redirect)
        if (
            f"{parsed.scheme}://{parsed.netloc}" != OAUTH_REDIRECT_RELAY_ORIGIN
            or not parsed.path.startswith(_OAUTH_CALLBACK_PATH_PREFIX)
            or not parsed.path.endswith("/callback")
            or parsed.path.count("/") != 3
        ):
            raise ValueError("oauth_url_invalid")
        return redirect
    except ValueError:
        raise ValueError("oauth_profile_redirect_uri_invalid") from None


def canonical_redirect_uri(value: Any) -> str:
    """A profile redirect is either the loopback callback or its HTTPS relay form."""
    try:
        return canonical_relay_redirect(value)
    except ValueError:
        return canonical_loopback_redirect(value)


def callback_landing_uri(redirect_uri: str) -> str:
    """Where the browser actually arrives for ``redirect_uri``.

    A relay redirect lands on the loopback HUD at the same path (the relay's
    302 target); a loopback redirect lands on itself. The token exchange still
    presents the registered ``redirect_uri`` unchanged.
    """
    parsed = urlsplit(redirect_uri)
    if f"{parsed.scheme}://{parsed.netloc}" == OAUTH_REDIRECT_RELAY_ORIGIN:
        return f"{OAUTH_LOOPBACK_CALLBACK_ORIGIN}{parsed.path}"
    return redirect_uri


def optional_public_client_id(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return _stable_string(value)
    except ValueError:
        raise ValueError("oauth_profile_client_mode_fields_invalid") from None


def optional_credential_ref(value: Any) -> str | None:
    if value is None:
        return None
    try:
        reference = _stable_string(value)
    except ValueError:
        raise ValueError("oauth_profile_client_mode_fields_invalid") from None
    if _CREDENTIAL_REF_RE.fullmatch(reference) is None:
        raise ValueError("oauth_profile_client_mode_fields_invalid")
    return reference


def optional_metadata_url(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return _canonical_https_url(value, allow_path=True)
    except ValueError:
        raise ValueError("oauth_profile_metadata_url_invalid") from None


def exact_account_partition(value: Any) -> str:
    if value != "issuer_account" or type(value) is not str:
        raise ValueError("oauth_profile_account_partition_invalid")
    return value


def _dynamic_registration_scope(value: Any) -> str:
    if type(value) is not str:
        raise ValueError("oauth_dynamic_registration_template_invalid")
    if value == "":
        return value
    tokens = value.split(" ")
    if len(tokens) != len(set(tokens)) or any(
        _SCOPE_TOKEN_RE.fullmatch(token) is None for token in tokens
    ):
        raise ValueError("oauth_dynamic_registration_template_invalid")
    return value


@dataclass(frozen=True, slots=True)
class OAuthDynamicRegistrationTemplate:
    """The fixed RFC 7591 dynamic-client-registration request Omega signs."""

    client_name: str
    redirect_uris: tuple[str, ...]
    token_endpoint_auth_method: str
    grant_types: tuple[str, ...]
    response_types: tuple[str, ...]
    scope: str
    software_id: str

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> OAuthDynamicRegistrationTemplate:
        try:
            fields = exact_object(
                value,
                _DYNAMIC_REGISTRATION_TEMPLATE_FIELDS,
                reason="oauth_dynamic_registration_template_invalid",
            )
            if (
                fields["client_name"] != OMEGA_DYNAMIC_REGISTRATION_CLIENT_NAME
                or fields["token_endpoint_auth_method"]
                != OMEGA_DYNAMIC_REGISTRATION_TOKEN_ENDPOINT_AUTH_METHOD
                or fields["software_id"] != OMEGA_DYNAMIC_REGISTRATION_SOFTWARE_ID
                or type(fields["grant_types"]) is not list
                or tuple(fields["grant_types"]) != OMEGA_DYNAMIC_REGISTRATION_GRANT_TYPES
                or type(fields["response_types"]) is not list
                or tuple(fields["response_types"]) != OMEGA_DYNAMIC_REGISTRATION_RESPONSE_TYPES
                or type(fields["redirect_uris"]) is not list
                or len(fields["redirect_uris"]) != 1
            ):
                raise ValueError("oauth_dynamic_registration_template_invalid")
            redirect_uri = _stable_string(fields["redirect_uris"][0])
            scope = _dynamic_registration_scope(fields["scope"])
        except ValueError:
            raise ValueError("oauth_dynamic_registration_template_invalid") from None
        return cls(
            client_name=fields["client_name"],
            redirect_uris=(redirect_uri,),
            token_endpoint_auth_method=fields["token_endpoint_auth_method"],
            grant_types=OMEGA_DYNAMIC_REGISTRATION_GRANT_TYPES,
            response_types=OMEGA_DYNAMIC_REGISTRATION_RESPONSE_TYPES,
            scope=scope,
            software_id=fields["software_id"],
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "client_name": self.client_name,
            "redirect_uris": list(self.redirect_uris),
            "token_endpoint_auth_method": self.token_endpoint_auth_method,
            "grant_types": list(self.grant_types),
            "response_types": list(self.response_types),
            "scope": self.scope,
            "software_id": self.software_id,
        }


@dataclass(frozen=True, slots=True, repr=False)
class OAuthProfile:
    """An immutable client profile parsed only after catalog verification."""

    schema_version: int
    profile_id: str
    catalog_signer: str
    allowed_plugin_publishers: tuple[str, ...]
    app_publisher: str
    provider_id: str
    authorization_issuer: str
    resource_origins: tuple[str, ...]
    client_mode: OAuthClientMode
    client_id: str | None
    client_secret_ref: str | None
    client_metadata_document_url: str | None
    redirect_uri: str | None
    account_partition: str
    revision: str
    authorization_flow: OAuthAuthorizationFlow = OAuthAuthorizationFlow.AUTHORIZATION_CODE_PKCE
    registration: OAuthDynamicRegistrationTemplate | None = None
    resource_identifier: str | None = None

    def __repr__(self) -> str:
        return f"OAuthProfile(profile_id={self.profile_id!r}, revision={self.revision!r})"

    def identifier_for(self, resource_origin: str) -> str:
        """Return the RFC 9728 resource identifier governing one declared endpoint.

        Without an explicit identifier an endpoint identifies itself, which is
        how every provider that publishes its metadata at the endpoint's own
        suffixed well-known path behaves.
        """
        if resource_origin not in self.resource_origins:
            raise ValueError("oauth_resource_mismatch")
        return self.resource_identifier or resource_origin

    @classmethod
    def from_verified_dict(
        cls,
        value: Mapping[str, object],
        *,
        catalog_signer: str,
    ) -> OAuthProfile:
        fields = exact_profile_fields(value)
        if fields["catalog_signer"] != catalog_signer:
            raise ValueError("oauth_profile_catalog_signer_mismatch")
        try:
            client_mode = OAuthClientMode(fields["client_mode"])
        except (TypeError, ValueError):
            raise ValueError("oauth_profile_client_mode_invalid") from None
        try:
            authorization_flow = OAuthAuthorizationFlow(fields["authorization_flow"])
        except (TypeError, ValueError):
            raise ValueError("oauth_profile_authorization_flow_invalid") from None
        validate_client_mode_document_fields(fields, client_mode)
        validate_authorization_flow_document_fields(fields, authorization_flow, client_mode)
        profile = cls(
            schema_version=exact_schema_version(fields["schema_version"]),
            profile_id=stable_id(fields["profile_id"]),
            catalog_signer=stable_id(fields["catalog_signer"]),
            allowed_plugin_publishers=stable_id_tuple(fields["allowed_plugin_publishers"]),
            app_publisher=stable_id(fields["app_publisher"]),
            provider_id=stable_id(fields["provider_id"]),
            authorization_issuer=canonical_issuer(fields["authorization_issuer"]),
            resource_origins=canonical_resource_tuple(fields["resource_origins"]),
            client_mode=client_mode,
            client_id=optional_public_client_id(fields.get("client_id")),
            client_secret_ref=optional_credential_ref(fields.get("client_secret_ref")),
            client_metadata_document_url=optional_metadata_url(
                fields.get("client_metadata_document_url")
            ),
            redirect_uri=(
                canonical_redirect_uri(fields["redirect_uri"])
                if authorization_flow is OAuthAuthorizationFlow.AUTHORIZATION_CODE_PKCE
                else None
            ),
            account_partition=exact_account_partition(fields["account_partition"]),
            revision=revision_digest(fields["revision"]),
            authorization_flow=authorization_flow,
            registration=(
                OAuthDynamicRegistrationTemplate.from_dict(fields["registration"])
                if fields.get("registration") is not None
                else None
            ),
            resource_identifier=optional_resource_identifier(
                fields.get("resource_identifier")
            ),
        )
        validate_resource_identifier(profile)
        validate_client_mode_fields(profile)
        if (
            profile.registration is not None
            and profile.registration.redirect_uris != (profile.redirect_uri,)
        ):
            raise ValueError("oauth_dynamic_registration_template_invalid")
        if profile.revision != computed_profile_revision(profile):
            raise ValueError("oauth_profile_revision_mismatch")
        return profile


def optional_resource_identifier(value: Any) -> str | None:
    if value is None:
        return None
    return _canonical_resource_origin(value)


def validate_resource_identifier(profile: OAuthProfile) -> None:
    """An identifier may only speak for endpoints on its own origin.

    Without this a signed profile could bind a token audience to one host while
    the client posts its requests to another, which is exactly the confusion
    RFC 8707's ``resource`` parameter exists to prevent.
    """
    if profile.resource_identifier is None:
        return
    identifier = urlsplit(profile.resource_identifier)
    for origin in profile.resource_origins:
        endpoint = urlsplit(origin)
        if (identifier.scheme, identifier.netloc) != (endpoint.scheme, endpoint.netloc):
            raise ValueError("oauth_profile_resource_identifier_invalid")


def validate_client_mode_fields(profile: OAuthProfile) -> None:
    if profile.client_mode in (OAuthClientMode.PREREGISTERED, OAuthClientMode.HOSTED_BRIDGE):
        valid = (
            profile.client_id is not None
            and profile.client_metadata_document_url is None
            and profile.registration is None
            and (
                profile.client_mode is not OAuthClientMode.HOSTED_BRIDGE
                or profile.client_secret_ref is None
            )
        )
    elif profile.client_mode is OAuthClientMode.CLIENT_METADATA_DOCUMENT:
        valid = (
            profile.client_id is None
            and profile.client_secret_ref is None
            and profile.client_metadata_document_url is not None
            and profile.registration is None
        )
    else:
        valid = (
            profile.client_id is None
            and profile.client_secret_ref is None
            and profile.client_metadata_document_url is None
            and profile.registration is not None
        )
    if not valid:
        raise ValueError("oauth_profile_client_mode_fields_invalid")
    if profile.authorization_flow is OAuthAuthorizationFlow.AUTHORIZATION_CODE_PKCE:
        flow_valid = profile.redirect_uri is not None
    else:
        flow_valid = (
            profile.client_mode is OAuthClientMode.PREREGISTERED
            and profile.client_secret_ref is None
            and profile.redirect_uri is None
        )
    if not flow_valid:
        raise ValueError("oauth_profile_authorization_flow_invalid")


def _unsigned_profile_dict(profile: OAuthProfile) -> dict[str, object]:
    unsigned: dict[str, object] = {
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
    }
    if profile.redirect_uri is not None:
        unsigned["redirect_uri"] = profile.redirect_uri
    if profile.client_id is not None:
        unsigned["client_id"] = profile.client_id
    if profile.client_secret_ref is not None:
        unsigned["client_secret_ref"] = profile.client_secret_ref
    if profile.client_metadata_document_url is not None:
        unsigned["client_metadata_document_url"] = profile.client_metadata_document_url
    if profile.registration is not None:
        unsigned["registration"] = profile.registration.to_dict()
    if profile.resource_identifier is not None:
        unsigned["resource_identifier"] = profile.resource_identifier
    return unsigned


def computed_profile_revision(profile: OAuthProfile) -> str:
    digest = sha256(canonical_bytes(_unsigned_profile_dict(profile))).hexdigest()
    return f"sha256:{digest}"


@dataclass(frozen=True, slots=True)
class OAuthProfileMatchInput:
    """Already-verified identity fields accepted by the exact resolver."""

    catalog_signer: str
    plugin_publisher: str
    app_publisher: str
    provider_id: str
    authorization_issuer: str
    resource_origin: str

    def normalized(self) -> OAuthProfileMatchInput:
        return replace(
            self,
            authorization_issuer=canonical_issuer(self.authorization_issuer),
            resource_origin=_canonical_resource_origin(self.resource_origin),
        )


def _canonical_resource_origin(value: Any) -> str:
    try:
        return _canonical_https_url(value, allow_path=True)
    except ValueError:
        raise ValueError("oauth_profile_resource_origin_invalid") from None


@dataclass(frozen=True, slots=True)
class OAuthProfileCatalog:
    """One publisher-bound, signed catalog of immutable OAuth profiles."""

    schema_version: int
    catalog_id: str
    publisher_id: str
    sequence: int
    signing_key_id: str
    profiles: tuple[OAuthProfile, ...]
    signature: str

    @classmethod
    def from_signed_dict(
        cls,
        value: Mapping[str, object],
        key_ring: TrustedPublisherKeyRing,
    ) -> OAuthProfileCatalog:
        document = exact_object(
            value,
            _CATALOG_FIELDS,
            reason="oauth_profile_catalog_invalid",
        )
        signature = stable_signature(document["signature"])
        unsigned = {key: item for key, item in document.items() if key != "signature"}
        catalog_id = stable_id(document["catalog_id"])
        publisher_id = stable_id(document["publisher_id"])
        signing_key_id = stable_id(document["signing_key_id"])
        verify_signed_bytes(
            canonical_bytes(unsigned),
            signature,
            key_ring,
            key_id=signing_key_id,
            publisher_id=publisher_id,
            subject_id=catalog_id,
        )
        profiles = tuple(
            OAuthProfile.from_verified_dict(item, catalog_signer=catalog_id)
            for item in exact_list(document["profiles"])
        )
        profile_ids = tuple(profile.profile_id for profile in profiles)
        if len(profile_ids) != len(set(profile_ids)):
            raise ValueError("oauth_profile_catalog_invalid")
        return cls(
            schema_version=exact_schema_version(document["schema_version"]),
            catalog_id=catalog_id,
            publisher_id=publisher_id,
            sequence=positive_sequence(document["sequence"]),
            signing_key_id=signing_key_id,
            profiles=profiles,
            signature=signature,
        )


@dataclass(frozen=True, slots=True)
class OAuthProfileRegistry:
    """Verified OAuth profiles resolved by one exact signed identity tuple."""

    _catalog: OAuthProfileCatalog

    @classmethod
    def from_document(
        cls,
        document: Mapping[str, object],
        key_ring: TrustedPublisherKeyRing,
    ) -> OAuthProfileRegistry:
        return cls(OAuthProfileCatalog.from_signed_dict(document, key_ring))

    @classmethod
    def from_path(
        cls,
        path: Path,
        key_ring: TrustedPublisherKeyRing,
    ) -> OAuthProfileRegistry:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_document(document, key_ring)

    def resolve_by_profile_id(self, profile_id: str) -> OAuthProfile:
        """Return the one profile with this id (build-time overlay lookup)."""
        found = tuple(
            profile for profile in self._catalog.profiles if profile.profile_id == profile_id
        )
        if len(found) != 1:
            raise ValueError("oauth_profile_unavailable")
        return found[0]

    def get_exact(self, profile_id: str, revision: str) -> OAuthProfile:
        found = tuple(
            profile
            for profile in self._catalog.profiles
            if profile.profile_id == profile_id and profile.revision == revision
        )
        if len(found) != 1:
            raise ValueError("oauth_profile_unavailable")
        return found[0]

    def resolve(self, match: OAuthProfileMatchInput) -> OAuthProfile:
        normalized = match.normalized()
        found = tuple(
            profile
            for profile in self._catalog.profiles
            if profile.catalog_signer == normalized.catalog_signer
            and normalized.plugin_publisher in profile.allowed_plugin_publishers
            and profile.app_publisher == normalized.app_publisher
            and profile.provider_id == normalized.provider_id
            and profile.authorization_issuer == normalized.authorization_issuer
            and normalized.resource_origin in profile.resource_origins
        )
        if not found:
            raise ValueError("oauth_profile_unavailable")
        if len(found) != 1:
            raise ValueError("oauth_profile_ambiguous")
        return found[0]

    def resolve_declared_app(self, match: OAuthProfileMatchInput) -> OAuthProfile:
        """Resolve an exact profile beneath a signed app's issuer-origin ceiling.

        App packages may declare only the provider origin while the signed
        desktop profile owns the RFC 8414 issuer path.  Only that root-origin
        specialization is permitted; aliases and non-root path substitutions
        remain unavailable.
        """

        normalized = match.normalized()
        declared = urlsplit(normalized.authorization_issuer)
        if declared.path not in {"", "/"}:
            return self.resolve(normalized)
        found: list[OAuthProfile] = []
        for profile in self._catalog.profiles:
            issuer = urlsplit(profile.authorization_issuer)
            if (
                profile.catalog_signer == normalized.catalog_signer
                and normalized.plugin_publisher in profile.allowed_plugin_publishers
                and profile.app_publisher == normalized.app_publisher
                and profile.provider_id == normalized.provider_id
                and normalized.resource_origin in profile.resource_origins
                and issuer.scheme == declared.scheme
                and issuer.netloc == declared.netloc
            ):
                found.append(profile)
        if not found:
            raise ValueError("oauth_profile_unavailable")
        if len(found) != 1:
            raise ValueError("oauth_profile_ambiguous")
        return found[0]
