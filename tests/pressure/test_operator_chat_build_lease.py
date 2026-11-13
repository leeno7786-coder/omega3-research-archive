"""Operator chat during a build: queue into shared state for the next episode, never swap.

operator 2026-06-28: "we never swap in the middle of a run, we just queue that message to be
included in the shared state for the next episode." While a build holds the GPU, a question /
chit-chat turn must NOT fire a gemma generate (fast_reply or the synchronous cascade) — that
would evict the active build (the hotswap thrash). It enqueues to the durable operator-input
box; the next episode, after the handoff frees the GPU, folds it in.

PHASE 4 (operator 2026-08-11) SPLIT THAT INTO TWO WORLDS, and these tests now cover both.
``runtime.gpu_lease`` goes INERT under lane parallelism: one resident model serves four
llama-server slots, chat is slot 0 and the agent build is slot 1 on the SAME weights, so there
is no hotswap to guard and the canned busy ack only cost the turn its cognition (measured live:
three operator turns answered by the ack, none by the mind). The acknowledgement path is now
retired in both parallel and serial operation: every turn remains queued for its actual AUTO
result.

What is invariant across both: a chat turn during a build never generates on the operator's
thread, and it is always captured durably for the next episode.
"""

from __future__ import annotations

import pytest

from core.shared_state import SharedState
from pressure_loop.episode import EpisodeStore
from pressure_loop import cognition_queue as operator_queue
from pressure_loop.operator_chat import handle_operator_chat
from pressure_loop.runtime import PressureRuntime
from runtime import gpu_lease


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setenv("OMEGA_GPU_BUILD_LEASE", "1")
    gpu_lease._reset_for_tests()
    operator_queue.clear_inputs()
    # Deterministic ingress classification (no runtime intent call in the unit test).
    monkeypatch.setattr(
        "auto_loop.promptpack.intent_classifier.classify_chat_intent",
        lambda *a, **k: "question",
    )
    yield
    gpu_lease._reset_for_tests()
    operator_queue.clear_inputs()


def _runtime():
    return PressureRuntime(shared_state=SharedState(), episode_store=EpisodeStore())


class _Boom:
    """A runtime client that fails the test if anything generates on this thread."""

    def health_body(self):
        return {"status": "ok"}

    def generate_completion(self, **k):
        raise AssertionError("chat text never generates on the operator thread")

    def generate(self, *a, **k):
        raise AssertionError("chat text never generates on the operator thread")


def test_question_during_build_is_queued_not_generated(monkeypatch):
    """UNDER LANE PARALLELISM THE ACK IS GONE, and that is the ruling, not a bug.

    This test used to assert ``build_busy_ack``. The lease was RETIRED for the
    parallel world on 2026-08-11 (runtime.gpu_lease, "PHASE 4 RETIREMENT") and
    this file was never moved with it, so it has been asserting the exact
    behaviour the operator measured and removed: "pressure_loop.operator_chat
    parked a plain chat turn behind a canned 'I'll get to this as soon as the
    GPU frees up' ack instead of igniting it (measured live 2026-08-11: three
    operator turns answered by the ack, none by cognition)".

    With one resident model on four slots, chat is slot 0 and the agent build is
    slot 1 running the SAME weights — there is no hotswap to thrash and nothing
    to evict, so the ack bought nothing and cost the turn its cognition.

    WHAT THE TEST WAS REALLY FOR SURVIVES, and it is asserted here: a chat turn
    during a build still never generates on the operator's thread, and it is
    still captured durably for the next episode. Only the canned pre-delivery is
    gone; the answer now comes from the mind at AUTO.
    """
    monkeypatch.setattr("runtime.client.get_runtime_client", lambda: _Boom())

    gpu_lease.acquire_gpu_for_build("build")
    result = handle_operator_chat("What is Python?", runtime=_runtime())

    assert result["ok"] is True
    assert result["queued"] is True
    # The lease is inert under lane parallelism, so this is the PLAIN queued
    # shape — identical to the no-build case below, which is the whole point.
    assert result["delivery"]["rendered_by"] == "queued"
    assert result["message"] == ""  # the spinner holds; cognition answers at AUTO
    pending = operator_queue.peek_inputs()
    assert len(pending) == 1  # captured for the next episode — never lost
    assert pending[0].metadata.get("message") == "What is Python?"


def test_serial_world_also_keeps_the_turn_open_for_auto(monkeypatch):
    """A serial runtime may delay the turn, but admission never answers it."""
    monkeypatch.setenv("OMEGA_LANE_PARALLEL", "0")
    monkeypatch.setattr("runtime.client.get_runtime_client", lambda: _Boom())

    gpu_lease.acquire_gpu_for_build("build")
    result = handle_operator_chat("What is Python?", runtime=_runtime())

    assert result["queued"] is True
    assert result["delivery"] == {"text": "", "rendered_by": "queued"}
    assert result["message"] == ""
    pending = operator_queue.peek_inputs()
    assert len(pending) == 1
    assert "pre_delivered_response_text" not in pending[0].metadata


def test_gate_is_inert_when_gpu_free(monkeypatch):
    # No build holding the GPU -> the build-busy gate is bypassed. Chat text still
    # queues (deliver-at-AUTO, operator 2026-07-03) but WITHOUT the busy ack —
    # the plain queued shape, and nothing generates on the operator's thread.
    class _Boom:
        def health_body(self):
            return {"status": "ok"}

        def generate_completion(self, **k):
            raise AssertionError("chat text never generates on the operator thread")

    monkeypatch.setattr("runtime.client.get_runtime_client", lambda: _Boom())

    result = handle_operator_chat("What is Python?", runtime=_runtime())

    assert result["ok"] is True
    assert result["queued"] is True
    assert result["delivery"]["rendered_by"] == "queued"  # NOT the busy ack
    assert result["message"] == ""  # spinner holds for the poll
    pending = operator_queue.peek_inputs()
    assert len(pending) == 1


def test_operator_turn_always_uses_queue_before_auto_inference(monkeypatch):
    monkeypatch.setattr("runtime.client.get_runtime_client", lambda: _Boom())

    result = handle_operator_chat("What is Python?", runtime=_runtime())

    assert result["queued"] is True
    assert result["delivery"] == {"text": "", "rendered_by": "queued"}
    assert len(operator_queue.peek_inputs()) == 1


def test_flag_off_disables_the_gate(monkeypatch):
    # Escape hatch: with the lease gating off, a held lease is invisible — the turn
    # takes the plain queued path, never the busy ack.
    monkeypatch.setenv("OMEGA_GPU_BUILD_LEASE", "0")

    class _Fake:
        def health_body(self):
            return {"status": "ok", "capabilities": {"coding_model": True}}

        def generate_completion(self, **k):
            return {"text": "ok.", "tool_calls": []}

    monkeypatch.setattr("runtime.client.get_runtime_client", lambda: _Fake())

    gpu_lease.acquire_gpu_for_build("build")
    result = handle_operator_chat("What is Python?", runtime=_runtime())
    assert result["delivery"]["rendered_by"] != "build_busy_ack"
    assert result["queued"] is True
