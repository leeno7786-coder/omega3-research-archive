from __future__ import annotations

from auto_loop.scaffold.runtime import (
    temporal_hits_from_query,
    verified_hits_from_results,
)
from organ.promptorgan.packs.chat.answer import answer_system_prompt, build_answer_prompt


def _results():
    return [
        {
            "tool": "remember_persona",
            "verdict": "verified",
            "evidence": [
                {
                    "memory_id": "1.01.mitchell_owner",
                    "remembered_as": "Mitchell Ray is part owner of Omega AI LLC.",
                },
                {
                    "memory_id": "1.00.noah_owner",
                    "remembered_as": "Noah Lee is part owner of Omega AI LLC.",
                },
            ],
        },
        {
            "tool": "remember_identity",
            "verdict": "conflict",
            "evidence": [
                {
                    "memory_id": "0.00.omega_identity",
                    "remembered_as": "Omega is an autonomous partner.",
                }
            ],
        },
    ]


def test_verified_hits_are_exactly_the_host_admitted_evidence():
    assert verified_hits_from_results(_results()) == {
        "1.01.mitchell_owner": "Mitchell Ray is part owner of Omega AI LLC.",
        "1.00.noah_owner": "Noah Lee is part owner of Omega AI LLC.",
    }


def test_answer_prompt_carries_verified_records_behind_the_ingress_fence():
    query = "What do Mitchell and I both do at Omega AI?"
    prompt = build_answer_prompt(
        query, verified_hits_from_results(_results()), memory_required=True
    )
    assert "Mitchell Ray is part owner" in prompt
    # Recall winners are data, never instructions -- the external-ingress fence
    # is what says so to the answer model.
    assert '<external-data trust="untrusted-human-authored">' in prompt
    assert prompt.rstrip().endswith(query)


def test_verified_source_citation_reaches_answer_prompt_with_basis_guidance():
    source_citation = {
        "evidence_strength": "source_ledger_crypto",
        "source_id": "src_synthetic_01",
        "source_type": "operator_chat",
        "source_session_id": "session-synthetic-01",
        "source_session_timestamp": "2025-04-03T12:30:00+00:00",
        "span_start": 4,
        "span_end": 33,
        "span_hash": "a" * 64,
        "source_content_hash": "b" * 64,
    }
    results = [
        {
            "verdict": "verified",
            "evidence": [
                {
                    "memory_id": "2.00.python-layout",
                    "remembered_as": "Python project layout uses a source root.",
                    "source_citation": source_citation,
                }
            ],
        }
    ]

    hits = verified_hits_from_results(results)
    prompt = build_answer_prompt(
        "How do you know the project layout?", hits, memory_required=True
    )

    for field in source_citation.values():
        assert str(field) in prompt
    assert "If asked how you know" in answer_system_prompt("question")


def test_memory_bound_turn_with_no_hits_refuses_instead_of_guessing():
    prompt = build_answer_prompt("Where did I park?", {}, memory_required=True)
    assert "do not recall" in prompt
    assert "do not guess" in prompt


def test_general_turn_with_no_hits_answers_from_model_knowledge():
    # Recall admission and answer authority are separate contracts: an empty
    # search must not gag a question that was never memory-bound.
    prompt = build_answer_prompt("Why is the sky blue?", {}, memory_required=False)
    assert "Answer the Turn directly and accurately." in prompt
    assert "do not recall" not in prompt
    assert prompt.rstrip().endswith("Why is the sky blue?")


def test_answer_prompt_is_deterministic():
    # The composer that used to build this prompt was a model and could drift
    # between identical turns. The host path must not.
    args = ("What do Mitchell and I both do?", verified_hits_from_results(_results()))
    assert build_answer_prompt(*args, memory_required=True) == build_answer_prompt(
        *args, memory_required=True
    )


def test_temporal_records_arrive_as_dated_hits(monkeypatch):
    monkeypatch.setattr(
        "memory.temporal_recall.recall_temporal",
        lambda query: {
            "label": "yesterday",
            "records": [
                {
                    "observed_at": "2026-07-15T16:00:00+00:00",
                    "title": "TODO tests",
                    "summary": "added pytest coverage",
                }
            ],
        },
    )
    assert temporal_hits_from_query("what did we build yesterday") == {
        "temporal.00.record": "[2026-07-15] TODO tests: added pytest coverage"
    }


