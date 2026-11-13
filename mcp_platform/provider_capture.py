"""Read a provider's live MCP surface with the operator's own token.

This is the one step the ordinary client stack structurally cannot perform.
``CurrentMcpClient`` refuses to open a connection to a surface Omega's key has
not vouched for, and restricted discovery is hard-wired unauthenticated -- so a
provider that answers ``tools/list`` with 401 until a token exists, as Slack
does, can never have its first signed capability authority built from inside the
normal path. Authorize once, capture here, weave, sign.

It lives in ``mcp_platform`` rather than beside its command because httpx2 is
confined to this package on purpose: every outbound provider request stays in
one auditable place. The command in ``scripts/`` holds the vault read, the
policy review and the file writing, and none of the network.

Maintainer-facing. Never imported by the runtime.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx2

from mcp_platform.package_contracts import MCP_PROTOCOL_VERSION
from mcp_platform.authorization import MCP_SESSION_HEADER
from mcp_platform.transports.streamable_http import build_bounded_sync_http_client

_ACCEPT = "application/json, text/event-stream"
_MAX_PROVIDER_MESSAGE = 600
_REQUEST_SECONDS = 45.0
_CONNECT_SECONDS = 15.0
#: A provider that keeps handing back a cursor must not spin forever.
_MAX_PAGES = 50
#: How long to keep asking a server that declared tools and listed none.
#: Grafana Cloud needs about fifteen seconds to warm a stack; this leaves
#: room for a slower one without letting a genuinely empty provider hang.
_EMPTY_TOOLS_WAIT_SECONDS = 45.0


class ProviderCaptureError(RuntimeError):
    """A capture that could not complete, phrased for the operator reading it."""


def _decode(response: httpx2.Response) -> dict[str, Any]:
    """Streamable HTTP answers either JSON or one SSE event carrying the JSON."""
    media = (response.headers.get("content-type") or "").split(";")[0].strip()
    if media == "text/event-stream":
        for line in response.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise ProviderCaptureError("event stream carried no data frame")
    return response.json()


def _provider_message(response: httpx2.Response) -> str:
    """A refusal's human part, bounded. Falls back to the raw start of the body.

    Omega's RUNTIME never logs provider-authored text, and still does not:
    ``_log_resource_http_failure`` reports a closed enum. This is a maintainer
    command whose whole job is to explain a refusal, run by hand, and the
    explanation is routinely the only actionable part. Slack answers a missing
    app setting with a 400 naming the switch to flip; without the body that
    reads as an unexplained protocol failure.
    """
    try:
        body = _decode(response)
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])[:_MAX_PROVIDER_MESSAGE]
    except Exception:  # noqa: BLE001 - a body we cannot parse is still worth showing
        pass
    return (response.text or "(empty body)")[:_MAX_PROVIDER_MESSAGE]


def _rpc(
    client: httpx2.Client,
    endpoint: str,
    payload: dict[str, Any],
    headers: dict[str, str],
) -> tuple[dict[str, Any], httpx2.Response]:
    response = client.post(endpoint, json=payload, headers=headers, timeout=_REQUEST_SECONDS)
    if response.status_code >= 400:
        challenge = response.headers.get("WWW-Authenticate", "")
        raise ProviderCaptureError(
            f"{payload['method']} failed: HTTP {response.status_code} {challenge}".strip()
            + f"\n  provider said: {_provider_message(response)}"
        )
    body = _decode(response)
    if "error" in body:
        raise ProviderCaptureError(f"{payload['method']} refused: {body['error']}")
    return body.get("result") or {}, response


def capture_provider_surface(
    endpoint: str,
    *,
    access_token: str,
    offered_version: str = MCP_PROTOCOL_VERSION,
) -> dict[str, Any]:
    """Handshake, enumerate every tool page, and return the discovery document.

    The document has the same shape as the captured fixtures the shipped Google
    packages were built from, so a capture is comparable to what was signed
    before it. The token is used and discarded: it is never returned, written,
    or logged.
    """
    # No MCP-Protocol-Version yet: `initialize` NEGOTIATES the version in its
    # body, and a header asserting one before the answer contradicts it.
    # Hugging Face and Netlify both refuse that with HTTP 400 "the request
    # headers and body disagree". The header is added once the server answers.
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": _ACCEPT,
        "Content-Type": "application/json",
    }
    # The transport module is Omega's sole owner of HTTP client construction,
    # so the client comes from its factory rather than being built here. That
    # rail is what keeps every outbound request bounded the same way.
    with build_bounded_sync_http_client(
        transport=None,
        connection_seconds=_CONNECT_SECONDS,
        operation_seconds=_REQUEST_SECONDS,
    ) as client:
        result, response = _rpc(
            client,
            endpoint,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": offered_version,
                    "capabilities": {},
                    "clientInfo": {"name": "omega-capture", "version": "3.0.0"},
                },
            },
            headers,
        )
        # The version the SERVER settled on, which is what the package must
        # declare: the client checks the live negotiated version against it.
        protocol_version = str(result.get("protocolVersion") or offered_version)
        headers["MCP-Protocol-Version"] = protocol_version
        # A server that issues a session requires it back on every later
        # request; Wix answers `tools/list` with "Mcp-Session-Id header is
        # required" without it. Echoing an id the server just minted, for one
        # short exchange, is protocol compliance -- session AUTHORITY still
        # belongs to the SDK, which is why the header is spelled in exactly one
        # place and imported here rather than repeated.
        session_id = response.headers.get(MCP_SESSION_HEADER)
        if session_id:
            headers[MCP_SESSION_HEADER] = session_id
        server_capabilities = result.get("capabilities") or {}

        client.post(
            endpoint,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=headers,
            timeout=_REQUEST_SECONDS,
        )

        def enumerate_group(method: str, key: str, *, required: bool) -> list[dict[str, Any]]:
            """Page one capability group. Optional groups tolerate a refusal.

            ``capabilities`` in the initialize result is the server's own claim
            about what it serves, and providers get it wrong in both
            directions, so an advertised group that answers "method not found"
            counts as empty rather than as a failed capture. Tools are the one
            group a capture exists for, so a refusal there is still an error.
            """
            collected: list[dict[str, Any]] = []
            cursor: str | None = None
            for page in range(_MAX_PAGES):
                params: dict[str, Any] = {} if cursor is None else {"cursor": cursor}
                try:
                    listed, _ = _rpc(
                        client,
                        endpoint,
                        {"jsonrpc": "2.0", "id": 100 + page, "method": method, "params": params},
                        headers,
                    )
                except ProviderCaptureError:
                    if required:
                        raise
                    return []
                collected.extend(listed.get(key) or [])
                cursor = listed.get("nextCursor")
                if not cursor:
                    return collected
            raise ProviderCaptureError(f"provider paged past {_MAX_PAGES} {key} pages")

        tools = enumerate_group("tools/list", "tools", required=True)
        # A server that declared `tools` and then listed none is contradicting
        # itself, and `listChanged` is it saying the list may still arrive.
        # Grafana Cloud takes about fifteen seconds to warm a stack and serves
        # 119 tools after it; captured on the first answer it looks like a
        # provider with nothing to offer.
        if not tools and "tools" in (
            server_capabilities if isinstance(server_capabilities, dict) else {}
        ):
            deadline = time.monotonic() + _EMPTY_TOOLS_WAIT_SECONDS
            delay = 1.0
            while not tools and time.monotonic() < deadline:
                print(
                    f"  server declares tools and listed none; waiting {delay:.0f}s"
                    " (it advertises listChanged)"
                )
                time.sleep(delay)
                tools = enumerate_group("tools/list", "tools", required=True)
                delay = min(delay * 2, 8.0)
            if not tools:
                print(
                    f"  still no tools after {_EMPTY_TOOLS_WAIT_SECONDS:.0f}s;"
                    " capturing the empty surface"
                )
        # Only what the server says it serves. Asking a tools-only provider for
        # resources earns a "method not found" per group and nothing else.
        served = server_capabilities if isinstance(server_capabilities, dict) else {}
        resources = (
            enumerate_group("resources/list", "resources", required=False)
            if "resources" in served
            else []
        )
        resource_templates = (
            enumerate_group("resources/templates/list", "resourceTemplates", required=False)
            if "resources" in served
            else []
        )
        prompts = (
            enumerate_group("prompts/list", "prompts", required=False)
            if "prompts" in served
            else []
        )

    def by_name(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(items, key=lambda item: str(item.get("name") or ""))

    return {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "endpoint": endpoint,
        "protocol_version": protocol_version,
        "prompts": by_name(prompts),
        "resource_templates": by_name(resource_templates),
        "resources": by_name(resources),
        "server_capabilities": dict(result.get("capabilities") or {}),
        "server_info": dict(result.get("serverInfo") or {}),
        "tools": by_name(tools),
    }
