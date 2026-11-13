"""The return channel: what Omega concludes must change what Omega does next.

This is the project's core thesis stated as a test. A system that learns from what it does, and
alters its decisions from that learning, needs exactly one thing these tests assert: a window
reflection that names a real target must reach the slot the NEXT ignition reads.

What already exists, verified in live code before writing any of this:

* the window produces ``trajectory_update`` and it is committed to the journal and
  ``episode.omega`` (``cognition_chain._commit_window_reflection``);
* ``deepsleep_loop/trajectory/run.py::maintain_trajectory`` upserts a real trajectory store from
  it — fail-hard, inside ``store_transaction``, decaying and superseding as it goes;
* ``schema/omega_trajectory.py`` is a complete lifecycle API (``make_trajectory``,
  ``supersede_trajectory``, ``decrement_trajectory``, ``is_active``);
* ``pressure_loop/pressure.py:1990`` reads ``LAST_OMEGA_TRAJECTORY`` into the next ignition's
  ``trajectory_snapshot``, which reaches Meta A through ``auto_loop/intake``.

What does not exist:

* nothing reads ``ProjectionState.trajectory_projection`` — it is built and consumed by nobody;
* nothing writes ``LAST_OMEGA_TRAJECTORY`` from the trajectory store. Its only live writers are
  ``pressure_loop/operator_pressure.py`` (an operator directive) and
  ``hud/operator_onboarding.py`` (onboarding).

So today the operator sets Omega's learning target, Omega reflects, writes down what it learned,
and the conclusion dies in the journal. ``operator_pressure``'s own docstring already describes
the intended repair — the canonical trajectory_store path "republishes the SS slot" — which also
settles precedence: superseding the operator's single-cycle seed is the designed behaviour, not
an escalation invented here.
"""

from __future__ import annotations

import pytest


def _slot():
    """The live SharedState instance, reached the way production reaches it.

    ``core.shared_state`` is a MODULE; the slot lives on a ``SharedState`` instance, which both
    ``pressure_loop/pressure.py`` and ``deepsleep_loop/loop.py`` obtain through
    ``get_default_runtime().shared_state``. Reading the module would have tested nothing.
    """
    from pressure_loop.runtime import get_default_runtime

    return get_default_runtime().shared_state


@pytest.fixture
def clean_trajectory(tmp_path, monkeypatch):
    """An isolated trajectory store and an isolated shared-state slot.

    A FRESH ``SharedState`` is registered for the test rather than clearing the live slot: this
    key is operator-facing guidance, and a test that writes it directly would be steering the
    real Omega. The previous runtime is restored afterwards.
    """
    from core.shared_state import SharedState
    from pressure_loop import runtime as pressure_runtime

    store = tmp_path / "trajectory.json"
    monkeypatch.setattr(
        "pressure_loop.policy.deepsleep_trajectory_path", lambda: store, raising=False
    )

    previous = pressure_runtime.try_get_default_runtime()
    pressure_runtime.set_default_runtime(
        pressure_runtime.PressureRuntime(shared_state=SharedState())
    )
    try:
        yield store
    finally:
        if previous is not None:
            pressure_runtime.set_default_runtime(previous)


def _ds_input(episode_id: str, update: dict):
    """A DeepSleepInput carrying one window reflection's trajectory_update.

    Window-level by construction, which is correct rather than incidental: the
    ``trajectory_update`` is produced by the ten-episode reflection, and ``episode_id`` /
    ``omega_outcome`` are read-only aliases of ``window_id`` / ``window_reflection``.
    """
    from deepsleep_loop._deepsleep_state import DeepSleepInput

    return DeepSleepInput(
        window_id=episode_id,
        window_reflection={"trajectory_update": dict(update)},
        episode_window=[episode_id],
        trajectory_snapshot={},
    )


REAL_UPDATE = {
    "target_domain": "python_web_builds",
    "target_capability": "finishing a CRUD route before claiming it is done",
    "confidence": 0.82,
    "reason": "ten episodes planned the same CRUD work and touched no file",
}


def test_a_window_conclusion_reaches_the_slot_the_next_ignition_reads(clean_trajectory):
    """The thesis. Omega concludes something; the next episode starts from it."""
    from core.shared_state_keys import LAST_OMEGA_TRAJECTORY
    from deepsleep_loop.trajectory.run import maintain_trajectory

    records = maintain_trajectory(_ds_input("ep-1", REAL_UPDATE))
    assert records, "the store upsert itself must have happened"

    published = _slot().read(LAST_OMEGA_TRAJECTORY, None)

    assert published is not None, (
        "the window's conclusion never reached LAST_OMEGA_TRAJECTORY, which is the ONLY "
        "trajectory input pressure_loop/pressure.py:1990 reads. Omega can learn and record "
        "what it learned, but nothing it learns can change what it does next"
    )
    assert published["target_domain"] == REAL_UPDATE["target_domain"]
    assert published["target_capability"] == REAL_UPDATE["target_capability"]


