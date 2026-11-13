"""WO-07 Step 3 — every mutating per-episode effect is retry-safe, with one stable effect id.

``EpisodeStore.close`` keeps an episode open when its terminus raises, and the carrier is closed
again from the top: the whole lane-local half runs a second time with the same episode. Before
WO-07 every store it touched was read-modify-write with nothing that remembered the first pass:

* mastery ``evidence_count = prior + 1`` — and the retry's reported effect was a zero delta,
  so the journal row, the canonical body and the window delta all LOST the real one;
* failure ``frequency = prior + 1``;
* a second DeepSleep journal row, and a second canonical pathway outcome for one attempt.

The rule now: each effect carries a stable id derived from its episode and target, the store
update and its receipt land in the same atomic write, and a retry that finds its receipt
reconciles from it instead of applying again. The receipt, not a recomputation, is what the
retry reports — so the effect the first pass applied survives into the sealed record.
"""

from __future__ import annotations

import pytest

from pressure_loop.episode import EpisodeStore
from tests.helpers import continuity_terminus as ct

DOMAIN = "quantum_physics"
DOMAIN_ID = "quantum-physics"


@pytest.fixture(autouse=True)
def _terminus(tmp_path, monkeypatch):
    from memory import episode_sram

    paths = ct.isolate(monkeypatch, tmp_path)
    yield paths
    episode_sram.reset()


def _fail_once(monkeypatch, target, name: str, message: str) -> dict:
    """Wrap ``target.name`` so its FIRST call raises after (or instead of) the real work."""
    real = getattr(target, name)
    state = {"calls": 0}

    def wrapper(*args, **kwargs):
        state["calls"] += 1
        if state["calls"] == 1:
            raise RuntimeError(message)
        return real(*args, **kwargs)

    monkeypatch.setattr(target, name, wrapper)
    return state


def _fail_after_once(monkeypatch, target, name: str, message: str) -> dict:
    """Wrap ``target.name`` so its FIRST call does the real work, THEN raises."""
    real = getattr(target, name)
    state = {"calls": 0}

    def wrapper(*args, **kwargs):
        state["calls"] += 1
        result = real(*args, **kwargs)
        if state["calls"] == 1:
            raise RuntimeError(message)
        return result

    monkeypatch.setattr(target, name, wrapper)
    return state


def _mastery():
    from deepsleep_loop.mastery_store import load_mastery

    return load_mastery()[DOMAIN_ID]


def _sealed(episode_id: str):
    from memory.episode_commit_view import episode_record_ref, resolve_episode_commit
    from tests.continuity.test_episode_commit_view import _snapshot

    return resolve_episode_commit(episode_record_ref(episode_id), snapshot=_snapshot()).record


def _receipt_ids(receipts, episode_id: str) -> set[str]:
    return {effect_id for effect_id in dict(receipts or {}) if episode_id in effect_id}


# ── the seed: a real store update, then a crash before its acknowledgement ────


def test_a_crash_after_the_mastery_update_retains_one_effect_id_and_one_credit(monkeypatch):
    import deepsleep_loop.loop as loop

    store = EpisodeStore()
    carrier = ct.dress_learn(ct.open_carrier(store), domain=DOMAIN)
    eid = carrier.episode_id
    # The mastery store is updated, then the NEXT phase raises: after the effect, before any
    # acknowledgement of it.
    _fail_once(monkeypatch, loop.failure_phase, "consolidate_failures", "injected after mastery")
    with pytest.raises(RuntimeError, match="injected after mastery"):
        ct.close(store)
    assert store.current() is carrier, "the episode must stay open for the retry"

    first = _mastery()
    assert first.evidence_count == 1
    first_ids = _receipt_ids(getattr(first, "effect_receipts", None), eid)
    assert len(first_ids) == 1, "the store update carries no receipt naming its effect"

    pack = ct.close(store)
    again = _mastery()
    assert again.evidence_count == 1, "the retry credited the same mastery effect twice"
    assert _receipt_ids(again.effect_receipts, eid) == first_ids, "the effect id changed on retry"
    assert pack.journal_entry["mastery_effects"][DOMAIN_ID]["knowledge_delta"] == pytest.approx(0.5), (
        "the retry recomputed the effect from the already-updated store and lost it"
    )

    record = _sealed(eid)
    applied = {row["effect_id"] for row in record.learning["applied_receipts"]}
    assert first_ids <= applied, "the sealed record does not carry the applied effect's receipt"


def test_a_crash_before_the_mastery_update_applies_it_once_on_retry(monkeypatch):
    import deepsleep_loop.mastery.run as mastery_run

    store = EpisodeStore()
    ct.dress_learn(ct.open_carrier(store), domain=DOMAIN)
    _fail_once(monkeypatch, mastery_run, "upsert_mastery", "injected before the write")
    with pytest.raises(RuntimeError, match="injected before the write"):
        ct.close(store)

    from deepsleep_loop.mastery_store import load_mastery

    assert DOMAIN_ID not in load_mastery(), "a refused write must leave no partial credit"
    ct.close(store)
    assert _mastery().evidence_count == 1


