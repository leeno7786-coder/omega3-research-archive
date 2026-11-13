from __future__ import annotations

import json

from memory.recall_verifier import (
    append_verdict_log,
    pressure_signal_for_status,
    verify_candidates,
    verdict_log_path,
)


def _write_record(
    tmp_path,
    memory_id: str,
    *,
    title: str,
    summary: str,
    coord=None,
    provenance=None,
    enrichment=None,
    domain=None,
    record_type=None,
    workspace_source=None,
    cite=True,
):
    coord = coord or [2, 0, 1]
    text = f"{title}. {summary}"
    prov = dict(provenance or {})
    # Production reality (post 2026-07-21 fail-closed): every canonical record
    # carries a SourceLedger citation — create_staging mints one, the seed writer
    # calls record_source. Recall now verifies via ledger crypto and no longer
    # accepts a bare file-match (rederive). So cite by default; pass cite=False to
    # exercise the unprovenanced/drift path (which now resolves to unknown).
    if cite and not prov.get("source_id"):
        from memory.source_ledger.store import record_source

        artifact = record_source(
            text, source_type="operator_chat", origin_id=f"cycle-{memory_id}", root=tmp_path
        )
        prov.update(artifact.citation())
    memory_root = tmp_path / "memory"
    (memory_root / "indexes").mkdir(parents=True, exist_ok=True)
    record_path = memory_root / "200_knowledge" / f"{memory_id}.json"
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(
        json.dumps(
            {
                "id": memory_id,
                "coord": coord,
                "title": title,
                "summary": summary,
                "text": text,
                "provenance": prov,
                "enrichment": dict(enrichment or {}),
                "domain": domain or "",
                "type": record_type or "",
                **({"workspace_source": dict(workspace_source)} if workspace_source else {}),
            }
        ),
        encoding="utf-8",
    )
    index_path = memory_root / "indexes" / "coord_index.json"
    coord_index = {}
    if index_path.exists():
        coord_index = json.loads(index_path.read_text(encoding="utf-8"))
    coord_index[memory_id] = {"coord": coord, "path": str(record_path.relative_to(tmp_path))}
    index_path.write_text(json.dumps(coord_index), encoding="utf-8")


def _assert_retrieved_accounting(retrieved, verdict, *, tmp_path, cycle_id, verified_ids=None):
    admitted_count = len((verdict.answer_payload or {}).get("candidates") or ())
    rejected_count = len(verdict.rejected_hits)
    assert len(retrieved) == admitted_count + rejected_count
    accounting = verdict.retrieval_trace["candidate_accounting"]
    assert accounting == {
        "scope": "pre_verifier_candidate_pool",
        "retrieved_count": len(retrieved),
        "admitted_count": admitted_count,
        "rejected_count": rejected_count,
    }

    append_verdict_log(verdict, cycle_id=cycle_id, root=tmp_path)
    log_record = json.loads(verdict_log_path(root=tmp_path).read_text(encoding="utf-8"))
    assert log_record["evidence_count"] == admitted_count
    assert log_record["admitted_count"] == admitted_count
    assert log_record["rejected_count"] == rejected_count
    assert log_record["retrieved_count"] == len(retrieved)
    assert log_record["retrieval_trace"] == verdict.retrieval_trace
    assert len(retrieved) == log_record["evidence_count"] + log_record["rejected_count"]
    if verified_ids is not None:
        # the durable row names WHAT was grounded on, not merely how many: only the
        # admitted evidence, never a rejected candidate the gate refused.
        assert log_record["verified_memory_ids"] == list(verified_ids)
        rejected_ids = {str(hit.get("memory_id") or "") for hit in verdict.rejected_hits}
        assert not (set(log_record["verified_memory_ids"]) & rejected_ids)


def test_verify_candidates_returns_verified_for_live_match(tmp_path):
    _write_record(tmp_path, "mem.creator", title="Creator", summary="Built by Ada")
    verdict = verify_candidates(
        "creator",
        [
            {
                "memory_id": "mem.creator",
                "coord": [2, 0, 1],
                "score": 0.9,
                "title": "Creator",
                "summary": "Built by Ada",
            }
        ],
        root=tmp_path,
    )
    assert verdict.status == "verified"
    assert verdict.answer_payload is not None
    assert verdict.answer_payload["candidates"][0]["memory_id"] == "mem.creator"


