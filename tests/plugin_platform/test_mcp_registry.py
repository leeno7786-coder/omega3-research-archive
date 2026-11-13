"""Bounded, fail-closed ingestion of the Official MCP Registry as discovery input."""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from mcp_platform.canonical import canonical_bytes, sha256_hex
from plugin_platform.catalog import TrustTier
from plugin_platform.mcp_registry import (
    MAX_ARGUMENTS,
    MAX_ENVIRONMENT_VARIABLES,
    MAX_EXTENSION_BYTES,
    MAX_HEADERS,
    MAX_PACKAGES,
    MAX_PAGE_BYTES,
    MAX_PAGE_SERVERS,
    MAX_REGISTRY_PAGES,
    MAX_REMOTES,
    McpRegistryClient,
    McpRegistryError,
    McpRegistryRequest,
    McpRegistryServer,
    NamespaceKind,
    PackageRegistryType,
    RegistryStatus,
    RemoteTransport,
)

_FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "mcp_registry"


def _fixture_document(name: str) -> dict[str, Any]:
    """Return one parsed on-disk registry response fixture."""
    return json.loads((_FIXTURE_ROOT / name).read_text(encoding="utf-8"))


def _paging_transport(documents: tuple[dict[str, Any], ...]):
    """Return a transport chaining these documents by their declared cursors."""
    by_cursor: dict[str | None, dict[str, Any]] = {}
    previous: str | None = None
    for document in documents:
        by_cursor[previous] = document
        previous = document.get("metadata", {}).get("nextCursor")

    def transport(request: McpRegistryRequest) -> bytes:
        try:
            document = by_cursor[request.cursor]
        except KeyError:
            raise McpRegistryError("plugin_registry_page_invalid") from None
        return json.dumps(document).encode("utf-8")

    return transport


def registry_client(*documents: dict[str, Any], **options: Any) -> McpRegistryClient:
    """Return a client reading exactly these in-memory registry responses."""
    return McpRegistryClient(transport=_paging_transport(documents), **options)


def fixture_registry_client(*names: str) -> McpRegistryClient:
    """Return a client paging exactly these registry response fixtures."""
    return registry_client(*(_fixture_document(name) for name in names))


def _entry(index: int = 0, *, name: str = "servers-page-1.json") -> dict[str, Any]:
    """Return one mutable copy of a fixture response entry."""
    return deepcopy(_fixture_document(name)["servers"][index])


def _page(*entries: dict[str, Any], next_cursor: str | None = None) -> dict[str, Any]:
    """Return one registry response envelope carrying exactly these entries."""
    metadata: dict[str, Any] = {"count": len(entries)}
    if next_cursor is not None:
        metadata["nextCursor"] = next_cursor
    return {"servers": list(entries), "metadata": metadata}


def _only_record(document: dict[str, Any]) -> McpRegistryServer:
    """Return the single parsed record of a one-entry response."""
    page = registry_client(document).search()
    assert len(page.servers) == 1
    return page.servers[0]


def _remote_entry(url: str, *, transport: str = "streamable-http") -> dict[str, Any]:
    """Return a fixture entry whose only distribution is this remote."""
    entry = _entry()
    entry["server"].pop("packages")
    entry["server"]["remotes"] = [{"type": transport, "url": url}]
    return entry


def _mcpb_entry() -> dict[str, Any]:
    """Return an entry declaring only a distribution Omega does not support."""
    entry = _entry()
    entry["server"]["name"] = "io.github.example/bundle"
    entry["server"].pop("remotes")
    entry["server"]["packages"] = [
        {
            "registryType": "mcpb",
            "identifier": "https://example.com/bundle.mcpb",
            "transport": {"type": "stdio"},
        }
    ]
    return entry


def cyclic_registry_client() -> McpRegistryClient:
    """Return a client whose second page points its cursor back at itself."""
    cursor = "io.github.example/bundle:1.4.0"
    documents = {
        None: _page(_mcpb_entry(), next_cursor=cursor),
        cursor: _page(_entry(1), next_cursor=cursor),
    }

    def transport(request: McpRegistryRequest) -> bytes:
        return json.dumps(documents[request.cursor]).encode("utf-8")

    return McpRegistryClient(transport=transport)


