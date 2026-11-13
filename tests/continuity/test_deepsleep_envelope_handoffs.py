"""DeepSleep preserves its actual inputs and failures through the final publication.

The immutable episode seal describes what was known when it was committed. A later
terminus failure still belongs to that episode: it is retained in SRAM, committed on
retry through the same drain, and exposed beside the seal only in a graph that holds it.
"""

from __future__ import annotations

import json

import pytest

from pressure_loop.episode import EpisodeStore
from tests.helpers import continuity_terminus as ct


LATE_FAILURE = "SENTINEL: Program 0 could not publish this episode"


@pytest.fixture(autouse=True)
def _terminus(tmp_path, monkeypatch):
    from memory import episode_sram

    monkeypatch.setenv("OMEGA_WINDOW_LANE", "1")
    paths = ct.isolate(monkeypatch, tmp_path)
    yield paths
    episode_sram.reset()


def _snapshot():
    from tests.continuity.test_episode_commit_view import _snapshot as current_snapshot

    return current_snapshot()


def _publication_failures(monkeypatch, *, count=1):
    import deepsleep_loop.loop as loop

    publish = loop._publish_projection_after_deepsleep
    calls = []

    def fail_then_publish(**kwargs):
        calls.append(dict(kwargs))
        if len(calls) <= count:
            raise RuntimeError(f"{LATE_FAILURE} ({len(calls)})")
        return publish(**kwargs)

    monkeypatch.setattr(loop, "_publish_projection_after_deepsleep", fail_then_publish)


def _failure_events(episode_id):
    from memory.cognition_capture import DEEPSLEEP_FAILED
    from memory.episode_events import read_episode_events

    return tuple(event for event in read_episode_events(episode_id)
                 if event.event_kind == DEEPSLEEP_FAILED)


def _event_documents(episode_id):
    from core.paths import MEMORY_ROOT

    found = []
    for path in MEMORY_ROOT.rglob("*.json"):
        if "indexes" in path.parts or "tmp_writes" in path.parts:
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (isinstance(document, dict) and document.get("type") == "episode_event"
                and (document.get("enrichment") or {}).get("episode_id") == episode_id):
            found.append(document)
    return found


def test_episode_journal_consumes_actual_global_b_and_omega_envelopes():
    from deepsleep_loop.consolidate.run import build_journal_entry
    from deepsleep_loop.intake.run import build_deepsleep_input_for_episode
    from pressure_loop.episode_journal import enriched_episode_from_episode

    carrier = ct.dress_failed_build(ct.open_carrier(EpisodeStore()))
    body = enriched_episode_from_episode(carrier)
    ds_input = build_deepsleep_input_for_episode(body, carrier.episode_id)
    journal = build_journal_entry(ds_input, scope="episode").to_dict()
    actual_pack = carrier.global_["outcome"]
    actual_omega = carrier.global_["omega_result"]

    assert journal["outcome"] == actual_pack["outcome"], (
        "the per-episode journal replaced Global B's actual verdict with a window summary"
    )
    assert journal["observations"] == actual_pack["observations"]
    assert journal.get("global_outcome") == actual_pack, "the complete Global B pack was lost"
    assert journal.get("omega_result") == actual_omega, "the actual Omega result was lost"
    assert journal["omega_reasoning"], "per-episode metacognition became empty window reasoning"


def test_canonical_journal_preserves_the_actual_omega_reason(_terminus):
    from core.paths import MEMORY_ROOT
    from deepsleep_loop.journal_store import read_journal

    store = EpisodeStore()
    carrier = ct.dress_failed_build(ct.open_carrier(store))
    pack = ct.close(store)
    persisted = read_journal(1, path=_terminus["deepsleep_journal"])[0].to_dict()
    assert persisted.get("omega_result") == json.loads(json.dumps(carrier.global_["omega_result"]))
    assert pack.journal_entry.get("global_outcome") == carrier.global_["outcome"]

    documents = [json.loads(path.read_text(encoding="utf-8"))
                 for path in (MEMORY_ROOT / "600_journal").rglob("*.json")]
    journals = [doc for doc in documents if doc.get("type") == "consolidation_journal"]
    assert journals, "no canonical consolidation journal was committed"
    assert any(ct.SENTINEL_REASON in json.dumps(doc, ensure_ascii=False) for doc in journals), (
        "the lobe-6 journal persisted an empty window reflection instead of Omega's reason"
    )


