"""Every shipped plugin, driven through the whole customer lifecycle.

The plugin set is read from the real signed marketplace catalog
(``plugin_platform/marketplace/catalog.json``) at collection time, so a plugin
added to the catalog is covered here without editing this file.

For each shipped plugin the journey below runs, in order, against ONE real
``compose_plugin_lifecycle_service`` over an isolated temporary state root:

    install -> consent offer -> authorization request -> connect
    -> ask-before-changes (a read runs, a write asks) -> run-directly + narrowing
    -> automatic update (retained consent) -> manual update (re-consent)
    -> uninstall

What is real: the signed catalog, every signed package, the embedded trust
ring, the signed OAuth profile catalog, the OAuth broker, credential vault,
grant/authority/permission stores, ``OAuthProtocolClient`` (it builds every
authorization URL and parses every provider document itself), the plugin
execution worker process, the runtime catalog compiler, AUTO's
``plan_and_execute_plugin_request`` and the ``RuntimeClient`` seam it uses in
production, catalog synchronisation, and the package artifact fetcher.

What is fake, and only at the network edge:

* the provider's authorization server -- an ``httpx2.MockTransport`` handed to
  the real ``OAuthProtocolClient``; it enforces PKCE S256, the signed
  dynamic-registration template, the exact redirect and client id, and echoes
  the requested scope exactly; for a ``hosted_bridge`` profile the same fake
  also answers the documented bridge API (docs/plugin-hosted-oauth-bridge.md)
  handed to the real ``HttpHostedOAuthBridge``;
* the provider's MCP server -- the worker's child handler answers discovery
  from the signed authority (the shared round-one fixture) and answers tool
  calls with a canned result, refusing any credential the fake authorization
  server did not issue;
* the marketplace's HTTPS origin -- the next release is served to the real
  ``sync_catalog`` / ``PackageArtifactFetcher`` through the same transport seam;
* the boot-owned transport loop -- the production submitter runs on a
  dedicated loop thread instead of the MCP host supervisor.

The next release of each REAL package is the same signed bytes with only the
version bumped, re-signed by an ephemeral key that is added to the embedded
trust ring as an ``omega.first_party`` key (the real private key is, correctly,
not available to tests). The catalog carrying it is re-signed the same way.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import hashlib
import io
import json
import os
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

import httpx2
import pytest
from jsonschema import Draft202012Validator, ValidationError
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from packaging.version import Version

import mcp_platform.plugin_lifecycle as plugin_lifecycle_module
import mcp_platform.oauth_protocol as oauth_protocol_module
import mcp_platform.service as mcp_service_module
from auto_loop.execute.plugin_plan import plan_and_execute_plugin_request
from core import paths as core_paths
from memory import episode_sram
from mcp_platform.canonical import canonical_bytes
from mcp_platform.oauth_profiles import (
    OAUTH_LOOPBACK_CALLBACK_ORIGIN,
    OAUTH_REDIRECT_RELAY_ORIGIN,
    OAuthClientMode,
    OAuthProfileRegistry,
)
from mcp_platform.oauth_protocol import OAuthProtocolClient
from mcp_platform.package_contracts import RiskClass
from mcp_platform.package_fetch import PackageArtifactFetcher
from mcp_platform.plugin_planning import validate_plugin_argument_provenance
from mcp_platform.plugin_lifecycle import (
    PluginUpdatePending,
    compose_plugin_lifecycle_service,
)
from mcp_platform.plugin_state import PluginLifecycleState
from plugin_platform.catalog import verify_plugin_catalog
from plugin_platform.catalog_sync import CatalogFetchResponse
from plugin_platform.package_verifier import verify_plugin_package
from plugin_platform.records import PluginRegistryError
from plugin_platform.trust import TrustedPublisherKeyRing, load_embedded_key_ring
from runtime.client import RuntimeClient
from tests.helpers.plugin_consent import full_permission_settings


_MARKETPLACE_ROOT = core_paths.OMEGA_ROOT / "plugin_platform" / "marketplace"
_CATALOG_PATH = _MARKETPLACE_ROOT / "catalog.json"
_TRUST_RING_PATH = core_paths.OMEGA_ROOT / "plugin_platform" / "trusted_plugin_keys.json"
_OMEGA_VERSION = "3.0.0"
_ACCOUNT_KEY = "matrix-operator-account"
_ACCESS_PREFIX = "matrix-access-"
_CALL_LOG_ENV = "OMEGA_TEST_PLUGIN_MATRIX_CALL_LOG"
_RELEASE_KEY_ID = "matrix-test-release"
_OPERATOR_COMMAND = {"source": "operator", "type": "command"}


def _shipped_plugin_ids() -> tuple[str, ...]:
    """The shipped plugin set, from the real signed catalog -- never a list here."""

    catalog = verify_plugin_catalog(_CATALOG_PATH, load_embedded_key_ring())
    return tuple(sorted(entry.plugin_id for entry in catalog.entries))


SHIPPED_PLUGIN_IDS = _shipped_plugin_ids()


# --------------------------------------------------------------------------- #
# The provider's MCP server: runs inside the real plugin execution worker.
# --------------------------------------------------------------------------- #


def _matrix_provider_handler(body: dict[str, Any], credential: dict[str, Any]) -> dict[str, Any]:
    """Synthetic provider behind the production worker envelope.

    Discovery answers from the signed authority; a tool call is recorded and
    answered with a canned result. Both refuse a credential the fake
    authorization server never issued, so a token that did not flow through the
    real broker/vault cannot produce a success.
    """

    from tests.mcp_platform.plugin_execution_worker_round1_fixture import discovery_handler

    token = str(credential.get("access_token") or "")
    if not token.startswith(_ACCESS_PREFIX):
        raise ValueError("matrix_provider_credential_not_issued")
    if body.get("discovery") is True:
        return discovery_handler(body, credential)
    request = body["request"]
    log_path = os.environ.get(_CALL_LOG_ENV)
    if log_path:
        with open(log_path, "a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "app_id": request["app_id"],
                        "capability_id": request["capability_id"],
                        "arguments": request.get("arguments"),
                        "token": token,
                    },
                    sort_keys=True,
                )
                + "\n"
            )
    return {
        "request_id": request["request_id"],
        "app_id": request["app_id"],
        "capability_id": request["capability_id"],
        "capability_revision": request["capability_revision"],
        "status": "completed",
        "data": {
            "content": [{"type": "text", "text": "matrix provider result"}],
            "structured_content": None,
            "is_error": False,
        },
    }


# --------------------------------------------------------------------------- #
# The provider's authorization server (network edge of the real protocol client).
# --------------------------------------------------------------------------- #


def _s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class _FakeAuthorizationServer:
    """One provider's authorization server for one signed OAuth profile."""

    def __init__(self, profile: Any) -> None:
        self.profile = profile
        issuer = urlsplit(profile.authorization_issuer)
        self.origin = f"{issuer.scheme}://{issuer.netloc}"
        self.requests: list[tuple[str, str]] = []
        self.registrations: list[dict[str, Any]] = []
        self.registered_client_ids: list[str] = []
        self.revoked_tokens: list[str] = []
        self.issued_access_tokens: list[str] = []
        self.issued_refresh_tokens: list[str] = []
        self.device_requests: list[dict[str, str]] = []
        self._codes: dict[str, dict[str, str]] = {}
        self.bridge_requests: list[tuple[str, dict[str, Any]]] = []
        self._bridge_begins: dict[str, dict[str, str]] = {}
        self._bridge_handles: dict[str, dict[str, str]] = {}
        self._devices: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._counter = 0

    @property
    def device_flow(self) -> bool:
        return self.profile.authorization_flow.value == "device_code"

    def _next(self) -> int:
        with self._lock:
            self._counter += 1
            return self._counter

    def metadata(self) -> dict[str, Any]:
        grants = ["authorization_code", "refresh_token"]
        document: dict[str, Any] = {
            "issuer": self.profile.authorization_issuer,
            "token_endpoint": f"{self.origin}/oauth/token",
            "revocation_endpoint": f"{self.origin}/oauth/revoke",
            "token_endpoint_auth_methods_supported": ["none"],
            "authorization_response_iss_parameter_supported": True,
        }
        if self.device_flow:
            grants.append("urn:ietf:params:oauth:grant-type:device_code")
            document["device_authorization_endpoint"] = f"{self.origin}/oauth/device"
        else:
            document["authorization_endpoint"] = f"{self.origin}/oauth/authorize"
            document["response_types_supported"] = ["code"]
            document["code_challenge_methods_supported"] = ["S256"]
        if self.profile.client_mode is OAuthClientMode.DYNAMIC_REGISTRATION:
            document["registration_endpoint"] = f"{self.origin}/oauth/register"
        document["grant_types_supported"] = grants
        return document

    def _token(self, scope: str) -> dict[str, Any]:
        number = self._next()
        access = f"{_ACCESS_PREFIX}{number}"
        refresh = f"matrix-refresh-{number}"
        self.issued_access_tokens.append(access)
        self.issued_refresh_tokens.append(refresh)
        return {
            "access_token": access,
            "refresh_token": refresh,
            "token_type": "Bearer",
            "expires_in": 3600,
            # Echo exactly what the product asked for.
            "scope": scope,
        }

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        path = request.url.path
        self.requests.append((request.method, url))
        if path.startswith("/bridge/v1/"):
            return self._bridge(request)
        if request.method == "GET" and path.startswith("/.well-known/oauth-protected-resource"):
            return httpx2.Response(
                200,
                json={
                    "resource": self._resource_for(request),
                    "authorization_servers": [self.profile.authorization_issuer],
                },
            )
        if request.method == "GET" and path.startswith("/.well-known/oauth-authorization-server"):
            if f"{request.url.scheme}://{request.url.host}" != self.origin:
                return httpx2.Response(404)
            return httpx2.Response(200, json=self.metadata())
        if f"{request.url.scheme}://{request.url.host}" != self.origin:
            return httpx2.Response(404)
        if request.method == "POST" and path == "/oauth/register":
            body = json.loads(request.content.decode("utf-8"))
            self.registrations.append(body)
            if body != self.profile.registration.to_dict():
                return httpx2.Response(400, json={"error": "invalid_client_metadata"})
            client_id = f"matrix-dcr-client-{self._next()}"
            self.registered_client_ids.append(client_id)
            return httpx2.Response(
                201, json={"client_id": client_id, "token_endpoint_auth_method": "none"}
            )
        form = {key: values[0] for key, values in parse_qs(request.content.decode("ascii")).items()}
        if request.method == "POST" and path == "/oauth/device":
            self.device_requests.append(form)
            device_code = f"matrix-device-{self._next()}"
            self._devices[device_code] = {
                "client_id": form.get("client_id"),
                "scope": form.get("scope", ""),
                "approved": False,
            }
            return httpx2.Response(
                200,
                json={
                    "device_code": device_code,
                    "user_code": "MTRX-0001",
                    "verification_uri": f"{self.origin}/login/device",
                    "expires_in": 600,
                    "interval": 1,
                },
            )
        if request.method == "POST" and path == "/oauth/token":
            grant = form.get("grant_type")
            if grant == "authorization_code":
                pending = self._codes.pop(form.get("code", ""), None)
                if (
                    pending is None
                    or form.get("client_id") != pending["client_id"]
                    or form.get("redirect_uri") != pending["redirect_uri"]
                    or _s256(form.get("code_verifier", "")) != pending["code_challenge"]
                ):
                    return httpx2.Response(400, json={"error": "invalid_grant"})
                return httpx2.Response(200, json=self._token(pending["scope"]))
            if grant == "urn:ietf:params:oauth:grant-type:device_code":
                device = self._devices.get(form.get("device_code", ""))
                if device is None or device["client_id"] != form.get("client_id"):
                    return httpx2.Response(400, json={"error": "invalid_grant"})
                if not device["approved"]:
                    return httpx2.Response(400, json={"error": "authorization_pending"})
                self._devices.pop(form["device_code"])
                return httpx2.Response(200, json=self._token(device["scope"]))
            if grant == "refresh_token":
                return httpx2.Response(200, json=self._token(form.get("scope", "")))
            return httpx2.Response(400, json={"error": "unsupported_grant_type"})
        if request.method == "POST" and path == "/oauth/revoke":
            self.revoked_tokens.append(form.get("token", ""))
            return httpx2.Response(200)
        return httpx2.Response(404)

    def _resource_for(self, request: httpx2.Request) -> str:
        # The document names the signed resource identifier the product asked
        # about; the product then compares it exactly.
        for origin in self.profile.resource_origins:
            identifier = self.profile.identifier_for(origin)
            parsed = urlsplit(identifier)
            suffix = "" if parsed.path == "/" else parsed.path
            if (
                request.url.host == parsed.hostname
                and request.url.path == "/.well-known/oauth-protected-resource" + suffix
            ):
                return identifier
        return "unknown-resource"

    # -- hosted bridge (docs/plugin-hosted-oauth-bridge.md), for hosted_bridge profiles --

    def _bridge(self, request: httpx2.Request) -> httpx2.Response:
        redirect = urlsplit(self.profile.redirect_uri or "")
        prefix = f"/bridge/v1/{self.profile.profile_id}/"
        if (
            self.profile.client_mode is not OAuthClientMode.HOSTED_BRIDGE
            or f"{request.url.scheme}://{request.url.host}" != f"{redirect.scheme}://{redirect.netloc}"
            or not request.url.path.startswith(prefix)
            or request.method != "POST"
        ):
            return httpx2.Response(404, json={"error": "unknown_profile"})
        operation = request.url.path.removeprefix(prefix)
        body = json.loads(request.content.decode("utf-8"))
        self.bridge_requests.append((operation, body))
        if operation == "begin":
            scopes = sorted(body["scopes"])
            self._bridge_begins[body["state_digest"]] = {
                "code_challenge": body["pkce_challenge"],
                "scope": " ".join(scopes),
            }
            query = {
                "client_id": self.profile.client_id,
                "code_challenge": body["pkce_challenge"],
                "code_challenge_method": "S256",
                "redirect_uri": self.profile.redirect_uri,
                # The signed resource identifier discovery names, as the real bridge sends
                # (and as the unit, which rebuilds this request, requires).
                "resource": self.profile.identifier_for(self.profile.resource_origins[0]),
                "response_type": "code",
                "scope": " ".join(scopes),
                "state": body["state_digest"],
            }
            return httpx2.Response(
                200, json={"authorization_url": f"{self.origin}/oauth/authorize?{urlencode(query)}"}
            )
        if operation == "redeem":
            pending = self._bridge_handles.pop(body.get("completion_handle", ""), None)
            if (
                pending is None
                or pending["state_digest"] != body.get("state_digest")
                or _s256(body.get("pkce_verifier", "")) != pending["code_challenge"]
            ):
                return httpx2.Response(400, json={"error": "invalid_grant"})
            return httpx2.Response(200, json=self._token(pending["scope"]))
        if operation == "refresh":
            return httpx2.Response(200, json=self._token(""))
        if operation == "revoke":
            self.revoked_tokens.append(body.get("refresh_token", ""))
            return httpx2.Response(
                200, json={"endpoint_advertised": False, "provider_request_sent": False}
            )
        return httpx2.Response(404, json={"error": "unknown_operation"})

    def authorize(self, authorization_url: str) -> str:
        """The browser + the user's consent: return the callback the browser lands on."""

        query = {key: values[0] for key, values in parse_qs(urlsplit(authorization_url).query).items()}
        if self.profile.client_mode is OAuthClientMode.HOSTED_BRIDGE:
            # The provider redirects to the bridge, which seals the code into a
            # completion handle and 302s to the loopback HUD.
            begun = self._bridge_begins.pop(query["state"])
            handle = f"matrix-handle-{self._next()}"
            self._bridge_handles[handle] = {**begun, "state_digest": query["state"]}
            redirect = urlsplit(self.profile.redirect_uri)
            return f"{OAUTH_LOOPBACK_CALLBACK_ORIGIN}{redirect.path}?" + urlencode(
                {
                    "state": query["state"],
                    "completion_handle": handle,
                    "iss": self.profile.authorization_issuer,
                }
            )
        code = f"matrix-code-{self._next()}"
        self._codes[code] = {
            "client_id": query["client_id"],
            "redirect_uri": query["redirect_uri"],
            "code_challenge": query["code_challenge"],
            "scope": query.get("scope", ""),
        }
        redirect = urlsplit(query["redirect_uri"])
        landing = (
            f"{OAUTH_LOOPBACK_CALLBACK_ORIGIN}{redirect.path}"
            if f"{redirect.scheme}://{redirect.netloc}" == OAUTH_REDIRECT_RELAY_ORIGIN
            else query["redirect_uri"]
        )
        return f"{landing}?" + urlencode(
            {"code": code, "state": query["state"], "iss": self.profile.authorization_issuer}
        )

    def approve_device(self) -> None:
        for device in self._devices.values():
            device["approved"] = True


