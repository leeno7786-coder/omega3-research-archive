"""Normalization of one Official MCP Registry listing into an Omega candidate.

An upstream ``server.json`` is discovery metadata. It authenticates a publisher
namespace; it does not endorse behavior, pin artifacts, or grant capabilities.
This module turns exactly one such listing into a provider-neutral manifest-v2
candidate, and it does so by refusing everything it cannot state faithfully.

Three refusals are deliberate and shape the whole module:

* **Nothing is repaired.** A listing discovery already recorded non-support for,
  an endpoint whose exact form the manifest cannot express, a declared variable
  outside Omega's field grammar, or a name outside the plugin-id grammar is an
  import failure. Trimming a path, renaming a variable, or truncating a title
  would put words in a publisher's mouth and hand the user a plugin that is not
  the one the publisher published.
* **Nothing is inferred.** A missing description or title is a failure rather
  than a default; declared headers become labeled secret fields rather than a
  guessed authentication scheme; a package without an artifact digest cannot be
  imported at all, because its bytes could never be re-proved.
* **Nothing generated here is Omega's own.** A candidate carries the publisher's
  namespace as its publisher identity and ``TrustTier.PUBLISHER_VERIFIED`` as its
  tier -- never Omega's first-party identity and never ``OMEGA_VERIFIED``. Its
  manifest is explicitly unsealed: the signing identity is a sentinel, so the
  package verifier rejects it until a later local sealing step signs it.

``NormalizedPluginCandidate`` is a proposal, not a package. Only
``VerifiedNormalizedPluginCandidate`` -- constructible only by ``verify`` after
the source evidence re-derives the manifest identity and any stored artifact is
re-read and re-hashed -- is admissible to the installer.

Two boundaries are worth stating explicitly. The registry's public-host check is
syntactic: it does not, and cannot, defend against a wildcard-DNS name that
resolves to a private address. This module therefore records the exact endpoint
and nothing more; re-checking where a name actually resolves at the moment a
connection is opened belongs to the connection layer, not to import. And the
plugin foundation performs no network I/O, so artifact bytes arrive as an
injected iterable of chunks from the layer that owns the network.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

from mcp_platform.canonical import canonical_bytes, sha256_hex

from .artifact_store import (
    MAX_ARTIFACT_BYTES,
    ArtifactStore,
    ArtifactStoreError,
    ArtifactStreamError,
    StoredArtifact,
)
from .catalog import DistributionKind, TrustTier
from .contracts import (
    PLUGIN_PROTOCOL_VERSION,
    AppDeployment,
    McpTransport,
    PluginKind,
    PluginManifest,
    _https_issuer,
    _https_origin,
    _identifier,
    _semver,
)
from .mcp_registry import (
    McpRegistryPackage,
    McpRegistryRemote,
    McpRegistryServer,
    McpRegistryVariable,
    NamespaceKind,
    PackageRegistryType,
    RemoteTransport,
)

DEFAULT_MAX_ARTIFACT_BYTES = MAX_ARTIFACT_BYTES
IMPORT_MINIMUM_OMEGA_VERSION = "3.0.0"

# A generated candidate is deliberately unsignable until a later local sealing
# step signs it: no trusted key ring holds this identity, so the package
# verifier refuses an unsealed candidate rather than installing one.
UNSEALED_SIGNING_KEY_ID = "omega.unsealed"
UNSEALED_SIGNATURE = "unsealed"

# Each admitted package registry maps to exactly one Omega-owned launcher.
# Publisher launch text is never accepted as a command; a hint that does not
# name this registry's launcher is refused rather than reinterpreted.
_RUNTIME_BY_REGISTRY = {
    PackageRegistryType.NPM: "npx",
    PackageRegistryType.PYPI: "uvx",
}

_ARTIFACT_EVIDENCE_ABSENT = "plugin_import_artifact_evidence_absent"
_ARTIFACT_INVALID = "plugin_import_artifact_invalid"
_ARTIFACT_UNAVAILABLE = "plugin_import_artifact_unavailable"
_CANDIDATE_UNVERIFIED = "plugin_import_candidate_unverified"
_DISTRIBUTION_AMBIGUOUS = "plugin_import_distribution_ambiguous"
_ENDPOINT_UNSUPPORTED = "plugin_import_endpoint_unsupported"
_IDENTITY_UNSUPPORTED = "plugin_import_identity_unsupported"
_MANIFEST_INVALID = "plugin_import_manifest_invalid"
_METADATA_ABSENT = "plugin_import_metadata_absent"
_PACKAGE_IDENTITY_MISMATCH = "plugin_import_package_identity_mismatch"
_PACKAGE_LINKAGE_MISMATCH = "plugin_import_package_linkage_mismatch"
_REQUEST_INVALID = "plugin_import_request_invalid"
_RUNTIME_UNSUPPORTED = "plugin_import_runtime_unsupported"
_SECRET_FIELD_UNSUPPORTED = "plugin_import_secret_field_unsupported"
_SOURCE_UNSUPPORTED = "plugin_import_source_unsupported"
_TRANSPORT_UNSUPPORTED = "plugin_import_transport_unsupported"
_VERSION_MISMATCH = "plugin_import_version_mismatch"

_IDENTIFIER_DELIMITERS = re.compile(r"[@/._+:-]+")
_VERIFICATION_SEAL: Any = object()


class PluginImportError(Exception):
    """A stable, content-free marketplace import rejection cause."""


class ArtifactSource(Protocol):
    """Synchronous bounded artifact transport supplied by the network-owning layer."""

    def __call__(self, request: ArtifactRequest) -> Iterable[bytes]: ...


@dataclass(frozen=True, slots=True)
class ArtifactRequest:
    """The exact bounded artifact one package distribution asks a source to deliver."""

    registry_type: PackageRegistryType
    identifier: str
    version: str
    registry_base_url: str | None
    sha256: str
    max_bytes: int


@dataclass(frozen=True, slots=True)
class RegistrySourceEvidence:
    """The registry facts one import relied on, kept so a later audit can re-check."""

    registry_name: str
    namespace_kind: NamespaceKind
    version: str
    metadata_sha256: str
    artifact_sha256: str | None


@dataclass(frozen=True, slots=True)
class NormalizedPluginCandidate:
    """One import proposal: a manifest, its evidence, and what it would pin.

    This type is deliberately not installable. It records what an import would
    produce; ``MarketplaceImporter.verify`` is the only path from here to
    something the installer will accept.
    """

    manifest: PluginManifest
    trust_tier: TrustTier
    distribution: DistributionKind
    source_evidence: RegistrySourceEvidence
    artifact: StoredArtifact | None


class VerifiedNormalizedPluginCandidate:
    """Marker for a candidate whose evidence and artifact digests re-verified.

    The constructor demands a seal held only by this module, so the type states
    a fact the normalizer established rather than an intention a caller declared.
    """

    __slots__ = ("candidate",)

    def __init__(self, candidate: NormalizedPluginCandidate, seal: object) -> None:
        if seal is not _VERIFICATION_SEAL or type(candidate) is not NormalizedPluginCandidate:
            raise PluginImportError(_CANDIDATE_UNVERIFIED)
        object.__setattr__(self, "candidate", candidate)

    def __setattr__(self, name: str, value: object) -> None:
        raise PluginImportError(_CANDIDATE_UNVERIFIED)

    @property
    def manifest(self) -> PluginManifest:
        return self.candidate.manifest

    @property
    def trust_tier(self) -> TrustTier:
        return self.candidate.trust_tier


class MarketplaceImporter:
    """Normalize registry discovery into candidates Omega could later install."""

    def __init__(
        self,
        *,
        artifact_store: ArtifactStore | None = None,
        artifact_source: ArtifactSource | None = None,
        max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
    ) -> None:
        if artifact_store is not None and type(artifact_store) is not ArtifactStore:
            raise PluginImportError(_REQUEST_INVALID)
        if artifact_source is not None and not callable(artifact_source):
            raise PluginImportError(_REQUEST_INVALID)
        if (
            type(max_artifact_bytes) is not int
            or not 1 <= max_artifact_bytes <= MAX_ARTIFACT_BYTES
        ):
            raise PluginImportError(_REQUEST_INVALID)
        self._store = artifact_store
        self._source = artifact_source
        self._max_artifact_bytes = max_artifact_bytes

    def normalize(self, server: object, version: object) -> NormalizedPluginCandidate:
        """Turn one registry listing at one resolved version into an import proposal."""
        if type(server) is not McpRegistryServer:
            raise PluginImportError(_REQUEST_INVALID)
        # Discovery's own refusals come first: a listing Omega already declined to
        # describe in full is not a listing it may install part of.
        if server.unsupported_reasons or not server.compatible:
            raise PluginImportError(_SOURCE_UNSUPPORTED)
        resolved = _resolved_version(server, version)
        title, description = _publisher_text(server)
        plugin_id, publisher_id = _plugin_identity(server)
        remote, package = _one_distribution(server)

        artifact: StoredArtifact | None = None
        if remote is not None:
            distribution = DistributionKind.REMOTE_MCP
            requirement = _remote_requirement(plugin_id, publisher_id, remote)
        elif package is not None:
            distribution = DistributionKind.REMOTE_PACKAGE
            artifact = self._pin_artifact(server, package, resolved)
            requirement = _local_requirement(plugin_id, publisher_id, package)
        else:
            raise PluginImportError(_DISTRIBUTION_AMBIGUOUS)

        manifest = _manifest(
            plugin_id=plugin_id,
            publisher_id=publisher_id,
            version=resolved,
            title=title,
            description=description,
            requirement=requirement,
        )
        return NormalizedPluginCandidate(
            manifest=manifest,
            trust_tier=TrustTier.PUBLISHER_VERIFIED,
            distribution=distribution,
            source_evidence=RegistrySourceEvidence(
                registry_name=server.name,
                namespace_kind=server.namespace_kind,
                version=resolved,
                metadata_sha256=_registry_metadata_digest(server),
                artifact_sha256=None if artifact is None else artifact.sha256,
            ),
            artifact=artifact,
        )

    def verify(self, candidate: object) -> VerifiedNormalizedPluginCandidate:
        """Re-prove a candidate's evidence and artifact before it becomes installable."""
        if type(candidate) is not NormalizedPluginCandidate:
            raise PluginImportError(_CANDIDATE_UNVERIFIED)
        if (
            candidate.trust_tier is not TrustTier.PUBLISHER_VERIFIED
            or type(candidate.manifest) is not PluginManifest
            or type(candidate.source_evidence) is not RegistrySourceEvidence
            or type(candidate.distribution) is not DistributionKind
        ):
            raise PluginImportError(_CANDIDATE_UNVERIFIED)
        evidence = candidate.source_evidence
        manifest = candidate.manifest
        try:
            plugin_id, publisher_id = _identity_from_name(evidence.registry_name)
        except PluginImportError:
            raise PluginImportError(_CANDIDATE_UNVERIFIED) from None
        if (
            manifest.plugin_id != plugin_id
            or manifest.publisher_id != publisher_id
            or manifest.plugin_version != evidence.version
            or manifest.signing_key_id != UNSEALED_SIGNING_KEY_ID
        ):
            raise PluginImportError(_CANDIDATE_UNVERIFIED)
        self._verify_artifact(candidate)
        return VerifiedNormalizedPluginCandidate(candidate, _VERIFICATION_SEAL)

    def _verify_artifact(self, candidate: NormalizedPluginCandidate) -> None:
        """Re-read any pinned artifact so a recorded digest is never taken on trust."""
        expected = candidate.source_evidence.artifact_sha256
        artifact = candidate.artifact
        if expected is None:
            if artifact is not None:
                raise PluginImportError(_CANDIDATE_UNVERIFIED)
            return
        if (
            type(artifact) is not StoredArtifact
            or artifact.sha256 != expected
            or self._store is None
        ):
            raise PluginImportError(_CANDIDATE_UNVERIFIED)
        try:
            self._store.verify(artifact)
        except ArtifactStoreError:
            raise PluginImportError(_CANDIDATE_UNVERIFIED) from None

    def _pin_artifact(
        self,
        server: McpRegistryServer,
        package: McpRegistryPackage,
        version: str,
    ) -> StoredArtifact:
        """Store the declared artifact by digest before any manifest describes it."""
        if package.version != version:
            raise PluginImportError(_PACKAGE_IDENTITY_MISMATCH)
        # A registry Omega owns no launcher for has no runtime to agree with, so
        # the absent expectation is tested on its own: comparing it directly to an
        # equally absent hint would let such a package through as a match.
        expected_runtime = _RUNTIME_BY_REGISTRY.get(package.registry_type)
        if expected_runtime is None or expected_runtime != package.runtime_hint:
            raise PluginImportError(_RUNTIME_UNSUPPORTED)
        if not _namespace_covers(server.namespace_owner, package.identifier):
            raise PluginImportError(_PACKAGE_LINKAGE_MISMATCH)
        if package.file_sha256 is None:
            raise PluginImportError(_ARTIFACT_EVIDENCE_ABSENT)
        if self._store is None or self._source is None:
            raise PluginImportError(_ARTIFACT_UNAVAILABLE)
        request = ArtifactRequest(
            registry_type=package.registry_type,
            identifier=package.identifier,
            version=version,
            registry_base_url=package.registry_base_url,
            sha256=package.file_sha256,
            max_bytes=self._max_artifact_bytes,
        )
        try:
            stream = self._source(request)
        except Exception:
            raise PluginImportError(_ARTIFACT_UNAVAILABLE) from None
        try:
            staged = self._store.stage(stream, request.sha256, request.max_bytes)
            return self._store.promote(staged)
        except ArtifactStreamError:
            # No artifact arrived; that is a delivery failure, not a verdict that
            # the publisher's declared artifact is wrong.
            raise PluginImportError(_ARTIFACT_UNAVAILABLE) from None
        except ArtifactStoreError:
            raise PluginImportError(_ARTIFACT_INVALID) from None


