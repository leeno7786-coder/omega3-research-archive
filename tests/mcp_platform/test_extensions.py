from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from mcp import Client, types
from mcp.client.caching import CacheEntry, CacheKey
from mcp.client.subscriptions import (
    PromptsListChanged,
    ResourceUpdated,
    ResourcesListChanged,
    ToolsListChanged,
)
from mcp.server import Server
from mcp.server.subscriptions import InMemorySubscriptionBus, ListenHandler
from pydantic import ValidationError

from mcp_platform.extensions import (
    MCP_APPS_EXTENSION_IDENTIFIER,
    BoundedResponseCacheStore,
    build_cache_config,
    normalize_subscription_event,
    validate_mcp_apps_metadata,
)
from mcp_platform.package_contracts import MCP_PROTOCOL_VERSION


def test_cache_config_is_bounded_nonsecret_and_partitioned_by_authorization_context() -> None:
    now = [100.0]
    first = build_cache_config(
        app_id="com.example.calendar",
        connection_revision="a" * 64,
        authorization_context_revision="b" * 64,
        max_entries=2,
        max_ttl_seconds=5,
        clock=lambda: now[0],
    )
    second = build_cache_config(
        app_id="com.example.calendar",
        connection_revision="a" * 64,
        authorization_context_revision="c" * 64,
        max_entries=2,
        max_ttl_seconds=5,
        clock=lambda: now[0],
    )

    assert first.partition != second.partition
    assert first.target_id == second.target_id == "a" * 64
    assert first.default_ttl_ms == 0
    assert first.share_public is False
    assert len(first.partition) == 64
    assert "com.example.calendar" not in first.partition
    assert "b" * 64 not in first.partition
    assert isinstance(first.store, BoundedResponseCacheStore)

    with pytest.raises(ValueError, match="authorization_context_revision"):
        build_cache_config(
            app_id="com.example.calendar",
            connection_revision="a" * 64,
            authorization_context_revision="Bearer secret-token",
            max_entries=2,
            max_ttl_seconds=5,
        )


def test_cache_store_caps_ttl_and_entry_count_while_missing_ttl_stays_uncached() -> None:
    async def exercise() -> None:
        now = [100.0]
        config = build_cache_config(
            app_id="com.example.calendar",
            connection_revision="a" * 64,
            authorization_context_revision="b" * 64,
            max_entries=2,
            max_ttl_seconds=5,
            clock=lambda: now[0],
        )
        store = config.store
        assert isinstance(store, BoundedResponseCacheStore)

        first = CacheKey(method="tools/list", params_key="one", partition=config.partition)
        second = CacheKey(method="tools/list", params_key="two", partition=config.partition)
        third = CacheKey(method="tools/list", params_key="three", partition=config.partition)
        await store.set(first, CacheEntry(value=1, scope="private", expires_at=1_000.0))
        await store.set(second, CacheEntry(value=2, scope="private", expires_at=1_000.0))
        entry = await store.get(first)
        assert entry is not None
        assert entry.expires_at == 105.0
        await store.set(third, CacheEntry(value=3, scope="private", expires_at=1_000.0))
        assert await store.get(first) is not None
        assert await store.get(second) is None
        assert await store.get(third) is not None

        # The SDK treats default_ttl_ms=0 as no cache write when ttlMs is absent.
        assert config.default_ttl_ms == 0
        with pytest.raises(ValueError, match="max_ttl_seconds"):
            build_cache_config(
                app_id="com.example.calendar",
                connection_revision="a" * 64,
                authorization_context_revision="b" * 64,
                max_entries=2,
                max_ttl_seconds=0,
            )

    asyncio.run(exercise())


