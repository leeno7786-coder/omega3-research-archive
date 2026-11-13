"""Idle-wake governor — an idle TIMER, not a per-tick poll.

Operator design 2026-07-09 (the autonomy rhythm, refined): the pressure
loop no longer ticks every second. It runs an idle TIMER instead:

  * The idle clock is reset ONLY by USER (operator) input — nothing
    else. Autonomous episodes, DeepSleep consolidation, and the wake
    firing itself do NOT reset it. The clock measures "quiet since the
    operator last spoke."
  * When the clock passes the idle window (default 300s / 5 min) AND
    nothing is active (no episode in flight, no pending build), the
    loop wakes the mind: `fire_awakening` fires the gemma model with the
    awaken prompt, which reads the whole mastery map and decides ONE
    action (LEARN a gap / EXECUTE a learned-but-unapplied topic) and
    records it as real pressure.
  * ONCE WOKEN, the mind reignites pressure continually on its own —
    for as long as it wants — via the existing per-episode Omega
    pressure authority. The wake does NOT reset the timer, so the mind
    is free to chain autonomous work indefinitely; only operator input
    interrupts/reprioritises it. The mind RESTS on its own time, when
    IT chooses to go idle. DeepSleep does not force a rest.
  * The wake-KICK itself is throttled (`_last_wake_attempt_ts`) so a
    persistently-idle mind is re-stimulated at most once per idle
    window — the throttle bounds the gemma kick, never the mind's own
    chaining.

Connector scope does not own this governor's cadence.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Optional

log = logging.getLogger("omega.pressure.idle_wake")

DEFAULT_IDLE_WAKE_SECONDS = 300.0  # 5 min quiet (operator 2026-07-08, was 900)


def governor_enabled() -> bool:
    """Production default ON. Tests run with OMEGA_IDLE_WAKE_ENABLED=0
    (tests/conftest.py) so the thousands of evaluate() tests keep their
    always-awake world; the governor's own tests enable it explicitly.
    The durable OPERATOR toggle (HUD cockpit button, 2026-07-18) gates it
    too — both must be on for idle autonomy to fire."""
    if os.environ.get("OMEGA_IDLE_WAKE_ENABLED", "1").strip() == "0":
        return False
    return operator_idle_learning_enabled()


def _toggle_path():
    from core.paths import GUARDIAN_STATE_ROOT

    return GUARDIAN_STATE_ROOT / "idle_learning.json"


def operator_idle_learning_enabled() -> bool:
    """Durable operator toggle for the idle-learning timer (left cockpit
    button). Missing or corrupt state reads as OFF so a fresh installation
    cannot start network-dependent learning before connectivity is configured.
    An explicit operator opt-in remains durable and applies on the next tick."""
    import json

    try:
        data = json.loads(_toggle_path().read_text(encoding="utf-8"))
        return bool(data.get("enabled", False)) if isinstance(data, dict) else False
    except (OSError, ValueError):
        return False


def set_operator_idle_learning(enabled: bool) -> dict:
    """Persist the operator toggle (atomic tmp+replace). Returns the state."""
    import json

    path = _toggle_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"enabled": bool(enabled)}
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, path)
    log.info("idle learning %s by operator toggle", "ENABLED" if enabled else "PAUSED")
    return payload


_last_activity_ts: float = time.time()  # reset ONLY by user (operator) input
_last_activity_kind: str = "boot"
_last_wake_attempt_ts: float = 0.0  # throttles the gemma wake-kick, NOT the mind


def idle_wake_seconds() -> float:
    return DEFAULT_IDLE_WAKE_SECONDS


def record_activity(kind: str, now: Optional[float] = None) -> None:
    """Reset the idle clock. Operator design 2026-07-09: ONLY user (operator)
    input calls this on the live path — autonomous episodes, DeepSleep, and the
    wake firing itself deliberately do NOT, so the clock measures "quiet since the
    operator last spoke" and the mind can run itself indefinitely between turns."""
    global _last_activity_ts, _last_activity_kind
    _last_activity_ts = time.time() if now is None else float(now)
    _last_activity_kind = str(kind or "unknown")


def _anything_active() -> bool:
    """Ongoing work holds the idle clock — never wake into a busy system."""
    try:
        from runtime.gpu_lease import gpu_held_by_build

        if gpu_held_by_build():
            return True
    except Exception:
        pass
    return False


def is_resting(now: Optional[float] = None) -> bool:
    """True while the quiet period is still running (suppress autonomous
    ignition). Activity in the system counts as rest-with-the-clock-held."""
    t = time.time() if now is None else float(now)
    if _anything_active():
        return True
    return (t - _last_activity_ts) < idle_wake_seconds()


