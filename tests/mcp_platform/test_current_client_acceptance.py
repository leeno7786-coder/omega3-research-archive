"""Phase acceptance for signed dormant apps and both current MCP transports."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import warnings
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx2
import psutil
import pytest
import uvicorn
from cryptography.hazmat.primitives import serialization
from mcp import types
from mcp.server import Server
from pydantic.warnings import TypedDictExtraConfigWarning

from mcp_platform.app_registry import McpAppRegistry, McpAppRegistrySnapshot
from mcp_platform.app_sources import TrustedRemoteAppSource, VerifiedLocalAppSource
from mcp_platform.authorization import (
    McpAuthorizationConfig,
    create_mcp_authorization_channel,
)
from mcp_platform.canonical import canonical_bytes
from mcp_platform.capabilities import project_declared_capabilities

with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message=(
            r"TypedDict class 'RequestParamsMeta' allows extra items, but 'extra' configuration is set to `'forbid'`\. "
            r"The 'extra' configuration value will be ignored\.$"
        ),
        category=TypedDictExtraConfigWarning,
    )
    from mcp_platform.client import CurrentMcpClient
from mcp_platform.client_contracts import (
    DiscoverySource,
    McpClientDeployment,
    McpClientError,
    McpClientErrorReason,
    McpClientLimits,
    McpConnectionDescriptor,
    McpDiscoveryReceipt,
    McpOperationKind,
    McpOperationRequest,
    McpOperationStatus,
    McpTransport,
)
from mcp_platform.package_contracts import MCP_PROTOCOL_VERSION
from mcp_platform.package_verifier import VerifiedPackage, verify_package
from mcp_platform.provisioner import McpAppProvisioner
from mcp_platform.redaction import RedactingTextSink
from mcp_platform.registry import PackageRegistry
from mcp_platform.transports.stdio import (
    VerifiedStdioLaunchGuard,
    build_verified_stdio_transport,
)
from mcp_platform.transports.streamable_http import (
    StreamableHttpClientConfig,
    build_streamable_http_transport,
)
from plugin_platform.catalog import MarketplaceCatalogEntry, verify_plugin_catalog
from plugin_platform.contracts import AppRequirement
from plugin_platform.installer import PluginInstaller
from plugin_platform.package_verifier import VerifiedPlugin, verify_plugin_package
from plugin_platform.registry import PluginRegistry
from plugin_platform.trust import TrustedPublisherKeyRing
from tests.helpers.fs import link_or_copy
from tests.mcp_platform.test_package_verifier import (
    build_signed_package,
    ephemeral_ring,
    host_tag,
    rewrite_and_resign_manifest,
)
from tests.mcp_platform.test_stdio_transport import (
    _descriptor as _local_descriptor,
)
from tests.mcp_platform.test_stdio_transport import (
    _launch_environment,
    _requirement as _local_requirement,
)
from tests.mcp_platform.test_streamable_http import _OAuthStore
from tests.plugin_platform.support import (
    build_signed_plugin,
    ephemeral_publisher_ring,
    marketplace_entry_document,
)


_TRANSACTION_ID = "d" * 32
_ROOT = Path(__file__).resolve().parents[2]
_REMOTE_ORIGIN = "https://apps.example.com"
#: What the manifest declares and the client posts to. Omega no longer appends
#: "/mcp" to a bare origin -- it does not invent a path a provider did not
#: publish -- so a fixture wanting that endpoint states it, as a manifest does.
#: `_REMOTE_ORIGIN` stays the host, which is what the well-known URLs hang off.
_REMOTE_ENDPOINT = f"{_REMOTE_ORIGIN}/mcp"
_AUTHORIZATION_ISSUER = "https://auth.example.com/issuer"
_CALLBACK_URI = "https://omega2ai.com/oauth/callback"
_PRIVATE_MARKERS = (
    "acceptance-code-material",
    "acceptance-access-material",
    "acceptance-refresh-material",
    "acceptance-client-material",
)


class _AcceptConfirmation:
    def __init__(self) -> None:
        self.digests: list[str] = []

    def consume(self, confirmation: object, expected_review_sha256: str) -> None:
        assert confirmation is self
        self.digests.append(expected_review_sha256)


class _SourceResolver:
    def __init__(self, package_registry: PackageRegistry) -> None:
        self.package_registry = package_registry

    def resolve(
        self,
        requirement: AppRequirement,
    ) -> VerifiedLocalAppSource | TrustedRemoteAppSource:
        if requirement.deployment.value == "local":
            return VerifiedLocalAppSource(
                requirement=requirement,
                registry=self.package_registry,
            )
        return TrustedRemoteAppSource(requirement=requirement)


class _OAuthPresenter:
    def __init__(self) -> None:
        self.authorization_url: str | None = None
        self.exchange: dict[str, str] | None = None
        self.callback_binding: tuple[str, str, str] | None = None

    async def present_authorization(self, authorization_url: str) -> None:
        self.authorization_url = authorization_url
        parsed = urlsplit(authorization_url)
        assert parsed._replace(query="").geturl() == f"{_AUTHORIZATION_ISSUER}/authorize"
        query = parse_qs(parsed.query, strict_parsing=True)
        assert set(query) == {
            "client_id",
            "code_challenge",
            "code_challenge_method",
            "redirect_uri",
            "resource",
            "response_type",
            "scope",
            "state",
        }
        assert all(len(values) == 1 for values in query.values())
        assert query["client_id"] == ["https://omega2ai.com/oauth/client-metadata.json"]
        assert query["code_challenge_method"] == ["S256"]
        assert query["redirect_uri"] == [_CALLBACK_URI]
        assert query["resource"] == [f"{_REMOTE_ORIGIN}/mcp"]
        assert query["response_type"] == ["code"]
        assert query["scope"] == ["acceptance.read"]
        assert len(query["code_challenge"][0]) == 43
        assert query["state"][0]
        self.exchange = {
            "challenge": query["code_challenge"][0],
            "client_id": query["client_id"][0],
            "callback_state": "",
            "code": "acceptance-code-material",
            "issuer": _AUTHORIZATION_ISSUER,
            "redirect_uri": query["redirect_uri"][0],
            "resource": query["resource"][0],
            "state": query["state"][0],
        }

    async def wait_for_callback(self) -> str:
        assert self.authorization_url is not None and self.exchange is not None
        state = self.exchange["state"]
        self.callback_binding = (state, _AUTHORIZATION_ISSUER, self.exchange["code"])
        self.exchange["callback_state"] = state
        return (
            f"{_CALLBACK_URI}?code={self.exchange['code']}"
            f"&state={state}&iss={_AUTHORIZATION_ISSUER}"
        )


class _AcceptanceOAuthStore(_OAuthStore):
    def __init__(self) -> None:
        super().__init__()
        self.closed_state: dict[str, int] | None = None

    def close(self) -> None:
        if self.closed_state is not None:
            return
        self.closed_state = {
            "authorization_code_digests": len(self.codes),
            "client_records": len(self.clients),
            "token_records": len(self.tokens),
        }
        self.tokens.clear()
        self.clients.clear()
        self.codes.clear()


def _validate_acceptance_oauth_exchange(
    request: httpx2.Request,
    expected: Mapping[str, str],
) -> None:
    assert set(expected) == {
        "challenge",
        "callback_state",
        "client_id",
        "code",
        "issuer",
        "redirect_uri",
        "resource",
        "state",
    }
    assert request.method == "POST"
    assert str(request.url) == f"{expected['issuer']}/token"
    assert expected["issuer"] == _AUTHORIZATION_ISSUER
    assert expected["state"] and expected["callback_state"] == expected["state"]
    assert request.headers["content-type"].partition(";")[0] == (
        "application/x-www-form-urlencoded"
    )
    form = parse_qs(request.content.decode("ascii"), strict_parsing=True)
    assert set(form) == {
        "client_id",
        "code",
        "code_verifier",
        "grant_type",
        "redirect_uri",
        "resource",
    }
    assert all(len(values) == 1 for values in form.values())
    assert form["grant_type"] == ["authorization_code"]
    assert form["code"] == [expected["code"]]
    assert form["client_id"] == [expected["client_id"]]
    assert form["redirect_uri"] == [expected["redirect_uri"]]
    assert form["resource"] == [expected["resource"]]
    verifier = form["code_verifier"][0]
    assert 43 <= len(verifier) <= 128
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    assert challenge == expected["challenge"]


def _metadata(descriptor: McpConnectionDescriptor) -> dict[str, object]:
    return {
        "appId": descriptor.app_id,
        "publisherId": descriptor.publisher_id,
        "deployment": descriptor.deployment.value,
        "providerId": descriptor.provider_id,
        "protocolVersion": descriptor.protocol_version,
        "transport": descriptor.transport.value,
        "remoteOrigin": descriptor.remote_origin,
        "authorizationIssuer": descriptor.authorization_issuer,
    }


def _peer_server(descriptor: McpConnectionDescriptor) -> Server[object]:
    metadata = _metadata(descriptor)

    async def discover(_context: object, _params: object) -> types.DiscoverResult:
        return types.DiscoverResult(
            supportedVersions=[MCP_PROTOCOL_VERSION],
            capabilities=server.get_capabilities(protocol_version=MCP_PROTOCOL_VERSION),
            ttlMs=0,
            cacheScope="private",
            _meta=metadata,
        )

    async def list_tools(_context: object, _params: object) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name="mail_search",
                    description="Search reviewed messages.",
                    inputSchema={
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                    },
                    outputSchema={
                        "type": "object",
                        "properties": {"results": {"type": "array"}},
                    },
                )
            ],
            _meta=metadata,
        )

    async def call_tool(
        _context: object,
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        assert params.name == "mail_search"
        assert params.arguments == {"query": "reviewed"}
        return types.CallToolResult(
            content=[],
            structuredContent={"results": ["reviewed-result"]},
            isError=False,
            _meta=metadata,
        )

    async def list_resources(_context: object, _params: object) -> types.ListResourcesResult:
        return types.ListResourcesResult(
            resources=[
                types.Resource(
                    uri="omega://mail/inbox",
                    name="Inbox",
                    mimeType="application/json",
                )
            ],
            _meta=metadata,
        )

    async def list_templates(
        _context: object,
        _params: object,
    ) -> types.ListResourceTemplatesResult:
        return types.ListResourceTemplatesResult(resourceTemplates=[], _meta=metadata)

    async def read_resource(
        _context: object,
        params: types.ReadResourceRequestParams,
    ) -> types.ReadResourceResult:
        assert params.uri == "omega://mail/inbox"
        return types.ReadResourceResult(
            contents=[
                types.TextResourceContents(
                    uri=params.uri,
                    mimeType="application/json",
                    text='{"folder":"inbox"}',
                )
            ],
            _meta=metadata,
        )

    server: Server[object] = Server(
        "omega.first_party.fake",
        version="1.0.0",
        on_list_tools=list_tools,
        on_call_tool=call_tool,
        on_list_resources=list_resources,
        on_list_resource_templates=list_templates,
        on_read_resource=read_resource,
    )
    server.add_request_handler("server/discover", types.RequestParams, discover)
    return server


_STDIO_PEER_SOURCE = r"""
import base64,ctypes,json,os,sys
_windows=ctypes.create_unicode_buffer(260)
ctypes.windll.kernel32.GetWindowsDirectoryW(_windows,260)
os.environ["SYSTEMROOT"]=_windows.value
import anyio
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server
V="2026-07-28"
M=json.loads(base64.b64decode(sys.argv[1]))
async def discover(_c,_p):
 return types.DiscoverResult(supportedVersions=[V],capabilities=S.get_capabilities(protocol_version=V),ttlMs=0,cacheScope="private",_meta=M)
