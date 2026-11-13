"""Reproducible, non-auto-verifying marketplace catalog builder tests.

The builder turns captured Official MCP Registry pages plus an explicit
maintainer review policy into a deterministic unsigned catalog. Two runs over
identical captured input must produce byte-identical unsigned catalog bytes,
and a listing absent from the review policy must never surface as anything
above ``publisher_verified`` -- promotion to ``omega_verified`` is something a
policy states, never something a candidate or a prior build implies.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

import pytest

import scripts.build_marketplace_catalog as build_marketplace_catalog
from mcp_platform.canonical import canonical_bytes
from plugin_platform.catalog import (
    MARKETPLACE_CATALOG_PATH,
    MARKETPLACE_PACKAGES_ROOT,
    MARKETPLACE_ROOT,
)
from plugin_platform.marketplace_importer import MarketplaceImporter
from plugin_platform.mcp_registry import McpRegistryRequest
from scripts.build_marketplace_catalog import (
    DEFAULT_BUILD_OUTPUT_ROOT,
    DEFAULT_METADATA_ROOT,
    DEFAULT_REVIEW_POLICY_PATH,
    AssembledCatalog,
    BuildError,
    ReviewPolicy,
    _parse_selection,
    assemble_catalog,
    fetch_registry_pages,
    load_review_policy,
    main,
    normalize_captured_pages,
    read_captured_pages,
    write_candidate_evidence,
    write_captured_pages,
    write_normalized_candidate,
)

def _captured_registry_page() -> bytes:
    """One self-contained captured Official MCP Registry page: a single listing.

    The remote endpoint path is pinned to the one form Omega's own manifest
    grammar admits (``/mcp/v1``, see ``plugin_platform.contracts._https_origin``)
    so this fixture exercises the real, unmodified normalization path rather
    than a relaxed one.
    """
    document = {
        "servers": [
            {
                "server": {
                    "name": "io.github.example/weather",
                    "title": "Weather",
                    "description": "Weather forecasts and severe weather alerts.",
                    "version": "1.4.0",
                    "remotes": [
                        {
                            "type": "streamable-http",
                            "url": "https://mcp.weather.example.com/mcp/v1",
                        }
                    ],
                },
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
    return json.dumps(document).encode("utf-8")


REGISTRY_FIXTURE = _captured_registry_page()
REVIEW_POLICY: dict[str, object] = {"schema_version": 1, "promotions": []}


def _weather_catalog_metadata() -> dict[str, object]:
    """Maintainer-curated listing metadata a registry capture never supplies."""
    return {
        "categories": ["productivity"],
        "search_terms": ["forecast", "weather"],
        "icon_asset_path": None,
        "support_url": None,
        "source_url": "https://github.com/example/weather",
        "distribution": {
            "kind": "remote_mcp",
            "remote_origin": "https://mcp.weather.example.com",
        },
        "pricing": "free",
        "offline_capable": False,
        "release_history": [],
        "update_notes_sha256": None,
        "released_at": "2026-08-01T10:30:00Z",
    }


@dataclass(frozen=True, slots=True)
class _BuiltSnapshot:
    catalog: AssembledCatalog

    @property
    def catalog_unsigned_bytes(self) -> bytes:
        return self.catalog.catalog_unsigned_bytes

    def entry(self, plugin_id: str):
        return self.catalog.entry(plugin_id)


def build_from_fixture(root: Path, registry_fixture: bytes, review_policy: dict) -> _BuiltSnapshot:
    """Run fetch-registry -> normalize -> assemble entirely offline within ``root``."""
    captured_dir = root / "captured"
    packages_root = root / "packages"
    metadata_root = root / "metadata"

    def loopback_transport(_request: McpRegistryRequest) -> bytes:
        return registry_fixture

    pages = fetch_registry_pages(loopback_transport, requests=(McpRegistryRequest(),))
    write_captured_pages(captured_dir, pages)

    result = normalize_captured_pages(
        read_captured_pages(captured_dir),
        selections=(("io.github.example/weather", "1.4.0"),),
        importer=MarketplaceImporter(),
    )
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    write_normalized_candidate(packages_root, candidate)
    metadata_dir = write_candidate_evidence(metadata_root, candidate)
    (metadata_dir / "catalog_metadata.json").write_bytes(
        canonical_bytes(_weather_catalog_metadata())
    )

    catalog = assemble_catalog(
        packages_root,
        metadata_root=metadata_root,
        catalog_id="test.marketplace",
        publisher_id="test.marketplace.publisher",
        sequence=1,
        signing_key_id="test-signing-key",
        review_policy=ReviewPolicy.from_dict(review_policy),
        omega_version="3.0.0",
    )
    return _BuiltSnapshot(catalog=catalog)


def test_marketplace_builder_is_reproducible_and_never_auto_verifies(tmp_path: Path) -> None:
    first = build_from_fixture(tmp_path / "one", REGISTRY_FIXTURE, REVIEW_POLICY)
    second = build_from_fixture(tmp_path / "two", REGISTRY_FIXTURE, REVIEW_POLICY)
    assert first.catalog_unsigned_bytes == second.catalog_unsigned_bytes
    weather = first.entry("io.github.example/weather")
    assert weather.trust_tier == "publisher_verified"


def test_fetch_registry_rejects_a_page_the_bounded_parser_cannot_admit(tmp_path: Path) -> None:
    """Catches a captured payload that is not a valid registry page from surviving."""

    def broken_transport(_request: McpRegistryRequest) -> bytes:
        return b"{not json"

    with pytest.raises(Exception):
        fetch_registry_pages(broken_transport, requests=(McpRegistryRequest(),))


def test_captured_pages_round_trip_through_disk_unmodified(tmp_path: Path) -> None:
    """Catches a capture step that re-encodes bytes instead of storing them verbatim."""
    payload = REGISTRY_FIXTURE
    pages = fetch_registry_pages(lambda _r: payload, requests=(McpRegistryRequest(),))
    write_captured_pages(tmp_path / "captured", pages)

    restored = read_captured_pages(tmp_path / "captured")

    assert restored == pages == (payload,)


def test_normalize_never_promotes_a_listing_the_registry_did_not_endorse() -> None:
    """Catches a normalized candidate claiming a trust tier the registry cannot grant."""
    pages = (REGISTRY_FIXTURE,)

    result = normalize_captured_pages(
        pages,
        selections=(("io.github.example/weather", "1.4.0"),),
        importer=MarketplaceImporter(),
    )

    assert len(result.candidates) == 1
    assert result.candidates[0].trust_tier.value == "publisher_verified"
    assert len(result.review_report) == 1
    assert result.review_report[0].plugin_id == "io.github.example.weather"


def test_normalize_fails_closed_on_an_unresolved_selection() -> None:
    """Catches silently skipping a maintainer-requested listing that never resolved."""
    pages = (REGISTRY_FIXTURE,)

    with pytest.raises(BuildError):
        normalize_captured_pages(
            pages,
            selections=(("io.github.example/does-not-exist", "1.0.0"),),
            importer=MarketplaceImporter(),
        )


def test_assemble_refuses_a_package_directory_without_curated_catalog_metadata(
    tmp_path: Path,
) -> None:
    """Catches assembling a listing with no maintainer-supplied product metadata."""
    pages = (REGISTRY_FIXTURE,)
    result = normalize_captured_pages(
        pages,
        selections=(("io.github.example/weather", "1.4.0"),),
        importer=MarketplaceImporter(),
    )
    packages_root = tmp_path / "packages"
    metadata_root = tmp_path / "metadata"
    write_normalized_candidate(packages_root, result.candidates[0])
    write_candidate_evidence(metadata_root, result.candidates[0])

    with pytest.raises(BuildError):
        assemble_catalog(
            packages_root,
            metadata_root=metadata_root,
            catalog_id="test.marketplace",
            publisher_id="test.marketplace.publisher",
            sequence=1,
            signing_key_id="test-signing-key",
            review_policy=ReviewPolicy.from_dict(REVIEW_POLICY),
        )


def test_assemble_refuses_a_review_policy_promotion_with_the_wrong_provenance_digest(
    tmp_path: Path,
) -> None:
    """Catches promoting a listing on a provenance digest the bundled record disagrees with."""
    pages = (REGISTRY_FIXTURE,)
    result = normalize_captured_pages(
        pages,
        selections=(("io.github.example/weather", "1.4.0"),),
        importer=MarketplaceImporter(),
    )
    packages_root = tmp_path / "packages"
    metadata_root = tmp_path / "metadata"
    write_normalized_candidate(packages_root, result.candidates[0])
    metadata_dir = write_candidate_evidence(metadata_root, result.candidates[0])
    (metadata_dir / "catalog_metadata.json").write_bytes(
        canonical_bytes(_weather_catalog_metadata())
    )
    forged_policy = ReviewPolicy.from_dict(
        {
            "schema_version": 1,
            "promotions": [
                {
                    "plugin_id": "io.github.example.weather",
                    "provenance_sha256": "a" * 64,
                }
            ],
        }
    )

    with pytest.raises(BuildError):
        assemble_catalog(
            packages_root,
            metadata_root=metadata_root,
            catalog_id="test.marketplace",
            publisher_id="test.marketplace.publisher",
            sequence=1,
            signing_key_id="test-signing-key",
            review_policy=forged_policy,
        )


def test_review_policy_rejects_a_document_with_unaccounted_fields() -> None:
    """Catches a review policy schema loosened to admit extra, unreviewed keys."""
    with pytest.raises(BuildError):
        ReviewPolicy.from_dict({"schema_version": 1, "promotions": [], "extra": True})


def test_assembled_catalog_entry_lookup_fails_closed_on_an_unknown_plugin_id(
    tmp_path: Path,
) -> None:
    """Catches an entry lookup that returns something for a plugin never assembled."""
    built = build_from_fixture(tmp_path, REGISTRY_FIXTURE, REVIEW_POLICY)

    with pytest.raises(BuildError):
        built.entry("not.a.catalogued.plugin")


def test_assemble_reproduces_the_shipped_snapshot_from_committed_build_inputs() -> None:
    """Catches reproducibility depending on anything not actually committed to the repo.

    This runs ``assemble`` from exactly its committed defaults -- the real,
    untouched ``plugin_platform/marketplace/packages``, the committed
    ``plugin_platform/marketplace_build/metadata`` catalog listing metadata, and
    the committed ``plugin_platform/marketplace_build/review_policy.json`` -- so
    a maintainer with nothing but this repository checked out can reproduce the
    shipped snapshot without any ephemeral, out-of-repo input.

    The sequence and key are read from the shipped catalog rather than frozen
    here. Pinning them made this fail on every release for a reason that had
    nothing to do with reproducibility, which is how it came to be failing
    alongside a real one: the review policy had gone stale against GitHub's
    provenance record after its issuer was corrected, and `assemble` was
    refusing outright.
    """
    shipped = json.loads(MARKETPLACE_CATALOG_PATH.read_bytes().decode("utf-8"))
    review_policy = load_review_policy(DEFAULT_REVIEW_POLICY_PATH)

    assembled = assemble_catalog(
        MARKETPLACE_PACKAGES_ROOT,
        metadata_root=DEFAULT_METADATA_ROOT,
        catalog_id="omega.featured",
        publisher_id="omega.first_party",
        sequence=shipped["sequence"],
        signing_key_id=shipped["signing_key_id"],
        review_policy=review_policy,
    )

    current_unsigned_bytes = canonical_bytes(
        {key: value for key, value in shipped.items() if key != "signature"}
    )

    assert assembled.catalog_unsigned_bytes == current_unsigned_bytes
    for plugin_id in (
        "github.official",
        "google.workspace.calendar",
        "google.workspace.drive",
        "google.workspace.gmail",
    ):
        assert assembled.entry(plugin_id).trust_tier == "omega_verified"


def test_assemble_defaults_to_the_committed_metadata_root_and_review_policy(
    tmp_path: Path,
) -> None:
    """Catches the CLI/library defaults drifting off the committed build inputs."""
    from inspect import signature

    parameters = signature(assemble_catalog).parameters
    assert parameters["metadata_root"].default == DEFAULT_METADATA_ROOT
    assert DEFAULT_METADATA_ROOT.is_dir()
    assert DEFAULT_REVIEW_POLICY_PATH.is_file()


def _tree_digest_snapshot(root: Path) -> dict[str, str]:
    """Return a relative-path -> sha256-hex snapshot of every regular file under root."""
    return {
        path.relative_to(root).as_posix(): sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _remove_build_output_root() -> None:
    output_path = DEFAULT_BUILD_OUTPUT_ROOT / "catalog.json"
    if output_path.exists():
        output_path.unlink()
    for directory in (DEFAULT_BUILD_OUTPUT_ROOT, DEFAULT_BUILD_OUTPUT_ROOT.parent):
        try:
            directory.rmdir()
        except OSError:
            pass


def test_assemble_cli_defaults_never_touch_the_shipped_marketplace_directory() -> None:
    """Catches a bare CLI default writing straight over the live, shipped catalog.

    This is exactly the mechanism that once clobbered
    ``plugin_platform/marketplace/catalog.json`` (stripping its ``signature``
    field): running ``assemble`` with no explicit ``--output`` used to default
    to that shipped path. Every file already under
    ``plugin_platform/marketplace/`` -- the catalog and every package -- must
    now be byte-identical before and after a default-``--output`` run, and the
    unsigned bytes must land in the build output directory instead.
    """
    shipped = json.loads(MARKETPLACE_CATALOG_PATH.read_bytes().decode("utf-8"))
    before = _tree_digest_snapshot(MARKETPLACE_ROOT)
    _remove_build_output_root()

    try:
        main(
            [
                "assemble",
                "--catalog-id",
                "omega.featured",
                "--publisher-id",
                "omega.first_party",
                "--sequence",
                str(shipped["sequence"]),
                "--signing-key-id",
                shipped["signing_key_id"],
            ]
        )

        after = _tree_digest_snapshot(MARKETPLACE_ROOT)
        assert after == before

        output_path = DEFAULT_BUILD_OUTPUT_ROOT / "catalog.json"
        assert output_path.is_file()
        current_raw = json.loads(MARKETPLACE_CATALOG_PATH.read_bytes().decode("utf-8"))
        current_unsigned_bytes = canonical_bytes(
            {key: value for key, value in current_raw.items() if key != "signature"}
        )
        assert output_path.read_bytes() == current_unsigned_bytes
    finally:
        _remove_build_output_root()


def test_verify_cli_defaults_to_the_build_output_directory_not_the_shipped_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches the ``verify --root`` default drifting back to the shipped snapshot path."""
    captured: dict[str, object] = {}

    def _fake_cmd_verify(args: object) -> None:
        captured["root"] = args.root  # type: ignore[attr-defined]

    monkeypatch.setattr(build_marketplace_catalog, "_cmd_verify", _fake_cmd_verify)

    build_marketplace_catalog.main(["verify"])

    assert captured["root"] == str(DEFAULT_BUILD_OUTPUT_ROOT)
    assert DEFAULT_BUILD_OUTPUT_ROOT != MARKETPLACE_ROOT


