"""Phase 4 cut 10 — the window lane: the ten-episode reflection actually runs.

Before this cut the reflection window filled, locked, and was archived
UNREFLECTED by the next episode's ignition seam. Every part of the machine
existed and none of it had a caller. These tests pin the four properties the cut
is made of:

* the DOUBLE BUFFER — a locked window moves to durable staging and collection
  starts over immediately, so episode 11 is never dropped on the floor;
* the SYNTHESIZED CONTRIBUTOR — the staged file IS the pressure, and it routes
  to the window lane on both the contributor side and the carrier side;
* EXACTLY-ONCE across crashes — the unstage is the commit point, the archive
  before it replays idempotently, the consolidation after it never doubles;
* window-lane episodes are EXCLUDED from windows — a reflection never becomes
  part of the next thing reflected on.

The operator's 2026-08-11 rulings add two more, each in its own section below:

* SCOPE (risk 7) — a window fold commits only what the window alone can know.
  The five phases whose evidence the ten episodes already consumed do not run,
  proved against the real terminus with a control that shows the same intake
  still consolidates when handed straight to the phase;
* NO-FAIL (risk 6) — the window commit always archives a verdict: Omega's
  window-lane reflection, or the counted deterministic verdict when that fold was
  contained as a failed thought, because a window that raised crashed the brain
  into a boot loop over a window that was still staged.

The canonical-cascade topology itself (Global A → Meta A → AUTO → Meta B →
Global B → Omega) is pinned by tests/architecture/test_window_canonical_cascade.py.
Everything here is hermetic: no model is invoked, and every durable path lands
under ``tmp_path``.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from core.pipelines import work_kind_for_contributor
from pressure_loop.window_lane import WINDOW_REFLECTION_READY
from pressure_loop import window_lane
from pressure_loop.contributors import make_contributor
from pressure_loop.episode import EpisodeStore
from pressure_loop.reflection_window import (
    WINDOW_SIZE,
    ReflectionWindow,
    load_reflection_window,
    load_staged_windows,
    staged_window_path,
)


@pytest.fixture
def window_state(tmp_path, monkeypatch):
    """Redirect the whole reflection-window surface into this test's tmp dir.

    ``staged_window_path`` derives from the active window's path on purpose, so
    redirecting the one function moves the double buffer's BOTH halves.
    """
    import pressure_loop.reflection_window as rw

    window_path = tmp_path / "reflection_window.json"
    monkeypatch.setattr(rw, "reflection_window_path", lambda: window_path)
    monkeypatch.setattr(rw, "current_trajectory_path", lambda: tmp_path / "trajectory.json")
    monkeypatch.setattr(
        rw, "reflection_window_archive_dir", lambda: tmp_path / "reflection_windows"
    )
    monkeypatch.setattr(
        "pressure_loop.policy.episode_journal_path",
        lambda policy=None: tmp_path / "episode_journal.jsonl",
    )
    return SimpleNamespace(
        window_path=window_path,
        staged_path=tmp_path / "reflection_window_staged.json",
        archive_dir=tmp_path / "reflection_windows",
        tmp=tmp_path,
    )


@pytest.fixture
def served_episode_ids(monkeypatch):
    """Model the served-graph membership prerequisite for synthetic windows.

    These routing tests use fabricated episode ids, so they cannot publish real
    episode records into Program 0. Keep the production release rule in place by
    replacing only its membership reader with an explicit in-memory served set;
    a staged member remains held until the test marks it served.
    """
    import memory.episode_commit_view as commit_view

    served = set()

    def unpublished(episode_ids, *, member_refs=None):
        del member_refs
        members = tuple(dict.fromkeys(str(item) for item in episode_ids if str(item)))
        return tuple(episode_id for episode_id in members if episode_id not in served)

    monkeypatch.setattr(commit_view, "window_members_unpublished", unpublished)
    return served


def _store(window_state) -> EpisodeStore:
    return EpisodeStore(
        reflection_window_path=window_state.window_path,
        trajectory_path=window_state.tmp / "trajectory.json",
    )


def _close_one(store: EpisodeStore, *, opened_by: str = "curiosity_gap") -> str:
    store.open(pipeline_id=0, work_kind="operator_turn", opened_by=opened_by)
    ep = store.current()
    assert ep is not None
    # Global B writes the whole GlobalOutcomePack here; the verdict is nested inside it.
    ep.global_ = {"action_goal": {"intent": "LEARN"}, "outcome": {"episode_id": "ep", "outcome": {"status": "success", "summary": "the step landed"},
                       "observations": {}, "artifacts": {}, "followups": [],
                       "consistency": {}}}
    ep.meta = {"execution_action_pack": {"action": "USE_TOOL"}}
    episode_id = ep.episode_id
    # Phase 4 cut 13: the reflection-window append moved out of ``close()`` and
    # into the DeepSleep terminus's serialized commit region, which reaches it
    # through ``before_finalize``. A test that closes an episode has to run the
    # same seam, or it is exercising a close that never happens in production.
    store.close(
        before_finalize=lambda ep, body: store.commit_reflection_window_append(ep, body)
    )
    return episode_id


def _fill_window(store: EpisodeStore) -> list[str]:
    return [_close_one(store) for _ in range(WINDOW_SIZE)]


def _fake_window(window_id: str = "win-1", episodes: int = WINDOW_SIZE) -> ReflectionWindow:
    return ReflectionWindow(
        window_id=window_id,
        started_at_episode="ep-1",
        trajectory_snapshot={"target_domain": "python"},
        episodes=[f"ep-{i}" for i in range(episodes)],
        episode_bodies=[
            {
                "episode_id": f"ep-{i}",
                "intent": "LEARN",
                "action": "USE_TOOL",
                "outcome": {"episode_id": "ep", "outcome": {"status": "success", "summary": "the step landed"},
                       "observations": {}, "artifacts": {}, "followups": [],
                       "consistency": {}},
                "cascade_status": "completed",
            }
            for i in range(episodes)
        ],
        status="locked",
        locked_at="2026-08-11T00:00:00Z",
    )


_GOOD_REFLECTION = json.dumps(
    {
        "reflection": {
            "summary": "Ten episodes of shallow lookups.",
            "reasoning": {
                "what": "answered questions",
                "why": "the operator asked",
                "identity": "prefers verified answers",
            },
            "emergent_observations": {
                "repeated_behaviors": ["reaches for recall before the web"],
                "stable_patterns": ["short answers"],
            },
            "trajectory": {
                "decision": "continue",
                "target_domain": "python",
                "target_capability": "async",
                "reason": "the gap keeps recurring",
            },
        },
        "trajectory_update": {
            "target_domain": "python",
            "target_capability": "async",
            "confidence": 0.6,
            "reason": "the gap keeps recurring",
        },
    }
)


# ── routing: both sides in lockstep ──────────────────────────────────────────


def test_window_routing_is_lockstep_on_both_sides():
    """THE cut-10 precondition. ``lane_for_opened_by`` had no window branch, so a
    window ignition would have opened a META carrier and cut 8's tick would have
    offered the window lane and then held the turn forever."""
    contributor = make_contributor("omega", WINDOW_REFLECTION_READY, 1.0, {})
    assert work_kind_for_contributor(contributor) == "reflection"


# ── the double buffer ────────────────────────────────────────────────────────


def test_locked_window_stages_and_collection_starts_over(window_state):
    store = _store(window_state)
    ids = _fill_window(store)

    staged = load_staged_windows(window_state.staged_path)
    assert [w.episodes for w in staged] == [ids]
    assert staged[0].is_ready_for_reflection()
    # The collecting half was dropped, not left holding a locked window.
    assert load_reflection_window(window_state.window_path) is None
    assert store.get_active_window() is None
    # ...and the ELEVENTH close opens a fresh window instead of being discarded
    # by append_episode_to_window's "no 11th append" rule, which is what happened
    # for as long as the locked window sat in the one window file.
    eleventh = _close_one(store)
    fresh = load_reflection_window(window_state.window_path)
    assert fresh is not None
    assert fresh.episodes == [eleventh]
    assert fresh.window_id != staged[0].window_id


def test_staging_is_idempotent_by_window_id(window_state):
    """A crash between the staging write and the active-window clear replays the
    stage. Re-staging must MERGE — a duplicate would be reflected twice."""
    window = _fake_window()
    assert window_lane.stage_locked_window(window, window_path=window_state.window_path)
    assert not window_lane.stage_locked_window(window, window_path=window_state.window_path)
    assert len(load_staged_windows(window_state.staged_path)) == 1


def test_boot_does_not_archive_a_window_that_is_already_staged(window_state):
    """The crash shape the double buffer can leave: staged AND still active.
    Boot reconciliation must clear the stale active copy, never file ten
    episodes away unreflected while the staged row waits to reflect them."""
    from pressure_loop.reflection_window import (
        reconcile_stale_reflection_on_boot,
        save_reflection_window,
    )

    window = _fake_window("win-crashed")
    save_reflection_window(window, window_state.window_path)
    window_lane.stage_locked_window(window, window_path=window_state.window_path)

    result = reconcile_stale_reflection_on_boot(window_path=window_state.window_path)

    assert result == {
        "window_id": "win-crashed",
        "episode_count": WINDOW_SIZE,
        "reason": "already_staged_for_reflection",
    }
    assert load_reflection_window(window_state.window_path) is None
    assert [w.window_id for w in load_staged_windows(window_state.staged_path)] == ["win-crashed"]
    assert not (window_state.archive_dir / "win-crashed.json").exists()


# ── the staged file IS the pressure ──────────────────────────────────────────


def test_staged_window_synthesizes_the_ready_contributor(window_state, served_episode_ids):
    assert window_lane.pressure_contributor(window_path=window_state.window_path) is None

    window = _fake_window("win-p")
    window_lane.stage_locked_window(window, window_path=window_state.window_path)
    assert window_lane.pressure_contributor(window_path=window_state.window_path) is None

    # Only the served projection membership releases the staged cohort.
    served_episode_ids.update(window.episodes)
    contributor = window_lane.pressure_contributor(window_path=window_state.window_path)

    assert contributor is not None
    assert contributor.source == "omega"  # survives the autonomous-authority filter
    assert contributor.type == WINDOW_REFLECTION_READY
    assert contributor.metadata["window_id"] == "win-p"
    assert work_kind_for_contributor(contributor) == "reflection"


def test_gather_projects_the_staged_window_as_pressure(
    window_state, monkeypatch, served_episode_ids
):
    """The seam the tick actually reads. ``gather_contributors`` uses the DEFAULT
    staged path, which the session fixture already redirects — stage through the
    same door here so the test exercises the production read."""
    from core.shared_state import SharedState
    from pressure_loop.pressure import gather_contributors

    default_staged = staged_window_path()
    try:
        assert not any(
            c.type == WINDOW_REFLECTION_READY for c in gather_contributors(SharedState())
        )
        window = _fake_window("win-gather")
        window_lane.stage_locked_window(window)
        assert not any(
            c.type == WINDOW_REFLECTION_READY for c in gather_contributors(SharedState())
        ), "an unpublished synthetic cohort must remain held"
        served_episode_ids.update(window.episodes)
        gathered = gather_contributors(SharedState())
        window_rows = [c for c in gathered if c.type == WINDOW_REFLECTION_READY]
        assert len(window_rows) == 1
        assert window_rows[0].metadata["window_id"] == "win-gather"
    finally:
        if default_staged.is_file():
            default_staged.unlink()



def test_the_window_view_carries_spines_not_bodies():
    """Global B packages each episode's SPINE, never its full body — ten full
    bodies were the 2026-06 window GPU burn.

    EARLY-V01 (2026-09-21) widened the spine to project receipts the body already
    carried, because the previous eight fields held at most two distinct values across
    twenty real episodes and the window could not tell a stalled build from a finished
    one. The key set below is the widened contract. The guarantee this test exists for is
    unchanged and asserted harder underneath: a spine is still a BOUNDED projection, and
    the body's bulk fields must not ride along.
    """
    import json

    from omega_loop.window.run import build_window_view

    view = build_window_view(_fake_window("win-view"))

    assert view["window_id"] == "win-view"
    assert len(view["episodes"]) == WINDOW_SIZE
    assert set(view["episodes"][0]) == {
        "n",
        "episode_id",
        "intent",
        "action",
        "cascade_status",
        "outcome_status",
        "outcome_detail",
        "turns",
        "work_kind",
        "synthesis",
        "control_decision",
        "intent_reason",
        "intent_confidence",
        "pressure_params",
        "agent_run",
        "supplemental_events",
    }
    assert view["episodes"][0]["outcome_status"] == "success", (
        "the spine must carry the verdict Global B wrote, not an empty field"
    )
    assert view["episodes"][0]["outcome_detail"] == "the step landed"

    # The 2026-06 burn, guarded directly rather than implied by an enumeration: the
    # episode body's bulk structures must never appear in a spine.
    body_only = {"execution", "outputs", "omega_result", "memory_epoch", "timekeeper",
                 "learning_state", "neuromod", "pressure", "temporal", "brain_turns"}
    assert set(view["episodes"][0]) & body_only == set(), (
        "a body field leaked into the spine; ten full bodies is the burn this projection exists "
        "to prevent"
    )
    rendered = json.dumps(view)
    assert len(rendered) < 24000, (
        f"a ten-episode view rendered to {len(rendered)} chars; the window pack's evidence "
        "block clips by binary search and would silently cut the most recent episodes"
    )


# ── ruling 2 (2026-08-11): the fold MUST NOT FAIL ────────────────────────────


def _completed_window(window_id: str = "win-det") -> ReflectionWindow:
    """Ten closed episodes with the outcomes a deterministic verdict counts."""
    window = _fake_window(window_id)
    bodies = []
    for index, body in enumerate(window.episode_bodies):
        row = dict(body)
        row["intent"] = "LEARN" if index < 6 else "USE_TOOL"
        row["action"] = "web_fetch"
        if index >= 8:
            row["cascade_status"] = "failed"
        row["motor_attempt_assessment"] = {
            "domain": "python",
            "verification_verdict": "verified" if index < 7 else "unverified",
            "repeated_failure": index == 9,
            "execution_evidence": {"failure_kind": "tool_failed" if index >= 8 else ""},
        }
        bodies.append(row)
    window.episode_bodies[:] = bodies
    return window


def _window_episode(window: ReflectionWindow, **meta) -> SimpleNamespace:
    return SimpleNamespace(episode_id="ep-window", meta=dict(meta), _window_lane_window=window)


@pytest.mark.parametrize("fold_reflected, expected_source", [(True, "model"), (False, "deterministic")])
def test_the_window_commit_always_archives_a_verdict(window_state, intake_state, fold_reflected, expected_source):
    """Operator ruling 2026-08-11 (risk 6): "we have to fix it so it does not fail."

    The reflection is Omega's window-lane fold. A fold contained as a failed thought
    leaves no reflection, so the commit archives the counted verdict instead: a
    window that raised would stay staged and re-ignite into the same crash after
    every restart.
    """
    import cognition_chain as chain
    from memory.episode_sram import open_bucket
    from omega_loop.window.run import build_window_view, coerce_window_outcome

    window = _completed_window("win-always")
    window_lane.stage_locked_window(window, window_path=window_state.window_path)
    reflected = coerce_window_outcome(json.loads(_GOOD_REFLECTION), build_window_view(window))
    result = SimpleNamespace(window_reflection=reflected) if fold_reflected else None
    episode = _window_episode(window, omega_fold_failure="EpisodeCognitionParseError: prose")
    open_bucket("ep-window", pipeline_id=0)

    chain._commit_window_reflection(episode, result)

    outcome = window_lane.carried_outcome(episode)
    assert outcome["status"] == "reflected"
    assert outcome["window_id"] == "win-always"
    assert outcome["episode_count"] == WINDOW_SIZE
    # (d) the archive always says which mind produced it.
    assert outcome["reflection_source"] == expected_source
    assert str(outcome["reflection"]["summary"]).strip()
    assert episode.omega == outcome
    assert window_lane.reflection_committed(episode)
    archived = json.loads((window_state.archive_dir / "win-always.json").read_text())
    assert archived["omega_outcome"]["reflection_source"] == expected_source


def test_the_deterministic_verdict_is_derived_from_the_episodes_own_outcomes():
    """(c) honest because DERIVED. Every clause is a count off the ten closed
    episodes — cascade_status, motor verdicts, intents, actions — so nothing in
    it is a thought the mind did not have."""
    from omega_loop.window.run import deterministic_window_verdict

    outcome = deterministic_window_verdict(
        _completed_window(), reason="EpisodeCognitionNoOutputError: model produced no output"
    )
    reflection = outcome["reflection"]

    assert outcome["reflection_source"] == "deterministic"
    assert "NoOutput" in outcome["reflection_fallback_reason"]
    # 8 of 10 completed, 2 failed; 7 of 10 motor attempts verified, 1 repeating.
    assert "8 of 10 episodes completed" in reflection["summary"]
    assert "7 of 10 motor attempts verified" in reflection["summary"]
    assert "repeating a known failure" in reflection["summary"]
    assert any("LEARN" in row for row in reflection["emergent_observations"]["repeated_behaviors"])
    assert any(
        "tool_failed" in row for row in reflection["emergent_observations"]["stable_patterns"]
    )
    # It NEVER names a trajectory it has no basis to name. An empty update is
    # read by the trajectory phase as "this window changed nothing" — while
    # carrying the existing target forward would suppress its decay on evidence
    # no mind ever weighed.
    assert outcome["trajectory_update"] == {}
    assert reflection["trajectory"]["target_domain"] == ""
    assert reflection["reasoning"]["identity"] == ""


def test_an_unrecorded_cascade_status_is_never_counted_as_completed():
    """WO-11: the count defaulted an ABSENT ``cascade_status`` to ``"completed"``, so a body that
    never recorded its outcome was reported as one that finished — a completion nobody observed.
    An unrecorded status is unknown, and it is named as unknown."""
    from omega_loop.window.run import deterministic_window_verdict

    window = _fake_window("win-unrecorded", episodes=2)
    for body in window.episode_bodies:
        body.pop("cascade_status")

    summary = deterministic_window_verdict(window)["reflection"]["summary"]
    assert "2 of 2 episodes completed" not in summary, "an absent status was counted as completed"
    assert "0 of 2 episodes completed" in summary
    assert "2 did not record a cascade status" in summary


def test_an_unknown_cascade_status_is_reported_as_unknown_not_as_a_failure():
    from omega_loop.window.run import deterministic_window_verdict

    window = _fake_window("win-unknown", episodes=3)
    window.episode_bodies[0]["cascade_status"] = "unknown"
    window.episode_bodies[1]["cascade_status"] = "failed"

    outcome = deterministic_window_verdict(window)
    summary = outcome["reflection"]["summary"]
    assert "1 of 3 episodes completed" in summary
    assert "unknown×1" not in summary, "an unknown status was named as a failure kind"
    assert "failed×1" in summary
    assert "1 did not record a cascade status" in summary
    patterns = outcome["reflection"]["emergent_observations"]["stable_patterns"]
    assert not any("unknown" in row for row in patterns)


def test_a_window_with_no_episode_bodies_still_yields_a_verdict():
    """A window no fold can reflect still ends in an honest counted verdict."""
    from omega_loop.window.run import deterministic_window_verdict

    outcome = deterministic_window_verdict(_fake_window("win-empty", episodes=0))

    assert outcome["reflection_source"] == "deterministic"
    assert "no episode bodies" in outcome["reflection"]["summary"]


@pytest.mark.parametrize(
    "payload, rejection",
    [
        ({"reflection": {"reasoning": "sure thing"}}, "reflection.reasoning must be an object"),
        (
            {"reflection": {"summary": "", "reasoning": {"what": "", "why": ""}}},
            "nothing was reflected",
        ),
        (
            {"reflection": {"summary": "s", "reasoning": {"what": 3}}},
            "reflection.reasoning.what must be a string",
        ),
        (
            {
                "reflection": {"summary": "s", "reasoning": {"what": "w"}},
                "trajectory_update": {"target_domain": "python", "confidence": "high"},
            },
            "confidence must be a number",
        ),
        (
            {
                "reflection": {"summary": "s", "reasoning": {"what": "w"}},
                "trajectory_update": {"target_domain": "python", "confidence": 1.7},
            },
            "outside [0.0, 1.0]",
        ),
        (
            {
                "reflection": {"summary": "s", "reasoning": {"what": "w"}},
                "trajectory_update": {"target_capability": "async"},
            },
            "cannot be recorded",
        ),
        (
            {
                "reflection": {
                    "summary": "s",
                    "reasoning": {"what": "w"},
                    "trajectory": {"decision": "vibes"},
                }
            },
            "invalid reflection.trajectory.decision",
        ),
        (
            {
                "reflection": {
                    "summary": "s",
                    "reasoning": {"what": "w"},
                    "emergent_observations": {"repeated_behaviors": "not a list"},
                }
            },
            "must be a list of strings",
        ),
    ],
)
def test_the_schema_validator_names_the_violation_it_rejects(payload, rejection):
    """(a) schema-validated output, mirroring the strictest validator in the tree
    (``omega_loop.episode.prompts._parse_pressure_lifecycle``): every rejection
    NAMES the field, because that sentence is what the repair pass feeds back.

    Each of these passed the predecessor's two-key check and was archived —
    a reasoning that was a sentence, a confidence of "high" silently read as 0.0,
    a capability the trajectory store could never key.
    """
    from omega_loop.window.run import validate_window_payload

    with pytest.raises(ValueError, match=re.escape(rejection)):
        validate_window_payload(payload)


def test_a_failed_window_fold_still_archives_a_real_window(window_state, intake_state):
    """The whole no-fail chain at the seam that used to crash: a window fold
    contained as a failed thought still archives a verdict and unstages, so the
    staged file empties and the boot loop has nothing to re-ignite."""
    import cognition_chain as chain
    from memory.episode_sram import open_bucket

    window = _completed_window("win-hopeless")
    window_lane.stage_locked_window(window, window_path=window_state.window_path)
    open_bucket("ep-window", pipeline_id=0)

    chain._commit_window_reflection(
        _window_episode(window, omega_fold_failure="EpisodeCognitionNoOutputError: runtime down"),
        None,
    )

    archived = json.loads((window_state.archive_dir / "win-hopeless.json").read_text())
    assert archived["omega_outcome"]["status"] == "reflected"
    assert archived["omega_outcome"]["reflection_source"] == "deterministic"
    assert load_staged_windows(window_state.staged_path) == []
    assert window_lane.pressure_contributor(window_path=window_state.window_path) is None


# ── ruling 1 (2026-08-11): what a window fold may and may not consolidate ────


_MODEL_OUTCOME = {
    "status": "reflected",
    "window_id": "win-scope",
    "episode_count": WINDOW_SIZE,
    "reflection_source": "model",
    "reflection": {
        "summary": "Ten episodes circling one gap.",
        "reasoning": {
            "what": "looked things up",
            "why": "the same gap kept recurring",
            "identity": "prefers verified answers",
        },
        "emergent_observations": {
            "repeated_behaviors": ["reaches for recall before the web"],
            "stable_patterns": ["short answers"],
        },
        "trajectory": {
            "decision": "continue",
            "target_domain": "python",
            "target_capability": "async",
            "reason": "the gap keeps recurring",
        },
    },
    "trajectory_update": {
        "target_domain": "python",
        "target_capability": "async",
        "confidence": 0.6,
        "supporting_episode": "win-scope",
        "reason": "the gap keeps recurring",
    },
}


def _run_real_window_fold(tmp_path, monkeypatch, *, observed: bool = False):
    """Run the REAL window terminus over ten episodes that each carry live
    mastery/failure/semantic/identity/query evidence, and report what it wrote.

    ``observed=True`` first commits, into the episode's own bucket, the ``observed`` causal
    fact ``commit_reflection`` records when the window's archive lands (T11)."""
    import deepsleep_loop.consolidate_memory.run as consolidate_memory_run
    import deepsleep_loop.loop as ds_loop
    from deepsleep_loop.loop import run_deepsleep_for_window
    from memory.episode_sram import open_bucket
    from tests.core.deepsleep_window_fixtures import (
        episode_entry,
        patch_window_deepsleep_paths,
    )

    paths = patch_window_deepsleep_paths(monkeypatch, tmp_path)

    envelopes: list = []
    real_write = consolidate_memory_run._write_envelope
    monkeypatch.setattr(
        consolidate_memory_run,
        "_write_envelope",
        lambda envelope: envelopes.append(envelope) or real_write(envelope),
    )
    events: list[tuple] = []
    real_emit = ds_loop.emit_data
    monkeypatch.setattr(
        ds_loop,
        "emit_data",
        lambda loop, event, **fields: events.append((event, dict(fields)))
        or real_emit(loop, event, **fields),
    )

    bodies = [
        episode_entry(
            f"ep-{i}",
            intent="LEARN",
            target="Python",
            success=(i < 8),
            failure_kind=None if i < 8 else "tool_failed",
            knowledge_mastery=0.4,
        )
        for i in range(WINDOW_SIZE)
    ]
    window = ReflectionWindow(
        window_id="win-scope",
        started_at_episode="ep-0",
        trajectory_snapshot={"target_domain": "python"},
        episodes=[f"ep-{i}" for i in range(WINDOW_SIZE)],
        episode_bodies=bodies,
        status="locked",
        locked_at="2026-08-11T00:00:00Z",
    )

    store = EpisodeStore(
        reflection_window_path=tmp_path / "reflection_window.json",
        trajectory_path=tmp_path / "current_trajectory.json",
    )
    episode = store.open(pipeline_id=0, work_kind="reflection", opened_by=WINDOW_REFLECTION_READY)
    assert episode is not None and episode.work_kind == "reflection"
    open_bucket(episode.episode_id, pipeline_id=episode.pipeline_id)
    fact = None
    if observed:
        from memory.causal_edges import commit_causal_fact
        from memory.episode_sram import bind_episode

        with bind_episode(episode.episode_id, pipeline_id=episode.pipeline_id):
            fact = commit_causal_fact(episode, kind="observed", source=f"episode:{episode.episode_id}",
                                      target=f"window:{window.window_id}")

    pack = run_deepsleep_for_window(store, window, dict(_MODEL_OUTCOME))
    return SimpleNamespace(
        paths=paths,
        pack=pack,
        window=window,
        envelopes=envelopes,
        events=events,
        episode_id=episode.episode_id,
        fact=fact,
    )


