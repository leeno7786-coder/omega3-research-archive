"""Provider-neutral multimodal vision for the selected brain seat.

The local seat is LM Studio/OpenAI-compatible and the cloud seat is routed by
``runtime.brain_cloud``. Both receive the same multimodal chat message shape;
the selected backend owns endpoint, model, credentials, and transport.
"""

from __future__ import annotations

import base64
import logging
from typing import Any, Optional

log = logging.getLogger("omega.runtime.vision")

_IMAGE_FORMATS = frozenset({"jpeg", "jpg", "png", "webp", "gif", "bmp"})
_DEFAULT_PROMPT = (
    "Describe this image factually in 2-4 sentences: subjects, setting, "
    "notable objects, and any visible text. Do not speculate about identity."
)


def _safe_image_format(image_format: str) -> str:
    value = str(image_format or "jpeg").strip().lower().lstrip(".")
    if value == "jpg":
        return "jpeg"
    return value if value in _IMAGE_FORMATS else "jpeg"


def _vision_payload(
    image_bytes: bytes,
    *,
    prompt: str,
    image_format: str,
    max_tokens: Optional[int],
    temperature: Optional[float],
) -> dict[str, Any]:
    fmt = _safe_image_format(image_format)
    image_url = f"data:image/{fmt};base64," + base64.b64encode(image_bytes).decode("ascii")
    payload: dict[str, Any] = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt or _DEFAULT_PROMPT},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            }
        ],
        "max_tokens": int(max_tokens if max_tokens is not None else 512),
        "temperature": float(temperature if temperature is not None else 0.2),
    }
    return payload


def _selected_backend() -> tuple[str, Optional[dict[str, Any]]]:
    from runtime import brain_cloud_config

    mode = brain_cloud_config.backend()
    cloud = brain_cloud_config.descriptor()
    if mode == "cloud":
        return "cloud", cloud
    if mode == "local":
        return "local", None
    # auto: use cloud only when its descriptor is complete; otherwise local.
    return ("cloud", cloud) if cloud else ("local", None)


def describe_image(
    image_bytes: bytes,
    *,
    prompt: Optional[str] = None,
    image_format: str = "jpeg",
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    timeout: float = 30.0,
    cycle_id: str = "",
    episode_id: str = "",
    pipeline_id: Optional[int] = None,
    work_kind: Optional[str] = None,
    job_id: str = "",
) -> Optional[str]:
    """Describe an image through the selected offline or cloud mind seat.

    Returns text on a successful multimodal response and ``None`` on a
    provider/configuration/transport failure. The caller owns the failure-soft
    memory and Guardian behavior.
    """
    if not image_bytes:
        return None
    from pressure_loop import turn_deadline

    bounded_timeout = turn_deadline.clamp_timeout(
        timeout, cycle_id, episode_id, stage="vision"
    )
    payload = _vision_payload(
        image_bytes,
        prompt=prompt or _DEFAULT_PROMPT,
        image_format=image_format,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    backend, descriptor = _selected_backend()
    if backend == "cloud":
        if not descriptor:
            log.warning("vision cloud seat selected but no usable cloud descriptor exists")
            return None
    from runtime.client import get_runtime_client

    result = get_runtime_client().generate_completion(
        "",
        messages=payload["messages"],
        max_tokens=payload["max_tokens"],
        temperature=payload["temperature"],
        timeout=bounded_timeout,
        local_only=backend == "local",
        pipeline_id=pipeline_id,
        work_kind=work_kind,
        cycle_id=cycle_id or None,
        job_id=job_id or None,
    )
    # Like the text socket, vision providers can ignore timeout hints. A late result cannot
    # count as a successful operator turn.
    turn_deadline.check(cycle_id, episode_id, stage="vision")
    if not isinstance(result, dict) or not result.get("ok"):
        log.warning(
            "vision description failed backend=%s reason=%s",
            backend,
            (result or {}).get("reason") if isinstance(result, dict) else "no_result",
        )
        return None
    text = str(result.get("text") or "").strip()
    return text or None


def status() -> dict[str, Any]:
    """Return the selected multimodal seat without exposing credentials."""
    try:
        from runtime import brain_cloud_config

        backend, descriptor = _selected_backend()
        if backend == "cloud":
            return {
                "ok": descriptor is not None,
                "backend": "cloud",
                "provider": str((descriptor or {}).get("provider") or ""),
                "model": str((descriptor or {}).get("model") or ""),
                "reason": "configured" if descriptor else "cloud_descriptor_unavailable",
            }
        local = brain_cloud_config.local_descriptor()
        return {
            "ok": local is not None,
            "backend": "local",
            "provider": "lmstudio",
            "model": str((local or {}).get("model") or ""),
            "reason": "configured" if local else "local_descriptor_unavailable",
        }
    except Exception as exc:
        return {"ok": False, "backend": "unknown", "reason": type(exc).__name__}


__all__ = ["describe_image", "status"]
