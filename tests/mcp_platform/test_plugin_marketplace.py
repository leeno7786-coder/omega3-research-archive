"""Marketplace search, install, synchronization, and atomic update services."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import mcp_platform.plugin_lifecycle as lifecycle_module
from mcp_platform.provisioner import _protected_app_ids
from mcp_platform.plugin_lifecycle import (
    PluginLifecycleService,
    compose_plugin_lifecycle_service,
)
from mcp_platform.plugin_directory import (
    CatalogPluginEntry,
    CatalogSourceSnapshot,
    PluginDirectoryService,
)
from mcp_platform.plugin_state import PluginLifecycleSnapshot, PluginLifecycleState
from mcp_platform.state_store import McpStateStore
from plugin_platform.catalog_sources import (
    CatalogSourceDescriptor,
    CatalogSourceKind,
    CatalogSourcePolicy,
)
from plugin_platform.catalog_sync import CatalogFetchError, CatalogSyncStatus
from plugin_platform.catalog import DistributionKind, TrustTier
from plugin_platform.contracts import AppDeployment
from plugin_platform.installer import PluginRegistryError
from mcp_platform.plugin_authority import PluginAuthorityStore
from mcp_platform.plugin_backend import CurrentMcpPluginBackend
from mcp_platform.plugin_catalog import PluginCatalogAppAuthority
from mcp_platform.plugin_connections import PluginConnectionCoordinator, PluginConnectionService
from mcp_platform.plugin_discovery import PluginDiscoveryError
from mcp_platform.plugin_execution_worker import PluginExecutionWorker
from mcp_platform.plugin_planning import plugin_binding_is_plannable, retrieve_plugin_candidates
from mcp_platform.plugin_runtime import (
    PluginOperationRequest,
    PluginRuntime,
)
from mcp_platform.package_contracts import RiskClass
from tests.mcp_platform.test_plugin_discovery import discovery_peer, unreviewed_tool
from tests.mcp_platform.test_plugin_lifecycle import _attach_lifecycle_operation_state
from tests.mcp_platform.test_plugin_connections import _Broker, _GrantService, _direct_submit
from tests.helpers.plugin_consent import full_connection_grant


def _worker_submit(operation, **_kwargs):
    return asyncio.run(operation())


def _sealed_forecast_worker_operation(body: dict, credential: dict) -> dict:
    """The one fake ``PluginExecutionWorker`` child handler this module uses.

    Module level (not a test-local closure) because ``PluginExecutionWorker``
    spawns a real ``multiprocessing`` child process for every operation, so
    ``child_handler`` must be picklable by reference. Handles both the
    discovery probe a fresh no-auth ``connect`` issues and the one real
    ``tools/call`` the product-path test executes, mirroring
    ``tests.mcp_platform.test_plugin_zero_tool_lifecycle._surface_worker_operation``'s
    dual-purpose shape for the one capability this module ever discovers:
    the sealed ``forecast`` tool.
    """

    del credential
    descriptor = body["descriptor"]
    if body.get("discovery") is True:
        signed = descriptor["signed_capability_authority"]
        return {
            "discovery": {
                "app_id": descriptor["app_id"],
                "publisher_id": descriptor["publisher_id"],
                "deployment": descriptor["deployment"],
                "transport": descriptor["transport"],
                "protocol_version": descriptor["protocol_version"],
                "source": "live_authenticated",
                "server_id": descriptor["app_id"],
                "server_version": "1.0.0",
                "tools": {name: {} for name in signed["tools"]},
                "prompts": {name: {} for name in signed["prompts"]},
                "resources": {name: {} for name in signed["resources"]},
                "resource_templates": {
                    name: {} for name in signed["resource_templates"]
                },
                "extensions": {},
                "cache_metadata": {},
                "capability_revision": descriptor["capability_revision"],
                "signed_capability_authority": signed,
            }
        }
    request = body["request"]
    return {
        "request_id": request["request_id"],
        "app_id": request["app_id"],
        "capability_id": request["capability_id"],
        "capability_revision": request["capability_revision"],
        "status": "completed",
        "data": {
            # The MCP tool-call result envelope PluginRuntime._validate_tool_
            # result requires: no text content (so no "text/plain" mime type
            # widens beyond the sealed tool's allowed_mime_types), structured
            # content only, matching the sealed open-object output schema.
            "content": [],
            "structured_content": {"forecast": "sunny"},
            "is_error": False,
        },
    }


# --- shared fixtures -----------------------------------------------------


def _catalog_plugin_entry(
    plugin_id: str,
    *,
    title: str = "Plugin",
    description: str = "A marketplace plugin.",
    categories: tuple[str, ...] = (),
    trust_tier: str = "omega_verified",
) -> CatalogPluginEntry:
    return CatalogPluginEntry(
        plugin_id=plugin_id,
        publisher_id="vendor",
        plugin_version="1.0.0",
        title=title,
        description=description,
        signing_key_id="vendor-release",
        manifest_sha256="a" * 64,
        package_sha256="b" * 64,
        required_app_ids=(),
        optional_app_ids=(),
        starter_requests=(),
        entry_point_ids=("start",),
        categories=categories,
        trust_tier=trust_tier,
    )


class _FakeCatalogSource:
    """A minimal ``CatalogSourceAdapter`` carrying exact synthetic entries."""

    def __init__(
        self,
        entries: tuple[CatalogPluginEntry, ...],
        *,
        source_id: str = "omega.marketplace",
        kind: CatalogSourceKind = CatalogSourceKind.MARKETPLACE,
        approval_revision: str | None = None,
        failure: Exception | None = None,
    ) -> None:
        self.entries = entries
        self.descriptor = CatalogSourceDescriptor(
            source_id=source_id,
            title=source_id,
            kind=kind,
            approval_revision=approval_revision,
        )
        self.failure = failure

    def snapshot(self) -> CatalogSourceSnapshot:
        if self.failure is not None:
            raise self.failure
        return CatalogSourceSnapshot(
            source=self.descriptor,
            revision="b" * 64,
            plugins=tuple(sorted(self.entries, key=lambda item: item.plugin_id)),
        )

    def review(self, plugin_id: str) -> object:
        raise NotImplementedError

    def install(self, plugin_id: str, *, confirmation: object) -> object:
        raise NotImplementedError


class _EmptyRegistry:
    def snapshot(self) -> object:
        return SimpleNamespace(records=(), revision="c" * 64)

    def get(self, plugin_id: str) -> object:
        raise PluginRegistryError("plugin_not_installed")

    def reverify_installed(self, plugin_id: str) -> object:
        raise PluginRegistryError("plugin_not_installed")


def _search_service(
    sources: tuple[object, ...],
    *,
    source_policy: CatalogSourcePolicy | None = None,
) -> PluginLifecycleService:
    service = object.__new__(PluginLifecycleService)
    service._directory = PluginDirectoryService(
        sources=sources,
        plugin_registry=_EmptyRegistry(),
        source_policy=source_policy,
    )
    service._catalog_cursor_key = b"k" * 32
    return service


def marketplace_service_with(count: int) -> PluginLifecycleService:
    """A search-only service exposing ``count`` deterministic synthetic entries."""

    entries = []
    for index in range(count):
        plugin_id = f"vendor{index:03d}.plugin"
        if index == 0:
            title, categories, trust = "Calendar Sync", ("productivity",), "publisher_verified"
        else:
            title = f"Plugin {index}"
            categories = ()
            trust = "omega_verified" if index % 2 == 0 else "publisher_verified"
        entries.append(
            _catalog_plugin_entry(
                plugin_id,
                title=title,
                categories=categories,
                trust_tier=trust,
            )
        )
    return _search_service((_FakeCatalogSource(tuple(entries)),))


def _app_requirement(
    app_id: str = "weather.mcp",
    *,
    deployment: object = AppDeployment.REMOTE,
    remote_origin: str | None = "https://weather.example.com",
) -> SimpleNamespace:
    return SimpleNamespace(
        app_id=app_id,
        deployment=deployment,
        remote_origin=remote_origin,
        authentication=SimpleNamespace(kind="none", scopes=()),
        capabilities=(),
    )


def _provenance_record(
    *,
    provider_publisher_id: str = "acme",
    provider_id: str = "acme.weather",
    authoritative_source_url: str = "https://plugins.example.com/weather",
) -> SimpleNamespace:
    document = {
        "provider_publisher_id": provider_publisher_id,
        "provider_id": provider_id,
        "authoritative_source_url": authoritative_source_url,
    }
    return SimpleNamespace(
        provider_publisher_id=provider_publisher_id,
        provider_id=provider_id,
        authoritative_source_url=authoritative_source_url,
        to_dict=lambda: dict(document),
    )


def installed_service(
    *,
    version: str,
    candidate: str,
    installed_manifest_sha256: str = "1" * 64,
    installed_package_sha256: str = "2" * 64,
    candidate_manifest_sha256: str = "3" * 64,
    candidate_package_sha256: str = "4" * 64,
    installed_required_apps: tuple[object, ...] = (),
    candidate_required_apps: tuple[object, ...] = (),
    installed_provenance: object | None = None,
    candidate_provenance: object | None = None,
) -> PluginLifecycleService:
    """A white-box service for one already-installed plugin ("weather") with a
    reviewable candidate catalog version, wired exactly enough to exercise
    ``update_review``/``status``/``update`` without the deep candidate-app
    continuation machinery (this fixture's plugin has no app dependencies by
    default)."""

    state_root = Path(tempfile.mkdtemp(prefix="omega-plugin-marketplace-test-"))
    service = object.__new__(PluginLifecycleService)
    service._authority_mutation_lock = lifecycle_module._LifecycleMutationBoundary()
    _attach_lifecycle_operation_state(service, state_root)
    service._update_state = McpStateStore(
        state_root=state_root,
        document_path=state_root / "update.json",
        invalid_path_reason="plugin_update_recovery_required",
        max_document_bytes=1024 * 1024,
    )

    record = SimpleNamespace(plugin_id="weather", selected_version=version)

    class _Registry:
        def get(self, plugin_id: str) -> object:
            assert plugin_id == "weather"
            return record

    service.plugin_registry = _Registry()

    installed_verified = SimpleNamespace(
        manifest_sha256=installed_manifest_sha256,
        package_sha256=installed_package_sha256,
        manifest=SimpleNamespace(
            required_apps=installed_required_apps, optional_apps=()
        ),
    )
    candidate_manifest = SimpleNamespace(
        plugin_id="weather",
        plugin_version=candidate,
        required_apps=candidate_required_apps,
        optional_apps=(),
    )
    candidate_verified = SimpleNamespace(
        manifest_sha256=candidate_manifest_sha256,
        package_sha256=candidate_package_sha256,
        manifest=candidate_manifest,
    )
    catalog_entry = SimpleNamespace(plugin_version=candidate)
    review_sha256 = "e" * 64

    service._verified_with_provenance = lambda _plugin_id: (
        object(),
        catalog_entry,
        candidate_verified,
        candidate_provenance,
    )
    service._installed_verified_with_provenance = lambda _plugin_id: (
        installed_verified,
        installed_provenance,
    )
    service.review = lambda _plugin_id: SimpleNamespace(sha256=review_sha256)

    def fake_update_locked(plugin_id: str, *, version: str, confirmation: object) -> object:
        assert plugin_id == "weather"
        assert version == candidate
        assert confirmation == review_sha256
        record.selected_version = candidate
        return SimpleNamespace(record=record, revision="f" * 64)

    service._update_locked = fake_update_locked

    def submit_connection_operation(operation: object) -> object:
        # ``update()`` calls this synchronously; the brief's example test is
        # itself an async test already running inside anyio's event loop, so
        # bridge through a dedicated thread rather than asyncio.run(), which
        # refuses to nest inside a loop that is already running -- exactly
        # how the real operation submitter bridges into its own owned loop.
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(lambda: asyncio.run(operation())).result()

    service._submit_connection_operation = submit_connection_operation

    async def fake_status(plugin_id: str) -> PluginLifecycleSnapshot:
        return PluginLifecycleSnapshot(
            plugin_id=plugin_id,
            state=PluginLifecycleState.CONNECTED,
            registry_revision="a" * 64,
            package_version=record.selected_version,
            apps=(),
            next_required_app_id=None,
            enabled=True,
            error_code=None,
        )

    service.status = fake_status
    return service


# --- Step 1 example tests (verbatim) --------------------------------------


def test_search_is_deterministic_bounded_and_distinguishes_no_matches() -> None:
    service = marketplace_service_with(35)
    page = service.search_catalog("calendar", {"trust": "publisher_verified"}, None, 12)
    assert len(page.items) <= 12
    assert page.status == "ok"
    assert [item["plugin_id"] for item in page.items] == sorted(
        item["plugin_id"] for item in page.items
    )
    assert service.search_catalog("no-such-plugin", {}, None, 12).items == ()


@pytest.mark.anyio
async def test_update_keeps_old_version_current_until_candidate_is_approved() -> None:
    service = installed_service(version="1.0.0", candidate="2.0.0")
    review = service.update_review("weather", "2.0.0")
    assert review.permission_changes
    # The brief's example asserts `.version`; the real `PluginLifecycleSnapshot`
    # (which `update()`/`status()` now genuinely return -- see fix round 1
    # finding 5) names this field `package_version`.
    assert (await service.status("weather")).package_version == "1.0.0"
    updated = service.update("weather", "2.0.0", review.sha256)
    assert updated.package_version == "2.0.0"


# --- bounded page size / opaque cursor ------------------------------------


def test_search_catalog_rejects_a_page_size_beyond_the_bound() -> None:
    service = marketplace_service_with(5)
    with pytest.raises(ValueError, match="^plugin_catalog_signature_invalid$"):
        service.search_catalog("", {}, None, 101)


def test_search_catalog_cursor_advances_without_repeating_or_skipping_entries() -> None:
    service = marketplace_service_with(5)

    first = service.search_catalog("", {}, None, 2)
    assert len(first.items) == 2
    assert first.next_cursor is not None

    second = service.search_catalog("", {}, first.next_cursor, 2)
    assert len(second.items) == 2
    first_ids = {item["plugin_id"] for item in first.items}
    second_ids = {item["plugin_id"] for item in second.items}
    assert first_ids.isdisjoint(second_ids)

    third = service.search_catalog("", {}, second.next_cursor, 2)
    assert len(third.items) == 1
    assert third.next_cursor is None


def test_search_catalog_rejects_a_forged_or_malformed_cursor() -> None:
    service = marketplace_service_with(5)
    with pytest.raises(ValueError, match="^plugin_catalog_cursor_invalid$"):
        service.search_catalog("", {}, "not-a-real-cursor", 2)


def test_search_catalog_rejects_a_cursor_whose_query_no_longer_matches() -> None:
    service = marketplace_service_with(5)
    page = service.search_catalog("", {}, None, 2)
    with pytest.raises(ValueError, match="^plugin_catalog_cursor_invalid$"):
        service.search_catalog("plugin 1", {}, page.next_cursor, 2)


# --- source failure / offline snapshot ------------------------------------


def test_search_catalog_reports_degraded_status_on_a_source_failure() -> None:
    service = _search_service(
        (_FakeCatalogSource((), failure=OSError("marketplace offline")),)
    )

    page = service.search_catalog("", {}, None, 10)

    assert page.status == "degraded"
    assert page.items == ()


def test_search_catalog_serves_the_bundled_offline_snapshot_without_any_sync(
    tmp_path: Path,
) -> None:
    """Catches search depending on a synchronized snapshot that was never fetched."""
    service = compose_plugin_lifecycle_service(
        state_root=tmp_path,
        oauth_key_provider=lambda: b"k" * 32,
    )

    page = service.search_catalog("github", {}, None, 10)

    assert any(item["plugin_id"] == "github.official" for item in page.items)
    assert page.status == "ok"


# --- sync result -----------------------------------------------------------


def test_sync_catalog_reports_a_stable_failure_cause_and_never_installs(
    tmp_path: Path,
) -> None:
    service = compose_plugin_lifecycle_service(
        state_root=tmp_path,
        oauth_key_provider=lambda: b"k" * 32,
    )

    def failing_fetch() -> object:
        raise CatalogFetchError("plugin_catalog_unreachable")

    service._catalog_fetch = failing_fetch

    result = service.sync_catalog()

    assert result.status == CatalogSyncStatus.FAILED
    assert result.error == "plugin_catalog_unreachable"
    assert service.plugin_registry.snapshot().records == ()
    # The bundled snapshot remains fully readable after a failed sync.
    page = service.search_catalog("github", {}, None, 10)
    assert any(item["plugin_id"] == "github.official" for item in page.items)


# --- import_marketplace / install confirmation revision --------------------


def test_import_marketplace_rejects_a_version_the_catalog_no_longer_offers(
    tmp_path: Path,
) -> None:
    service = compose_plugin_lifecycle_service(
        state_root=tmp_path,
        oauth_key_provider=lambda: b"k" * 32,
    )

    with pytest.raises(ValueError, match="^plugin_install_review_changed$"):
        service.import_marketplace("github.official", "0.0.1-stale")


def test_import_marketplace_follows_the_existing_review_for_an_omega_verified_entry(
    tmp_path: Path,
) -> None:
    service = compose_plugin_lifecycle_service(
        state_root=tmp_path,
        oauth_key_provider=lambda: b"k" * 32,
    )
    current_version = service._current_catalog().entry("github.official").plugin_version

    review = service.import_marketplace("github.official", current_version)

    assert review.sha256 == service.review("github.official").sha256


def _remote_mcp_entry(
    plugin_id: str = "vendor.remote",
    *,
    publisher_id: str = "vendor",
    plugin_version: str = "1.0.0",
    remote_origin: str = "https://mcp.vendor.example.com/mcp/v1",
    title: str = "Vendor Remote",
    summary: str = "A publisher-verified remote MCP listing.",
    released_at: str = "2026-08-01T10:30:00Z",
) -> SimpleNamespace:
    """A minimal ``MarketplaceCatalogEntry``-shaped double for a publisher-
    verified remote-MCP listing -- ``trust_tier``/``distribution.kind`` are the
    real enums, since ``import_marketplace`` compares them by identity."""

    return SimpleNamespace(
        plugin_id=plugin_id,
        publisher_id=publisher_id,
        plugin_version=plugin_version,
        title=title,
        summary=summary,
        released_at=released_at,
        trust_tier=TrustTier.PUBLISHER_VERIFIED,
        distribution=SimpleNamespace(
            kind=DistributionKind.REMOTE_MCP,
            remote_origin=remote_origin,
        ),
    )


def test_import_marketplace_rejects_an_unwired_publisher_verified_entry() -> None:
    entry = _remote_mcp_entry("vendor.remote")
    service = object.__new__(PluginLifecycleService)
    service._discovery_peer_provider = None

    class _Store:
        def entry(self, plugin_id: str) -> object:
            assert plugin_id == "vendor.remote"
            return entry

    service._current_catalog = lambda: _Store()

    with pytest.raises(ValueError, match="^plugin_runtime_unsupported$"):
        service.import_marketplace("vendor.remote", "1.0.0")


def test_import_marketplace_returns_a_real_discovery_review_and_issues_no_tool_call() -> None:
    """The success path of `_discovery_review`, exercised through the public
    `import_marketplace` entry point end to end: a wired discovery peer is
    negotiated with, every enumerated capability carries `external_unreviewed`,
    and the peer itself proves no tool/prompt/resource call ever reaches it."""

    peer = discovery_peer(tools=[unreviewed_tool("forecast")])
    entry = _remote_mcp_entry("vendor.remote", remote_origin=peer.endpoint)
    service = object.__new__(PluginLifecycleService)
    service._discovery_peer_provider = lambda _entry: peer
    service.operator_account_key = lambda: "operator"
    service._submit_connection_operation = lambda operation: asyncio.run(operation())

    class _Store:
        def entry(self, plugin_id: str) -> object:
            assert plugin_id == "vendor.remote"
            return entry

    service._current_catalog = lambda: _Store()

    review = service.import_marketplace("vendor.remote", "1.0.0")

    assert type(review) is lifecycle_module.PluginDiscoveryInstallReview
    assert review.plugin_id == "vendor.remote"
    assert review.trust_tier == "publisher_verified"
    capabilities = review.to_dict()["capabilities"]
    assert {item["name"] for item in capabilities} == {"forecast"}
    assert all(item["risk_class"] == "external_unreviewed" for item in capabilities)
    assert peer.operations == [
        "initialize",
        "tools/list",
        "prompts/list",
        "resources/list",
        "resources/templates/list",
    ]
    with pytest.raises(PluginDiscoveryError, match="^plugin_review_required$"):
        asyncio.run(peer.call_tool("forecast", {}))


def test_default_composition_wires_a_real_restricted_discovery_peer_provider(
    tmp_path: Path,
) -> None:
    """The provider is present by default -- compose_plugin_lifecycle_service()'s
    zero-argument (discovery-wise) composition no longer needs a test to
    inject one for a publisher-verified entry to be reviewable at all."""
    service = compose_plugin_lifecycle_service(
        state_root=tmp_path,
        oauth_key_provider=lambda: b"k" * 32,
    )
    assert service._discovery_peer_provider is lifecycle_module._default_discovery_peer_provider

    entry = _remote_mcp_entry("vendor.weather", remote_origin="https://mcp.vendor.example.com")
    peer = service._discovery_peer_provider(entry)
    assert peer.endpoint == "https://mcp.vendor.example.com"


def test_default_composition_performs_restricted_discovery_against_a_real_server(
    tmp_path: Path,
) -> None:
    """End to end from the default composition (no injected discovery peer)
    against a real MCP server: import_marketplace returns a
    PluginDiscoveryInstallReview after exactly initialize plus the four list
    calls the real server received, and nothing else -- the server never
    sees tools/call, prompts/get, or resources/read."""
    import socket
    import threading

    import httpx2
    import uvicorn
    from mcp import types
    from mcp.server import Server

    import mcp_platform.plugin_discovery_transport as transport_module
    from mcp_platform.transports.streamable_http import StreamableHttpClientConfig

    calls: list[str] = []

    async def list_tools(_ctx: object, _params: object) -> types.ListToolsResult:
        calls.append("tools/list")
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name="forecast",
                    title="Forecast",
                    inputSchema={
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                )
            ]
        )

    async def list_prompts(_ctx: object, _params: object) -> types.ListPromptsResult:
        calls.append("prompts/list")
        return types.ListPromptsResult(prompts=[])

    async def list_resources(_ctx: object, _params: object) -> types.ListResourcesResult:
        calls.append("resources/list")
        return types.ListResourcesResult(resources=[])

    async def list_templates(_ctx: object, _params: object) -> types.ListResourceTemplatesResult:
        calls.append("resources/templates/list")
        return types.ListResourceTemplatesResult(resourceTemplates=[])

    async def call_tool(_ctx: object, _params: object) -> types.CallToolResult:
        calls.append("tools/call")
        raise AssertionError("a discovery-only client must never call a tool")

    async def get_prompt(_ctx: object, _params: object) -> types.GetPromptResult:
        calls.append("prompts/get")
        raise AssertionError("a discovery-only client must never fetch a prompt")

    async def read_resource(_ctx: object, _params: object) -> types.ReadResourceResult:
        calls.append("resources/read")
        raise AssertionError("a discovery-only client must never read a resource")

    server = Server(
        "vendor.weather",
        version="1.0.0",
        on_list_tools=list_tools,
        on_call_tool=call_tool,
        on_list_prompts=list_prompts,
        on_get_prompt=get_prompt,
        on_list_resources=list_resources,
        on_list_resource_templates=list_templates,
        on_read_resource=read_resource,
    )
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    peer_owner = uvicorn.Server(
        uvicorn.Config(
            server.streamable_http_app(json_response=True, stateless_http=True, host="127.0.0.1"),
            host="127.0.0.1",
            port=port,
            access_log=False,
            log_level="critical",
        )
    )
    loopback_url = f"http://127.0.0.1:{port}/mcp"
    server_thread = threading.Thread(target=lambda: asyncio.run(peer_owner.serve()), daemon=True)
    server_thread.start()
    try:
        while not peer_owner.started:
            pass

        async def route(request: httpx2.Request) -> httpx2.Response:
            async with httpx2.AsyncClient() as forward:
                response = await forward.post(
                    loopback_url,
                    content=request.content,
                    headers={
                        name: value
                        for name, value in request.headers.items()
                        if name.casefold()
                        in {"accept", "content-type", "mcp-protocol-version", "mcp-method", "mcp-name"}
                    },
                )
            return httpx2.Response(
                response.status_code,
                headers={"content-type": response.headers["content-type"]},
                content=response.content,
            )

        # ``_for_loopback_test`` (not ``_for_https_test``): the latter's
        # "a caller-supplied transport implies authenticated" test-only
        # quirk would make ``build_streamable_http_transport`` report
        # ``DiscoverySource.LIVE_AUTHENTICATED`` even for a bare
        # ``MockTransport(route)`` with no real auth attached --
        # ``CurrentMcpClient._discovery_sdk_client_builder`` (correctly)
        # only ever accepts ``LIVE_OBSERVED`` for a discovery-only client,
        # matching what a genuinely unauthenticated production connection
        # produces.
        loopback_config = StreamableHttpClientConfig._for_loopback_test(httpx2.MockTransport(route))
        original_unauthenticated = transport_module.StreamableHttpClientConfig.unauthenticated
        transport_module.StreamableHttpClientConfig.unauthenticated = classmethod(
            lambda cls: loopback_config
        )
        try:
            entry = _remote_mcp_entry(
                "vendor.weather",
                publisher_id="vendor",
                remote_origin="https://mcp.vendor.example.com",
            )
            service = compose_plugin_lifecycle_service(
                state_root=tmp_path,
                oauth_key_provider=lambda: b"k" * 32,
            )
            service._current_catalog = lambda: SimpleNamespace(entry=lambda plugin_id: entry)
            service.operator_account_key = lambda: "operator"
            service._submit_connection_operation = lambda operation: asyncio.run(operation())

            review = service.import_marketplace("vendor.weather", "1.0.0")
        finally:
            transport_module.StreamableHttpClientConfig.unauthenticated = original_unauthenticated
    finally:
        peer_owner.should_exit = True
        server_thread.join(timeout=5)

    assert calls == [
        "tools/list",
        "prompts/list",
        "resources/list",
        "resources/templates/list",
    ]
    assert type(review) is lifecycle_module.PluginDiscoveryInstallReview
    assert review.plugin_id == "vendor.weather"
    assert review.trust_tier == "publisher_verified"
    capabilities = review.to_dict()["capabilities"]
    assert {item["name"] for item in capabilities} == {"forecast"}
    assert all(item["risk_class"] == "external_unreviewed" for item in capabilities)


# --- publisher-verified durable install (Task 6b) ---------------------------


def _wire_publisher_verified_discovery(
    service: PluginLifecycleService, entry: object, peer: object
) -> None:
    service._discovery_peer_provider = lambda _entry: peer
    service._current_catalog = lambda: SimpleNamespace(entry=lambda plugin_id: entry)
    service.operator_account_key = lambda: "operator"
    service._submit_connection_operation = lambda operation: asyncio.run(operation())


def test_publisher_verified_install_completes_the_durable_sealed_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The full product-path lifecycle for a locally sealed publisher-verified
    package: import -> discovery review -> install(confirmation) -> registry
    row with the sealed tree -> the sealed authority carries an
    external_unreviewed capability -> a runtime catalog compilation for the
    enabled plugin compiles a real ``PluginToolBinding`` with the literal
    input schema -> the planner accepts it as a candidate at
    ``external_unreviewed`` risk -> connecting the app through the ordinary
    no-auth connection path -> one read-only tool call executed through the
    ordinary ``PluginRuntime``/execution-worker path -> ``disconnect()``
    succeeds -> restart reverifies -> uninstall leaves nothing.
    """
    from mcp_platform.plugin_catalog import compile_plugin_runtime_catalog
    from plugin_platform.installer import read_local_seal_document

    peer = discovery_peer(tools=[unreviewed_tool("forecast")])
    entry = _remote_mcp_entry("vendor.remote", remote_origin=peer.endpoint)

    service = compose_plugin_lifecycle_service(
        state_root=tmp_path,
        oauth_key_provider=lambda: b"k" * 32,
    )
    _wire_publisher_verified_discovery(service, entry, peer)

    review = service.import_marketplace("vendor.remote", "1.0.0")
    assert type(review) is lifecycle_module.PluginDiscoveryInstallReview

    result = service.install("vendor.remote", confirmation=review.sha256)
    assert result.record.plugin_id == "vendor.remote"
    assert result.record.status == "installed"
    assert result.record.selected_version == "1.0.0"

    row = service.plugin_registry.get("vendor.remote")
    assert row.selected_version == "1.0.0"
    installed_version = next(
        item for item in row.versions if item.plugin_version == "1.0.0"
    )
    assert installed_version.signing_key_id == "omega.unsealed"

    verified = service.plugin_registry.reverify_installed("vendor.remote")
    from plugin_platform.catalog import TrustTier as _TrustTier

    assert verified.trust_tier is _TrustTier.PUBLISHER_VERIFIED
    # The sealed authority itself carries the discovered capability at
    # external_unreviewed risk, with the literal schema Task 6c seals
    # (mcp_platform.plugin_discovery.DiscoveredCapability, plugin_platform.
    # local_trust._capability_requirements) -- this is what a runtime catalog
    # compilation draws an external_unreviewed PluginToolBinding from,
    # reusing compile_plugin_tool_bindings's own signed-authority path
    # unmodified (no second compiler).
    authority = verified.capability_authority["vendor.remote"]
    forecast_policy = authority["tools"]["forecast"]
    assert forecast_policy["risk_class"] == "external_unreviewed"
    assert dict(forecast_policy["input_schema"]) == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    # A fresh install is not enabled until something (here, the test itself,
    # standing in for the connect step) asks for it -- compile_plugin_runtime_
    # catalog only ever compiles bindings for an enabled plugin. It must not
    # raise for an enabled, sealed publisher-verified plugin either way.
    service._set_enabled("vendor.remote", True)
    catalog = compile_plugin_runtime_catalog(
        plugin_snapshot=service.plugin_registry.snapshot(),
        app_snapshot=service.app_registry.snapshot(),
        descriptor_for_app=service.descriptor_for_plugin_app,
        account_key_hash="a" * 64,
        connection_revision_provider=lambda *_args, **_kwargs: "0" * 64,
        verified_plugin_provider=service._installed_verified,
    )
    assert catalog.plugin_registry_revision == service.plugin_registry.snapshot().revision

    # The sealed capability compiled a real, literal-schema tool binding --
    # not merely "did not raise" -- and the planner accepts it as a
    # candidate at exactly the sealed external_unreviewed risk.
    binding = next(
        entry for entry in catalog.entries if entry.capability == "forecast"
    )
    assert binding.risk_class is RiskClass.EXTERNAL_UNREVIEWED
    assert dict(binding.input_schema) == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    assert plugin_binding_is_plannable(binding)
    candidates = retrieve_plugin_candidates("forecast the weather", (binding,))
    assert candidates == (binding,)

    # Connect the sealed app through the ordinary no-auth connection path,
    # execute one read-only tool call through the ordinary PluginRuntime/
    # execution-worker path against the auth_none fake provider, then
    # disconnect -- all with the connection scope and permission contract
    # this test's own discovery review already established, unchanged.
    execution_worker = PluginExecutionWorker(
        child_handler=_sealed_forecast_worker_operation,
        shutdown_timeout_seconds=10.0,
    )
    connect_broker = _Broker()
    connect_grants = _GrantService(connect_broker)
    connect_grants.authority_store = PluginAuthorityStore(
        path=tmp_path / "sealed-connect-authority.json"
    )
    connection_service = PluginConnectionService(
        app_registry=service.app_registry,
        plugin_registry=service.plugin_registry,
        broker=connect_broker,
        grant_service=connect_grants,
        descriptor_provider=service.descriptor_for_plugin_app,
        requirement_provider=service._connection_requirement,
        offered_scopes_provider=lambda *_args: (),
        operation_submitter=_direct_submit,
        transport_authority=object(),
        execution_worker=execution_worker,
    )
    started = asyncio.run(
        connection_service.start(
            "vendor.remote",
            app_id="vendor.remote",
            account_key="operator-account",
            **full_connection_grant(connection_service, "vendor.remote", "vendor.remote"),
        )
    )
    assert started.status == "connected"
    assert started.authorization_url is None

    # A live authority (real digests this exact connection produced, not
    # the placeholder ones used to prove planner candidacy above) is what
    # execution must be validated against -- recompile the catalog with it
    # through the same compile_plugin_tool_bindings/compile_plugin_runtime_
    # catalog path, never a second one.
    preparation = asyncio.run(
        connection_service.prepare_execution_worker(
            "vendor.remote",
            app_id="vendor.remote",
            account_key="operator-account",
            capability_kind="tool",
            capability_id="forecast",
        )
    )
    execution_catalog = compile_plugin_runtime_catalog(
        plugin_snapshot=service.plugin_registry.snapshot(),
        app_snapshot=service.app_registry.snapshot(),
        descriptor_for_app=service.descriptor_for_plugin_app,
        account_key_hash=started.account_key_sha256,
        app_authority_provider=lambda *_args: PluginCatalogAppAuthority(
            app_id="vendor.remote",
            account_key_hash=started.account_key_sha256,
            connection_revision=preparation.authority.connection_revision,
            authority_revision=preparation.authority.authority_revision,
            capability_revision=preparation.authority.capability_revision,
        ),
        verified_plugin_provider=service._installed_verified,
    )
    execution_binding = next(
        entry for entry in execution_catalog.entries if entry.capability == "forecast"
    )

    backend = CurrentMcpPluginBackend(
        connection_service=connection_service,
        descriptor_provider=service.descriptor_for_plugin_app,
        plugin_id_for_app={"vendor.remote": "vendor.remote"},
        account_key_provider=lambda: "operator-account",
        operation_submitter=_worker_submit,
        execution_worker=execution_worker,
    )
    runtime = PluginRuntime(
        plugin_registry=service.plugin_registry,
        app_registry=service.app_registry,
        backend=backend,
        catalog_provider=lambda: execution_catalog,
    )
    call_result = runtime.invoke_tool(
        PluginOperationRequest(
            plugin_id="vendor.remote",
            app_id="vendor.remote",
            capability="forecast",
            arguments={},
            candidate_id=execution_binding.candidate_id,
            account_key_hash=execution_binding.account_key_hash,
            package_revision=execution_binding.package_revision,
            connection_revision=execution_binding.connection_revision,
            catalog_authority_revision=execution_binding.authority_revision,
            capability_revision=execution_binding.capability_revision,
            binding_revision=execution_binding.binding_revision,
        )
    )
    assert call_result.to_dict()["data"]["structured_content"] == {"forecast": "sunny"}
    backend.close()

    connection_coordinator = PluginConnectionCoordinator(
        plugin_registry=service.plugin_registry,
        connection_service=connection_service,
    )
    disconnected = asyncio.run(
        connection_coordinator.disconnect("vendor.remote", "operator-account")
    )
    assert disconnected.state.value != "connected"
    execution_worker.close()

    # A portable/offline restart: no operator account configured on this
    # machine's environment either (matching _uninstall_locked's own
    # documented tolerance for that case) -- delete it so following this
    # device's actual state isn't accidental.
    monkeypatch.delenv("OMEGA_OPERATOR_ACCOUNT_KEY", raising=False)

    # Restart with the same vault key: reverification succeeds unchanged. No
    # discovery/account wiring is needed here -- reverify_installed only ever
    # touches the registry and this device's local-trust root.
    restarted = compose_plugin_lifecycle_service(
        state_root=tmp_path,
        oauth_key_provider=lambda: b"k" * 32,
    )
    restarted_verified = restarted.plugin_registry.reverify_installed("vendor.remote")
    assert restarted_verified.trust_tier is _TrustTier.PUBLISHER_VERIFIED
    assert restarted_verified.package_sha256 == verified.package_sha256

    # Negative: a different vault key on restart fails closed.
    different_key_service = compose_plugin_lifecycle_service(
        state_root=tmp_path,
        oauth_key_provider=lambda: b"z" * 32,
    )
    with pytest.raises(PluginRegistryError, match="^plugin_registry_reconciliation_failed$"):
        different_key_service.plugin_registry.reverify_installed("vendor.remote")

    manifest_sha256 = installed_version.manifest_sha256
    assert (
        read_local_seal_document(service.plugin_registry.state_root, manifest_sha256)
        is not None
    )

    # Uninstall through the ordinary path. No operator account is configured
    # on this instance (matching a portable/offline install per _uninstall_
    # locked's own docstring), so the connect/disconnect step this test never
    # exercised is tolerated exactly as it already is for any other plugin.
    app_ids = tuple(restarted.app_ids_for_plugin("vendor.remote"))
    assert app_ids
    journal_path = restarted.app_registry.state_root / "app_provision_journal.json"
    assert set(app_ids) <= _protected_app_ids(json.loads(journal_path.read_bytes()))

    asyncio.run(restarted.uninstall("vendor.remote"))

    with pytest.raises(Exception):
        restarted.plugin_registry.get("vendor.remote")
    assert (
        read_local_seal_document(service.plugin_registry.state_root, manifest_sha256)
        is None
    )
    assert not (
        service.plugin_registry.packages_root / "vendor.remote"
    ).exists()
    assert not [
        record
        for record in restarted.app_registry.snapshot().records
        if record.app_id in app_ids
    ]
    # "Leaves nothing" has to mean every durable document, not just the two the
    # uninstall happens to write. Uninstall removes the app record through the
    # registry's exact CAS, which the provision journal knows nothing about, so
    # its transaction stayed committed and kept naming an app that no longer
    # existed -- which keeps that app in the protected set and silently blocks
    # the NEXT uninstall. A re-release at the same version then leaves two
    # committed transactions disagreeing about one app, and rollback refuses
    # that outright. Observed live on 2026-09-07 after a Slack uninstall left
    # two such entries behind.
    #
    # Scanning every state document rather than naming the stores by hand is
    # deliberate: it fails for a NEW surface someone adds later without
    # teaching uninstall about it, which is exactly how this one was missed.
    names = ("vendor.remote", *app_ids)
    survivors = {
        path.relative_to(tmp_path).as_posix(): [n for n in names if n in path.read_text("utf-8")]
        for path in sorted(tmp_path.rglob("*.json"))
        if path.is_file() and any(n in path.read_text("utf-8") for n in names)
    }
    assert survivors == {}, survivors


