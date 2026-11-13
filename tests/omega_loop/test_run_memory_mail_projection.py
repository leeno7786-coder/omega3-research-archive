"""Proof metadata must not crowd distinct cross-lane evidence out of Omega."""
from copy import deepcopy
import json

import pytest

from global_loop import intersections as mail
from omega_loop.episode.packet import omega_mail_fits
from omega_loop.episode.prompts import CROSS_LANE_MAIL_BLOCK_CHARS, render_cross_lane_block
from omega_loop.episode.types import bounded_mail
from pressure_loop import cognition_queue as cq
from pressure_loop import specialist_lane_handoff as handoff


def _row():
    anchor = {"job_id": "job-a", "thread_id": "thread-a", "instruction_revision": 3,
              "project_root": "J:/" + "portable-project/" * 12}
    evidence = []
    for index in range(5):
        citation = {
            "source_id": f"src-{index}", "source_type": "auto_loop.dev_observation",
            "source_session_id": "previous-agent-episode",
            "source_session_timestamp": "2026-09-28T12:00:00+00:00",
            "span_start": 0, "span_end": 400,
            "span_hash": "a" * 64, "source_content_hash": "b" * 64,
        }
        evidence.append({
            "memory_id": f"memory-{index}", "coord": [11, 1, index],
            "title": f"Build observation {index}", "excerpt": "verified build detail; " * 18,
            "score": float(5 - index), "source_citation": citation,
            "source_provenance": {**citation, "author": "promotion:previous-agent-episode",
                                  "confidence": 1.0, "write_ordinal": index,
                                  "projection_receipt_required": True},
            # Previous revisions remain eligible; do not replace this with the current anchor.
            "run_provenance": {**anchor, "instruction_revision": index % 3},
        })
    return {
        "mail_id": "instruction-mail", "purpose": "specialist_lane_handoff",
        "requirement": "required", "source_actor": "operator",
        "source_episode_id": "operator-cycle", "arrival_seq": 1,
        "payload": {"lane": "agent", "text": 'Use CSV column order; retain <schema> & "names".',
                    "route_context": "previous discussion", "handoff_id": "handoff-a",
                    "original_cycle_id": "operator-cycle",
                    "run_memory": {"kind": "run_scoped_recall", "status": "verified",
                                   "anchor": anchor, "query": "current run CSV update",
                                   "allowed_lobes": [8, 11], "evidence": evidence,
                                   "recall_method": "remember_active_run"}},
        "provenance": [f"memory:memory-{index}" for index in range(5)],
    }


def _boundary_row():
    row = _row()
    # Exercise the reported boundary: one fully verified instruction fits by itself,
    # but duplicating its proof leaves no space for another small, eligible mail row.
    available = CROSS_LANE_MAIL_BLOCK_CHARS - 100 - len(render_cross_lane_block([row]))
    assert available > 0
    row["payload"]["route_context"] += "x" * available
    assert len(render_cross_lane_block([row])) == CROSS_LANE_MAIL_BLOCK_CHARS - 100
    return row


def _browser_row():
    return {
        "mail_id": "browser-mail", "purpose": "lane_evidence", "requirement": "preferred",
        "source_actor": "browser", "source_episode_id": "browser-episode", "arrival_seq": 2,
        "payload": {"text": "The streaming writer handles files larger than memory",
                    "url": "https://docs.invalid/streaming-writers"},
        "provenance": ["episode:browser-episode"],
    }


def test_run_memory_projection_is_lossless_pure_and_idempotent():
    source = _row()
    before = deepcopy(source)
    [projected] = bounded_mail([source])
    assert source == before
    assert bounded_mail([projected]) == (projected,)
    original = source["payload"]["run_memory"]
    actual = projected["payload"]["run_memory"]
    assert {key: value for key, value in actual.items() if key != "evidence"} == {
        key: value for key, value in original.items() if key != "evidence"}
    assert len(actual["evidence"]) == len(original["evidence"])
    for one, expected in zip(actual["evidence"], original["evidence"], strict=True):
        assert {key: value for key, value in one.items() if key != "source_provenance"} == {
            key: value for key, value in expected.items() if key != "source_provenance"}
        assert one["source_provenance"] == {
            key: value for key, value in expected["source_provenance"].items()
            if key not in expected["source_citation"]}
        assert {**one["source_citation"], **one["source_provenance"]} == expected["source_provenance"]
    assert projected["payload"]["text"] == source["payload"]["text"]
    assert projected["payload"]["route_context"] == source["payload"]["route_context"]