async def list_tools(_c,_p):
 return types.ListToolsResult(tools=[types.Tool(name="mail_search",description="Search reviewed messages.",inputSchema={"type":"object","properties":{"query":{"type":"string"}}},outputSchema={"type":"object","properties":{"results":{"type":"array"}}})],_meta=M)
async def call_tool(_c,p):
 assert p.name=="mail_search" and p.arguments=={"query":"reviewed"}
 return types.CallToolResult(content=[],structuredContent={"results":["reviewed-result"]},isError=False,_meta=M)
async def list_resources(_c,_p):
 return types.ListResourcesResult(resources=[types.Resource(uri="omega://mail/inbox",name="Inbox",mimeType="application/json")],_meta=M)
async def list_templates(_c,_p):
 return types.ListResourceTemplatesResult(resourceTemplates=[],_meta=M)
async def read_resource(_c,p):
 assert p.uri=="omega://mail/inbox"
 return types.ReadResourceResult(contents=[types.TextResourceContents(uri=p.uri,mimeType="application/json",text='{"folder":"inbox"}')],_meta=M)
S=Server("omega.first_party.fake",version="1.0.0",on_list_tools=list_tools,on_call_tool=call_tool,on_list_resources=list_resources,on_list_resource_templates=list_templates,on_read_resource=read_resource)
S.add_request_handler("server/discover",types.RequestParams,discover)
async def main():
 async with stdio_server() as streams:
  await S.run(*streams,S.create_initialization_options())
