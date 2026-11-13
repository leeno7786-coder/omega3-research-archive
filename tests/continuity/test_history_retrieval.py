"""WO-10 — history retrieval is stable, audited, counterexample-aware and deduplicated.

Step 4: explicit related ids first, scoped comparable cases next, relevant counterexamples next,
deterministic canonical-id tie-breaking inside each class, a pinned policy revision, and an
audit of the search scope, the considered / returned / admitted records and every omission.
Step 5: ``search_not_run``, ``searched_no_matches``, ``searched_found_matches`` and
``reader_failed`` are four different results; duplicated reflections referencing one primary
event add no new evidence; historical supports never increase focal membership.

Acceptance cases covered here: **Counterexamples**, **Deduplication**, **Deterministic
retrieval** — plus the cap, the governed limits and the input contract.
"""

from __future__ import annotations

import pytest

from pressure_loop.episode import EpisodeStore
from schema.continuity_json import (
    ContinuityContractError,
    WindowMembershipError,
    WindowReferenceError,
    content_hash,
)
from tests.helpers import continuity_history as ch
from tests.helpers import continuity_terminus as ct
from tests.helpers import continuity_window as cw


@pytest.fixture(autouse=True)
def _terminus(tmp_path, monkeypatch):
    from memory import episode_sram

    monkeypatch.setenv("OMEGA_WINDOW_LANE", "1")
    ct.isolate(monkeypatch, tmp_path)
    yield
    episode_sram.reset()


def _history_store(tmp_path):
    return EpisodeStore(reflection_window_path=tmp_path / "history_window.json")


def _close_kind(store, kind: str):
    """One real episode with a real sealed outcome: success, failure or idle."""
    if kind == "failure":
        return ch.close_with(store, lambda c: None, kind="failure")
    carrier = ch.open_carrier(store)
    ct.dress_learn(carrier)
    if kind == "idle":
        carrier.meta["reflection"]["execution"].update(
            executed=False, result="none", status="idle", disposition="idle")
    ct.close(store)
    return carrier


def _cohort(store, kinds):
    return [_close_kind(store, kind) for kind in kinds]


# ── Deterministic retrieval ──────────────────────────────────────────────────


def test_reordered_storage_enumeration_selects_the_same_records_and_digest(tmp_path, monkeypatch):
    history_store = _history_store(tmp_path)
    _cohort(history_store, ["success", "failure", "success", "failure", "success"])
    store = EpisodeStore()
    _cohort(store, ["success", "failure"] * 5)
    _window, manifest, records = ch.seal_staged()

    h = ch.history()
    first = ch.collect(manifest, records)
    real_rows = h._snapshot_rows
    monkeypatch.setattr(h, "_snapshot_rows", lambda graph: list(reversed(real_rows(graph))))
    second = ch.collect(manifest, records)

    assert list(second.retrieval_audit["admitted_ids"]) == list(first.retrieval_audit["admitted_ids"])
    assert content_hash(second.to_dict()) == content_hash(first.to_dict())
    admitted = list(first.retrieval_audit["admitted_ids"])
    assert admitted, "the history is empty, so the ordering proves nothing"


def test_the_history_episode_that_produced_tested_bytes_is_explicit_history(tmp_path):
    """A member's check of bytes another episode produced relates the two by host evidence."""
    from memory.auto_evidence import capture_artifact_revision, capture_verification

    sha = "e" * 64
    history_store = _history_store(tmp_path)
    producer = ch.close_with(history_store, lambda c: capture_artifact_revision(
        c, path="lexer.py", revision=1, content_sha256=sha, change_kind="created"))
    store = EpisodeStore()
    ch.close_with(store, lambda c: capture_verification(
        c, criterion="pytest passes", path="lexer.py", revision=1, content_sha256=sha,
        passed=True))
    ch.close_members(store, 9)
    _window, manifest, records = ch.seal_staged()

    rows = {row["episode_id"]: row for row in ch.collect(manifest, records).primary_support}
    assert rows[producer.episode_id]["retrieval_class"] == "explicit"
    assert "relation:tests" in rows[producer.episode_id]["reasons"]


# ── Counterexamples: four different results ─────────────────────────────────