def test_recall_verdict_accounts_for_every_retrieved_candidate(tmp_path):
    from memory.recall_truncation import build_truncation_receipt

    _write_record(
        tmp_path,
        "mem.catalog",
        title="Build catalog",
        summary="The build catalog keeps signed package metadata.",
    )
    retrieved = [
        {
            "memory_id": "mem.catalog",
            "coord": [2, 0, 1],
            "score": 0.9,
            "title": "Build catalog",
            "summary": "The build catalog keeps signed package metadata.",
        },
        {
            "memory_id": "mem.missing",
            "coord": [2, 0, 2],
            "score": 0.8,
            "title": "Build catalog",
            "summary": "The build catalog keeps unsigned package metadata.",
        },
    ]
    taxonomy_receipt = build_truncation_receipt(
        "taxonomy_top_k",
        ["mem.catalog", "mem.missing", "mem.not_retrieved"],
        ["mem.catalog", "mem.missing"],
    )
    dme_receipt = build_truncation_receipt(
        "dme_hop_search:max_results",
        ["mem.catalog", "mem.dme_trimmed"],
        ["mem.catalog"],
    )

    verdict = verify_candidates(
        "build catalog",
        retrieved,
        root=tmp_path,
        allowed_lobes=(2,),
        meta={"candidate_truncation_receipts": [taxonomy_receipt, dme_receipt]},
    )

    assert verdict.status == "verified"
    admitted_count = len((verdict.answer_payload or {}).get("candidates") or ())
    rejected_count = len(verdict.rejected_hits)
    assert admitted_count == 1
    assert rejected_count == 1
    outcome_ids = {
        item["memory_id"]
        for item in (verdict.answer_payload or {}).get("candidates", ())
    } | {item["memory_id"] for item in verdict.rejected_hits}
    trace = verdict.retrieval_trace
    assert outcome_ids == {item["memory_id"] for item in retrieved}
    assert trace["pre_verifier_candidate_pool"]["candidate_count"] == len(retrieved)
    assert set(trace["pre_verifier_candidate_pool"]["candidate_ids"]) == outcome_ids
    assert trace["truncation_receipts"][0]["dropped_candidate_ids"] == [
        "mem.not_retrieved"
    ]
    assert trace["truncation_receipts"][1]["stage"] == "dme_hop_search:max_results"
    assert trace["truncation_receipts"][1]["dropped_candidate_ids"] == [
        "mem.dme_trimmed"
    ]
    _assert_retrieved_accounting(
        retrieved,
        verdict,
        tmp_path=tmp_path,
        cycle_id="count-invariant",
        verified_ids=["mem.catalog"],
    )


def test_query_sense_filter_rejections_preserve_candidate_accounting(tmp_path, monkeypatch):
    _write_record(
        tmp_path,
        "mem.astronomy.one",
        title="Astronomy",
        summary="Astronomy covers the first topic",
        coord=[2, 0, 1],
        record_type="semantic_fact",
    )
    _write_record(
        tmp_path,
        "mem.astronomy.two",
        title="Astronomy",
        summary="Astronomy covers the second topic",
        coord=[2, 0, 2],
        record_type="semantic_fact",
    )
    retrieved = [
        {
            "memory_id": "mem.astronomy.one",
            "coord": [2, 0, 1],
            "score": 0.9,
            "title": "Astronomy",
            "summary": "Astronomy covers the first topic",
        },
        {
            "memory_id": "mem.astronomy.two",
            "coord": [2, 0, 2],
            "score": 0.8,
            "title": "Astronomy",
            "summary": "Astronomy covers the second topic",
        },
    ]
    monkeypatch.setattr(
        "memory.recall_verifier._filter_verified_by_query_sense",
        lambda _query, verified, *, root: verified[:1],
    )

    verdict = verify_candidates(
        "astronomy",
        retrieved,
        root=tmp_path,
        allowed_lobes=(2,),
    )

    assert verdict.status == "verified"
    assert [row["memory_id"] for row in verdict.answer_payload["candidates"]] == [
        "mem.astronomy.one"
    ]
    assert [row["memory_id"] for row in verdict.rejected_hits] == ["mem.astronomy.two"]
    assert "resolved query sense" in verdict.rejected_hits[0]["reason"]
    _assert_retrieved_accounting(
        retrieved, verdict, tmp_path=tmp_path, cycle_id="sense-filter-count"
    )


