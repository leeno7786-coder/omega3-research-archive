"""Step 5a of the metacognitive pressure evolution — the learn->apply bleed SUBSTRATE.

A MEASURED learning-progress plateau signal (recent knowledge_delta -> ~0, §6.1) over the
per-domain delta buffer. DeepSleep's mastery pass keeps the buffer, reads the plateau and
emits it as telemetry. The per-episode prompt advisory that once consumed a projection of it
rendered only into a prompt half no live lane sent, and was retired (2026-09-21).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deepsleep_loop._deepsleep_state import MasteryRecord  # noqa: E402
from pressure_loop import learning_progress as lp  # noqa: E402


# ── the delta buffer + MasteryRecord round-trip ──────────────────────────────
def test_mastery_record_round_trips_new_delta_fields():
    rec = MasteryRecord(
        domain_id="ai",
        domain_name="AI",
        knowledge_mastery=0.7,
        evidence_count=4,
        recent_knowledge_deltas=(0.3, 0.05, 0.01),
        last_plateau_flag=True,
        last_plateau_episode="ep9",
    )
    again = MasteryRecord.from_dict(rec.to_dict())
    assert again.recent_knowledge_deltas == (0.3, 0.05, 0.01)
    assert again.last_plateau_flag is True and again.last_plateau_episode == "ep9"


def test_old_mastery_dict_loads_with_defaults():
    # an OLD mastery.json record (no Step-5 keys) must load unchanged
    old = {"domain_id": "ai", "domain_name": "AI", "knowledge_mastery": 0.5}
    rec = MasteryRecord.from_dict(old)
    assert rec.recent_knowledge_deltas == () and rec.last_plateau_flag is False


def test_append_delta_caps_and_evicts_oldest():
    buf = ()
    for v in (0.5, 0.4, 0.3, 0.2, 0.1, 0.05, 0.01):
        buf = lp.append_delta(buf, v, cap=5)
    assert len(buf) == 5
    assert buf == (0.3, 0.2, 0.1, 0.05, 0.01)  # oldest two evicted
    # garbage value -> buffer unchanged, never raises
    assert lp.append_delta(buf, "notanumber", cap=5) == buf


# ── the plateau predicate (v1 heuristic) ─────────────────────────────────────
def test_plateau_true_when_recent_improvement_flat():
    facts = lp.plateau_from((0.4, 0.01, 0.005, 0.008), sample_count=4)
    assert facts["plateau"] is True
    assert facts["knowledge_improving"] is False


def test_plateau_false_when_still_improving():
    facts = lp.plateau_from((0.01, 0.2, 0.15), sample_count=3)
    assert facts["plateau"] is False
    assert facts["knowledge_improving"] is True  # latest delta clearly positive


def test_plateau_false_before_k_min_samples():
    facts = lp.plateau_from((0.005, 0.004), sample_count=2)  # < K_MIN deltas
    assert facts["plateau"] is False  # cold start is inert (no premature bleed)


def test_plateau_decay_terms_track_sample_count():
    f1 = lp.plateau_from((), sample_count=1)
    f9 = lp.plateau_from((), sample_count=9)
    # novelty + uncertainty both DECAY as the domain is sampled more (§6.1)
    assert f9["novelty_decay"] < f1["novelty_decay"]
    assert f9["uncertainty"] < f1["uncertainty"]
    assert f1["uncertainty"] == pytest.approx(1.0)  # 1/sqrt(1)
    # n=0 safety branch: no 0**negative / div-by-zero -> neutral 1.0 (cold start)
    f0 = lp.plateau_from((), sample_count=0)
    assert f0["novelty_decay"] == 1.0 and f0["uncertainty"] == 1.0
