"""MRIL v2 — lobe-specific remember_* tools and inference delivery."""

from __future__ import annotations

import json

import pytest

from auto_loop.mril_tools import (
    ALL_REMEMBER_TOOL_NAMES,
    format_remembered_knowledge_block,
    invoke_remember_tool,
    route_memory_tool,
    route_memory_tools,
    memory_recall_required,
)
from auto_loop.mril_tools.run import run_forced_memory_tools
from auto_loop.mril_tools.candidate_hygiene import finalize_recall_results
from auto_loop.promptpack.run import build_prompt_pack
from auto_loop.execute.run import execute
from auto_loop._auto_state import MemoryEnvelope
from auto_loop.auto import run_auto_front


class _Candidate:
    def __init__(self, *, mid="mem_1", coord=None, score=0.7, rerank=0.91, summary="snippet"):
        self.id = mid
        self.coord = coord or [8, 0, 0]
        self.score = score
        self.rerank_score = rerank
        self.provenance = {"timestamp": "2026-01-01"}
        self.summary = summary
        self.title = "title"


class _Result:
    def __init__(self, candidates):
        self.candidates = candidates


class _Verdict:
    def __init__(self, *, status="verified", candidates=None, reason=""):
        self.status = status
        self.reason = reason
        self.answer_payload = {"candidates": candidates or []} if candidates is not None else None


def _candidate_payload(candidate: _Candidate) -> dict:
    return {
        "memory_id": candidate.id,
        "coord": candidate.coord,
        "score": candidate.rerank_score,
        "summary": candidate.summary,
        "title": candidate.title,
    }


def _verified_result(*candidates: _Candidate) -> _Verdict:
    return _Verdict(
        status="verified",
        candidates=[_candidate_payload(candidate) for candidate in candidates],
    )


def test_all_twelve_remember_tools_registered():
    expected = {
        "remember_identity",
        "remember_persona",
        "remember_knowledge",
        "remember_reasoning",
        "remember_situation",
        "remember_procedure",
        "remember_journal",
        "remember_vision",
        "remember_code",
        "remember_time",
        "remember_workflow",
        "remember_dev",
    }
    assert set(ALL_REMEMBER_TOOL_NAMES) == expected


def test_remember_code_uses_exact_lobe_eight():
    seen = {}

    def fake_read(spec):
        seen["allowed"] = list(spec.allowed_lobes)
        seen["denied"] = list(spec.denied_lobes)
        # the recalled memory's text must lexically match the query ("Python project
        # layout") or the relevance floor (_partition_responsive, Gate-1 spec) correctly
        # blocks it from the answer context — a real recall for this query WOULD match.
        return _verified_result(_Candidate(summary="Python project layout"))

    out = invoke_remember_tool(
        {
            "tool": "remember_code",
            "query": "Python project layout",
            "allowed_lobes": [8],
            "memory_type": "episodic",
        },
        "ep_1",
        memory=fake_read,
    )
    assert seen["allowed"] == [8]
    assert seen["denied"] == [4, 6, 11]  # situational now denied for non-situational lanes
    assert out["kind"] == "remembered_knowledge"
    assert out["tool"] == "remember_code"
    assert out["lane"] == "coding"
    assert out["lobe"] == 8
    assert out["recall_reason"] == "operator_query"
    assert out["memory_type"] == "episodic"
    assert out["confidence"] == 0.91
    assert out["evidence"][0]["memory_id"] == "mem_1"
    assert out["evidence"][0]["coord"] == [8, 0, 0]
    assert out["evidence"][0]["remembered_as"] == "Python project layout"