anyio.run(main)
""".strip()


# The scripted peer does not use image/numeric helpers or static type checking.
# Broad dependency hooks otherwise bundle these unrelated optional packages.
_PEER_EXCLUDED_MODULES = ("numpy", "PIL", "mypy")


def _signed_python_package(
    root: Path,
    peer_cache_root: Path,
) -> tuple[Path, VerifiedPackage, object, bytes]:
    root.mkdir(parents=True)
    peer_source = root / "current-peer.py"
    peer_source.write_text(_STDIO_PEER_SOURCE, encoding="utf-8")
    # The verified launch cwd is an immutable signed package. Onefile extraction
    # must stay in writable session scratch even if forced shutdown leaves residue.
    runtime_root = (peer_cache_root / "runtime").resolve()
    runtime_root.mkdir(parents=True, exist_ok=True)
    build_options = [
        "--clean",
        "--noconfirm",
        "--onefile",
        "--noupx",
        "--runtime-tmpdir",
        str(runtime_root),
        "--name",
        "omega-mcp-fake",
    ]
    for module in _PEER_EXCLUDED_MODULES:
        build_options.extend(("--exclude-module", module))
    cache_key = hashlib.sha256(
        canonical_bytes(
            {
                "source": _STDIO_PEER_SOURCE,
                "python": sys.version,
                "platform": sys.platform,
                "build_options": build_options,
            }
        )
    ).hexdigest()
    peer_cache = peer_cache_root / cache_key
    peer_cache.mkdir(parents=True, exist_ok=True)
    compiled_artifact = peer_cache / "omega-mcp-fake.exe"
    if not compiled_artifact.is_file():
        dist = root / "peer-dist"
        build = root / "peer-build"
        spec = root / "peer-spec"
        compiled = subprocess.run(
            [
                sys.executable,
                "-m",
                "PyInstaller",
                *build_options,
                "--distpath",
                str(dist),
                "--workpath",
                str(build),
                "--specpath",
                str(spec),
                str(peer_source),
            ],
            # The portable venv keeps its embedded-Python base relative to the
            # product root. Build tooling must run from that root even when the
            # source package is an isolated temporary fixture.
            cwd=_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=150,
        )
        assert compiled.returncode == 0, compiled.stderr
        built_artifact = dist / "omega-mcp-fake.exe"
        assert built_artifact.is_file()
        link_or_copy(built_artifact, compiled_artifact)
    assert compiled_artifact.is_file()
    private, ring = ephemeral_ring()
    package = build_signed_package(root, private, signing_key_id="test-release")
    tool_catalog_path = package / "tool_catalog.json"
    tool_catalog = json.loads(tool_catalog_path.read_text(encoding="utf-8"))
    for tool in tool_catalog["tools"]:
        tool["effect"] = "read"
    tool_catalog_bytes = canonical_bytes(tool_catalog)
    tool_catalog_path.write_bytes(tool_catalog_bytes)
    original_artifact = package / "omega-mcp-fake.exe"
    original_artifact.unlink()
    scripts = package / "Scripts"
    scripts.mkdir()
    artifact = scripts / "omega-mcp-fake.exe"
    link_or_copy(compiled_artifact, artifact)
    local_identity = base64.b64encode(
        json.dumps(
            {
                "appId": "omega.first_party.fake",
                "publisherId": "omega.first_party",
                "deployment": "local",
                "providerId": None,
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "transport": "stdio",
                "remoteOrigin": None,
                "authorizationIssuer": None,
            },
            separators=(",", ":"),
        ).encode("utf-8")
    ).decode("ascii")
    rewrite_and_resign_manifest(
        package,
        private,
        {
            "entrypoint": {
                "artifact": artifact.relative_to(package).as_posix(),
                "arguments": [local_identity],
            },
            "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            "tool_catalog_sha256": hashlib.sha256(tool_catalog_bytes).hexdigest(),
        },
    )
    verified = verify_package(
        package,
        ring,
        omega_version="3.0.0",
        platform_tag=host_tag(),
    )
    secret = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return package, verified, ring, secret


def _add_python_runtime(package: Path) -> Path:
    """Retain the focused foundation fixture's relocatable-runtime helper."""
    scripts = package / "Scripts"
    for runtime_file in Path(sys.executable).parent.iterdir():
        if runtime_file.suffix.lower() in {".dll", ".pyd", ".zip"}:
            link_or_copy(runtime_file, scripts / runtime_file.name)
    venv_config = Path(sys.executable).parents[1] / "pyvenv.cfg"
    if venv_config.is_file():
        shutil.copyfile(venv_config, package / "pyvenv.cfg")
    site_packages = package / "Lib" / "site-packages"
    site_packages.mkdir(parents=True)
    runtime_pointer = site_packages / "current-sdk-runtime.pth"
    sdk_site_packages = next(
        parent
        for parent in Path(types.__file__).resolve().parents
        if parent.name == "site-packages"
    )
    runtime_pointer.write_text(
        "\n".join(
            (
                str(sdk_site_packages),
                str(sdk_site_packages / "win32"),
                str(sdk_site_packages / "win32" / "lib"),
                str(sdk_site_packages / "pythonwin"),
                "import pywin32_bootstrap",
            )
        ),
        encoding="utf-8",
    )
    return runtime_pointer


