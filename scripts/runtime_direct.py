"""
runtime_direct.py — Direct CLI endpoint to the Omega portable runtime (:11434).

Bypasses the HUD/substrate entirely. Talks straight to the runtime's OpenAI-compatible
/v1 API for generation, embeddings, model listing, and health checks.

Usage:
    .\\.venv-brain\\Scripts\\python.exe scripts\\runtime_direct.py list-models
    .\\.venv-brain\\Scripts\\python.exe scripts\\runtime_direct.py generate "Hello, world" --model jackrong_qwen
    .\\.venv-brain\\Scripts\\python.exe scripts\\runtime_direct.py health
    .\\.venv-brain\\Scripts\\python.exe scripts\\runtime_direct.py chat --system "You are Omega" --prompt "Hi"

Environment:
    OMEGA_RUNTIME_URL — override base URL (default: http://127.0.0.1:11434)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import urllib.request
import urllib.error

DEFAULT_BASE_URL = (os.environ.get("OMEGA_RUNTIME_URL") or "http://127.0.0.1:11434").rstrip("/")


def _fetch(
    base_url: str, method: str, path: str, payload: dict | None = None, timeout: int = 30
) -> dict:
    url = f"{base_url}{path}"
    data = json.dumps(payload).encode("utf-8") if payload else None
    req = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {"ok": True}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {"ok": False, "error": body or str(e), "status": e.code}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def cmd_health(base_url: str, _args: argparse.Namespace) -> int:
    data = _fetch(base_url, "GET", "/v1/models")
    if "data" in data:
        models = [m["id"] for m in data.get("data", [])]
        print(f"Runtime OK at {base_url}")
        print(f"Models: {', '.join(models)}")
        return 0
    print(f"Runtime UNREACHABLE at {base_url}")
    print(f"  {data.get('error', 'unknown error')}")
    return 1


def cmd_list_models(base_url: str, _args: argparse.Namespace) -> int:
    data = _fetch(base_url, "GET", "/v1/models")
    if "data" not in data:
        print(f"Error: {data.get('error', 'unknown')}")
        return 1
    for m in data["data"]:
        loaded = "loaded" if m.get("loaded") else "not loaded"
        print(f"  {m['id']:<24} {m.get('format', '?'):<8} {loaded}")
    return 0


def cmd_generate(base_url: str, args: argparse.Namespace) -> int:
    payload = {
        "model": args.model,
        "messages": [{"role": "user", "content": args.prompt}],
        "max_tokens": args.max_tokens,
        "temperature": args.temperature,
        "stream": False,
    }
    data = _fetch(base_url, "POST", "/v1/chat/completions", payload, timeout=120)
    choice = data.get("choices", [{}])[0]
    msg = choice.get("message", {})
    content = msg.get("content", "")
    reasoning = msg.get("reasoning_content") or ""
    if reasoning:
        print(f"[reasoning]\n{reasoning}\n[/reasoning]")
    print(content)
    return 0


def cmd_chat(base_url: str, args: argparse.Namespace) -> int:
    messages: list[dict[str, Any]] = []
    if args.system:
        messages.append({"role": "system", "content": args.system})
    messages.append({"role": "user", "content": args.prompt})
    payload = {
        "model": args.model,
        "messages": messages,
        "max_tokens": args.max_tokens,
        "temperature": args.temperature,
        "stream": False,
    }
    data = _fetch(base_url, "POST", "/v1/chat/completions", payload, timeout=120)
    choice = data.get("choices", [{}])[0]
    msg = choice.get("message", {})
    content = msg.get("content", "")
    reasoning = msg.get("reasoning_content") or ""
    if reasoning:
        print(f"[reasoning]\n{reasoning}\n[/reasoning]")
    print(content)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="runtime_direct", description="Direct CLI to Omega runtime :11434"
    )
    parser.add_argument("--url", default=DEFAULT_BASE_URL, help="Runtime base URL")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("health", help="Check runtime health")
    sub.add_parser("list-models", help="List available models")

    p_gen = sub.add_parser("generate", help="Run a single generation")
    p_gen.add_argument("prompt")
    p_gen.add_argument("--model", default="jackrong_qwen")
    p_gen.add_argument("--max-tokens", type=int, default=256)
    p_gen.add_argument("--temperature", type=float, default=0.7)

    p_chat = sub.add_parser("chat", help="Chat with system prompt")
    p_chat.add_argument("--system", "-s", default="", help="System message")
    p_chat.add_argument("--prompt", "-p", required=True)
    p_chat.add_argument("--model", default="jackrong_qwen")
    p_chat.add_argument("--max-tokens", type=int, default=512)
    p_chat.add_argument("--temperature", type=float, default=0.7)

    args = parser.parse_args(argv)
    base_url = args.url.rstrip("/")

    handlers = {
        "health": cmd_health,
        "list-models": cmd_list_models,
        "generate": cmd_generate,
        "chat": cmd_chat,
    }
    return handlers[args.command](base_url, args)


if __name__ == "__main__":
    sys.exit(main())
