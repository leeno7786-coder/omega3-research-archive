"""WO-11 — the window's numbers, with their populations (`build_window_metrics`).

The defect this order exists to prevent: a model reading ten episodes and reporting "60%
success" over a population nobody can reconstruct, with a completed cascade counted as a
success, an unverified claim counted as a verified one, and three retries of one job counted as
three independent trials.

So every number here is a row carrying its own numerator population, denominator population,
exclusions and unknowns with reasons, the stratum it was computed within, and the computation
version. A rate over nothing is not 0, not 0.5 and not 100% — it is an **undefined
observation** that still reports its counts.

The seed is the order's required reproduction: one completed-but-failed task, one verified
success, one unverified claim, one not-applicable idle episode.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.paths import OMEGA_ROOT
from schema.continuity_json import ContinuityContractError, Observation, canonical_bytes
from schema.window_evidence import WindowMetrics
from tests.helpers import continuity_metrics as cm

GOLDEN = Path(OMEGA_ROOT) / "tests" / "continuity" / "fixtures" / "metrics"


def _metrics(records=None, history=None):
    from omega_loop.window.metrics import build_window_metrics

    return build_window_metrics(
        tuple(records if records is not None else cm.seed_records()),
        history=history if history is not None else cm.history(),
    )


# ── Step 1: the populations are distinct, and none of them is "success" ───────


def test_the_computation_version_is_the_modules_own_and_travels_on_every_row():
    from omega_loop.window.metrics import METRICS_COMPUTATION_VERSION, build_window_metrics

    metrics = _metrics()
    assert isinstance(metrics, WindowMetrics)
    assert metrics.computation_version == METRICS_COMPUTATION_VERSION
    assert METRICS_COMPUTATION_VERSION.startswith("omega_loop.window.metrics/")
    for row in metrics.rows:
        assert row["computation_version"] == METRICS_COMPUTATION_VERSION
    assert build_window_metrics.__module__ == "omega_loop.window.metrics"


def test_a_completed_cascade_is_not_a_task_success():
    """All four cascades completed. Only one task succeeded with a passed check."""
    metrics = _metrics()
    cascade = cm.only(metrics, "cascade_completed")
    assert (cascade["numerator"], cascade["denominator"]) == (4, 4)

    task = cm.only(metrics, "task_success_applicable")
    assert (task["numerator"], task["denominator"]) == (2, 3), (
        "the idle episode must not be in an applicable task population"
    )
    verified = cm.only(metrics, "verified_success")
    assert (verified["numerator"], verified["denominator"]) == (1, 2)
    assert cascade["numerator"] != verified["numerator"], (
        "completion was counted as verified success"
    )


def test_the_idle_episode_is_excluded_by_name_and_is_never_a_success():
    metrics = _metrics()
    task = cm.only(metrics, "task_success_applicable")
    assert "ep-0004" not in task["population_refs"]
    assert "ep-0004" not in task["numerator_refs"]
    assert task["excluded"] == 1
    reasons = {row["id"]: row["reason"] for row in task["omissions"]}
    assert reasons["ep-0004"] == "not_applicable"
    for row in metrics.rows:
        if str(row["metric_id"]).startswith(("task_success", "verified_success", "action_succeeded")):
            assert "ep-0004" not in row["numerator_refs"]


def test_the_unverified_claim_never_enters_the_verified_numerator():
    metrics = _metrics()
    verified = cm.only(metrics, "verified_success")
    assert "ep-0003" not in verified["numerator_refs"]
    assert "ep-0003" not in verified["population_refs"], (
        "an unperformed check is not a verified attempt"
    )
    assert {row["id"] for row in verified["omissions"]} == {"ep-0003", "ep-0004"}

    unverified = cm.only(metrics, "unverified_attempts")
    assert list(unverified["numerator_refs"]) == ["ep-0003"]
    assert (unverified["numerator"], unverified["denominator"]) == (1, 3)


def test_the_verified_fraction_case_from_the_order_yields_populations_not_six_successes():
    """C01: three passed checks among four applicable verified attempts, two unverified."""
    records = (
        cm.record("ep-1001", task="success", verification="passed", commit_seq=1),
        cm.record("ep-1002", task="success", verification="passed", commit_seq=2),
        cm.record("ep-1003", task="success", verification="passed", commit_seq=3),
        cm.record("ep-1004", task="failure", action="failed", verification="failed", commit_seq=4),
        cm.record("ep-1005", task="success", verification="not_performed", commit_seq=5),
        cm.record("ep-1006", task="success", verification="not_performed", commit_seq=6),
    )
    metrics = _metrics(records)
    verified = cm.only(metrics, "verified_success")
    assert (verified["numerator"], verified["denominator"]) == (3, 4)
    assert verified["excluded"] == 2
    assert sorted(verified["population_refs"]) == ["ep-1001", "ep-1002", "ep-1003", "ep-1004"]
    unverified = cm.only(metrics, "unverified_attempts")
    assert (unverified["numerator"], unverified["denominator"]) == (2, 6)
    for row in metrics.rows:
        if str(row["metric_id"]).startswith(("verified_success", "task_success", "unverified")):
            assert row["numerator"] != 6, (
                f"{row['metric_id']}: six claimed successes were reported as one population"
            )


def test_action_outcomes_are_their_own_population_with_the_unattempted_excluded():
    metrics = _metrics()
    action = cm.only(metrics, "action_succeeded")
    assert (action["numerator"], action["denominator"]) == (2, 3)
    assert action["excluded"] == 1
    assert {row["reason"] for row in action["omissions"]} == {"not_attempted"}


def test_a_population_with_no_applicable_trial_is_undefined_and_still_reports_its_counts():
    """C02: no applicable trials yields an unknown rate and denominator 0 — never 0, 0.5 or 100%."""
    records = (
        cm.record("ep-2001", action="not_attempted", task="not_applicable",
                  verification="not_applicable", commit_seq=1),
        cm.record("ep-2002", action="not_attempted", task="not_applicable",
                  verification="not_applicable", commit_seq=2),
    )
    metrics = _metrics(records)
    task = cm.only(metrics, "task_success_applicable")
    assert task["observation"] == "undefined"
    assert (task["numerator"], task["denominator"]) == (0, 0)
    assert task["excluded"] == 2
    assert "not_applicable" in task["reason"]
    assert list(task["population_refs"]) == []
    for row in metrics.rows:
        assert row["numerator"] <= row["denominator"] or row["observation"] == "undefined"


def test_an_undefined_observation_is_the_only_way_a_row_may_carry_a_zero_denominator():
    """WO-02's refusal stands for every measured row; an undefined one declares itself."""
    base = {
        "metric_id": "verified_success@s1",
        "numerator": 0,
        "denominator": 0,
        "population_refs": [],
        "excluded": 0,
        "unknown": 2,
        "stratification": {},
        "computation_version": "metrics@1",
    }
    with pytest.raises(ContinuityContractError):
        WindowMetrics.from_dict({"computation_version": "metrics@1", "rows": [dict(base)]})
    with pytest.raises(ContinuityContractError, match="reason"):
        WindowMetrics.from_dict(
            {"computation_version": "metrics@1",
             "rows": [dict(base, observation="undefined")]}
        )
    accepted = WindowMetrics.from_dict(
        {"computation_version": "metrics@1",
         "rows": [dict(base, observation="undefined", reason="no applicable trial")]}
    )
    assert accepted.rows[0]["denominator"] == 0
    with pytest.raises(ContinuityContractError, match="undefined"):
        WindowMetrics.from_dict(
            {"computation_version": "metrics@1",
             "rows": [dict(base, observation="undefined", reason="x", numerator=1,
                           denominator=2, population_refs=["ep-1"], numerator_refs=["ep-1"])]}
        )


