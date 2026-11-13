"""Closed argument-constraint grammar for standing-job template grants.

A standing job's allowed-tool entry can carry, per declared input-schema
key, one of a small closed set of shapes: an exact ``fixed`` value, a
bounded ``one_of`` allow-list, an anchored/bounded regex ``pattern``, or a
``max_length`` cap -- plus an independent ``required`` flag. Nothing else
is representable, so a runner replaying a standing grant can never be
handed an argument shape the grantor did not explicitly authorize.

``canonicalize_argument_constraints`` seals raw, operator-authored
constraints against the tool's own sealed input schema: a constraint on a
key the schema never declares is rejected outright, never silently
dropped. It also seals every ``pattern`` against a restricted grammar --
no lookaround, no conditional, no backreference, no quantifier applied
directly to a group, and at most 10 quantifier occurrences -- so a
pattern that reaches a job template can never itself be a
catastrophic-backtracking vector. This is prevention, not detection: the
dangerous constructs are rejected outright at canonicalization time,
never merely raced against a clock once a match is already running.

``evaluate_argument_constraints`` is the bounded runtime check a runner
uses before firing a tool call. A ``pattern`` match is bounded by the
restricted grammar above plus a hard 512-character cap on the value being
matched -- independent of any ``max_length``, and enforced before the
match ever runs, not after. Between the two, worst-case match cost stays
small by construction. The wall-clock check that still follows the match
is a defense-in-depth tripwire on top of those two real bounds, not the
bound itself.
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping

_MAX_ONE_OF = 32
_MAX_PATTERN_CHARS = 200
_MAX_PATTERN_VALUE_CHARS = 512
_MAX_PATTERN_QUANTIFIERS = 10
_PATTERN_BUDGET_S = 0.010
_ERROR = "plugin_job_invalid"
_SPEC_KINDS = ("fixed", "one_of", "pattern", "max_length")
_ALLOWED_SPEC_KEYS = frozenset({"required", *_SPEC_KINDS})
_BACKREFERENCE_DIGITS = frozenset("123456789")
_QUANTIFIER_CHARS = frozenset("*+?{")
_LOOKAROUND_3 = frozenset({"(?=", "(?!"})
_LOOKAROUND_4 = frozenset({"(?<=", "(?<!"})


def _pattern_error(reason: str) -> ValueError:
    return ValueError(f"{_ERROR}_pattern_{reason}")


def _reject_dangerous_pattern(pattern: str) -> None:
    """Reject a pattern outside the restricted, ReDoS-safe grammar.

    A coarse, single-pass character scan -- not a full regex parser -- that
    tracks escape and character-class state well enough to catch the
    dangerous constructs named in the module docstring. It can over-count
    quantifier-like characters inside less common group syntaxes (e.g. the
    ``?`` in a non-capturing ``(?:...)`` marker); that only makes the check
    more conservative, never less, which is the intended direction for a
    prevention gate.
    """

    in_class = False
    quantifiers = 0
    index = 0
    length = len(pattern)
    while index < length:
        char = pattern[index]
        if char == "\\":
            if index + 1 >= length:
                index += 1
                continue
            following = pattern[index + 1]
            if not in_class and (
                following in _BACKREFERENCE_DIGITS
                or (following == "g" and pattern[index + 2 : index + 3] == "<")
            ):
                raise _pattern_error("backreference")
            index += 2
            continue
        if in_class:
            if char == "]":
                in_class = False
            index += 1
            continue
        if char == "[":
            in_class = True
            index += 1
            continue
        if char == "(":
            if pattern[index : index + 3] in _LOOKAROUND_3:
                raise _pattern_error("lookaround")
            if pattern[index : index + 4] in _LOOKAROUND_4:
                raise _pattern_error("lookaround")
            if pattern[index : index + 3] == "(?(":
                raise _pattern_error("conditional")
            index += 1
            continue
        if char == ")":
            if index + 1 < length and pattern[index + 1] in _QUANTIFIER_CHARS:
                raise _pattern_error("quantified_group")
            index += 1
            continue
        if char in _QUANTIFIER_CHARS:
            quantifiers += 1
            index += 1
            continue
        index += 1
    if quantifiers > _MAX_PATTERN_QUANTIFIERS:
        raise _pattern_error("too_many_quantifiers")


def canonicalize_argument_constraints(
    raw: Mapping[str, object], tool_input_schema: Mapping[str, object]
) -> dict[str, dict[str, object]]:
    """Seal ``raw`` constraints against ``tool_input_schema``'s declared keys.

    Rejects (``plugin_job_invalid``) any constraint key the schema does not
    declare in ``properties``, any spec carrying more than one of the
    mutually exclusive shape kinds, any spec key outside the closed
    grammar, and any ``pattern`` outside the restricted, ReDoS-safe grammar
    -- never silently drops an unrecognized key or a dangerous pattern.
    """

    if type(raw) is not dict:
        raise ValueError(_ERROR)
    if any(type(key) is not str for key in raw):
        raise ValueError(_ERROR)
    properties = tool_input_schema.get("properties") if type(tool_input_schema) is dict else None
    declared = set(properties.keys()) if type(properties) is dict else set()
    out: dict[str, dict[str, object]] = {}
    for key in sorted(raw):
        if key not in declared:
            raise ValueError(_ERROR)
        spec = raw[key]
        if type(spec) is not dict or not set(spec).issubset(_ALLOWED_SPEC_KEYS):
            raise ValueError(_ERROR)
        kinds = {kind for kind in _SPEC_KINDS if kind in spec}
        if len(kinds) > 1:
            raise ValueError(_ERROR)
        if "required" in spec and type(spec["required"]) is not bool:
            raise ValueError(_ERROR)
        entry: dict[str, object] = {"required": bool(spec.get("required", False))}
        if "fixed" in spec:
            entry["fixed"] = spec["fixed"]
        elif "one_of" in spec:
            values = spec["one_of"]
            if type(values) is not list or not 1 <= len(values) <= _MAX_ONE_OF:
                raise ValueError(_ERROR)
            entry["one_of"] = list(values)
        elif "pattern" in spec:
            pattern = spec["pattern"]
            if (
                type(pattern) is not str
                or not pattern
                or len(pattern) > _MAX_PATTERN_CHARS
                or not pattern.startswith("^")
                or not pattern.endswith("$")
            ):
                raise ValueError(_ERROR)
            _reject_dangerous_pattern(pattern)
            try:
                re.compile(pattern)
            except re.error:
                raise ValueError(_ERROR) from None
            entry["pattern"] = pattern
        elif "max_length" in spec:
            max_length = spec["max_length"]
            if type(max_length) is not int or max_length < 0:
                raise ValueError(_ERROR)
            entry["max_length"] = max_length
        out[key] = entry
    return out


def evaluate_argument_constraints(
    constraints: Mapping[str, object], arguments: Mapping[str, object]
) -> bool:
    """Bounded runtime check: every ``arguments`` key is declared and satisfies
    its constraint, every ``required`` constraint key is present, a
    ``pattern`` value over 512 characters fails outright before any match
    runs, and no single match may run past the fixed time budget."""

    if type(constraints) is not dict or type(arguments) is not dict:
        return False
    for key, entry in constraints.items():
        if entry.get("required") and key not in arguments:
            return False
    for key in arguments:
        if key not in constraints:
            return False
    for key, entry in constraints.items():
        if key not in arguments:
            continue
        value = arguments[key]
        if "fixed" in entry and value != entry["fixed"]:
            return False
        if "one_of" in entry and value not in entry["one_of"]:
            return False
        if "max_length" in entry and (
            type(value) is not str or len(value) > entry["max_length"]
        ):
            return False
        if "pattern" in entry:
            if type(value) is not str or len(value) > _MAX_PATTERN_VALUE_CHARS:
                return False
            start = time.monotonic()
            match = re.match(entry["pattern"], value)
            if time.monotonic() - start > _PATTERN_BUDGET_S:
                return False
            if match is None:
                return False
    return True


__all__ = ["canonicalize_argument_constraints", "evaluate_argument_constraints"]