def _failing_transport(cause: str):
    """Return a transport whose every attempt raises this stable typed cause."""

    def transport(request: McpRegistryRequest) -> bytes:
        raise McpRegistryError(cause)

    return transport


def test_registry_pages_preserve_verified_namespace_without_omega_endorsement() -> None:
    client = fixture_registry_client("servers-page-1.json", "servers-page-2.json")
    records = tuple(client.iter_latest())
    assert records[0].name == "io.github.example/weather"
    assert records[0].namespace_kind == "github"
    assert records[0].trust_tier is TrustTier.PUBLISHER_VERIFIED
    assert all(record.trust_tier is not TrustTier.OMEGA_VERIFIED for record in records)


def test_registry_cursor_cycle_and_unknown_distribution_fail_closed() -> None:
    with pytest.raises(McpRegistryError, match="plugin_registry_page_invalid"):
        tuple(cyclic_registry_client().iter_latest())


def test_registry_yields_the_unknown_distribution_before_the_cursor_cycle_fails() -> None:
    """Catches dropping an unsupported listing instead of stating why it is unusable."""
    records: list[McpRegistryServer] = []
    with pytest.raises(McpRegistryError, match="plugin_registry_page_invalid"):
        for record in cyclic_registry_client().iter_latest():
            records.append(record)
    assert records[0].name == "io.github.example/bundle"
    assert records[0].compatible is False
    assert "plugin_registry_package_registry_unsupported" in records[0].unsupported_reasons


def test_registry_iter_latest_skips_superseded_and_deleted_listings() -> None:
    """Catches surfacing an old version or a registry-deleted listing as installable."""
    client = fixture_registry_client("servers-page-1.json", "servers-page-2.json")
    identities = tuple((record.name, record.version) for record in client.iter_latest())
    assert identities == (
        ("io.github.example/weather", "1.4.0"),
        ("com.example.analytics/reports", "2.0.0"),
        ("io.github.example/containers", "3.1.2"),
        ("io.github.example/desktop-bundle", "1.0.0"),
    )


def test_registry_search_reports_the_exact_page_the_transport_supplied() -> None:
    """Catches a search that silently filters or renumbers the registry's own page."""
    page = fixture_registry_client("servers-page-1.json").search(query="weather", limit=3)
    assert page.count == 3
    assert page.next_cursor == "io.github.example/weather:0.9.0"
    assert tuple(record.status for record in page.servers) == (
        RegistryStatus.ACTIVE,
        RegistryStatus.ACTIVE,
        RegistryStatus.DEPRECATED,
    )


def test_registry_preserves_package_and_remote_metadata_without_value_text() -> None:
    """Catches leaking publisher defaults or secret values into a discovery record."""
    record = _only_record(_page(_entry()))
    assert record.namespace == "io.github.example"
    assert record.namespace_owner == "example"
    assert record.server_name == "weather"
    assert record.title == "Weather"
    assert record.website_url == "https://weather.example.com/docs"
    assert record.repository_url == "https://github.com/example/weather"
    assert record.published_at == "2026-08-01T10:30:00Z"
    assert record.is_latest is True
    assert record.compatible is True
    package = record.packages[0]
    assert package.registry_type is PackageRegistryType.NPM
    assert package.identifier == "@example/weather-mcp"
    assert package.version == "1.4.0"
    assert package.registry_base_url == "https://registry.npmjs.org"
    assert package.runtime_hint == "npx"
    assert tuple(variable.name for variable in package.environment_variables) == (
        "WEATHER_API_KEY",
        "WEATHER_UNITS",
    )
    assert package.environment_variables[0].is_secret is True
    assert package.environment_variables[1].is_required is False
    remote = record.remotes[0]
    assert remote.transport is RemoteTransport.STREAMABLE_HTTP
    assert remote.url == "https://mcp.weather.example.com/mcp"
    assert tuple(header.name for header in remote.headers) == ("X-Api-Key",)
    assert "celsius" not in repr(record)