def test_the_real_window_terminus_commits_the_observed_fact_from_the_episodes_bucket(tmp_path, monkeypatch):
    """T11 gate finding: the window episode's bucket is no longer empty at its terminus — it
    holds the ``observed`` fact. The REAL window-scope DeepSleep drains that bucket like any
    episode's: the fact's own journal record is committed to a canonical path, and nothing is
    left pending for ``_close_episode_sram`` to refuse."""
    from memory import episode_sram, intake_journal

    result = _run_real_window_fold(tmp_path, monkeypatch, observed=True)

    record = intake_journal.record_by_id(result.fact["journal_record_id"])
    assert record.status == intake_journal.COMMITTED and record.canonical_path
    assert record.owner_episode_id == result.episode_id
    assert episode_sram.pending(result.episode_id) == []


def test_a_window_fold_does_not_reconsolidate_what_each_episode_already_did(
    tmp_path, monkeypatch
):
    """Operator ruling 2026-08-11 (risk 7). Cut 10 ran the FULL twelve-phase
    terminus at window scope, on top of the ten per-episode termini that had each
    already consolidated their own evidence: a domain touched in five episodes was
    credited five times and then a sixth. Contradicts the 2026-08-03
    per-episode-only ruling, so the five stores must be untouched by a window fold.

    The control at the bottom is the point of the test: the SAME intake, run
    through the same phase directly, does write. The stores are empty because the
    window skipped them, not because the fixture had nothing to say.
    """
    from deepsleep_loop.failure_store import load_failures
    from deepsleep_loop.identity_store import load_identity
    from deepsleep_loop.mastery_store import load_mastery
    from deepsleep_loop.query_store import load_queries
    from deepsleep_loop.semantic_store import load_semantic

    result = _run_real_window_fold(tmp_path, monkeypatch)
    paths = result.paths

    assert load_mastery(path=paths["mastery_path"]) == {}
    assert load_failures(path=paths["failure_path"]) == {}
    assert load_semantic(path=paths["semantic_path"]) == {}
    assert load_identity(path=paths["identity_path"]) == {}
    assert load_queries(path=paths["query_path"]) == {}
    # ...and nothing they would have produced reached memory either.
    assert {envelope.type for envelope in result.envelopes}.isdisjoint(
        {"mastery_progress", "semantic_fact", "resolved_inquiry"}
    )

    # Telemetry is HONEST about it: five named skips, never silence.
    skipped = {
        event: fields.get("reason")
        for event, fields in result.events
        if event.endswith("_skipped")
    }
    assert skipped == {
        f"{phase}_skipped": "skipped:already_consolidated_per_episode"
        for phase in (
            "mastery",
            "failure",
            "semantic",
            "identity",
            "query_lifecycle",
        )
    }

    # THE CONTROL: the same window, handed straight to the phase, does consolidate.
    from deepsleep_loop import mastery as mastery_phase
    from deepsleep_loop.intake.run import build_deepsleep_input_for_window

    mastery_phase.update_mastery(build_deepsleep_input_for_window(result.window, _MODEL_OUTCOME))
    assert load_mastery(path=paths["mastery_path"])