def test_publisher_verified_install_rejects_a_confirmation_that_does_not_match(
    tmp_path: Path,
) -> None:
    from plugin_platform.installer import PluginInstallError

    peer = discovery_peer(tools=[unreviewed_tool("forecast")])
    entry = _remote_mcp_entry("vendor.remote", remote_origin=peer.endpoint)
    service = compose_plugin_lifecycle_service(
        state_root=tmp_path,
        oauth_key_provider=lambda: b"k" * 32,
    )
    _wire_publisher_verified_discovery(service, entry, peer)
    service.import_marketplace("vendor.remote", "1.0.0")

    with pytest.raises(PluginInstallError, match="^plugin_install_confirmation_mismatch$"):
        service.install("vendor.remote", confirmation="0" * 64)

    with pytest.raises(PluginRegistryError):
        service.plugin_registry.get("vendor.remote")


def test_publisher_verified_install_never_grants_omega_verified_on_tamper(
    tmp_path: Path,
) -> None:
    """A tampered on-disk local seal fails closed at the next reverification,
    never silently promoting the row to a higher tier."""
    import json as _json

    from plugin_platform.catalog import TrustTier as _TrustTier

    peer = discovery_peer(tools=[unreviewed_tool("forecast")])
    entry = _remote_mcp_entry("vendor.remote", remote_origin=peer.endpoint)
    service = compose_plugin_lifecycle_service(
        state_root=tmp_path,
        oauth_key_provider=lambda: b"k" * 32,
    )
    _wire_publisher_verified_discovery(service, entry, peer)
    review = service.import_marketplace("vendor.remote", "1.0.0")
    service.install("vendor.remote", confirmation=review.sha256)

    row = service.plugin_registry.get("vendor.remote")
    manifest_sha256 = row.versions[0].manifest_sha256
    seal_name = next(
        path
        for path in service.plugin_registry.state_root.iterdir()
        if path.name.startswith(".local-seal-") and manifest_sha256 in path.name
    )
    document = _json.loads(seal_name.read_bytes())
    document["trust_tier"] = "omega_verified"
    seal_name.write_bytes(_json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8"))

    with pytest.raises(PluginRegistryError, match="^plugin_registry_reconciliation_failed$"):
        service.plugin_registry.reverify_installed("vendor.remote")


def test_publisher_verified_install_rejects_a_tampered_sealed_schema(
    tmp_path: Path,
) -> None:
    """A literal input_schema tampered inside the installed tree's
    tools/authority.json fails the next reverification -- the sealed schema
    is under the same asset/authority integrity checks as every other sealed
    byte, never trusted unread from disk."""
    import json as _json

    peer = discovery_peer(tools=[unreviewed_tool("forecast")])
    entry = _remote_mcp_entry("vendor.remote", remote_origin=peer.endpoint)
    service = compose_plugin_lifecycle_service(
        state_root=tmp_path,
        oauth_key_provider=lambda: b"k" * 32,
    )
    _wire_publisher_verified_discovery(service, entry, peer)
    review = service.import_marketplace("vendor.remote", "1.0.0")
    service.install("vendor.remote", confirmation=review.sha256)

    authority_path = (
        service.plugin_registry.packages_root
        / "vendor.remote"
        / "1.0.0"
        / "tools"
        / "authority.json"
    )
    document = _json.loads(authority_path.read_bytes())
    document["apps"]["vendor.remote"]["tools"]["forecast"]["input_schema"]["properties"][
        "injected"
    ] = {"type": "string"}
    authority_path.write_bytes(
        _json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )

    with pytest.raises(PluginRegistryError, match="^plugin_registry_reconciliation_failed$"):
        service.plugin_registry.reverify_installed("vendor.remote")


# --- update failure preserves the old version -------------------------------


def test_update_failure_preserves_the_old_selected_version() -> None:
    service = installed_service(version="1.0.0", candidate="2.0.0")

    def failing_update_locked(plugin_id: str, *, version: str, confirmation: object) -> object:
        raise ValueError("plugin_update_failed")

    service._update_locked = failing_update_locked

    with pytest.raises(ValueError, match="^plugin_update_failed$"):
        service.update("weather", "2.0.0", "e" * 64)

    assert service.plugin_registry.get("weather").selected_version == "1.0.0"


# --- update_review compares provenance and the network/filesystem boundary --


def test_update_review_reports_a_provenance_only_change() -> None:
    """Catches a re-issued provenance record passing review because the
    manifest/package digests happen to be unchanged."""

    service = installed_service(
        version="1.0.0",
        candidate="2.0.0",
        installed_manifest_sha256="1" * 64,
        installed_package_sha256="2" * 64,
        candidate_manifest_sha256="1" * 64,
        candidate_package_sha256="2" * 64,
        installed_provenance=_provenance_record(provider_id="acme.weather"),
        candidate_provenance=_provenance_record(provider_id="acme.weather.v2"),
    )

    review = service.update_review("weather", "2.0.0")

    assert "manifest_changed" not in review.permission_changes
    assert "package_changed" not in review.permission_changes
    assert "provenance_changed" in review.permission_changes


def test_update_review_reports_an_origin_only_change() -> None:
    """Catches an endpoint move passing review because every digest and
    provenance record is unchanged."""

    provenance = _provenance_record()
    service = installed_service(
        version="1.0.0",
        candidate="2.0.0",
        installed_manifest_sha256="1" * 64,
        installed_package_sha256="2" * 64,
        candidate_manifest_sha256="1" * 64,
        candidate_package_sha256="2" * 64,
        installed_provenance=provenance,
        candidate_provenance=provenance,
        installed_required_apps=(
            _app_requirement(remote_origin="https://old.weather.example.com"),
        ),
        candidate_required_apps=(
            _app_requirement(remote_origin="https://new.weather.example.com"),
        ),
    )

    review = service.update_review("weather", "2.0.0")

    assert "manifest_changed" not in review.permission_changes
    assert "package_changed" not in review.permission_changes
    assert "provenance_changed" not in review.permission_changes
    assert "endpoint_changed:weather.mcp" in review.permission_changes
    assert "network_boundary_changed" in review.permission_changes


def test_update_review_reports_no_filesystem_boundary_for_a_remote_only_candidate() -> None:
    """Ruling: a remote-only candidate never reports a filesystem boundary."""

    provenance = _provenance_record()
    service = installed_service(
        version="1.0.0",
        candidate="2.0.0",
        installed_provenance=provenance,
        candidate_provenance=provenance,
        candidate_required_apps=(_app_requirement(),),
    )

    review = service.update_review("weather", "2.0.0")

    assert not any(
        item.startswith("filesystem_boundary") for item in review.permission_changes
    )


def test_update_review_reports_a_filesystem_boundary_change_for_a_local_app() -> None:
    provenance = _provenance_record()
    service = installed_service(
        version="1.0.0",
        candidate="2.0.0",
        installed_manifest_sha256="1" * 64,
        installed_package_sha256="2" * 64,
        candidate_manifest_sha256="3" * 64,
        candidate_package_sha256="4" * 64,
        installed_provenance=provenance,
        candidate_provenance=provenance,
        installed_required_apps=(
            _app_requirement(
                "weather.local", deployment=AppDeployment.LOCAL, remote_origin=None
            ),
        ),
        candidate_required_apps=(
            _app_requirement(
                "weather.local", deployment=AppDeployment.LOCAL, remote_origin=None
            ),
        ),
    )

    review = service.update_review("weather", "2.0.0")

    assert "filesystem_boundary_changed" in review.permission_changes


# --- trust filters -----------------------------------------------------------


def test_search_catalog_filters_by_trust_tier() -> None:
    service = marketplace_service_with(6)

    page = service.search_catalog("", {"trust": "publisher_verified"}, None, 20)

    assert page.items
    assert all(item["trust_state"] == "publisher_verified" for item in page.items)


def test_search_catalog_rejects_an_unknown_trust_filter() -> None:
    service = marketplace_service_with(3)
    with pytest.raises(ValueError, match="^plugin_catalog_signature_invalid$"):
        service.search_catalog("", {"trust": "not-a-real-tier"}, None, 12)


# --- merged enterprise source / identity collision --------------------------


def test_search_catalog_merges_an_approved_enterprise_source_into_one_page() -> None:
    marketplace = _FakeCatalogSource((_catalog_plugin_entry("vendor.alpha"),))
    enterprise = _FakeCatalogSource(
        (_catalog_plugin_entry("acme.internal.tool", title="Internal Tool"),),
        source_id="acme.enterprise",
        kind=CatalogSourceKind.ENTERPRISE,
        approval_revision="f" * 64,
    )
    service = _search_service(
        (marketplace, enterprise),
        source_policy=CatalogSourcePolicy(
            developer_mode=False,
            enterprise_approvals={"acme.enterprise": "f" * 64},
        ),
    )

    page = service.search_catalog("", {}, None, 20)

    assert {item["plugin_id"] for item in page.items} == {"vendor.alpha", "acme.internal.tool"}


def test_search_catalog_fails_closed_on_a_cross_source_identity_collision() -> None:
    public_source = _FakeCatalogSource((_catalog_plugin_entry("vendor.alpha"),))
    enterprise_source = _FakeCatalogSource(
        (_catalog_plugin_entry("vendor.alpha"),),
        source_id="acme.enterprise",
        kind=CatalogSourceKind.ENTERPRISE,
        approval_revision="f" * 64,
    )
    service = _search_service(
        (public_source, enterprise_source),
        source_policy=CatalogSourcePolicy(
            developer_mode=False,
            enterprise_approvals={"acme.enterprise": "f" * 64},
        ),
    )

    with pytest.raises(ValueError, match="^plugin_catalog_identity_ambiguous$"):
        service.search_catalog("", {}, None, 20)