def test_failed_publication_retains_the_live_owner_and_exact_failure_event(monkeypatch):
    from memory import episode_sram, intake_journal

    store = EpisodeStore()
    carrier = ct.dress_learn(ct.open_carrier(store))
    _publication_failures(monkeypatch)
    with pytest.raises(RuntimeError, match=LATE_FAILURE):
        ct.close(store)

    assert store.current() is carrier
    assert episode_sram.is_open(carrier.episode_id), (
        "DeepSleep destroyed the live SRAM owner before its last required phase"
    )
    events = _failure_events(carrier.episode_id)
    assert len(events) == 1, "the post-drain failure never entered episode content"
    assert LATE_FAILURE in events[0].payload["reason"]
    rows = intake_journal.pending_records(owner_episode_id=carrier.episode_id)
    assert events[0].event_id in {row.event_id for row in rows}


def test_first_pass_requires_its_exact_published_parent_before_acknowledging_events(monkeypatch):
    import deepsleep_loop.loop as loop
    from memory import episode_commit_view as view, episode_sram, intake_journal
    from memory.episode_events import read_episode_events
    from memory.projection.dispatch import DispatchReport

    baseline_store = EpisodeStore()
    ct.dress_learn(ct.open_carrier(baseline_store))
    ct.close(baseline_store)
    served = _snapshot()
    store = EpisodeStore()
    carrier = ct.dress_failed_build(ct.open_carrier(store))
    original_events = {event.event_id for event in read_episode_events(carrier.episode_id)}
    assert original_events

    # A generic successful dispatch receipt is not proof of this specific episode. The real
    # drain still writes its complete canonical seal, but the served graph remains the older
    # genuine publication. This is the first close, so there are no supplemental events.
    monkeypatch.setattr(loop, "_publish_projection_after_deepsleep", lambda **_kwargs: DispatchReport(
        owner="program0", action="published", publication=None,
        drain_status="complete", generation_id=served.generation_id,
    ))
    with pytest.raises(RuntimeError, match="parent.*published"):
        ct.close(store)

    ref = view.episode_record_ref(carrier.episode_id)
    commit = view.resolve_episode_commit(ref, snapshot=served)
    assert commit.memory_commit_status == "canonical_committed"
    assert commit.publication["member"] is False
    assert store.current() is carrier
    assert episode_sram.is_open(carrier.episode_id)
    pending = intake_journal.pending_records(owner_episode_id=carrier.episode_id)
    assert original_events <= {row.event_id for row in pending}
    assert _failure_events(carrier.episode_id)


def test_failure_after_publication_retains_unacknowledged_events_and_window_hold(monkeypatch):
    import deepsleep_loop.loop as loop
    from memory import episode_commit_view as view, episode_sram, intake_journal
    from memory.cognition_capture import capture_episode_opened

    store = EpisodeStore()
    carrier = ct.dress_learn(ct.open_carrier(store))
    original_event = capture_episode_opened(carrier, pin=None, trajectory={})
    compact = loop._compact_episode_temporal_summary
    calls = []

    def fail_summary_once(value):
        calls.append(value)
        if len(calls) == 1:
            raise RuntimeError("SENTINEL: final temporal summary failed")
        return compact(value)

    monkeypatch.setattr(loop, "_compact_episode_temporal_summary", fail_summary_once)
    with pytest.raises(RuntimeError, match="final temporal summary failed"):
        ct.close(store)
    assert episode_sram.is_open(carrier.episode_id)
    events = _failure_events(carrier.episode_id)
    assert len(events) == 1
    pending = intake_journal.pending_records(owner_episode_id=carrier.episode_id)
    assert events[0].event_id in {row.event_id for row in pending}
    assert original_event.event_id in {row.event_id for row in pending}, (
        "original events were acknowledged before the last required summary succeeded"
    )
    ref = view.episode_record_ref(carrier.episode_id)
    assert view.window_members_unpublished([carrier.episode_id]) == (carrier.episode_id,)
    ct.close(store)
    assert view.episode_record_ref(carrier.episode_id) == ref
    assert len(view.resolve_episode_commit(ref, snapshot=_snapshot()).supplemental_events) == 1
    assert view.window_members_unpublished([carrier.episode_id]) == ()


def test_window_terminus_failure_enters_its_own_episode_memory(monkeypatch):
    from types import SimpleNamespace

    import deepsleep_loop.loop as loop
    from memory import episode_sram, intake_journal

    store = EpisodeStore()
    carrier = ct.dress_learn(ct.open_carrier(store, work_kind="reflection"))

    def failed_terminus(*args, **kwargs):
        loop._flush_episode_sram(carrier.episode_id)
        raise RuntimeError("SENTINEL: window terminus failed after drain")

    monkeypatch.setattr(loop, "_run_deepsleep_episode_core", failed_terminus)
    with pytest.raises(RuntimeError, match="window terminus failed after drain"):
        loop.run_deepsleep_for_window(store, SimpleNamespace(window_id="window-tail"), {})

    assert store.current() is carrier
    assert episode_sram.is_open(carrier.episode_id)
    events = _failure_events(carrier.episode_id)
    assert len(events) == 1, "window DeepSleep failure was never captured in its own episode"
    assert "window terminus failed after drain" in events[0].payload["reason"]
    assert events[0].event_id in {
        row.event_id for row in intake_journal.pending_records(owner_episode_id=carrier.episode_id)
    }


