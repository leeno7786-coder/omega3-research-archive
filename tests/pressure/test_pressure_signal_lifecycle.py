"""One pressure lifecycle: emitted, lives, dies at ignition (operator 2026-08-15).

"there should be no minted anything for pressure, there should only be the
deterministic decisions that the system calls, that's what the model needs to
emit. and then it needs to die on episode ignition so omega can either emit
another pressure signal or go idle."

Before this, pressure had TWO lifecycles. Setpoints (omega_levels) persisted
until admitted and died at EpisodeStore.open — correct. Staged signals lived in
a list that every gather drained read-and-clear, so a signal died by being
LOOKED AT: it got exactly one contest, and if it lost, it was destroyed unheard.

That is not theoretical. On 2026-08-15 a closing fold minted
``python_syntax_fundamentals_content_gap`` at 0.850, which collided with the
standing ``mastery_gap`` at 0.850. The ignition gate refused the tie
(winner_margin 0.05, gap 0.000 -> ambiguous_winner), nothing ignited, the signal
was already drained, and autonomous cognition parked for 7m23s.
"""

from __future__ import annotations

import pytest

from pressure_loop import pressure
from pressure_loop.contributors import make_contributor


@pytest.fixture(autouse=True)
def _clean_staging():
    with pressure._RESEED_LOCK:
        pressure._RESEED_STAGING.clear()
    yield
    with pressure._RESEED_LOCK:
        pressure._RESEED_STAGING.clear()


def _signal(ctype="mastery_gap", weight=0.85, domain="python"):
    return make_contributor("omega", ctype, weight, {"domain": domain})


# ── the signal lives until ignition ─────────────────────────────────────────


def test_observation_never_consumes_a_signal():
    """THE regression: a gather used to destroy what it merely looked at."""
    pressure.stage_reseed([_signal()])

    for _ in range(5):
        assert len(pressure._read_reseed_staging()) == 1

    assert len(pressure.staged_signals()) == 1


def test_a_signal_that_loses_a_contest_survives_to_the_next_one():
    """The 7m23s stall, as a test.

    A signal that does not win its first contest must still be there for the
    second. Losing a tie is not consent to be forgotten.
    """
    signal = _signal()
    pressure.stage_reseed([signal])

    pressure._read_reseed_staging()  # contest 1: collides, nothing ignites
    survivors = pressure._read_reseed_staging()  # contest 2

    assert [c.type for c in survivors] == ["mastery_gap"]
    assert survivors[0].weight == pytest.approx(0.85)


def test_a_signal_dies_at_ignition_and_only_there():
    signal = _signal()
    pressure.stage_reseed([signal])

    assert pressure.release_staged(signal) is True
    assert pressure.staged_signals() == ()
    # Idempotent: a stale contributor from a superseded ignition is a no-op.
    assert pressure.release_staged(signal) is False


def test_release_matches_on_identity_not_on_object():
    """The ignition carries a REBUILT contributor, not the staged instance."""
    pressure.stage_reseed([_signal(weight=0.85)])

    rebuilt = make_contributor("omega", "mastery_gap", 0.42, {"domain": "python"})
    assert pressure.release_staged(rebuilt) is True
    assert pressure.staged_signals() == ()


# ── one signal per identity ─────────────────────────────────────────────────


def test_a_newer_emission_replaces_the_same_signal():
    """(type, domain) is the identity — the contract omega_levels documents."""
    pressure.stage_reseed([_signal(weight=0.5)])
    pressure.stage_reseed([_signal(weight=0.9)])

    live = pressure.staged_signals()
    assert len(live) == 1
    assert live[0].weight == pytest.approx(0.9)


def test_different_domains_are_different_signals():
    pressure.stage_reseed([_signal(domain="python")])
    pressure.stage_reseed([_signal(domain="rust")])

    assert len(pressure.staged_signals()) == 2


def test_a_zero_weight_emission_is_not_a_signal():
    pressure.stage_reseed([_signal(weight=0.0)])
    assert pressure.staged_signals() == ()


# ── no minting: a closing fold does not author a rival ──────────────────────


def test_emergent_pressure_does_not_stage_a_rival_contributor():
    """A fold's emergent pressure is MEMORY, not a competing drive.

    It used to enter the very next gather under a fold-authored name at a
    fold-authored urgency, contending with the standing drive it was supposed to
    be a consequence of.
    """
    import inspect

    from pressure_loop import emergent_pressure

    source = inspect.getsource(emergent_pressure)
    assert "stage_reseed" not in source


# ── the loop does not rest on a mind that still wants to act ────────────────


def test_pressure_signature_tracks_emission_and_ignition():
    from pressure_loop import loop

    quiet = loop._pressure_signature()

    signal = _signal()
    pressure.stage_reseed([signal])
    live = loop._pressure_signature()
    assert live != quiet, "an emitted signal must change the picture"

    # Stable while nothing changes, or the loop would re-pass forever.
    assert loop._pressure_signature() == live

    pressure.release_staged(signal)
    assert loop._pressure_signature() == quiet


def test_a_raised_weight_is_a_changed_picture():
    """Re-emitting the SAME signal louder must earn another pass."""
    from pressure_loop import loop

    pressure.stage_reseed([_signal(weight=0.5)])
    before = loop._pressure_signature()

    pressure.stage_reseed([_signal(weight=0.9)])
    assert loop._pressure_signature() != before
