"""Content-free plugin permission decisions carried by the one operator queue.

This module is transport only.  It never approves, cancels, consumes authority,
or invokes a provider.  A read-only resolver projects the exact pending request
lineage from the permission service; the durable operator queue then owns that
projection until pressure admits it to a continuation episode.  AUTO must call
``plugin_permission_continuation_from_contributor`` again immediately before it
handles the decision so stale permission state fails before dispatch.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal

from pressure_loop.contributors import Contributor, make_plugin_permission_decision_contributor

_OPAQUE = re.compile(r"[0-9a-f]{32,128}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_LINEAGE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_DECISIONS = frozenset({"allow", "cancel"})

_METADATA_KEYS = frozenset(
    {
        "request_id",
        "expected_revision",
        "decision",
        "cycle_id",
        "prior_episode_id",
        "task_id",
        "plan_id",
        "plan_digest",
        "arrival_seq",
        "directive_intent",
        "ingress_class",
    }
)
_QUEUE_STAMP_KEYS = frozenset()


def _opaque(value: object, *, error: str) -> str:
    if type(value) is not str or _OPAQUE.fullmatch(value) is None:
        raise ValueError(error)
    return value


def _digest(value: object, *, error: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise ValueError(error)
    return value


def _lineage_id(value: object, *, error: str) -> str:
    if type(value) is not str or _LINEAGE_ID.fullmatch(value) is None:
        raise ValueError(error)
    return value


@dataclass(frozen=True, slots=True)
class PluginPermissionDecision:
    request_id: str
    expected_revision: str
    decision: Literal["allow", "cancel"]

    def __post_init__(self) -> None:
        error = "plugin_permission_decision_invalid"
        object.__setattr__(self, "request_id", _opaque(self.request_id, error=error))
        object.__setattr__(
            self,
            "expected_revision",
            _digest(self.expected_revision, error=error),
        )
        if type(self.decision) is not str or self.decision not in _DECISIONS:
            raise ValueError(error)


@dataclass(frozen=True, slots=True)
class PluginPermissionLineage:
    prior_episode_id: str
    task_id: str
    plan_id: str
    plan_digest: str

    def __post_init__(self) -> None:
        error = "plugin_permission_lineage_invalid"
        object.__setattr__(
            self,
            "prior_episode_id",
            _lineage_id(self.prior_episode_id, error=error),
        )
        object.__setattr__(self, "task_id", _lineage_id(self.task_id, error=error))
        object.__setattr__(self, "plan_id", _digest(self.plan_id, error=error))
        object.__setattr__(
            self,
            "plan_digest",
            _digest(self.plan_digest, error=error),
        )


@dataclass(frozen=True, slots=True)
class PluginPermissionContinuation:
    decision: PluginPermissionDecision
    lineage: PluginPermissionLineage
    arrival_seq: int

    def __post_init__(self) -> None:
        if (
            type(self.decision) is not PluginPermissionDecision
            or type(self.lineage) is not PluginPermissionLineage
            or type(self.arrival_seq) is not int
            or self.arrival_seq <= 0
        ):
            raise ValueError("plugin_permission_continuation_invalid")


LineageResolver = Callable[[str, str], PluginPermissionLineage]
_RESOLVER_LOCK = threading.RLock()
_LINEAGE_RESOLVER: LineageResolver | None = None


def configure_plugin_permission_lineage_resolver(resolver: LineageResolver) -> None:
    """Wire the permission service's read-only exact-lineage projection."""
    if not callable(resolver):
        raise ValueError("plugin_permission_lineage_resolver_invalid")
    global _LINEAGE_RESOLVER
    with _RESOLVER_LOCK:
        _LINEAGE_RESOLVER = resolver


def _configured_resolver() -> LineageResolver:
    global _LINEAGE_RESOLVER
    with _RESOLVER_LOCK:
        resolver = _LINEAGE_RESOLVER
    if resolver is None:
        from mcp_platform.plugin_permissions import get_default_plugin_permission_store

        store = get_default_plugin_permission_store()

        def durable_resolver(request_id: str, expected_revision: str) -> PluginPermissionLineage:
            current = store.resolve_continuation_lineage(request_id, expected_revision)
            return PluginPermissionLineage(
                prior_episode_id=current.prior_episode_id,
                task_id=current.task_id,
                plan_id=current.plan_id,
                plan_digest=current.plan_digest,
            )

        with _RESOLVER_LOCK:
            if _LINEAGE_RESOLVER is None:
                _LINEAGE_RESOLVER = durable_resolver
            resolver = _LINEAGE_RESOLVER
    return resolver


def _reset_lineage_resolver_for_tests() -> None:
    global _LINEAGE_RESOLVER
    with _RESOLVER_LOCK:
        _LINEAGE_RESOLVER = None


def _resolve_lineage(
    decision: PluginPermissionDecision,
    resolver: LineageResolver,
) -> PluginPermissionLineage:
    lineage = resolver(decision.request_id, decision.expected_revision)
    if type(lineage) is not PluginPermissionLineage:
        raise ValueError("plugin_permission_lineage_invalid")
    return lineage