def test_temporal_answer_merges_dated_records_with_lane_rows_and_drops_prior_replies(monkeypatch):
    """Live 2026-09-06, "Do you remember what I built yesterday?": the dated block
    alone fed the prompt (the retained builder artifact never reached it), and
    once lane rows were merged, seven transcripts of Omega's own earlier wrong
    replies outweighed the artifact and were repeated as fact."""
    import memory.temporal_recall as tr
    from auto_loop.scaffold.runtime import temporal_answer_hits

    monkeypatch.setattr(
        tr,
        "recall_temporal",
        lambda query, now=None, **kw: {
            "label": "yesterday",
            "range": {},
            "records": [{"memory_id": "11.00.e718aec5", "observed_at": "2026-09-05T20:40:00-05:00", "title": "Builder artifact: countdown.py", "summary": "Builder captured countdown.py"}],
        },
    )
    wrong = "On 2026-09-05 you built web-api-design and vastai"
    results = [
        {
            "tool": "remember_time",
            "verdict": "verified",
            "evidence": [
                {"memory_id": "9.01.aaaa0001", "title": "RESPOND: Do you remember what I built yesterday?", "remembered_as": f"operator said: Do you remember what I built yesterday? → omega replied: {wrong}"},
                {"memory_id": "9.01.aaaa0002", "title": "RESPOND: Do you remember what I built yesterday?", "remembered_as": f"operator: Do you remember what I built yesterday? → omega: {wrong}"},
                {"memory_id": "9.01.aaaa0003", "title": "USE_SKILL: write a countdown script", "remembered_as": "operator said: write a countdown script → omega replied: wrote countdown.py"},
            ],
        },
        {"tool": "remember_dev", "verdict": "verified", "evidence": [{"memory_id": "11.00.a740bf02", "title": "Builder artifact: greeting.py", "remembered_as": "Builder captured greeting.py"}]},
        {"tool": "remember_code", "verdict": "unknown", "evidence": []},
    ]
    hits = temporal_answer_hits("Do you remember what I built yesterday?", results)
    assert list(hits)[0] == "11.00.e718aec5"  # the dated block leads
    assert "11.00.a740bf02" in hits  # the lane's verified artifact rides along
    blob = "\n".join(hits.values())
    assert "web-api-design" not in blob  # no prior reply of Omega's is evidence
    assert "9.01.aaaa0002" not in hits  # identical operator side deduped
    assert hits["9.01.aaaa0003"].endswith("omega replied: wrote countdown.py")  # work episodes keep the reply


def test_episode_transcripts_are_dated_and_still_lose_their_reply_in_temporal_answers(monkeypatch):
    """Live 2026-09-06, "What did I ask you about earlier?": eight undated
    transcripts in score order — the model could not tell which was earlier."""
    from auto_loop.scaffold.runtime import _operator_side_of_transcript, verified_hits_from_results

    results = [
        {
            "tool": "remember_time",
            "verdict": "verified",
            "evidence": [
                {"memory_id": "9.01.aaaa0001", "coord": [9, 1, 0], "observed_at": "2026-09-06T05:46:57+00:00",
                 "title": "RESPOND: What is my dog's name?", "remembered_as": "operator said: What is my dog's name? → omega replied: Biscuit."},
                {"memory_id": "1.00.bedbf082", "coord": [1, 0, 0], "observed_at": "2026-09-06T03:57:00+00:00",
                 "title": "Operator preference: favorite editor: vim", "remembered_as": "operator's favorite editor is vim."},
            ],
        }
    ]
    hits = verified_hits_from_results(results)
    episode = hits["9.01.aaaa0001"]
    assert episode.startswith("[2026-09-0") and "] RESPOND: What is my dog's name?" in episode  # dated, local day
    assert hits["1.00.bedbf082"].startswith("Operator preference")  # non-episode rows unchanged
    assert _operator_side_of_transcript(episode).endswith("operator said: What is my dog's name?")


def test_conversation_recall_turns_are_pointed_at_the_current_conversation():
    """Live 2026-09-06: with the current conversation block and dated transcripts
    both present, "What did I ask you about earlier?" still got "the verified
    data does not contain it" — the register directive pointed at memory only."""
    from organ.promptorgan.packs.chat.answer import build_answer_prompt

    hits = {"9.01.aaaa0001": "[2026-09-06 12:57] RESPOND: What is my dog's name?: operator said: What is my dog's name? → omega replied: Biscuit."}
    prompt = build_answer_prompt("What did I ask you about earlier?", hits, memory_required=False, prompt_type="RESPONSE_QUESTION", multiturn=True)
    assert "Current conversation block above first" in prompt
    plain = build_answer_prompt("What is a mutex?", {}, memory_required=False, prompt_type="RESPONSE_QUESTION", multiturn=True)
    assert "Current conversation block" not in plain