def _resolved_version(server: McpRegistryServer, version: object) -> str:
    """Return the one immutable version this import pins, or refuse to guess it."""
    if type(version) is not str or version != server.version:
        raise PluginImportError(_VERSION_MISMATCH)
    try:
        return _semver(version, field="import version")
    except ValueError:
        raise PluginImportError(_VERSION_MISMATCH) from None


def _publisher_text(server: McpRegistryServer) -> tuple[str, str]:
    """Return the publisher's own title and description, or refuse to invent them."""
    if server.title is None or server.description is None:
        raise PluginImportError(_METADATA_ABSENT)
    return server.title, server.description


def _plugin_identity(server: McpRegistryServer) -> tuple[str, str]:
    return _identity_from_name(server.name)


def _identity_from_name(name: object) -> tuple[str, str]:
    """Map one registry name onto exactly one plugin identity, or fail closed.

    ``namespace/server`` becomes ``namespace.server``. The server segment may not
    itself contain a dot: without that rule ``a.b/c.d`` and ``a.b.c/d`` would
    both land on ``a.b.c.d``, and one plugin identity would name two distinct
    publishers' listings. That dot rule is the only spelling this mapping
    refuses among names the registry parser admits -- every other character a
    registry name may carry is one the plugin-id grammar also carries, so an
    admitted name keeps its exact spelling rather than being normalized. Both
    segments are still validated independently: this function is also reached
    from ``verify`` with evidence that never passed the registry parser, and a
    name it cannot map is refused rather than rewritten.
    """
    if type(name) is not str:
        raise PluginImportError(_IDENTITY_UNSUPPORTED)
    segments = name.split("/")
    if len(segments) != 2:
        raise PluginImportError(_IDENTITY_UNSUPPORTED)
    namespace, server_name = segments
    if "." in server_name:
        raise PluginImportError(_IDENTITY_UNSUPPORTED)
    try:
        _identifier(server_name, field="registry server name")
        return (
            _identifier(f"{namespace}.{server_name}", field="plugin_id"),
            _identifier(namespace, field="publisher_id"),
        )
    except ValueError:
        raise PluginImportError(_IDENTITY_UNSUPPORTED) from None


