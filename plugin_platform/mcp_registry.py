"""Bounded validation and paging of the Official MCP Registry as discovery input.

The upstream registry authenticates publisher namespaces; it does not endorse
behavior, pin artifacts, or grant capabilities. Nothing here may therefore reach
``TrustTier.OMEGA_VERIFIED``: a record admitted from the registry is at most
``TrustTier.PUBLISHER_VERIFIED``, and a response that tries to assert Omega trust
is rejected rather than believed.

The plugin foundation performs no network I/O, so paging is expressed as a pure
state machine over an injected synchronous transport that returns one page of
bounded bytes. The transport reports its own failures as a typed
``McpRegistryError`` carrying a stable ``plugin_registry_*`` cause, which this
module propagates unchanged; any other exception it raises is replaced with a
stable cause so raw transport text never crosses the boundary.

Two separate failure modes are deliberate:

* **Structural rejection** (``plugin_registry_page_invalid`` and friends) applies
  to what the registry itself controls: the response envelope, the entry wrapper,
  registry-managed ``_meta``, the cursor and count, and the ``name``/``version``
  identity of a listing. Those, and only those, fail the whole page closed,
  because a response that violates them is not the registry's output and no
  listing can be admitted without an identity.
* **Recorded non-support** (``unsupported_reasons``) applies to everything else a
  publisher controls inside one listing: distribution kinds, endpoint origins,
  link canonicality, version resolvability, declaration sizes and shapes, and
  publisher display text. Those degrade one record explicitly instead of letting
  a single stray field deny discovery to every other publisher on the page. A
  reason in ``_DISQUALIFYING_REASONS`` also makes the record incompatible, so
  degrading is never a way to smuggle unusable metadata through.

Publisher-authored extension metadata is never surfaced as text. It is preserved
only as a capped canonical digest so a later importer can prove the extension
bytes it saw are the extension bytes discovery saw.
"""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import urlsplit

from mcp_platform.canonical import canonical_bytes, sha256_hex
from plugin_platform.catalog import TrustTier
from plugin_platform.contracts import _identifier, _semver, _stable_string

MAX_REGISTRY_PAGES = 8
MAX_PAGE_SERVERS = 100
MAX_PAGE_BYTES = 512 * 1024
MAX_DOCUMENT_DEPTH = 12
MAX_NAME_CHARS = 200
MAX_TITLE_CHARS = 100
MAX_DESCRIPTION_CHARS = 400
MAX_VERSION_CHARS = 64
MAX_CURSOR_CHARS = 200
MAX_QUERY_CHARS = 100
MAX_URL_CHARS = 300
MAX_URL_SEGMENTS = 8
MAX_IDENTIFIER_CHARS = 160
MAX_PACKAGES = 6
MAX_REMOTES = 4
MAX_ARGUMENTS = 16
MAX_ENVIRONMENT_VARIABLES = 24
MAX_HEADERS = 8
MAX_EXTENSION_BYTES = 8192
DEFAULT_PAGE_LIMIT = 50

_CAUSE_RE = re.compile(r"plugin_registry_[a-z][a-z_]{0,62}\Z")
_CLIENT_INVALID = "plugin_registry_client_invalid"
_PAGE_INVALID = "plugin_registry_page_invalid"
_PAGE_TOO_LARGE = "plugin_registry_page_too_large"
_RECORD_INVALID = "plugin_registry_record_invalid"
_REQUEST_INVALID = "plugin_registry_request_invalid"
_TOO_MANY_PAGES = "plugin_registry_too_many_pages"
_TRANSPORT_INVALID = "plugin_registry_transport_invalid"

DECLARATION_UNBOUNDED = "plugin_registry_declaration_unbounded"
DISTRIBUTION_ABSENT = "plugin_registry_distribution_absent"
EXTENSIONS_INVALID = "plugin_registry_extensions_invalid"
EXTENSIONS_TOO_LARGE = "plugin_registry_extensions_too_large"
METADATA_INVALID = "plugin_registry_metadata_invalid"
PACKAGE_DECLARATION_UNBOUNDED = "plugin_registry_package_declaration_unbounded"
PACKAGE_INVALID = "plugin_registry_package_invalid"
PACKAGE_REGISTRY_UNSUPPORTED = "plugin_registry_package_registry_unsupported"
PACKAGE_TRANSPORT_UNSUPPORTED = "plugin_registry_package_transport_unsupported"
REMOTE_DECLARATION_UNBOUNDED = "plugin_registry_remote_declaration_unbounded"
REMOTE_INVALID = "plugin_registry_remote_invalid"
REMOTE_ORIGIN_PRIVATE = "plugin_registry_remote_origin_private"
REMOTE_TRANSPORT_UNSUPPORTED = "plugin_registry_remote_transport_unsupported"
REMOTE_URL_NONCANONICAL = "plugin_registry_remote_url_noncanonical"
URL_NONCANONICAL = "plugin_registry_url_noncanonical"
VERSION_UNSUPPORTED = "plugin_registry_version_unsupported"

