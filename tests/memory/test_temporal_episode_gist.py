"""A lobe-9 episode record must be READABLE as a recall hit (live 2026-09-05).

Every recalled episode used to be titled with its id and summarised as
"episode <id> (completed)", so the answer model saw eight identical-looking
hits and grounded nothing on them. The operator's words and the reply's gist
are the episode; the id stays in enrichment.
"""

from __future__ import annotations

from types import SimpleNamespace

from pressure_loop.temporal_store import _episode_gist


def _episode(message: str | None, reply: str | None, intent: str = "RESPOND"):
    contributors = []
    if message:
        contributors.append({"id": "operator_query", "detail": {"message": message, "cycle_id": "c1"}})
    ep = SimpleNamespace(pressure={"contributors": contributors}, episode_id="ep-1")
    body = {"intent": intent, "action": "RESPONSE", "outcome": {"artifacts": {"response_text": reply}}}
    return ep, body


def test_operator_turn_titles_and_summarises_the_record():
    ep, body = _episode("Write a basic hello world script", "Here's a basic hello world in Python: print('Hello')")
    title, summary = _episode_gist(ep, body, "ep-1", "completed")
    assert title == "RESPOND: Write a basic hello world script"
    assert summary.startswith("operator said: Write a basic hello world script → omega replied: Here's a basic hello world")
    assert "ep-1" not in title


def test_a_failed_episode_says_so_and_long_text_is_clipped():
    ep, body = _episode("x" * 300, "y" * 500, intent="USE_SKILL")
    title, summary = _episode_gist(ep, body, "ep-2", "failed")
    assert title.startswith("USE_SKILL: ") and len(title) <= len("USE_SKILL: ") + 72
    assert summary.startswith("[failed] operator said: ")
    assert len(summary) < 450


def test_an_autonomous_episode_falls_back_to_the_reply_then_the_id():
    ep, body = _episode(None, "Learned three facts about CRDTs.", intent="LEARN")
    title, summary = _episode_gist(ep, body, "ep-3", "completed")
    assert title == "LEARN: Learned three facts about CRDTs."
    assert summary == "omega replied: Learned three facts about CRDTs."
    ep, body = _episode(None, None, intent="IDLE")
    assert _episode_gist(ep, body, "ep-4", "completed") == ("ep-4", "episode ep-4 (completed)")
