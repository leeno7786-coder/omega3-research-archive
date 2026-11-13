"""Real providers are not shaped the way Omega assumed, and five rules said no.

Slack's MCP server is signed, installed and authorized, and every one of these
turned a working connection into a refusal. Each was a rule that read as a
security boundary but was really an unstated assumption about how a provider
must look, and each is narrower than the thing it sat next to:

* an endpoint path of ``/mcp`` -- the exact path Omega appends itself;
* a wire version from the handshake era that is not the newest one;
* an ``_meta`` block for an extension the connection never negotiated;
* a ``serverInfo.name`` with a space in it;
* a resource named the way MCP says resources are named.

The last needed `NamedCapability` to carry a KIND before it could be fixed at
all: it had none, and guessed one from the name's shape.

Fixed 2026-09-07. Every case here is Slack's real answer, so a regression is a
plugin that stops connecting rather than a test that stops passing.
"""

from __future__ import annotations

import pytest

from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS

from mcp_platform.authorization import _worker_endpoint
from mcp_platform.client_contracts import (
    DiscoverySource,
    McpClientDeployment,
    McpClientError,
    McpDiscoveryReceipt,
    McpTransport,
    _server_id,
)
from tests.mcp_platform.test_authorization import _descriptor, _requirement


def _remote(origin: str):
    """A real descriptor for one origin -- both endpoint rules snapshot it."""
    return _descriptor(_requirement(origin=origin))


# What Slack actually answers.
SLACK_ORIGIN = "https://mcp.slack.com/mcp"
SLACK_SERVER_NAME = "Slack MCP"
SLACK_WIRE_VERSION = "2025-06-18"  # noqa: E501 - what Slack answers, not a retired literal


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        # The case that broke: the path Omega appends itself was the one path
        # it refused to accept when a provider stated it.
        (SLACK_ORIGIN, SLACK_ORIGIN),
        # And a bare origin is the endpoint, not an invitation to invent "/mcp".
        # Miro and Stripe serve at their root and are the only two shipped
        # providers with a bare origin; the append 404'd both.
        ("https://mcp.miro.com", "https://mcp.miro.com"),
        ("https://mcp.stripe.com", "https://mcp.stripe.com"),
        ("https://calendarmcp.googleapis.com/mcp/v1", "https://calendarmcp.googleapis.com/mcp/v1"),
        ("https://vendor.example/mcp/", "https://vendor.example/mcp/"),
        ("https://vendor.example/deep/path", "https://vendor.example/deep/path"),
    ],
)
def test_a_stated_endpoint_path_is_the_endpoint(origin: str, expected: str) -> None:
    assert _worker_endpoint(_remote(origin)) == expected


@pytest.mark.parametrize("path", ["/../escape", "/has space", "/trailing//double"])
def test_a_path_outside_the_canonical_grammar_never_reaches_an_endpoint(path: str) -> None:
    """Widening the endpoint rules must not widen what a manifest may declare.

    A traversal, a space or an empty segment is refused where it should be --
    at the requirement, before anything derives an endpoint from it. That is
    what makes it safe for the two derivations below to trust a stated path.
    """
    with pytest.raises(ValueError, match="remote_origin"):
        _requirement(origin=f"https://vendor.example{path}")


def test_the_transport_derives_the_same_endpoint_as_the_worker() -> None:
    """Two copies of this rule exist; they must not drift apart again.

    They already had: the contracts modules were widened to a path grammar
    while both endpoint derivations kept a three-literal allowlist, so Slack
    passed manifest validation and then failed at connect.
    """
    from mcp_platform.transports.streamable_http import _canonical_endpoint

    for origin in (SLACK_ORIGIN, "https://mcp.slack.com", "https://vendor.example/mcp/v1"):
        descriptor = _remote(origin)
        assert _canonical_endpoint(descriptor, loopback_test=False) == _worker_endpoint(descriptor)


