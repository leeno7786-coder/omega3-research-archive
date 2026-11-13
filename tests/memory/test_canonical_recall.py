"""Standalone-organ canonical recall integration contracts.

These tests stay on ``tmp_path`` deliberately: recall must be provably read-only
and must never inspect, rebuild, or mutate the live Omega memory tree.
"""

from __future__ import annotations

import json
from pathlib import Path

from memory.api import QuerySpec, RetrievalResult


def _write_record(
    root: Path,
    *,
    memory_id: str,
    coord: list[int],
    title: str,
    text: str,
) -> Path:
    lobe_dir = {
        1: "100_personas",
        2: "200_knowledge",
    }[coord[0]]
    target = root / "memory" / lobe_dir / "operator" / f"{memory_id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "id": memory_id,
                "coord": coord,
                "type": "persona" if coord[0] == 1 else "knowledge",
                "title": title,
                "summary": text,
                "text": text,
                "domain": "operator" if coord[0] == 1 else "python",
                "tags": ["operator", "noah"],
                "provenance": {},
                "temporal_epoch_us": 100,
            }
        ),
        encoding="utf-8",
    )
    return target


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_exact_noah_query_recovers_persona_from_canonical_files(tmp_path: Path) -> None:
    from memory.canonical_recall import search_canonical_files

    _write_record(
        tmp_path,
        memory_id="1.00.operator_name",
        coord=[1, 0, 100],
        title="Operator name",
        text="The operator's full name is Noah Lee.",
    )

    hits = search_canonical_files(
        tmp_path,
        "Omega, what do you remember about Noah?",
        limit=8,
        allowed_lobes=(1,),
    )

    assert hits
    assert hits[0]["memory_id"] == "1.00.operator_name"
    assert hits[0]["coord"][0] == 1


def test_canonical_candidate_caps_receipt_names_dropped_ids(tmp_path: Path) -> None:
    from memory.canonical_recall import search_canonical_files

    for index in range(4):
        _write_record(
            tmp_path,
            memory_id=f"2.00.catalog_{index}",
            coord=[2, 0, 100 + index],
            title=f"Build catalog entry {index}",
            text=f"The build catalog tracks signed package {index}.",
        )

    receipts: list[dict] = []
    hits = search_canonical_files(
        tmp_path,
        "build catalog",
        limit=2,
        evidence_limit=1,
        allowed_lobes=(2,),
        denied_lobes=(),
        truncation_receipts=receipts,
    )

    assert len(hits) == 2
    lexical = next(row for row in receipts if row["stage"] == "canonical_lexical_admission")
    final = next(row for row in receipts if row["stage"] == "canonical_final_evidence_cap")
    assert lexical["input_count"] == lexical["output_count"] == 4
    assert lexical["dropped_count"] == 0
    assert final["input_count"] == 4
    assert final["output_count"] == 2
    assert final["dropped_count"] == 2
    assert set(final["dropped_candidate_ids"]) == {
        f"2.00.catalog_{index}" for index in range(4)
    } - {row["memory_id"] for row in hits}
    assert final["details"]["evidence_window_limit"] == 1

    from memory.recall_verifier import verify_candidates

    verdict = verify_candidates(
        "build catalog",
        hits,
        meta={"candidate_truncation_receipts": receipts},
        root=tmp_path,
        allowed_lobes=(2,),
    )
    trace = verdict.retrieval_trace
    pre_verifier_ids = set(trace["pre_verifier_candidate_pool"]["candidate_ids"])
    verifier_outcome_ids = {item["memory_id"] for item in verdict.rejected_hits}
    assert pre_verifier_ids == verifier_outcome_ids == {
        row["memory_id"] for row in hits
    }
    assert set(final["dropped_candidate_ids"]).isdisjoint(pre_verifier_ids)


