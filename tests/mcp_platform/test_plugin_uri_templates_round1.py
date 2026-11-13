from __future__ import annotations

import pytest

from mcp_platform.client_contracts import McpClientLimits
from mcp_platform.uri_templates import matches_uri_template


@pytest.mark.parametrize(
    "uri",
    (
        "docs://projects/alpha%2fsecret",
        "docs://projects/alpha%2Fsecret",
        "docs://projects/alpha%5csecret",
        "docs://projects/alpha%5Csecret",
        "docs://projects/alpha%2f..%5csecret",
        "docs://projects/%2e%2e",
        "docs://projects/..",
        "docs://projects/%252f",
        "docs://projects/%255c",
        "docs://projects/%252e%252e",
        "docs://projects/%2541",
        "docs://projects/%FF",
        "docs://projects/%C3%28",
        "docs://projects/bad%",
        "docs://projects/bad%2",
    ),
)
def test_template_variable_rejects_encoded_separators_traversal_and_bad_utf8(
    uri: str,
) -> None:
    assert not matches_uri_template(
        "docs://projects/{project}",
        uri,
        McpClientLimits(),
    )


def test_template_variable_rejects_literal_separator_even_for_reserved_operator() -> None:
    assert not matches_uri_template(
        "docs://projects/{+project}",
        "docs://projects/alpha/secret",
        McpClientLimits(),
    )


@pytest.mark.parametrize(
    "uri",
    (
        "docs://projects/alpha",
        "docs://projects/alpha%20beta",
        "docs://projects/%E2%9C%93",
        "docs://projects/a%2Bb",
        "docs://projects/a.b-c_d~e",
    ),
)
def test_template_variable_accepts_canonical_escaped_data(uri: str) -> None:
    assert matches_uri_template(
        "docs://projects/{project}",
        uri,
        McpClientLimits(),
    )


@pytest.mark.parametrize(
    ("template", "uri"),
    (
        ("docs://projects/{project}", "docs://projects/%41"),
        ("docs://projects/{project}", "docs://projects/%e2%9c%93"),
        ("docs://projects/../{project}", "docs://projects/../alpha"),
        ("docs://projects/%2E%2E/{project}", "docs://projects/%2E%2E/alpha"),
        ("docs://projects/%70refix/{project}", "docs://projects/%70refix/alpha"),
    ),
)
def test_whole_template_and_uri_require_one_canonical_nontraversing_path(
    template: str,
    uri: str,
) -> None:
    assert not matches_uri_template(template, uri, McpClientLimits())


def test_client_and_runtime_share_the_one_template_matcher() -> None:
    from mcp_platform import client, plugin_runtime, uri_templates

    assert client._matches_uri_template is uri_templates.matches_uri_template
    assert plugin_runtime._matches_uri_template is uri_templates.matches_uri_template