def test_the_published_trajectory_is_what_the_next_ignition_actually_carries(clean_trajectory):
    """Not just written — READ BACK through the real pressure path.

    Asserting the slot alone would prove the write, not the loop. This drives the same read
    ``evaluate`` performs, so a shape the validator accepts but pressure cannot use still fails.
    """
    from deepsleep_loop.trajectory.run import maintain_trajectory

    maintain_trajectory(_ds_input("ep-1", REAL_UPDATE))

    from core.shared_state_keys import LAST_OMEGA_TRAJECTORY
    from schema.omega_trajectory import OmegaTrajectory

    raw = _slot().read(LAST_OMEGA_TRAJECTORY, None)
    assert raw is not None
    trajectory = OmegaTrajectory.from_dict(raw)

    assert trajectory.is_active(), (
        "the published trajectory is not active, so the next ignition would carry a trajectory "
        "that guides nothing"
    )
    assert trajectory.target_domain == REAL_UPDATE["target_domain"]


def test_a_window_that_names_no_target_publishes_nothing(clean_trajectory):
    """An empty conclusion must not erase a standing trajectory.

    The window policy tells the model to leave the target empty when it has no honest basis for
    one, and an empty target already means "this window changed nothing" downstream. Publishing
    an empty trajectory would turn 'I learned nothing this window' into 'forget what you were
    working on', which is a louder claim than the reflection made.
    """
    from core.shared_state_keys import LAST_OMEGA_TRAJECTORY
    from deepsleep_loop.trajectory.run import maintain_trajectory

    maintain_trajectory(_ds_input("ep-1", REAL_UPDATE))
    standing = _slot().read(LAST_OMEGA_TRAJECTORY, None)
    assert standing is not None, "precondition: something is standing"

    records = maintain_trajectory(_ds_input("ep-2", {"target_domain": "", "confidence": 0.0}))

    assert records == [], "an empty target must not upsert a record"
    after = _slot().read(LAST_OMEGA_TRAJECTORY, None)
    assert after == standing, (
        "a window that concluded nothing overwrote the standing trajectory; 'I learned nothing' "
        "is not 'forget what you were working on'"
    )


def test_a_reinforced_target_is_deepened_and_a_new_one_is_explored(clean_trajectory):
    """The one judgement in this wire, made from the record's own facts.

    ``OmegaTrajectory.intent`` is a four-value enum and the window emits a different vocabulary
    (``establish|continue|adjust|complete``), so there is no direct source. Rather than map two
    unrelated vocabularies and call it forced, the intent is derived from what the STORE says
    happened: a first-time target is exploration, a reinforced one is deepening. Both are
    statements the record supports.
    """
    from core.shared_state_keys import LAST_OMEGA_TRAJECTORY
    from deepsleep_loop.trajectory.run import maintain_trajectory

    maintain_trajectory(_ds_input("ep-1", REAL_UPDATE))
    first = _slot().read(LAST_OMEGA_TRAJECTORY, None)
    assert first["intent"] == "explore_domain", (
        f"a first-time target was published as {first['intent']!r}; nothing yet supports a claim "
        "of deepening"
    )

    maintain_trajectory(_ds_input("ep-2", REAL_UPDATE))
    second = _slot().read(LAST_OMEGA_TRAJECTORY, None)
    assert second["intent"] == "deepen_capability", (
        "the same target reinforced by a second window is still reported as first-time "
        "exploration, so repeated evidence carries no more weight than a single window"
    )


def test_the_published_record_carries_its_supporting_episode(clean_trajectory):
    """A trajectory with no provenance cannot be audited or disbelieved.

    The operator has to be able to ask "why am I being steered here?" and get an episode back.
    """
    from core.shared_state_keys import LAST_OMEGA_TRAJECTORY
    from deepsleep_loop.trajectory.run import maintain_trajectory

    maintain_trajectory(_ds_input("ep-provenance", REAL_UPDATE))
    published = _slot().read(LAST_OMEGA_TRAJECTORY, None)

    assert published["rationale"], "a published trajectory with no rationale steers silently"
    blob = repr(published)
    assert "ep-provenance" in blob or REAL_UPDATE["reason"][:20] in blob, (
        "the published trajectory names neither the episode that produced it nor the window's "
        "own reason, so nobody can trace why Omega is being steered"
    )


# ── The durable half: the record Omega's OWN prompt reads ────────────────────
#
# Found while tracing consumers after the slot was wired. There are three trajectory locations,
# and publishing to the slot reached only two of the three readers:
#
#   deepsleep_loop/state/trajectory.json   — the store; read by DeepSleep only
#   LAST_OMEGA_TRAJECTORY shared-state slot — the next ignition -> Global B's alignment score,
#                                             Meta A / AUTO intake
#   lobe-9 canonical record at (9,2)        — omega_loop/episode/packet._trajectory_influencer_block,
#                                             i.e. Omega's per-episode prompt influencer
#
# The third is the one that matters most for the thesis, because Omega is where "what happens
# next" is chosen (AGENTS.md section 3) and that influencer is how the trajectory reaches it.
# Its only production writer was hud/operator_onboarding. So with the slot alone, Omega's own
# prompt would still only ever see what the operator set at onboarding.
#
# operator_pressure's docstring names both halves of the intended step: the canonical path
# "writes the durable identity-lobe record + republishes the SS slot". This pins the first.