def test_a_window_fold_commits_the_trajectory_and_its_own_synthesis(tmp_path, monkeypatch):
    """The other half of the ruling: what the window ALONE can know still lands.

    Trajectory is the window's whole purpose — no single episode can say where ten
    of them are heading — and the synthesis is what the mind concluded by looking
    across them, produced by no episode in the window. Both are real writes and
    both go through the ordinary commit region and its stamp (cut 13), which is
    why the region is not skipped along with the five phases.
    """
    from deepsleep_loop.trajectory_store import load_trajectory

    result = _run_real_window_fold(tmp_path, monkeypatch)

    trajectory = load_trajectory(path=result.paths["trajectory_path"])
    assert [record.target_domain for record in trajectory.values()] == ["python"]

    synthesis = [e for e in result.envelopes if e.type == "window_synthesis"]
    assert len(synthesis) == 1
    text = synthesis[0].payload["text"]
    assert "[model reflection over win-scope]" in text
    assert "Ten episodes circling one gap." in text
    assert "reaches for recall before the web" in text

    # The journal ROW is keyed by the WINDOW and labelled with its scope.
    rows = [
        json.loads(line)
        for line in result.paths["deepsleep_journal"].read_text().splitlines()
        if line.strip()
    ]
    assert rows[-1]["episode_id"] == "win-scope"
    assert rows[-1]["scope"] == "window"
    assert rows[-1]["mastery_effects"] == {}
    assert rows[-1]["trajectory_effects"]


