"""Adding a provider must be a pipeline, not a day of archaeology.

Six artefacts have to agree before a package can be signed: the manifest, its
skill asset, the curated listing, a provenance record, a review-policy
promotion naming that record's digest, and a catalog entry. Slack's first
release was hand-authored, and every disagreement surfaced as an opaque signing
failure naming none of them -- ``marketplace_asset_invalid``,
``capability_authority_invalid``, ``plugin signing_key_id must be a non-empty
stable string``.

This runs the whole offline half for a provider that does not exist -- scaffold
then weave -- and checks each artefact against the parser that will judge it at
release time. It is the guarantee that the next provider costs a spec file.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
from hashlib import sha256
from pathlib import Path

import pytest

from mcp_platform.canonical import canonical_bytes

ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    "capture_provider_tools", ROOT / "scripts" / "capture_provider_tools.py"
)
onboarding = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(onboarding)

SPEC = {
    "plugin_id": "vendor.official",
    "title": "Vendor",
    "summary": "Official Vendor MCP access for the things it does.",
    "publisher_display_name": "Vendor",
    "app_publisher": "com.vendor",
    "provider_id": "com.vendor.mcp",
    "remote_origin": "https://mcp.vendor.example/mcp",
    "authorization_issuer": "https://mcp.vendor.example",
    "authoritative_source_url": "https://docs.vendor.example/mcp",
    "source_url": "https://vendor.example",
    "scopes": ["vendor.read"],
    "categories": ["productivity"],
    "search_terms": ["vendor"],
    "skill_slug": "vendor",
}

# A provider answering an older wire version, naming itself with a space, and
# declaring no result schemas -- all three are Slack's real shape.
CAPTURE = {
    "captured_at": "2026-09-07T02:00:00Z",
    "endpoint": SPEC["remote_origin"],
    "protocol_version": "2025-06-18",
    "server_capabilities": {"tools": {"listChanged": True}},
    "server_info": {"name": "Vendor MCP", "version": "2.1"},
    "tools": [
        {
            "name": "vendor_read",
            "description": "Read a thing.",
            "annotations": {"title": "Read a thing", "readOnlyHint": True},
            "inputSchema": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
        {
            "name": "vendor_send",
            "description": "Send a thing.",
            "annotations": {"title": "Send a thing"},
            "inputSchema": {
                "type": "object",
                "properties": {"body": {"type": "string"}},
                "required": ["body"],
            },
        },
    ],
}


@pytest.fixture
def onboarded(tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch):
    """Run scaffold + weave against copies of the real build inputs."""
    root = Path(str(tmp_path))
    packages = root / "packages"
    build = root / "marketplace_build"
    packages.mkdir(parents=True)
    (build / "metadata").mkdir(parents=True)

    real_build = ROOT / "plugin_platform" / "marketplace_build"
    catalog = root / "catalog.json"
    provenance = root / "marketplace_provenance.json"
    policy = build / "review_policy.json"
    shutil.copyfile(ROOT / "plugin_platform" / "marketplace" / "catalog.json", catalog)
    shutil.copyfile(ROOT / "plugin_platform" / "marketplace_provenance.json", provenance)
    shutil.copyfile(real_build / "review_policy.json", policy)

    monkeypatch.setattr(onboarding, "PACKAGES", packages)
    monkeypatch.setattr(onboarding, "BUILD_ROOT", build)
    monkeypatch.setattr(onboarding, "CATALOG_PATH", catalog)
    monkeypatch.setattr(onboarding, "PROVENANCE_PATH", provenance)
    monkeypatch.setattr(onboarding, "REVIEW_POLICY_PATH", policy)

    spec_path = root / "spec.json"
    spec_path.write_bytes(json.dumps(SPEC).encode())
    capture_path = root / "capture.json"
    capture_path.write_bytes(json.dumps(CAPTURE).encode())

    assert onboarding.scaffold(spec_path) == 0
    assert onboarding.weave(SPEC["plugin_id"], capture_path, None) == 0
    return {
        "package": packages / SPEC["plugin_id"],
        "catalog": catalog,
        "provenance": provenance,
        "policy": policy,
        "build": build,
    }


def test_the_manifest_parses_under_the_contract_that_will_judge_it(onboarded) -> None:
    """`plugin signing_key_id must be a non-empty stable string` cost a run."""
    from plugin_platform.contracts import PluginManifest

    manifest = PluginManifest.from_dict(
        json.loads((onboarded["package"] / "plugin.json").read_bytes())
    )

    assert manifest.plugin_id == SPEC["plugin_id"]
    app = manifest.required_apps[0]
    assert app.app_id == f"{SPEC['plugin_id']}.mcp"
    assert app.remote_origin == SPEC["remote_origin"]
    assert {capability.name for capability in app.capabilities} == {
        tool["name"] for tool in CAPTURE["tools"]
    }


def test_every_signed_document_is_canonical_bytes(onboarded) -> None:
    """A pretty-printed signed asset verifies as `asset_invalid`, nothing more."""
    for path in (
        onboarded["package"] / "plugin.json",
        onboarded["package"] / "tools" / "authority.json",
        onboarded["catalog"],
        onboarded["provenance"],
    ):
        payload = path.read_bytes()
        assert canonical_bytes(json.loads(payload)) == payload, path.name


def test_the_authority_is_accepted_against_its_own_manifest(onboarded) -> None:
    """The only parser of `tools/authority.json` must take what weave wrote."""
    from plugin_platform.contracts import PluginManifest
    from plugin_platform.package_verifier import _validate_capability_authority

    manifest = PluginManifest.from_dict(
        json.loads((onboarded["package"] / "plugin.json").read_bytes())
    )
    document = json.loads((onboarded["package"] / "tools" / "authority.json").read_bytes())

    projected, legacy = _validate_capability_authority(document, manifest)

    assert legacy is False
    tools = projected[manifest.required_apps[0].app_id]["tools"]
    assert set(tools) == {tool["name"] for tool in CAPTURE["tools"]}


def test_the_manifest_declares_the_authority_as_an_asset(onboarded) -> None:
    """Signing recomputes declared asset digests but never ADDS one.

    Without the declaration the package signs cleanly and ships with no
    capability authority at all, which is exactly the state Slack shipped in.
    """
    manifest = json.loads((onboarded["package"] / "plugin.json").read_bytes())
    authority_bytes = (onboarded["package"] / "tools" / "authority.json").read_bytes()

    asset = next(a for a in manifest["assets"] if a["path"] == "tools/authority.json")

    assert asset["kind"] == "capability_authority"
    assert asset["sha256"] == sha256(authority_bytes).hexdigest()


def test_the_provenance_record_parses_and_the_policy_names_its_digest(onboarded) -> None:
    """A promotion is only honoured when it names the digest the record carries.

    Getting this pair out of step is what made assemble and sign disagree about
    `provenance_sha256` for an unpromoted entry.
    """
    from plugin_platform.marketplace_provenance import ProviderProvenanceRecord

    provenance = json.loads(onboarded["provenance"].read_bytes())
    raw = next(r for r in provenance["records"] if r["plugin_id"] == SPEC["plugin_id"])
    record = ProviderProvenanceRecord.from_dict(raw)
    assert record.trust_tier == "omega_verified"

    policy = json.loads(onboarded["policy"].read_bytes())
    promotion = next(p for p in policy["promotions"] if p["plugin_id"] == SPEC["plugin_id"])
    assert promotion["provenance_sha256"] == sha256(canonical_bytes(record.to_dict())).hexdigest()


def test_the_catalog_entry_parses_and_points_at_this_version(onboarded) -> None:
    """Signing refuses a version or URI that disagrees with the manifest."""
    from plugin_platform.catalog import MarketplaceCatalogEntry

    catalog = json.loads(onboarded["catalog"].read_bytes())
    entry = MarketplaceCatalogEntry.from_dict(
        next(e for e in catalog["entries"] if e["plugin_id"] == SPEC["plugin_id"])
    )

    assert entry.plugin_version == "1.0.0"
    assert entry.package_uri.endswith(f"/{SPEC['plugin_id']}/1.0.0")
    assert entry.trust_tier.value == "omega_verified"


def test_an_older_provider_is_declared_with_the_version_it_speaks(onboarded) -> None:
    """The wire version is the drift check, so it must be what was observed."""
    document = json.loads((onboarded["package"] / "tools" / "authority.json").read_bytes())
    app = next(iter(document["apps"].values()))

    compatibility = app["runtime_compatibility"]
    assert compatibility["wire_protocol_version"] == CAPTURE["protocol_version"]
    assert compatibility["missing_metadata"] == {
        "server_id": "Vendor MCP",
        "server_version": "2.1",
    }


def test_scaffolding_over_an_existing_package_is_refused(onboarded, tmp_path) -> None:
    """Re-running must not silently rewrite a released package."""
    spec_path = Path(str(tmp_path)) / "spec.json"

    with pytest.raises(SystemExit, match="already exists"):
        onboarding.scaffold(spec_path)


@pytest.mark.parametrize("dropped", sorted(SPEC))
def test_an_incomplete_spec_names_what_is_missing(
    dropped: str, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every field is load-bearing, so a gap must be reported, not defaulted."""
    root = Path(str(tmp_path))
    monkeypatch.setattr(onboarding, "PACKAGES", root / "packages")
    spec_path = root / "spec.json"
    spec_path.write_bytes(json.dumps({k: v for k, v in SPEC.items() if k != dropped}).encode())

    with pytest.raises(SystemExit, match=dropped):
        onboarding.scaffold(spec_path)


def test_a_spec_rejected_on_its_content_writes_nothing(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing field is caught before the writes; a malformed one must be too.

    Four of the twelve providers onboarded on 2026-09-07 were refused on their
    documentation URL, and the refusal happened after the manifest and skill
    were already written. The rerun then reported only "already exists;
    scaffold will not overwrite it", which names neither the real problem nor
    the four directories that had to be deleted by hand first.
    """
    root = Path(str(tmp_path))
    packages = root / "packages"
    build = root / "marketplace_build"
    packages.mkdir(parents=True)
    (build / "metadata").mkdir(parents=True)
    monkeypatch.setattr(onboarding, "PACKAGES", packages)
    monkeypatch.setattr(onboarding, "BUILD_ROOT", build)

    spec_path = root / "spec.json"
    spec_path.write_bytes(
        json.dumps({**SPEC, "authoritative_source_url": "https://docs.example/a/../b"}).encode()
    )

    with pytest.raises(ValueError, match="^provenance_invalid$"):
        onboarding.scaffold(spec_path)

    assert list(packages.iterdir()) == []
    assert list((build / "metadata").iterdir()) == []