def wake_due(now: Optional[float] = None) -> bool:
    t = time.time() if now is None else float(now)
    if _anything_active():
        return False
    return (t - _last_activity_ts) >= idle_wake_seconds()


# Re-check cadence while work is genuinely in flight (pending build / GPU held):
# short enough to resume promptly, long enough to never spam the terminal.
_ACTIVE_RECHECK_SECONDS = 5.0


def wake_attempt_allowed(now: Optional[float] = None) -> bool:
    """Throttle the gemma wake-KICK: at most one attempt per idle window. Bounds a
    persistently-idle mind's re-stimulation WITHOUT touching the mind's own chaining
    (that rides its own pressure and is never throttled here)."""
    t = time.time() if now is None else float(now)
    return (t - _last_wake_attempt_ts) >= idle_wake_seconds()


def seconds_until_wake_attempt(now: Optional[float] = None) -> float:
    """How long the loop should sleep before the wake CAN next fire: the max of the
    time until the idle window expires (since user input) and the throttle window
    (since the last wake-kick). 0 means "fire now". A short recheck while active so a
    pending build resumes promptly. Interruptible — operator input wakes the loop."""
    t = time.time() if now is None else float(now)
    if _anything_active():
        return _ACTIVE_RECHECK_SECONDS
    window = idle_wake_seconds()
    due_in = max(0.0, window - (t - _last_activity_ts))  # since user input
    throttle_in = max(0.0, window - (t - _last_wake_attempt_ts))  # since last wake-kick
    return max(due_in, throttle_in)


# How long the loop may rest while idle autonomy is OFF. With the governor off
# the wake can never fire, so "when may the wake next fire" has no answer and
# must never be used as a sleep bound: `seconds_until_wake_attempt` reads 0.0
# the moment the idle window passes, because nothing ever stamps the throttle.
# A zero-second wait is a hot spin, not a rest.
#
# Live 2026-09-07: a fresh install (idle learning off by default) sat quiet for
# five minutes, then ran the pressure tick ~60 times a SECOND and buried the
# operator's terminal in gather/decay/summary rows until it was killed.
#
# The loop still rests on EVENTS — operator input, an episode closing, a job
# admitting all call `notify_work` and wake it instantly. This bound only
# guarantees a periodic pass for pressure recorded by a path that does not
# signal the loop, so a quiet mind costs one pass a minute rather than sixty a
# second.
IDLE_RECHECK_SECONDS_GOVERNOR_OFF = 60.0


def seconds_until_idle_recheck(now: Optional[float] = None) -> float:
    """How long the loop may rest while idle autonomy is OFF. Never zero.

    Distinct from :func:`seconds_until_wake_attempt`, which answers a question
    only the governor-ON path can ask.
    """
    if _anything_active():
        return _ACTIVE_RECHECK_SECONDS
    return IDLE_RECHECK_SECONDS_GOVERNOR_OFF


_AWAKEN_SYSTEM = (
    "You are Omega, waking after a stretch of quiet with nothing queued and no "
    "operator input. This is your moment to decide what to pursue next — from "
    "dead-stop to self-directed action. Below is your whole memory: every topic "
    "you have, how far you have LEARNED it (knowledge, 0-1) and how far you have "
    "APPLIED it by actually building/executing (application, 0-1), grouped by the "
    "categories your memory formed. Choose exactly ONE next action that raises a "
    "real mastery:\n"
    "- LEARN a topic you have NOT yet learned (low knowledge) — to raise knowledge mastery.\n"
    "- EXECUTE a topic you HAVE learned but have NOT applied (high knowledge, low "
    "application) — to raise application mastery by building/practising it. A topic is "
    "only READY to execute at learned >= 0.5; below that, choose LEARN for it instead.\n"
    "Do not reflect, do not idle while any gap remains. Output ONLY one JSON object."
)


def _mastery_map_text(records: dict, cap: int = 30) -> str:
    rows = sorted(
        records.values(),
        key=lambda r: (
            float(getattr(r, "knowledge_mastery", 0.0)),
            float(getattr(r, "application_mastery", 0.0)),
        ),
    )
    lines = []
    for r in rows[:cap]:
        name = str(getattr(r, "domain_name", "") or getattr(r, "domain_id", ""))
        parent = str(getattr(r, "parent", "") or "").strip()
        k = float(getattr(r, "knowledge_mastery", 0.0))
        a = float(getattr(r, "application_mastery", 0.0))
        t = float(getattr(r, "target_mastery", 0.7))
        cat = f" [{parent}]" if parent else ""
        lines.append(f"- {name}{cat}: learned {k:.2f}, applied {a:.2f} (target {t:.2f})")
    return "\n".join(lines)