def _one_distribution(
    server: McpRegistryServer,
) -> tuple[McpRegistryRemote | None, McpRegistryPackage | None]:
    """Choose the one distribution to import, preferring the one that runs nothing.

    A publisher offering both an endpoint and a package is offering two different
    risk profiles for one listing. Omega takes the endpoint: a remote connection
    introduces no local execution. Ambiguity within a kind is a refusal, because
    silently taking the first of several would hide which one a user approved.
    """
    if server.remotes:
        if len(server.remotes) != 1:
            raise PluginImportError(_DISTRIBUTION_AMBIGUOUS)
        return server.remotes[0], None
    if len(server.packages) != 1:
        raise PluginImportError(_DISTRIBUTION_AMBIGUOUS)
    return None, server.packages[0]


def _remote_requirement(
    plugin_id: str, publisher_id: str, remote: McpRegistryRemote
) -> dict[str, object]:
    """Describe one remote endpoint exactly as the publisher stated it."""
    if remote.transport is not RemoteTransport.STREAMABLE_HTTP:
        raise PluginImportError(_TRANSPORT_UNSUPPORTED)
    try:
        origin = _https_origin(remote.url, field="import remote origin")
    except ValueError:
        raise PluginImportError(_ENDPOINT_UNSUPPORTED) from None
    return _requirement(
        plugin_id=plugin_id,
        publisher_id=publisher_id,
        deployment=AppDeployment.REMOTE,
        transports=(McpTransport.STREAMABLE_HTTP,),
        remote_origin=origin,
        authorization_issuer=_endpoint_issuer(origin),
        variables=remote.headers,
    )


