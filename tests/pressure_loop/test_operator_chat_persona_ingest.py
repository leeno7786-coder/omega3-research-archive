"""Operator-chat ingress no longer ingests fresh memory (operator 2026-07-16).

Perception before consolidation is STRUCTURAL now: a stated personal fact is NOT written to
system memory at chat ingress. It rides the contributor into the episode, is held in the
per-episode SRAM bucket (memory.episode_sram, opened at episode open), and flushes into
system memory at DeepSleep/close — after that episode's recall. So a stated fact can never be
recalled the same turn it's stated ("Omega remembers something before it's told").

This suite asserts the ingress seam writes NOTHING (the old pre-cascade persona ingest is
gone). The bucket + default-consolidator behaviour is covered by
tests/memory/test_episode_sram.py; the persona-ingest orchestrator by test_persona_ingest.py;
the end-to-end held-then-flushed recall by test_persona_recall_e2e.py.
"""

from __future__ import annotations

import pytest

CANONICAL_TEXT = (
    "I'm Noah, I have a sister named Miranda, a son named Abel, "
    "and I go to college for cybersecurity."
)


@pytest.fixture(autouse=True)
def _isolate_persona_router(tmp_path, monkeypatch):
    import core.persona_router as pr

    monkeypatch.setattr(pr, "OMEGA_ROOT", tmp_path)
    monkeypatch.setattr(pr, "_INDEX_PATH", tmp_path / "guardian" / "state" / "persona_index.json")


def test_handle_operator_chat_writes_no_persona_facts_at_ingress(monkeypatch):
    """The old pre-cascade persona ingest wrote person stubs at chat ingress — the race the
    operator caught. It's gone: driving handle_operator_chat must leave NO persona records
    behind. The paused gate short-circuits right after the (now inert) ingress seam, and the
    old code ingested BEFORE that gate, so any surviving ingress write would show up here."""
    from pressure_loop import operator_chat, pressure_loop_pause

    monkeypatch.setattr(pressure_loop_pause, "is_pressure_loop_paused", lambda: True)

    result = operator_chat.handle_operator_chat(CANONICAL_TEXT)
    # A pause HOLDS the line rather than discarding the turn (AGENTS.md §4.0),
    # so the short-circuit this test rides on now returns ok/queued.
    assert result["ok"] is True and result["status"] == "substrate_paused"

    import core.persona_router as pr

    assert not pr._INDEX_PATH.exists(), (
        "chat ingress wrote persona records — fresh intake must be deferred to the SRAM "
        "bucket, never written on the operator's own thread"
    )


def test_handle_operator_chat_is_decoupled_from_persona_ingest(monkeypatch):
    """Ingress never calls persona ingest anymore, so even a persona_ingest that raises can
    never affect chat ingress — the two pipelines are separate."""
    import memory.persona_ingest as persona_ingest_module
    from pressure_loop import operator_chat, pressure_loop_pause

    def _boom(*_a, **_kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(persona_ingest_module, "ingest_persona_statement", _boom)
    monkeypatch.setattr(pressure_loop_pause, "is_pressure_loop_paused", lambda: True)

    result = operator_chat.handle_operator_chat(CANONICAL_TEXT)  # must not raise
    assert result["ok"] is True
    assert result["status"] == "substrate_paused"