def test_canonical_recall_preserves_mril_lobe_restrictions(tmp_path: Path) -> None:
    from memory.canonical_recall import search_canonical_files

    _write_record(
        tmp_path,
        memory_id="1.00.operator_name",
        coord=[1, 0, 100],
        title="Operator name",
        text="The operator's full name is Noah Lee.",
    )
    _write_record(
        tmp_path,
        memory_id="2.01.noah_package",
        coord=[2, 1, 101],
        title="Noah Python package",
        text="Noah is the name of a Python package used in this example.",
    )

    hits = search_canonical_files(
        tmp_path,
        "What do you remember about Noah?",
        limit=8,
        allowed_lobes=(1,),
    )

    assert {hit["memory_id"] for hit in hits} == {"1.00.operator_name"}


def test_canonical_recall_distinguishes_spouse_and_child_family_concepts(
    tmp_path: Path,
) -> None:
    from memory.canonical_recall import search_canonical_files

    _write_record(
        tmp_path,
        memory_id="1.01.mitchell_married",
        coord=[1, 1, 100],
        title="Mitchell Ray marital status",
        text="Mitchell Ray is married.",
    )
    _write_record(
        tmp_path,
        memory_id="1.01.mitchell_daughters",
        coord=[1, 1, 90],
        title="Mitchell Ray children",
        text="Mitchell Ray has 2 daughters.",
    )

    spouse_hits = search_canonical_files(
        tmp_path,
        "Who has a spouse?",
        allowed_lobes=(1,),
        limit=8,
    )
    child_hits = search_canonical_files(
        tmp_path,
        "Who has children?",
        allowed_lobes=(1,),
        limit=8,
    )

    assert spouse_hits[0]["memory_id"] == "1.01.mitchell_married"
    assert child_hits[0]["memory_id"] == "1.01.mitchell_daughters"


def test_canonical_recall_bridges_doctor_to_medical_specialties(tmp_path: Path) -> None:
    from memory.canonical_recall import search_canonical_files

    _write_record(
        tmp_path,
        memory_id="1.01.marguerite",
        coord=[1, 1, 100],
        title="Marguerite Osei",
        text="Marguerite Osei is a dermatologist.",
    )
    _write_record(
        tmp_path,
        memory_id="1.01.dana",
        coord=[1, 1, 90],
        title="Dana Whitfield",
        text="Dana Whitfield is a veterinarian.",
    )

    hits = search_canonical_files(
        tmp_path,
        "Who are the doctors I know?",
        allowed_lobes=(1,),
        limit=8,
    )

    assert {hit["memory_id"] for hit in hits} == {"1.01.marguerite", "1.01.dana"}


def test_verifier_topicality_uses_precise_family_subconcepts() -> None:
    from memory.recall_verifier import _partition_topicality

    verified = [
        {
            "memory_id": "1.01.mitchell_married",
            "title": "Mitchell Ray marital status",
            "summary": "Mitchell Ray is married.",
            "text": "Mitchell Ray is married.",
            "tags": [],
        },
        {
            "memory_id": "1.01.mitchell_daughters",
            "title": "Mitchell Ray children",
            "summary": "Mitchell Ray has 2 daughters.",
            "text": "Mitchell Ray has 2 daughters.",
            "tags": [],
        },
    ]

    spouse_topical, _ = _partition_topicality("Who has a spouse?", verified)
    child_topical, _ = _partition_topicality("Who has children?", verified)

    assert [item["memory_id"] for item in spouse_topical] == ["1.01.mitchell_married"]
    assert [item["memory_id"] for item in child_topical] == ["1.01.mitchell_daughters"]


def test_canonical_recall_is_strictly_read_only(tmp_path: Path) -> None:
    from memory.canonical_recall import search_canonical_files

    _write_record(
        tmp_path,
        memory_id="1.00.operator_name",
        coord=[1, 0, 100],
        title="Operator name",
        text="The operator's full name is Noah Lee.",
    )
    before = _tree_bytes(tmp_path)
    search_canonical_files(
        tmp_path,
        "What do you remember about Noah?",
        limit=8,
        allowed_lobes=(1,),
    )

    assert _tree_bytes(tmp_path) == before


