"""Face identify/enroll via portable-lite ``/face/*`` + brain-side anchors."""

from __future__ import annotations

import logging
import re

log = logging.getLogger("omega.runtime.face.recognition")

_ANCHOR_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


def _canonical_anchor_name(name: str) -> str:
    want = (name or "").strip()
    try:
        from runtime.face.anchor_store import get_anchor_store

        for existing in get_anchor_store().names() or []:
            if existing.casefold() == want.casefold():
                return existing
    except Exception as exc:
        log.debug("canonical_anchor_name lookup failed (%s)", exc)
    return _validate_anchor_name(want)


def _face_signal_present(result: dict) -> bool:
    if not isinstance(result, dict) or result.get("reason") == "no_face_detected":
        return False
    if result.get("bbox") is not None:
        return True
    if int(result.get("face_count") or 0) > 0:
        return True
    emb = result.get("embedding")
    return isinstance(emb, list) and len(emb) >= 8


def _apply_brain_anchor_fallback(result: dict) -> dict:
    if not _face_signal_present(result):
        return result
    emb = result.get("embedding")
    if not isinstance(emb, list) or len(emb) < 8:
        return result
    try:
        from runtime.face.anchor_store import get_anchor_store

        anchor = get_anchor_store().identify(emb)
    except Exception as exc:
        log.debug("anchor_store identify fallback failed: %s", exc)
        return result
    anchor_name = anchor.get("recognized_name")
    if not anchor_name:
        return result
    anchor_conf = float(anchor.get("confidence") or 0.0)
    runtime_name = result.get("recognized_name") or result.get("name")
    runtime_conf = float(result.get("confidence") or 0.0)
    if runtime_name and anchor_conf <= runtime_conf:
        return result
    result["recognized_name"] = anchor_name
    result["name"] = anchor_name
    result["confidence"] = anchor_conf
    if anchor.get("candidates") is not None:
        result["candidates"] = anchor.get("candidates")
    if anchor.get("threshold") is not None:
        result["threshold"] = anchor.get("threshold")
    result["recognition_source"] = "anchor_store"
    return result


def _validate_anchor_name(name: str) -> str:
    if not isinstance(name, str) or not _ANCHOR_NAME_PATTERN.match(name):
        raise ValueError("invalid anchor name")
    return name


def anchor_thumbnail_bytes(name: str) -> bytes | None:
    """Read the latest committed capture; thumbnails never become a second truth store."""
    import base64
    import json
    from core.paths import MEMORY_ROOT

    _validate_anchor_name(name)
    selected = None
    directory = MEMORY_ROOT / "700_vision" / "face_embeddings"
    for path in directory.rglob("*.json"):
        if path.name.endswith(".embedding.json"):
            continue
        record = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(record, dict):
            raise RuntimeError(f"invalid canonical face record: {path}")
        if record.get("type") != "face_memory":
            continue
        face = (record.get("enrichment") or {}).get("face_memory")
        if not isinstance(face, dict):
            raise RuntimeError(f"canonical face payload missing: {path}")
        if face.get("name") != name:
            continue
        key = (str(record.get("timestamp_created") or ""), str(record.get("id") or ""))
        if selected is None or key > selected[0]:
            selected = (key, face)
    if selected is None:
        return None
    try:
        raw = base64.b64decode(str(selected[1]["image_b64"]), validate=True)
        if not raw:
            raise ValueError("empty capture")
        return raw
    except (KeyError, ValueError) as exc:
        raise RuntimeError("canonical face capture is unreadable") from exc


def _identify_impl(image_bytes: bytes) -> dict:
    import time

    t0 = time.perf_counter()
    from runtime.face.recognizer import FaceModelUnavailable, get_recognizer

    try:
        result = get_recognizer().identify(image_bytes)
    except FaceModelUnavailable as exc:
        result = _face_runtime_unavailable_result(reason="face_models_unavailable")
        result["error"] = str(exc)
    if not isinstance(result, dict):
        out = {
            "name": None,
            "recognized_name": None,
            "confidence": 0.0,
            "ok": False,
            "degraded": True,
            "reason": "face_runtime_unavailable",
        }
        try:
            from guardian.terminal_feedback import emit_inference

            emit_inference(
                "runtime",
                "face_identify",
                ok=False,
                status="fail",
                latency_ms=int((time.perf_counter() - t0) * 1000),
                reason="face_runtime_unavailable",
            )
        except Exception:
            pass
        return out
    if result.get("degraded"):
        return result
    name = result.get("recognized_name") or result.get("name")
    result["name"] = name
    result["recognized_name"] = name
    result.setdefault("ok", True)
    result["degraded"] = False
    final = _apply_brain_anchor_fallback(result)
    try:
        from guardian.terminal_feedback import emit_inference

        emit_inference(
            "runtime",
            "face_identify",
            ok=bool(final.get("ok")),
            status="ok" if final.get("ok") else "fail",
            latency_ms=int((time.perf_counter() - t0) * 1000),
            recognized_name=final.get("recognized_name"),
            confidence=float(final.get("confidence") or 0.0),
            recognition_source=final.get("recognition_source", "brain_local_cpu"),
        )
    except Exception:
        pass
    return final