def test_the_per_episode_terminus_still_runs_every_phase(tmp_path, monkeypatch):
    """The skip is scoped to the WINDOW and nowhere else. An ordinary episode's
    terminus is the one that consumes its own evidence, so it must be untouched."""
    from deepsleep_loop.loop import run_deepsleep_for_episode
    from deepsleep_loop.mastery_store import load_mastery
    from memory.episode_sram import open_bucket
    from tests.core.deepsleep_window_fixtures import (
        episode_entry,
        meta_b_reflection_for,
        patch_window_deepsleep_paths,
    )

    paths = patch_window_deepsleep_paths(monkeypatch, tmp_path)
    body = episode_entry("ep-solo", intent="LEARN", target="Python", knowledge_mastery=0.4)

    store = EpisodeStore()
    episode = store.open(pipeline_id=0, work_kind="autonomous", opened_by="curiosity_gap")
    episode.episode_id = "ep-solo"
    episode.meta = {
        "execution_action_pack": dict(
            (body.get("execution") or {}).get("execution_action_pack") or {}
        ),
        **meta_b_reflection_for(body),
    }
    episode.global_ = {
        "intent_decision": {"intent": body.get("intent")},
        "outcome": dict(body.get("outcome") or {}),
        "omega_result": body.get("omega_result"),
    }
    open_bucket("ep-solo", pipeline_id=episode.pipeline_id)

    run_deepsleep_for_episode(store)

    assert load_mastery(path=paths["mastery_path"])