# --------------------------------------------------------------------------- #
# Signing the next release of a real package.
# --------------------------------------------------------------------------- #


def _extended_ring(private_key: Ed25519PrivateKey) -> TrustedPublisherKeyRing:
    raw = json.loads(_TRUST_RING_PATH.read_bytes())
    raw["keys"].append(
        {
            "key_id": _RELEASE_KEY_ID,
            "publisher_id": "omega.first_party",
            "public_key_base64": base64.b64encode(
                private_key.public_key().public_bytes(
                    serialization.Encoding.Raw, serialization.PublicFormat.Raw
                )
            ).decode("ascii"),
        }
    )
    return TrustedPublisherKeyRing.from_dict(raw)


def _signed(document: dict[str, Any], private_key: Ed25519PrivateKey) -> bytes:
    unsigned = {key: value for key, value in document.items() if key != "signature"}
    unsigned["signing_key_id"] = _RELEASE_KEY_ID
    signature = base64.b64encode(private_key.sign(canonical_bytes(unsigned))).decode("ascii")
    return canonical_bytes({**unsigned, "signature": signature})


@dataclass
class _Release:
    version: str
    catalog_bytes: bytes
    archive: bytes
    package_uri: str


def _next_release(
    plugin_id: str,
    *,
    version: str,
    sequence: int,
    base_catalog: dict[str, Any],
    history: list[dict[str, str]],
    private_key: Ed25519PrivateKey,
    ring: TrustedPublisherKeyRing,
    workdir: Path,
) -> _Release:
    """Re-sign the REAL package at ``version`` and a catalog listing it."""

    source = _MARKETPLACE_ROOT / "packages" / plugin_id
    package = workdir / f"{plugin_id}-{version}"
    files: dict[str, bytes] = {}
    for path in sorted(source.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            files[path.relative_to(source).as_posix()] = path.read_bytes()
    manifest = json.loads(files["plugin.json"])
    manifest["plugin_version"] = version
    files["plugin.json"] = _signed(manifest, private_key)
    for relative, payload in files.items():
        target = package / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    verified = verify_plugin_package(package, ring, _OMEGA_VERSION)
    assert verified.manifest.plugin_version == version

    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for relative, payload in files.items():
            bundle.writestr(f"{plugin_id}/{relative}", payload)

    package_uri = (
        f"https://plugins.omega2ai.com/marketplace/packages/{plugin_id}/{version}"
    )
    catalog = json.loads(json.dumps(base_catalog))
    catalog["sequence"] = sequence
    for entry in catalog["entries"]:
        if entry["plugin_id"] != plugin_id:
            continue
        entry["plugin_version"] = version
        entry["manifest_sha256"] = verified.manifest_sha256
        entry["package_sha256"] = verified.package_sha256
        entry["package_uri"] = package_uri
        entry["release_history"] = list(history)
    catalog_bytes = _signed(catalog, private_key)
    catalog_path = workdir / f"catalog-{sequence}.json"
    catalog_path.write_bytes(catalog_bytes)
    verify_plugin_catalog(catalog_path, ring)
    return _Release(version, catalog_bytes, archive.getvalue(), package_uri)


# --------------------------------------------------------------------------- #
# The boot-owned transport loop, substituted.
# --------------------------------------------------------------------------- #


class _TransportLoop:
    """One forever loop, as the MCP host supervisor owns in production."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def submit_plugin_operation_sync(self, operation: Callable[[], Any], *, timeout_s: float | None = None) -> Any:
        future = asyncio.run_coroutine_threadsafe(operation(), self.loop)
        return future.result(timeout=300.0 if timeout_s is None else timeout_s)

    def close(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=10.0)


# --------------------------------------------------------------------------- #
# Journey state shared by the ordered steps of one plugin.
# --------------------------------------------------------------------------- #


@dataclass
class _Journey:
    plugin_id: str
    root: Path
    service: Any
    server: _FakeAuthorizationServer
    profile: Any
    app: Any
    base_catalog: dict[str, Any]
    private_key: Ed25519PrivateKey
    ring: TrustedPublisherKeyRing
    call_log: Path
    failed_step: str | None = None
    facts: dict[str, Any] = field(default_factory=dict)

    @property
    def app_id(self) -> str:
        return self.app.app_id

    def submit(self, operation: Callable[[], Any]) -> Any:
        return self.service._operation_submitter(operation, timeout_s=300.0)

    def calls(self) -> list[dict[str, Any]]:
        if not self.call_log.exists():
            return []
        return [json.loads(line) for line in self.call_log.read_text("utf-8").splitlines() if line]

    def served(self) -> tuple[Any, ...]:
        return tuple(
            item
            for item in self.service.runtime_catalog().tools
            if item.plugin_id == self.plugin_id
        )


def _signed_profile_for(app: Any, ring: TrustedPublisherKeyRing) -> Any:
    """The test's own oracle: the one signed profile for this app's provider."""

    document = json.loads(core_paths.OAUTH_PROFILE_CATALOG_PATH.read_bytes())
    matches = [
        item
        for item in document["profiles"]
        if item["provider_id"] == app.provider_id and app.remote_origin in item["resource_origins"]
    ]
    assert len(matches) == 1, f"{app.app_id}: {len(matches)} signed OAuth profiles"
    registry = OAuthProfileRegistry.from_path(core_paths.OAUTH_PROFILE_CATALOG_PATH, ring)
    return registry.resolve_by_profile_id(matches[0]["profile_id"])


def _no_network(self: object, request: httpx2.Request) -> httpx2.Response:
    raise AssertionError(f"real network request attempted: {request.method} {request.url}")


@pytest.fixture(scope="class", params=SHIPPED_PLUGIN_IDS)
def journey(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory):
    plugin_id: str = request.param
    root = tmp_path_factory.mktemp(f"matrix-{plugin_id}")
    private_key = Ed25519PrivateKey.generate()
    ring = _extended_ring(private_key)
    shipped = verify_plugin_package(
        _MARKETPLACE_ROOT / "packages" / plugin_id, load_embedded_key_ring(), _OMEGA_VERSION
    )
    apps = shipped.manifest.required_apps + shipped.manifest.optional_apps
    assert len(apps) == 1, f"{plugin_id} ships {len(apps)} apps; the matrix drives one"
    app = apps[0]
    profile = _signed_profile_for(app, load_embedded_key_ring())
    server = _FakeAuthorizationServer(profile)
    transport = httpx2.MockTransport(server.handle)
    loop = _TransportLoop()
    call_log = root / "provider-calls.jsonl"

    patcher = pytest.MonkeyPatch()
    patcher.setenv("OMEGA_OPERATOR_ACCOUNT_KEY", _ACCOUNT_KEY)
    patcher.setenv(_CALL_LOG_ENV, str(call_log))
    patcher.setattr(plugin_lifecycle_module, "load_embedded_key_ring", lambda: ring)
    patcher.setattr(
        plugin_lifecycle_module,
        "OAuthProtocolClient",
        lambda: OAuthProtocolClient(transport=transport),
    )
    patcher.setattr(plugin_lifecycle_module, "execute_mcp_operation", _matrix_provider_handler)
    hosted_bridge = oauth_protocol_module.HttpHostedOAuthBridge
    patcher.setattr(
        oauth_protocol_module,
        "HttpHostedOAuthBridge",
        lambda profile_for, **_kwargs: hosted_bridge(
            profile_for, protocol=OAuthProtocolClient(transport=transport)
        ),
    )
    # No network, ever: any client that escaped the fakes fails loudly.
    patcher.setattr(httpx2.AsyncHTTPTransport, "handle_async_request", _no_network)
    patcher.setattr(httpx2.HTTPTransport, "handle_request", _no_network)
    patcher.setattr(
        mcp_service_module,
        "get_default_mcp_host",
        lambda: SimpleNamespace(supervisor=loop),
    )
    service = None
    try:
        service = compose_plugin_lifecycle_service(
            state_root=root / "state",
            oauth_key_provider=lambda: b"m" * 32,
        )
        # The marketplace origin, at the same seams sync and fetch own.
        service._catalog_fetch = lambda: (_ for _ in ()).throw(
            AssertionError("no release published yet")
        )
        state = _Journey(
            plugin_id=plugin_id,
            root=root,
            service=service,
            server=server,
            profile=profile,
            app=app,
            base_catalog=json.loads(_CATALOG_PATH.read_bytes()),
            private_key=private_key,
            ring=ring,
            call_log=call_log,
        )
        yield state
    finally:
        if service is not None:
            service.close()
        loop.close()
        patcher.undo()


def _step(method: Callable[..., None], *, blocking: bool = True) -> Callable[..., None]:
    """Run one ordered journey step; a failed blocking step blocks the ones after it."""

    @functools.wraps(method)
    def run(self: Any, journey: _Journey, *args: Any, **kwargs: Any) -> None:
        if journey.failed_step is not None:
            pytest.skip(f"blocked: {journey.plugin_id} failed at {journey.failed_step}")
        try:
            method(self, journey, *args, **kwargs)
        except pytest.skip.Exception:
            raise
        except BaseException:
            if blocking:
                journey.failed_step = method.__name__
            raise

    return run


def _independent_step(method: Callable[..., None]) -> Callable[..., None]:
    """A step whose failure is about itself and leaves the journey's state intact."""

    return _step(method, blocking=False)


# --------------------------------------------------------------------------- #
# Helpers that walk the product's own surfaces.
# --------------------------------------------------------------------------- #


def _permissions(journey: _Journey) -> dict[str, Any]:
    return journey.submit(
        lambda: journey.service.permissions(journey.plugin_id, journey.app_id)
    )


def _authorize(journey: _Journey, started: Any) -> Any:
    """Complete a started connection the way the user's browser/device does."""

    service = journey.service
    if getattr(started, "status", None) == "connected":
        return started
    if started.authorization_flow == "device_code":
        journey.server.approve_device()
        deadline = time.monotonic() + 30.0
        while True:
            try:
                status = journey.submit(lambda: service.poll_connection(started.request_id))
            except ValueError as exc:
                if str(exc) not in {
                    "oauth_device_authorization_pending",
                    "oauth_device_slow_down",
                    "oauth_device_poll_too_soon",
                } or time.monotonic() > deadline:
                    raise
                time.sleep(1.1)
                continue
            if getattr(status, "status", None) == "finalizing" and time.monotonic() < deadline:
                time.sleep(0.2)
                continue
            return status
    callback = journey.server.authorize(started.authorization_url)
    return journey.submit(
        lambda: service.complete_connection(callback, request_id=started.request_id)
    )


def _schema_value(schema: Any) -> Any:
    """A minimal value the signed input schema admits."""

    if not isinstance(schema, Mapping):
        return "matrix"
    if "const" in schema:
        return schema["const"]
    if schema.get("enum"):
        return schema["enum"][0]
    for combinator in ("anyOf", "oneOf", "allOf"):
        if schema.get(combinator):
            return _schema_value(schema[combinator][0])
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((item for item in kind if item != "null"), "string")
    if kind == "object":
        properties = schema.get("properties") or {}
        return {name: _schema_value(properties.get(name, {})) for name in schema.get("required", [])}
    if kind == "array":
        return [_schema_value(schema.get("items", {})) for _ in range(int(schema.get("minItems", 0) or 0))]
    if kind == "integer":
        return max(int(schema.get("minimum", 1)), 1)
    if kind == "number":
        return max(float(schema.get("minimum", 1)), 1.0)
    if kind == "boolean":
        return True
    return "matrix"


def _literal_arguments(binding: Any) -> dict[str, Any] | None:
    """Arguments an operator's words could supply literally, or None.

    The planner refuses literal values for identifier-like fields (file, url,
    path, ...): those must come from an earlier step's output. A tool that
    needs one cannot be the single step of a direct request, so it is not a
    candidate for these probes.
    """

    arguments = _schema_value(binding.input_schema)
    try:
        validate_plugin_argument_provenance(arguments)
        Draft202012Validator(_plain(binding.input_schema)).validate(arguments)
    except (ValueError, ValidationError):
        return None
    return arguments


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _simplest(
    bindings: tuple[Any, ...], *, read_only: bool, exclude: frozenset[str] = frozenset()
) -> Any | None:
    """The simplest served tool of one kind that a direct request can call."""

    matches = [
        item
        for item in bindings
        if (item.risk_class is RiskClass.READ_ONLY) is read_only
        and not item.requires_revision
        and item.capability not in exclude
        and _literal_arguments(item) is not None
    ]
    if not matches:
        return None
    return sorted(
        matches,
        key=lambda item: (len(item.input_schema.get("required", []) or []), item.capability),
    )[0]


def _served_binding(journey: "_Journey", capability: str) -> Any:
    return next(item for item in journey.served() if item.capability == capability)


def _dispatch(
    journey: _Journey,
    monkeypatch: pytest.MonkeyPatch,
    *,
    binding: Any | None = None,
    explicit_operation: Mapping[str, object] | None = None,
    grant: Any | None = None,
    plan_override: Any | None = None,
) -> Any:
    """Run one plugin action through AUTO's production plugin path.

    As in production, the action runs inside an open cognition episode bound to
    its pipeline thread: direct-turn authority is issued only to the episode
    that is actually running.
    """

    monkeypatch.setattr(plugin_lifecycle_module, "_DEFAULT_SERVICE", journey.service)
    runtime_client = RuntimeClient()
    episode_id = f"matrix-episode-{uuid4().hex}"
    # The previous action's episode is not this test's subject; DeepSleep would
    # have closed it. Start from an empty SRAM as the next episode would.
    episode_sram.reset()
    episode_sram.open_bucket(episode_id, pipeline_id=0)
    with episode_sram.bind_episode(episode_id, pipeline_id=0):
        if plan_override is not None:
            return plan_and_execute_plugin_request(
                "__approved_plugin_plan__",
                episode_id=episode_id,
                runtime_client=runtime_client,
                grant=grant,
                plan_override=plan_override,
                work_kind="operator_turn",
                active_contributor=_OPERATOR_COMMAND,
            )
        if explicit_operation is None:
            assert binding is not None
            explicit_operation = {
                "plugin_id": binding.plugin_id,
                "app_id": binding.app_id,
                "capability": binding.capability,
                "arguments": _literal_arguments(binding),
            }
        return plan_and_execute_plugin_request(
            f"Use {journey.plugin_id} to run {explicit_operation['capability']}",
            episode_id=episode_id,
            runtime_client=runtime_client,
            explicit_operation=explicit_operation,
            work_kind="operator_turn",
            active_contributor=_OPERATOR_COMMAND,
        )


def _asks_then_executes_once(
    journey: _Journey, monkeypatch: pytest.MonkeyPatch, binding: Any
) -> None:
    before = len(journey.calls())
    asked = _dispatch(journey, monkeypatch, binding=binding)
    assert asked.status == "permission_required", (
        f"{binding.capability} ({binding.risk_class.value}) under 'ask before changes' "
        f"returned {asked.status!r} ({asked.failure_reason!r}) instead of asking"
    )
    assert asked.permission_request is not None
    assert len(journey.calls()) == before, "the provider was called before the user approved"
    store = journey.service.permission_store
    request = asked.permission_request
    grant = store.approve(request.request_id, expected_revision=request.revision)
    plan = store.plan_for_grant(grant.grant_id)
    approved = _dispatch(journey, monkeypatch, grant=grant, plan_override=plan)
    assert approved.status == "completed", approved.failure_reason
    calls = journey.calls()[before:]
    assert [call["capability_id"] for call in calls] == [f"tool:{binding.capability}"]
    replay = _dispatch(journey, monkeypatch, grant=grant, plan_override=plan)
    assert replay.status != "completed", "one approval executed twice"
    assert len(journey.calls()) == before + 1


# --------------------------------------------------------------------------- #
# The matrix.
# --------------------------------------------------------------------------- #


class TestShippedPluginLifecycle:
    @_step
    def test_01_install_from_signed_catalog(self, journey: _Journey) -> None:
        """Review then install the real signed package with the review's confirmation."""

        service = journey.service
        review = service.review(journey.plugin_id)
        entry = next(
            item for item in journey.base_catalog["entries"] if item["plugin_id"] == journey.plugin_id
        )
        service.install(journey.plugin_id, confirmation=review.sha256)
        record = service.plugin_registry.get(journey.plugin_id)
        assert record.selected_version == entry["plugin_version"]
        assert service.plugin_registry.reverify_installed(journey.plugin_id).package_sha256 == (
            entry["package_sha256"]
        )
        assert service.app_registry.get(journey.app_id).app_id == journey.app_id
        # Installed is not connected: nothing is served before consent.
        assert record.enabled is False
        assert journey.served() == ()

    @_step
    def test_02_consent_offers_the_signed_scopes_and_tools(self, journey: _Journey) -> None:
        """permissions() offers exactly the manifest's scopes and signed tools."""

        selection = _permissions(journey)
        offered = selection["offered"]
        assert offered["provider_scopes"] == list(journey.app.authentication.scopes)
        authority = json.loads(
            (_MARKETPLACE_ROOT / "packages" / journey.plugin_id / "tools" / "authority.json").read_bytes()
        )
        signed_tools = {f"tool:{name}" for name in authority["apps"][journey.app_id]["tools"]}
        assert set(offered["capability_ids"]) == signed_tools
        assert offered["execution_policies"] == ["direct_turn", "confirm_each"]
        # Conservative default: nothing selected, ask before changes.
        assert selection["selected"]["provider_scopes"] == []
        assert selection["selected"]["capability_ids"] == []
        assert selection["selected"]["execution_policy"] == "confirm_each"
        journey.facts["selection"] = selection

    @_step
    def test_03_authorization_request_matches_the_signed_profile(self, journey: _Journey) -> None:
        """start_connection builds the exact request the signed profile dictates."""

        profile = journey.profile
        app = journey.app
        selection = journey.facts["selection"]
        settings = full_permission_settings(selection)
        settings["execution_policy"] = "confirm_each"
        started = journey.submit(
            lambda: journey.service.start_connection(
                journey.plugin_id,
                journey.app_id,
                revision=selection["revision"],
                settings=settings,
            )
        )
        journey.facts["started"] = started
        journey.facts["confirm_settings"] = settings
        assert started.status == "connecting"
        manifest_scopes = set(app.authentication.scopes)
        assert profile.authorization_issuer.rstrip("/") == str(app.authorization_issuer).rstrip("/")
        # Discovery went to the signed issuer's metadata, nowhere else.
        issuer_host = urlsplit(profile.authorization_issuer).netloc
        metadata_hosts = {
            urlsplit(url).netloc
            for method, url in journey.server.requests
            if "/.well-known/oauth-authorization-server" in url
        }
        assert metadata_hosts == {issuer_host}
        if profile.client_mode is OAuthClientMode.DYNAMIC_REGISTRATION:
            assert profile.registration is not None
            assert set(profile.registration.scope.split()) == manifest_scopes
            assert journey.server.registrations == [profile.registration.to_dict()]
        if started.authorization_flow == "device_code":
            assert profile.redirect_uri is None
            # The page to open is the provider's device page, never a redirect.
            assert started.authorization_url == started.verification_uri
            assert started.user_code and started.verification_uri
            assert profile.client_mode is OAuthClientMode.PREREGISTERED
            (device_request,) = journey.server.device_requests
            assert device_request["client_id"] == profile.client_id
            assert set(device_request["scope"].split()) == manifest_scopes
            return
        if profile.client_mode is OAuthClientMode.HOSTED_BRIDGE:
            # The unit holds no provider secret: it asks the bridge at the origin
            # of its signed relay redirect to begin, keeping PKCE and state local.
            assert profile.client_secret_ref is None
            assert profile.redirect_uri == (
                f"{OAUTH_REDIRECT_RELAY_ORIGIN}/oauth/{profile.profile_id}/callback"
            )
            begins = [body for operation, body in journey.server.bridge_requests if operation == "begin"]
            assert len(begins) == 1, journey.server.bridge_requests
            (begin,) = begins
            bridge_calls = [
                url for method, url in journey.server.requests if "/bridge/v1/" in url
            ]
            assert bridge_calls == [
                f"{OAUTH_REDIRECT_RELAY_ORIGIN}/bridge/v1/{profile.profile_id}/begin"
            ]
            assert len(begin["state_digest"]) == 64
            assert set(begin["state_digest"]) <= set("0123456789abcdef")
            assert 43 <= len(begin["pkce_challenge"]) <= 128
            assert set(begin["scopes"]) == manifest_scopes
            assert len(begin["scopes"]) == len(manifest_scopes)
            return
        query = {
            key: values[0]
            for key, values in parse_qs(urlsplit(started.authorization_url).query).items()
        }
        assert started.authorization_url.startswith(journey.server.metadata()["authorization_endpoint"] + "?")
        if profile.client_mode is OAuthClientMode.DYNAMIC_REGISTRATION:
            assert query["client_id"] == journey.server.registered_client_ids[-1]
        else:
            assert query["client_id"] == profile.client_id
        assert query["redirect_uri"] == profile.redirect_uri
        relay = f"{OAUTH_REDIRECT_RELAY_ORIGIN}/oauth/{profile.profile_id}/callback"
        loopback = f"{OAUTH_LOOPBACK_CALLBACK_ORIGIN}/oauth/{profile.profile_id}/callback"
        assert query["redirect_uri"] in {relay, loopback}
        if profile.client_mode is OAuthClientMode.DYNAMIC_REGISTRATION:
            assert query["redirect_uri"] == relay
            assert list(profile.registration.redirect_uris) == [relay]
        assert query["response_type"] == "code"
        assert query["code_challenge_method"] == "S256"
        assert len(query["code_challenge"]) == 43
        assert query["state"]
        assert query["resource"] in {app.remote_origin, profile.identifier_for(app.remote_origin)}
        assert set(query["scope"].split()) == manifest_scopes
        assert len(query["scope"].split()) == len(manifest_scopes)

    @_step
    def test_04_connect_enables_and_serves_the_tools(self, journey: _Journey) -> None:
        """A faked provider consent connects, enables, and serves the plugin's tools."""

        status = _authorize(journey, journey.facts["started"])
        assert status.state is PluginLifecycleState.CONNECTED, status
        record = journey.service.plugin_registry.get(journey.plugin_id)
        assert record.enabled is True
        settings = journey.facts["confirm_settings"]
        served = journey.served()
        assert {f"tool:{item.capability}" for item in served} == set(settings["capability_ids"])
        # The saved consent round-trips.
        selection = _permissions(journey)
        assert selection["status"] == "connected"
        assert selection["selected"]["execution_policy"] == "confirm_each"
        assert set(selection["selected"]["provider_scopes"]) == set(settings["provider_scopes"])
        assert set(selection["selected"]["capability_ids"]) == set(settings["capability_ids"])
        grant = journey.service.plugin_grants.current(
            journey.plugin_id,
            app_id=journey.app_id,
            account_key_sha256=hashlib.sha256(_ACCOUNT_KEY.encode("utf-8")).hexdigest(),
        )
        assert grant is not None
        assert set(grant.receipt.scopes) == set(journey.app.authentication.scopes)

    @_independent_step
    def test_05_ask_before_changes_runs_a_read_directly(
        self, journey: _Journey, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """'Ask before changes' (confirm_each) lets a read-only tool run at once, with no
        card: only writes ask (operator ruling 2026-09-25; the HUD label says so)."""

        binding = _simplest(journey.served(), read_only=True)
        assert binding is not None, "no read-only tool is served"
        before = len(journey.calls())
        outcome = _dispatch(journey, monkeypatch, binding=binding)
        assert outcome.status == "completed", (outcome.status, outcome.failure_reason)
        assert outcome.plugin_result is not None
        assert len(journey.calls()) == before + 1

    @_independent_step
    def test_06_ask_before_changes_asks_before_a_write(
        self, journey: _Journey, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A write/send/destructive tool asks first; one approval executes it once."""

        binding = _simplest(journey.served(), read_only=False)
        if binding is None:
            pytest.skip(f"{journey.plugin_id} signs no non-read-only tool")
        _asks_then_executes_once(journey, monkeypatch, binding)

    @_step
    def test_07_run_directly_and_narrowing_are_saved(self, journey: _Journey) -> None:
        """Switch to 'runs directly' with one tool withheld; the choice round-trips."""

        served = journey.served()
        read = _simplest(served, read_only=True)
        write = _simplest(served, read_only=False)
        assert read is not None, "no read-only tool is callable from a direct request"
        keep = frozenset(item.capability for item in (read, write) if item is not None)
        withheld = _simplest(served, read_only=True, exclude=keep) or _simplest(
            served, read_only=False, exclude=keep
        )
        assert withheld is not None, "no second callable tool to withhold"
        journey.facts.update(read=read.capability, withheld=withheld.capability)
        journey.facts["write"] = None if write is None else write.capability
        selection = _permissions(journey)
        settings = full_permission_settings(selection)
        settings["capability_ids"] = [
            capability
            for capability in settings["capability_ids"]
            if capability != f"tool:{withheld.capability}"
        ]
        settings["execution_policy"] = "direct_turn"
        started = journey.submit(
            lambda: journey.service.set_permissions(
                journey.plugin_id,
                revision=selection["revision"],
                settings=settings,
                app_id=journey.app_id,
            )
        )
        status = _authorize(journey, started)
        assert getattr(status, "state", None) is PluginLifecycleState.CONNECTED or (
            getattr(status, "status", None) == "connected"
        ), status
        saved = _permissions(journey)
        assert saved["selected"]["execution_policy"] == "direct_turn"
        assert set(saved["selected"]["capability_ids"]) == set(settings["capability_ids"])
        assert set(saved["selected"]["provider_scopes"]) == set(settings["provider_scopes"])
        journey.facts["direct_settings"] = settings

    @_independent_step
    def test_08_a_withheld_tool_is_not_served(self, journey: _Journey) -> None:
        """The runtime catalog serves exactly the tools the user left selected."""

        settings = journey.facts["direct_settings"]
        served_now = {item.capability for item in journey.served()}
        assert journey.facts["withheld"] not in served_now, (
            f"{journey.facts['withheld']} was withheld in the consent form but is still "
            "offered to the planner"
        )
        assert served_now == {c.removeprefix("tool:") for c in settings["capability_ids"]}

    @_independent_step
    def test_09_a_withheld_tool_is_refused_at_dispatch(
        self, journey: _Journey, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even when named explicitly, a withheld tool never reaches the provider."""

        withheld = journey.facts["withheld"]
        before = len(journey.calls())
        served = {item.capability: item for item in journey.served()}
        if withheld not in served:
            # Not offered to the planner: an explicit request finds no candidate.
            template = _served_binding(journey, journey.facts["read"])
            outcome = _dispatch(
                journey,
                monkeypatch,
                explicit_operation={
                    "plugin_id": template.plugin_id,
                    "app_id": template.app_id,
                    "capability": withheld,
                    "arguments": {},
                },
            )
        else:
            outcome = _dispatch(journey, monkeypatch, binding=served[withheld])
        assert outcome.status != "completed", (
            f"withheld tool {withheld} executed ({outcome.status})"
        )
        assert len(journey.calls()) == before, f"withheld tool {withheld} reached the provider"

    @_independent_step
    def test_10_run_directly_executes_without_asking(
        self, journey: _Journey, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Under 'runs directly' an operator command executes reads and writes at once."""

        before = len(journey.calls())
        read = journey.facts["read"]
        outcome = _dispatch(journey, monkeypatch, binding=_served_binding(journey, read))
        assert outcome.status == "completed", (outcome.status, outcome.failure_reason)
        assert outcome.plugin_result is not None
        assert [call["capability_id"] for call in journey.calls()[before:]] == [f"tool:{read}"]
        write = journey.facts["write"]
        if write is None:
            return
        outcome = _dispatch(journey, monkeypatch, binding=_served_binding(journey, write))
        assert outcome.status == "completed", (outcome.status, outcome.failure_reason)
        assert [call["capability_id"] for call in journey.calls()[before:]] == [
            f"tool:{read}",
            f"tool:{write}",
        ]

    @_independent_step
    def test_11_automatic_update_retains_consent(self, journey: _Journey) -> None:
        """A compatible re-signed next release applies automatically under the same consent."""

        service = journey.service
        served_before = {item.capability for item in journey.served()}
        record = service.plugin_registry.get(journey.plugin_id)
        current = record.selected_version
        entry = next(
            item for item in journey.base_catalog["entries"] if item["plugin_id"] == journey.plugin_id
        )
        # Newest first, as the signed catalog orders superseded releases.
        history = [
            {"plugin_version": current, "package_sha256": entry["package_sha256"]},
            *entry["release_history"],
        ]
        release = _publish(journey, _bumped(current), journey.base_catalog["sequence"] + 1, history)
        journey.facts["history"] = history
        journey.facts["auto_release"] = release
        result = service.check_updates(force=True)
        assert (journey.plugin_id, release.version) in result.candidate_versions, result
        # What the operator is left with, first ...
        assert service.plugin_registry.get(journey.plugin_id).selected_version == release.version
        status = journey.submit(lambda: service.status(journey.plugin_id))
        assert status.state is PluginLifecycleState.CONNECTED, status
        assert status.package_version == release.version
        saved = _permissions(journey)
        assert saved["selected"]["execution_policy"] == "direct_turn"
        assert set(saved["selected"]["capability_ids"]) == set(
            journey.facts["direct_settings"]["capability_ids"]
        )
        assert {item.capability for item in journey.served()} == served_before
        # ... then what the product reported about it.
        assert result.status == "checked" and result.staged == () and result.error is None, (
            f"the update was applied, but the check reported {result}"
        )

    @_step
    def test_12_manual_update_asks_for_consent_again(self, journey: _Journey) -> None:
        """An explicit update re-consents the app through a fresh sign-in."""

        service = journey.service
        current = service.plugin_registry.get(journey.plugin_id).selected_version
        auto_release: _Release = journey.facts["auto_release"]
        history = [
            {
                "plugin_version": current,
                "package_sha256": service.plugin_registry.reverify_installed(
                    journey.plugin_id
                ).package_sha256,
            },
            *journey.facts["history"],
        ]
        assert auto_release.version == current
        release = _publish(journey, _bumped(current), journey.base_catalog["sequence"] + 2, history)
        sync = service.sync_catalog()
        assert sync.status.value != "failed", sync
        review = service.update_review(journey.plugin_id, release.version)
        # Only the version moved: new bytes, and no scope/endpoint/capability delta.
        assert review.permission_changes == ("manifest_changed", "package_changed")
        pending = service.update(journey.plugin_id, release.version, review.sha256)
        assert isinstance(pending, PluginUpdatePending), pending
        assert pending.next_required_app_id == journey.app_id
        selection = _permissions(journey)
        assert selection["candidate"] is True
        assert selection["offered"]["provider_scopes"] == list(journey.app.authentication.scopes)
        settings = full_permission_settings(selection)
        started = journey.submit(
            lambda: service.start_connection(
                journey.plugin_id,
                journey.app_id,
                revision=selection["revision"],
                settings=settings,
            )
        )
        _authorize(journey, started)
        assert service.plugin_registry.get(journey.plugin_id).selected_version == release.version
        status = journey.submit(lambda: service.status(journey.plugin_id))
        assert status.state is PluginLifecycleState.CONNECTED, status
        assert status.package_version == release.version
        assert {f"tool:{item.capability}" for item in journey.served()} == set(
            settings["capability_ids"]
        )

    @_step
    def test_13_uninstall_revokes_and_removes_everything(self, journey: _Journey) -> None:
        """Uninstall revokes the grant, clears the vault, and removes every record."""

        service = journey.service
        account_hash = hashlib.sha256(_ACCOUNT_KEY.encode("utf-8")).hexdigest()
        grant = service.plugin_grants.current(
            journey.plugin_id, app_id=journey.app_id, account_key_sha256=account_hash
        )
        assert grant is not None
        partition_key = grant.receipt.token_partition_key
        vault = service._oauth_vault
        assert vault.load_token_partition(partition_key) is not None
        journey.submit(lambda: service.uninstall(journey.plugin_id))

        with pytest.raises(PluginRegistryError, match="^plugin_not_installed$"):
            service.plugin_registry.get(journey.plugin_id)
        assert journey.app_id not in {record.app_id for record in service.app_registry.snapshot().records}
        assert service.plugin_grants.current(
            journey.plugin_id, app_id=journey.app_id, account_key_sha256=account_hash
        ) is None
        with pytest.raises(ValueError):
            service.plugin_grants.authority_store.connection(
                journey.plugin_id, journey.app_id, account_hash
            )
        with pytest.raises(ValueError, match="^oauth_token_partition_missing$"):
            vault.load_token_partition(partition_key)
        issued = set(journey.server.issued_access_tokens) | set(journey.server.issued_refresh_tokens)
        assert set(journey.server.revoked_tokens) & issued, "no issued token was revoked at the provider"
        assert journey.served() == ()


def _bumped(version: str) -> str:
    parsed = Version(version)
    return f"{parsed.major}.{parsed.minor}.{parsed.micro + 1}"


def _publish(journey: _Journey, version: str, sequence: int, history: list[dict[str, str]]) -> _Release:
    """Publish a release at the marketplace origin the product syncs from."""

    release = _next_release(
        journey.plugin_id,
        version=version,
        sequence=sequence,
        base_catalog=journey.base_catalog,
        history=history,
        private_key=journey.private_key,
        ring=journey.ring,
        workdir=journey.root / "releases",
    )

    def serve(request: httpx2.Request) -> httpx2.Response:
        if str(request.url) == release.package_uri:
            return httpx2.Response(200, content=release.archive)
        return httpx2.Response(404)

    service = journey.service
    service._catalog_fetch = lambda: CatalogFetchResponse(payload=release.catalog_bytes)
    service._package_fetcher = PackageArtifactFetcher(
        root=service._plugin_state_root / "artifacts",
        key_ring=journey.ring,
        omega_version=_OMEGA_VERSION,
        transport=httpx2.MockTransport(serve),
    )
    return release
