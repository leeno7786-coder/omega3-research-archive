"""Vitis lane provider child: DME tile sessions on the VitisAI execution provider.

Runs under ``.venv-vitis`` (CPython 3.12 on ``python-3.12-embed``), started by
``runtime.vitis_lane.VitisWorkerClient`` as a child of the local-services
gateway:

    .venv-vitis\\Scripts\\python.exe -I -m runtime.vitis_worker

``onnxruntime-vitisai`` exists only for 3.12, and a native provider that does
not match the chipset or driver can take down the process that imports it.
Keeping it in this child means such a failure ends one worker with an exit
code and a log, never the gateway.

Protocol: one JSON request per line on stdin; one framed JSON reply per
request on the original stdout, each line prefixed with ``FRAME_PREFIX``.
Native libraries print to stdout too, so at startup the protocol channel is
moved to a private duplicate and fd 1 is pointed at stderr.  The parent reads
only framed lines.

Import policy: stdlib only at module level.  numpy, onnxruntime and the DME
backend are imported by the operations that need them, so a broken provider
is reported as an error reply to ``hello`` instead of a silent exit.
"""

from __future__ import annotations

import base64
import json
import os
import platform
import sys
import traceback
from pathlib import Path
from typing import Any, BinaryIO

FRAME_PREFIX = b"@@omega-vitis@@ "
_TRACEBACK_LIMIT = 4000


def encode_frame(message: dict[str, Any]) -> bytes:
    """Serialize one protocol reply as a single framed line."""
    return FRAME_PREFIX + json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"


def decode_bits(payload: dict[str, Any]) -> Any:
    """Inverse of ``runtime.vitis_lane.encode_bits``: packed rows -> u8 bits."""
    import numpy as np

    rows, k = int(payload["rows"]), int(payload["k"])
    packed = np.frombuffer(base64.b64decode(payload["packed"], validate=True), dtype=np.uint8)
    width = (k + 7) // 8
    if rows < 1 or k < 1 or packed.size != rows * width:
        raise ValueError(
            f"packed query bits do not match rows={rows} k={k}: {packed.size} byte(s)"
        )
    return np.unpackbits(packed.reshape(rows, width), axis=1, count=k, bitorder="little")


class Worker:
    """Operation dispatch.  Every failure becomes an error reply with its cause."""

    def __init__(self) -> None:
        self._session: Any = None

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = request.get("id")
        op = request.get("op")
        try:
            handler = getattr(self, f"_op_{op}", None) if isinstance(op, str) else None
            if handler is None or not str(op).isidentifier():
                raise ValueError(f"unknown Vitis worker operation: {op!r}")
            return {"id": request_id, "ok": True, "result": handler(dict(request.get("params") or {}))}
        except Exception as exc:  # noqa: BLE001 - the reply carries the cause
            return {
                "id": request_id,
                "ok": False,
                "error": {
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "traceback": traceback.format_exc()[-_TRACEBACK_LIMIT:],
                },
            }

    def _op_ping(self, _params: dict[str, Any]) -> dict[str, Any]:
        return {"pong": True}

    def _op_hello(self, _params: dict[str, Any]) -> dict[str, Any]:
        import onnxruntime as ort

        from core.paths import OMEGA_ROOT

        return {
            "python": platform.python_version(),
            "executable": sys.executable,
            "onnxruntime": str(ort.__version__),
            "providers": [str(item) for item in ort.get_available_providers()],
            "omega_root": str(OMEGA_ROOT),
            "pid": os.getpid(),
        }

    def _op_open_tiles(self, params: dict[str, Any]) -> dict[str, Any]:
        from memory.dme import npu_backend

        self.release()
        session = npu_backend.ShardedOverlapSession.from_manifest(
            params["manifest"],
            cache_root=Path(params["cache_root"]),
            verify_npu=bool(params.get("verify_npu", True)),
        )
        self._session = session
        return {
            "tiles": len(session.tile_keys),
            "providers": list(session.providers),
            "engagement": session.engagement_report(),
        }

    def _op_overlap(self, params: dict[str, Any]) -> dict[str, Any]:
        if self._session is None:
            raise RuntimeError("no DME tiles are open in the Vitis worker")
        return {"overlaps": self._session.overlap(decode_bits(params["bits"])).tolist()}

    def _op_close(self, _params: dict[str, Any]) -> dict[str, Any]:
        self.release()
        return {"closed": True}

    def release(self) -> None:
        session = self._session
        self._session = None
        if session is not None:
            session.close()


def serve(requests: BinaryIO, protocol: BinaryIO) -> int:
    """Answer requests until ``close`` or end of input."""
    worker = Worker()
    try:
        for line in requests:
            if not line.strip():
                continue
            try:
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise ValueError("a Vitis worker request must be a JSON object")
            except ValueError as exc:
                reply = {
                    "id": None,
                    "ok": False,
                    "error": {"type": type(exc).__name__, "message": str(exc), "traceback": ""},
                }
                request = {}
            else:
                reply = worker.handle(request)
            protocol.write(encode_frame(reply))
            protocol.flush()
            if request.get("op") == "close":
                break
    finally:
        worker.release()
    return 0


def main() -> int:
    # The protocol owns a private copy of stdout; everything else that writes
    # to fd 1 (VitisAI and XRT banners included) lands in the stderr log.
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
    sys.stdout.flush()
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    return serve(sys.stdin.buffer, protocol)


if __name__ == "__main__":
    sys.exit(main())