def test_a_numerator_reference_outside_its_own_population_is_refused():
    row = {
        "metric_id": "verified_success@s1",
        "numerator": 1,
        "denominator": 1,
        "population_refs": ["ep-0002"],
        "numerator_refs": ["ep-9999"],
        "excluded": 0,
        "unknown": 0,
        "stratification": {},
        "computation_version": "metrics@1",
    }
    with pytest.raises(ContinuityContractError, match="population"):
        WindowMetrics.from_dict({"computation_version": "metrics@1", "rows": [row]})


def test_capture_failures_are_counted_from_the_records_own_coverage():
    records = (
        cm.record("ep-3001", commit_seq=1),
        cm.record("ep-3002", commit_seq=2, unavailable=["evt-lost"]),
        cm.record("ep-3003", commit_seq=3, inaccessible=["the tool log"]),
    )
    metrics = _metrics(records)
    capture = cm.only(metrics, "capture_failures")
    assert (capture["numerator"], capture["denominator"]) == (2, 3)
    assert sorted(capture["numerator_refs"]) == ["ep-3002", "ep-3003"]
    assert capture["numerator"] != 0, "an unretained payload is a capture failure, not a success"


def test_publication_status_is_undefined_here_and_says_where_it_lives():
    """`memory_commit_status` is not a field of the sealed record (WO-02 decision 5), and this
    computation receives neither the manifest nor the commit view."""
    metrics = _metrics()
    publication = cm.only(metrics, "publication_failures")
    assert publication["observation"] == "undefined"
    assert publication["denominator"] == 0
    assert publication["unknown"] == 4
    assert "memory_commit_status" in publication["reason"]
    assert "manifest" in publication["reason"] or "commit view" in publication["reason"]


