"""OpenAI-compatible cloud adapter for the backend-neutral mind route.

When ``runtime/brain_cloud_config`` resolves a usable cloud descriptor, ``RuntimeClient``'s three
cognition entry points (generate / generate_completion / generate_stream) route HERE instead
of the local-services process. This module translates the gateway's ``/generate`` payload into
an OpenAI-compatible ``POST {base}/chat/completions`` and translates the response BACK into
the gateway's body shape (``ok`` / ``text`` / ``tool_calls`` / ``finish_reason`` /
``tokens``), so every downstream consumer is untouched.

Deliberate scope:
* system+prompt -> messages; max_tokens/temperature/top_p/stop/tools pass through.
* chat_template_kwargs (enable_thinking) is FORWARDED so per-pass thinking rides through to
  an OpenAI-compatible llama.cpp/vLLM seat: motor planning -> False (clean schema JSON),
  10-window reflection + foldback -> True (deliberate reasoning). top_k stays dropped (a
  llama-native sampler, not a portable OpenAI field).
* tool_calls stay in OpenAI shape (the local gateway is OpenAI-compatible; consumers
  already parse that shape).
* No GPU gating: a cloud generate cannot evict a build, so the mind keeps thinking even
  while the executor holds the local GPU — a side benefit of the two-model split.
* Failure contract matches the local path: transport/HTTP failure -> ``{"ok": False,
  "reason", "detail"}``; callers already degrade-quiet on it.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Dict, Iterator, Optional

import requests
from requests.adapters import HTTPAdapter

log = logging.getLogger("omega.runtime.brain_cloud")

_session: Optional[requests.Session] = None
_session_lock = threading.RLock()

# Generous read budget: frontier endpoints can queue; connect stays short so a dead
# route degrades fast (mirrors runtime.client's connect/read split).
#
# CONNECT stays 5s deliberately — a route that is not there should fail fast, and
# no model size changes how long a TCP handshake ought to take. It is only the
# READ budget that scales with the seat.
_CONNECT_TIMEOUT = 5.0

# The read ceiling is a SAFETY CAP, not a service-level expectation (operator
# 2026-08-15: "we are on frontier time now with giant models, let it breathe").
# 120s was sized for a modest cloud seat; a 1M-context model doing a full
# foldback over a long build exceeds it comfortably, and the failure mode is
# actively misleading — a cut-off read surfaces to the cascade as a FAILED
# generate, indistinguishable from the model refusing, when the model was simply
# still thinking. Better to wait ten minutes than to record a lie.
#
# Still capped rather than unbounded: a lane thread blocked forever holds its
# lane's episode open and nothing behind it ever ignites, which is the deadlock
# shape this system already paid for once.
_DEFAULT_TIMEOUT_S = 600.0
_TIMEOUT_ENV = "OMEGA_BRAIN_READ_TIMEOUT"


def _default_read_timeout() -> float:
    """The read ceiling, overridable per deployment. Read live, not cached, so a
    drop-in edit takes effect on restart without a code change."""
    raw = (os.environ.get(_TIMEOUT_ENV) or "").strip()
    if raw:
        try:
            parsed = float(raw)
            if parsed > 0:
                return parsed
        except ValueError:
            pass
    return _DEFAULT_TIMEOUT_S


# How long a pooled connection may sit unused before we stop trusting it.
#
# Keep-alive sockets to a cloud endpoint die SILENTLY: the NAT mapping expires
# or the edge drops the flow, and nothing tells this side. The socket stays
# ESTABLISHED in the pool and looks perfectly healthy, so the next request picks
# it up and writes into a black hole -- then blocks for the entire read timeout.
#
# Measured 2026-08-15: the appliance ran episodes until 08:07, idled ~7 minutes,
# and the operator's build at 08:14:28 drew two dead sockets from the pool. Both
# lanes stalled with Send-Q frozen (9236 and 11042 bytes, unacked 5 and 8,
# 23 and 16 retransmissions, rto backed off to its 120s maximum) while a FRESH
# connection to the same endpoint served an 11KB body in 1.5s. Two episodes held
# open for the full 600s cap by sockets that had been dead for minutes.
#
# 90s is well under the ~2-5 minute idle timeouts these paths typically use, and
# the cost of being wrong is one TLS handshake (~200ms) on the first request
# after a quiet spell -- paid on a lane that has just been idle anyway.
_IDLE_RECYCLE_S = 90.0
_last_request_at: float = 0.0


def _socket_keepalive_options() -> list:
    """TCP keepalive: make the KERNEL notice a dead peer instead of waiting on us.

    Idle recycling above catches the common case; this catches a peer that dies
    DURING a long generate, which no idle policy can see. ~60s to detection
    (30s idle + 3 probes x 10s) against a 600s read cap. Platform-guarded --
    the constants are Linux/most-BSD, and a missing one must never break boot.
    """
    import socket

    options = [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]
    for name, value in (("TCP_KEEPIDLE", 30), ("TCP_KEEPINTVL", 10), ("TCP_KEEPCNT", 3)):
        opt = getattr(socket, name, None)
        if opt is not None:
            options.append((socket.IPPROTO_TCP, opt, value))
    return options


def _build_session() -> requests.Session:
    from urllib3.util.retry import Retry

    s = requests.Session()
    # Retry what the seat REJECTED; never retry what it may be answering.
    #
    # connect: the TCP/TLS handshake failed, so nothing was sent.
    # status:  429/502/503/504 are the seat telling us it did not process the
    #          request at all. Replaying is free, exactly like a failed connect.
    # read=0:  a read timeout may mean the model IS generating. Re-sending there
    #          duplicates a generate, which is worse than an honest failure.
    #
    # Without the status retries a few seconds of provider trouble destroyed a
    # whole onboarding (2026-08-15): Mistral returned 503 "Service unavailable"
    # and the cascade burned both of its attempts 758ms apart — 12:06:56.812 and
    # 12:06:57.570 — straight back into the same blip, so both the onboarding
    # episode and the LEARN behind it closed failed within five seconds. The seat
    # answered 200 in 0.6s when probed minutes later; nothing was wrong with it
    # by then, and nothing had been wrong with the request.
    #
    # backoff_factor 0.5 spreads three attempts over ~0.5s/1s/2s, which rides out
    # a blip of that length instead of sprinting through it, and
    # respect_retry_after_header honours a 429's own pacing.
    retry = Retry(
        total=None,
        connect=2,
        read=0,
        status=3,
        status_forcelist=(429, 502, 503, 504),
        backoff_factor=0.5,
        respect_retry_after_header=True,
        allowed_methods=None,  # POST included: these statuses mean nothing ran
    )
    adapter = HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=retry)
    try:
        from urllib3.connection import HTTPConnection

        HTTPConnection.default_socket_options = (
            HTTPConnection.default_socket_options + _socket_keepalive_options()
        )
    except Exception:  # noqa: BLE001 -- keepalive is defense, never a boot blocker
        log.debug("brain_cloud: TCP keepalive options unavailable", exc_info=True)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def _ensure_session() -> requests.Session:
    """The shared session, rebuilt when its pooled connections have gone stale."""
    global _session, _last_request_at
    with _session_lock:
        now = time.monotonic()
        idle = now - _last_request_at
        if _session is not None and _last_request_at and idle > _IDLE_RECYCLE_S:
            log.info(
                "brain_cloud: retiring pooled connections after %.0fs idle "
                "(a reused dead socket costs a full read timeout)",
                idle,
            )
            try:
                _session.close()
            except Exception:  # noqa: BLE001
                pass
            _session = None
        if _session is None:
            _session = _build_session()
        _last_request_at = now
        return _session


def enabled() -> bool:
    """True when cognition generates should route to the configured cloud brain."""
    from runtime import brain_cloud_config

    descriptor = brain_cloud_config.descriptor()
    if brain_cloud_config.backend() == "cloud" and descriptor is None:
        raise brain_cloud_config.MindBackendConfigError("cloud_mind_backend_unusable")
    return descriptor is not None


_TEMPLATE_KWARGS_ENV = "OMEGA_BRAIN_TEMPLATE_KWARGS"


def _template_kwargs_allowed() -> bool:
    """True when this cloud seat accepts the llama.cpp chat_template_kwargs extension.

    Default FALSE: a strict OpenAI validator rejects the whole request over an
    unknown body key, which turns a working seat into a total cognition outage.
    Read live so a drop-in can flip it without a code change.
    """
    return (os.environ.get(_TEMPLATE_KWARGS_ENV) or "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _chat_body(payload: Dict[str, Any], model: str, dialect: str = "openai") -> Dict[str, Any]:
    """Gateway /generate payload -> OpenAI-compatible chat.completions body."""
    supplied = payload.get("messages")
    messages = [dict(turn) for turn in supplied] if isinstance(supplied, list) else []
    system = payload.get("system")
    if system and not any(str(turn.get("role") or "") == "system" for turn in messages):
        messages.append({"role": "system", "content": str(system)})
    if not isinstance(supplied, list):
        messages.append({"role": "user", "content": str(payload.get("prompt") or "")})
    body: Dict[str, Any] = {"model": model, "messages": messages}
    if payload.get("max_tokens") is not None:
        body["max_tokens"] = payload["max_tokens"]
    if payload.get("temperature") is not None:
        body["temperature"] = payload["temperature"]
    if payload.get("top_p") is not None:
        body["top_p"] = payload["top_p"]
    if payload.get("stop") is not None:
        body["stop"] = payload["stop"]
    if payload.get("tools") is not None:
        body["tools"] = payload["tools"]
        body["tool_choice"] = payload.get("tool_choice", "auto")
    if payload.get("seed") is not None:
        # Most OpenAI-compatible providers use ``seed``; Mistral keeps its
        # provider-specific ``random_seed`` spelling.
        body["random_seed" if dialect == "mistral" else "seed"] = int(payload["seed"])
    if payload.get("response_format") is not None:
        response_format = payload["response_format"]
        if dialect == "mistral" and isinstance(response_format, dict):
            # Mistral's OpenAI-compatible endpoint accepts JSON mode but rejects
            # Omega's richer schema vocabulary ($schema, oneOf, and nested
            # json_schema metadata). The planner still supplies the closed
            # contract in its prompt and validates the returned object locally.
            body["response_format"] = {"type": "json_object"}
        else:
            body["response_format"] = response_format
    # chat_template_kwargs (carrying enable_thinking) is a llama.cpp/vLLM
    # EXTENSION, not OpenAI. It rides the local seat to make the per-pass
    # thinking distinction — motor sends enable_thinking=False for clean schema
    # JSON, reflection and foldback send True.
    #
    # It is NOT sent to a cloud seat by default. The old comment here assumed "a
    # seat that doesn't understand the field ignores it", and that assumption
    # cost a full test run on 2026-08-15: Mistral's endpoint validates the body
    # strictly and answered every single generate with
    #   HTTP 422 extra_forbidden loc=["body","chat_template_kwargs"]
    # so EVERY cascade failed -- onboarding and the operator's LEARN both died
    # in motor planning, reported as "model output was not parseable JSON" when
    # the truth was that no output existed at all.
    #
    # Same lesson as the unload ladder and the /health probe: do not send a
    # foreign surface something it never agreed to accept. Set
    # OMEGA_BRAIN_TEMPLATE_KWARGS=1 for a cloud seat that genuinely takes it
    # (a self-hosted vLLM behind an OpenAI shim), and the provider's own default
    # applies otherwise.
    ctk = payload.get("chat_template_kwargs")
    if dialect == "lmstudio":
        from runtime.model_templates import resolve_lmstudio_template_profile

        profile = resolve_lmstudio_template_profile(model)
        # The global Omega config can carry stop markers from more than one
        # tokenizer family. Never send those generic markers to LM Studio: its
        # model metadata owns the rendered template, and known profiles add the
        # matching family boundary here.
        structural_tokens = {"<end_of_turn>", "<|im_end|>", "<turn|>", "<|tool_response>"}
        supplied_stops = body.get("stop")
        stops = supplied_stops if isinstance(supplied_stops, list) else []
        family_stops = []
        if profile is not None:
            family_stops.append(profile.end_token)
            if body.get("tools"):
                family_stops.extend(profile.tool_end_tokens)
        body["stop"] = [
            *(
                stop for stop in stops
                if str(stop) not in structural_tokens
                and stop not in family_stops
            ),
            *family_stops,
        ]
        if not body["stop"]:
            body.pop("stop")
        if profile is not None:
            if profile.supports_template_kwargs and isinstance(ctk, dict) and ctk:
                body["chat_template_kwargs"] = dict(ctk)
    elif isinstance(ctk, dict) and ctk and _template_kwargs_allowed():
        body["chat_template_kwargs"] = ctk
    return body


def _tokens_from_usage(usage: Any) -> Dict[str, int]:
    u = usage if isinstance(usage, dict) else {}
    prompt = int(u.get("prompt_tokens") or 0)
    completion = int(u.get("completion_tokens") or 0)
    total = int(u.get("total_tokens") or (prompt + completion))
    return {"prompt": prompt, "completion": completion, "total": total}


def _request_digest(wire_body: Any) -> str:
    """A digest of the request as TRANSMITTED, computed at the transport seam.

    WO-05 step 1 forbids hashing "an earlier unused prompt", and that is a real trap here:
    the caller hands in a payload dict, and ``_chat_body`` restructures it into the wire
    shape (messages array, model id, dialect-specific fields) only at the moment of the
    post. A digest taken before that describes a request nobody ever sent, so any later
    claim that "this is what the model saw" would be unfalsifiable.

    Computed from the exact object handed to the session, so a change between composition
    and transmission changes the digest. Canonical bytes, so key order cannot alter it.
    """
    from schema.continuity_json import content_hash

    # content_hash canonicalises internally; handing it canonical_bytes' output would be
    # hashing bytes, which the contract rejects outright rather than silently stringifying.
    return content_hash(wire_body)


def _reasoning_trace(message: Any, part_reasoning: str = "") -> str:
    """The thinking trace, under whichever OpenAI-dialect spelling the provider used.

    Providers disagree: some emit ``reasoning_content``, some ``reasoning``, and a thinking
    seat emits it as content parts. Reading only one spelling silently discards the trace —
    and then a completion that produced no answer has no evidence left to explain itself
    (EARLY-V01, 2026-09-21: a live endpoint returned 505 characters of ``reasoning`` with
    ``content: null`` and the trace was dropped on the floor).
    """
    if isinstance(message, dict):
        for key in ("reasoning_content", "reasoning"):
            value = message.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return part_reasoning


def _budget_exhausted(finish_reason: Any, reasoning: str) -> bool:
    """Did the model spend its whole completion budget thinking and never answer?

    ``finish_reason == "length"`` with a reasoning trace present and no answer means the
    ceiling cut the thought off mid-sentence. That is NOT the operator's recoverable
    empty-completion case (precedent 2026-07-11, which is an empty answer that terminated
    normally and can be retried think-off): here thinking is what consumed the budget, so a
    think-off retry is the one thing that cannot help. The remedy is a higher ceiling, and
    the cause has to say so.
    """
    return str(finish_reason or "").strip().lower() == "length" and bool(reasoning.strip())


def _message_content_parts(content: Any) -> tuple[str, str]:
    """Return Mistral's final text and thinking trace as separate strings.

    A thinking seat answers with a list of content parts (``text`` / ``thinking``)
    instead of one string; the trace must never leak into the reply text."""
    if isinstance(content, str):
        return content, ""
    if isinstance(content, dict):
        kind = str(content.get("type") or "").strip().lower()
        text = content.get("text")
        if kind in {"text", "output_text", "content"} and isinstance(text, str):
            return text, ""
        return "", ""
    if not isinstance(content, list):
        return "", ""
    answer_parts: list[str] = []
    reasoning_parts: list[str] = []
    for chunk in content:
        if not isinstance(chunk, dict):
            continue
        kind = str(chunk.get("type") or "").strip().lower()
        if kind in {"text", "output_text", "content"}:
            answer_parts.append(str(chunk.get("text") or ""))
            continue
        if kind != "thinking":
            if not kind and isinstance(chunk.get("text"), str):
                answer_parts.append(chunk["text"])
            continue
        thinking = chunk.get("thinking")
        if isinstance(thinking, str):
            reasoning_parts.append(thinking)
        elif isinstance(thinking, list):
            reasoning_parts.extend(
                str(item.get("text") or "")
                for item in thinking
                if isinstance(item, dict) and item.get("type") == "text"
            )
    return "".join(answer_parts), "".join(reasoning_parts)


def _iter_utf8_sse_lines(response: Any) -> Iterator[str]:
    """Yield decoded SSE lines using the UTF-8 encoding required by SSE.

    ``requests`` infers ISO-8859-1 for ``text/event-stream`` when a provider
    omits the charset. That is legal for generic HTTP text but wrong for the
    JSON stream: UTF-8 punctuation such as curly quotes and em dashes becomes
    mojibake (``â...``) before the model response reaches the HUD. Set the
    encoding before ``iter_lines`` performs its conversion and keep a strict
    bytes path for test doubles and unusual adapters.
    """
    response.encoding = "utf-8"
    for raw in response.iter_lines(decode_unicode=True):
        if isinstance(raw, bytes):
            yield raw.decode("utf-8")
        elif isinstance(raw, str):
            yield raw
        else:
            raise ValueError("SSE response line must be text or bytes")


def generate_completion(
    payload: Dict[str, Any],
    *,
    timeout: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Run one gateway-shaped generate against the cloud brain. Returns the gateway body
    shape (ok/text/tool_calls/finish_reason/tokens) or an ok=False body; None only when
    the route is not usable (caller should not have called — treat as failure)."""
    context = {
        key: payload[key]
        for key in ("pipeline_id", "work_kind", "cycle_id", "job_id")
        if payload.get(key) not in (None, "")
    }
    try:
        from runtime import brain_cloud_config

        desc = brain_cloud_config.descriptor()
    except Exception:
        desc = None
    if not desc:
        return None
    url = f"{str(desc['base_url']).rstrip('/')}/chat/completions"
    read_timeout = float(timeout) if timeout else _default_read_timeout()
    t0 = time.perf_counter()
    # Composed ONCE and held, so the digest describes the same object that is posted. Building
    # it twice would let the two drift and make the receipt a claim about a different request.
    wire_body = _chat_body(payload, str(desc["model"]), str(desc.get("dialect") or "openai"))
    request_digest = _request_digest(wire_body)
    context = {**context, "request_digest": request_digest}
    try:
        r = _ensure_session().post(
            url,
            json=wire_body,
            headers=(
                {"Authorization": f"Bearer {desc['api_key']}"}
                if str(desc.get("api_key") or "")
                else {}
            ),
            timeout=(_CONNECT_TIMEOUT, read_timeout),
        )
    except requests.RequestException as exc:
        log.warning("brain_cloud generate transport failure: %s", exc)
        return {"ok": False, "reason": "cloud_transport", "detail": str(exc)[:300], **context}
    latency_ms = int((time.perf_counter() - t0) * 1000)
    if r.status_code >= 400:
        log.warning("brain_cloud generate HTTP %s: %s", r.status_code, r.text[:300])
        return {
            "ok": False,
            "reason": f"cloud_http_{r.status_code}",
            "detail": r.text[:300],
            **context,
        }
    try:
        data = r.json()
    except ValueError:
        return {"ok": False, "reason": "cloud_non_json", "detail": r.text[:300], **context}
    choices = data.get("choices") or []
    choice = choices[0] if isinstance(choices, list) and choices else {}
    msg = choice.get("message") if isinstance(choice, dict) else {}
    msg = msg if isinstance(msg, dict) else {}
    text, part_reasoning = _message_content_parts(msg.get("content"))
    reasoning_trace = _reasoning_trace(msg, part_reasoning)
    tool_calls = msg.get("tool_calls")
    tool_calls = tool_calls if isinstance(tool_calls, list) else []
    finish_reason = choice.get("finish_reason") if isinstance(choice, dict) else None
    tokens = _tokens_from_usage(data.get("usage"))
    model_id = str(desc["model"])
    if not text.strip() and not tool_calls:
        # A 2xx response is transport success, not inference success.  Treat a
        # body with no answer and no tool call as an attributable provider
        # failure so AUTO can run its explicit browser fallback and surface the
        # real cause instead of accepting an empty completion.
        # Two causes, two remedies, so never one reason string: a ceiling that cut the
        # thought off wants a bigger ceiling; a silent provider wants a different provider.
        # Collapsing them sends whoever reads the failure to the wrong place.
        if _budget_exhausted(finish_reason, reasoning_trace):
            reason = "cloud_reasoning_budget_exhausted"
            detail = (
                "the model spent its whole completion budget on a reasoning trace and "
                "emitted no answer; raise the token ceiling for this seat"
            )
        else:
            reason = "cloud_empty_completion"
            detail = "cloud provider returned HTTP success without text or tool calls"
        return {
            "ok": False,
            "reason": reason,
            "detail": detail,
            "reasoning": reasoning_trace,
            "finish_reason": finish_reason,
            "tokens": tokens,
            "model": model_id,
            "backend": "cloud",
            "latency_ms": latency_ms,
            **context,
        }
    body: Dict[str, Any] = {
        "ok": True,
        "text": text,
        "finish_reason": finish_reason,
        "tokens": tokens,
        "model": model_id,
        "backend": "cloud",
        "latency_ms": latency_ms,
        **context,
    }
    if tool_calls:
        body["tool_calls"] = tool_calls
    # Pass the think trace back as `reasoning` (a thinking seat emits it separately from the
    # answer) so the reflection's think-vs-JSON telemetry + HUD see what the model reasoned.
    if reasoning_trace:
        body["reasoning"] = reasoning_trace
    return body