def test_verified_source_proof_survives_gate_remember_and_answer_context(
    tmp_path, monkeypatch
):
    import memory.api as memory_api
    from auto_loop.mril_tools.remember import invoke_remember_tool
    from auto_loop.scaffold.runtime import verified_hits_from_results
    from memory.api import QuerySpec, RetrievalCandidate, RetrievalResult

    _write_record(
        tmp_path,
        "mem.catalog",
        title="Build catalog",
        summary="The signed package catalog was built by Ada.",
    )
    query = QuerySpec(
        cycle_id="source-proof-e2e",
        text="build catalog",
        allowed_lobes=[2],
        denied_lobes=[],
    )
    retrieved = RetrievalResult(
        cycle_id=query.cycle_id,
        candidates=[
            RetrievalCandidate(
                id="mem.catalog",
                coord=[2, 0, 1],
                score=0.9,
                title="Build catalog",
                summary="The signed package catalog was built by Ada.",
            )
        ],
        meta={},
    )
    monkeypatch.setattr(
        memory_api,
        "_apply_verified_recall_side_effects",
        lambda *, meta, **_kwargs: meta,
    )

    gated = memory_api._apply_recall_verdict_gate(query, retrieved, root=tmp_path)

    assert gated.meta["recall_verdict"]["status"] == "verified"
    assert gated.candidates[0].source_proof["evidence_strength"] == "source_ledger_crypto"
    remembered = invoke_remember_tool(
        {
            "tool": "remember_knowledge",
            "query": query.text,
            "allowed_lobes": [2],
            "limit": 5,
        },
        "source-proof-e2e",
        memory=lambda _spec: gated,
    )

    assert remembered["verdict"] == "verified"
    citation = remembered["evidence"][0]["source_citation"]
    assert citation["evidence_strength"] == "source_ledger_crypto"
    answer_hits = verified_hits_from_results([remembered])
    answer_text = answer_hits["mem.catalog"]
    assert "Source citation (verified):" in answer_text
    assert f"source_id={citation['source_id']}" in answer_text


def _workspace_source(revision_id: str, *, availability_state: str = "available") -> dict[str, str]:
    return {
        "connection_id": "conn-1",
        "capability": "docs",
        "object_id": "doc-1",
        "revision_id": revision_id,
        "content_digest": revision_id * 64,
        "source_ledger_receipt": f"src-{revision_id}",
        "observed_at": f"2026-06-0{revision_id}T00:00:00+00:00",
        "availability_state": availability_state,
    }


def test_current_recall_rejects_superseded_workspace_revision(tmp_path):
    from memory.source_validity import rebuild_from_canonical

    _write_record(
        tmp_path,
        "ws.plan.v1",
        title="Project plan",
        summary="Project plan said ship in June.",
        workspace_source=_workspace_source("1"),
    )
    _write_record(
        tmp_path,
        "ws.plan.v2",
        title="Project plan",
        summary="Project plan says ship in July.",
        workspace_source=_workspace_source("2"),
    )
    rebuild_from_canonical(tmp_path)

    verdict = verify_candidates(
        "current project plan",
        [
            {
                "memory_id": "ws.plan.v1",
                "coord": [9, 1, 1],
                "score": 0.9,
                "title": "Project plan",
                "summary": "Project plan said ship in June.",
            }
        ],
        root=tmp_path,
    )

    assert verdict.status == "unknown"
    assert verdict.rejected_hits[0]["reason"] == "workspace source revision is superseded"


def test_historical_mode_can_admit_retained_workspace_revision(tmp_path):
    from memory.source_validity import rebuild_from_canonical

    _write_record(
        tmp_path,
        "ws.plan.v1",
        title="Project plan",
        summary="Project plan said ship in June.",
        workspace_source=_workspace_source("1"),
    )
    _write_record(
        tmp_path,
        "ws.plan.v2",
        title="Project plan",
        summary="Project plan says ship in July.",
        workspace_source=_workspace_source("2"),
    )
    rebuild_from_canonical(tmp_path)

    verdict = verify_candidates(
        "what did the project plan say in june",
        [
            {
                "memory_id": "ws.plan.v1",
                "coord": [9, 1, 1],
                "score": 0.9,
                "title": "Project plan",
                "summary": "Project plan said ship in June.",
            }
        ],
        root=tmp_path,
        include_historical_sources=True,
    )

    assert verdict.status == "verified"
    assert verdict.evidence[0]["workspace_source"]["revision_id"] == "1"


def test_current_recall_rejects_workspace_revision_after_access_loss(tmp_path):
    from memory.source_validity import rebuild_from_canonical

    _write_record(
        tmp_path,
        "ws.plan.v1",
        title="Project plan",
        summary="Project plan said ship in June.",
        workspace_source=_workspace_source("1"),
    )
    _write_record(
        tmp_path,
        "ws.plan.v2",
        title="Project plan",
        summary="workspace:docs inaccessible doc-1",
        workspace_source=_workspace_source("2", availability_state="inaccessible"),
    )
    rebuild_from_canonical(tmp_path)

    verdict = verify_candidates(
        "current project plan",
        [
            {
                "memory_id": "ws.plan.v1",
                "coord": [9, 1, 1],
                "score": 0.9,
                "title": "Project plan",
                "summary": "Project plan said ship in June.",
            }
        ],
        root=tmp_path,
    )

    assert verdict.status == "unknown"
    assert verdict.rejected_hits[0]["reason"] == "workspace source revision is superseded"