def test_run_proof_dedup_retains_conflicting_values_and_json_types():
    row = _row()
    record = row["payload"]["run_memory"]["evidence"][0]
    record["source_citation"].update(flag=True, sequence=[True], numeric=1, nested={"a": 1, "b": 2})
    record["source_provenance"].update(flag=1, sequence=[1], numeric=1.0, nested={"b": 2, "a": 1},
                                      source_id="different-source")
    before = deepcopy(record["source_provenance"])
    [projected] = bounded_mail([row])
    actual = projected["payload"]["run_memory"]["evidence"][0]["source_provenance"]
    assert "nested" not in actual, "JSON object key order is not evidence"
    for key in ("flag", "sequence", "numeric", "source_id"):
        assert json.dumps(actual[key]) == json.dumps(before[key])


def test_exact_projection_budget_keeps_distinct_browser_evidence_with_all_run_records():
    rows = [_boundary_row(), _browser_row()]
    assert len(render_cross_lane_block(rows)) > CROSS_LANE_MAIL_BLOCK_CHARS
    assert omega_mail_fits(rows), "duplicate proof must not crowd out distinct lane evidence"
    projected = bounded_mail(rows)
    assert len(render_cross_lane_block(projected)) <= CROSS_LANE_MAIL_BLOCK_CHARS
    assert [row["mail_id"] for row in projected] == [row["mail_id"] for row in rows]
    assert len(projected[0]["payload"]["run_memory"]["evidence"]) == 5


def test_actual_claim_and_restart_keep_identical_projection_and_durable_full_proof(monkeypatch):
    row, browser = _boundary_row(), _browser_row()
    context = deepcopy(row["payload"]["run_memory"])
    monkeypatch.setattr(handoff, "_active_run_memory", lambda *_args: (
        "job-a", "job-cycle", "build the export", deepcopy(context)))
    receipt = handoff.stage_operator_lane_work(
        lane="agent", request=row["payload"]["text"], cycle_id="operator-cycle",
        thread_id="thread-a", route_context=row["payload"]["route_context"])
    evidence = mail.pin_mail(
        thread_id="thread-a", delivery_point="omega_reason", purpose="lane_evidence",
        requirement="preferred", source_actor="browser", source_episode_id="browser-episode",
        payload=browser["payload"], provenance=browser["provenance"])
    wanted = [receipt["mail_id"], evidence["mail_id"]]
    durable = json.loads(mail.mail_path().read_text(encoding="utf-8"))
    pinned = durable["mail"][receipt["mail_id"]]
    assert pinned["payload"]["run_memory"] == context, "selection retains all complete proofs"
    first = mail.claim_for_fold(
        fold_id="stable-fold", lane="agent", thread_id="thread-a", job_id="job-a",
        episode_id="fold-before-restart", lease_s=1800, admit=omega_mail_fits)
    assert [one["mail_id"] for one in first["claimed"]] == wanted
    assert first["refused"] == []
    prompt = render_cross_lane_block(bounded_mail(first["claimed"]))
    assert len(prompt) <= CROSS_LANE_MAIL_BLOCK_CHARS
    cq._reset_cache_for_tests()
    mail.reset_cache_for_tests()
    again = mail.claim_for_fold(
        fold_id="stable-fold", lane="agent", thread_id="thread-a", job_id="job-a",
        episode_id="fold-after-restart", lease_s=1800, admit=omega_mail_fits)
    assert again["disposition"] == "replayed"
    assert [one["mail_id"] for one in again["claimed"]] == wanted
    assert render_cross_lane_block(bounded_mail(again["claimed"])) == prompt
    assert again["claimed"][0]["payload"]["run_memory"] == context
    after = json.loads(mail.mail_path().read_text(encoding="utf-8"))
    assert after["mail"][receipt["mail_id"]]["payload"] == pinned["payload"]