_OFFICIAL_META_KEY = "io.modelcontextprotocol.registry/official"
_GITHUB_NAMESPACE_PREFIX = "io.github."
_STDIO_TRANSPORT = "stdio"

_ENVELOPE_FIELDS = frozenset({"servers", "metadata"})
_ENTRY_FIELDS = frozenset({"server", "_meta"})
_METADATA_REQUIRED = frozenset({"count"})
_METADATA_OPTIONAL = frozenset({"nextCursor"})
_OFFICIAL_REQUIRED = frozenset({"status", "publishedAt", "isLatest"})
_OFFICIAL_OPTIONAL = frozenset({"updatedAt"})
_SERVER_REQUIRED = frozenset({"name", "version"})
_SERVER_FIELDS = frozenset(
    {
        "name",
        "description",
        "version",
        "title",
        "websiteUrl",
        "repository",
        "packages",
        "remotes",
    }
)
_REPOSITORY_REQUIRED = frozenset({"url"})
_REPOSITORY_FIELDS = frozenset({"url", "source", "id", "subfolder"})
_PACKAGE_REQUIRED = frozenset({"registryType", "identifier", "transport"})
_PACKAGE_FIELDS = frozenset(
    {
        "registryType",
        "registryBaseUrl",
        "identifier",
        "version",
        "fileSha256",
        "runtimeHint",
        "transport",
        "runtimeArguments",
        "packageArguments",
        "environmentVariables",
    }
)
_REMOTE_REQUIRED = frozenset({"type", "url"})
_REMOTE_FIELDS = frozenset({"type", "url", "headers"})
_TRANSPORT_REQUIRED = frozenset({"type"})
_TRANSPORT_FIELDS = frozenset({"type", "url", "headers"})
_VARIABLE_REQUIRED = frozenset({"name"})
_VARIABLE_FIELDS = frozenset(
    {
        "name",
        "description",
        "isRequired",
        "isSecret",
        "default",
        "value",
        "format",
        "choices",
        "isRepeated",
        "variables",
    }
)

_NAMESPACE_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+\Z")
_SERVER_SEGMENT_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,62}[A-Za-z0-9])?\Z")
_CURSOR_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._:/@+-]{0,198}[A-Za-z0-9])?\Z")
_QUERY_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9 ._/@+-]{0,98}[A-Za-z0-9])?\Z")
_HOST_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*\Z")
_TLD_RE = re.compile(r"[a-z]{2,63}\Z")
_URL_SEGMENT_RE = re.compile(r"[A-Za-z0-9._~-]+\Z")
_PACKAGE_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9@](?:[A-Za-z0-9._@/:+-]{0,158}[A-Za-z0-9])?\Z")
_VARIABLE_NAME_RE = re.compile(r"[A-Za-z](?:[A-Za-z0-9_-]{0,62}[A-Za-z0-9])?\Z")
_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")

_QUOTE_BYTE = 0x22
_ESCAPE_BYTE = 0x5C
_OPEN_STRUCTURE_BYTES = frozenset({0x5B, 0x7B})
_CLOSE_STRUCTURE_BYTES = frozenset({0x5D, 0x7D})
_DISQUALIFYING_REASONS = frozenset({METADATA_INVALID})

_PRIVATE_HOSTS = frozenset({"localhost"})
_PRIVATE_HOST_SUFFIXES = (
    ".corp",
    ".home.arpa",
    ".internal",
    ".intranet",
    ".lan",
    ".local",
    ".localhost",
    ".private",
)


class McpRegistryError(Exception):
    """A stable, content-free Official MCP Registry rejection cause."""

    def __init__(self, cause: str) -> None:
        if type(cause) is not str or _CAUSE_RE.fullmatch(cause) is None:
            raise ValueError("plugin_registry_cause_invalid")
        super().__init__(cause)
        self.cause = cause


class _PrivateOrigin(ValueError):
    """A syntactically canonical URL that names an origin Omega will not import."""


class NamespaceKind(StrEnum):
    """Closed set of publisher namespaces the registry can authenticate."""

    GITHUB = "github"
    DNS = "dns"