@pytest.mark.parametrize("wire_version", HANDSHAKE_PROTOCOL_VERSIONS)
def test_any_handshake_era_wire_version_may_be_declared(wire_version: str) -> None:
    """The field names the version THIS server speaks, not the newest one.

    Google answers the newest handshake version exactly, so pinning the field
    to that constant looked correct for years. Slack answers an older one, and
    the pin made it -- and every other legacy server off that single version --
    impossible to declare. Parametrised over the SDK's own set, so a version
    added there is covered without editing a list here.
    """
    from plugin_platform.package_verifier import _validate_runtime_compatibility

    _validate_runtime_compatibility(
        {
            "wire_protocol_version": wire_version,
            "handshake_mode": "legacy",
            "missing_metadata": {"server_id": SLACK_SERVER_NAME, "server_version": "1.0.0"},
        }
    )


@pytest.mark.parametrize("wire_version", ["2026-07-28", "1999-01-01", "", "latest"])
def test_a_version_outside_the_handshake_era_is_refused(wire_version: str) -> None:
    """Still a closed set: the drift check downstream depends on it."""
    from plugin_platform.package_verifier import _validate_runtime_compatibility

    with pytest.raises(ValueError, match="^capability_authority_invalid$"):
        _validate_runtime_compatibility(
            {
                "wire_protocol_version": wire_version,
                "handshake_mode": "legacy",
                "missing_metadata": {"server_id": SLACK_SERVER_NAME, "server_version": "1.0.0"},
            }
        )


@pytest.mark.parametrize(
    "name",
    [SLACK_SERVER_NAME, "GitHub MCP Server", "StatelessServer", "a", "x" * 120],
)
def test_a_server_may_name_itself_the_way_a_human_would(name: str) -> None:
    """`serverInfo.name` is a label, and the signed side always treated it as one.

    A package's own ``runtime_compatibility.missing_metadata.server_id`` has
    always been a bounded plain string, and ``_normalize_metadata`` copies that
    exact value into the receipt -- so requiring an identifier here contradicted
    the rule right next to it for any provider not named like a slug.
    """
    assert _server_id(name) == name


@pytest.mark.parametrize("name", ["", "x" * 121, " leading", "trailing ", "new\nline"])
def test_a_server_name_is_still_bounded_and_plain(name: str) -> None:
    with pytest.raises(ValueError):
        _server_id(name)


def test_a_live_receipt_accepts_the_name_slack_reports() -> None:
    """The exact construction that failed, at the boundary that failed."""
    receipt = McpDiscoveryReceipt(
        app_id="slack.official.mcp",
        publisher_id="com.slack",
        deployment=McpClientDeployment.REMOTE,
        transport=McpTransport.STREAMABLE_HTTP,
        protocol_version="2026-07-28",
        source=DiscoverySource.LIVE_AUTHENTICATED,
        server_id=SLACK_SERVER_NAME,
        server_version="1.0.0",
        tools={},
        prompts={},
        resources={},
        resource_templates={},
        extensions={},
        cache_metadata={},
        capability_revision="0" * 64,
    )

    assert receipt.server_id == SLACK_SERVER_NAME


# --- an extension the connection never negotiated ---------------------------

SLACK_UI_HINT = {"resourceUri": "ui://send-message-input.html"}


def test_an_unnegotiated_extension_hint_is_ignored_not_treated_as_drift() -> None:
    """Slack attaches an MCP Apps UI hint to one tool and advertises no extension.

    MCP puts extension data in ``_meta`` precisely so a client that does not
    know the extension can ignore it. Reading it as drift made a signed,
    installed, authorized plugin fail at connect -- and it would do the same to
    any provider shipping an extension Omega has not declared.
    """
    from mcp_platform.capabilities import _validated_apps_ui

    descriptor = _remote(SLACK_ORIGIN)
    assert descriptor.extension_versions == {}

    assert (
        _validated_apps_ui(
            descriptor, group="tools", name="slack_send_message_draft", observed=SLACK_UI_HINT
        )
        is None
    )