def test_source_validity_rebuild_ignores_program0_working_snapshots(tmp_path):
    from memory.source_validity import rebuild_from_canonical

    staged = (
        tmp_path
        / "memory"
        / "projection"
        / "work-test"
        / "input_profile"
        / "memory"
        / "200_knowledge"
        / "incomplete.json"
    )
    staged.parent.mkdir(parents=True)
    staged.write_text("{", encoding="utf-8")

    report = rebuild_from_canonical(tmp_path)

    assert report["records_scanned"] == 0


def test_verify_candidates_returns_unknown_when_projection_drifted(tmp_path):
    # cite=False: an uncited record is unprovenanced and fails closed. Here the
    # drifted graph label is caught by the label check first (uncited path).
    _write_record(tmp_path, "mem.creator", title="Creator", summary="Built by Ada", cite=False)
    verdict = verify_candidates(
        "creator",
        [
            {
                "memory_id": "mem.creator",
                "coord": [2, 0, 1],
                "score": 0.9,
                "title": "Different Title",
                "summary": "Built by Ada",
            }
        ],
        root=tmp_path,
    )
    assert verdict.status == "unknown"
    assert "label" in verdict.reason


def test_uncited_record_fails_closed_to_unknown(tmp_path):
    # Fail-closed contract (operator 2026-07-21, ledger-or-I-don't-know): an
    # unprovenanced record (no SourceLedger citation) is NEVER "verified" — a bare
    # file-match (rederive) is not verification, even when the label matches.
    _write_record(tmp_path, "mem.uncited", title="Creator", summary="Built by Ada", cite=False)
    verdict = verify_candidates(
        "creator",
        [
            {
                "memory_id": "mem.uncited",
                "coord": [2, 0, 1],
                "score": 0.9,
                "title": "Creator",
                "summary": "Built by Ada",
            }
        ],
        root=tmp_path,
    )
    assert verdict.status == "unknown"
    assert "unprovenanced" in verdict.reason or "citation" in verdict.reason


def test_verify_candidates_returns_ambiguous_for_single_token_multi_sense(tmp_path):
    _write_record(
        tmp_path, "mem.neuron.bio", title="Neuron", summary="A biological cell", coord=[2, 0, 1]
    )
    _write_record(
        tmp_path, "mem.neuron.ml", title="Neuron", summary="A model unit", coord=[8, 0, 1]
    )
    retrieved = [
        {
            "memory_id": "mem.neuron.bio",
            "coord": [2, 0, 1],
            "score": 0.8,
            "title": "Neuron",
            "summary": "A biological cell",
        },
        {
            "memory_id": "mem.neuron.ml",
            "coord": [8, 0, 1],
            "score": 0.7,
            "title": "Neuron",
            "summary": "A model unit",
        },
    ]
    verdict = verify_candidates(
        "neuron",
        retrieved,
        root=tmp_path,
    )
    assert verdict.status == "ambiguous"
    assert pressure_signal_for_status(verdict.status) == "sense_resolution"
    assert {hit["memory_id"] for hit in verdict.rejected_hits} == {
        "mem.neuron.bio",
        "mem.neuron.ml",
    }
    assert {hit["status"] for hit in verdict.rejected_hits} == {"ambiguous"}
    _assert_retrieved_accounting(
        retrieved, verdict, tmp_path=tmp_path, cycle_id="ambiguous-count"
    )


def test_verify_candidates_includes_source_ledger_proof_when_canonical_cites_source(tmp_path):
    from memory.source_ledger.store import record_source

    artifact = record_source(
        "Creator. Built by Ada",
        source_type="operator_chat",
        origin_id="cycle-creator",
        metadata={
            "source_session_id": "session-creator",
            "source_session_timestamp": "2025-04-05T06:07:08+00:00",
        },
        root=tmp_path,
    )
    _write_record(
        tmp_path,
        "mem.creator",
        title="Creator",
        summary="Built by Ada",
        provenance=artifact.citation(),
    )

    verdict = verify_candidates(
        "creator",
        [
            {
                "memory_id": "mem.creator",
                "coord": [2, 0, 1],
                "score": 0.9,
                "title": "Creator",
                "summary": "Built by Ada",
            }
        ],
        root=tmp_path,
    )

    assert verdict.status == "verified"
    assert verdict.evidence[0]["evidence_strength"] == "source_ledger_crypto"
    assert verdict.evidence[0]["source_id"] == artifact.source_id
    assert verdict.evidence[0]["span_hash"] == artifact.span_hash
    assert verdict.evidence[0]["source_session_id"] == "session-creator"
    assert verdict.evidence[0]["source_session_timestamp"] == "2025-04-05T06:07:08+00:00"