class RegistryStatus(StrEnum):
    """Closed registry-managed lifecycle state for one published version."""

    ACTIVE = "active"
    DEPRECATED = "deprecated"
    DELETED = "deleted"


class PackageRegistryType(StrEnum):
    """Closed set of public package registries Omega can later pin and verify."""

    NPM = "npm"
    PYPI = "pypi"
    OCI = "oci"


class RemoteTransport(StrEnum):
    """Closed set of remote MCP transports Omega can reach over public HTTPS."""

    STREAMABLE_HTTP = "streamable-http"
    SSE = "sse"


@dataclass(frozen=True, slots=True)
class McpRegistryVariable:
    """One declared header or environment variable name and its disclosure flags.

    Only the name and the publisher's own required/secret claims are preserved.
    Defaults, example values, and descriptions are deliberately dropped: they are
    publisher-authored text with no role in identity, and a default is a common
    place for a credential to be written down by mistake.
    """

    name: str
    is_required: bool
    is_secret: bool


@dataclass(frozen=True, slots=True)
class McpRegistryPackage:
    """One publisher-declared package distribution admitted from the registry.

    Only ``stdio`` packages are admitted. A package that declares an HTTP-family
    transport describes an endpoint, and the registry documents endpoints in
    ``remotes``; admitting it here would create a second, unreviewed path to the
    same connection decision. Argument lists are preserved only as counts,
    because registry argument text is never accepted as a launch command.
    """

    registry_type: PackageRegistryType
    identifier: str
    version: str | None
    registry_base_url: str | None
    runtime_hint: str | None
    file_sha256: str | None
    runtime_argument_count: int
    package_argument_count: int
    environment_variables: tuple[McpRegistryVariable, ...]


@dataclass(frozen=True, slots=True)
class McpRegistryRemote:
    """One publisher-declared remote MCP endpoint admitted from the registry."""

    transport: RemoteTransport
    url: str
    headers: tuple[McpRegistryVariable, ...]


@dataclass(frozen=True, slots=True)
class McpRegistryServer:
    """One registry listing: publisher discovery metadata, never Omega authority."""

    name: str
    namespace: str
    namespace_kind: NamespaceKind
    namespace_owner: str
    server_name: str
    version: str
    description: str | None
    title: str | None
    status: RegistryStatus
    published_at: str
    is_latest: bool
    website_url: str | None
    repository_url: str | None
    packages: tuple[McpRegistryPackage, ...]
    remotes: tuple[McpRegistryRemote, ...]
    unsupported_reasons: tuple[str, ...]
    trust_tier: TrustTier
    extensions_sha256: str | None

    def __post_init__(self) -> None:
        if self.trust_tier is TrustTier.OMEGA_VERIFIED:
            raise McpRegistryError(_RECORD_INVALID)

    @property
    def compatible(self) -> bool:
        """Report whether Omega could attempt an import from this listing.

        One distribution must have survived admission and no disqualifying
        reason may have been recorded, so refusing a publisher's metadata still
        disqualifies the record rather than merely annotating it.
        """
        if not (bool(self.packages) or bool(self.remotes)):
            return False
        return not _DISQUALIFYING_REASONS.intersection(self.unsupported_reasons)


@dataclass(frozen=True, slots=True)
class McpRegistryPage:
    """One validated registry response page and the cursor that follows it."""

    servers: tuple[McpRegistryServer, ...]
    next_cursor: str | None
    count: int


@dataclass(frozen=True, slots=True)
class McpRegistryRequest:
    """The exact bounded query one page fetch may express."""

    query: str | None = None
    cursor: str | None = None
    limit: int = DEFAULT_PAGE_LIMIT

    def __post_init__(self) -> None:
        try:
            if self.query is not None:
                _bounded_pattern(self.query, _QUERY_RE, field="query", limit=MAX_QUERY_CHARS)
            if self.cursor is not None:
                _bounded_pattern(self.cursor, _CURSOR_RE, field="cursor", limit=MAX_CURSOR_CHARS)
            if type(self.limit) is not int or not 1 <= self.limit <= MAX_PAGE_SERVERS:
                raise ValueError("limit is out of bounds")
        except (TypeError, ValueError):
            raise McpRegistryError(_REQUEST_INVALID) from None


class McpRegistryTransport(Protocol):
    """Synchronous bounded page transport supplied by the network-owning layer."""

    def __call__(self, request: McpRegistryRequest) -> bytes: ...