def _parse_awaken_decision(text: str) -> Optional[dict]:
    """Extract {action, domain, level, reasoning} from the model output.
    action is LEARN or EXECUTE. Uses the shared repair-tolerant extractor
    (strips <think> blocks, balances brackets). Failure-soft -> None."""
    try:
        from omega_loop._json_extract import extract_json_object

        obj = extract_json_object(text)
    except Exception:
        obj = None
    if not isinstance(obj, dict):
        return None
    action = str(obj.get("action") or "").strip().upper()
    domain = str(obj.get("domain") or "").strip()
    # The mastery map renders rows as "- {name} [{parent}]: ..." and the model
    # sometimes copies the WHOLE label as the domain (live 2026-07-10:
    # "Data Structures in Python (...) [python]" -> slug "...-tuples-python",
    # a PHANTOM domain id that fragments mastery crediting). Strip the
    # trailing [category] suffix deterministically.
    domain = re.sub(r"\s*\[[^\]]*\]\s*$", "", domain).strip()
    if action not in ("LEARN", "EXECUTE") or not domain:
        return None
    try:
        level = float(obj.get("level", 1.0))
    except (TypeError, ValueError):
        level = 1.0
    level = max(0.5, min(1.0, level))  # must be ignition-capable
    return {
        "action": action,
        "domain": domain,
        # the SPECIFIC thing to learn/build (operator 2026-07-08): Omega chooses
        # the exact sub-topic, not the broad domain — this becomes the fetch
        # query so learning stops cycling over the same domain overview. Falls
        # back to the domain when the model omits it.
        "topic": str(obj.get("topic") or domain).strip(),
        "level": level,
        "reasoning": str(obj.get("reasoning") or "")[:300],
    }