def test_subscription_events_are_closed_bounded_metadata_without_wire_objects() -> None:
    assert normalize_subscription_event(ToolsListChanged()) == {"kind": "tools_list_changed"}
    assert normalize_subscription_event(PromptsListChanged()) == {"kind": "prompts_list_changed"}
    assert normalize_subscription_event(ResourcesListChanged()) == {
        "kind": "resources_list_changed"
    }
    assert normalize_subscription_event(ResourceUpdated(uri="docs://calendar/1")) == {
        "kind": "resource_updated",
        "uri": "docs://calendar/1",
    }
    with pytest.raises(ValueError, match="subscription event"):
        normalize_subscription_event(object())
    with pytest.raises(ValueError, match="subscription event"):
        normalize_subscription_event(ResourceUpdated(uri="x" * 4_097))
    for invalid_uri in (
        "not a URI",
        "docs://CALENDAR/1",
        "docs://calendar/entry/../1",
        "docs://calendar/%31",
        "docs://calendar/1\rserver secret",
    ):
        with pytest.raises(ValueError, match="subscription event"):
            normalize_subscription_event(ResourceUpdated(uri=invalid_uri))


def test_mcp_apps_metadata_requires_exact_signed_declaration_and_never_executes_values() -> None:
    calls: list[str] = []

    def executable() -> str:
        calls.append("executed")
        return "ui://calendar/widget"

    metadata = {
        "ui": {
            "resourceUri": "ui://calendar/widget",
            "mimeType": "text/html",
            "title": "Calendar widget",
        }
    }
    validated = validate_mcp_apps_metadata(
        metadata,
        signed_metadata=metadata,
        declared_extensions={MCP_APPS_EXTENSION_IDENTIFIER: "1.0.0"},
        max_bytes=1_024,
    )
    assert validated == metadata
    assert validated is not metadata

    invalid = [
        {"ui": {"resourceUri": executable}},
        {"ui": {"resourceUri": "javascript:alert(1)"}},
        {"ui": {"resourceUri": "ui://calendar/widget", "html": "<script>x</script>"}},
        {"ui": {"resourceUri": "ui://calendar/widget", "onMessage": "run"}},
    ]
    for candidate in invalid:
        with pytest.raises(ValueError, match="MCP Apps metadata"):
            validate_mcp_apps_metadata(
                candidate,
                signed_metadata=candidate,
                declared_extensions={MCP_APPS_EXTENSION_IDENTIFIER: "1.0.0"},
                max_bytes=1_024,
            )
    assert calls == []

    with pytest.raises(ValueError, match="MCP Apps metadata"):
        validate_mcp_apps_metadata(
            metadata,
            signed_metadata={"ui": {"resourceUri": "ui://other/widget"}},
            declared_extensions={MCP_APPS_EXTENSION_IDENTIFIER: "1.0.0"},
            max_bytes=1_024,
        )


def test_mcp_apps_metadata_rejects_aggregate_structure_before_recursive_copy() -> None:
    metadata = {
        "first": {f"a{index}": index for index in range(64)},
        "second": {f"b{index}": index for index in range(64)},
    }

    with pytest.raises(ValueError, match="aggregate item limit"):
        validate_mcp_apps_metadata(
            metadata,
            signed_metadata=metadata,
            declared_extensions={MCP_APPS_EXTENSION_IDENTIFIER: "1.0.0"},
            max_bytes=65_536,
        )
    with pytest.raises(ValueError, match="MCP Apps metadata"):
        validate_mcp_apps_metadata(
            metadata,
            signed_metadata=metadata,
            declared_extensions={},
            max_bytes=1_024,
        )


def test_extensions_module_has_no_rendering_or_app_message_execution_surface() -> None:
    source = Path("mcp_platform/extensions.py").read_text(encoding="utf-8")
    forbidden = (
        "eval(",
        "exec(",
        "render_html",
        "post_message",
        "app_message",
        "webview",
    )
    assert not any(term in source for term in forbidden)