def test_retry_publishes_late_failure_without_rewriting_original_seal(monkeypatch):
    from memory import episode_commit_view as view, episode_sram, intake_journal

    store = EpisodeStore()
    carrier = ct.dress_learn(ct.open_carrier(store))
    _publication_failures(monkeypatch)
    with pytest.raises(RuntimeError, match=LATE_FAILURE):
        ct.close(store)
    original_ref = view.episode_record_ref(carrier.episode_id)
    original_document = ct.canonical_episode_documents(carrier.episode_id)[0]
    original_stamp = dict(carrier.timekeeper)

    ct.close(store)
    assert view.episode_record_ref(carrier.episode_id) == original_ref
    assert ct.canonical_episode_documents(carrier.episode_id)[0]["text"] == original_document["text"]
    assert carrier.timekeeper == original_stamp
    commit = view.resolve_episode_commit(original_ref, snapshot=_snapshot())
    supplements = getattr(commit, "supplemental_events", ())
    assert len(supplements) == 1, "the retry's failure is absent from published episode evidence"
    assert LATE_FAILURE in supplements[0].payload["reason"]
    assert supplements[0].event_id not in {e.event_id for e in commit.record.events["by_value"]}
    assert commit.record.content_sha256() == original_ref.content_sha256

    documents = _event_documents(carrier.episode_id)
    assert len(documents) == 1
    enrichment = documents[0]["enrichment"]
    assert enrichment["parent_episode_ref"] == original_ref.to_dict()
    assert enrichment["episode_event"] == supplements[0].to_dict()
    assert not intake_journal.pending_records(owner_episode_id=carrier.episode_id)
    assert not episode_sram.is_open(carrier.episode_id)


def test_unpublished_supplements_never_leak_into_an_older_snapshot(monkeypatch):
    from memory import episode_commit_view as view, intake_journal

    baseline = EpisodeStore()
    ct.dress_learn(ct.open_carrier(baseline))
    ct.close(baseline)
    before = _snapshot()
    store = EpisodeStore()
    carrier = ct.dress_learn(ct.open_carrier(store))
    _publication_failures(monkeypatch, count=2)
    with pytest.raises(RuntimeError, match=LATE_FAILURE):
        ct.close(store)
    ref = view.episode_record_ref(carrier.episode_id)
    with pytest.raises(RuntimeError, match=LATE_FAILURE):
        ct.close(store)

    assert _event_documents(carrier.episode_id), "the first failure was never canonically drained"
    commit_before = view.resolve_episode_commit(ref, snapshot=before)
    assert getattr(commit_before, "supplemental_events", ()) == (), (
        "canonical durability was mistaken for publication in the requested snapshot"
    )
    assert any(row.kind == "episode_event" for row in
               intake_journal.pending_records(owner_episode_id=carrier.episode_id))
    ct.close(store)
    current = view.resolve_episode_commit(ref, snapshot=_snapshot())
    assert len(getattr(current, "supplemental_events", ())) == 2
    assert not intake_journal.pending_records(owner_episode_id=carrier.episode_id)


def test_released_late_failure_publishes_under_its_original_parent_after_boot(monkeypatch):
    from memory import episode_commit_view as view, episode_sram, intake_journal

    first_store = EpisodeStore()
    original = ct.dress_learn(ct.open_carrier(first_store))
    _publication_failures(monkeypatch)
    with pytest.raises(RuntimeError, match=LATE_FAILURE):
        ct.close(first_store)
    original_ref = view.episode_record_ref(original.episode_id)
    original_body = ct.canonical_episode_documents(original.episode_id)[0]["text"]
    original_event = _failure_events(original.episode_id)[0]

    # Exercise the actual durable boot release/adoption seam after losing all SRAM.
    episode_sram.reset()
    intake_journal.reset_cache_for_tests()
    released = intake_journal.reconcile_at_boot(
        episode_id=original.episode_id, pipeline_id=original.pipeline_id, cascade_replay=False,
    )
    assert released["released"] >= 1
    recovered_store = EpisodeStore()
    recovered = ct.dress_learn(ct.open_carrier(recovered_store))
    adopted = [item for item in episode_sram.pending(recovered.episode_id)
               if item.kind == "event"]
    assert original_event.event_id in {item.event_id for item in adopted}
    ct.close(recovered_store)

    commit = view.resolve_episode_commit(original_ref, snapshot=_snapshot())
    assert [event.to_dict() for event in commit.supplemental_events] == [original_event.to_dict()], (
        "the recovery owner drained the late event without publishing its original parent's evidence"
    )
    assert view.episode_record_ref(original.episode_id) == original_ref
    assert ct.canonical_episode_documents(original.episode_id)[0]["text"] == original_body
    document = _event_documents(original.episode_id)[0]
    assert document["enrichment"]["parent_episode_ref"] == original_ref.to_dict()
    row = next(row for row in intake_journal.records() if row.event_id == original_event.event_id)
    assert row.owner_episode_id == recovered.episode_id
    assert row.origin_episode_id == original.episode_id
    assert row.status == intake_journal.CONSUMED
    assert not intake_journal.pending_records(owner_episode_id=recovered.episode_id)
    assert not episode_sram.is_open(recovered.episode_id)


