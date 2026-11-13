"""Runtime model handoff helpers for operator-owned agents.

The mind does not load weights directly; the runtime service owns model
registry and slots. These helpers call runtime control endpoints when present
and fall back to a tiny OpenAI-compatible warmup request for the CLI model.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterable
from typing import Any, Optional
from urllib.parse import quote

import requests

from runtime.client import resolve_runtime_base_url

log = logging.getLogger("omega.runtime.model_control")


def _emit_model_control(
    event_name: str,
    *,
    ok: bool | None,
    status: str | None = None,
    **data: Any,
) -> None:
    payload = {"ok": ok, **data}
    try:
        from guardian.terminal_feedback import emit_guardian_event

        emit_guardian_event(
            "omega.runtime.model_control",
            event_name,
            status=status or ("ok" if ok else "fail"),
            **payload,
        )
    except Exception:
        pass


# Seat unification (operator 2026-08-09, weights upgraded 2026-08-13): qwen3.5
# holds EVERY role — the single-model concurrent-lane resident (cognition +
# agent/browser/CLI + folds) @ 262k unified ctx. Cognition and driver being the
# SAME model kills the old cli_model hotswap dance: loading the agent seat no
# longer evicts the resident because it IS the resident, so agent sessions and
# cognition share the engine's batching slots (lanes) instead of swapping
# weights.
#
# 2026-08-13: the 4B became the 9B and the rollback levers were REMOVED — the
# operator retired qwen35_4b_unsloth, gemma_e4b and agents_a1_4b from the
# runtime registry outright ("no qwen 4b. remove it, the 9b replaces all that").
# These two ids were therefore naming a model the runtime no longer has, which
# is not cosmetic here: prepare_dev_pill_runtime sends DEFAULT_BROWSER_MODEL to
# /models/load, and the runtime answers unknown_model for an id it cannot find.
DEFAULT_BROWSER_MODEL = "qwen35_9b"
# 2026-09-05 (operator: "give omega a real brain"): cognition is Mistral Medium 3.5
# through the runtime's remote_openai seat. The 9B stays as the runtime-side
# /generate fallback and as the local CLI / browser-agent driver.
DEFAULT_COGNITION_MODEL = "mistral_medium_35"
# The LOCAL resident LLM: the runtime-side fallback for /generate and the seat the
# CLI / browser-agent driver runs on. A handoff only ever moves THIS off the iGPU.
LOCAL_RESIDENT_MODEL = "qwen35_9b"
# Cognition seats that live on a hosted API (runtime ``remote_openai``). They hold
# no local accelerator memory, so a handoff has nothing to yield from them: never
# unload one, never take the GPU lease for one.
CLOUD_COGNITION_SEATS = frozenset({"mistral_medium_35"})
_CONTROL_TIMEOUT = float(os.environ.get("OMEGA_RUNTIME_LOAD_TIMEOUT", "45"))
_WARMUP_TIMEOUT = 90.0
_HEALTH_PROBE_TIMEOUT = 2.0

# Boot-time cognition residency. Cognition runs the model at this configured
# context or does not run. Shrinking the context after a failed load changes
# cognition semantics and is therefore a hard failure, not a recovery route.
COGNITION_CTX_PREFERRED = 65536  # 64k

# How long the boot probe waits for a runtime that is merely SLOW, before it
# calls the runtime ABSENT. RISK 4(a), operator 2026-08-11.
#
# MEASURED on this appliance, all three numbers from the same box on the same
# day, so they are comparable:
#
#   warm gateway (page cache hot)   spawn -> /health   0.5s
#                                   -> 262k 4B seated  8.3s   (total 8.8s)
#   cache-evicted gateway (18GB of  spawn -> /health   0.5s
#   venv + weights dropped from     -> 262k 4B seated  6.3s   (total 6.8s)
#   page cache first)
#   REAL cold boot (machine up      spawn -> /health   NEVER inside 120s
#   12:55:48, unit start 12:55:59,
#   gateway spawned 12:56:00)
#
# The third line is the one that matters and the first two are why: a cold boot
# is NOT slow because of disk, and it is not slow because of the model — the 4B
# is lazy_load and seats in seconds either way. It is slow because the gateway
# binds its port only after gateway/cli.build_runtime_app has finished probing
# the host synchronously (Vulkan/CUDA detection and the ORT NPU smoke tests),
# and eleven seconds after a machine boot that probe is contending with every
# other thing systemd started, on GPU and NPU drivers that are themselves still
# initializing. Nothing in the brain can make that faster; the only honest
# response is to WAIT for it.
#
# 300s is deliberately far past the 121s we measured a cold boot exceed, because
# the true ceiling is unknown — we only ever observed the supervisor give up at
# its own 120s and tree-kill a gateway that was still legitimately starting. It
# also still fits the appliance's self-healing story: the unit is
# Restart=on-failure with RestartSec=5, so even a genuinely dead runtime costs
# one bounded wait before the crash that fail-hard requires.
COGNITION_PROBE_WAIT_S = 300.0
# Poll cadence for that wait: frequent at first (a warm restart answers in under
# a second and must not pay a fixed penalty for the cold case), backing off to a
# ceiling so a five-minute wait is not five minutes of loopback requests.
_PROBE_POLL_START_S = 0.5
_PROBE_POLL_CEILING_S = 5.0


def _cloud_cli_route() -> Optional[dict]:
    """Return the configured cloud mind seat for agent execution, or ``None`` for local.

    Agent work is an AUTO action under the one configured mind seat. There is no separate
    CLI/provider override and no second persisted provider-selection file.
    """
    from runtime import brain_cloud_config

    seat = brain_cloud_config.descriptor()
    route: Optional[dict] = None
    if seat:
        model = str(seat.get("model") or "").strip()
        if model:
            route = {"provider": "brain_cloud", "model": model}
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "runtime",
            "cloud_mind_route_check",
            status="ok",
            summary=(
                f"Cloud mind route enabled={bool(route)}"
                + (f" provider={route['provider']} model={route['model']}" if route else "")
            ),
            enabled=bool(route),
            provider=(route or {}).get("provider", ""),
            model=(route or {}).get("model", ""),
            source="mind_backend" if route else "",
        )
    except Exception:
        pass
    return route


class RuntimeCognitionUnavailable(RuntimeError):
    """The cognition model could not be made resident at any acceptable context.

    A boot-fatal condition: cognition cannot run without a resident model, so the
    whole system must crash rather than tick on a missing/limping model (boot.py
    is tied to start.bat — this propagates out as a hard startup failure)."""


def _post_json(
    base_url: str,
    path: str,
    body: dict[str, Any],
    *,
    timeout: float = _CONTROL_TIMEOUT,
) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}{path}"
    # Attach admin auth when configured: the runtime's model-control/face routes
    # require_admin, which 401s on a non-loopback bind (check_bind_safety forces a
    # token there). Without this header, unload would 401 and jackrong_qwen would
    # never be released — defeating the §11 dev-pill hotswap reclaim. Loopback with
    # no token is unaffected (header simply absent).
    headers: dict[str, str] = {}
    _admin_token = (os.environ.get("OMEGA_ADMIN_TOKEN") or "").strip()
    if _admin_token:
        headers["Authorization"] = f"Bearer {_admin_token}"
    try:
        res = requests.post(url, json=body, timeout=timeout, headers=headers or None)
    except (requests.ConnectionError, requests.Timeout) as exc:
        return {
            "ok": False,
            "route": path,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
    except requests.RequestException as exc:
        return {
            "ok": False,
            "route": path,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }

    payload: Any = None
    try:
        payload = res.json()
    except ValueError:
        payload = None

    if res.status_code in (404, 405):
        return {
            "ok": False,
            "route": path,
            "status": res.status_code,
            "unsupported": True,
        }
    if res.status_code >= 400:
        return {
            "ok": False,
            "route": path,
            "status": res.status_code,
            "error": str(payload or res.text)[:240],
        }
    if isinstance(payload, dict) and payload.get("ok") is False:
        return {
            "ok": False,
            "route": path,
            "status": res.status_code,
            "error": str(payload.get("reason") or payload.get("error") or payload)[:240],
            "payload": payload,
        }
    return {
        "ok": True,
        "route": path,
        "status": res.status_code,
        "payload": payload,
    }


def _probe_runtime_reachable(base_url: str) -> bool:
    """Fast health probe before expensive model-load attempts."""
    url = f"{base_url.rstrip('/')}/health"
    try:
        res = requests.get(url, timeout=_HEALTH_PROBE_TIMEOUT)
    except (requests.ConnectionError, requests.Timeout):
        return False
    except requests.RequestException:
        return False
    if res.status_code >= 400:
        return False
    try:
        body = res.json()
    except ValueError:
        return False
    return bool(isinstance(body, dict) and body.get("status") == "ok")


def _first_ok(results: Iterable[dict[str, Any]]) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    for result in results:
        attempts.append(result)
        if result.get("ok"):
            return result
    return {
        "ok": False,
        "attempts": attempts,
        "error": "no supported model-control route succeeded",
    }


def _resolve_cognition_model(target_model: str) -> str:
    env_model = os.environ.get("OMEGA_COGNITION_MODEL", "").strip()
    if env_model and env_model != target_model:
        return env_model
    # The mind socket is backend-neutral and its slot is not a model address.
    # Never turn that display label into an unload target; only an explicit
    # deployment override may identify a concrete local model.
    return ""


# ── Endpoint-addressed control (the preferred door) ─────────────────────────
# Operator ruling 2026-08-15: "the omega system itself should only resolve to
# endpoints, let the runtime handle the models, omega just calls them."
#
# The cognition path already obeys this — runtime.socket sends no model id, it
# POSTs /generate and the runtime resolves the seat (config/runtime.yaml is
# explicit that ``slot:`` there is a display/audit LABEL, not an address).
# Model CONTROL was the hole: it took that label and used it as an address,
# which is how the brain ended up naming weights it does not own.
#
# Addressing an endpoint instead means swapping a provider is a runtime config
# edit, not a brain code change — the whole point of the scaling work.


def resolve_endpoint_model(endpoint: str, *, base_url: str | None = None) -> str:
    """Ask the RUNTIME which model is behind ``endpoint``; "" when it cannot say.

    For the few places that must put a model name on the wire because a
    foreign protocol demands the field — the TS bridge speaks OpenAI, where
    ``model`` is required — this keeps the brain from *knowing* the answer. It
    asks, and the runtime (which owns the registry, the overrides and any
    /remap) answers. Callers keep a local label as a last-resort fallback so a
    runtime that is down degrades to today's behaviour instead of failing.
    """
    endpoint = str(endpoint or "").strip()
    if not endpoint:
        return ""
    base = (base_url or resolve_runtime_base_url()).rstrip("/")
    headers: dict[str, str] = {}
    token = (os.environ.get("OMEGA_ADMIN_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        res = requests.get(
            f"{base}/admin/orchestration/endpoints",
            timeout=_HEALTH_PROBE_TIMEOUT,
            headers=headers or None,
        )
        if res.status_code >= 400:
            return ""
        payload = res.json()
    except (requests.RequestException, ValueError) as exc:
        log.debug("resolve_endpoint_model(%s): runtime did not answer: %s", endpoint, exc)
        return ""
    if not isinstance(payload, dict):
        return ""
    resolved = (payload.get("endpoints") or {}).get("resolved")
    if not isinstance(resolved, dict):
        return ""
    value = resolved.get(endpoint)
    if isinstance(value, str) and value.strip():
        return value.strip()
    # Pipelines resolve to a list; the first stage is the addressable one.
    if isinstance(value, (list, tuple)) and value:
        first = value[0]
        if isinstance(first, str) and first.strip():
            return first.strip()
    return ""


def unload_endpoint(endpoint: str, *, base_url: str | None = None) -> dict[str, Any]:
    """Release whatever model(s) the runtime has behind ``endpoint``.

    Returns the runtime's verdict verbatim. A refusal (a pinned seat) comes
    back ``ok: False`` with its reason — this never falls back to some other
    route in the hope of a yes.
    """
    endpoint = str(endpoint or "").strip()
    if not endpoint:
        raise ValueError("endpoint required")
    base = (base_url or resolve_runtime_base_url()).rstrip("/")
    result = _post_json(
        base,
        f"/admin/orchestration/endpoints/{quote(endpoint, safe='')}/unload",
        {},
        timeout=_CONTROL_TIMEOUT,
    )
    result["endpoint"] = endpoint
    unsupported = bool(result.get("unsupported"))
    _emit_model_control(
        "unload_endpoint",
        ok=None if unsupported else bool(result.get("ok")),
        status="skip" if unsupported else None,
        endpoint=endpoint,
        route=result.get("route"),
        status_code=result.get("status"),
        unsupported=unsupported,
        error=result.get("error"),
    )
    return result


def load_endpoint(
    endpoint: str,
    *,
    base_url: str | None = None,
    context_size: int | None = None,
) -> dict[str, Any]:
    """Seat whatever model(s) the runtime has behind ``endpoint``."""
    endpoint = str(endpoint or "").strip()
    if not endpoint:
        raise ValueError("endpoint required")
    base = (base_url or resolve_runtime_base_url()).rstrip("/")
    body = _with_ctx({}, context_size)
    result = _post_json(
        base,
        f"/admin/orchestration/endpoints/{quote(endpoint, safe='')}/load",
        body,
        timeout=_WARMUP_TIMEOUT,
    )
    result["endpoint"] = endpoint
    if context_size:
        result["context_size"] = int(context_size)
    _emit_model_control(
        "load_endpoint",
        ok=bool(result.get("ok")),
        endpoint=endpoint,
        route=result.get("route"),
        context_size=context_size,
    )
    return result


def unload_model(model: str, *, base_url: str | None = None) -> dict[str, Any]:
    """Ask the runtime to unload ``model`` using known compatible routes.

    Prefer :func:`unload_endpoint`. This stays for the dev-pill / browser-agent
    hotswap flows, which address a specific hot-swappable weight rather than a
    capability the mind uses.
    """

    model = str(model or "").strip()
    if not model:
        raise ValueError("model required")
    base = (base_url or resolve_runtime_base_url()).rstrip("/")
    encoded = quote(model, safe="")
    # THE route first: the runtime service serves unload at
    # /admin/orchestration/models/{id}/unload (routes_orchestration, prefixed
    # router) and /models/unload (routes_models_control). The rest are legacy
    # shapes kept for other runtimes; they 404 here and cost one local
    # round-trip each.
    #
    # There is deliberately NO generate-based fallback. Ollama unloads by
    # POSTing /api/generate with keep_alive: 0 — on THIS runtime /api/generate
    # is the generate route and _ensure_model_worker loads on demand,
    # unguarded, so that "unload" LOADS. It did (2026-08-15): a cloud-brain
    # boot walked the ladder, every real route refused with
    # pinned_model:qwen35_9b, the generate fallback spawned the 9B in 23s and
    # returned 200, and boot logged "Evicted stale local resident" about a
    # model it had just seated — 9862 MiB held for a seat with no callers.
    # A control call that cannot release must REPORT that, never do the
    # opposite and claim success.
    attempts = [
        (f"/admin/orchestration/models/{encoded}/unload", {}, _CONTROL_TIMEOUT),
        ("/models/unload", {"model_id": model}, _CONTROL_TIMEOUT),
        (f"/admin/models/{encoded}/unload", {}, _CONTROL_TIMEOUT),
        ("/admin/unload", {"model_id": model}, _CONTROL_TIMEOUT),
        (f"/models/{encoded}/unload", {}, _CONTROL_TIMEOUT),
        ("/unload", {"model_id": model}, _CONTROL_TIMEOUT),
    ]
    result = _first_ok(
        _post_json(base, path, body, timeout=timeout) for path, body, timeout in attempts
    )
    result["model"] = model
    _emit_model_control(
        "unload_model",
        ok=bool(result.get("ok")),
        model=model,
        route=result.get("route"),
    )
    return result


def _with_ctx(body: dict[str, Any], context_size: int | None) -> dict[str, Any]:
    """Best-effort: thread a requested context window into a load request body.

    Sets a top-level ``num_ctx`` and, when the body carries an Ollama-style
    ``options`` block, mirrors it there. Runtimes that don't honor the field
    simply ignore it; adjust the key here if the runtime expects another name."""
    if not context_size:
        return body
    out = dict(body)
    out["num_ctx"] = int(context_size)
    if isinstance(out.get("options"), dict):
        opts = dict(out["options"])
        opts["num_ctx"] = int(context_size)
        out["options"] = opts
    return out


def load_model(
    model: str,
    *,
    base_url: str | None = None,
    context_size: int | None = None,
) -> dict[str, Any]:
    """Ask the runtime to load/warm ``model`` using known compatible routes.

    When ``context_size`` is given it is threaded into each request as ``num_ctx``
    (best-effort) so the model loads at the requested context window."""

    model = str(model or "").strip()
    if not model:
        raise ValueError("model required")
    base = (base_url or resolve_runtime_base_url()).rstrip("/")
    encoded = quote(model, safe="")
    # THE route first (see unload_model for why guessing order matters). The
    # generate/chat warmups stay as the last resort: unlike the unload case,
    # "load by using it" has the same effect as the name promises, so a runtime
    # that exposes no control surface at all still gets warmed.
    attempts = [
        (f"/admin/orchestration/models/{encoded}/load", {}, _CONTROL_TIMEOUT),
        ("/models/load", {"model_id": model}, _CONTROL_TIMEOUT),
        (f"/admin/models/{encoded}/load", {}, _CONTROL_TIMEOUT),
        ("/admin/load", {"model_id": model}, _CONTROL_TIMEOUT),
        (f"/models/{encoded}/load", {}, _CONTROL_TIMEOUT),
        ("/load", {"model_id": model}, _CONTROL_TIMEOUT),
        (
            "/api/generate",
            {
                "model": model,
                "prompt": "OK",
                "stream": False,
                "keep_alive": "30m",
                "options": {"num_predict": 1},
            },
            _WARMUP_TIMEOUT,
        ),
        (
            "/v1/chat/completions",
            {
                "model": model,
                "messages": [{"role": "user", "content": "Reply OK."}],
                "temperature": 0,
                "max_tokens": 1,
                "stream": False,
                "enable_thinking": False,
                "chat_template_kwargs": {"enable_thinking": False},
            },
            _WARMUP_TIMEOUT,
        ),
    ]
    result = _first_ok(
        _post_json(base, path, _with_ctx(body, context_size), timeout=timeout)
        for path, body, timeout in attempts
    )
    result["model"] = model
    if context_size:
        result["context_size"] = int(context_size)
    _emit_model_control(
        "load_model",
        ok=bool(result.get("ok")),
        model=model,
        route=result.get("route"),
        context_size=context_size,
    )
    return result


def prepare_browser_agent_runtime(
    *,
    target_model: str = DEFAULT_BROWSER_MODEL,
    unload_cognition: bool = True,
    unload_models: list[str] | None = None,
) -> dict[str, Any]:
    """Pause-owner handoff: unload cognition candidate(s), then warm target."""

    # Cloud route (legacy OpenRouter flag OR any cloud provider saved in the terminal
    # settings dropdown): the CLI executor runs against the cloud model, so there is no
    # local CLI model to load and no GPU to lease. Skipping the handoff means gemma_e4b
    # is never hot-swapped off the GPU — it stays resident. No-op + ok so callers proceed
    # unchanged.
    _route = _cloud_cli_route()
    if _route:
        _skip = _route["provider"]
        _target = _route["model"] or _skip
        _emit_model_control(
            "prepare_browser_agent_runtime", ok=True, target_model=_target, unload_cognition=False
        )
        return {
            "ok": True,
            "skipped": _skip,
            "target_model": _target,
            "unload_cognition": False,
            "unload": [],
            "load": {"ok": True, "skipped": _skip},
        }

    target = str(target_model or "").strip() or DEFAULT_BROWSER_MODEL
    unload_candidates: list[str] = []
    for model in unload_models or []:
        cleaned = str(model or "").strip()
        if cleaned and cleaned != target and cleaned not in unload_candidates:
            unload_candidates.append(cleaned)
    if unload_cognition:
        cognition_model = _resolve_cognition_model(target)
        if cognition_model and cognition_model not in unload_candidates:
            unload_candidates.append(cognition_model)
    # A cloud cognition seat is not on the iGPU: evicting it is a no-op that would
    # still claim the GPU lease below and answer chat with the busy-ack.
    unload_candidates = [m for m in unload_candidates if m not in CLOUD_COGNITION_SEATS]

    base = resolve_runtime_base_url().rstrip("/")
    unload_results = [unload_model(model, base_url=base) for model in unload_candidates]
    load_result = load_model(target, base_url=base)
    out = {
        "ok": bool(load_result.get("ok")),
        "base_url": base,
        "target_model": target,
        "unload_cognition": bool(unload_cognition),
        "unload": unload_results,
        "load": load_result,
    }
    # GPU LEASE: the build's model is now on the iGPU — mark the build as holding the GPU so the
    # brain WAITS its turn instead of firing a generate that would evict this active run
    # (project_exit58_gateway_flap_diagnosis_2026-06-28). Only on a successful load: a failed
    # load means the target never made the GPU, so there is nothing to guard against.
    #
    # AND only when a swap ACTUALLY happened (2026-08-11). ``unload_candidates`` is empty
    # exactly when nothing had to be evicted to seat the target — which is the normal case now
    # that the CLI executor runs the SAME resident model as cognition (qwen35_4b_unsloth, the
    # one serving all four lanes): line ~397 drops the target from its own unload list, so the
    # load is a no-op on an already-resident seat. Leasing there claimed a mutual exclusion that
    # does not exist and cost the operator three chat turns to a canned busy-ack. No eviction,
    # no lease.
    if out["ok"] and unload_candidates:
        try:
            from runtime.gpu_lease import acquire_gpu_for_build

            acquire_gpu_for_build(holder=target)
        except Exception as exc:  # never block the handoff on the lease
            log.warning("gpu lease acquire failed (continuing): %s", exc)
    _emit_model_control(
        "prepare_browser_agent_runtime",
        ok=out["ok"],
        target_model=target,
        unload_cognition=bool(unload_cognition),
    )
    return out


def prepare_dev_pill_runtime() -> dict[str, Any]:
    """Yield Gemma cognition residency to the CLI dev-agent model (jackrong_qwen)."""

    return prepare_browser_agent_runtime(
        target_model=DEFAULT_BROWSER_MODEL,
        unload_cognition=False,
        unload_models=[DEFAULT_COGNITION_MODEL],
    )


def release_browser_agent_runtime(
    *,
    target_model: str | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    """End a CLI / browser-agent session and release its local model/lease.

    The CLI model (``jackrong_qwen``) has NO ``evict_after_use`` in the runtime
    registry, so the runtime will NOT auto-unload it the way it does
    ``cosmos_reason2b`` / ``sd_turbo`` — the substrate MUST. The response seat
    is intentionally not warmed here; the bounded request path loads it on the
    next inference and reports failures there. Idempotent: unloading an
    already-absent model is a harmless no-op."""
    # Cloud route: nothing local was loaded (see prepare_browser_agent_runtime) and the GPU
    # lease was never acquired, so there is nothing local to tear down.
    _route = _cloud_cli_route()
    if _route:
        _skip = _route["provider"]
        _target = _route["model"] or _skip
        _emit_model_control("release_browser_agent_runtime", ok=True, target_model=_target)
        return {"ok": True, "skipped": _skip, "target_model": _target, "unload": {}}

    target = str(target_model or "").strip() or DEFAULT_BROWSER_MODEL
    base = (base_url or resolve_runtime_base_url()).rstrip("/")
    try:
        result = unload_model(target, base_url=base)
    finally:
        # GPU LEASE: release UNCONDITIONALLY after the unload — the GPU is genuinely free for
        # gemma only once jackrong is torn down (releasing earlier would race a queued generate
        # against the in-flight kill). A `finally` guarantees a failed/hung unload can never
        # wedge the brain into permanent waiting. The apply-decision generate runs after this
        # returns, so it is never blocked.
        try:
            from runtime.gpu_lease import release_gpu_for_build

            release_gpu_for_build()
        except Exception as exc:
            log.warning("gpu lease release failed (continuing): %s", exc)
    out = {
        "ok": bool(result.get("ok")),
        "base_url": base,
        "target_model": target,
        "unload": result,
    }
    # Lazy-residency contract: releasing an agent session gives the shared GPU
    # seat back and stops.  The next cognition request already owns bounded
    # on-demand model loading; warming Gemma here immediately before that request
    # performed an avoidable extra cold load and left it resident between turns.
    out["cognition_restore"] = {"ok": True, "deferred": True}
    _emit_model_control(
        "release_browser_agent_runtime",
        ok=out["ok"],
        target_model=target,
    )
    return out


def release_dev_pill_runtime() -> dict[str, Any]:
    """Inverse of ``prepare_dev_pill_runtime``: unload the dev-agent CLI model
    so the next cognition request can claim the shared seat lazily."""
    return release_browser_agent_runtime(target_model=DEFAULT_BROWSER_MODEL)


def _probe_wait_seconds(explicit: float | None = None) -> float:
    """The boot probe's deadline. ``OMEGA_COGNITION_PROBE_WAIT_S`` overrides.

    0 (or negative) restores the single-shot probe — the pre-2026-08-11 shape,
    kept as the lever for anyone who wants a boot that fails instantly on a down
    runtime rather than waiting one out.
    """
    if explicit is not None:
        return max(0.0, float(explicit))
    raw = (os.environ.get("OMEGA_COGNITION_PROBE_WAIT_S") or "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return COGNITION_PROBE_WAIT_S


def _wait_for_runtime(base: str, wait_s: float) -> tuple[bool, float, int]:
    """Poll ``/health`` with backoff until it answers or the deadline passes.

    Returns ``(reachable, elapsed_seconds, attempts)``. Never raises — the
    caller owns the fail-hard decision, because only the caller knows that a
    runtime which never arrived is boot-fatal.
    """
    started = time.monotonic()
    deadline = started + wait_s
    delay = _PROBE_POLL_START_S
    attempts = 0
    announced = False
    while True:
        attempts += 1
        if _probe_runtime_reachable(base):
            return True, time.monotonic() - started, attempts
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, time.monotonic() - started, attempts
        if not announced:
            announced = True
            # ONE line, at the moment waiting begins. A cold boot is a known
            # slow path and the operator watching the journal has to be able to
            # tell "waiting for a cold gateway" from "hung", but a line per poll
            # would bury the boot log in the very case it is meant to explain.
            log.info(
                "cognition runtime at %s not up yet — waiting up to %.0fs "
                "(cold boot is a known slow path: the gateway binds only after "
                "its synchronous GPU/NPU host probe finishes)",
                base,
                wait_s,
            )
        time.sleep(min(delay, remaining))
        delay = min(delay * 2, _PROBE_POLL_CEILING_S)


def probe_cognition_runtime(
    *,
    model: str | None = None,
    base_url: str | None = None,
    wait_s: float | None = None,
) -> dict[str, Any]:
    """Boot gate WITHOUT loading anything: is the runtime reachable — YET?

    THE WAIT (RISK 4(a), operator 2026-08-11). This used to be one HTTP probe
    with no patience at all, and the measured consequence was a cold boot that
    crashed the brain by law: machine up 12:55:48, unit started 12:55:59,
    gateway spawned 12:56:00 and still not answering /health when this probe
    fired at 13:00:40. systemd restarted the brain five seconds later and the
    second attempt succeeded, so the appliance self-healed — but self-healing
    off a crash is the wrong answer to a KNOWN slow path. A cold start is not a
    failure; being unable to tell the two apart was the defect. So the probe now
    polls with backoff to a bounded deadline (``COGNITION_PROBE_WAIT_S``, see
    the measurements there) and hard-fails only when that deadline passes.

    WHAT DOES NOT CHANGE, and it is the important half. Past the deadline this
    still raises ``RuntimeCognitionUnavailable`` and boot still dies: cognition
    genuinely requires a runtime, and proceeding without one would be a mind
    that ticks without being able to think. Waiting is not tolerating.

    NOTHING IS LOADED HERE. The old boot gate loaded gemma at 64k to prove
    cognition could run. That proof is real but it is bought at the worst
    possible moment: the load competes with the graph compile, the embed lane
    indexing, the HUD and the bridge all starting at once, and the operator
    measured severe boot lag from exactly that contention (2026-07-30).

    So boot checks only what is both CHEAP and LIKELY: a down runtime.
    Loadability is not verified here — a model that will not load surfaces on
    the first generate, loudly, via _ensure_model_worker raising the load
    failure (gateway/workers/proxy.py:324). That is a worse place to find out,
    but the trade is deliberate: it is a rare, permanent, config-shaped failure,
    and paying a multi-gigabyte load on every boot to detect it is the wrong
    price. Callers that genuinely need the model warm should call
    ``ensure_cognition_runtime`` instead."""
    model = str(model or DEFAULT_COGNITION_MODEL).strip()
    base = (base_url or resolve_runtime_base_url()).rstrip("/")
    try:
        from runtime import brain_cloud

        if brain_cloud.enabled():
            _emit_model_control(
                "probe_cognition_runtime", ok=True, model=model, skipped="brain_cloud"
            )
            return {"ok": True, "skipped": "brain_cloud", "model": model, "base_url": base}
    except Exception:
        pass
    wait = _probe_wait_seconds(wait_s)
    reachable, waited, attempts = _wait_for_runtime(base, wait)
    if not reachable:
        _emit_model_control(
            "probe_cognition_runtime",
            ok=False,
            model=model,
            waited_s=round(waited, 1),
            attempts=attempts,
        )
        raise RuntimeCognitionUnavailable(
            f"cognition runtime at {base} is not reachable after waiting "
            f"{waited:.0f}s ({attempts} probes) — cannot run cognition"
        )
    if waited >= _PROBE_POLL_START_S:
        # Only when it actually waited. A warm boot answers on probe 1 and
        # should say nothing here — boot.py already logs the reachable line.
        log.info(
            "cognition runtime became reachable after %.1fs (%d probes) — "
            "cold start, not a failure",
            waited,
            attempts,
        )
    _emit_model_control(
        "probe_cognition_runtime",
        ok=True,
        model=model,
        loaded=False,
        waited_s=round(waited, 1),
        attempts=attempts,
    )
    return {
        "ok": True,
        "model": model,
        "base_url": base,
        "loaded": False,
        "waited_s": round(waited, 3),
        "probes": attempts,
    }


def ensure_cognition_runtime(
    *,
    model: str | None = None,
    base_url: str | None = None,
    preferred_ctx: int = COGNITION_CTX_PREFERRED,
    keep_resident: bool = True,
) -> dict[str, Any]:
    """Boot gate: prove the cognition model CAN load, or HARD-FAIL.

    Loads ``model`` at ``preferred_ctx`` (64k by default). If the configured
    context cannot load, raises ``RuntimeCognitionUnavailable``. The caller
    must let that propagate; there is intentionally no smaller context route.

    ``keep_resident=False`` unloads again once the load is proven. That is the
    boot posture under the residency policy (operator 2026-07-30: only DME, the
    harrier embed lane and the intent classifier stay hot; everything else
    dances). The GUARANTEE is what boot needs — a model that will not load is
    cognitive poison and must crash startup rather than surface mid-conversation
    — but the RESIDENCY is not: every request path already loads on demand
    (gateway/workers/proxy.py:312 ``_ensure_model_worker`` calls ``load_model``
    before each generate, with no is-loaded guard), so a warm seat buys only the
    first turn's latency at the cost of holding shared-memory VRAM for the whole
    session."""
    model = str(model or DEFAULT_COGNITION_MODEL).strip()
    if not model:
        raise ValueError("cognition model required")
    base = (base_url or resolve_runtime_base_url()).rstrip("/")
    # Cloud brain seat: cognition runs on the frontier endpoint, so there is no local
    # model to make resident — this explicit verification helper is a no-op (not
    # a hard-fail). Local-brain callers fall through unchanged.
    try:
        from runtime import brain_cloud

        if brain_cloud.enabled():
            _emit_model_control(
                "ensure_cognition_runtime", ok=True, model=model, skipped="brain_cloud"
            )
            return {"ok": True, "skipped": "brain_cloud", "model": model, "base_url": base}
    except Exception:
        pass
    if not _probe_runtime_reachable(base):
        raise RuntimeCognitionUnavailable(
            f"cognition runtime at {base} is not reachable — cannot run cognition"
        )
    result = load_model(model, base_url=base, context_size=preferred_ctx)
    attempt = {
        "context_size": preferred_ctx,
        "ok": bool(result.get("ok")),
        "load": result,
    }
    if result.get("ok"):
        out = {
            "ok": True,
            "model": model,
            "base_url": base,
            "context_size": preferred_ctx,
            "attempts": [attempt],
        }
        if not keep_resident:
            # Proven, now give the VRAM back. Failure to unload is logged and
            # NOT raised: the gate's job was to prove loadability, and a seat
            # left warm is a waste, never a correctness fault.
            released = unload_model(model, base_url=base)
            out["released"] = bool(released.get("ok"))
            if not released.get("ok"):
                log.warning(
                    "cognition verify could not unload %s afterwards (left resident): %s",
                    model,
                    released.get("reason"),
                )
        _emit_model_control(
            "ensure_cognition_runtime",
            ok=True,
            model=model,
            context_size=preferred_ctx,
            kept_resident=keep_resident,
        )
        return out
    reason = str(result.get("error") or result.get("reason") or "load rejected")
    log.warning(
        "cognition model %s would not load at configured ctx=%d: %s",
        model,
        preferred_ctx,
        reason,
    )
    _emit_model_control(
        "ensure_cognition_runtime",
        ok=False,
        model=model,
        context_size=preferred_ctx,
        reason=reason,
    )
    raise RuntimeCognitionUnavailable(
        f"cognition model {model!r} would not load at configured context "
        f"{preferred_ctx} at {base}: {reason} — cannot run cognition"
    )
