from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace

import cognition_chain as chain
from auto_loop.intake.run import auto_input_from_episode
from omega_loop.motor.run import attach_motor_program
from pressure_loop.episode_journal import enriched_episode_from_episode
from schema.motor_program import MotorProgram


def _canonical_program(*, episode_id: str = "ep-motor-intake") -> MotorProgram:
    return MotorProgram.from_dict(
        {
            "program_id": f"mp-{episode_id}",
            "parent_program_id": "",
            "outcome_id": "out-motor",
            "episode_id": episode_id,
            "attempt_number": 1,
            "created_at": "2026-07-11T12:00:00Z",
            "outcome_contract": {
                "objective": "research and verify a factual result",
                "acceptance_criteria": ["content is verified"],
                "constraints": ["preserve Omega authority"],
                "priorities": ["correctness"],
            },
            "pathway": {
                "pathway_id": "research-verify",
                "revision": 1,
                "name": "research verify",
                "method": "retrieve structured content and verify it",
                "operational_rationale": "exercise the transport boundary",
                "failed_predecessor_id": "",
                "change_evidence": [],
            },
            "execution_plan": {
                "capability_class": "web_research",
                "required_capabilities": ["web_research"],
                "operations": ["retrieve structured content"],
                "permitted_local_discretion": [],
                "prohibited_deviations": ["change the objective"],
                "return_to_mind_conditions": ["verification unavailable"],
            },
            "evidence_contract": {
                "required_artifacts": ["results"],
                "required_source_proof": ["source"],
                "required_checks": ["fetch_verified"],
                "completion_signals": ["verified content"],
                "disqualifying_failures": ["missing source"],
            },
        }
    )


def test_meta_a_binds_the_execution_contract_before_auto(monkeypatch) -> None:
    calls: list[str] = []
    episode = SimpleNamespace(
        episode_id="ep-motor-order",
        opened_by="test",
        pressure={},
        global_={},
        meta={},
    )

    monkeypatch.setattr(
        "meta_loop.loop.run_meta_a",
        lambda ep: (
            ep.meta.update({"execution_action_pack": {"action": "EXECUTE_MOTOR_PROGRAM"}})
            or ep.meta["execution_action_pack"]
        ),
    )
    monkeypatch.setattr(
        "meta_loop.execution_contract.bind_execution_contract",
        lambda _ep: calls.append("contract"),
    )
    monkeypatch.setattr(
        chain,
        "_start_auto",
        lambda _store, _ep, _status="completed": calls.append("auto"),
    )

    # WO-04: `_start_meta_a` captures the action pack and the acceptance criteria into the
    # episode SRAM bucket the admission seam always opens, so this test supplies that owner.
    from tests.helpers.episode_owner import owned

    with owned(episode):
        chain._start_meta_a(object(), episode)

    assert calls == ["contract", "auto"]


def test_disabled_execution_contracts_bind_nothing_and_still_reach_auto(monkeypatch) -> None:
    calls: list[str] = []
    episode = SimpleNamespace(
        episode_id="ep-motor-disabled",
        opened_by="test",
        pressure={},
        global_={},
        meta={},
    )

    monkeypatch.setattr(
        "meta_loop.loop.run_meta_a",
        lambda ep: (
            ep.meta.update(
                {"execution_action_pack": {"episode_id": ep.episode_id, "action": "USE_SKILL"}}
            )
            or ep.meta["execution_action_pack"]
        ),
    )
    monkeypatch.setattr("meta_loop.execution_contract.motor_programs_enabled", lambda *_a: False)
    monkeypatch.setattr(
        "meta_loop.execution_contract.execution_contract_for",
        lambda _ep: (_ for _ in ()).throw(AssertionError("disabled contracts must not project")),
    )
    monkeypatch.setattr(
        chain,
        "_start_auto",
        lambda _store, _ep, _status="completed": calls.append("auto"),
    )

    # WO-04: the acceptance-criteria capture needs the owner the admission seam always opens.
    from tests.helpers.episode_owner import owned

    with owned(episode):
        chain._start_meta_a(object(), episode)

    assert calls == ["auto"]
    assert "motor_program" not in episode.meta