def _remote_requirement(local: AppRequirement) -> AppRequirement:
    document = local.to_dict()
    document.update(
        {
            "app_id": "omega.remote.fake",
            "deployment": "remote",
            "transports": ["streamable_http"],
            "remote_origin": _REMOTE_ENDPOINT,
            "authorization_issuer": _AUTHORIZATION_ISSUER,
        }
    )
    return AppRequirement.from_dict(document)


def _signed_plugin(
    root: Path,
    requirements: tuple[AppRequirement, ...],
) -> tuple[Path, VerifiedPlugin, object, TrustedPublisherKeyRing, bytes]:
    private, ring = ephemeral_publisher_ring()
    package = build_signed_plugin(root, private)
    document = json.loads((package / "plugin.json").read_bytes())
    document["kind"] = "composite"
    requirement_documents = [requirement.to_dict() for requirement in requirements]
    for requirement_document in requirement_documents:
        requirement_document["capabilities"] = sorted(
            requirement_document["capabilities"],
            key=lambda capability: (capability["kind"], capability["name"]),
        )
    document["required_apps"] = requirement_documents
    document["optional_apps"] = []
    document["entry_points"][0]["required_app_ids"] = [
        requirement.app_id for requirement in requirements
    ]
    unsigned = {key: value for key, value in document.items() if key != "signature"}
    document["signature"] = base64.b64encode(private.sign(canonical_bytes(unsigned))).decode(
        "ascii"
    )
    (package / "plugin.json").write_bytes(canonical_bytes(document))
    verified = verify_plugin_package(package, ring, "3.0.0")
    secret = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return package, verified, private, ring, secret


def _signed_catalog(
    root: Path,
    verified: VerifiedPlugin,
    private: object,
    ring: TrustedPublisherKeyRing,
) -> tuple[object, MarketplaceCatalogEntry]:
    entry = MarketplaceCatalogEntry.from_dict(
        marketplace_entry_document(
            plugin_id=verified.manifest.plugin_id,
            publisher_id=verified.manifest.publisher_id,
            plugin_version=verified.manifest.plugin_version,
            manifest_sha256=verified.manifest_sha256,
            package_sha256=verified.package_sha256,
            package_uri="https://plugins.example.test/acceptance/1.0.0",
        )
    )
    unsigned: dict[str, object] = {
        "schema_version": 2,
        "catalog_id": "test.acceptance.catalog",
        "publisher_id": verified.manifest.publisher_id,
        "sequence": 1,
        "entries": [entry.to_dict()],
        "signing_key_id": "test-plugin-key",
    }
    signature = base64.b64encode(private.sign(canonical_bytes(unsigned))).decode("ascii")
    path = root / "catalog.json"
    path.write_bytes(canonical_bytes({**unsigned, "signature": signature}))
    return verify_plugin_catalog(path, ring), entry


