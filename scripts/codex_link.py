"""Live Claude <-> Codex conferring link over Codex's stdio MCP server.

Two modes:

  daemon  Hold one Codex thread open and serve it. Prints every prompt, every
          progress event and every reply to stdout, so the operator's terminal
          IS the live window. Run this in a visible terminal.

              .venv-brain/Scripts/python.exe scripts/codex_link.py daemon

  send    Drop a message in the daemon's inbox and block until the reply lands.
          This is what the other agent calls.

              .venv-brain/Scripts/python.exe scripts/codex_link.py send --text "..."
              .venv-brain/Scripts/python.exe scripts/codex_link.py send --file msg.txt

The daemon keeps the Codex conversation id, so the FIRST message opens a thread
via the `codex` tool and every later one continues it via `codex-reply` -- a real
back-and-forth, not a cold start per message.

Sandbox: read-only, approvals never. This is a CONFERRING channel; Codex can
read the tree to ground itself but cannot modify anything and will never block
on an approval prompt nobody is watching.

State (all under .agents/codex-link/, gitignored):
  inbox/<ts>-<id>.json     pending messages
  outbox/<id>.json         replies
  transcript.md            append-only human-readable log
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_CWD = os.environ.get("CODEX_LINK_CWD") or str(Path(__file__).resolve().parents[1])
MODEL = os.environ.get("CODEX_LINK_MODEL", "gpt-5.6-sol")
EFFORT = os.environ.get("CODEX_LINK_EFFORT", "xhigh")


def resolve_codex_command() -> str:
    """Return the explicitly configured portable Codex executable."""
    value = os.environ.get("CODEX_CMD", "").strip()
    if not value:
        raise RuntimeError("CODEX_CMD must explicitly name the portable Codex executable")
    return value


def _root() -> Path:
    return Path(__file__).resolve().parent.parent / ".agents" / "codex-link"


def _dirs() -> tuple[Path, Path, Path]:
    root = _root()
    inbox, outbox = root / "inbox", root / "outbox"
    inbox.mkdir(parents=True, exist_ok=True)
    outbox.mkdir(parents=True, exist_ok=True)
    return root, inbox, outbox


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log(root: Path, text: str) -> None:
    with open(root / "transcript.md", "a", encoding="utf-8") as fh:
        fh.write(text.rstrip() + "\n")


def _say(root: Path, line: str) -> None:
    """Print to the live window AND persist to the transcript."""
    print(line, flush=True)
    _log(root, line)


# ---------------------------------------------------------------- MCP plumbing


class CodexServer:
    def __init__(self, cwd: str) -> None:
        self.cwd = cwd
        self.proc = subprocess.Popen(
            [resolve_codex_command(), "mcp-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._id = 0
        self.thread_id: str | None = None
        self.stderr: list[str] = []
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _drain_stderr(self) -> None:
        for line in self.proc.stderr:
            self.stderr.append(line)

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _send(self, obj: dict) -> None:
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def initialize(self) -> dict:
        rid = self._next_id()
        self._send(
            {
                "jsonrpc": "2.0",
                "id": rid,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "codex-link", "version": "1"},
                },
            }
        )
        info = self._await(rid, on_event=None, timeout=60)
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return (info or {}).get("serverInfo") or {}

    def _await(self, rid: int, on_event, timeout: int):
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                return None
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "id" not in msg:
                if on_event:
                    on_event(msg)
                continue
            if msg.get("id") == rid:
                if "error" in msg:
                    raise RuntimeError(json.dumps(msg["error"])[:2000])
                return msg.get("result") or {}
            # A message carrying BOTH id and method is a server->CLIENT request
            # (Codex asking us something, e.g. approval to call an MCP tool).
            # Dropping it hangs Codex forever waiting on a reply that never
            # comes -- which is exactly what happened on the first exchange.
            if msg.get("method"):
                self._answer_request(msg, on_event)
                continue
        raise TimeoutError(f"no response to request {rid} within {timeout}s")

    def _answer_request(self, msg: dict, on_event) -> None:
        method = str(msg.get("method") or "")
        if on_event:
            on_event({"method": f"<< request {method}", "params": msg.get("params")})
        # Elicitations are approval prompts. We already constrain the session to
        # a read-only sandbox with no external MCP servers, and no human is
        # watching this socket, so accept rather than deadlock. Anything else we
        # answer with an empty result purely so Codex is never left blocking.
        result = {"action": "accept", "content": {}} if "elicit" in method else {}
        self._send({"jsonrpc": "2.0", "id": msg.get("id"), "result": result})

    def ask(self, prompt: str, on_event, timeout: int) -> str:
        rid = self._next_id()
        if self.thread_id:
            name = "codex-reply"
            targs = {
                "prompt": prompt,
                "threadId": self.thread_id,
                "conversationId": self.thread_id,
            }
        else:
            name = "codex"
            targs = {
                "prompt": prompt,
                "cwd": self.cwd,
                "sandbox": "read-only",
                "approval-policy": "never",
                "model": MODEL,
                # `approval-policy: never` governs SHELL commands only. An MCP
                # tool call raises a separate approval elicitation, and Codex's
                # SessionStart hook pushes it toward codebase-memory-mcp. With
                # no human on this socket, the cheapest correct answer is to
                # give the conferring session no external MCP servers at all --
                # it only needs to read the tree.
                "config": {
                    "model_reasoning_effort": EFFORT,
                    "mcp_servers": {},
                },
                "developer-instructions": (
                    "You are on a read-only conferring link with Claude Code. "
                    "No external MCP servers are attached: ground yourself with "
                    "direct file reads and ripgrep, not codebase-memory-mcp. "
                    "Answer the questions asked; do not attempt to edit files."
                ),
            }
        self._send(
            {
                "jsonrpc": "2.0",
                "id": rid,
                "method": "tools/call",
                "params": {"name": name, "arguments": targs},
            }
        )
        result = self._await(rid, on_event=on_event, timeout=timeout)
        found = _find_thread(result)
        if found:
            self.thread_id = found
        parts = [
            b.get("text", "")
            for b in (result or {}).get("content") or ()
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        return "\n".join(p for p in parts if p).strip() or json.dumps(result)[:4000]

    def close(self) -> None:
        try:
            self.proc.kill()
        except Exception:
            pass


def _find_thread(obj):
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for key in ("conversationId", "threadId", "conversation_id", "thread_id"):
                val = cur.get(key)
                if isinstance(val, str) and val:
                    return val
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return None


# --------------------------------------------------------------------- daemon


def run_daemon(args) -> int:
    root, inbox, outbox = _dirs()
    print("=" * 72, flush=True)
    print(f" CODEX LINK  |  {MODEL} @ {EFFORT}  |  sandbox=read-only", flush=True)
    print(f" cwd        : {args.cwd}", flush=True)
    print(f" inbox      : {inbox}", flush=True)
    print(f" transcript : {root / 'transcript.md'}", flush=True)
    print("=" * 72, flush=True)

    server = CodexServer(args.cwd)
    try:
        info = server.initialize()
    except Exception as exc:
        print(f"!! could not start codex mcp-server: {exc}", flush=True)
        print("".join(server.stderr[-20:]), flush=True)
        return 1
    print(f" connected  : {info.get('name')} {info.get('version')}\n", flush=True)
    _log(root, f"\n\n## link opened {_now()} ({info.get('name')} {info.get('version')})")

    def on_event(msg: dict) -> None:
        method = str(msg.get("method") or "")
        params = msg.get("params") or {}
        blob = json.dumps(params, ensure_ascii=False)
        if len(blob) > 400:
            blob = blob[:400] + "…"
        print(f"   · {method} {blob}", flush=True)

    print("waiting for messages…  (Ctrl-C to stop)\n", flush=True)
    try:
        while True:
            pending = sorted(inbox.glob("*.json"))
            if not pending:
                time.sleep(1.0)
                continue
            for path in pending:
                try:
                    msg = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    path.unlink(missing_ok=True)
                    continue
                mid = msg.get("id") or uuid.uuid4().hex[:8]
                sender = msg.get("from") or "claude"
                prompt = msg.get("text") or ""
                path.unlink(missing_ok=True)

                _say(root, f"\n{'-' * 72}")
                _say(root, f"### {sender} -> codex  [{mid}]  {_now()}")
                _say(root, prompt)
                _say(root, f"{'-' * 72}")
                print("   (codex working…)", flush=True)

                started = time.time()
                try:
                    reply = server.ask(prompt, on_event=on_event, timeout=args.timeout)
                    err = None
                except Exception as exc:
                    reply, err = "", str(exc)
                took = time.time() - started

                if err:
                    _say(root, f"\n### codex ERROR  [{mid}]  ({took:.0f}s)\n{err}")
                else:
                    _say(root, f"\n### codex -> {sender}  [{mid}]  ({took:.0f}s)")
                    _say(root, reply)

                (outbox / f"{mid}.json").write_text(
                    json.dumps(
                        {
                            "id": mid,
                            "ts": _now(),
                            "reply": reply,
                            "error": err,
                            "seconds": round(took, 1),
                            "thread": server.thread_id,
                        },
                        ensure_ascii=False,
                        indent=1,
                    ),
                    encoding="utf-8",
                )
                print(f"\n   reply written -> outbox/{mid}.json", flush=True)
                print("\nwaiting for messages…\n", flush=True)
    except KeyboardInterrupt:
        print("\nlink closed.", flush=True)
        _log(root, f"\n## link closed {_now()}")
    finally:
        server.close()
    return 0


# ----------------------------------------------------------------------- send


def run_send(args) -> int:
    _root_dir, inbox, outbox = _dirs()
    text = args.text
    if args.file:
        text = Path(args.file).read_text(encoding="utf-8")
    if not text or not text.strip():
        print("refusing to send an empty message", file=sys.stderr)
        return 2

    mid = uuid.uuid4().hex[:8]
    payload = {"id": mid, "from": args.sender, "ts": _now(), "text": text}
    # Write to a temp name then rename, so the daemon never reads a half-file.
    tmp = inbox / f".{mid}.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.rename(inbox / f"{payload['ts'].replace(':', '')}-{mid}.json")

    target = outbox / f"{mid}.json"
    deadline = time.time() + args.wait
    while time.time() < deadline:
        if target.exists():
            data = json.loads(target.read_text(encoding="utf-8"))
            if data.get("error"):
                print(f"CODEX ERROR after {data.get('seconds')}s:\n{data['error']}")
                return 1
            print(data.get("reply") or "")
            print(f"\n[{data.get('seconds')}s · thread={data.get('thread')}]")
            return 0
        time.sleep(1.0)
    print(f"no reply within {args.wait}s (daemon running?)", file=sys.stderr)
    return 1


def main(argv=None) -> int:
    # Codex writes real prose: arrows, dashes, quotes. Windows consoles default
    # to cp1252, which raises UnicodeEncodeError mid-print and loses a reply
    # that was already safely on disk in outbox/.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="mode", required=True)

    d = sub.add_parser("daemon", help="serve the link in a visible terminal")
    d.add_argument("--cwd", default=DEFAULT_CWD)
    d.add_argument("--timeout", type=int, default=1800)
    d.set_defaults(func=run_daemon)

    s = sub.add_parser("send", help="send a message and wait for the reply")
    s.add_argument("--text", default=None)
    s.add_argument("--file", default=None)
    s.add_argument("--sender", default="claude")
    s.add_argument("--wait", type=int, default=1800)
    s.set_defaults(func=run_send)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