def _local_requirement(
    plugin_id: str, publisher_id: str, package: McpRegistryPackage
) -> dict[str, object]:
    """Describe one pinned stdio package as a local app requirement."""
    return _requirement(
        plugin_id=plugin_id,
        publisher_id=publisher_id,
        deployment=AppDeployment.LOCAL,
        transports=(McpTransport.STDIO,),
        remote_origin=None,
        authorization_issuer=None,
        variables=package.environment_variables,
    )


def _requirement(
    *,
    plugin_id: str,
    publisher_id: str,
    deployment: AppDeployment,
    transports: tuple[McpTransport, ...],
    remote_origin: str | None,
    authorization_issuer: str | None,
    variables: tuple[McpRegistryVariable, ...],
) -> dict[str, object]:
    return {
        "app_id": plugin_id,
        "publisher_id": publisher_id,
        "deployment": deployment.value,
        "provider_id": None,
        "protocol_version": PLUGIN_PROTOCOL_VERSION,
        "transports": [item.value for item in transports],
        "remote_origin": remote_origin,
        "authorization_issuer": authorization_issuer,
        "authentication": _authentication(variables),
        "capabilities": [],
    }


def _endpoint_issuer(origin: str) -> str:
    """Return the endpoint's own origin as its authorization issuer.

    The registry states no issuer. Naming the endpoint's own origin adds no
    authority the listing did not already carry -- it points at the same host the
    user is being asked to approve -- and it never invents a third party. Which
    issuer actually protects that resource is discovered at connection time by
    the connection layer, which is also where a name's resolution is re-checked.
    """
    parsed = urlsplit(origin)
    try:
        return _https_issuer(f"{parsed.scheme}://{parsed.netloc}", field="import issuer")
    except ValueError:
        raise PluginImportError(_ENDPOINT_UNSUPPORTED) from None


