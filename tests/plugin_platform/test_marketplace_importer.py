"""Provider-neutral normalization of registry discovery into Omega candidates."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from mcp_platform.canonical import sha256_hex
from plugin_platform.artifact_store import ArtifactStore
from plugin_platform.catalog import DistributionKind, TrustTier
from plugin_platform.contracts import (
    PLUGIN_PROTOCOL_VERSION,
    AppDeployment,
    McpTransport,
    PluginAuthenticationKind,
    PluginKind,
)
from plugin_platform.marketplace_importer import (
    DEFAULT_MAX_ARTIFACT_BYTES,
    UNSEALED_SIGNATURE,
    UNSEALED_SIGNING_KEY_ID,
    ArtifactRequest,
    MarketplaceImporter,
    NormalizedPluginCandidate,
    PluginImportError,
    VerifiedNormalizedPluginCandidate,
)
from plugin_platform.mcp_registry import (
    METADATA_INVALID,
    URL_NONCANONICAL,
    McpRegistryClient,
    McpRegistryServer,
    McpRegistryVariable,
    NamespaceKind,
    PackageRegistryType,
)

_ENDPOINT = "https://mcp.weather.example.com/mcp/v1"
_ARTIFACT = b"weather-mcp-package-bytes"
_ARTIFACT_DIGEST = sha256_hex(_ARTIFACT)


def _remote(**overrides: Any) -> dict[str, Any]:
    """Return one publisher-declared remote endpoint declaration."""
    return {
        "type": "streamable-http",
        "url": _ENDPOINT,
        "headers": [{"name": "X-Api-Key", "isRequired": True, "isSecret": True}],
        **overrides,
    }


def _package(**overrides: Any) -> dict[str, Any]:
    """Return one publisher-declared stdio package declaration."""
    return {
        "registryType": "npm",
        "registryBaseUrl": "https://registry.npmjs.org",
        "identifier": "@example/weather-mcp",
        "version": "1.2.0",
        "runtimeHint": "npx",
        "fileSha256": _ARTIFACT_DIGEST,
        "transport": {"type": "stdio"},
        **overrides,
    }


def _record(**overrides: Any) -> McpRegistryServer:
    """Return one registry record parsed by the real bounded registry parser."""
    server: dict[str, Any] = {
        "name": "io.github.example/weather",
        "title": "Weather",
        "description": "Weather forecasts and severe weather alerts.",
        "version": "1.2.0",
        "remotes": [_remote()],
        **overrides,
    }
    document = {
        "servers": [
            {
                "server": server,
                "_meta": {
                    "io.modelcontextprotocol.registry/official": {
                        "status": "active",
                        "publishedAt": "2026-08-01T10:30:00Z",
                        "isLatest": True,
                    }
                },
            }
        ],
        "metadata": {"count": 1},
    }
    payload = json.dumps(document).encode("utf-8")
    page = McpRegistryClient(transport=lambda _request: payload).search()
    assert len(page.servers) == 1
    return page.servers[0]


def registry_weather_server() -> McpRegistryServer:
    """Return the pinned remote weather listing every example test normalizes."""
    return _record()


def _package_record(**package_overrides: Any) -> McpRegistryServer:
    """Return a listing whose only distribution is one stdio package."""
    return _record(packages=[_package(**package_overrides)], remotes=[])


def _artifact_source(
    payload: bytes = _ARTIFACT, requests: list[ArtifactRequest] | None = None
):
    """Return a source handing back exactly these bytes for any pinned request."""

    def source(request: ArtifactRequest) -> list[bytes]:
        if requests is not None:
            requests.append(request)
        return [payload[:4], payload[4:]]

    return source


def importer(tmp_path: Path | None = None, **overrides: Any) -> MarketplaceImporter:
    """Return an importer with no network reach and an optional artifact store."""
    if tmp_path is not None:
        overrides.setdefault("artifact_store", ArtifactStore(tmp_path))
        overrides.setdefault("artifact_source", _artifact_source())
    return MarketplaceImporter(**overrides)


def _cause(exc_info: pytest.ExceptionInfo[PluginImportError]) -> str:
    return str(exc_info.value)


def test_registry_remote_normalizes_to_provider_neutral_manifest_v2() -> None:
    """Catches an import that fabricates or drops the publisher's own identity."""
    candidate = importer().normalize(registry_weather_server(), "1.2.0")
    assert candidate.manifest.manifest_version == 2
    assert candidate.manifest.plugin_id == "io.github.example.weather"
    assert candidate.trust_tier is TrustTier.PUBLISHER_VERIFIED
    assert candidate.manifest.required_apps[0].deployment.value == "remote"