def test_registry_dns_namespaces_are_publisher_verified_without_github_identity() -> None:
    """Catches promoting a reverse-DNS namespace to a GitHub owner identity."""
    record = _only_record(_page(_entry(1)))
    assert record.namespace_kind is NamespaceKind.DNS
    assert record.namespace == "com.example.analytics"
    assert record.namespace_owner == "analytics.example.com"
    assert record.trust_tier is TrustTier.PUBLISHER_VERIFIED


@pytest.mark.parametrize(
    "name",
    [
        "weather",
        "io.github.example/weather/extra",
        "io.github.example/",
        "/weather",
        "IO.GitHub.Example/weather",
        "io.github.example/wea ther",
        "localhost/weather",
        "io.github.example/" + "w" * 200,
    ],
)
def test_registry_rejects_namespaces_it_cannot_authenticate(name: str) -> None:
    """Catches admitting a listing whose publisher namespace is not registry identity."""
    entry = _entry()
    entry["server"]["name"] = name
    with pytest.raises(McpRegistryError, match="plugin_registry_page_invalid"):
        registry_client(_page(entry)).search()


def test_registry_rejects_duplicate_names_and_versions_within_one_page() -> None:
    """Catches two competing records for one canonical registry identity."""
    with pytest.raises(McpRegistryError, match="plugin_registry_page_invalid"):
        registry_client(_page(_entry(), _entry())).search()


def test_registry_rejects_duplicate_names_and_versions_across_pages() -> None:
    """Catches a pager that replays the same identity under a fresh cursor."""
    first = _page(_entry(), next_cursor="io.github.example/weather:1.4.0")
    second = _page(_entry())
    with pytest.raises(McpRegistryError, match="plugin_registry_page_invalid"):
        tuple(registry_client(first, second).iter_latest())


def test_registry_admits_one_name_at_two_versions() -> None:
    """Catches mistaking a legitimate version history for a duplicate identity."""
    page = fixture_registry_client("servers-page-1.json").search()
    weather = tuple(record for record in page.servers if record.name.endswith("/weather"))
    assert tuple(record.version for record in weather) == ("1.4.0", "0.9.0")


def test_registry_rejects_registry_metadata_claiming_omega_trust() -> None:
    """Catches an upstream response smuggling an Omega endorsement into a record."""
    with pytest.raises(McpRegistryError, match="plugin_registry_page_invalid"):
        fixture_registry_client("invalid-server.json").search()


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda page: page.__setitem__("extra", 1), id="envelope"),
        pytest.param(lambda page: page["metadata"].__setitem__("total", 1), id="metadata"),
        pytest.param(lambda page: page["servers"][0].__setitem__("trust", "x"), id="entry"),
        pytest.param(lambda page: page["servers"][0]["_meta"].__setitem__("x/y", {}), id="meta"),
    ],
)
def test_registry_rejects_unknown_keys_at_required_structural_levels(mutate) -> None:
    """Catches accepting an envelope Omega cannot fully account for."""
    document = _page(_entry())
    mutate(document)
    with pytest.raises(McpRegistryError, match="plugin_registry_page_invalid"):
        registry_client(document).search()


@pytest.mark.parametrize("missing", ["servers", "metadata"])
def test_registry_rejects_a_response_missing_required_structure(missing: str) -> None:
    """Catches treating a truncated response as an empty but valid page."""
    document = _page(_entry())
    document.pop(missing)
    with pytest.raises(McpRegistryError, match="plugin_registry_page_invalid"):
        registry_client(document).search()


@pytest.mark.parametrize(
    "url",
    [
        "http://mcp.example.com/mcp",
        "https://mcp.example.com/mcp?token=abc",
        "https://mcp.example.com/mcp#frag",
        "https://user:pass@mcp.example.com/mcp",
        "https://MCP.Example.com/mcp",
        # A trailing slash is NOT here any more. It is the vendor's choice of
        # endpoint: GitHub serves at https://api.githubcopilot.com/mcp/, which
        # is the exact origin Omega ships and connects to -- so calling it
        # non-canonical meant a package Omega already runs could not be
        # imported through its own registry path. This list and
        # test_marketplace_importer's own parametrisation directly contradicted
        # each other until then. An interior "//" is still an empty segment and
        # is still refused.
        "https://mcp.example.com/mcp//v1",
        "https://mcp.example.com/../mcp",
        "https://mcp.example.com:443/mcp",
        "https://mcp.example.com:8443/mcp",
        "//mcp.example.com/mcp",
    ],
)
def test_registry_records_noncanonical_remote_urls_as_unsupported(url: str) -> None:
    """Catches admitting a remote endpoint Omega cannot name unambiguously."""
    record = _only_record(_page(_remote_entry(url)))
    assert record.compatible is False
    assert record.remotes == ()
    assert "plugin_registry_remote_url_noncanonical" in record.unsupported_reasons


