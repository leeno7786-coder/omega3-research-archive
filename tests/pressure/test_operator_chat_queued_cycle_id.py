"""The queued (episode_active) chat return must carry a cycle_id — the key the
HUD polls on to resolve the 'thinking' bubble into the eventual answer. Without
it the frontend has no correlation key and renders an error."""

from __future__ import annotations

from unittest.mock import MagicMock

import pressure_loop.operator_chat as oc


def test_operator_command_keeps_its_bubble_open_for_the_real_result(monkeypatch):
    """A tool-capable operator command stays on the same chat response rail.

    It must not resolve the HUD bubble with a scripted acknowledgement before
    AUTO and the brain executor return the actual result.
    """
    enqueued = []

    import pressure_loop.cognition_queue as oq

    monkeypatch.setattr(oq, "enqueue_input", lambda c, **kwargs: enqueued.append(c) or True)
    rt = MagicMock()
    rt.episode_store.current.return_value = object()  # episode active

    result = oc.handle_operator_chat("Learn Python", runtime=rt)

    assert result["ok"] is True
    assert result["message"] == ""
    assert result["delivery"] == {"text": "", "rendered_by": "queued"}
    assert result["route"]["opened_by"] == "operator_command"
    assert len(enqueued) == 1
    assert enqueued[0].type == "operator_command"
    assert enqueued[0].metadata["directive_intent"] == "LEARN"
    assert "pre_delivered_response_text" not in enqueued[0].metadata


def test_explicit_remember_command_keeps_its_bubble_open_for_the_actual_result(monkeypatch):
    """Remember uses the same queued response rail as every other HUD turn."""
    import pressure_loop.cognition_queue as oq

    enqueued = []
    monkeypatch.setattr(oq, "enqueue_input", lambda c, **kwargs: enqueued.append(c) or True)
    rt = MagicMock()
    rt.episode_store.current.return_value = object()

    result = oc.handle_operator_chat(
        "Remember Mitchell Ray. He is Noahs best friend and business partner.",
        runtime=rt,
    )

    assert result["delivery"] == {"text": "", "rendered_by": "queued"}
    assert result["message"] == ""
    assert len(enqueued) == 1
    assert enqueued[0].metadata["operator_command_kind"] == "remember"
    assert "pre_delivered_response_text" not in enqueued[0].metadata
    assert "deterministic_response_text" not in enqueued[0].metadata
    assert "operator_reply_delivered" not in enqueued[0].metadata


def test_conversational_message_uses_the_same_async_reply_path(monkeypatch):
    """A plain/phatic message also keeps the one async response rail."""
    import pressure_loop.cognition_queue as oq

    monkeypatch.setattr(oq, "enqueue_input", lambda c, **kwargs: True)
    rt = MagicMock()
    rt.episode_store.current.return_value = object()

    result = oc.handle_operator_chat("Hello Omega, I am Noah", runtime=rt)

    assert result["delivery"] == {"text": "", "rendered_by": "queued"}
    assert result["queued"] is True  # goes to the async poll path


def test_queued_return_carries_cycle_id(monkeypatch):
    # Every artifact directive enters the durable FIFO, including while another
    # episode is active, and must carry the cycle_id poll key without reporting
    # queueing as an error.
    monkeypatch.setattr(
        "pressure_loop.pressure.evaluate_operator_ignition", lambda *a, **k: None
    )

    rt = MagicMock()
    rt.episode_store.current.return_value = object()  # an autonomous episode is live

    result = oc.handle_operator_chat(
        "a bright star filled galaxy", runtime=rt, directive_intent="IMAGE_CREATE"
    )

    assert result["queued"] is True
    assert result["error"] == ""
    cid = result.get("cycle_id")
    assert isinstance(cid, str) and cid  # non-empty key for the HUD to poll on