def test_signed_metadata_without_the_extension_declared_is_still_an_error() -> None:
    """Only the undeclared-and-unsigned case is waived.

    A package carrying MCP Apps metadata while declaring no MCP Apps extension
    is malformed, and quietly skipping it would hide that.
    """
    from dataclasses import fields

    from mcp_platform.capabilities import _validated_apps_ui
    from mcp_platform.client_contracts import McpConnectionDescriptor

    base = _remote(SLACK_ORIGIN)
    forged = object.__new__(McpConnectionDescriptor)
    for item in fields(base):
        object.__setattr__(forged, item.name, getattr(base, item.name))
    object.__setattr__(
        forged,
        "signed_capability_authority",
        {"mcp_apps_metadata": {"tools": {"slack_send_message_draft": SLACK_UI_HINT}}},
    )

    with pytest.raises(McpClientError):
        _validated_apps_ui(
            forged, group="tools", name="slack_send_message_draft", observed=None
        )


# --- a resource is named for people, a tool for the planner -------------------

SLACK_RESOURCE_NAME = "Send Message Input Form"


def test_a_resource_may_carry_the_name_a_provider_gave_it() -> None:
    """MCP calls a resource's `name` human-readable, and providers mean it.

    Slack's is "Send Message Input Form". Demanding identifier shape made any
    provider whose resources got enumerated unusable -- the same class of
    refusal as the four that kept Slack from connecting, and the last one left.

    It could not be fixed without `NamedCapability` carrying a KIND. It had
    none, and inferred one from the name's shape, which can tell a tool from an
    extension only because those happen to look different and cannot tell a
    resource from either. Every caller that knows the kind now states it.
    """
    from plugin_platform.contracts import CapabilityKind, CapabilityRequirement
    from plugin_platform.dependencies import NamedCapability

    declared = CapabilityRequirement.from_dict(
        {"kind": "resource", "name": SLACK_RESOURCE_NAME, "schema_sha256": None, "version": None}
    )
    observed = NamedCapability(
        name=SLACK_RESOURCE_NAME,
        schema_sha256=None,
        version=None,
        description="x",
        kind=CapabilityKind.RESOURCE,
    )

    assert declared.name == observed.name == SLACK_RESOURCE_NAME
    assert observed.kind is CapabilityKind.RESOURCE


@pytest.mark.parametrize(
    ("kind", "name"),
    [
        # A tool name is a programmatic identity: the planner calls it.
        ("tool", SLACK_RESOURCE_NAME),
        ("prompt", SLACK_RESOURCE_NAME),
        # Loosening resource names must not hand away a reserved protocol identity.
        ("resource", "io.modelcontextprotocol/tasks"),
        ("resource", " leading space"),
        ("resource", "trailing space "),
        ("resource", "x" * 121),
        ("resource", ""),
    ],
)
def test_what_a_capability_name_still_may_not_be(kind: str, name: str) -> None:
    from plugin_platform.contracts import CapabilityRequirement

    with pytest.raises(ValueError):
        CapabilityRequirement.from_dict(
            {"kind": kind, "name": name, "schema_sha256": None, "version": None}
        )


def test_the_kind_is_absent_from_the_capability_revision() -> None:
    """Adding a field must not move a revision that signed packages depend on.

    `_revision_document` hashes name, schema digest and version. If the kind
    reached it, every shipped package's capability_revision would change and
    every installed plugin would read as drift.
    """
    from mcp_platform.capabilities import _capability_revision
    from plugin_platform.contracts import CapabilityKind
    from plugin_platform.dependencies import NamedCapability

    def one(kind):
        return {
            "tools": (
                NamedCapability(
                    name="a_tool", schema_sha256="0" * 64, version=None,
                    description="x", kind=kind,
                ),
            ),
            "prompts": (),
            "resources": (),
            "extensions": (),
        }

    assert _capability_revision(one(CapabilityKind.TOOL)) == _capability_revision(one(None))


# --- the terminating slash an identifier is allowed to carry -----------------


