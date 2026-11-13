"""Omega speaks once when it wakes — the residency proof you can hear.

Operator, 2026-09-22: *"on hud boot, omega should speak the onboarding phrase and if onboarding is
complete, omega should say Hello (operator's name). that way you always know if its resident on
boot"*, and: *"it boots voice off, but that doesn't matter, make it always speak"*.

That is a better test than any log line. A greeting you can hear proves the whole chain in one
event — the talker resident on the GPU, synthesis, PCM over HTTP, scheduled audio, playback. Silence
means something in that chain is broken, and you learn it before saying a word.

Two deliberate exceptions, stated rather than assumed:

* **The voice pill boots OFF and this still speaks.** A default-off toggle is not a considered
  choice, and this utterance is the boot probe. The notice carries ``speak_always``.
* **An explicit ``tts_enabled: false`` is still honoured** — that IS a considered choice, and a
  boundary the operator set is one Omega keeps (AGENTS.md §0.1).

Two things it will not do (§0, no person-specific defaults):

* never invent, guess or infer a name — it comes from the operator's own recorded profile, and
  until one exists Omega says "operator", a role rather than an identity;
* never assume a timezone, which rules out "good morning".

It is not a reply. It answers no turn, resolves no bubble, and never touches ``append_response``
(§4.1); it rides the proactive-notice rail the HUD already appends and speaks.
"""

from __future__ import annotations

import logging
import random
from typing import Any, Callable, Optional, Sequence

log = logging.getLogger("omega.pressure.boot_greeting")

#: The rail's kind for this notice, so the surface can treat it deliberately.
NOTICE_KIND = "BOOT_GREETING"

#: Said until onboarding is complete. It invites the first thing onboarding needs — a name — and
#: assumes nothing about who is listening.
ONBOARDING_PHRASE = "Hello. I'm Omega. Let's set you up — tell me what to call you."

#: Variety, so boot does not feel like a recording. Every one is neutral about time, place and
#: person; the only thing slotted in is the name the operator recorded for themselves.
NAMED_GREETINGS: tuple[str, ...] = (
    "Hello, {name}.",
    "Welcome back, {name}.",
    "Good to see you, {name}.",
    "I'm awake, {name}.",
    "Ready when you are, {name}.",
    "I'm here, {name}.",
    "Back online, {name}.",
)

#: Said in place of a name until the operator records one (operator 2026-09-22: "either the
#: operator's name, or simply operator if there is none"). A role noun assumes nothing about who is
#: listening, which is what §0 asks for — unlike a machine owner, a login, or a guess.
FALLBACK_NAME = "operator"

_MAX_NAME_CHARS = 64

_STATE: dict[str, Any] = {"greeted": False, "failure": None}


def reset_for_tests() -> None:
    _STATE.update({"greeted": False, "failure": None})


def last_failure() -> Optional[str]:
    """Why the last greeting did not publish, or ``None``. A boot probe that fails silently is
    worse than no probe: it reads as "speech is fine" when nothing was ever said."""
    return _STATE["failure"]


def _clean_name(value: Any) -> str:
    name = str(value or "").strip()
    return name[:_MAX_NAME_CHARS] if name else ""


def greeting_text(
    *,
    setup_completed: bool,
    name: Any = None,
    pick: Callable[[Sequence[str]], str] = random.choice,
) -> str:
    """The words to say. Pure: no I/O, no clock, no store."""
    if not setup_completed:
        return ONBOARDING_PHRASE
    return pick(NAMED_GREETINGS).format(name=_clean_name(name) or FALLBACK_NAME)


def _setup_completed() -> bool:
    """Whether the operator identity profile has been saved.

    The launcher setup record tracks backend wizard progress; it is not the durable source for
    operator identity onboarding. Use the same profile-backed check as the HUD onboarding route.
    """
    from hud.operator_identity import is_onboarded

    return bool(is_onboarded())


def _recall_operator_name() -> str:
    """The name the operator recorded for themselves, or ``""``.

    Reuses the ONE canonical reader — ``memory.persona_ingest._resolve_operator_name``, which reads
    the ``1.00.operator_name`` anchor written when the operator named themselves, and returns None
    until they have. A second reader here would be a second answer to the same question, and
    "what is the operator called" must have exactly one (§2). Deliberately private-imported rather
    than reimplemented. Returns ``""`` when unnamed — the caller then addresses the operator by role
    (:data:`FALLBACK_NAME`); nothing here reaches for a machine owner, a login, or a guess.
    """
    from memory.persona_ingest import _resolve_operator_name

    return _clean_name(_resolve_operator_name())


def _append_notice(*, text: str, kind: str, speak_always: bool) -> bool:
    from pressure_loop.proactive_chat_messages import append_update

    record = append_update(
        text=text,
        episode_id="",
        opened_by="boot_greeting",
        intent="PROACTIVE_RESPONSE",
        kind=kind,
        payload={"speak_always": bool(speak_always), "boot_greeting": True},
    )
    return record is not None


def boot_greeting() -> str:
    """The greeting for this boot, resolved from the operator's own state.

    Every read is failure-soft in the direction of still saying something: an unreadable profile is
    nameless, and an unreadable setup state speaks the onboarding phrase, because the probe's job is
    to be heard.
    """
    try:
        completed = bool(_setup_completed())
    except Exception:
        log.warning("setup state unreadable; speaking the onboarding phrase", exc_info=True)
        return ONBOARDING_PHRASE
    name = ""
    if completed:
        try:
            name = _recall_operator_name()
        except Exception:
            log.warning("operator name unreadable; greeting the operator by role", exc_info=True)
            name = ""
    return greeting_text(setup_completed=completed, name=name)


def publish_boot_greeting() -> bool:
    """Say it, once per boot. Returns whether a greeting was published.

    ``False`` means either "already greeted this boot" — a HUD refresh must not re-greet — or a
    publication failure, which is recorded in :func:`last_failure` rather than swallowed. A failed
    greeting may be retried; a delivered one is never repeated.
    """
    if _STATE["greeted"]:
        return False
    text = boot_greeting()
    try:
        published = _append_notice(text=text, kind=NOTICE_KIND, speak_always=True)
    except Exception as exc:
        _STATE["failure"] = f"{type(exc).__name__}: {exc}"
        log.error("boot greeting not published: %s", _STATE["failure"], exc_info=True)
        return False
    if not published:
        _STATE["failure"] = "the proactive rail accepted no record"
        log.error("boot greeting not published: %s", _STATE["failure"])
        return False
    _STATE.update({"greeted": True, "failure": None})
    log.info("boot greeting published: %s", text)
    return True


__all__ = [
    "FALLBACK_NAME",
    "NAMED_GREETINGS",
    "NOTICE_KIND",
    "ONBOARDING_PHRASE",
    "boot_greeting",
    "greeting_text",
    "last_failure",
    "publish_boot_greeting",
    "reset_for_tests",
]