def fire_awakening(now: Optional[float] = None) -> bool:
    """Idle expiry (operator 2026-07-08): fire the gemma model DIRECTLY with the
    awaken prompt so the mind goes from dead-stop to an autonomous pressure
    ignition. The model reads its whole mastery map and decides ONE action —
    LEARN a gap (raises knowledge) or EXECUTE a learned-but-unapplied topic
    (raises application). Its decision enters pressure as one validated Omega
    successor (LEARN or USE_SKILL, domain-scoped), exactly like Omega's own PRESSURE;
    the loop ignites it on the NEXT cycle,
    and the mind reignites from there for as long as it wants.

    Firing does NOT reset the idle clock (operator 2026-07-09) — only user input
    does — so the woken mind is free to chain autonomous work indefinitely. It DOES
    stamp `_last_wake_attempt_ts` to throttle the gemma KICK to one attempt per idle
    window (never a machine-gun), which bounds re-stimulation of a persistently-idle
    mind without touching the mind's own chaining.
    Returns True when a decision was recorded. Failure-soft (a down runtime /
    empty map just rests and retries next window)."""
    global _last_wake_attempt_ts
    if not wake_due(now=now):
        return False
    _last_wake_attempt_ts = time.time() if now is None else float(now)
    fired = False
    try:
        from deepsleep_loop.mastery_store import load_mastery

        records = load_mastery()
        if not records:
            return False  # cold start: nothing to learn yet (onboarding seeds it)

        prompt = (
            "Your memory — every topic, how far learned vs applied, by category:\n"
            + _mastery_map_text(records)
            + "\n\nDecide your ONE next action. Output ONLY:\n"
            '{"reasoning": "<one sentence>", "action": "LEARN"|"EXECUTE", '
            '"domain": "<the exact domain name from the list above>", '
            '"topic": "<the SPECIFIC thing to learn or build about that domain '
            'right now — a concrete sub-aspect (e.g. \\"Dijkstra shortest-path '
            'algorithm\\", not \\"Algorithms\\"), advancing beyond what you '
            'already know>", "level": 1.0}\n'
            "Pick LEARN for a low-learned topic, EXECUTE for a high-learned/"
            "low-applied topic. Choose the single most valuable, SPECIFIC move."
        )
        from runtime.client import get_runtime_client

        client = get_runtime_client()
        # THINKING BUDGET (live root-cause 2026-07-10): at max_tokens=400 gemma
        # spends the whole budget inside its <think> block over the ~26-row
        # mastery map; the runtime strips thinking and returns ok=true with
        # EMPTY text, so the wake silently rested every window and pressure
        # never saw the decision. 1500 gives think + JSON headroom.
        out = client.generate(
            prompt,
            system=_AWAKEN_SYSTEM,
            max_tokens=1500,
            temperature=0.0,
            enable_thinking=True,
            timeout=240.0,
            work_kind="autonomous",
        )
        decision = _parse_awaken_decision(out) if out else None
        if decision is None:
            # Deterministic fallback: one retry with thinking OFF (structured-
            # output mode, P3-S32) so a think-runaway or JSON drift cannot cost
            # the mind a whole idle window. Parse repair-tolerant, fail-closed.
            out2 = client.generate(
                prompt,
                system=_AWAKEN_SYSTEM,
                max_tokens=400,
                temperature=0.0,
                enable_thinking=False,
                timeout=240.0,
                work_kind="autonomous",
            )
            decision = _parse_awaken_decision(out2) if out2 else None
            if decision is None:
                # Failure is data — the wake deciding NOTHING must be visible,
                # not a silent rest (this hid the empty-generate bug live).
                try:
                    from guardian.terminal_feedback import emit_data

                    emit_data(
                        "pressure",
                        "idle_awakening",
                        status="fail",
                        ok=False,
                        summary="awakening produced no decision (empty/unparseable)",
                        error=(f"primary_len={len(out or '')} fallback_len={len(out2 or '')}"),
                    )
                except Exception:
                    pass
                log.warning(
                    "idle awakening produced no decision (primary_len=%d "
                    "fallback_len=%d) — resting until next window",
                    len(out or ""),
                    len(out2 or ""),
                )
        if decision is not None:
            from knowledge_extractor.derive.run import _slug
            from pressure_loop.omega_successor import stage_idle_learning_successor

            action = decision["action"]
            downgraded = False
            if action == "EXECUTE":
                # APPLY-READINESS at decision time (live root-cause #2,
                # 2026-07-10): USE_SKILL needs a mintable objective, and the
                # mint enforces the operator's practice bar (knowledge >=
                # MIND_DRIVEN_KNOWLEDGE_BAR + own-warm substrate). An EXECUTE
                # the mint cannot back died as use_skill_undispatchable ->
                # IDLE every window — the wake->die loop. Under the bar the
                # decision DOWNGRADES to LEARN on the SAME domain+topic:
                # study until ready, then EXECUTE follows through. When ready,
                # the mind's specific choice is stashed so the mint backs
                # WHAT IT DECIDED (skill pack -> prompt -> model swap).
                try:
                    from pressure_loop.practice_mint import (
                        is_apply_ready,
                        set_apply_choice,
                    )

                    if is_apply_ready(_slug(decision["domain"])):
                        set_apply_choice(decision["domain"], decision["topic"])
                    else:
                        action = "LEARN"
                        downgraded = True
                except Exception:
                    action = "LEARN"
                    downgraded = True

            lever = "mastery_debt_delta" if action == "LEARN" else "execution_debt_delta"
            domain_id = _slug(decision["domain"])
            record = records.get(domain_id)
            observed = (
                f"knowledge mastery {float(record.knowledge_mastery):.2f}, application mastery "
                f"{float(record.application_mastery):.2f}, target {float(record.target_mastery):.2f}"
                if record is not None
                else "no mastery record yet"
            )
            # The LEARN it ignites studies Omega's SPECIFIC chosen topic (the successor stashes it).
            stage_idle_learning_successor(
                intent="LEARN" if action == "LEARN" else "USE_SKILL",
                domain=domain_id,
                topic=decision["topic"],
                level=decision["level"],
                lever=lever,
                reasoning=decision["reasoning"],
                observed=observed,
            )
            fired = True
            try:
                from guardian.terminal_feedback import emit_data

                emit_data(
                    "pressure",
                    "idle_awakening",
                    status="ok",
                    ok=True,
                    summary=(
                        f"awakening -> {action} "
                        f"{decision['domain']} ({lever}={decision['level']})"
                        + (" [EXECUTE downgraded: below practice bar]" if downgraded else "")
                    ),
                    action=action,
                    domain=decision["domain"],
                    downgraded_from_execute=downgraded,
                )
            except Exception:
                pass
            log.info(
                "idle awakening fired -> %s %s (%s=%.2f)%s",
                action,
                decision["domain"],
                lever,
                decision["level"],
                " [downgraded from EXECUTE: below practice bar]" if downgraded else "",
            )
    except Exception:
        log.warning("idle awakening failed (non-fatal) — resting", exc_info=True)
    # The idle clock is NOT reset here (only user input resets it) — the mind stays
    # free to reignite. The throttle stamp above (_last_wake_attempt_ts) is what keeps
    # a persistently-idle mind from machine-gunning the gemma kick.
    return fired


def _reset_for_tests() -> None:
    global _last_activity_ts, _last_activity_kind, _last_wake_attempt_ts
    _last_activity_ts = time.time()
    _last_activity_kind = "boot"
    _last_wake_attempt_ts = 0.0
