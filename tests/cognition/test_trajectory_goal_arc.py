"""Goal-directed trajectory arc — learn until ready, then yield to apply.

Operator design 2026-07-08: the trajectory is a future APPLICATION goal, not
a permanent LEARN lock. The mind pursues LEARN on the goal domain only until
knowledge is high enough to APPLY it; once learned-enough, the trajectory
stops driving LEARN (the practice/apply path takes the domain, and LEARN
focus rotates to what is not yet learned). When the application goal itself
is met, the trajectory is achieved and no longer drives topic selection.
This de-biases the old lock where one high-confidence domain captured every
LEARN forever (the 'python' lock, live 2026-07-08).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import cognition.topic_resolution.resolve_topic as rt  # noqa: E402
from deepsleep_loop._deepsleep_state import MasteryRecord  # noqa: E402


class _State:
    meta = {}
    mi = None
    target_str = ""
    text = ""
    episode_id = "ep-arc"


def _wire(monkeypatch, domain, *, knowledge, application, target=0.7):
    rec = MasteryRecord(
        domain_id=domain,
        domain_name=domain.replace("-", " "),
        knowledge_mastery=knowledge,
        application_mastery=application,
        target_mastery=target,
    )
    monkeypatch.setattr(rt, "load_mastery", lambda *a, **k: {domain: rec})
    monkeypatch.setattr(
        rt,
        "read_trajectory",
        lambda *a, **k: {"target_domain": domain.replace("-", " ")},
    )


def test_learning_phase_trajectory_still_drives(monkeypatch):
    # knowledge below the apply-bar -> keep pursuing LEARN (convergence holds)
    _wire(monkeypatch, "async-python", knowledge=0.3, application=0.0)
    resolved = rt._resolve_trajectory(_State())
    assert resolved is not None
    assert resolved.source == "trajectory"


def test_learned_enough_yields_learn(monkeypatch):
    # knowledge past the apply-bar -> STOP driving LEARN; the apply path takes
    # the domain and LEARN rotates to what is not yet learned
    _wire(monkeypatch, "python", knowledge=0.6, application=0.05)
    assert rt._resolve_trajectory(_State()) is None


def test_goal_met_retires_trajectory(monkeypatch):
    # application goal reached -> trajectory achieved, drives nothing
    _wire(monkeypatch, "python", knowledge=0.9, application=0.75, target=0.7)
    assert rt._resolve_trajectory(_State()) is None


def test_full_cascade_rotates_off_learned_domain(monkeypatch):
    # python learned-enough + a fresh 0.0 domain -> LEARN rotates to the fresh
    # one (mastery-gap bootstrap) instead of re-locking python
    py = MasteryRecord(
        domain_id="python",
        domain_name="python",
        knowledge_mastery=0.6,
        application_mastery=0.05,
        target_mastery=0.7,
    )
    fresh = MasteryRecord(
        domain_id="caching",
        domain_name="caching",
        knowledge_mastery=0.0,
        application_mastery=0.0,
        target_mastery=0.7,
    )
    monkeypatch.setattr(rt, "load_mastery", lambda *a, **k: {"python": py, "caching": fresh})
    monkeypatch.setattr(rt, "read_trajectory", lambda *a, **k: {"target_domain": "python"})
    resolved = rt.resolve_learning_topic(_State())
    assert resolved.topic == "caching", "LEARN must rotate to the unlearned domain"
    assert resolved.source == "mastery_gap"