@pytest.mark.parametrize(
    "url",
    [
        "https://localhost/mcp",
        "https://127.0.0.1/mcp",
        "https://10.1.2.3/mcp",
        "https://192.168.0.7/mcp",
        "https://172.16.4.9/mcp",
        "https://169.254.1.1/mcp",
        "https://[::1]/mcp",
        "https://printer.local/mcp",
        "https://tools.internal/mcp",
        "https://tools.corp/mcp",
    ],
)
def test_registry_rejects_private_network_remote_origins(url: str) -> None:
    """Catches importing a listing that points Omega at the operator's own network."""
    record = _only_record(_page(_remote_entry(url)))
    assert record.compatible is False
    assert record.remotes == ()
    assert "plugin_registry_remote_origin_private" in record.unsupported_reasons


@pytest.mark.parametrize("transport", ["stdio", "websocket", "streamable_http", ""])
def test_registry_records_unknown_remote_transports_as_unsupported(transport: str) -> None:
    """Catches connecting to a remote over a transport Omega never validated."""
    entry = _remote_entry("https://mcp.example.com/mcp", transport=transport)
    record = _only_record(_page(entry))
    assert record.compatible is False
    assert "plugin_registry_remote_transport_unsupported" in record.unsupported_reasons


@pytest.mark.parametrize("registry_type", ["mcpb", "nuget", "gem", "", "NPM"])
def test_registry_records_unsupported_package_registries_as_unsupported(
    registry_type: str,
) -> None:
    """Catches importing an artifact from a registry Omega cannot pin or verify."""
    entry = _entry()
    entry["server"].pop("remotes")
    entry["server"]["packages"][0]["registryType"] = registry_type
    record = _only_record(_page(entry))
    assert record.compatible is False
    assert record.packages == ()
    assert "plugin_registry_package_registry_unsupported" in record.unsupported_reasons


@pytest.mark.parametrize("transport", ["streamable-http", "sse", "unix", ""])
def test_registry_records_unsupported_package_transports_as_unsupported(
    transport: str,
) -> None:
    """Catches launching a local package over a transport the runtime cannot seat."""
    entry = _entry()
    entry["server"].pop("remotes")
    entry["server"]["packages"][0]["transport"] = {"type": transport}
    record = _only_record(_page(entry))
    assert record.compatible is False
    assert "plugin_registry_package_transport_unsupported" in record.unsupported_reasons


def test_registry_records_unknown_package_keys_as_unsupported() -> None:
    """Catches silently ignoring publisher launch data Omega has not reviewed."""
    entry = _entry()
    entry["server"].pop("remotes")
    entry["server"]["packages"][0]["installCommand"] = "curl | sh"
    record = _only_record(_page(entry))
    assert record.compatible is False
    assert "plugin_registry_package_invalid" in record.unsupported_reasons
    assert "curl" not in repr(record)


@pytest.mark.parametrize(
    ("field", "count"),
    [
        ("runtimeArguments", MAX_ARGUMENTS + 1),
        ("packageArguments", MAX_ARGUMENTS + 1),
        ("environmentVariables", MAX_ENVIRONMENT_VARIABLES + 1),
    ],
)
def test_registry_records_unbounded_package_declarations_as_unsupported(
    field: str, count: int
) -> None:
    """Catches carrying an unbounded publisher argument or environment list forward."""
    entry = _entry()
    entry["server"].pop("remotes")
    entry["server"]["packages"][0][field] = [{"name": f"ARG_{index}"} for index in range(count)]
    record = _only_record(_page(entry))
    assert record.compatible is False
    assert "plugin_registry_package_declaration_unbounded" in record.unsupported_reasons


