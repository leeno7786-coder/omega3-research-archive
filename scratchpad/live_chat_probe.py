"""Send operator turns to the live brain over the HUD chat socket and print the
replies (plus the streamed tool/reasoning trace). Same transport the HUD uses:
``ws://127.0.0.1:13310/omega/chat``.

    .venv-brain\\Scripts\\python.exe scratchpad\\live_chat_probe.py "What is my favorite editor?" "What is my dog's name?"

Each turn waits for its ``chat.reply`` (matched on the cycle_id this client
mints) before sending the next. ``--timeout`` seconds per turn (default 180).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid

import websockets


async def _turn(ws, text: str, timeout: float) -> dict:
    cycle_id = str(uuid.uuid4())  # the socket requires a canonical UUID4
    await ws.send(json.dumps({"kind": "chat.user_message", "text": text, "cycle_id": cycle_id}))
    started = time.monotonic()
    trace: list[str] = []
    while True:
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            return {"cycle_id": cycle_id, "timed_out": True, "trace": trace}
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        except asyncio.TimeoutError:
            return {"cycle_id": cycle_id, "timed_out": True, "trace": trace}
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        if str(payload.get("cycle_id") or "") != cycle_id:
            continue
        kind = str(payload.get("kind") or "")
        if kind == "chat.tool_call":
            trace.append(f"tool_call {payload.get('name') or payload.get('tool')}: {json.dumps(payload.get('arguments') or payload.get('args') or {})[:160]}")
        elif kind == "chat.tool_result":
            trace.append(f"tool_result {payload.get('name') or payload.get('tool')}: ok={payload.get('ok')}")
        elif kind == "chat.reasoning_delta":
            trace.append("reasoning …")
        elif kind == "chat.reply":
            return {
                "cycle_id": cycle_id,
                "timed_out": False,
                "elapsed_s": round(time.monotonic() - started, 1),
                "text": payload.get("text") or "",
                "route": payload.get("route"),
                "thinking": payload.get("thinking"),
                "prompt_pack": payload.get("prompt_pack"),
                "trace": trace,
            }


async def _main(turns: list[str], timeout: float) -> int:
    port = os.environ.get("OMEGA_HUD_PORT", "13310")
    url = f"ws://127.0.0.1:{port}/omega/chat"
    async with websockets.connect(url, max_size=None) as ws:
        for text in turns:
            print(f"\n>>> {text}")
            result = await _turn(ws, text, timeout)
            for line in result.get("trace") or []:
                print(f"    [trace] {line}")
            if result.get("timed_out"):
                print(f"<<< TIMED OUT after {timeout:.0f}s (cycle {result['cycle_id']})")
                return 1
            print(f"<<< ({result['elapsed_s']}s, cycle {result['cycle_id']})\n{result['text']}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("turns", nargs="+")
    ap.add_argument("--timeout", type=float, default=180.0)
    ns = ap.parse_args()
    sys.exit(asyncio.run(_main(ns.turns, ns.timeout)))