def test_every_population_reference_resolves_to_packet_eligible_evidence():
    """Step 6: a metric that cites an id the packet cannot show is not evidence."""
    records = cm.seed_records()
    history = cm.history(
        primary_support=(
            {
                "unit_id": "9.01.ep-0900",
                "support_class": "historical",
                "retrieval_class": "comparable",
                "ref": {"record_id": "9.01.ep-0900", "record_version": 1,
                        "content_sha256": "0" * 64},
                "episode_id": "ep-0900",
                "as_of": {"commit_seq": 90},
                "task_outcome": "failure",
                "verification_status": "failed",
                "cascade_status": "completed",
                "summary": "an earlier comparable attempt failed",
                "reasons": ["thread"],
                "evidence_refs": ["9.01.ep-0900"],
            },
        ),
    )
    metrics = _metrics(records, history)
    resolvable = {record.episode_id for record in records}
    for record in records:
        resolvable |= {event.event_id for event in record.events["by_value"]}
        resolvable |= {ref.record_id for ref in record.events["refs"]}
    resolvable |= {str(row["unit_id"]) for row in history.primary_support}
    for row in metrics.rows:
        for ref in list(row["population_refs"]) + list(row["numerator_refs"]):
            assert str(ref) in resolvable, f"{row['metric_id']} cites unresolvable {ref!r}"


# ── Step 4: strata, and no universal rate ─────────────────────────────────────


def test_one_comparable_population_is_one_stratum_that_names_itself():
    metrics = _metrics()
    task = cm.only(metrics, "task_success_applicable")
    assert str(task["metric_id"]).endswith("@s1")
    strat = task["stratification"]
    assert strat["criteria_revision"] == "acceptance@1"
    assert strat["lane"] == "agent"
    assert strat["work_kind"] == "operator_turn"
    assert list(strat["producer_revisions"]) == [cm.PRODUCER_A]
    assert strat["stratum_id"] == "s1"


