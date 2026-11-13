"""Durable operational ledger for proactive ask-to-foldback continuity."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import threading
from typing import Any, Mapping, Optional, Sequence

from pressure_loop.durable_write import append_jsonl_line, write_text_atomic
from schema.proactive_lifecycle import (
    OperatorPolicy,
    ProactiveAsk,
    ProactiveFoldback,
    ProactiveLifecycleEvent,
    ProactiveResult,
    ProactiveThread,
    stable_proactive_id,
)


log = logging.getLogger("omega.pressure_loop.proactive_lifecycle")
_LOCK = threading.RLock()
_EVENTS: list[ProactiveLifecycleEvent] = []
_BY_ID: dict[str, ProactiveLifecycleEvent] = {}
_LOADED = False
_NEEDS_REPAIR = False
DEFAULT_MAX_DISPATCH_ATTEMPTS = 2
_PROJECTION_CACHE: Optional[tuple[Any, ...]] = None
_PROJECTION_EVENT_COUNT = -1


def _ledger_path() -> Path:
    from core.paths import OMEGA_ROOT

    return OMEGA_ROOT / "guardian" / "state" / "proactive_lifecycle.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _semantic(event: ProactiveLifecycleEvent) -> dict[str, Any]:
    return {
        "event_type": event.event_type,
        "thread_id": event.thread_id,
        "episode_id": event.episode_id,
        "payload": event.payload,
    }


def _rewrite_valid_prefix() -> None:
    global _NEEDS_REPAIR
    if not _NEEDS_REPAIR:
        return
    text = "".join(
        json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True) + "\n" for event in _EVENTS
    )
    write_text_atomic(_ledger_path(), text)
    _NEEDS_REPAIR = False


def _quarantine_corrupt_ledger(path: Path, *, reason: str) -> None:
    """Retire an unreadable operational ledger without killing cognition."""
    global _PROJECTION_CACHE, _PROJECTION_EVENT_COUNT
    _EVENTS.clear()
    _BY_ID.clear()
    _PROJECTION_CACHE = None
    _PROJECTION_EVENT_COUNT = -1
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = path.with_name(f"{path.name}.corrupt.{stamp}")
    try:
        path.replace(target)
    except OSError:
        log.exception("failed to quarantine corrupt proactive lifecycle ledger")
    log.error(
        "quarantined proactive lifecycle ledger; starting with empty operational state: %s",
        reason,
    )
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "pressure",
            "proactive_lifecycle_quarantined",
            status="degraded",
            ok=False,
            reason=reason[:200],
            quarantine_path=str(target),
        )
    except Exception:
        pass


def _ensure_loaded() -> None:
    global _LOADED, _NEEDS_REPAIR
    if _LOADED:
        return
    _LOADED = True
    path = _ledger_path()
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        log.error("proactive lifecycle load failed; using empty state: %s", exc)
        return
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
            event = ProactiveLifecycleEvent.from_dict(raw)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            if index == len(lines) - 1:
                _NEEDS_REPAIR = True
                log.warning("ignoring malformed trailing proactive lifecycle record")
                break
            _quarantine_corrupt_ledger(
                path,
                reason=f"malformed record at line {index + 1}: {exc}",
            )
            return
        prior = _BY_ID.get(event.event_id)
        if prior is not None and _semantic(prior) != _semantic(event):
            _quarantine_corrupt_ledger(
                path,
                reason=f"conflicting event {event.event_id!r}",
            )
            return
        if prior is None:
            _EVENTS.append(event)
            _BY_ID[event.event_id] = event


def _append_event(
    event_type: str,
    *,
    identity: str,
    thread_id: str,
    episode_id: str,
    payload: Mapping[str, Any],
) -> ProactiveLifecycleEvent:
    with _LOCK:
        _ensure_loaded()
        event = ProactiveLifecycleEvent(
            event_id=stable_proactive_id(event_type, identity),
            event_type=event_type,
            thread_id=str(thread_id or ""),
            episode_id=str(episode_id or ""),
            timestamp=_now_iso(),
            payload=dict(payload),
        )
        prior = _BY_ID.get(event.event_id)
        if prior is not None:
            if _semantic(prior) != _semantic(event):
                raise ValueError(f"conflicting proactive lifecycle event {event.event_id}")
            return prior
        _rewrite_valid_prefix()
        append_jsonl_line(
            _ledger_path(),
            json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True),
        )
        _EVENTS.append(event)
        _BY_ID[event.event_id] = event
        return event


def _projection() -> tuple[
    dict[str, ProactiveThread],
    dict[str, ProactiveAsk],
    dict[str, OperatorPolicy],
    dict[str, ProactiveResult],
    dict[str, ProactiveFoldback],
]:
    global _PROJECTION_CACHE, _PROJECTION_EVENT_COUNT
    _ensure_loaded()
    if _PROJECTION_CACHE is not None and _PROJECTION_EVENT_COUNT == len(_EVENTS):
        return _PROJECTION_CACHE  # type: ignore[return-value]
    threads: dict[str, ProactiveThread] = {}
    asks: dict[str, ProactiveAsk] = {}
    policies: dict[str, OperatorPolicy] = {}
    results: dict[str, ProactiveResult] = {}
    foldbacks: dict[str, ProactiveFoldback] = {}
    for event in _EVENTS:
        payload = event.payload
        if event.event_type == "ask_created":
            thread = ProactiveThread.from_dict(payload["thread"])
            ask = ProactiveAsk.from_dict(payload["ask"])
            threads[thread.thread_id] = thread
            asks[ask.prompt_id] = ask
            continue
        thread = threads.get(event.thread_id)
        if event.event_type in {"policy_bound", "policy_structured"}:
            policy = OperatorPolicy.from_dict(payload["policy"])
            policies[policy.policy_id] = policy
            if thread is not None:
                threads[event.thread_id] = replace(
                    thread,
                    active_episode_id=event.episode_id or thread.active_episode_id,
                    state="policy_bound",
                    updated_at=event.timestamp,
                    version=thread.version + 1,
                )
            ask = asks.get(policy.origin_prompt_id)
            if ask is not None:
                asks[ask.prompt_id] = replace(ask, status="answered")
        elif event.event_type in {"learning", "applying"} and thread is not None:
            threads[event.thread_id] = replace(
                thread,
                active_episode_id=event.episode_id,
                state=event.event_type,
                updated_at=event.timestamp,
                version=thread.version + 1,
            )
        elif event.event_type == "result_ready":
            result = ProactiveResult.from_dict(payload["result"])
            results[result.result_id] = result
            if thread is not None:
                threads[event.thread_id] = replace(
                    thread,
                    active_episode_id=event.episode_id,
                    state="result_ready",
                    updated_at=event.timestamp,
                    version=thread.version + 1,
                )
        elif event.event_type == "result_presented":
            result_id = str(payload.get("result_id") or "")
            result = results.get(result_id)
            if result is not None:
                results[result_id] = replace(result, status="presented")
            if thread is not None:
                threads[event.thread_id] = replace(
                    thread,
                    active_episode_id=event.episode_id or thread.active_episode_id,
                    state="result_presented",
                    updated_at=event.timestamp,
                    version=thread.version + 1,
                )
        elif event.event_type == "folded":
            foldback = ProactiveFoldback.from_dict(payload["foldback"])
            foldbacks[foldback.foldback_id] = foldback
            result = results.get(foldback.origin_result_id)
            if result is not None:
                results[result.result_id] = replace(result, status="folded")
            if thread is not None:
                threads[event.thread_id] = replace(
                    thread,
                    active_episode_id=event.episode_id or thread.active_episode_id,
                    state="folded",
                    updated_at=event.timestamp,
                    version=thread.version + 1,
                )
        elif (
            event.event_type in {"resolved", "continuing", "dispatch_parked"} and thread is not None
        ):
            threads[event.thread_id] = replace(
                thread,
                active_episode_id=event.episode_id or thread.active_episode_id,
                state=("continuing" if event.event_type == "dispatch_parked" else event.event_type),
                updated_at=event.timestamp,
                version=thread.version + 1,
            )
    projection = (threads, asks, policies, results, foldbacks)
    _PROJECTION_CACHE = projection
    _PROJECTION_EVENT_COUNT = len(_EVENTS)
    return projection


def record_ask(
    *,
    prompt_id: str,
    episode_id: str,
    question: str,
    why: str,
    pressure_winner: str,
    expected_response: str,
    options: Sequence[str],
) -> ProactiveThread:
    thread_id = stable_proactive_id("thread", prompt_id, episode_id)
    now = _now_iso()
    thread = ProactiveThread(
        thread_id=thread_id,
        origin_prompt_id=prompt_id,
        origin_episode_id=episode_id,
        active_episode_id=episode_id,
        state="asked",
        created_at=now,
        updated_at=now,
    )
    ask = ProactiveAsk(
        prompt_id=prompt_id,
        thread_id=thread_id,
        episode_id=episode_id,
        question=question,
        why=str(why or ""),
        pressure_winner=str(pressure_winner or ""),
        expected_response=str(expected_response or ""),
        options=tuple(options or ()),
        status="pending",
    )
    _append_event(
        "ask_created",
        identity=prompt_id,
        thread_id=thread_id,
        episode_id=episode_id,
        payload={"thread": thread.to_dict(), "ask": ask.to_dict()},
    )
    projected = thread_for_prompt(prompt_id)
    if projected is None:
        raise RuntimeError("proactive ask projection failed")
    return projected


def thread_for_prompt(prompt_id: str) -> Optional[ProactiveThread]:
    with _LOCK:
        threads, asks, _policies, _results, _foldbacks = _projection()
        ask = asks.get(str(prompt_id or ""))
        return threads.get(ask.thread_id) if ask is not None else None


def thread_state(thread_id: str) -> Optional[str]:
    with _LOCK:
        threads, *_ = _projection()
        thread = threads.get(str(thread_id or ""))
        return thread.state if thread is not None else None


def bind_operator_policy(
    *, prompt_id: str, answer_episode_id: str, operator_text: str
) -> OperatorPolicy:
    with _LOCK:
        thread = thread_for_prompt(prompt_id)
        if thread is None:
            raise ValueError(f"unknown proactive prompt {prompt_id!r}")
        policy = OperatorPolicy(
            policy_id=stable_proactive_id("policy", thread.thread_id, prompt_id),
            thread_id=thread.thread_id,
            origin_prompt_id=prompt_id,
            origin_episode_id=thread.origin_episode_id,
            answer_episode_id=answer_episode_id or thread.origin_episode_id,
            operator_text=operator_text,
            objective=operator_text,
            constraints=(),
            learning_steps=(),
            requested_demonstrations=(),
            continuation_rule="",
            status="bound",
        )
        _append_event(
            "policy_bound",
            identity=policy.policy_id,
            thread_id=thread.thread_id,
            episode_id=policy.answer_episode_id,
            payload={"policy": policy.to_dict()},
        )
        return active_policy(thread_id=thread.thread_id) or policy


def _find_policy(policy_id: str) -> Optional[OperatorPolicy]:
    _threads, _asks, policies, _results, _foldbacks = _projection()
    return policies.get(str(policy_id or ""))


def structure_operator_policy(
    *,
    policy_id: str,
    objective: str,
    constraints: Sequence[str],
    learning_steps: Sequence[str],
    requested_demonstrations: Sequence[str],
    continuation_rule: str,
) -> OperatorPolicy:
    with _LOCK:
        prior = _find_policy(policy_id)
        if prior is None:
            raise ValueError(f"unknown proactive policy {policy_id!r}")
        policy = replace(
            prior,
            objective=objective,
            constraints=tuple(constraints),
            learning_steps=tuple(learning_steps),
            requested_demonstrations=tuple(requested_demonstrations),
            continuation_rule=str(continuation_rule or ""),
            status="active",
        )
        _append_event(
            "policy_structured",
            identity=policy.policy_id,
            thread_id=policy.thread_id,
            episode_id=policy.answer_episode_id,
            payload={"policy": policy.to_dict()},
        )
        return active_policy(thread_id=policy.thread_id) or policy


def active_policy(*, thread_id: str | None = None) -> Optional[OperatorPolicy]:
    with _LOCK:
        threads, _asks, policies, _results, _foldbacks = _projection()
        candidates = [
            policy
            for policy in policies.values()
            if policy.status in {"bound", "active"}
            and (thread_id is None or policy.thread_id == thread_id)
            and threads.get(policy.thread_id) is not None
            and threads[policy.thread_id].state not in {"resolved"}
        ]
        return candidates[-1] if candidates else None


def _transition(
    thread_id: str,
    *,
    episode_id: str,
    event_type: str,
    allowed_from: frozenset[str],
) -> ProactiveThread:
    with _LOCK:
        state = thread_state(thread_id)
        if state not in allowed_from:
            raise ValueError(f"invalid proactive lifecycle transition {state!r}->{event_type!r}")
        _append_event(
            event_type,
            identity=f"{thread_id}:{event_type}:{episode_id}",
            thread_id=thread_id,
            episode_id=episode_id,
            payload={"state": event_type},
        )
        threads, *_ = _projection()
        return threads[thread_id]


def mark_learning(thread_id: str, *, episode_id: str) -> ProactiveThread:
    # continuing/resolved are re-entry points (operator 2026-07-12): a graded
    # result's answer often carries NEW curriculum — the fold structures it and
    # the same thread learns again rather than minting a parallel conversation.
    return _transition(
        thread_id,
        episode_id=episode_id,
        event_type="learning",
        allowed_from=frozenset({"policy_bound", "learning", "continuing", "resolved"}),
    )


def mark_applying(thread_id: str, *, episode_id: str) -> ProactiveThread:
    return _transition(
        thread_id,
        episode_id=episode_id,
        event_type="applying",
        allowed_from=frozenset({"learning", "continuing", "applying"}),
    )


def pending_dialogue_dispatch() -> Optional[dict[str, Any]]:
    with _LOCK:
        threads, _asks, policies, _results, _foldbacks = _projection()
        applying = [thread for thread in threads.values() if thread.state == "applying"]
        if not applying:
            return None
        # FIFO prevents a newer policy from starving an older ready application.
        thread = applying[0]
        candidates = [p for p in policies.values() if p.thread_id == thread.thread_id]
        if not candidates:
            return None
        policy = candidates[-1]
        dispatch_id = stable_proactive_id(
            "application",
            thread.thread_id,
            policy.policy_id,
            thread.active_episode_id,
        )
        return {
            "kind": "PRESENT_RESULT",
            "dispatch_id": dispatch_id,
            "thread_id": thread.thread_id,
            "origin_prompt_id": policy.origin_prompt_id,
            "policy_id": policy.policy_id,
            "operator_policy": policy.to_dict(),
            "message": policy.objective,
        }


def claim_dialogue_dispatch(
    *, max_attempts: int = DEFAULT_MAX_DISPATCH_ATTEMPTS
) -> Optional[dict[str, Any]]:
    """Claim one bounded presentation attempt or park an exhausted thread."""
    with _LOCK:
        dispatch = pending_dialogue_dispatch()
        if dispatch is None:
            return None
        limit = max(1, int(max_attempts))
        dispatch_id = str(dispatch["dispatch_id"])
        attempts = sum(
            1
            for event in _EVENTS
            if event.event_type == "application_dispatched"
            and str(event.payload.get("dispatch_id") or "") == dispatch_id
        )
        if attempts >= limit:
            _append_event(
                "dispatch_parked",
                identity=dispatch_id,
                thread_id=str(dispatch["thread_id"]),
                episode_id=dispatch_id,
                payload={
                    "dispatch_id": dispatch_id,
                    "attempts": attempts,
                    "state": "continuing",
                    "reason": "dialogue application made no durable progress",
                },
            )
            return {
                **dispatch,
                "kind": "PARKED",
                "attempts": attempts,
                "message": (
                    "I parked this dialogue application after repeated attempts "
                    "without a durable result. The policy remains available for "
                    "a corrective learning pass or operator direction."
                ),
            }
        attempt = attempts + 1
        _append_event(
            "application_dispatched",
            identity=f"{dispatch_id}:{attempt}",
            thread_id=str(dispatch["thread_id"]),
            episode_id=dispatch_id,
            payload={"dispatch_id": dispatch_id, "attempt": attempt},
        )
        return {**dispatch, "attempt": attempt, "max_attempts": limit}


def record_result(result: ProactiveResult | Mapping[str, Any]) -> ProactiveResult:
    value = result if isinstance(result, ProactiveResult) else ProactiveResult.from_dict(result)
    _append_event(
        "result_ready",
        identity=value.result_id,
        thread_id=value.thread_id,
        episode_id=value.episode_id,
        payload={"result": value.to_dict()},
    )
    return result_by_id(value.result_id) or value


def result_by_id(result_id: str) -> Optional[ProactiveResult]:
    with _LOCK:
        _threads, _asks, _policies, results, _foldbacks = _projection()
        return results.get(str(result_id or ""))


def result_for_episode(episode_id: str) -> Optional[ProactiveResult]:
    with _LOCK:
        _threads, _asks, _policies, results, _foldbacks = _projection()
        matches = [r for r in results.values() if r.episode_id == str(episode_id or "")]
        return matches[-1] if matches else None


def mark_result_presented(result_id: str, *, message_id: str) -> Optional[ProactiveResult]:
    with _LOCK:
        result = result_by_id(result_id)
        if result is None:
            return None
        _append_event(
            "result_presented",
            identity=result.result_id,
            thread_id=result.thread_id,
            episode_id=result.episode_id,
            payload={"result_id": result.result_id, "message_id": str(message_id or "")},
        )
        return result_by_id(result.result_id)


def record_foldback(foldback: ProactiveFoldback | Mapping[str, Any]) -> ProactiveFoldback:
    value = (
        foldback
        if isinstance(foldback, ProactiveFoldback)
        else ProactiveFoldback.from_dict(foldback)
    )
    _append_event(
        "folded",
        identity=value.foldback_id,
        thread_id=value.thread_id,
        episode_id=value.episode_id,
        payload={"foldback": value.to_dict()},
    )
    return foldback_for_thread(value.thread_id) or value


def foldback_for_thread(thread_id: str) -> Optional[ProactiveFoldback]:
    with _LOCK:
        _threads, _asks, _policies, _results, foldbacks = _projection()
        matches = [f for f in foldbacks.values() if f.thread_id == str(thread_id or "")]
        return matches[-1] if matches else None


def fold_presented_result(
    result_id: str, *, mastery_delta: float = 0.0
) -> Optional[ProactiveFoldback]:
    """Create one corrective/success fold, then resolve or continue the thread."""
    with _LOCK:
        result = result_by_id(result_id)
        if result is None or result.status not in {"presented", "folded"}:
            return None
        existing = foldback_for_thread(result.thread_id)
        if existing is not None and existing.origin_result_id == result.result_id:
            return existing
        if result.mastery_status == "passed":
            resolution = "resolve"
            durable_rules = tuple(dict.fromkeys((*result.learned, *result.mastery_evidence)))
            corrected_rules: tuple[str, ...] = ()
            unresolved: tuple[str, ...] = ()
        else:
            resolution = "ask_operator" if result.operator_input_needed else "continue"
            durable_rules = ()
            corrected_rules = (result.next_step,)
            unresolved = result.weak_patterns
            mastery_delta = 0.0
        foldback = ProactiveFoldback(
            foldback_id=stable_proactive_id("foldback", result.result_id),
            thread_id=result.thread_id,
            episode_id=result.episode_id,
            origin_result_id=result.result_id,
            mastery_status=result.mastery_status,
            mastery_delta=max(0.0, min(1.0, float(mastery_delta or 0.0))),
            durable_rules=durable_rules,
            corrected_rules=corrected_rules,
            unresolved_gaps=unresolved,
            next_pressure_candidates=(result.next_step,),
            resolution=resolution,
            status="ready",
        )
        recorded = record_foldback(foldback)
        terminal_state = "resolved" if resolution == "resolve" else "continuing"
        _append_event(
            terminal_state,
            identity=result.result_id,
            thread_id=result.thread_id,
            episode_id=result.episode_id,
            payload={
                "result_id": result.result_id,
                "foldback_id": recorded.foldback_id,
                "resolution": resolution,
            },
        )
        return recorded


def _reset_cache_for_tests() -> None:
    global _LOADED, _NEEDS_REPAIR, _PROJECTION_CACHE, _PROJECTION_EVENT_COUNT
    with _LOCK:
        _EVENTS.clear()
        _BY_ID.clear()
        _LOADED = False
        _NEEDS_REPAIR = False
        _PROJECTION_CACHE = None
        _PROJECTION_EVENT_COUNT = -1


__all__ = [
    "active_policy",
    "bind_operator_policy",
    "foldback_for_thread",
    "fold_presented_result",
    "mark_applying",
    "mark_learning",
    "mark_result_presented",
    "claim_dialogue_dispatch",
    "pending_dialogue_dispatch",
    "record_ask",
    "record_foldback",
    "record_result",
    "result_by_id",
    "result_for_episode",
    "structure_operator_policy",
    "thread_for_prompt",
    "thread_state",
]