def test_remote_candidate_records_the_exact_published_endpoint() -> None:
    """Catches rewriting, defaulting, or widening the endpoint a publisher declared."""
    candidate = importer().normalize(registry_weather_server(), "1.2.0")
    requirement = candidate.manifest.required_apps[0]

    assert requirement.remote_origin == _ENDPOINT
    assert requirement.transports == (McpTransport.STREAMABLE_HTTP,)
    assert requirement.protocol_version == PLUGIN_PROTOCOL_VERSION
    assert requirement.authorization_issuer == "https://mcp.weather.example.com"
    assert candidate.distribution is DistributionKind.REMOTE_MCP
    assert candidate.artifact is None
    assert candidate.manifest.kind is PluginKind.MCP_BACKED


def test_generated_candidate_never_claims_omega_publisher_or_verification() -> None:
    """Catches a generated package promoting itself into Omega's own trust lane."""
    candidate = importer().normalize(registry_weather_server(), "1.2.0")

    assert candidate.manifest.publisher_id == "io.github.example"
    assert candidate.manifest.publisher_id != "omega.first_party"
    assert candidate.trust_tier is not TrustTier.OMEGA_VERIFIED
    assert candidate.manifest.signing_key_id == UNSEALED_SIGNING_KEY_ID
    assert candidate.manifest.signature == UNSEALED_SIGNATURE


def test_source_evidence_pins_the_registry_facts_the_import_relied_on() -> None:
    """Catches an import that keeps no proof of which listing authorized it."""
    record = registry_weather_server()
    evidence = importer().normalize(record, "1.2.0").source_evidence

    assert evidence.registry_name == "io.github.example/weather"
    assert evidence.namespace_kind is NamespaceKind.GITHUB
    assert evidence.version == "1.2.0"
    assert len(evidence.metadata_sha256) == 64
    assert evidence.artifact_sha256 is None

    superseded = importer().normalize(replace(record, is_latest=False), "1.2.0")
    assert superseded.source_evidence.metadata_sha256 != evidence.metadata_sha256


def test_declared_headers_become_labeled_secret_fields_without_invention() -> None:
    """Catches inferring an authentication scheme the publisher never declared."""
    candidate = importer().normalize(registry_weather_server(), "1.2.0")
    authentication = candidate.manifest.required_apps[0].authentication

    assert authentication.kind is PluginAuthenticationKind.DECLARED_SECRET_FIELDS
    assert authentication.scopes == ()
    assert len(authentication.secret_fields) == 1
    field = authentication.secret_fields[0]
    assert (field.field_id, field.label, field.secret, field.required) == (
        "X-Api-Key",
        "X-Api-Key",
        True,
        True,
    )


def test_an_endpoint_without_headers_declares_no_authentication() -> None:
    """Catches manufacturing a credential prompt for an open endpoint."""
    candidate = importer().normalize(_record(remotes=[_remote(headers=[])]), "1.2.0")
    authentication = candidate.manifest.required_apps[0].authentication

    assert authentication.kind is PluginAuthenticationKind.NONE
    assert authentication.secret_fields == ()


def test_a_declared_variable_outside_the_field_grammar_fails_closed() -> None:
    """Catches renaming a publisher's declared variable to force it through."""
    record = _record()
    forged = replace(
        record,
        remotes=(
            replace(
                record.remotes[0],
                headers=(
                    McpRegistryVariable(name="Api Key", is_required=True, is_secret=True),
                ),
            ),
        ),
    )

    with pytest.raises(PluginImportError) as exc_info:
        importer().normalize(forged, "1.2.0")

    assert _cause(exc_info) == "plugin_import_secret_field_unsupported"


