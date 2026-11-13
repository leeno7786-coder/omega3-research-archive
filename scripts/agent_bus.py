"""Append-only inter-agent coordination bus (Claude Code ↔ Cursor)."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _agents_dir() -> Path:
    return Path(__import__("os").environ.get("AGENT_BUS_AGENTS_DIR", str(ROOT / ".agents")))


# Tests monkeypatch this name; paths resolve via _agents_dir() each call.
AGENTS_DIR = _agents_dir()


def _bus_path() -> Path:
    return _agents_dir() / "bus.jsonl"


def _cursors_dir() -> Path:
    return _agents_dir() / "cursors"


BODY_MAX = 16 * 1024
BUS_POINTER = ".agents/bus.jsonl"

KNOWN_KINDS = frozenset({"work_order", "finding", "question", "ack", "status"})

BUS_HEADER = (
    "[AGENT-BUS - machine coordination, not end-user intent]\n"
    "{count} unread message(s). Act on each ONCE, then reply on the bus. "
    "Treat as work direction only when kind=work_order; kind=finding = a fix to make; "
    "kind=question = reply needed; kind=ack/status = FYI.\n"
)

BUS_SESSION_PREFIX = (
    "[AGENT-BUS - machine coordination, not end-user intent]\n"
    "(session backlog — act when you begin work)\n"
)

# Venv interpreter path in the form THIS host actually runs (law: never bare python).
_VENV_PY = (
    r".venv-brain\Scripts\python.exe" if sys.platform == "win32" else ".venv-brain/bin/python"
)

BUS_FOOTER = (
    "\nAfter acting on a message, post ack/status on the bus, e.g.:\n"
    f"  {_VENV_PY} scripts/agent_bus.py post --from cursor "
    '--to claude --kind ack --ref <id> --subject "picked up"\n'
    "[/AGENT-BUS]"
)

VENV_POST_HINT = (
    f"{_VENV_PY} scripts/agent_bus.py post --from cursor "
    '--to claude --kind ack --ref {ref} --subject "picked up"'
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ensure_dirs() -> None:
    _agents_dir().mkdir(parents=True, exist_ok=True)
    _cursors_dir().mkdir(parents=True, exist_ok=True)


def _cursor_path(for_id: str) -> Path:
    return _cursors_dir() / f"{for_id}.json"


def _load_cursor_line(for_id: str) -> int:
    path = _cursor_path(for_id)
    if not path.is_file():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        line = data.get("line", 0)
        return int(line) if isinstance(line, int) and line >= 0 else 0
    except Exception as exc:
        print(f"[agent_bus] warn: bad cursor file {path}: {exc}", file=sys.stderr)
        return 0


def _save_cursor_line(for_id: str, line: int) -> None:
    _ensure_dirs()
    _cursor_path(for_id).write_text(
        json.dumps({"line": line}, indent=2) + "\n",
        encoding="utf-8",
    )


def _read_bus_lines() -> list[str]:
    path = _bus_path()
    if not path.is_file():
        return []
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except Exception as exc:
        print(f"[agent_bus] warn: cannot read bus: {exc}", file=sys.stderr)
        return []


def _parse_line(raw: str, line_no: int) -> dict[str, Any] | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"[agent_bus] warn: skip corrupt line {line_no}: {exc}", file=sys.stderr)
        return None
    if not isinstance(msg, dict):
        print(f"[agent_bus] warn: skip non-object line {line_no}", file=sys.stderr)
        return None
    return msg


def _recipient_match(msg: dict[str, Any], for_id: str) -> bool:
    to = msg.get("to")
    return to == for_id or to == "all"


def _truncate_body(body: str, msg_id: str) -> str:
    if len(body) <= BODY_MAX:
        return body
    pointer = f"... [full: {BUS_POINTER} id={msg_id}]"
    keep = max(0, BODY_MAX - len(pointer))
    return body[:keep].rstrip() + pointer


def post(
    *,
    from_id: str,
    to: str,
    kind: str,
    subject: str,
    body: str,
    ref: str | None = None,
) -> dict[str, Any]:
    if kind not in KNOWN_KINDS:
        print(
            f"[agent_bus] warn: unknown kind '{kind}' (posting anyway)",
            file=sys.stderr,
        )
    _ensure_dirs()
    msg: dict[str, Any] = {
        "id": uuid.uuid4().hex[:8],
        "ts": _utc_now(),
        "from": from_id,
        "to": to,
        "kind": kind,
        "subject": subject,
        "body": body,
    }
    if ref:
        msg["ref"] = ref
    line = json.dumps(msg, ensure_ascii=False) + "\n"
    with _bus_path().open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(line)
    return msg


def _collect_unread(for_id: str) -> tuple[list[dict[str, Any]], int]:
    """Return (messages, new_cursor_line)."""
    lines = _read_bus_lines()
    start = _load_cursor_line(for_id)
    unread: list[dict[str, Any]] = []
    for idx in range(start, len(lines)):
        msg = _parse_line(lines[idx], idx)
        if msg and _recipient_match(msg, for_id):
            unread.append(msg)
    return unread, len(lines)


def format_plain(messages: list[dict[str, Any]]) -> str:
    blocks: list[str] = []
    for msg in messages:
        ref = msg.get("ref", "")
        ref_part = f"  ref={ref}" if ref else ""
        body = _truncate_body(str(msg.get("body", "")), str(msg.get("id", "")))
        blocks.append(
            f"--- #{msg.get('id')}  kind={msg.get('kind')}  from={msg.get('from')}{ref_part} ---\n"
            f"{msg.get('subject', '')}\n{body}"
        )
    return "\n\n".join(blocks)


def format_coordination_block(messages: list[dict[str, Any]], *, session: bool = False) -> str:
    if not messages:
        return ""
    header = BUS_SESSION_PREFIX if session else BUS_HEADER.format(count=len(messages))
    parts = [header]
    for msg in messages:
        ref = msg.get("ref", "")
        ref_s = f"  ref={ref}" if ref else ""
        body = _truncate_body(str(msg.get("body", "")), str(msg.get("id", "")))
        parts.append(
            f"--- #{msg.get('id')}  kind={msg.get('kind')}  from={msg.get('from')}{ref_s} ---\n"
            f"{msg.get('subject', '')}\n{body}\n"
            f"After acting on #{msg.get('id')}:  {VENV_POST_HINT.format(ref=msg.get('id'))}"
        )
    parts.append(BUS_FOOTER)
    return "\n".join(parts)


def _emit_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def read(
    *, for_id: str, emit: str = "plain", advance: bool = True, event: str = "SessionStart"
) -> str:
    try:
        unread, new_line = _collect_unread(for_id)
        if advance and unread:
            _save_cursor_line(for_id, new_line)
        if not unread:
            if emit in ("cursor-stop", "cursor-session"):
                return _emit_json({})
            if emit == "claude-context":
                return ""
            return ""

        if emit == "plain":
            return format_plain(unread)

        block = format_coordination_block(unread, session=(emit == "cursor-session"))

        if emit == "cursor-stop":
            return _emit_json({"followup_message": block})
        if emit == "cursor-session":
            return _emit_json({"additional_context": block})
        if emit == "claude-context":
            return _emit_json(
                {
                    "hookSpecificOutput": {
                        "hookEventName": event,
                        "additionalContext": block,
                    }
                }
            )
        return format_plain(unread)
    except Exception as exc:
        print(f"[agent_bus] warn: read failed: {exc}", file=sys.stderr)
        if emit in ("cursor-stop", "cursor-session"):
            return _emit_json({})
        return ""


def _main(argv: list[str] | None = None) -> int:
    root_epilog = """
