"""The previous assistant action Global A's intent router reads beside a follow-up message.

"do it", "fix that" and "now make it a pdf" name no route on their own. The router was
trained on ``[PREVIOUS ASSISTANT ACTION] <ROUTE>: <what Omega just did>`` so such
follow-ups resolve against the last episode that answered the operator. The record is
in-session only (the chat panel is cleared every boot) and expires after a quiet gap,
so a message typed an hour later is routed on its own words.

The record also names WHICH episode and thread it describes (T3, review 2026-09-16).
Without that, a follow-up had to guess — and the ingress binder guessed "the newest
journal row", which with four lanes running at once is routinely another lane's thread.
Reply delivery also updates this record at AUTO, before the answering episode archives,
so the newest row can be an older thread even in the single-lane case. One TTL governs
the line and the identity together: an expired line cannot anchor a referent.
"""

from __future__ import annotations

import threading
import time

from global_loop.intent.router import ROUTES

CONTEXT_TTL_SECONDS = 20 * 60
_REQUEST_LIMIT = 160
_ACTIONS = {
    "FREEFORM_CHAT": "answered",
    "CODING_AGENT": "worked in the operator's project on",
    "BROWSER_AGENT": "worked in the browser on",
    "DOCUMENT_CREATION": "created a document for",
}

_lock = threading.Lock()
# (stamped_at, line, thread_id, episode_id)
_last: tuple[float, str, str, str] | None = None


def remember_action(
    route: str,
    request: str,
    *,
    thread_id: str = "",
    episode_id: str = "",
    now: float | None = None,
) -> None:
    """Record the route that just answered the operator and the request it answered.

    ``thread_id``/``episode_id`` identify the episode this line describes, so a
    follow-up binds to that thread rather than to whatever archived most recently.
    Both are host-owned; empty means the caller could not name one, which reads
    truthfully as "this line anchors no thread".
    """
    global _last
    if route not in ROUTES:
        raise ValueError(f"unknown route {route!r}")
    clean = " ".join(str(request or "").split())
    if not clean:
        return
    if len(clean) > _REQUEST_LIMIT:
        clean = clean[:_REQUEST_LIMIT - 1].rstrip() + "…"
    line = f'{route}: {_ACTIONS[route]} "{clean}"'
    with _lock:
        _last = (time.time() if now is None else now, line,
                 str(thread_id or ""), str(episode_id or ""))


def _live(now: float | None) -> tuple[float, str, str, str] | None:
    with _lock:
        last = _last
    if last is None:
        return None
    stamped = last[0]
    if (time.time() if now is None else now) - stamped > CONTEXT_TTL_SECONDS:
        return None
    return last


def previous_action(*, now: float | None = None) -> str:
    """The router's context line, or an empty string when there is no recent action."""
    last = _live(now)
    return last[1] if last is not None else ""


def previous_thread(*, now: float | None = None) -> str:
    """The thread the recent context DESCRIBES, or "" when it names none.

    Never a fallback to "the most recent thread": an answer here is an identity
    linkage or nothing at all.
    """
    last = _live(now)
    return last[2] if last is not None else ""


def previous_episode(*, now: float | None = None) -> str:
    """The episode the recent context describes, or "" when it names none."""
    last = _live(now)
    return last[3] if last is not None else ""


_SETTLED = {"completed": "completed", "failed": "failed", "stopped": "was stopped",
            "missing": "no longer exists"}


def held_instruction_context(held) -> tuple[str, str]:
    """``(router line, cognition context)`` for operator words that outlived their build.

    Cross-lane T7: words sent while a build ran re-enter as an ordinary turn once no fold of
    that build remains. The build IS the previous assistant action they follow, so the
    router reads it in the exact form it was trained on; cognition additionally reads the
    host's record of how that build settled. Both are host-authored from the job record,
    never from the message.
    """
    objective = " ".join(str(held.get("objective") or "").split()) or "the build"
    shown = objective if len(objective) <= _REQUEST_LIMIT else objective[:_REQUEST_LIMIT - 1].rstrip() + "…"
    router_line = f'CODING_AGENT: {_ACTIONS["CODING_AGENT"]} "{shown}"'
    status = str(held.get("job_status") or "")
    note = (f"Host record: the operator sent the current message while build "
            f"{held.get('job_id') or ''} was running (objective: \"{objective}\"")
    if held.get("project_root"):
        note += f"; project: {held['project_root']}"
    note += f"). That build {_SETTLED.get(status, status or 'settled')} before it read the message"
    if held.get("summary"):
        note += f"; its final summary: \"{held['summary']}\""
    return router_line, f"{router_line}\n{note}."


def reset() -> None:
    global _last
    with _lock:
        _last = None


__all__ = ["CONTEXT_TTL_SECONDS", "held_instruction_context", "previous_action",
           "previous_episode", "previous_thread", "remember_action", "reset"]