def test_a_criteria_revision_change_splits_the_strata_and_refuses_a_pooled_rate():
    """C06: version changes produce separate strata or an explicit incomparable status."""
    records = (
        cm.record("ep-4001", criteria="acceptance@1", verification="passed", commit_seq=1),
        cm.record("ep-4002", criteria="acceptance@2", verification="failed", task="failure",
                  action="failed", commit_seq=2),
    )
    metrics = _metrics(records)
    rows = [row for row in metrics.rows
            if str(row["metric_id"]).split("@")[0] == "task_success_applicable"]
    strata = {str(row["metric_id"]) for row in rows}
    assert strata == {"task_success_applicable@s1", "task_success_applicable@s2",
                      "task_success_applicable"}
    pooled = next(row for row in rows if str(row["metric_id"]) == "task_success_applicable")
    assert pooled["observation"] == "undefined"
    assert "incomparable" in pooled["reason"]
    assert "2" in pooled["reason"]
    for row in rows:
        if row["observation"] == "measured":
            assert row["denominator"] == 1, "two revisions were pooled into one rate"
    revisions = {row["stratification"].get("criteria_revision") for row in rows
                 if row["observation"] == "measured"}
    assert revisions == {"acceptance@1", "acceptance@2"}


def test_a_producer_revision_change_also_splits_the_strata():
    records = (
        cm.record("ep-4101", producer_version=cm.PRODUCER_A, commit_seq=1),
        cm.record("ep-4102", producer_version=cm.PRODUCER_B, commit_seq=2),
    )
    metrics = _metrics(records)
    measured = [row for row in metrics.rows
                if str(row["metric_id"]).startswith("task_success_applicable@")]
    assert len(measured) == 2
    assert {tuple(row["stratification"]["producer_revisions"]) for row in measured} == {
        (cm.PRODUCER_A,), (cm.PRODUCER_B,)}


def test_a_lane_or_work_kind_change_is_a_different_task_scope():
    records = (
        cm.record("ep-4201", lane="agent", work_kind="operator_turn", commit_seq=1),
        cm.record("ep-4202", lane="browse", work_kind="autonomous", commit_seq=2),
    )
    metrics = _metrics(records)
    scopes = {(row["stratification"]["lane"], row["stratification"]["work_kind"])
              for row in metrics.rows
              if str(row["metric_id"]).startswith("cascade_completed@")}
    assert scopes == {("agent", "operator_turn"), ("browse", "autonomous")}


# ── Step 5: measurements, clocks and what is never inferred ───────────────────


def test_elapsed_is_summed_only_from_recorded_measurements_within_one_boot():
    records = (
        cm.record("ep-5001", operations=[("op-a", 1_000), ("op-b", 2_500)], commit_seq=1),
    )
    metrics = _metrics(records)
    elapsed = cm.only(metrics, "elapsed_measured")
    assert (elapsed["numerator"], elapsed["denominator"]) == (2, 2)
    assert elapsed["measurement"] == {"units": "ns", "total": 3_500, "clock_scope": cm.BOOT_A}


def test_a_missing_elapsed_observation_is_unknown_and_never_imputed():
    records = (
        cm.record("ep-5101", operations=[("op-a", 1_000), ("op-b", None)], commit_seq=1),
    )
    metrics = _metrics(records)
    elapsed = cm.only(metrics, "elapsed_measured")
    assert (elapsed["numerator"], elapsed["denominator"], elapsed["unknown"]) == (1, 2, 1)
    assert elapsed["measurement"]["total"] == 1_000
    assert "1" in elapsed["note"]


def test_measurements_from_two_boots_are_undefined_rather_than_subtracted():
    """C07: cross-boot elapsed values cannot yield an invented duration."""
    records = (
        cm.record("ep-5201", boot=cm.BOOT_A, operations=[("op-a", 1_000)], commit_seq=1),
        cm.record("ep-5202", boot=cm.BOOT_B, operations=[("op-b", 2_000)], commit_seq=2),
    )
    metrics = _metrics(records)
    elapsed = cm.only(metrics, "elapsed_measured")
    assert elapsed["observation"] == "undefined"
    assert elapsed["denominator"] == 0
    assert "measurement" not in elapsed
    assert cm.BOOT_A in elapsed["reason"] and cm.BOOT_B in elapsed["reason"]
    assert "3000" not in str(dict(elapsed)), "two boots' elapsed values were added"