def test_bounded_cache_store_uses_the_public_async_store_seams() -> None:
    async def exercise() -> None:
        store = BoundedResponseCacheStore(max_entries=1, max_ttl_seconds=1)
        key = CacheKey(method="tools/list")
        await store.set(key, CacheEntry(value={"ok": True}, scope="private", expires_at=None))
        assert await store.get(key) is None
        await store.delete(key)
        assert await store.get(key) is None
        await store.clear()

    asyncio.run(exercise())


def test_cache_clear_generation_rejects_captured_stale_write_without_rejecting_fresh_write() -> (
    None
):
    async def exercise() -> None:
        store = BoundedResponseCacheStore(max_entries=4, max_ttl_seconds=30)
        key = CacheKey(method="tools/list", params_key="", partition="private")
        stale_captured = asyncio.Event()
        release_stale = asyncio.Event()

        async def stale_fetch() -> None:
            assert await store.get(key) is None
            stale_captured.set()
            await release_stale.wait()
            await store.set(
                key,
                CacheEntry(value="stale", scope="private", expires_at=1_000_000.0),
            )

        stale = asyncio.create_task(stale_fetch())
        await stale_captured.wait()
        await store.clear()

        async def fresh_fetch() -> None:
            assert await store.get(key) is None
            await store.set(
                key,
                CacheEntry(value="fresh", scope="private", expires_at=1_000_000.0),
            )

        await asyncio.create_task(fresh_fetch())
        release_stale.set()
        await stale
        entry = await store.get(key)
        assert entry is not None
        assert entry.value == "fresh"

    asyncio.run(exercise())


def test_cache_key_invalidation_is_partitioned_and_cancellation_leaves_no_owner_state() -> None:
    async def exercise() -> None:
        store = BoundedResponseCacheStore(max_entries=2, max_ttl_seconds=30)
        first = CacheKey(method="resources/read", params_key="docs://one", partition="private")
        second = CacheKey(method="resources/read", params_key="docs://two", partition="private")
        assert await store.get(first) is None
        await store.invalidate(second)
        await store.set(
            first,
            CacheEntry(value="one", scope="private", expires_at=1_000_000.0),
        )
        assert (await store.get(first)).value == "one"  # type: ignore[union-attr]

        entered = asyncio.Event()

        async def cancelled_fetch() -> None:
            assert await store.get(second) is None
            entered.set()
            await asyncio.Event().wait()

        pending = asyncio.create_task(cancelled_fetch())
        await entered.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await store.clear()
        await store.set(
            second,
            CacheEntry(value="two", scope="private", expires_at=1_000_000.0),
        )
        assert (await store.get(second)).value == "two"  # type: ignore[union-attr]

    asyncio.run(exercise())


def test_public_sdk_listen_evicts_cache_before_the_event_consumer_refetches() -> None:
    async def exercise() -> None:
        now = [100.0]
        list_calls = 0
        bus = InMemorySubscriptionBus()

        async def list_tools(
            _context: object,
            _params: types.PaginatedRequestParams | None,
        ) -> types.ListToolsResult:
            nonlocal list_calls
            list_calls += 1
            return types.ListToolsResult(
                tools=[types.Tool(name=f"tool-{list_calls}", inputSchema={"type": "object"})],
                ttlMs=60_000,
                cacheScope="private",
            )

        server = Server(
            "cache-listen-proof",
            version="1.0.0",
            on_list_tools=list_tools,
            on_subscriptions_listen=ListenHandler(bus, max_subscriptions=1),
        )
        cache = build_cache_config(
            app_id="com.example.calendar",
            connection_revision="a" * 64,
            authorization_context_revision="b" * 64,
            max_entries=2,
            max_ttl_seconds=5,
            clock=lambda: now[0],
        )
        async with Client(
            server,
            mode=MCP_PROTOCOL_VERSION,
            client_info=types.Implementation(name="cache-listen-proof", version="1.0.0"),
            extensions=(),
            cache=cache,
        ) as connected:
            first = await connected.list_tools(cache_mode="use")
            cached = await connected.list_tools(cache_mode="use")
            assert first.tools[0].name == cached.tools[0].name == "tool-1"
            assert list_calls == 1

            async with connected.listen(tools_list_changed=True) as subscription:
                await bus.publish(ToolsListChanged())
                event = await anext(subscription)
                assert type(event) is ToolsListChanged
                refreshed = await connected.list_tools(cache_mode="use")
                assert refreshed.tools[0].name == "tool-2"
                assert list_calls == 2

            # The Omega store's shorter cap wins over a longer server TTL.
            now[0] += 6
            expired = await connected.list_tools(cache_mode="use")
            assert expired.tools[0].name == "tool-3"
            assert list_calls == 3

    asyncio.run(exercise())


