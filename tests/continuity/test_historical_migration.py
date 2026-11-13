"""WO-08 Steps 1–3 / 5–6 — historical records convert without inventing experience.

The retained formats are real (read from an isolated look at the live lobe-9 records): the
enriched body WO-06 and WO-07 build on, carrying a finish-line stamp and memory epochs but no thread
id, no open time and no clock epoch; the older distilled ``PriorEpisode`` the 4a path wrote, with no
stamp, no epoch and often no work kind; window episodes whose reflection a model produced, or only
a deterministic verdict counted from recorded outcomes. The fixtures copy those SHAPES with
synthetic values — no operator data.

The converter is pure and offline. What a source recorded is extracted with its path; what it did
not record becomes ``not_captured`` with conversion attribution — never a date taken from a file
time, a thread made up from the episode id, a success check, or a model reflection that never ran.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from schema.continuity_json import CanonicalEpisodeUnavailable, canonical_bytes, content_hash
from schema.episode_record import EpisodeRecord

FIXTURES = Path(__file__).parent / "fixtures" / "historical"


def _migrate():
    import importlib

    try:
        module = importlib.import_module("scripts.migrate_episode_continuity")
    except ModuleNotFoundError:
        pytest.fail("scripts/migrate_episode_continuity.py does not exist: history cannot convert")
    return module


def _source(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _convert(name: str):
    return _migrate().migrate_episode_document(_source(name))


def _decisions(result) -> dict[str, str]:
    return {row["field"]: row["decision"] for row in result.decisions}


def _observation(value):
    return value.state if hasattr(value, "state") else "plain"


# ── the seed ────────────────────────────────────────────────────────────────


def test_a_record_without_thread_or_time_converts_to_explicit_not_captured():
    source = _source("untimed_record")
    result = _migrate().migrate_episode_document(source)
    assert result.status == "converted", result.errors
    record = result.record

    assert record.identity["episode_id"] == "hist-untimed-0006", "the original episode id is kept"
    assert result.source_ref["sha256"] == content_hash(source)
    assert result.source_ref["record_id"] == source["id"]

    assert _observation(record.identity["thread_id"]) == "not_captured"
    for name in ("opened_at", "closed_at", "queue_arrival_seq"):
        assert record.temporal[name].state == "not_captured", name
    assert _observation(record.temporal["finish_line"]) == "not_captured"
    assert "conversion" in record.identity["thread_id"].reason.lower() or "convert" in (
        record.identity["thread_id"].reason.lower())

    text = json.dumps(result.converted_document)
    assert "2026-" not in json.dumps(record.to_dict()["temporal"]), (
        "a timestamp appeared that the source never recorded"
    )
    assert "hist-untimed-0006" in text


def test_converting_one_source_twice_is_byte_identical():
    first = _convert("untimed_record")
    second = _convert("untimed_record")
    assert canonical_bytes(first.converted_document) == canonical_bytes(second.converted_document)
    assert first.record.content_sha256() == second.record.content_sha256()


# ── the observed enriched record ────────────────────────────────────────────


def test_the_enriched_record_extracts_what_it_recorded_and_nothing_more():
    result = _convert("enriched_record")
    assert result.status == "converted", result.errors
    record = result.record
    decisions = _decisions(result)

    finish = record.temporal["finish_line"]
    assert finish["commit_seq"] == 41 and finish["event_date"] == "2026-08-02"
    assert decisions["temporal.finish_line"] == "extracted"
    assert _observation(record.temporal["coverage_clock"]) == "not_captured", (
        "no clock epoch id was ever recorded; an invented one would claim an ordering authority"
    )
    assert record.temporal["read_epoch_start"] == 40
    assert record.temporal["read_pin"]["graph_snapshot_hash"] == "a" * 64
    assert record.temporal["closed_at"].value == "2026-08-02T10:11:12.000000+00:00"
    assert record.temporal["opened_at"].state == "not_captured"
    assert _observation(record.identity["thread_id"]) == "not_captured"

    # concurrent commits stay concurrent commits — never consumed evidence
    assert list(record.temporal["concurrent_commits"]["episode_ids"]) == ["hist-parent-0001"]
    assert record.temporal["concurrent_commits"]["complete"] is True
    assert record.events["by_value"] == () and record.events["refs"] == ()
    assert "hist-parent-0001" not in json.dumps(record.to_dict()["events"])


def test_the_old_nested_outcome_is_extracted_without_a_runtime_accessor():
    record = _convert("enriched_record").record
    assert record.outcome.cascade_status == "completed"
    assert record.outcome.task_outcome == "success", "the nested pack said completed"
    assert record.outcome.verification_status == "unknown", "no check was ever recorded"
    assert record.outcome.feedback_status == "unknown"
    assert record.outcome.action_status == "unknown"
    note = record.outcome.assessments["task_outcome"]["note"]
    assert "outcome.outcome.status" in note, "the extraction names the path it read"

    failed = _convert("untimed_record").record
    assert failed.outcome.task_outcome == "failure"


def test_generated_text_stays_an_attributed_historical_assessment():
    record = _convert("enriched_record").record
    meta = record.metacognition
    assert meta["control"].decision == "DONE"
    assessment = record.to_dict()["metacognition"]["model_assessment"]
    assert assessment["author"] == "model"
    assert "historical" in json.dumps(assessment).lower()
    assert assessment["synthesis"].startswith("HIST-SYNTHESIS")
    assert all(row["status"] == "proposed" for row in meta["proposals"])
    assert record.events["by_value"] == (), "old generated text never becomes a verified event"


def test_the_converted_record_keeps_its_episode_and_takes_a_conversion_identity():
    from memory.episode_record_seal import episode_record_id

    source = _source("enriched_record")
    result = _migrate().migrate_episode_document(source)
    document = result.converted_document
    assert document["id"] == episode_record_id("hist-enriched-0001")
    assert document["coord"] == [9, 1, 0] and document["type"] == "episode"
    conversion = document["enrichment"]["conversion"]
    assert conversion["version"] == result.conversion_version
    assert conversion["source_record_id"] == source["id"]
    assert conversion["source_sha256"] == content_hash(source)
    body = json.loads(document["text"])
    assert EpisodeRecord.from_dict(body["episode_record"]) == result.record
    assert body["outcome_axes"]["task_outcome"] == "success"


# ── older and window shapes ─────────────────────────────────────────────────


def test_an_older_distilled_prior_episode_converts_with_its_gaps_named():
    result = _convert("distilled_prior_episode")
    assert result.status == "converted", result.errors
    record = result.record
    decisions = _decisions(result)
    assert record.origin["trigger"]["opened_by"] == "operator_query"
    assert _observation(record.identity["work_kind"]) == "not_captured"
    assert _observation(record.temporal["finish_line"]) == "not_captured"
    assert _observation(record.objective["original_request"]) == "not_captured"
    assert _observation(record.objective["criteria_revision"]) == "not_captured"
    assert record.outcome.task_outcome == "success"
    assert decisions["identity.work_kind"] == "not_captured"
    assert decisions["temporal.finish_line"] == "not_captured"


def test_a_model_reflection_is_preserved_as_the_models_attributed_assessment():
    record = _convert("model_reflection").record
    assessment = record.to_dict()["metacognition"]["model_assessment"]
    assert assessment["author"] == "model"
    assert "HIST-MODEL-REFLECTION" in json.dumps(assessment)
    assert assessment["window_reflection"]["reflection_source"] == "model"


def test_a_deterministic_verdict_stays_a_diagnostic_never_a_model_reflection():
    result = _convert("deterministic_reflection")
    assert result.status == "converted", result.errors
    meta = result.record.metacognition
    control = meta["control"]
    assert getattr(control, "state", None) == "unavailable"
    assert "deterministic" in control.reason.lower()
    assert "10 of 10 episodes completed" in control.reason
    assert meta["model_assessment"] == {} and meta["proposals"] == ()
    assert _decisions(result)["metacognition"] == "diagnostic"


def test_malformed_record_text_is_reported_never_dropped():
    source = _source("malformed_text")
    result = _migrate().migrate_episode_document(source)
    assert result.status == "malformed"
    assert result.errors and result.converted_document is None and result.record is None
    assert result.source_ref["record_id"] == "9.01.deadbeef"
    assert result.source_ref["sha256"] == content_hash(source)


def test_the_expected_unknown_decisions_are_exact_per_fixture():
    expected = {
        "enriched_record": {"identity.thread_id", "temporal.opened_at", "temporal.coverage_clock",
                            "temporal.queue_arrival_seq", "objective.original_request",
                            "origin.authority_snapshot_ref", "origin.initial_trajectory_ref",
                            "identity.turn_id", "identity.continuation_id", "identity.job_id",
                            "identity.attempt_id"},
        "untimed_record": {"identity.thread_id", "temporal.opened_at", "temporal.closed_at",
                           "temporal.coverage_clock", "temporal.queue_arrival_seq",
                           "temporal.finish_line", "temporal.read_epoch_start",
                           "temporal.read_pin", "objective.original_request",
                           "objective.criteria_revision", "identity.work_kind", "identity.lane",
                           "origin.trigger.opened_by", "origin.authority_snapshot_ref",
                           "origin.initial_trajectory_ref", "identity.turn_id",
                           "identity.continuation_id", "identity.job_id", "identity.attempt_id",
                           "identity.pipeline_id", "temporal.concurrent_commits", "metacognition"},
    }
    for name, unknowns in expected.items():
        decided = {field for field, decision in _decisions(_convert(name)).items()
                   if decision in ("not_captured", "diagnostic")}
        assert decided == unknowns, (name, decided ^ unknowns)


def test_an_already_sealed_record_is_current_and_not_converted_again(tmp_path, monkeypatch):
    from tests.helpers import continuity_terminus as ct
    from pressure_loop.episode import EpisodeStore

    ct.isolate(monkeypatch, tmp_path)
    store = EpisodeStore()
    carrier = ct.dress_learn(ct.open_carrier(store))
    ct.close(store)
    docs = ct.canonical_episode_documents(carrier.episode_id)
    result = _migrate().migrate_episode_document(docs[0])
    assert result.status == "current"
    assert result.converted_document is None and not result.errors
    from memory import episode_sram

    episode_sram.reset()


# ── the new-format-only reader and its activation preflight ─────────────────


def test_the_new_format_reader_accepts_a_converted_record():
    from pressure_loop.temporal_store import read_canonical_episode_record

    result = _convert("enriched_record")
    assert read_canonical_episode_record(result.converted_document) == result.record


def test_the_new_format_reader_reports_an_old_record_as_a_migration_requirement():
    from pressure_loop.temporal_store import read_canonical_episode_record

    with pytest.raises(CanonicalEpisodeUnavailable, match="migration required"):
        read_canonical_episode_record(_source("enriched_record"))


def test_the_activation_preflight_counts_every_record_and_is_never_empty_history(tmp_path):
    from pressure_loop.temporal_store import episode_migration_preflight

    root = tmp_path / "memory" / "900_temporal" / "events" / "episode"
    root.mkdir(parents=True)
    for name in ("enriched_record", "distilled_prior_episode", "malformed_text"):
        (root / f"{name}.json").write_text(json.dumps(_source(name)), encoding="utf-8")
    converted = _convert("model_reflection").converted_document
    (root / "converted.json").write_text(json.dumps(converted), encoding="utf-8")

    report = episode_migration_preflight(tmp_path / "memory")
    assert report["current"] == [converted["id"]]
    assert sorted(report["requires_migration"]) == ["9.01.0d150002", "9.01.0e1c0001"]
    assert report["malformed"] == ["9.01.deadbeef"]
    assert report["ready"] is False


# ── contract decision 19: the historical-absence form ───────────────────────


def test_a_historical_absence_is_accepted_only_as_a_non_observed_observation():
    from schema.continuity_json import ContinuityContractError, Observation
    from tests.helpers.continuity_records import make_episode_record

    base = make_episode_record().to_dict()
    absent = Observation.missing("not_captured", reason="the source era did not record it")

    base["identity"]["thread_id"] = absent.to_dict()
    base["temporal"]["finish_line"] = absent.to_dict()
    record = EpisodeRecord.from_dict(base)
    assert record.identity["thread_id"].state == "not_captured"
    assert EpisodeRecord.from_dict(record.to_dict()) == record

    observed = make_episode_record().to_dict()
    observed["identity"]["thread_id"] = Observation.observed("thread-x").to_dict()
    with pytest.raises(ContinuityContractError, match="plain value or an absence"):
        EpisodeRecord.from_dict(observed)

    blank = make_episode_record().to_dict()
    blank["identity"]["thread_id"] = ""
    with pytest.raises(ContinuityContractError):
        EpisodeRecord.from_dict(blank)