def test_no_duration_is_derived_by_subtracting_timestamps():
    records = (cm.record("ep-5301", operations=[("op-a", None)], commit_seq=1),)
    metrics = _metrics(records)
    elapsed = cm.only(metrics, "elapsed_measured")
    assert elapsed["observation"] == "undefined"
    assert "measurement" not in elapsed
    assert not any("wall" in str(row["metric_id"]) or "duration" in str(row["metric_id"])
                   for row in metrics.rows)


def test_a_models_self_reported_confidence_never_becomes_a_number():
    records = (
        cm.record("ep-5401", model_assessment={"summary": "I am confident", "confidence": 0.93},
                  commit_seq=1),
    )
    metrics = _metrics(records)
    serialized = json.dumps(metrics.to_dict())
    assert "confidence" not in serialized
    assert "0.93" not in serialized


def test_co_occurrence_is_a_count_that_says_it_is_not_causation():
    records = (
        cm.record("ep-5501", commit_seq=1),
        cm.record("ep-5502", commit_seq=2),
    )
    history = cm.history(
        links=(
            {"kind": "concurrent_commit", "source": "episode:ep-5501", "target": "episode:ep-5502",
             "evidence": ["ep-5501"], "author": "host", "detail": {"complete": True}},
        ),
    )
    metrics = _metrics(records, history)
    row = cm.only(metrics, "concurrent_commit_cooccurrences")
    assert (row["numerator"], row["denominator"]) == (2, 2)
    assert sorted(row["numerator_refs"]) == ["ep-5501", "ep-5502"]
    assert "causation" in row["note"]
    assert not any("caused" in str(r["metric_id"]) for r in metrics.rows)


def test_history_comparables_carry_the_counterexample_search_status():
    history = cm.history(
        primary_support=(
            {"unit_id": "9.01.ep-0900", "support_class": "historical",
             "retrieval_class": "comparable",
             "ref": {"record_id": "9.01.ep-0900", "record_version": 1, "content_sha256": "0" * 64},
             "episode_id": "ep-0900", "as_of": {"commit_seq": 90}, "task_outcome": "failure",
             "verification_status": "failed", "cascade_status": "completed",
             "summary": "s", "reasons": ["thread"], "evidence_refs": ["9.01.ep-0900"]},
        ),
    )
    metrics = _metrics(None, history)
    row = cm.only(metrics, "history_comparable_cases")
    assert row["numerator"] == 1
    assert "search_not_run" in row["note"]


# ── Step 6: determinism, and the golden fixture ───────────────────────────────


def test_the_rows_are_identical_under_any_input_order():
    records = cm.seed_records()
    forward = _metrics(records)
    backward = _metrics(tuple(reversed(records)))
    rotated = _metrics(records[2:] + records[:2])
    assert forward.to_dict() == backward.to_dict() == rotated.to_dict()


def test_the_recorded_event_sequence_is_preserved_inside_a_record():
    """Permutation stability is about the INPUT ORDER of records, never about reordering the
    events a record recorded."""
    records = (
        cm.record("ep-6001", operations=[("op-c", 3), ("op-a", 1), ("op-b", 2)], commit_seq=1),
    )
    metrics = _metrics(records)
    attempts = cm.only(metrics, "attempts")
    assert list(attempts["population_refs"]) == [
        "evt-ep-6001-0", "evt-ep-6001-1", "evt-ep-6001-2"]
    assert list(attempts["numerator_refs"]) == list(attempts["population_refs"]), (
        "the attempt population was sorted out of its recorded sequence"
    )


def test_the_inputs_are_not_mutated():
    records = cm.seed_records()
    before = [record.to_dict() for record in records]
    history = cm.history()
    before_history = history.to_dict()
    _metrics(records, history)
    assert [record.to_dict() for record in records] == before
    assert history.to_dict() == before_history


