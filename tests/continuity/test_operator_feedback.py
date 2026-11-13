"""The operator's later word about an episode, recorded by the host (closes WO-10's gap).

WO-10 could read ``operator_feedback_received`` and turn it into an ``evaluates`` relation, but
no production component ever recorded one: only its test suites did, through the raw event API.
So ``feedback_status`` could never be anything but ``not_received``, and "the operator said this
didn't work" existed nowhere in memory.

What makes it recordable without a model deciding anything:

* **The binding is the host's.** Omega presents a result for an episode
  (``system_messages.append_result_presentation``), and the operator's reply to that presentation
  is bound to it by the store. The reply's contributor already carries
  ``system_query_episode_id`` — the episode the result was about. Nothing infers the subject.
* **The polarity is the operator's.** The HUD's result card offers explicit verdict controls, and
  the reply carries the verdict the operator chose. A reply with no verdict is recorded as
  ``unknown`` — never classified from the words. "It sounds negative" is not evidence.
* **An answer to a QUESTION is not a verdict on an outcome.** Answering "which domain should I
  study?" is a policy, not the operator grading a result, and it records no feedback.

Every episode here is admitted through the real seam over the real durable queue, and the reply
travels the real ``respond_to_message`` rail.
"""

from __future__ import annotations

import pytest

from pressure_loop.episode import EpisodeStore
from tests.helpers import continuity_history as ch
from tests.helpers import continuity_terminus as ct

PRESENTED = "I finished the parser refactor you asked about: the acceptance test passes now."


@pytest.fixture(autouse=True)
def _terminus(tmp_path, monkeypatch):
    from memory import episode_sram

    monkeypatch.setenv("OMEGA_WINDOW_LANE", "1")
    ct.isolate(monkeypatch, tmp_path)
    yield
    episode_sram.reset()


def _present(about_episode_id: str) -> str:
    """Present a result FOR that episode. The ids are the subject's: the system-message store is
    shared across tests, and a reused result_id would return the earlier presentation."""
    from pressure_loop.system_messages import append_result_presentation

    record = append_result_presentation(
        text=PRESENTED,
        episode_id=about_episode_id,
        opened_by="omega_successor",
        result_id=f"result-{about_episode_id}",
        thread_id=f"thread-{about_episode_id}",
    )
    assert record is not None
    return str(record["id"])


def _feedback_events(episode_id: str) -> list:
    from memory.episode_events import read_episode_events

    return [event for event in read_episode_events(episode_id)
            if event.event_kind == "operator_feedback_received"]


def _respond(message_id: str, text: str, **kwargs):
    import uuid

    from pressure_loop.system_messages import respond_to_message

    return respond_to_message(message_id, text, cycle_id=f"bubble-{uuid.uuid4().hex[:8]}",
                              **kwargs)


# ── the producer ─────────────────────────────────────────────────────────────


def test_a_graded_result_records_the_operators_word_about_that_episode():
    store = EpisodeStore()
    subject = ch.close_learn(store)
    message_id = _present(subject.episode_id)

    assert _respond(message_id, "it didn't work, the parser still fails", verdict="contradicts")
    answering = ch.admit_queued(store)

    events = _feedback_events(answering.episode_id)
    assert len(events) == 1, "the operator's grade reached no episode's evidence"
    event = events[0]
    assert event.payload["about_episode_id"] == subject.episode_id
    assert event.payload["about_message_id"] == message_id
    assert event.payload["feedback"] == "contradicts"
    assert event.payload["verdict_source"] == "operator_control"
    assert "the parser still fails" in event.payload["statement"]
    assert event.evidence_class == "operator_statement"
    assert event.phase == "admission"
    assert event.producer["authority"] == "host"
    ct.close(store)


def test_a_grade_without_a_verdict_is_unknown_and_the_words_are_never_classified():
    store = EpisodeStore()
    subject = ch.close_learn(store)
    message_id = _present(subject.episode_id)

    assert _respond(message_id, "that was completely wrong and unhelpful")
    answering = ch.admit_queued(store)

    (event,) = _feedback_events(answering.episode_id)
    assert event.payload["feedback"] == "unknown", (
        "polarity was guessed from the operator's wording"
    )
    assert event.payload["verdict_source"] == "not_stated"
    assert event.payload["about_episode_id"] == subject.episode_id
    ct.close(store)


def test_an_answer_to_a_question_records_no_verdict_on_that_episode():
    from pressure_loop.system_messages import append_system_query

    store = EpisodeStore()
    subject = ch.close_learn(store)
    record = append_system_query(
        text="Which domain should I study next?",
        episode_id=subject.episode_id,
        opened_by="operator_priority",
    )
    assert record is not None

    assert _respond(str(record["id"]), "quantum physics, please")
    answering = ch.admit_queued(store)

    assert not _feedback_events(answering.episode_id), (
        "answering a question was recorded as the operator grading that episode's outcome"
    )
    ct.close(store)


def test_a_credential_in_the_operators_words_is_not_persisted():
    store = EpisodeStore()
    subject = ch.close_learn(store)
    message_id = _present(subject.episode_id)

    leaked = "zzz999yyy888xxx777"
    assert _respond(message_id, f"it failed with token={leaked}", verdict="contradicts")
    answering = ch.admit_queued(store)

    (event,) = _feedback_events(answering.episode_id)
    assert leaked not in event.payload["statement"]
    assert leaked not in str(event.to_dict())
    ct.close(store)


def test_an_unknown_verdict_is_refused_rather_than_recorded_as_something_else():
    from schema.episode_record import FEEDBACK_STATUSES

    store = EpisodeStore()
    subject = ch.close_learn(store)
    message_id = _present(subject.episode_id)

    with pytest.raises(ValueError, match="verdict"):
        _respond(message_id, "it worked", verdict="lgtm")
    for refused in ("not_received", "unknown"):
        assert refused in FEEDBACK_STATUSES
        with pytest.raises(ValueError, match="verdict"):
            _respond(message_id, "it worked", verdict=refused)


def test_the_same_grade_is_recorded_once_however_often_it_is_replayed():
    store = EpisodeStore()
    subject = ch.close_learn(store)
    message_id = _present(subject.episode_id)

    assert _respond(message_id, "it worked", verdict="supports")
    assert _respond(message_id, "it worked", verdict="supports")  # already responded: no second turn
    answering = ch.admit_queued(store)
    assert len(_feedback_events(answering.episode_id)) == 1
    from pressure_loop import cognition_queue as cq

    assert cq.lease_next_input(0) is None, "the replayed grade queued a second turn"
    ct.close(store)


# ── end to end: the relation WO-10 reads now comes from the producer ─────────


def test_the_recorded_grade_becomes_the_history_relation_without_any_test_recording_it():
    store = EpisodeStore()
    subject = ch.close_learn(store)
    message_id = _present(subject.episode_id)
    assert _respond(message_id, "it didn't work, the parser still fails", verdict="contradicts")
    answering = ch.admit_queued(store)
    ct.dress_learn(answering)
    ct.close(store)
    ch.close_members(store, 8)

    _window, manifest, records = ch.seal_staged()
    bundle = ch.collect(manifest, records)
    rows = [row for row in ch.links_of(bundle, "evaluates")
            if row["source"] == f"episode:{answering.episode_id}"
            and row["target"] == f"episode:{subject.episode_id}"]
    assert len(rows) == 1, "the operator's recorded grade did not become an evaluates relation"
    assert rows[0]["detail"]["feedback"] == "contradicts"
    assert rows[0]["author"] == "host"
