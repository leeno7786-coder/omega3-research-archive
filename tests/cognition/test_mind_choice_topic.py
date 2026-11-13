"""Mind-choice topic selection — the mind decides, not an automatic picker.

Operator directive 2026-07-08: "make sure it isnt automatic — let it see that
it mastered something and empty categories etc so it can choose and decide
pressure." The mind sees its full mastery map, then RAISES learning pressure
(mastery_debt_delta, domain-scoped) for the domain IT picks. That explicit,
domain-scoped choice drives the LEARN topic — ABOVE the convergence trajectory
and ABOVE the deterministic mastery-gap picker, which is now only a cold-start
bootstrap for when the mind has made no choice at all.
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
    episode_id = "ep-mind-choice"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    yield


def _records(monkeypatch, **domains):
    recs = {
        d: MasteryRecord(
            domain_id=d,
            domain_name=d.replace("-", " "),
            knowledge_mastery=k,
            application_mastery=a,
            target_mastery=0.7,
        )
        for d, (k, a) in domains.items()
    }
    monkeypatch.setattr(rt, "load_mastery", lambda *a, **k: recs)


def test_mastery_gap_still_bootstraps_cold_start(monkeypatch):
    # no mind choice, no trajectory -> the deterministic picker bootstraps
    _records(monkeypatch, caching=(0.0, 0.0))
    monkeypatch.setattr(rt, "read_trajectory", lambda *a, **k: {})
    resolved = rt.resolve_learning_topic(_State())
    assert resolved.source == "mastery_gap"
    assert resolved.topic == "caching"
