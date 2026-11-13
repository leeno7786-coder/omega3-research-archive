"""Cross-platform graphify PreToolUse hook (replaces bash hook on Windows)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_SEARCH_MARKERS = ("grep", "rg ", "ripgrep", "find ", "fd ", "ack ", "ag ")
_CONTEXT = (
    '{"hookSpecificOutput":{"hookEventName":"PreToolUse",'
    '"additionalContext":"graphify: knowledge graph at graphify-out/. '
    'For focused questions, run `graphify query \\"<question>\\"` '
    "(scoped subgraph, usually much smaller than GRAPH_REPORT.md) "
    "instead of grepping raw files. Read GRAPH_REPORT.md only for broad "
    'architecture context."}}'
)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0

    cmd = payload.get("tool_input", payload).get("command", "")
    if not any(marker in cmd for marker in _SEARCH_MARKERS):
        return 0

    graph = Path("graphify-out/graph.json")
    if graph.is_file():
        print(_CONTEXT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