def test_public_sdk_captured_fetch_cannot_resurrect_after_omega_store_clear() -> None:
    async def exercise() -> None:
        stale_entered = asyncio.Event()
        release_stale = asyncio.Event()
        calls = 0

        async def list_tools(
            _context: object,
            _params: types.PaginatedRequestParams | None,
        ) -> types.ListToolsResult:
            nonlocal calls
            calls += 1
            call = calls
            if call == 1:
                stale_entered.set()
                await release_stale.wait()
            return types.ListToolsResult(
                tools=[
                    types.Tool(
                        name="stale" if call == 1 else "fresh",
                        inputSchema={"type": "object"},
                    )
                ],
                ttlMs=60_000,
                cacheScope="private",
            )

        server = Server("cache-race-proof", version="1.0.0", on_list_tools=list_tools)
        cache = build_cache_config(
            app_id="com.example.calendar",
            connection_revision="a" * 64,
            authorization_context_revision="b" * 64,
            max_entries=4,
            max_ttl_seconds=30,
        )
        async with Client(
            server,
            mode=MCP_PROTOCOL_VERSION,
            client_info=types.Implementation(name="cache-race-proof", version="1.0.0"),
            extensions=(),
            cache=cache,
        ) as connected:
            stale = asyncio.create_task(connected.list_tools(cache_mode="use"))
            await stale_entered.wait()
            await cache.store.clear()
            fresh = await connected.list_tools(cache_mode="use")
            assert fresh.tools[0].name == "fresh"
            release_stale.set()
            assert (await stale).tools[0].name == "stale"
            cached = await connected.list_tools(cache_mode="use")
            assert cached.tools[0].name == "fresh"
            assert calls == 2

    asyncio.run(exercise())


def test_public_sdk_missing_ttl_is_not_cached_and_invalid_ttl_is_rejected() -> None:
    with pytest.raises(ValidationError):
        types.ListToolsResult(tools=[], ttlMs=-1)

    async def exercise() -> None:
        list_calls = 0

        async def list_tools(
            _context: object,
            _params: types.PaginatedRequestParams | None,
        ) -> types.ListToolsResult:
            nonlocal list_calls
            list_calls += 1
            return types.ListToolsResult(
                tools=[types.Tool(name=f"tool-{list_calls}", inputSchema={"type": "object"})]
            )

        server = Server("missing-ttl-proof", version="1.0.0", on_list_tools=list_tools)
        cache = build_cache_config(
            app_id="com.example.calendar",
            connection_revision="a" * 64,
            authorization_context_revision="b" * 64,
            max_entries=2,
            max_ttl_seconds=5,
        )
        async with Client(
            server,
            mode=MCP_PROTOCOL_VERSION,
            client_info=types.Implementation(name="missing-ttl-proof", version="1.0.0"),
            extensions=(),
            cache=cache,
        ) as connected:
            first = await connected.list_tools(cache_mode="use")
            second = await connected.list_tools(cache_mode="use")

        assert first.tools[0].name == "tool-1"
        assert second.tools[0].name == "tool-2"
        assert list_calls == 2

    asyncio.run(exercise())
