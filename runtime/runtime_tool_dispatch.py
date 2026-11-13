"""Dispatch runtime-backed Meta tools via config/runtime.yaml sockets."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from core.paths import OMEGA_ROOT


def _read_image_ref(args: dict[str, Any]) -> Optional[bytes]:
    ref = args.get("image_ref") or args.get("image_path") or args.get("query") or args.get("path")
    if not isinstance(ref, str) or not ref.strip():
        return None
    path = Path(ref.strip())
    if not path.is_absolute():
        path = OMEGA_ROOT / path
    if not path.is_file():
        return None
    return path.read_bytes()


def invoke_runtime_backed_tool(
    tool_name: str,
    args: dict[str, Any],
    *,
    episode_id: str,
    runtime_client: Any = None,
    pipeline_id: int | None = None,
    work_kind: str | None = None,
    cycle_id: str = "",
    job_id: str = "",
) -> Any:
    """Call omega-portable-lite for tools listed in runtime.yaml; else None."""
    from pressure_loop.policy import runtime_tool_names

    if tool_name not in runtime_tool_names():
        return None

    from runtime.socket import invoke_runtime_tool

    op = f"tool_{tool_name}"

    def _emit_dispatch(*, ok: bool, error: str | None = None) -> None:
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "runtime",
                "runtime_tool_dispatch",
                episode_id=episode_id,
                tool=tool_name,
                operation=op,
                ok=ok,
                status="ok" if ok else "fail",
                error=error,
            )
        except Exception:
            pass

    if tool_name == "image_tool":
        result = invoke_runtime_tool(
            tool_name,
            operation=op,
            episode_id=episode_id,
            client=runtime_client,
            prompt=str(args.get("prompt") or args.get("query") or ""),
            width=int(args.get("width") or 1024),
            height=int(args.get("height") or 1024),
            steps=int(args.get("steps") or 4),
            seed=args.get("seed"),
        )
        _emit_dispatch(ok=result is not None)
        return result

    image_bytes = _read_image_ref(args)
    if tool_name == "vision_reasoning_tool":
        if image_bytes is None:
            raise RuntimeError("vision_reasoning_tool requires a readable image_ref")
        result = invoke_runtime_tool(
            tool_name,
            operation=op,
            episode_id=episode_id,
            client=runtime_client,
            image_bytes=image_bytes,
            prompt=str(args.get("question") or args.get("prompt") or "Describe this image."),
            image_format=str(args.get("image_format") or "jpeg"),
            pipeline_id=pipeline_id,
            work_kind=work_kind,
            cycle_id=cycle_id,
            job_id=job_id,
        )
        _emit_dispatch(ok=result is not None)
        return result

    if tool_name == "face_tool":
        if image_bytes is None:
            raise RuntimeError("face_tool requires a readable image_ref")
        result = invoke_runtime_tool(
            tool_name,
            operation=op,
            episode_id=episode_id,
            client=runtime_client,
            image_bytes=image_bytes,
            image_format=str(args.get("image_format") or "jpeg"),
        )
        _emit_dispatch(ok=result is not None)
        return result

    return None
