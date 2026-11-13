"""Self-directed cascades pace themselves between phases (operator 2026-09-22).

Measured live: Omega's own successors ran their phases back to back with no gap, so a chain of
them hammered the one cognition route continuously — seven successors holding pipelines 18-50 s
each, and an operator "hello" pushed from 7.5 s to 28.8 s to its first word.

The operator's instruction: "put a pause between the autonomous loop cascade phases so it isn't
swamping the model repeatedly one after another ... Global A, few second pause, Meta A, few second
pause, etc."

Two rules this must never break:

* **An operator turn is never paced.** Somebody is waiting for it. The pause applies only to work
  Omega chose for itself.
* **A pause is never taken while holding a commit region or a substrate seam.** It goes at the
  phase handoff, where the cascade is between phases and holds nothing but its own lane.
"""

from __future__ import annotations

import pytest

import cognition_chain as cc


class _Ep:
    def __init__(self, work_kind: str, opened_by: str = "curiosity"):
        self.episode_id = "ep-pace-1"
        self.work_kind = work_kind
        self.opened_by = opened_by


@pytest.fixture(autouse=True)
def _no_real_sleeping(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(cc.time, "sleep", lambda s: slept.append(float(s)))
    yield slept


def test_an_operator_turn_is_never_paced(_no_real_sleeping):
    cc.pace_autonomous_phase(_Ep("operator_turn", opened_by="operator_query"), "meta_a")
    assert _no_real_sleeping == [], "somebody is waiting for an operator turn"


def test_self_directed_work_pauses_between_phases(monkeypatch, _no_real_sleeping):
    # The suite zeroes the pace so thousands of autonomous-cascade tests do not sleep for hours
    # (tests/conftest.py); this test is the one that asserts the production value.
    monkeypatch.delenv("OMEGA_AUTONOMOUS_PHASE_PAUSE_S", raising=False)
    cc.pace_autonomous_phase(_Ep("omega_successor"), "meta_a")
    assert _no_real_sleeping == [cc.DEFAULT_AUTONOMOUS_PHASE_PAUSE_S]
    assert cc.DEFAULT_AUTONOMOUS_PHASE_PAUSE_S >= 1.0, "a 'few second pause' is seconds, not none"


def test_the_pause_is_configurable_and_zero_disables_it(monkeypatch, _no_real_sleeping):
    monkeypatch.setenv("OMEGA_AUTONOMOUS_PHASE_PAUSE_S", "0")
    cc.pace_autonomous_phase(_Ep("omega_successor"), "auto")
    assert _no_real_sleeping == [], "zero means no pacing, for a benchmark or a test"

    monkeypatch.setenv("OMEGA_AUTONOMOUS_PHASE_PAUSE_S", "2.5")
    cc.pace_autonomous_phase(_Ep("omega_successor"), "auto")
    assert _no_real_sleeping == [2.5]


def test_a_nonsense_pause_setting_falls_back_to_the_default_rather_than_hanging(monkeypatch):
    monkeypatch.setenv("OMEGA_AUTONOMOUS_PHASE_PAUSE_S", "forever")
    assert cc.autonomous_phase_pause_seconds() == cc.DEFAULT_AUTONOMOUS_PHASE_PAUSE_S

    monkeypatch.setenv("OMEGA_AUTONOMOUS_PHASE_PAUSE_S", "-5")
    assert cc.autonomous_phase_pause_seconds() == 0.0, "a negative pause is no pause, never a wait"

    monkeypatch.setenv("OMEGA_AUTONOMOUS_PHASE_PAUSE_S", "9999")
    assert cc.autonomous_phase_pause_seconds() <= cc.MAX_AUTONOMOUS_PHASE_PAUSE_S, (
        "a pause long enough to look like a hang is capped"
    )


def test_pacing_never_raises_on_an_episode_it_cannot_read(_no_real_sleeping):
    class _Odd:
        def __getattr__(self, name):
            raise RuntimeError("unreadable episode")

    cc.pace_autonomous_phase(_Odd(), "omega")
    assert _no_real_sleeping == [], "an unreadable episode is not paced, and nothing raises"


def test_the_cascade_paces_every_phase_after_the_first():
    """The pause is taken at the handoffs, not inside a phase, and not before Global A — the
    first phase of a self-directed episode has nothing to space itself from."""
    import inspect

    source = inspect.getsource(cc)
    for phase in ("meta_a", "auto", "omega", "deepsleep"):
        assert f'pace_autonomous_phase(episode, "{phase}")' in source, phase
    assert 'pace_autonomous_phase(episode, "global_a")' not in source