def test_a_comparable_record_with_the_opposite_outcome_is_a_counterexample(tmp_path):
    history_store = _history_store(tmp_path)
    counter = _cohort(history_store, ["success", "success"])
    store = EpisodeStore()
    _cohort(store, ["failure"] * 10)
    _window, manifest, records = ch.seal_staged()

    bundle = ch.collect(manifest, records)
    assert bundle.retrieval_audit["counterexample_search_status"] == "searched_found_matches"
    found = {row["episode_id"] for row in bundle.primary_support
             if row["retrieval_class"] == "counterexample"}
    assert found == {c.episode_id for c in counter}


def test_comparable_records_that_agree_are_searched_with_no_matches(tmp_path):
    history_store = _history_store(tmp_path)
    _cohort(history_store, ["success", "success"])
    store = EpisodeStore()
    _cohort(store, ["success"] * 10)
    _window, manifest, records = ch.seal_staged()

    bundle = ch.collect(manifest, records)
    assert bundle.retrieval_audit["counterexample_search_status"] == "searched_no_matches"
    assert not [row for row in bundle.primary_support
                if row["retrieval_class"] == "counterexample"]
    assert [row for row in bundle.primary_support if row["retrieval_class"] == "comparable"]


def test_a_cohort_with_no_outcome_to_counter_never_runs_the_search(tmp_path):
    history_store = _history_store(tmp_path)
    _cohort(history_store, ["success", "failure"])
    store = EpisodeStore()
    _cohort(store, ["idle"] * 10)
    _window, manifest, records = ch.seal_staged()

    bundle = ch.collect(manifest, records)
    assert bundle.retrieval_audit["counterexample_search_status"] == "search_not_run"


def test_an_unreadable_history_record_is_reader_failed_and_omitted_with_its_reason(
    tmp_path, monkeypatch
):

    history_store = _history_store(tmp_path)
    broken = _cohort(history_store, ["success"])[0]
    store = EpisodeStore()
    _cohort(store, ["failure"] * 10)
    _window, manifest, records = ch.seal_staged()

    # Program 0 generations are immutable. Inject corrupt reader bytes at the graph-row
    # boundary instead of rewriting the retired fixed-name legacy graph on disk.
    history = ch.history()
    read_rows = history._snapshot_rows

    def unreadable_row(graph):
        rows = read_rows(graph)
        event_id = f"episode-{broken.episode_id}"
        return [
            (*row[:3], '{"episode_record": {"schema_version": "broken"}}', *row[4:])
            if row[0] == event_id else row
            for row in rows
        ]

    monkeypatch.setattr(history, "_snapshot_rows", unreadable_row)

    bundle = ch.collect(manifest, records)
    assert bundle.retrieval_audit["counterexample_search_status"] == "reader_failed"
    omitted = {row["id"]: row["reason"] for row in bundle.retrieval_audit["omitted"]}
    assert any(broken.episode_id in key and "unreadable" in reason
               for key, reason in omitted.items()), omitted


# ── Deduplication ────────────────────────────────────────────────────────────


def test_three_reflections_citing_one_cohort_contribute_one_support_unit(monkeypatch):
    import pressure_loop.reflection_window as reflection_window
    import pressure_loop.window_lane as window_lane

    store = EpisodeStore()
    reflected = ch.close_members(store, 10)
    reflections = []
    for attempt in range(3):
        episode = ct.dress_learn(cw.open_window_episode(store, pipeline_id=3))
        window = window_lane.restore_window(episode)
        if attempt < 2:  # the substrate is unwritable: the reflection is not committed
            with monkeypatch.context() as m:
                m.setattr(reflection_window, "archive_reflection_window", lambda *a, **k: None)
                m.setattr(reflection_window, "archive_failed_reflection_window",
                          lambda *a, **k: None)
                assert not window_lane.commit_reflection(episode, window, {"status": "reflected"})
        else:
            assert window_lane.commit_reflection(episode, window, {"status": "reflected"})
        ct.close(store)
        reflections.append(episode)

    # Each reflection's own record names exactly what it admitted.
    admitted_events = [
        [e for e in record.events["by_value"] if e.event_kind == "window.manifest_admitted"]
        for record in _records_of(reflections)
    ]
    assert all(len(rows) == 1 for rows in admitted_events), admitted_events
    assert {tuple(rows[0].payload["member_ids"]) for rows in admitted_events} == {
        tuple(c.episode_id for c in reflected)
    }

    ch.close_members(store, 10)
    _window, manifest, records = ch.seal_staged()
    bundle = ch.collect(manifest, records)

    findings = list(bundle.prior_findings)
    assert len(findings) == 1, "three reflections over one cohort became three findings"
    assert sorted(findings[0]["reflection_episode_ids"]) == sorted(r.episode_id for r in reflections)
    units = [row["unit_id"] for row in bundle.primary_support]
    assert len(units) == len(set(units)), "a primary unit appears twice"
    reflection_ids = {r.episode_id for r in reflections}
    assert not reflection_ids & {row["episode_id"] for row in bundle.primary_support}, (
        "a reflection was counted as primary evidence"
    )
    assert manifest.member_count == 10