def test_auto_input_reparses_motor_program_into_independent_transport(monkeypatch) -> None:
    program = _canonical_program()
    episode = SimpleNamespace(
        episode_id="ep-motor-intake",
        opened_by="test",
        pressure={},
        global_={},
        meta={
            "execution_action_pack": {
                "episode_id": "ep-motor-intake",
                "action": "EXECUTE_MOTOR_PROGRAM",
                "source_action_goal": {"episode_id": "ep-motor-intake"},
                "execution_inputs": {"query": "run it", "motor_transport": "USE_TOOL"},
            },
        },
    )
    monkeypatch.setattr("omega_loop.motor.run.motor_programs_shadow_mode", lambda: False)
    attach_motor_program(episode, program)
    canonical = episode.meta["motor_program"]
    transported = episode.meta["execution_action_pack"]["execution_inputs"]["motor_program"]

    auto_input = auto_input_from_episode(episode)

    assert auto_input is not None
    assert auto_input.motor_program == canonical == transported
    assert auto_input.motor_program is not canonical
    assert transported is not canonical
    assert auto_input.execution_pack is not episode.meta["execution_action_pack"]
    assert auto_input.motor_program["fingerprint"] == program.fingerprint()

    auto_input.motor_program["pathway"]["method"] = "transport mutation"
    auto_input.execution_pack["execution_inputs"]["motor_program"]["pathway"]["method"] = (
        "pack mutation"
    )
    assert episode.meta["motor_program"]["pathway"]["method"] == (
        "retrieve structured content and verify it"
    )


def test_live_motor_attachment_synchronizes_the_skill_execution_contract(monkeypatch) -> None:
    program = _canonical_program()
    stale_skill_pack = {
        "episode_id": "ep-motor-intake",
        "objective": "close the pressure message",
        "evidence_required": ["old evidence"],
        "completion_criteria": ["old completion"],
        "source_action_goal": {"episode_id": "ep-motor-intake"},
    }
    episode = SimpleNamespace(
        episode_id="ep-motor-intake",
        opened_by="test",
        pressure={},
        global_={},
        meta={
            "execution_action_pack": {
                "episode_id": "ep-motor-intake",
                "action": "EXECUTE_MOTOR_PROGRAM",
                "source_action_goal": {"episode_id": "ep-motor-intake"},
                "execution_inputs": {
                    "skill_execution_pack": deepcopy(stale_skill_pack),
                },
            },
            "skill_execution_pack": deepcopy(stale_skill_pack),
        },
    )
    monkeypatch.setattr("omega_loop.motor.run.motor_programs_shadow_mode", lambda: False)

    attach_motor_program(episode, program)

    transported = episode.meta["execution_action_pack"]["execution_inputs"]["skill_execution_pack"]
    folded = episode.meta["skill_execution_pack"]
    assert transported == folded
    assert transported is not folded
    assert transported["objective"] == program.outcome_contract.objective
    assert transported["evidence_required"] == ["results", "source", "fetch_verified"]
    assert transported["completion_criteria"] == ["content is verified"]
    assert auto_input_from_episode(episode) is not None


def test_shadow_motor_program_is_telemetry_only_and_auto_is_byte_identical(
    monkeypatch,
) -> None:
    episode = SimpleNamespace(
        episode_id="ep-shadow",
        opened_by="test",
        pressure={},
        global_={},
        meta={
            "execution_action_pack": {
                "episode_id": "ep-shadow",
                "action": "USE_TOOL",
                "tool": "web_fetch",
                "source_action_goal": {"episode_id": "ep-shadow"},
                "execution_inputs": {"query": "unchanged query"},
            }
        },
        omega={},
        auto={},
        brain_turns=[],
        temporal={},
        learning_state={},
        cascade_status="completed",
        state="closed",
    )
    before = deepcopy(episode.meta["execution_action_pack"])
    monkeypatch.setattr("omega_loop.motor.run.motor_programs_shadow_mode", lambda: True)

    attached = attach_motor_program(episode, _canonical_program(episode_id="ep-shadow"))

    assert attached["shadow"] is True
    assert episode.meta["execution_action_pack"] == before
    assert "motor_program" not in episode.meta
    assert episode.meta["shadow_motor_program"] == attached
    auto_input = auto_input_from_episode(episode)
    assert auto_input is not None and auto_input.motor_program is None
    body = enriched_episode_from_episode(episode)
    assert body["motor_program"] is None
    assert body["motor_attempt_assessment"] is None


