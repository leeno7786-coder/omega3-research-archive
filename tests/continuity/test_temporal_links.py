"""WO-10 — relations between episodes come only from host evidence, never from chronology or text.

Step 2: ``triggered_by``, ``continued_from``, ``read_from``, ``admitted_from``,
``produces``/``revises``, ``tests``/``evaluates`` and ``concurrent_commit`` are built from what the
host recorded. Model-inferred causation is a proposed finding with support, not a factual edge.

Step 1 / acceptance "Concurrency": committing during another episode does not imply reading
from it. Chronology (who crossed the finish line first) and observed information dependency
(what an episode actually admitted as input) are separate relations, and only admitted inputs
establish known exposure — a record that was retrieved and then excluded was never supplied.

Every episode closes through the real DeepSleep terminus; the evidence each relation rests on is
produced by the component that owns it (``tests/helpers/continuity_history.py``).
"""

from __future__ import annotations

import pytest

from memory.episode_record_seal import episode_record_id
from pressure_loop.episode import EpisodeStore
from tests.helpers import continuity_history as ch
from tests.helpers import continuity_terminus as ct

SHA_P = "b" * 64
SHA_X = "c" * 64


@pytest.fixture(autouse=True)
def _terminus(tmp_path, monkeypatch):
    from memory import episode_sram

    monkeypatch.setenv("OMEGA_WINDOW_LANE", "1")
    ct.isolate(monkeypatch, tmp_path)
    yield
    episode_sram.reset()


def _link(bundle, kind, source, target):
    return [row for row in ch.links_of(bundle, kind)
            if row["source"] == source and row["target"] == target]


def _ep(carrier) -> str:
    return f"episode:{carrier.episode_id}"


# ── Concurrency: chronology is not information flow ──────────────────────────


def test_overlap_in_reverse_commit_order_is_concurrency_and_chronology_never_read_from():
    store = EpisodeStore()
    ch.close_members(store, 8)
    a, b = ch.overlapping_pair(store)
    _staged, manifest, records = ch.seal_staged()
    assert manifest.member_ids[-2:] == (b.episode_id, a.episode_id), "B must commit before A"

    bundle = ch.collect(manifest, records)
    assert _link(bundle, "concurrent_commit", _ep(a), _ep(b)), (
        "A's sealed record says B committed while A was thinking; the relation is missing"
    )
    assert _link(bundle, "commit_precedes", _ep(b), _ep(a)), "chronology was not recorded"
    for kind in ("read_from", "admitted_from"):
        for source, target in ((_ep(a), _ep(b)), (_ep(b), _ep(a))):
            assert not _link(bundle, kind, source, target), (
                f"{kind} was inferred from overlap: committing during is not reading from"
            )


def test_only_an_admitted_input_establishes_read_from(monkeypatch):
    """Retrieved A and B, admitted A, excluded B (a duplicate of A): only A was supplied."""
    store = EpisodeStore()
    p = ch.close_learn(store)
    q = ch.close_learn(store)
    ch.close_members(store, 7)
    p_rec, q_rec = episode_record_id(p.episode_id), episode_record_id(q.episode_id)
    note = "the parser acceptance test fails on line 42"
    m = ch.close_with(store, lambda carrier: ch.recall_with_exposure(
        carrier, [ch.lane_result((p_rec, note, 0.95), (q_rec, note, 0.93))], monkeypatch))
    _staged, manifest, records = ch.seal_staged()

    # The producer recorded what it retrieved and what it admitted, in M's own sealed record.
    exposure = [event for event in records[-1].events["by_value"]
                if event.event_kind == "auto.input_exposure"]
    assert len(exposure) == 1, "the recall's exposure was never recorded as an episode event"
    payload = exposure[0].payload
    assert list(payload["admitted"]) == [p_rec]
    assert [row["memory_id"] for row in payload["excluded"]] == [q_rec]
    assert all(row["reason"] for row in payload["excluded"])

    bundle = ch.collect(manifest, records)
    assert _link(bundle, "admitted_from", _ep(m), f"memory:{p_rec}")
    assert _link(bundle, "read_from", _ep(m), _ep(p))
    assert not _link(bundle, "admitted_from", _ep(m), f"memory:{q_rec}")
    assert not _link(bundle, "read_from", _ep(m), _ep(q)), (
        "a retrieved-but-excluded record was treated as supplied"
    )


# ── continuation, trigger, artifacts, feedback ──────────────────────────────


def _successor_carrier(store, *, parent_id: str, successor_id: str):
    from memory import episode_sram
    from memory.epoch import open_episode_epoch

    pressure = {
        "opened_by": "omega_successor",
        "contributors": [{
            "id": "omega_successor", "type": "omega_successor", "source": "omega",
            "detail": {"successor_id": successor_id, "parent_episode_id": parent_id,
                       "thread_id": ct.SENTINEL_THREAD,
                       "continued_from": {"continuation_key": "k-wo10", "fold_id": "f-wo10"}},
            "metadata": {},
        }],
    }
    carrier = store.open(opened_by="omega_successor", pressure=pressure, pipeline_id=0,
                         work_kind="autonomous")
    episode_sram.open_bucket(carrier.episode_id, pipeline_id=0)
    open_episode_epoch(carrier)
    return carrier