def test_api_union_recovers_noah_when_graph_and_dme_return_nothing(
    tmp_path: Path,
) -> None:
    from memory import api as memory_api

    _write_record(
        tmp_path,
        memory_id="1.00.operator_name",
        coord=[1, 0, 100],
        title="Operator name",
        text="The operator's full name is Noah Lee.",
    )
    empty = RetrievalResult(cycle_id="noah-cold", candidates=[], meta={})

    merged = memory_api._merge_canonical_recall(
        QuerySpec(
            cycle_id="noah-cold",
            text="Omega, what do you remember about Noah?",
            allowed_lobes=[1],
            denied_lobes=[],
            limit=8,
        ),
        empty,
        root=tmp_path,
    )

    assert [candidate.id for candidate in merged.candidates] == ["1.00.operator_name"]
    assert merged.meta["canonical_recall"]["filesystem_fallback_used"] is True


def test_canonical_merge_preserves_posture_winner_order(monkeypatch, tmp_path: Path) -> None:
    from memory import api as memory_api
    from memory.api import RetrievalCandidate

    lane_winner = RetrievalCandidate(
        id="1.00.operator_identity",
        coord=[1, 0, 100],
        score=0.4,
        title="Operator identity",
    )
    bge_winner = RetrievalCandidate(
        id="2.00.similar_text",
        coord=[2, 0, 101],
        score=0.9,
        title="Similar text",
    )

    def canonical_results(*args, **kwargs):
        del args, kwargs
        return [
            {"memory_id": bge_winner.id, "coord": bge_winner.coord, "title": bge_winner.title},
            {"memory_id": lane_winner.id, "coord": lane_winner.coord, "title": lane_winner.title},
        ]

    monkeypatch.setattr("memory.canonical_recall.search_canonical_files", canonical_results)
    monkeypatch.setattr("memory.canonical_recall.last_semantic_admission", lambda: None)

    merged = memory_api._merge_canonical_recall(
        QuerySpec(cycle_id="posture-order", text="who is the operator", limit=5),
        RetrievalResult(
            cycle_id="posture-order",
            candidates=[lane_winner, bge_winner],
            meta={
                "recall_winners": [
                    {"memory_id": lane_winner.id},
                    {"memory_id": bge_winner.id},
                ]
            },
        ),
        root=tmp_path,
    )

    assert [candidate.id for candidate in merged.candidates] == [
        lane_winner.id,
        bge_winner.id,
    ]


def test_verdict_gate_exposes_only_admitted_posture_winners(monkeypatch, tmp_path: Path) -> None:
    from memory import api as memory_api
    from memory.api import RetrievalCandidate
    from memory.taxonomy.schemas import RecallVerdict

    admitted = RetrievalCandidate(
        id="1.00.admitted",
        coord=[1, 0, 100],
        score=0.7,
        title="Admitted",
    )
    rejected = RetrievalCandidate(
        id="2.00.rejected",
        coord=[2, 0, 101],
        score=0.8,
        title="Rejected",
    )
    verdict = RecallVerdict(
        status="verified",
        query="who is the operator",
        answer_payload={
            "candidates": (
                {
                    "memory_id": admitted.id,
                    "coord": admitted.coord,
                    "score": admitted.score,
                    "title": admitted.title,
                    "summary": admitted.title,
                },
            )
        },
    )

    monkeypatch.setattr("memory.recall_verifier.verify_retrieval_result", lambda *args, **kwargs: verdict)
    monkeypatch.setattr("memory.recall_verifier.append_verdict_log", lambda *args, **kwargs: None)

    gated = memory_api._apply_recall_verdict_gate(
        QuerySpec(cycle_id="verdict-order", text="who is the operator"),
        RetrievalResult(
            cycle_id="verdict-order",
            candidates=[admitted, rejected],
            meta={
                "recall_winners": [
                    {"memory_id": rejected.id},
                    {"memory_id": admitted.id},
                ]
            },
        ),
        root=tmp_path,
    )

    assert [winner["memory_id"] for winner in gated.meta["recall_winners"]] == [admitted.id]
    assert [candidate.id for candidate in gated.candidates] == [admitted.id]


