"""Topic Resolution Phase 3c.2b — persistence schema + window stamp/increment."""

from __future__ import annotations

import json

import pytest

import pressure_loop.open_questions as oq
from pressure_loop.open_questions import (
    STATUS_ANSWERED,
    STATUS_OPEN,
    STATUS_RESOLVED,
    OpenQuestion,
)
from pressure_loop.reflection_window import ReflectionWindow


@pytest.fixture
def questions_path(tmp_path, monkeypatch):
    path = tmp_path / "open_questions.jsonl"
    monkeypatch.setattr(oq, "OPEN_QUESTIONS_PATH", path)
    return path


def _base_question(**overrides) -> OpenQuestion:
    data = {
        "question_id": "oq-test-1",
        "curiosity_id": "cur-1",
        "act": "follow_up_question",
        "question_text": "how do attention heads combine?",
        "asked_cycle": "cycle-1",
        "asked_at": "2026-06-01T12:00:00+00:00",
        "status": STATUS_OPEN,
    }
    data.update(overrides)
    return OpenQuestion(**data)


# --- B1: schema round-trip + back-compat ---


def test_open_question_schema_round_trip():
    q = _base_question(asked_window_id="win-abc", windows_survived=2)
    restored = OpenQuestion.from_dict(q.to_dict())
    assert restored is not None
    assert restored.asked_window_id == "win-abc"
    assert restored.windows_survived == 2


def test_open_question_from_dict_back_compat():
    legacy = {
        "question_id": "oq-legacy",
        "curiosity_id": "cur-legacy",
        "act": "tension_response",
        "question_text": "legacy question",
        "asked_cycle": "cycle-legacy",
        "asked_at": "2026-06-01T12:00:00+00:00",
        "status": STATUS_OPEN,
    }
    restored = OpenQuestion.from_dict(legacy)
    assert restored is not None
    assert restored.asked_window_id is None
    assert restored.windows_survived == 0


# --- B2: stamp at creation ---


def test_record_open_question_stamps_window_id(questions_path, monkeypatch):
    window = ReflectionWindow(
        window_id="win-stamp-1",
        started_at_episode="ep-1",
        trajectory_snapshot={},
    )
    monkeypatch.setattr(
        "pressure_loop.reflection_window.load_reflection_window",
        lambda: window,
    )

    qid = oq.record_open_question(
        curiosity_id="cur-stamp",
        act="follow_up_question",
        question_text="what is attention?",
        asked_cycle="cycle-stamp",
    )
    assert qid is not None
    recorded = oq.get(qid)
    assert recorded is not None
    assert recorded.asked_window_id == "win-stamp-1"
    assert recorded.windows_survived == 0


def test_record_open_question_no_active_window(questions_path, monkeypatch):
    monkeypatch.setattr(
        "pressure_loop.reflection_window.load_reflection_window",
        lambda: None,
    )

    qid = oq.record_open_question(
        curiosity_id="cur-no-window",
        act="follow_up_question",
        question_text="what is attention?",
        asked_cycle="cycle-no-window",
    )
    assert qid is not None
    recorded = oq.get(qid)
    assert recorded is not None
    assert recorded.asked_window_id is None
    assert recorded.windows_survived == 0


# --- B3: increment on archive ---


def _append_question(path, **overrides) -> OpenQuestion:
    q = _base_question(**overrides)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(q.to_dict()) + "\n")
    return q


def test_increment_survived_bumps_open_questions_only(questions_path):
    _append_question(questions_path, question_id="q-open", status=STATUS_OPEN)
    _append_question(
        questions_path,
        question_id="q-answered",
        status=STATUS_ANSWERED,
        answer_text="done",
    )
    _append_question(
        questions_path,
        question_id="q-resolved",
        status=STATUS_RESOLVED,
        resolved_by="omega",
    )

    oq.increment_windows_survived_for_open_questions()

    assert oq.get("q-open").windows_survived == 1
    assert oq.get("q-answered").windows_survived == 0
    assert oq.get("q-resolved").windows_survived == 0


def test_increment_survived_appends_only(questions_path):
    _append_question(questions_path, question_id="q-open-1", status=STATUS_OPEN)
    _append_question(questions_path, question_id="q-open-2", status=STATUS_OPEN)

    before = questions_path.read_text(encoding="utf-8").count("\n")
    oq.increment_windows_survived_for_open_questions()
    after = questions_path.read_text(encoding="utf-8").count("\n")

    assert after == before + 2


def test_increment_survived_idempotent(questions_path):
    _append_question(questions_path, question_id="q-open", status=STATUS_OPEN)

    oq.increment_windows_survived_for_open_questions()
    oq.increment_windows_survived_for_open_questions()

    assert oq.get("q-open").windows_survived == 2


def test_increment_survived_equals_call_count(questions_path):
    _append_question(questions_path, question_id="q-open", status=STATUS_OPEN)

    for expected in (1, 2, 3):
        oq.increment_windows_survived_for_open_questions()
        assert oq.get("q-open").windows_survived == expected