# ── the commit point ─────────────────────────────────────────────────────────


def test_commit_archives_with_the_real_outcome_then_unstages(window_state, intake_state):
    from memory.episode_sram import open_bucket

    episode = SimpleNamespace(episode_id="ep-window", meta={})
    window = _fake_window("win-commit")
    window_lane.stage_locked_window(window, window_path=window_state.window_path)
    open_bucket("ep-window", pipeline_id=0)
    outcome = {"status": "reflected", "reflection": {"summary": "real"}}

    window_lane.commit_reflection(episode, window, outcome)

    archived = json.loads((window_state.archive_dir / "win-commit.json").read_text())
    # THE point of the cut: not "archived_by_per_episode_cycle".
    assert archived["omega_outcome"] == outcome
    assert archived["window"]["episodes"] == window.episodes
    assert load_staged_windows(window_state.staged_path) == []


def test_commit_refuses_to_unstage_when_NOTHING_could_be_archived(window_state, monkeypatch):
    """Dropping the staged row when no record landed anywhere would lose ten
    episodes with nothing to show for them. It still refuses — but it refuses
    TOTALLY (RISK-9): no raise, so the window cannot re-ignite into the same
    exception forever."""
    import pressure_loop.reflection_window as rw

    monkeypatch.setattr(rw, "archive_reflection_window", lambda *a, **k: None)
    monkeypatch.setattr(rw, "archive_failed_reflection_window", lambda *a, **k: None)
    window = _fake_window("win-lost")
    window_lane.stage_locked_window(window, window_path=window_state.window_path)
    episode = SimpleNamespace(episode_id="ep", meta={})

    assert window_lane.commit_reflection(episode, window, {}) is False
    assert [w.window_id for w in load_staged_windows(window_state.staged_path)] == ["win-lost"]
    # The episode closes through the ORDINARY per-episode terminus, which is
    # what stops the window-scope consolidation from doubling on the retry.
    assert window_lane.reflection_committed(episode) is False