def test_a_continuation_link_rests_on_the_hosts_continued_fact():
    import cognition_chain as chain

    store = EpisodeStore()
    parent = ch.close_learn(store)
    successor = _successor_carrier(store, parent_id=parent.episode_id, successor_id="succ-wo10")
    chain._record_continuation_fact(successor)
    ct.dress_learn(successor)
    ct.close(store)
    ch.close_members(store, 8)
    _staged, manifest, records = ch.seal_staged()

    bundle = ch.collect(manifest, records)
    rows = _link(bundle, "continued_from", _ep(successor), _ep(parent))
    assert len(rows) == 1, "the host's continued fact did not become the relation"
    assert rows[0]["evidence_refs"], "the relation does not cite the fact it rests on"
    assert len(ch.links_of(bundle, "continued_from")) == 1, "a continuation was invented"


def test_triggered_by_names_the_request_the_queue_admitted():
    store = EpisodeStore()
    admitted = ch.admit_and_close(store, trajectory=ch.trajectory("t1", operator=True),
                                  cycle="bubble-wo10-trigger", arrival=7001)
    ch.close_members(store, 9)
    _staged, manifest, records = ch.seal_staged()

    bundle = ch.collect(manifest, records)
    triggered = [row for row in ch.links_of(bundle, "triggered_by")
                 if row["source"] == _ep(admitted)]
    assert len(triggered) == 1
    assert triggered[0]["target"].startswith("request:")
    assert triggered[0]["detail"]["contributor_type"] == "operator_query"
    # Episodes that never passed the admission seam have no recorded trigger: none is invented.
    assert {row["source"] for row in ch.links_of(bundle, "triggered_by")} == {_ep(admitted)}


def test_a_tests_link_is_bound_to_the_exact_bytes_another_episode_produced():
    from memory.auto_evidence import capture_artifact_revision, capture_verification

    store = EpisodeStore()
    producer = ch.close_with(store, lambda c: capture_artifact_revision(
        c, path="parser.py", revision=1, content_sha256=SHA_P, change_kind="created"))
    same_bytes = ch.close_with(store, lambda c: capture_verification(
        c, criterion="pytest passes", path="parser.py", revision=1, content_sha256=SHA_P,
        passed=True))
    other_bytes = ch.close_with(store, lambda c: capture_verification(
        c, criterion="pytest passes", path="parser.py", revision=2, content_sha256=SHA_X,
        passed=False))
    ch.close_members(store, 7)
    _staged, manifest, records = ch.seal_staged()

    bundle = ch.collect(manifest, records)
    assert _link(bundle, "produces", _ep(producer), f"artifact:parser.py@{SHA_P}")
    exact = _link(bundle, "tests", _ep(same_bytes), f"artifact:parser.py@{SHA_P}")
    assert len(exact) == 1 and exact[0]["detail"]["produced_by"] == _ep(producer)
    other = _link(bundle, "tests", _ep(other_bytes), f"artifact:parser.py@{SHA_X}")
    assert len(other) == 1 and other[0]["detail"]["produced_by"] is None, (
        "a check of different bytes was credited to the episode that produced other bytes"
    )


def test_operator_feedback_is_an_evaluates_relation_carrying_its_polarity():
    store = EpisodeStore()
    claim = ch.close_learn(store)
    ch.close_members(store, 3)
    critic = ch.close_with(store, lambda c: ch.record_feedback(
        c, about_episode_id=claim.episode_id, feedback="contradicts",
        statement="that summary was wrong"))
    ch.close_members(store, 5)
    _staged, manifest, records = ch.seal_staged()

    bundle = ch.collect(manifest, records)
    rows = _link(bundle, "evaluates", _ep(critic), _ep(claim))
    assert len(rows) == 1
    assert rows[0]["detail"]["feedback"] == "contradicts"
    assert rows[0]["evidence_refs"]


# ── nothing a model says becomes an edge ────────────────────────────────────


def test_every_relation_is_host_authored_and_cites_evidence_it_rests_on():
    store = EpisodeStore()
    failed = ch.close_with(store, lambda c: None, kind="failure")  # Omega asserts a cause in text
    ch.close_members(store, 9)
    _staged, manifest, records = ch.seal_staged()

    h = ch.history()
    bundle = ch.collect(manifest, records)
    assert bundle.links, "a cohort with a chronology has at least its commit order"
    for row in bundle.links:
        assert row["relation_kind"] in h.HOST_RELATION_KINDS, row
        assert row["author"] == "host", row
        assert row["evidence_refs"], row
    text = " ".join(str(dict(row)) for row in bundle.links)
    assert "acceptance test fails" not in text and "failed its acceptance" not in text, (
        "model-authored assessment text became part of a relation"
    )
    assert not [row for row in bundle.links
                if row["relation_kind"] in ("read_from", "continued_from")
                and _ep(failed) in (row["source"], row["target"])]
