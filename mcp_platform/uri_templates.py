"""Single strict URI-template matching boundary for MCP capability surfaces."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from urllib.parse import unquote_to_bytes, urlsplit

if TYPE_CHECKING:
    from mcp_platform.client_contracts import McpClientLimits

_SECOND_DECODE = re.compile(r"%[0-9A-Fa-f]{2}")
_ENCODED_PATH_SEPARATOR = re.compile(r"%(?:2F|5C)")
_UNRESERVED_BYTES = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)


def _canonical_encoded_text(value: str) -> str | None:
    """Return strict once-decoded UTF-8 from one canonical ASCII spelling."""

    if type(value) is not str or not value or not value.isascii():
        return None
    encoded = bytearray()
    position = 0
    while position < len(value):
        character = value[position]
        if character != "%":
            encoded.append(ord(character))
            position += 1
            continue
        if position + 2 >= len(value):
            return None
        pair = value[position + 1 : position + 3]
        if (
            pair != pair.upper()
            or any(item not in "0123456789ABCDEF" for item in pair)
        ):
            return None
        octet = int(pair, 16)
        if octet in _UNRESERVED_BYTES:
            return None
        encoded.append(octet)
        position += 3
    try:
        decoded = bytes(encoded).decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    if (
        _SECOND_DECODE.search(decoded)
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in decoded)
    ):
        return None
    return decoded


def _strict_path(value: str) -> bool:
    if _ENCODED_PATH_SEPARATOR.search(value):
        return False
    try:
        path = urlsplit(value).path
    except ValueError:
        return False
    decoded = _canonical_encoded_text(path) if path else ""
    if decoded is None or "\\" in decoded:
        return False
    return all(segment not in {".", ".."} for segment in decoded.split("/"))


def _template_shape(pieces: tuple[tuple[str, object | None], ...]) -> str:
    shaped: list[str] = []
    for literal, expression in pieces:
        shaped.append(literal)
        if expression is None:
            continue
        operator = expression.operator
        shaped.append(
            {
                "/": "/x",
                ".": ".x",
                ";": ";x=x",
                "?": "?x=x",
                "&": "&x=x",
                "#": "#x",
            }.get(operator, "x")
        )
    return "".join(shaped)


def _strict_variable_atom(value: str) -> bool:
    """Validate one expansion atom as exactly-once decoded UTF-8 data."""

    if "/" in value or "\\" in value:
        return False
    decoded = _canonical_encoded_text(value)
    if decoded is None:
        return False
    if (
        "/" in decoded
        or "\\" in decoded
        or decoded in {".", ".."}
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in decoded)
    ):
        return False
    if _SECOND_DECODE.search(decoded):
        return False
    try:
        second = unquote_to_bytes(decoded).decode("utf-8", errors="strict")
    except (UnicodeDecodeError, UnicodeEncodeError, ValueError):
        return False
    return second not in {".", ".."}


def _strict_expansion(operator: str, expansion: str) -> bool:
    if not expansion:
        return True
    if operator == "/":
        if not expansion.startswith("/"):
            return False
        atoms = expansion[1:].split("/")
    elif operator == ".":
        if not expansion.startswith("."):
            return False
        atoms = expansion[1:].split(".")
    elif operator in {"?", "&", ";"}:
        if not expansion.startswith(operator):
            return False
        separator = ";" if operator == ";" else "&"
        atoms = []
        for component in expansion[1:].split(separator):
            key, marker, value = component.partition("=")
            atoms.append(key)
            if marker:
                atoms.extend(value.split(","))
    else:
        value = expansion[1:] if operator == "#" else expansion
        atoms = value.split(",")
    return bool(atoms) and all(_strict_variable_atom(atom) for atom in atoms)


def matches_uri_template(
    template: str,
    uri: str,
    limits: McpClientLimits,
) -> bool:
    """Match a URI while rejecting traversal and separator-bearing expansions."""

    from mcp_platform.client import _legacy_matches_uri_template, _parse_uri_template

    if _canonical_encoded_text(template) is None or _canonical_encoded_text(uri) is None:
        return False
    pieces = _parse_uri_template(template, max_variables=limits.max_list_items)
    if not _strict_path(_template_shape(pieces)) or not _strict_path(uri):
        return False
    if not _legacy_matches_uri_template(template, uri, limits):
        return False
    pattern: list[str] = [r"\A"]
    expressions = []
    for literal, expression in pieces:
        if expression is None:
            pattern.append(re.escape(literal))
            continue
        capture = {
            "/": r"(?:/[^?#]*)?",
            ".": r"(?:\.[^/?#]*)?",
            ";": r"(?:;[^/?#]*)?",
            "?": r"(?:\?[^#]*)?",
            "&": r"(?:&[^#]*)?",
            "#": r"(?:\#.*)?",
        }.get(expression.operator, r".*?")
        pattern.append(f"(?P<expression_{len(expressions)}>{capture})")
        expressions.append(expression)
    pattern.append(r"\Z")
    matched = re.fullmatch("".join(pattern), uri)
    return matched is not None and all(
        _strict_expansion(
            expression.operator,
            matched.group(f"expression_{index}"),
        )
        for index, expression in enumerate(expressions)
    )


__all__ = ["matches_uri_template"]