def test_a_crash_inside_the_commit_region_does_not_recredit_the_lane_local_half(monkeypatch):
    import memory.pathway_performance as pathway

    store = EpisodeStore()
    carrier = ct.dress_learn(ct.open_carrier(store), domain=DOMAIN)
    _fail_once(monkeypatch, pathway, "rebuild_pathway_projection", "injected in the region")
    with pytest.raises(RuntimeError, match="injected in the region"):
        ct.close(store)
    pack = ct.close(store)

    assert _mastery().evidence_count == 1
    assert pack.journal_entry["mastery_effects"][DOMAIN_ID]["knowledge_delta"] == pytest.approx(0.5)
    from deepsleep_loop.journal_store import read_journal

    rows = [row for row in read_journal(50) if row.episode_id == carrier.episode_id]
    assert len(rows) == 1, f"one terminus wrote {len(rows)} DeepSleep journal rows"


# ── failure patterns ─────────────────────────────────────────────────────────


def test_a_retried_failure_pattern_is_counted_once(monkeypatch):
    import deepsleep_loop.loop as loop
    from deepsleep_loop.failure_store import load_failures

    store = EpisodeStore()
    carrier = ct.dress_failed_build(ct.open_carrier(store))
    _fail_once(monkeypatch, loop.semantic_phase, "promote_semantic", "injected after failure")
    with pytest.raises(RuntimeError, match="injected after failure"):
        ct.close(store)
    first = {key: pattern.frequency for key, pattern in load_failures().items()}
    assert first and all(freq == 1 for freq in first.values()), first

    pack = ct.close(store)
    after = {key: pattern.frequency for key, pattern in load_failures().items()}
    assert after == first, "the retry counted the same failure again"
    assert pack.journal_entry["failure_effects"], "the retry lost the failure effect it reported"
    for pattern in load_failures().values():
        assert _receipt_ids(getattr(pattern, "effect_receipts", None), carrier.episode_id)


# ── pathway outcomes (canonical, through SRAM) ──────────────────────────────


def test_a_retried_motor_attempt_records_one_pathway_outcome(monkeypatch):
    import memory.pathway_performance as pathway
    from core.paths import MEMORY_ROOT
    from memory.projection_receipt import verify_projection_receipt

    store = EpisodeStore()
    carrier = ct.dress_failed_build(ct.open_carrier(store))
    _fail_once(monkeypatch, pathway, "rebuild_pathway_projection", "injected in the region")
    with pytest.raises(RuntimeError, match="injected in the region"):
        ct.close(store)
    ct.close(store)

    outcomes = [row for row in pathway._load_canonical_pathway_rows()
                if row.get("type") == "pathway_outcome"
                and str((row.get("enrichment") or {}).get("episode_id") or "") == carrier.episode_id]
    assert len(outcomes) == 1, f"one attempt produced {len(outcomes)} canonical pathway outcomes"
    proof = verify_projection_receipt(outcomes[0], root=MEMORY_ROOT)
    assert proof.ok, proof.reason


# ── the window scope ─────────────────────────────────────────────────────────


def test_a_window_terminus_does_not_credit_its_ten_episodes_again(monkeypatch):
    from deepsleep_loop.failure_store import load_failures
    from deepsleep_loop.journal_store import read_journal
    from deepsleep_loop.loop import run_deepsleep_for_window
    from pressure_loop import window_lane
    from pressure_loop.reflection_window import WINDOW_SIZE

    monkeypatch.setenv("OMEGA_WINDOW_LANE", "1")
    store = EpisodeStore()
    for _ in range(WINDOW_SIZE):
        ct.dress_learn(ct.open_carrier(store), domain=DOMAIN)
        ct.close(store)
    credited = _mastery()
    assert credited.evidence_count == WINDOW_SIZE
    failures_before = {key: row.frequency for key, row in load_failures().items()}
    staged = window_lane.peek_staged_window(window_path=None)
    assert staged is not None and len(staged.episodes) == WINDOW_SIZE

    from pressure_loop.window_lane import WINDOW_REFLECTION_READY

    window_episode = store.open(opened_by=WINDOW_REFLECTION_READY, pressure={},
                                pipeline_id=3, work_kind="reflection")
    from memory import episode_sram

    episode_sram.open_bucket(window_episode.episode_id, pipeline_id=3)
    window = window_lane.restore_window(window_episode)
    outcome = {"reflection_source": "deterministic", "trajectory_update": {},
               "synthesis": "ten LEARN episodes on one domain"}
    run_deepsleep_for_window(store, window, outcome)

    after = _mastery()
    assert after.evidence_count == WINDOW_SIZE, "the window terminus credited its members again"
    assert after.effect_receipts == credited.effect_receipts
    assert {key: row.frequency for key, row in load_failures().items()} == failures_before
    window_rows = [row for row in read_journal(50) if row.scope == "window"]
    assert len(window_rows) == 1 and not window_rows[0].mastery_effects