@pytest.mark.parametrize(
    ("identifier", "expected"),
    [
        # What Miro, Netlify and Vercel publish. RFC 8414 s3.1 and RFC 9728 s3.1
        # both say to remove a terminating "/" before inserting the well-known
        # path. Omega kept it and asked for ".../oauth-authorization-server/",
        # which providers answer with a 404 or a redirect -- and Omega refuses
        # redirects, so the failure surfaced as `oauth_provider_rejected` and
        # `oauth_redirect_rejected` rather than as anything about a slash.
        ("https://mcp.miro.com/", "https://mcp.miro.com/.well-known/oauth-authorization-server"),
        (
            "https://netlify-mcp.netlify.app/",
            "https://netlify-mcp.netlify.app/.well-known/oauth-authorization-server",
        ),
        # Unchanged for every identifier that never had one.
        ("https://mcp.slack.com", "https://mcp.slack.com/.well-known/oauth-authorization-server"),
        (
            "https://access.stripe.com/mcp",
            "https://access.stripe.com/.well-known/oauth-authorization-server/mcp",
        ),
        (
            "https://calendarmcp.googleapis.com/mcp/v1",
            "https://calendarmcp.googleapis.com/.well-known/oauth-authorization-server/mcp/v1",
        ),
    ],
)
def test_a_terminating_slash_is_stripped_before_the_well_known_path(
    identifier: str, expected: str
) -> None:
    from mcp_platform.oauth_protocol import authorization_server_metadata_url

    assert authorization_server_metadata_url(identifier) == expected


def test_the_resource_metadata_url_follows_the_same_rule() -> None:
    """Vercel publishes its RESOURCE with the slash; the same rule has to apply."""
    from mcp_platform.oauth_protocol import protected_resource_metadata_url

    assert (
        protected_resource_metadata_url("https://mcp.vercel.com/")
        == "https://mcp.vercel.com/.well-known/oauth-protected-resource"
    )
    assert (
        protected_resource_metadata_url("https://mcp.monday.com/mcp")
        == "https://mcp.monday.com/.well-known/oauth-protected-resource/mcp"
    )


def test_a_trailing_slash_inside_a_real_path_is_part_of_the_identifier() -> None:
    """The counterexample that bounds the rule above, and it is a shipped one.

    GitHub's resource IS ``https://api.githubcopilot.com/mcp/``. Its metadata
    lives at ``.../oauth-protected-resource/mcp/`` and the document names the
    resource with the slash, so trimming it fetched a document for a different
    resource and failed the exact comparison -- turning a working plugin into
    ``oauth_resource_mismatch``. Only a bare origin's root delimiter is dropped.
    """
    from mcp_platform.oauth_protocol import (
        authorization_server_metadata_url,
        protected_resource_metadata_url,
    )

    assert (
        protected_resource_metadata_url("https://api.githubcopilot.com/mcp/")
        == "https://api.githubcopilot.com/.well-known/oauth-protected-resource/mcp/"
    )
    assert (
        authorization_server_metadata_url("https://vendor.example/oauth/")
        == "https://vendor.example/.well-known/oauth-authorization-server/oauth/"
    )


# --- pass 2: what stopped a captured, signed package from connecting ---------
#
# Six providers were authorized, captured and signed, and four of them still
# could not connect. Every cause below is the same shape as the six above: a
# rule that reads as a boundary but is an assumption about how a provider looks.


@pytest.mark.parametrize(
    "claim",
    ["logging", "completions", "tasks", "experimental"],
)
def test_a_remote_server_may_advertise_a_surface_omega_never_asked_for(claim: str) -> None:
    """Wix advertises `logging`; Miro adds `experimental: {}`.

    Neither is something a client that did not request it ever receives, and
    both are what a FastMCP-shaped server emits by default. The rule beside this
    one already knew: the `server/discover` branch passes
    ``allow_unrequested_standard=(deployment is REMOTE)``. Only servers speaking
    the legacy handshake -- which answer that method with METHOD_NOT_FOUND and
    never reach it -- were held to the strict reading.
    """
    import mcp_types as types

    from mcp_platform.client import _advertised_surfaces

    capabilities = types.ServerCapabilities(
        tools=types.ToolsCapability(listChanged=True),
        **{claim: {} if claim == "experimental" else types.LoggingCapability()},
    ) if claim in {"logging", "experimental"} else types.ServerCapabilities(
        tools=types.ToolsCapability(listChanged=True)
    )

    assert _advertised_surfaces(capabilities, allow_unrequested_standard=True)[0] is True

    if claim in {"logging", "experimental"}:
        with pytest.raises(McpClientError):
            _advertised_surfaces(capabilities, allow_unrequested_standard=False)


