"""OpenAI-compatible LM Studio adapter for the local mind seat."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Iterator, Optional

import requests

from runtime.brain_cloud import (
    _budget_exhausted,
    _CONNECT_TIMEOUT,
    _chat_body,
    _request_digest,
    _ensure_session,
    _iter_utf8_sse_lines,
    _message_content_parts,
    _reasoning_trace,
    _tokens_from_usage,
)

log = logging.getLogger("omega.runtime.brain_local")


def _context(payload: Dict[str, Any]) -> dict[str, Any]:
    return {
        key: payload[key]
        for key in ("pipeline_id", "work_kind", "cycle_id", "job_id")
        if payload.get(key) not in (None, "")
    }


def generate_completion(
    payload: Dict[str, Any],
    *,
    timeout: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Translate one Omega generate request to LM Studio's chat endpoint."""
    from runtime import brain_cloud_config

    descriptor = brain_cloud_config.local_descriptor()
    context = _context(payload)
    if not descriptor:
        return {
            "ok": False,
            "reason": "mind_local_unavailable",
            "detail": "local mind seat has no configured LM Studio model",
            **context,
        }
    url = f"{str(descriptor['base_url']).rstrip('/')}/chat/completions"
    # Composed once and held; see _request_digest for why the digest cannot be taken earlier.
    wire_body = _chat_body(payload, str(descriptor["model"]), "lmstudio")
    context = {**context, "request_digest": _request_digest(wire_body)}
    try:
        response = _ensure_session().post(
            url,
            json=wire_body,
            headers={},
            timeout=(_CONNECT_TIMEOUT, float(timeout) if timeout else 600.0),
        )
    except requests.RequestException as exc:
        log.warning("local mind transport failure: %s", exc)
        return {"ok": False, "reason": "local_transport", "detail": str(exc)[:300], **context}
    if response.status_code >= 400:
        log.warning("local mind HTTP %s: %s", response.status_code, response.text[:300])
        return {
            "ok": False,
            "reason": f"local_http_{response.status_code}",
            "detail": response.text[:300],
            **context,
        }
    try:
        data = response.json()
    except ValueError:
        return {"ok": False, "reason": "local_non_json", "detail": response.text[:300], **context}
    choices = data.get("choices") or []
    choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice, dict) else {}
    message = message if isinstance(message, dict) else {}
    text, part_reasoning = _message_content_parts(message.get("content"))
    reasoning = _reasoning_trace(message, part_reasoning)
    finish_reason = choice.get("finish_reason")
    tool_calls = message.get("tool_calls")
    if (
        not text.strip()
        and not (isinstance(tool_calls, list) and tool_calls)
        and _budget_exhausted(finish_reason, reasoning)
    ):
        # The cloud path already refuses an answerless 2xx body; the local path reported
        # ok=True with text="" and the caller could not tell "the ceiling cut the thought
        # off" from "the model concluded nothing" (law section 2.1). Scoped deliberately to
        # the truncation shape: an empty completion that terminated normally stays the
        # operator's recoverable case (precedent 2026-07-11).
        return {
            "ok": False,
            "reason": "local_reasoning_budget_exhausted",
            "detail": (
                "the model spent its whole completion budget on a reasoning trace and "
                "emitted no answer; raise the token ceiling for this seat"
            ),
            "reasoning": reasoning,
            "finish_reason": finish_reason,
            "tokens": _tokens_from_usage(data.get("usage")),
            "model": str(descriptor["model"]),
            "backend": "local",
            "latency_ms": 0,
            **context,
        }
    body: Dict[str, Any] = {
        "ok": True,
        "text": text,
        "finish_reason": finish_reason,
        "tokens": _tokens_from_usage(data.get("usage")),
        "model": str(descriptor["model"]),
        "backend": "local",
        "latency_ms": 0,
        **context,
    }
    if reasoning:
        body["reasoning"] = reasoning
    if message.get("tool_calls"):
        body["tool_calls"] = message["tool_calls"]
    return body


