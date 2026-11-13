"""Esc halts a build, and the agent's question reaches the operator as numbered choices.

Operator asks 2026-09-15: set up the Esc button to halt a build in progress, and give the agent
a question system with pre-numbered choices plus a "type your own response" path.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MONOLITH = ROOT / "hud" / "omega_interface.html"
UI_SRC = ROOT / "hud" / "ui-src"


def test_escape_halts_the_build_from_the_hud():
    source = (UI_SRC / "core" / "live-wiring.js").read_text(encoding="utf-8")

    assert "e.key !== 'Escape'" in source
    assert "/api/agent/halt" in source
    assert "cycle_ids" in source
    assert "stopped_jobs" in source
    assert "Stop request failed" in source
    assert "Stop requested" in source


def test_the_chat_feed_paints_numbered_choices_and_a_free_answer():
    source = (UI_SRC / "core" / "chat-feed.js").read_text(encoding="utf-8")

    assert "result.operator_question" in source
    assert "agent-question-options" in source
    assert "or type your own answer below" in source
    assert "chat.user_message" in source, "clicking a choice sends it as the operator's answer"


def test_the_built_bundle_carries_both():
    bundle = MONOLITH.read_text(encoding="utf-8")

    assert "/api/agent/halt" in bundle, "run hud/scripts/build_hud.py after editing ui-src"
    assert "agent-question-options" in bundle
    assert "Stop request failed" in bundle
    assert "chat.tool_progress" in bundle
    assert "Browser setup:" in bundle


def test_the_halt_route_stops_every_running_job_with_no_cycle_named(tmp_path):
    from auto_loop.execute import job_control as jobs
    from pressure_loop.operator_chat import halt_operator_work

    project = tmp_path / "todo_app"
    project.mkdir(parents=True)
    jobs.open_job("build", "bubble-1", "build a todo app", tmp_path, project_root=project)

    halted = halt_operator_work("")

    assert halted["stopped_jobs"] == ["build"], "Esc means stop, even with no bubble open"


def test_the_response_record_carries_the_question_and_its_options():
    from pressure_loop.operator_chat_responses import append_response, get_response

    stored = append_response(
        cycle_id="bubble-question",
        episode_id="ep-1",
        text="Which storage should the todo list use?",
        operator_question={"question": "Which storage should the todo list use?",
                           "options": ["in-memory", "SQLite file"], "job_id": "build"},
    )

    assert stored is not None
    card = get_response("bubble-question")["operator_question"]
    assert card["options"] == ["in-memory", "SQLite file"]
    assert card["job_id"] == "build"


def test_an_ask_delivery_attaches_the_receipts_options(monkeypatch):
    """The options come from the batch receipt, so the card shows exactly what the agent offered."""
    import cognition_chain
    from types import SimpleNamespace

    seen: dict[str, object] = {}

    def fake_append_response(**kwargs):
        seen.update(kwargs)
        return {"cycle_id": kwargs.get("cycle_id")}

    monkeypatch.setattr("pressure_loop.operator_chat_responses.append_response",
                        fake_append_response)
    monkeypatch.setattr(cognition_chain, "_operator_chat_async_payload",
                        lambda episode, text: {"delivery": {"text": text}, "route": {}, "prompt_pack": {}})
    monkeypatch.setattr(cognition_chain, "_chat_correlation", lambda episode: {})
    monkeypatch.setattr("pressure_loop.operator_chat_responses.waiting_chat_turn",
                        lambda pressure: {"cycle_id": "bubble-1", "chat_bubble_waiting": True})
    monkeypatch.setattr("pressure_loop.omega_successor.build_omega_successor",
                        lambda episode, control: None)
    episode = SimpleNamespace(
        episode_id="ep-1",
        meta={},
        pressure={},
        global_={"intent_decision": {"omega_lane": "agent"}},
        auto={"outputs": {"agent_run": {"status": "ask_operator",
                                        "question": "Which storage?",
                                        "options": ["in-memory", "SQLite"],
                                        "job_id": "build"}}},
    )
    control = SimpleNamespace(decision="ASK_OPERATOR", synthesis="Which storage?")

    cognition_chain._settle_operator_turn(episode, control)

    card = seen.get("operator_question")
    assert card and card["options"] == ["in-memory", "SQLite"]
    assert card["job_id"] == "build"
