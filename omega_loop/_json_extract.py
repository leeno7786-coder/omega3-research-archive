"""Shared robust JSON-object extraction from model output.

Reasoning models emit a ``<think>``/``<thinking>`` trace before the answer, and
the chat template can pre-open ``<think>`` so the trace is unbalanced. The final
JSON lands AFTER the last close tag, often inside a ```json fence. A naive greedy
``{.*}`` over the whole text captures stray braces inside the reasoning and fails
to parse, so we work from the tail after the last reasoning-close tag.
"""

from __future__ import annotations

import ast
import json
import re
from typing import Any


def extract_json_object(text: str | None) -> dict[str, Any] | None:
    """Pull the JSON object out of a model response, tolerating a reasoning
    preamble + markdown fences. Returns None when no JSON object is present."""
    text = (text or "").strip()
    if not text:
        return None

    def _try(candidate: str) -> dict[str, Any] | None:
        candidate = candidate.strip()
        if not candidate:
            return None
        try:
            parsed = json.loads(candidate)
            return parsed if isinstance(parsed, dict) else None
        # Fail-CLOSED on ANY parse pathology, not just JSONDecodeError: a
        # deeply-nested payload can raise RecursionError, etc. Callers treat
        # None as "unparseable" — extract_json_object must never itself raise.
        except (ValueError, RecursionError):
            pass
        # Some model outputs are syntactically Python-literal-ish rather than
        # strict JSON: single-quoted keys/strings, trailing commas, or
        # True/False/None. Accept that shape only when it still forms a full
        # dict literal; anything else continues to fail closed.
        try:
            parsed = ast.literal_eval(candidate)
            return parsed if isinstance(parsed, dict) else None
        except (ValueError, SyntaxError, RecursionError):
            return None

    # 1. Clean response — parses directly.
    direct = _try(text)
    if direct is not None:
        return direct

    # 2. The answer follows the last </think>/</thinking> close tag.
    parts = re.split(r"</think(?:ing)?>", text, flags=re.IGNORECASE)
    region = parts[-1] if len(parts) > 1 else text
    region = region.replace("```json", " ").replace("```", " ")  # drop md fences

    # 3. Greedy first-{ to last-} on the cleaned tail (full nested JSON).
    match = re.search(r"\{.*\}", region, flags=re.DOTALL)
    if match:
        tail = _try(match.group(0))
        if tail is not None:
            return tail

    # 4. Greedy over the whole text (handles reasoning braces before the answer).
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if match:
        whole = _try(match.group(0))
        if whole is not None:
            return whole

    # 5. Try all balanced top-level {…} blocks in order — the model may wrap the
    #    output in an array [{…}] or emit multiple brace blocks with the JSON last.
    blocks = _find_top_level_blocks(region)
    for block in blocks:
        result = _try(block)
        if result is not None:
            return result

    # 6. Tolerant repair — LAST resort, only after every strict attempt failed,
    # so well-formed output is never touched. A reasoning model occasionally
    # mis-closes a bracket (closes a "[" array with "}") or truncates the
    # trailing closers under prefill pressure; either makes the whole object
    # unparseable. Repair the bracket balance (string/escape-aware) and re-parse.
    # If the *content* is not valid JSON, json.loads still fails and we return
    # None — repair fixes structure, it never fabricates a value from garbage.
    repaired = _repair_brackets(region)
    if repaired is not None:
        _emit_repaired_parse()
        return repaired
    repaired = _repair_brackets(text)
    if repaired is not None:
        _emit_repaired_parse()
        return repaired
    return None


_OPENERS = {"{": "}", "[": "]"}
_CLOSERS = {"}", "]"}


def _repair_brackets(region: str) -> dict[str, Any] | None:
    """Rebuild a balanced top-level JSON object from the first ``{``.

    Scans character by character honoring string literals + escapes. When a
    closer mismatches the innermost opener, the CORRECT closer is substituted
    (fixing ``[ ... }`` → ``[ ... ]``); any openers still on the stack at end of
    input get their closers appended (fixing truncation). The rebuilt string is
    handed to ``json.loads`` — invalid content yields ``None``, not a guess.
    """
    start = region.find("{")
    if start < 0:
        return None

    out: list[str] = []
    stack: list[str] = []
    in_str = False
    esc = False
    for ch in region[start:]:
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
        elif ch in _OPENERS:
            stack.append(_OPENERS[ch])
            out.append(ch)
        elif ch in _CLOSERS:
            if not stack:
                break  # extra closer with nothing open — object already complete
            out.append(stack.pop())  # substitute the EXPECTED closer
            if not stack:
                break  # top-level object closed — ignore any trailing text
        else:
            out.append(ch)

    while stack:  # truncated input — append the missing closers
        out.append(stack.pop())

    try:
        parsed = json.loads("".join(out))
    except (ValueError, RecursionError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _find_top_level_blocks(text: str) -> list[str]:
    """Return all balanced top-level `{...}` blocks in order, handling nesting.

    This captures every distinct JSON object region. A model that wraps output in
    an array ``[{...}]`` yields one block; a model that emits prose with multiple
    brace-delimited snippets yields them left-to-right. The outermost braces are
    included in each returned block.
    """
    blocks: list[str] = []
    start = -1
    depth = 0
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                blocks.append(text[start : i + 1])
                start = -1
    return blocks


def _emit_repaired_parse() -> None:
    """Soft telemetry so a salvaged parse is never silent (a dashboard hiccup
    must never break extraction)."""
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "omega",
            "repaired_parse",
            summary="model JSON repaired (bracket balance) before parse",
        )
    except Exception:
        pass
