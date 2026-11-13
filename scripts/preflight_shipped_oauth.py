"""Live OAuth pre-flight for every shipped plugin, up to -- never through -- consent.

Run from the repo root:

    .venv-brain\\Scripts\\python.exe scripts\\preflight_shipped_oauth.py

This is docs/plugin-provider-onboarding.md "Pass 0" run against the shipped
catalog, using the product's own machinery at every step:

  0. offline: the manifest's ``authentication.scopes`` equal the signed
     profile's ``registration.scope`` (dynamic registration only);
  1. discovery: ``OAuthProtocolClient.discover`` + the broker's binding check;
  2. registration (dynamic_registration only): ``OAuthProtocolClient.register``,
     deregistered again afterwards when the provider issued a management token;
  3. authorize: the URL ``OAuthBroker.begin`` would open, built by
     ``OAuthAuthorizationParameters.for_profile`` + ``build_authorization_url``,
     then its redirect chain is followed (bounded, GET only, no cookies) and PASS
     needs login or consent evidence on the page it lands on. Device-code profiles
     instead send the device authorization request (the first step, no consent
     shown) and are never polled. Hosted-bridge profiles start through the product's
     bridge client, and the bridge's URL must be exactly the signed request the
     unit's broker accepts.

Nobody signs in and no token is exchanged. Tokens, client secrets, device codes
and registration access tokens are never printed: only client_id and provider
error codes/descriptions are.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import secrets
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import paths as core_paths  # noqa: E402
from mcp_platform.oauth_broker import OAuthBroker  # noqa: E402
from mcp_platform.oauth_profiles import (  # noqa: E402
    OAuthAuthorizationFlow,
    OAuthClientMode,
    OAuthProfileMatchInput,
    OAuthProfileRegistry,
)
from mcp_platform.oauth_protocol import (  # noqa: E402
    OAuthAuthorizationParameters,
    OAuthNetworkError,
    OAuthProtocolClient,
    OAuthProtocolError,
    OAuthProviderError,
)
from mcp_platform.oauth_vault import OAuthClientCredential  # noqa: E402
from plugin_platform.catalog import MARKETPLACE_CATALOG_PATH, verify_plugin_catalog  # noqa: E402
from plugin_platform.package_verifier import verify_plugin_package  # noqa: E402
from plugin_platform.trust import load_embedded_key_ring  # noqa: E402

PACKAGES = ROOT / "plugin_platform" / "marketplace" / "packages"
TIMEOUT_SECONDS = 20
MAX_BODY_BYTES = 1_000_000
#: The authorize URL is opened in the user's browser, so it is fetched as one.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "identity",
}
OAUTH_ERROR_CODES = (
    "invalid_scope",
    "invalid_client",
    "invalid_request",
    "unauthorized_client",
    "unsupported_response_type",
    "invalid_redirect_uri",
    "redirect_uri_mismatch",
    "invalid_target",
)
_ERROR_CODE_RE = re.compile(r"\b(" + "|".join(OAUTH_ERROR_CODES) + r")\b")
_REDIRECT_TEXT_RE = re.compile(
    r"redirect[_ ]?ur[il]s?\W{0,40}(?:\w+\W{0,3}){0,6}?"
    r"(?:mismatch|not (?:allowed|registered|valid|match)|is invalid|invalid)",
    re.IGNORECASE,
)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_ERROR_TITLE_RE = re.compile(
    r"\b(error|not found|invalid|problem|went wrong|unavailable|denied)\b", re.IGNORECASE
)
_ERROR_PATH_RE = re.compile(r"(^|/)(error|errors|oops|not[-_]?found)(/|$)", re.IGNORECASE)
_LOGIN_EVIDENCE_RE = re.compile(
    r"sign[ -]?in|log[ -]?in|authori[sz]e|consent|allow access|continue with|password"
    r"|type=[\"']?(email|password)",
    re.IGNORECASE,
)
_FLOW_BY_AUTHENTICATION = {
    "oauth_authorization_code_pkce": OAuthAuthorizationFlow.AUTHORIZATION_CODE_PKCE,
    "oauth_device_code": OAuthAuthorizationFlow.DEVICE_CODE,
}
_SECRET_KEYS = {
    "client_secret",
    "registration_access_token",
    "access_token",
    "refresh_token",
    "id_token",
    "device_code",
}


# --------------------------------------------------------------------------
# Pure helpers (unit-tested in tests/scripts/test_preflight_shipped_oauth.py)
# --------------------------------------------------------------------------


def scope_check(manifest_scopes: tuple[str, ...], registration_scope: str | None) -> str:
    """Offline: do the manifest's scopes and the signed registration.scope agree?"""
    if registration_scope is None:
        return "n/a"
    registered = registration_scope.split()
    if not registered:
        return "FAIL registration.scope is empty"
    if len(registered) != len(set(registered)):
        return "FAIL registration.scope repeats a scope"
    if set(registered) != set(manifest_scopes):
        missing = sorted(set(manifest_scopes) - set(registered))
        extra = sorted(set(registered) - set(manifest_scopes))
        return f"FAIL manifest-only={missing} registration-only={extra}"
    return "ok"