def test_registry_records_unbounded_remote_headers_as_unsupported() -> None:
    """Catches carrying an unbounded publisher header list into the connection plan."""
    entry = _remote_entry("https://mcp.example.com/mcp")
    entry["server"]["remotes"][0]["headers"] = [
        {"name": f"X-Header-{index}"} for index in range(MAX_HEADERS + 1)
    ]
    record = _only_record(_page(entry))
    assert record.compatible is False
    assert "plugin_registry_remote_declaration_unbounded" in record.unsupported_reasons


@pytest.mark.parametrize(
    ("field", "count"),
    [("packages", MAX_PACKAGES + 1), ("remotes", MAX_REMOTES + 1)],
)
def test_registry_records_unbounded_distribution_lists_as_unsupported(
    field: str, count: int
) -> None:
    """Catches expanding one listing into an unbounded set of install candidates."""
    entry = _entry()
    template = entry["server"][field][0]
    entry["server"][field] = [deepcopy(template) for _ in range(count)]
    record = _only_record(_page(entry))
    assert "plugin_registry_declaration_unbounded" in record.unsupported_reasons


def test_registry_records_absent_distributions_as_unsupported() -> None:
    """Catches presenting a listing with nothing to install as installable."""
    entry = _entry()
    entry["server"].pop("packages")
    entry["server"].pop("remotes")
    record = _only_record(_page(entry))
    assert record.compatible is False
    assert record.unsupported_reasons == ("plugin_registry_distribution_absent",)


@pytest.mark.parametrize("version", ["2026.01.15", "v1.4.0", "1.4", "latest"])
def test_registry_records_unresolvable_versions_as_unsupported(version: str) -> None:
    """Catches importing a listing Omega cannot pin to one immutable version."""
    entry = _entry()
    entry["server"]["version"] = version
    record = _only_record(_page(entry))
    assert record.version == version
    assert "plugin_registry_version_unsupported" in record.unsupported_reasons


@pytest.mark.parametrize(
    "value",
    ["http://weather.example.com", "https://weather.example.com/docs?ref=1"],
)
def test_registry_records_noncanonical_publisher_urls_as_unsupported(value: str) -> None:
    """Catches surfacing an unverifiable publisher link as source evidence."""
    entry = _entry()
    entry["server"]["websiteUrl"] = value
    record = _only_record(_page(entry))
    assert record.website_url is None
    assert "plugin_registry_url_noncanonical" in record.unsupported_reasons
    assert record.compatible is True


def test_registry_preserves_publisher_extensions_only_as_a_capped_digest() -> None:
    """Catches passing publisher-authored extension text to the model or installer."""
    entry = _entry()
    server = entry["server"]
    expected = sha256_hex(canonical_bytes({"$schema": server["$schema"], "_meta": server["_meta"]}))
    record = _only_record(_page(entry))
    assert record.extensions_sha256 == expected
    assert "ci-publisher" not in repr(record)


def test_registry_drops_publisher_extensions_that_exceed_the_cap() -> None:
    """Catches digesting an unbounded publisher extension blob."""
    entry = _entry()
    entry["server"]["x-omega-notes"] = "n" * (MAX_EXTENSION_BYTES + 1)
    record = _only_record(_page(entry))
    assert record.extensions_sha256 is None
    assert "plugin_registry_extensions_too_large" in record.unsupported_reasons


def test_registry_reports_no_digest_when_a_publisher_declares_no_extensions() -> None:
    """Catches fabricating an extension digest over an empty extension set."""
    record = _only_record(_page(_entry(1)))
    assert record.extensions_sha256 is None


def test_registry_rejects_excessive_items_on_one_page() -> None:
    """Catches accepting a page far larger than the requested bound."""
    entries = []
    for index in range(MAX_PAGE_SERVERS + 1):
        entry = _entry()
        entry["server"]["name"] = f"io.github.example/weather-{index}"
        entries.append(entry)
    with pytest.raises(McpRegistryError, match="plugin_registry_page_invalid"):
        registry_client(_page(*entries)).search()