def _bounded_text(value: Any, *, field: str, limit: int) -> str:
    text = _stable_string(value, field=field)
    if len(text) > limit:
        raise ValueError(f"{field} exceeds its bound")
    return text


def _bounded_pattern(value: Any, pattern: re.Pattern[str], *, field: str, limit: int) -> str:
    text = _bounded_text(value, field=field, limit=limit)
    if pattern.fullmatch(text) is None:
        raise ValueError(f"{field} is invalid")
    return text


def _bounded_token(value: Any, *, field: str, limit: int) -> str:
    token = _identifier(value, field=field)
    if len(token) > limit:
        raise ValueError(f"{field} exceeds its bound")
    return token


def _flag(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if type(value) is not bool:
        raise ValueError("registry flag must be a boolean")
    return value


def _closed_object(
    value: Any,
    *,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise ValueError("registry object is malformed")
    keys = set(value)
    if not required <= keys or not keys <= (required | optional):
        raise ValueError("registry object has unaccounted fields")
    return value


def _within_depth(value: Any, budget: int) -> bool:
    if budget <= 0:
        return False
    if type(value) is dict:
        return all(_within_depth(item, budget - 1) for item in value.values())
    if type(value) is list:
        return all(_within_depth(item, budget - 1) for item in value)
    return True


def _within_list_bound(value: Any, limit: int) -> bool:
    return value is None or (type(value) is list and len(value) <= limit)


def _is_ip_literal(hostname: str) -> bool:
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return True


def _require_public_host(hostname: str) -> None:
    """Reject any host Omega will not treat as a public, resolvable origin."""
    labels = hostname.split(".")
    if (
        _is_ip_literal(hostname)
        or hostname in _PRIVATE_HOSTS
        or any(hostname.endswith(suffix) for suffix in _PRIVATE_HOST_SUFFIXES)
        or len(labels) < 2
        or _TLD_RE.fullmatch(labels[-1]) is None
    ):
        raise _PrivateOrigin("registry url names a private origin")
    if _HOST_RE.fullmatch(hostname) is None:
        raise ValueError("registry url host is not canonical")


def _public_https_url(value: Any, *, field: str) -> str:
    """Return one canonical public HTTPS URL, or raise why it was refused.

    An explicit port is refused even when it is the default. Registry discovery
    is third-party input, the default port is the only one a canonical public
    endpoint needs, and a stated port is a strong signal of a private or
    development deployment. Refusal is recorded, never silent, so widening this
    later costs nothing that was dropped.
    """
    url = _bounded_text(value, field=field, limit=MAX_URL_CHARS)
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError:
        raise ValueError(f"{field} has an unreadable authority") from None
    hostname = parsed.hostname
    if hostname is None:
        raise ValueError(f"{field} has no host")
    expected_netloc = f"[{hostname}]" if ":" in hostname else hostname
    # A trailing slash is the vendor's choice of endpoint, not a defect. GitHub
    # serves its MCP endpoint at https://api.githubcopilot.com/mcp/ -- the exact
    # origin Omega ships, installs and connects to -- so refusing it here meant
    # a package Omega already runs could not be imported through its own
    # registry path. Only ONE trailing empty segment is forgiven; an interior
    # "//" is still an empty segment and still fails below.
    raw_segments = parsed.path.split("/")[1:] if parsed.path else []
    if raw_segments and raw_segments[-1] == "":
        raw_segments = raw_segments[:-1]
    segments = tuple(raw_segments)
    if (
        parsed.scheme != "https"
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.netloc != expected_netloc
        or parsed.geturl() != url
        or len(segments) > MAX_URL_SEGMENTS
        or any(_URL_SEGMENT_RE.fullmatch(segment) is None for segment in segments)
        or any(set(segment) == {"."} for segment in segments)
    ):
        raise ValueError(f"{field} is not a canonical public HTTPS URL")
    _require_public_host(hostname)
    return url


def _parse_name(value: Any) -> tuple[str, str, NamespaceKind, str, str]:
    """Return the canonical identity of one listing, or fail closed."""
    name = _bounded_text(value, field="registry server name", limit=MAX_NAME_CHARS)
    segments = name.split("/")
    if len(segments) != 2:
        raise ValueError("registry server name must be namespace/name")
    namespace, server_name = segments
    if (
        _NAMESPACE_RE.fullmatch(namespace) is None
        or _SERVER_SEGMENT_RE.fullmatch(server_name) is None
    ):
        raise ValueError("registry server name is not a registry identity")
    labels = namespace.split(".")
    if namespace.startswith(_GITHUB_NAMESPACE_PREFIX) and len(labels) == 3:
        return name, namespace, NamespaceKind.GITHUB, labels[2], server_name
    return name, namespace, NamespaceKind.DNS, ".".join(reversed(labels)), server_name


def _parse_variable(value: Any) -> McpRegistryVariable:
    field = _closed_object(value, required=_VARIABLE_REQUIRED, optional=_VARIABLE_FIELDS)
    return McpRegistryVariable(
        name=_bounded_pattern(
            field["name"], _VARIABLE_NAME_RE, field="registry variable name", limit=64
        ),
        is_required=_flag(field.get("isRequired"), default=False),
        is_secret=_flag(field.get("isSecret"), default=False),
    )


def _parse_variables(value: Any) -> tuple[McpRegistryVariable, ...]:
    if value is None:
        return ()
    variables = tuple(_parse_variable(item) for item in value)
    names = tuple(variable.name for variable in variables)
    if len(set(names)) != len(names):
        raise ValueError("registry variable names must be unique")
    return variables


def _transport_type(value: Any) -> str:
    field = _closed_object(value, required=_TRANSPORT_REQUIRED, optional=_TRANSPORT_FIELDS)
    kind = field["type"]
    if type(kind) is not str:
        raise ValueError("registry transport type must be a string")
    return kind


def _parse_package(value: Any, reasons: list[str]) -> McpRegistryPackage | None:
    try:
        field = _closed_object(value, required=_PACKAGE_REQUIRED, optional=_PACKAGE_FIELDS)
        transport_type = _transport_type(field["transport"])
    except ValueError:
        reasons.append(PACKAGE_INVALID)
        return None
    registry_type = _member_or_none(field["registryType"], PackageRegistryType)
    if registry_type is None:
        reasons.append(PACKAGE_REGISTRY_UNSUPPORTED)
        return None
    if transport_type != _STDIO_TRANSPORT:
        reasons.append(PACKAGE_TRANSPORT_UNSUPPORTED)
        return None
    if (
        not _within_list_bound(field.get("runtimeArguments"), MAX_ARGUMENTS)
        or not _within_list_bound(field.get("packageArguments"), MAX_ARGUMENTS)
        or not _within_list_bound(field.get("environmentVariables"), MAX_ENVIRONMENT_VARIABLES)
    ):
        reasons.append(PACKAGE_DECLARATION_UNBOUNDED)
        return None
    try:
        return McpRegistryPackage(
            registry_type=registry_type,
            identifier=_bounded_pattern(
                field["identifier"],
                _PACKAGE_IDENTIFIER_RE,
                field="registry package identifier",
                limit=MAX_IDENTIFIER_CHARS,
            ),
            version=_optional(field.get("version"), _package_version),
            registry_base_url=_optional(field.get("registryBaseUrl"), _registry_base_url),
            runtime_hint=_optional(field.get("runtimeHint"), _runtime_hint),
            file_sha256=_optional(field.get("fileSha256"), _file_digest),
            runtime_argument_count=len(field.get("runtimeArguments") or ()),
            package_argument_count=len(field.get("packageArguments") or ()),
            environment_variables=_parse_variables(field.get("environmentVariables")),
        )
    except ValueError:
        reasons.append(PACKAGE_INVALID)
        return None


def _parse_remote(value: Any, reasons: list[str]) -> McpRegistryRemote | None:
    try:
        field = _closed_object(value, required=_REMOTE_REQUIRED, optional=_REMOTE_FIELDS)
    except ValueError:
        reasons.append(REMOTE_INVALID)
        return None
    transport = _member_or_none(field["type"], RemoteTransport)
    if transport is None:
        reasons.append(REMOTE_TRANSPORT_UNSUPPORTED)
        return None
    try:
        url = _public_https_url(field["url"], field="registry remote url")
    except _PrivateOrigin:
        reasons.append(REMOTE_ORIGIN_PRIVATE)
        return None
    except ValueError:
        reasons.append(REMOTE_URL_NONCANONICAL)
        return None
    if not _within_list_bound(field.get("headers"), MAX_HEADERS):
        reasons.append(REMOTE_DECLARATION_UNBOUNDED)
        return None
    try:
        headers = _parse_variables(field.get("headers"))
    except ValueError:
        reasons.append(REMOTE_INVALID)
        return None
    return McpRegistryRemote(transport=transport, url=url, headers=headers)


def _parse_packages(value: Any, reasons: list[str]) -> tuple[McpRegistryPackage, ...]:
    if value is None:
        return ()
    if type(value) is not list:
        reasons.append(METADATA_INVALID)
        return ()
    if len(value) > MAX_PACKAGES:
        reasons.append(DECLARATION_UNBOUNDED)
        return ()
    parsed = tuple(_parse_package(item, reasons) for item in value)
    return tuple(package for package in parsed if package is not None)


def _parse_remotes(value: Any, reasons: list[str]) -> tuple[McpRegistryRemote, ...]:
    if value is None:
        return ()
    if type(value) is not list:
        reasons.append(METADATA_INVALID)
        return ()
    if len(value) > MAX_REMOTES:
        reasons.append(DECLARATION_UNBOUNDED)
        return ()
    parsed = tuple(_parse_remote(item, reasons) for item in value)
    return tuple(remote for remote in parsed if remote is not None)


def _package_version(value: Any) -> str:
    return _bounded_token(value, field="registry package version", limit=MAX_VERSION_CHARS)


def _runtime_hint(value: Any) -> str:
    return _bounded_token(value, field="registry runtime hint", limit=64)


def _file_digest(value: Any) -> str:
    digest = _bounded_text(value, field="registry package digest", limit=64)
    if _DIGEST_RE.fullmatch(digest) is None:
        raise ValueError("registry package digest is invalid")
    return digest


def _registry_base_url(value: Any) -> str:
    return _public_https_url(value, field="registry package base url")


def _optional(value: Any, parser: Any) -> Any:
    return None if value is None else parser(value)


def _member_or_none(value: Any, enum: type[StrEnum]) -> Any:
    if type(value) is not str:
        return None
    try:
        return enum(value)
    except ValueError:
        return None


def _repository_url(value: Any, reasons: list[str]) -> str | None:
    if value is None:
        return None
    try:
        field = _closed_object(
            value, required=_REPOSITORY_REQUIRED, optional=_REPOSITORY_FIELDS
        )
        return _public_https_url(field["url"], field="registry repository url")
    except ValueError:
        reasons.append(URL_NONCANONICAL)
        return None


def _website_url(value: Any, reasons: list[str]) -> str | None:
    if value is None:
        return None
    try:
        return _public_https_url(value, field="registry website url")
    except ValueError:
        reasons.append(URL_NONCANONICAL)
        return None


def _extension_digest(document: dict[str, Any], reasons: list[str]) -> str | None:
    """Return a capped canonical digest over everything Omega does not model."""
    extensions = {key: value for key, value in document.items() if key not in _SERVER_FIELDS}
    if not extensions:
        return None
    try:
        payload = canonical_bytes(extensions)
    except (TypeError, ValueError):
        reasons.append(EXTENSIONS_INVALID)
        return None
    if len(payload) > MAX_EXTENSION_BYTES:
        reasons.append(EXTENSIONS_TOO_LARGE)
        return None
    return sha256_hex(payload)


def _is_semver(version: str) -> bool:
    try:
        _semver(version, field="registry version")
    except ValueError:
        return False
    return True


def _parse_server(
    value: Any,
    *,
    status: RegistryStatus,
    published_at: str,
    is_latest: bool,
) -> McpRegistryServer:
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise ValueError("registry server must be an object")
    if not _SERVER_REQUIRED <= set(value):
        raise ValueError("registry server is missing required identity")
    name, namespace, namespace_kind, namespace_owner, server_name = _parse_name(value["name"])
    reasons: list[str] = []
    version = _bounded_token(value["version"], field="registry version", limit=MAX_VERSION_CHARS)
    if not _is_semver(version):
        reasons.append(VERSION_UNSUPPORTED)
    packages = _parse_packages(value.get("packages"), reasons)
    remotes = _parse_remotes(value.get("remotes"), reasons)
    if value.get("packages") is None and value.get("remotes") is None:
        reasons.append(DISTRIBUTION_ABSENT)
    description = _publisher_text(
        value.get("description"),
        field="registry description",
        limit=MAX_DESCRIPTION_CHARS,
        required=True,
        reasons=reasons,
    )
    title = _publisher_text(
        value.get("title"),
        field="registry title",
        limit=MAX_TITLE_CHARS,
        required=False,
        reasons=reasons,
    )
    website_url = _website_url(value.get("websiteUrl"), reasons)
    repository_url = _repository_url(value.get("repository"), reasons)
    extensions_sha256 = _extension_digest(value, reasons)
    # Every reason-producing step above must run before the tuple is frozen.
    return McpRegistryServer(
        name=name,
        namespace=namespace,
        namespace_kind=namespace_kind,
        namespace_owner=namespace_owner,
        server_name=server_name,
        version=version,
        description=description,
        title=title,
        status=status,
        published_at=published_at,
        is_latest=is_latest,
        website_url=website_url,
        repository_url=repository_url,
        packages=packages,
        remotes=remotes,
        unsupported_reasons=tuple(sorted(set(reasons))),
        trust_tier=TrustTier.PUBLISHER_VERIFIED,
        extensions_sha256=extensions_sha256,
    )


def _publisher_text(
    value: Any,
    *,
    field: str,
    limit: int,
    required: bool,
    reasons: list[str],
) -> str | None:
    """Return admitted publisher display text, or record why it was refused.

    Refused text is dropped rather than repaired: truncating or normalizing it
    would put words in a publisher's mouth. The bound itself is never relaxed --
    the reason is disqualifying, so a listing Omega cannot describe faithfully
    never reads as installable.
    """
    if value is None:
        if required:
            reasons.append(METADATA_INVALID)
        return None
    try:
        return _bounded_text(value, field=field, limit=limit)
    except ValueError:
        reasons.append(METADATA_INVALID)
        return None


def _parse_entry(value: Any) -> McpRegistryServer:
    entry = _closed_object(value, required=_ENTRY_FIELDS)
    meta = _closed_object(entry["_meta"], required=frozenset({_OFFICIAL_META_KEY}))
    official = _closed_object(
        meta[_OFFICIAL_META_KEY], required=_OFFICIAL_REQUIRED, optional=_OFFICIAL_OPTIONAL
    )
    status = _member_or_none(official["status"], RegistryStatus)
    if status is None or type(official["isLatest"]) is not bool:
        raise ValueError("registry managed metadata is invalid")
    return _parse_server(
        entry["server"],
        status=status,
        published_at=_bounded_pattern(
            official["publishedAt"], _TIMESTAMP_RE, field="registry publishedAt", limit=32
        ),
        is_latest=official["isLatest"],
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject an object whose meaning depends on which duplicate key wins."""
    keys = tuple(key for key, _ in pairs)
    if len(set(keys)) != len(keys):
        raise ValueError("registry response repeats an object key")
    return dict(pairs)


def _reject_constant(literal: str) -> Any:
    """Reject `NaN` and `Infinity`, which RFC 8259 does not define."""
    raise ValueError("registry response uses a non-JSON constant")


def _encoded_depth_within(payload: bytes, budget: int) -> bool:
    """Bound structural nesting before any decoder recurses over the body.

    A JSON decoder recurses once per nesting level, so a body nested past the
    interpreter's limit raises `RecursionError` from inside `json.loads`. That
    derives from `RuntimeError`, not `ValueError`, so it would cross this
    module's boundary as a raw interpreter error instead of a stable cause.
    Rejecting the bytes first means the decoder is never asked to go that deep.
    """
    depth = 0
    in_string = False
    escaped = False
    for byte in payload:
        if in_string:
            if escaped:
                escaped = False
            elif byte == _ESCAPE_BYTE:
                escaped = True
            elif byte == _QUOTE_BYTE:
                in_string = False
        elif byte == _QUOTE_BYTE:
            in_string = True
        elif byte in _OPEN_STRUCTURE_BYTES:
            depth += 1
            if depth > budget:
                return False
        elif byte in _CLOSE_STRUCTURE_BYTES:
            depth -= 1
    return True


def _registry_page(payload: bytes) -> McpRegistryPage:
    if not _encoded_depth_within(payload, MAX_DOCUMENT_DEPTH):
        raise ValueError("registry response is nested beyond its bound")
    document = json.loads(
        payload.decode("utf-8"),
        object_pairs_hook=_unique_object,
        parse_constant=_reject_constant,
    )
    if not _within_depth(document, MAX_DOCUMENT_DEPTH):
        raise ValueError("registry response is nested beyond its bound")
    envelope = _closed_object(document, required=_ENVELOPE_FIELDS)
    entries = envelope["servers"]
    if type(entries) is not list or len(entries) > MAX_PAGE_SERVERS:
        raise ValueError("registry servers list is invalid")
    metadata = _closed_object(
        envelope["metadata"], required=_METADATA_REQUIRED, optional=_METADATA_OPTIONAL
    )
    count = metadata["count"]
    if type(count) is not int or count != len(entries):
        raise ValueError("registry count disagrees with the page")
    raw_cursor = metadata.get("nextCursor")
    next_cursor = (
        None
        if raw_cursor is None
        else _bounded_pattern(
            raw_cursor, _CURSOR_RE, field="registry nextCursor", limit=MAX_CURSOR_CHARS
        )
    )
    servers = tuple(_parse_entry(entry) for entry in entries)
    identities = tuple((server.name, server.version) for server in servers)
    if len(set(identities)) != len(identities):
        raise ValueError("registry page repeats one canonical identity")
    return McpRegistryPage(servers=servers, next_cursor=next_cursor, count=count)


class McpRegistryClient:
    """Page the Official MCP Registry without granting it any Omega authority."""

    def __init__(
        self,
        *,
        transport: McpRegistryTransport,
        page_limit: int = DEFAULT_PAGE_LIMIT,
        max_pages: int = MAX_REGISTRY_PAGES,
    ) -> None:
        if (
            not callable(transport)
            or type(page_limit) is not int
            or not 1 <= page_limit <= MAX_PAGE_SERVERS
            or type(max_pages) is not int
            or not 1 <= max_pages <= MAX_REGISTRY_PAGES
        ):
            raise McpRegistryError(_CLIENT_INVALID)
        self._transport = transport
        self._page_limit = page_limit
        self._max_pages = max_pages

    def search(
        self,
        query: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> McpRegistryPage:
        """Return exactly one validated registry page for this bounded request."""
        request = McpRegistryRequest(
            query=query,
            cursor=cursor,
            limit=self._page_limit if limit is None else limit,
        )
        try:
            payload = self._transport(request)
        except McpRegistryError:
            raise
        except Exception:
            raise McpRegistryError(_TRANSPORT_INVALID) from None
        if type(payload) is not bytes:
            raise McpRegistryError(_TRANSPORT_INVALID)
        if len(payload) > MAX_PAGE_BYTES:
            raise McpRegistryError(_PAGE_TOO_LARGE)
        try:
            return _registry_page(payload)
        except (KeyError, RecursionError, TypeError, UnicodeDecodeError, ValueError):
            raise McpRegistryError(_PAGE_INVALID) from None

    def iter_latest(self) -> Iterator[McpRegistryServer]:
        """Yield every current, undeleted listing across a bounded cursor chain."""
        cursor: str | None = None
        seen_cursors: set[str] = set()
        seen_identities: set[tuple[str, str]] = set()
        for _ in range(self._max_pages):
            page = self.search(cursor=cursor)
            for record in page.servers:
                identity = (record.name, record.version)
                if identity in seen_identities:
                    raise McpRegistryError(_PAGE_INVALID)
                seen_identities.add(identity)
                if record.is_latest and record.status is not RegistryStatus.DELETED:
                    yield record
            cursor = page.next_cursor
            if cursor is None:
                return
            if cursor in seen_cursors:
                raise McpRegistryError(_PAGE_INVALID)
            seen_cursors.add(cursor)
        raise McpRegistryError(_TOO_MANY_PAGES)


__all__ = [
    "DECLARATION_UNBOUNDED",
    "DEFAULT_PAGE_LIMIT",
    "DISTRIBUTION_ABSENT",
    "EXTENSIONS_INVALID",
    "EXTENSIONS_TOO_LARGE",
    "METADATA_INVALID",
    "MAX_ARGUMENTS",
    "MAX_ENVIRONMENT_VARIABLES",
    "MAX_EXTENSION_BYTES",
    "MAX_HEADERS",
    "MAX_PACKAGES",
    "MAX_PAGE_BYTES",
    "MAX_PAGE_SERVERS",
    "MAX_REGISTRY_PAGES",
    "MAX_REMOTES",
    "PACKAGE_DECLARATION_UNBOUNDED",
    "PACKAGE_INVALID",
    "PACKAGE_REGISTRY_UNSUPPORTED",
    "PACKAGE_TRANSPORT_UNSUPPORTED",
    "REMOTE_DECLARATION_UNBOUNDED",
    "REMOTE_INVALID",
    "REMOTE_ORIGIN_PRIVATE",
    "REMOTE_TRANSPORT_UNSUPPORTED",
    "REMOTE_URL_NONCANONICAL",
    "URL_NONCANONICAL",
    "VERSION_UNSUPPORTED",
    "McpRegistryClient",
    "McpRegistryError",
    "McpRegistryPackage",
    "McpRegistryPage",
    "McpRegistryRemote",
    "McpRegistryRequest",
    "McpRegistryServer",
    "McpRegistryTransport",
    "McpRegistryVariable",
    "NamespaceKind",
    "PackageRegistryType",
    "RegistryStatus",
    "RemoteTransport",
]
