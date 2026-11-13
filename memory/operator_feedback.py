"""The operator's later word about an earlier episode, recorded as that episode's evidence.

WO-06 gave the sealed record a ``feedback_status`` axis and said plainly what it is: "the
operator's later word, which this cannot know". WO-10 could read an
``operator_feedback_received`` event and turn it into an ``evaluates`` relation. Between the two
there was no producer, so the axis could only ever say ``not_received`` and the relation only ever
came from a test. This module is that producer.

Two things make it recordable without a model deciding anything.

**The subject is bound by the host.** Omega presents a result FOR an episode
(``pressure_loop.system_messages.append_result_presentation``); the operator's reply to that
presentation is bound to the record by the store, and the reply's contributor carries
``system_query_episode_id`` — the episode the result was about. The subject is never inferred from
the operator's words, and an answer to a QUESTION is not a verdict on an outcome: a policy the
operator states ("study quantum physics next") says nothing about whether an earlier episode
succeeded, so only a graded result presentation produces feedback.

**The polarity is the operator's own.** It comes from the verdict control they used
(``supports`` / ``contradicts`` / ``mixed``). A reply that states no verdict is recorded as
``unknown`` — never classified from its wording. "It sounds negative" is exactly the kind of
inference WO-10 refuses to turn into a fact; the statement is kept so a later reader can judge it,
attributed to the operator and redacted through the existing treatment.

**It lands in the ANSWERING episode.** The episode being judged is closed and its record sealed
and immutable, so nothing can be added to it. The observation belongs to the episode that received
the statement, and it names the one it is about — which is what WO-10's history reads.
"""

from __future__ import annotations

from typing import Any

from memory.episode_events import capture_context, record_event

#: The event kind. Its shape is the one the WO-02 contract corpus fixed
#: (``tests/continuity/fixtures/contracts/late_feedback_event.json``).
OPERATOR_FEEDBACK_RECEIVED = "operator_feedback_received"

#: The verdicts an operator can state, derived from the record's own axis vocabulary rather than
#: invented here. ``not_received`` and ``unknown`` are absences, so neither can be *stated*.
def operator_verdicts() -> tuple[str, ...]:
    from schema.episode_record import FEEDBACK_STATUSES

    return tuple(status for status in FEEDBACK_STATUSES
                 if status not in ("not_received", "unknown"))


#: Only a graded result presentation is the operator grading an outcome.
GRADED_KIND = "result_presentation"

_UNSTATED = "unknown"


def checked_verdict(value: Any) -> str:
    """The stated verdict, or ``""`` when none was stated. An unknown value is refused.

    Fail-hard on purpose: a verdict the host does not recognise must not be silently dropped (the
    operator would have stated one and memory would say they did not) nor stored as-is (it would
    become a value no reader's vocabulary contains).
    """
    verdict = str(value or "").strip()
    if not verdict:
        return ""
    allowed = operator_verdicts()
    if verdict not in allowed:
        raise ValueError(
            f"operator verdict {verdict!r} is not one of {allowed}; an unrecognised verdict is "
            "refused rather than recorded as something else"
        )
    return verdict


def capture_operator_feedback(episode: Any, contributor: Any) -> Any:
    """Record the operator's verdict on an earlier episode, at the admission of the reply.

    Returns the event, or ``None`` when this turn is not the operator grading an episode's
    result — which is the ordinary case for every other kind of turn.
    """
    metadata = dict(getattr(contributor, "metadata", None) or {})
    about = str(metadata.get("system_query_episode_id") or "").strip()
    kind = str(metadata.get("system_query_kind") or "").strip()
    episode_id = str(getattr(episode, "episode_id", "") or "").strip()
    if not about or not episode_id or kind != GRADED_KIND or about == episode_id:
        return None

    from memory.auto_evidence import safe_detail

    verdict = checked_verdict(metadata.get("system_query_verdict"))
    payload: dict[str, Any] = {
        "about_episode_id": about,
        "about_message_id": str(metadata.get("system_query_id") or ""),
        "feedback": verdict or _UNSTATED,
        # Where the polarity came from, so a reader never mistakes an unstated verdict for a
        # judgement somebody made.
        "verdict_source": "operator_control" if verdict else "not_stated",
        "received_in_episode": episode_id,
        # The operator's own words, through the existing sensitive-field treatment. Kept because
        # the verdict alone cannot be re-judged later; attributed by evidence_class, never read as
        # host authority.
        "statement": safe_detail(metadata.get("message")),
    }
    with capture_context(
        thread_id=str(getattr(episode, "thread_id", "") or "") or episode_id,
        turn_id=str(metadata.get("turn_id") or ""),
        arrival_seq=int(metadata.get("arrival_seq") or 0),
    ):
        return record_event(
            episode_id,
            operation_id=f"operator-feedback:{episode_id}:{payload['about_message_id'] or about}",
            phase="admission",
            event_kind=OPERATOR_FEEDBACK_RECEIVED,
            payload=payload,
            evidence_class="operator_statement",
        )


__all__ = [
    "GRADED_KIND",
    "OPERATOR_FEEDBACK_RECEIVED",
    "capture_operator_feedback",
    "checked_verdict",
    "operator_verdicts",
]