def _remote_descriptor(requirement: AppRequirement) -> McpConnectionDescriptor:
    projection = project_declared_capabilities(requirement)
    return McpConnectionDescriptor(
        app_id=requirement.app_id,
        publisher_id=requirement.publisher_id,
        deployment=McpClientDeployment.REMOTE,
        provider_id=requirement.provider_id,
        protocol_version=requirement.protocol_version,
        transport=McpTransport.STREAMABLE_HTTP,
        verified_launch_sha256=None,
        remote_origin=requirement.remote_origin,
        authorization_issuer=requirement.authorization_issuer,
        authentication=requirement.authentication,
        declared_capabilities={
            f"{capability.kind.value}:{capability.name}": capability.schema_sha256
            for capability in requirement.capabilities
        },
        extension_versions={},
        capability_revision=projection.snapshot.capability_revision,
        limits=McpClientLimits(),
    )


def _request(
    descriptor: McpConnectionDescriptor,
    operation: McpOperationKind,
    capability_id: str,
    arguments: Mapping[str, object] | None = None,
) -> McpOperationRequest:
    return McpOperationRequest(
        request_id=f"acceptance-{operation.value}",
        app_id=descriptor.app_id,
        operation=operation,
        capability_id=capability_id,
        arguments={} if arguments is None else arguments,
        capability_revision=descriptor.capability_revision,
        deadline_monotonic=asyncio.get_running_loop().time() + 30,
        cancellation_id=f"cancel-{operation.value}",
        task_id=None,
        input_request_id=None,
        request_state=None,
    )


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_plain_json(item) for item in value]
    return value


def _closed_capability_shape(receipt: McpDiscoveryReceipt) -> dict[str, object]:
    return {
        "protocol_version": receipt.protocol_version,
        "server_version": receipt.server_version,
        "tools": _plain_json(receipt.tools),
        "prompts": _plain_json(receipt.prompts),
        "resources": _plain_json(receipt.resources),
        "resource_templates": _plain_json(receipt.resource_templates),
        "extensions": _plain_json(receipt.extensions),
        "capability_revision": receipt.capability_revision,
    }


async def _closed_shape(
    client: CurrentMcpClient,
    descriptor: McpConnectionDescriptor,
) -> dict[str, object]:
    operations = (
        await client.list_tools(_request(descriptor, McpOperationKind.TOOLS_LIST, "tools/list")),
        await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:mail_search",
                {"query": "reviewed"},
            )
        ),
        await client.list_resources(
            _request(descriptor, McpOperationKind.RESOURCES_LIST, "resources/list")
        ),
        await client.read_resource(
            _request(
                descriptor,
                McpOperationKind.RESOURCE_READ,
                "resource:Inbox",
            )
        ),
    )
    assert all(result.status is McpOperationStatus.COMPLETED for result in operations), repr(
        operations
    )
    return {
        "operations": [
            {
                "status": result.status.value,
                "data": result.data,
                "error": result.error,
                "task_handle": result.task_handle,
                "input_request": result.input_request,
            }
            for result in operations
        ]
    }


def _assert_same_closed_shape(local: object, remote: object) -> None:
    assert local == remote
    encoded = json.dumps(local, sort_keys=True, separators=(",", ":"), default=str)
    assert all(marker not in encoded for marker in _PRIVATE_MARKERS)
    assert "authorization" not in encoded.casefold()


async def _start_with_authorization(
    client: CurrentMcpClient,
    channel: object,
    presenter: _OAuthPresenter,
) -> object:
    async def drive() -> None:
        request = await channel.receive_authorization_request()
        await presenter.present_authorization(request.authorization_url)
        await channel.submit_authorization_callback(
            request.request_id,
            await presenter.wait_for_callback(),
        )

    driver = asyncio.create_task(drive(), name="acceptance-oauth-driver")
    starter = asyncio.create_task(client.start(), name="acceptance-http-start")
    try:
        return await starter
    finally:
        if not driver.done():
            driver.cancel()
        try:
            await driver
        except BaseException:
            pass


async def _listener_started(owner: uvicorn.Server) -> None:
    for _attempt in range(300):
        if owner.started:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("loopback listener did not start")