def test_verified_relational_record_carries_on_file_relation_facets(tmp_path):
    # The verifier already loads the FS record to verify it. It must stash that record's
    # OWN parsed relations (enrichment.entities) onto the verified candidate — the FS is
    # the truth source for the planner's grouping and the subject-strip, never the graph.
    _write_record(
        tmp_path,
        "1.00.noah",
        title="Operator son: abel",
        summary="Noah's son is Abel.",
        coord=[1, 0, 0],
        enrichment={
            "entities": [
                {
                    "name": "son",
                    "facet": "relationship",
                    "linked_name": "abel",
                    "subject": "operator",
                },
                {"name": "omega ai", "facet": "business", "subject": "operator"},
            ]
        },
    )
    verdict = verify_candidates(
        "abel",
        [
            {
                "memory_id": "1.00.noah",
                "coord": [1, 0, 0],
                "score": 0.9,
                "title": "Operator son: abel",
                "summary": "Noah's son is Abel.",
            }
        ],
        root=tmp_path,
    )
    assert verdict.status == "verified"
    facets = verdict.answer_payload["candidates"][0].get("relation_facets")
    assert facets, "verified relational record must carry its on-file relation facets"
    by_facet = {f["facet"]: f for f in facets}
    assert by_facet["relationship"]["subject"] == "operator"
    assert by_facet["relationship"]["relation"] == "son"
    assert by_facet["relationship"]["object"] == "abel"
    assert by_facet["business"]["object"] == "omega ai"


def test_verified_non_relational_record_has_empty_relation_facets(tmp_path):
    _write_record(
        tmp_path, "mem.k", title="Python", summary="A programming language", coord=[2, 0, 1]
    )
    verdict = verify_candidates(
        "python",
        [
            {
                "memory_id": "mem.k",
                "coord": [2, 0, 1],
                "score": 0.9,
                "title": "Python",
                "summary": "A programming language",
            }
        ],
        root=tmp_path,
    )
    assert verdict.status == "verified"
    assert verdict.answer_payload["candidates"][0].get("relation_facets") == []


def test_verify_candidates_returns_conflict_when_source_ledger_span_hash_mismatches(tmp_path):
    from memory.source_ledger.store import record_source

    artifact = record_source(
        "Creator. Built by Ada",
        source_type="operator_chat",
        root=tmp_path,
    )
    citation = artifact.citation()
    citation["span_hash"] = "wrong-span-hash"
    _write_record(
        tmp_path,
        "mem.creator",
        title="Creator",
        summary="Built by Ada",
        provenance=citation,
    )

    verdict = verify_candidates(
        "creator",
        [
            {
                "memory_id": "mem.creator",
                "coord": [2, 0, 1],
                "score": 0.9,
                "title": "Creator",
                "summary": "Built by Ada",
            }
        ],
        root=tmp_path,
    )

    assert verdict.status == "conflict"
    assert "source ledger" in verdict.reason


def test_verify_candidates_uses_sense_registry_to_resolve_cued_polysemy(tmp_path):
    from memory.sense_registry import register_senses_for_memory

    bio_sense = register_senses_for_memory(
        memory_id="mem.agent.robot",
        title="Agent",
        summary="A robot agent plans and acts in a physical environment.",
        text="A robot agent plans and acts in a physical environment.",
        coord=[2, 0, 1],
        domain="robotics",
        root=tmp_path,
    )
    ai_sense = register_senses_for_memory(
        memory_id="mem.agent.software",
        title="Agent",
        summary="A software agent invokes tools and coordinates tasks.",
        text="A software agent invokes tools and coordinates tasks.",
        coord=[8, 0, 1],
        domain="software",
        root=tmp_path,
    )
    _write_record(
        tmp_path,
        "mem.agent.robot",
        title="Agent",
        summary="A robot agent plans and acts in a physical environment.",
        coord=[2, 0, 1],
        enrichment={"sense_ids": bio_sense},
        domain="robotics",
    )
    _write_record(
        tmp_path,
        "mem.agent.software",
        title="Agent",
        summary="A software agent invokes tools and coordinates tasks.",
        coord=[8, 0, 1],
        enrichment={"sense_ids": ai_sense},
        domain="software",
    )

    verdict = verify_candidates(
        "robot agent",
        [
            {
                "memory_id": "mem.agent.robot",
                "coord": [2, 0, 1],
                "score": 0.8,
                "title": "Agent",
                "summary": "A robot agent plans and acts in a physical environment.",
            },
            {
                "memory_id": "mem.agent.software",
                "coord": [8, 0, 1],
                "score": 0.7,
                "title": "Agent",
                "summary": "A software agent invokes tools and coordinates tasks.",
            },
        ],
        root=tmp_path,
    )

    assert verdict.status == "verified"
    assert verdict.answer_payload is not None
    candidates = verdict.answer_payload["candidates"]
    assert len(candidates) == 1
    assert candidates[0]["memory_id"] == "mem.agent.robot"