@pytest.mark.parametrize("item", ["no-at-sign", "", "a@b@c", "@1.0.0", "io.github.example/x@"])
def test_cli_select_parsing_fails_closed_on_a_malformed_name_at_version(item: str) -> None:
    """Catches a malformed --select argument crashing as a raw unpacking ValueError."""
    with pytest.raises(BuildError):
        _parse_selection(item)


def test_cli_select_parsing_accepts_the_documented_name_at_version_shape() -> None:
    """Catches the parser rejecting the exact shape its own --help documents."""
    assert _parse_selection("io.github.example/weather@1.4.0") == (
        "io.github.example/weather",
        "1.4.0",
    )


# ---------------------------------------------------------------------------
# authentication.json overlay: the one maintainer widening assemble accepts
# ---------------------------------------------------------------------------

_OVERLAY_ISSUER = "https://mcp.weather.example.com"
_OVERLAY_PROFILE_ID = "weather.desktop"


def _stage_none_candidate(root: Path) -> tuple[Path, Path, str]:
    """Normalize the weather fixture (no variables -> authentication kind none)."""
    captured_dir = root / "captured"
    packages_root = root / "packages"
    metadata_root = root / "metadata"
    pages = fetch_registry_pages(
        lambda _request: REGISTRY_FIXTURE, requests=(McpRegistryRequest(),)
    )
    write_captured_pages(captured_dir, pages)
    result = normalize_captured_pages(
        read_captured_pages(captured_dir),
        selections=(("io.github.example/weather", "1.4.0"),),
        importer=MarketplaceImporter(),
    )
    candidate = result.candidates[0]
    write_normalized_candidate(packages_root, candidate)
    metadata_dir = write_candidate_evidence(metadata_root, candidate)
    (metadata_dir / "catalog_metadata.json").write_bytes(
        canonical_bytes(_weather_catalog_metadata())
    )
    return packages_root, metadata_root, candidate.manifest.plugin_id