def _available_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.mark.timeout(180)
def test_signed_dormant_apps_run_identical_current_stdio_and_authorized_http(
    tmp_path: Path,
    mcp_compiled_peer_cache: Path,
) -> None:
    root_import = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import mcp_platform;"
                "from mcp_platform.app_registry import McpAppRegistrySnapshot;"
                "assert mcp_platform.McpAppRegistrySnapshot is McpAppRegistrySnapshot"
            ),
        ],
        cwd=Path(__file__).resolve().parents[2],
        check=False,
        capture_output=True,
        text=True,
    )
    assert root_import.returncode == 0, root_import.stderr
    package_source, package, package_ring, package_secret = _signed_python_package(
        tmp_path / "package-source",
        mcp_compiled_peer_cache,
    )

    def package_verifier(path: Path) -> VerifiedPackage:
        return verify_package(
            path,
            package_ring,
            omega_version="3.0.0",
            platform_tag=host_tag(),
        )

    state_root = tmp_path / "mcp-state"
    package_registry = PackageRegistry(state_root=state_root, verifier=package_verifier)
    package_registry.install(package_source)
    installed_path = package_registry.package_path(package_registry.get(package.manifest.server_id))
    installed_package = package_registry.reverify_installed(package.manifest.server_id)
    nominal_local = _local_requirement(installed_package)
    nominal_remote = _remote_requirement(nominal_local)
    plugin_source, plugin, plugin_private, plugin_ring, plugin_secret = _signed_plugin(
        tmp_path / "plugin-source",
        (nominal_local, nominal_remote),
    )
    local_requirement, remote_requirement = plugin.manifest.required_apps
    catalog, entry = _signed_catalog(tmp_path, plugin, plugin_private, plugin_ring)

    plugin_registry = PluginRegistry(
        state_root=tmp_path / "plugin-state",
        verifier=lambda path: verify_plugin_package(path, plugin_ring, "3.0.0"),
    )
    app_registry = McpAppRegistry(state_root=state_root)
    provisioner = McpAppProvisioner(
        app_registry=app_registry,
        package_registry=package_registry,
        source_resolver=_SourceResolver(package_registry),
    )
    confirmation = _AcceptConfirmation()
    installer = PluginInstaller(
        registry=plugin_registry,
        provisioner=provisioner,
        confirmation_verifier=confirmation,
        transaction_id_factory=lambda: _TRANSACTION_ID,
    )
    result = installer.install_from_catalog(catalog, entry, plugin, confirmation)
    assert result.record.enabled is False
    assert result.record.status == "installed"
    assert confirmation.digests == [result.review_sha256]
    assert result.dependency_receipt.introduced_app_ids == tuple(
        sorted((local_requirement.app_id, remote_requirement.app_id))
    )
    app_snapshot = app_registry.snapshot()
    assert type(app_snapshot) is McpAppRegistrySnapshot
    app_records = {record.app_id: record for record in app_snapshot.records}
    assert {app_id: record.enabled for app_id, record in app_records.items()} == {
        local_requirement.app_id: False,
        remote_requirement.app_id: False,
    }
    assert package_registry.get(local_requirement.app_id).enabled is False

    local_source = provisioner.source_resolver.resolve(local_requirement)
    assert type(local_source) is VerifiedLocalAppSource
    assert local_source.package.artifact_path == installed_package.artifact_path
    assert local_source.package.tool_catalog_path.parent == installed_path
    launch_package = package_registry.reverify_installed(local_requirement.app_id)
    local_descriptor = _local_descriptor(launch_package)
    remote_descriptor = _remote_descriptor(remote_requirement)
    for requirement, descriptor in (
        (local_requirement, local_descriptor),
        (remote_requirement, remote_descriptor),
    ):
        record = app_records[requirement.app_id]
        assert descriptor.app_id == record.app_id
        assert descriptor.publisher_id == record.publisher_id
        assert descriptor.protocol_version == record.protocol_version
        assert (
            descriptor.capability_revision
            == project_declared_capabilities(requirement).snapshot.capability_revision
        )
    with pytest.raises(McpClientError) as substituted_source:
        VerifiedStdioLaunchGuard.acquire(
            launch_package,
            package_root=package_source,
            environment=_launch_environment(launch_package),
        )
    assert substituted_source.value.reason is McpClientErrorReason.IDENTITY_MISMATCH
    local_guard = VerifiedStdioLaunchGuard.acquire(
        launch_package,
        package_root=installed_path,
        environment=_launch_environment(launch_package),
    )
    assert local_guard.verified_package.artifact_path == installed_package.artifact_path
    assert local_guard.package_root == installed_path
    local_sink = RedactingTextSink(omega_root=tmp_path)
    local_client = CurrentMcpClient(
        local_descriptor,
        build_verified_stdio_transport(
            local_descriptor,
            local_guard,
            local_sink,
        ),
    )

    port = _available_port()
    peer_owner = uvicorn.Server(
        uvicorn.Config(
            _peer_server(remote_descriptor).streamable_http_app(
                json_response=True,
                stateless_http=True,
                host="127.0.0.1",
            ),
            host="127.0.0.1",
            port=port,
            access_log=False,
            log_level="critical",
        )
    )
    loopback_url = f"http://127.0.0.1:{port}/mcp"
    forwarded = 0
    forwarded_messages: list[tuple[object, object]] = []

    async def route(request: httpx2.Request) -> httpx2.Response:
        nonlocal forwarded
        url = str(request.url)
        if url == f"{_REMOTE_ORIGIN}/mcp":
            if request.headers.get("Authorization") != "Bearer acceptance-access-material":
                return httpx2.Response(
                    401,
                    headers={
                        "WWW-Authenticate": (
                            f'Bearer resource_metadata="{_REMOTE_ORIGIN}/'
                            '.well-known/oauth-protected-resource/mcp", '
                            'scope="acceptance.read"'
                        )
                    },
                )
            forwarded += 1
            headers = {
                name: value
                for name, value in request.headers.items()
                if name.casefold()
                in {
                    "accept",
                    "content-type",
                    "mcp-method",
                    "mcp-name",
                    "mcp-protocol-version",
                }
            }
            async with httpx2.AsyncClient() as forward:
                response = await forward.post(
                    loopback_url,
                    content=request.content,
                    headers=headers,
                )
            forwarded_messages.append((json.loads(request.content), json.loads(response.content)))
            return httpx2.Response(
                response.status_code,
                headers={"content-type": response.headers["content-type"]},
                content=response.content,
            )
        if url == f"{_REMOTE_ORIGIN}/.well-known/oauth-protected-resource/mcp":
            return httpx2.Response(
                200,
                json={
                    "resource": f"{_REMOTE_ORIGIN}/mcp",
                    "authorization_servers": [_AUTHORIZATION_ISSUER],
                    "scopes_supported": ["acceptance.read"],
                },
            )
        if url == "https://auth.example.com/.well-known/oauth-authorization-server/issuer":
            return httpx2.Response(
                200,
                json={
                    "issuer": _AUTHORIZATION_ISSUER,
                    "authorization_endpoint": f"{_AUTHORIZATION_ISSUER}/authorize",
                    "token_endpoint": f"{_AUTHORIZATION_ISSUER}/token",
                    "client_id_metadata_document_supported": True,
                    "code_challenge_methods_supported": ["S256"],
                    "response_types_supported": ["code"],
                    "scopes_supported": ["acceptance.read"],
                },
            )
        if url == f"{_AUTHORIZATION_ISSUER}/token":
            assert oauth_presenter.exchange is not None
            assert oauth_presenter.callback_binding == (
                oauth_presenter.exchange["state"],
                oauth_presenter.exchange["issuer"],
                oauth_presenter.exchange["code"],
            )
            _validate_acceptance_oauth_exchange(request, oauth_presenter.exchange)
            return httpx2.Response(
                200,
                json={
                    "access_token": "acceptance-access-material",
                    "token_type": "Bearer",
                    "expires_in": 60,
                    "refresh_token": "acceptance-refresh-material",
                    "scope": "acceptance.read",
                    "audience": f"{_REMOTE_ORIGIN}/mcp",
                },
            )
        raise AssertionError(f"unexpected acceptance request: {request.method} {url}")

    token_store = _AcceptanceOAuthStore()
    presenter, authorization_port = create_mcp_authorization_channel()
    oauth_presenter = _OAuthPresenter()
    authorization = McpAuthorizationConfig.for_client_metadata_document(
        remote_descriptor,
        TrustedRemoteAppSource(requirement=remote_requirement),
        authorization_context_revision="a" * 64,
        redirect_uri=_CALLBACK_URI,
        scopes=("acceptance.read",),
        presenter=presenter,
        client_metadata_url="https://omega2ai.com/oauth/client-metadata.json",
        token_store=token_store,
    )
    http_config = StreamableHttpClientConfig._for_https_test(
        httpx2.MockTransport(route),
        auth=authorization,
    )
    http_factory = build_streamable_http_transport(remote_descriptor, http_config)
    assert http_factory.discovery_source is DiscoverySource.LIVE_AUTHENTICATED
    remote_client = CurrentMcpClient(remote_descriptor, http_factory)

    parent = psutil.Process()
    child_pids_before = {child.pid for child in parent.children(recursive=True)}

    async def exercise() -> tuple[object, object, object, object, tuple[str, ...]]:
        baseline = set(asyncio.all_tasks())
        peer_task = asyncio.create_task(peer_owner.serve(), name="acceptance-loopback-peer")
        local_shape: object | None = None
        remote_shape: object | None = None
        try:
            await _listener_started(peer_owner)
            try:
                local_receipt = await local_client.start()
            except BaseException as error:
                raise AssertionError(local_sink.getvalue()) from error
            try:
                remote_receipt = await _start_with_authorization(
                    remote_client,
                    authorization_port,
                    oauth_presenter,
                )
            except BaseException as error:
                raise AssertionError(repr(forwarded_messages)) from error
            assert local_receipt.source is DiscoverySource.LIVE_OBSERVED
            assert remote_receipt.source is DiscoverySource.LIVE_AUTHENTICATED
            assert (
                local_receipt.app_id,
                local_receipt.publisher_id,
                local_receipt.protocol_version,
            ) == (
                app_records[local_requirement.app_id].app_id,
                app_records[local_requirement.app_id].publisher_id,
                app_records[local_requirement.app_id].protocol_version,
            )
            assert (
                remote_receipt.app_id,
                remote_receipt.publisher_id,
                remote_receipt.protocol_version,
            ) == (
                app_records[remote_requirement.app_id].app_id,
                app_records[remote_requirement.app_id].publisher_id,
                app_records[remote_requirement.app_id].protocol_version,
            )
            local_capabilities = _closed_capability_shape(local_receipt)
            remote_capabilities = _closed_capability_shape(remote_receipt)
            _assert_same_closed_shape(local_capabilities, remote_capabilities)
            local_shape = await _closed_shape(local_client, local_descriptor)
            try:
                remote_shape = await _closed_shape(remote_client, remote_descriptor)
            except BaseException as error:
                raise AssertionError(repr(forwarded_messages)) from error
            _assert_same_closed_shape(local_shape, remote_shape)
        finally:
            await remote_client.close()
            await local_client.close()
            await authorization_port.close()
            token_store.close()
            peer_owner.should_exit = True
            try:
                await asyncio.wait_for(peer_task, timeout=5)
            except BaseException:
                pass
        await asyncio.sleep(0)
        survivors = tuple(
            sorted(
                task.get_name()
                for task in asyncio.all_tasks()
                if task not in baseline and not task.done()
            )
        )
        assert local_shape is not None
        assert remote_shape is not None
        return (
            local_capabilities,
            remote_capabilities,
            local_shape,
            remote_shape,
            survivors,
        )

    (
        local_capabilities,
        remote_capabilities,
        local_shape,
        remote_shape,
        task_survivors,
    ) = asyncio.run(exercise())
    assert forwarded >= 5
    assert local_guard.closed is True
    closed_package = package_registry.reverify_installed(local_requirement.app_id)
    assert closed_package.artifact_path == installed_package.artifact_path
    assert closed_package.artifact_identity == installed_package.artifact_identity
    assert token_store.tokens == token_store.clients == {}
    assert token_store.codes == set()
    assert token_store.closed_state == {
        "authorization_code_digests": 1,
        "client_records": 1,
        "token_records": 1,
    }
    assert all(marker not in repr(token_store.__dict__) for marker in _PRIVATE_MARKERS)
    with socket.socket() as probe:
        probe.settimeout(0.2)
        assert probe.connect_ex(("127.0.0.1", port)) != 0
    survivors = [
        child
        for child in parent.children(recursive=True)
        if child.pid not in child_pids_before and child.is_running()
    ]
    _gone, alive = psutil.wait_procs(survivors, timeout=5)
    assert alive == []
    assert task_survivors == ()
    _assert_same_closed_shape(local_capabilities, remote_capabilities)
    _assert_same_closed_shape(local_shape, remote_shape)

    public_text = "\n".join(
        repr(value)
        for value in (
            result,
            plugin_registry.snapshot(),
            package_registry.snapshot(),
            app_snapshot,
            local_capabilities,
            remote_capabilities,
            local_shape,
            remote_shape,
            authorization,
            http_config,
            http_factory,
        )
    )
    assert all(marker not in public_text for marker in _PRIVATE_MARKERS)
    persisted = tuple(path for path in tmp_path.rglob("*") if path.is_file())
    assert persisted
    for path in persisted:
        content = path.read_bytes()
        assert package_secret not in content
        assert plugin_secret not in content
        assert all(marker.encode("utf-8") not in content for marker in _PRIVATE_MARKERS)
    assert plugin_source.is_dir()