def test_verify_candidates_keeps_bare_polyseme_ambiguous_with_registered_senses(tmp_path):
    from memory.sense_registry import register_senses_for_memory

    cell_sense = register_senses_for_memory(
        memory_id="mem.neuron.bio",
        title="Neuron",
        summary="A biological cell.",
        text="A biological cell.",
        coord=[2, 0, 1],
        domain="biology",
        root=tmp_path,
    )
    model_sense = register_senses_for_memory(
        memory_id="mem.neuron.ml",
        title="Neuron",
        summary="A model unit.",
        text="A model unit.",
        coord=[8, 0, 1],
        domain="machine learning",
        root=tmp_path,
    )
    _write_record(
        tmp_path,
        "mem.neuron.bio",
        title="Neuron",
        summary="A biological cell.",
        coord=[2, 0, 1],
        enrichment={"sense_ids": cell_sense},
        domain="biology",
    )
    _write_record(
        tmp_path,
        "mem.neuron.ml",
        title="Neuron",
        summary="A model unit.",
        coord=[8, 0, 1],
        enrichment={"sense_ids": model_sense},
        domain="machine learning",
    )

    verdict = verify_candidates(
        "neuron",
        [
            {
                "memory_id": "mem.neuron.bio",
                "coord": [2, 0, 1],
                "score": 0.8,
                "title": "Neuron",
                "summary": "A biological cell.",
            },
            {
                "memory_id": "mem.neuron.ml",
                "coord": [8, 0, 1],
                "score": 0.7,
                "title": "Neuron",
                "summary": "A model unit.",
            },
        ],
        root=tmp_path,
    )

    assert verdict.status == "ambiguous"


def test_verify_leaf_revalidates_live_record(tmp_path):
    _write_record(tmp_path, "mem.creator", title="Creator", summary="Built by Ada")
    from memory.recall_verifier import verify_leaf

    verdict = verify_leaf("mem.creator", root=tmp_path)
    assert verdict.status == "verified"


def test_verify_candidates_returns_conflict_for_same_title_different_summaries(tmp_path):
    _write_record(
        tmp_path, "mem.a", title="Favorite color", summary="Blue is best", coord=[2, 0, 1]
    )
    _write_record(
        tmp_path, "mem.b", title="Favorite color", summary="Green is best", coord=[2, 0, 2]
    )
    retrieved = [
        {
            "memory_id": "mem.a",
            "coord": [2, 0, 1],
            "score": 0.9,
            "title": "Favorite color",
            "summary": "Blue is best",
        },
        {
            "memory_id": "mem.b",
            "coord": [2, 0, 2],
            "score": 0.8,
            "title": "Favorite color",
            "summary": "Green is best",
        },
    ]
    verdict = verify_candidates(
        "favorite color",
        retrieved,
        root=tmp_path,
    )
    assert verdict.status == "conflict"
    assert {hit["memory_id"] for hit in verdict.rejected_hits} == {"mem.a", "mem.b"}
    assert {hit["status"] for hit in verdict.rejected_hits} == {"conflict"}
    assert {hit["memory_id"] for hit in verdict.blocked_conflicts} == {"mem.a", "mem.b"}
    _assert_retrieved_accounting(
        retrieved, verdict, tmp_path=tmp_path, cycle_id="conflict-count"
    )


def test_conflict_is_quarantined_not_allowed_to_poison_unrelated_facts(tmp_path):
    # A contradicting singular claim must not suppress every co-recalled fact. The
    # conflict pair is quarantined to blocked_conflicts; the clean fact still answers.
    _write_record(
        tmp_path,
        "mem.device.mode.compact",
        title="Device mode",
        summary="Device mode is compact",
        coord=[2, 0, 1],
    )
    _write_record(
        tmp_path,
        "mem.device.mode.verbose",
        title="Device mode",
        summary="Device mode is verbose",
        coord=[2, 0, 2],
    )
    _write_record(
        tmp_path,
        "mem.device.setting.theme",
        title="Device mode setting",
        summary="Device mode setting uses a teal display theme",
        coord=[2, 0, 3],
    )
    retrieved = [
        {
            "memory_id": "mem.device.mode.compact",
            "coord": [2, 0, 1],
            "score": 0.9,
            "title": "Device mode",
            "summary": "Device mode is compact",
        },
        {
            "memory_id": "mem.device.mode.verbose",
            "coord": [2, 0, 2],
            "score": 0.8,
            "title": "Device mode",
            "summary": "Device mode is verbose",
        },
        {
            "memory_id": "mem.device.setting.theme",
            "coord": [2, 0, 3],
            "score": 0.7,
            "title": "Device mode setting",
            "summary": "Device mode setting uses a teal display theme",
        },
    ]
    verdict = verify_candidates(
        "device mode",
        retrieved,
        root=tmp_path,
        allowed_lobes=(2,),
    )
    assert verdict.status == "verified", "the clean fact must still answer"
    answer_ids = {c["memory_id"] for c in verdict.answer_payload["candidates"]}
    assert answer_ids == {"mem.device.setting.theme"}, "only the non-conflicting fact answers"
    blocked_ids = {c["memory_id"] for c in verdict.blocked_conflicts}
    assert blocked_ids == {
        "mem.device.mode.compact",
        "mem.device.mode.verbose",
    }, "the conflict pair is quarantined"
    assert {
        hit["memory_id"] for hit in verdict.rejected_hits if hit["status"] == "conflict"
    } == blocked_ids
    _assert_retrieved_accounting(
        retrieved, verdict, tmp_path=tmp_path, cycle_id="mixed-conflict-count"
    )


