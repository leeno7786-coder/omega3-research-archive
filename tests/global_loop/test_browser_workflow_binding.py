"""Global A binds a BROWSE turn to the durable workflow and never takes the cascade down."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from global_loop.browser_workflow import bind_browser_workflow
from pressure_loop.contributors import make_contributor


def _ignition(source, kind, metadata):
    return SimpleNamespace(dominant_contributor=make_contributor(source, kind, 1.0, metadata))


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("OMEGA_BROWSER_WORKFLOW_PATH", str(tmp_path / "browser.json"))


def test_an_operator_turn_opens_then_joins_the_workflow_and_a_blocked_one_revives():
    from meta_loop.browser.workflow import record_browser_run

    first = bind_browser_workflow(_ignition("operator", "operator_query", {"message": "open Hugging Face", "cycle_id": "c1"}),
                                  operator_turn=True)
    assert first["status"] == "active" and first["objective"] == "open Hugging Face"
    for _ in range(3):
        record_browser_run(first["workflow_id"], {"status": "blocked", "steps": 0, "stopped_reason": "edge gone"})
    # Live 2026-09-15: the old binder raised "browser workflow is not active" here and the boot died.
    second = bind_browser_workflow(_ignition("operator", "operator_query", {"message": "try again", "cycle_id": "c2"}),
                                   operator_turn=True)
    assert second["workflow_id"] == first["workflow_id"]
    assert second["status"] == "active" and second["instruction"] == "try again" and second["pending_notes"] == 1


def test_a_close_phrase_with_nothing_open_binds_a_closed_workflow_instead_of_raising():
    receipt = bind_browser_workflow(_ignition("operator", "operator_query", {"message": "close the browser", "cycle_id": "c3"}),
                                    operator_turn=True)
    assert receipt["status"] == "completed" and receipt["objective"] == "close the browser"


def test_a_successor_continues_only_a_runnable_workflow():
    from meta_loop.browser.workflow import close_browser_workflow

    opened = bind_browser_workflow(_ignition("operator", "operator_query", {"message": "open Hugging Face"}), operator_turn=True)
    successor = _ignition("omega", "omega_successor", {"browser_workflow_id": opened["workflow_id"]})
    assert bind_browser_workflow(successor, operator_turn=False)["workflow_id"] == opened["workflow_id"]
    close_browser_workflow(opened["workflow_id"])
    with pytest.raises(ValueError, match="no longer run"):
        bind_browser_workflow(successor, operator_turn=False)
    with pytest.raises(ValueError, match="opened by the operator"):
        bind_browser_workflow(_ignition("operator", "operator_query", {"message": "x"}), operator_turn=False)