def _clip(text: str, limit: int = 160) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def _query_error(url: str) -> str | None:
    parts = urlsplit(url)
    for component in (parts.query, parts.fragment):
        params = parse_qs(component)
        if "error" in params:
            error = params["error"][0]
            description = params.get("error_description", [""])[0]
            return f"error={error}" + (f" ({_clip(description, 200)})" if description else "")
    return None


def classify_authorize(status: int, location: str | None, body: str) -> tuple[bool, str]:
    """Classify one un-followed authorize response. ``(passed, detail)``."""
    if location is not None:
        error = _query_error(location)
        if error is not None:
            return False, f"{status} -> {_clip(location, 120)} {error}"
        lowered = location.lower()
        for code in OAUTH_ERROR_CODES:
            if code in lowered:
                return False, f"{status} -> {_clip(location, 200)} (carries {code})"
        if 300 <= status < 400:
            target = urlsplit(location)
            where = f"{status} -> {target.scheme}://{target.netloc}{target.path}"
            if _ERROR_PATH_RE.search(target.path):
                return False, f"{where} (an error page, not login or consent)"
            return True, where
    match = _ERROR_CODE_RE.search(body) or _REDIRECT_TEXT_RE.search(body)
    title_match = _TITLE_RE.search(body)
    title = _clip(title_match.group(1), 80) if title_match else ""
    if match is not None:
        start = max(0, match.start() - 80)
        snippet = _clip(body[start : match.end() + 120], 220)
        return False, f"{status} body names '{match.group(0)}': {snippet}"
    if status == 200:
        # A 200 is a PASS only with evidence of a login or consent page; a generic error
        # page, or a page that shows neither, is never reported as reaching consent.
        if _ERROR_TITLE_RE.search(title):
            return False, f"200 error page title={title!r}"
        if _LOGIN_EVIDENCE_RE.search(body):
            return True, f"200 page title={title!r}"
        return False, f"200 unconfirmed: no login or consent evidence (title={title!r})"
    return False, f"{status} title={title!r} body={_clip(body, 160)!r}"


MAX_REDIRECT_HOPS = 5


def follow_authorize(url: str, fetch) -> tuple[bool, str]:
    """Follow the authorize redirect chain (bounded) and classify where it lands.

    A PASS needs positive login or consent evidence on the page the chain ends at; a
    redirect alone proves nothing. Any error signal along the way fails, and so does a
    chain that does not settle within MAX_REDIRECT_HOPS.
    """
    from urllib.parse import urljoin

    hops: list[str] = []
    for _ in range(MAX_REDIRECT_HOPS + 1):
        status, location, body = fetch(url)
        if location is not None and 300 <= status < 400:
            passed, detail = classify_authorize(status, location, body)
            if not passed:
                return False, " -> ".join([*hops, detail])
            target = urlsplit(urljoin(url, location))
            hops.append(f"{status} -> {target.netloc}{target.path}")
            url = urljoin(url, location)
            continue
        passed, detail = classify_authorize(status, location, body)
        return passed, " -> ".join([*hops, detail])
    return False, " -> ".join([*hops, f"no page after {MAX_REDIRECT_HOPS} redirects"])


def pkce_pair() -> tuple[str, str]:
    """A verifier and its S256 challenge, as ``oauth_broker._pkce_challenge`` makes it."""
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    return verifier, challenge