def test_registry_rejects_a_count_that_disagrees_with_the_page() -> None:
    """Catches trusting a declared count the response body does not support."""
    document = _page(_entry())
    document["metadata"]["count"] = 7
    with pytest.raises(McpRegistryError, match="plugin_registry_page_invalid"):
        registry_client(document).search()


def test_registry_rejects_excessive_pages() -> None:
    """Catches following an endlessly advancing cursor chain."""
    fetched: list[int] = []

    def transport(request: McpRegistryRequest) -> bytes:
        index = len(fetched)
        fetched.append(index)
        entry = _entry()
        entry["server"]["name"] = f"io.github.example/weather-{index}"
        return json.dumps(_page(entry, next_cursor=f"cursor-{index}")).encode("utf-8")

    client = McpRegistryClient(transport=transport)
    with pytest.raises(McpRegistryError, match="plugin_registry_too_many_pages"):
        tuple(client.iter_latest())
    assert len(fetched) == MAX_REGISTRY_PAGES


@pytest.mark.parametrize(
    "cursor",
    ["", " ", "cursor with spaces", "cursor" + chr(10) + "1", "c" * 400, "-lead", chr(233)],
)
def test_registry_rejects_malformed_cursors(cursor: str) -> None:
    """Catches echoing an unbounded or unstable cursor back to the transport."""
    document = _page(_entry())
    document["metadata"]["nextCursor"] = cursor
    with pytest.raises(McpRegistryError, match="plugin_registry_page_invalid"):
        registry_client(document).search()


@pytest.mark.parametrize(
    "options",
    [
        {"limit": 0},
        {"limit": MAX_PAGE_SERVERS + 1},
        {"limit": True},
        {"limit": "10"},
        {"cursor": ""},
        {"cursor": "bad cursor"},
        {"cursor": 7},
        {"query": ""},
        {"query": " weather"},
        {"query": "w" * 200},
    ],
)
def test_registry_rejects_invalid_search_requests(options: dict[str, Any]) -> None:
    """Catches sending an unbounded or unstable query to the registry transport."""
    client = fixture_registry_client("servers-page-1.json")
    with pytest.raises(McpRegistryError, match="plugin_registry_request_invalid"):
        client.search(**options)


def test_registry_propagates_a_transport_timeout_cause_unchanged() -> None:
    """Catches rewriting or swallowing the transport's own stable timeout cause."""
    client = McpRegistryClient(transport=_failing_transport("plugin_registry_timeout"))
    with pytest.raises(McpRegistryError) as exc_info:
        client.search()
    assert exc_info.value.cause == "plugin_registry_timeout"


def test_registry_reports_untyped_transport_failures_without_raw_text() -> None:
    """Catches leaking raw transport exception text across the foundation boundary."""

    def transport(request: McpRegistryRequest) -> bytes:
        raise RuntimeError("connect to 10.0.0.5:8443 failed: certificate expired")

    with pytest.raises(McpRegistryError) as exc_info:
        McpRegistryClient(transport=transport).search()
    assert exc_info.value.cause == "plugin_registry_transport_invalid"
    assert "10.0.0.5" not in str(exc_info.value)


@pytest.mark.parametrize("payload", ["{}", None, 7, bytearray(b"{}")])
def test_registry_rejects_a_transport_that_returns_something_other_than_bytes(
    payload: Any,
) -> None:
    """Catches parsing whatever an injected transport happens to hand back."""

    def transport(request: McpRegistryRequest) -> bytes:
        return payload

    with pytest.raises(McpRegistryError, match="plugin_registry_transport_invalid"):
        McpRegistryClient(transport=transport).search()


def test_registry_rejects_oversized_page_payloads() -> None:
    """Catches parsing an unbounded response body into memory."""

    def transport(request: McpRegistryRequest) -> bytes:
        return b"[" + b"0," * MAX_PAGE_BYTES + b"0]"

    with pytest.raises(McpRegistryError, match="plugin_registry_page_too_large"):
        McpRegistryClient(transport=transport).search()