def test_verifier_accepts_cited_filesystem_candidate_when_coord_index_is_cold(
    tmp_path: Path,
) -> None:
    from memory.recall_verifier import verify_candidates
    from memory.source_ledger.store import record_source

    text = "The operator's full name is Noah Lee."
    artifact = record_source(
        text,
        source_type="operator_onboarding",
        origin_id="onboarding-noah",
        root=tmp_path,
    )
    target = _write_record(
        tmp_path,
        memory_id="1.00.operator_name",
        coord=[1, 0, 100],
        title="Operator name",
        text=text,
    )
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["provenance"] = artifact.citation()
    target.write_text(json.dumps(payload), encoding="utf-8")

    verdict = verify_candidates(
        "What do you remember about Noah?",
        [
            {
                "memory_id": "1.00.operator_name",
                "coord": [1, 0, 100],
                "title": "Operator name",
                "summary": text,
                "path": target.relative_to(tmp_path).as_posix(),
                "filesystem_fallback": True,
            }
        ],
        root=tmp_path,
        allowed_lobes=(1,),
    )

    assert verdict.status == "verified", verdict.reason
    assert verdict.answer_payload["candidates"][0]["memory_id"] == "1.00.operator_name"


def test_verifier_rejects_required_projection_receipt_when_missing(tmp_path: Path) -> None:
    from memory.recall_verifier import verify_candidates
    from memory.source_ledger.store import record_source

    text = "The operator's full name is Noah Lee."
    artifact = record_source(
        text,
        source_type="operator_onboarding",
        origin_id="onboarding-noah",
        root=tmp_path,
    )
    target = _write_record(
        tmp_path,
        memory_id="1.00.operator_name",
        coord=[1, 0, 100],
        title="Operator name",
        text=text,
    )
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["provenance"] = {
        **artifact.citation(),
        "projection_receipt_required": True,
    }
    payload["flags"] = {
        "flagged": True,
        "flag_reason": "sensitive_persona",
        "flag_response_mode": "hedge",
    }
    target.write_text(json.dumps(payload), encoding="utf-8")

    verdict = verify_candidates(
        "What do you remember about Noah?",
        [
            {
                "memory_id": payload["id"],
                "coord": payload["coord"],
                "title": payload["title"],
                "summary": text,
                "path": target.relative_to(tmp_path).as_posix(),
                "filesystem_fallback": True,
            }
        ],
        root=tmp_path,
        allowed_lobes=(1,),
    )

    assert verdict.status == "unknown"
    assert "projection receipt" in verdict.reason


def test_graph_membership_does_not_impersonate_semantic_admission(tmp_path: Path) -> None:
    from memory.canonical_recall import search_canonical_files

    _write_record(
        tmp_path,
        memory_id="2.01.cold_member",
        coord=[2, 1, 1],
        title="ENT visit",
        text="I was diagnosed with chronic sinusitis by an ENT specialist.",
    )
    hits = search_canonical_files(
        tmp_path,
        "orchard harvest apples",
        include_ids=("2.01.cold_member",),
        allowed_lobes=(2,),
        limit=8,
    )

    assert hits and hits[0]["memory_id"] == "2.01.cold_member"
    assert hits[0].get("semantic_recall") is not True


def test_verifier_uses_same_morphology_as_canonical_scorer(tmp_path: Path) -> None:
    from memory.recall_verifier import verify_candidates
    from memory.source_ledger.store import record_source

    text = "Specialists."
    artifact = record_source(
        text,
        source_type="operator_chat",
        origin_id="morphology",
        root=tmp_path,
    )
    target = _write_record(
        tmp_path,
        memory_id="2.01.specialists",
        coord=[2, 1, 2],
        title="Care record",
        text=text,
    )
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["provenance"] = artifact.citation()
    target.write_text(json.dumps(payload), encoding="utf-8")

    verdict = verify_candidates(
        "Which specialist?",
        [
            {
                "memory_id": payload["id"],
                "coord": payload["coord"],
                "title": payload["title"],
                "summary": text,
                "path": target.relative_to(tmp_path).as_posix(),
                "filesystem_fallback": True,
            }
        ],
        root=tmp_path,
        allowed_lobes=(2,),
    )

    assert verdict.status == "verified", verdict.reason