def test_the_seed_metrics_match_the_committed_golden_fixture():
    metrics = _metrics()
    path = GOLDEN / "seed_window_metrics.json"
    assert path.exists(), (
        f"the golden fixture {path} is missing; regenerate it with "
        "tests.helpers.continuity_metrics.write_metric_golden"
    )
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["fixture_kind"] == "window_metrics"
    assert canonical_bytes(document["body"]) == canonical_bytes(metrics.to_dict())
    assert WindowMetrics.from_dict(document["body"]).rows == metrics.rows


def test_the_golden_fixture_is_reloadable_through_the_production_contract():
    path = GOLDEN / "seed_window_metrics.json"
    body = json.loads(path.read_text(encoding="utf-8"))["body"]
    reloaded = WindowMetrics.from_dict(body)
    assert reloaded.metric_ids == _metrics().metric_ids
    assert all(row["computation_version"] == reloaded.computation_version for row in reloaded.rows)


# ── what this order forbids ──────────────────────────────────────────────────


def test_no_row_pools_every_member_into_a_universal_success_rate():
    records = (
        cm.record("ep-7001", criteria="acceptance@1", commit_seq=1),
        cm.record("ep-7002", criteria="acceptance@2", commit_seq=2),
        cm.record("ep-7003", criteria="acceptance@3", commit_seq=3),
    )
    metrics = _metrics(records)
    rates = ("cascade_completed", "action_succeeded", "task_success_applicable",
             "verified_success", "unverified_attempts")
    for row in metrics.rows:
        base = str(row["metric_id"]).split("@")[0]
        if base in rates and row["observation"] == "measured":
            assert "@" in str(row["metric_id"]), f"{base} was pooled across strata"
            assert row["denominator"] < len(records)
        if str(row["metric_id"]) in ("success_rate", "overall_success_rate"):
            raise AssertionError("a universal success rate was emitted")


def test_an_unknown_axis_value_is_unknown_and_not_a_failure():
    records = (
        cm.record("ep-8001", cascade="unknown", action="unknown", task="unknown",
                  verification="unknown", commit_seq=1),
    )
    metrics = _metrics(records)
    for base in ("cascade_completed", "task_success_applicable", "action_succeeded"):
        row = cm.only(metrics, base)
        assert row["observation"] == "undefined"
        assert row["unknown"] == 1
        assert "unknown" in row["reason"]
        assert row["numerator"] == 0
    assert not any("fail" in str(row["metric_id"]) and row["numerator"] > 0
                   for row in metrics.rows)


def test_history_is_required_and_its_type_is_checked():
    from omega_loop.window.metrics import build_window_metrics

    with pytest.raises(ContinuityContractError, match="HistoryBundle"):
        build_window_metrics(cm.seed_records(), history={"primary_support": []})
    with pytest.raises(ContinuityContractError, match="EpisodeRecord"):
        build_window_metrics(({"episode_id": "ep-0001"},), history=cm.history())


def test_an_empty_cohort_is_undefined_everywhere_and_invents_nothing():
    metrics = _metrics(())
    assert metrics.rows, "an empty cohort still reports what it could not measure"
    for row in metrics.rows:
        assert row["observation"] == "undefined"
        assert (row["numerator"], row["denominator"]) == (0, 0)
        assert row["reason"]


def test_a_historical_record_with_no_thread_identity_is_never_dropped_silently():
    records = (
        cm.record("ep-9001", commit_seq=1),
        cm.record("ep-9002", thread=Observation.missing("not_captured", reason="migrated era"),
                  attempt=None, commit_seq=2),
    )
    metrics = _metrics(records)
    cascade = cm.only(metrics, "cascade_completed")
    assert cascade["denominator"] == 2, "a record with no thread identity left the cohort"
    cases = cm.only(metrics, "independent_cases")
    assert cases["unknown"] == 1