def test_remember_evidence_carries_the_verified_source_citation():
    source_proof = {
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
    out = invoke_remember_tool(
        {
            "tool": "remember_knowledge",
            "query": "Python project layout",
            "allowed_lobes": [2],
            "memory_type": "semantic",
        },
        "ep_citation",
        memory=lambda _spec: _Verdict(
            status="verified",
            candidates=[
                {
                    "memory_id": "2.00.python-layout",
                    "coord": [2, 0, 0],
                    "score": 0.93,
                    "summary": "Python project layout uses a source root.",
                    "title": "Python project layout",
                    "source_proof": source_proof,
                }
            ],
        ),
    )

    assert out["evidence"][0]["source_citation"] == source_proof


def test_tool_result_recall_includes_the_verified_bounded_excerpt():
    out = invoke_remember_tool(
        {
            "tool": "remember_situation",
            "query": "what did Slack say about shipping?",
            "allowed_lobes": [4],
            "memory_type": "episodic",
        },
        "ep_plugin_recall",
        memory=lambda _spec: _Verdict(
            status="verified",
            candidates=[
                {
                    "memory_id": "4.06.plugin-result",
                    "coord": [4, 6, 0],
                    "score": 0.91,
                    "summary": "slack.official search_messages result (completed)",
                    "title": "slack.official search_messages result",
                    "subtopic": "tool_result",
                    "text": (
                        "Event kind: tool_result\n"
                        'Payload: {"data":"shipping friday","channel":"#release"}'
                    ),
                }
            ],
        ),
    )

    assert out["evidence"][0]["memory_id"] == "4.06.plugin-result"
    assert "shipping friday" in out["evidence"][0]["remembered_as"]


def test_retrospective_plugin_query_targets_the_outcome_subdomain():
    from auto_loop.mril_tools.router import route_memory_tool

    entry = route_memory_tool("what did Slack say about shipping?")

    assert entry["tool"] == "remember_situation"
    assert entry["allowed_subdomains"] == [6]


def test_semantic_verified_candidate_survives_mril_relevance_floor():
    out = invoke_remember_tool(
        {
            "tool": "remember_persona",
            "query": "Does Mitchell have any children?",
            "allowed_lobes": [1],
            "memory_type": "episodic",
        },
        "ep_semantic",
        memory=lambda spec: _Verdict(
            status="verified",
            candidates=[
                {
                    "memory_id": "1.01.children",
                    "coord": [1, 1, 0],
                    "score": 0.88,
                    "summary": "Mitchell Ray family detail.",
                    "title": "Mitchell Ray children",
                    "text": "Mitchell Ray has 2 daughters.",
                    "semantic_recall": True,
                }
            ],
        ),
    )

    assert out["grounding"] == "VERIFIED_RELEVANT"
    assert [item["memory_id"] for item in out["evidence"]] == ["1.01.children"]


def test_scoped_recall_widens_whole_lobe_when_candidates_are_nonresponsive():
    seen_subdomains: list[object] = []

    def fake_read(spec):
        seen_subdomains.append(spec.allowed_subdomains)
        if spec.allowed_subdomains == [0]:
            return _Verdict(
                status="verified",
                candidates=[
                    {
                        "memory_id": "2.00.python",
                        "coord": [2, 0, 0],
                        "score": 0.70,
                        "summary": "Python is a programming language.",
                        "title": "Python",
                    }
                ],
            )
        return _Verdict(
            status="verified",
            candidates=[
                {
                    "memory_id": "2.19.hash",
                    "coord": [2, 19, 0],
                    "score": 0.91,
                    "summary": "A hash table maps keys to values.",
                    "title": "Hash table",
                }
            ],
        )

    out = invoke_remember_tool(
        {
            "tool": "remember_knowledge",
            "query": "what do you know about hash tables",
            "allowed_lobes": [2],
            "memory_type": "semantic",
            "subdomain": 0,
        },
        "ep_widen",
        memory=fake_read,
    )

    assert seen_subdomains == [[0], None]
    assert out["widened_lobe"] is True
    assert [item["memory_id"] for item in out["evidence"]] == ["2.19.hash"]


def test_final_recall_candidates_are_query_scoped_and_semantically_deduplicated():
    """A precise relationship question reaches the model/HUD as one useful fact.

    This catches the live failure where whole-lobe grounding concatenated identity
    seals, operator anchors, encounter stubs, a generic friend projection, the
    reverse copy of the same relationship, and a code-review skill into 16 hits.
    """
    query = "Who is Noahs best friend?"
    relation = [
        {
            "subject": "operator",
            "facet": "relationship",
            "relation": "best_friend",
            "object": "mitchell ray",
        }
    ]

    by_lobe = {
        0: [
            {
                "memory_id": "0.00.guardian_boundary",
                "coord": [0, 0, 0],
                "score": 0.99,
                "title": "Guardian boundary",
                "summary": "Guardian is the immune boundary for outside input only.",
            },
            {
                "memory_id": "0.00.omega_identity",
                "coord": [0, 0, 0],
                "score": 0.98,
                "title": "Omega identity",
                "summary": "Omega remembers, learns, builds, and grows.",
            },
        ],
        1: [
            {
                "memory_id": "1.00.best-friend",
                "coord": [1, 0, 0],
                "score": 0.97,
                "title": "Noah Lee's best friend: Mitchell Ray",
                "summary": "Noah Lee's best friend is Mitchell Ray.",
                "relation_facets": relation,
            },
            {
                "memory_id": "1.01.best-friend-reverse",
                "coord": [1, 1, 0],
                "score": 0.96,
                "title": "Mitchell Ray — best friend of Noah Lee",
                "summary": "Mitchell Ray is Noah Lee's best friend.",
                "relation_facets": relation,
            },
            {
                "memory_id": "1.00.generic-friend",
                "coord": [1, 0, 0],
                "score": 0.95,
                "title": "Noah Lee's friend: Mitchell Ray",
                "summary": "Noah Lee's friend is Mitchell Ray.",
                "relation_facets": [
                    {
                        "subject": "operator",
                        "facet": "relationship",
                        "relation": "friend",
                        "object": "mitchell ray",
                    }
                ],
            },
            {
                "memory_id": "1.00.operator_name",
                "coord": [1, 0, 0],
                "score": 0.94,
                "title": "Operator name",
                "summary": "The operator is Noah Lee.",
            },
            {
                "memory_id": "1.01.first-encounter",
                "coord": [1, 1, 0],
                "score": 0.90,
                "title": "First encounter with Mitchell Ray",
                "summary": "First encounter with Mitchell Ray.",
            },
        ],
        5: [
            {
                "memory_id": "5.01.qwenskill-code-review",
                "coord": [5, 1, 0],
                "score": 0.93,
                "title": "Code review",
                "summary": "Best friend code-review practices for constructive feedback.",
            },
        ],
    }

    def fake_read(spec):
        lobe = list(spec.allowed_lobes)[0]
        return _Verdict(status="verified", candidates=by_lobe[lobe])

    results, envelope = run_forced_memory_tools(
        {
            "memory_required": True,
            "memory_query": query,
            "memory_reason": "operator relationship recall",
            "memory_scope": ("persona", "identity", "procedure"),
            "memory_tools": (
                {
                    "tool": "remember_persona",
                    "query": query,
                    "allowed_lobes": [1],
                    "limit": 20,
                },
                {
                    "tool": "remember_identity",
                    "query": query,
                    "allowed_lobes": [0],
                    "limit": 20,
                },
                {
                    "tool": "remember_procedure",
                    "query": query,
                    "allowed_lobes": [5],
                },
            ),
        },
        "ep-best-friend",
        memory=fake_read,
    )

    final_evidence = [evidence for result in results for evidence in result.get("evidence") or []]
    assert [item["memory_id"] for item in final_evidence] == ["1.00.best-friend"]
    assert [item["memory_id"] for item in envelope.items] == ["1.00.best-friend"]
    assert envelope.items[0]["content"] == "Noah Lee's best friend is Mitchell Ray."


def test_final_recall_prefers_qualified_relation_over_legacy_base_duplicate():
    relation = {
        "subject": "operator",
        "facet": "relationship",
        "object": "mitchell ray",
    }
    results = finalize_recall_results(
        [
            {
                "tool": "remember_persona",
                "verdict": "verified",
                "evidence": [
                    {
                        "memory_id": "1.00.legacy-friend",
                        "confidence": 0.99,
                        "remembered_as": "Noah Lee's friend is Mitchell Ray.",
                        "relation_facets": [{**relation, "relation": "friend"}],
                    },
                    {
                        "memory_id": "1.00.best-friend",
                        "confidence": 0.95,
                        "remembered_as": "Noah Lee's best friend is Mitchell Ray.",
                        "relation_facets": [{**relation, "relation": "best_friend"}],
                    },
                ],
            }
        ],
        "Who is Noahs friend?",
    )

    assert [item["memory_id"] for item in results[0]["evidence"]] == ["1.00.best-friend"]


def test_final_recall_deduplicates_facetless_live_relationship_candidates():
    """The verifier's live text-only shape still collapses legacy relation copies."""
    results = finalize_recall_results(
        [
            {
                "tool": "remember_persona",
                "verdict": "verified",
                "evidence": [
                    {
                        "memory_id": "1.00.72e32588",
                        "confidence": 0.58,
                        "remembered_as": (
                            "Noah Lee's best friend is Mitchell Ray. "
                            "The operator's best friend is Mitchell Ray."
                        ),
                        "relation_facets": [],
                    },
                    {
                        "memory_id": "1.00.f9156a1a",
                        "confidence": 0.10,
                        "remembered_as": (
                            "Noah Lee's friend is Mitchell Ray. "
                            "The operator's friend is Mitchell Ray."
                        ),
                        "relation_facets": [],
                    },
                    {
                        "memory_id": "1.01.8e279961",
                        "confidence": 0.10,
                        "remembered_as": (
                            "Mitchell Ray is Noah Lee's friend — the operator's friend."
                        ),
                        "relation_facets": [],
                    },
                ],
            }
        ],
        "What do you remember about Mitchell Ray?",
    )

    assert [item["memory_id"] for item in results[0]["evidence"]] == ["1.00.72e32588"]


def test_final_recall_deduplicates_mixed_structured_and_legacy_relationships():
    """New enriched relations must collapse their older text-only base copies."""
    results = finalize_recall_results(
        [
            {
                "tool": "remember_persona",
                "verdict": "verified",
                "evidence": [
                    {
                        "memory_id": "1.00.72e32588",
                        "confidence": 0.58,
                        "remembered_as": (
                            "Noah Lee's best friend is Mitchell Ray. "
                            "The operator's best friend is Mitchell Ray."
                        ),
                        "relation_facets": [
                            {
                                "subject": "operator",
                                "facet": "relationship",
                                "relation": "best_friend",
                                "object": "mitchell ray",
                            }
                        ],
                    },
                    {
                        "memory_id": "1.00.f9156a1a",
                        "confidence": 0.10,
                        "remembered_as": (
                            "Noah Lee's friend is Mitchell Ray. "
                            "The operator's friend is Mitchell Ray."
                        ),
                        "relation_facets": [],
                    },
                ],
            }
        ],
        "What do you remember about Mitchell Ray?",
    )

    assert [item["memory_id"] for item in results[0]["evidence"]] == ["1.00.72e32588"]


def test_final_recall_removes_persona_scaffolding_from_subject_recall():
    """A profile root is an index anchor, not answer evidence."""
    results = finalize_recall_results(
        [
            {
                "tool": "remember_persona",
                "verdict": "verified",
                "evidence": [
                    {
                        "memory_id": "1.01.11c8ee78",
                        "confidence": 0.90,
                        "remembered_as": "First encounter with Mitchell Ray.",
                        "title": "persona:Mitchell Ray",
                    },
                    {
                        "memory_id": "1.01.020b2298",
                        "confidence": 0.80,
                        "remembered_as": "Mitchell Ray has 2 daughters.",
                        "title": "Mitchell Ray children: 2 daughters",
                    },
                ],
            }
        ],
        "What do you remember about Mitchell Ray?",
    )

    assert [item["memory_id"] for item in results[0]["evidence"]] == ["1.01.020b2298"]


def test_final_recall_scopes_multi_person_organization_role_question():
    query = (
        "Yes, Mitchell is a part owner, but Noah is also a part owner. "
        "What do Mitchell and Noah do at Omega AI?"
    )
    results = finalize_recall_results(
        [
            {
                "tool": "remember_identity",
                "verdict": "verified",
                "evidence": [
                    {
                        "memory_id": "0.00.omega_identity",
                        "coord": [0, 0, 0],
                        "confidence": 0.99,
                        "remembered_as": "Omega is a persistent autonomous partner.",
                    }
                ],
            },
            {
                "tool": "remember_persona",
                "verdict": "verified",
                "evidence": [
                    {
                        "memory_id": "1.01.mitchell_owner",
                        "coord": [1, 1, 1],
                        "confidence": 0.95,
                        "remembered_as": "Mitchell Ray is part owner of Omega AI LLC.",
                        "relation_facets": [
                            {
                                "subject": "mitchell ray",
                                "facet": "organization_role",
                                "relation": "part owner",
                                "object": "Omega AI LLC",
                            }
                        ],
                    },
                    {
                        "memory_id": "1.00.operator_onboarding",
                        "coord": [1, 0, 1],
                        "confidence": 0.94,
                        "remembered_as": (
                            "The operator's onboarding notes are: Noah Lee is a software "
                            "engineer and I also own a company called Omega AI LLC."
                        ),
                    },
                    {
                        "memory_id": "1.00.best_friend",
                        "coord": [1, 0, 1],
                        "confidence": 0.93,
                        "remembered_as": "Noah Lee's best friend is Mitchell Ray.",
                        "relation_facets": [
                            {
                                "subject": "operator",
                                "facet": "relationship",
                                "relation": "best_friend",
                                "object": "mitchell ray",
                            }
                        ],
                    },
                ],
            },
            {
                "tool": "remember_code",
                "verdict": "verified",
                "evidence": [
                    {
                        "memory_id": "2.19.hash_table",
                        "coord": [2, 19, 1],
                        "confidence": 0.80,
                        "remembered_as": "A hash table maps keys to values.",
                    }
                ],
            },
        ],
        query,
    )

    retained = [
        item["memory_id"]
        for result in results
        for item in result.get("evidence") or []
    ]
    assert retained == ["1.01.mitchell_owner", "1.00.operator_onboarding"]


def test_empty_recall_returns_none_memory_type():
    out = invoke_remember_tool(
        {
            "tool": "remember_knowledge",
            "query": "unknown topic",
            "allowed_lobes": [2],
            "memory_type": "episodic",
        },
        "ep_1",
        memory=lambda spec: _Verdict(
            status="unknown", reason="no canonical memory matched the query"
        ),
    )
    assert out["memory_type"] == "none"
    assert out["evidence"] == []
    assert out["confidence"] == 0.0


def test_unverified_recall_returns_verdict_marker_without_evidence():
    out = invoke_remember_tool(
        {
            "tool": "remember_knowledge",
            "query": "neuron",
            "allowed_lobes": [2],
            "memory_type": "episodic",
        },
        "ep_1",
        memory=lambda spec: _Verdict(
            status="ambiguous", reason="query resolves to multiple senses"
        ),
    )
    assert out["memory_type"] == "none"
    assert out["evidence"] == []
    assert out["verdict"] == "ambiguous"
    assert out["pressure_signal"] == "sense_resolution"


def test_recall_read_failure_raises():
    def boom(spec):
        raise RuntimeError("substrate down")

    with pytest.raises(RuntimeError, match="substrate down"):
        invoke_remember_tool(
            {
                "tool": "remember_identity",
                "query": "who am I",
                "allowed_lobes": [0],
                "memory_type": "episodic",
            },
            "ep_1",
            memory=boom,
        )


def test_route_project_code_query():
    tool = route_memory_tools("What do you remember about my Python project?")[0]
    assert tool["tool"] == "remember_code"
    assert len(route_memory_tools("What do you remember about my Python project?")) == 1


def test_route_memory_tool_returns_one_entry():
    assert route_memory_tools("Who am I and what are my traits?")[0]["tool"] == "remember_identity"


def test_format_remembered_knowledge_block_uses_required_label():
    block = format_remembered_knowledge_block(
        [
            {
                "kind": "remembered_knowledge",
                "tool": "remember_code",
                "memory_type": "episodic",
                "evidence": [{"remembered_as": "uses pytest"}],
            }
        ]
    )
    assert block.startswith("Remembered Knowledge:")
    lowered = block.lower()
    for forbidden in ("context", "retrieved", "provided information", "relevant information"):
        assert forbidden not in lowered


def test_mril_promptpack_skips_relevant_memory_block():
    pack = {
        "action": "RESPONSE",
        "memory_required": True,
        "memory_delivery_mode": "single_hop_system",
        "memory_query": "What do you remember about my Python project?",
        "memory_tools": (route_memory_tool("What do you remember about my Python project?"),),
        "execution_intent": "respond",
        "execution_inputs": {},
        "source_action_goal": {
            "target": {"question": "What do you remember about my Python project?"}
        },
    }
    env = MemoryEnvelope(
        memory_required=True,
        memory_status="skipped",
        memory_reason="mril",
        memory_scope=(),
        items=(),
        error=None,
    )
    pp = build_prompt_pack(pack, env)
    assert "memory_block" not in pp.context_blocks
    assert pp.context_blocks.get("remembered_knowledge_tool_results") == ()


def test_execute_runs_forced_memory_tools_system_side_no_generate():
    rt_calls = []
    tool_calls = []

    class FakeRuntime:
        def health_body(self):
            return {"status": "ok", "capabilities": {"coding_model": True}}

        def generate_completion(self, **kw):
            rt_calls.append(kw)
            return {"text": "I remember your pytest layout.", "tokens_in": 1, "tokens_out": 2}

    def fake_read(spec):
        tool_calls.append(list(spec.allowed_lobes))
        return _verified_result(_Candidate(coord=[8, 0, 0], summary="Python project pytest layout"))

    pack = {
        "action": "RESPONSE",
        "memory_required": True,
        "memory_delivery_mode": "single_hop_system",
        "memory_query": "What do you remember about my Python project?",
        "memory_tools": (route_memory_tool("What do you remember about my Python project?"),),
        "execution_intent": "respond",
        "execution_inputs": {},
        "source_action_goal": {
            "target": {"question": "What do you remember about my Python project?"}
        },
    }
    env = MemoryEnvelope(
        memory_required=True,
        memory_status="skipped",
        memory_reason="mril",
        memory_scope=(),
        items=(),
        error=None,
    )
    pp = build_prompt_pack(pack, env)
    res = execute(
        pp,
        "TEXT_REASONING",
        pack,
        env,
        "ep_1",
        runtime_client=FakeRuntime(),
        memory=fake_read,
    )
    # Recall TOOLS are a system-side substrate read (no model hop) for any
    # memory_required episode.
    assert tool_calls == [[8]]
    # A terminal RESPONSE is now ONE model hop: the composer that used to run
    # immediately before the answer model was retired, and the host builds its
    # prompt directly. The verified recall still reaches the answer model as data.
    assert len(rt_calls) == 1
    assert "Python project pytest layout" in rt_calls[0]["prompt"]
    assert res["outputs"]["remembered_knowledge_tool_results"]


def test_mril_recall_failure_sets_continuity_failure_kind():
    class FakeRuntime:
        def health_body(self):
            return {"status": "ok", "capabilities": {"coding_model": True}}

        def generate_completion(self, **kw):
            return {"text": "should not run", "tokens_in": 1, "tokens_out": 1}

    pack = {
        "action": "RESPONSE",
        "memory_required": True,
        "memory_delivery_mode": "single_hop_system",
        "memory_query": "Who am I?",
        "memory_tools": (route_memory_tool("Who am I and what are my traits?"),),
        "execution_intent": "respond",
        "execution_inputs": {},
        "source_action_goal": {"target": {"question": "Who am I?"}},
    }
    env = MemoryEnvelope(
        memory_required=True,
        memory_status="skipped",
        memory_reason="mril",
        memory_scope=(),
        items=(),
        error=None,
    )
    pp = build_prompt_pack(pack, env)

    def boom(spec):
        raise OSError("unavailable")

    res = execute(
        pp,
        "TEXT_REASONING",
        pack,
        env,
        "ep_1",
        runtime_client=FakeRuntime(),
        memory=boom,
    )
    assert res["failure_kind"] == "memory_continuity_substrate_invalid"
    assert res["outputs"].get("response_text") is None


def test_run_auto_front_uses_single_lobe_reads_only():
    legacy_reads = []
    mril_reads = []

    class Ep:
        episode_id = "ep_mril"
        pressure = {}
        global_ = {"action_goal": {"intent": "RESPOND", "episode_id": "ep_mril"}}
        meta = {
            "execution_action_pack": {
                "episode_id": "ep_mril",
                "action": "RESPONSE",
                "tool": None,
                "tool_reason": None,
                "memory_required": True,
                "memory_reason": "operator query",
                "memory_scope": ["coding", "workflows"],
                "memory_query": "What do you remember about my Python project?",
                "memory_delivery_mode": "single_hop_system",
                "memory_tools": (
                    route_memory_tool("What do you remember about my Python project?"),
                ),
                "execution_intent": "respond with remembered knowledge",
                "execution_inputs": {},
                "execution_constraints": {
                    "planning_only": True,
                    "no_inference_in_meta": True,
                    "no_execution_in_meta": True,
                    "no_model_fields": True,
                },
                "source_action_goal": {
                    "intent": "RESPOND",
                    "episode_id": "ep_mril",
                    "target": {"question": "What do you remember about my Python project?"},
                },
            }
        }
        auto = None

    class FakeRuntime:
        def health_body(self):
            return {"status": "ok", "capabilities": {"coding_model": True}}

        def generate_completion(self, **kw):
            return {"text": "natural memory use", "tokens_in": 1, "tokens_out": 2}

    def tracking_read(spec):
        if len(spec.allowed_lobes) > 1:
            legacy_reads.append(spec.allowed_lobes)
        else:
            mril_reads.append(spec.allowed_lobes)
        return _verified_result(_Candidate(coord=[8, 0, 0], summary="Python project pytest layout"))

    run_auto_front(Ep, runtime_client=FakeRuntime(), memory=tracking_read)
    assert legacy_reads == []
    assert mril_reads == [[8]]
    assert Ep.auto["memory"]["memory_status"] == "success"
    assert Ep.auto["outputs"]["remembered_knowledge_tool_results"]


def test_memory_recall_required_flag():
    assert memory_recall_required({"memory_required": True, "memory_tools": [{}]})
    assert not memory_recall_required({"memory_required": False})


# ── Cut 1: verified DME activation traces ride the remember receipt ─────────

_TRACE = {
    "type": "dme_activation_trace",
    "trace_id": "trace-1",
    "cycle_id": "ep-mril-remember_code",
    "episode_id": "ep",
    "query_hash": "abc",
    "projection_generation": "snap-1",
    "backend": "numpy",
    "cue_receipt_id": None,
    "query_coordinates": [[8, 0, 1]],
    "candidates": [],
    "associations": [],
    "schema_version": "omega.dme_activation_trace.v1",
}


def _verdict_with_meta(candidates, meta):
    v = _Verdict(status="verified", candidates=candidates)
    v.answer_payload["meta"] = dict(meta)
    return v


def _strip_receipt(out: dict) -> dict:
    return {k: v for k, v in out.items() if k != "receipt"}


def test_remember_receipt_carries_verified_dme_trace():
    payload = [_candidate_payload(_Candidate(summary="Python project layout"))]
    with_trace = invoke_remember_tool(
        {"tool": "remember_code", "query": "Python project layout", "allowed_lobes": [8]},
        "ep",
        memory=lambda _spec: _verdict_with_meta(payload, {"dme_activation_trace": _TRACE}),
    )
    without_trace = invoke_remember_tool(
        {"tool": "remember_code", "query": "Python project layout", "allowed_lobes": [8]},
        "ep",
        memory=lambda _spec: _verdict_with_meta(payload, {}),
    )
    assert with_trace["evidence"] == without_trace["evidence"]
    assert with_trace["receipt"] == {"dme_activation_traces": [_TRACE]}
    assert without_trace["receipt"] == {}
    # only the receipt differs; evidence/grounding/verdict are byte-identical
    assert _strip_receipt(with_trace) == _strip_receipt(without_trace)


def test_remember_receipt_accumulates_traces_across_widening_attempts():
    seen = []

    def fake_read(spec):
        seen.append(spec.cycle_id)
        trace = dict(_TRACE, trace_id=f"trace-{len(seen)}", cycle_id=spec.cycle_id)
        if len(seen) < 3:
            # first two attempts verify nothing responsive → widen
            return _verdict_with_meta([], {"dme_activation_trace": trace})
        return _verdict_with_meta(
            [_candidate_payload(_Candidate(summary="Python project layout"))],
            {"dme_activation_trace": trace},
        )

    out = invoke_remember_tool(
        {"tool": "remember_code", "query": "Python project layout", "subdomain": 3},
        "ep",
        memory=fake_read,
    )
    assert out["widened_unsorted"] is True and out["widened_lobe"] is True
    traces = out["receipt"]["dme_activation_traces"]
    assert [t["trace_id"] for t in traces] == ["trace-1", "trace-2", "trace-3"]
    assert [t["cycle_id"] for t in traces] == seen


def test_remember_no_hit_path_still_returns_trace_receipt():
    out = invoke_remember_tool(
        {"tool": "remember_code", "query": "Python project layout", "allowed_lobes": [8]},
        "ep",
        memory=lambda _spec: _verdict_with_meta([], {"dme_activation_trace": _TRACE}),
    )
    assert out["memory_type"] == "none"
    assert out["evidence"] == []
    assert out["receipt"] == {"dme_activation_traces": [_TRACE]}


def test_remember_receipt_ignores_non_dict_trace_metadata():
    payload = [_candidate_payload(_Candidate(summary="Python project layout"))]
    out = invoke_remember_tool(
        {"tool": "remember_code", "query": "Python project layout", "allowed_lobes": [8]},
        "ep",
        memory=lambda _spec: _verdict_with_meta(payload, {"dme_activation_trace": "garbage"}),
    )
    assert out["receipt"] == {}


# ── Cut 3: a verifier-validated RecallBasis anchors MRIL relevance ──────────


def test_verifier_validated_basis_anchors_relevance_but_bare_basis_id_does_not():
    """The verifier marks rows it admitted through an independently revalidated
    RecallBasis with recall_basis_validated=True. MRIL's relevance floor treats
    that mark as an anchor (routing proof), exactly like strong DME provenance.
    A bare recall_basis_id without the verifier's mark anchors nothing —
    only the verifier decides validity."""
    zero_overlap = {
        "memory_id": "r1", "coord": [2, 0, 1], "score": 0.9,
        "summary": "Observed Saturn through the Dobsonian", "title": "Stargazing night",
        "provenance": {"source_id": "src"},
    }
    validated = {**zero_overlap, "recall_basis_id": "rb1", "recall_basis_validated": True}
    unvalidated = {**zero_overlap, "recall_basis_id": "rb1"}
    q = "Which telescope did I use?"
    out_ok = invoke_remember_tool(
        {"tool": "remember_knowledge", "query": q, "allowed_lobes": [2]}, "ep",
        memory=lambda _s: _Verdict(status="verified", candidates=[validated]),
    )
    out_no = invoke_remember_tool(
        {"tool": "remember_knowledge", "query": q, "allowed_lobes": [2]}, "ep",
        memory=lambda _s: _Verdict(status="verified", candidates=[unvalidated]),
    )
    assert out_ok["grounding"] == "VERIFIED_RELEVANT"
    assert [e["memory_id"] for e in out_ok["evidence"]] == ["r1"]
    assert out_no["grounding"] == "VERIFIED_NONRESPONSIVE"
    assert out_no["evidence"] == []