def test_legacy_shadow_marker_cannot_enter_auto_or_episode_memory() -> None:
    shadow = {**_canonical_program(episode_id="ep-shadow-poison").to_dict(), "shadow": True}
    episode = SimpleNamespace(
        episode_id="ep-shadow-poison",
        opened_by="test",
        pressure={},
        global_={},
        meta={
            "execution_action_pack": {
                "episode_id": "ep-shadow-poison",
                "action": "USE_TOOL",
                "source_action_goal": {"episode_id": "ep-shadow-poison"},
                "execution_inputs": {"query": "unchanged", "motor_program": shadow},
            },
            "motor_program": shadow,
        },
        omega={},
        auto={},
        brain_turns=[],
        temporal={},
        learning_state={},
        cascade_status="completed",
        state="closed",
    )

    auto_input = auto_input_from_episode(episode)
    body = enriched_episode_from_episode(episode)

    assert auto_input is not None
    assert auto_input.motor_program is None
    assert "motor_program" not in auto_input.execution_pack["execution_inputs"]
    assert body["motor_program"] is None
    assert body["motor_attempt_assessment"] is None


def test_enriched_episode_preserves_compact_motor_lineage() -> None:
    episode = SimpleNamespace(
        episode_id="ep-journal",
        opened_by="test",
        pressure={},
        global_={},
        meta={
            "execution_action_pack": {"action": "EXECUTE_MOTOR_PROGRAM"},
            "motor_program": {
                "program_id": "mp1",
                "parent_program_id": "mp0",
                "outcome_id": "out1",
                "episode_id": "ep-journal",
                "attempt_number": 2,
                "created_at": "2026-07-11T00:00:00+00:00",
                "fingerprint": "fp1",
                "outcome_contract": {
                    "objective": "verify journal compaction",
                    "acceptance_criteria": ["journal contains compact lineage"],
                    "constraints": [],
                    "priorities": ["correctness"],
                },
                "pathway": {
                    "pathway_id": "derive",
                    "revision": 1,
                    "name": "derive",
                    "method": "derive and verify",
                    "operational_rationale": "exercise canonical journal input",
                    "failed_predecessor_id": "mp0",
                    "change_evidence": ["full canonical contract"],
                },
                "evidence_contract": {
                    "required_artifacts": [],
                    "required_source_proof": [],
                    "required_checks": ["journal compaction check"],
                    "completion_signals": ["compact lineage present"],
                    "disqualifying_failures": ["full execution plan leaked"],
                },
                "execution_plan": {
                    "capability_class": "testing",
                    "operations": ["do-not-journal"],
                    "permitted_local_discretion": [],
                    "prohibited_deviations": ["leak execution details"],
                    "return_to_mind_conditions": ["journal failure"],
                },
            },
        },
        omega={},
        auto={},
        brain_turns=[],
        temporal={},
        learning_state={},
        cascade_status="completed",
        state="closed",
    )

    body = enriched_episode_from_episode(episode)

    assert body["motor_program"] == {
        "program_id": "mp1",
        "parent_program_id": "mp0",
        "outcome_id": "out1",
        "attempt_number": 2,
        "fingerprint": "fp1",
        "pathway": {
            "pathway_id": "derive",
            "revision": 1,
            "name": "derive",
            "method": "derive and verify",
            "operational_rationale": "exercise canonical journal input",
            "failed_predecessor_id": "mp0",
            "change_evidence": ["full canonical contract"],
        },
        "evidence_contract": {
            "required_artifacts": [],
            "required_source_proof": [],
            "required_checks": ["journal compaction check"],
            "completion_signals": ["compact lineage present"],
            "disqualifying_failures": ["full execution plan leaked"],
        },
    }
    assert "execution_plan" not in body["motor_program"]