def test_declared_environment_variables_keep_their_exact_published_names(
    tmp_path: Path,
) -> None:
    """Catches rewriting a package's declared environment variable names."""
    record = _package_record(
        environmentVariables=[
            {"name": "WEATHER_API_KEY", "isRequired": True, "isSecret": True},
            {"name": "WEATHER_UNITS"},
        ]
    )
    candidate = importer(tmp_path).normalize(record, "1.2.0")
    authentication = candidate.manifest.required_apps[0].authentication

    assert authentication.kind is PluginAuthenticationKind.DECLARED_SECRET_FIELDS
    assert tuple(
        (item.field_id, item.label, item.secret, item.required)
        for item in authentication.secret_fields
    ) == (
        ("WEATHER_API_KEY", "WEATHER_API_KEY", True, True),
        ("WEATHER_UNITS", "WEATHER_UNITS", False, False),
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://mcp.weather.example.com/mcp?tenant=1",
        "https://mcp.weather.example.com/mcp#fragment",
        "https://mcp.weather.example.com/../mcp",
        "https://mcp.weather.example.com/./mcp",
        "https://mcp.weather.example.com//mcp",
        "http://mcp.weather.example.com/mcp",
    ],
)
def test_an_endpoint_omega_cannot_state_exactly_fails_closed(url: str) -> None:
    """Catches trimming a publisher endpoint into a shape the manifest accepts.

    A vendor path Omega can state exactly is admitted verbatim; anything it
    cannot is refused by whichever layer sees it first -- the registry record
    validator or the manifest origin grammar -- and never silently rewritten.
    """
    with pytest.raises(PluginImportError) as exc_info:
        importer().normalize(_record(remotes=[_remote(url=url)]), "1.2.0")

    assert _cause(exc_info) in {
        "plugin_import_endpoint_unsupported",
        "plugin_import_source_unsupported",
    }


@pytest.mark.parametrize(
    "url",
    [
        "https://mcp.weather.example.com/mcp",
        "https://mcp.weather.example.com/a/b/c",
        "https://mcp.weather.example.com/mcp/",
    ],
)
def test_a_canonical_vendor_path_is_stated_exactly_rather_than_rejected(url: str) -> None:
    """Vendors choose their own endpoint path; Slack serves MCP at /mcp.

    Catches a regression to the three hardcoded paths Omega happened to ship
    first, which would make most real registry servers unimportable.
    """
    candidate = importer().normalize(_record(remotes=[_remote(url=url)]), "1.2.0")

    assert candidate.manifest.required_apps[0].remote_origin == url


def test_a_transport_omega_does_not_speak_fails_closed() -> None:
    """Catches importing an SSE endpoint as if it were streamable HTTP."""
    record = _record(remotes=[{"type": "sse", "url": _ENDPOINT}])

    with pytest.raises(PluginImportError) as exc_info:
        importer().normalize(record, "1.2.0")

    assert _cause(exc_info) == "plugin_import_transport_unsupported"


def test_more_than_one_endpoint_is_ambiguous_rather_than_silently_first() -> None:
    """Catches picking one of several distributions without saying which or why."""
    record = _record(
        remotes=[_remote(), _remote(url="https://alt.weather.example.com/mcp/v1")]
    )

    with pytest.raises(PluginImportError) as exc_info:
        importer().normalize(record, "1.2.0")

    assert _cause(exc_info) == "plugin_import_distribution_ambiguous"


def test_an_endpoint_is_preferred_over_a_package_that_would_run_locally(
    tmp_path: Path,
) -> None:
    """Catches importing local execution when a publisher also offers an endpoint."""
    record = _record(packages=[_package()], remotes=[_remote()])
    candidate = importer(tmp_path).normalize(record, "1.2.0")

    assert candidate.distribution is DistributionKind.REMOTE_MCP
    assert candidate.artifact is None
    assert candidate.manifest.required_apps[0].deployment is AppDeployment.REMOTE


@pytest.mark.parametrize(
    "reason_overrides",
    [{"websiteUrl": "http://weather.example.com"}, {"version": "1.2"}],
)
def test_a_listing_with_any_recorded_non_support_cannot_normalize(
    reason_overrides: dict[str, Any],
) -> None:
    """Catches importing a listing discovery already refused part of."""
    record = _record(**reason_overrides)
    assert record.unsupported_reasons

    with pytest.raises(PluginImportError) as exc_info:
        importer().normalize(record, record.version)

    assert _cause(exc_info) == "plugin_import_source_unsupported"


def test_a_recorded_url_refusal_is_not_a_partial_success() -> None:
    """Catches treating a non-canonical publisher URL as a droppable annotation."""
    record = _record(websiteUrl="http://weather.example.com")
    assert URL_NONCANONICAL in record.unsupported_reasons
    assert record.compatible

    with pytest.raises(PluginImportError):
        importer().normalize(record, "1.2.0")