def test_verify_candidates_verifies_fact_stream_sharing_one_title(tmp_path):
    """Learn-cascade shape: many semantic facts per domain share ONE title label.
    Same-title/different-summary is complementary there, not disagreement —
    the conflict rule applies only to singular-claim records."""
    _write_record(
        tmp_path,
        "mem.f1",
        title="Semantic - python",
        summary="Lists are mutable ordered sequences",
        coord=[8, 0, 1],
        record_type="semantic_fact",
    )
    _write_record(
        tmp_path,
        "mem.f2",
        title="Semantic - python",
        summary="Tuples are immutable ordered sequences",
        coord=[8, 0, 2],
        record_type="semantic_fact",
    )
    verdict = verify_candidates(
        "python data structures mutability",
        [
            {
                "memory_id": "mem.f1",
                "coord": [8, 0, 1],
                "score": 0.9,
                "title": "Semantic - python",
                "summary": "Lists are mutable ordered sequences",
            },
            {
                "memory_id": "mem.f2",
                "coord": [8, 0, 2],
                "score": 0.8,
                "title": "Semantic - python",
                "summary": "Tuples are immutable ordered sequences",
            },
        ],
        root=tmp_path,
    )
    assert verdict.status == "verified"
    assert len(verdict.answer_payload["candidates"]) == 2


# ── VERDICT TOPICALITY GATE (operator adjudication 2026-07-18) ──
# Router-as-topicality-authority + deterministic zero-overlap floor; every
# demotion is STRICT UNKNOWN (fail-closed, no visible "weak" status). These
# pins close the 2026-07-17 verified-on-residue gap at unit granularity; the
# determinism harness pins it E2E on both rails.


def test_topicality_gate_demotes_zero_overlap_residue_to_strict_unknown(tmp_path):
    # Provenance-real but OFF-TOPIC: the ATP-class residue that used to ground
    # "replay badge color" at score 0.02. Zero content-token overlap => the
    # verdict may not ground on it, regardless of how real the file is.
    _write_record(
        tmp_path,
        "mem.atp",
        title="ATP synthase in cristae",
        summary="Cristae host ATP synthase for oxidative phosphorylation",
    )
    verdict = verify_candidates(
        "replay badge color",
        [
            {
                "memory_id": "mem.atp",
                "coord": [2, 0, 1],
                "score": 0.02,
                "title": "ATP synthase in cristae",
                "summary": "Cristae host ATP synthase for oxidative phosphorylation",
            },
        ],
        root=tmp_path,
        allowed_lobes=(2,),
    )
    assert verdict.status == "unknown"
    assert verdict.answer_payload is None
    assert any(
        "no topical overlap" in str(hit.get("reason") or "") for hit in verdict.rejected_hits
    )


def test_topicality_gate_restores_conflict_past_off_topic_residue(tmp_path):
    # The conflict_query gap: with the residue demoted, the quarantined
    # conflict pair is what the query actually found — the verdict must say
    # CONFLICT (drives memory_reconciliation pressure), not verify residue.
    _write_record(
        tmp_path,
        "mem.blue",
        title="Replay badge color",
        summary="The replay badge color is blue",
        coord=[2, 12, 1],
    )
    _write_record(
        tmp_path,
        "mem.green",
        title="Replay badge color",
        summary="The replay badge color is green",
        coord=[2, 12, 2],
    )
    _write_record(
        tmp_path,
        "mem.atp",
        title="ATP synthase in cristae",
        summary="Cristae host ATP synthase for oxidative phosphorylation",
    )
    verdict = verify_candidates(
        "replay badge color",
        [
            {
                "memory_id": "mem.blue",
                "coord": [2, 12, 1],
                "score": 0.9,
                "title": "Replay badge color",
                "summary": "The replay badge color is blue",
            },
            {
                "memory_id": "mem.green",
                "coord": [2, 12, 2],
                "score": 0.85,
                "title": "Replay badge color",
                "summary": "The replay badge color is green",
            },
            {
                "memory_id": "mem.atp",
                "coord": [2, 0, 1],
                "score": 0.02,
                "title": "ATP synthase in cristae",
                "summary": "Cristae host ATP synthase for oxidative phosphorylation",
            },
        ],
        root=tmp_path,
        allowed_lobes=(2,),
    )
    assert verdict.status == "conflict"
    assert verdict.answer_payload is None