@pytest.mark.parametrize("payload", [b"", b"not json", b"[]", b"null", b"\xff\xfe"])
def test_registry_rejects_responses_that_are_not_registry_documents(payload: bytes) -> None:
    """Catches accepting a body that never was a registry response."""

    def transport(request: McpRegistryRequest) -> bytes:
        return payload

    with pytest.raises(McpRegistryError, match="plugin_registry_page_invalid"):
        McpRegistryClient(transport=transport).search()


def test_registry_rejects_deeply_nested_documents() -> None:
    """Catches recursing without a bound over publisher-controlled nesting."""
    entry = _entry()
    nested: Any = "leaf"
    for _ in range(40):
        nested = {"next": nested}
    entry["server"]["_meta"]["io.modelcontextprotocol.registry/publisher-provided"] = nested
    with pytest.raises(McpRegistryError, match="plugin_registry_page_invalid"):
        registry_client(_page(entry)).search()


@pytest.mark.parametrize(
    "options",
    [
        {"page_limit": 0},
        {"page_limit": MAX_PAGE_SERVERS + 1},
        {"max_pages": 0},
        {"max_pages": MAX_REGISTRY_PAGES + 1},
        {"page_limit": True},
    ],
)
def test_registry_client_construction_is_bounded(options: dict[str, Any]) -> None:
    """Catches widening the pager's bounds through construction arguments."""
    with pytest.raises(McpRegistryError, match="plugin_registry_client_invalid"):
        registry_client(_page(_entry()), **options)


def test_registry_client_requires_a_callable_transport() -> None:
    """Catches constructing a pager with no injected transport at all."""
    with pytest.raises(McpRegistryError, match="plugin_registry_client_invalid"):
        McpRegistryClient(transport=None)


def test_registry_records_are_immutable() -> None:
    """Catches a mutable discovery record being edited after admission."""
    record = _only_record(_page(_entry()))
    with pytest.raises((AttributeError, TypeError)):
        record.trust_tier = TrustTier.OMEGA_VERIFIED


def test_registry_records_cannot_be_relabelled_as_omega_verified() -> None:
    """Catches any later stage promoting a registry listing to Omega endorsement."""
    record = _only_record(_page(_entry()))
    with pytest.raises(McpRegistryError, match="plugin_registry_record_invalid"):
        replace(record, trust_tier=TrustTier.OMEGA_VERIFIED)


@pytest.mark.parametrize(
    "constant",
    ["Infinity", "-Infinity", "NaN"],
)
def test_registry_rejects_non_standard_json_constants(constant: str) -> None:
    """Catches admitting a body no conforming JSON reader would agree on."""
    body = json.dumps(_page(_entry())).replace(
        '"version": "1.4.0"',
        '"x-omega-metric": ' + constant + ', "version": "1.4.0"',
        1,
    )

    def transport(request: McpRegistryRequest) -> bytes:
        return body.encode("utf-8")

    with pytest.raises(McpRegistryError, match="plugin_registry_page_invalid"):
        McpRegistryClient(transport=transport).search()


def test_registry_rejects_repeated_object_keys() -> None:
    """Catches a response whose identity depends on which duplicate key wins."""
    body = json.dumps(_page(_entry())).replace(
        '"name": "io.github.example/weather"',
        '"name": "io.github.example/weather", "name": "io.github.attacker/weather"',
        1,
    )

    def transport(request: McpRegistryRequest) -> bytes:
        return body.encode("utf-8")

    with pytest.raises(McpRegistryError, match="plugin_registry_page_invalid"):
        McpRegistryClient(transport=transport).search()


def test_registry_reports_uncanonicalizable_extensions_without_a_digest() -> None:
    """Catches emitting a digest over extension bytes RFC 8785 cannot represent."""
    entry = _entry()
    entry["server"]["x-omega-counter"] = 2**70
    record = _only_record(_page(entry))
    assert record.extensions_sha256 is None
    assert "plugin_registry_extensions_invalid" in record.unsupported_reasons


def _nested_array_body(depth: int) -> bytes:
    """Return a body of `depth` nested arrays and nothing else."""
    return b"[" * depth + b"]" * depth


def _nested_object_body(depth: int) -> bytes:
    """Return a body of `depth` nested objects and nothing else."""
    return b'{"a":' * depth + b"1" + b"}" * depth


