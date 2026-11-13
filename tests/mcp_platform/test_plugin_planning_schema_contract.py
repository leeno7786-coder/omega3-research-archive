"""The planning contract must admit the schemas real MCP servers publish.

Until 2026-09-06 it demanded an explicit ``additionalProperties`` and refused
``description``, so not one shipped plugin tool was plannable, retrieval
returned no candidate for any request, and the chat route into a plugin could
never fire. These tests pin both halves: real schemas are admitted, and the
contract stays closed against everything it was protecting against.

The end-to-end consequence -- a chat turn reaching a plugin without a
permission card for a read -- is covered by
``tests/mcp_platform/test_plugin_drive_chat_eligibility.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_platform import plugin_planning as planning

PACKAGES = Path("plugin_platform/marketplace/packages")

# The 10 shipped tools of 431 the planner cannot fill, named so a NEW one fails
# this test rather than passing quietly. Four causes, and none of them is a
# provider merely writing an open argument object -- the contract closes those
# itself now, which took this list from 43 to 10.
#
# * TOO DEEP OR TOO LARGE (4). Notion's query DSL recurses: its argument schemas
#   reach depth 19-32 against a bound of 8, and up to 64 KiB against 8 KiB.
# * A CYCLE IN A LOCAL $ref (3). Zapier factors a self-referential JSON value
#   into `definitions/__schema0`, which cannot be flattened into one document.
#   Fails closed on purpose.
# * A FREE-FORM RAW API BODY (2). Wix's `body` is an object with no declared
#   properties, so closing it leaves nothing the planner may fill.
# * AN ENUM OVER THE ITEM BOUND (1). Linear's `list_issues.fields` offers 37
#   values against `_MAX_ITEMS` of 32.
#
# Every one of these ships and is authorized; only the chat route cannot reach
# them. The list is expected to SHRINK -- if a provider tightens a schema or the
# contract learns a construct, delete the entry.
UNPLANNABLE_BY_DESIGN = frozenset(
    {
        ("linear.official", "list_issues"),
        ("notion.official", "notion-create-comment"),
        ("notion.official", "notion-query-data-sources"),
        ("notion.official", "notion-query-meeting-notes"),
        ("notion.official", "notion-query-sessions"),
        ("wix.official", "CallWixSiteAPI"),
        ("wix.official", "ManageWixSite"),
        ("zapier.official", "execute_zapier_read_action"),
        ("zapier.official", "execute_zapier_write_action"),
        ("zapier.official", "inspect_zapier_actions"),
    }
)


def _tools(plugin_id: str) -> dict[str, dict]:
    authority = json.loads((PACKAGES / plugin_id / "tools" / "authority.json").read_bytes())
    return next(iter(authority["apps"].values()))["tools"]


def _shipped_tools() -> list[tuple[str, str, dict]]:
    rows: list[tuple[str, str, dict]] = []
    for package in sorted(PACKAGES.iterdir()):
        if not (package / "tools" / "authority.json").is_file():
            continue
        for name, tool in _tools(package.name).items():
            rows.append((package.name, name, tool))
    return rows


@pytest.mark.parametrize(
    ("plugin_id", "tool_name", "tool"),
    [pytest.param(*row, id=f"{row[0]}::{row[1]}") for row in _shipped_tools()],
)
def test_every_shipped_tool_is_plannable(plugin_id: str, tool_name: str, tool: dict) -> None:
    """A tool that cannot be planned can never be reached from chat.

    A tool on UNPLANNABLE_BY_DESIGN must still be refused. These rows used to skip, so an entry
    whose schema a provider had since tightened stayed on the list silently; now it fails here
    and the entry gets deleted, as the list's comment asks.
    """
    if (plugin_id, tool_name) in UNPLANNABLE_BY_DESIGN:
        with pytest.raises(ValueError, match="plugin_plan_invalid"):
            planning._schema(tool["input_schema"])
        planning._result_schema(tool["output_schema"])
        return

    planning._schema(tool["input_schema"])
    planning._result_schema(tool["output_schema"])


def test_an_argument_object_closes_and_a_result_object_opens() -> None:
    """The planner may never invent an argument; a provider may add a field."""
    declared = {"type": "object", "properties": {"query": {"type": "string"}}}

    arguments = planning._schema(declared)
    result = planning._result_schema(declared)

    assert arguments["additionalProperties"] is False
    assert result["additionalProperties"] is True


def test_an_explicitly_open_argument_object_is_closed_rather_than_refused() -> None:
    """The invariant is what the PLANNER may emit, not what the provider wrote.

    `_schema` already normalized an omitted `additionalProperties` -- 107 of the
    shipped tools are plannable only because of that -- while refusing a provider
    who wrote the open value explicitly. That cost 41 of 431 planner-visible
    tools their reachability from chat, 30 of them Notion's, for a difference in
    how the same intent was spelled.

    Closing it here keeps the guarantee exactly: the planner is handed a schema
    that admits only declared arguments, so it still cannot invent one. It is
    also stricter than the provider's own contract, so a server that tolerates
    extras still accepts a call that sends none.
    """
    for stated in (True, {}, {"type": "string"}):
        admitted = planning._schema(
            {
                "type": "object",
                "additionalProperties": stated,
                "properties": {"q": {"type": "string"}},
            }
        )
        assert admitted["additionalProperties"] is False, stated
        assert set(admitted["properties"]) == {"q"}


def test_a_result_object_keeps_its_own_openness() -> None:
    """Only an argument is closed. The planner never fills a result."""
    opened = planning._result_schema({"type": "object", "properties": {}})
    assert opened["additionalProperties"] is True

    closed = planning._schema(
        {"type": "object", "additionalProperties": False, "properties": {}},
        open_default=True,
    )
    assert closed["additionalProperties"] is False


def test_annotations_are_admitted_and_bounded() -> None:
    admitted = planning._schema(
        {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to search for.",
                    "title": "Query",
                    "format": "text",
                    "deprecated": False,
                }
            },
        }
    )

    assert admitted["properties"]["query"]["description"] == "What to search for."
    over_long = {
        "type": "object",
        "properties": {"q": {"type": "string", "description": "x" * 5000}},
    }
    with pytest.raises(ValueError, match="^plugin_plan_invalid$"):
        planning._schema(over_long)
    wrong_type = {"type": "object", "properties": {"q": {"type": "string", "description": 7}}}
    with pytest.raises(ValueError, match="^plugin_plan_invalid$"):
        planning._schema(wrong_type)


def test_a_vendor_extension_keyword_is_dropped_rather_than_refused() -> None:
    """One provider annotation must not make an ordinary tool unusable."""
    admitted = planning._schema(
        {
            "type": "object",
            "x-google-enum-descriptions": ["ignored"],
            "properties": {"q": {"type": "string", "x-mcp-header": "ignored"}},
        }
    )

    assert "x-google-enum-descriptions" not in admitted
    assert "x-mcp-header" not in admitted["properties"]["q"]


def test_a_union_type_is_admitted_but_an_unknown_member_is_not() -> None:
    admitted = planning._schema(
        {"type": "object", "properties": {"cursor": {"type": ["string", "null"]}}}
    )

    assert admitted["properties"]["cursor"]["type"] == ["string", "null"]
    with pytest.raises(ValueError, match="^plugin_plan_invalid$"):
        planning._schema({"type": "object", "properties": {"q": {"type": ["string", "widget"]}}})


def test_local_definitions_inline_and_every_other_reference_fails_closed() -> None:
    inlined = planning._schema(
        {
            "$defs": {"Attendee": {"type": "object", "properties": {"email": {"type": "string"}}}},
            "type": "object",
            "properties": {"attendee": {"$ref": "#/$defs/Attendee"}},
        }
    )

    assert inlined["properties"]["attendee"]["properties"]["email"]["type"] == "string"
    assert "$defs" not in inlined

    for hostile in (
        {"type": "object", "properties": {"a": {"$ref": "https://elsewhere.example/x"}}},
        {"type": "object", "properties": {"a": {"$ref": "#/$defs/absent"}}},
        {
            "$defs": {"A": {"type": "object", "properties": {"a": {"$ref": "#/$defs/A"}}}},
            "type": "object",
            "properties": {"a": {"$ref": "#/$defs/A"}},
        },
    ):
        with pytest.raises(ValueError, match="^plugin_plan_invalid$"):
            planning._schema(hostile)


def test_an_unknown_keyword_is_still_refused() -> None:
    """Catches widening the contract into an open one."""
    with pytest.raises(ValueError, match="^plugin_plan_invalid$"):
        planning._schema({"type": "object", "properties": {}, "unevaluatedProperties": True})


# --- what real providers write, added 2026-09-07 ------------------------------
#
# Six surfaces captured that day declared 213 tools between them, and one
# unanswerable keyword makes a whole tool uncallable. These are the keywords
# they actually used.


def test_dialect_and_payload_metadata_is_dropped_rather_than_refused() -> None:
    """Every Hugging Face, Netlify, Wix and Neon tool declares ``$schema``.

    It names the JSON Schema dialect. Refusing it made four whole providers
    unplannable, which is to say uncallable, and bounded nothing.
    """
    admitted = planning._schema(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://vendor.example/tool",
            "$comment": "internal",
            "type": "object",
            "properties": {"body": {"type": "string", "contentEncoding": "base64"}},
        }
    )

    assert set(admitted) == {"type", "properties", "additionalProperties"}
    assert "contentEncoding" not in admitted["properties"]["body"]


def test_a_provider_regex_never_reaches_the_normalized_document() -> None:
    """``pattern`` is dropped ON PURPOSE, not because it was overlooked.

    The normalized document is handed to a jsonschema validator, and compiling
    an untrusted provider regex there is a denial-of-service surface. The
    provider still enforces its own pattern server-side; this contract exists to
    bound what the PLANNER may emit, not to re-validate the provider.
    """
    admitted = planning._schema(
        {
            "type": "object",
            "properties": {"id": {"type": "string", "pattern": "^(a+)+$"}},
            "propertyNames": {"pattern": "^(x+)+$"},
        }
    )

    assert "pattern" not in admitted["properties"]["id"]
    assert "propertyNames" not in admitted
    assert "(a+)+" not in json.dumps(admitted)


def test_nullable_is_dropped_and_can_only_narrow() -> None:
    """The OpenAPI spelling of a type union. Dropping it never widens."""
    admitted = planning._schema(
        {"type": "object", "properties": {"cursor": {"type": "string", "nullable": True}}}
    )

    assert admitted["properties"]["cursor"] == {"type": "string"}


def test_const_becomes_the_single_item_enum_it_is() -> None:
    """Netlify pins each union branch with a ``const`` operation name.

    It is the narrowest constraint JSON Schema has, so it is normalized into the
    enum the contract already bounds rather than dropped with the metadata.
    """
    admitted = planning._schema(
        {"type": "object", "properties": {"operation": {"type": "string", "const": "get-project"}}}
    )

    assert admitted["properties"]["operation"]["enum"] == ["get-project"]
    assert "const" not in admitted["properties"]["operation"]

    stated = planning._schema(
        {
            "type": "object",
            "properties": {"op": {"type": "string", "const": "a", "enum": ["a", "b"]}},
        }
    )
    assert stated["properties"]["op"]["enum"] == ["a", "b"]


def test_a_branch_union_is_admitted_and_every_branch_is_normalized() -> None:
    """Netlify discriminates six operations per tool with ``anyOf``.

    A branch may omit "type" because the union describes the shape. What it may
    not do is admit anything an ordinary subschema could not -- each branch goes
    through this same contract, so an open argument object inside a branch is
    refused exactly as it is outside one.
    """
    admitted = planning._schema(
        {
            "type": "object",
            "properties": {
                "selectSchema": {
                    "anyOf": [
                        {"type": "object", "properties": {"siteId": {"type": "string"}}},
                        {"type": "object", "properties": {"teamSlug": {"type": "string"}}},
                    ]
                }
            },
        }
    )

    branches = admitted["properties"]["selectSchema"]["anyOf"]
    assert [branch["additionalProperties"] for branch in branches] == [False, False]

    # A branch is normalized by the same contract, so an open argument object
    # inside one is closed exactly as it is outside one.
    normalized = planning._schema(
        {
            "type": "object",
            "properties": {
                "s": {
                    "anyOf": [
                        {"type": "object", "additionalProperties": True, "properties": {}},
                        {"type": "object", "properties": {"id": {"type": "string"}}},
                    ]
                }
            },
        }
    )
    assert [b["additionalProperties"] for b in normalized["properties"]["s"]["anyOf"]] == [
        False,
        False,
    ]
    for malformed in ({"anyOf": []}, {"anyOf": {}}, {"oneOf": [{"type": "widget"}]}):
        with pytest.raises(ValueError, match="^plugin_plan_invalid$"):
            planning._schema({"type": "object", "properties": {"s": malformed}})


def test_a_looser_declared_bound_is_clamped_not_refused() -> None:
    """Miro declares ``maxLength: 200000`` on an SVG body.

    That is the PROVIDER's limit. Omega's applies regardless, so refusing the
    schema bounded nothing and cost the tool.
    """
    admitted = planning._schema(
        {
            "type": "object",
            "properties": {
                "svg": {"type": "string", "maxLength": 200_000},
                "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
            },
        }
    )

    assert admitted["properties"]["svg"]["maxLength"] == planning._MAX_TEXT
    assert admitted["properties"]["tags"]["maxItems"] == 5

    with pytest.raises(ValueError, match="^plugin_plan_invalid$"):
        planning._schema({"type": "object", "properties": {"s": {"type": "string", "maxLength": -1}}})


def test_a_draft_07_definitions_pointer_resolves_like_a_defs_one() -> None:
    """Notion and Zapier emit ``#/definitions/``.

    It is what draft-07 called ``$defs`` -- the same construct, renamed in
    2019-09 -- so which one a provider writes says nothing but which draft its
    generator targeted. Resolving only one made a local reference fail on that.
    """
    inlined = planning._schema(
        {
            "definitions": {
                "Page": {"type": "object", "properties": {"id": {"type": "string"}}}
            },
            "type": "object",
            "properties": {"page": {"$ref": "#/definitions/Page"}},
        }
    )

    assert inlined["properties"]["page"]["properties"]["id"]["type"] == "string"
    assert "definitions" not in inlined

    for hostile in (
        {"type": "object", "properties": {"a": {"$ref": "#/definitions/absent"}}},
        {
            "definitions": {"A": {"type": "object", "properties": {"a": {"$ref": "#/definitions/A"}}}},
            "type": "object",
            "properties": {"a": {"$ref": "#/definitions/A"}},
        },
    ):
        with pytest.raises(ValueError, match="^plugin_plan_invalid$"):
            planning._schema(hostile)


def test_the_exclusive_numeric_bounds_are_admitted() -> None:
    """They sit beside ``minimum``/``maximum`` and only narrow."""
    admitted = planning._schema(
        {
            "type": "object",
            "properties": {
                "count": {"type": "integer", "exclusiveMinimum": 0, "exclusiveMaximum": 100}
            },
        }
    )

    assert admitted["properties"]["count"]["exclusiveMinimum"] == 0
    assert admitted["properties"]["count"]["exclusiveMaximum"] == 100


def test_no_tool_is_listed_unplannable_that_the_planner_can_now_fill() -> None:
    """The exception list must shrink honestly, never linger.

    A tool that starts planning after a provider tightens its schema -- or after
    this contract learns another construct -- has to be removed from the list,
    or the list stops meaning anything.
    """
    stale = [
        (plugin_id, name)
        for plugin_id, name, tool in _shipped_tools()
        if (plugin_id, name) in UNPLANNABLE_BY_DESIGN
        and _is_plannable(tool)
    ]

    assert stale == []


def _is_plannable(tool: dict) -> bool:
    try:
        planning._schema(tool["input_schema"])
        planning._result_schema(tool["output_schema"])
    except Exception:  # noqa: BLE001 - the question is only whether it raised
        return False
    return True


def test_the_contract_answers_the_same_for_a_frozen_schema_as_a_plain_one() -> None:
    """A schema's REPRESENTATION must not change whether it is plannable.

    `_freeze` turns every array into a tuple and the planner is handed frozen
    schemas, but `_inline_refs` matched only `list` -- so on the production path
    `$ref` resolution and its cycle guard silently skipped anything inside an
    array. Zapier's self-referential `definitions/__schema0` therefore failed
    closed when the schema was read from the package file and passed when the
    same bytes arrived from a live binding, which meant this whole contract test
    was not measuring what production does.
    """
    recursive = {
        "type": "object",
        "definitions": {
            "node": {
                "type": "object",
                "properties": {"children": {"type": "array", "items": {"$ref": "#/definitions/node"}}},
            }
        },
        "properties": {"root": {"anyOf": [{"$ref": "#/definitions/node"}]}},
    }

    def plannable(schema: object) -> bool:
        try:
            planning._schema(schema)
        except Exception:  # noqa: BLE001 - only whether it raised
            return False
        return True

    # A cycle fails closed, and it fails closed both ways.
    assert plannable(recursive) is False
    assert plannable(planning._freeze(recursive)) is False

    ordinary = {
        "type": "object",
        "$defs": {"who": {"type": "string"}},
        "properties": {"names": {"type": "array", "items": {"$ref": "#/$defs/who"}}},
    }
    assert plannable(ordinary) is True
    assert plannable(planning._freeze(ordinary)) is True
    # And a reference inside an array really is resolved, not skipped.
    assert planning._schema(planning._freeze(ordinary))["properties"]["names"]["items"] == {
        "type": "string"
    }