def test_an_incompatible_listing_cannot_normalize() -> None:
    """Catches importing a record that declares no admitted distribution at all."""
    record = _record(packages=[], remotes=[])
    assert record.unsupported_reasons == ()
    assert record.compatible is False

    with pytest.raises(PluginImportError) as exc_info:
        importer().normalize(record, "1.2.0")

    assert _cause(exc_info) == "plugin_import_source_unsupported"


def test_absent_publisher_description_is_an_import_failure() -> None:
    """Catches defaulting a description Omega would then attribute to a publisher."""
    recorded = _record(description=None)
    assert METADATA_INVALID in recorded.unsupported_reasons
    with pytest.raises(PluginImportError):
        importer().normalize(recorded, "1.2.0")

    unrecorded = replace(_record(), description=None, unsupported_reasons=())
    with pytest.raises(PluginImportError) as exc_info:
        importer().normalize(unrecorded, "1.2.0")

    assert _cause(exc_info) == "plugin_import_metadata_absent"


def test_absent_publisher_title_is_an_import_failure() -> None:
    """Catches synthesizing a display title from an identifier the user never saw."""
    record = _record(title=None)
    assert record.unsupported_reasons == ()
    assert record.compatible

    with pytest.raises(PluginImportError) as exc_info:
        importer().normalize(record, "1.2.0")

    assert _cause(exc_info) == "plugin_import_metadata_absent"


def test_plugin_identity_maps_one_registry_name_onto_one_plugin_id() -> None:
    """Catches a name mapping two distinct listings could both land on."""
    assert (
        importer().normalize(_record(), "1.2.0").manifest.plugin_id
        == "io.github.example.weather"
    )
    nested = importer().normalize(_record(name="io.github.example.weather/forecast"), "1.2.0")
    assert nested.manifest.plugin_id == "io.github.example.weather.forecast"

    with pytest.raises(PluginImportError) as exc_info:
        importer().normalize(_record(name="io.github.example/weather.forecast"), "1.2.0")

    assert _cause(exc_info) == "plugin_import_identity_unsupported"


@pytest.mark.parametrize(
    "name",
    [
        "weather",
        "io.github.example/weather/extra",
        "io.github.example/",
        "/weather",
        "",
    ],
)
def test_a_name_that_is_not_one_registry_identity_fails_closed(name: str) -> None:
    """Catches deriving a plugin id from text that is not a namespaced identity."""
    with pytest.raises(PluginImportError) as exc_info:
        importer().normalize(replace(_record(), name=name), "1.2.0")

    assert _cause(exc_info) == "plugin_import_identity_unsupported"


def test_a_registry_name_the_plugin_grammar_admits_keeps_its_exact_spelling() -> None:
    """Catches normalizing away characters a publisher's own name legitimately uses."""
    candidate = importer().normalize(_record(name="io.github.example/weather_beta-2"), "1.2.0")

    assert candidate.manifest.plugin_id == "io.github.example.weather_beta-2"
    assert candidate.manifest.required_apps[0].app_id == "io.github.example.weather_beta-2"


@pytest.mark.parametrize("version", ["1.3.0", "1.2", "", None, 120])
def test_normalization_pins_exactly_the_version_the_caller_resolved(
    version: object,
) -> None:
    """Catches importing whatever version the listing happened to carry."""
    with pytest.raises(PluginImportError) as exc_info:
        importer().normalize(registry_weather_server(), version)

    assert _cause(exc_info) == "plugin_import_version_mismatch"


def test_package_distribution_is_pinned_by_digest_before_the_manifest_exists(
    tmp_path: Path,
) -> None:
    """Catches building a manifest for an artifact that was never stored or proved."""
    requests: list[ArtifactRequest] = []
    candidate = MarketplaceImporter(
        artifact_store=ArtifactStore(tmp_path),
        artifact_source=_artifact_source(requests=requests),
    ).normalize(_package_record(), "1.2.0")

    assert candidate.distribution is DistributionKind.REMOTE_PACKAGE
    assert candidate.artifact is not None
    assert candidate.artifact.sha256 == _ARTIFACT_DIGEST
    assert candidate.artifact.path == tmp_path / _ARTIFACT_DIGEST[:2] / _ARTIFACT_DIGEST
    assert candidate.artifact.path.read_bytes() == _ARTIFACT
    assert candidate.source_evidence.artifact_sha256 == _ARTIFACT_DIGEST
    assert len(requests) == 1
    assert requests[0].registry_type is PackageRegistryType.NPM
    assert requests[0].identifier == "@example/weather-mcp"
    assert requests[0].sha256 == _ARTIFACT_DIGEST

    requirement = candidate.manifest.required_apps[0]
    assert requirement.deployment is AppDeployment.LOCAL
    assert requirement.transports == (McpTransport.STDIO,)
    assert requirement.remote_origin is None
    assert requirement.authorization_issuer is None