def test_an_unwritable_archive_does_not_crash_restage_crash(
    window_state, intake_state, monkeypatch, served_episode_ids
):
    """The re-ignition loop RISK-9 closes, driven end to end.

    A permanently broken archive volume used to mean: commit raises, the window
    stays staged, the next tick's contributor re-ignites the same window, commit
    raises again — forever, and each turn burned a full model pass. Ten attempts
    here must produce ten quiet failures, not one exception."""
    import pressure_loop.reflection_window as rw

    real_write = rw._write_archive_payload
    volume_broken = {"yes": True}
    monkeypatch.setattr(
        rw,
        "_write_archive_payload",
        lambda *a, **k: None if volume_broken["yes"] else real_write(*a, **k),
    )
    from memory.episode_sram import open_bucket

    window = _fake_window("win-unwritable")
    window_lane.stage_locked_window(window, window_path=window_state.window_path)
    served_episode_ids.update(window.episodes)
    open_bucket("ep", pipeline_id=0)
    open_bucket("ep-heal", pipeline_id=0)

    for _ in range(10):
        assert (
            window_lane.pressure_contributor(window_path=window_state.window_path) is not None
        ), "the window must stay staged so a recovered volume still gets it"
        committed = window_lane.commit_reflection(
            SimpleNamespace(episode_id="ep", meta={}), window, {"status": "reflected"}
        )
        assert committed is False

    # ...and it self-heals the moment the volume comes back.
    volume_broken["yes"] = False
    assert window_lane.commit_reflection(
        SimpleNamespace(episode_id="ep-heal", meta={}), window, {"status": "reflected"}
    )
    assert window_lane.pressure_contributor(window_path=window_state.window_path) is None


def test_an_unarchivable_payload_falls_back_to_an_honest_failed_archive(
    window_state, intake_state, monkeypatch
):
    """The middle outcome, and the one that actually breaks the loop: the FULL
    record cannot be written, a small honest one can. The window is unstaged on
    the strength of that record — nothing is lost with no trace, and the next
    tick has nothing to re-ignite."""
    import pressure_loop.reflection_window as rw

    from memory.episode_sram import open_bucket

    monkeypatch.setattr(rw, "archive_reflection_window", lambda *a, **k: None)
    window = _fake_window("win-degraded")
    window_lane.stage_locked_window(window, window_path=window_state.window_path)
    episode = SimpleNamespace(episode_id="ep-degraded", meta={})
    open_bucket("ep-degraded", pipeline_id=0)

    assert window_lane.commit_reflection(episode, window, {"status": "reflected"}) is True

    archived = json.loads((window_state.archive_dir / "win-degraded.json").read_text())
    assert archived["archive_status"] == "failed"
    assert archived["window"]["episodes"] == window.episodes
    assert archived["archive_failure_reason"]
    # Unstaged -> no re-ignition. This is the bound on the loop.
    assert load_staged_windows(window_state.staged_path) == []
    assert window_lane.pressure_contributor(window_path=window_state.window_path) is None
    # The reflection outcome is real and still consolidates; the record says
    # plainly that its archive is degraded.
    assert window_lane.reflection_committed(episode) is True
    record = episode.meta[window_lane.WINDOW_META_KEY]
    assert record["archive_degraded"] is True


def test_a_half_written_archive_is_never_counted_as_landed(window_state, monkeypatch):
    """The old check was ``is_file()`` after a function that returned nothing.
    An in-place write that dies mid-file passes that test and loses the window
    anyway. The archive publishes through os.replace and is read back."""
    import pressure_loop.reflection_window as rw

    real_replace = __import__("os").replace

    def _die_before_publish(src, dst):
        # Simulate the volume filling exactly at publish time.
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("os.replace", _die_before_publish)
    window = _fake_window("win-torn")
    assert rw.archive_reflection_window(window, omega_outcome={"status": "reflected"}) is None
    assert not (window_state.archive_dir / "win-torn.json").exists()
    # No temp debris left behind either.
    assert list(window_state.archive_dir.glob(".*")) == []

    monkeypatch.setattr("os.replace", real_replace)
    assert rw.archive_reflection_window(window, omega_outcome={"status": "reflected"}) is not None


def test_a_crash_before_the_commit_leaves_the_window_staged_for_a_clean_retry(
    window_state, intake_state, served_episode_ids
):
    """Exactly-once, stated where it is actually decided. A reflection that dies
    before ``commit_reflection`` leaves the row staged, so the next tick's
    contributor re-ignites it — and the retry's archive OVERWRITES rather than
    duplicating, because the archive is keyed by window_id."""
    from memory.episode_sram import open_bucket

    window = _fake_window("win-retry")
    window_lane.stage_locked_window(window, window_path=window_state.window_path)
    served_episode_ids.update(window.episodes)
    open_bucket("ep-2", pipeline_id=0)

    # attempt 1 dies inside the fold
    assert window_lane.pressure_contributor(window_path=window_state.window_path) is not None
    # attempt 2 completes
    window_lane.commit_reflection(
        SimpleNamespace(episode_id="ep-2", meta={}), window, {"status": "reflected"}
    )
    assert window_lane.pressure_contributor(window_path=window_state.window_path) is None
    assert len(list(window_state.archive_dir.glob("*.json"))) == 1