def test_verified_semantic_candidate_is_not_vetoed_by_lexical_topicality(
    tmp_path: Path,
) -> None:
    from memory.recall_verifier import verify_candidates
    from memory.source_ledger.store import record_source

    text = "I was diagnosed with chronic sinusitis by an ENT specialist."
    artifact = record_source(
        text,
        source_type="operator_chat",
        origin_id="semantic-doctor",
        root=tmp_path,
    )
    target = _write_record(
        tmp_path,
        memory_id="2.01.ent_visit",
        coord=[2, 1, 3],
        title="Medical event",
        text=text,
    )
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["provenance"] = artifact.citation()
    target.write_text(json.dumps(payload), encoding="utf-8")

    verdict = verify_candidates(
        "Which doctor did I visit?",
        [
            {
                "memory_id": payload["id"],
                "coord": payload["coord"],
                "title": payload["title"],
                "summary": text,
                "path": target.relative_to(tmp_path).as_posix(),
                "filesystem_fallback": True,
                "semantic_recall": True,
            }
        ],
        root=tmp_path,
        allowed_lobes=(2,),
    )

    assert verdict.status == "verified", verdict.reason


def test_read_memory_end_to_end_recovers_verified_noah_with_cold_projections(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from memory import api as memory_api
    from memory.projection_receipt import write_projection_receipt
    from memory.source_ledger.store import record_source

    profile_root = tmp_path / "_canonical_memory_isolation"
    text = "The operator's full name is Noah Lee."
    artifact = record_source(
        text,
        source_type="operator_onboarding",
        origin_id="onboarding-noah",
        root=profile_root,
    )
    target = _write_record(
        profile_root,
        memory_id="1.00.operator_name",
        coord=[1, 0, 100],
        title="Operator name",
        text=text,
    )
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["provenance"] = {
        **artifact.citation(),
        "projection_receipt_required": True,
    }
    payload["flags"] = {
        "flagged": True,
        "flag_reason": "sensitive_persona",
        "flag_response_mode": "hedge",
    }
    target.write_text(json.dumps(payload), encoding="utf-8")
    write_projection_receipt(payload, root=profile_root)
    monkeypatch.setattr(memory_api, "OMEGA_ROOT", profile_root)
    # bypass the DME lane entirely: the recall hook consumes the observed merge
    # seam (Cut 1), so stub that one and hand back an empty execution
    from memory.dme.adapter import DmeRecallExecution

    monkeypatch.setattr(
        "memory.dme.adapter.merge_dme_candidate_dicts_observed",
        lambda _query, candidates, **_kwargs: (
            candidates,
            DmeRecallExecution((), None, "", ""),
        ),
    )

    result = memory_api.read_memory(
        QuerySpec(
            cycle_id="noah-end-to-end",
            text="Omega, what do you remember about Noah?",
            allowed_lobes=[1],
            denied_lobes=[],
            limit=8,
        )
    )

    assert result.meta["canonical_recall"]["filesystem_fallback_used"] is True
    assert result.meta["recall_verdict"]["status"] == "verified", result.meta["recall_verdict"][
        "reason"
    ]
    assert [candidate.id for candidate in result.candidates] == ["1.00.operator_name"]
    assert result.candidates[0].flagged is True
    assert result.candidates[0].flag_response_mode == "hedge"
    assert result.meta["recall_flag_present"] is True


def test_session_companion_that_is_also_ranked_keeps_its_semantic_mark(tmp_path: Path, monkeypatch) -> None:
    """Live 2026-09-06, "what's the name of my pet?": the retina admitted both dog
    records, but session-context transport rebuilt the second one as a companion
    of the first WITHOUT its ``semantic_recall`` mark, the marked ranked row was
    then skipped as already selected, and the verifier's lexical topicality
    floor rejected the unmarked copy as atopical. A companion that is also a
    ranked candidate must keep the ranked row itself."""
    import memory.canonical_recall as cr

    def _record(memory_id: str, title: str) -> dict:
        return {
            "id": memory_id,
            "coord": [1, 0, 1],
            "title": title,
            "text": title,
            "provenance": {"source_session_id": "s1"},
            "_epoch": 1,
            "_source_epoch": None,
            "_path": str(tmp_path / "memory" / f"{memory_id}.json"),
        }

    records = [
        _record("1.00.anchor", "Operator's dog: Biscuit"),
        _record("1.01.companion", "Biscuit — dog of operator"),
        _record("1.01.plain", "Biscuit is a known person."),
    ]
    positions = {"1.00.anchor": ("s1", 1), "1.01.companion": ("s1", 2), "1.01.plain": ("s1", 3)}
    monkeypatch.setattr(cr, "_provenance_position", lambda record: positions.get(str(record.get("id") or "")))
    monkeypatch.setattr(cr, "_has_cryptographic_source_binding", lambda record, *, root: True)

    anchor = cr._candidate_from_record(records[0], root=tmp_path, score=6.7)
    anchor["semantic_recall"] = True
    companion = cr._candidate_from_record(records[1], root=tmp_path, score=6.5)
    companion["semantic_recall"] = True
    ranked = [(6.7, 1, anchor), (6.5, 2, companion)]

    selected = cr._select_with_adjacent_context(ranked, records=records, root=tmp_path, limit=8)
    by_id = {row["memory_id"]: row for row in selected}

    assert by_id["1.01.companion"]["semantic_recall"] is True
    assert by_id["1.01.companion"]["score"] == 6.5  # its own ranked score, not a companion epsilon
    assert by_id["1.01.companion"]["context_for_memory_id"] == "1.00.anchor"
    # a neighbour that is NOT a ranked candidate is still transported as before
    assert by_id["1.01.plain"]["context_for_memory_id"] == "1.00.anchor"
    assert by_id["1.01.plain"].get("semantic_recall") is not True


def test_a_stored_plugin_result_is_recallable_from_the_situational_lobe(
    tmp_path: Path,
) -> None:
    """The last link in the plugin pipeline, and the one nothing covered.

    Ingest was proved end to end -- fold screens a provider result, the taxonomy
    adapter mints a `tool_result` event, and the capture hook writes it to lobe 4
    at the `outcome` facet. What no test asked is whether recall can then reach
    it. `remember_situation` is the MRIL tool bound to lobe 4, so a plugin
    result that lands there must come back for a question about it; if it did
    not, every plugin call would be written down and never remembered.
    """
    from memory.canonical_recall import search_canonical_files
    from memory.dewey_map import SITUATIONAL_FACETS
    from auto_loop.mril_tools.registry import REMEMBER_TOOL_REGISTRY

    outcome_y = SITUATIONAL_FACETS.index("outcome")
    memory_id = "4.%02d.slack_search_result" % outcome_y
    target = tmp_path / "memory" / "400_situational" / "operator" / f"{memory_id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "id": memory_id,
                "coord": [4, outcome_y, 0],
                "type": "situational",
                "title": "slack.official search_messages result",
                "summary": "slack.official search_messages result (completed)",
                "text": "slack.official search_messages returned: shipping friday in #release",
                "domain": "operator",
                "tags": ["operator", "slack", "plugin"],
                "provenance": {},
                "temporal_epoch_us": 100,
            }
        ),
        encoding="utf-8",
    )

    hits = search_canonical_files(
        tmp_path,
        "what did slack say about shipping?",
        limit=8,
        allowed_lobes=(4,),
    )

    assert hits, "a plugin result written to lobe 4 was not recallable"
    assert hits[0]["memory_id"] == memory_id
    assert hits[0]["coord"][0] == 4
    # And lobe 4 is the lobe the situational recall tool is actually bound to,
    # so the record is reachable by the tool the router picks -- not merely
    # present on disk.
    assert REMEMBER_TOOL_REGISTRY["remember_situation"][1] == 4