def test_router_refuses_unscoped_query_that_routes_to_journal(tmp_path):
    # Rule 1, router path: no caller region, and route_memory_tool names lobe 6
    # for deepsleep/journal vocabulary. NOTHING grounds — including a
    # provenance-real public-lobe record that shares query tokens (the
    # identity-residue class that verified at 0.956 pre-gate).
    _write_record(
        tmp_path,
        "mem.residue",
        title="Consolidation notes summary",
        summary="Consolidation of replay traces for the report",
    )
    verdict = verify_candidates(
        "DeepSleep private replay consolidation journal",
        [
            {
                "memory_id": "mem.residue",
                "coord": [2, 0, 1],
                "score": 0.95,
                "title": "Consolidation notes summary",
                "summary": "Consolidation of replay traces for the report",
            },
        ],
        root=tmp_path,
    )
    assert verdict.status == "unknown"
    assert verdict.answer_payload is None
    assert "non-recallable" in verdict.reason


def test_generic_vision_vocabulary_does_not_veto_unscoped_recall(tmp_path):
    # Review finding 2026-07-18: the dispatch router's greedy grammar-probe
    # tier routes single generic words ("patterns", "screen", "photo",
    # "display") to lobe 7. That tier is recoverable for TOOL DISPATCH but
    # must never drive the verdict's unrecoverable region veto — only the
    # high-precision journal/vision intent regexes may. An unscoped query
    # about decision patterns must still ground its reasoning-lobe answer.
    _write_record(
        tmp_path,
        "mem.decisions",
        title="Decision patterns",
        summary="The operator prefers reversible decisions",
        coord=[3, 0, 1],
    )
    verdict = verify_candidates(
        "what patterns have you noticed in how I make decisions",
        [
            {
                "memory_id": "mem.decisions",
                "coord": [3, 0, 1],
                "score": 0.8,
                "title": "Decision patterns",
                "summary": "The operator prefers reversible decisions",
            },
        ],
        root=tmp_path,
    )
    assert verdict.status == "verified", verdict.reason
    assert verdict.answer_payload is not None


def test_caller_region_entirely_non_recallable_grounds_nothing(tmp_path):
    # Rule 1, caller path: an explicit allowed_lobes wholly inside
    # NON_RECALLABLE_LOBES is a structural refusal at the verdict layer too
    # (defense in depth under the recall engine's own boundary).
    _write_record(tmp_path, "mem.note", title="Private note", summary="A private note")
    verdict = verify_candidates(
        "private note",
        [
            {
                "memory_id": "mem.note",
                "coord": [6, 0, 1],
                "score": 0.9,
                "title": "Private note",
                "summary": "A private note",
            },
        ],
        root=tmp_path,
        allowed_lobes=(6,),
    )
    assert verdict.status == "unknown"
    assert verdict.answer_payload is None
    assert "non-recallable" in verdict.reason


def test_mixed_region_with_recallable_lobe_is_not_refused(tmp_path):
    # Rule 1 requires the WHOLE region to be non-recallable: a mixed region
    # still grounds on its recallable lobes (denying lobe 6 candidates is the
    # recall engine's structural job, not the verdict's).
    _write_record(tmp_path, "mem.creator", title="Creator", summary="Built by Ada")
    verdict = verify_candidates(
        "creator",
        [
            {
                "memory_id": "mem.creator",
                "coord": [2, 0, 1],
                "score": 0.9,
                "title": "Creator",
                "summary": "Built by Ada",
            },
        ],
        root=tmp_path,
        allowed_lobes=(2, 6),
    )
    assert verdict.status == "verified"
    assert verdict.answer_payload is not None


def test_contentless_query_does_not_topicality_gate(tmp_path):
    # A query that is all stopwords has no topical core — the floor must not
    # gate (mirrors the recall engine's hop-gate fallback), or every
    # framing-word query would go dark.
    _write_record(tmp_path, "mem.creator", title="Creator", summary="Built by Ada")
    verdict = verify_candidates(
        "who is that",
        [
            {
                "memory_id": "mem.creator",
                "coord": [2, 0, 1],
                "score": 0.9,
                "title": "Creator",
                "summary": "Built by Ada",
            },
        ],
        root=tmp_path,
        allowed_lobes=(2,),
    )
    assert verdict.status == "verified"
    assert verdict.answer_payload is not None