def test_the_staged_file_is_never_readable_half_written(window_state, monkeypatch):
    """The staged FIFO is read FAIL-HARD, on every single tick, by
    ``gather_contributors`` — and it is written by a lane thread with no lock
    between them. An in-place truncating write therefore has two ways to brick
    the appliance: a power cut leaves a half-file that raises on every later
    tick forever, and even without a crash the tick can read the file mid-write.
    The write must be a temp sibling plus ``os.replace``, so a reader sees the
    old complete file or the new one and never anything between.
    """
    import pressure_loop.reflection_window as rw

    import pressure_loop.durable_write as durable

    rw.save_staged_windows([_fake_window("win-old")], window_state.staged_path)
    seen: list[list[str]] = []
    real = durable.write_text_atomic

    def _observing_write(path, text):
        # Whatever a reader does WHILE the write is in flight must still parse.
        seen.append([w.window_id for w in rw.load_staged_windows(window_state.staged_path)])
        real(path, text)

    monkeypatch.setattr(durable, "write_text_atomic", _observing_write)
    rw.save_staged_windows(
        [_fake_window("win-old"), _fake_window("win-new")], window_state.staged_path
    )

    assert seen == [["win-old"]]  # the PREVIOUS complete file, never a fragment
    assert [w.window_id for w in rw.load_staged_windows(window_state.staged_path)] == [
        "win-old",
        "win-new",
    ]


