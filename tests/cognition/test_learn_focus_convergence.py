"""Convergence fix: the mind's TRAJECTORY drives the LEARN focus, not the largest-gap
rescan — so learning stays stable on the mind's chosen subtopic until mastery (the live
hop bug). The mastery-gap rescan is BOOTSTRAP-only (cold start, no trajectory yet),
seeding from the operator's onboarded mastery topics.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import cognition.topic_resolution.resolve_topic as rt  # noqa: E402
from deepsleep_loop._deepsleep_state import MasteryRecord  # noqa: E402


class _State:
    mi = None
    meta = None


def _mastery(monkeypatch, records):
    monkeypatch.setattr(rt, "load_mastery", lambda *a, **k: records)


def _trajectory(monkeypatch, target_domain):
    monkeypatch.setattr(
        rt,
        "read_trajectory",
        lambda *a, **k: {"target_domain": target_domain} if target_domain else {},
    )
    # no critical open questions -> plain trajectory topic
    monkeypatch.setattr(rt, "_pick_critical_open_question", lambda *a, **k: None)


_GAPS = {
    "sysdesign": MasteryRecord(
        domain_id="sysdesign",
        domain_name="system design",
        knowledge_mastery=0.1,
        target_mastery=0.7,
    ),
    "async-python": MasteryRecord(
        domain_id="async-python",
        domain_name="async python",
        knowledge_mastery=0.4,
        target_mastery=0.7,
    ),
}


# ── the mind's trajectory drives once set ────────────────────────────────────
def test_trajectory_beats_mastery_gap(monkeypatch):
    _mastery(monkeypatch, _GAPS)  # largest gap = system design (0.1)
    _trajectory(monkeypatch, "async python")  # but the mind set its focus to async python
    resolved = rt.resolve_learning_topic(_State())
    assert resolved.topic == "async python"  # the MIND's subtopic wins, not the largest gap
    assert resolved.source == "trajectory"


def test_trajectory_stable_not_largest_gap(monkeypatch):
    # even though system-design has the larger gap, the mind's trajectory holds the focus
    _mastery(monkeypatch, _GAPS)
    _trajectory(monkeypatch, "async python")
    for _ in range(3):  # stable across ticks (no hop)
        assert rt.resolve_learning_topic(_State()).topic == "async python"


# ── bootstrap: no trajectory -> mastery_gap seeds from onboarded topics ──────
def test_mastery_gap_bootstraps_when_no_trajectory(monkeypatch):
    _mastery(monkeypatch, _GAPS)
    _trajectory(monkeypatch, "")  # cold start: no trajectory set
    # lower the severe-gap threshold so the bootstrap fires in the test
    monkeypatch.setattr(rt, "_severe_mastery_gap_threshold", lambda: 0.1)
    resolved = rt.resolve_learning_topic(_State())
    assert resolved.source == "mastery_gap"  # bootstraps from the onboarded mastery list
    assert resolved.topic in ("system design", "async python")