def test_closed_shape_comparison_rejects_a_private_material_superset() -> None:
    closed = {"operations": [{"status": "succeeded", "data": {}}]}
    mutated = {
        "operations": [
            {
                "status": "succeeded",
                "data": {},
                "authorization": "acceptance-access-material",
            }
        ]
    }

    with pytest.raises(AssertionError):
        _assert_same_closed_shape(closed, mutated)


@pytest.mark.parametrize(
    "seam",
    (
        "method",
        "grant_type",
        "code",
        "client_id",
        "redirect_uri",
        "resource",
        "code_verifier",
        "code_challenge",
        "issuer",
        "state",
    ),
)
def test_deterministic_oauth_peer_rejects_each_mismatched_exchange_field(seam: str) -> None:
    verifier = "v" * 43
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    expected = {
        "challenge": challenge,
        "callback_state": "acceptance-bound-state",
        "client_id": "https://omega2ai.com/oauth/client-metadata.json",
        "code": "acceptance-code-material",
        "issuer": _AUTHORIZATION_ISSUER,
        "redirect_uri": _CALLBACK_URI,
        "resource": f"{_REMOTE_ORIGIN}/mcp",
        "state": "acceptance-bound-state",
    }
    form = {
        "grant_type": "authorization_code",
        "code": expected["code"],
        "client_id": expected["client_id"],
        "redirect_uri": expected["redirect_uri"],
        "resource": expected["resource"],
        "code_verifier": verifier,
    }
    method = "POST"
    if seam == "method":
        method = "GET"
    elif seam in form:
        form[seam] = "mismatch"
    else:
        expected[seam.removeprefix("code_")] = "mismatch"
    request = httpx2.Request(
        method,
        f"{_AUTHORIZATION_ISSUER}/token",
        content=urlencode(form),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    with pytest.raises(AssertionError):
        _validate_acceptance_oauth_exchange(request, expected)


def test_acceptance_oauth_store_close_causally_removes_private_material() -> None:
    store = _AcceptanceOAuthStore()
    store.tokens["token-key"] = (0, "acceptance-access-material")
    store.clients["client-key"] = "acceptance-client-material"
    store.codes.add(("code-key", "acceptance-code-material"))

    store.close()

    assert store.closed_state == {
        "authorization_code_digests": 1,
        "client_records": 1,
        "token_records": 1,
    }
    assert all(marker not in repr(store.__dict__) for marker in _PRIVATE_MARKERS)