@pytest.mark.parametrize("build", [_nested_array_body, _nested_object_body])
def test_registry_fails_closed_on_nesting_beyond_the_recursion_limit(build) -> None:
    """Catches a raw RecursionError escaping the stable-cause boundary."""
    body = build(50_000)
    assert len(body) <= MAX_PAGE_BYTES
    assert 50_000 > sys.getrecursionlimit()

    def transport(request: McpRegistryRequest) -> bytes:
        return body

    with pytest.raises(McpRegistryError) as exc_info:
        McpRegistryClient(transport=transport).search()
    assert exc_info.value.cause == "plugin_registry_page_invalid"


def _sibling_servers(mutate) -> tuple[McpRegistryServer, ...]:
    """Return both listings of a page whose first listing this mutation degrades."""
    degraded = _entry()
    degraded["server"]["name"] = "io.github.example/degraded"
    mutate(degraded["server"])
    return registry_client(_page(degraded, _entry(1))).search().servers


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        pytest.param(
            lambda server: server.__setitem__("description", "Cafe" + chr(0x301) + " tools"),
            "plugin_registry_metadata_invalid",
            id="non-nfc-description",
        ),
        pytest.param(
            lambda server: server.__setitem__("description", "Trailing space "),
            "plugin_registry_metadata_invalid",
            id="trailing-space-description",
        ),
        pytest.param(
            lambda server: server.__setitem__("description", "d" * 500),
            "plugin_registry_metadata_invalid",
            id="over-long-description",
        ),
        pytest.param(
            lambda server: server.pop("description"),
            "plugin_registry_metadata_invalid",
            id="absent-description",
        ),
        pytest.param(
            lambda server: server.__setitem__("title", "t" * 200),
            "plugin_registry_metadata_invalid",
            id="over-long-title",
        ),
        pytest.param(
            lambda server: server["repository"].__setitem__("stars", 5),
            "plugin_registry_url_noncanonical",
            id="unknown-repository-key",
        ),
        pytest.param(
            lambda server: server.__setitem__("packages", {}),
            "plugin_registry_metadata_invalid",
            id="packages-not-a-list",
        ),
        pytest.param(
            lambda server: server.__setitem__("remotes", {}),
            "plugin_registry_metadata_invalid",
            id="remotes-not-a-list",
        ),
    ],
)
def test_registry_degrades_one_listing_without_denying_the_whole_page(mutate, reason) -> None:
    """Catches one publisher's stray field denying discovery to every other listing."""
    servers = _sibling_servers(mutate)
    assert len(servers) == 2
    assert servers[0].name == "io.github.example/degraded"
    assert reason in servers[0].unsupported_reasons
    assert servers[1].name == "com.example.analytics/reports"
    assert servers[1].unsupported_reasons == ()
    assert servers[1].compatible is True


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda server: server.__setitem__("description", "Trailing space "),
            id="trailing-space-description",
        ),
        pytest.param(
            lambda server: server.__setitem__("packages", {}),
            id="packages-not-a-list",
        ),
    ],
)
def test_registry_disqualifies_a_listing_whose_metadata_it_refused(mutate) -> None:
    """Catches a refused description or distribution list still reading as installable."""
    servers = _sibling_servers(mutate)
    assert servers[0].compatible is False
    assert servers[1].compatible is True


def test_registry_reports_no_description_it_refused_to_admit() -> None:
    """Catches surfacing publisher text that failed the stable-string contract."""
    servers = _sibling_servers(
        lambda server: server.__setitem__("description", "Trailing space ")
    )
    assert servers[0].description is None
    assert "Trailing space" not in repr(servers[0])


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda server: server.__setitem__("name", "nope"), id="name"),
        pytest.param(lambda server: server.pop("version"), id="version"),
    ],
)
def test_registry_still_rejects_the_page_for_broken_listing_identity(mutate) -> None:
    """Catches degrading identity, which no listing may be admitted without."""
    degraded = _entry()
    mutate(degraded["server"])
    with pytest.raises(McpRegistryError, match="plugin_registry_page_invalid"):
        registry_client(_page(degraded, _entry(1))).search()
