"""Step 0 of the metacognitive pressure evolution (RED → GREEN).

Two parts, per docs/architecture/metacognitive-pressure-evolution.md §7 Step 0:

  (1) Route the orphan ``application_gap`` contributor → USE_SKILL (it once
      dead-ended at the catch-all reflect intent, since removed and collapsed to
      IDLE), so a learned-but-unapplied reseed signal actually opens a USE_SKILL
      episode. Post-flip
      (OMEGA_USE_SKILL_ROUTING default-on) the legacy APPLY action is RETIRED.

  (2) The 0.60 operational practice band (operator-ratified, clean switch),
      behind ``OPERATIONAL_PRACTICE_BAND`` (shipped default OFF at Step 0 =
      byte-identical; FLIPPED ON at Step 5b — the keystone — so a learned domain
      leaves learning_debt and APPLY can win):
      partition each domain into EXACTLY ONE contributor by knowledge —
        knowledge < 0.60          → learning_debt (learning)
        0.60 <= knowledge < target → application_gap/execution_gap (practice)
        knowledge >= target        → execution_gap (full application)
      No LEARN/APPLY double-count. Omega (Step 5) later owns the within-band
      learn-vs-practice split via learning-progress.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pressure_loop.pressure_field as pf  # noqa: E402
from global_loop.intent.run import _map_opened_by_to_intent, run as global_intent_run  # noqa: E402
from pressure_loop.contributors import EpisodeIgnition, make_contributor  # noqa: E402


def _rec(knowledge: float, application: float, target: float = 0.7, domain_id=None):
    return SimpleNamespace(
        knowledge_mastery=knowledge,
        application_mastery=application,
        target_mastery=target,
        domain_id=domain_id,
    )


def _patch_mastery(monkeypatch, records: dict) -> None:
    monkeypatch.setattr("deepsleep_loop.mastery_store.load_mastery", lambda *a, **k: records)


# ── (1) intent route: application_gap → USE_SKILL ────────────────────────
def test_application_gap_routes_to_use_skill():
    # Post-flip (OMEGA_USE_SKILL_ROUTING default-on): the learned-but-unapplied
    # reseed signal opens a USE_SKILL episode — the legacy APPLY action is RETIRED.
    assert _map_opened_by_to_intent("application_gap", "") == "USE_SKILL"


def test_application_gap_mints_practice_objective_before_use_skill_guard(monkeypatch):
    minted = {"called": False}

    def _mint():
        minted["called"] = True
        return True

    monkeypatch.setattr("pressure_loop.practice_mint.ensure_practice_objective", _mint)
    contributor = make_contributor(
        "autonomous",
        "application_gap",
        0.5,
        {"domain": "python"},
    )
    ignition = EpisodeIgnition(
        episode_id="episode-application-gap",
        aggregate_pressure=0.5,
        opened_by="application_gap",
        dominant_contributor=contributor,
        contributors=(contributor,),
        trajectory_snapshot=None,
    )

    assert global_intent_run(ignition) == "USE_SKILL"
    assert minted["called"] is True


def test_execution_gap_still_use_skill():
    assert _map_opened_by_to_intent("execution_gap", "") == "USE_SKILL"


def test_learning_debt_still_learn():
    assert _map_opened_by_to_intent("learning_debt", "") == "LEARN"


# ── (2) the operational constant ─────────────────────────────────────────
def test_operational_constant_is_060():
    assert pf.OPERATIONAL_MASTERY == 0.60


def test_band_default_on_since_5b():
    # CONSCIOUSLY FLIPPED at Step 5b (the keystone): a learned domain
    # ([OPERATIONAL, target)) now LEAVES learning_debt and JOINS application_gap so
    # APPLY can win. Set OPERATIONAL_PRACTICE_BAND=False to revert to the target-based
    # partition. The band-OFF/ON tests below monkeypatch explicitly, so they verify
    # BOTH partitions regardless of this shipped default.
    assert pf.OPERATIONAL_PRACTICE_BAND is True


# ── (2) band OFF (default) == today's target-based partition ─────────────
def test_band_off_domain_in_band_is_learning_not_application(monkeypatch):
    monkeypatch.setattr(pf, "OPERATIONAL_PRACTICE_BAND", False)
    _patch_mastery(monkeypatch, {"d": _rec(0.62, 0.0)})  # in [0.60, target)
    assert pf._mastery_gap_count() == 1  # still counts as learning
    assert pf._application_gap_domains() == []  # not yet a practice candidate


# ── (2) band ON == clean switch at 0.60 ──────────────────────────────────
def test_band_on_domain_in_band_is_application_not_learning(monkeypatch):
    monkeypatch.setattr(pf, "OPERATIONAL_PRACTICE_BAND", True)
    _patch_mastery(monkeypatch, {"d": _rec(0.62, 0.0)})
    assert pf._application_gap_domains() == ["d"]  # now a practice candidate
    assert pf._mastery_gap_count() == 0  # removed from learning


def test_band_on_below_operational_still_learning(monkeypatch):
    monkeypatch.setattr(pf, "OPERATIONAL_PRACTICE_BAND", True)
    _patch_mastery(monkeypatch, {"d": _rec(0.4, 0.0)})
    assert pf._mastery_gap_count() == 1
    assert pf._application_gap_domains() == []


def test_band_on_above_target_still_application(monkeypatch):
    monkeypatch.setattr(pf, "OPERATIONAL_PRACTICE_BAND", True)
    _patch_mastery(monkeypatch, {"d": _rec(0.9, 0.2)})
    assert pf._application_gap_domains() == ["d"]
    assert pf._mastery_gap_count() == 0


def test_band_on_already_applied_excluded(monkeypatch):
    monkeypatch.setattr(pf, "OPERATIONAL_PRACTICE_BAND", True)
    _patch_mastery(monkeypatch, {"d": _rec(0.62, 0.75)})  # application >= target
    assert pf._application_gap_domains() == []


def test_band_on_partition_is_disjoint(monkeypatch):
    monkeypatch.setattr(pf, "OPERATIONAL_PRACTICE_BAND", True)
    _patch_mastery(
        monkeypatch,
        {
            "learn": _rec(0.40, 0.0),  # < 0.60 → learning only
            "practice": _rec(0.62, 0.0),  # band → application only
            "apply": _rec(0.90, 0.2),  # >= target → application only
        },
    )
    gaps = set(pf._application_gap_domains())
    assert gaps == {"practice", "apply"}
    assert pf._mastery_gap_count() == 1  # only "learn"


# ── Canonical application-gap predicate ──────────────────────────────────────
# DeepSleep mastery records are context for Omega, not automatic pressure
# producers. The live field reader owns this deterministic classification.


def test_has_application_gap_shared_predicate_band_on(monkeypatch):
    monkeypatch.setattr(pf, "OPERATIONAL_PRACTICE_BAND", True)
    # the finding's drift example: learned past the operational floor but applied
    # below target → IS a gap (the old reseed band wrongly skipped it).
    assert pf.has_application_gap(0.65, 0.90, 0.50) is True
    # below the operational floor (band ON) → not yet a practice candidate
    assert pf.has_application_gap(0.40, 0.90, 0.10) is False
    # application already at/above target → applied, no gap
    assert pf.has_application_gap(0.95, 0.90, 0.92) is False


def test_has_application_gap_shared_predicate_band_off(monkeypatch):
    monkeypatch.setattr(pf, "OPERATIONAL_PRACTICE_BAND", False)
    # band OFF: the lower bound is `target`, so [operational, target) is learning,
    # not application — the drift example is NOT a gap here.
    assert pf.has_application_gap(0.65, 0.90, 0.50) is False
    # knowledge at/above target with application below → a gap regardless of band
    assert pf.has_application_gap(0.95, 0.90, 0.50) is True
