"""WO-11 — material progress, and the three things that are not failure (Step 3).

The error this prevents is a window reading "three attempts, no success" and concluding the
approach failed, when the evidence says one of three different things:

* **no observed change** — the same criteria and the same artifact revisions came back; nothing
  moved, and that is not a verdict on the work;
* **waiting** — the work was blocked or deferred on a named condition;
* **not observable** — nothing was captured that could show movement either way.

Only recorded changes count as progress: a criterion newly satisfied, a real artifact revision,
a blocker that later cleared, an authorized change of direction, or a verified intermediate
output. Progress is counted per CASE, never per episode, so a retry cannot look like advance.
"""

from __future__ import annotations

from schema.continuity_json import Observation
from tests.helpers import continuity_metrics as cm

PROGRESS = ("progress_observed", "progress_none_observed", "progress_waiting",
            "progress_not_observable")


def _metrics(records, history=None):
    from omega_loop.window.metrics import build_window_metrics

    return build_window_metrics(tuple(records), history=history or cm.history())


def _states(metrics):
    return {base: cm.only(metrics, base) for base in PROGRESS}


# ── the four states partition the cases ───────────────────────────────────────


def test_the_four_progress_states_partition_the_cases_and_sum_to_the_denominator():
    records = (
        cm.record("ep-0101", job="job-a", thread="thread-a", verification="passed",
                  commit_seq=1),
        cm.record("ep-0102", job="job-b", thread="thread-b", task="blocked", action="blocked",
                  verification="not_performed", gap="waiting on an external approval",
                  commit_seq=2),
        cm.record("ep-0103", job="job-c", thread="thread-c", task="unknown",
                  verification="unknown", unrecorded=["the check never reported"], commit_seq=3),
    )
    metrics = _metrics(records)
    states = _states(metrics)
    denominators = {row["denominator"] for row in states.values()}
    assert denominators == {3}, "every progress row is measured over the same cases"
    assert sum(row["numerator"] for row in states.values()) == 3
    for row in states.values():
        assert row["numerator"] <= row["denominator"]


def test_a_newly_satisfied_criterion_is_observed_progress():
    records = (
        cm.record("ep-0201", job="job-a", task="failure", action="failed",
                  verification="failed", commit_seq=1),
        cm.record("ep-0202", job="job-a", task="success", verification="passed", commit_seq=2),
    )
    states = _states(_metrics(records))
    assert states["progress_observed"]["numerator"] == 1
    assert states["progress_none_observed"]["numerator"] == 0
    reasons = states["progress_observed"]["stratification"]["progress_evidence"]
    assert "criterion_newly_satisfied" in reasons["job:job-a"]


def test_a_new_artifact_revision_is_observed_progress():
    records = (
        cm.record("ep-0301", job="job-a", artifact="artifact@1", task="partial",
                  verification="partial", commit_seq=1),
        cm.record("ep-0302", job="job-a", artifact="artifact@2", task="partial",
                  verification="partial", commit_seq=2),
    )
    states = _states(_metrics(records))
    assert states["progress_observed"]["numerator"] == 1
    evidence = states["progress_observed"]["stratification"]["progress_evidence"]["job:job-a"]
    assert "artifact_revised" in evidence
    assert "verified_intermediate_output" in evidence, "a passed partial check is a real output"


def test_a_blocker_that_later_cleared_is_observed_progress():
    records = (
        cm.record("ep-0401", job="job-a", task="blocked", action="blocked",
                  verification="not_performed", gap="the dependency was missing", commit_seq=1),
        cm.record("ep-0402", job="job-a", task="success", verification="passed", commit_seq=2),
    )
    states = _states(_metrics(records))
    assert states["progress_observed"]["numerator"] == 1
    evidence = states["progress_observed"]["stratification"]["progress_evidence"]["job:job-a"]
    assert "blocker_resolved" in evidence


def test_an_authorized_direction_change_is_progress_evidence_from_history():
    records = (cm.record("ep-0501", job="job-a", task="partial", verification="partial",
                         commit_seq=1),)
    history = cm.history(
        intervening_direction_changes=(
            {"support_class": "direction_change", "kind": "trajectory_revised",
             "episode_id": "ep-0501", "from": "trajectory@rev-1", "to": "trajectory@rev-2",
             "evidence_refs": ["ep-0501"], "summary": "the operator authorized a new target"},
        ),
    )
    states = _states(_metrics(records, history))
    evidence = states["progress_observed"]["stratification"]["progress_evidence"]["job:job-a"]
    assert "authorized_scope_change" in evidence


# ── the three things that are not failure ─────────────────────────────────────


