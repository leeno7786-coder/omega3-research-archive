"""Build notices: short human text (single objective copy) + durable chat-history line."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import cognition_chain as cc  # noqa: E402

REQ = (
    "Rebuild the csv_stats project from scratch in the workspace: delete the old files "
    "first, then csv_stats.py, a pytest test file with at least 4 tests, and a README "
    "with usage examples. Run the tests and confirm they all pass."
)


@pytest.fixture()
def sinks(monkeypatch):
    import hud.chat_history as ch
    import pressure_loop.proactive_chat_messages as pcm

    proactive, history = [], []
    monkeypatch.setattr(pcm, "append_update", lambda **kw: proactive.append(kw) or dict(kw))
    monkeypatch.setattr(ch, "append_message", lambda raw: history.append(raw) or dict(raw))
    return proactive, history


def _ep():
    return SimpleNamespace(
        episode_id="ep-notice", opened_by="operator_command", pressure={}, meta={}, global_={}
    )


def test_compose_uses_first_sentence_of_request_once():
    text = cc._compose_build_notice_text(
        objective=REQ + " " + REQ,  # doubled stash objective must not leak through
        original_request=REQ,
        summary="",
        files=["F:/omega-workspace/csv_stats.py", "F:/omega-workspace/README.md"],
    )
    assert text.startswith("✅ Finished: Rebuild the csv_stats project")
    assert text.count("Rebuild the csv_stats project") == 1
    assert "csv_stats.py" in text and "README.md" in text
    assert len(text) < 400


def test_complete_notice_lands_in_proactive_only(sinks):
    """Operator 2026-07-12: the chat panel is in-session only — notices ride the
    proactive feed (+ agent activity) and must NOT persist to the chat ring."""
    proactive, history = sinks
    cc._emit_build_complete_to_operator(
        _ep(),
        REQ,
        {"summary": "", "files_changed": ["csv_stats.py"]},
        origin_opened_by="operator_command",
        original_request=REQ,
    )
    assert len(proactive) == 1 and proactive[0]["text"].startswith("✅ Finished:")
    assert history == [], "notices must never persist to the chat-history ring"


def test_autonomous_origin_emits_nothing(sinks):
    proactive, history = sinks
    cc._emit_build_complete_to_operator(
        SimpleNamespace(
            episode_id="e", opened_by="execution_gap", pressure={}, meta={}, global_={}
        ),
        REQ,
        {},
        origin_opened_by="",
    )
    assert proactive == [] and history == []


def test_stopped_notice_lands_in_proactive_only(sinks):
    proactive, history = sinks
    cc._emit_build_stopped_to_operator(
        _ep(),
        REQ,
        "No progress against the remaining requirements.",
        origin_opened_by="operator_command",
    )
    assert len(proactive) == 1 and proactive[0]["text"].startswith("⛔ Stopped:")
    assert history == [], "notices must never persist to the chat-history ring"