def generate_stream_events(
    payload: Dict[str, Any],
    *,
    timeout: Optional[float] = None,
) -> Iterator[tuple[str, Any]]:
    """The cloud seat's SSE stream as Omega stream events, the contract the local gateway serves.

    ``("token", str)`` for prose, ``("reasoning", str)`` for a think trace,
    ``("tool_call", {index, id, name, arguments})`` for each tool-call fragment, and one
    ``("done", {finish_reason, model_id})`` before the stream ends. Provider, transport and
    missing-completion failures raise RuntimeStreamError with the original request context.

    Live 2026-09-15: this stream yielded content deltas only, so every streamed operator pass on
    a cloud seat lost its tool calls. An agent turn ("Build a basic todo app") on Mistral came
    back as prose or nothing and never built; the blocking path kept the calls.

    Fragments are re-keyed by call id. OpenAI sends a call's id once and continues it under the
    same index; Mistral can send whole parallel calls that share one index, or carry none. A
    fragment with a new id opens a new call instead of being appended to the previous one,
    which would fuse two calls into one unparseable argument string.
    """
    from runtime.client import RuntimeStreamError

    context = {
        key: payload[key]
        for key in ("pipeline_id", "work_kind", "cycle_id", "job_id")
        if payload.get(key) not in (None, "")
    }

    try:
        from runtime import brain_cloud_config

        desc = brain_cloud_config.descriptor()
    except Exception:
        desc = None
    if not desc:
        raise RuntimeStreamError("cloud_descriptor_unavailable", context=context)
    url = f"{str(desc['base_url']).rstrip('/')}/chat/completions"
    body = _chat_body(payload, str(desc["model"]), str(desc.get("dialect") or "openai"))
    body["stream"] = True
    # WO-05 step 1 for STREAMED requests: the digest of the exact object posted below, taken
    # after the last mutation. It rides the done frame and every failure's context.
    request_digest = _request_digest(body)
    context = {**context, "request_digest": request_digest}
    read_timeout = float(timeout) if timeout else _default_read_timeout()
    finish_reason: Any = None
    slot_for_provider_index: Dict[int, int] = {}
    id_for_slot: Dict[int, str] = {}
    try:
        with _ensure_session().post(
            url,
            json=body,
            headers=(
                {"Authorization": f"Bearer {desc['api_key']}"}
                if str(desc.get("api_key") or "")
                else {}
            ),
            timeout=(_CONNECT_TIMEOUT, read_timeout),
            stream=True,
        ) as r:
            if r.status_code != 200:
                log.warning(
                    "brain_cloud stream non-200: status=%s body=%s",
                    r.status_code,
                    r.text[:200],
                )
                raise RuntimeStreamError(f"cloud_http_{r.status_code}", r.text[:200], context=context)
            for raw in _iter_utf8_sse_lines(r):
                if not raw or not raw.startswith("data:"):
                    continue
                data_str = raw[len("data:") :].strip()
                if data_str == "[DONE]":
                    yield "done", {"finish_reason": finish_reason or "stop", "model_id": str(desc["model"]),
                                   "request_digest": request_digest}
                    return
                try:
                    chunk = json.loads(data_str)
                except ValueError:
                    continue
                if chunk.get("error"):
                    err = chunk["error"]
                    detail = str(err.get("message") if isinstance(err, dict) else err)
                    raise RuntimeStreamError(
                        "cloud_stream_error", detail[:300], context=context
                    )
                choices = chunk.get("choices") or []
                choice = (choices[0] or {}) if choices else {}
                choice = choice if isinstance(choice, dict) else {}
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
                delta = choice.get("delta")
                delta = delta if isinstance(delta, dict) else {}
                tok = delta.get("content")
                reasoning = ""
                if isinstance(tok, list):
                    tok, reasoning = _message_content_parts(tok)
                for key in ("reasoning_content", "reasoning"):
                    if isinstance(delta.get(key), str) and delta[key]:
                        reasoning += delta[key]
                if reasoning:
                    yield "reasoning", reasoning
                if tok:
                    yield "token", str(tok)
                for fragment in delta.get("tool_calls") or ():
                    if not isinstance(fragment, dict):
                        continue
                    function = fragment.get("function") if isinstance(fragment.get("function"), dict) else {}
                    try:
                        provider_index = int(fragment["index"])
                    except (KeyError, TypeError, ValueError):
                        provider_index = -1
                    call_id = str(fragment.get("id") or "")
                    slot = slot_for_provider_index.get(provider_index)
                    if slot is None or (call_id and id_for_slot.get(slot) not in ("", call_id)):
                        slot = len(id_for_slot)
                        slot_for_provider_index[provider_index] = slot
                        id_for_slot[slot] = ""
                    if call_id:
                        id_for_slot[slot] = call_id
                    arguments = function.get("arguments")
                    if isinstance(arguments, (dict, list)):
                        arguments = json.dumps(arguments, ensure_ascii=False)
                    yield "tool_call", {
                        "index": slot,
                        "id": call_id,
                        "name": str(function.get("name") or ""),
                        "arguments": "" if arguments is None else str(arguments),
                    }
            raise RuntimeStreamError("cloud_missing_done", "Cloud stream ended without completion", context=context)
    except RuntimeStreamError:
        raise
    except (requests.RequestException, ValueError) as exc:
        log.warning("brain_cloud stream transport failure: %s", exc)
        raise RuntimeStreamError("cloud_transport", str(exc), context=context) from exc
