"""WO-11 — attempts are not trials, and a unique episode id is not a case (Step 2).

Three retries of one job are three attempts and ONE case. The inflation this prevents is the
window reading "three independent confirmations" off three rows that are one piece of work,
and the fabrication it prevents is counting cases by episode UUID — which is always unique and
therefore always yields "n independent cases", a number with no content.

A case identity is only what the host recorded: a durable job id, a continuation id, or a
thread id. Nothing is inferred from prose, and a record that carries none of them leaves
independence **unknown**.
"""

from __future__ import annotations

from schema.continuity_json import Observation
from tests.helpers import continuity_metrics as cm


def _metrics(records, history=None):
    from omega_loop.window.metrics import build_window_metrics

    return build_window_metrics(tuple(records), history=history or cm.history())


# ── attempts: distinct real operation tokens ──────────────────────────────────


def test_attempts_are_distinct_recorded_operation_tokens():
    records = (
        cm.record("ep-0101", operations=[("op-a", 10), ("op-b", 20)], commit_seq=1),
        cm.record("ep-0102", operations=[("op-c", 30)], commit_seq=2),
    )
    metrics = _metrics(records)
    attempts = cm.only(metrics, "attempts")
    assert attempts["numerator"] == 3
    assert attempts["denominator"] == 3, "the population is the events that were examined"
    assert attempts["unknown"] == 0


def test_the_same_operation_token_replayed_is_one_attempt():
    """A retried phase that reuses its operation id is one attempt, not two."""
    records = (
        cm.record("ep-0201", operations=[("op-same", 10), ("op-same", 11), ("op-other", 12)],
                  commit_seq=1),
    )
    metrics = _metrics(records)
    attempts = cm.only(metrics, "attempts")
    assert attempts["numerator"] == 2
    assert attempts["denominator"] == 3
    assert len(attempts["numerator_refs"]) == 2
    assert {row["reason"] for row in attempts["omissions"]} == {"duplicate_operation_token"}


# ── independent cases: host identity only ─────────────────────────────────────


def test_three_retries_of_one_job_are_three_attempts_and_one_case():
    """C03: attempts=3, independent_cases=1."""
    records = (
        cm.record("ep-0301", job="job-0001", operations=[("op-1", 10)], commit_seq=1),
        cm.record("ep-0302", job="job-0001", operations=[("op-2", 10)], task="failure",
                  action="failed", verification="failed", commit_seq=2),
        cm.record("ep-0303", job="job-0001", operations=[("op-3", 10)], commit_seq=3),
    )
    metrics = _metrics(records)
    assert cm.only(metrics, "attempts")["numerator"] == 3
    cases = cm.only(metrics, "independent_cases")
    assert cases["numerator"] == 1
    assert cases["denominator"] == 3, "the population is the members examined"
    assert cases["unknown"] == 0
    assert len(cases["numerator_refs"]) == 1
    assert list(cases["stratification"]["case_keys"]) == ["job:job-0001"]


def test_same_thread_retries_are_one_case_even_without_a_job():
    records = (
        cm.record("ep-0401", thread="thread-x", commit_seq=1),
        cm.record("ep-0402", thread="thread-x", commit_seq=2),
    )
    cases = cm.only(_metrics(records), "independent_cases")
    assert cases["numerator"] == 1
    assert list(cases["stratification"]["case_keys"]) == ["thread:thread-x"]


def test_a_continuation_chain_is_one_case():
    records = (
        cm.record("ep-0501", thread="thread-a", continuation="root-1", commit_seq=1),
        cm.record("ep-0502", thread="thread-b", continuation="root-1", commit_seq=2),
    )
    cases = cm.only(_metrics(records), "independent_cases")
    assert cases["numerator"] == 1, "a continued chain is the same root work"


def test_two_separate_threads_are_two_cases():
    records = (
        cm.record("ep-0601", thread="thread-a", commit_seq=1),
        cm.record("ep-0602", thread="thread-b", commit_seq=2),
    )
    cases = cm.only(_metrics(records), "independent_cases")
    assert cases["numerator"] == 2
    assert cases["denominator"] == 2
    assert list(cases["stratification"]["case_keys"]) == ["thread:thread-a",
                                                          "thread:thread-b"]