# --------------------------------------------------------------------------
# Live probes
# --------------------------------------------------------------------------


class _Redirected(Exception):
    def __init__(self, status: int, location: str) -> None:
        self.status = status
        self.location = location


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, D102
        raise _Redirected(code, newurl)


def _raw_get(url: str) -> tuple[int, str | None, str]:
    opener = urllib.request.build_opener(_NoRedirect())
    request = urllib.request.Request(url, headers=BROWSER_HEADERS, method="GET")
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            body = response.read(MAX_BODY_BYTES)
            return response.status, None, body.decode("utf-8", "replace")
    except _Redirected as exc:
        return exc.status, exc.location, ""
    except urllib.error.HTTPError as exc:
        body = exc.read(MAX_BODY_BYTES).decode("utf-8", "replace")
        return exc.code, exc.headers.get("Location"), body


def _reason(exc: BaseException) -> str:
    reason = getattr(exc, "reason", None) or str(exc) or type(exc).__name__
    if isinstance(exc, OAuthNetworkError):
        return f"network:{reason}"
    if isinstance(exc, OAuthProviderError):
        return f"provider-refused:{reason}"
    if isinstance(exc, OAuthProtocolError):
        return f"omega-refused:{reason}"
    return f"{type(exc).__name__}:{reason}"


@dataclass
class Row:
    plugin_id: str
    profile_id: str = "-"
    mode: str = "-"
    scopes: str = "-"
    discovery: str = "-"
    registration: str = "-"
    authorize: str = "-"
    detail: str = ""

    @property
    def failed(self) -> bool:
        return any(
            value.startswith("FAIL")
            for value in (self.scopes, self.discovery, self.registration, self.authorize)
        )