def _authentication(variables: tuple[McpRegistryVariable, ...]) -> dict[str, object]:
    """Describe declared variables as labeled fields, never as a guessed scheme."""
    if not variables:
        return {"kind": "none", "scopes": [], "secret_fields": []}
    fields: list[dict[str, object]] = []
    for variable in variables:
        try:
            field_id = _identifier(variable.name, field="import secret field_id")
        except ValueError:
            raise PluginImportError(_SECRET_FIELD_UNSUPPORTED) from None
        fields.append(
            {
                "field_id": field_id,
                "label": variable.name,
                "secret": variable.is_secret,
                "required": variable.is_required,
            }
        )
    return {"kind": "declared_secret_fields", "scopes": [], "secret_fields": fields}


def _namespace_covers(namespace_owner: str, identifier: str) -> bool:
    """Report whether the authenticated namespace owner names this package.

    The owner's labels must appear as one contiguous run of the identifier's own
    delimiter-separated tokens. The rule is provider-neutral and deliberately
    conservative: an identifier the authenticated namespace does not visibly
    cover is refused, because the registry authenticates namespaces, not the
    package registries those namespaces point at.
    """
    owner = tuple(part.casefold() for part in namespace_owner.split(".") if part)
    tokens = tuple(
        part.casefold() for part in _IDENTIFIER_DELIMITERS.split(identifier) if part
    )
    if not owner or len(owner) > len(tokens):
        return False
    return any(
        tokens[index : index + len(owner)] == owner
        for index in range(len(tokens) - len(owner) + 1)
    )


