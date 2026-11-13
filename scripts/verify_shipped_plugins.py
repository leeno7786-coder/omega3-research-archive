"""Verify every shipped plugin and OAuth profile against reality.

Run from the repo root:

    .venv-brain\\Scripts\\python.exe scripts\verify_shipped_plugins.py


Three checks per subject, all read-only:
  1. the signed artefacts verify offline (catalog, package, profile catalog);
  2. the HTTPS relay actually redirects that profile's callback to loopback;
  3. Omega's own discovery resolves the vendor's live metadata for that profile.

No credential is used and no authorization is started: discovery only reads the
two well-known documents a provider publishes.
"""

from __future__ import annotations

import asyncio
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import paths as core_paths  # noqa: E402
from mcp_platform.oauth_profiles import (  # noqa: E402
    OAuthAuthorizationFlow,
    OAuthClientMode,
    OAuthProfileMatchInput,
    OAuthProfileRegistry,
)
from mcp_platform.oauth_protocol import OAuthProtocolClient  # noqa: E402
from mcp_platform import plugin_planning as planning  # noqa: E402
from plugin_platform.catalog import MARKETPLACE_CATALOG_PATH, verify_plugin_catalog  # noqa: E402
from plugin_platform.package_verifier import verify_plugin_package  # noqa: E402
from plugin_platform.trust import load_embedded_key_ring  # noqa: E402

RELAY = "https://plugins.omega2ai.com"
PACKAGES = ROOT / "plugin_platform" / "marketplace" / "packages"


def relay_check(profile_id: str) -> str:
    """Does the public relay hand this profile's callback back to loopback?"""
    url = f"{RELAY}/oauth/{profile_id}/callback?code=probe&state=probe"
    request = urllib.request.Request(url, method="HEAD")
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        response = opener.open(request, timeout=20)
        return f"no-redirect({response.status})"
    except _Redirected as exc:
        expected = f"http://127.0.0.1:13310/oauth/{profile_id}/callback?code=probe&state=probe"
        return "ok" if exc.location == expected else f"wrong-target({exc.location})"
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        return f"error({type(exc).__name__})"


class _Redirected(Exception):
    def __init__(self, location: str) -> None:
        self.location = location


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, D102
        raise _Redirected(newurl)


async def discovery_check(protocol: OAuthProtocolClient, profile) -> str:
    """Does the vendor publish metadata this signed profile actually matches?"""
    origin = profile.resource_origins[0]
    try:
        discovery = await protocol.discover(profile, resource_origin=origin)
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        reason = getattr(exc, "reason", None) or str(exc) or type(exc).__name__
        return f"FAIL {reason}"
    notes = []
    if profile.authorization_flow is OAuthAuthorizationFlow.AUTHORIZATION_CODE_PKCE:
        if "S256" not in discovery.code_challenge_methods_supported:
            notes.append("no-S256")
    if profile.client_mode is OAuthClientMode.DYNAMIC_REGISTRATION:
        if discovery.registration_endpoint is None:
            notes.append("no-registration-endpoint")
    return "ok" + (" | " + ", ".join(notes) if notes else "")


async def main() -> int:
    ring = load_embedded_key_ring()
    print("=" * 100)
    print("SIGNED ARTEFACTS")
    print("=" * 100)
    catalog = verify_plugin_catalog(MARKETPLACE_CATALOG_PATH, ring)
    print(f"marketplace catalog: sequence {catalog.sequence}, {len(catalog.entries)} entries, verified")
    registry = OAuthProfileRegistry.from_path(core_paths.OAUTH_PROFILE_CATALOG_PATH, ring)
    document = json.loads(core_paths.OAUTH_PROFILE_CATALOG_PATH.read_bytes())
    profiles = [registry.resolve_by_profile_id(item["profile_id"]) for item in document["profiles"]]
    print(f"oauth profile catalog: sequence {document['sequence']}, {len(profiles)} profiles, verified")

    print()
    print("=" * 100)
    print("PACKAGES  (offline verification, profile match, planner reachability)")
    print("=" * 100)
    print(f"{'plugin_id':30} {'ver':7} {'tools':>5} {'plannable':>9}  {'profile':24} {'trust'}")
    package_rows = 0
    for entry in catalog.entries:
        package = PACKAGES / entry.plugin_id
        verified = verify_plugin_package(package, ring, "3.0.0")
        app = verified.manifest.required_apps[0]
        try:
            profile = registry.resolve(
                OAuthProfileMatchInput(
                    catalog_signer="omega.featured",
                    plugin_publisher=verified.manifest.publisher_id,
                    app_publisher=app.publisher_id,
                    provider_id=str(app.provider_id),
                    authorization_issuer=str(app.authorization_issuer),
                    resource_origin=str(app.remote_origin),
                )
            )
            profile_id = profile.profile_id
        except Exception as exc:  # noqa: BLE001
            profile_id = f"NO MATCH ({exc})"
        authority_path = package / "tools" / "authority.json"
        total = plannable = 0
        if authority_path.is_file():
            tools = next(iter(json.loads(authority_path.read_bytes())["apps"].values()))["tools"]
            for tool in tools.values():
                total += 1
                try:
                    planning._schema(tool["input_schema"])
                    planning._result_schema(tool["output_schema"])
                    plannable += 1
                except Exception:  # noqa: BLE001
                    pass
        print(
            f"{entry.plugin_id:30} {entry.plugin_version:7} {total:5} {plannable:9}  "
            f"{profile_id:24} {entry.trust_tier.value}"
        )
        package_rows += 1

    print()
    print("=" * 100)
    print("PROFILES  (public relay redirect + live vendor metadata discovery)")
    print("=" * 100)
    print(f"{'profile_id':26} {'mode':22} {'relay':>10}  discovery")
    protocol = OAuthProtocolClient()
    relay_ok = discovery_ok = 0
    failures: list[str] = []
    for profile in profiles:
        if profile.redirect_uri is None:
            relay = "n/a (device)"
        elif profile.redirect_uri.startswith(RELAY):
            relay = relay_check(profile.profile_id)
        else:
            # A loopback redirect never travels through the relay; Google's
            # desktop client only accepts loopback, so probing the relay for it
            # would report a redirect the flow does not use.
            relay = "loopback"
        discovery = await discovery_check(protocol, profile)
        if relay in {"ok", "n/a (device)"}:
            relay_ok += 1
        if discovery.startswith("ok"):
            discovery_ok += 1
        else:
            failures.append(f"{profile.profile_id}: {discovery}")
        print(f"{profile.profile_id:26} {profile.client_mode.value:22} {relay:>10}  {discovery}")

    print()
    print("=" * 100)
    print(
        f"SUMMARY: {package_rows} packages verified | relay {relay_ok}/{len(profiles)} | "
        f"live discovery {discovery_ok}/{len(profiles)}"
    )
    if failures:
        print("\nProfiles whose vendor metadata did not resolve:")
        for line in failures:
            print("  -", line)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