def test_a_retried_tenth_episode_joins_its_window_once(monkeypatch):
    """The tenth append stages the window and starts a new one. A retry after that must not
    append the same episode again — to the NEXT window."""
    import memory.pathway_performance as pathway
    from pressure_loop import window_lane
    from pressure_loop.reflection_window import WINDOW_SIZE

    monkeypatch.setenv("OMEGA_WINDOW_LANE", "1")
    store = EpisodeStore()
    for _ in range(WINDOW_SIZE - 1):
        ct.dress_learn(ct.open_carrier(store), domain=DOMAIN)
        ct.close(store)
    tenth = ct.dress_learn(ct.open_carrier(store), domain=DOMAIN)
    _fail_once(monkeypatch, pathway, "rebuild_pathway_projection", "injected after the append")
    with pytest.raises(RuntimeError, match="injected after the append"):
        ct.close(store)
    ct.close(store)

    staged = window_lane.peek_staged_window(window_path=None)
    assert staged is not None and staged.episodes.count(tenth.episode_id) == 1
    collecting = store.get_active_window()
    assert collecting is None or tenth.episode_id not in collecting.episodes, (
        "the retried tenth episode was appended to the next window as well"
    )


def test_a_retried_window_terminus_reinforces_its_trajectory_once(monkeypatch):
    """The window terminus's own effect: one trajectory pass per window, whatever the retry."""
    import memory.pathway_performance as pathway
    from deepsleep_loop.loop import run_deepsleep_for_window
    from deepsleep_loop.trajectory_store import load_trajectory
    from memory import episode_sram
    from pressure_loop import window_lane
    from pressure_loop.reflection_window import WINDOW_SIZE
    from pressure_loop.window_lane import WINDOW_REFLECTION_READY

    monkeypatch.setenv("OMEGA_WINDOW_LANE", "1")
    store = EpisodeStore()
    for _ in range(WINDOW_SIZE):
        ct.dress_learn(ct.open_carrier(store), domain=DOMAIN)
        ct.close(store)
    window_episode = store.open(opened_by=WINDOW_REFLECTION_READY, pressure={},
                                pipeline_id=3, work_kind="reflection")
    episode_sram.open_bucket(window_episode.episode_id, pipeline_id=3)
    window = window_lane.restore_window(window_episode)
    outcome = {
        "reflection_source": "deterministic",
        "synthesis": "ten LEARN episodes on one domain",
        "trajectory_update": {"target_domain": DOMAIN, "target_capability": "foundations",
                              "confidence": 0.7, "reason": "the domain keeps recurring"},
    }
    _fail_once(monkeypatch, pathway, "rebuild_pathway_projection", "injected in the window")
    with pytest.raises(RuntimeError, match="injected in the window"):
        run_deepsleep_for_window(store, window, outcome)
    first = {key: record.reinforcement_count for key, record in load_trajectory().items()}
    assert first, "the first attempt saved no trajectory pass"

    run_deepsleep_for_window(store, window, outcome)
    after = load_trajectory()
    assert {key: record.reinforcement_count for key, record in after.items()} == first, (
        "the retried window terminus reinforced its trajectory a second time"
    )
    (record,) = [row for row in after.values() if row.target_domain == DOMAIN]
    assert _receipt_ids(record.effect_receipts, window.window_id)


def test_a_named_trajectory_write_lands_once():
    from memory import episode_sram, intake_journal
    from pressure_loop.temporal_store import write_trajectory
    from schema.omega_trajectory import make_trajectory

    episode_sram.open_bucket("ep-trajectory", pipeline_id=0)
    published = make_trajectory(
        cycle_id="window-x", epoch_emitted=0, intent="explore_domain", target_domain=DOMAIN,
        target_capability="foundations", rationale="window reflection", influencer_text="",
    ).to_dict()
    for _ in range(2):  # the second call is a retry: same name, a new close time inside
        write_trajectory(published, event_id="trajectory:window-x:quantum")
    rows = [row for row in intake_journal.records()
            if row.kind == "memory" and (row.payload or {}).get("type") == "trajectory"]
    assert len(rows) == 1, f"one named trajectory write journalled {len(rows)} records"


# ── effect identity is stable and derived, never minted ─────────────────────


def test_effect_ids_are_derived_from_episode_and_target(monkeypatch):
    store = EpisodeStore()
    carrier = ct.dress_learn(ct.open_carrier(store), domain=DOMAIN)
    ct.close(store)
    (effect_id,) = _receipt_ids(_mastery().effect_receipts, carrier.episode_id)
    assert effect_id.startswith("mastery:")
    assert carrier.episode_id in effect_id and DOMAIN_ID in effect_id