def test_identities_that_overlap_are_merged_rather_than_counted_twice():
    """A member sharing a thread with one group and a job with another is ONE case with both:
    the conservative direction, because independence is the claim that needs evidence."""
    records = (
        cm.record("ep-0701", thread="thread-a", job="job-1", commit_seq=1),
        cm.record("ep-0702", thread="thread-a", commit_seq=2),
        cm.record("ep-0703", thread="thread-b", job="job-1", commit_seq=3),
    )
    cases = cm.only(_metrics(records), "independent_cases")
    assert cases["numerator"] == 1, "overlapping identities were counted as separate cases"
    assert list(cases["stratification"]["case_keys"]) == ["job:job-1"], (
        "a case is named by the strongest identity its members recorded"
    )
    assert {key: list(value) for key, value
            in cases["stratification"]["case_members"].items()} == {
        "job:job-1": ["ep-0701", "ep-0702", "ep-0703"]}


def test_unknown_case_identity_stays_unknown_and_is_never_the_episode_id():
    """C04: no automatic independence count based on unique episode UUIDs."""
    absent = Observation.missing("not_captured", reason="the migrated era recorded no thread")
    records = (
        cm.record("ep-0801", thread=absent, attempt=None, commit_seq=1),
        cm.record("ep-0802", thread=absent, attempt=None, commit_seq=2),
    )
    metrics = _metrics(records)
    cases = cm.only(metrics, "independent_cases")
    assert cases["observation"] == "undefined"
    assert (cases["numerator"], cases["denominator"]) == (0, 0)
    assert cases["unknown"] == 2
    assert "identity" in cases["reason"]
    assert "ep-0801" not in cases["numerator_refs"]
    reasons = {row["id"]: row["reason"] for row in cases["omissions"]}
    assert reasons == {"ep-0801": "no_recorded_case_identity",
                       "ep-0802": "no_recorded_case_identity"}


def test_a_mixed_cohort_counts_the_known_cases_and_reports_the_unknown_ones():
    absent = Observation.missing("not_captured", reason="the migrated era recorded no thread")
    records = (
        cm.record("ep-0901", thread="thread-a", commit_seq=1),
        cm.record("ep-0902", thread="thread-b", commit_seq=2),
        cm.record("ep-0903", thread=absent, attempt=None, commit_seq=3),
    )
    cases = cm.only(_metrics(records), "independent_cases")
    assert (cases["numerator"], cases["denominator"], cases["unknown"]) == (2, 2, 1)
    assert "ep-0903" not in cases["population_refs"]


def test_independence_is_counted_inside_the_declared_comparison_scope():
    """Two revisions are two strata; a case is never independent ACROSS an incomparable scope."""
    records = (
        cm.record("ep-1001", thread="thread-a", criteria="acceptance@1", commit_seq=1),
        cm.record("ep-1002", thread="thread-a", criteria="acceptance@2", commit_seq=2),
    )
    metrics = _metrics(records)
    rows = [row for row in metrics.rows
            if str(row["metric_id"]).split("@")[0] == "independent_cases"]
    measured = [row for row in rows if row["observation"] == "measured"]
    assert len(measured) == 2
    for row in measured:
        assert row["numerator"] == 1 and row["denominator"] == 1
    pooled = [row for row in rows if row["observation"] == "undefined"]
    assert pooled and "incomparable" in pooled[0]["reason"]


def test_an_attempt_id_is_not_a_case_identity():
    """attempt_id distinguishes retries; treating it as a case would inflate independence."""
    records = (
        cm.record("ep-1101", thread="thread-a", attempt="attempt-1", commit_seq=1),
        cm.record("ep-1102", thread="thread-a", attempt="attempt-2", commit_seq=2),
    )
    cases = cm.only(_metrics(records), "independent_cases")
    assert cases["numerator"] == 1
    assert all("attempt" not in key for key in cases["stratification"]["case_keys"])


def test_an_event_whose_bytes_were_not_retained_is_unknown_not_an_attempt():
    """`operation_id` is required on every event (`event.operation_id` must be nonempty), so a
    retained event always has its token. The unknown column is what was REFERENCED and not
    retained: its operation may or may not be one of the tokens already counted."""
    records = (
        cm.record("ep-1201", operations=[("op-a", 10)], unavailable=["evt-ep-1201-lost"],
                  commit_seq=1),
    )
    attempts = cm.only(_metrics(records), "attempts")
    assert attempts["numerator"] == 1
    assert attempts["denominator"] == 2, "a referenced event is part of the population"
    assert attempts["unknown"] == 1
    assert "evt-ep-1201-lost" not in attempts["numerator_refs"]
    reasons = {row["id"]: row["reason"] for row in attempts["omissions"]}
    assert reasons == {"evt-ep-1201-lost": "event_bytes_not_retained"}