def test_a_package_without_artifact_evidence_cannot_be_imported(tmp_path: Path) -> None:
    """Catches importing a local runtime whose bytes Omega can never re-prove."""
    with pytest.raises(PluginImportError) as exc_info:
        importer(tmp_path).normalize(_package_record(fileSha256=None), "1.2.0")

    assert _cause(exc_info) == "plugin_import_artifact_evidence_absent"


def test_a_package_version_that_disagrees_with_the_listing_fails_closed(
    tmp_path: Path,
) -> None:
    """Catches pinning an artifact from a release other than the resolved one."""
    with pytest.raises(PluginImportError) as exc_info:
        importer(tmp_path).normalize(_package_record(version="1.1.0"), "1.2.0")

    assert _cause(exc_info) == "plugin_import_package_identity_mismatch"


@pytest.mark.parametrize(
    "overrides",
    [
        {"runtimeHint": "deno"},
        {"runtimeHint": None},
        {"runtimeHint": "uvx"},
        {"registryType": "oci", "identifier": "example/weather-mcp"},
        {"registryType": "oci", "identifier": "example/weather-mcp", "runtimeHint": None},
    ],
)
def test_a_package_runtime_omega_does_not_own_fails_closed(
    tmp_path: Path, overrides: dict[str, Any]
) -> None:
    """Catches accepting publisher launch text as an Omega-owned runtime."""
    with pytest.raises(PluginImportError) as exc_info:
        importer(tmp_path).normalize(_package_record(**overrides), "1.2.0")

    assert _cause(exc_info) == "plugin_import_runtime_unsupported"


def test_a_package_unlinked_from_the_registry_namespace_fails_closed(
    tmp_path: Path,
) -> None:
    """Catches importing an artifact the authenticated namespace does not cover."""
    with pytest.raises(PluginImportError) as exc_info:
        importer(tmp_path).normalize(_package_record(identifier="@other/weather-mcp"), "1.2.0")

    assert _cause(exc_info) == "plugin_import_package_linkage_mismatch"


def test_artifact_bytes_that_do_not_match_the_declared_digest_store_nothing(
    tmp_path: Path,
) -> None:
    """Catches pinning whatever bytes arrived instead of the declared artifact."""
    swapped = MarketplaceImporter(
        artifact_store=ArtifactStore(tmp_path),
        artifact_source=_artifact_source(payload=b"different-package-bytes"),
    )

    with pytest.raises(PluginImportError) as exc_info:
        swapped.normalize(_package_record(), "1.2.0")

    assert _cause(exc_info) == "plugin_import_artifact_invalid"
    assert not (tmp_path / _ARTIFACT_DIGEST[:2] / _ARTIFACT_DIGEST).exists()


def test_a_package_import_without_a_store_cannot_silently_skip_pinning() -> None:
    """Catches degrading to an unpinned import when no artifact store is configured."""
    with pytest.raises(PluginImportError) as exc_info:
        importer().normalize(_package_record(), "1.2.0")

    assert _cause(exc_info) == "plugin_import_artifact_unavailable"


def test_an_artifact_source_failure_is_reported_as_a_stable_import_cause(
    tmp_path: Path,
) -> None:
    """Catches raw transport text crossing the provider-neutral import boundary."""

    def source(_request: ArtifactRequest) -> list[bytes]:
        raise RuntimeError("connection to registry.npmjs.org refused")

    with pytest.raises(PluginImportError) as exc_info:
        MarketplaceImporter(
            artifact_store=ArtifactStore(tmp_path), artifact_source=source
        ).normalize(_package_record(), "1.2.0")

    assert _cause(exc_info) == "plugin_import_artifact_unavailable"


def test_the_importer_never_widens_the_artifact_byte_bound(tmp_path: Path) -> None:
    """Catches an importer that asks the store to hold an unbounded artifact."""
    requests: list[ArtifactRequest] = []
    MarketplaceImporter(
        artifact_store=ArtifactStore(tmp_path),
        artifact_source=_artifact_source(requests=requests),
    ).normalize(_package_record(), "1.2.0")

    assert 0 < requests[0].max_bytes <= DEFAULT_MAX_ARTIFACT_BYTES


