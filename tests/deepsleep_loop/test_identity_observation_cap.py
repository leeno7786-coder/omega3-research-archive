"""Stored supporting trait observations are capped per trait (live 2026-09-06).

219 trait-observation records held 19 distinct texts (one repeated 52 times) and
102 were minted in one hour of chat: every DeepSleep re-observed the same
already-promoted traits. The vote is decided at promote_evidence (floor 2); past
the cap a supporting observation adds a file and nothing else. Tensions are
always recorded — they can demote.
"""

from __future__ import annotations

from deepsleep_loop.identity.run import SUPPORTING_OBSERVATION_CAP, observation_capped


def test_supporting_observations_stop_at_the_cap():
    counts = {"factually_anchored_identity": SUPPORTING_OBSERVATION_CAP}
    assert observation_capped("factually_anchored_identity", "new", counts) is True
    assert observation_capped("factually_anchored_identity", "confirmed", counts) is True


def test_below_the_cap_and_unknown_traits_are_recorded():
    counts = {"factually_anchored_identity": SUPPORTING_OBSERVATION_CAP - 1}
    assert observation_capped("factually_anchored_identity", "new", counts) is False
    assert observation_capped("brand_new_trait", "new", counts) is False


def test_tensions_are_always_recorded():
    counts = {"factually_anchored_identity": SUPPORTING_OBSERVATION_CAP * 3}
    assert observation_capped("factually_anchored_identity", "tensions_with_prior", counts) is False


def test_the_cap_clears_the_promotion_floor():
    from pressure_loop.policy import deepsleep_identity_promote_evidence

    assert SUPPORTING_OBSERVATION_CAP >= deepsleep_identity_promote_evidence() * 2