def test_recovered_event_without_a_canonical_parent_stays_pending_without_blocking_deepsleep():
    """A source that died before its seal has no parent to publish under. Its adopted event
    is deferred, neither published under the adopter nor dropped, and the adopter still
    closes. Blocking here would stop every later terminus on the pipeline."""
    from memory import episode_commit_view as view, episode_sram, intake_journal
    from memory.cognition_capture import capture_episode_opened

    interrupted_store = EpisodeStore()
    interrupted = ct.dress_learn(ct.open_carrier(interrupted_store))
    original_event = capture_episode_opened(interrupted, pin=None, trajectory={})
    assert view.episode_record_ref(interrupted.episode_id) is None
    episode_sram.reset()
    intake_journal.reset_cache_for_tests()
    intake_journal.reconcile_at_boot(
        episode_id=interrupted.episode_id, pipeline_id=interrupted.pipeline_id,
        cascade_replay=False,
    )
    recovered_store = EpisodeStore()
    recovered = ct.dress_learn(ct.open_carrier(recovered_store))
    assert original_event.event_id in {
        item.event_id for item in episode_sram.pending(recovered.episode_id)
    }, "the recovered episode adopts the released event"
    ct.close(recovered_store)

    assert recovered_store.current() is None, "the adopter closes"
    assert view.episode_record_ref(recovered.episode_id) is not None
    pending = intake_journal.pending_records(owner_episode_id=recovered.episode_id)
    assert original_event.event_id in {row.event_id for row in pending}, \
        "the unprovable event is deferred, not consumed or dropped"
    assert view.episode_record_ref(interrupted.episode_id) is None, \
        "and it was never published under a parent it does not have"


def test_window_hydration_and_prompt_carry_published_late_failure(monkeypatch):
    from omega_loop.window.membership import hydrate_window_members, seal_window, window_snapshot
    from omega_loop.window.run import build_window_view
    from tests.helpers import continuity_window as cw
    from types import SimpleNamespace

    store = EpisodeStore()
    carrier = ct.dress_learn(ct.open_carrier(store))
    _publication_failures(monkeypatch)
    with pytest.raises(RuntimeError, match=LATE_FAILURE):
        ct.close(store)
    ct.close(store)
    cw.close_cohort(store, ("success",) * 9)
    staged = cw.staged_cohort()
    manifest = seal_window(staged, snapshot=window_snapshot(staged))
    members = hydrate_window_members(manifest)
    member = next(one for one in members if one.record.episode_id == carrier.episode_id)
    assert len(member.body.get("supplemental_events") or ()) == 1, (
        "window hydration dropped the member's published post-seal failure"
    )
    assert member.record.content_sha256() == member.ref.content_sha256
    prompt_view = build_window_view(SimpleNamespace(
        window_id=manifest.window_id, episode_count=len(members),
        episode_bodies=[dict(one.body) for one in members], trajectory_snapshot={},
        cascade_status_by_episode={},
    ))
    assert LATE_FAILURE in json.dumps(prompt_view, ensure_ascii=False), (
        "the window prompt erased the failure even though canonical hydration supplied it"
    )


def test_window_prompt_bounds_late_events_and_states_the_total():
    from types import SimpleNamespace

    from omega_loop.window.run import build_window_view

    events = [
        {"event_id": f"late-{index}", "phase": "deepsleep", "event_kind": "deepsleep.phase_failed",
         "status": "failed", "payload": {"reason": "late failure " * 200}}
        for index in range(20)
    ]
    prompt = build_window_view(SimpleNamespace(
        window_id="window-tail-budget", episode_count=1,
        episode_bodies=[{"episode_id": "episode-tail-budget", "supplemental_events": events}],
        trajectory_snapshot={}, cascade_status_by_episode={},
    ))
    summary = prompt["episodes"][0]["supplemental_events"]
    assert isinstance(summary, dict), "bounded prompt evidence must state its total and recent subset"
    assert summary["count"] == 20
    assert len(summary["recent"]) == 2
    assert [event["event_id"] for event in summary["recent"]] == ["late-18", "late-19"]
    assert all(len(event["reason"]) <= 240 for event in summary["recent"])