def test_normalize_refuses_anything_that_is_not_a_registry_record() -> None:
    """Catches normalizing a look-alike object that never passed registry parsing."""
    with pytest.raises(PluginImportError) as exc_info:
        importer().normalize(object(), "1.2.0")

    assert _cause(exc_info) == "plugin_import_request_invalid"


def test_verification_produces_the_only_installable_marker(tmp_path: Path) -> None:
    """Catches an installable marker reachable without re-proving the evidence."""
    candidate = importer(tmp_path).normalize(_package_record(), "1.2.0")
    verified = importer(tmp_path).verify(candidate)

    assert type(verified) is VerifiedNormalizedPluginCandidate
    assert verified.candidate == candidate
    assert verified.manifest == candidate.manifest
    assert verified.trust_tier is TrustTier.PUBLISHER_VERIFIED


def test_the_verified_marker_cannot_be_constructed_without_the_normalizer(
    tmp_path: Path,
) -> None:
    """Catches a marker any caller can mint around an unchecked candidate."""
    candidate = importer(tmp_path).normalize(_package_record(), "1.2.0")

    with pytest.raises(PluginImportError):
        VerifiedNormalizedPluginCandidate(candidate, object())


def test_verification_refuses_a_candidate_whose_artifact_changed_on_disk(
    tmp_path: Path,
) -> None:
    """Catches verifying a recorded digest rather than the bytes it now names."""
    candidate = importer(tmp_path).normalize(_package_record(), "1.2.0")
    candidate.artifact.path.write_bytes(b"tampered")

    with pytest.raises(PluginImportError) as exc_info:
        importer(tmp_path).verify(candidate)

    assert _cause(exc_info) == "plugin_import_candidate_unverified"


def test_verification_refuses_evidence_that_disagrees_with_the_manifest() -> None:
    """Catches admitting a candidate whose manifest drifted from its source proof."""
    candidate = importer().normalize(registry_weather_server(), "1.2.0")
    forged = replace(
        candidate,
        source_evidence=replace(
            candidate.source_evidence, registry_name="io.github.other/weather"
        ),
    )

    with pytest.raises(PluginImportError) as exc_info:
        importer().verify(forged)

    assert _cause(exc_info) == "plugin_import_candidate_unverified"


def test_verification_refuses_a_candidate_that_claims_omega_verification() -> None:
    """Catches a candidate that upgrades its own tier between normalize and verify."""
    candidate = importer().normalize(registry_weather_server(), "1.2.0")
    forged = replace(candidate, trust_tier=TrustTier.OMEGA_VERIFIED)

    with pytest.raises(PluginImportError) as exc_info:
        importer().verify(forged)

    assert _cause(exc_info) == "plugin_import_candidate_unverified"


def test_a_normalized_candidate_is_not_itself_installable() -> None:
    """Catches an unverified candidate satisfying the marker type by duck typing."""
    candidate = importer().normalize(registry_weather_server(), "1.2.0")

    assert type(candidate) is NormalizedPluginCandidate
    assert not isinstance(candidate, VerifiedNormalizedPluginCandidate)


def test_the_artifact_request_stays_an_immutable_bounded_value() -> None:
    """Catches a mutable artifact request a caller could widen after validation."""
    request = ArtifactRequest(
        registry_type=PackageRegistryType.NPM,
        identifier="@example/weather-mcp",
        version="1.2.0",
        registry_base_url="https://registry.npmjs.org",
        sha256=_ARTIFACT_DIGEST,
        max_bytes=1024,
    )

    with pytest.raises(Exception):
        request.max_bytes = DEFAULT_MAX_ARTIFACT_BYTES  # type: ignore[misc]


def test_an_artifact_stream_that_dies_midway_is_reported_as_unavailable(
    tmp_path: Path,
) -> None:
    """Catches a mid-download failure reading as a wrong artifact rather than none."""

    def source(_request: ArtifactRequest):
        yield _ARTIFACT[:4]
        raise RuntimeError("connection to artifacts.example.test reset")

    with pytest.raises(PluginImportError) as exc_info:
        MarketplaceImporter(
            artifact_store=ArtifactStore(tmp_path), artifact_source=source
        ).normalize(_package_record(), "1.2.0")

    assert _cause(exc_info) == "plugin_import_artifact_unavailable"
    assert "example.test" not in _cause(exc_info)
    assert not (tmp_path / _ARTIFACT_DIGEST[:2] / _ARTIFACT_DIGEST).exists()