def test_three_unchanged_repeats_are_no_observed_progress_and_not_a_failure():
    """C05: unchanged artifacts and criteria are distinguishable from progress and waiting."""
    records = (
        cm.record("ep-0601", job="job-a", task="failure", action="failed", verification="failed",
                  artifact="artifact@1", commit_seq=1),
        cm.record("ep-0602", job="job-a", task="failure", action="failed", verification="failed",
                  artifact="artifact@1", commit_seq=2),
        cm.record("ep-0603", job="job-a", task="failure", action="failed", verification="failed",
                  artifact="artifact@1", commit_seq=3),
    )
    metrics = _metrics(records)
    states = _states(metrics)
    assert states["progress_none_observed"]["numerator"] == 1
    assert states["progress_observed"]["numerator"] == 0
    assert states["progress_waiting"]["numerator"] == 0
    note = states["progress_none_observed"]["note"]
    assert "not a failure" in note
    assert not any("failed_case" in str(row["metric_id"]) for row in metrics.rows)
    assert cm.only(metrics, "attempts")["numerator"] == 3
    assert cm.only(metrics, "independent_cases")["numerator"] == 1


def test_a_blocked_case_with_a_named_condition_is_waiting_not_failure():
    records = (
        cm.record("ep-0701", job="job-a", task="blocked", action="blocked",
                  verification="not_performed", gap="waiting on the operator's approval",
                  commit_seq=1),
    )
    states = _states(_metrics(records))
    assert states["progress_waiting"]["numerator"] == 1
    assert states["progress_none_observed"]["numerator"] == 0
    condition = states["progress_waiting"]["stratification"]["conditions"]["job:job-a"]
    assert "approval" in condition


def test_a_case_with_nothing_captured_is_not_observable_rather_than_unchanged():
    records = (
        cm.record("ep-0801", job="job-a", task="unknown", action="unknown",
                  verification="unknown", unrecorded=["the tool never reported"],
                  unavailable=["evt-ep-0801-lost"], commit_seq=1),
    )
    states = _states(_metrics(records))
    assert states["progress_not_observable"]["numerator"] == 1
    assert states["progress_none_observed"]["numerator"] == 0
    reason = states["progress_not_observable"]["stratification"]["conditions"]["job:job-a"]
    assert "not retained" in reason or "unrecorded" in reason


def test_a_cancelled_case_is_waiting_or_unchanged_but_never_progress():
    records = (
        cm.record("ep-0901", job="job-a", task="cancelled", action="cancelled",
                  verification="not_performed", gap="the operator cancelled the run",
                  commit_seq=1),
    )
    states = _states(_metrics(records))
    assert states["progress_observed"]["numerator"] == 0
    assert states["progress_waiting"]["numerator"] == 1


# ── progress is per case, and unknown identity stays unknown ───────────────────


def test_progress_is_counted_per_case_not_per_episode():
    records = (
        cm.record("ep-1001", job="job-a", thread="thread-a", task="failure", action="failed",
                  verification="failed", commit_seq=1),
        cm.record("ep-1002", job="job-a", thread="thread-a", task="success",
                  verification="passed", commit_seq=2),
        cm.record("ep-1003", job="job-b", thread="thread-b", task="failure", action="failed",
                  verification="failed", commit_seq=3),
    )
    states = _states(_metrics(records))
    assert states["progress_observed"]["denominator"] == 2, "three episodes are two cases"
    assert states["progress_observed"]["numerator"] == 1
    assert states["progress_none_observed"]["numerator"] == 1


def test_a_case_with_no_recorded_identity_is_unknown_progress_not_a_case():
    absent = Observation.missing("not_captured", reason="the migrated era recorded no thread")
    records = (
        cm.record("ep-1101", job="job-a", verification="passed", commit_seq=1),
        cm.record("ep-1102", thread=absent, attempt=None, commit_seq=2),
    )
    states = _states(_metrics(records))
    for base, row in states.items():
        assert row["denominator"] == 1, base
        assert row["unknown"] == 1, base
        assert "ep-1102" not in row["population_refs"], base


def test_progress_rows_cite_a_representative_episode_of_each_case():
    records = (
        cm.record("ep-1201", job="job-a", task="failure", action="failed", verification="failed",
                  commit_seq=1),
        cm.record("ep-1202", job="job-a", task="success", verification="passed", commit_seq=2),
    )
    states = _states(_metrics(records))
    observed = states["progress_observed"]
    assert list(observed["population_refs"]) == ["ep-1201"], (
        "the earliest member by finish line represents its case"
    )
    assert list(observed["numerator_refs"]) == ["ep-1201"]


def test_no_progress_row_is_produced_from_a_model_sentence():
    records = (
        cm.record("ep-1301", job="job-a", task="failure", action="failed", verification="failed",
                  model_assessment={"summary": "we made great progress on the parser"},
                  commit_seq=1),
    )
    states = _states(_metrics(records))
    assert states["progress_observed"]["numerator"] == 0, (
        "a model's claim of progress became a measurement"
    )
    assert states["progress_none_observed"]["numerator"] == 1