def _records_of(carriers):
    from memory.episode_commit_view import canonical_episode_body, episode_record_ref
    from schema.episode_record import EpisodeRecord

    out = []
    for carrier in carriers:
        body, why = canonical_episode_body(episode_record_ref(carrier.episode_id).record_id)
        assert body is not None, why
        out.append(EpisodeRecord.from_dict(body["episode_record"]))
    return out


# ── the cap, the audit, the limits and the inputs ───────────────────────────


def test_the_cap_admits_in_class_then_id_order_and_audits_every_omission(tmp_path):
    history_store = _history_store(tmp_path)
    _cohort(history_store, ["success"] * 6)
    store = EpisodeStore()
    _cohort(store, ["success"] * 10)
    _window, manifest, records = ch.seal_staged()

    bundle = ch.collect(manifest, records, record_cap=3)
    audit = bundle.retrieval_audit
    assert audit["cap"] == 3
    assert len(audit["admitted_ids"]) == 3
    assert set(audit["admitted_ids"]) <= set(audit["returned_ids"]) <= set(audit["considered_ids"])
    capped = [row["id"] for row in audit["omitted"] if row["reason"] == "cap"]
    assert sorted(capped) == sorted(set(audit["returned_ids"]) - set(audit["admitted_ids"]))
    assert list(audit["admitted_ids"]) == sorted(audit["admitted_ids"]), (
        "one class, so admission follows canonical id order"
    )
    members = {row["ref"].record_id for row in manifest.members}
    assert not members & set(audit["considered_ids"]), "a focal member was treated as history"
    assert audit["query_policy_revision"] == ch.history().HISTORY_POLICY_REVISION


def test_the_limits_come_from_governed_configuration_and_pin_the_policy(monkeypatch):
    import config.loader as loader

    h = ch.history()
    configured = h.history_limits()
    assert configured.record_cap > 0 and configured.token_candidate_cap > 0
    assert configured.retrieval_policy_revision == h.HISTORY_POLICY_REVISION

    injected = OSError("injected: reflection_window.yaml is unreadable")

    def unreadable(_name):
        raise injected

    monkeypatch.setattr(loader, "load_subsystem_bundle", unreadable)
    with pytest.raises(RuntimeError) as caught:
        h.history_limits()
    assert caught.value.__cause__ is injected


def test_a_foreign_policy_revision_and_mismatched_inputs_are_refused():
    from schema.window_evidence import HistoryLimits

    store = EpisodeStore()
    ch.close_members(store, 10)
    _window, manifest, records = ch.seal_staged()
    h = ch.history()

    with pytest.raises(ContinuityContractError, match="policy"):
        h.collect_history(manifest, records, limits=HistoryLimits(
            record_cap=8, token_candidate_cap=64, retrieval_policy_revision="someone.else/9"))
    with pytest.raises(WindowReferenceError):
        h.collect_history(manifest, tuple(reversed(records)), limits=ch.limits())
    incomplete = type(manifest).from_dict({**manifest.to_dict(), "complete": False})
    with pytest.raises(WindowMembershipError, match="complete"):
        h.collect_history(incomplete, records, limits=ch.limits())
