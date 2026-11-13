"""Runtime capability broker -- Symphony v0.2 S15.

The cognitive side asks for named capabilities. The local runtime service
owns model residency, model files, and degraded state. This
module reads the runtime health surface through ``runtime.client``
and returns small routeable status records; it never imports runtime
compatibility shims or model-loader packages.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional


class RuntimeCapability(str, Enum):
    VOICE = "voice"
    IMAGE = "image_generation"
    STT = "stt"
    TTS = "tts"
    FACE = "face"
    DOCUMENT_PARSING = "document_parsing"
    CODING_MODEL = "coding_model"
    SPECIALIST_VISION = "specialist_vision"
    SPECIALIST_VIDEO = "specialist_video"


class RuntimeCapabilityState(str, Enum):
    AVAILABLE = "available"
    DEGRADED = "degraded"
    DOWN = "down"


class RuntimeCapabilityRoute(str, Enum):
    PROCEED = "proceed"
    RESPONSE_HOLD = "response_hold"
    ALTERNATE_CAPABILITY = "alternate_capability"


@dataclass(frozen=True)
class CapabilityDeclaration:
    capability: RuntimeCapability
    endpoint: str
    client_method: str
    alternate_capability: Optional[RuntimeCapability] = None


@dataclass(frozen=True)
class RuntimeCapabilityStatus:
    capability: RuntimeCapability
    state: RuntimeCapabilityState
    route: RuntimeCapabilityRoute
    reason: str
    endpoint: str
    client_method: str
    runtime_base_url: str = ""
    alternate_capability: Optional[RuntimeCapability] = None

    @property
    def available(self) -> bool:
        return self.state == RuntimeCapabilityState.AVAILABLE

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability.value,
            "state": self.state.value,
            "route": self.route.value,
            "reason": self.reason,
            "endpoint": self.endpoint,
            "client_method": self.client_method,
            "runtime_base_url": self.runtime_base_url,
            "alternate_capability": (
                self.alternate_capability.value if self.alternate_capability is not None else None
            ),
            "available": self.available,
        }


CAPABILITY_DECLARATIONS: dict[RuntimeCapability, CapabilityDeclaration] = {
    RuntimeCapability.VOICE: CapabilityDeclaration(
        RuntimeCapability.VOICE,
        "/voice/command",
        "voice_command",
    ),
    RuntimeCapability.IMAGE: CapabilityDeclaration(
        RuntimeCapability.IMAGE,
        "/image/generate",
        "image_generate",
    ),
    RuntimeCapability.STT: CapabilityDeclaration(
        RuntimeCapability.STT,
        "/transcribe",
        "transcribe",
    ),
    RuntimeCapability.TTS: CapabilityDeclaration(
        RuntimeCapability.TTS,
        "/synthesise",
        "synthesise",
    ),
    RuntimeCapability.FACE: CapabilityDeclaration(
        RuntimeCapability.FACE,
        "/face/identify",
        "face_identify",
    ),
    RuntimeCapability.DOCUMENT_PARSING: CapabilityDeclaration(
        RuntimeCapability.DOCUMENT_PARSING,
        "/document/extract",
        "document_extract",
    ),
    RuntimeCapability.CODING_MODEL: CapabilityDeclaration(
        RuntimeCapability.CODING_MODEL,
        "/generate",
        "generate_completion",
    ),
    RuntimeCapability.SPECIALIST_VISION: CapabilityDeclaration(
        RuntimeCapability.SPECIALIST_VISION,
        "/vision/describe",
        "vision_describe",
    ),
    RuntimeCapability.SPECIALIST_VIDEO: CapabilityDeclaration(
        RuntimeCapability.SPECIALIST_VIDEO,
        "/video/describe",
        "video_describe",
        alternate_capability=RuntimeCapability.SPECIALIST_VISION,
    ),
}


def brokered_capabilities() -> tuple[RuntimeCapability, ...]:
    return tuple(RuntimeCapability)


def _coerce_capability(capability: RuntimeCapability | str) -> RuntimeCapability:
    if isinstance(capability, RuntimeCapability):
        return capability
    return RuntimeCapability(str(capability))


def _runtime_base_url(client: object) -> str:
    raw = getattr(client, "base_url", "")
    if callable(raw):
        try:
            raw = raw()
        except Exception:
            return ""
    return str(raw or "")


def _read_health_body(client: object) -> Optional[Mapping[str, Any]]:
    try:
        health_body = getattr(client, "health_body")()
    except Exception:
        return None
    return health_body if isinstance(health_body, Mapping) else None


def _runtime_is_healthy(health_body: Optional[Mapping[str, Any]]) -> bool:
    if not isinstance(health_body, Mapping):
        return False
    return str(health_body.get("status") or "").lower() == "ok"


def _capability_flag(
    health_body: Optional[Mapping[str, Any]],
    capability: RuntimeCapability,
) -> Optional[bool]:
    if not isinstance(health_body, Mapping):
        return None
    capabilities = health_body.get("capabilities")
    if isinstance(capabilities, Mapping):
        raw = capabilities.get(capability.value)
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, Mapping):
            status = str(raw.get("status") or raw.get("state") or "").lower()
            if status in {"ok", "ready", "available", "live"}:
                return True
            if status in {"down", "degraded", "disabled", "missing", "unavailable"}:
                return False
    raw_capability = health_body.get(capability.value)
    if isinstance(raw_capability, bool):
        return raw_capability
    if isinstance(raw_capability, Mapping):
        status = str(raw_capability.get("status") or raw_capability.get("state") or "").lower()
        if status in {"ok", "ready", "available", "live"}:
            return True
        if status in {"down", "degraded", "disabled", "missing", "unavailable"}:
            return False
    return None


def _status(
    declaration: CapabilityDeclaration,
    state: RuntimeCapabilityState,
    route: RuntimeCapabilityRoute,
    reason: str,
    *,
    client: object,
) -> RuntimeCapabilityStatus:
    return RuntimeCapabilityStatus(
        capability=declaration.capability,
        state=state,
        route=route,
        reason=reason,
        endpoint=declaration.endpoint,
        client_method=declaration.client_method,
        runtime_base_url=_runtime_base_url(client),
        alternate_capability=declaration.alternate_capability,
    )


def _emit_capability_evaluation(status: RuntimeCapabilityStatus) -> None:
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "runtime",
            "capability_evaluate",
            capability=status.capability.value,
            state=status.state.value,
            route=status.route.value,
            reason=status.reason,
            available=status.available,
        )
    except Exception:
        pass


def _evaluate_runtime_capability(
    capability: RuntimeCapability | str,
    *,
    client: object | None = None,
    health_body: Optional[Mapping[str, Any]] = None,
    emit_evaluation: bool = True,
) -> RuntimeCapabilityStatus:
    selected = _coerce_capability(capability)
    declaration = CAPABILITY_DECLARATIONS[selected]
    if selected in {RuntimeCapability.FACE, RuntimeCapability.SPECIALIST_VISION}:
        # Face inference is brain-local CPU ONNX. General vision follows the
        # selected local/cloud mind seat. Neither capability participates in
        # DME/NPU placement.
        try:
            if selected is RuntimeCapability.FACE:
                from runtime.face.recognition import face_status

                local = face_status()
                available = bool(local.get("face_ready"))
                endpoint = "brain-local-cpu"
                reason = "brain_local_cpu_ready" if available else str(
                    local.get("reason") or "brain_local_face_models_unavailable"
                )
            else:
                from runtime.vision import status as vision_status

                local = vision_status()
                available = bool(local.get("ok"))
                endpoint = f"brain-{local.get('backend') or 'unknown'}-multimodal"
                reason = "vision_seat_configured" if available else str(
                    local.get("reason") or "vision_seat_unavailable"
                )
            state = (
                RuntimeCapabilityState.AVAILABLE
                if available
                else RuntimeCapabilityState.DOWN
            )
        except Exception as exc:
            state = RuntimeCapabilityState.DOWN
            reason = type(exc).__name__
            endpoint = "brain-local"
        result = RuntimeCapabilityStatus(
            capability=declaration.capability,
            state=state,
            route=(
                RuntimeCapabilityRoute.PROCEED
                if state is RuntimeCapabilityState.AVAILABLE
                else RuntimeCapabilityRoute.RESPONSE_HOLD
            ),
            reason=reason,
            endpoint=endpoint,
            client_method=declaration.client_method,
            runtime_base_url=endpoint,
            alternate_capability=declaration.alternate_capability,
        )
        if emit_evaluation:
            _emit_capability_evaluation(result)
        return result
    if client is None:
        from runtime.client import get_runtime_client

        client = get_runtime_client()

    resolved_health = health_body if health_body is not None else _read_health_body(client)
    if not _runtime_is_healthy(resolved_health):
        result = _status(
            declaration,
            RuntimeCapabilityState.DOWN,
            RuntimeCapabilityRoute.RESPONSE_HOLD,
            "runtime_unreachable_or_unhealthy",
            client=client,
        )
        if emit_evaluation:
            _emit_capability_evaluation(result)
        return result
    if not hasattr(client, declaration.client_method):
        route = (
            RuntimeCapabilityRoute.ALTERNATE_CAPABILITY
            if declaration.alternate_capability is not None
            else RuntimeCapabilityRoute.RESPONSE_HOLD
        )
        result = _status(
            declaration,
            RuntimeCapabilityState.DEGRADED,
            route,
            "client_method_missing",
            client=client,
        )
        if emit_evaluation:
            _emit_capability_evaluation(result)
        return result
    if _capability_flag(resolved_health, selected) is False:
        route = (
            RuntimeCapabilityRoute.ALTERNATE_CAPABILITY
            if declaration.alternate_capability is not None
            else RuntimeCapabilityRoute.RESPONSE_HOLD
        )
        result = _status(
            declaration,
            RuntimeCapabilityState.DEGRADED,
            route,
            "runtime_reported_capability_unavailable",
            client=client,
        )
        if emit_evaluation:
            _emit_capability_evaluation(result)
        return result
    result = _status(
        declaration,
        RuntimeCapabilityState.AVAILABLE,
        RuntimeCapabilityRoute.PROCEED,
        "runtime_healthy_client_method_present",
        client=client,
    )
    if emit_evaluation:
        _emit_capability_evaluation(result)
    return result


def check_runtime_capability(
    capability: RuntimeCapability | str,
    *,
    client: object | None = None,
    health_body: Optional[Mapping[str, Any]] = None,
    cycle_id: str = "",
    emit: bool = True,
) -> RuntimeCapabilityStatus:
    """Return an honest, routeable status for one runtime capability.

    ``DOWN`` means the runtime health surface was unreachable or not ok.
    ``DEGRADED`` means the runtime is reachable but either the client
    lacks the broker method or the runtime explicitly reported that the
    capability is unavailable. ``AVAILABLE`` means the runtime is up and
    the broker has an HTTP method for that capability.
    """
    selected = _coerce_capability(capability)
    if not emit:
        return _evaluate_runtime_capability(
            selected,
            client=client,
            health_body=health_body,
            emit_evaluation=False,
        )

    from guardian.terminal_feedback import phase_span

    with phase_span(
        "broker", "capability_check", capability=str(selected.value), cycle_id=cycle_id
    ) as span:
        result = _evaluate_runtime_capability(
            selected,
            client=client,
            health_body=health_body,
        )
        span.set(
            state=result.state.value,
            route=result.route.value,
            reason=result.reason,
            available=result.available,
        )
        return result


# =====================================================================
# Phase 2 §P2-S18 — single boundary helper for runtime operations.
#
# Every product runtime operation (STT/TTS, autonomous generate, Omega
# evaluate, Omega tool synthesis) must call `preflight_runtime_operation`
# BEFORE invoking the runtime. The helper returns a routeable decision
# carrying the broker status + a compact receipt for episode/diagnostic
# attachment. Callers branch on `decision.proceed`:
#   - True  → call the runtime, record the receipt on the operation
#             surface (episode.auto / context.evaluation_report / etc.)
#   - False → do NOT call the runtime. Record the receipt as
#             operational hold / degraded truth. Never fall through to
#             Guardian tool authority or hidden tool creation.
#
# The helper is pure except for the injected broker call:
# `check_runtime_capability` can be called with `client=<spy>` or
# `health_body=<dict>` so tests drive it deterministically.
# =====================================================================


@dataclass(frozen=True)
class RuntimeOperationReceipt:
    """Compact, episode-attachable record of one preflight decision."""

    capability: str  # e.g. "coding_model" / "stt" / "tts"
    operation: str  # e.g. "autonomous_generate" / "omega_tool_synthesis"
    route: str  # "proceed" / "response_hold" / "alternate_capability"
    state: str  # "available" / "degraded" / "down"
    reason: str  # broker reason string
    cycle_id: str = ""
    alternate_capability: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "operation": self.operation,
            "route": self.route,
            "state": self.state,
            "reason": self.reason,
            "cycle_id": self.cycle_id,
            "alternate_capability": self.alternate_capability,
        }


@dataclass(frozen=True)
class RuntimeOperationDecision:
    """Outcome of `preflight_runtime_operation`."""

    proceed: bool
    route: RuntimeCapabilityRoute
    capability_status: RuntimeCapabilityStatus
    receipt: RuntimeOperationReceipt

    @property
    def response_hold(self) -> bool:
        return self.route == RuntimeCapabilityRoute.RESPONSE_HOLD

    @property
    def alternate_capability(self) -> bool:
        return self.route == RuntimeCapabilityRoute.ALTERNATE_CAPABILITY


def preflight_runtime_operation(
    capability: RuntimeCapability | str,
    *,
    operation: str,
    cycle_id: str = "",
    client: object | None = None,
    health_body: Optional[Mapping[str, Any]] = None,
) -> RuntimeOperationDecision:
    """Preflight one runtime operation against the brokered capability
    health surface. Returns a routeable decision + compact receipt.

    `operation` is a short string the caller uses to tag the receipt
    (e.g. ``"autonomous_generate"``, ``"omega_tool_synthesis"``,
    ``"stt_transcribe"``). `cycle_id` lets the caller correlate the
    receipt with the active cycle / episode if any.

    Test seam: pass `client=<spy>` or `health_body=<dict>` to drive the
    decision without touching runtime.client.

    Phase B-2 (logging coverage): every preflight call emits a
    heartbeat to `feedback_audit.jsonl` via emit_phase. PROCEED is
    status=ok; RESPONSE_HOLD / ALTERNATE_CAPABILITY are status=
    degraded. This gives the remote debug surface coverage of EVERY
    brokered call site (STT/TTS/voice/autonomous/omega/reply_evaluator/
    brain_voice_box) without each caller needing to remember to
    emit. The emit is best-effort — a feedback_audit failure must
    not affect preflight semantics.
    """
    from guardian.terminal_feedback import phase_span

    with phase_span(
        "broker", str(operation or "preflight"), cycle_id=cycle_id, capability=str(capability)
    ) as span:
        status = check_runtime_capability(
            capability,
            client=client,
            health_body=health_body,
        )
        decision = RuntimeOperationDecision(
            proceed=(status.route == RuntimeCapabilityRoute.PROCEED),
            route=status.route,
            capability_status=status,
            receipt=RuntimeOperationReceipt(
                capability=status.capability.value,
                operation=str(operation or ""),
                route=status.route.value,
                state=status.state.value,
                reason=status.reason,
                cycle_id=str(cycle_id or ""),
                alternate_capability=(
                    status.alternate_capability.value
                    if status.alternate_capability is not None
                    else ""
                ),
            ),
        )
        span.set(
            proceed=decision.proceed,
            route=decision.receipt.route,
            state=decision.receipt.state,
            reason=decision.receipt.reason,
            alternate_capability=decision.receipt.alternate_capability or None,
        )
        return decision


def runtime_capability_snapshot(
    *,
    client: object | None = None,
) -> dict[RuntimeCapability, RuntimeCapabilityStatus]:
    if client is None:
        from runtime.client import get_runtime_client

        client = get_runtime_client()
    health_body = _read_health_body(client)
    # A failed probe returns None. Passing that through would make each
    # per-capability check treat health as "unset" and re-fetch /health
    # (11 capabilities × health timeout ≈ HUD poll freeze).
    if not _runtime_is_healthy(health_body):
        health_body = health_body if isinstance(health_body, Mapping) else {}
    return {
        capability: check_runtime_capability(
            capability,
            client=client,
            health_body=health_body,
            emit=False,
        )
        for capability in brokered_capabilities()
    }


def runtime_capability_snapshot_dict(
    *,
    client: object | None = None,
) -> dict[str, dict[str, Any]]:
    return {
        capability.value: status.to_dict()
        for capability, status in runtime_capability_snapshot(client=client).items()
    }


__all__ = [
    "CAPABILITY_DECLARATIONS",
    "CapabilityDeclaration",
    "RuntimeCapability",
    "RuntimeCapabilityRoute",
    "RuntimeCapabilityState",
    "RuntimeCapabilityStatus",
    "RuntimeOperationDecision",
    "RuntimeOperationReceipt",
    "brokered_capabilities",
    "check_runtime_capability",
    "preflight_runtime_operation",
    "runtime_capability_snapshot",
    "runtime_capability_snapshot_dict",
]