def _metadata(continuation: PluginPermissionContinuation) -> dict[str, object]:
    decision = continuation.decision
    lineage = continuation.lineage
    return {
        "request_id": decision.request_id,
        "expected_revision": decision.expected_revision,
        "decision": decision.decision,
        "cycle_id": f"plugin-permission:{decision.request_id}",
        "prior_episode_id": lineage.prior_episode_id,
        "task_id": lineage.task_id,
        "plan_id": lineage.plan_id,
        "plan_digest": lineage.plan_digest,
        "arrival_seq": continuation.arrival_seq,
        "directive_intent": "PLUGIN_OPERATION",
        "ingress_class": "authority_request",
    }


def queued_plugin_permission_continuation_from_contributor(
    contributor: Contributor,
) -> PluginPermissionContinuation:
    """Parse the durable queue envelope without treating it as current authority."""
    if (
        type(contributor) is not Contributor
        or contributor.source != "operator"
        or contributor.type != "plugin_permission_decision"
        or contributor.weight != 1.0
        or type(contributor.metadata) is not dict
        or set(contributor.metadata) - _QUEUE_STAMP_KEYS != _METADATA_KEYS
    ):
        raise ValueError("plugin_permission_continuation_invalid")
    metadata = contributor.metadata
    if (
        metadata.get("directive_intent") != "PLUGIN_OPERATION"
        or metadata.get("ingress_class") != "authority_request"
    ):
        raise ValueError("plugin_permission_continuation_invalid")
    decision = PluginPermissionDecision(
        request_id=metadata.get("request_id"),
        expected_revision=metadata.get("expected_revision"),
        decision=metadata.get("decision"),
    )
    lineage = PluginPermissionLineage(
        prior_episode_id=metadata.get("prior_episode_id"),
        task_id=metadata.get("task_id"),
        plan_id=metadata.get("plan_id"),
        plan_digest=metadata.get("plan_digest"),
    )
    continuation = PluginPermissionContinuation(
        decision=decision,
        lineage=lineage,
        arrival_seq=metadata.get("arrival_seq"),
    )
    if metadata.get("cycle_id") != f"plugin-permission:{decision.request_id}":
        raise ValueError("plugin_permission_continuation_invalid")
    return continuation


def plugin_permission_contributor_from_metadata(metadata: Mapping[str, object]) -> Contributor:
    """Rebuild and validate one persisted permission-decision contributor."""
    if type(metadata) is not dict:
        raise ValueError("plugin_permission_continuation_invalid")
    if not _METADATA_KEYS <= set(metadata):
        raise ValueError("plugin_permission_continuation_invalid")
    candidate = make_plugin_permission_decision_contributor(
        {key: metadata[key] for key in _METADATA_KEYS}
    )
    continuation = queued_plugin_permission_continuation_from_contributor(candidate)
    return make_plugin_permission_decision_contributor(_metadata(continuation))


def plugin_permission_continuation_from_contributor(
    contributor: Contributor,
    *,
    lineage_resolver: LineageResolver | None = None,
) -> PluginPermissionContinuation:
    """Revalidate queued lineage against the permission service for AUTO handoff."""
    queued = queued_plugin_permission_continuation_from_contributor(contributor)
    resolver = lineage_resolver or _configured_resolver()
    current = _resolve_lineage(queued.decision, resolver)
    if current != queued.lineage:
        raise ValueError("plugin_permission_lineage_conflict")
    return queued


def enqueue_plugin_permission_decision(
    decision: PluginPermissionDecision,
    *,
    arrival_seq: int,
) -> bool:
    """Durably enqueue one structured Allow/Cancel decision; perform no action."""
    if type(decision) is not PluginPermissionDecision:
        raise ValueError("plugin_permission_decision_invalid")
    # Import locally so the queue remains the only converging point without a
    # module-import cycle between persistence and its specialized envelope.
    from pressure_loop.cognition_queue import (
        abandon_arrival_reservation,
        enqueue_input,
        reserve_plugin_permission_arrival,
    )

    try:
        reserve_plugin_permission_arrival(
            decision.request_id,
            decision.expected_revision,
            arrival_seq,
        )
        lineage = _resolve_lineage(decision, _configured_resolver())
        continuation = PluginPermissionContinuation(
            decision=decision,
            lineage=lineage,
            arrival_seq=arrival_seq,
        )
        contributor = make_plugin_permission_decision_contributor(_metadata(continuation))
        return enqueue_input(contributor, operator_activity=True)
    except BaseException as exc:
        try:
            abandon_arrival_reservation(
                arrival_seq,
                request_id=decision.request_id,
                expected_revision=decision.expected_revision,
            )
        except BaseException as cleanup_error:
            exc.add_note(f"plugin_permission_arrival_cleanup_failed={cleanup_error!r}")
        raise


__all__ = [
    "PluginPermissionContinuation",
    "PluginPermissionDecision",
    "PluginPermissionLineage",
    "configure_plugin_permission_lineage_resolver",
    "enqueue_plugin_permission_decision",
    "plugin_permission_continuation_from_contributor",
    "queued_plugin_permission_continuation_from_contributor",
]