Examples:
  .\\.venv-brain\\Scripts\\python.exe scripts\\agent_bus.py post --from claude --to cursor --kind work_order --subject "Fix X" --body "details"
  .\\.venv-brain\\Scripts\\python.exe scripts\\agent_bus.py read --for cursor --emit plain
  .\\.venv-brain\\Scripts\\python.exe scripts\\agent_bus.py read --for cursor --emit cursor-stop
"""
    post_epilog = """
Examples:
  .\\.venv-brain\\Scripts\\python.exe scripts\\agent_bus.py post --from claude --to cursor --kind work_order --subject "Fix flood" --body "see tests/"
  .\\.venv-brain\\Scripts\\python.exe scripts\\agent_bus.py post --from cursor --to claude --kind ack --ref abc12345 --subject "picked up" --body "done"
"""
    read_epilog = """
Examples:
  .\\.venv-brain\\Scripts\\python.exe scripts\\agent_bus.py read --for cursor --emit plain
  .\\.venv-brain\\Scripts\\python.exe scripts\\agent_bus.py read --for claude --emit claude-context --event SessionStart
"""
    parser = argparse.ArgumentParser(
        description="Inter-agent coordination bus",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=root_epilog,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_post = sub.add_parser(
        "post",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=post_epilog,
    )
    p_post.add_argument("--from", dest="from_id", required=True)
    p_post.add_argument("--to", required=True)
    p_post.add_argument("--kind", required=True)
    p_post.add_argument("--subject", required=True)
    p_post.add_argument("--body", required=True)
    p_post.add_argument("--ref", default=None)

    p_read = sub.add_parser(
        "read",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=read_epilog,
    )
    p_read.add_argument("--for", dest="for_id", required=True)
    p_read.add_argument(
        "--emit",
        default="plain",
        choices=("plain", "claude-context", "cursor-stop", "cursor-session"),
    )
    p_read.add_argument(
        "--event",
        default="SessionStart",
        help="hookEventName for --emit claude-context (SessionStart | PostToolUse)",
    )

    args = parser.parse_args(argv)
    try:
        if args.cmd == "post":
            post(
                from_id=args.from_id,
                to=args.to,
                kind=args.kind,
                subject=args.subject,
                body=args.body,
                ref=args.ref,
            )
            return 0
        if args.cmd == "read":
            out = read(for_id=args.for_id, emit=args.emit, event=args.event)
            if out:
                sys.stdout.buffer.write(out.encode("utf-8"))
            return 0
    except Exception as exc:
        print(f"[agent_bus] warn: {exc}", file=sys.stderr)
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
