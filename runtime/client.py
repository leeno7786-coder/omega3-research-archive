"""
Cognition → Omega Local Services HTTP Client
=====================================

Thin wrapper around ``requests`` for the Omega local-services HTTP API.

The mind binds **actions** to **endpoints** (``config/runtime.yaml``); the
runtime process maps endpoints to models (hot-swappable).

Failure contract (universal, per spec §4.4):
    - Connection / timeout error  → return None (or default coord for classify)
    - Runtime returned ok=false   → return None (or default coord)
    - Runtime returned 5xx        → return None
    - Brain code MUST check for None and use degraded path
      (default coord [5,0], walker arbitration, silence, etc.)

Singleton via ``get_runtime_client()``.

Configurable base URL via:
    1. Constructor arg
    2. ``OMEGA_RUNTIME_URL`` env var
    3. ``config/runtime.yaml`` ``runtime.endpoint``
    4. Default: http://127.0.0.1:11434
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Iterator, Optional

import requests
from requests.adapters import HTTPAdapter

log = logging.getLogger("omega.runtime.client")


DEFAULT_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_FALLBACK_COORD: list[int] = [
    5,
    0,
]  # skills/tools lobe — see project_brain_ml_refactor_inventory.md


# ────────────────────────────────────────────────────────────────────────
# Per-op timeout defaults (seconds). Tuned to spec §4 latency expectations.
# ────────────────────────────────────────────────────────────────────────
# HUD polls /health via broker + voice probes several times per snapshot;
# cache the body briefly so a down runtime costs one timeout per TTL window.
_HEALTH_CACHE_TTL_SECONDS = 1.5
MAX_CONCURRENT_PREDICTIONS = 4


def _synthesise_timeout_seconds() -> int:
    """Cold TTS init in the Qwen sidecar can exceed 60s."""
    try:
        from config.loader import load_subsystem_bundle

        block = (load_subsystem_bundle("tts").get("config") or {}).get("tts") or {}
        raw = block.get("synthesise_timeout_seconds", 180)
        return max(30, int(raw))
    except Exception:
        return 180


def _configured_socket_path(name: str, *, service: bool) -> str | None:
    """Resolve a runtime path from the governed socket catalog."""
    try:
        from pressure_loop.policy import runtime_service_socket, runtime_tool_socket

        socket = (runtime_service_socket if service else runtime_tool_socket)(name)
        return socket.endpoint
    except (KeyError, OSError, TypeError, ValueError) as exc:
        log.error("runtime socket configuration unavailable name=%s: %s", name, exc)
        return None


TIMEOUTS = {
    "health": 2,
    "telemetry": 3,
    "classify": 30,
    "embed": 30,
    "generate": 60,
    "transcribe": 120,
    "synthesise": _synthesise_timeout_seconds(),
    "face": 10,
    "dme": 30,
    # Flux.2 @1024 on the iGPU is a staged flow (gemma prompt-upscale + hotswap +
    # te=cpu encoder + --max-vram segmented diffusion) ≈ 200-300s; match the runtime's
    # own OMEGA_SD_TIMEOUT (600s) so the brain client doesn't abort mid-render (2026-07-01).
    "image_gen": 600,
    "vision": 30,
    "document": 60,
}


def _gpu_wait_seconds() -> float:
    """Retained timeout input for the inert historical GPU-lease preflight seam."""
    try:
        return max(0.0, float(os.environ.get("OMEGA_GPU_WAIT_S", "120")))
    except (TypeError, ValueError):
        return 120.0


def _generate_retries() -> int:
    """Bounded local-runtime readiness retries; the current Mistral route is one-shot."""
    try:
        return max(0, int(os.environ.get("OMEGA_GENERATE_RETRIES", "2")))
    except (TypeError, ValueError):
        return 2


def _generate_retry_backoff_s() -> float:
    try:
        return max(0.0, float(os.environ.get("OMEGA_GENERATE_RETRY_BACKOFF_S", "1.0")))
    except (TypeError, ValueError):
        return 1.0


@dataclass(frozen=True)
class ClassifyResult:
    """Hierarchical classify response. Matches spec §4.2 /classify shape."""

    ok: bool
    coord: list[int]  # always present (default [5,0] on failure)
    label: Optional[str] = None
    confidence: Optional[float] = None
    tokens: Optional[list[str]] = None
    stages: Optional[dict] = None
    latency_ms: Optional[int] = None
    reason: Optional[str] = None


class RuntimeStreamError(Exception):
    """Raised by `generate_stream` when the server emits an SSE
    `event: error` frame mid-stream. Previously these frames were
    silently discarded — the generator looked like it had ended
    cleanly with zero tokens, identical to a successful empty
    completion. Callers can now distinguish hard runtime failures
    (this exception) from genuinely empty model output (no exception,
    empty iterator)."""

    def __init__(
        self,
        reason: str,
        detail: str = "",
        *,
        context: Optional[dict[str, Any]] = None,
    ) -> None:
        message = f"{reason}: {detail}" if detail else reason
        super().__init__(message)
        self.reason = reason
        self.detail = detail
        self.context = dict(context or {})


_PIPELINE_CONTEXT_KEYS = ("pipeline_id", "work_kind", "cycle_id", "job_id")
_WORK_KINDS = frozenset(
    {"operator_turn", "operator_lane_work", "agent_continuation", "autonomous", "reflection"}
)


def _pipeline_context(
    *,
    pipeline_id: Optional[int],
    work_kind: Optional[str],
    cycle_id: Optional[str],
    job_id: Optional[str],
) -> dict[str, Any]:
    if pipeline_id is not None and (type(pipeline_id) is not int or pipeline_id not in range(4)):
        raise ValueError("pipeline_id must be one of the physical integers 0, 1, 2, or 3")
    if work_kind is not None and work_kind not in _WORK_KINDS:
        raise ValueError(f"unknown work_kind {work_kind!r}")
    context = {
        "pipeline_id": pipeline_id,
        "work_kind": work_kind,
        "cycle_id": cycle_id,
        "job_id": job_id,
    }
    return {key: value for key, value in context.items() if value not in (None, "")}


def _response_context(body: dict[str, Any]) -> dict[str, Any]:
    return {key: body[key] for key in _PIPELINE_CONTEXT_KEYS if body.get(key) not in (None, "")}


class RuntimeClient:
    """Thin HTTP client. Singleton via module-level `runtime` instance.

    Thread-safe — uses a single `requests.Session` for connection pooling.
    All ops are synchronous; streaming uses generators.
    """

    def __init__(self, base_url: Optional[str] = None) -> None:
        self._base_url = (base_url or resolve_runtime_base_url()).rstrip("/")
        self._session: Optional[requests.Session] = None
        self._lock = threading.RLock()
        self._prediction_slots = threading.BoundedSemaphore(MAX_CONCURRENT_PREDICTIONS)
        self._lane_prediction_locks = {pipeline_id: threading.Lock() for pipeline_id in range(4)}
        self._health_cache_body: Optional[dict] = None
        self._health_cache_mono: float = 0.0

    @contextmanager
    def _prediction_admission(self, pipeline_id: Optional[int]):
        """Admit at most four model calls and serialize each physical semantic lane."""
        lane_lock = None
        if pipeline_id is not None:
            if type(pipeline_id) is not int or pipeline_id not in self._lane_prediction_locks:
                raise ValueError("invalid_pipeline_id_for_prediction_admission")
            lane_lock = self._lane_prediction_locks[pipeline_id]
            lane_lock.acquire()
        try:
            self._prediction_slots.acquire()
            try:
                yield
            finally:
                self._prediction_slots.release()
        finally:
            if lane_lock is not None:
                lane_lock.release()

    # ──────────────────────────────────────────────────────────────────
    # Session management
    # ──────────────────────────────────────────────────────────────────
    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def plugin_lifecycle_service(self):
        from mcp_platform.plugin_lifecycle import get_default_plugin_lifecycle_service

        return get_default_plugin_lifecycle_service()

    @property
    def plugin_runtime(self):
        return self.plugin_lifecycle_service.plugin_runtime

    @property
    def permission_store(self):
        return self.plugin_lifecycle_service.permission_store

    def plugin_execution_mutation_scope(self):
        return self.plugin_lifecycle_service.plugin_execution_mutation_scope()

    def plugin_tool_bindings(self):
        return self.plugin_lifecycle_service.runtime_catalog().tools

    def resolve_plugin_authority(self, plan, binding, step):
        return self.plugin_lifecycle_service.resolve_plugin_authority(
            plan,
            binding,
            step,
        )

    def _ensure_session(self) -> requests.Session:
        with self._lock:
            if self._session is None:
                s = requests.Session()
                # Modest connection pool for concurrent ops.
                adapter = HTTPAdapter(pool_connections=8, pool_maxsize=16)
                s.mount("http://", adapter)
                s.mount("https://", adapter)
                self._session = s
            return self._session

    def close(self) -> None:
        with self._lock:
            if self._session is not None:
                self._session.close()
                self._session = None

    # ──────────────────────────────────────────────────────────────────
    # Internal request helper
    # ──────────────────────────────────────────────────────────────────
    def _post(
        self,
        path: str,
        json_body: dict,
        *,
        op: str,
        timeout: Optional[float] = None,
        retries: int = 0,
        retry_statuses: tuple[int, ...] = (503,),
    ) -> Optional[dict]:
        """POST JSON, return parsed dict or None on any failure.

        ``retries`` (default 0 = one-shot behavior) bounds retry-with-backoff for
        local transport errors and ``503`` not-ready responses. An HONEST failure
        (4xx, other 5xx, ``ok=false`` body) is never retried. The current remote
        Mistral generation path does not call this helper."""
        url = f"{self._base_url}{path}"
        timeout = timeout if timeout is not None else TIMEOUTS.get(op, 10)
        # Cat-2 fail-soft: a runtime generate is a WORLD/reality-observation
        # inference, not a continuity structure — it must degrade-not-hang. Bound
        # the *connect* phase to a short ceiling (so "no runtime listening" /
        # a connection-level stall fails fast → ConnectionError → None below)
        # while keeping the full ``timeout`` budget for the read phase of a
        # genuinely-busy runtime. requests accepts a (connect, read) tuple.
        connect_timeout = min(5.0, float(timeout))
        attempt = 0
        while True:
            try:
                r = self._ensure_session().post(
                    url, json=json_body, timeout=(connect_timeout, timeout)
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt < retries:
                    time.sleep(_generate_retry_backoff_s() * (attempt + 1))
                    attempt += 1
                    continue
                log.warning("%s connection/timeout: %s", op, exc)
                return None
            except requests.RequestException as exc:
                log.warning("%s request error: %s", op, exc)
                return None
            if r.status_code in retry_statuses and attempt < retries:
                time.sleep(_generate_retry_backoff_s() * (attempt + 1))
                attempt += 1
                continue
            if r.status_code >= 500:
                log.warning("%s 5xx: status=%s body=%s", op, r.status_code, r.text[:200])
                return None
            if r.status_code >= 400:
                log.warning("%s %s client_error body=%s", op, r.status_code, r.text[:200])
            try:
                return r.json()
            except ValueError:
                log.warning("%s non-JSON response: %s", op, r.text[:200])
                return None

    def _gpu_clear_to_generate(self, op: str) -> bool:
        """Run the historical GPU preflight, now inert for shared physical pipelines."""
        from runtime import gpu_lease

        if gpu_lease.wait_for_gpu(_gpu_wait_seconds()):
            return True
        log.info(
            "%s deferred: GPU held by build (%s) — degrading quiet this tick",
            op,
            gpu_lease.current_holder(),
        )
        return False

    def _get(self, path: str, *, op: str, timeout: Optional[float] = None) -> Optional[dict]:
        url = f"{self._base_url}{path}"
        timeout = timeout if timeout is not None else TIMEOUTS.get(op, 10)
        try:
            r = self._ensure_session().get(url, timeout=timeout)
            if r.status_code >= 500:
                return None
            return r.json()
        except (requests.RequestException, ValueError) as exc:
            log.debug("%s GET failed: %s", op, exc)
            return None

    # ──────────────────────────────────────────────────────────────────
    # Health / introspection
    # ──────────────────────────────────────────────────────────────────
    def health(self) -> bool:
        body = self.health_body()
        return bool(body and body.get("status") == "ok")

    def health_body(self) -> Optional[dict]:
        """Full ``GET /health`` JSON (includes face contract when runtime supports it)."""
        now = time.monotonic()
        with self._lock:
            if (
                self._health_cache_mono
                and (now - self._health_cache_mono) < _HEALTH_CACHE_TTL_SECONDS
            ):
                return self._health_cache_body
        body = self._get("/health", op="health")
        with self._lock:
            self._health_cache_body = body
            self._health_cache_mono = time.monotonic()
        return body

    def face_recognition_threshold(self) -> Optional[float]:
        """Runtime-published cosine-match cutoff for ``/face/identify``."""
        body = self.health_body()
        if not body:
            return None
        face = body.get("face")
        if not isinstance(face, dict):
            return None
        raw = face.get("recognition_threshold")
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    def wait_until_ready(self, timeout_s: float = 30.0, poll_interval_s: float = 0.5) -> bool:
        """Block until /health returns ok, or timeout. Returns True if ready."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.health():
                return True
            time.sleep(poll_interval_s)
        return False

    def telemetry(self) -> Optional[dict]:
        return self._get("/telemetry", op="telemetry")

    def detect(self) -> Optional[dict]:
        return self._get("/detect", op="health")

    def list_models(self) -> Optional[dict]:
        """OpenAI-style model list — ``/v1/models`` returns {"object":"list","data":[...]}.
        (GET /models is a filtered big-model dict with no data[]; don't use it here.)"""
        return self._get("/v1/models", op="health")

    # ──────────────────────────────────────────────────────────────────
    # Cognitive ops — match spec §4.2
    # ──────────────────────────────────────────────────────────────────
    def classify_hierarchical(
        self, text: str, *, force_lobe: Optional[int] = None
    ) -> ClassifyResult:
        """Returns ClassifyResult — `coord` is ALWAYS populated (defaults
        to [5,0] if runtime is down or returned ok=false).

        force_lobe: optional int 0-11. When set, runtime skips stage-1 and
        only runs the named lobe's stage-2 specialist. Used by gates that
        own a fixed lobe but still need a trained subdomain prediction.
        """
        t0 = time.perf_counter()
        body = self._post("/classify", {"text": text, "force_lobe": force_lobe}, op="classify")
        latency_ms = int((time.perf_counter() - t0) * 1000)
        if body is None:
            log.warning(
                "classify_hierarchical: runtime unreachable — degraded "
                "to fallback coord %s (force_lobe=%s)",
                DEFAULT_FALLBACK_COORD,
                force_lobe,
            )
            result = ClassifyResult(
                ok=False, coord=list(DEFAULT_FALLBACK_COORD), reason="runtime_unreachable"
            )
            self._emit_classify_inference(result, latency_ms=latency_ms)
            return result
        if not body.get("ok", False):
            log.warning(
                "classify_hierarchical ok=false reason=%s — degraded to coord %s (force_lobe=%s)",
                body.get("reason"),
                body.get("coord") or DEFAULT_FALLBACK_COORD,
                force_lobe,
            )
            result = ClassifyResult(
                ok=False,
                coord=list(body.get("coord") or DEFAULT_FALLBACK_COORD),
                reason=body.get("reason"),
            )
            self._emit_classify_inference(result, latency_ms=latency_ms, tokens=body.get("tokens"))
            return result
        result = ClassifyResult(
            ok=True,
            coord=list(body.get("coord") or DEFAULT_FALLBACK_COORD),
            label=body.get("label"),
            confidence=body.get("confidence"),
            tokens=body.get("tokens"),
            stages=body.get("stages"),
            latency_ms=body.get("latency_ms") or latency_ms,
        )
        self._emit_classify_inference(result, latency_ms=result.latency_ms, tokens=result.tokens)
        return result

    def _emit_classify_inference(
        self,
        result: ClassifyResult,
        *,
        latency_ms: Optional[int] = None,
        tokens: Any = None,
    ) -> None:
        try:
            from guardian.terminal_feedback import emit_inference

            emit_inference(
                "memory",
                "classify_hierarchical",
                latency_ms=latency_ms,
                ok=bool(result.ok),
                tokens=tokens,
                status="ok" if result.ok else "fail",
                error=result.reason,
            )
        except Exception:
            pass

    def generate(
        self,
        prompt: str,
        *,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        top_k: Optional[int] = None,
        stop: Optional[list[str]] = None,
        system: Optional[str] = None,
        tools: Optional[list[dict]] = None,
        chat_template_kwargs: Optional[dict] = None,
        enable_thinking: bool = False,
        response_format: Optional[dict] = None,
        timeout: Optional[float] = None,
        context_size: Optional[int] = None,
        pipeline_id: Optional[int] = None,
        work_kind: Optional[str] = None,
        cycle_id: Optional[str] = None,
        job_id: Optional[str] = None,
    ) -> Optional[str]:
        # Sampler params are intentionally omitted by default — the runtime
        # (OmegaRuntimeStudio) owns the canonical defaults via its registry.
        # See memory `runtime_owns_inference_params`. Callers may still pass
        # explicit values when a phase needs deterministic / non-default
        # behavior (document the override inline at the call site).
        #
        # P3-S32 (2026-05-27) — operator directive: "turn its reasoning off".
        # Qwen 3 / 3.5 chat templates enable a `<think>...</think>` reasoning
        # block by default. For the substrate's cognitive paths (tool_call
        # dispatch, JSON synthesis, structured eval) the reasoning block
        # burns the 60s read timeout without producing output — the
        # operator saw this as `runtime returned empty response` for omega
        # tool synthesis in the first live boot trace. Defaulting
        # `enable_thinking=False` makes structured output the norm; callers
        # that genuinely benefit from reasoning (chain-of-thought eval,
        # exploratory reflection) opt back in via `enable_thinking=True`.
        # Merged into chat_template_kwargs so an explicit caller-provided
        # value still wins.
        merged_template_kwargs = dict(chat_template_kwargs or {})
        if "enable_thinking" not in merged_template_kwargs:
            merged_template_kwargs["enable_thinking"] = bool(enable_thinking)

        payload: dict = {"prompt": prompt}
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if temperature is not None:
            payload["temperature"] = temperature
        if top_p is not None:
            payload["top_p"] = top_p
        if top_k is not None:
            payload["top_k"] = top_k
        if stop is not None:
            payload["stop"] = stop
        if system is not None:
            payload["system"] = system
        if tools is not None:
            payload["tools"] = tools
        if response_format is not None:
            payload["response_format"] = response_format
        if context_size is not None:
            payload["num_ctx"] = int(context_size)
            if isinstance(payload.get("options"), dict):
                opts = dict(payload["options"])
                opts["num_ctx"] = int(context_size)
                payload["options"] = opts
            else:
                payload["options"] = {"num_ctx": int(context_size)}
        payload["chat_template_kwargs"] = merged_template_kwargs
        payload.update(
            _pipeline_context(
                pipeline_id=pipeline_id,
                work_kind=work_kind,
                cycle_id=cycle_id,
                job_id=job_id,
            )
        )
        with self._prediction_admission(pipeline_id):
            body = self._cloud_or_local_generate(payload, op="generate", timeout=timeout)
        if body is None:
            return None
        if not body.get("ok"):
            log.warning(
                "generate ok=false reason=%s detail=%s",
                body.get("reason"),
                str(body.get("detail") or "")[:200],
            )
            return None
        return body.get("text")

    def _cloud_or_local_generate(
        self,
        payload: dict,
        *,
        op: str,
        timeout: Optional[float] = None,
        local_only: bool = False,
    ) -> Optional[dict]:
        """One generate against the configured mind seat.

        Cloud uses the selected OpenAI-compatible provider. Local uses the
        local Omega service, which proxies the operator's LM Studio endpoint.
        Sensory services remain on that same local service regardless of which
        mind seat answers."""
        from runtime import brain_cloud, brain_cloud_config

        selected_backend = brain_cloud_config.backend()
        cloud_ready = brain_cloud.enabled()
        if selected_backend == "cloud" and not cloud_ready and not local_only:
            return {
                "ok": False,
                "reason": "mind_cloud_unavailable",
                "detail": "cloud mind backend selected without a complete provider descriptor",
            }
        if selected_backend != "local" and cloud_ready and not local_only:
            body = brain_cloud.generate_completion(payload, timeout=timeout)
            ok = bool(body and body.get("ok"))
            try:
                from guardian.terminal_feedback import emit_inference

                emit_inference(
                    "runtime",
                    op,
                    latency_ms=int((body or {}).get("latency_ms") or 0),
                    ok=ok,
                    status="ok" if ok else "fail",
                    backend="cloud",
                    model=str((body or {}).get("model") or ""),
                    error=(str((body or {}).get("reason") or "")[:120] or None)
                    if not ok
                    else None,
                )
            except Exception:
                pass
            return body
        if not self._gpu_clear_to_generate(op):
            return None
        t0 = time.perf_counter()
        body = self._post(
            "/generate", payload, op="generate", timeout=timeout, retries=_generate_retries()
        )
        latency_ms = int((time.perf_counter() - t0) * 1000)
        ok = bool(body and body.get("ok"))
        try:
            from guardian.terminal_feedback import emit_inference

            emit_inference(
                "runtime",
                op,
                latency_ms=latency_ms,
                ok=ok,
                status="ok" if ok else "fail",
                error=(str((body or {}).get("reason") or "")[:120] or None)
                if not ok
                else None,
                **(
                    {"tool_calls": body.get("tool_calls")}
                    if op == "generate_completion" and isinstance(body, dict)
                    else {}
                ),
            )
        except Exception:
            pass
        return body

    def generate_completion(
        self,
        prompt: str,
        *,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        top_k: Optional[int] = None,
        stop: Optional[list[str]] = None,
        system: Optional[str] = None,
        tools: Optional[list[dict]] = None,
        chat_template_kwargs: Optional[dict] = None,
        enable_thinking: bool = False,
        response_format: Optional[dict] = None,
        timeout: Optional[float] = None,
        context_size: Optional[int] = None,
        model: Optional[str] = None,
        local_only: bool = False,
        pipeline_id: Optional[int] = None,
        work_kind: Optional[str] = None,
        cycle_id: Optional[str] = None,
        job_id: Optional[str] = None,
        messages: Optional[list[dict]] = None,
        tool_choice: Optional[Any] = None,
        seed: Optional[int] = None,
    ) -> Optional[dict]:
        """POST /generate and return the **full** JSON body on success.

        Use when callers need ``tool_calls``, ``finish_reason``, or fields
        other than ``text`` (llama.cpp / local-services native tool calling).
        Returns ``None`` on failure — same contract as ``generate()`` for
        transport errors and ``ok=false`` responses.

        A caller running a native tool loop may provide the complete
        ``messages`` transcript. Assistant tool calls and matching ``role=tool``
        results are forwarded verbatim; legacy prompt callers are unchanged.
        """
        # P3-S32: thinking off by default — see generate() for the rationale.
        merged_template_kwargs = dict(chat_template_kwargs or {})
        if "enable_thinking" not in merged_template_kwargs:
            merged_template_kwargs["enable_thinking"] = bool(enable_thinking)

        payload: dict = {"prompt": prompt}
        # An explicit endpoint-model override rides the body and wins over the
        # /generate binding. It does not select a physical pipeline.
        if model:
            payload["model"] = model
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if temperature is not None:
            payload["temperature"] = temperature
        if top_p is not None:
            payload["top_p"] = top_p
        if top_k is not None:
            payload["top_k"] = top_k
        if stop is not None:
            payload["stop"] = stop
        if system is not None:
            payload["system"] = system
        if tools is not None:
            payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        if seed is not None:
            payload["seed"] = int(seed)
        if messages is not None:
            payload["messages"] = [dict(turn) for turn in messages]
        if response_format is not None:
            payload["response_format"] = response_format
        if context_size is not None:
            payload["num_ctx"] = int(context_size)
            if isinstance(payload.get("options"), dict):
                opts = dict(payload["options"])
                opts["num_ctx"] = int(context_size)
                payload["options"] = opts
            else:
                payload["options"] = {"num_ctx": int(context_size)}
        payload["chat_template_kwargs"] = merged_template_kwargs
        payload.update(
            _pipeline_context(
                pipeline_id=pipeline_id,
                work_kind=work_kind,
                cycle_id=cycle_id,
                job_id=job_id,
            )
        )
        with self._prediction_admission(pipeline_id):
            body = self._cloud_or_local_generate(
                payload,
                op="generate_completion",
                timeout=timeout,
                local_only=local_only,
            )
        if body is None:
            return None
        if not body.get("ok"):
            log.warning(
                "generate_completion ok=false reason=%s detail=%s",
                body.get("reason"),
                str(body.get("detail") or "")[:200],
            )
            return None
        return body

    def _stream_payload(
        self,
        prompt: str,
        *,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        top_k: Optional[int] = None,
        stop: Optional[list[str]] = None,
        system: Optional[str] = None,
        chat_template_kwargs: Optional[dict] = None,
        enable_thinking: bool = False,
        model: Optional[str] = None,
        pipeline_id: Optional[int] = None,
        work_kind: Optional[str] = None,
        cycle_id: Optional[str] = None,
        job_id: Optional[str] = None,
        tools: Optional[list[dict]] = None,
        tool_choice: Optional[Any] = None,
        messages: Optional[list[dict]] = None,
    ) -> dict:
        """The /generate/stream body. Built to the SAME rules as ``generate()``'s
        — same omit-by-default sampler contract, same endpoint override, and same
        physical request context — because a pass must not mean something
        different for having been streamed."""
        # P3-S32: thinking off by default — see generate() for the rationale.
        merged_template_kwargs = dict(chat_template_kwargs or {})
        if "enable_thinking" not in merged_template_kwargs:
            merged_template_kwargs["enable_thinking"] = bool(enable_thinking)
        payload: dict = {"prompt": prompt}
        # Endpoint-model override, exactly as generate_completion carries it.
        if model:
            payload["model"] = model
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if temperature is not None:
            payload["temperature"] = temperature
        if top_p is not None:
            payload["top_p"] = top_p
        if top_k is not None:
            payload["top_k"] = top_k
        if stop is not None:
            payload["stop"] = stop
        if system is not None:
            payload["system"] = system
        # A native tool loop streams too (2026-09-05): the transcript and the
        # tool table ride exactly as generate_completion sends them.
        if tools is not None:
            payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        if messages is not None:
            payload["messages"] = [dict(turn) for turn in messages]
        payload["chat_template_kwargs"] = merged_template_kwargs
        payload.update(
            _pipeline_context(
                pipeline_id=pipeline_id,
                work_kind=work_kind,
                cycle_id=cycle_id,
                job_id=job_id,
            )
        )
        return payload

    def _stream_events(
        self, payload: dict, *, timeout: Optional[float] = None
    ) -> Iterator[tuple[str, dict]]:
        """Raw ``(event, data)`` pairs from /generate/stream, terminating on
        ``done`` (which it yields first, so the caller can read tokens_out /
        finish_reason / model_id off it).

        Inference/provider/transport failures raise a contextual RuntimeStreamError.
        Completion assembly separately rejects a stream without a done frame.
        """
        timeout = timeout if timeout is not None else TIMEOUTS["generate"]
        # Cloud brain route: stream from the frontier endpoint (see
        # _cloud_or_local_generate); it does not touch the local engine, so no
        # local preflight is required.
        from runtime import brain_cloud

        if brain_cloud.enabled():
            # The events the local gateway serves, tool-call fragments included. A prose-only
            # cloud stream dropped every streamed operator tool call (live 2026-09-15).
            context = _response_context(payload)
            for event, data in brain_cloud.generate_stream_events(payload, timeout=timeout):
                if event == "token":
                    yield ("token", {"token": data, **context})
                elif event == "reasoning":
                    yield ("reasoning", {"text": data, **context})
                elif event == "tool_call":
                    yield ("tool_call", {**data, **context})
                elif event == "done":
                    yield ("done", {**dict(data or {}), **context})
                    return
            return
        if not self._gpu_clear_to_generate("generate_stream"):
            raise RuntimeStreamError("generation_preflight_declined", context=_response_context(payload))
        url = f"{self._base_url}/generate/stream"
        from schema.continuity_json import content_hash

        # WO-05 step 1: the digest of the exact body posted to the local gateway.
        request_digest = content_hash(payload)
        try:
            with self._ensure_session().post(url, json=payload, stream=True, timeout=timeout) as r:
                if r.status_code != 200:
                    log.warning(
                        "generate_stream non-200: status=%s body=%s", r.status_code, r.text[:200]
                    )
                    raise RuntimeStreamError(f"stream_http_{r.status_code}", r.text[:200], context=_response_context(payload))
                event = None
                for raw in r.iter_lines(decode_unicode=True):
                    if not raw:
                        continue
                    if raw.startswith("event:"):
                        event = raw[len("event:") :].strip()
                    elif raw.startswith("data:"):
                        data_str = raw[len("data:") :].strip()
                        if event == "error":
                            try:
                                err = json.loads(data_str)
                            except ValueError:
                                err = {"reason": "inference_error", "detail": data_str}
                            reason = str(err.get("reason") or "inference_error")
                            detail = str(err.get("detail") or "")
                            log.warning(
                                "generate_stream server event:error reason=%s detail=%s",
                                reason,
                                detail[:200],
                            )
                            raise RuntimeStreamError(
                                reason,
                                detail,
                                context=_response_context(payload),
                            )
                        try:
                            data = json.loads(data_str)
                        except ValueError:
                            continue
                        if not isinstance(data, dict):
                            continue
                        data = {**data, **_response_context(payload)}
                        if event == "done":
                            data["request_digest"] = request_digest
                        yield (str(event or ""), data)
                        if event == "done":
                            return
        except RuntimeStreamError:
            # Re-raise so callers can distinguish server-reported errors from
            # transport failures / empty completions.
            raise
        except (requests.RequestException, ValueError) as exc:
            log.warning("generate_stream transport failure: %s", exc)
            raise RuntimeStreamError("stream_transport", str(exc), context=_response_context(payload)) from exc

    def generate_stream(
        self,
        prompt: str,
        *,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        top_k: Optional[int] = None,
        stop: Optional[list[str]] = None,
        system: Optional[str] = None,
        chat_template_kwargs: Optional[dict] = None,
        enable_thinking: bool = False,
        timeout: Optional[float] = None,
        model: Optional[str] = None,
        pipeline_id: Optional[int] = None,
        work_kind: Optional[str] = None,
        cycle_id: Optional[str] = None,
        job_id: Optional[str] = None,
    ) -> Iterator[str]:
        """Token-by-token SSE stream. Yields tokens as strings; ends silently
        on `event: done`. Yields nothing on connection failure (matches
        empty-iterator failure contract).

        Sampler params follow the same omit-by-default contract as
        ``generate()`` — runtime registry holds the truth; pass explicit
        values only when the phase has documented intent."""
        payload = self._stream_payload(
            prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            stop=stop,
            system=system,
            chat_template_kwargs=chat_template_kwargs,
            enable_thinking=enable_thinking,
            model=model,
            pipeline_id=pipeline_id,
            work_kind=work_kind,
            cycle_id=cycle_id,
            job_id=job_id,
        )
        with self._prediction_admission(pipeline_id):
            for event, data in self._stream_events(payload, timeout=timeout):
                if event != "token":
                    continue
                token = data.get("token")
                if token is not None:
                    yield token

    def generate_completion_stream(
        self,
        prompt: str,
        *,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        top_k: Optional[int] = None,
        stop: Optional[list[str]] = None,
        system: Optional[str] = None,
        chat_template_kwargs: Optional[dict] = None,
        enable_thinking: bool = False,
        timeout: Optional[float] = None,
        model: Optional[str] = None,
        pipeline_id: Optional[int] = None,
        work_kind: Optional[str] = None,
        cycle_id: Optional[str] = None,
        job_id: Optional[str] = None,
        on_token: Optional[Callable[[str], None]] = None,
        on_reasoning: Optional[Callable[[str], None]] = None,
        tools: Optional[list[dict]] = None,
        tool_choice: Optional[Any] = None,
        messages: Optional[list[dict]] = None,
    ) -> Optional[dict]:
        """``generate_completion``'s answer, delivered token by token on the way.

        ``tools`` / ``tool_choice`` / ``messages`` (2026-09-05) let a native tool
        loop stream: the gateway forwards ``tool_call`` fragments as they arrive
        and this reassembles them, so the body carries ``tool_calls`` exactly as
        the blocking call would while the prose still reached ``on_token`` live.
        ``on_reasoning`` receives the think trace as it streams; it is also
        returned whole under ``reasoning``.

        RISK 5 (operator 2026-08-11). The cascade needs a WHOLE reply — every
        stage after AUTO reads ``response_text``, and the durable response rail
        stores one string — so streaming cannot be allowed to change the shape
        of a cognition pass. It changes only WHEN the operator hears it: tokens
        go to ``on_token`` as the engine produces them and the assembled text
        comes back in the same dict ``generate_completion`` returns.

        ``on_token`` is DELIVERY, never storage, and never a dependency: it is
        called inside a try/except and a sink that raises costs its own tokens
        and nothing else. A dead browser tab must not be able to fail a thought.

        Raises RuntimeStreamError on inference failure or missing completion.
        A successful EMPTY completion remains ``{"ok": True, "text": ""}``.
        """
        payload = self._stream_payload(
            prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            stop=stop,
            system=system,
            chat_template_kwargs=chat_template_kwargs,
            enable_thinking=enable_thinking,
            model=model,
            pipeline_id=pipeline_id,
            work_kind=work_kind,
            cycle_id=cycle_id,
            job_id=job_id,
            tools=tools,
            tool_choice=tool_choice,
            messages=messages,
        )
        started = time.perf_counter()
        parts: list[str] = []
        reasoning: list[str] = []
        calls: dict[int, dict] = {}
        done: dict = {}
        saw_done = False
        with self._prediction_admission(pipeline_id):
            try:
                for event, data in self._stream_events(payload, timeout=timeout):
                    if event == "token":
                        token = data.get("token")
                        if token is None:
                            continue
                        parts.append(str(token))
                        if on_token is not None:
                            try:
                                on_token(str(token))
                            except Exception:  # noqa: BLE001 — delivery is never a dependency
                                log.debug("stream token sink failed; dropping it", exc_info=True)
                                on_token = None
                    elif event == "reasoning":
                        piece = str(data.get("text") or "")
                        reasoning.append(piece)
                        if on_reasoning is not None and piece:
                            try:
                                on_reasoning(piece)
                            except Exception:  # noqa: BLE001 — delivery is never a dependency
                                log.debug("stream reasoning sink failed; dropping it", exc_info=True)
                                on_reasoning = None
                    elif event == "tool_call":
                        _merge_tool_call_fragment(calls, data)
                    elif event == "done":
                        done = data
                        saw_done = True
            except RuntimeStreamError as exc:
                log.warning("generate_completion_stream error reason=%s", exc.reason)
                raise
        if not saw_done:
            # No done frame: the connection died, the preflight declined, or the
            # gateway never answered 200. Whatever partial text arrived is not a
            # completion, and calling it one would hand the cascade a truncated
            # reply it cannot tell from a finished one.
            raise RuntimeStreamError("stream_missing_done", "Stream ended without completion", context=_response_context(payload))
        body: dict = {
            "ok": True,
            "text": "".join(parts),
            "finish_reason": str(done.get("finish_reason") or "stop"),
            "tokens_out": done.get("tokens_out"),
            "latency_ms": done.get("latency_ms")
            or int((time.perf_counter() - started) * 1000),
            "streamed": True,
            **_response_context(done),
        }
        model_id = done.get("model_id")
        if model_id:
            body["model_id"] = model_id
        if done.get("request_digest"):
            body["request_digest"] = str(done["request_digest"])
        joined_reasoning = "".join(reasoning).strip()
        if joined_reasoning:
            body["reasoning"] = joined_reasoning
        if calls:
            body["tool_calls"] = [calls[index] for index in sorted(calls)]
            body["finish_reason"] = "tool_calls"
        return body

    _BENIGN_TRANSCRIBE_REASONS = frozenset(
        {
            "no_speech",
            "silence",
            "empty",
            "empty_transcript",
            "vad",
            "vad_filtered",
            "no_audio",
            "too_short",
        }
    )

    def transcribe(
        self,
        audio_bytes: bytes,
        *,
        audio_format: str = "wav",
        language: str = "auto",
        task: Optional[str] = None,
        vad_filter: bool = True,
    ) -> Optional[str]:
        """STT via Omega's portable Parakeet /transcribe service.
        ``language='auto'`` leaves language selection to the configured service.

        Returns ``None`` only on transport/runtime failure. Empty string means
        the call succeeded but no speech was detected (silence / VAD)."""
        payload = {
            "audio_b64": base64.b64encode(audio_bytes).decode("ascii"),
            "format": audio_format,
            "language": language,
            "vad_filter": vad_filter,
        }
        if task is not None:
            payload["task"] = task

        endpoint = _configured_socket_path("stt", service=True)
        if endpoint is None:
            return None
        body = self._post(
            endpoint,
            payload,
            op="transcribe",
        )
        if body is None:
            return None
        if not body.get("ok"):
            reason = str(body.get("reason") or "").strip().lower()
            if reason in self._BENIGN_TRANSCRIBE_REASONS:
                return str(body.get("text") or "")
            log.warning("transcribe ok=false reason=%s", body.get("reason"))
            return None
        return str(body.get("text") or "")

    def voice_command(
        self,
        audio_bytes: bytes,
        *,
        audio_format: str = "wav",
        tools: Optional[list[dict]] = None,
        system: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> Optional[dict]:
        """Audio-In, Action-Out. Voice → transcript + intent + tool_calls
        in ONE model call. Requires the runtime endpoint to be backed by
        Gemma 4 (or another model with native voice-to-tool). Returns the
        full response dict on success: {transcript, intent, tool_calls,
        text, language, latency_ms}. None on failure.

        Sampler params (max_tokens, temperature) are runtime-owned by
        default; only pass when the phase has documented intent.
        ``audio_format`` stays explicit since it's a content-type hint,
        not a sampler config."""
        payload: dict = {
            "audio_b64": base64.b64encode(audio_bytes).decode("ascii"),
            "format": audio_format,
        }
        if tools is not None:
            payload["tools"] = tools
        if system is not None:
            payload["system"] = system
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if temperature is not None:
            payload["temperature"] = temperature
        endpoint = _configured_socket_path("voice", service=True)
        if endpoint is None:
            return None
        body = self._post(
            endpoint,
            payload,
            op="generate",  # voice/command shares generate's latency budget
        )
        if body is None:
            return None
        if not body.get("ok"):
            log.warning("voice_command ok=false reason=%s", body.get("reason"))
            return None
        return body

    def synthesise(
        self, text: str, *, voice: str = "", length_scale: float = 1.0
    ) -> Optional[bytes]:
        # The RUNTIME owns TTS (the Qwen sidecar; LAW 2026-08-01). The in-brain engine that used
        # to answer here loaded a 92 MB ONNX inside the cascade's own process, so
        # synthesis fought cognition for CPU — 24 chars took 559 ms idle but a
        # 163-char reply took 23 s mid-episode. The runtime hop keeps synthesis
        # outside every temporary cognition pipeline.
        endpoint = _configured_socket_path("tts", service=True)
        if endpoint is None:
            return None
        body = self._post(
            endpoint,
            {"text": text, "voice": voice, "length_scale": length_scale},
            op="synthesise",
        )
        if body is None:
            return None
        if not body.get("ok"):
            log.warning("synthesise ok=false reason=%s", body.get("reason"))
            return None
        b64 = body.get("audio_b64")
        if not b64:
            log.warning("synthesise ok=true but audio_b64 missing")
            return None
        try:
            return base64.b64decode(b64)
        except (ValueError, TypeError) as exc:
            log.warning("synthesise audio_b64 decode failed: %s", exc)
            return None

    async def synthesise_stream(
        self, text: str, *, voice: str = "", length_scale: float = 1.0
    ) -> AsyncIterator[bytes]:
        """Yield raw s16le 24 kHz mono chunks from the runtime TTS stream."""
        endpoint = _configured_socket_path("tts", service=True)
        if endpoint is None:
            raise RuntimeError("TTS service socket is not configured")
        url = f"{self._base_url}{endpoint}/stream"
        from contextlib import aclosing
        from runtime.tts_stream import http_pcm, pcm_client

        async with aclosing(http_pcm(
            url, {"text": text, "voice": voice, "length_scale": length_scale},
            client_factory=lambda: pcm_client(TIMEOUTS["synthesise"], url=url),
        )) as stream:
            async for chunk in stream:
                yield chunk

    def face_identify(self, image_bytes: bytes, *, image_format: str = "jpeg") -> Optional[dict]:
        """Returns {name, confidence, landmarks, bbox, ...} on success.

        When the runtime provides it, also includes ``embedding`` (512-dim float
        list) for brain-side anchor_store / remember-face persistence.
        ``name`` is None if a face was detected but below recognition threshold."""
        body = self._post(
            "/face/identify",
            {"image_b64": base64.b64encode(image_bytes).decode("ascii"), "format": image_format},
            op="face",
        )
        if body is None:
            return None
        if not body.get("ok"):
            log.warning("face_identify ok=false reason=%s", body.get("reason"))
            return None
        return body

    def face_landmarks(self, image_bytes: bytes, *, kind: str = "2d106") -> Optional[dict]:
        body = self._post(
            "/face/landmarks",
            {"image_b64": base64.b64encode(image_bytes).decode("ascii"), "kind": kind},
            op="face",
        )
        if body is None:
            return None
        if not body.get("ok"):
            log.warning("face_landmarks ok=false reason=%s kind=%s", body.get("reason"), kind)
            return None
        return body

    def image_generate(
        self,
        prompt: str,
        *,
        width: int = 1024,
        height: int = 1024,
        steps: int = 4,
        seed: Optional[int] = None,
    ) -> Optional[bytes]:
        # Omit `seed` when not supplied — the runtime's schema requires a
        # valid integer and rejects an explicit null (422
        # Unprocessable). Leaving it out lets the runtime pick its own
        # (random) seed; pass an int to pin reproducibility.
        payload = {"prompt": prompt, "width": width, "height": height, "steps": steps}
        if seed is not None:
            payload["seed"] = int(seed)
        endpoint = _configured_socket_path("image_tool", service=False)
        if endpoint is None:
            return None
        body = self._post(endpoint, payload, op="image_gen")
        if body is None:
            return None
        if not body.get("ok"):
            log.warning("image_generate ok=false reason=%s", body.get("reason"))
            return None
        b64 = body.get("image_b64")
        if not b64:
            log.warning("image_generate ok=true but image_b64 missing")
            return None
        try:
            return base64.b64decode(b64)
        except (ValueError, TypeError) as exc:
            log.warning("image_generate image_b64 decode failed: %s", exc)
            return None

    def vision_describe(
        self,
        image_bytes: bytes,
        *,
        prompt: Optional[str] = None,
        image_format: str = "jpeg",
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> Optional[str]:
        """Describe an image through the selected offline or cloud mind seat.

        Kept on ``RuntimeClient`` for callers that already use this method;
        the provider-neutral implementation no longer posts to the retired
        local-services ``/vision/describe`` route.
        """
        from runtime.vision import describe_image

        return describe_image(
            image_bytes,
            prompt=prompt,
            image_format=image_format,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    def document_extract(
        self,
        file_bytes: bytes,
        *,
        filename: str = "document",
        document_format: str = "auto",
    ) -> Optional[dict]:
        """Server-side text extraction from uploaded files (PDF, DOCX,
        HTML, TXT, MD). Brain has local parsers for common formats too
        (see brain.document_loader); this endpoint is the catch-all and
        the centralised-extraction-policy path. Returns {text, format,
        page_count, metadata, latency_ms} on success."""
        body = self._post(
            "/document/extract",
            {
                "file_b64": base64.b64encode(file_bytes).decode("ascii"),
                "filename": filename,
                "format": document_format,
            },
            op="document",
        )
        if body is None:
            return None
        if not body.get("ok"):
            log.warning(
                "document_extract ok=false reason=%s filename=%r", body.get("reason"), filename
            )
            return None
        return body


def _merge_tool_call_fragment(calls: dict[int, dict], data: dict) -> None:
    """Fold one streamed ``tool_call`` fragment into the call it belongs to.

    The gateway forwards each ``delta.tool_calls`` piece keyed by ``index``: the
    first piece of a call carries its id and name, later pieces carry more of
    its ``arguments`` string. The result is the OpenAI-shaped call the blocking
    path would have returned whole.
    """
    if not isinstance(data, dict):
        return
    try:
        index = int(data.get("index") or 0)
    except (TypeError, ValueError):
        index = 0
    call = calls.setdefault(
        index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
    )
    if data.get("id"):
        call["id"] = str(data["id"])
    if data.get("name"):
        call["function"]["name"] = str(data["name"])
    piece = data.get("arguments")
    if piece is not None:
        call["function"]["arguments"] += str(piece)


def resolve_runtime_base_url() -> str:
    """Base URL for Omega local services: env overrides declarative config."""
    env = os.environ.get("OMEGA_RUNTIME_URL", "").strip()
    if env:
        return env.rstrip("/")
    try:
        from pressure_loop.policy import runtime_endpoint

        return runtime_endpoint()
    except Exception:
        return DEFAULT_BASE_URL


# ────────────────────────────────────────────────────────────────────────
# Singleton
# ────────────────────────────────────────────────────────────────────────
_singleton: Optional[RuntimeClient] = None
_singleton_lock = threading.Lock()


def get_runtime_client() -> RuntimeClient:
    """Get the module-level singleton. Thread-safe lazy init."""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = RuntimeClient()
    return _singleton