def _face_runtime_unavailable_result(
    *,
    reason: str = "face_runtime_unavailable",
) -> dict:
    return {
        "name": None,
        "recognized_name": None,
        "confidence": 0.0,
        "ok": False,
        "degraded": True,
        "reason": reason,
    }


def identify(image_bytes: bytes) -> dict:
    from runtime.face.lifecycle import face_inference_session

    try:
        with face_inference_session():
            return _identify_impl(image_bytes)
    except RuntimeError:
        out = _face_runtime_unavailable_result(reason="face_models_unavailable")
        try:
            from guardian.terminal_feedback import emit_inference

            emit_inference(
                "runtime",
                "face_identify",
                ok=False,
                status="fail",
                reason="face_models_unavailable",
            )
        except Exception:
            pass
        return out


def enroll(image_bytes: bytes, name: str) -> dict:
    try:
        cname = _canonical_anchor_name(name)
    except ValueError:
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "runtime",
                "face_enroll",
                status="fail",
                ok=False,
                reason="invalid_name",
                name=name,
            )
        except Exception:
            pass
        return {"ok": False, "name": name, "error": "invalid_name"}

    from runtime.face.lifecycle import face_inference_session

    try:
        with face_inference_session():
            ident = _identify_impl(image_bytes)
    except RuntimeError:
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "runtime",
                "face_enroll",
                status="degraded",
                ok=False,
                reason="face_models_unavailable",
                name=cname,
            )
        except Exception:
            pass
        return {
            "ok": False,
            "name": cname,
            "error": "face_models_unavailable",
            "reason": "face_models_unavailable",
        }
    return _enqueue_face_observation(image_bytes, cname, ident)


def _enqueue_face_observation(image_bytes: bytes, cname: str, ident: dict) -> dict:
    if not isinstance(ident, dict) or ident.get("degraded"):
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "runtime",
                "face_enroll",
                status="degraded",
                ok=False,
                reason=(ident or {}).get("reason", "face_runtime_unavailable"),
                name=cname,
            )
        except Exception:
            pass
        return {
            "ok": False,
            "name": cname,
            "error": "face_runtime_unavailable",
            "reason": (ident or {}).get("reason", "face_runtime_unavailable"),
        }
    emb = ident.get("embedding")
    if not (isinstance(emb, list) and len(emb) == 512):
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "runtime",
                "face_enroll",
                status="fail",
                ok=False,
                reason=ident.get("reason", "no_face_detected"),
                name=cname,
            )
        except Exception:
            pass
        return {
            "ok": False,
            "name": cname,
            "error": "no_face_detected",
            "reason": ident.get("reason", "no_face_detected"),
        }

    import base64
    from pressure_loop.operator_vision import queue_face_memory

    result = queue_face_memory(name=cname, embedding=emb,
                               image_b64=base64.b64encode(image_bytes).decode("ascii"),
                               bbox=ident.get("bbox"))
    return {**result, "name": cname, "bbox": ident.get("bbox")}

def known_names() -> list[str]:
    from runtime.face.anchor_store import get_anchor_store

    return get_anchor_store().names()


def forget(name: str) -> bool:
    try:
        _validate_anchor_name(name)
    except ValueError:
        return False
    from runtime.face.anchor_store import get_anchor_store

    forgot = get_anchor_store().forget(name)
    return forgot


def face_status() -> dict:
    from runtime.face.recognizer import get_recognizer

    try:
        status = get_recognizer().status()
    except Exception as exc:
        status = {
            "ok": False,
            "face_ready": False,
            "models_loaded": False,
            "lazy_load": True,
            "runtime": "brain-local-cpu",
            "runtime_reachable": True,
            "reason": str(exc)[:240],
            "models": [],
            "missing": [],
        }
    names = list(status.get("known_names") or [])
    status["anchor_count"] = len(names)
    status["known_names"] = names
    # Legacy HUD status fields remain, but now report the local CPU engine.
    status["asset_root"] = status.get("model_root", "")
    status["model_count"] = len(status.get("models") or [])
    return status