def _registry_metadata_digest(server: McpRegistryServer) -> str:
    """Digest exactly the lifecycle facts the registry itself controls."""
    return sha256_hex(
        canonical_bytes(
            {
                "is_latest": server.is_latest,
                "published_at": server.published_at,
                "status": server.status.value,
            }
        )
    )


def _manifest(
    *,
    plugin_id: str,
    publisher_id: str,
    version: str,
    title: str,
    description: str,
    requirement: dict[str, object],
) -> PluginManifest:
    """Build the unsealed manifest-v2 document this listing normalizes to."""
    try:
        return PluginManifest.from_dict(
            {
                "manifest_version": 2,
                "plugin_id": plugin_id,
                "publisher_id": publisher_id,
                "plugin_version": version,
                "minimum_omega_version": IMPORT_MINIMUM_OMEGA_VERSION,
                "kind": PluginKind.MCP_BACKED.value,
                "title": title,
                "description": description,
                "assets": [],
                "entry_points": [],
                "starter_requests": [],
                "required_apps": [requirement],
                "optional_apps": [],
                "schedule_template_paths": [],
                "proactive_event_rules": [],
                "enterprise_roles": [],
                "signing_key_id": UNSEALED_SIGNING_KEY_ID,
                "signature": UNSEALED_SIGNATURE,
            }
        )
    except (TypeError, ValueError):
        raise PluginImportError(_MANIFEST_INVALID) from None


__all__ = [
    "DEFAULT_MAX_ARTIFACT_BYTES",
    "IMPORT_MINIMUM_OMEGA_VERSION",
    "UNSEALED_SIGNATURE",
    "UNSEALED_SIGNING_KEY_ID",
    "ArtifactRequest",
    "ArtifactSource",
    "MarketplaceImporter",
    "NormalizedPluginCandidate",
    "PluginImportError",
    "RegistrySourceEvidence",
    "VerifiedNormalizedPluginCandidate",
]
