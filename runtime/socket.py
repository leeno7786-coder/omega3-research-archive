"""Invoke runtime by declarative sockets (tools + shared generate — no model IDs)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from runtime.client import RuntimeClient, get_runtime_client


def pipeline_context_for_episode(episode: Any) -> dict[str, Any]:
    """Correlation owned by one concrete episode, never ambient/global state."""
    pipeline_id = getattr(episode, "pipeline_id", None)
    work_kind = str(getattr(episode, "work_kind", "") or "")
    cycle_id = str(getattr(episode, "episode_id", "") or "")
    job_id = ""
    pressure = getattr(episode, "pressure", None) or {}
    for contributor in pressure.get("contributors") or ():
        if not isinstance(contributor, dict):
            continue
        contributor_id = str(contributor.get("id") or "")
        if not contributor_id.startswith("operator_"):
            continue
        detail = {
            **dict(contributor.get("metadata") or {}),
            **dict(contributor.get("detail") or {}),
        }
        cycle_id = str(detail.get("cycle_id") or cycle_id)
        job_id = str(detail.get("job_id") or "")
        break
    if pipeline_id is not None:
        from core.pipeline_state import read_envelope
        envelope = read_envelope(pipeline_id)
        if envelope.active_episode_id == str(getattr(episode, "episode_id", "") or "") and envelope.cycle_id:
            # The exact leased episode is authoritative even for a native
            # machine dispatch; unrelated contributor metadata never donates it.
            cycle_id = envelope.cycle_id
            job_id = envelope.job_id or job_id
    context = {
        "pipeline_id": pipeline_id,
        "work_kind": work_kind,
        "cycle_id": cycle_id,
        "job_id": job_id,
    }
    return {key: value for key, value in context.items() if value not in (None, "")}


@dataclass(frozen=True)
class RuntimeSocket:
    """Mind-side plug: name → runtime HTTP route (models bound in portable-lite)."""

    name: str
    endpoint: str
    capability: str
    method: str
    max_tokens: Optional[int] = None
    slot: Optional[str] = None
    slots: Optional[dict[str, str]] = None


def _capability_enum(capability: str):
    from runtime.runtime_capability_broker import RuntimeCapability

    cap_key = capability.upper()
    cap = getattr(RuntimeCapability, cap_key, None)
    if cap is None:
        return RuntimeCapability(capability)
    return cap


def _emit_runtime_enter(
    operation: str,
    episode_id: str,
    pipeline_id: Optional[int] = None,
    **fields: Any,
) -> None:
    """Announce a runtime call BEFORE it blocks. Never raises, never delays.

    The paired completion event rides a ``finally``, which cannot fire while a
    call is still in flight — so without this, a lane waiting on a model is
    indistinguishable in the journal from a lane doing nothing. Both are
    silence. That silence is what made every stall today a forensic exercise
    (sockets, Send-Q, retransmit counters) instead of a log line.
    """
    try:
        from guardian.terminal_feedback import emit_inference

        emit_inference(
            "runtime",
            operation,
            episode_id=episode_id,
            status="enter",
            ok=None,
            pipeline_id=pipeline_id,
            summary=f"{operation} in flight"
            + (f" [pipeline {pipeline_id}]" if pipeline_id is not None else ""),
            **fields,
        )
    except Exception:
        pass


def _preflight_socket(
    sock: RuntimeSocket,
    *,
    operation: str,
    episode_id: str,
    client: Optional[RuntimeClient] = None,
) -> tuple[bool, Any]:
    from runtime.runtime_capability_broker import preflight_runtime_operation

    rt = client or get_runtime_client()
    decision = preflight_runtime_operation(
        _capability_enum(sock.capability),
        operation=operation,
        cycle_id=episode_id,
        client=rt,
    )
    return bool(decision.proceed), decision


def _invoke_socket(
    sock: RuntimeSocket,
    *,
    operation: str,
    episode_id: str,
    client: Optional[RuntimeClient] = None,
    call: Optional[Callable[..., Any]] = None,
    method: Optional[str] = None,
    **kwargs: Any,
) -> Any:
    """``method`` overrides the socket's bound client method for ONE call.

    The socket names the runtime SURFACE (/generate) and its capability; which
    client method drives that surface is a property of the call, not of the
    binding — the operator-facing reply streams the same endpoint the rest of
    cognition posts to (RISK 5). The preflight above is deliberately still on
    the socket's own capability: a streamed pass is the same pass and does not
    get to skip the gate that decides whether the runtime may be asked at all.
    """
    rt = client or get_runtime_client()
    proceed, decision = _preflight_socket(
        sock,
        operation=operation,
        episode_id=episode_id,
        client=rt,
    )
    if not proceed:
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "runtime",
                "socket_preflight_declined",
                status="degraded",
                ok=False,
                reason=getattr(decision, "reason", None),
                operation=operation,
            )
        except Exception:
            pass
        return None
    if call is not None:
        return call()
    method_name = method or sock.method
    fn = getattr(rt, method_name, None)
    if fn is None or not callable(fn):
        raise RuntimeError(
            f"runtime client has no method {method_name!r} for socket {sock.name!r}"
        )
    return fn(**kwargs)


def resolve_runtime_tool(tool: str) -> RuntimeSocket:
    from pressure_loop.policy import runtime_tool_socket

    return runtime_tool_socket(tool)


def resolve_runtime_generate() -> RuntimeSocket:
    from pressure_loop.policy import runtime_generate_socket

    return runtime_generate_socket()


def preflight_runtime_tool(
    tool: str,
    *,
    operation: str,
    episode_id: str,
    client: Optional[RuntimeClient] = None,
) -> tuple[bool, Any]:
    return _preflight_socket(
        resolve_runtime_tool(tool),
        operation=operation,
        episode_id=episode_id,
        client=client,
    )


def invoke_runtime_tool(
    tool: str,
    *,
    operation: str,
    episode_id: str,
    client: Optional[RuntimeClient] = None,
    **kwargs: Any,
) -> Any:
    sock = resolve_runtime_tool(tool)
    t0 = time.perf_counter()
    ok = False
    error = None
    _emit_runtime_enter(operation, episode_id, kwargs.get("pipeline_id"), tool=tool)
    try:
        if tool == "face_tool":
            # Face inference is brain-owned CPU ONNX, not a DME/NPU workload
            # and no longer depends on the retired runtime HTTP service.
            from runtime.face.recognition import identify

            result = identify(kwargs["image_bytes"])
        elif tool == "vision_reasoning_tool":
            # General scene understanding follows the selected offline/cloud
            # mind seat through the provider-neutral multimodal adapter.
            from runtime.vision import describe_image

            text = describe_image(
                kwargs["image_bytes"],
                prompt=kwargs.get("prompt"),
                image_format=str(kwargs.get("image_format") or "jpeg"),
                cycle_id=str(kwargs.get("cycle_id") or ""),
                episode_id=str(episode_id or ""),
                pipeline_id=kwargs.get("pipeline_id"),
                work_kind=kwargs.get("work_kind"),
                job_id=str(kwargs.get("job_id") or ""),
            )
            result = {"ok": bool(text), "text": text or "", "backend": "mind"}
        else:
            result = _invoke_socket(
                sock,
                operation=operation,
                episode_id=episode_id,
                client=client,
                **kwargs,
            )
        ok = result is not None
        return result
    except Exception as exc:
        error = str(exc)
        raise
    finally:
        try:
            from guardian.terminal_feedback import emit_inference

            emit_inference(
                "runtime",
                operation,
                episode_id=episode_id,
                latency_ms=int((time.perf_counter() - t0) * 1000),
                ok=ok,
                slot=sock.slot,
                status="ok" if ok else "fail",
                error=error,
                tool=tool,
            )
        except Exception:
            pass


def _apply_turn_deadline(operation: str, episode_id: str, kwargs: dict[str, Any]) -> None:
    """Hand this call only what is LEFT of the operator's turn (AGENTS.md §2.1 shape).

    The backend read cap bounds ONE read; a turn chains several (intent escalation, the
    reply, the tool loop, Omega's fold). Without this the caps multiply and a blackholed
    provider buys ten minutes of silence per stage. So the turn's own budget is consulted
    here, at the single seam every cognition text pass goes through:

    * no budget (autonomous work, onboarding, anything nobody is waiting on) → untouched;
    * budget left → ``timeout`` becomes the smaller of what the caller asked for and what
      the turn still has, so no stage can claim the whole ceiling twice;
    * budget gone → ``TurnDeadlineExpired`` BEFORE the broker is even asked. A call that
      cannot finish inside the turn is never started, and the refusal names its stage
      instead of arriving as an indistinguishable ``None``.

    The turn is found by the keys this call actually carries — the operator's ``cycle_id``
    and the episode — falling back to the deadline the cascade bound to this thread, which
    is what covers the stages that ask under a derived id (``…-omega-ep``).
    """
    from pressure_loop import turn_deadline

    left = turn_deadline.check(
        str(kwargs.get("cycle_id") or ""), str(episode_id or ""), stage=str(operation or "")
    )
    if left is None:
        return
    asked = kwargs.get("timeout")
    kwargs["timeout"] = left if asked is None else min(float(asked), left)


def invoke_runtime_generate(
    *,
    operation: str,
    episode_id: str,
    client: Optional[RuntimeClient] = None,
    **kwargs: Any,
) -> Any:
    _apply_turn_deadline(operation, episode_id, kwargs)
    t0 = time.perf_counter()
    ok = False
    tokens = None
    model = None
    result = None
    error = None
    # ENTER, before the call (operator 2026-08-15: "we put emit feedback hooks at
    # every stop of every lane from the hud to the deepsleep").
    #
    # The completion emit below lives in a finally, so it fires only when the
    # call RETURNS or RAISES — a call that does neither emits nothing at all.
    # Every hang this system has had today was invisible for exactly that
    # reason: the last line in the journal was the broker saying "proceed", then
    # silence, and no way to tell a blocked lane from an idle one without
    # reading sockets and guessing. An in-flight generate now says so, with its
    # lane and episode, the moment it starts.
    _emit_runtime_enter(operation, episode_id, kwargs.get("pipeline_id"))
    try:
        result = _invoke_socket(
            resolve_runtime_generate(),
            operation=operation,
            episode_id=episode_id,
            client=client,
            **kwargs,
        )
        # Socket timeout values are best-effort at the provider seam (some adapters stream
        # or ignore them). Recheck the absolute turn clock after the call so an overrun can
        # never be reported as a successful reply just because the provider returned late.
        from pressure_loop import turn_deadline

        turn_deadline.check(
            str(kwargs.get("cycle_id") or ""),
            str(episode_id or ""),
            stage=str(operation or "runtime_generate"),
        )
        ok = result is not None
        if isinstance(result, dict):
            tokens = result.get("tokens") or result.get("usage")
            if tokens is None:
                ti = result.get("tokens_in")
                to = result.get("tokens_out")
                if ti is not None or to is not None:
                    tokens = {"prompt": ti, "completion": to}
            # Runtime reports the model it actually served under "model_id"
            # (ops.resolve("generate")). Reading the wrong key here let a stale
            # socket slot name ("jackrong_qwen") mislabel a gemma_e4b inference.
            model = result.get("model") or result.get("model_id")
        return result
    except Exception as exc:
        error = str(exc)
        raise
    finally:
        try:
            from guardian.terminal_feedback import emit_inference

            emit_inference(
                "runtime",
                operation,
                episode_id=episode_id,
                latency_ms=int((time.perf_counter() - t0) * 1000),
                ok=ok,
                tokens=tokens,
                model=model,
                slot=resolve_runtime_generate().slot,
                status="ok" if ok else "fail",
                error=error,
            )
        except Exception:
            pass