def test_enriched_episode_removes_non_shadow_motor_program_from_execution() -> None:
    program = {
        "program_id": "mp-non-shadow",
        "parent_program_id": "mp-parent",
        "outcome_id": "out1",
        "episode_id": "ep-journal-non-shadow",
        "attempt_number": 2,
        "created_at": "2026-07-11T00:00:00+00:00",
        "fingerprint": "fp1",
        "outcome_contract": {
            "objective": "verify non-shadow journal compaction",
            "acceptance_criteria": ["execution motor program is removed"],
            "constraints": [],
            "priorities": ["correctness"],
        },
        "pathway": {
            "pathway_id": "derive",
            "revision": 1,
            "name": "derive",
            "method": "derive and verify",
            "operational_rationale": "exercise canonical non-shadow input",
            "failed_predecessor_id": "mp-parent",
            "change_evidence": ["full canonical contract"],
        },
        "evidence_contract": {
            "required_artifacts": [],
            "required_source_proof": [],
            "required_checks": ["motor program removal check"],
            "completion_signals": ["compact lineage present"],
            "disqualifying_failures": ["execution detail persisted"],
        },
        "execution_plan": {
            "capability_class": "testing",
            "operations": ["must-not-persist"],
            "permitted_local_discretion": [],
            "prohibited_deviations": ["persist execution detail"],
            "return_to_mind_conditions": ["journal failure"],
        },
    }
    execution_inputs = {
        "query": "run it",
        "motor_program": program,
    }
    episode = SimpleNamespace(
        episode_id="ep-journal-non-shadow",
        opened_by="test",
        pressure={},
        global_={},
        meta={
            "execution_action_pack": {
                "action": "EXECUTE_MOTOR_PROGRAM",
                "execution_inputs": execution_inputs,
            },
            "motor_program": program,
        },
        omega={},
        auto={},
        brain_turns=[],
        temporal={},
        learning_state={},
        cascade_status="completed",
        state="closed",
    )

    body = enriched_episode_from_episode(episode)

    journal_inputs = body["execution"]["execution_action_pack"]["execution_inputs"]
    assert journal_inputs == {"query": "run it"}
    assert "execution_plan" not in json.dumps(body, sort_keys=True)
    assert "must-not-persist" not in json.dumps(body, sort_keys=True)
    assert execution_inputs["motor_program"] is program


def test_fold_knowledge_products_write_with_receipts(monkeypatch) -> None:
    """Memory is first and truth: fold knowledge writes in-episode and the
    receipts ride omega_result into the journal."""
    episode = SimpleNamespace(episode_id="ep-know", global_={}, meta={})
    result = SimpleNamespace(
        knowledge_to_ingest=({"domain": "ml", "statement": "s1", "confidence": 0.9},),
    )
    global_ = {"omega_result": {"mastery_effects": []}}
    fake_receipts = [
        {"domain": "ml", "coord": [2, 0, 0], "path": "x", "status": "committed", "statement": "s1"}
    ]
    monkeypatch.setattr(
        "memory.knowledge_ingest.ingest_knowledge_products",
        # replay_key (cross-lane T8): intake events keyed by the fold's stable identity.
        lambda _ep, _products, **_kwargs: fake_receipts,
    )

    chain._ingest_fold_knowledge(episode, result, global_)

    assert global_["omega_result"]["knowledge_receipts"] == fake_receipts


def test_fold_knowledge_mastery_capped_at_prior_without_statements() -> None:
    """No receipts, no increase: absolute mastery stays at its prior level."""
    episode = SimpleNamespace(episode_id="ep-strip", global_={}, meta={})
    result = SimpleNamespace(knowledge_to_ingest=())
    global_ = {
        "omega_packet": {
            "domain": "ml",
            "prior_mastery": {"knowledge_mastery": 0.4},
        },
        "omega_result": {
            "mastery_effects": [
                {
                    "domain": "ml",
                    "knowledge_mastery": 0.7,
                    "application_mastery": 0.05,
                    "rationale": "prose claim",
                }
            ]
        },
    }

    chain._ingest_fold_knowledge(episode, result, global_)

    effect = global_["omega_result"]["mastery_effects"][0]
    assert effect["knowledge_mastery"] == 0.4, "prose earns no increase"
    assert effect["application_mastery"] == 0.05, "application delta untouched"


def test_fold_knowledge_allows_honest_mastery_downgrade_without_statements() -> None:
    """No-new-learning can reveal shallow coverage; an absolute downgrade is valid."""
    episode = SimpleNamespace(episode_id="ep-downgrade", global_={}, meta={})
    result = SimpleNamespace(knowledge_to_ingest=())
    global_ = {
        "omega_packet": {
            "domain": "ml",
            "prior_mastery": {"knowledge_mastery": 0.4},
        },
        "omega_result": {
            "mastery_effects": [
                {
                    "domain": "ml",
                    "knowledge_mastery": 0.2,
                    "application_mastery": 0.05,
                    "rationale": "coverage proved shallow",
                }
            ]
        },
    }

    chain._ingest_fold_knowledge(episode, result, global_)

    assert global_["omega_result"]["mastery_effects"][0]["knowledge_mastery"] == 0.2


