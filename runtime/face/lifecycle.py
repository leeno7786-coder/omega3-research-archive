"""On-demand brain-side CPU face model residency.

Face weights stay unloaded until a webcam/upload identify path runs, then the
CPU ONNX sessions are released after that inference completes. DME owns the
NPU independently; this module never selects or claims an NPU provider.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Any, Iterator

log = logging.getLogger("omega.runtime.face.lifecycle")

_SESSION_LOCK = threading.RLock()
_SESSION_DEPTH = 0
_MODELS_RESIDENT = False


def face_model_ids() -> tuple[str, ...]:
    """Registry model IDs for the face_tool socket (detector + embedding + …)."""
    from pressure_loop.policy import runtime_tool_socket

    sock = runtime_tool_socket("face_tool")
    seen: list[str] = []
    if sock.slots:
        for model_id in sock.slots.values():
            cleaned = str(model_id or "").strip()
            if cleaned and cleaned not in seen:
                seen.append(cleaned)
    return tuple(seen)


def ensure_face_models_loaded(*, base_url: str | None = None) -> dict[str, Any]:
    """Load the local CPU detector and embedding sessions."""
    del base_url  # retained for callers that used the retired HTTP seam
    from runtime.face.recognizer import get_recognizer

    try:
        out = get_recognizer().load()
    except Exception as exc:
        out = {
            "ok": False,
            "provider": "CPUExecutionProvider",
            "runtime": "brain-local-cpu",
            "models": list(face_model_ids()),
            "reason": str(exc)[:240],
        }
    _emit_lifecycle("face_models_load", ok=bool(out.get("ok")), provider=out.get("provider"))
    if not out.get("ok"):
        log.warning("face model load failed: %s", out)
    return out


def release_face_models(*, base_url: str | None = None) -> dict[str, Any]:
    """Release local CPU face sessions."""
    del base_url  # retained for callers that used the retired HTTP seam
    from runtime.face.recognizer import get_recognizer

    get_recognizer().unload()
    out = {
        "ok": True,
        "provider": "CPUExecutionProvider",
        "runtime": "brain-local-cpu",
        "models": list(face_model_ids()),
    }
    _emit_lifecycle("face_models_unload", ok=True, provider=out["provider"])
    return out


def _emit_lifecycle(event: str, *, ok: bool, **fields: Any) -> None:
    try:
        from guardian.terminal_feedback import emit_data

        emit_data("runtime", event, ok=ok, status="ok" if ok else "fail", **fields)
    except Exception:
        pass


@contextmanager
def face_inference_session() -> Iterator[None]:
    """Reference-counted load bracket for one or more face inferences."""
    global _SESSION_DEPTH, _MODELS_RESIDENT
    with _SESSION_LOCK:
        if _SESSION_DEPTH == 0:
            result = ensure_face_models_loaded()
            if not result.get("ok"):
                raise RuntimeError("face_runtime_unavailable")
            _MODELS_RESIDENT = True
        _SESSION_DEPTH += 1
    try:
        yield
    finally:
        with _SESSION_LOCK:
            _SESSION_DEPTH -= 1
            if _SESSION_DEPTH == 0 and _MODELS_RESIDENT:
                try:
                    release_face_models()
                finally:
                    _MODELS_RESIDENT = False


def reset_face_session_state_for_tests() -> None:
    """Test isolation — clear in-process session counters."""
    global _SESSION_DEPTH, _MODELS_RESIDENT
    with _SESSION_LOCK:
        _SESSION_DEPTH = 0
        _MODELS_RESIDENT = False
