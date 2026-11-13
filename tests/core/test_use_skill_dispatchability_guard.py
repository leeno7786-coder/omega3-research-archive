"""USE_SKILL dispatchability guard — no road to a doomed dispatch.

Day-one live finding (operator 2026-07-08, episode 75dbdbd9): the mind's
PREPARED intent said APPLY on a substrate with nothing learned and
nothing prepared; the override honored it, APPLY canonicalized to
USE_SKILL, and skill selection died with missing_skill_pack — the third
member of the null-execution family (after execution_gap and the
queued-input loop). Opener-specific gates can't cover a model-prepared
intent, so the cure moves to the CHOKEPOINT: at intent resolution,
USE_SKILL from a NON-operator source with NO SRAM objective is
undispatchable BY CONSTRUCTION and downgrades to IDLE. Operator
turns always pass (their message IS the objective); a held build or
minted practice objective always passes.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import importlib  # noqa: E402

from memory import episode_sram  # noqa: E402
from memory.build_objective import (  # noqa: E402
    BuildObjective,
    current_build_objective,
    hold_build_objective,
)

# the intent package re-exports the run() FUNCTION under the module's name;
# resolve the module itself explicitly
intent_run = importlib.import_module("global_loop.intent.run")


@pytest.fixture(autouse=True)
def _fresh():
    episode_sram.reset()
    episode_sram.open_bucket("ep-guard-test", pipeline_id=0)
    yield
    episode_sram.reset()


def _hold_objective(objective: str, origin: str) -> None:
    hold_build_objective(
        BuildObjective(
            episode_id="ep-guard-test",
            objective=objective,
            origin_opened_by=origin,
        )
    )


def _ignition(
    opened_by: str, *, source: str = "autonomous", directive: str = "", message: str = ""
) -> SimpleNamespace:
    md = {"message": message}
    if directive:
        md["directive_intent"] = directive
    return SimpleNamespace(
        opened_by=opened_by,
        dominant_contributor=SimpleNamespace(source=source, metadata=md),
        aggregate_pressure=0.4,
        episode_id="ep-guard-test",
    )


def test_execution_gap_with_held_objective_still_dispatches(monkeypatch):
    _hold_objective("resume the parser build", "operator_query")
    assert intent_run.run(_ignition("execution_gap")) == "USE_SKILL"


def test_execution_gap_without_objective_downgrades(monkeypatch):
    monkeypatch.setattr("pressure_loop.practice_mint.mint_for_prepared_apply", lambda: False)
    assert intent_run.run(_ignition("execution_gap")) == "IDLE"


def test_application_gap_without_practice_objective_downgrades(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "pressure_loop.practice_mint.ensure_practice_objective",
        lambda: calls.append("ensure") or False,
    )
    monkeypatch.setattr(
        "pressure_loop.practice_mint.mint_for_prepared_apply",
        lambda: calls.append("prepared_apply") or False,
    )

    assert intent_run._guard_use_skill("USE_SKILL", _ignition("application_gap")) == "IDLE"
    assert calls == ["ensure", "prepared_apply"]


def test_application_gap_practice_mint_exception_propagates(monkeypatch):
    failure = RuntimeError("practice objective lookup failed")

    def raise_failure():
        raise failure

    monkeypatch.setattr(
        "pressure_loop.practice_mint.ensure_practice_objective",
        raise_failure,
    )
    monkeypatch.setattr(
        "pressure_loop.practice_mint.mint_for_prepared_apply",
        lambda: pytest.fail("prepared-apply mint must not mask the failed application mint"),
    )

    with pytest.raises(RuntimeError, match="practice objective lookup failed") as raised:
        intent_run._guard_use_skill("USE_SKILL", _ignition("application_gap"))
    assert raised.value is failure


def test_prepared_apply_mint_exception_propagates(monkeypatch):
    failure = RuntimeError("prepared APPLY mint failed")

    def raise_failure():
        raise failure

    monkeypatch.setattr(
        "pressure_loop.practice_mint.mint_for_prepared_apply",
        raise_failure,
    )

    with pytest.raises(RuntimeError, match="prepared APPLY mint failed") as raised:
        intent_run._guard_use_skill("USE_SKILL", _ignition("execution_gap"))
    assert raised.value is failure


def test_operator_command_stays_opaque_without_held_objective(monkeypatch):
    intent = intent_run.run(
        _ignition(
            "operator_command", source="operator", message="build me a rate limiter and test it"
        )
    )
    assert intent == "RESPOND", "AUTO must interpret the operator's opaque words"
