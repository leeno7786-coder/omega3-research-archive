"""Autonomous practice mint — application mastery becomes real.

The seam: learned-but-unapplied + warm substrate -> deterministic minted
objective in episode SRAM (origin omega_practice) -> the existing
build machinery carries it -> a battery-verified completion grounds an
application_mastery delta. One build at a time; kill-switched; the
grounding never fires for unknown domains.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pressure_loop.practice_mint as pm  # noqa: E402
from deepsleep_loop._deepsleep_state import MasteryRecord  # noqa: E402
from memory import episode_sram  # noqa: E402
from memory.build_objective import (  # noqa: E402
    BuildObjective,
    current_build_objective,
    hold_build_objective,
)


def _rec(
    domain_id: str, *, knowledge=0.8, application=0.0, target=0.7, evidence=3
) -> MasteryRecord:
    return MasteryRecord(
        domain_id=domain_id,
        domain_name=domain_id.replace("-", " ").title(),
        knowledge_mastery=knowledge,
        application_mastery=application,
        confidence=0.6,
        evidence_count=evidence,
        target_mastery=target,
    )


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    episode_sram.reset()
    episode_sram.open_bucket("ep-practice", pipeline_id=0)
    monkeypatch.delenv("OMEGA_PRACTICE_ENABLED", raising=False)
    # The mints sweep workspace leftovers before a fresh objective (2026-07-10);
    # tests must never port the REAL workspace — stub it, record invocations.
    import core.workspace_port as wp

    swept: list = []
    monkeypatch.setattr(
        wp,
        "sweep_workspace_for_fresh_build",
        lambda **kw: swept.append(kw) or {"ported": False, "skipped": "test"},
    )
    yield swept
    episode_sram.reset()


def _hold_objective(objective: str, origin: str, practice_domain: str = "") -> None:
    hold_build_objective(
        BuildObjective(
            episode_id="ep-practice",
            objective=objective,
            origin_opened_by=origin,
            practice_domain=practice_domain,
        )
    )


def _ready_world(monkeypatch, records):
    monkeypatch.setattr("deepsleep_loop.mastery_store.load_mastery", lambda path=None: records)
    monkeypatch.setattr(
        "pressure_loop.pressure_field._application_gap_domains",
        lambda: [
            k
            for k, r in records.items()
            if r.knowledge_mastery >= r.target_mastery and r.application_mastery < r.target_mastery
        ],
    )
    monkeypatch.setattr(
        "memory.symphony_strength.hot_count_for_domain",
        lambda domain_id, strength_index=None: 5,
    )


def test_mint_holds_practice_objective(monkeypatch):
    _ready_world(monkeypatch, {"asyncio-basics": _rec("asyncio-basics")})
    assert pm.ensure_practice_objective() is True
    objective = current_build_objective()
    assert objective is not None
    assert objective.origin_opened_by == "omega_practice"
    assert objective.practice_domain == "asyncio-basics"
    assert "Asyncio Basics" in objective.objective
    assert "test" in objective.objective.lower(), "objective must demand proof"


def test_mint_never_preempts_a_real_build(monkeypatch):
    _ready_world(monkeypatch, {"asyncio-basics": _rec("asyncio-basics")})
    _hold_objective("operator build in flight", "operator_query")
    assert pm.ensure_practice_objective() is False
    assert current_build_objective().origin_opened_by == "operator_query"


def test_mint_noop_without_ready_domain(monkeypatch):
    # still learning: knowledge below target -> not practice-ready
    _ready_world(monkeypatch, {"caching": _rec("caching", knowledge=0.3)})
    assert pm.practice_ready_domain() is None
    assert pm.ensure_practice_objective() is False
    assert pm.mint_for_prepared_apply() is False
    assert current_build_objective() is None


def test_practice_ready_domain_propagates_mastery_lookup_failure(monkeypatch):
    _ready_world(monkeypatch, {"asyncio-basics": _rec("asyncio-basics")})
    failure = RuntimeError("mastery store unavailable")

    def fail_load(*, path=None):
        raise failure

    monkeypatch.setattr("deepsleep_loop.mastery_store.load_mastery", fail_load)
    with pytest.raises(RuntimeError, match="mastery store unavailable") as raised:
        pm.practice_ready_domain()
    assert raised.value is failure


def test_mint_propagates_objective_persistence_failure(monkeypatch):
    _ready_world(monkeypatch, {"asyncio-basics": _rec("asyncio-basics")})
    failure = RuntimeError("episode SRAM unavailable")

    def fail_hold(*args, **kwargs):
        raise failure

    monkeypatch.setattr("memory.build_objective.hold_build_objective", fail_hold)
    with pytest.raises(RuntimeError, match="episode SRAM unavailable") as raised:
        pm.ensure_practice_objective()
    assert raised.value is failure


def test_prepared_apply_mint_propagates_mastery_lookup_failure(monkeypatch):
    _ready_world(monkeypatch, {"asyncio-basics": _rec("asyncio-basics")})
    failure = RuntimeError("prepared mastery lookup failed")

    def fail_load(*, path=None):
        raise failure

    monkeypatch.setattr("deepsleep_loop.mastery_store.load_mastery", fail_load)
    with pytest.raises(RuntimeError, match="prepared mastery lookup failed") as raised:
        pm.mint_for_prepared_apply()
    assert raised.value is failure


def test_mint_respects_kill_switch(monkeypatch):
    _ready_world(monkeypatch, {"asyncio-basics": _rec("asyncio-basics")})
    monkeypatch.setenv("OMEGA_PRACTICE_ENABLED", "0")
    assert pm.ensure_practice_objective() is False


def test_mint_picks_neediest_domain(monkeypatch):
    _ready_world(
        monkeypatch,
        {
            "zebra-topic": _rec("zebra-topic", application=0.0),
            "alpha-topic": _rec("alpha-topic", application=0.4),
        },
    )
    assert pm.ensure_practice_objective() is True
    assert current_build_objective().practice_domain == "zebra-topic"


def test_mind_driven_mint_backs_the_wake_choice(monkeypatch):
    """Idle-awakening EXECUTE follow-through (2026-07-10): when the mind's
    held apply choice names a ready domain, the mint practices THAT domain
    with the mind's specific topic — not just the best-known default."""
    _ready_world(
        monkeypatch,
        {
            "python": _rec("python", knowledge=0.6),  # the mind's choice
            "asyncio-basics": _rec("asyncio-basics", knowledge=0.9),  # best-known default
        },
    )
    pm.set_apply_choice("python", "File I/O and Error Handling")
    assert pm.mint_for_prepared_apply() is True
    objective = current_build_objective()
    assert objective.practice_domain == "python"
    assert "File I/O and Error Handling" in objective.objective
    assert pm.take_apply_choice() is None  # one-shot: consumed by the mint