def test_a_stage_and_an_unstage_at_the_same_instant_lose_nothing(window_state, monkeypatch):
    """The two halves of the double buffer run on DIFFERENT lane threads.

    Staging happens inside a closing episode's terminus — chat, agent or meta,
    under the cognition lock — while the unstage is the window lane's commit
    point, taken in the unlocked model-pass middle where the fold spends its
    seconds. Both are read-modify-writes of one small file, and unsynchronized
    they corrupt it in both directions: the stage's stale read RESURRECTS the
    window the unstage just committed (reflected and window-consolidated twice —
    the exact mastery double-count the commit point exists to prevent), or the
    unstage's stale read DROPS the window that was just staged (ten closed
    episodes with no record anywhere, and the collecting half has moved on).

    The save is slowed here so the interleaving is not a matter of luck.
    """
    import threading
    import time

    import pressure_loop.reflection_window as rw

    reflected = _fake_window("win-reflected")
    just_locked = _fake_window("win-just-locked")
    rw.save_staged_windows([reflected], window_state.staged_path)

    real_save = rw.save_staged_windows

    def _slow_save(windows, path=None):
        time.sleep(0.05)
        real_save(windows, path)

    monkeypatch.setattr(rw, "save_staged_windows", _slow_save)

    start = threading.Barrier(2)
    errors: list[BaseException] = []

    def _run(fn):
        def _body():
            start.wait()
            try:
                fn()
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        return _body

    threads = [
        threading.Thread(
            target=_run(
                lambda: rw.unstage_reflection_window(
                    "win-reflected", window_state.staged_path
                )
            )
        ),
        threading.Thread(
            target=_run(
                lambda: rw.stage_reflection_window(just_locked, window_state.staged_path)
            )
        ),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert not errors
    staged = rw.load_staged_windows(window_state.staged_path)
    assert [w.window_id for w in staged] == ["win-just-locked"]


# ── the store's two buffers ──────────────────────────────────────────────────


def test_get_locked_window_and_archive_release_only_the_staged_half(window_state):
    """``archive_and_reset_window`` used to clear the ACTIVE window file. With the
    double buffer that file now holds a different, still-collecting window —
    clearing it would throw away every episode closed since the lock."""
    store = _store(window_state)
    _fill_window(store)
    eleventh = _close_one(store)

    window = store.consume_window_ready()
    assert window is not None
    assert window.episode_count == WINDOW_SIZE
    assert store.window_ready is False  # consumed

    store.archive_and_reset_window(window, omega_outcome={"status": "reflected"})

    assert load_staged_windows(window_state.staged_path) == []
    collecting = load_reflection_window(window_state.window_path)
    assert collecting is not None
    assert collecting.episodes == [eleventh]  # the fresh half SURVIVED
    assert store.consume_window_ready() is None


def test_window_lane_episodes_are_excluded_from_windows(window_state):
    """A reflection must never become part of the next thing reflected on."""
    store = _store(window_state)
    _close_one(store)

    store.open(pipeline_id=0, work_kind="reflection", opened_by=WINDOW_REFLECTION_READY)
    reflection_episode = store.current()
    assert reflection_episode is not None
    assert reflection_episode.work_kind == "reflection"
    store.close()

    collecting = load_reflection_window(window_state.window_path)
    assert collecting is not None
    assert collecting.episode_count == 1
    assert reflection_episode.episode_id not in collecting.episodes


# ── the kill switch ──────────────────────────────────────────────────────────


def test_kill_switch_archives_unreflected_verbatim(window_state, monkeypatch):
    """OMEGA_WINDOW_LANE=0 restores the pre-cut-10 record exactly: the locked
    window is filed away under the same outcome string, nothing is staged, and
    no window contributor is ever minted."""
    monkeypatch.setenv("OMEGA_WINDOW_LANE", "0")
    store = _store(window_state)
    ids = _fill_window(store)

    assert load_staged_windows(window_state.staged_path) == []
    archives = list(window_state.archive_dir.glob("*.json"))
    assert len(archives) == 1
    payload = json.loads(archives[0].read_text())
    assert payload["omega_outcome"] == {"status": "archived_by_per_episode_cycle"}
    assert payload["window"]["episodes"] == ids
    assert window_lane.pressure_contributor(window_path=window_state.window_path) is None
    assert store.get_locked_window() is None


def test_kill_switch_drains_windows_staged_before_the_flip(window_state, monkeypatch):
    """The cut-9 review's finding, applied before it could recur: a switch that
    only stopped minting would strand work already minted. Flipping this one
    DRAINS the staged backlog instead of leaving it unreflected forever."""
    window_lane.stage_locked_window(
        _fake_window("win-orphan"), window_path=window_state.window_path
    )

    monkeypatch.setenv("OMEGA_WINDOW_LANE", "0")
    store = _store(window_state)
    _fill_window(store)

    assert load_staged_windows(window_state.staged_path) == []
    archived = json.loads((window_state.archive_dir / "win-orphan.json").read_text())
    assert archived["omega_outcome"] == {"status": "archived_by_per_episode_cycle"}


def test_the_kill_switch_never_drops_a_window_it_could_not_archive(
    window_state, monkeypatch
):
    """The safety lever must not be the thing that destroys the evidence.

    ``archive_reflection_window`` is failure-soft and returns ``Path | None``.
    This drain ignored that and cleared the staging file unconditionally, so on
    a full or failing volume every staged window vanished with no record
    anywhere — the exact loss RISK-9 refused to accept at ``commit_reflection``,
    still reachable through the switch an operator flips when something is
    ALREADY wrong.

    A row whose archive did not land stays staged and is owed to the next boot.
    """
    import pressure_loop.reflection_window as rw

    for wid in ("win-lands", "win-fails"):
        window_lane.stage_locked_window(
            _fake_window(wid), window_path=window_state.window_path
        )

    real = rw.archive_reflection_window

    def _flaky(window, **kwargs):
        if getattr(window, "window_id", "") == "win-fails":
            return None  # the volume refused this one
        return real(window, **kwargs)

    monkeypatch.setattr(rw, "archive_reflection_window", _flaky)
    monkeypatch.setenv("OMEGA_WINDOW_LANE", "0")
    store = _store(window_state)
    _fill_window(store)

    still = load_staged_windows(window_state.staged_path)
    assert [w.window_id for w in still] == ["win-fails"], (
        "a window that did not archive was dropped from staging anyway — it is "
        "now lost with no record on disk"
    )
    # And the one that DID land was unstaged, so the drain still makes progress.
    assert (window_state.archive_dir / "win-lands.json").is_file()


def test_the_kill_switch_drain_still_clears_when_everything_archives(
    window_state, monkeypatch
):
    """The ordinary path is unchanged: nothing owed, nothing left staged."""
    for wid in ("win-a", "win-b"):
        window_lane.stage_locked_window(
            _fake_window(wid), window_path=window_state.window_path
        )
    monkeypatch.setenv("OMEGA_WINDOW_LANE", "0")
    _fill_window(_store(window_state))

    assert load_staged_windows(window_state.staged_path) == []
    assert (window_state.archive_dir / "win-a.json").is_file()
    assert (window_state.archive_dir / "win-b.json").is_file()


def test_the_consumption_side_is_never_gated_on_the_switch(window_state, monkeypatch):
    """An episode already dispatched must always find its work — the asymmetry
    that keeps a mid-flight reflection from being orphaned by a switch flip."""
    window_lane.stage_locked_window(
        _fake_window("win-inflight"), window_path=window_state.window_path
    )
    monkeypatch.setenv("OMEGA_WINDOW_LANE", "0")

    episode = SimpleNamespace(episode_id="ep-inflight", meta={}, pipeline_id=0, work_kind="reflection", opened_by=WINDOW_REFLECTION_READY)
    import pressure_loop.reflection_window as rw
    from pressure_loop import temporal_store

    monkeypatch.setattr(rw, "staged_window_path", lambda p=None: window_state.staged_path)
    hydration_calls: list[str] = []

    def _hydrate(window):
        hydration_calls.append(window.window_id)
        window.episode_bodies = [
            {"episode_id": episode_id, "intent": "LEARN"}
            for episode_id in window.episodes
        ]
        return window

    monkeypatch.setattr(temporal_store, "hydrate_reflection_window", _hydrate)
    carried = window_lane.restore_window(episode)
    assert carried is not None
    assert carried.window_id == "win-inflight"
    assert hydration_calls == ["win-inflight"]
    assert len(carried.episode_bodies) == WINDOW_SIZE
    assert window_lane.is_window_episode(episode)


def test_a_window_episode_with_no_staged_window_fails_hard(window_state, monkeypatch):
    """Fail-hard is law. The staged file and the ignition disagreeing is a
    programming error, not a race to paper over."""
    import pressure_loop.reflection_window as rw

    monkeypatch.setattr(rw, "staged_window_path", lambda p=None: window_state.staged_path)
    episode = SimpleNamespace(episode_id="ep-empty", meta={}, pipeline_id=0, work_kind="reflection", opened_by=WINDOW_REFLECTION_READY)
    with pytest.raises(RuntimeError, match="no staged reflection window"):
        window_lane.restore_window(episode)


def test_restore_is_a_no_op_for_every_other_lane(window_state):
    episode = SimpleNamespace(episode_id="ep-chat", meta={}, pipeline_id=0, work_kind="operator_turn", opened_by="operator_query")
    assert window_lane.restore_window(episode) is None
    assert window_lane.is_window_episode(episode) is False


# ── T11: the reflection OBSERVED its window (cross-lane spec section 9) ──────


@pytest.fixture
def intake_state(tmp_path, monkeypatch):
    """The window episode's own intake bucket, over an isolated journal: the fact it commits
    is an intake like any other and must never reach the live journal."""
    from memory import episode_sram, intake_journal

    monkeypatch.setattr(intake_journal, "journal_root", lambda: tmp_path / "intake")
    intake_journal.reset_cache_for_tests()
    episode_sram.reset()
    yield intake_journal
    episode_sram.reset()
    intake_journal.reset_cache_for_tests()


def test_a_committed_reflection_records_that_it_observed_the_window(window_state, intake_state):
    from memory.causal_edges import causal_facts
    from memory.episode_sram import open_bucket, pending

    episode = SimpleNamespace(episode_id="ep-window", meta={})
    window = _fake_window("win-observed")
    window_lane.stage_locked_window(window, window_path=window_state.window_path)
    open_bucket("ep-window", pipeline_id=0)              # as ignition does, before the fold

    window_lane.commit_reflection(episode, window, {"status": "reflected", "reflection": {"summary": "s"}})

    [fact] = causal_facts(episode)
    assert (fact["kind"], fact["source"], fact["target"]) == (
        "observed", "episode:ep-window", "window:win-observed")
    assert fact["evidence_event_id"] == "episode-ep-window"
    assert fact["detail"]["episode_count"] == int(window.episode_count)
    record = intake_state.record_by_id(fact["journal_record_id"])
    assert record.status == intake_state.PENDING and record.owner_episode_id == "ep-window"
    assert [i.journal_record_id for i in pending("ep-window")] == [record.record_id], \
        "held in the window episode's bucket: DeepSleep's drain commits it"


def test_a_reflection_outside_any_bucket_propagates_the_fact_failure(window_state, intake_state):
    """The observed edge is required window-lane output. If SRAM intake is unavailable, the
    archive may already have landed but the failure still has to survive the commit seam."""
    from memory.causal_edges import CausalFactRefused, causal_facts

    episode = SimpleNamespace(episode_id="ep-window", meta={})
    window = _fake_window("win-observed")
    window_lane.stage_locked_window(window, window_path=window_state.window_path)

    with pytest.raises(CausalFactRefused, match="bucket"):
        window_lane.commit_reflection(episode, window, {"status": "reflected"})

    assert causal_facts(episode) == [] and intake_state.records() == ()
    assert episode.meta[window_lane.WINDOW_META_KEY]["archived"] is True


def test_a_reflection_that_could_not_be_archived_observed_nothing(window_state, intake_state, monkeypatch):
    import pressure_loop.reflection_window as rw
    from memory.causal_edges import causal_facts
    from memory.episode_sram import open_bucket

    monkeypatch.setattr(rw, "archive_reflection_window", lambda *a, **k: None)
    monkeypatch.setattr(rw, "archive_failed_reflection_window", lambda *a, **k: None)
    window = _fake_window("win-lost")
    window_lane.stage_locked_window(window, window_path=window_state.window_path)
    episode = SimpleNamespace(episode_id="ep", meta={})
    open_bucket("ep", pipeline_id=0)

    assert window_lane.commit_reflection(episode, window, {}) is False
    assert causal_facts(episode) == [] and intake_state.records() == ()