@pytest.mark.parametrize("change", ["purpose", "kind", "empty", "empty_run", "not_applicable", "no_active_run"])
def test_other_mail_and_no_memory_context_are_exact(change):
    row = _row()
    if change == "purpose":
        row["purpose"] = "lane_evidence"
    elif change == "kind":
        row["payload"]["run_memory"]["kind"] = "other_evidence"
    elif change == "empty_run":
        row["payload"]["run_memory"].update(status="empty", evidence=[])
    else:
        row["payload"]["run_memory"] = {"status": change, "evidence": []}
    assert bounded_mail([row])[0] == row
    assert render_cross_lane_block(bounded_mail([row])) == render_cross_lane_block([row])


def test_distinct_large_excerpts_still_exceed_budget():
    row = _row()
    row["payload"]["run_memory"]["evidence"][0]["excerpt"] = "large distinct evidence " * 1000
    assert not omega_mail_fits([row])
    assert bounded_mail([row])[0]["payload"]["run_memory"]["evidence"][0]["excerpt"] == (
        row["payload"]["run_memory"]["evidence"][0]["excerpt"])


def test_selection_also_fits_the_unchanged_durable_payload_limit(monkeypatch, tmp_path):
    from memory.source_ledger import record_source

    row = _row()
    context = row["payload"]["run_memory"]
    context["evidence"] = context["evidence"][:2]
    for index, record in enumerate(context["evidence"]):
        artifact = record_source(
            record["excerpt"], source_type="auto_loop.dev_observation",
            origin_id=f"original-observation-{index}",
            metadata={"source_session_id": "session:" + "s" * 3500,
                      "source_session_timestamp": "2026-09-28T12:00:00+00:00"},
            root=tmp_path / "source-ledger")
        citation = artifact.citation()
        record["source_citation"] = citation
        record["source_provenance"] = {**citation, "author": "promotion:previous-agent-episode"}
    assert omega_mail_fits([row]), "rendered context fits after lossless proof dedup"
    with pytest.raises(ValueError, match="mail payload exceeds"):
        mail._payload_for_pin(row["payload"])
    original = deepcopy(context)
    monkeypatch.setattr(handoff, "_active_run_memory", lambda *_args: (
        "job-a", "job-cycle", "build the export", deepcopy(context)))

    receipt = handoff.stage_operator_lane_work(
        lane="agent", request=row["payload"]["text"], cycle_id="operator-cycle",
        thread_id="thread-a", route_context=row["payload"]["route_context"])

    [pinned] = mail.pending_mail()
    selected = pinned["payload"]["run_memory"]
    assert selected["evidence"] == original["evidence"][:1]
    assert selected["evidence_selection"] == {
        "retrieved_count": 2, "included_count": 1,
        "omitted_memory_ids": [original["evidence"][1]["memory_id"]],
        "reason": "mail_payload_capacity",
    }
    assert context == original
    assert mail._payload_for_pin(pinned["payload"]) == pinned["payload"]
    assert omega_mail_fits([pinned])
    assert receipt["status"] == "queued"


def test_invalid_payload_is_explicit_even_when_capacity_would_omit_that_record(monkeypatch):
    row = _row()
    context = row["payload"]["run_memory"]
    context["evidence"][0]["excerpt"] = "distinct evidence " * 2000
    context["evidence"][1]["source_provenance"]["command"] = "forbidden authority"
    monkeypatch.setattr(handoff, "_active_run_memory", lambda *_args: (
        "job-a", "job-cycle", "build the export", deepcopy(context)))
    with pytest.raises(ValueError, match="may not carry executable authority"):
        handoff.stage_operator_lane_work(
            lane="agent", request=row["payload"]["text"], cycle_id="operator-cycle",
            thread_id="thread-a", route_context=row["payload"]["route_context"])
    assert mail.pending_mail() == []
    assert cq.peek_inputs() == ()


def test_pin_returns_the_same_isolated_snapshot_that_passed_capacity(monkeypatch):
    source = {"text": "original words"}
    original = deepcopy(source)
    real_fits = mail.payload_fits

    def mutate_caller_after_check(payload):
        fits = real_fits(payload)
        source["text"] = "large changed payload " * 1000
        return fits

    monkeypatch.setattr(mail, "payload_fits", mutate_caller_after_check)
    checked = mail._payload_for_pin(source)
    assert checked == original, "pin must return the isolated snapshot whose size it checked"
    assert real_fits(checked)