def test_the_durable_record_omegas_own_prompt_reads_is_written(clean_trajectory):
    """Through the real intake, the real DeepSleep drain, and Omega's real reader."""
    from deepsleep_loop.loop import _flush_episode_sram
    from deepsleep_loop.trajectory.run import maintain_trajectory
    from memory import episode_sram
    from omega_loop.episode.packet import _trajectory_influencer_block

    episode_sram.open_bucket("ep-window-1", pipeline_id=0)
    maintain_trajectory(_ds_input("ep-window-1", REAL_UPDATE))
    # The same drain DeepSleep runs after every producer has placed its envelopes in SRAM.
    _flush_episode_sram("ep-window-1")

    influencer = _trajectory_influencer_block()

    assert influencer.get("active_domain") == REAL_UPDATE["target_domain"], (
        "Omega's per-episode prompt influencer does not carry the window's conclusion. The slot "
        "reaches pressure and the ignition, but Omega — the phase that chooses what happens next "
        "— reads a lobe-9 record whose only writer was onboarding, so its own learning still "
        f"cannot reach its own decisions. Influencer read: {influencer!r}"
    )
    assert influencer.get("active_goal") == REAL_UPDATE["target_capability"]
    assert influencer.get("reason"), "the durable record carries no rationale, so it steers silently"


def test_a_thread_that_owns_no_episode_publishes_the_slot_but_writes_no_memory(clean_trajectory):
    """The durable write goes through intake_memory into the owning episode's SRAM bucket.

    A thread that owns no bucket has nowhere lawful to put a memory record: writing around SRAM
    would be the second memory lane AGENTS.md section 5 forbids. That is an honest absence, the
    same split WO-05 landed at the tool seam — and it keeps every existing maintain_trajectory
    test, none of which opens a bucket, working unchanged.
    """
    from core.shared_state_keys import LAST_OMEGA_TRAJECTORY
    from deepsleep_loop.trajectory.run import maintain_trajectory
    from memory import episode_sram
    from omega_loop.episode.packet import _trajectory_influencer_block

    episode_sram.reset()
    assert episode_sram.bound_episode_id() is None, "precondition: this thread owns nothing"

    records = maintain_trajectory(_ds_input("ep-unowned", REAL_UPDATE))

    assert records, "the store upsert must still happen"
    assert _slot().read(LAST_OMEGA_TRAJECTORY, None) is not None, (
        "the slot is shared state, not memory, and must still be published"
    )
    assert _trajectory_influencer_block() == {}, (
        "a memory record was written with no owning episode, which can only have gone around "
        "the single memory lane"
    )


# ── The last hop: from the durable record to the choice Omega makes ─────────
#
# Operator, 2026-09-21: "the ten windows purpose is purely for trajectory decisions and those
# decisions should affect omegas overall intent choices based on what was observed." The durable
# record above reached Omega's packet only as raw JSON: the agent and browser lanes dump
# foldback_packet into their evidence, the chat lane shows it nowhere, and the formatted
# TRAJECTORY INFLUENCE line rode a system half the live agent lane discards (since retired).
# This pins the whole
# chain through the real code: window conclusion -> DeepSleep publish + drain -> Omega's packet
# -> the CONDUCT guidance in the prompt the next episode chooses its intent from.


def test_the_windows_conclusion_reaches_the_prompt_the_next_intent_is_chosen_from(
    clean_trajectory, monkeypatch
):
    from deepsleep_loop.loop import _flush_episode_sram
    from deepsleep_loop.trajectory.run import maintain_trajectory
    from memory import episode_sram
    from omega_loop.episode.packet import _assemble_foldback_packet
    from organ.promptorgan.packs.foldback import foldback_spec_for
    from tests.omega_loop.test_foldback_prompt_dispatch import packet_fixture

    # The domain has no mastery record: a trajectory Omega has not started yet is phase "new".
    monkeypatch.setattr(
        "cognition.topic_resolution.resolve_topic.load_mastery", lambda path=None: {}
    )

    episode_sram.open_bucket("ep-window-1", pipeline_id=0)
    maintain_trajectory(_ds_input("ep-window-1", REAL_UPDATE))
    _flush_episode_sram("ep-window-1")

    foldback = _assemble_foldback_packet(object(), {}, None, None, "")
    packet = packet_fixture("chat", foldback_packet=foldback)
    system, _user = foldback_spec_for(packet).build(packet)

    assert foldback["conduct"]["domain"] == REAL_UPDATE["target_domain"], (
        f"the window's conclusion did not reach Omega's packet: {foldback['conduct']!r}"
    )
    assert "CONDUCT" in system and REAL_UPDATE["target_domain"] in system, (
        "the window's conclusion reached the packet but not the prompt Omega chooses its next "
        "intent from, so what it observed still cannot change what it does"
    )
    assert "LANES THAT FIT THIS PHASE: web fetch (intent=LEARN)" in system