def _fetch_episode() -> SimpleNamespace:
    return SimpleNamespace(
        episode_id="ep-fetch",
        global_={},
        meta={},
        auto={
            "execution_pack": {"action": "USE_TOOL", "tool": "web_fetch"},
            "outputs": {
                "tool_result": {
                    "domain": "machine-learning",
                    "results": [
                        {"title": "", "url": "https://x/paper", "content": "DOME framework..."}
                    ],
                },
                "knowledge_artifact_id": "art-1",
            },
        },
    )


def test_fetched_comprehension_commits_before_fold(monkeypatch) -> None:
    """Operator pipeline 2026-07-11: pull deterministically -> ONE inference names
    sources + proses what was learned -> memory commit RIGHT AFTER inference ->
    the fold reads the committed form (receipt attached to outputs)."""
    order: list[str] = []
    episode = _fetch_episode()

    generated = {}

    def _generate(**kwargs):
        generated.update(kwargs)
        order.append("inference")
        return {
            "text": json.dumps(
                {
                    "kind": "world",
                    "sources": [{"name": "DOME paper", "url": "https://x/paper"}],
                    "learned": "DOME standardizes supervised ML validation",
                }
            )
        }

    monkeypatch.setattr(
        "runtime.socket.invoke_runtime_generate",
        _generate,
    )
    monkeypatch.setattr(
        "memory.fetched_ingest.ingest_fetched_knowledge",
        lambda ep_id, **kw: (
            order.append("commit")
            or {
                "coord": [2, 4, 0],
                "lobe": 2,
                "kind": "world",
                "path": "p/1",
                "status": "committed",
                "sources": kw["sources"],
            }
        ),
    )

    chain._comprehend_and_ingest_fetched(episode)

    assert order == ["inference", "commit"], "commit lands right after inference"
    fk = episode.auto["outputs"]["fetched_knowledge"]
    assert fk["kind"] == "world"
    assert fk["learned"].startswith("DOME standardizes")
    assert fk["receipt"]["status"] == "committed"
    assert fk["sources"][0]["name"] == "DOME paper", "the missing name was added"
    assert generated["timeout"] == 600, "local Gemma needs the governed long budget"


def test_fetched_comprehension_failure_is_honest_not_fatal(monkeypatch) -> None:
    episode = _fetch_episode()
    monkeypatch.setattr(
        "runtime.socket.invoke_runtime_generate",
        lambda **_k: (_ for _ in ()).throw(RuntimeError("runtime down")),
    )

    chain._comprehend_and_ingest_fetched(episode)

    assert "fetched_knowledge" not in episode.auto["outputs"]
    assert "runtime down" in episode.auto["outputs"]["fetched_knowledge_error"]


def test_domainless_fetch_is_filed_where_the_comprehension_chooses(monkeypatch) -> None:
    """A fetch with no learning domain asks the comprehension to pick a known home."""
    episode = _fetch_episode()
    del episode.auto["outputs"]["tool_result"]["domain"]
    generated, committed = {}, {}

    def _generate(**kwargs):
        generated.update(kwargs)
        return {"text": json.dumps({"kind": "coding", "domain": "protocols_http",
                                    "sources": [{"name": "DOME", "url": "https://x/paper"}],
                                    "learned": "HTTP status codes classify responses"})}

    monkeypatch.setattr("runtime.socket.invoke_runtime_generate", _generate)
    monkeypatch.setattr("memory.fetched_ingest.ingest_fetched_knowledge",
                        lambda ep_id, **kw: committed.update(kw) or {"kind": "coding", "sources": kw["sources"]})

    chain._comprehend_and_ingest_fetched(episode)

    assert '"protocols_http"' in generated["prompt"] and "known_domains" in generated["system"]
    assert committed["domain"] == "protocols_http"