def test_mind_driven_mint_falls_back_when_choice_not_ready(monkeypatch):
    _ready_world(
        monkeypatch,
        {
            "asyncio-basics": _rec("asyncio-basics", knowledge=0.9),
        },
    )
    pm.set_apply_choice("cold-domain", "anything")  # not in the ready set
    assert pm.mint_for_prepared_apply() is True
    assert current_build_objective().practice_domain == "asyncio-basics"


def test_fresh_mint_sweeps_workspace_leftovers(monkeypatch, _fresh):
    """A fresh objective starts in a fresh sandbox: the mint sweeps leftovers
    (a build that never hit a deterministic ending) before holding the objective."""
    _ready_world(monkeypatch, {"asyncio-basics": _rec("asyncio-basics")})
    assert pm.ensure_practice_objective() is True
    assert len(_fresh) == 1  # sweep ran exactly once, before the stash


def test_is_apply_ready_enforces_the_practice_bar(monkeypatch):
    _ready_world(
        monkeypatch,
        {
            "python": _rec("python", knowledge=0.35),
            "caching": _rec("caching", knowledge=0.6),
        },
    )
    assert pm.is_apply_ready("python") is False  # under the 0.5 bar
    assert pm.is_apply_ready("caching") is True
    assert pm.is_apply_ready("unknown") is False


def test_grounding_raises_application_mastery(monkeypatch, tmp_path):
    from deepsleep_loop.mastery_store import load_mastery, upsert_mastery

    store_path = tmp_path / "mastery.json"
    monkeypatch.setattr("pressure_loop.policy.deepsleep_mastery_path", lambda: store_path)
    upsert_mastery(_rec("asyncio-basics", application=0.2), path=store_path)

    new_val = pm.apply_practice_success("asyncio-basics", episode_id="ep-practice")
    assert new_val == pytest.approx(0.4)
    rec = load_mastery(store_path)["asyncio-basics"]
    assert rec.application_mastery == pytest.approx(0.4)
    assert rec.evidence_count == 4
    assert rec.knowledge_mastery == pytest.approx(0.8), "knowledge untouched"
    assert rec.last_practice_episode == "ep-practice"


def test_grounding_is_idempotent_for_one_episode(monkeypatch, tmp_path):
    from deepsleep_loop.mastery_store import load_mastery, upsert_mastery

    store_path = tmp_path / "mastery.json"
    monkeypatch.setattr("pressure_loop.policy.deepsleep_mastery_path", lambda: store_path)
    upsert_mastery(_rec("asyncio-basics", application=0.2), path=store_path)

    assert pm.apply_practice_success("asyncio-basics", episode_id="ep-practice") == pytest.approx(
        0.4
    )
    assert pm.apply_practice_success("asyncio-basics", episode_id="ep-practice") == pytest.approx(
        0.4
    )

    rec = load_mastery(store_path)["asyncio-basics"]
    assert rec.application_mastery == pytest.approx(0.4)
    assert rec.evidence_count == 4


def test_grounding_unknown_or_empty_domain_fails_loudly(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "pressure_loop.policy.deepsleep_mastery_path",
        lambda: tmp_path / "mastery.json",
    )
    with pytest.raises(KeyError, match="never-seeded"):
        pm.apply_practice_success("never-seeded", episode_id="ep-unknown")
    with pytest.raises(ValueError, match="domain"):
        pm.apply_practice_success("", episode_id="ep-empty")