async def probe(protocol: OAuthProtocolClient, registry, plugin_id, publisher, app) -> Row:
    row = Row(plugin_id=plugin_id)
    manifest_scopes = tuple(app.authentication.scopes)
    # The product's own resolution (plugin_lifecycle.profile_match).
    declared = OAuthProfileMatchInput(
        catalog_signer="omega.featured",
        plugin_publisher=publisher,
        app_publisher=app.publisher_id,
        provider_id=str(app.provider_id),
        authorization_issuer=str(app.authorization_issuer),
        resource_origin=str(app.remote_origin),
    )
    try:
        profile = registry.resolve_declared_app(declared)
        match = OAuthProfileMatchInput(
            catalog_signer=declared.catalog_signer,
            plugin_publisher=declared.plugin_publisher,
            app_publisher=declared.app_publisher,
            provider_id=declared.provider_id,
            authorization_issuer=profile.authorization_issuer,
            resource_origin=declared.resource_origin,
        )
        profile = registry.resolve(match)
        OAuthBroker._profile_route(profile)
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        row.discovery = f"FAIL profile: {_reason(exc)}"
        return row
    row.profile_id = profile.profile_id
    row.mode = profile.client_mode.value
    resource_origin = match.normalized().resource_origin
    registration_scope = None if profile.registration is None else profile.registration.scope
    row.scopes = scope_check(manifest_scopes, registration_scope)
    expected_flow = _FLOW_BY_AUTHENTICATION.get(str(app.authentication.kind))
    if expected_flow is not profile.authorization_flow:
        row.discovery = f"FAIL flow manifest={app.authentication.kind} profile={profile.authorization_flow.value}"
        return row
    # The broker asks for the sorted manifest scopes (vault.scope_union).
    scopes = tuple(sorted(manifest_scopes))

    try:
        discovery = await protocol.discover(profile, resource_origin=resource_origin)
        OAuthBroker._validate_discovery(profile, discovery, resource_origin)
    except Exception as exc:  # noqa: BLE001
        row.discovery = f"FAIL {_reason(exc)}"
        return row
    row.discovery = "ok"
    if discovery.scopes_supported is not None:
        unsupported = sorted(set(scopes) - set(discovery.scopes_supported))
        if unsupported:
            row.detail += f"scopes not in scopes_supported: {unsupported}; "

    # ---- device code (GitHub) --------------------------------------------
    if profile.authorization_flow is OAuthAuthorizationFlow.DEVICE_CODE:
        row.registration = "n/a"
        try:
            device = await protocol.begin_device_authorization(
                discovery, client_id=profile.client_id, scopes=scopes
            )
        except Exception as exc:  # noqa: BLE001
            row.authorize = f"FAIL device {_reason(exc)}"
            return row
        has_code = bool(device.user_code)
        row.authorize = "PASS device" if has_code else "FAIL device no user_code"
        row.detail += f"client_id={profile.client_id} verification_uri={device.verification_uri}"
        return row

    # ---- client identity, exactly as OAuthBroker.begin resolves it ---------
    registration = None
    if profile.client_mode is OAuthClientMode.DYNAMIC_REGISTRATION:
        try:
            registration = await protocol.register(discovery, profile=profile)
        except Exception as exc:  # noqa: BLE001
            # Report the original failure only: registering again for diagnostics could
            # create another client at the provider that nothing ever deregisters.
            row.registration = f"FAIL {_reason(exc)}"
            return row
        row.registration = "ok" + (" (confidential)" if registration.client_secret else "")
        credential = OAuthClientCredential(registration.client_id, registration.client_secret)
    elif profile.client_mode is OAuthClientMode.HOSTED_BRIDGE:
        row.registration = "bridge"
        # The product's own bridge client (the one _compose_oauth_broker wires) asks the
        # hosted bridge for the authorization URL, and the broker's own rule then holds it
        # to the exact signed request before anything is fetched, as a shipped unit does.
        from mcp_platform.oauth_broker import _authorization_request
        from mcp_platform.oauth_protocol import HttpHostedOAuthBridge

        state_digest = hashlib.sha256(secrets.token_urlsafe(32).encode("utf-8")).hexdigest()
        try:
            _verifier, challenge = pkce_pair()
            bridge_url = await HttpHostedOAuthBridge(registry.resolve_by_profile_id, protocol=protocol).begin(
                profile.profile_id, state_digest, challenge, tuple(scopes),
            )
            expected_url = protocol.build_authorization_url(
                discovery,
                OAuthAuthorizationParameters.for_profile(
                    profile,
                    credential=OAuthClientCredential(profile.client_id, None),
                    scopes=tuple(scopes),
                    state=state_digest,
                    code_challenge=challenge,
                    resource_origin=resource_origin,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            row.authorize = f"FAIL bridge-begin {_reason(exc)}"
            row.detail += _informational_bridge_probe(discovery, profile, scopes)
            return row
        if _authorization_request(bridge_url) != _authorization_request(expected_url):
            row.authorize = "FAIL bridge-url"
            row.detail += "the bridge's authorization URL is not the signed request the unit accepts"
            return row
        credential = None
    else:
        row.registration = "n/a"
        # Only client_id reaches an authorization URL; any vault secret does not.
        credential = OAuthClientCredential(profile.client_id, None)

    if profile.client_mode is OAuthClientMode.HOSTED_BRIDGE:
        url = bridge_url
    else:
        try:
            _verifier, challenge = pkce_pair()
            parameters = OAuthAuthorizationParameters.for_profile(
                profile,
                credential=credential,
                scopes=scopes,
                state=secrets.token_urlsafe(32),
                code_challenge=challenge,
                resource_origin=resource_origin,
            )
            url = protocol.build_authorization_url(discovery, parameters)
        except Exception as exc:  # noqa: BLE001
            row.authorize = f"FAIL build-url {_reason(exc)}"
            await _deregister(protocol, registration)
            return row
    try:
        passed, detail = await asyncio.to_thread(follow_authorize, url, _raw_get)
    except Exception as exc:  # noqa: BLE001
        passed, detail = False, f"request failed: {type(exc).__name__}: {_clip(str(exc), 120)}"
    row.authorize = "PASS" if passed else "FAIL"
    client_note = f"client_id={credential.client_id} " if registration is not None else ""
    row.detail += client_note + detail
    row.detail += await _deregister(protocol, registration)
    return row


def _informational_bridge_probe(discovery, profile, scopes) -> str:
    """What the vendor says to this client_id, if a bridge ever started it.

    A control request with a scope no provider issues is sent too: a vendor that
    answers both identically (Slack serves one script-rendered page for either)
    validates client-side, so its 200 proves nothing about the real request.
    """
    if discovery.authorization_endpoint is None:
        return "no authorization_endpoint"

    def fetch(scope: str) -> tuple[bool, str]:
        _verifier, challenge = pkce_pair()
        query = urlencode(
            {
                "client_id": profile.client_id,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "redirect_uri": profile.redirect_uri,
                "resource": discovery.resource_parameter,
                "response_type": "code",
                "scope": scope,
                "state": secrets.token_urlsafe(32),
            }
        )
        status, location, body = _raw_get(f"{discovery.authorization_endpoint}?{query}")
        return classify_authorize(status, location, body)

    try:
        passed, detail = fetch(" ".join(scopes))
        control_passed, _control = fetch("omega-preflight:bogus-scope")
    except Exception as exc:  # noqa: BLE001
        return f"[info vendor probe failed: {type(exc).__name__}]"
    verdict = "PASS" if passed else "FAIL"
    if passed and control_passed:
        verdict = "INCONCLUSIVE (a bogus-scope control also passes)"
    return f"[info: raw vendor authorize {verdict}: {detail}]"


async def _deregister(protocol: OAuthProtocolClient, registration) -> str:
    if registration is None:
        return ""
    if registration.registration_client_uri is None or registration.registration_access_token is None:
        return " | probe client left registered (no management token)"
    try:
        await protocol.deregister(
            registration.registration_client_uri,
            registration_access_token=registration.registration_access_token,
        )
    except Exception as exc:  # noqa: BLE001
        return f" | deregister failed: {_reason(exc)}"
    return " | probe client deregistered"


async def main(only: tuple[str, ...] = ()) -> int:
    """Probe every shipped plugin, or only the plugin ids given on the command line.

    Dynamic-registration probes leave an Omega client registered at the provider,
    so probing one plugin avoids re-registering at every other one.
    """
    ring = load_embedded_key_ring()
    catalog = verify_plugin_catalog(MARKETPLACE_CATALOG_PATH, ring)
    unknown = sorted(set(only) - {entry.plugin_id for entry in catalog.entries})
    if unknown:
        print(f"not in the shipped catalog: {', '.join(unknown)}", file=sys.stderr)
        return 2
    registry = OAuthProfileRegistry.from_path(core_paths.OAUTH_PROFILE_CATALOG_PATH, ring)
    protocol = OAuthProtocolClient()
    rows: list[Row] = []
    try:
        for entry in sorted(catalog.entries, key=lambda item: item.plugin_id):
            if only and entry.plugin_id not in only:
                continue
            verified = verify_plugin_package(PACKAGES / entry.plugin_id, ring, "3.0.0")
            manifest = verified.manifest
            apps = tuple(manifest.required_apps) + tuple(manifest.optional_apps)
            for app in apps:
                if not str(app.authentication.kind).startswith("oauth"):
                    continue
                label = entry.plugin_id if len(apps) == 1 else f"{entry.plugin_id}:{app.app_id}"
                row = await probe(protocol, registry, label, manifest.publisher_id, app)
                rows.append(row)
                print(f"  probed {row.plugin_id}", file=sys.stderr, flush=True)
    finally:
        await protocol.aclose()

    header = ("plugin_id", "profile_id", "mode", "scopes", "discovery", "registration", "authorize")
    widths = [max(len(h), *(len(str(getattr(r, h))) for r in rows)) for h in header]
    widths = [min(w, 48) for w in widths]
    print(" | ".join(h.ljust(w) for h, w in zip(header, widths)) + " | detail")
    print("-+-".join("-" * w for w in widths) + "-+-------")
    for row in rows:
        cells = [str(getattr(row, h))[:48].ljust(w) for h, w in zip(header, widths)]
        print(" | ".join(cells) + " | " + row.detail)
    failed = [row for row in rows if row.failed]
    print()
    print(f"SUMMARY: {len(rows)} OAuth apps probed | {len(rows) - len(failed)} pass | {len(failed)} fail")
    for row in failed:
        print(
            f"  FAIL {row.plugin_id}: scopes={row.scopes} discovery={row.discovery} "
            f"registration={row.registration} authorize={row.authorize}"
        )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(tuple(sys.argv[1:]))))