def test_fetch_with_no_fileable_domain_degrades_instead_of_shutting_down(monkeypatch) -> None:
    """Live 2026-09-14: a domain-less web fetch raised through the cascade and killed Omega."""
    from memory.fetched_ingest import UnfileableDomain

    episode = _fetch_episode()
    del episode.auto["outputs"]["tool_result"]["domain"]
    monkeypatch.setattr("runtime.socket.invoke_runtime_generate", lambda **_k: {"text": json.dumps(
        {"kind": "world", "sources": [{"name": "DOME", "url": "https://x/paper"}], "learned": "DOME prose"})})

    def _unfileable(ep_id, **kw):
        raise UnfileableDomain("illegal domain '' for lobe 2: not a known subdomain")

    monkeypatch.setattr("memory.fetched_ingest.ingest_fetched_knowledge", _unfileable)

    chain._comprehend_and_ingest_fetched(episode)

    assert "fetched_knowledge" not in episode.auto["outputs"]
    assert "no filed domain" in episode.auto["outputs"]["fetched_knowledge_error"]


def test_fetched_comprehension_noop_without_fetch_material() -> None:
    episode = SimpleNamespace(episode_id="ep-none", global_={}, meta={}, auto={"outputs": {}})
    chain._comprehend_and_ingest_fetched(episode)
    assert episode.auto["outputs"] == {}


def test_chat_lane_fetch_still_commits_to_memory(monkeypatch) -> None:
    """RESPOND has no pack tool; the pages still take the LEARN ingest path."""
    order: list[str] = []
    episode = SimpleNamespace(
        episode_id="ep-chat-fetch",
        global_={},
        meta={},
        auto={
            "execution_pack": {"action": "RESPONSE", "tool": None},
            "outputs": {
                "tool_result": {
                    "domain": "python",
                    "results": [
                        {
                            "title": "asyncio",
                            "url": "https://docs.python.org/3/library/asyncio.html",
                            "content": "asyncio is a library to write concurrent code.",
                        }
                    ],
                },
                "knowledge_artifact_id": "art-chat",
            },
        },
    )

    def _generate(**kwargs):
        order.append("inference")
        return {
            "text": json.dumps(
                {
                    "kind": "world",
                    "sources": [
                        {
                            "name": "Python docs",
                            "url": "https://docs.python.org/3/library/asyncio.html",
                        }
                    ],
                    "learned": "asyncio is Python's concurrent I/O library",
                }
            )
        }

    monkeypatch.setattr("runtime.socket.invoke_runtime_generate", _generate)
    monkeypatch.setattr(
        "memory.fetched_ingest.ingest_fetched_knowledge",
        lambda ep_id, **kw: (
            order.append("commit")
            or {
                "coord": [2, 4, 0],
                "lobe": 2,
                "kind": "world",
                "path": "p/chat",
                "status": "committed",
                "sources": kw["sources"],
            }
        ),
    )

    chain._comprehend_and_ingest_fetched(episode)

    assert order == ["inference", "commit"]
    assert episode.auto["outputs"]["fetched_knowledge"]["receipt"]["status"] == "committed"


def test_fetched_comprehension_rejects_hallucinated_source_url(monkeypatch) -> None:
    episode = _fetch_episode()
    calls = {"n": 0}

    def _hallucinated_response(**_k):
        calls["n"] += 1
        return {
            "text": json.dumps(
                {
                    "kind": "world",
                    "sources": [{"name": "Invented", "url": "https://invented.invalid/paper"}],
                    "learned": "unsupported prose",
                }
            )
        }

    monkeypatch.setattr(
        "runtime.socket.invoke_runtime_generate",
        _hallucinated_response,
    )

    chain._comprehend_and_ingest_fetched(episode)

    assert calls["n"] == 1
    assert "fetched_knowledge" not in episode.auto["outputs"]
    assert "grounded" in episode.auto["outputs"]["fetched_knowledge_error"]


def test_fetched_comprehension_ignores_non_web_result_lists(monkeypatch) -> None:
    episode = _fetch_episode()
    episode.auto["execution_pack"]["tool"] = "remember_knowledge"
    monkeypatch.setattr(
        "runtime.socket.invoke_runtime_generate",
        lambda **_k: (_ for _ in ()).throw(AssertionError("must not infer")),
    )

    chain._comprehend_and_ingest_fetched(episode)

    assert "fetched_knowledge" not in episode.auto["outputs"]
