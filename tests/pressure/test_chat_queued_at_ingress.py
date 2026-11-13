"""Fix A (operator 2026-07-03): a chat TEXT turn never runs the cascade on the
operator's HTTP thread. It enqueues to the one durable cognition queue and returns the
queued/cycle_id shape the HUD already resolves via the /api/chat/responses poll.
"fold it back the same way every other input gets folded back — run the cascade
after the action." Artifact directives use the same ordered queue and async
artifact response rail.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import pressure_loop.operator_chat as oc
import pressure_loop.cognition_queue as oq


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    # Chat text always reaches the queue gate; the retired inline fast-reply path
    # must not be revived by this fixture.
    # Deterministic ingress classification (no runtime intent call in the unit test).
    monkeypatch.setattr(
        "auto_loop.promptpack.intent_classifier.classify_chat_intent",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(oq, "_queue_path", lambda: tmp_path / "operator_queue.json")
    monkeypatch.setenv("OMEGA_BROWSER_WORKFLOW_PATH", str(tmp_path / "browser_workflow.json"))
    oq._reset_cache_for_tests()
    yield
    oq.clear_inputs()


def _runtime():
    rt = MagicMock()
    rt.episode_store.current.return_value = None  # no episode active
    return rt


@pytest.mark.parametrize(
    "message",
    [
        "Hello Omega, I am Noah",  # phatic
        "What is Python?",  # epistemic_request
    ],
)
def test_chat_text_enqueues_and_returns_queued_shape(monkeypatch, message):
    import cognition_chain

    def _no_sync(*_a, **_k):
        raise AssertionError("chat text must not ignite the cascade synchronously")

    monkeypatch.setattr(cognition_chain, "ignite_episode", _no_sync)

    result = oc.handle_operator_chat(message, runtime=_runtime())

    # The exact shape chat-feed.js resolves: queued + cycle_id + NO text keeps
    # the thinking spinner and polls /api/chat/responses on the cycle_id.
    assert result["ok"] is True
    assert result["queued"] is True
    cid = result.get("cycle_id")
    assert isinstance(cid, str) and cid
    assert result["message"] == ""
    assert result["delivery"]["text"] == ""
    assert result["delivery"]["rendered_by"] == "queued"
    assert result.get("error", "") == ""

    # Parked as top pressure for the next episode (operator supremacy queue).
    pending = oq.peek_inputs()
    assert len(pending) == 1
    assert pending[0].metadata.get("message") == message
    assert pending[0].metadata.get("cycle_id") == cid


def test_artifact_directive_uses_same_fifo_without_synchronous_ignition(monkeypatch):
    """Artifact turns cannot overtake operator turns already waiting in FIFO."""
    import cognition_chain

    monkeypatch.setattr(
        cognition_chain,
        "ignite_episode",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("artifact directives must not ignite synchronously")
        ),
    )

    result = oc.handle_operator_chat(
        "a bright star filled galaxy",
        runtime=_runtime(),
        directive_intent="IMAGE_CREATE",
    )

    assert result["ok"] is True
    assert result["queued"] is True
    assert result["error"] == ""
    queued = oq.peek_inputs()
    assert len(queued) == 1
    assert queued[0].metadata["directive_intent"] == "IMAGE_CREATE"


@pytest.mark.parametrize(
    "message",
    [
        "Omega open the browser and look up huggingface",
        "Inspect the current directory",
        "can you write a script for assorting files by time and date?",
    ],
)
def test_operator_turn_reaches_the_queue_unrouted(message):
    """Routing is Global A's intent router: ingress stamps no lane and binds no browser workflow."""
    from meta_loop.browser.workflow import load_browser_workflow

    result = oc.handle_operator_chat(message, runtime=_runtime())

    queued = oq.peek_inputs()
    assert result["queued"] is True
    assert len(queued) == 1
    assert "operator_route" not in queued[0].metadata
    assert "browser_workflow_id" not in queued[0].metadata
    assert load_browser_workflow() is None


def test_explicit_finish_closes_the_open_browser_workflow_at_ingress():
    from meta_loop.browser.workflow import load_browser_workflow, prepare_browser_turn

    opened = prepare_browser_turn("open browser and navigate to huggingface", cycle_id="browser-1")
    workflow_id = opened["workflow"]["workflow_id"]

    closed = oc.handle_operator_chat("finished", runtime=_runtime())

    assert closed["queued"] is False
    assert closed["browser_workflow"]["workflow_id"] == workflow_id
    assert closed["browser_workflow"]["status"] == "completed"
    assert load_browser_workflow()["status"] == "completed"
    assert not oq.peek_inputs()