def generate_stream(
    payload: Dict[str, Any],
    *,
    timeout: Optional[float] = None,
) -> Iterator[str]:
    """Yield the seat's SSE content deltas until its done frame (prose only)."""
    for event, data in generate_stream_events(payload, timeout=timeout):
        if event == "token":
            yield str(data)


def generate_stream_events(
    payload: Dict[str, Any],
    *,
    timeout: Optional[float] = None,
) -> Iterator[tuple[str, Any]]:
    """Yield the seat's SSE stream as Omega stream events until its done frame.

    ``("token", str)`` for prose, ``("reasoning", str)`` for a think trace,
    ``("tool_call", {index, id, name, arguments})`` for each ``delta.tool_calls`` fragment
    (the client folds them back into whole calls), and one ``("done", {finish_reason})``
    before the stream ends. Forwarding the tool fragments is what lets a tool-carrying
    pass stream: without them a reply that called ``web_search`` streamed as nothing and
    AUTO reported an empty completion (live 2026-09-15).
    """
    from runtime.client import RuntimeStreamError
    from runtime import brain_cloud_config

    descriptor = brain_cloud_config.local_descriptor()
    context = _context(payload)
    if not descriptor:
        raise RuntimeStreamError("mind_local_unavailable", context=context)
    body = _chat_body(payload, str(descriptor["model"]), "lmstudio")
    body["stream"] = True
    url = f"{str(descriptor['base_url']).rstrip('/')}/chat/completions"
    saw_done = False
    finish_reason: Any = None
    try:
        with _ensure_session().post(
            url,
            json=body,
            headers={},
            timeout=(_CONNECT_TIMEOUT, float(timeout) if timeout else 600.0),
            stream=True,
        ) as response:
            if response.status_code != 200:
                raise RuntimeStreamError(
                    f"local_http_{response.status_code}", response.text[:300], context=context
                )
            for raw in _iter_utf8_sse_lines(response):
                if not raw or not raw.startswith("data:"):
                    continue
                data_text = raw[len("data:") :].strip()
                if data_text == "[DONE]":
                    saw_done = True
                    break
                try:
                    chunk = json.loads(data_text)
                except ValueError:
                    continue
                if chunk.get("error"):
                    err = chunk["error"]
                    detail = str(err.get("message") if isinstance(err, dict) else err)
                    raise RuntimeStreamError("local_stream_error", detail[:300], context=context)
                choices = chunk.get("choices") or []
                choice = (choices[0] or {}) if choices else {}
                delta = choice.get("delta") if isinstance(choice, dict) else {}
                delta = delta if isinstance(delta, dict) else {}
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
                token = delta.get("content")
                reasoning = ""
                if isinstance(token, list):
                    token, reasoning = _message_content_parts(token)
                for key in ("reasoning_content", "reasoning"):
                    if isinstance(delta.get(key), str) and delta[key]:
                        reasoning += delta[key]
                if reasoning:
                    yield "reasoning", reasoning
                if token:
                    yield "token", str(token)
                for fragment in delta.get("tool_calls") or ():
                    if not isinstance(fragment, dict):
                        continue
                    function = fragment.get("function") if isinstance(fragment.get("function"), dict) else {}
                    yield "tool_call", {
                        "index": int(fragment.get("index") or 0),
                        "id": str(fragment.get("id") or ""),
                        "name": str(function.get("name") or ""),
                        "arguments": str(function.get("arguments") or ""),
                    }
    except RuntimeStreamError:
        raise
    except (requests.RequestException, ValueError) as exc:
        raise RuntimeStreamError("local_transport", str(exc), context=context) from exc
    if not saw_done:
        raise RuntimeStreamError("local_missing_done", context=context)
    yield "done", {"finish_reason": finish_reason or "stop", "model_id": str(descriptor["model"])}