def test_a_truncated_description_is_stripped_after_the_cut() -> None:
    """Whether Wix connected came down to whether character 1010 was a space.

    ``_provider_description`` collapses whitespace and then cuts to the bound.
    The cut lands wherever the bound falls, and when that is just after a space
    the result ends in one -- which ``_description``, the very next call,
    refuses as unstripped.
    """
    from mcp_platform.capabilities import _EXTERNAL_DESCRIPTION_PREFIX, _provider_description

    limit = 1_024 - len(_EXTERNAL_DESCRIPTION_PREFIX)
    descriptor = _remote(SLACK_ORIGIN)
    # Land a space exactly on the cut.
    padded = ("word " * ((limit // 5) + 4))[: limit - 1] + " tail"

    normalized = _provider_description(padded, descriptor)

    assert normalized == normalized.strip()
    assert len(normalized) <= limit


def test_a_legacy_connection_takes_its_identity_from_the_handshake() -> None:
    """Stripe puts a Ruby object's inspect string in every result's `_meta`.

        "io.modelcontextprotocol/serverInfo":
            "#<Opus::ModelContextProtocol::...::ServerInfo:0x...>"

    That is not identity, and Omega is right to refuse it as such. What it must
    not do is let it veto an identity the handshake already established: a
    package declaring the legacy handshake names the server in
    ``runtime_compatibility.missing_metadata``, checked against the initialize
    result before any of this.
    """
    from dataclasses import fields

    from mcp_platform.client import _normalize_metadata
    from mcp_platform.client_contracts import McpConnectionDescriptor

    base = _remote(SLACK_ORIGIN)
    legacy = object.__new__(McpConnectionDescriptor)
    for item in fields(base):
        object.__setattr__(legacy, item.name, getattr(base, item.name))
    object.__setattr__(
        legacy,
        "runtime_compatibility",
        {
            "handshake_mode": "legacy",
            "missing_metadata": {"server_id": "stripe-mcp", "server_version": "1.0.0"},
            "wire_protocol_version": "2025-03-26",
        },
    )

    for metadata in (
        None,
        {"io.modelcontextprotocol/serverInfo": "#<Ruby::Object:0x0000f55cdaaa6bd0>"},
    ):
        _normalized, identity = _normalize_metadata(
            legacy, metadata, expected_server=None
        )
        assert identity == ("stripe-mcp", "1.0.0")


def test_a_signed_resource_may_be_a_label_without_a_mime_type() -> None:
    """The first providers that actually serve a resource broke both rules.

    Stripe names one "Balance summary"; the signed side demanded an identifier
    for a value MCP calls human-readable -- the same contradiction the manifest
    side resolved for `CapabilityRequirement`. And Wix's three MCP Apps widgets
    state no ``mimeType`` at all, so the receipt carries None and no signed
    value could ever equal it: a package could declare such a resource only by
    inventing a type, which then failed the drift check it was invented for.
    """
    from mcp_platform.client_contracts import _signed_capability_authority

    authority = {
        "tools": {},
        "prompts": {},
        "resource_templates": {},
        "resources": {
            "Balance summary": {
                "uri": "ui://widget/balance-summary.html",
                "mime_type": None,
                "max_bytes": 65536,
            },
            "WixSiteBuilder_RESOURCE": {
                "uri": "ui://widget/site-builder.html",
                "mime_type": "text/html",
                "max_bytes": 65536,
            },
        },
    }

    parsed = _signed_capability_authority(authority)

    assert parsed["resources"]["Balance summary"]["mime_type"] is None
    assert parsed["resources"]["WixSiteBuilder_RESOURCE"]["mime_type"] == "text/html"


@pytest.mark.parametrize(
    "name",
    ["", "x" * 121, " leading", "trailing ", "new\nline"],
)
def test_a_signed_resource_name_is_still_bounded_and_plain(name: str) -> None:
    from mcp_platform.client_contracts import _signed_capability_authority

    with pytest.raises(ValueError):
        _signed_capability_authority(
            {
                "tools": {},
                "prompts": {},
                "resource_templates": {},
                "resources": {
                    name: {"uri": "ui://x.html", "mime_type": None, "max_bytes": 1}
                },
            }
        )