def _write_overlay(metadata_root: Path, plugin_id: str, **overrides: object) -> None:
    document: dict[str, object] = {
        "kind": "oauth_authorization_code_pkce",
        "authorization_issuer": _OVERLAY_ISSUER,
        "scopes": ["read", "write"],
        "profile_id": _OVERLAY_PROFILE_ID,
        **overrides,
    }
    (metadata_root / plugin_id / "authentication.json").write_bytes(canonical_bytes(document))


def _fixture_profile_registry(root: Path):
    """A signed profile catalog with one dynamic-registration weather profile."""
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from mcp_platform.oauth_profiles import (
        OAuthAuthorizationFlow,
        OAuthClientMode,
        OAuthDynamicRegistrationTemplate,
    )
    from plugin_platform.trust import TrustedPublisherKeyRing
    from scripts.provision_oauth_profiles import (
        OAUTH_PROFILE_KEY_ID,
        OAuthProviderInput,
        provision_oauth_profiles,
    )

    seed = bytes(range(32))
    public_key = (
        Ed25519PrivateKey.from_private_bytes(seed)
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )
    ring = TrustedPublisherKeyRing.from_dict(
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
    redirect = f"http://127.0.0.1:13310/oauth/{_OVERLAY_PROFILE_ID}/callback"
    provider = OAuthProviderInput(
        profile_id=_OVERLAY_PROFILE_ID,
        provider_id="com.example.weather.mcp",
        app_publisher="com.example.weather",
        authorization_issuer=_OVERLAY_ISSUER,
        resource_origins=("https://mcp.weather.example.com/mcp/v1",),
        authorization_flow=OAuthAuthorizationFlow.AUTHORIZATION_CODE_PKCE,
        client_mode=OAuthClientMode.DYNAMIC_REGISTRATION,
        account_partition="issuer_account",
        allowed_plugin_publishers=("com.example.weather",),
        redirect_uri=redirect,
        registration=OAuthDynamicRegistrationTemplate.from_dict(
            {
                "client_name": "Omega",
                "redirect_uris": [redirect],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "scope": "",
                "software_id": "org.omega2ai.desktop",
            }
        ),
        registration_file=None,
    )
    result = provision_oauth_profiles(
        providers=(provider,),
        catalog_path=root / "oauth_profiles.json",
        signing_seed=seed,
        key_ring=ring,
    )
    return result.registry


def _assemble_overlaid(packages_root: Path, metadata_root: Path, registry) -> AssembledCatalog:
    return assemble_catalog(
        packages_root,
        metadata_root=metadata_root,
        catalog_id="test.marketplace",
        publisher_id="test.marketplace.publisher",
        sequence=9,
        signing_key_id="test-signing-key",
        review_policy=ReviewPolicy.from_dict(REVIEW_POLICY),
        omega_version="3.0.0",
        profile_registry=registry,
    )


_OVERLAY_TOUCHED = {"authentication", "authorization_issuer", "provider_id"}


def test_assemble_applies_the_overlay_to_a_none_candidate(tmp_path: Path) -> None:
    packages_root, metadata_root, plugin_id = _stage_none_candidate(tmp_path)
    before = json.loads((packages_root / plugin_id / "plugin.json").read_bytes())
    assert before["required_apps"][0]["authentication"]["kind"] == "none"
    _write_overlay(metadata_root, plugin_id)
    registry = _fixture_profile_registry(tmp_path)

    assembled = _assemble_overlaid(packages_root, metadata_root, registry)

    entry = assembled.entry(plugin_id)
    assert entry.trust_tier.value == "publisher_verified"
    written = json.loads((packages_root / plugin_id / "plugin.json").read_bytes())
    app = written["required_apps"][0]
    assert app["authentication"] == {
        "kind": "oauth_authorization_code_pkce",
        "scopes": ["read", "write"],
        "secret_fields": [],
    }
    assert app["authorization_issuer"] == _OVERLAY_ISSUER
    assert app["provider_id"] == "com.example.weather.mcp"
    assert {k: v for k, v in app.items() if k not in _OVERLAY_TOUCHED} == {
        k: v for k, v in before["required_apps"][0].items() if k not in _OVERLAY_TOUCHED
    }
    # the overlaid manifest is exactly what the entry digests, and a rerun is identical
    again = _assemble_overlaid(packages_root, metadata_root, registry)
    assert again.catalog_unsigned_bytes == assembled.catalog_unsigned_bytes
    assert entry.manifest_sha256 == sha256(canonical_bytes(written)).hexdigest()


def test_assemble_without_an_overlay_leaves_a_none_candidate_untouched(tmp_path: Path) -> None:
    packages_root, metadata_root, plugin_id = _stage_none_candidate(tmp_path)
    before = (packages_root / plugin_id / "plugin.json").read_bytes()

    _assemble_overlaid(packages_root, metadata_root, None)

    assert (packages_root / plugin_id / "plugin.json").read_bytes() == before


def test_assemble_rejects_an_overlay_for_a_candidate_that_already_declares_secret_fields(
    tmp_path: Path,
) -> None:
    packages_root, metadata_root, plugin_id = _stage_none_candidate(tmp_path)
    manifest_path = packages_root / plugin_id / "plugin.json"
    document = json.loads(manifest_path.read_bytes())
    document["required_apps"][0]["authentication"] = {
        "kind": "declared_secret_fields",
        "scopes": [],
        "secret_fields": [
            {"field_id": "api_key", "label": "API key", "secret": True, "required": True}
        ],
    }
    manifest_path.write_bytes(canonical_bytes(document))
    before = manifest_path.read_bytes()
    _write_overlay(metadata_root, plugin_id)

    with pytest.raises(BuildError, match="marketplace_build_overlay_invalid"):
        _assemble_overlaid(packages_root, metadata_root, _fixture_profile_registry(tmp_path))
    assert manifest_path.read_bytes() == before


@pytest.mark.parametrize(
    "overrides",
    (
        {"authorization_issuer": "http://mcp.weather.example.com"},
        {"authorization_issuer": "https://accounts.example.com"},
        {"profile_id": "no-such-profile"},
        {"kind": "declared_secret_fields"},
        {"scopes": []},
        {"unexpected": True},
    ),
    ids=("http-issuer", "issuer-not-the-profiles", "unknown-profile", "wrong-kind", "no-scopes", "extra-field"),
)
def test_assemble_rejects_a_malformed_or_unbacked_overlay(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    packages_root, metadata_root, plugin_id = _stage_none_candidate(tmp_path)
    manifest_path = packages_root / plugin_id / "plugin.json"
    before = manifest_path.read_bytes()
    _write_overlay(metadata_root, plugin_id, **overrides)

    with pytest.raises(BuildError, match="marketplace_build_overlay_invalid"):
        _assemble_overlaid(packages_root, metadata_root, _fixture_profile_registry(tmp_path))
    assert manifest_path.read_bytes() == before
